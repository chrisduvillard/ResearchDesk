import pytest


def test_nominal_accounting_prices_reconstruct_future_splits():
    from tracker.analysis_worker import accounting_bars

    rows = [
        {"date": "2026-01-02", "open": 50, "close": 50, "dividend": 0, "split": 0},
        {"date": "2026-01-05", "open": 50, "close": 55, "dividend": 1, "split": 2},
    ]
    bars = accounting_bars(rows)
    assert bars["2026-01-02"]["open"] == 100
    assert bars["2026-01-05"]["open"] == 50
    assert bars["2026-01-05"]["dividend"] == 1
    with pytest.raises(ValueError):
        accounting_bars([dict(rows[0], open=float("nan"))])
