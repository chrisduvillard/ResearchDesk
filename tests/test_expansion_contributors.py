from datetime import datetime
import pytest
from fastapi.testclient import TestClient
from tracker import db
from tracker.api import app
from conftest import page, moment


def test_scoped_disclosures_empty_missing_and_reviews(database):
    from tracker.research import ingest_disclosure, positions, review_position

    first = moment()
    ingest_disclosure(
        database,
        "karen-finerman",
        page("Karen Finerman is long MSFT.\nShe is short TLT."),
        first,
    )
    ingest_disclosure(database, "guy-adami", page("Guy Adami is short MSFT."), first)
    assert positions(database, "karen-finerman")[0]["direction"] == "bullish"
    review_position(database, "guy-adami", "MSFT", "unknown", "Ambiguous attribution")
    assert positions(database, "karen-finerman")[0]["direction"] == "bullish"
    assert positions(database, "guy-adami")[0]["direction"] == "unknown"
    assert (
        ingest_disclosure(database, "karen-finerman", "<html>missing</html>", first)[
            "status"
        ]
        == "rejected"
    )
    assert len(positions(database, "karen-finerman")) == 2
    ingest_disclosure(
        database,
        "karen-finerman",
        page(
            "Karen Finerman has no positions.",
            "Disclosures as of 10/1/26 (4:30 PM ET):",
        ),
        moment("2026-10-01T22:15:00-04:00"),
    )
    assert positions(database, "karen-finerman") == []
    assert database.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
    assert (
        database.execute(
            "SELECT count(*) FROM research_events WHERE kind='removed'"
        ).fetchone()[0]
        == 2
    )


def test_attribution_not_assumed_and_duplicate_observation_not_signal(database):
    from tracker.research import ingest_disclosure

    html = page("Guy Adami is long MSFT.")
    ingest_disclosure(database, "guy-adami", html, moment())
    ingest_disclosure(database, "guy-adami", html, moment())
    assert database.execute("SELECT count(*) FROM research_events").fetchone()[0] == 1
    assert (
        ingest_disclosure(
            database,
            "guy-adami",
            page("Guy Adami is long MSFT. His firm is short TLT."),
            moment(),
        )["status"]
        == "rejected"
    )
    assert database.execute("SELECT count(*) FROM research_events").fetchone()[0] == 1


def test_legacy_bridge_keeps_ids_links_and_baselines_ineligible(database):
    from tracker.history import ingest
    from tracker.research import sync_legacy

    ingest(database, page(), moment(), resolve=False)
    sync_legacy(database)
    sync_legacy(database)
    event = database.execute(
        "SELECT * FROM research_events ORDER BY legacy_id LIMIT 1"
    ).fetchone()
    assert event["contributor_id"] == "dan-nathan"
    assert event["legacy_id"] == 1 and not event["eligible"]
    assert event["available_at"] == "2026-10-01T02:15:00+00:00"
    assert database.execute("SELECT count(*) FROM research_events").fetchone()[0] == 5


def login(client):
    r = client.post(
        "/api/v2/auth/login", json={"password": "a long local test password"}
    )
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def test_call_approval_revision_and_retraction_timing(database):
    from tracker.auth import setup_owner
    from tracker.research import sync_legacy

    setup_owner(database, "a long local test password")
    sync_legacy(database)
    payload = dict(
        contributor_id="karen-finerman",
        asset_id="legacy:MSFT",
        action="long",
        spoken_at="2026-01-05T15:00:00-05:00",
        timezone="America/New_York",
        source_url="https://www.cnbc.com/video/example",
        excerpt="I would buy Microsoft.",
        horizon="several weeks",
        conditions="",
        target=None,
    )
    with TestClient(app, base_url="https://testserver") as client:
        headers = login(client)
        response = client.post("/api/v2/calls", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        call = response.json()
        assert call["status"] == "draft"
        approved = client.post(
            f"/api/v2/calls/{call['id']}/approve", json={"revision": 1}, headers=headers
        )
        assert approved.status_code == 200, approved.text
        event = dict(
            database.execute(
                "SELECT * FROM research_events WHERE evidence_type='call'"
            ).fetchone()
        )
        assert event["eligible"]
        assert datetime.fromisoformat(event["available_at"]) >= datetime.fromisoformat(
            approved.json()["approved_at"]
        )
        assert event["available_at"] != payload["spoken_at"]
        # Optimistic revision prevents two browser tabs approving stale edits.
        assert (
            client.put(
                f"/api/v2/calls/{call['id']}",
                json=dict(payload, revision=0),
                headers=headers,
            ).status_code
            == 409
        )
        edited = client.put(
            f"/api/v2/calls/{call['id']}",
            json=dict(payload, revision=2, conditions="Only below 100"),
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["status"] == "draft"
        assert (
            database.execute(
                "SELECT eligible FROM research_events WHERE id=?", (event["id"],)
            ).fetchone()[0]
            == 0
        )
        r = client.post(
            f"/api/v2/calls/{call['id']}/approve", json={"revision": 3}, headers=headers
        )
        assert r.status_code == 200
        assert not database.execute(
            "SELECT eligible FROM research_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        r = client.post(
            f"/api/v2/calls/{call['id']}/retract",
            json={"revision": 4, "reason": "Correction"},
            headers=headers,
        )
        assert r.status_code == 200
        assert (
            database.execute("SELECT count(*) FROM call_revisions").fetchone()[0] == 5
        )
        assert (
            client.post(
                "/api/v2/calls",
                json=dict(payload, source_url="javascript:alert(1)"),
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v2/calls",
                json=dict(payload, spoken_at="2026-11-01T01:30:00"),
                headers=headers,
            ).status_code
            == 422
        )


def test_provider_options_and_non_personal_disclosures(database):
    from tracker.research import ingest_disclosure, positions

    text = "Karen Finerman is long MSFT.\nShe is short calls in META, MSFT, and TLT."
    assert (
        ingest_disclosure(database, "karen-finerman", page(text), moment())["status"]
        == "accepted"
    )
    short = [p for p in positions(database, "karen-finerman") if p["side"] == "short"]
    assert len(short) == 3 and all(p["strategy"] == "calls" for p in short)
    guy = "Guy Adami is long MSFT.\nGuy Adami's wife, Linda Snow, works at Merck.\nGuy Adami is on the board of HLBZ."
    assert (
        ingest_disclosure(database, "guy-adami", page(guy), moment())["status"]
        == "accepted"
    )
    assert [p["symbol"] for p in positions(database, "guy-adami")] == ["MSFT"]
    # A family member's position cannot quietly become a personal holding.
    assert (
        ingest_disclosure(
            database,
            "guy-adami",
            page("Guy Adami is long MSFT.\nHis wife is long TLT."),
            moment(),
        )["status"]
        == "rejected"
    )


def test_scoped_review_records_correction_without_invalidating_other_strategy(database):
    from tracker.research import ingest_disclosure, review_position

    ingest_disclosure(database, "guy-adami", page("Guy Adami is long MSFT."), moment())
    review_position(database, "guy-adami", "MSFT", "unknown", "Source ambiguity")
    event = database.execute(
        "SELECT * FROM research_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["kind"] == "correction" and not event["eligible"]
    assert (
        database.execute(
            "SELECT count(*) FROM activity WHERE kind='correction'"
        ).fetchone()[0]
        == 1
    )
