"""Read-only comparisons of accepted observations and immutable source revisions."""

from collections import defaultdict
from decimal import Decimal, localcontext

from .store import aggregate, exposure_response, report_dict, reports


FIELDS = ('market_id', 'original_name', 'identifier', 'ticker', 'expiry',
          'quantity', 'notional', 'weight', 'exposure_pct')
NUMERIC = {'quantity', 'notional', 'weight', 'exposure_pct'}


def revision_pairs(conn, since_id=0, before_id=2**63 - 1, limit=100, source_date=None):
    # Acquisition time, not import order or revision number, establishes order.
    # A live table and a consolidated historical schedule are different sources.
    return [dict(row) for row in conn.execute('''
        SELECT * FROM (
          SELECT id,source_date,source_kind,revision,collected_at,processed_at,
                 lag(id) OVER (PARTITION BY source_date,source_kind ORDER BY collected_at,original_observation_order,id) AS previous_id
          FROM dbmf_observed_reports WHERE status='accepted'
        ) WHERE previous_id IS NOT NULL AND id>? AND id<? AND (? IS NULL OR source_date=?)
        ORDER BY id DESC LIMIT ?
        ''', (since_id, before_id, source_date, source_date, limit))]


def changes(conn, baseline_id=None, since_id=None):
    result = exposure_response(conn, report_id=baseline_id)
    current, previous = result['current'], result['comparison']
    items = []
    if current and previous:
        old = {m['id']: m for m in previous['markets']}
        for market in current['markets']:
            before = old[market['id']]
            if Decimal(market['change_pp']) or market['absent'] != before['absent']:
                items.append(dict(id=market['id'], name=market['name'], category=market['category'],
                                  before_pct=before['net_pct'], after_pct=market['net_pct'],
                                  change_pp=market['change_pp'],
                                  kind='new' if before['absent'] and not market['absent'] else
                                  'absent' if market['absent'] and not before['absent'] else 'changed'))
        items.sort(key=lambda m: abs(Decimal(m['change_pp'])), reverse=True)
    available = reports(conn)
    max_id = conn.execute("SELECT coalesce(max(id),0) FROM dbmf_reports WHERE status='accepted'").fetchone()[0]
    # Default summary shows revisions to the current reporting date; a visit
    # cursor also catches corrections to old reporting dates imported later.
    revisions = revision_pairs(conn, since_id or 0, limit=101,
                               source_date=current['source_date'] if since_id is None and current else None)
    return dict(current={k: current[k] for k in ('id', 'source_date', 'collected_at')} if current else None,
                comparison={k: previous[k] for k in ('id', 'source_date', 'collected_at')} if previous else None,
                items=items, revisions=revisions[:100], revisions_more=len(revisions) > 100,
                new_reports=conn.execute("SELECT count(*) FROM dbmf_reports WHERE status='accepted' AND id>?", (since_id,)).fetchone()[0] if since_id is not None else None,
                cursor=dict(report_id=current['id'] if current else None, after_id=max_id),
                observation_count=len(available))


def _equal(field, a, b):
    if field in NUMERIC and a is not None and b is not None:
        return Decimal(a) == Decimal(b)
    return a == b


def _identity(row):
    # No fuzzy matching: changed identifiers without an exact match are shown
    # as removed/added rows. Ambiguous duplicate groups are never paired by order.
    return row['market_id'], row['identifier'] or row['ticker'] or row['original_name'], row['expiry']


def compare_revisions(conn, report_id, against_id=None):
    current = conn.execute("SELECT * FROM dbmf_observed_reports WHERE id=? AND status='accepted'", (report_id,)).fetchone()
    if not current:
        raise LookupError('Accepted report not found')
    if against_id is None:
        previous = conn.execute("""SELECT * FROM dbmf_observed_reports WHERE status='accepted'
            AND source_date=? AND source_kind=? AND (collected_at,original_observation_order,id)<(?,?,?)
            ORDER BY collected_at DESC,original_observation_order DESC,id DESC LIMIT 1""",
            (current['source_date'], current['source_kind'], current['collected_at'], current['original_observation_order'], report_id)).fetchone()
    else:
        previous = conn.execute("SELECT * FROM dbmf_observed_reports WHERE id=? AND status='accepted'", (against_id,)).fetchone()
        if not previous:
            raise LookupError('Comparison report not found')
        if previous['source_date'] != current['source_date'] or previous['source_kind'] != current['source_kind']:
            raise ValueError('Revisions must have the same reporting date and source type')
        if tuple(previous[k] for k in ('collected_at', 'original_observation_order', 'id')) >= tuple(current[k] for k in ('collected_at', 'original_observation_order', 'id')):
            raise ValueError('Choose an earlier revision of this source')
    if not previous:
        return dict(current=report_dict(current), previous=None, rows=[], markets=[], changed_rows=0, origin=None)
    groups = defaultdict(lambda: [[], []])
    for side, report in enumerate((previous, current)):
        for row in conn.execute('SELECT * FROM dbmf_holdings WHERE report_id=? ORDER BY row_number', (report['id'],)):
            groups[_identity(row)][side].append(dict(row))
    rows = []
    for before, after in groups.values():
        # Remove identical rows first, so reordering never appears as a change.
        for old in before[:]:
            match = next((new for new in after if all(_equal(k, old[k], new[k]) for k in FIELDS)), None)
            if match is not None:
                before.remove(old)
                after.remove(match)
        if len(before) == len(after) == 1:
            rows.append(dict(kind='changed', before=before[0], after=after[0],
                             fields=[k for k in FIELDS if not _equal(k, before[0][k], after[0][k])]))
        else:
            rows.extend(dict(kind='removed', before=row, after=None, fields=list(FIELDS)) for row in before)
            rows.extend(dict(kind='added', before=None, after=row, fields=list(FIELDS)) for row in after)
    old_markets = {m['id']: m for m in aggregate(conn, previous)}
    markets = []
    with localcontext() as ctx:
        ctx.prec = 50
        for market in aggregate(conn, current):
            before = old_markets[market['id']]
            if any(Decimal(market[k]) != Decimal(before[k]) for k in ('net_pct', 'gross_pct', 'net_notional')):
                markets.append(dict(id=market['id'], name=market['name'], before_pct=before['net_pct'],
                                    after_pct=market['net_pct'], change_pp=str(Decimal(market['net_pct']) - Decimal(before['net_pct'])),
                                    before_gross_pct=before['gross_pct'], after_gross_pct=market['gross_pct']))
    same_source = current['raw_hash'] == previous['raw_hash']
    processing_changed = any(current[k] != previous[k] for k in ('parser_version', 'mapping_version'))
    return dict(current=report_dict(current), previous=report_dict(previous), rows=rows, markets=markets,
                changed_rows=len(rows), net_assets_changed=Decimal(current['net_assets']) != Decimal(previous['net_assets']),
                origin='reprocessed' if same_source else 'source_and_processing' if processing_changed else 'source_changed')
