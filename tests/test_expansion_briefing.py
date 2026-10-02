from datetime import datetime
import pytest
from tracker import db


def test_cutoff_dst_and_immutable_briefing(database):
    from tracker.briefing import cutoff, generate, activity_page
    from tracker.research import activity

    assert (
        cutoff(datetime.fromisoformat("2026-03-29T04:59:00+00:00")).isoformat()
        == "2026-03-28T06:00:00+00:00"
    )
    assert cutoff(datetime.fromisoformat("2026-03-29T04:59:00+00:00"), hour=7).hour == 6
    # 07:00 Zurich after summer-time transition is 05:00 UTC.
    assert (
        cutoff(datetime.fromisoformat("2026-03-29T06:00:00+00:00")).isoformat()
        == "2026-03-29T05:00:00+00:00"
    )
    with database:
        activity(
            database,
            "x",
            "cnbc:dan-nathan",
            None,
            "added",
            "2026-01-01",
            "2026-01-02T05:00:00+00:00",
            "Test",
            "https://www.cnbc.com/",
            "event",
            1,
        )
    now = datetime.fromisoformat("2026-01-02T07:00:00+00:00")
    first = generate(database, now)
    second = generate(database, now)
    assert first == second
    page = activity_page(database, None, 1)
    assert len(page["items"]) == 1
    cursor = page["cursor"]
    assert not activity_page(database, cursor, 1)["items"]
    with database:
        db.set_setting(database, "activity_epoch", "restored")
    assert activity_page(database, cursor, 1)["cursor_reset"]


def test_alert_threshold_and_correction_exclusion():
    from tracker.briefing import should_alert

    rule = {
        "contributor_changes": True,
        "futures_pp": 5,
        "equity_pp": 1,
        "flip_min": 1,
        "source_health": True,
    }
    assert should_alert({"kind": "added", "details_json": "{}"}, rule)
    assert not should_alert({"kind": "correction", "details_json": "{}"}, rule)
    assert should_alert(
        {
            "kind": "exposure_change",
            "details_json": '{"measure":"notional_pct_nav","old":10,"new":15}',
        },
        rule,
    )
    assert not should_alert(
        {
            "kind": "exposure_change",
            "details_json": '{"measure":"notional_pct_nav","old":0.1,"new":-0.1}',
        },
        rule,
    )


def test_cross_source_disagreement_has_denominator_and_separate_streams(database):
    from tracker.research import ingest_disclosure
    from tracker.briefing import disagreements
    from conftest import page

    now = datetime.fromisoformat("2026-10-02T22:00:00+00:00")
    header = f"Disclosures as of {now.month}/{now.day}/{now.year} (4:30 AM ET):"
    # Fixed fixture before current time; acquisition remains current.
    header = "Disclosures as of 10/1/26 (4:30 PM ET):"
    ingest_disclosure(
        database, "karen-finerman", page("Karen Finerman is long MSFT.", header), now
    )
    ingest_disclosure(
        database, "guy-adami", page("Guy Adami is short MSFT.", header), now
    )
    result = disagreements(database, now)
    found = [r for r in result if r["asset_id"] == "legacy:MSFT"]
    assert len(found) == 1
    assert found[0]["evidence_type"] == "disclosure" and found[0]["denominator"] == 2
    assert found[0]["bullish"] == 1 and found[0]["bearish"] == 1
