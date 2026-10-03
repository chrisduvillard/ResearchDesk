"""Shared fund projections for the Exposure Desk. Units never cross measure groups."""

import csv
import io
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from fastapi import HTTPException
from . import db
from .dbmf.collector import schedule


def profile(conn, fund):
    r = conn.execute(
        "SELECT f.*,s.url AS source_url,s.coverage,s.reason FROM funds f JOIN sources s ON s.fund_id=f.id WHERE f.id=?",
        (fund,),
    ).fetchone()
    if not r:
        raise HTTPException(404, "Fund not found")
    return dict(r)


def reports(conn, fund, all_revisions=False):
    rows = conn.execute(
        "SELECT * FROM fund_reports WHERE fund_id=? AND status='accepted' AND complete=1 ORDER BY source_date,revision,id",
        (fund,),
    ).fetchall()
    return rows if all_revisions else list({r["source_date"]: r for r in rows}.values())


def report(conn, fund, identifier):
    r = conn.execute(
        "SELECT * FROM fund_reports WHERE fund_id=? AND id=? AND status='accepted' AND complete=1",
        (fund, identifier),
    ).fetchone()
    if not r:
        raise HTTPException(404, "Accepted report not found for this fund")
    return r


def key(row):
    return row["asset_id"] or "unmapped:" + row["instrument_key"]


def catalog(conn, fund):
    measure = (
        "equity_weight_pct"
        if profile(conn, fund)["kind"] == "equity"
        else "notional_pct_nav"
    )
    rows = conn.execute(
        "SELECT h.*,a.symbol,a.name AS asset_name,a.provider_symbol,a.verified FROM fund_holdings h JOIN fund_reports r ON r.id=h.report_id LEFT JOIN assets a ON a.id=h.asset_id WHERE r.fund_id=? AND r.status='accepted' AND r.complete=1 AND (h.measure=? OR h.collateral=1) ORDER BY r.source_date,r.revision",
        (fund, measure),
    ).fetchall()
    values = {}
    for r in rows:
        identifier = key(r)
        legacy = (
            conn.execute(
                "SELECT * FROM dbmf_markets WHERE id=?",
                (identifier.removeprefix("market:"),),
            ).fetchone()
            if identifier.startswith("market:")
            else None
        )
        m = (
            dict(legacy)
            if legacy
            else dict(
                name=r["asset_name"] or r["name"],
                category="Equities" if measure == "equity_weight_pct" else "Markets",
                provider_symbol=r["provider_symbol"] if r["verified"] else None,
                price_reference=r["asset_name"] or r["name"],
                price_kind="Stock" if measure == "equity_weight_pct" else "unavailable",
                invert=0,
            )
        )
        from .chart_prices import reference

        ref = reference(conn, identifier)
        if ref:
            m.update(
                provider_symbol=ref["symbol"],
                price_reference=ref["name"],
                price_kind=ref["kind"],
                invert=ref["invert"],
            )
        m.update(
            id=identifier,
            asset_id=r["asset_id"],
            symbol=r["symbol"],
            measure=r["measure"],
        )
        if r["collateral"]:
            m["category"] = "Collateral"
        values[identifier] = m
    return sorted(
        values.values(), key=lambda m: (m["category"] == "Collateral", m["name"])
    )


