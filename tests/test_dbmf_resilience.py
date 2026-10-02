import gzip
import json
import sqlite3
import tarfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tracker import db
from tracker.api import app
from tracker.collector import backup
from tracker.dbmf import parser, recovery, store
from tracker.dbmf.catalog import Catalog, import_mapping
from tracker.dbmf.markets import UnknownInstrument, identify

FIXTURES = Path(__file__).parent / 'fixtures' / 'dbmf'
NOW = datetime.fromisoformat('2026-10-01T22:30:00-04:00')


def page():
    return BeautifulSoup((FIXTURES / 'live.html').read_text(), 'html.parser')


def unknown_page(two=False):
    soup = page()
    soup.select('tbody .security_name')[0].string = 'NEW SILVER CONTRACT DEC26'
    if two:
        soup.select('tbody .security_name')[1].string = 'NEW COPPER CONTRACT DEC26'
    return str(soup)


def definition(key='silver', aliases=None):
    return dict(market=dict(id=key, name=key.title(), category='Commodities', value_basis='signed_notional'),
                aliases=aliases or [f'NEW {key.upper()} CONTRACT'],
                source_url='https://www.imgp.com/reviewed-source', reason='Verified the new contract and its signed notional field')


@pytest.mark.parametrize('name,expected', [
    (' us 2-year note (CBT) mar2027 ', 'us2y'),
    ('US 10YR NOTE (CBT)\u00a0JUN27', 'us10y'),
    ('U.S. Treasury 10\u2013Year Ultra Note Futures (a)', 'us10ultra'),
    ('U.S. Treasury Ultra\u2011Long Bond Futures', 'usultra'),
    ('gold 100 oz futures DEC 2027', 'gold'),
    ('S&P500 EMINI FUTURES MAR27', 'sp500'),
    ('MSCI EAFE JUN2027', 'eafe'),
    ('US DOLLAR', 'cash'),
])
def test_known_contracts_tolerate_typography_and_new_expiries(name, expected):
    assert identify(name) == expected


@pytest.mark.parametrize('name', ['US 5YR NOTE (CBT) DEC26', 'Gold 10 Oz Futures',
                                  'U.S. Treasury 30-Year Ultra Note Futures', 'Canadian Dollar Currency Futures',
                                  'US 2YR NOTE (CBT) ABC27'])
def test_normalization_keeps_material_instrument_differences(name):
    with pytest.raises(UnknownInstrument):
        identify(name)


