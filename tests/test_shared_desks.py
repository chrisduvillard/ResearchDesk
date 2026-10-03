from datetime import datetime
import json
from fastapi.testclient import TestClient
from tracker.api import app
from tracker.research import ingest_disclosure, sync_legacy
from tracker.history import ingest
from tracker.funds import accept_report, holding
from conftest import page, moment


def contributors(conn):
    ingest(conn, page(), moment(), resolve=False)
    sync_legacy(conn)
    ingest_disclosure(
        conn, "karen-finerman", page("Karen Finerman is long MSFT."), moment()
    )
    ingest_disclosure(conn, "guy-adami", page("Guy Adami is short MSFT."), moment())
    ingest_disclosure(
        conn,
        "karen-finerman",
        page(
            "Karen Finerman is short MSFT.", "Disclosures as of 10/1/26 (4:30 PM ET):"
        ),
        moment("2026-10-01T22:15:00-04:00"),
    )


def test_contributor_dashboard_is_scoped_and_complete(database):
    contributors(database)
    with TestClient(app) as c:
        root = "/api/v2/contributors/karen-finerman/desk/"
        r = c.get(root + "status")
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Karen Finerman"
        assert r.json()["active_instruments"] == 1
        assert c.get(root + "positions").json()[0]["direction"] == "bearish"
        events = c.get(root + "timeline/MSFT").json()
        assert [e["kind"] for e in events] == ["baseline", "direction_changed"]
        assert all(e["contributor_id"] == "karen-finerman" for e in events)
        assert events[-1]["before"][0]["direction"] == "bullish"
        assert c.get(events[-1]["source_href"]).status_code == 200
        assert c.get(root + "analysis/MSFT").json()["strategies"]
        assert "Karen" in c.get(root + "export/history.csv").text
        assert (
            c.get("/api/v2/contributors/guy-adami/desk/status").json()["event_count"]
            == 1
        )
        assert c.get("/api/positions").json()[0]["symbol"] != ""
        assert c.get("/contributors/karen-finerman").status_code == 200
        assert c.get("/contributors/not-a-person").status_code == 404


def fund_report(conn, fund, day, weight, key="MSFT", raw=b"first"):
    return accept_report(
        conn,
        fund,
        dict(
            source_date=day,
            complete=True,
            holdings=[
                holding(
                    "Microsoft",
                    key,
                    "legacy:MSFT",
                    "equity_weight_pct",
                    weight,
                    quantity="10",
                )
            ],
            metadata={},
        ),
        raw,
        datetime.fromisoformat(day + "T22:00:00+00:00"),
    )


def test_fund_dashboard_compares_dates_and_preserves_opaque_ids(database):
    sync_legacy(database)
    first = fund_report(database, "ARKK", "2026-09-30", "4")
    second = fund_report(database, "ARKK", "2026-10-01", "6", raw=b"second")
    fund_report(database, "ARKQ", "2026-10-01", "8", raw=b"other")
    with TestClient(app) as c:
        root = "/api/v2/funds/ARKK/desk/"
        r = c.get(root + "exposures")
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["current"]["id"] != result["comparison"]["id"]
        market = result["current"]["markets"][0]
        assert float(market["net_pct"]) == 6
        assert float(market["change_pp"]) == 2
        assert result["current"]["net_assets"] is None
        assert len(c.get(root + "history").json()["observations"]) == 2
        source = c.get(root + "reports/" + result["current"]["id"]).json()[
            "source_href"
        ]
        assert c.get(source).status_code == 200
        other = c.get("/api/v2/funds/ARKQ/desk/exposures").json()["current"]["id"]
        assert c.get(root + "reports/" + other).status_code == 404
        assert c.get(root + "exposures", params={"report_id": other}).status_code == 404
        assert "equity_weight_pct" in c.get(root + "export/history.csv").text
        assert c.get("/funds/ARKK").status_code == 200
        assert c.get("/funds/UNKNOWN").status_code == 404


def test_fund_revisions_do_not_add_history_dates_or_mix_risk(database):
    sync_legacy(database)
    first = fund_report(database, "ARKK", "2026-10-01", "4")
    second = fund_report(database, "ARKK", "2026-10-01", "7", raw=b"correction")
    with TestClient(app) as c:
        root = "/api/v2/funds/ARKK/desk/"
        assert len(c.get(root + "history").json()["observations"]) == 1
        change = c.get(root + "revisions/" + second["id"]).json()
        assert change["changed_rows"] == 1 and change["origin"] == "source_changed"
        assert change["previous"]["id"] == first["id"]
        assert c.get(root + "exposures").json()["comparison_missing"] is True
        assert len(c.get(root + "revisions").json()["items"]) == 1
        assert c.get(root + "exposures?compare=date").status_code == 400
    doc = dict(
        source_date="2026-10-01",
        complete=True,
        metadata={"net_assets": "1000"},
        holdings=[
            holding(
                "Gold",
                "gold-contract",
                "market:gold",
                "notional_pct_nav",
                "20",
                notional="200",
            ),
            holding(
                "Gold risk", "risk:gold", "market:gold", "issuer_risk_weight_pct", "50"
            ),
        ],
    )
    accept_report(database, "CTA", doc, b"cta")
    with TestClient(app) as c:
        data = c.get("/api/v2/funds/CTA/desk/exposures").json()["current"]
        assert float(data["markets"][0]["net_pct"]) == 20
        assert data["risk_measures"][0]["value"] == "50"
        assert data["summary"]["collateral_pct"] is None