def snapshot(conn, fund, row, markets=None):
    if row is None:
        return None
    markets = markets if markets is not None else catalog(conn, fund)
    metadata = json.loads(row["metadata_json"])
    primary = (
        "equity_weight_pct"
        if profile(conn, fund)["kind"] == "equity"
        else "notional_pct_nav"
    )
    holdings = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM fund_holdings WHERE report_id=? ORDER BY id", (row["id"],)
        )
    ]
    result = dict(
        row,
        metadata=metadata,
        net_assets=metadata.get("net_assets"),
        source_kind="live",
        collected_at=row["acquired_at"],
        revision=row["revision"] + 1,
        mapping_version="Verified asset identities",
        source_url=profile(conn, fund)["source_url"],
        source_href="/api/v2/fund-reports/" + row["id"] + "/source",
        equity=primary == "equity_weight_pct",
    )
    result["markets"] = []
    for m in markets:
        subset = [
            h
            for h in holdings
            if key(h) == m["id"] and h["measure"] in (primary, "collateral_pct_nav")
        ]
        values = [Decimal(h["value"]) for h in subset]
        long = sum((v for v in values if v > 0), Decimal(0))
        short = sum((v for v in values if v < 0), Decimal(0))
        mapped = [
            dict(
                h,
                original_name=h["name"],
                market_id=m["id"],
                identifier=h["instrument_key"],
                ticker=m.get("symbol"),
                exposure_pct=h["value"],
                weight=None,
            )
            for h in subset
        ]
        result["markets"].append(
            dict(
                m,
                net_pct=str(long + short),
                long_pct=str(long),
                short_pct=str(short),
                gross_pct=str(long - short),
                net_notional=(
                    str(
                        sum(
                            (
                                Decimal(h["notional"])
                                for h in subset
                                if h["notional"] is not None
                            ),
                            Decimal(0),
                        )
                    )
                    if subset and all(h["notional"] is not None for h in subset)
                    else None
                ),
                holding_count=len(subset),
                holdings=mapped,
                absent=not subset,
            )
        )
    active = [m for m in result["markets"] if m["category"] != "Collateral"]
    collateral = [
        m
        for m in result["markets"]
        if m["category"] == "Collateral" and not m["absent"]
    ]
    result["summary"] = {
        field: str(sum((Decimal(m[field]) for m in active), Decimal(0)))
        for field in ("long_pct", "short_pct", "gross_pct")
    }
    result["summary"].update(
        collateral_pct=(
            str(sum(Decimal(m["net_pct"]) for m in collateral)) if collateral else None
        ),
        holdings=sum(not m["absent"] for m in active),
        top_five_pct=str(
            sum(sorted((Decimal(m["net_pct"]) for m in active), reverse=True)[:5])
        ),
    )
    result["risk_measures"] = [
        h for h in holdings if h["measure"] not in (primary, "collateral_pct_nav")
    ]
    return result


def exposures(conn, fund, compare="previous", compare_date=None, report_id=None):
    rows = reports(conn, fund)
    current = rows[-1] if rows else None
    target = None
    requested = None
    if report_id:
        target = report(conn, fund, report_id)
    elif current:
        day = date.fromisoformat(current["source_date"])
        if compare == "previous":
            target = rows[-2] if len(rows) > 1 else None
        else:
            if compare == "week":
                requested = (day - timedelta(days=7)).isoformat()
            elif compare == "month":
                end = day.replace(day=1) - timedelta(days=1)
                requested = end.replace(day=min(day.day, end.day)).isoformat()
            else:
                requested = compare_date
            target = next(
                (
                    r
                    for r in reversed(rows)
                    if requested and r["source_date"] <= requested
                ),
                None,
            )
    markets = catalog(conn, fund)
    now, previous = snapshot(conn, fund, current, markets), snapshot(
        conn, fund, target, markets
    )
    if now:
        old = {m["id"]: m for m in previous["markets"]} if previous else {}
        for m in now["markets"]:
            m["comparison_pct"] = old[m["id"]]["net_pct"] if old else None
            m["change_pp"] = (
                str(Decimal(m["net_pct"]) - Decimal(m["comparison_pct"]))
                if old
                else None
            )
            before = (
                {h["instrument_key"] for h in old[m["id"]]["holdings"]}
                if old
                else set()
            )
            after = {h["instrument_key"] for h in m["holdings"]}
            m["contract_roll"] = not now["equity"] and bool(
                before and after and before != after
            )
    return dict(
        current=now,
        comparison=previous,
        requested_date=requested,
        comparison_date=target["source_date"] if target else None,
        comparison_missing=target is None,
    )


def history(conn, fund):
    markets = catalog(conn, fund)
    observations = []
    for r in reports(conn, fund):
        s = snapshot(conn, fund, r, markets)
        observations.append(
            dict(
                id=r["id"],
                date=r["source_date"],
                source_kind="live",
                revision=r["revision"] + 1,
                net_assets=s["net_assets"],
                exposures={
                    m["id"]: {
                        k: m[k]
                        for k in (
                            "net_pct",
                            "long_pct",
                            "short_pct",
                            "gross_pct",
                            "absent",
                        )
                    }
                    for m in s["markets"]
                },
            )
        )
    return dict(markets=markets, observations=observations, interpolation=False)


