"""Deterministic cutoff snapshots and durable per-browser activity cursors."""

import base64
import json
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
from . import db


def cutoff(now, hour=7):
    local = now.astimezone(ZoneInfo("Europe/Zurich"))
    day = (
        local.date() if local.time() >= time(hour) else local.date() - timedelta(days=1)
    )
    return datetime.combine(day, time(hour), local.tzinfo).astimezone(timezone.utc)


def generate(conn, now=None):
    now = now or db.utcnow()
    end = cutoff(now)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM briefings WHERE cutoff=?", (db.iso(end),)
        ).fetchone()
        if existing:
            return dict(existing)
        last = conn.execute(
            "SELECT cutoff FROM briefings WHERE cutoff<? ORDER BY cutoff DESC LIMIT 1",
            (db.iso(end),),
        ).fetchone()
        previous = last[0] if last else db.iso(cutoff(end - timedelta(seconds=1)))
        rows = conn.execute(
            "SELECT id FROM activity WHERE recorded_at>? AND recorded_at<=? ORDER BY id",
            (previous, db.iso(end)),
        ).fetchall()
        conn.execute(
            "INSERT INTO briefings(cutoff,previous_cutoff,created_at,activity_ids_json) VALUES(?,?,?,?)",
            (db.iso(end), previous, db.iso(now), json.dumps([r[0] for r in rows])),
        )
        return dict(
            conn.execute(
                "SELECT * FROM briefings WHERE cutoff=?", (db.iso(end),)
            ).fetchone()
        )


def encode(epoch, identifier):
    return (
        base64.urlsafe_b64encode(
            json.dumps([epoch, identifier], separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )


def activity_page(
    conn, cursor, limit=100, *, current=False, source_id=None, asset_id=None
):
    epoch = db.setting(conn, "activity_epoch")
    maximum = conn.execute("SELECT coalesce(max(id),0) FROM activity").fetchone()[0]
    after = 0
    reset = False
    if cursor:
        try:
            if len(cursor) > 200:
                raise ValueError("cursor too long")
            old, after = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )
            if type(after) != int or after < 0:
                raise ValueError("bad cursor")
            if old != epoch or after > maximum:
                after = maximum
                reset = True
        except Exception as exc:
            raise ValueError("Invalid activity cursor") from exc
    if current:
        return dict(
            items=[], cursor=encode(epoch, maximum), cursor_reset=reset, has_more=False
        )
    from .research import related_ids

    asset_ids = json.dumps(related_ids(conn, asset_id)) if asset_id else "[]"
    rows = conn.execute(
        """SELECT * FROM activity WHERE id>? AND (? IS NULL OR source_id=?) AND (? IS NULL OR asset_id IN (SELECT value FROM json_each(?)))
      ORDER BY id LIMIT ?""",
        (after, source_id, source_id, asset_id, asset_ids, limit + 1),
    ).fetchall()
    items = [dict(r) for r in rows[:limit]]
    return dict(
        items=items,
        cursor=encode(epoch, items[-1]["id"] if items else max(after, maximum)),
        cursor_reset=reset,
        has_more=len(rows) > limit,
    )


def should_alert(item, rule):
    kind = item["kind"]
    details = json.loads(item.get("details_json") or "{}")
    if kind in ("added", "removed", "direction_changed", "call"):
        return rule.get("contributor_changes", True)
    if kind in ("source_failure", "source_overdue", "source_recovery"):
        return rule.get("source_health", True)
    if (
        kind != "exposure_change"
        or details.get("old") is None
        or details.get("new") is None
    ):
        return False
    old, new = details["old"], details["new"]
    measure = details["measure"]
    if measure not in ("notional_pct_nav", "equity_weight_pct"):
        return False
    threshold = (
        rule.get("futures_pp", 5)
        if measure == "notional_pct_nav"
        else rule.get("equity_pp", 1)
    )
    return (
        abs(new - old) >= threshold
        or old * new < 0
        and min(abs(old), abs(new)) > rule.get("flip_min", 1)
    )


def alerts(conn, items):
    rules = [dict(r) for r in conn.execute("SELECT * FROM alert_rules ORDER BY id")]
    result = []
    for item in items:
        source = conn.execute(
            "SELECT coverage FROM sources WHERE id=?", (item["source_id"],)
        ).fetchone()
        if item["kind"] == "exposure_change" and (
            not source or source[0] != "qualified"
        ):
            continue
        matching = [
            r
            for r in rules
            if (r["source_id"] is None or r["source_id"] == item["source_id"])
            and (r["asset_id"] is None or r["asset_id"] == item["asset_id"])
        ]
        matching.sort(
            key=lambda r: (bool(r["source_id"]) + bool(r["asset_id"]), r["id"]),
            reverse=True,
        )
        if (
            matching
            and matching[0]["enabled"]
            and should_alert(item, json.loads(matching[0]["config_json"]))
        ):
            result.append(item)
    return result


def disagreements(conn, now=None):
    from .research import positions, canonical_id
    from .parser import aggregate
    from collections import defaultdict

    now = now or db.utcnow()
    votes = defaultdict(list)
    for contributor in conn.execute("SELECT id FROM contributors"):
        snapshot = conn.execute(
            "SELECT * FROM disclosures WHERE contributor_id=? AND status='accepted' ORDER BY id DESC LIMIT 1",
            (contributor[0],),
        ).fetchone()
        if (
            snapshot
            and now - datetime.fromisoformat(snapshot["acquired_at"])
            <= timedelta(hours=36)
            and now - datetime.fromisoformat(snapshot["source_as_of"])
            <= timedelta(days=7)
        ):
            grouped = defaultdict(list)
            for p in positions(conn, contributor[0]):
                grouped[canonical_id(conn, "legacy:" + p["symbol"])].append(p)
            for asset, items in grouped.items():
                direction = aggregate(items)
                if direction in ("bullish", "bearish"):
                    votes[(asset, "disclosure")].append(
                        {
                            "contributor_id": contributor[0],
                            "direction": direction,
                            "reference": snapshot["id"],
                        }
                    )
        events = conn.execute(
            """SELECT e.* FROM research_events e JOIN calls c ON c.current_revision=e.call_revision_id
          WHERE e.contributor_id=? AND e.evidence_type='call' ORDER BY available_at DESC,id DESC""",
            (contributor[0],),
        ).fetchall()
        seen = set()
        for e in events:
            asset = canonical_id(conn, e["asset_id"])
            if asset in seen:
                continue
            seen.add(asset)
            if e["eligible"] and now - datetime.fromisoformat(
                e["available_at"]
            ) <= timedelta(days=30):
                votes[(asset, "call")].append(
                    {
                        "contributor_id": contributor[0],
                        "direction": e["direction"],
                        "reference": e["id"],
                    }
                )
    result = []
    for (asset, evidence), items in sorted(votes.items()):
        up = sum(v["direction"] == "bullish" for v in items)
        down = sum(v["direction"] == "bearish" for v in items)
        if up and down:
            result.append(
                dict(
                    asset_id=asset,
                    evidence_type=evidence,
                    bullish=up,
                    bearish=down,
                    denominator=len(items),
                    votes=items,
                )
            )
    return result