def test_chart_refresh_validates_ohlc_and_keeps_previous_batch(database, monkeypatch):
    import pandas as pd
    import yfinance as yf
    from tracker.chart_prices import refresh, prices

    sync_legacy(database)
    frame = pd.DataFrame(
        [dict(Open=100, High=110, Low=99, Close=105, Volume=10)],
        index=pd.to_datetime(["2026-09-01"]),
    )

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, **kwargs):
            return frame

    monkeypatch.setattr(yf, "Ticker", Ticker)
    refresh(database, "legacy:MSFT")
    first = prices(database, "legacy:MSFT")
    assert first["bars"][0]["high"] == 110
    frame.loc[frame.index[0], "High"] = 50
    import pytest

    with pytest.raises(ValueError):
        refresh(database, "legacy:MSFT")
    assert prices(database, "legacy:MSFT")["bars"] == first["bars"]
    assert (
        database.execute(
            "SELECT count(*) FROM price_batches WHERE kind='accounting'"
        ).fetchone()[0]
        == 0
    )


def test_dashboard_health_uses_running_generalized_collectors(database):
    contributors(database)
    fund_report(database, "ARKK", "2026-10-01", "4")
    with database:
        from tracker import db

        db.set_setting(database, "contributor_worker_heartbeat", db.iso())
        db.set_setting(database, "fund_worker_heartbeat", db.iso())
    with TestClient(app) as c:
        assert c.get("/api/v2/contributors/karen-finerman/desk/status").json()[
            "worker_healthy"
        ]
        assert c.get("/api/v2/funds/ARKK/desk/status").json()["worker_healthy"]
        assert c.get("/api/v2/funds/DBMF/desk/status").json()["worker_healthy"]


def test_dan_verified_option_details_keep_existing_payoff(database):
    from tracker.history import attach_details

    ingest(database, page("Dan Nathan is long MSFT."), moment(), resolve=False)
    attach_details(
        database,
        "MSFT",
        {"legs": [{"kind": "stock", "quantity": 1}]},
        "Verified transcript",
        "Stock confirmed",
        observed=moment("2026-10-01T10:00:00-04:00"),
    )
    sync_legacy(database)
    with TestClient(app) as c:
        legacy = c.get("/api/analysis/MSFT").json()["strategies"][0]
        shared = c.get("/api/v2/contributors/dan-nathan/desk/analysis/MSFT").json()[
            "strategies"
        ][0]
        assert legacy["payoff"] is not None
        assert shared["payoff"] == legacy["payoff"]
        assert shared["analysis"] == legacy["analysis"]


def test_fund_visit_only_counts_new_reports_and_revisions(database):
    from tracker import fund_desk

    sync_legacy(database)
    first = fund_report(database, "ARKK", "2026-10-01", "4")
    cursor = fund_desk.changes(database, "ARKK")["cursor"]
    correction = fund_report(database, "ARKK", "2026-10-01", "5", raw=b"fix")
    changes = fund_desk.changes(
        database, "ARKK", cursor["report_id"], cursor["after_id"]
    )
    assert changes["new_reports"] == 1
    assert len(changes["revisions"]) == 1
    visited = changes["cursor"]
    repeated = fund_desk.changes(
        database, "ARKK", visited["report_id"], visited["after_id"]
    )
    assert repeated["new_reports"] == 0
    assert repeated["revisions"] == []


def test_generic_indicator_keeps_legacy_download(database):
    with TestClient(app) as c:
        assert (
            "Dan Nathan · Disclosed Positions"
            in c.get("/downloads/dan-nathan.pine").text
        )
        assert (
            "Research Desk · Disclosed Positions"
            in c.get("/downloads/research-desk.pine").text
        )


def test_chart_first_collection_omits_bad_bars_with_evidence(database, monkeypatch):
    import pandas as pd
    import yfinance as yf
    from tracker.chart_prices import refresh, prices

    sync_legacy(database)
    frame = pd.DataFrame(
        [
            dict(Open=100, High=50, Low=99, Close=105, Volume=10),
            dict(Open=100, High=110, Low=99, Close=105, Volume=10),
        ],
        index=pd.to_datetime(["2026-09-01", "2026-09-02"]),
    )

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, **kw):
            return frame

    monkeypatch.setattr(yf, "Ticker", Ticker)
    refresh(database, "legacy:MSFT")
    result = prices(database, "legacy:MSFT")
    assert [b["time"] for b in result["bars"]] == ["2026-09-02"]
    assert result["omitted_bars"][0]["date"] == "2026-09-01"
    assert result["omitted_bars"][0]["raw_ohlc"]["high"] == "50.0"
