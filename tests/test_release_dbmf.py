from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
from pathlib import Path

import pytest

from tracker import db
from tracker.dbmf import recovery, store
from tracker.dbmf.catalog import import_mapping
from tracker.dbmf.changes import changes, compare_revisions, revision_pairs


RAW = (Path(__file__).parent / 'fixtures/dbmf/live.html').read_text()
NOW = datetime.fromisoformat('2026-10-01T22:30:00-04:00')


@pytest.mark.parametrize('interval', [timedelta(minutes=1), timedelta(0)])
def test_replay_respects_later_duplicate_acquisition(database, interval):
    first = store.ingest(database, RAW, NOW)
    original = dict(database.execute('SELECT * FROM dbmf_reports WHERE id=?', (first['id'],)).fetchone())
    rejected = store.ingest(database, RAW.replace('US 2YR NOTE (CBT) DEC26', 'NEW SILVER CONTRACT DEC26'), NOW + interval)
    repeated = store.ingest(database, '<!-- fetched again -->' + RAW, NOW + interval * 2)
    assert repeated['duplicate'] and repeated['id'] == first['id']
    import_mapping(database, dict(
        market=dict(id='silver', name='Silver', category='Commodities', value_basis='signed_notional'),
        aliases=['NEW SILVER CONTRACT'], source_url='https://www.imgp.com/reviewed-source',
        reason='Verified fixture contract and signed notional field'))

    assert recovery.replay_pending()['recovered'] == 1
    current = store.exposure_response(database)['current']
    assert current['id'] == first['id']
    assert next(m for m in current['markets'] if m['id'] == 'silver')['absent']
    assert dict(database.execute('SELECT * FROM dbmf_reports WHERE id=?', (first['id'],)).fetchone()) == original
    assert database.execute("SELECT count(*) FROM dbmf_reports WHERE status='accepted'").fetchone()[0] == 2
    summary = changes(database, baseline_id=first['id'], since_id=rejected['id'])
    assert summary['current']['id'] == first['id'] and not summary['items']
    assert summary['new_reports'] == 1
    assert revision_pairs(database)[0]['previous_id'] == first['id']
    observations = database.execute('SELECT * FROM dbmf_observations WHERE report_id=? ORDER BY id', (first['id'],)).fetchall()
    repeated_raw = gzip.decompress((db.DATA_DIR / observations[-1]['raw_path']).read_bytes())
    assert repeated_raw == ('<!-- fetched again -->' + RAW).encode()
    assert hashlib.sha256(repeated_raw).hexdigest() == observations[-1]['raw_hash']
    assert observations[-1]['collected_at'] == db.iso(NOW + interval * 2)
    assert store.ingest(database, RAW, NOW + interval * 3)['duplicate']
    assert database.execute("SELECT status FROM dbmf_reports WHERE id=?", (rejected['id'],)).fetchone()[0] == 'rejected'


def test_existing_reports_migrate_once_during_concurrent_startup(database):
    store.ingest(database, RAW, NOW)
    latest = store.ingest(database, RAW.replace('-5,284,498,755.18', '-5,284,498,755.19'), NOW + timedelta(minutes=1))
    assert not latest['duplicate']
    original = [dict(row) for row in database.execute('SELECT * FROM dbmf_reports ORDER BY id')]
    with database:
        database.execute('DROP VIEW dbmf_observed_reports')
        database.execute('DROP TABLE dbmf_observations')

    def initialize(_):
        with db.database() as conn:
            db.initialize(conn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(initialize, range(2)))
    db.initialize(database)

    assert store.reports(database)[-1]['id'] == latest['id']
    assert [dict(row) for row in database.execute('SELECT * FROM dbmf_reports ORDER BY id')] == original
    assert database.execute('SELECT count(*) FROM dbmf_observations').fetchone()[0] == 2


@pytest.mark.parametrize('run_status', ['success', 'partial'])
def test_legacy_duplicate_run_keeps_later_holdings_after_replay(database, run_status):
    first = store.ingest(database, RAW, NOW)
    rejected = store.ingest(database, RAW.replace('US 2YR NOTE (CBT) DEC26', 'NEW SILVER CONTRACT DEC26'), NOW + timedelta(minutes=1))
    duplicate_raw = '<!-- later fetch -->' + RAW
    repeated = store.ingest(database, duplicate_raw, NOW + timedelta(minutes=2, seconds=30))
    start, finish = db.iso(NOW + timedelta(minutes=2)), db.iso(NOW + timedelta(minutes=3))
    with database:
        run_id = database.execute('''INSERT INTO dbmf_runs
            (started_at,finished_at,status,report_id,raw_path,source_url)
            VALUES(?,?,?,?,?,?)''', (start, finish, run_status, first['id'], repeated['raw_path'], 'https://www.imgp.com/holdings')).lastrowid
        database.execute('DROP VIEW dbmf_observed_reports')
        database.execute('DROP TABLE dbmf_observations')
    db.initialize(database)
    import_mapping(database, dict(
        market=dict(id='silver', name='Silver', category='Commodities', value_basis='signed_notional'),
        aliases=['NEW SILVER CONTRACT'], source_url='https://www.imgp.com/reviewed-source',
        reason='Verified fixture contract and signed notional field'))
    assert recovery.replay_pending()['recovered'] == 1
    assert store.reports(database)[-1]['id'] == first['id']
    observation = database.execute('SELECT * FROM dbmf_observations WHERE run_id=?', (run_id,)).fetchone()
    assert observation['collected_at'] == start
    assert observation['collected_before'] == finish
    assert observation['timestamp_basis'] == 'run_start_lower_bound'
    assert observation['raw_hash'] == hashlib.sha256(duplicate_raw.encode()).hexdigest()
    db.initialize(database)
    assert database.execute('SELECT count(*) FROM dbmf_observations WHERE run_id=?', (run_id,)).fetchone()[0] == 1
    assert database.execute('SELECT collected_at FROM dbmf_reports WHERE id=?', (first['id'],)).fetchone()[0] == db.iso(NOW)