def status(conn, fund):
    p = profile(conn, fund)
    rows = reports(conn, fund)
    now = db.utcnow()
    if fund == "DBMF":
        from .dbmf.api import status as legacy_status

        result = legacy_status()
        heartbeat = (
            db.setting(conn, "fund_worker_heartbeat") or result["worker_heartbeat"]
        )
        result.update(
            id=fund,
            name="DBMF",
            equity=False,
            worker_heartbeat=heartbeat,
            worker_healthy=bool(
                heartbeat
                and now - datetime.fromisoformat(heartbeat) < timedelta(minutes=10)
            ),
        )
        return result
    latest = rows[-1] if rows else None
    run = conn.execute(
        "SELECT * FROM source_runs WHERE source_id=? ORDER BY id DESC LIMIT 1",
        ("fund:" + fund,),
    ).fetchone()
    observed = conn.execute(
        "SELECT max(o.acquired_at) FROM fund_observations o JOIN fund_reports r ON r.id=o.report_id WHERE r.fund_id=?",
        (fund,),
    ).fetchone()[0]
    heartbeat = db.setting(conn, "fund_worker_heartbeat") or db.setting(
        conn, "dbmf_worker_heartbeat"
    )
    return dict(
        p,
        price_version=conn.execute(
            "SELECT max(created_at) FROM price_batches WHERE kind='chart' AND provider IN (SELECT h.asset_id FROM fund_holdings h JOIN fund_reports r ON r.id=h.report_id WHERE r.fund_id=?)",
            (fund,),
        ).fetchone()[0],
        equity=p["kind"] == "equity",
        current=snapshot(conn, fund, latest),
        server_time=db.iso(now),
        latest_report_id=latest["id"] if latest else None,
        catalog_version=1,
        review={"items": []},
        last_run=dict(run) if run else None,
        last_collected=observed,
        worker_healthy=bool(
            heartbeat
            and now - datetime.fromisoformat(heartbeat) < timedelta(minutes=10)
        ),
        collection_stale=not observed
        or now - datetime.fromisoformat(observed) > timedelta(hours=26),
        holdings_stale=not latest
        or (now.date() - date.fromisoformat(latest["source_date"])).days > 4,
        next_scheduled_run=db.iso(schedule(now)[1]),
        schedule="10:00 and 22:30 America/New_York, every day",
        last_backup=db.setting(conn, "last_backup"),
        backup_error=db.setting(conn, "backup_error"),
        historical_errors=[],
        coverage=dict(
            observations=len(rows),
            historical_reports=0,
            first_report=rows[0]["source_date"] if rows else None,
            latest_report=latest["source_date"] if latest else None,
            note="Only accepted reporting dates are shown. Positions between reports are unknown.",
            sec_access=[],
        ),
    )


def revisions(conn, fund):
    previous = {}
    items = []
    for r in reports(conn, fund, True):
        if r["source_date"] in previous:
            items.append(
                dict(
                    id=r["id"],
                    source_date=r["source_date"],
                    source_kind="live",
                    revision=r["revision"] + 1,
                    collected_at=r["acquired_at"],
                    previous_id=previous[r["source_date"]],
                )
            )
        previous[r["source_date"]] = r["id"]
    return dict(items=list(reversed(items)), has_more=False)


def revision(conn, fund, identifier):
    row = report(conn, fund, identifier)
    markets = catalog(conn, fund)
    current = snapshot(conn, fund, row, markets)
    earlier = [
        r
        for r in reports(conn, fund, True)
        if r["source_date"] == row["source_date"] and r["revision"] < row["revision"]
    ]
    previous = snapshot(conn, fund, earlier[-1], markets) if earlier else None
    result = dict(
        current=current,
        previous=previous,
        rows=[],
        markets=[],
        changed_rows=0,
        net_assets_changed=False,
        origin=None,
    )
    if previous is None:
        return result

    # Exact row identity and measure preserve collateral and risk revisions too.
    def all_rows(r):
        return {
            (h["instrument_key"], h["measure"]): dict(
                h,
                original_name=h["name"],
                market_id=h["asset_id"],
                identifier=h["instrument_key"],
                ticker=None,
                weight=None,
                exposure_pct=h["value"],
            )
            for h in conn.execute(
                "SELECT * FROM fund_holdings WHERE report_id=?", (r["id"],)
            )
        }

    old, new = all_rows(previous), all_rows(current)
    fields = (
        "market_id",
        "original_name",
        "identifier",
        "expiry",
        "quantity",
        "notional",
        "exposure_pct",
        "measure",
        "unit",
    )
    for key_ in sorted(old.keys() | new.keys()):
        a, b = old.get(key_), new.get(key_)
        changed = [k for k in fields if (a or {}).get(k) != (b or {}).get(k)]
        if changed:
            result["rows"].append(
                dict(
                    before=a,
                    after=b,
                    kind="changed" if a and b else "added" if b else "removed",
                    fields=changed,
                )
            )
    for a, b in zip(previous["markets"], current["markets"]):
        delta = Decimal(b["net_pct"]) - Decimal(a["net_pct"])
        if delta:
            result["markets"].append(
                dict(id=b["id"], name=b["name"], change_pp=str(delta))
            )
    result.update(
        changed_rows=len(result["rows"]),
        net_assets_changed=current["net_assets"] != previous["net_assets"],
        origin=(
            "reprocessed"
            if current["content_hash"] == previous["content_hash"]
            else (
                "source_and_processing"
                if current["parser_version"] != previous["parser_version"]
                else "source_changed"
            )
        ),
    )
    return result


