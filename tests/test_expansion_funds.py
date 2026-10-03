from datetime import datetime
from pathlib import Path
import pytest
from tracker import db

FIX = Path(__file__).parent / "fixtures" / "funds"


def test_kmlm_units_signs_and_completeness():
    from tracker.funds import parse_kmlm

    doc = parse_kmlm(
        (FIX / "kmlm.csv").read_bytes(),
        net_assets="562525502",
        expected_date="2026-10-01",
    )
    assert doc["source_date"] == "2026-10-01"
    futures = [r for r in doc["holdings"] if r["measure"] == "notional_pct_nav"]
    assert len(futures) == 22
    short = next(r for r in futures if r["name"].startswith("GOLD"))
    assert float(short["value"]) == pytest.approx(-6.1257384, abs=0.0001)
    assert short["quantity"] == "-82"
    assert short["expiry"] == "2026-12"
    assert any(r["measure"] == "collateral_pct_nav" for r in doc["holdings"])
    raw = (FIX / "kmlm.csv").read_bytes()
    with pytest.raises(ValueError):
        parse_kmlm(
            raw.replace(b"KMLM Holdings", b"OTHER Holdings"),
            net_assets="562525502",
            expected_date="2026-10-01",
        )
    with pytest.raises(ValueError):
        parse_kmlm(raw, net_assets="562525502", expected_date="2026-10-02")
    with pytest.raises(ValueError):
        parse_kmlm(
            b"\n".join(raw.splitlines()[:-5]),
            net_assets="562525502",
            expected_date="2026-10-01",
            expected_rows=32,
        )


def test_cta_notional_and_risk_separate():
    from tracker.funds import parse_cta

    doc = parse_cta((FIX / "cta.xlsx").read_bytes(), expected_date="2026-10-01")
    note = next(
        r
        for r in doc["holdings"]
        if r["asset_id"] == "market:us2y" and r["measure"] == "notional_pct_nav"
    )
    assert float(note["value"]) == pytest.approx(-50.9197784133, abs=0.00001)
    assert note["notional"] == "-687392581.5"
    assert any(r["measure"] == "issuer_risk_weight_pct" for r in doc["holdings"])
    assert any(r["measure"] == "volatility_contribution_pct" for r in doc["holdings"])
    assert not any(r["measure"] == "equity_weight_pct" for r in doc["holdings"])


def test_fund_duplicate_revision_partial_and_qualification(database):
    from tracker.funds import accept_report, qualify, comparison

    def doc(day="2026-10-01", value="20"):
        return dict(
            source_date=day,
            complete=True,
            metadata={},
            holdings=[
                dict(
                    asset_id="market:gold",
                    name="Gold",
                    instrument_key="GCZ6",
                    measure="notional_pct_nav",
                    value=value,
                    unit="percent_nav",
                    quantity="1",
                    notional="100",
                    expiry="2026-12",
                    currency="USD",
                    collateral=False,
                    evidence="Gold",
                )
            ],
        )

    one = accept_report(
        database,
        "CTA",
        doc(),
        b"first",
        datetime.fromisoformat("2026-10-02T12:00:00+00:00"),
    )
    two = accept_report(
        database,
        "CTA",
        doc(),
        b"first",
        datetime.fromisoformat("2026-10-02T13:00:00+00:00"),
    )
    assert one["id"] == two["id"]
    revised = accept_report(database, "CTA", doc(value="25"), b"revised")
    assert revised["revision"] == 1
    with pytest.raises(ValueError):
        accept_report(database, "CTA", dict(doc(), complete=False), b"partial")
    assert len(comparison(database, ["CTA", "KMLM"], "notional_pct_nav")["rows"]) == 2
    missing = comparison(database, ["CTA", "KMLM"], "notional_pct_nav")["rows"][1]
    assert (
        missing["source_date"] is None
        and missing["cells"]["market:gold"]["value"] is None
    )
    for i in range(5):
        with database:
            database.execute(
                "INSERT INTO source_runs(source_id,started_at,status,slot,report_date,scheduled) VALUES('fund:CTA',?,'success',?,?,1)",
                (
                    f"2026-10-0{i + 1}T12:00:00+00:00",
                    str(i),
                    "2026-10-01" if i < 3 else "2026-10-02",
                ),
            )
    assert qualify(database, "fund:CTA")
    with database:
        database.execute(
            "INSERT INTO source_runs(source_id,started_at,status,slot,scheduled) VALUES('fund:CTA','2026-10-06T12:00:00+00:00','error','5',1)"
        )
    assert not qualify(database, "fund:CTA")


@pytest.mark.parametrize("fund", ["ARKK", "ARKQ", "ARKW", "ARKG", "ARKF", "ARKX"])
def test_ark_identity_total_and_distinct_securities(fund):
    from tracker.funds import parse_ark

    raw = (FIX / (fund + ".csv")).read_bytes()
    doc = parse_ark(raw, fund)
    assert doc["source_date"] == "2026-10-02"
    assert 98 < sum(float(h["value"]) for h in doc["holdings"]) < 102
    assert len({h["instrument_key"] for h in doc["holdings"]}) == len(doc["holdings"])
    assert all(h["asset_id"].startswith(("cusip:", "ark-id:")) for h in doc["holdings"])
    with pytest.raises(ValueError):
        parse_ark(raw, "OTHER")
    with pytest.raises(ValueError):
        parse_ark(b"\n".join(raw.splitlines()[:5]), fund)