def test_live_headers_can_move_and_lose_css_without_changing_values():
    soup = page()
    table = soup.table
    table['id'] = 'issuer-new-holdings-id'
    for cell in table.select('td'):
        cell.attrs = {}
    for row in table.select('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        for cell in reversed(cells):
            row.append(cell.extract())
    table.select('thead td')[-1].string = 'AS OF DATE'
    result = parser.parse_live(str(soup), NOW)
    assert len(result['holdings']) == 20
    assert result['holdings'][0]['exposure_pct'].startswith('-100.775732076')


def test_live_explicit_percent_weights_and_extra_columns():
    soup = page()
    soup.select_one('thead .weight').string = 'Weight (%)'
    for row in soup.select('tbody tr')[:-1]:
        value = parser.decimal(row.select_one('.market_value').text)
        row.select_one('.weight').string = format(value / Decimal('5243820755.56') * 100, '.2f') + '%'
    for row in soup.select('tr'):
        extra = soup.new_tag('td')
        extra.string = 'Exchange' if row.parent.name == 'thead' else 'CBOT'
        row.append(extra)
    result = parser.parse_live(str(soup), NOW)
    assert result['holdings'][0]['weight'] == '-1.0078'
    assert result['metadata']['extra_columns'] == ['Exchange']


def test_percentage_validation_uses_the_published_precision():
    soup = page()
    soup.select_one('tbody .weight').string = '-101%'
    assert parser.parse_live(str(soup), NOW)['holdings'][0]['weight'] == '-1.01'
    soup.select_one('tbody .weight').string = '-101.00%'
    with pytest.raises(ValueError, match='inconsistent'):
        parser.parse_live(str(soup), NOW)


def test_explicitly_unavailable_quantity_is_preserved_without_inventing_one():
    soup = page()
    row = next(row for row in soup.select('tbody tr') if row.select_one('.security_name').text.strip() == 'TREASURY BILL')
    identifier = row.select_one('.isin').text.strip()
    row.select_one('.shares_qty').string = '-'
    result = parser.parse_live(str(soup), NOW)
    holding = next(item for item in result['holdings'] if item['identifier'] == identifier)
    assert holding['quantity'] is None
    assert holding['market_id'] == 'tbills'


@pytest.mark.parametrize('mutation', ['duplicate_header', 'extra_value', 'two_tables', 'non_usd', 'missing_header'])
def test_changed_layout_is_rejected_when_meaning_is_ambiguous(mutation):
    soup = page()
    if mutation == 'two_tables':
        soup.table['id'] = 'new-id'
        soup.append(BeautifulSoup(str(soup.table), 'html.parser'))
    elif mutation == 'missing_header':
        soup.select_one('thead .market_value').string = 'Balance'
    else:
        label = {'duplicate_header':'Security', 'extra_value':'Notional Value', 'non_usd':'Currency'}[mutation]
        for row in soup.select('tr'):
            extra = soup.new_tag('td')
            extra.string = label if row.parent.name == 'thead' else 'JPY'
            row.append(extra)
    with pytest.raises(ValueError):
        parser.parse_live(str(soup), NOW)


def test_all_unknowns_are_recorded_only_after_complete_validation(database):
    original = store.ingest(database, str(page()), NOW)
    rejected = store.ingest(database, unknown_page(two=True), NOW + timedelta(hours=1))
    review = recovery.pending_review(database)
    assert review['unmapped_names'] == 2 and review['pending_reports'] == 1
    assert review['items'][0]['reported_value'] == '-5284498755.18'
    assert review['items'][0]['reporting_date'] == '2026-10-01'
    assert store.reports(database)[-1]['id'] == original['id']
    assert database.execute('SELECT count(*) FROM dbmf_holdings WHERE report_id=?', (rejected['id'],)).fetchone()[0] == 0
    broken = BeautifulSoup(unknown_page(two=True), 'html.parser')
    broken.select_one('tbody .market_value').string = '-100'
    result = store.ingest(database, str(broken), NOW)
    assert result['status'] == 'rejected' and 'inconsistent' in result['error']
    assert recovery.pending_review(database)['pending_reports'] == 1


def test_mapping_is_persistent_idempotent_and_audited(database):
    before = Catalog(database).version
    document = definition()
    result = import_mapping(database, document)
    assert result['changed'] and result['catalog_version'] != before
    assert not import_mapping(database, document)['changed']
    db.initialize(database)
    with db.database() as reopened:
        catalog = Catalog(reopened)
        assert catalog.identify('NEW SILVER CONTRACT MAR2027') == 'silver'
        assert reopened.execute('SELECT count(*) FROM dbmf_mapping_changes').fetchone()[0] == 1
        market = reopened.execute("SELECT * FROM dbmf_markets WHERE id='silver'").fetchone()
        assert market['provider_symbol'] is None
    alias = dict(market_id='gold', aliases=['NEW GOLD CONTRACT'], source_url=document['source_url'], reason=document['reason'])
    import_mapping(database, alias)
    assert Catalog(database).identify('NEW GOLD CONTRACT JUN27') == 'gold'


@pytest.mark.parametrize('change', ['collision', 'missing_basis', 'invalid_category', 'bad_price', 'invert_future', 'unknown_key', 'wildcard', 'credentials'])
def test_invalid_or_conflicting_mapping_leaves_database_unchanged(database, change):
    document = definition()
    if change == 'collision': document['aliases'] = ['US 2YR NOTE (CBT) MAR27']
    if change == 'missing_basis': del document['market']['value_basis']
    if change == 'invalid_category': document['market']['category'] = 'Anything'
    if change == 'bad_price': document['market']['provider_symbol'] = 'SI=F'
    if change == 'invert_future': document['market'].update(provider_symbol='SI=F', price_reference='Silver futures', price_kind='futures', invert=True)
    if change == 'unknown_key': document['auto_discover'] = True
    if change == 'wildcard': document['aliases'] = ['SILVER.*']
    if change == 'credentials': document['source_url'] = 'https://private:secret@example.com/file'
    before = Catalog(database).version
    with pytest.raises(ValueError): import_mapping(database, document)
    assert Catalog(database).version == before
    assert database.execute("SELECT count(*) FROM dbmf_markets WHERE id='silver'").fetchone()[0] == 0
    assert database.execute('SELECT count(*) FROM dbmf_mapping_changes').fetchone()[0] == 0


def test_new_market_is_recovered_from_archive_with_original_dates(database):
    old = store.ingest(database, str(page()), NOW)
    rejected = store.ingest(database, unknown_page(), NOW + timedelta(minutes=1))
    assert recovery.replay_pending()['attempted'] == 0
    import_mapping(database, definition())
    result = recovery.replay_pending()
    assert result['recovered'] == 1 and result['attempted'] == 1
    current = store.reports(database)[-1]
    assert current['id'] != old['id'] and current['replayed_from'] == rejected['id']
    assert current['source_date'] == '2026-10-01'
    assert current['collected_at'] == db.iso(NOW + timedelta(minutes=1))
    assert database.execute('SELECT status FROM dbmf_reports WHERE id=?', (rejected['id'],)).fetchone()[0] == 'rejected'
    market = next(m for m in store.snapshot(database, current)['markets'] if m['id'] == 'silver')
    assert market['holding_count'] == 1 and float(market['net_pct']) < -100
    assert recovery.pending_review(database)['items'] == []
    assert recovery.replay_pending()['attempted'] == 0


def test_replaying_an_older_same_date_report_cannot_replace_a_later_revision(database):
    rejected = store.ingest(database, unknown_page(), NOW)
    later = store.ingest(database, str(page()), NOW + timedelta(minutes=1))
    import_mapping(database, definition())
    assert recovery.replay_pending()['recovered'] == 1
    assert store.reports(database)[-1]['id'] == later['id']
    assert database.execute('SELECT count(*) FROM dbmf_reports WHERE status=\'accepted\'').fetchone()[0] == 2
    assert recovery.pending_review(database)['pending_reports'] == 0


def test_failed_replays_do_not_loop_and_resume_after_another_mapping(database):
    store.ingest(database, unknown_page(two=True), NOW)
    import_mapping(database, definition())
    first = recovery.replay_pending()
    assert first['attempted'] == 1 and first['recovered'] == 0
    assert recovery.pending_review(database)['unmapped_names'] == 1
    assert recovery.pending_review(database)['items'][0]['original_name'].startswith('NEW COPPER')
    assert recovery.replay_pending()['attempted'] == 0
    import_mapping(database, definition('copper'))
    assert recovery.replay_pending()['recovered'] == 1
    assert recovery.replay_pending(force=True)['attempted'] == 0


def test_replay_never_bypasses_catalog_date_check_or_archive_checksum(database):
    first = store.ingest(database, str(page()), NOW, expected_date='2025-10-01')
    second = store.ingest(database, unknown_page(), NOW)
    (db.DATA_DIR / second['raw_path']).write_bytes(gzip.compress(b'changed archive'))
    import_mapping(database, definition())
    result = recovery.replay_pending()
    assert result['recovered'] == 0 and result['attempted'] == 2
    assert 'Catalog date' in result['results'][0]['error']
    assert 'checksum' in result['results'][1]['error']
    assert not store.reports(database)


def test_review_and_recovered_collection_status_api(database, monkeypatch):
    monkeypatch.setattr(db, 'utcnow', lambda: NOW)
    rejected = store.ingest(database, unknown_page(), NOW)
    with database:
        database.execute("INSERT INTO dbmf_runs(started_at,finished_at,status,error,report_id) VALUES(?,?,'error',?,?)",
                         (db.iso(NOW), db.iso(NOW), rejected['error'], rejected['id']))
    with TestClient(app) as client:
        before = client.get('/api/dbmf/status').json()
        assert before['review']['unmapped_names'] == 1 and before['collection_stale']
        assert client.get('/api/dbmf/unmapped').json()['pending_reports'] == 1
        assert client.get('/api/dbmf/mappings').status_code == 200
        assert client.post('/api/dbmf/mappings', json=definition()).status_code == 405
        import_mapping(database, definition())
        recovery.replay_pending()
        after = client.get('/api/dbmf/status').json()
        assert after['last_run']['recovered_by_replay']
        assert not after['collection_stale'] and after['review']['items'] == []
        assert client.get('/api/dbmf/prices/silver').json()['completion_rule'] is None
        assert client.get('/api/dbmf/prices/gold?start=2026-10-02&end=2026-10-01').status_code == 400


def test_schema_upgrade_and_backup_preserve_reviewed_configuration(database, tmp_path):
    store.ingest(database, unknown_page(), NOW)
    import_mapping(database, definition())
    recovery.replay_pending()
    path = backup(database, NOW)
    restored = tmp_path / 'restore'
    with tarfile.open(path) as archive:
        archive.extractall(restored, filter='data')
    with db.connect(restored / 'tracker.sqlite3') as conn:
        db.initialize(conn)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert Catalog(conn).identify('NEW SILVER CONTRACT JUN27') == 'silver'
        assert conn.execute('SELECT count(*) FROM dbmf_mapping_changes').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM dbmf_replay_attempts').fetchone()[0] == 1
        assert len(store.reports(conn)) == 1


def test_cli_import_and_worker_recovery_need_no_restart(database, tmp_path, monkeypatch, capsys):
    import sys
    from tracker import cli
    from tracker.dbmf import collector
    rejected = store.ingest(database, unknown_page(), NOW)
    path = tmp_path / 'silver.json'
    path.write_text(json.dumps(definition()))
    monkeypatch.setattr(sys, 'argv', ['tracker', 'dbmf-map', '--file', str(path)])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['market_id'] == 'silver' and result['replay']['recovered'] == 1
    second = store.ingest(database, unknown_page(two=True), NOW + timedelta(minutes=1))
    import_mapping(database, definition('copper'))
    monkeypatch.setattr(collector, 'collect', lambda: {'status':'success'})
    def stop(_): raise KeyboardInterrupt
    monkeypatch.setattr(collector.clock, 'sleep', stop)
    with pytest.raises(KeyboardInterrupt): collector.worker()
    assert not recovery.pending_review(database)['items']
    assert store.reports(database)[-1]['replayed_from'] == second['id']


def test_recovered_new_market_stays_zero_in_earlier_complete_reports(database):
    earlier = str(page()).replace('10/01/2026', '09/30/2026')
    store.ingest(database, earlier, NOW)
    store.ingest(database, unknown_page(), NOW + timedelta(minutes=1))
    import_mapping(database, definition())
    recovery.replay_pending()
    observations = store.history_response(database)['observations']
    assert observations[0]['exposures']['silver']['net_pct'] == '0'
    assert observations[0]['exposures']['silver']['absent']
    assert float(observations[1]['exposures']['silver']['net_pct']) < -100


def test_incomplete_report_stays_rejected_even_after_names_are_known(database):
    invalid = unknown_page().replace('</tbody>', '')
    store.ingest(database, invalid, NOW)
    import_mapping(database, definition())
    result = recovery.replay_pending()
    assert result['attempted'] == 1 and result['recovered'] == 0
    assert not store.reports(database)


def test_custom_alias_cannot_be_reassigned_to_another_market(database):
    import_mapping(database, definition())
    document = definition('copper', aliases=['NEW SILVER CONTRACT JUN27'])
    with pytest.raises(ValueError, match='already belongs'):
        import_mapping(database, document)
    assert Catalog(database).identify('NEW SILVER CONTRACT DEC28') == 'silver'
    assert database.execute("SELECT 1 FROM dbmf_markets WHERE id='copper'").fetchone() is None
