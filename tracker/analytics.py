"""Deterministic research models. No brokerage or actual-portfolio inference."""

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import date, datetime
import pandas as pd
from . import db
from .calendar import entry_session, horizon_date, completed, calendar

VERSION = "research-analytics-1"
DEFAULTS = dict(
    capital=100000.0,
    weight=0.1,
    max_positions=10,
    max_gross=1.0,
    cost_bps=5.0,
    borrow_rate=0.05,
    horizon=20,
    exit="fixed",
)


def metrics(values):
    return dict(
        n=len(values),
        mean=statistics.mean(values) if values else None,
        median=statistics.median(values) if values else None,
        win_rate=sum(v > 0 for v in values) / len(values) if values else None,
    )


def bootstrap(observations, horizon, seed=71023):
    """Non-overlapping exchange-session blocks; same-day observations stay together."""
    if len(observations) < 30:
        return None
    grouped = defaultdict(list)
    start = min(d for d, _ in observations)
    end = max(d for d, _ in observations)
    sessions = [s.date().isoformat() for s in calendar().sessions_in_range(start, end)]
    indices = {d: i for i, d in enumerate(sessions)}
    if len(sessions) < 3 * horizon:
        return None
    full_blocks = len(sessions) // horizon
    # Merge the trailing partial block into the last full block: all observations
    # remain represented and no block is shorter than the evaluated horizon.
    for day, value in observations:
        grouped[min(indices[day] // horizon, full_blocks - 1)].append(value)
    blocks = [grouped[i] for i in range(full_blocks) if grouped[i]]
    if len(blocks) < 3:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(1000):
        sample = [v for _ in blocks for v in rng.choice(blocks)]
        means.append(statistics.mean(sample))
    means.sort()
    return dict(
        lower=means[24],
        upper=means[974],
        confidence=0.95,
        seed=seed,
        blocks=len(blocks),
        block_sessions=horizon,
        method="descriptive block bootstrap",
    )


def scorecard(inputs):
    config = DEFAULTS | inputs.get("config", {})
    now = datetime.fromisoformat(inputs["as_of"])
    horizons = config.get("horizons", [1, 5, 20, 60])
    signals = []
    occupied = {}
    prices = inputs["prices"]
    assets = inputs.get("assets", {})
    benchmarks = inputs.get("benchmarks", {})
    cost = config["cost_bps"] / 10000
    for e in sorted(inputs["events"], key=lambda e: (e["available_at"], e["id"])):
        if not e["eligible"] or e["direction"] not in ("bullish", "bearish"):
            continue
        entry = entry_session(datetime.fromisoformat(e["available_at"]))
        results = {}
        for h in horizons:
            end = horizon_date(entry, h)
            key = (e["contributor_id"], e["evidence_type"], e["asset_id"], h)
            overlap = key in occupied and entry <= occupied[key]
            if not overlap:
                occupied[key] = end
            opening = prices.get(e["asset_id"], {}).get(entry)
            closing = prices.get(e["asset_id"], {}).get(end)
            state = (
                "overlap"
                if overlap
                else "pending"
                if not completed(end, now)
                else "missing_prices"
                if not opening or not closing
                else "complete"
            )
            result = dict(status=state, entry_date=entry, end_date=end)
            if state == "complete":
                underlying = closing["close"] / opening["open"] - 1
                direction = 1 if e["direction"] == "bullish" else -1
                days = (date.fromisoformat(end) - date.fromisoformat(entry)).days
                for mode, sign in [
                    ("follow", direction),
                    ("fade", -direction),
                    ("always_long", 1),
                ]:
                    result[mode] = underlying * sign
                    result[mode + "_net"] = (
                        underlying * sign
                        - cost * (2 + underlying)
                        - (config["borrow_rate"] * days / 365 if sign < 0 else 0)
                    )
                benchmark = benchmarks.get(e["asset_id"])
                bopen = prices.get(benchmark, {}).get(entry)
                bclose = prices.get(benchmark, {}).get(end)
                result["benchmark"] = (
                    bclose["close"] / bopen["open"] - 1 if bopen and bclose else None
                )
                result["benchmark_status"] = (
                    "complete"
                    if result["benchmark"] is not None
                    else "missing_prices"
                    if benchmark
                    else "unmapped"
                )
                result["excess"] = (
                    result["follow"] - result["benchmark"]
                    if result["benchmark"] is not None
                    else None
                )
            results[str(h)] = result
        signals.append(
            dict(
                event_id=e["id"],
                asset_id=e["asset_id"],
                asset_class=assets.get(e["asset_id"], {}).get("kind", "Unverified"),
                entry_date=entry,
                results=results,
            )
        )
    summary = []
    for asset_class in sorted({s["asset_class"] for s in signals}):
        for h in horizons:
            rows = [
                s["results"][str(h)] for s in signals if s["asset_class"] == asset_class
            ]
            done = [r for r in rows if r["status"] == "complete"]
            interval = bootstrap([(r["entry_date"], r["follow_net"]) for r in done], h)
            summary.append(
                dict(
                    asset_class=asset_class,
                    horizon=h,
                    n=len(done),
                    missing=sum(r["status"] == "missing_prices" for r in rows),
                    pending=sum(r["status"] == "pending" for r in rows),
                    overlaps=sum(r["status"] == "overlap" for r in rows),
                    benchmark=metrics(
                        [r["benchmark"] for r in done if r["benchmark"] is not None]
                    ),
                    excess=metrics(
                        [r["excess"] for r in done if r["excess"] is not None]
                    ),
                    missing_benchmarks=sum(r["benchmark"] is None for r in done),
                    interval=interval,
                    rankable=interval is not None,
                    **{
                        mode: metrics([r[mode] for r in done])
                        for mode in (
                            "follow",
                            "fade",
                            "always_long",
                            "follow_net",
                            "fade_net",
                            "always_long_net",
                        )
                    },
                )
            )
    return dict(
        version=VERSION,
        summary=summary,
        signals=signals,
        assumptions=config,
        methodology="Underlying adjusted-price directional research; first usable signal until each horizon ends. Costs and borrow are scenarios. Descriptive intervals are suppressed below 30 signals or three full blocks. Not actual trading performance.",
    )


def valid_bar(bar):
    return (
        bool(bar)
        and all(
            isinstance(bar.get(k), (int, float))
            and math.isfinite(bar[k])
            and bar[k] > 0
            for k in ("open", "close")
        )
        and all(
            math.isfinite(bar.get(k, 0)) and bar.get(k, 0) >= 0
            for k in ("split", "dividend")
        )
    )


def simulate(inputs, mode):
    c = DEFAULTS | inputs.get("config", {})
    if mode not in ("follow", "fade"):
        raise ValueError("Unknown direction mode")
    if (
        c["capital"] <= 0
        or not 0 < c["weight"] <= 1
        or not 1 <= c["max_positions"] <= 100
        or not 0 < c["max_gross"] <= 1
        or c["cost_bps"] < 0
        or c["borrow_rate"] < 0
        or c["horizon"] < 1
    ):
        raise ValueError("Invalid simulation assumptions")
    cash = float(c["capital"])
    costs = borrow = dividends = turnover = 0.0
    positions = {}
    trades = []
    skipped = []
    curve = []
    status = "complete"
    prices = inputs["prices"]
    sessions = inputs["sessions"]
    cost_rate = c["cost_bps"] / 10000
    byday = defaultdict(list)
    for event in sorted(inputs["events"], key=lambda e: (e["available_at"], e["id"])):
        if (
            inputs.get("forward_start")
            and event["available_at"] < inputs["forward_start"]
        ):
            continue
        byday[entry_session(datetime.fromisoformat(event["available_at"]))].append(
            event
        )
    peak = cash
    max_drawdown = 0.0
    last_day = None

    def close(asset, day, price, reason, event_id=None):
        nonlocal cash, costs, turnover
        p = positions.pop(asset)
        value = p["quantity"] * price
        fee = abs(value) * cost_rate
        cash += value - fee
        costs += fee
        turnover += abs(value)
        trades.append(
            dict(
                asset_id=asset,
                date=day,
                action="exit",
                quantity=-p["quantity"],
                price=price,
                cost=fee,
                reason=reason,
                event_id=event_id,
            )
        )

    for index, day in enumerate(sessions):
        opening = {}
        closing = {}
        missing = []
        for asset, p in positions.items():
            bar = prices.get(asset, {}).get(day)
            if not valid_bar(bar):
                missing.append(asset)
                continue
            # Borrow accrues on prior session's short market value over actual nights.
            if p["quantity"] < 0 and last_day:
                fee = (
                    abs(p["quantity"] * p["last_close"])
                    * c["borrow_rate"]
                    * (date.fromisoformat(day) - date.fromisoformat(last_day)).days
                    / 365
                )
                cash -= fee
                borrow += fee
            p["quantity"] *= bar.get("split", 0) or 1
            dividend = p["quantity"] * bar.get("dividend", 0)
            cash += dividend
            dividends += dividend
            opening[asset] = bar["open"]
            closing[asset] = bar["close"]
        if missing:
            status = "incomplete"
            curve.append(dict(date=day, equity=None, missing_assets=missing))
            break
        for event in byday.get(day, []):
            asset = event["asset_id"]
            direction = (
                1
                if event["direction"] == "bullish"
                else -1
                if event["direction"] == "bearish"
                else 0
            ) * (1 if mode == "follow" else -1)
            usable = (
                event.get("eligible_at_creation", event["eligible"])
                if inputs.get("forward_start")
                else event["eligible"]
            )
            invalidated = bool(
                inputs.get("forward_start")
                and event.get("invalidated_at")
                and datetime.fromisoformat(event["invalidated_at"])
                <= calendar().session_open(pd.Timestamp(day)).to_pydatetime()
            )
            if invalidated:
                skipped.append(
                    dict(
                        event_id=event["id"],
                        date=day,
                        reason="invalidated_before_entry",
                    )
                )
                continue
            p = positions.get(asset)
            kind = event["kind"]
            details = json.loads(event.get("details_json") or "{}")
            explicit_exit = (
                kind in ("close", "removed")
                and not details.get("conditions")
                and details.get("instrument_type") != "option"
                and details.get("exit_eligible", True)
            )
            if (
                c["exit"] == "signal"
                and p
                and (explicit_exit or usable and direction * p["quantity"] < 0)
            ):
                close(
                    asset,
                    day,
                    opening[asset],
                    "observed removal"
                    if kind == "removed"
                    else "opposing signal"
                    if not explicit_exit
                    else "explicit close",
                    event["id"],
                )
            if explicit_exit:
                continue
            reason = None
            if (
                not usable
                or not direction
                or details.get("instrument_type") == "option"
            ):
                reason = "ineligible"
            elif asset in positions:
                reason = "already_open"
            bar = prices.get(asset, {}).get(day)
            if not reason and not valid_bar(bar):
                reason = "missing_entry_price"
            if reason:
                skipped.append(dict(event_id=event["id"], date=day, reason=reason))
                continue
            equity = cash + sum(
                p["quantity"] * opening[a] for a, p in positions.items()
            )
            gross = sum(abs(p["quantity"] * opening[a]) for a, p in positions.items())
            restricted = sum(
                p["short_proceeds"] + p["short_margin"] for p in positions.values()
            )
            target = equity * c["weight"]
            fee = target * cost_rate
            if equity <= 0:
                reason = "nonpositive_equity"
            elif len(positions) >= c["max_positions"]:
                reason = "position_limit"
            elif gross + target > equity * c["max_gross"] + 1e-8:
                reason = "gross_exposure_limit"
            elif target + fee > cash - restricted + 1e-8:
                reason = "capital_limit"
            if reason:
                skipped.append(dict(event_id=event["id"], date=day, reason=reason))
                continue
            quantity = target / bar["open"] * direction
            cash -= quantity * bar["open"] + fee
            costs += fee
            turnover += target
            positions[asset] = dict(
                asset_id=asset,
                quantity=quantity,
                entry_index=index,
                entry_date=day,
                short_proceeds=target if quantity < 0 else 0,
                short_margin=target if quantity < 0 else 0,
                last_close=bar["close"],
            )
            opening[asset] = bar["open"]
            closing[asset] = bar["close"]
            trades.append(
                dict(
                    asset_id=asset,
                    event_id=event["id"],
                    date=day,
                    action="entry",
                    quantity=quantity,
                    price=bar["open"],
                    cost=fee,
                    reason="signal",
                )
            )
        limit = c["horizon"] if c["exit"] == "fixed" else 60
        for asset, p in list(positions.items()):
            if index - p["entry_index"] + 1 >= limit:
                close(
                    asset,
                    day,
                    closing[asset],
                    "fixed horizon" if c["exit"] == "fixed" else "60-session maximum",
                )
        equity = cash + sum(p["quantity"] * closing[a] for a, p in positions.items())
        gross = sum(abs(p["quantity"] * closing[a]) for a, p in positions.items())
        peak = max(peak, equity)
        drawdown = equity / peak - 1
        max_drawdown = min(max_drawdown, drawdown)
        curve.append(
            dict(
                date=day,
                equity=equity,
                cash=cash,
                gross_exposure=gross / equity if equity > 0 else None,
                drawdown=drawdown,
            )
        )
        for asset, p in positions.items():
            p["last_close"] = closing[asset]
        last_day = day
        if equity <= 0:
            status = "insolvent"
            break
    benchmark = buy_hold(inputs)
    return dict(
        benchmark=benchmark,
        coverage={
            "missing_entries": sum(
                s["reason"] == "missing_entry_price" for s in skipped
            ),
            "eligible_signals": sum(bool(e["eligible"]) for e in inputs["events"]),
            "open_positions": len(positions),
        },
        version=VERSION,
        label="Hypothetical CNBC " + mode,
        mode=mode,
        status=status,
        assumptions=c,
        equity_curve=curve,
        positions=list(positions.values()),
        trades=trades,
        skipped=skipped,
        costs=costs,
        borrowing=borrow,
        dividends=dividends,
        turnover=turnover / c["capital"],
        max_drawdown=max_drawdown if status != "incomplete" else None,
        total_return=(
            curve[-1]["equity"] / c["capital"] - 1
            if curve and status != "incomplete"
            else None
        ),
    )


def enqueue(conn, kind, inputs):
    document = json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    digest = hashlib.sha256((VERSION + kind + document).encode()).hexdigest()
    job = digest[:32]
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO analysis_runs(id,kind,status,created_at,inputs_json,input_hash) VALUES(?,?,'queued',?,?,?)",
            (job, kind, db.iso(), document, digest),
        )
    return job


def process_job(conn, job):
    with conn:
        changed = conn.execute(
            "UPDATE analysis_runs SET status='running',started_at=? WHERE id=? AND status='queued'",
            (db.iso(), job),
        ).rowcount
    if not changed:
        return
    row = conn.execute("SELECT * FROM analysis_runs WHERE id=?", (job,)).fetchone()
    try:
        inputs = json.loads(row["inputs_json"])
        result = (
            scorecard(inputs)
            if row["kind"] == "scorecard"
            else {mode: simulate(inputs, mode) for mode in ("follow", "fade")}
        )
        with conn:
            conn.execute(
                "UPDATE analysis_runs SET status='complete',finished_at=?,result_json=? WHERE id=?",
                (db.iso(), json.dumps(result, allow_nan=False), job),
            )
    except Exception as exc:
        with conn:
            conn.execute(
                "UPDATE analysis_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                (db.iso(), str(exc)[:1500], job),
            )


def freeze_inputs(
    conn, contributor, evidence, config, *, kind="scorecard", forward_start=None
):
    events = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM research_events WHERE contributor_id=? AND evidence_type=? AND (? OR call_revision_id IS NULL OR call_revision_id IN (SELECT current_revision FROM calls)) ORDER BY available_at,id",
            (contributor, evidence, bool(forward_start)),
        )
    ]
    from .research import canonical_id

    for event in events:
        event["original_asset_id"] = event["asset_id"]
        event["asset_id"] = canonical_id(conn, event["asset_id"])
    assets = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM assets")}
    benchmarks = {a: r["benchmark_id"] for a, r in assets.items() if r["benchmark_id"]}
    prices = {}
    batch_ids = []
    for asset in {e["asset_id"] for e in events} | set(benchmarks.values()):
        if kind == "simulation":
            batch = conn.execute(
                "SELECT * FROM price_batches WHERE kind='accounting' AND provider=? ORDER BY created_at DESC LIMIT 1",
                (asset,),
            ).fetchone()
            prices[asset] = json.loads(batch["inputs_json"]) if batch else {}
            if batch:
                batch_ids.append(batch["id"])
        else:
            batch = conn.execute(
                "SELECT * FROM price_batches WHERE kind='adjusted' AND provider=? ORDER BY created_at DESC LIMIT 1",
                (asset,),
            ).fetchone()
            if batch:
                prices[asset] = json.loads(batch["inputs_json"])
                batch_ids.append(batch["id"])
                continue
        if kind != "simulation" and asset.startswith("legacy:"):
            prices[asset] = {
                r["date"]: {"open": r["open"], "close": r["close"]}
                for r in conn.execute(
                    "SELECT * FROM prices WHERE symbol=? AND complete=1",
                    (asset.removeprefix("legacy:"),),
                )
            }
    now = db.utcnow()
    days = []
    eligible = [
        e
        for e in events
        if (
            e.get("eligible_at_creation", e["eligible"])
            if forward_start
            else e["eligible"]
        )
        and (not forward_start or e["available_at"] >= forward_start)
    ]
    if eligible:
        first = entry_session(datetime.fromisoformat(eligible[0]["available_at"]))
        if first <= now.date().isoformat():
            days = [
                s.date().isoformat()
                for s in calendar().sessions_in_range(first, now.date().isoformat())
                if completed(s.date().isoformat(), now)
            ]
    return dict(
        version=VERSION,
        events=events,
        assets=assets,
        benchmarks=benchmarks,
        prices=prices,
        price_batch_ids=batch_ids,
        as_of=db.iso(now),
        config=config,
        sessions=days,
        forward_start=forward_start,
    )


