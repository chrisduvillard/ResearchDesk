import json
import re
from urllib.request import Request, urlopen

SEEDS = [
    dict(symbol="DRAM", name="Roundhill Memory ETF", asset_class="Equity funds", description="Global memory-chip companies", provider_symbol="DRAM", tv_symbol="CBOE:DRAM", verified=1, currency="USD", exchange="CBOE", name_source="https://www.roundhillinvestments.com/etf/dram/"),
    dict(symbol="TBLA", name="Taboola", asset_class="Companies", description="Advertising technology", provider_symbol="TBLA", tv_symbol="NASDAQ:TBLA", verified=1, currency="USD", exchange="NASDAQ", name_source="https://investors.taboola.com/investor-resources/investor-faqs"),
    dict(symbol="TMCR", name="The Metals Royalty Company", asset_class="Companies", description="Metals and mineral royalties", provider_symbol="TMCR", tv_symbol="NASDAQ:TMCR", verified=1, currency="USD", exchange="NASDAQ", name_source="https://www.themetalsroyaltyco.com/investor-relations/investor-resources"),
    dict(symbol="MSFT", name="Microsoft", asset_class="Companies", description="Software, cloud services and AI", provider_symbol="MSFT", tv_symbol="NASDAQ:MSFT", verified=1, currency="USD", exchange="NASDAQ", name_source="https://www.microsoft.com/en-us/investor/default"),
    dict(symbol="TLT", name="iShares 20+ Year Treasury Bond ETF", asset_class="Bonds", description="Long-term US Treasury bond prices", provider_symbol="TLT", tv_symbol="NASDAQ:TLT", verified=1, currency="USD", exchange="NASDAQ", name_source="https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf"),
]


def ensure_instrument(conn, symbol):
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
        raise ValueError("Invalid instrument identifier")
    conn.execute("INSERT OR IGNORE INTO instruments(symbol,name,provider_symbol) VALUES(?,?,?)", (symbol, f"Unverified instrument ({symbol})", symbol))


def chart_metadata(symbol):
    req = Request(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d", headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=25) as response:
        payload = json.load(response)
    return payload["chart"]["result"][0]["meta"]


def resolve_instrument(conn, symbol):
    row = conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()
    if row["verified"]:
        return
    meta = chart_metadata(row["provider_symbol"])
    exchanges = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NYQ": "NYSE", "ASE": "AMEX", "PCX": "AMEX", "BTS": "CBOE"}
    exchange = exchanges.get(meta.get("exchangeName"))
    name = meta.get("longName") or meta.get("shortName")
    kind = meta.get("instrumentType")
    if not name or not exchange or meta.get("currency") != "USD" or kind not in ("EQUITY", "ETF"):
        raise ValueError("Instrument needs a verified US listing and company/fund name")
    # Only curated mappings are exportable to TradingView. Provider exchanges alone
    # cannot establish TradingView's exchange prefix for a newly encountered fund.
    asset = "Equity funds" if kind == "ETF" else "Companies"
    if kind == "ETF" and any(word in name.lower() for word in ("bond", "treasury", "fixed income")):
        asset = "Bonds"
    conn.execute("UPDATE instruments SET name=?,asset_class=?,verified=1,currency='USD',exchange=?,name_source=? WHERE symbol=?", (name, asset, exchange, "Yahoo Finance metadata", symbol))
