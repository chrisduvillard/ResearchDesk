import gzip
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import db
from .calendar import chart_date
from .strategies import analyze, DIRECTIONS
from .payoff import calculate as calculate_payoff
from .instruments import ensure_instrument, resolve_instrument
from .parser import PARSER_VERSION, SOURCE_URL, ParseError, extract, parse_positions, aggregate


def latest_snapshot(conn):
    row = conn.execute("SELECT * FROM snapshots WHERE status='accepted' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def apply_reviews(conn, positions):
    result = [dict(p) for p in positions]
    for pos in result:
        if "analysis" not in pos:
            pos["analysis"] = analyze(pos["side"], pos["strategy"])
        detail = conn.execute("SELECT * FROM strategy_details WHERE symbol=? AND signature=? ORDER BY id DESC LIMIT 1", (pos["symbol"], pos["key"])).fetchone()
        if detail:
            payoff = calculate_payoff(json.loads(detail["model_json"]))
            analysis = dict(pos["analysis"], family="reviewed_legs", title="Sourced option legs",
                            direction=payoff["direction"], confidence="reviewed", score_eligible=False,
                            legs=payoff["model"]["legs"], same_expiry=payoff["model"]["same_expiry"],
                            net_cost=payoff["model"]["net_cost"], payoff_available=True,
                            explanation="Expiration payoff calculated from a sourced clarification of the legs.",
                            basis="Sourced clarification; quantities are as entered.",
                            volatility="Not calculated", time_effect="Not calculated",
                            missing=[] if payoff["model"]["net_cost"] is not None else ["Total entry cost for profit/loss and breakevens"],
                            assumptions=payoff["assumptions"], detail_source=detail["source"],
                            detail_reason=detail["reason"], detail_recorded=detail["created_at"])
            from .strategies import LABELS
            analysis["label"] = LABELS[analysis["direction"]]
            pos.update(analysis=analysis, direction=analysis["direction"], confidence="reviewed",
                       explanation=analysis["explanation"])
        review = conn.execute("SELECT * FROM reviews WHERE symbol=? AND signature=? ORDER BY id DESC LIMIT 1", (pos["symbol"], pos["key"])).fetchone()
        # Older records only have second precision. When their order cannot be
        # established, retain the manual review and hide the uncertain payoff.
        if review and (not detail or datetime.fromisoformat(review["created_at"]) >= datetime.fromisoformat(detail["created_at"])):
            from .strategies import LABELS
            pos["analysis"] = dict(pos["analysis"], direction=review["direction"], label=LABELS[review["direction"]], confidence="reviewed", score_eligible=False, payoff_available=False, explanation=review["reason"], missing=["Verified option legs for a payoff after the manual interpretation review"])
            pos.update(direction=review["direction"], confidence="reviewed", explanation=review["reason"])
    return result


def current_positions(conn):
    snap = latest_snapshot(conn)
    return apply_reviews(conn, json.loads(snap["positions_json"])) if snap else []


def grouped(positions):
    groups = defaultdict(list)
    for pos in positions:
        groups[pos["symbol"]].append(pos)
    return dict(groups)


def confidence(positions):
    levels = {p["confidence"] for p in positions}
    return next((x for x in ["reviewed", "uncertain", "inferred", "explicit"] if x in levels), "uncertain")


def add_event(conn, snapshot_id, symbol, before, after, observed, source_as_of, kind, eligible=False, reason=None):
    conn.execute('''INSERT INTO events(snapshot_id,symbol,observed_at,source_as_of,chart_date,kind,direction,previous_direction,eligible,confidence,before_json,after_json,raw_text,reason,parser_version)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (snapshot_id, symbol, db.iso(observed), source_as_of, chart_date(observed), kind, aggregate(after), aggregate(before), int(eligible), confidence(after), json.dumps(before), json.dumps(after), "; ".join(p["raw_text"] for p in after or before), reason, PARSER_VERSION))


def ingest(conn, html, observed=None, data_dir=None, resolve=True):
    observed = observed or db.utcnow()
    data_dir = Path(data_dir) if data_dir else db.DATA_DIR
    digest = hashlib.sha256(html.encode()).hexdigest()
    raw_path = f"snapshots/{digest}.html.gz"
    raw_file = data_dir / raw_path
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    if not raw_file.exists():
        temp = raw_file.with_suffix(".tmp")
        temp.write_bytes(gzip.compress(html.encode(), mtime=0))
        temp.replace(raw_file)
    as_of = None
    disclosure = None
    positions = None
    error = None
    status = "accepted"
    previous = latest_snapshot(conn)
    try:
        _, disclosure, as_of_dt = extract(html)
        as_of = db.iso(as_of_dt)
        if as_of_dt > observed + timedelta(minutes=5):
            raise ParseError("Source timestamp is in the future")
        positions = parse_positions(disclosure)
        if previous and as_of < previous["source_as_of"]:
            status, error = "older", "Older source timestamp; current positions preserved"
    except (ParseError, ValueError) as exc:
        status, error = "rejected", str(exc)
    if status == "accepted":
        for symbol in grouped(positions):
            ensure_instrument(conn, symbol)
        conn.commit()
        if resolve:
            for symbol in grouped(positions):
                try:
                    resolve_instrument(conn, symbol)
                except Exception as exc:
                    conn.execute("UPDATE instruments SET price_error=? WHERE symbol=?", (f"Name/listing verification: {exc}", symbol))
            conn.commit()
        positions = apply_reviews(conn, positions)
    with conn:
        cursor = conn.execute('''INSERT INTO snapshots(fetched_at,source_as_of,source_url,raw_path,content_hash,disclosure,status,error,parser_version,positions_json)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (db.iso(observed), as_of, SOURCE_URL, raw_path, digest, disclosure, status, error, PARSER_VERSION, json.dumps(positions) if positions is not None else None))
        snapshot_id = cursor.lastrowid
        if status != "accepted":
            return dict(id=snapshot_id, status=status, error=error, events=0)
        # Re-read inside the write transaction; a manual review may have happened
        # during instrument metadata retrieval.
        old = grouped(apply_reviews(conn, json.loads(previous["positions_json"]))) if previous else {}
        new = grouped(apply_reviews(conn, positions))
        count = 0
        for symbol in sorted(old.keys() | new.keys()):
            before, after = old.get(symbol, []), new.get(symbol, [])
            before_dir, after_dir = aggregate(before), aggregate(after)
            old_keys = sorted(p["key"] for p in before)
            new_keys = sorted(p["key"] for p in after)
            if previous and old_keys == new_keys and before_dir == after_dir:
                continue
            if previous is None:
                kind = "baseline"
            elif previous["parser_version"] != PARSER_VERSION and old_keys == new_keys:
                kind = "correction"
            elif not before:
                kind = "added"
            elif not after:
                kind = "removed"
            elif before_dir != after_dir:
                kind = "direction_changed"
            else:
                kind = "strategy_changed"
            verified = conn.execute("SELECT verified FROM instruments WHERE symbol=?", (symbol,)).fetchone()[0]
            eligible = bool(verified and kind in ("added", "direction_changed") and after_dir in ("bullish", "bearish") and confidence(after) != "reviewed" and all(p.get("analysis", {}).get("score_eligible", True) for p in after))
            add_event(conn, snapshot_id, symbol, before, after, observed, as_of, kind, eligible)
            count += 1
        return dict(id=snapshot_id, status=status, error=None, events=count)


def review(conn, symbol, direction, reason, observed=None):
    if direction not in DIRECTIONS or not reason.strip():
        raise ValueError("A supported direction and a reason are required")
    observed = observed or db.utcnow()
    snap = latest_snapshot(conn)
    before = grouped(current_positions(conn)).get(symbol)
    if not before:
        raise ValueError("The instrument is not in the latest disclosure")
    if len(before) != 1:
        raise ValueError("Multiple strategies require a per-strategy review; automatic overrides are disabled")
    with conn:
        conn.execute("INSERT INTO reviews(symbol,signature,direction,reason,created_at) VALUES(?,?,?,?,?)", (symbol, before[0]["key"], direction, reason, observed.astimezone(timezone.utc).isoformat(timespec="microseconds")))
        after = apply_reviews(conn, before)
        add_event(conn, snap["id"], symbol, before, after, observed, snap["source_as_of"], "correction", reason=reason)


def event_dict(row):
    result = dict(row)
    result["before"] = json.loads(result.pop("before_json"))
    result["after"] = json.loads(result.pop("after_json"))
    result["eligible"] = bool(result["eligible"])
    return result


def attach_details(conn, symbol, model, source, reason, signature=None, observed=None):
    if not source.strip() or not reason.strip():
        raise ValueError("A source and reason are required")
    validated = calculate_payoff(model)["model"]
    observed = observed or db.utcnow()
    snap = latest_snapshot(conn)
    before = grouped(current_positions(conn)).get(symbol, [])
    candidates = [p for p in before if not signature or p["key"] == signature]
    if len(candidates) != 1:
        raise ValueError("Select exactly one current strategy using its signature")
    with conn:
        conn.execute("INSERT INTO strategy_details(symbol,signature,model_json,source,reason,created_at) VALUES(?,?,?,?,?,?)",
                     (symbol, candidates[0]["key"], json.dumps(validated), source, reason, observed.astimezone(timezone.utc).isoformat(timespec="microseconds")))
        after = apply_reviews(conn, before)
        add_event(conn, snap["id"], symbol, before, after, observed, snap["source_as_of"],
                  "correction", reason=reason)
