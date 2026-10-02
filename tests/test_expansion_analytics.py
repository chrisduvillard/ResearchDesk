from datetime import datetime
import pytest


def signal(
    i=1,
    day="2026-01-02T12:00:00+00:00",
    direction="bullish",
    asset="a",
    kind="call",
    eligible=True,
):
    return dict(
        id=i,
        asset_id=asset,
        available_at=day,
        direction=direction,
        eligible=eligible,
        kind=kind,
        details_json="{}",
        contributor_id="dan-nathan",
        evidence_type="call",
    )


def test_fixed_long_short_cash_and_costs():
    from tracker.analytics import simulate

    inputs = dict(
        events=[signal()],
        sessions=["2026-01-02"],
        prices={"a": {"2026-01-02": dict(open=100, close=110, dividend=0, split=0)}},
        config={"horizon": 1, "cost_bps": 5, "borrow_rate": 0.05},
    )
    follow = simulate(inputs, "follow")
    fade = simulate(inputs, "fade")
    assert follow["status"] == "complete" and fade["status"] == "complete"
    # 100 shares, $5 entry cost and $5.50 exit cost.
    assert follow["equity_curve"][-1]["equity"] == pytest.approx(100989.5)
    assert fade["equity_curve"][-1]["equity"] == pytest.approx(98989.5)
    assert follow["costs"] == pytest.approx(10.5)
    assert follow["trades"][0]["quantity"] == 100


def test_dividends_splits_borrow_and_missing_valuation():
    from tracker.analytics import simulate

    inputs = dict(
        events=[signal()],
        sessions=["2026-01-02", "2026-01-05"],
        prices={
            "a": {
                "2026-01-02": dict(open=100, close=100, dividend=0, split=0),
                "2026-01-05": dict(open=50, close=55, dividend=1, split=2),
            }
        },
        config={"horizon": 2, "cost_bps": 0, "borrow_rate": 0.05},
    )
    long = simulate(inputs, "follow")
    short = simulate(inputs, "fade")
    assert long["equity_curve"][-1]["equity"] == pytest.approx(101200)
    assert short["equity_curve"][-1]["equity"] == pytest.approx(
        98800 - 10000 * 0.05 * 3 / 365
    )
    assert short["dividends"] == -200
    inputs["prices"]["a"].pop("2026-01-05")
    incomplete = simulate(inputs, "follow")
    assert incomplete["status"] == "incomplete"
    assert incomplete["equity_curve"][-1]["equity"] is None
    assert incomplete["total_return"] is None


def test_signal_reversal_ties_and_no_pyramiding():
    from tracker.analytics import simulate

    events = [signal(), signal(2), signal(3, "2026-01-02T21:30:00+00:00", "bearish")]
    inputs = dict(
        events=events,
        sessions=["2026-01-02", "2026-01-05"],
        prices={
            "a": {
                "2026-01-02": dict(open=100, close=100, dividend=0, split=0),
                "2026-01-05": dict(open=110, close=110, dividend=0, split=0),
            }
        },
        config={"exit": "signal", "cost_bps": 0, "borrow_rate": 0},
    )
    result = simulate(inputs, "follow")
    assert len(result["trades"]) == 3  # enter, close, reverse
    assert result["skipped"][0]["reason"] == "already_open"
    assert result["positions"][0]["quantity"] < 0
    assert result["equity_curve"][-1]["equity"] == pytest.approx(101000)


def test_short_proceeds_do_not_fund_extra_entries_and_insolvency():
    from tracker.analytics import simulate

    events = [signal(i + 1, asset=str(i), direction="bearish") for i in range(12)]
    prices = {
        str(i): {"2026-01-02": dict(open=100, close=100, dividend=0, split=0)}
        for i in range(12)
    }
    inputs = dict(
        events=events,
        sessions=["2026-01-02"],
        prices=prices,
        config={"horizon": 20, "cost_bps": 0, "borrow_rate": 0},
    )
    result = simulate(inputs, "follow")
    assert len(result["positions"]) == 10
    assert len(result["skipped"]) == 2
    inputs["prices"]["0"]["2026-01-02"]["close"] = 2000
    result = simulate(inputs, "follow")
    assert result["status"] == "insolvent"


def test_score_overlap_net_and_reproducible_intervals():
    from tracker.analytics import scorecard, bootstrap

    inputs = dict(
        events=[signal(), signal(2)],
        prices={"a": {"2026-01-02": dict(open=100, close=110)}},
        config={"horizons": [1], "cost_bps": 5, "borrow_rate": 0.05},
        as_of="2026-01-03T00:00:00+00:00",
        assets={"a": {"kind": "Companies", "currency": "USD"}},
        benchmarks={},
    )
    result = scorecard(inputs)
    assert result["summary"][0]["n"] == 1
    assert result["signals"][1]["results"]["1"]["status"] == "overlap"
    assert result["summary"][0]["follow"]["mean"] == pytest.approx(0.1)
    assert result["summary"][0]["follow_net"]["mean"] == pytest.approx(0.09895)
    assert result["summary"][0]["interval"] is None
    assert bootstrap([("2026-01-02", 0.1)] * 30, 20) is None


