import logging
import math
from datetime import timedelta
import yfinance as yf
from . import db
from .calendar import completed

log = logging.getLogger(__name__)


def refresh(conn, now=None):
    now = now or db.utcnow()
    errors = {}
    for instrument in conn.execute("SELECT * FROM instruments WHERE symbol IN (SELECT DISTINCT symbol FROM events) ORDER BY symbol").fetchall():
        symbol = instrument["symbol"]
        if not instrument["verified"]:
            errors[symbol] = "Instrument listing and company/fund name need verification"
            continue
        try:
            first = conn.execute("SELECT min(chart_date) FROM events WHERE symbol=?", (symbol,)).fetchone()[0]
            # Refresh the complete tracked interval to incorporate later splits and
            # distributions consistently into all adjusted OHLC values.
            cached_start = conn.execute("SELECT min(date) FROM prices WHERE symbol=?", (symbol,)).fetchone()[0]
            start = min(first, cached_start or first, (now.date() - timedelta(days=730)).isoformat())
            frame = yf.Ticker(instrument["provider_symbol"]).history(start=start, interval="1d", auto_adjust=True, actions=True, raise_errors=True)
            if frame.empty:
                raise ValueError("No daily prices returned")
            rows, dates = [], set()
            for index, bar in frame.iterrows():
                day = index.date().isoformat()
                values = [float(bar[k]) for k in ("Open", "High", "Low", "Close")]
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise ValueError(f"Invalid daily price bar on {day}; previous prices retained")
                if values[1] < max(values[0], values[3], values[2]) or values[2] > min(values[0], values[3]):
                    raise ValueError(f"Inconsistent daily price bar on {day}")
                if day in dates:
                    raise ValueError(f"Duplicate provider date: {day}")
                dates.add(day)
                metadata = []
                for field in ("Volume", "Dividends", "Stock Splits"):
                    value = float(bar.get(field, 0))
                    if not math.isfinite(value) or value < 0:
                        raise ValueError(f"Invalid {field} on {day}; previous prices retained")
                    metadata.append(value)
                rows.append((symbol, day, *values, *metadata, int(completed(day, now)), db.iso(now), "Yahoo Finance / yfinance; split and dividend adjusted"))
            if not rows:
                raise ValueError("No valid daily bars returned")
            cached = {row[0] for row in conn.execute("SELECT date FROM prices WHERE symbol=? AND complete=1 AND date>=?", (symbol, start))}
            complete_dates = {row[1] for row in rows if row[9]}
            if not cached.issubset(complete_dates):
                raise ValueError("Provider history is missing cached dates; previous prices retained")
            # Replace this provider interval atomically. Never erase a cached series
            # on a failed request or mix adjustment vintages inside this interval.
            with conn:
                conn.execute("DELETE FROM prices WHERE symbol=? AND date>=?", (symbol, start))
                conn.executemany("INSERT INTO prices(symbol,date,open,high,low,close,volume,dividend,split,complete,fetched_at,provider) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                conn.execute("UPDATE instruments SET price_checked_at=?,price_error=NULL WHERE symbol=?", (db.iso(now), symbol))
        except Exception as exc:
            log.warning("Price refresh failed for %s: %s", symbol, exc)
            errors[symbol] = str(exc)[:500]
            with conn:
                conn.execute("UPDATE instruments SET price_error=? WHERE symbol=?", (errors[symbol], symbol))
    return errors
