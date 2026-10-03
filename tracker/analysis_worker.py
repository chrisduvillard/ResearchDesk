"""Single analytics worker: queued models, price vintages, briefing and health."""

import fcntl
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
import yfinance as yf
from . import db
from .calendar import completed
from .analytics import enqueue, freeze_inputs, process_job, valid_bar
from .briefing import generate
from .research import sync_legacy, sync_assets, activity
from .funds import sync_dbmf

log = logging.getLogger(__name__)


def accounting_bars(rows):
    result = {}
    factor = 1.0
    for row in sorted(rows, key=lambda r: r["date"], reverse=True):
        if row["date"] in result or not valid_bar(row):
            raise ValueError("Invalid or duplicate accounting bar")
        result[row["date"]] = {
            k: row[k] * factor for k in ("open", "close", "dividend")
        }
        result[row["date"]]["split"] = row["split"]
        factor *= row["split"] or 1
    return result


def price_batch(conn, asset_id, kind, bars, now):
    encoded = json.dumps(bars, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    identifier = hashlib.sha256((asset_id + kind + digest).encode()).hexdigest()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO price_batches(id,created_at,provider,kind,inputs_json,content_hash) VALUES(?,?,?,?,?,?)",
            (identifier, db.iso(now), asset_id, kind, encoded, digest),
        )
    return identifier


def refresh_asset(conn, asset_id):
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if (
        not row
        or not row["verified"]
        or row["currency"] != "USD"
        or not row["provider_symbol"]
    ):
        return
    now = db.utcnow()
    event = conn.execute(
        "SELECT min(available_at) FROM research_events WHERE asset_id=?", (asset_id,)
    ).fetchone()[0]
    start = min(
        (event or db.iso(now))[:10], (now.date() - timedelta(days=365 * 5)).isoformat()
    )
    raw = yf.Ticker(row["provider_symbol"]).history(
        start=start, auto_adjust=False, actions=True, raise_errors=True
    )
    if raw.empty:
        raise ValueError("Price provider returned no history")
    rows = []
    adjusted = {}
    for index, bar in raw.iterrows():
        day = index.date().isoformat()
        if not completed(day, now):
            continue
        item = dict(
            date=day,
            open=float(bar["Open"]),
            close=float(bar["Close"]),
            dividend=float(bar.get("Dividends", 0)),
            split=float(bar.get("Stock Splits", 0)),
        )
        if not valid_bar(item):
            raise ValueError("Invalid price input")
        ratio = float(bar["Adj Close"]) / item["close"]
        if not 0 < ratio < 100:
            raise ValueError("Invalid adjustment ratio")
        adjusted[day] = {"open": item["open"] * ratio, "close": float(bar["Adj Close"])}
        rows.append(item)
    if not rows:
        raise ValueError("No completed price sessions")
    accounting = accounting_bars(rows)
    for kind, bars in [("accounting", accounting), ("adjusted", adjusted)]:
        previous = conn.execute(
            "SELECT inputs_json FROM price_batches WHERE provider=? AND kind=? ORDER BY created_at DESC LIMIT 1",
            (asset_id, kind),
        ).fetchone()
        if previous and not set(json.loads(previous[0])).issubset(bars):
            raise ValueError("Provider omitted previously cached sessions")
        price_batch(conn, asset_id, kind, bars, now)
    with conn:
        db.set_setting(conn, "price_checked:" + asset_id, db.iso(now))


def maintain_prices(conn):
    # At most one slow provider operation per tick: queued jobs keep moving.
    from .instruments import ensure_instrument, resolve_instrument

    with conn:
        ensure_instrument(conn, "SPY")
    candidate = conn.execute(
        "SELECT symbol FROM instruments WHERE verified=0 AND (symbol='SPY' OR symbol IN (SELECT symbol FROM research_events)) AND (price_checked_at IS NULL OR price_checked_at<?) ORDER BY symbol LIMIT 1",
        (db.iso(db.utcnow() - timedelta(days=1)),),
    ).fetchone()
    if candidate:
        try:
            resolve_instrument(conn, candidate[0])
            with conn:
                conn.execute(
                    "UPDATE instruments SET price_checked_at=? WHERE symbol=?",
                    (db.iso(), candidate[0]),
                )
                sync_assets(conn)
        except Exception as exc:
            with conn:
                conn.execute(
                    "UPDATE instruments SET price_checked_at=?,price_error=? WHERE symbol=?",
                    (db.iso(), str(exc)[:500], candidate[0]),
                )
        return
    with conn:
        sync_assets(conn)
        # Default applies only to verified US equity listings, not bonds/futures.
        if conn.execute(
            "SELECT 1 FROM assets WHERE id='legacy:SPY' AND verified=1"
        ).fetchone():
            for a in conn.execute(
                "SELECT * FROM assets WHERE verified=1 AND kind IN ('Companies','Equity funds') AND currency='USD' AND benchmark_id IS NULL"
            ).fetchall():
                key = "default_benchmark:" + a["id"]
                if not db.setting(conn, key):
                    conn.execute(
                        "UPDATE assets SET benchmark_id='legacy:SPY' WHERE id=?",
                        (a["id"],),
                    )
                    db.set_setting(conn, key, True)
    for row in conn.execute(
        "SELECT * FROM assets WHERE id IN (SELECT asset_id FROM research_events) OR id='legacy:SPY'"
    ).fetchall():
        if not row["verified"] or not row["provider_symbol"]:
            continue
        last = db.setting(conn, "price_attempt:" + row["id"])
        if last and db.utcnow() - datetime.fromisoformat(last) < timedelta(hours=12):
            continue
        with conn:
            db.set_setting(conn, "price_attempt:" + row["id"], db.iso())
        try:
            refresh_asset(conn, row["id"])
            with conn:
                db.set_setting(conn, "price_error:" + row["id"], None)
        except Exception as exc:
            with conn:
                db.set_setting(conn, "price_error:" + row["id"], str(exc)[:500])
        return


