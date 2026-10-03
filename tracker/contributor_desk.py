"""Contributor-scoped projections for the original Disclosure Desk interface."""

import csv
import io
import json
from datetime import datetime, timedelta
from fastapi import HTTPException
from . import db, research
from .history import aggregate, confidence, grouped
from .calendar import chart_date
from .collector import schedule
from .strategies import payoff_for


def profile(conn, contributor):
    row = conn.execute(
        "SELECT c.*,s.url AS source_url FROM contributors c JOIN sources s ON s.contributor_id=c.id WHERE c.id=?",
        (contributor,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Contributor not found")
    return dict(row)


def events(conn, contributor, symbol=None):
    result = []
    for r in conn.execute(
        "SELECT e.*,i.name,i.asset_class,d.parser_version FROM research_events e LEFT JOIN instruments i ON i.symbol=e.symbol LEFT JOIN disclosures d ON d.id=e.disclosure_id WHERE e.contributor_id=? AND e.evidence_type='disclosure' AND (? IS NULL OR e.symbol=?) ORDER BY e.available_at,e.id",
        (contributor, symbol, symbol),
    ):
        e = dict(r)
        detail = json.loads(e.pop("details_json"))
        before = [p for p in detail.get("before", []) if p["symbol"] == e["symbol"]]
        after = [p for p in detail.get("after", []) if p["symbol"] == e["symbol"]]
        e.update(
            before=before,
            after=after,
            name=e["name"] or e["symbol"],
            observed_at=e["available_at"],
            source_as_of=e["public_at"],
            chart_date=chart_date(datetime.fromisoformat(e["available_at"])),
            raw_text="; ".join(p["raw_text"] for p in after or before) or e["excerpt"],
            previous_direction=aggregate(before),
            confidence=confidence(after),
            snapshot_id=e["disclosure_id"],
            reason=detail.get("reason"),
            parser_version=e["parser_version"],
            source_href=f"/api/v2/disclosures/{e['disclosure_id']}/source",
        )
        result.append(e)
    return result


def instruments(conn, contributor):
    current = grouped(research.positions(conn, contributor))
    return [
        dict(r, active=r["symbol"] in current)
        for r in conn.execute(
            "SELECT * FROM instruments WHERE symbol IN (SELECT symbol FROM research_events WHERE contributor_id=? AND evidence_type='disclosure') ORDER BY name",
            (contributor,),
        )
    ]


def positions(conn, contributor):
    latest = research.latest(conn, contributor)
    catalog = {i["symbol"]: i for i in instruments(conn, contributor)}
    history = {e["symbol"]: e for e in events(conn, contributor)}
    return sorted(
        [
            dict(
                catalog[symbol],
                direction=aggregate(rows),
                confidence=confidence(rows),
                strategies=rows,
                source_as_of=latest["source_as_of"],
                last_change=history.get(symbol),
                expiry_note="Expiration day and year are not assumed.",
            )
            for symbol, rows in grouped(research.positions(conn, contributor)).items()
        ],
        key=lambda p: p["name"],
    )


def status(conn, contributor):
    p = profile(conn, contributor)
    snap = research.latest(conn, contributor)
    rows = positions(conn, contributor)
    now = db.utcnow()
    last = conn.execute(
        "SELECT * FROM source_runs WHERE source_id=? ORDER BY id DESC LIMIT 1",
        ("cnbc:" + contributor,),
    ).fetchone()
    if contributor == "dan-nathan":
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    heartbeat = db.setting(conn, "contributor_worker_heartbeat") or db.setting(
        conn, "worker_heartbeat"
    )
    observed = snap["acquired_at"] if snap else None
    source_at = snap["source_as_of"] if snap else None
    ev = events(conn, contributor)
    return dict(
        p,
        price_version=conn.execute(
            "SELECT max(created_at) FROM price_batches WHERE kind='chart' AND provider IN (SELECT asset_id FROM research_events WHERE contributor_id=?)",
            (contributor,),
        ).fetchone()[0],
        server_time=db.iso(now),
        source_as_of=source_at,
        last_observed=observed,
        tracking_started=conn.execute(
            "SELECT min(acquired_at) FROM disclosures WHERE contributor_id=? AND status='accepted'",
            (contributor,),
        ).fetchone()[0],
        next_scheduled_run=db.iso(schedule(now)[1]),
        schedule="22:15 America/New_York, every day",
        worker_heartbeat=heartbeat,
        worker_healthy=bool(
            heartbeat
            and now - datetime.fromisoformat(heartbeat) < timedelta(minutes=10)
        ),
        stale=not observed
        or now - datetime.fromisoformat(observed) > timedelta(hours=36),
        source_outdated=bool(
            source_at and now - datetime.fromisoformat(source_at) > timedelta(days=7)
        ),
        last_run=dict(last, price_errors={}) if last else None,
        last_backup=db.setting(conn, "last_backup"),
        backup_error=db.setting(conn, "backup_error"),
        active_instruments=len(rows),
        bullish=sum(p["direction"] == "bullish" for p in rows),
        bearish=sum(p["direction"] == "bearish" for p in rows),
        other=sum(p["direction"] not in ("bullish", "bearish") for p in rows),
        event_count=len(ev),
        eligible_signals=sum(e["eligible"] for e in ev),
        disclosure=snap["text"] if snap else None,
    )


def changes(conn, contributor, after_id=None):
    ev = events(conn, contributor)
    maximum = max((e["id"] for e in ev), default=0)
    snaps = conn.execute(
        "SELECT * FROM disclosures WHERE contributor_id=? AND status='accepted' ORDER BY id DESC LIMIT 2",
        (contributor,),
    ).fetchall()
    reset = after_id is not None and after_id > maximum
    if after_id is None or reset:
        selected = [e for e in ev if snaps and e["disclosure_id"] == snaps[0]["id"]]
    else:
        selected = [e for e in ev if e["id"] > after_id]

    def brief(row):
        return dict(
            id=row["id"],
            source_as_of=row["source_as_of"],
            fetched_at=row["acquired_at"],
        )

    return dict(
        current=brief(snaps[0]) if snaps else None,
        previous=brief(snaps[1]) if len(snaps) > 1 else None,
        items=list(reversed(selected)),
        total=len(selected),
        cursor={"after_id": maximum},
        cursor_reset=reset,
    )


def analysis(conn, contributor, symbol):
    item = next(
        (i for i in instruments(conn, contributor) if i["symbol"] == symbol), None
    )
    if not item:
        raise HTTPException(404, "Instrument not disclosed by this contributor")
    rows = grouped(research.positions(conn, contributor)).get(symbol, [])
    result = []
    for p in rows:
        a = dict(p["analysis"])
        if contributor != "dan-nathan" and p["confidence"] == "reviewed":
            a.update(
                direction=p["direction"],
                confidence="reviewed",
                score_eligible=False,
                payoff_available=False,
                explanation=p["explanation"],
            )
        result.append(
            dict(
                signature=p["key"],
                wording=p["raw_text"],
                side=p["side"],
                analysis=a,
                payoff=payoff_for(a) if a.get("payoff_available") else None,
            )
        )
    return dict(name=item["name"], symbol=symbol, strategies=result)


def scores(conn, contributor, symbol=None):
    """Read frozen worker results; a page request never runs a simulation/bootstrap."""
    from .analytics import metrics

    stored = None
    for run in conn.execute(
        "SELECT * FROM analysis_runs WHERE kind='scorecard' AND status='complete' ORDER BY created_at DESC"
    ):
        inputs = json.loads(run["inputs_json"])
        ev = inputs.get("events", [])
        if (
            ev
            and set((1, 5, 20, 60)).issubset(
                inputs.get("config", {}).get("horizons", [1, 5, 20, 60])
            )
            and all(
                e["contributor_id"] == contributor
                and e["evidence_type"] == "disclosure"
                for e in ev
            )
        ):
            stored = dict(run)
            break
    if not stored:
        return dict(
            summary=[
                dict(
                    horizon=h,
                    asset_class="Awaiting observations",
                    follow=metrics([]),
                    oppose=metrics([]),
                    always_long=metrics([]),
                    follow_net=metrics([]),
                    oppose_net=metrics([]),
                    always_long_net=metrics([]),
                    pending=0,
                    missing=0,
                    overlaps=0,
                )
                for h in (1, 5, 20, 60)
            ],
            signals=[],
            methodology="Scorecards are prepared by the analytics worker. Initial disclosures are a baseline, not scored signals.",
            run_id=None,
        )
    result = json.loads(stored["result_json"])
    inputs = json.loads(stored["inputs_json"])
    lookup = {e["id"]: e for e in inputs["events"]}
    signals = []
    for s in result["signals"]:
        e = lookup[s["event_id"]]
        if symbol and e["symbol"] != symbol:
            continue
        signals.append(
            dict(
                s,
                name=inputs["assets"].get(s["asset_id"], {}).get("name", e["symbol"]),
                direction=e["direction"],
                observed_at=e["available_at"],
            )
        )
    summary = []
    classes = sorted({s["asset_class"] for s in signals}) or ["Awaiting observations"]
    for category in classes:
        for h in (1, 5, 20, 60):
            outcomes = [
                s["results"][str(h)] for s in signals if s["asset_class"] == category
            ]
            done = [r for r in outcomes if r["status"] == "complete"]
            row = dict(
                horizon=h,
                asset_class=category,
                pending=sum(r["status"] == "pending" for r in outcomes),
                missing=sum(r["status"] == "missing_prices" for r in outcomes),
                overlaps=sum(r["status"] == "overlap" for r in outcomes),
            )
            for label, mode in [
                ("follow", "follow"),
                ("oppose", "fade"),
                ("always_long", "always_long"),
                ("follow_net", "follow_net"),
                ("oppose_net", "fade_net"),
                ("always_long_net", "always_long_net"),
            ]:
                row[label] = metrics([r[mode] for r in done])
            summary.append(row)
    return dict(
        summary=summary,
        signals=signals,
        methodology=result["methodology"],
        assumptions=result["assumptions"],
        run_id=stored["id"],
        as_of=inputs["as_of"],
        cohorts=result["summary"] if not symbol else [],
    )


def export_csv(conn, contributor):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        [
            "contributor",
            "event_id",
            "instrument",
            "symbol",
            "event",
            "direction",
            "first_observed",
            "source_date",
            "wording",
            "source",
        ]
    )
    for e in events(conn, contributor):
        row = [
            profile(conn, contributor)["name"],
            e["id"],
            e["name"],
            e["symbol"],
            e["kind"],
            e["direction"],
            e["observed_at"],
            e["source_as_of"],
            e["raw_text"],
            e["source_href"],
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


def export_pine(conn, contributor, symbol):
    from .export import clean, LIMIT, MAX_EVENTS

    item = next(
        (i for i in instruments(conn, contributor) if i["symbol"] == symbol), None
    )
    if not item or not item["tv_symbol"]:
        raise HTTPException(400, "A verified TradingView mapping is required")
    ev = events(conn, contributor, symbol)
    if not ev:
        raise HTTPException(400, "No observations to export")
    lines = [
        "|".join(
            clean(v)
            for v in (
                e["chart_date"].replace("-", ""),
                e["kind"],
                e["direction"],
                e["source_as_of"],
                e["observed_at"],
                e["raw_text"][:500],
            )
        )
        for e in ev
    ]
    selected = lines[-MAX_EVENTS:]

    def pack():
        return (
            "|".join(
                clean(v)
                for v in (
                    "DN2",
                    item["tv_symbol"],
                    profile(conn, contributor)["name"] + " · " + item["name"],
                    db.iso(),
                    ev[-1]["source_as_of"],
                    len(lines),
                    len(selected),
                    selected[0].split("|")[0],
                )
            )
            + "\n"
            + "\n".join(selected)
        )

    while len(pack()) > LIMIT and len(selected) > 1:
        selected.pop(0)
    return pack()
