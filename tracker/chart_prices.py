"""Chart-only OHLC cache. Saved analytics inputs remain immutable and separate."""

import json
from . import db
from .research import canonical_id


def prices(conn, asset_id):
    asset_id = canonical_id(conn, asset_id)
    asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    result = dict(
        symbol=asset["symbol"] if asset else asset_id,
        name=asset["name"] if asset else asset_id,
        bars=[],
        error=None,
        fetched_at=None,
        adjustment="Split and dividend adjusted",
        completed_sessions_only=True,
        currency=asset["currency"] if asset else None,
    )
    if asset_id.startswith("market:"):
        market_id = asset_id.removeprefix("market:")
        market = conn.execute(
            "SELECT * FROM dbmf_markets WHERE id=?", (market_id,)
        ).fetchone()
        if market:
            from .dbmf.api import prices as legacy_prices

            old = legacy_prices(market_id, None, None)
            return dict(
                result,
                **{
                    k: v
                    for k, v in old.items()
                    if k not in result or k in ("bars", "adjustment")
                },
                error=market["price_error"],
                fetched_at=market["price_checked_at"]
            )
    if asset and (asset["verified"] or reference(conn, asset_id)):
        batch = conn.execute(
            "SELECT * FROM price_batches WHERE provider=? AND kind='chart' ORDER BY created_at DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        if batch:
            document = json.loads(batch["inputs_json"])
            result.update(
                bars=document["bars"],
                omitted_bars=document.get("omitted_bars", []),
                quality_note="Inconsistent provider bars are omitted. Original OHLC values are preserved before currency inversion.",
                adjustment=document["adjustment"],
                fetched_at=db.setting(conn, "chart_checked:" + asset_id)
                or batch["created_at"],
            )
        elif asset_id.startswith("legacy:"):
            result["bars"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT date AS time,open,high,low,close,volume FROM prices WHERE symbol=? AND complete=1 ORDER BY date",
                    (asset["symbol"],),
                )
            ]
            old = conn.execute(
                "SELECT price_checked_at,price_error FROM instruments WHERE symbol=?",
                (asset["symbol"],),
            ).fetchone()
            if old:
                result.update(fetched_at=old[0], error=old[1])
        result["error"] = db.setting(conn, "chart_error:" + asset_id) or result["error"]
    else:
        result["error"] = "Price mapping has not been verified"
    return result


# Explicit price references, not substitutes for the contracts held. Overseas
# bond futures without a verified free reference remain visibly unavailable.
REFERENCES = {
    "aud": ("AUDUSD=X", "Australian dollar / US dollar", "currency", False),
    "gbp": ("GBPUSD=X", "British pound / US dollar", "currency", False),
    "cad": ("CAD=X", "US dollar / Canadian dollar, inverted", "currency", True),
    "chf": ("CHF=X", "US dollar / Swiss franc, inverted", "currency", True),
    "corn": ("ZC=F", "Corn futures", "futures", False),
    "copper": ("HG=F", "Copper futures", "futures", False),
    "heating-oil": ("HO=F", "Heating oil futures", "futures", False),
    "live-cattle": ("LE=F", "Live cattle futures", "futures", False),
    "natural-gas": ("NG=F", "Natural gas futures", "futures", False),
    "soybeans": ("ZS=F", "Soybean futures", "futures", False),
    "sugar": ("SB=F", "Sugar futures", "futures", False),
    "wheat": ("ZW=F", "Wheat futures", "futures", False),
    "gasoline": ("RB=F", "RBOB gasoline futures", "futures", False),
    "soybean-oil": ("ZL=F", "Soybean oil futures", "futures", False),
    "brent": ("BZ=F", "Brent crude oil futures", "futures", False),
    "kc-wheat": ("KE=F", "Kansas wheat futures", "futures", False),
}


def reference(conn, asset_id):
    return db.setting(conn, "chart_reference:" + asset_id)


def company_key(name):
    import re

    name = re.sub(r"\b(?:CLASS|CL)\s*[A-Z]\b|\s+-\s*[A-Z]$", "", name.upper())
    name = re.sub(
        r"\b(?:INCORPORATED|INC|CORPORATION|CORP|LIMITED|LTD|PLC|HOLDINGS?|ORDINARY|SHARES?)\b",
        "",
        name,
    )
    return re.sub(r"[^A-Z0-9]", "", name)


def qualify(conn, asset):
    """Match an issuer's display symbol AND company name against provider metadata."""
    from .instruments import chart_metadata

    if asset["provider_symbol"] and asset["verified"]:
        return dict(
            symbol=asset["provider_symbol"],
            name=asset["name"],
            kind="Stock",
            invert=False,
        )
    if asset["id"].startswith("market:"):
        spec = REFERENCES.get(asset["id"].removeprefix("market:"))
        if not spec:
            raise ValueError("No verified free price reference for this market")
        symbol, name, kind, invert = spec
        meta = chart_metadata(symbol)
        if meta.get("symbol") != symbol or meta.get("instrumentType") not in (
            "FUTURE",
            "CURRENCY",
        ):
            raise ValueError("Price reference identity did not match provider metadata")
        result = dict(
            symbol=symbol,
            name=name,
            kind=kind,
            invert=invert,
            provenance="Explicit market reference, Yahoo chart metadata: "
            + str(meta.get("shortName", meta.get("longName", ""))),
        )
    else:
        import re

        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", asset["symbol"]):
            raise ValueError("No verified US listing for this holding")
        meta = chart_metadata(asset["symbol"])
        official = company_key(asset["name"])
        provider = company_key(meta.get("longName") or meta.get("shortName") or "")
        matches = official == provider or (
            len(official) >= 24 and provider.startswith(official)
        )
        if (
            not matches
            or meta.get("symbol") != asset["symbol"]
            or meta.get("currency") != "USD"
            or meta.get("instrumentType") not in ("EQUITY", "ETF")
            or meta.get("exchangeName")
            not in ("NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS")
        ):
            raise ValueError(
                "Issuer security and provider listing need a verified mapping"
            )
        result = dict(
            symbol=asset["symbol"],
            name=asset["name"],
            kind="Stock",
            invert=False,
            provenance="Issuer symbol and company name matched Yahoo chart metadata",
        )
        with conn:
            conn.execute(
                "UPDATE assets SET provider_symbol=?,currency='USD',exchange=?,verified=1,provenance=provenance||? WHERE id=?",
                (
                    asset["symbol"],
                    meta["exchangeName"],
                    "; " + result["provenance"],
                    asset["id"],
                ),
            )
    with conn:
        db.set_setting(conn, "chart_reference:" + asset["id"], result)
    return result


