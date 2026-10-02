import re
import unicodedata

# Explicit, versioned mappings. Unknown instruments stop publication of a report.
MAPPING_VERSION = "dbmf-markets-4"
MARKETS = [
    ("us2y", "US 2-year Treasury notes", "Bonds", "ZT=F", "2-year Treasury futures", "futures", 0),
    ("us10y", "US 10-year Treasury notes", "Bonds", "ZN=F", "10-year Treasury futures", "futures", 0),
    ("uslong", "US long Treasury bonds", "Bonds", "ZB=F", "Treasury bond futures", "futures", 0),
    ("sp500", "US large-cap equities", "Equities", "ES=F", "S&P 500 futures", "futures", 0),
    ("eafe", "Developed equities outside North America", "Equities", "EFA", "iShares MSCI EAFE ETF", "ETF proxy", 0),
    ("em", "Emerging-market equities", "Equities", "EEM", "iShares MSCI Emerging Markets ETF", "ETF proxy", 0),
    ("gold", "Gold", "Commodities", "GC=F", "Gold futures", "futures", 0),
    ("wti", "WTI crude oil", "Commodities", "CL=F", "Crude oil futures", "futures", 0),
    ("eur", "Euro versus US dollar", "Currencies", "EURUSD=X", "EUR/USD", "currency", 0),
    ("jpy", "Japanese yen versus US dollar", "Currencies", "JPY=X", "Inverted USD/JPY (USD per yen)", "currency", 1),
    ("us10ultra", "US ultra 10-year Treasury notes", "Bonds", "TN=F", "Ultra 10-year Treasury futures", "futures", 0),
    ("usultra", "US ultra-long Treasury bonds", "Bonds", "UB=F", "Ultra Treasury bond futures", "futures", 0),
    ("fedfunds", "US federal funds futures", "Short-term rates", "ZQ=F", "30-day federal funds futures", "futures", 0),
    ("eurodollar", "US dollar Eurodollar futures", "Short-term rates", None, "Eurodollar futures ended in 2023; Yahoo history unavailable", "unavailable", 0),
    ("sofr", "US 3-month SOFR futures", "Short-term rates", None, "SOFR futures: Yahoo daily history unavailable", "unavailable", 0),
    ("tbills", "Treasury bills", "Collateral", None, None, None, 0),
    ("cash", "Cash", "Collateral", None, None, None, 0),
    ("repo", "Repurchase agreements", "Collateral", None, None, None, 0),
]

PATTERNS = {
    "us2y": [r"US 2YR NOTE \(CBT\) ?[A-Z]{3}\d{2}", r"U\.S\. Treasury 2-Year Note Futures"],
    "us10y": [r"US 10YR NOTE \(CBT\) ?[A-Z]{3}\d{2}", r"U\.S\. Treasury 10-Year Note Futures"],
    "uslong": [r"US LONG BOND\(CBT\) ?[A-Z]{3}\d{2}", r"U\.S\. Treasury Long Bond Futures", r"U\.S\. Treasury Bonds 20 Year Bond Futures"],
    "us10ultra": [r"U\.S\. Treasury 10-Year Ultra (?:Note|Bond) Futures"],
    "usultra": [r"U\.S\. Treasury Ultra(?:-Long)? Bond Futures"],
    "sp500": [r"S\+P500 EMINI FUT [A-Z]{3}\d{2}", r"S&P 500 E-Mini Index Futures"],
    "eafe": [r"MSCI EAFE ?[A-Z]{3}\d{2}", r"MSCI EAFE Index Futures"],
    "em": [r"MSCI EMGMKT [A-Z]{3}\d{2}", r"MSCI Emerging Market Index(?: Futures)?"],
    "gold": [r"GOLD 100 OZ FUTR [A-Z]{3}\d{2}", r"Gold 100 Oz\.? Futures"],
    "wti": [r"WTI CRUDEFUTURE [A-Z]{3}\d{2}", r"WTI Crude Futures"],
    "eur": [r"EURO FX CURR FUT [A-Z]{3}\d{2}", r"Euro FX Currency Futures"],
    "jpy": [r"JPN YEN CURR FUT [A-Z]{3}\d{2}", r"Japanese Yen Currency Futures"],
    "fedfunds": [r"30[ -]day (?:Fed Fund|Federal Funds) Futures"],
    "eurodollar": [r"90-day Euro-Dollar Futures"],
    "sofr": [r"3 Months SOFR Futures"],
    "tbills": [r"TREASURY BILL", r"U\.S\. Treasury Bills?(?: .+)?"],
    "cash": [r"CASH", r"US DOLLAR", r"CASH \(USD\)"],
    "repo": [r"Fixed Income Clearing Corp\.", r"REPURCHASE AGREEMENTS?"],
}
PATTERNS = {key: [pattern.replace(r'[A-Z]{3}', r'(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)')
                  for pattern in patterns] for key, patterns in PATTERNS.items()}


