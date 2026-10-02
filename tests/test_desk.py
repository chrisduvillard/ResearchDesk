from datetime import timedelta
from decimal import Decimal, localcontext

from fastapi.testclient import TestClient

from tracker import db
from tracker.api import app
from tracker.desk import health_view
from tracker.history import ingest, review
from tracker.dbmf import store
from tracker.dbmf.changes import changes, compare_revisions, revision_pairs
from conftest import page, moment
from test_dbmf import live, changed, import_history, NOW


def test_disclosure_digest_repeats_rejections_visits_and_corrections(database):
    ingest(database, page(), moment(), resolve=False)
    with TestClient(app) as client:
        first = client.get('/api/changes').json()
        assert len(first['items']) == 5
        assert all(e['kind'] == 'baseline' for e in first['items'])
        cursor = first['cursor']['after_id']
        ingest(database, page(), moment() + timedelta(hours=1), resolve=False)
        assert client.get('/api/changes').json()['items'] == []
        ingest(database, 'malformed', moment() + timedelta(hours=2), resolve=False)
        assert client.get('/api/changes').json()['items'] == []
        ingest(database, page('Dan Nathan is long MSFT.'), moment() + timedelta(days=1), resolve=False)
        result = client.get(f'/api/changes?after_id={cursor}').json()
        assert {e['kind'] for e in result['items']} == {'removed', 'direction_changed'}
        assert len(result['items']) == 5
        after = result['cursor']['after_id']
        review(database, 'MSFT', 'unknown', 'Uncertain wording', moment() + timedelta(days=1, hours=1))
        assert client.get(f'/api/changes?after_id={after}').json()['items'][0]['kind'] == 'correction'
        assert client.get('/api/changes?after_id=-1').status_code == 422


def test_dbmf_digest_last_seen_revision_old_import_and_absent_markets(database):
    old = store.ingest(database, live(), NOW)
    revised = store.ingest(database, changed(remove='GOLD'), NOW + timedelta(hours=1))
    result = changes(database, old['id'], old['id'])
    gold = next(m for m in result['items'] if m['id'] == 'gold')
    assert gold['kind'] == 'absent' and Decimal(gold['after_pct']) == 0
    assert result['comparison']['id'] == old['id']
    assert result['current']['id'] == revised['id']
    assert result['revisions'][0]['previous_id'] == old['id']
    # A backfill adds evidence but doesn't change the last-seen portfolio baseline.
    import_history(database, '2025-09-30')
    result = changes(database, revised['id'], revised['id'])
    assert result['items'] == [] and result['new_reports'] == 1
    assert result['current']['id'] == revised['id']
    store.ingest(database, live().replace('</table>', ''), NOW + timedelta(hours=2))
    assert changes(database, revised['id'], revised['id'])['items'] == []


def test_revision_diff_exact_values_reordering_nav_and_not_trade_dates(database):
    old = store.ingest(database, live(), NOW)
    revised = store.ingest(database, changed(change=('tbody .market_value', '-5284498755.19')), NOW + timedelta(hours=1))
    diff = compare_revisions(database, revised['id'])
    assert diff['origin'] == 'source_changed'
    assert diff['previous']['id'] == old['id'] and diff['changed_rows'] == 1
    row = diff['rows'][0]
    assert row['fields'] == ['notional', 'exposure_pct']
    assert row['before']['notional'] == '-5284498755.18'
    assert row['after']['notional'] == '-5284498755.19'
    with localcontext() as ctx:
        ctx.prec = 50
        assert Decimal(diff['markets'][0]['change_pp']) == Decimal(row['after']['exposure_pct']) - Decimal(row['before']['exposure_pct'])
    nav_revision = store.ingest(database, live().replace('5,243,820,755.56', '5,243,820,756.56'), NOW + timedelta(hours=2))
    diff = compare_revisions(database, nav_revision['id'])
    assert diff['net_assets_changed'] and diff['changed_rows'] == 20


def test_revision_same_source_reprocessing_and_added_removed_rows(database):
    first = store.ingest(database, live(), NOW)
    second = store.ingest(database, changed(remove='GOLD'), NOW + timedelta(hours=1))
    diff = compare_revisions(database, second['id'])
    assert len(diff['rows']) == 1 and diff['rows'][0]['kind'] == 'removed'
    # Simulate the metadata of a reviewed reprocessing result, independent of
    # revision comparison: unchanged raw bytes must never imply issuer activity.
    with database:
        database.execute('UPDATE dbmf_reports SET raw_hash=(SELECT raw_hash FROM dbmf_reports WHERE id=?),parser_version=? WHERE id=?', (first['id'], 'reviewed-parser', second['id']))
    assert compare_revisions(database, second['id'])['origin'] == 'reprocessed'