def refresh(conn, asset_id):
    import math
    from datetime import timedelta
    import yfinance as yf
    from .analysis_worker import price_batch
    from .calendar import completed
    from .dbmf.prices import completed as market_completed, transform

    asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not asset:
        raise ValueError("Unknown asset")
    ref = reference(conn, asset_id) or qualify(conn, asset)
    now = db.utcnow()
    is_market = asset_id.startswith("market:")
    start = (now.date() - timedelta(days=365 * 5)).isoformat()
    earliest = conn.execute(
        "SELECT min(available_at) FROM research_events WHERE asset_id=?", (asset_id,)
    ).fetchone()[0]
    if earliest:
        start = min(start, earliest[:10])
    frame = yf.Ticker(ref["symbol"]).history(
        start=start,
        interval="1d",
        auto_adjust=not is_market,
        actions=False,
        raise_errors=True,
    )
    if frame.empty:
        raise ValueError("Price provider returned no history")
    bars = []
    omitted = []
    seen = set()
    provider_dates = set()
    for index, row in frame.iterrows():
        day = index.date().isoformat()
        if not (
            market_completed(day, ref["kind"], now)
            if is_market
            else completed(day, now)
        ):
            continue
        if day in provider_dates:
            raise ValueError("Duplicate chart session")
        provider_dates.add(day)
        values = {k: float(row[k.title()]) for k in ("open", "high", "low", "close")}
        if (
            any(not math.isfinite(v) or v <= 0 for v in values.values())
            or values["high"] < max(values["open"], values["close"], values["low"])
            or values["low"] > min(values["open"], values["close"], values["high"])
        ):
            from .dbmf.prices import invalid_bar_evidence

            evidence = invalid_bar_evidence(
                day,
                list(values.values()),
                "Invalid daily chart bar",
                {"provider_symbol": ref["symbol"]},
                now,
            )
            omitted.append(evidence)
            continue
        if day in seen:
            raise ValueError("Duplicate chart session")
        seen.add(day)
        if ref["invert"]:
            values = dict(
                open=1 / values["open"],
                high=1 / values["low"],
                low=1 / values["high"],
                close=1 / values["close"],
            )
        volume = float(row.get("Volume", 0))
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("Invalid daily chart volume")
        bars.append(dict(time=day, **values, volume=volume))
    bars.sort(key=lambda b: b["time"])
    if not bars:
        raise ValueError("No completed chart sessions")
    previous = conn.execute(
        "SELECT inputs_json FROM price_batches WHERE provider=? AND kind='chart' ORDER BY created_at DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    if previous and not {
        b["time"] for b in json.loads(previous[0])["bars"] if b["time"] >= start
    }.issubset(seen):
        raise ValueError(
            "Price provider omitted cached sessions; previous chart retained"
        )
    if previous:
        older = [b for b in json.loads(previous[0])["bars"] if b["time"] < start]
        bars = older + bars
    document = dict(
        bars=bars,
        adjustment=(
            "Unadjusted market reference; futures rolls may affect prices"
            if is_market
            else "Split and dividend adjusted"
        ),
        reference=ref,
        omitted_bars=omitted,
    )
    price_batch(conn, asset_id, "chart", document, now)
    with conn:
        db.set_setting(conn, "chart_error:" + asset_id, None)
        db.set_setting(conn, "chart_checked:" + asset_id, db.iso(now))


def maintain(conn):
    """One bounded request per tick, independent from simulation price accounting."""
    from datetime import datetime, timedelta

    candidates = conn.execute(
        """SELECT a.* FROM assets a WHERE a.id IN (SELECT asset_id FROM research_events UNION SELECT h.asset_id FROM fund_holdings h JOIN fund_reports r ON r.id=h.report_id WHERE r.status='accepted' AND h.collateral=0) ORDER BY a.id"""
    ).fetchall()

    def last(a):
        return db.setting(conn, "chart_attempt:" + a["id"]) or ""

    for asset in sorted(candidates, key=last):
        if (
            asset["id"].startswith("market:")
            and conn.execute(
                "SELECT 1 FROM dbmf_markets WHERE id=?",
                (asset["id"].removeprefix("market:"),),
            ).fetchone()
        ):
            continue
        attempt = last(asset)
        if attempt and db.utcnow() - datetime.fromisoformat(attempt) < timedelta(
            hours=12
        ):
            continue
        with conn:
            db.set_setting(conn, "chart_attempt:" + asset["id"], db.iso())
        try:
            refresh(conn, asset["id"])
        except Exception as exc:
            with conn:
                db.set_setting(conn, "chart_error:" + asset["id"], str(exc)[:500])
        return
