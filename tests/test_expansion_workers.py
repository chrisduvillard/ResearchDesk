from datetime import datetime, timedelta
from tracker import db


def test_source_retry_budget_and_isolation(database):
    from tracker.sources import due, collect_source

    now = datetime.fromisoformat("2026-10-02T14:00:00+00:00")

    def broken(url):
        raise ValueError("offline")

    result = collect_source(
        "cnbc:karen-finerman", scheduled=True, now=now, download=broken
    )
    assert result["status"] == "error"
    assert not due(database, "cnbc:karen-finerman", now + timedelta(minutes=4))
    assert due(database, "cnbc:guy-adami", now)
    assert due(database, "cnbc:karen-finerman", now + timedelta(minutes=6))
    collect_source(
        "cnbc:karen-finerman",
        scheduled=True,
        now=now + timedelta(minutes=6),
        download=broken,
    )
    collect_source(
        "cnbc:karen-finerman",
        scheduled=True,
        now=now + timedelta(minutes=37),
        download=broken,
    )
    assert not due(database, "cnbc:karen-finerman", now + timedelta(hours=1))
    assert (
        database.execute(
            "SELECT count(*) FROM activity WHERE kind='source_failure'"
        ).fetchone()[0]
        == 1
    )


def test_dbmf_bridge_preserves_report_ids(database):
    from tracker.dbmf.store import ingest
    from tracker.funds import sync_dbmf
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures/dbmf/live.html").read_text()
    result = ingest(database, raw, datetime.fromisoformat("2026-10-01T23:00:00+00:00"))
    sync_dbmf(database)
    sync_dbmf(database)
    row = database.execute("SELECT * FROM fund_reports").fetchone()
    assert row["id"] == "DBMF:" + str(result["id"])
    assert row["raw_path"].startswith("dbmf/")
    assert database.execute("SELECT count(*) FROM fund_reports").fetchone()[0] == 1


def test_dbmf_changes_are_idempotent_and_same_contract_is_not_roll(database):
    from tracker.dbmf.store import ingest
    from tracker.funds import sync_dbmf, report_changes
    from tracker.briefing import alerts
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures/dbmf/live.html").read_text()
    first = ingest(database, raw, datetime.fromisoformat("2026-10-01T23:00:00+00:00"))
    # Source-date change, same contract identity. Report fixture is 2026-10-01.
    second_raw = (
        raw.replace("10/01/2026", "10/02/2026")
        .replace("10/1/2026", "10/2/2026")
        .replace("2026-10-01", "2026-10-02")
    )
    # Use the actual reporting-date token if its formatting differs.
    second_raw = second_raw.replace("-5,284,498,755.18", "-4,756,048,879.66").replace(
        ">-1.01<", ">-0.91<"
    )
    second = ingest(
        database, second_raw, datetime.fromisoformat("2026-10-02T23:00:00+00:00")
    )
    assert first["id"] != second["id"]
    sync_dbmf(database)
    sync_dbmf(database)
    changes = report_changes(database, "DBMF:" + str(second["id"]))
    assert changes and not any(r["contract_roll"] for r in changes)
    with database:
        database.execute("UPDATE sources SET coverage='qualified' WHERE id='fund:DBMF'")
    items = [
        dict(r)
        for r in database.execute("SELECT * FROM activity WHERE kind='exposure_change'")
    ]
    assert len(items) == 1 and len(alerts(database, items)) == 1


def test_dbmf_duplicate_lots_preserved_with_stable_contract_identity(database):
    from tracker.dbmf.store import ingest
    from tracker.funds import sync_dbmf
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures/dbmf/live.html").read_text()
    report = ingest(database, raw, datetime.fromisoformat("2026-10-01T23:00:00+00:00"))
    row = database.execute(
        "SELECT * FROM dbmf_holdings WHERE report_id=? LIMIT 1", (report["id"],)
    ).fetchone()
    fields = [k for k in row.keys() if k not in ("id", "row_number")]
    with database:
        database.execute(
            f"INSERT INTO dbmf_holdings(row_number,{','.join(fields)}) VALUES(999,{','.join('?' for _ in fields)})",
            [row[k] for k in fields],
        )
    count = database.execute("SELECT count(*) FROM dbmf_holdings").fetchone()[0]
    sync_dbmf(database)
    assert database.execute("SELECT count(*) FROM fund_holdings").fetchone()[0] == count


def test_dbmf_bridge_does_not_invent_acquisition_or_live_import_alerts(database):
    from tracker.dbmf.store import ingest
    from tracker.funds import sync_dbmf
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures/dbmf/live.html").read_text()
    report = ingest(database, raw, datetime.fromisoformat("2026-10-01T23:00:00+00:00"))
    with database:
        database.execute(
            "UPDATE dbmf_observations SET timestamp_basis='legacy_processing'"
        )
    sync_dbmf(database)
    bridged = database.execute(
        "SELECT * FROM fund_reports WHERE legacy_id=?", (report["id"],)
    ).fetchone()
    assert bridged["acquired_at"] is None
    assert (
        database.execute(
            "SELECT kind FROM activity WHERE dedup_key=?", ("fund:" + bridged["id"],)
        ).fetchone()[0]
        == "historical_report"
    )