def test_revision_orders_by_acquisition_and_restricts_date_and_source(database):
    first = store.ingest(database, live(), NOW)
    newest = store.ingest(database, changed(remove='GOLD'), NOW + timedelta(hours=3))
    middle = store.ingest(database, changed(remove='EURO'), NOW + timedelta(hours=1))
    assert compare_revisions(database, newest['id'])['previous']['id'] == middle['id']
    assert compare_revisions(database, middle['id'])['previous']['id'] == first['id']
    other = import_history(database, '2025-09-30')
    with TestClient(app) as client:
        assert client.get(f'/api/dbmf/revisions/{newest["id"]}?against_id={other["id"]}').status_code == 400
        assert client.get(f'/api/dbmf/revisions/{first["id"]}?against_id={newest["id"]}').status_code == 400
        assert client.get('/api/dbmf/revisions/999').status_code == 404
        assert client.get('/api/dbmf/changes?baseline_id=999').status_code == 404
        listing = client.get('/api/dbmf/revisions?limit=1').json()
        assert listing['has_more'] and len(listing['items']) == 1
        older = client.get('/api/dbmf/revisions?before_id=' + str(listing['items'][0]['id'])).json()
        assert len(older['items']) == 1


def _run(conn, when, status='success', *, dbmf=False, report_id=None):
    table = 'dbmf_runs' if dbmf else 'runs'
    keys = 'started_at,finished_at,status,price_errors'
    values = [db.iso(when), db.iso(when), status, '{}']
    if dbmf:
        keys += ',report_id'
        values.append(report_id)
    with conn:
        conn.execute(f'INSERT INTO {table}({keys}) VALUES({",".join("?" for _ in values)})', values)


def test_health_separates_source_check_price_dates_and_retries(database):
    now = moment('2026-10-02T06:00:00-04:00')
    ingest(database, page('Dan Nathan is long MSFT.'), now - timedelta(hours=1), resolve=False)
    report = store.ingest(database, live(), now)
    for key in ('worker_heartbeat', 'dbmf_worker_heartbeat'):
        db.set_setting(database, key, db.iso(now))
    database.commit()
    _run(database, now)
    _run(database, now, dbmf=True, report_id=report['id'])
    health = health_view(database, now)
    dan, dbmf = health['desks']
    assert dan['source_date'] == '2026-09-30T20:30:00+00:00'
    assert dan['last_collected'] == db.iso(now - timedelta(hours=1))
    assert dbmf['source_date'] == '2026-10-01' and dbmf['last_collected'] == db.iso(now)
    assert dan['prices'][0]['stale']
    _run(database, now, 'partial')
    issue = next(i for i in health_view(database, now)['issues'] if i['key'] == 'dan:collection')
    assert not issue['notify']
    for _ in range(2):
        _run(database, now, 'error')
    assert next(i for i in health_view(database, now)['issues'] if i['key'] == 'dan:collection')['notify']
    _run(database, now, 'running')
    assert next(i for i in health_view(database, now)['issues'] if i['key'] == 'dan:collection')['notify']
    _run(database, now, 'success')
    assert not any(i['key'] == 'dan:collection' for i in health_view(database, now)['issues'])


def test_health_equity_holiday_weekend_and_retry_grace(database):
    now = moment('2026-09-07T23:30:00-04:00')  # Labor Day; previous exchange day is Friday.
    ingest(database, page('Dan Nathan is long MSFT.', 'Disclosures as of 9/4/26 (4:30 PM ET):'), now, resolve=False)
    with database:
        database.execute("INSERT INTO prices(symbol,date,open,high,low,close,complete,fetched_at,provider) VALUES('MSFT','2026-09-04',1,1,1,1,1,?,'test')", (db.iso(now),))
    price = health_view(database, now)['desks'][0]['prices'][0]
    assert price['expected_date'] == '2026-09-04' and not price['stale']
    tuesday = moment('2026-09-08T22:20:00-04:00')
    assert not health_view(database, tuesday)['desks'][0]['prices'][0]['stale']
    assert health_view(database, tuesday + timedelta(hours=1))['desks'][0]['prices'][0]['stale']


def test_unknown_review_alert_and_new_routes_are_read_only(database):
    store.ingest(database, live(), NOW)
    store.ingest(database, live().replace('GOLD 100 OZ DEC26', 'MYSTERY ASSET DEC26'), NOW)
    # Fixture names can evolve; derive the unfamiliar contract from the first row.
    store.ingest(database, live().replace('US 2YR NOTE (CBT) DEC26', 'MYSTERY ASSET DEC26'), NOW)
    before = database.execute('SELECT count(*) FROM dbmf_reports').fetchone()[0]
    with TestClient(app) as client:
        for path in ('/', '/dbmf', '/static/desk.js'):
            assert client.get(path).headers['cache-control'] == 'no-cache'
        health = client.get('/api/desk/health').json()
        assert any(i['key'] == 'dbmf:review' and i['notify'] for i in health['issues'])
        for path in ['/api/changes', '/api/dbmf/changes', '/api/dbmf/revisions', '/api/dbmf/revisions/1']:
            assert client.get(path).status_code == 200
            assert client.post(path, json={}).status_code == 405
    assert database.execute('SELECT count(*) FROM dbmf_reports').fetchone()[0] == before
