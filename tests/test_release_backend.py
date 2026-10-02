"""Regression coverage for the release's backend correctness review."""

import pytest
import pandas as pd
from fastapi.testclient import TestClient

from tracker import prices
from tracker.api import app
from tracker.history import attach_details, current_positions, ingest, review
from tracker.strategies import analyze, payoff_for
from conftest import moment, page


@pytest.mark.parametrize('later', ['review', 'details'])
def test_same_second_clarifications_keep_their_order_after_ingestion(database, later):
    html = page('Dan Nathan is long MSFT.')
    ingest(database, html, moment(), resolve=False)
    early = moment('2026-10-01T10:00:00.100000-04:00')
    late = moment('2026-10-01T10:00:00.900000-04:00')
    actions = {
        'review': lambda at: review(database, 'MSFT', 'unknown', 'Legs are uncertain', at),
        'details': lambda at: attach_details(
            database, 'MSFT', {'legs': [{'kind': 'stock', 'quantity': 1}]},
            'Verified transcript', 'Stock confirmed', observed=at),
    }
    actions['details' if later == 'review' else 'review'](early)
    actions[later](late)
    expected = 'unknown' if later == 'review' else 'bullish'
    position = current_positions(database)[0]
    assert position['direction'] == expected
    assert position['analysis']['payoff_available'] == (later == 'details')
    result = ingest(database, html, moment('2026-10-01T22:15:00-04:00'), resolve=False)
    assert result['events'] == 0
    assert current_positions(database)[0]['direction'] == expected


def test_legacy_equal_timestamp_review_hides_uncertain_payoff(database):
    ingest(database, page('Dan Nathan is long MSFT.'), moment(), resolve=False)
    at = moment('2026-10-01T10:00:00-04:00')
    attach_details(database, 'MSFT', {'legs': [{'kind': 'stock', 'quantity': 1}]},
                   'Verified transcript', 'Initial clarification', observed=at)
    review(database, 'MSFT', 'unknown', 'Legs are uncertain', at)
    with database:
        database.execute("UPDATE strategy_details SET created_at='2026-10-01T14:00:00+00:00'")
        database.execute("UPDATE reviews SET created_at='2026-10-01T14:00:00+00:00'")
    position = current_positions(database)[0]
    assert position['direction'] == 'unknown'
    assert not position['analysis']['payoff_available']


@pytest.mark.parametrize('side,wording', [
    ('long', 'Oct 100 call for $3 credit'),
    ('short', 'Oct 100 put for $3 debit'),
    ('long', 'Oct 100 straddle for $3 credit'),
    ('short', 'Oct 90/110 strangle for $3 debit'),
    ('long', 'Oct 80/90/110/120 credit iron condor for $3 debit'),
    ('long', 'Oct 90/100/110 debit iron butterfly for $3 credit'),
])
def test_conflicting_premiums_cannot_enable_payoffs_or_scores(side, wording):
    analysis = analyze(side, wording)
    assert analysis['direction'] == 'unknown'
    assert not analysis['score_eligible']
    assert payoff_for(analysis) is None


@pytest.mark.parametrize('wording,direction,minimum,maximum', [
    ('Oct 80/90/110/120 iron condor for $3 credit', 'range_bound', -700, 300),
    ('Oct 90/100/110 iron butterfly for $3 debit', 'large_move', -300, 700),
    ('Oct 100 call for $3 debit', 'bullish', -300, None),
    ('Oct 100 put for $3 credit', 'bullish', -9700, 300),
])
def test_consistent_option_premiums_keep_the_disclosed_payoff(
        wording, direction, minimum, maximum):
    side = 'short' if wording == 'Oct 100 put for $3 credit' else 'long'
    analysis = analyze(side, wording)
    assert analysis['direction'] == direction
    payoff = payoff_for(analysis)
    assert payoff is not None
    assert payoff['minimum'] == minimum
    assert payoff['maximum'] == maximum


@pytest.mark.parametrize('field', ['Volume', 'Dividends', 'Stock Splits'])
@pytest.mark.parametrize('bad_value', [float('inf'), float('-inf'), float('nan'), -1])
def test_invalid_price_metadata_preserves_cache_and_json_api(database, monkeypatch, field, bad_value):
    ingest(database, page('Dan Nathan is long MSFT.'), moment(), resolve=False)
    frame = pd.DataFrame({
        'Open': [100.0], 'High': [101.0], 'Low': [99.0], 'Close': [100.0],
        'Volume': [1000.0], 'Dividends': [0.0], 'Stock Splits': [0.0],
    }, index=pd.to_datetime(['2026-09-30']))

    class Provider:
        def history(self, **kwargs):
            return frame

    monkeypatch.setattr(prices.yf, 'Ticker', lambda symbol: Provider())
    now = moment('2026-10-01T22:15:00-04:00')
    assert prices.refresh(database, now) == {}
    cached = tuple(database.execute('SELECT * FROM prices').fetchone())
    frame.loc[frame.index[0], field] = bad_value
    errors = prices.refresh(database, moment('2026-10-02T22:15:00-04:00'))
    assert 'MSFT' in errors
    assert tuple(database.execute('SELECT * FROM prices').fetchone()) == cached
    with TestClient(app) as client:
        response = client.get('/api/prices/MSFT')
    assert response.status_code == 200
    assert response.json()['bars'][0]['volume'] == 1000
