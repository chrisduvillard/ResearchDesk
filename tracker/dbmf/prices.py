import logging
import math
from datetime import datetime, time, timedelta, timezone

import yfinance as yf

from .. import db
from ..calendar import NY, completed as equity_completed
from . import INCEPTION

log = logging.getLogger(__name__)
PROVIDER = 'Yahoo Finance / yfinance'
VALIDATOR_VERSION = 'dbmf-ohlc-2'
OHLC = ('Open', 'High', 'Low', 'Close')


def invalid_bar_evidence(day, values, error, market, now):
    """Keep provider values before inversion, including JSON-safe NaN/Inf text."""
    issues = []
    try:
        o, h, low, c = map(float, values)
        if not all(math.isfinite(v) for v in (o, h, low, c)):
            issues.append('Missing or nonfinite price')
        else:
            if h < low:
                issues.append('High is below low')
            if not low <= o <= h:
                issues.append('Open is outside the low–high range')
            if not low <= c <= h:
                issues.append('Close is outside the low–high range')
    except (TypeError, ValueError, OverflowError):
        issues.append('Nonnumeric price')
    return dict(date=day, reason=str(error), issues=issues or [str(error)],
                raw_ohlc={name.lower(): str(value) for name, value in zip(OHLC, values)},
                symbol=market['provider_symbol'], provider=PROVIDER,
                checked_at=db.iso(now), validator_version=VALIDATOR_VERSION)


def completed(day, kind, now):
    day = datetime.strptime(day, '%Y-%m-%d').date()
    if kind == 'ETF proxy':
        # Exchange holidays and early closes are handled by XNYS.
        return equity_completed(day.isoformat(), now)
    if day.weekday() >= 5:
        return False
    if kind == 'futures':
        # The listed CME contracts finish their trade date by 17:00 ET.
        # Wait an additional hour before publishing a daily bar.
        return now >= datetime.combine(day, time(18), NY)
    # FX provider bars use a calendar day. Wait until 01:00 UTC on the next day;
    # this also clears midnight London year-round and the New York FX close.
    return now >= datetime.combine(day + timedelta(days=1), time(1), timezone.utc)


def transform(values, invert=False):
    o, h, low, c = map(float, values)
    if not all(math.isfinite(v) for v in (o, h, low, c)) or h < max(o, c, low) or low > min(o, c):
        raise ValueError('Invalid OHLC bar')
    if invert:
        if min(o, h, low, c) <= 0:
            raise ValueError('Cannot invert a nonpositive currency price')
        return 1 / o, 1 / low, 1 / h, 1 / c
    return o, h, low, c


def refresh(conn, now=None):
    now = now or db.utcnow()
    errors = {}
    for market in conn.execute('SELECT * FROM dbmf_markets WHERE provider_symbol IS NOT NULL ORDER BY sort_order').fetchall():
        key = market['id']
        try:
            frame = yf.Ticker(market['provider_symbol']).history(start=INCEPTION, interval='1d', auto_adjust=False,
                                                              actions=False, raise_errors=True, timeout=25)
            if frame.empty:
                raise ValueError('No daily prices returned')
            rows, dates, seen, gaps = [], set(), set(), []
            for index, bar in frame.iterrows():
                day = index.date().isoformat()
                if not completed(day, market['price_kind'], now):
                    continue
                # Check before validation: a valid and invalid row for the same
                # date must not become both a candle and a chart gap.
                if day in seen:
                    raise ValueError('Duplicate provider date')
                seen.add(day)
                raw_values = [bar[k] for k in OHLC]
                try:
                    values = transform(raw_values, bool(market['invert']))
                    if market['price_kind'] != 'futures' and min(values) <= 0:
                        raise ValueError('Nonpositive price')
                except (ValueError, TypeError, OverflowError) as exc:
                    gaps.append(invalid_bar_evidence(day, raw_values, exc, market, now))
                    continue
                dates.add(day)
                volume = float(bar.get('Volume', 0))
                rows.append((key, day, *values, volume if math.isfinite(volume) else None, db.iso(now), PROVIDER))
            if not rows:
                raise ValueError('No completed daily prices returned')
            cached = {row[0] for row in conn.execute('SELECT date FROM dbmf_prices WHERE market_id=?', (key,))}
            if not cached.issubset(dates):
                raise ValueError('Provider history is missing cached dates; previous prices retained')
            with conn:
                conn.execute('DELETE FROM dbmf_prices WHERE market_id=?', (key,))
                conn.executemany('INSERT INTO dbmf_prices(market_id,date,open,high,low,close,volume,fetched_at,provider) VALUES(?,?,?,?,?,?,?,?,?)', rows)
                conn.execute('UPDATE dbmf_markets SET price_checked_at=?,price_error=NULL WHERE id=?', (db.iso(now), key))
                db.set_setting(conn, 'dbmf_price_gaps_' + key, gaps)
        except Exception as exc:
            errors[key] = str(exc)[:700]
            log.warning('DBMF price reference %s: %s', key, exc)
            with conn:
                conn.execute('UPDATE dbmf_markets SET price_error=? WHERE id=?', (errors[key], key))
    return errors
