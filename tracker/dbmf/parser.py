"""Strict source adapters. Dollar inputs are never rounded or coerced to floats."""
import re
import subprocess
from datetime import datetime
from decimal import Decimal, localcontext

from bs4 import BeautifulSoup

from .markets import EXPIRY, UnknownInstrument, identify

NUMBER = r"\(?-?\d[\d,]*(?:\.\d+)?\)?"
FUND = r"iMGP DBi Managed Futures Strategy ETF"
SCHEDULE_FUND = FUND + r"(?: \(formerly iM DBi Managed Futures Strategy ETF\))?"


def decimal(value):
    clean = re.sub(r"[\s$,]", "", str(value).replace('\u2212', '-'))
    if not re.fullmatch(r"(?:-?\d+(?:\.\d+)?|\(\d+(?:\.\d+)?\))", clean):
        raise ValueError(f"Invalid numeric value: {value!r}")
    result = Decimal('-' + clean[1:-1] if clean.startswith('(') else clean)
    if not result.is_finite():
        raise ValueError("Non-finite number")
    return result


def percentage(value, assets):
    with localcontext() as ctx:
        ctx.prec = 50
        return format(Decimal(value) / Decimal(assets) * 100, 'f')


def holding(name, value, *, quantity=None, identifier=None, ticker=None, expiry=None, weight=None, evidence=""):
    return dict(original_name=name, market_id=None, notional=str(decimal(value)),
                quantity=quantity, identifier=identifier, ticker=ticker, expiry=expiry,
                weight=weight, evidence=evidence)


class UnmappedReport(ValueError):
    def __init__(self, report, unknown):
        self.report = report
        report['metadata']['unmapped_holdings'] = unknown
        super().__init__('Unrecognized instruments: ' + '; '.join(dict.fromkeys(row['original_name'] for row in unknown)))


def validate(report, now, resolver=identify):
    assets = decimal(report['net_assets'])
    if assets <= 0:
        raise ValueError("Total net assets must be positive")
    day = datetime.strptime(report['source_date'], '%Y-%m-%d').date()
    from ..calendar import NY
    if day.isoformat() < '2019-05-07' or day > now.astimezone(NY).date():
        raise ValueError("Reporting date is outside DBMF's observed lifetime")
    if not report['holdings']:
        raise ValueError("Empty holdings report")
    seen = set()
    for item in report['holdings']:
        item['exposure_pct'] = percentage(item['notional'], assets)
        if item['weight'] is not None:
            # Website weights are rounded fractions (e.g. -1.01 = -101%).
            with localcontext() as ctx:
                ctx.prec = 50
                tolerance = Decimal(item.get('weight_tolerance', '0.005')) + Decimal('0.000000000001')
                if abs(decimal(item['notional']) / assets - decimal(item['weight'])) > tolerance:
                    raise ValueError(f"Rounded weight inconsistent with dollar exposure: {item['original_name']}")
        signature = tuple(item.get(k) for k in ('original_name', 'identifier', 'ticker', 'expiry', 'notional', 'quantity'))
        if signature in seen:
            raise ValueError(f"Duplicate source row: {item['original_name']}")
        seen.add(signature)
    report['net_assets'] = str(assets)
    unknown = []
    # Finish all structural and arithmetic checks before making any rows
    # available for review. Collect every unknown, rather than only the first.
    for number, item in enumerate(report['holdings'], 1):
        try:
            item['market_id'] = resolver(item['original_name'])
        except UnknownInstrument:
            unknown.append(dict(item, row_number=number))
    if unknown:
        raise UnmappedReport(report, unknown)
    return report


HEADINGS = {
    'date': ('date', 'asofdate', 'holdingsdate'),
    'name': ('securityname', 'security', 'holding'),
    'identifier': ('cusip', 'isin', 'identifier'),
    'ticker': ('ticker', 'symbol'),
    'quantity': ('sharesqty', 'sharesquantity', 'quantity', 'shares'),
    'value': ('marketvalue', 'marketvalueusd'),
    'weight': ('weight', 'weightpercent', 'weightpercentage'),
}


