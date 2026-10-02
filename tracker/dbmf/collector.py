import fcntl
import json
import logging
import time as clock
from datetime import datetime, time, timedelta
from urllib.request import Request, urlopen

from .. import db
from ..calendar import NY
from ..collector import backup
from . import SOURCE_URL
from .store import ingest
from .prices import refresh

log = logging.getLogger(__name__)


def schedule(now):
    day = now.astimezone(NY).date()
    slots = [datetime.combine(day + timedelta(days=offset), at, NY)
             for offset in (-1, 0, 1) for at in (time(10), time(22, 30))]
    return max(slot for slot in slots if slot <= now), min(slot for slot in slots if slot > now)


def is_due(conn, now):
    due, _ = schedule(now)
    rows = conn.execute("SELECT * FROM dbmf_runs WHERE slot=? AND kind='live' ORDER BY id", (db.iso(due),)).fetchall()
    if any(row['status'] == 'success' for row in rows):
        return False
    # An interrupted process gets another chance; a still-running one is guarded
    # by flock. Failures have at most three attempts in each scheduled slot.
    if rows and rows[-1]['status'] in ('running', 'interrupted'):
        return True
    failed = [row for row in rows if row['status'] in ('error', 'partial')]
    if len(failed) >= 3:
        return False
    if not failed:
        return True
    delay = 5 if len(failed) == 1 else 30
    return now >= datetime.fromisoformat(failed[-1]['finished_at'] or failed[-1]['started_at']) + timedelta(minutes=delay)


def download(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; DBMFHoldingsTracker/1.0; personal research)', 'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=40) as response:
        data = response.read(30_000_001)
        if len(data) > 30_000_000:
            raise ValueError('Report exceeds 30 MB limit')
        return data


def collect(*, prices=True):
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (db.DATA_DIR / 'dbmf-collector.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='busy')
        with db.database() as conn:
            db.initialize(conn)
            now = db.utcnow()
            with conn:
                conn.execute("UPDATE dbmf_runs SET status='interrupted',finished_at=?,error='DBMF collector restarted before completion' WHERE status='running' AND kind='live'", (db.iso(now),))
                run_id = conn.execute("INSERT INTO dbmf_runs(started_at,slot,status,source_url) VALUES(?,?,'running',?)", (db.iso(now), db.iso(schedule(now)[0]), SOURCE_URL)).lastrowid
                db.set_setting(conn, 'dbmf_worker_heartbeat', db.iso(now))
            outcome = dict(status='error', error=None, price_errors={})
            report = None
            try:
                report = ingest(conn, download(SOURCE_URL), db.utcnow())
                if report['status'] != 'accepted':
                    outcome['error'] = report['error']
                outcome['status'] = 'success' if report['status'] == 'accepted' else 'error'
            except Exception as exc:
                log.exception('DBMF holdings collection failed')
                outcome['error'] = str(exc)[:1500]
            # Refresh prices independently, even if the issuer is unavailable.
            if prices:
                try:
                    outcome['price_errors'] = refresh(conn)
                    if outcome['price_errors'] and outcome['status'] == 'success':
                        outcome['status'] = 'partial'
                except Exception as exc:
                    outcome['price_errors'] = {'provider': str(exc)[:700]}
                    if outcome['status'] == 'success':
                        outcome['status'] = 'partial'
            with conn:
                conn.execute('''UPDATE dbmf_runs SET finished_at=?,status=?,error=?,report_id=?,raw_path=?,price_errors=? WHERE id=?''',
                             (db.iso(), outcome['status'], outcome['error'], report['id'] if report else None,
                              report['raw_path'] if report else None, json.dumps(outcome['price_errors']), run_id))
                db.set_setting(conn, 'dbmf_worker_heartbeat', db.iso())
            try:
                backup(conn)
                with conn:
                    db.set_setting(conn, 'last_backup', db.iso())
                    db.set_setting(conn, 'backup_error', None)
            except Exception as exc:
                log.exception('DBMF backup failed')
                with conn:
                    db.set_setting(conn, 'backup_error', str(exc)[:1000])
            outcome.update(run_id=run_id, report=report)
            log.info('DBMF collection: %s', json.dumps(outcome))
            return outcome


def worker():
    log.info('DBMF collector starting: 10:00 and 22:30 America/New_York')
    while True:
        try:
            from .recovery import replay_pending
            replay_pending()
            with db.database() as conn:
                db.initialize(conn)
                with conn:
                    db.set_setting(conn, 'dbmf_worker_heartbeat', db.iso())
                due = is_due(conn, db.utcnow())
            if due:
                collect()
        except Exception:
            log.exception('DBMF worker failed; checking again in 30 seconds')
        clock.sleep(30)
