"""Inspect rejected holdings and recover archived reports after reviewed changes."""
import fcntl
import gzip
import hashlib
import json
import zlib
from datetime import datetime

from .. import db
from . import PARSER_VERSION
from .catalog import Catalog
from .markets import normalize_name
from .store import ingest


def pending_review(conn):
    rows = conn.execute('''SELECT r.*,a.result_report_id FROM dbmf_reports r
        LEFT JOIN dbmf_replay_attempts a ON a.id=(SELECT max(id) FROM dbmf_replay_attempts WHERE source_report_id=r.id)
        WHERE r.status='rejected' AND r.replayed_from IS NULL
        AND NOT EXISTS(SELECT 1 FROM dbmf_replay_attempts WHERE source_report_id=r.id AND status='accepted')
        ORDER BY r.collected_at,r.id''').fetchall()
    items, pending = {}, 0
    for row in rows:
        effective = conn.execute('SELECT * FROM dbmf_reports WHERE id=?', (row['result_report_id'],)).fetchone() if row['result_report_id'] else row
        unknown = json.loads(effective['metadata_json']).get('unmapped_holdings', [])
        if not unknown:
            continue
        pending += 1
        for holding in unknown:
            key = normalize_name(holding['original_name'])
            prior = items.get(key, {})
            items[key] = dict(normalized_name=key, original_name=holding['original_name'],
                              report_id=row['id'], reporting_date=effective['source_date'],
                              last_seen=row['collected_at'], first_seen=prior.get('first_seen', row['collected_at']),
                              occurrences=prior.get('occurrences', 0) + 1,
                              ticker=holding['ticker'], identifier=holding['identifier'],
                              reported_value=holding['notional'], value_pct=holding['exposure_pct'])
    return dict(pending_reports=pending, unmapped_names=len(items), items=list(items.values()))


def replay_pending(*, force=False, limit=20):
    """Once per parser/catalog version, under the same lock as live collection.

    Original fetch dates and rejection records stay intact. Failed replay rows
    cannot become new replay candidates, preventing an endless retry chain.
    """
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (db.DATA_DIR / 'dbmf-collector.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='busy', attempted=0, recovered=0)
        with db.database() as conn:
            db.initialize(conn)
            catalog = Catalog(conn)
            version = PARSER_VERSION + '/' + catalog.version
            candidates = conn.execute('''SELECT r.* FROM dbmf_reports r
                WHERE r.status='rejected' AND r.replayed_from IS NULL
                AND NOT EXISTS(SELECT 1 FROM dbmf_replay_attempts WHERE source_report_id=r.id AND status='accepted')
                AND (? OR ((r.parser_version!=? OR r.mapping_version!=?) AND NOT EXISTS
                    (SELECT 1 FROM dbmf_replay_attempts WHERE source_report_id=r.id AND adapter_version=?)))
                ORDER BY r.collected_at,r.id LIMIT ?''',
                (force, PARSER_VERSION, catalog.version, version, limit)).fetchall()
            results = []
            for row in candidates:
                result, error = None, None
                try:
                    raw = gzip.decompress((db.DATA_DIR / row['raw_path']).read_bytes())
                    if hashlib.sha256(raw).hexdigest() != row['raw_hash']:
                        raise ValueError('Archived report checksum mismatch')
                    metadata = json.loads(row['metadata_json'])
                    result = ingest(conn, raw, datetime.fromisoformat(row['collected_at']),
                                    source_url=row['source_url'], kind=row['source_kind'],
                                    expected_date=metadata.get('expected_date'), replayed_from=row['id'])
                    error = result.get('error')
                except (OSError, EOFError, ValueError, zlib.error) as exc:
                    error = str(exc)[:1500]
                recovered = bool(result and result['status'] == 'accepted')
                with conn:
                    conn.execute('''INSERT INTO dbmf_replay_attempts
                        (source_report_id,result_report_id,adapter_version,attempted_at,status,error)
                        VALUES(?,?,?,?,?,?)''',
                        (row['id'], result['id'] if result else None, version, db.iso(),
                         'accepted' if recovered else 'rejected', error))
                results.append(dict(source_report_id=row['id'], result_report_id=result['id'] if result else None,
                                    recovered=recovered, error=error))
            summary = dict(status='complete', attempted=len(results), recovered=sum(item['recovered'] for item in results),
                           checked_at=db.iso(), adapter_version=version, results=results)
            if results:
                with conn:
                    db.set_setting(conn, 'dbmf_last_replay', summary)
                from ..collector import backup
                try:
                    backup(conn)
                    with conn:
                        db.set_setting(conn, 'last_backup', db.iso())
                        db.set_setting(conn, 'backup_error', None)
                except Exception as exc:
                    with conn:
                        db.set_setting(conn, 'backup_error', str(exc)[:1000])
            return summary