def heading_map(table):
    rows = table.select('thead tr')
    if len(rows) != 1:
        raise ValueError('Expected one holdings header row')
    labels = [cell.get_text(' ', strip=True) for cell in rows[0].find_all(['td', 'th'], recursive=False)]
    columns, extra = {}, []
    for index, label in enumerate(labels):
        key = re.sub(r'[^a-z0-9]', '', label.lower())
        match = next((name for name, aliases in HEADINGS.items() if key in aliases), None)
        if match:
            if match in columns:
                raise ValueError(f'Ambiguous holdings column: {label}')
            columns[match] = index
        elif key in ('currency', 'ccy'):
            if 'currency' in columns:
                raise ValueError('Ambiguous currency column')
            columns['currency'] = index
        else:
            if any(word in key for word in ('weight', 'notional', 'value')):
                raise ValueError(f'Unrecognized financial column: {label}')
            extra.append(label)
    if not set(HEADINGS).issubset(columns):
        raise ValueError('Holdings columns changed; required fields are missing')
    weight_label = labels[columns['weight']].lower()
    return columns, labels, extra, '%' in weight_label or 'percent' in weight_label


def parse_live(raw, now, resolver=identify):
    html = raw.decode('utf-8') if isinstance(raw, bytes) else raw
    soup = BeautifulSoup(html, 'html.parser')
    if not re.search(FUND.replace(' ', r'\s+'), soup.get_text(' ', strip=True), re.I):
        raise ValueError("DBMF fund identity missing")
    tables = soup.select('table#breakdown-holdings-us')
    fragments = list(re.finditer(r'<table\b[^>]*>[\s\S]*?</table\s*>', html, re.I))
    candidates = []
    for fragment in fragments:
        candidate = BeautifulSoup(fragment[0], 'html.parser').find('table')
        if tables and candidate.get('id') != 'breakdown-holdings-us':
            continue
        try:
            mapping = heading_map(candidate)
        except ValueError:
            if tables and candidate.get('id') == 'breakdown-holdings-us':
                raise
            continue
        if not re.search(r'</tbody\s*>', fragment[0], re.I) or len(re.findall(r'<table\b', fragment[0], re.I)) != 1:
            raise ValueError('Truncated or nested holdings table')
        candidates.append((candidate, mapping))
    if len(tables) > 1 or len(candidates) != 1:
        raise ValueError('Expected one complete, unambiguous official holdings table')
    table, (columns, headers, extra, weight_percent) = candidates[0]
    body = table.find('tbody', recursive=False)
    if body is None:
        raise ValueError("Holdings body missing")
    rows = body.find_all('tr', recursive=False)
    if len(rows) < 2 or len(rows) != len(table.select('tbody tr')):
        raise ValueError("Empty or nested holdings rows")
    dates, holdings, total = set(), [], None
    for number, row in enumerate(rows):
        cells = row.find_all('td', recursive=False)
        if len(cells) != len(headers) or any(cell.get('colspan') not in (None, '1') or cell.get('rowspan') not in (None, '1') for cell in cells):
            raise ValueError(f"Incomplete source row {number + 1}")
        values = [cell.get_text(' ', strip=True) for cell in cells]
        day, name, identifier, ticker, quantity, value, weight = [values[columns[key]] for key in HEADINGS]
        dates.add(datetime.strptime(day, '%Y-%m-%d' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) else '%m/%d/%Y').date().isoformat())
        if name.upper() == 'TOTAL NET ASSETS':
            if number != len(rows) - 1 or total is not None or [identifier, ticker, quantity, weight] != ['-'] * 4:
                raise ValueError("Total net assets must be the single final row")
            total = str(decimal(value))
            continue
        if not identifier or not name:
            raise ValueError("Missing holding identity")
        if 'currency' in columns and values[columns['currency']].upper() != 'USD':
            raise ValueError('Holding values must be explicitly denominated in USD')
        with localcontext() as ctx:
            ctx.prec = 50
            published_weight = decimal(weight.replace('%', ''))
            divisor = 100 if weight_percent or '%' in weight else 1
            rounded_weight = published_weight / divisor
            weight_tolerance = Decimal(10) ** published_weight.as_tuple().exponent / 2 / divisor
        contract = EXPIRY.search(name)
        holdings.append(holding(name, value, identifier=identifier, ticker=ticker,
                                quantity=None if quantity.upper() in ('-', 'N/A') else str(decimal(quantity)), weight=str(rounded_weight),
                                expiry=contract[0].replace(' ', '').upper() if contract else None, evidence=' | '.join(values)))
        holdings[-1]['weight_tolerance'] = str(weight_tolerance)
    if total is None or len(dates) != 1:
        raise ValueError("Missing total net assets or inconsistent row dates")
    return validate(dict(source_date=dates.pop(), net_assets=total, holdings=holdings,
                         metadata=dict(completeness='All rows in the closed official table, final total and row weights validated',
                                       source_rows=len(rows), holdings_rows=len(holdings),
                                       value_column='Market Value (signed futures notional; collateral market value)',
                                       extra_columns=extra,
                                       weight_units='Rounded fraction of total net assets')), now, resolver)


