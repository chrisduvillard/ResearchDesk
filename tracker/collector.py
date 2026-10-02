import fcntl
from contextlib import closing
import json
import logging
import os
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from . import db
from .calendar import schedule
from .history import ingest
from .parser import SOURCE_URL
from .prices import refresh
from .export import pine_export, csv_export

log = logging.getLogger(__name__)


def backup(conn, now=None):
    # CNBC and DBMF can finish together. Serialize creation/rotation of the
    # shared daily backup; each archive includes one consistent DB snapshot.
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (db.DATA_DIR / 'backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _backup(conn, now)


def _backup(conn, now=None, target=None):
    now = now or db.utcnow()
    directory = db.DATA_DIR / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = target or directory / f"{now.date().isoformat()}.tar.gz"
    with tempfile.TemporaryDirectory(dir=directory) as work:
        import sqlite3
        dump = Path(work) / "tracker.sqlite3"
        with closing(sqlite3.connect(dump)) as dest:
            conn.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
            tables={r[0] for r in dest.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'sessions' in tables:
                dest.execute('DELETE FROM sessions')
                from secrets import token_hex
                dest.execute("UPDATE settings SET value=? WHERE key='activity_epoch'", (json.dumps(token_hex(16)),))
                dest.commit()
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup database integrity check failed")
        temp = Path(work) / "backup.tar.gz"
        with tarfile.open(temp, "w:gz") as archive:
            archive.add(dump, arcname="tracker.sqlite3")
            for name in ("snapshots", "exports", "dbmf", "sources", "funds"):
                path = db.DATA_DIR / name
                if path.exists():
                    archive.add(path, arcname=name)
        os.replace(temp, target)
    for old in sorted(directory.glob("*.tar.gz"))[:-30]:
        old.unlink()
    return target


def write_exports(conn):
    directory = db.DATA_DIR / 'exports'
    directory.mkdir(exist_ok=True)
    payloads = {f"{row['symbol']}.txt": pine_export(conn, row['symbol']) for row in
                conn.execute('SELECT symbol FROM instruments WHERE tv_symbol IS NOT NULL AND symbol IN (SELECT symbol FROM events)')}
    payloads['history.csv'] = csv_export(conn)
    for name, text in payloads.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
            os.replace(temporary, directory / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def collect():
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (db.DATA_DIR / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        with db.database() as conn:
            db.initialize(conn)
            started = db.utcnow()
            with conn:
                conn.execute("UPDATE runs SET status='interrupted',finished_at=?,error='Collector restarted before completion' WHERE status='running'", (db.iso(started),))
                run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES(?,'running')", (db.iso(started),)).lastrowid
            outcome = dict(status="error", error=None, price_errors={})
            snapshot_id = None
            try:
                request = Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; DanNathanDisclosureTracker/1.0; personal research)", "Cache-Control": "no-cache"})
                with urlopen(request, timeout=40) as response:
                    if response.status != 200:
                        raise ValueError(f"CNBC HTTP {response.status}")
                    html = response.read(5_000_001)
                    if len(html) > 5_000_000:
                        raise ValueError("CNBC response exceeds the expected size")
                # Availability time is recorded when the response has arrived.
                snapshot = ingest(conn, html.decode("utf-8"), db.utcnow())
                snapshot_id = snapshot["id"]
                outcome["snapshot"] = snapshot
                if snapshot["status"] != "accepted":
                    outcome["error"] = snapshot["error"]
                outcome["status"] = "success" if snapshot["status"] == "accepted" else "error"
            except Exception as exc:
                log.exception("Disclosure collection failed")
                outcome["error"] = str(exc)[:1000]
            # Price maintenance and exports also run when CNBC is unavailable.
            try:
                outcome['price_errors'] = refresh(conn)
            except Exception as exc:
                log.exception('Price refresh failed')
                outcome['price_errors'] = {'provider': str(exc)[:500]}
            if outcome['price_errors'] and outcome['status'] == 'success':
                outcome['status'] = 'partial'
            try:
                write_exports(conn)
            except Exception as exc:
                log.exception('Export failed')
                outcome['error'] = '; '.join(filter(None, [outcome['error'], str(exc)[:1000]]))
                if outcome['status'] == 'success':
                    outcome['status'] = 'partial'
            with conn:
                conn.execute("UPDATE runs SET finished_at=?,status=?,error=?,snapshot_id=?,price_errors=? WHERE id=?", (db.iso(), outcome["status"], outcome["error"], snapshot_id, json.dumps(outcome["price_errors"]), run_id))
            try:
                outcome["backup"] = str(backup(conn))
            except Exception as exc:
                log.exception("Backup failed")
                with conn:
                    db.set_setting(conn, "backup_error", str(exc))
            else:
                with conn:
                    db.set_setting(conn, "backup_error", None)
                    db.set_setting(conn, "last_backup", db.iso())
            log.info("Collection complete: %s", json.dumps(outcome))
            return outcome


def is_due(conn, now):
    due, _ = schedule(now)
    runs = conn.execute("SELECT * FROM runs WHERE started_at>=? ORDER BY id", (db.iso(due),)).fetchall()
    if any(row["status"] == "success" for row in runs):
        return False
    if runs and runs[-1]['status'] in ('running', 'interrupted'):
        return True  # The OS file lock protects a concurrently running collector.
    failed = [row for row in runs if row['status'] in ('error', 'partial')]
    if not failed:
        return True
    if len(failed) >= 3:
        return False
    last = failed[-1]
    delay = 5 if len(failed) == 1 else 30
    return datetime.fromisoformat(last["finished_at"] or last["started_at"]) + timedelta(minutes=delay) <= now


def worker():
    log.info("Daily collector starting; schedule 22:15 America/New_York")
    while True:
        try:
            with db.database() as conn:
                db.initialize(conn)
                with conn:
                    db.set_setting(conn, "worker_heartbeat", db.iso())
                run = is_due(conn, db.utcnow())
            if run:
                collect()
        except Exception:
            log.exception("Worker loop failed; retrying in 30 seconds")
        time.sleep(30)


def migration_backup(conn):
    """Rollback archives are outside daily rotation and never overwritten."""
    from secrets import token_hex
    directory=db.DATA_DIR/'pre-migration'
    directory.mkdir(parents=True,exist_ok=True)
    version=conn.execute('PRAGMA user_version').fetchone()[0]
    name=f"schema-{version}-{db.utcnow().strftime('%Y%m%dT%H%M%S')}-{token_hex(4)}.tar.gz"
    with (db.DATA_DIR/'backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _backup(conn,target=directory/name)