def buy_hold(inputs):
    mappings = inputs.get("benchmarks", {})
    assets = {e["asset_id"] for e in inputs["events"] if e["eligible"]}
    mapped = {mappings.get(a) for a in assets}
    if not assets or None in mapped or len(mapped) != 1:
        return {
            "status": "unmapped",
            "total_return": None,
            "reason": "One appropriate common benchmark must be mapped for all simulated assets",
        }
    benchmark = mapped.pop()
    bars = inputs["prices"].get(benchmark, {})
    sessions = inputs["sessions"]
    cash = 0.0
    quantity = None
    curve = []
    for day in sessions:
        bar = bars.get(day)
        if not valid_bar(bar):
            return {
                "status": "missing_prices",
                "total_return": None,
                "asset_id": benchmark,
            }
        if quantity is None:
            quantity = 1 / bar["open"]
        else:
            quantity *= bar.get("split", 0) or 1
            cash += quantity * bar.get("dividend", 0)
        curve.append({"date": day, "value": cash + quantity * bar["close"]})
    return {
        "status": "complete" if curve else "pending",
        "asset_id": benchmark,
        "total_return": curve[-1]["value"] - 1 if curve else None,
        "curve": curve,
        "assumptions": "Gross buy-and-hold; dividends held as cash",
    }
