"""Retired equity funds retain archives but leave the active research feed."""

import json
from datetime import timedelta
from fastapi.testclient import TestClient
from tracker import db
from tracker.api import app
from tracker.sources import prepare_sources, collect_source
from tracker.research import activity
from tracker.briefing import generate
from test_shared_desks import fund_report


def test_retired_funds_are_absent_from_active_views_and_collection(database):
    fund_report(database, "ARKK", "2026-10-01", "4")
    fund_report(database, "CTA", "2026-10-01", "6", raw=b"cta")
    now = db.utcnow()
    for source in ("fund:ARKK", "fund:CTA"):
        with database:
            activity(
                database,
                source + ":test",
                source,
                "legacy:MSFT",
                "source_recovery",
                None,
                db.iso(now - timedelta(days=1)),
                source,
                "/research",
                "source",
                source,
            )
    generate(database, now)
    # A saved briefing from before retirement may still reference archived items.
    with database:
        database.execute(
            "UPDATE briefings SET activity_ids_json=?",
            (json.dumps([r[0] for r in database.execute("SELECT id FROM activity")]),),
        )
    with database:
        database.execute(
            "UPDATE sources SET adapter='ark',enabled=1 WHERE fund_id LIKE 'ARK%'"
        )
    prepare_sources(database)
    assert (
        database.execute(
            "SELECT sum(enabled) FROM sources WHERE fund_id LIKE 'ARK%'"
        ).fetchone()[0]
        == 0
    )
    result = collect_source(
        "fund:ARKK",
        download=lambda url: (_ for _ in ()).throw(
            AssertionError("Retired source downloaded")
        ),
    )
    assert result["status"] == "disabled"
    with TestClient(app) as client:
        assert {r["id"] for r in client.get("/api/v2/funds").json()} == {
            "DBMF",
            "KMLM",
            "CTA",
            "WTMF",
        }
        assert not any(
            r["fund_id"] and r["fund_id"].startswith("ARK")
            for r in client.get("/api/v2/sources/status").json()
        )
        feed = client.get("/api/v2/activity").json()
        assert any(r["source_id"] == "fund:CTA" for r in feed["items"])
        assert not any(r["source_id"] == "fund:ARKK" for r in feed["items"])
        briefing = client.get("/api/v2/briefings").json()
        assert not any(
            r["source_id"] == "fund:ARKK" for r in briefing["items"] + briefing["live"]
        )
        assert all(
            r["fund_id"] == "CTA"
            for r in client.get("/api/v2/assets/legacy:MSFT").json()["holdings"]
        )
        assert not any(
            r["source_id"] == "fund:ARKK"
            for r in client.get("/api/v2/assets/legacy:MSFT/timeline").json()["items"]
        )
        # Explicit archive URLs still resolve; no historical evidence was deleted.
        assert client.get("/api/v2/funds/ARKK/reports").json()
    assert (
        database.execute(
            "SELECT count(*) FROM fund_reports WHERE fund_id='ARKK'"
        ).fetchone()[0]
        == 1
    )


def test_chart_worker_does_not_refresh_retired_fund_only_holdings(
    database, monkeypatch
):
    from tracker import chart_prices

    requested = []
    monkeypatch.setattr(
        chart_prices, "refresh", lambda conn, asset: requested.append(asset)
    )
    fund_report(database, "ARKK", "2026-10-01", "4")
    chart_prices.maintain(database)
    assert requested == []
    fund_report(database, "CTA", "2026-10-01", "6", raw=b"cta")
    chart_prices.maintain(database)
    assert requested == ["legacy:MSFT"]