def changes(conn, fund, baseline_id=None, since_id=None):
    maximum = conn.execute(
        "SELECT coalesce(max(o.id),0) FROM fund_observations o JOIN fund_reports r ON r.id=o.report_id WHERE r.fund_id=?",
        (fund,),
    ).fetchone()[0]
    reset = since_id is not None and since_id > maximum
    if reset:
        baseline_id = None
        since_id = None
    data = exposures(conn, fund, report_id=baseline_id)
    now, old = data["current"], data["comparison"]
    items = []
    if now and old:
        previous = {m["id"]: m for m in old["markets"]}
        for m in now["markets"]:
            before = previous[m["id"]]
            if (
                Decimal(m["change_pp"])
                or m["contract_roll"]
                or m["absent"] != before["absent"]
            ):
                items.append(
                    dict(
                        id=m["id"],
                        name=m["name"],
                        category=m["category"],
                        before_pct=m["comparison_pct"],
                        after_pct=m["net_pct"],
                        change_pp=m["change_pp"],
                        kind=(
                            "new"
                            if before["absent"] and not m["absent"]
                            else (
                                "absent"
                                if m["absent"] and not before["absent"]
                                else "changed"
                            )
                        ),
                        contract_roll=m["contract_roll"],
                    )
                )
        items.sort(key=lambda m: abs(Decimal(m["change_pp"])), reverse=True)
    revised = revisions(conn, fund)["items"]
    new_reports = None
    if since_id is not None:
        new_ids = {
            r[0]
            for r in conn.execute(
                "SELECT o.report_id FROM fund_observations o JOIN fund_reports r ON r.id=o.report_id WHERE r.fund_id=? GROUP BY o.report_id HAVING min(o.id)>?",
                (fund, since_id),
            )
        }
        revised = [r for r in revised if r["id"] in new_ids]
        new_reports = len(new_ids)
    else:
        revised = [r for r in revised if now and r["source_date"] == now["source_date"]]
    return dict(
        current=now,
        comparison=old,
        items=items,
        revisions=revised,
        revisions_more=False,
        new_reports=new_reports,
        cursor=dict(report_id=now["id"] if now else None, after_id=maximum),
        cursor_reset=reset,
    )


def export_csv(conn, fund):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        [
            "fund",
            "report_id",
            "source_date",
            "revision",
            "asset_id",
            "name",
            "measure",
            "value",
            "unit",
            "quantity",
            "notional",
            "expiry",
            "currency",
            "acquired_at",
            "source",
        ]
    )
    for r in reports(conn, fund, True):
        for h in conn.execute(
            "SELECT * FROM fund_holdings WHERE report_id=?", (r["id"],)
        ):
            row = [
                fund,
                r["id"],
                r["source_date"],
                r["revision"] + 1,
                *[
                    h[k]
                    for k in (
                        "asset_id",
                        "name",
                        "measure",
                        "value",
                        "unit",
                        "quantity",
                        "notional",
                        "expiry",
                        "currency",
                    )
                ],
                r["acquired_at"],
                "/api/v2/fund-reports/" + r["id"] + "/source",
            ]
            writer.writerow(
                [
                    (
                        "'" + v
                        if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                        else v
                    )
                    for v in row
                ]
            )
    return stream.getvalue()
