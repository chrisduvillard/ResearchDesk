import json
import statistics
from datetime import datetime
from . import db
from .calendar import entry_session, horizon_date, completed

HORIZONS = (1, 5, 20, 60)


def metrics(values):
    if not values:
        return dict(n=0, win_rate=None, mean=None, median=None)
    return dict(n=len(values), win_rate=sum(v > 0 for v in values) / len(values), mean=statistics.mean(values), median=statistics.median(values))


def calculate(conn, symbol=None, now=None):
    now = now or db.utcnow()
    sql = "SELECT e.*,i.name FROM events e JOIN instruments i USING(symbol) WHERE e.eligible=1"
    args = []
    if symbol:
        sql += " AND e.symbol=?"
        args.append(symbol)
    rows = conn.execute(sql + " ORDER BY e.observed_at,e.id", args).fetchall()
    signals = []
    for event in rows:
        after = json.loads(event["after_json"])
        reviewed = any(conn.execute("SELECT 1 FROM reviews WHERE symbol=? AND signature=?", (event["symbol"], p["key"])).fetchone() for p in after) or any(conn.execute("SELECT 1 FROM strategy_details WHERE symbol=? AND signature=?", (event["symbol"], p["key"])).fetchone() for p in after)
        entry = entry_session(datetime.fromisoformat(event["observed_at"]))
        opening = conn.execute("SELECT open FROM prices WHERE symbol=? AND date=? AND complete=1", (event["symbol"], entry)).fetchone()
        results = {}
        for horizon in HORIZONS:
            end = horizon_date(entry, horizon)
            endrow = conn.execute("SELECT close FROM prices WHERE symbol=? AND date=? AND complete=1", (event["symbol"], end)).fetchone()
            state = "reviewed" if reviewed else "pending" if not completed(end, now) else "missing_prices" if not opening or not endrow else "complete"
            underlying = endrow[0] / opening[0] - 1 if state == "complete" else None
            direction = 1 if event["direction"] == "bullish" else -1
            results[str(horizon)] = dict(status=state, end_date=end, follow=underlying * direction if underlying is not None else None, oppose=-underlying * direction if underlying is not None else None, always_long=underlying)
        signals.append(dict(event_id=event["id"], symbol=event["symbol"], name=event["name"], direction=event["direction"], observed_at=event["observed_at"], entry_date=entry, results=results))
    summary = []
    for horizon in HORIZONS:
        scores = [s["results"][str(horizon)] for s in signals]
        summary.append(dict(horizon=horizon, pending=sum(r["status"] == "pending" for r in scores), missing=sum(r["status"] == "missing_prices" for r in scores), reviewed=sum(r["status"] == "reviewed" for r in scores), **{mode: metrics([r[mode] for r in scores if r["status"] == "complete"]) for mode in ("follow", "oppose", "always_long")}))
    return dict(summary=summary, signals=signals, methodology="Gross, equal-weight directional comparisons using split- and dividend-adjusted daily prices. Entry is the first regular-session open after observation; entry day counts as day one. Baselines, ambiguous or conditional exposures, complex strategies and retrospective corrections are excluded. The scorecard covers shares, single options, conventional verticals and synthetic stock. Rolls with unchanged direction create no extra signal. Oppose is the negative directional return, before fees, borrow costs or financing. These are not options returns or a portfolio backtest. Observations can overlap.")
