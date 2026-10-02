import csv
import gzip
import hashlib
import io
import json
import os
import subprocess
import tempfile
from datetime import date, timedelta
from decimal import Decimal, localcontext

from .. import db
from . import PARSER_VERSION, SOURCE_URL
from .catalog import Catalog
from .parser import UnmappedReport, parse_live, parse_historical, pdf_text, percentage


def archive(raw, suffix):
    digest = hashlib.sha256(raw).hexdigest()
    relative = f'dbmf/raw/{digest}.{suffix}.gz'
    path = db.DATA_DIR / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(gzip.compress(raw, mtime=0))
        os.replace(temporary, path)
    return relative, digest


def ingest(conn, raw, now=None, *, source_url=SOURCE_URL, kind='live', extracted_text=None, expected_date=None, replayed_from=None):
    now = now or db.utcnow()
    if isinstance(raw, str):
        raw = raw.encode()
    path, raw_hash = archive(raw, 'html' if kind == 'live' else 'pdf')
    parsed, failure, fingerprint = None, None, None
    catalog = Catalog(conn)
    try:
        parsed = parse_live(raw, now, catalog.identify) if kind == 'live' else parse_historical(extracted_text if extracted_text is not None else pdf_text(raw), now, catalog.identify)
        if expected_date and parsed['source_date'] != expected_date:
            raise ValueError(f"Catalog date {expected_date} does not match report {parsed['source_date']}")
        # Source order / page decoration do not manufacture new observations.
        normalized = {key: parsed[key] for key in ('source_date', 'net_assets')}
        normalized['holdings'] = sorted([{key: row[key] for key in
            ('original_name', 'market_id', 'notional', 'quantity', 'identifier', 'ticker', 'expiry', 'weight')}
            for row in parsed['holdings']], key=lambda row: json.dumps(row, sort_keys=True))
        fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    except UnmappedReport as exc:
        parsed, failure = exc.report, str(exc)[:1500]
        if expected_date and parsed['source_date'] != expected_date:
            parsed['metadata'].pop('unmapped_holdings', None)
            failure = f"Catalog date {expected_date} does not match report {parsed['source_date']}"
    except (ValueError, UnicodeError, OSError, subprocess.SubprocessError) as exc:
        failure = str(exc)[:1500]
    metadata = dict(parsed['metadata']) if parsed else {}
    if expected_date:
        metadata['expected_date'] = expected_date
    if replayed_from:
        metadata['replayed_from'] = replayed_from
    with conn:
        # Collection, backfill and replay use separate process locks. Reserve
        # the database writer before checking for a duplicate or next revision.
        conn.execute('BEGIN IMMEDIATE')
        source_observation = (conn.execute('''SELECT id FROM dbmf_observations
            WHERE report_id=? AND source_observation_id IS NULL ORDER BY id LIMIT 1''',
            (replayed_from,)).fetchone() if replayed_from else None)
        def observe(report_id):
            conn.execute('''INSERT INTO dbmf_observations
                (report_id,collected_at,raw_path,raw_hash,source_url,source_observation_id)
                VALUES(?,?,?,?,?,?)''', (report_id, db.iso(now), path, raw_hash, source_url,
                    source_observation['id'] if source_observation else None))
        previous = None
        if parsed and not failure:
            previous = conn.execute("SELECT * FROM dbmf_observed_reports WHERE status='accepted' AND source_date=? AND source_kind=? ORDER BY last_observed_at DESC,(observation_time_basis='acquisition') DESC,observation_order DESC,id DESC LIMIT 1", (parsed['source_date'], kind)).fetchone()
            if previous and previous['fingerprint'] == fingerprint:
                observe(previous['id'])
                return dict(id=previous['id'], status='accepted', duplicate=True, raw_path=path, source_date=parsed['source_date'])
        revision = (conn.execute("SELECT coalesce(max(revision),0)+1 FROM dbmf_reports WHERE status='accepted' AND source_date=?", (parsed['source_date'],)).fetchone()[0] if parsed and not failure else 0)
        rid = conn.execute('''INSERT INTO dbmf_reports
            (source_date,net_assets,collected_at,source_url,source_kind,raw_path,raw_hash,fingerprint,
             parser_version,mapping_version,status,error,revision,metadata_json,replayed_from,processed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (parsed['source_date'] if parsed else None, parsed['net_assets'] if parsed else None,
             db.iso(now), source_url, kind, path, raw_hash, fingerprint, PARSER_VERSION, catalog.version,
             'rejected' if failure else 'accepted', failure, revision, json.dumps(metadata), replayed_from, db.iso())).lastrowid
        observe(rid)
        if not failure:
            for number, row in enumerate(parsed['holdings'], 1):
                keys = ['market_id','original_name','identifier','ticker','quantity','expiry','notional','weight','exposure_pct','evidence']
                conn.execute(f"INSERT INTO dbmf_holdings(report_id,row_number,{','.join(keys)}) VALUES({','.join('?' for _ in range(12))})",
                             (rid, number, *(row[key] for key in keys)))
    return dict(id=rid, status='rejected' if failure else 'accepted', error=failure,
                duplicate=False, raw_path=path, source_date=parsed['source_date'] if parsed else None)


def reports(conn):
    """One accepted observation per reporting date. Daily source wins a same-date import."""
    return conn.execute('''SELECT * FROM (
        SELECT *,row_number() OVER(PARTITION BY source_date ORDER BY
            CASE source_kind WHEN 'live' THEN 1 ELSE 0 END DESC,last_observed_at DESC,
            (observation_time_basis='acquisition') DESC,observation_order DESC,id DESC) AS rank
        FROM dbmf_observed_reports WHERE status='accepted') WHERE rank=1 ORDER BY source_date''').fetchall()


def report_dict(row):
    if row is None:
        return None
    result = dict(row)
    result.pop('rank', None)
    result['metadata'] = json.loads(result.pop('metadata_json'))
    return result


def aggregate(conn, report):
    if report is None:
        return []
    grouped = {}
    for row in conn.execute('SELECT * FROM dbmf_holdings WHERE report_id=? ORDER BY row_number', (report['id'],)):
        grouped.setdefault(row['market_id'], []).append(dict(row))
    result = []
    with localcontext() as ctx:
        ctx.prec = 50
        for market in conn.execute('SELECT * FROM dbmf_markets ORDER BY sort_order'):
            holdings = grouped.get(market['id'], [])
            values = [Decimal(row['notional']) for row in holdings]
            long = sum((v for v in values if v > 0), Decimal(0))
            short = sum((v for v in values if v < 0), Decimal(0))
            item = dict(market)
            item.update(net_pct=percentage(long + short, report['net_assets']),
                        long_pct=percentage(long, report['net_assets']),
                        short_pct=percentage(short, report['net_assets']),
                        gross_pct=percentage(long - short, report['net_assets']),
                        net_notional=str(long + short), holding_count=len(holdings), holdings=holdings,
                        absent=not holdings)
            result.append(item)
    return result


def snapshot(conn, row):
    if row is None:
        return None
    result = report_dict(row)
    result['markets'] = aggregate(conn, row)
    futures = [item for item in result['markets'] if item['category'] != 'Collateral']
    collateral = [item for item in result['markets'] if item['category'] == 'Collateral']
    with localcontext() as ctx:
        ctx.prec = 50
        result['summary'] = {key: str(sum((Decimal(item[key]) for item in futures), Decimal(0))) for key in ('long_pct', 'short_pct', 'gross_pct')}
        result['summary']['collateral_pct'] = str(sum((Decimal(item['net_pct']) for item in collateral), Decimal(0)))
    return result


def exposure_response(conn, compare='previous', compare_date=None, report_id=None):
    available = reports(conn)
    current = available[-1] if available else None
    target, requested = None, None
    if current:
        day = date.fromisoformat(current['source_date'])
        if report_id is not None:
            target = conn.execute("SELECT * FROM dbmf_reports WHERE id=? AND status='accepted'", (report_id,)).fetchone()
        elif compare == 'previous':
            target = available[-2] if len(available) > 1 else None
        else:
            if compare == 'week':
                requested = (day - timedelta(days=7)).isoformat()
            elif compare == 'month':
                # Same calendar day in the previous month, clipped at month end.
                previous_month = day.replace(day=1) - timedelta(days=1)
                requested = previous_month.replace(day=min(day.day, previous_month.day)).isoformat()
            else:
                requested = compare_date
            earlier = [row for row in available if requested and row['source_date'] <= requested]
            target = earlier[-1] if earlier else None
    current_data, comparison = snapshot(conn, current), snapshot(conn, target)
    if current_data:
        old = {m['id']: m for m in comparison['markets']} if comparison else {}
        with localcontext() as ctx:
            ctx.prec = 50
            for item in current_data['markets']:
                item['comparison_pct'] = old[item['id']]['net_pct'] if old else None
                item['change_pp'] = str(Decimal(item['net_pct']) - Decimal(item['comparison_pct'])) if old else None
    return dict(current=current_data, comparison=comparison, requested_date=requested,
                comparison_date=target['source_date'] if target else None,
                comparison_missing=target is None,
                interpretation='Exposure changes do not establish actual trades. Notional exposures can exceed 100% and do not measure risk contribution.')


def history_response(conn, start=None, end=None):
    observations = []
    for row in reports(conn):
        if (start and row['source_date'] < start) or (end and row['source_date'] > end):
            continue
        data = snapshot(conn, row)
        observations.append(dict(id=row['id'], date=row['source_date'], source_kind=row['source_kind'],
                                 revision=row['revision'], net_assets=row['net_assets'],
                                 exposures={m['id']: {key: m[key] for key in ('net_pct','long_pct','short_pct','gross_pct','absent')} for m in data['markets']}))
    return dict(observations=observations, markets=[dict(m) for m in conn.execute('SELECT * FROM dbmf_markets ORDER BY sort_order')],
                interpolation=False, gap_rule='Each mark is an observed report. Unreported dates have no exposure value.')


def csv_export(conn):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['report_id','reporting_date','revision','source_kind','market','net_exposure_pct','long_exposure_pct','short_exposure_pct','gross_exposure_pct','net_notional_usd','fund_net_assets_usd','collected_at','source_url','parser_version'])
    # All accepted revisions are exported; the dashboard selects the latest per date.
    for row in conn.execute("SELECT * FROM dbmf_reports WHERE status='accepted' ORDER BY source_date,revision"):
        for item in aggregate(conn, row):
            writer.writerow([row['id'],row['source_date'],row['revision'],row['source_kind'],item['name'],item['net_pct'],item['long_pct'],item['short_pct'],item['gross_pct'],item['net_notional'],row['net_assets'],row['collected_at'],row['source_url'],row['parser_version']])
    return output.getvalue()