class UnknownInstrument(ValueError):
    pass


# Only spelling, punctuation and a trailing exchange month/year are normalized.
# Maturity, currency, index, contract size and words such as "ultra" remain part
# of the identity. No fuzzy matching or ticker guessing is used.
EXPIRY = re.compile(r'(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*(\d{4}|\d{2})$', re.I)


def normalize_name(name):
    value = unicodedata.normalize('NFKC', name).strip()
    value = re.sub(r'\s*\([a-z]\)', '', value)
    value = EXPIRY.sub('', value).upper()
    value = re.sub(r'\bU\.?\s*S\.?\s*(?=\s|TREASURY|\d)', 'US ', value)
    value = re.sub(r'S\s*[+&]\s*P', 'SP', value)
    value = re.sub(r'(?<=\d)(?=[A-Z])|(?<=[A-Z])(?=\d)', ' ', value)
    words = re.findall(r'[A-Z0-9]+', value)
    equivalents = {'YR':'YEAR', 'YRS':'YEAR', 'YEARS':'YEAR', 'MONTHS':'MONTH',
                   'FUT':'FUTURES', 'FUTR':'FUTURES', 'FUTURE':'FUTURES', 'EMINI':'EMINI'}
    return ' '.join(equivalents.get(word, word) for word in words)


BASE_NAMES = {
    'us2y': ['US 2YR NOTE (CBT)', 'U.S. Treasury 2-Year Note Futures'],
    'us10y': ['US 10YR NOTE (CBT)', 'U.S. Treasury 10-Year Note Futures'],
    'uslong': ['US LONG BOND(CBT)', 'U.S. Treasury Long Bond Futures', 'U.S. Treasury Bonds 20 Year Bond Futures'],
    'us10ultra': ['U.S. Treasury 10-Year Ultra Note Futures', 'U.S. Treasury 10-Year Ultra Bond Futures'],
    'usultra': ['U.S. Treasury Ultra-Long Bond Futures', 'U.S. Treasury Ultra Bond Futures'],
    'sp500': ['S+P500 EMINI FUT', 'S&P 500 E-Mini Index Futures', 'S&P 500 E Mini Index'],
    'eafe': ['MSCI EAFE', 'MSCI EAFE Index Futures'],
    'em': ['MSCI EMGMKT', 'MSCI Emerging Market Index', 'MSCI Emerging Market Index Futures'],
    'gold': ['GOLD 100 OZ FUTR', 'Gold 100 Oz. Futures'],
    'wti': ['WTI CRUDEFUTURE', 'WTI Crude Futures'],
    'eur': ['EURO FX CURR FUT', 'Euro FX Currency Futures'],
    'jpy': ['JPN YEN CURR FUT', 'Japanese Yen Currency Futures'],
    'fedfunds': ['30-day Fed Fund Futures', '30-day Federal Funds Futures'],
    'eurodollar': ['90-day Euro-Dollar Futures'],
    'sofr': ['3 Months SOFR Futures'],
    'tbills': ['TREASURY BILL', 'U.S. Treasury Bills', 'U.S. Treasury Bill'],
    'cash': ['CASH', 'US DOLLAR', 'CASH (USD)'],
    'repo': ['Fixed Income Clearing Corp.', 'REPURCHASE AGREEMENT', 'REPURCHASE AGREEMENTS'],
}
BASE_ALIASES = {normalize_name(name): key for key, names in BASE_NAMES.items() for name in names}


def identify(name):
    clean = re.sub(r"\s*\([a-z]\)", "", name).strip()
    clean = " ".join(clean.split())
    matches = {market for market, patterns in PATTERNS.items()
               if any(re.fullmatch(pattern, clean, re.I) for pattern in patterns)}
    if normalized := BASE_ALIASES.get(normalize_name(name)):
        matches.add(normalized)
    if len(matches) > 1:
        raise ValueError(f'Ambiguous instrument mapping: {name}')
    if matches:
        return matches.pop()
    raise UnknownInstrument(f"Unrecognized instrument: {name}")


def initialize(conn):
    for order, (key, name, group, symbol, reference, kind, invert) in enumerate(MARKETS):
        conn.execute("""INSERT INTO dbmf_markets
            (id,name,category,provider_symbol,price_reference,price_kind,invert,sort_order,mapping_version)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,category=excluded.category,provider_symbol=excluded.provider_symbol,
            price_reference=excluded.price_reference,price_kind=excluded.price_kind,invert=excluded.invert,
            sort_order=excluded.sort_order,mapping_version=excluded.mapping_version""",
            (key, name, group, symbol, reference, kind, invert, order, MAPPING_VERSION))