def test_replayed_revision_uses_original_acquisition_order_within_a_second(database):
    first = store.ingest(database, RAW, NOW)
    store.ingest(database, RAW.replace('US 2YR NOTE (CBT) DEC26', 'NEW SILVER CONTRACT DEC26'), NOW)
    third = store.ingest(database, RAW.replace('-5,284,498,755.18', '-5,284,498,755.19'), NOW)
    assert not third['duplicate']
    import_mapping(database, dict(
        market=dict(id='silver', name='Silver', category='Commodities', value_basis='signed_notional'),
        aliases=['NEW SILVER CONTRACT'], source_url='https://www.imgp.com/reviewed-source',
        reason='Verified fixture contract and signed notional field'))
    replayed = recovery.replay_pending()['results'][0]['result_report_id']
    assert store.reports(database)[-1]['id'] == third['id']
    assert {row['id']: row['previous_id'] for row in revision_pairs(database)} == {
        replayed: first['id'], third['id']: replayed}
    assert compare_revisions(database, replayed)['previous']['id'] == first['id']
    assert compare_revisions(database, third['id'])['previous']['id'] == replayed
    assert compare_revisions(database, third['id'], replayed)['previous']['id'] == replayed
    with pytest.raises(ValueError):
        compare_revisions(database, replayed, third['id'])


@pytest.mark.parametrize('invalid', ['missing_archive', 'corrupt_archive', 'wrong_hash', 'failed', 'unfinished', 'reversed_bounds'])
def test_unverified_legacy_runs_cannot_replace_current_holdings(database, invalid):
    first = store.ingest(database, RAW, NOW)
    latest = store.ingest(database, RAW.replace('-5,284,498,755.18', '-5,284,498,755.19'), NOW + timedelta(minutes=1))
    path, _ = store.archive(('<!-- legacy -->' + RAW).encode(), 'html')
    archive = db.DATA_DIR / path
    if invalid == 'missing_archive':
        archive.unlink()
    elif invalid == 'corrupt_archive':
        archive.write_bytes(b'not gzip')
    elif invalid == 'wrong_hash':
        archive.write_bytes(gzip.compress(b'changed payload'))
    start, finish = db.iso(NOW + timedelta(minutes=2)), db.iso(NOW + timedelta(minutes=3))
    if invalid == 'reversed_bounds':
        start, finish = finish, start
    with database:
        run_id = database.execute('''INSERT INTO dbmf_runs
            (started_at,finished_at,status,report_id,raw_path) VALUES(?,?,?,?,?)''',
            (start, None if invalid == 'unfinished' else finish,
             'error' if invalid == 'failed' else 'success', first['id'], path)).lastrowid
    db.initialize(database)
    assert store.reports(database)[-1]['id'] == latest['id']
    assert not database.execute('SELECT 1 FROM dbmf_observations WHERE run_id=?', (run_id,)).fetchone()


def test_modern_run_keeps_exact_acquisition_without_legacy_import(database):
    first = store.ingest(database, RAW, NOW)
    repeated = store.ingest(database, '<!-- modern repeat -->' + RAW, NOW + timedelta(minutes=2, seconds=30))
    with database:
        database.execute('''INSERT INTO dbmf_runs
            (started_at,finished_at,status,report_id,raw_path) VALUES(?,?,?,?,?)''',
            (db.iso(NOW + timedelta(minutes=2)), db.iso(NOW + timedelta(minutes=3)),
             'success', first['id'], repeated['raw_path']))
    db.initialize(database)
    assert database.execute('SELECT count(*) FROM dbmf_observations').fetchone()[0] == 2
    current = store.reports(database)[-1]
    assert current['last_observed_at'] == db.iso(NOW + timedelta(minutes=2, seconds=30))
    assert current['observation_time_basis'] == 'acquisition'
    assert current['last_observed_before'] is None


def test_legacy_lower_bound_cannot_outrank_exact_acquisition_at_same_time(database):
    first = store.ingest(database, RAW, NOW)
    at = NOW + timedelta(minutes=1)
    rejected = store.ingest(database, RAW.replace('US 2YR NOTE (CBT) DEC26', 'NEW SILVER CONTRACT DEC26'), at)
    path, _ = store.archive(('<!-- unknown fetch time inside run -->' + RAW).encode(), 'html')
    with database:
        database.execute('''INSERT INTO dbmf_runs
            (started_at,finished_at,status,report_id,raw_path) VALUES(?,?,?,?,?)''',
            (db.iso(at), db.iso(at + timedelta(seconds=5)), 'success', first['id'], path))
    db.initialize(database)
    import_mapping(database, dict(
        market=dict(id='silver', name='Silver', category='Commodities', value_basis='signed_notional'),
        aliases=['NEW SILVER CONTRACT'], source_url='https://www.imgp.com/reviewed-source',
        reason='Verified fixture contract and signed notional field'))
    recovery.replay_pending()
    assert store.reports(database)[-1]['replayed_from'] == rejected['id']
