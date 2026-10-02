"""Failure cases found during the publication review."""
import io
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tracker import collector, db, prices
from tracker.api import app
from tracker.dbmf import store
from tracker.history import current_positions, ingest
from tracker.parser import ParseError, extract
from tracker.strategies import analyze, payoff_for
from conftest import moment, page


def test_price_refresh_cannot_replace_an_interior_session_with_a_new_session(database, monkeypatch):
    ingest(database, page('Dan Nathan is long MSFT.'), moment(), resolve=False)
    frame = pd.DataFrame({'Open':[100.]*3, 'High':[110.]*3, 'Low':[90.]*3, 'Close':[105.]*3},
                         index=pd.to_datetime(['2026-09-28','2026-09-29','2026-09-30']))
    class Ticker:
        def history(self, **kwargs): return frame
    monkeypatch.setattr(prices.yf, 'Ticker', lambda _: Ticker())
    now = moment('2026-10-02T10:00:00-04:00')
    assert prices.refresh(database, now) == {}
    original = [tuple(row) for row in database.execute('SELECT * FROM prices ORDER BY date')]
    frame.index = pd.to_datetime(['2026-09-28','2026-09-30','2026-10-01'])
    assert 'MSFT' in prices.refresh(database, now)
    assert [tuple(row) for row in database.execute('SELECT * FROM prices ORDER BY date')] == original


def test_source_network_failure_still_refreshes_prices_and_preserves_positions(database, monkeypatch):
    ingest(database, page(), moment(), resolve=False)
    refreshed = []
    def unavailable(*args, **kwargs): raise OSError('Source offline')
    monkeypatch.setattr(collector, 'urlopen', unavailable)
    monkeypatch.setattr(collector, 'refresh', lambda conn: refreshed.append(True) or {})
    result = collector.collect()
    assert refreshed == [True]
    assert result['status'] == 'error' and 'Source offline' in result['error']
    assert len(current_positions(database)) == 5
    assert (db.DATA_DIR / 'exports' / 'history.csv').exists()


def test_export_failure_is_not_recorded_as_success(database, monkeypatch):
    response = io.BytesIO(page().encode()); response.status = 200
    monkeypatch.setattr(collector, 'urlopen', lambda *args, **kwargs: response)
    monkeypatch.setattr(collector, 'refresh', lambda conn: {})
    def unavailable(*args, **kwargs): raise OSError('Export disk full')
    monkeypatch.setattr(collector, 'csv_export', unavailable)
    result = collector.collect()
    assert result['status'] == 'partial' and 'Export disk full' in result['error']
    assert database.execute('SELECT status FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0] == 'partial'


def test_interrupted_export_write_keeps_existing_file(database, monkeypatch):
    ingest(database, page(), moment(), resolve=False)
    collector.write_exports(database)
    original = {p.name:p.read_bytes() for p in (db.DATA_DIR/'exports').iterdir()}
    def fail_replace(*args): raise OSError('Interrupted replacement')
    monkeypatch.setattr(collector.os, 'replace', fail_replace)
    with pytest.raises(OSError): collector.write_exports(database)
    assert {p.name:p.read_bytes() for p in (db.DATA_DIR/'exports').iterdir()} == original


def test_api_docs_use_local_assets_with_the_dashboard_security_policy(database):
    from bs4 import BeautifulSoup
    with TestClient(app) as client:
        response = client.get('/docs')
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, 'html.parser')
        assert all(s.get('src', '').startswith('/static/') and not s.string for s in soup.select('script'))
        assert all(a['href'].startswith(('/static/', 'data:')) for a in soup.select('link'))
        policy = response.headers['content-security-policy'].split('script-src ')[1].split(';')[0]
        assert policy == "'self'"
        assert client.get('/redoc').url.path == '/docs'
        assert '/api/dbmf/exposures' in client.get('/openapi.json').json()['paths']


def test_repeated_process_interruptions_do_not_use_failure_retry_budget(database):
    now = moment('2026-10-01T22:16:00-04:00')
    with database:
        for offset in range(3):
            database.execute("INSERT INTO runs(started_at,status) VALUES(?,'interrupted')", (db.iso(now+timedelta(minutes=offset)),))
    assert collector.is_due(database, now+timedelta(minutes=4))


@pytest.mark.parametrize('clock', ['00:30 PM', '13:30 PM', '25:30 AM'])
def test_invalid_source_hours_are_rejected(clock):
    with pytest.raises(ParseError):
        extract(page(header=f'Disclosures as of 9/30/26 ({clock} ET):'))


@pytest.mark.parametrize('wording', [
    'bull call credit spread', 'bear put credit spread',
    'bull put debit spread', 'bear call debit spread',
    '100/110 call credit spread for $3 debit',
    '100/110 bull call spread for $3 credit',
    'covered call straddle', 'synthetic long short stock',
    'protective put butterfly', 'strangle ratio spread',
])
def test_conflicting_option_structures_cannot_create_directional_signals(wording):
    analysis = analyze('long', 'Oct ' + wording)
    assert analysis['direction'] == 'unknown'
    assert not analysis['score_eligible'] and not analysis['payoff_available']


def test_explicit_credit_for_a_vertical_sets_the_leg_orientation():
    analysis = analyze('long', 'Oct 100/110 call spread for $3 credit')
    payoff = payoff_for(analysis)
    assert analysis['direction'] == 'bearish'
    assert (payoff['minimum'], payoff['maximum']) == (-700, 300)


@pytest.mark.parametrize('route', [
    '/api/events?before_id=', '/api/changes?after_id=',
    '/api/snapshots/', '/api/dbmf/exposures?report_id=',
    '/api/dbmf/changes?since_id=', '/api/dbmf/changes?baseline_id=',
    '/api/dbmf/reports?before_id=', '/api/dbmf/reports/',
    '/api/dbmf/revisions?before_id=', '/api/dbmf/revisions/',
])
def test_out_of_range_database_ids_are_client_errors(database, route):
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get(route + str(2**63)).status_code == 422


def test_concurrent_downloads_create_one_dbmf_observation(database):
    # Hold the writer lock until both importers have entered the critical
    # section. A check-then-insert without a write lock creates two reports.
    gates = [threading.Event(), threading.Event()]
    raw = (Path(__file__).parent/'fixtures/dbmf/live.html').read_bytes()
    def run(index):
        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == 'BEGIN IMMEDIATE': gates[index].set()
                cursor = super().execute(sql, parameters)
                if sql.startswith('SELECT * FROM dbmf_reports WHERE'): gates[index].set()
                return cursor
        with sqlite3.connect(db.DATA_DIR/'tracker.sqlite3', timeout=10, factory=Connection) as conn:
            conn.row_factory = sqlite3.Row
            return store.ingest(conn, raw, moment('2026-10-02T02:30:00+00:00'))
    database.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(run, i) for i in range(2)]
        try:
            assert all(gate.wait(5) for gate in gates)
        finally:
            database.commit()
        results = [job.result() for job in jobs]
    assert len({r['id'] for r in results}) == 1
    assert sum(r['duplicate'] for r in results) == 1
    assert database.execute('SELECT count(*) FROM dbmf_reports').fetchone()[0] == 1