def test_roll_changes_and_corrections_do_not_become_trades(database):
    from tracker.funds import accept_report, report_changes

    def doc(day, key, value):
        return dict(
            source_date=day,
            complete=True,
            metadata={},
            holdings=[
                dict(
                    asset_id="market:gold",
                    name="Gold",
                    instrument_key=key,
                    measure="notional_pct_nav",
                    value=value,
                    unit="percent_nav",
                    quantity="1",
                    notional="100",
                    expiry=day[:7],
                    currency="USD",
                    collateral=False,
                    evidence="Gold",
                )
            ],
        )

    accept_report(database, "CTA", doc("2026-09-29", "GCZ6", "10"), b"one")
    latest = accept_report(database, "CTA", doc("2026-09-30", "GCG7", "16"), b"two")
    changes = report_changes(database, latest["id"])
    assert changes[0]["contract_roll"]
    assert changes[0]["change_pp"] == 6
    assert changes[0]["kind"] == "exposure_change"
    corrected = accept_report(
        database, "CTA", doc("2026-09-30", "GCG7", "30"), b"correction"
    )
    assert all(
        c["kind"] == "correction" for c in report_changes(database, corrected["id"])
    )


def test_complete_removal_is_zero_not_missing(database):
    from tracker.funds import accept_report, comparison

    def h(asset, value):
        return dict(
            asset_id=asset,
            name=asset,
            instrument_key=asset,
            measure="notional_pct_nav",
            value=value,
            unit="percent_nav",
            quantity="1",
            notional="1",
            expiry="2026-12",
            currency="USD",
            collateral=False,
            evidence="fixture",
        )

    accept_report(
        database,
        "CTA",
        dict(
            source_date="2026-09-29",
            complete=True,
            metadata={},
            holdings=[h("market:gold", "10"), h("market:wti", "20")],
        ),
        b"first",
    )
    accept_report(
        database,
        "CTA",
        dict(
            source_date="2026-09-30",
            complete=True,
            metadata={},
            holdings=[h("market:wti", "20")],
        ),
        b"second",
    )
    cell = comparison(database, ["CTA"], "notional_pct_nav")["rows"][0]["cells"][
        "market:gold"
    ]
    assert cell == {"value": 0, "change_pp": -10}


def test_cta_soybean_oil_is_distinct_from_soybeans():
    from tracker.funds import parse_cta

    doc = parse_cta(
        (FIX / "cta-2026-10-02.xlsx").read_bytes(), expected_date="2026-10-02"
    )
    oil = [r for r in doc["holdings"] if r["asset_id"] == "market:soybean-oil"]
    assert {r["measure"] for r in oil} >= {"notional_pct_nav", "issuer_risk_weight_pct"}
    assert all(r["asset_id"] != "market:soybeans" for r in oil)


def test_kmlm_reported_weights_remain_separate_from_published_nav():
    from tracker.funds import parse_kmlm

    raw = (FIX / "kmlm-2026-10-02.csv").read_bytes()
    doc = parse_kmlm(
        raw, net_assets="560096908", expected_date="2026-10-02", expected_rows=32
    )
    cash = next(
        h
        for h in doc["holdings"]
        if h["name"] == "Cash" and h["measure"] == "collateral_pct_nav"
    )
    issuer = next(
        h
        for h in doc["holdings"]
        if h["name"] == "Cash" and h["measure"] == "issuer_portfolio_weight_pct"
    )
    assert float(cash["value"]) == pytest.approx(184245603 / 560096908 * 100)
    assert float(issuer["value"]) == 32.62
    with pytest.raises(ValueError):
        parse_kmlm(
            raw.replace(b"Cash,32.62", b"Cash,3.262"),
            net_assets="560096908",
            expected_date="2026-10-02",
            expected_rows=32,
        )


def test_kmlm_requires_nav_date_to_match_holdings(database):
    from tracker.sources import fetch_fund

    page = (
        "<p>Fund Details Data as of 10/02/2026 Net Assets $562,525,502</p>"
        '<a href="https://kraneshares.com/csv/10_01_2026_kmlm_holdings.csv">CSV</a>'
        "<table><tr><th>Notional Value($)</th></tr>"
        + "".join(f"<tr><td>{i}</td></tr>" for i in range(1, 33))
        + "</table>"
    ).encode()
    source = {
        "fund_id": "KMLM",
        "url": "https://kraneshares.com/etf/kmlm/",
        "adapter": "kmlm",
    }
    with pytest.raises(ValueError, match="NAV.*date"):
        fetch_fund(
            source,
            lambda url: (FIX / "kmlm.csv").read_bytes() if ".csv" in url else page,
        )


def test_rejected_workbook_remains_archived(database):
    import gzip, hashlib
    from zipfile import BadZipFile
    from tracker.sources import fetch_fund

    raw = b"broken workbook download"
    page = b'<a href="https://www.simplify.us/sites/default/files/excel_holdings/2026_10_02_Simplify_Portfolio_EOD_Tracker.xlsx">Holdings</a>'
    with pytest.raises(BadZipFile):
        fetch_fund(
            {
                "fund_id": "CTA",
                "url": "https://www.simplify.us/etfs/cta",
                "adapter": "cta",
            },
            lambda url: raw if ".xlsx" in url else page,
        )
    path = db.DATA_DIR / "funds" / (hashlib.sha256(raw).hexdigest() + ".gz")
    assert path.exists() and gzip.decompress(path.read_bytes()) == raw
