"""Scoped disclosure evidence and a lossless bridge from legacy records."""

import gzip
import hashlib
import json
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from . import db
from .auth import audit
from .parser import extract, parse_positions, aggregate, PARSER_VERSION
from .history import grouped


def archive_bytes(raw, namespace="sources"):
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"{namespace}/{digest}.gz"
    path = db.DATA_DIR / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(gzip.compress(raw, mtime=0))
        temporary.replace(path)
    return relative, digest


def activity(
    conn,
    key,
    source,
    asset,
    kind,
    public,
    available,
    title,
    url,
    reference_type,
    reference_id,
    details=None,
):
    conn.execute(
        """INSERT OR IGNORE INTO activity(dedup_key,source_id,asset_id,kind,public_at,available_at,
      recorded_at,title,evidence_url,reference_type,reference_id,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            key,
            source,
            asset,
            kind,
            public,
            available,
            available,
            title,
            url,
            reference_type,
            str(reference_id),
            json.dumps(details or {}),
        ),
    )


def sync_assets(conn):
    for r in conn.execute("SELECT * FROM instruments").fetchall():
        conn.execute(
            """INSERT INTO assets(id,symbol,name,kind,currency,exchange,provider_symbol,verified,provenance)
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,
          currency=excluded.currency,exchange=excluded.exchange,provider_symbol=excluded.provider_symbol,
          verified=excluded.verified,provenance=excluded.provenance""",
            (
                "legacy:" + r["symbol"],
                r["symbol"],
                r["name"],
                r["asset_class"],
                r["currency"],
                r["exchange"],
                r["provider_symbol"],
                r["verified"],
                r["name_source"],
            ),
        )


def sync_legacy(conn):
    """Idempotent references, preserving first acquisition and original archive paths."""
    with conn:
        sync_assets(conn)
        for r in conn.execute(
            "SELECT * FROM snapshots WHERE id NOT IN (SELECT legacy_id FROM disclosures WHERE legacy_id IS NOT NULL)"
        ).fetchall():
            conn.execute(
                """INSERT INTO disclosures(contributor_id,source_id,source_as_of,acquired_at,processed_at,raw_path,
              content_hash,parser_version,status,error,text,positions_json,legacy_id) VALUES('dan-nathan','cnbc:dan-nathan',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["source_as_of"],
                    r["fetched_at"],
                    db.iso(),
                    r["raw_path"],
                    r["content_hash"],
                    r["parser_version"],
                    r["status"],
                    r["error"],
                    r["disclosure"],
                    r["positions_json"],
                    r["id"],
                ),
            )
        for r in conn.execute(
            "SELECT * FROM events WHERE id NOT IN (SELECT legacy_id FROM research_events WHERE legacy_id IS NOT NULL)"
        ).fetchall():
            disclosure = conn.execute(
                "SELECT id FROM disclosures WHERE legacy_id=?", (r["snapshot_id"],)
            ).fetchone()
            event = conn.execute(
                """INSERT INTO research_events(contributor_id,evidence_type,asset_id,symbol,kind,direction,eligible,
              public_at,available_at,source_url,excerpt,disclosure_id,legacy_id,details_json)
              VALUES('dan-nathan','disclosure',?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "legacy:" + r["symbol"],
                    r["symbol"],
                    r["kind"],
                    r["direction"],
                    r["eligible"],
                    r["source_as_of"],
                    r["observed_at"],
                    "https://www.cnbc.com/dan-nathan/",
                    r["raw_text"],
                    disclosure[0] if disclosure else None,
                    r["id"],
                    json.dumps(
                        {
                            "before": json.loads(r["before_json"]),
                            "after": json.loads(r["after_json"]),
                        }
                    ),
                ),
            ).lastrowid
            activity(
                conn,
                f"event:{event}",
                "cnbc:dan-nathan",
                "legacy:" + r["symbol"],
                r["kind"],
                r["source_as_of"],
                r["observed_at"],
                f"Dan Nathan: {r['symbol']} {r['kind']}",
                f"/api/snapshots/{r['snapshot_id']}/source",
                "event",
                event,
            )
        # Only matching historical strategy wording is invalidated. The initial
        # eligibility and review time remain available for prospective accounting.
        for event in conn.execute(
            "SELECT * FROM research_events WHERE contributor_id='dan-nathan' AND evidence_type='disclosure' AND eligible=1"
        ).fetchall():
            after = json.loads(event["details_json"]).get("after", [])
            stamps = []
            for pos in after:
                for table in ("reviews", "strategy_details"):
                    stamps.extend(
                        r[0]
                        for r in conn.execute(
                            f"SELECT created_at FROM {table} WHERE symbol=? AND signature=?",
                            (event["symbol"], pos["key"]),
                        )
                    )
            if stamps:
                conn.execute(
                    "UPDATE research_events SET eligible=0,invalidated_at=coalesce(invalidated_at,?) WHERE id=?",
                    (min(stamps), event["id"]),
                )


def latest(conn, contributor):
    return conn.execute(
        "SELECT * FROM disclosures WHERE contributor_id=? AND status='accepted' ORDER BY id DESC LIMIT 1",
        (contributor,),
    ).fetchone()


def positions(conn, contributor):
    if contributor == "dan-nathan":
        from .history import current_positions

        return current_positions(conn)
    snap = latest(conn, contributor)
    result = json.loads(snap["positions_json"]) if snap else []
    for pos in result:
        row = conn.execute(
            "SELECT * FROM contributor_reviews WHERE contributor_id=? AND symbol=? AND signature=? ORDER BY id DESC LIMIT 1",
            (contributor, pos["symbol"], pos["key"]),
        ).fetchone()
        if row:
            pos.update(
                direction=row["direction"],
                confidence="reviewed",
                explanation=row["reason"],
            )
    return result


def ingest_disclosure(conn, contributor, html, acquired=None):
    acquired = acquired or db.utcnow()
    if contributor == "dan-nathan":
        from .history import ingest

        result = ingest(conn, html, acquired)
        sync_legacy(conn)
        return result
    profile = conn.execute(
        "SELECT * FROM contributors WHERE id=?", (contributor,)
    ).fetchone()
    if not profile:
        raise ValueError("Unknown contributor")
    source = conn.execute(
        "SELECT * FROM sources WHERE contributor_id=?", (contributor,)
    ).fetchone()
    subject = (
        re.escape(profile["name"])
        + "|"
        + re.escape(profile["name"].split()[0])
        + ("|She" if contributor == "karen-finerman" else "|He")
    )
    path, digest = archive_bytes(html.encode())
    as_of, text, parsed, error, status = None, None, None, None, "accepted"
    try:
        ancillary = (
            re.compile(
                r"Guy Adami's wife, Linda Snow, works at Merck\.|Guy Adami is on the board of HLBZ\."
            )
            if contributor == "guy-adami"
            else None
        )
        _, text, stamp = extract(html, subject, ancillary)
        as_of = db.iso(stamp)
        if stamp > acquired + timedelta(minutes=5):
            raise ValueError("Source timestamp is in the future")
        parsed = parse_positions(
            "\n".join(
                p
                for p in text.splitlines()
                if not (ancillary and ancillary.fullmatch(p))
            ),
            subject,
        )
    except ValueError as exc:
        error, status = str(exc), "rejected"
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = latest(conn, contributor)
        if status == "accepted" and previous and as_of < previous["source_as_of"]:
            status, error = "older", "Older source; last accepted positions preserved"
        disclosure = conn.execute(
            """INSERT INTO disclosures(contributor_id,source_id,source_as_of,acquired_at,processed_at,raw_path,
          content_hash,parser_version,status,error,text,positions_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                contributor,
                source["id"],
                as_of,
                db.iso(acquired),
                db.iso(),
                path,
                digest,
                PARSER_VERSION,
                status,
                error,
                text,
                json.dumps(parsed),
            ),
        ).lastrowid
        if status != "accepted":
            return dict(id=disclosure, status=status, error=error)
        old = grouped(json.loads(previous["positions_json"])) if previous else {}
        new = grouped(parsed)
        for symbol in sorted(old.keys() | new.keys()):
            before, after = old.get(symbol, []), new.get(symbol, [])
            if before == after:
                continue
            # Same-dated changed wording is a correction, never a new live trade.
            kind = (
                "baseline"
                if not previous
                else "correction"
                if as_of == previous["source_as_of"]
                or previous["parser_version"] != PARSER_VERSION
                else "added"
                if not before
                else "removed"
                if not after
                else "direction_changed"
                if aggregate(before) != aggregate(after)
                else "strategy_changed"
            )
            from .instruments import ensure_instrument

            ensure_instrument(conn, symbol)
            sync_assets(conn)
            asset_id = "legacy:" + symbol
            asset = conn.execute(
                "SELECT * FROM assets WHERE id=?", (asset_id,)
            ).fetchone()
            direction = aggregate(after)
            reviewed = any(
                conn.execute(
                    "SELECT 1 FROM contributor_reviews WHERE contributor_id=? AND symbol=? AND signature=?",
                    (contributor, symbol, p["key"]),
                ).fetchone()
                for p in after
            )
            eligible = bool(
                asset["verified"]
                and not reviewed
                and kind in ("added", "direction_changed")
                and direction in ("bullish", "bearish")
                and all(p["analysis"]["score_eligible"] for p in after)
            )
            event = conn.execute(
                """INSERT INTO research_events(contributor_id,evidence_type,asset_id,symbol,kind,direction,eligible,
              public_at,available_at,source_url,excerpt,disclosure_id,details_json) VALUES(?,'disclosure',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    contributor,
                    asset_id,
                    symbol,
                    kind,
                    direction,
                    int(eligible),
                    as_of,
                    db.iso(acquired),
                    source["url"],
                    text,
                    disclosure,
                    json.dumps({"before": before, "after": after}),
                ),
            ).lastrowid
            activity(
                conn,
                f"event:{event}",
                source["id"],
                asset_id,
                kind,
                as_of,
                db.iso(acquired),
                f"{profile['name']}: {symbol} {kind}",
                f"/api/v2/disclosures/{disclosure}/source",
                "event",
                event,
            )
    return dict(id=disclosure, status=status, error=error)


def review_position(conn, contributor, symbol, direction, reason):
    from .strategies import DIRECTIONS

    if direction not in DIRECTIONS or not reason.strip():
        raise ValueError("Direction and reason are required")
    if contributor == "dan-nathan":
        from .history import review

        review(conn, symbol, direction, reason)
        sync_legacy(conn)
        with conn:
            audit(
                conn,
                "review",
                "contributor",
                contributor,
                {"symbol": symbol, "reason": reason},
            )
        return
    found = [p for p in positions(conn, contributor) if p["symbol"] == symbol]
    if len(found) != 1:
        raise ValueError("Review requires exactly one current strategy")
    with conn:
        conn.execute(
            "INSERT INTO contributor_reviews(contributor_id,symbol,signature,direction,reason,created_at) VALUES(?,?,?,?,?,?)",
            (contributor, symbol, found[0]["key"], direction, reason, db.iso()),
        )
        for event in conn.execute(
            "SELECT * FROM research_events WHERE contributor_id=? AND symbol=? AND evidence_type='disclosure' AND eligible=1",
            (contributor, symbol),
        ).fetchall():
            if any(
                p["key"] == found[0]["key"]
                for p in json.loads(event["details_json"]).get("after", [])
            ):
                conn.execute(
                    "UPDATE research_events SET eligible=0,invalidated_at=coalesce(invalidated_at,?) WHERE id=?",
                    (db.iso(), event["id"]),
                )
        snap = latest(conn, contributor)
        source = conn.execute(
            "SELECT * FROM sources WHERE contributor_id=?", (contributor,)
        ).fetchone()
        event_id = conn.execute(
            """INSERT INTO research_events(contributor_id,evidence_type,asset_id,symbol,kind,direction,eligible,public_at,available_at,source_url,excerpt,disclosure_id,details_json)
          VALUES(?,'disclosure',?,?,'correction',?,0,?,?,?,?,?,?)""",
            (
                contributor,
                "legacy:" + symbol,
                symbol,
                direction,
                snap["source_as_of"],
                db.iso(),
                source["url"],
                reason,
                snap["id"],
                json.dumps(
                    {
                        "before": found,
                        "after": positions(conn, contributor),
                        "reason": reason,
                    }
                ),
            ),
        ).lastrowid
        activity(
            conn,
            f"event:{event_id}",
            source["id"],
            "legacy:" + symbol,
            "correction",
            snap["source_as_of"],
            db.iso(),
            f"{contributor}: {symbol} interpretation corrected",
            f"/api/v2/disclosures/{snap['id']}/source",
            "event",
            event_id,
        )
        audit(
            conn,
            "review",
            "contributor",
            contributor,
            {"symbol": symbol, "direction": direction, "reason": reason},
        )


def canonical_id(conn, asset_id):
    row = conn.execute(
        "SELECT canonical_id FROM asset_links WHERE alias_id=?", (asset_id,)
    ).fetchone()
    return row[0] if row else asset_id


def related_ids(conn, asset_id):
    canonical = canonical_id(conn, asset_id)
    return [canonical] + [
        r[0]
        for r in conn.execute(
            "SELECT alias_id FROM asset_links WHERE canonical_id=?", (canonical,)
        )
    ]
