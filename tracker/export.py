import csv
import io
import json
from . import db
from .history import latest_snapshot

LIMIT = 40960
MAX_EVENTS = 400


def clean(value):
    return " ".join(str(value or "").replace("|", "/").split())


def pine_export(conn, symbol, generated=None):
    item = conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()
    if not item:
        raise ValueError("Unknown instrument")
    if not item["tv_symbol"]:
        raise ValueError("This instrument needs a verified TradingView symbol before export")
    events = conn.execute("SELECT * FROM events WHERE symbol=? ORDER BY observed_at,id", (symbol,)).fetchall()
    if not events:
        raise ValueError("This instrument has no recorded disclosures")
    generated = generated or db.iso()
    snap = latest_snapshot(conn)
    lines = []
    for event in events:
        positions = json.loads(event["after_json"]) or json.loads(event["before_json"])
        strategy = "; ".join(f"{p['side']} {p['strategy']}" + (": " + p["analysis"]["explanation"] if p.get("analysis") else "") for p in positions)
        lines.append("|".join(clean(v) for v in (event["chart_date"].replace("-", ""), event["kind"], event["direction"], event["source_as_of"], event["observed_at"], strategy[:500])))
    selected = lines[-MAX_EVENTS:]
    def pack():
        header = "|".join(clean(v) for v in ("DN2", item["tv_symbol"], item["name"], generated, snap["source_as_of"], len(lines), len(selected), selected[0].split("|")[0]))
        return header + "\n" + "\n".join(selected)
    while len(pack()) > LIMIT and len(selected) > 1:
        selected.pop(0)
    payload = pack()
    if len(payload) > LIMIT:
        raise ValueError("A single event exceeds TradingView's text limit")
    return payload


def csv_export(conn):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["event_id", "company_or_fund", "symbol", "event", "direction", "first_observed_utc", "source_as_of_utc", "chart_date", "confidence", "score_eligible", "original_wording"])
    for row in conn.execute("SELECT e.*,i.name FROM events e JOIN instruments i USING(symbol) ORDER BY observed_at,id"):
        values = [row[k] for k in ("id", "name", "symbol", "kind", "direction", "observed_at", "source_as_of", "chart_date", "confidence", "eligible", "raw_text")]
        # Prevent spreadsheet formula execution in exported source text.
        values = ["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in values]
        writer.writerow(values)
    return stream.getvalue()