def tick(conn, prices=False):
    sync_legacy(conn)
    sync_dbmf(conn)
    with conn:
        db.set_setting(conn, "analytics_worker_heartbeat", db.iso())
    for row in conn.execute(
        "SELECT id FROM analysis_runs WHERE status='queued' ORDER BY created_at LIMIT 4"
    ).fetchall():
        process_job(conn, row["id"])
    today = db.utcnow().date().isoformat()
    if db.setting(conn, "analytics_day") != today:
        for contributor, evidence in conn.execute(
            "SELECT DISTINCT contributor_id,evidence_type FROM research_events"
        ).fetchall():
            inputs = freeze_inputs(conn, contributor, evidence, {})
            job = enqueue(conn, "scorecard", inputs)
            process_job(conn, job)
        for definition in conn.execute(
            "SELECT * FROM simulation_definitions WHERE mode='prospective'"
        ).fetchall():
            inputs = freeze_inputs(
                conn,
                definition["contributor_id"],
                definition["evidence_type"],
                json.loads(definition["config_json"]),
                kind="simulation",
                forward_start=definition["forward_start"],
            )
            inputs["definition_id"] = definition["id"]
            enqueue(conn, "simulation", inputs)
        with conn:
            db.set_setting(conn, "analytics_day", today)
    # Only completed outcomes create an activity; the key survives reruns.
    for run in conn.execute(
        "SELECT * FROM analysis_runs WHERE kind='scorecard' AND status='complete' AND NOT EXISTS (SELECT 1 FROM settings WHERE key='outcomes:'||analysis_runs.id)"
    ).fetchall():
        result = json.loads(run["result_json"])
        with conn:
            for signal in result["signals"]:
                for horizon, outcome in signal["results"].items():
                    if outcome["status"] != "complete":
                        continue
                    event = conn.execute(
                        "SELECT * FROM research_events WHERE id=?",
                        (signal["event_id"],),
                    ).fetchone()
                    if event:
                        activity(
                            conn,
                            f"outcome:{event['id']}:{horizon}",
                            "cnbc:" + event["contributor_id"],
                            event["asset_id"],
                            "score_outcome",
                            outcome["end_date"],
                            db.iso(),
                            f"{event['symbol']}: {horizon}-session outcome",
                            "/research?view=analytics&run=" + run["id"],
                            "analysis",
                            run["id"],
                        )
            db.set_setting(conn, "outcomes:" + run["id"], run["id"])
    for source in conn.execute("SELECT * FROM sources WHERE enabled=1").fetchall():
        last = conn.execute(
            "SELECT finished_at FROM source_runs WHERE source_id=? AND status='success' ORDER BY id DESC LIMIT 1",
            (source["id"],),
        ).fetchone()
        overdue = not last or db.utcnow() - datetime.fromisoformat(last[0]) > timedelta(
            hours=26 if source["fund_id"] else 36
        )
        key = "overdue:" + source["id"]
        previous = db.setting(conn, key)
        with conn:
            if previous is not None and previous != overdue:
                activity(
                    conn,
                    key + ":" + db.iso(),
                    source["id"],
                    None,
                    "source_overdue" if overdue else "source_recovery",
                    None,
                    db.iso(),
                    source["id"] + (": overdue" if overdue else ": current"),
                    source["url"],
                    "source",
                    source["id"],
                )
            db.set_setting(conn, key, overdue)
    from .briefing import disagreements

    with conn:
        for item in disagreements(conn):
            fingerprint = hashlib.sha256(
                json.dumps(item, sort_keys=True).encode()
            ).hexdigest()
            activity(
                conn,
                "disagreement:" + fingerprint,
                None,
                item["asset_id"],
                "disagreement",
                None,
                db.iso(),
                f"{item['asset_id']}: {item['bullish']} bullish / {item['bearish']} bearish ({item['evidence_type']}, {item['denominator']} contributors)",
                "/research?view=asset&id=" + item["asset_id"],
                "asset",
                item["asset_id"],
                item,
            )
    generate(conn)
    if prices:
        maintain_prices(conn)
        from .chart_prices import maintain
        maintain(conn)


def worker():
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (db.DATA_DIR / "analytics.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with db.database() as conn:
            db.initialize(conn)
            with conn:
                conn.execute(
                    "UPDATE analysis_runs SET status='queued' WHERE status='running'"
                )
        while True:
            try:
                with db.database() as conn:
                    tick(conn, prices=True)
            except Exception:
                log.exception("Analytics tick failed")
            time.sleep(30)