def test_analysis_inputs_immutable_and_job_reuse(database):
    from tracker.analytics import enqueue, process_job

    inputs = dict(
        events=[signal()],
        sessions=["2026-01-02"],
        prices={"a": {"2026-01-02": dict(open=100, close=110, dividend=0, split=0)}},
        config={"horizon": 1},
    )
    job = enqueue(database, "simulation", inputs)
    assert enqueue(database, "simulation", inputs) == job
    inputs["prices"]["a"]["2026-01-02"]["close"] = 1
    process_job(database, job)
    import json

    row = database.execute("SELECT * FROM analysis_runs WHERE id=?", (job,)).fetchone()
    assert row["status"] == "complete"
    assert json.loads(row["result_json"])["follow"]["total_return"] > 0


def test_unrelated_asset_classes_are_never_pooled():
    from tracker.analytics import scorecard

    inputs = dict(
        events=[signal(asset="a"), signal(2, asset="b")],
        prices={a: {"2026-01-02": dict(open=100, close=110)} for a in ["a", "b"]},
        config={"horizons": [1]},
        as_of="2026-01-03T00:00:00+00:00",
        assets={"a": {"kind": "Companies"}, "b": {"kind": "Bonds"}},
        benchmarks={},
    )
    result = scorecard(inputs)
    assert len(result["summary"]) == 2
    assert all(r["n"] == 1 for r in result["summary"])
    assert {r["asset_class"] for r in result["summary"]} == {"Companies", "Bonds"}


def test_zero_data_is_not_complete_portfolio_performance():
    from tracker.analytics import simulate

    result = simulate(
        dict(events=[signal()], sessions=["2026-01-02"], prices={}, config={}), "follow"
    )
    assert result["coverage"]["missing_entries"] == 1
    assert (
        result["total_return"] == 0
    )  # Idle capital is shown, with missing entry coverage.
    assert result["benchmark"]["status"] == "unmapped"


def test_buy_hold_benchmark_and_missing_data():
    from tracker.analytics import simulate

    data = dict(
        events=[signal()],
        sessions=["2026-01-02"],
        prices={
            "a": {"2026-01-02": dict(open=100, close=110, split=0, dividend=0)},
            "spy": {"2026-01-02": dict(open=200, close=210, split=0, dividend=0)},
        },
        config={"horizon": 1, "cost_bps": 0},
        benchmarks={"a": "spy"},
    )
    result = simulate(data, "follow")
    assert result["benchmark"]["total_return"] == pytest.approx(0.05)


def test_prospective_signal_uses_eligibility_known_at_entry():
    from tracker.analytics import simulate

    e = signal()
    e.update(
        eligible=False,
        eligible_at_creation=True,
        invalidated_at="2026-01-05T15:00:00+00:00",
    )
    data = dict(
        events=[e],
        sessions=["2026-01-02"],
        prices={"a": {"2026-01-02": dict(open=100, close=110, split=0, dividend=0)}},
        config={"horizon": 1, "cost_bps": 0},
        forward_start="2026-01-01T00:00:00+00:00",
    )
    assert simulate(data, "follow")["total_return"] == pytest.approx(0.01)
    data["events"][0]["invalidated_at"] = "2026-01-02T13:00:00+00:00"
    result = simulate(data, "follow")
    assert not result["trades"]
    assert result["skipped"][0]["reason"] == "invalidated_before_entry"


def test_bootstrap_keeps_trailing_session_observations():
    from tracker.analytics import bootstrap
    from tracker.calendar import calendar

    days = calendar().sessions_in_range("2026-01-02", "2026-03-31")[:35]
    result = bootstrap(
        [(d.date().isoformat(), 0 if i < 30 else 1) for i, d in enumerate(days)], 10
    )
    assert result["blocks"] == 3
    assert result["upper"] > 0  # Last five observations must influence the interval.


def test_option_close_does_not_close_an_underlying_position():
    from tracker.analytics import simulate

    closing = signal(
        2,
        "2026-01-02T21:30:00+00:00",
        direction="unknown",
        kind="close",
        eligible=False,
    )
    closing["details_json"] = '{"instrument_type":"option"}'
    inputs = dict(
        events=[signal(), closing],
        sessions=["2026-01-02", "2026-01-05"],
        prices={
            "a": {
                d: dict(open=100, close=100, dividend=0, split=0)
                for d in ["2026-01-02", "2026-01-05"]
            }
        },
        config={"exit": "signal"},
    )
    result = simulate(inputs, "follow")
    assert len(result["positions"]) == 1
    assert len(result["trades"]) == 1
