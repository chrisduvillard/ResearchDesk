"""Provider failures must remain inspectable without manufacturing candles."""
import json
from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tracker import db
from tracker.api import app
from tracker.dbmf import parser, prices
from tracker.dbmf.markets import MARKETS
from test_dbmf import FIXTURES

NOW = datetime.fromisoformat('2026-10-02T09:00:00+00:00')


def test_verified_price_references_keep_distinct_treasury_contracts():
    definitions = {r[0]: r for r in MARKETS}
    expected = {'us2y':'ZT=F', 'us10y':'ZN=F', 'uslong':'ZB=F',
                'us10ultra':'TN=F', 'usultra':'UB=F', 'fedfunds':'ZQ=F'}
    assert {key: definitions[key][3] for key in expected} == expected
    assert all(definitions[key][5:] == ('futures', 0) for key in expected)
    # A live-only quote or a discontinued symbol must not masquerade as history.
    assert definitions['sofr'][3] is None and definitions['eurodollar'][3] is None


def test_duplicate_date_with_one_invalid_bar_rejects_refresh(database, monkeypatch):
    database.execute("UPDATE dbmf_markets SET provider_symbol=NULL WHERE id!='wti'")
    frame = pd.DataFrame({'Open':[10.], 'High':[12.], 'Low':[9.], 'Close':[11.]},
                         index=pd.to_datetime(['2026-09-30']))
    monkeypatch.setattr(prices.yf, 'Ticker', lambda _: type('Ticker', (), {'history':lambda *a, **k: frame})())
    assert prices.refresh(database, NOW) == {}
    original = tuple(database.execute('SELECT * FROM dbmf_prices').fetchone())
    frame = pd.concat([frame, frame.assign(Close=20)])
    assert 'Duplicate provider date' in prices.refresh(database, NOW)['wti']
    assert tuple(database.execute('SELECT * FROM dbmf_prices').fetchone()) == original
    assert db.setting(database, 'dbmf_price_gaps_wti') == []


def test_nonfinite_evidence_is_json_safe_and_failure_retains_evidence(database, monkeypatch):
    database.execute("UPDATE dbmf_markets SET provider_symbol=NULL WHERE id!='jpy'")
    frame = pd.DataFrame({'Open':[150.,float('nan'),150.], 'High':[152.,152.,float('inf')],
                          'Low':[149.,149.,149.], 'Close':[151.,151.,151.]},
                         index=pd.to_datetime(['2026-09-28','2026-09-29','2026-09-30']))
    monkeypatch.setattr(prices.yf, 'Ticker', lambda _: type('Ticker', (), {'history':lambda *a, **k: frame})())
    assert prices.refresh(database, NOW) == {}
    gaps = db.setting(database, 'dbmf_price_gaps_jpy')
    assert len(gaps) == 2 and gaps[0]['raw_ohlc']['open'] == 'nan'
    assert gaps[1]['raw_ohlc']['high'] == 'inf'
    assert all(g['issues'] == ['Missing or nonfinite price'] for g in gaps)
    with TestClient(app) as client:
        data = client.get('/api/dbmf/prices/jpy').json()
    json.dumps(data, allow_nan=False)
    database.execute("UPDATE dbmf_markets SET provider_symbol=NULL WHERE id!='jpy'")
    # Removing a cached date rejects the update and preserves its prior evidence.
    frame = frame.iloc[-1:]
    assert prices.refresh(database, NOW)['jpy']
    assert db.setting(database, 'dbmf_price_gaps_jpy') == gaps
    assert database.execute('SELECT count(*) FROM dbmf_prices').fetchone()[0] == 1


@pytest.mark.parametrize('day,notionals', [
    ('2023-09-30', {
        'tbills':'694323299','eur':'205617187','eafe':'79720575','sp500':'17734550',
        'wti':'167388000','sofr':'-466273888','fedfunds':'-180100970','gold':'-137531570',
        'jpy':'-385131437','em':'-30384900','us10ultra':'-186420937','us10y':'-188352938',
        'us2y':'-467248712','uslong':'-80215781','usultra':'-77977687',
    }),
    ('2024-06-30', {
        'tbills':'784595310','repo':'26296079','eur':'264230400','gold':'200971640',
        'eafe':'261735440','em':'75847540','wti':'130233600','jpy':'-661971000',
        'sp500':'-242669925','us10y':'-398913328','us2y':'-879774375','uslong':'-251650688',
    }),
])
def test_recovered_reports_match_every_visually_reviewed_notional(day, notionals):
    report = parser.parse_historical((FIXTURES / (day + '.txt')).read_text(), NOW)
    assert {h['market_id']:h['notional'] for h in report['holdings']} == notionals
    for h in report['holdings']:
        assert abs(Decimal(h['exposure_pct']) - Decimal(h['notional']) / Decimal(report['net_assets']) * 100) < Decimal('1e-24')