def pdf_text(raw):
    if not raw.startswith(b'%PDF'):
        raise ValueError("Expected a PDF report")
    process = subprocess.run(['pdftotext', '-layout', '-', '-'], input=raw, capture_output=True, timeout=60, check=True)
    return process.stdout.decode('utf-8')


def parse_historical(text, now, resolver=identify):
    text = text.replace('\f', '\n')
    # Anchor to this fund AND its consolidated schedules, never a subsidiary or another fund.
    headers = list(re.finditer(SCHEDULE_FUND + r'\s*\nCONSOLIDATED SCHEDULE OF INVESTMENTS IN SECURITIES\s+AT\s+([A-Za-z]+ \d{1,2}, \d{4})', text, re.I))
    if len(headers) != 1:
        raise ValueError("Expected one DBMF consolidated securities schedule")
    start = headers[0]
    day = datetime.strptime(start[1], '%B %d, %Y').date().isoformat()
    following = text[start.end():]
    next_fund = re.search(r'\n(?:iMGP|iM |Litman Gregory) [^\n]*\s*\n(?:CONSOLIDATED )?SCHEDULE OF INVESTMENTS', following, re.I)
    if not next_fund:
        raise ValueError("Consolidated futures schedule missing")
    securities = following[:next_fund.start()]
    future_header = re.match(r'\n' + SCHEDULE_FUND + r'\s*\nCONSOLIDATED SCHEDULE OF INVESTMENTS IN FUTURES CONTRACTS\s+AT\s+([A-Za-z]+ \d{1,2}, \d{4})', following[next_fund.start():], re.I)
    if not future_header or future_header[1].lower() != start[1].lower():
        raise ValueError("Futures identity/date does not match the securities schedule")
    futures = following[next_fund.start() + future_header.end():]
    end = re.search(r'Total Futures Contracts\s+\$?\s*(' + NUMBER + ')', futures, re.I)
    if not end:
        raise ValueError("Futures total missing")
    futures = futures[:end.end()]
    if not re.search(r'Notional\s+Value', futures) or not re.search(r'Notional\s+Amount', re.sub(r'\s+', ' ', futures)):
        # Some layouts put Notional over Amount, in a different header line.
        if not ('Notional Value' in futures and 'Amount' in futures and 'Contracts' in futures) and not re.search(r'Notional\s+Notional\s+Expiration[^\n]*\nDescription\s+Contracts\s+Amount\s+Value\s+Date', futures):
            raise ValueError("Current Notional Value column could not be identified")
    navs = re.findall(r'NET ASSETS:\s*100\.0%\s*\$?\s*(' + NUMBER + ')', securities)
    if len(navs) != 1:
        raise ValueError("Matching consolidated net assets missing")
    assets = decimal(navs[0])
    rows, subtotals, side = [], {'long': Decimal(0), 'short': Decimal(0)}, None
    observed_totals, sections = set(), set()
    row_re = re.compile(r'^(.+?)\s{2,}(\(?[\d,]+\)?)\s+\$?\s*(' + NUMBER + r')\s+\$?\s*(' + NUMBER + r')\s+(\d{1,2}/\d{1,2}/\d{4})\s+\$?\s*(' + NUMBER + r')\s*$')
    for line in futures.splitlines():
        clean = line.strip().replace('–', '-').replace('—', '-')
        if not clean:
            continue
        if clean in ('Futures Contracts - Long', 'Futures Contracts - Short'):
            side = clean.rsplit(' ', 1)[1].lower()
            if side in sections:
                raise ValueError('Duplicate futures section')
            sections.add(side)
            continue
        if side is None:
            continue
        total = re.fullmatch(r'Total (Long|Short|Futures Contracts)\s+\$?\s*(' + NUMBER + ')', clean, re.I)
        if total:
            key = total[1].lower()
            expected = sum(subtotals.values()) if key == 'futures contracts' else subtotals[key]
            if abs(decimal(total[2]) - expected) > Decimal(2):
                raise ValueError(f"Historical {key} row checksum failed")
            observed_totals.add(key)
            continue
        match = row_re.fullmatch(clean)
        if not match:
            raise ValueError(f"Unrecognized historical futures row: {clean[:180]}")
        name, quantity, original, current, expiry, pnl = match.groups()
        if (decimal(quantity) < 0) != (side == 'short'):
            raise ValueError("Historical contract direction disagrees with section")
        if abs(decimal(current) - decimal(original) - decimal(pnl)) > Decimal(2):
            raise ValueError(f"Historical current-notional reconciliation failed: {name}")
        subtotals[side] += decimal(pnl)
        rows.append(holding(name.strip(), current, quantity=str(decimal(quantity)),
                            expiry=datetime.strptime(expiry, '%m/%d/%Y').date().isoformat(), evidence=clean))
    if 'futures contracts' not in observed_totals or not sections.issubset(observed_totals) or not rows:
        raise ValueError("Incomplete historical futures schedule")
    # Read each bill's fair value, then reconcile against the category total.
    collateral_total = Decimal(0)
    bills = re.search(r'(?<!TOTAL )TREASURY BILLS:\s*[\d.]+%([\s\S]+?)TOTAL TREASURY BILLS\s*\n[^\n]+', securities, re.I)
    if bills:
        expected = decimal(re.findall(NUMBER, bills[0].splitlines()[-1])[-1])
        bill_rows = list(re.finditer(r'(\d{1,2}/\d{1,2}/\d{4})(?:\([a-z]\))*\s+\$?\s*(' + NUMBER + r')\s*(?:\n|$)', bills[1]))
        if not bill_rows or sum(decimal(m[2]) for m in bill_rows) != expected:
            raise ValueError("Treasury bill rows do not reconcile with total")
        for row in bill_rows:
            rows.append(holding('U.S. Treasury Bills ' + row[1], row[2],
                                expiry=datetime.strptime(row[1], '%m/%d/%Y').date().isoformat(), evidence=row[0].strip()))
        collateral_total += expected
    repo = re.search(r'REPURCHASE AGREEMENTS:\s*[\d.]+%([\s\S]+?)TOTAL REPURCHASE AGREEMENTS\s*\n([^\n]+)', securities, re.I)
    if repo:
        value = decimal(re.findall(NUMBER, repo[2])[-1])
        if not re.search(r'Fixed\s+Income\s+Clearing\s+Corp\.', repo[1]):
            raise ValueError("Unrecognized repurchase agreement counterparty")
        # The single counterparty's fair value must agree with the total; pledged
        # collateral and repayment proceeds inside its description are not holdings.
        fair = re.search(r'\)\s*\$?\s*(' + NUMBER + r')\s*$', repo[1])
        if not fair or decimal(fair[1]) != value:
            raise ValueError("Repurchase agreement does not reconcile with total")
        rows.append(holding('Fixed Income Clearing Corp.', value, evidence=repo[0].strip()))
        collateral_total += value
    investment = re.search(r'TOTAL INVESTMENTS\s*\n[^\n]*\s+(' + NUMBER + r')\s*(?:\n|$)', securities)
    if not investment or decimal(investment[1]) != collateral_total:
        raise ValueError("Securities schedule contains incomplete or unrecognized investments")
    other = re.search(r'Other Assets in Excess of\s*Liabilities:\s*[\d.]+%\s*(' + NUMBER + ')', securities)
    if not other or decimal(other[1]) + collateral_total != assets:
        raise ValueError("Historical assets and liabilities do not reconcile with net assets")
    return validate(dict(source_date=day, net_assets=str(assets), holdings=rows,
                         metadata=dict(completeness='Consolidated schedules; all futures rows, accounting checksums, collateral and net assets reconciled',
                                       value_column='Current Notional Value (not original amount or accounting gain/loss)',
                                       other_assets_less_liabilities=str(decimal(other[1])),
                                       collateral_note='Other assets less liabilities are not identified as cash.',
                                       holdings_rows=len(rows))), now, resolver)
