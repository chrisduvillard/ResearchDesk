import gzip
import json
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Path as ApiPath
from fastapi.responses import Response

from .. import db
from ..calendar import NY
from . import INCEPTION, SOURCE_URL
from .collector import schedule
from .store import reports, report_dict, snapshot, exposure_response, history_response, csv_export
from .catalog import Catalog
from .recovery import pending_review
from .changes import changes as changes_view, revision_pairs, compare_revisions

router = APIRouter(prefix='/api/dbmf', tags=['DBMF'])


@router.get('/changes')
def changes(baseline_id: int | None=Query(None, ge=1, le=db.MAX_ID), since_id: int | None=Query(None, ge=0, le=db.MAX_ID)):
    with db.database() as conn:
        if baseline_id is not None and not conn.execute("SELECT 1 FROM dbmf_reports WHERE id=? AND status='accepted'", (baseline_id,)).fetchone():
            raise HTTPException(404, 'Saved comparison report is no longer available')
        return changes_view(conn, baseline_id, since_id)


@router.get('/revisions')
def revisions(limit: int=Query(100, ge=1, le=200), before_id: int | None=Query(None, ge=1, le=db.MAX_ID)):
    with db.database() as conn:
        rows = revision_pairs(conn, before_id=before_id or 2**63-1, limit=limit+1)
        return dict(items=rows[:limit], has_more=len(rows)>limit)


@router.get('/revisions/{report_id}')
def revision(report_id: int = ApiPath(ge=1, le=db.MAX_ID), against_id: int | None=Query(None, ge=1, le=db.MAX_ID)):
    with db.database() as conn:
        try:
            return compare_revisions(conn, report_id, against_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get('/status')
def status():
    now = db.utcnow()
    with db.database() as conn:
        available = reports(conn)
        current = available[-1] if available else None
        last = conn.execute("SELECT * FROM dbmf_runs WHERE kind='live' ORDER BY id DESC LIMIT 1").fetchone()
        accepted = conn.execute("""SELECT r.finished_at FROM dbmf_runs r JOIN dbmf_reports p ON p.id=r.report_id
            WHERE r.kind='live' AND r.finished_at IS NOT NULL AND (p.status='accepted' OR EXISTS
            (SELECT 1 FROM dbmf_replay_attempts WHERE source_report_id=p.id AND status='accepted'))
            ORDER BY r.id DESC LIMIT 1""").fetchone()
        heartbeat = db.setting(conn, 'dbmf_worker_heartbeat')
        last_run = dict(last) if last else None
        if last_run:
            last_run['price_errors'] = json.loads(last_run['price_errors'])
            last_run['recovered_by_replay'] = bool(conn.execute("SELECT 1 FROM dbmf_replay_attempts WHERE source_report_id=? AND status='accepted'", (last_run['report_id'],)).fetchone())
        return dict(source_url=SOURCE_URL, server_time=db.iso(now), current=report_dict(current),
                    latest_report_id=max((row['id'] for row in available), default=None),
                    catalog_version=Catalog(conn).version, review=pending_review(conn),
                    last_replay=db.setting(conn, 'dbmf_last_replay'),
                    last_run=last_run, last_collected=accepted[0] if accepted else None,
                    schedule='10:00 and 22:30 America/New_York, every day', next_scheduled_run=db.iso(schedule(now)[1]),
                    worker_heartbeat=heartbeat, worker_healthy=bool(heartbeat and now - datetime.fromisoformat(heartbeat) < timedelta(minutes=10)),
                    collection_stale=not accepted or now - datetime.fromisoformat(accepted[0]) > timedelta(hours=26),
                    holdings_stale=not current or (now.astimezone(NY).date() - date.fromisoformat(current['source_date'])).days > 4,
                    last_backup=db.setting(conn, 'last_backup'), backup_error=db.setting(conn, 'backup_error'),
                    coverage=dict(inception=INCEPTION, first_report=available[0]['source_date'] if available else None,
                                  latest_report=current['source_date'] if current else None, observations=len(available),
                                  historical_reports=sum(row['source_kind'] == 'historical' for row in available),
                                  checked_at=db.setting(conn, 'dbmf_backfill_checked_at'), sec_access=db.setting(conn, 'dbmf_sec_access', []),
                                  note='Only verified reporting dates are shown. Coverage is incomplete from inception; intervening positions are unknown.'),
                    historical_errors=[dict(r) for r in conn.execute("""SELECT source_url,error,finished_at FROM dbmf_runs r
                        WHERE kind='historical' AND status='error'
                        AND NOT EXISTS(SELECT 1 FROM dbmf_replay_attempts WHERE source_report_id=r.report_id AND status='accepted')
                        AND id IN (SELECT max(id) FROM dbmf_runs WHERE kind='historical' GROUP BY source_url)""")])


@router.get('/unmapped')
def unmapped():
    with db.database() as conn:
        return pending_review(conn)


@router.get('/mappings')
def mappings():
    with db.database() as conn:
        return dict(catalog_version=Catalog(conn).version,
                    markets=[dict(row) for row in conn.execute('SELECT * FROM dbmf_markets ORDER BY sort_order')],
                    aliases=[dict(row) for row in conn.execute('SELECT * FROM dbmf_aliases ORDER BY normalized_name')],
                    changes=[dict(row) for row in conn.execute('SELECT * FROM dbmf_mapping_changes ORDER BY id DESC LIMIT 100')])


@router.get('/exposures')
def exposures(compare: Literal['previous','week','month','date']='previous', compare_date: date | None=None,
              report_id: int | None=Query(None, ge=1, le=db.MAX_ID)):
    if compare == 'date' and compare_date is None and report_id is None:
        raise HTTPException(400, 'Choose a comparison date')
    with db.database() as conn:
        if report_id is not None and not conn.execute("SELECT 1 FROM dbmf_reports WHERE id=? AND status='accepted'", (report_id,)).fetchone():
            raise HTTPException(404, 'Accepted report not found')
        return exposure_response(conn, compare, compare_date.isoformat() if compare_date else None, report_id)


@router.get('/history')
def history(start: date | None=None, end: date | None=None):
    if start and end and start > end:
        raise HTTPException(400, 'Start date is after end date')
    with db.database() as conn:
        return history_response(conn, start.isoformat() if start else None, end.isoformat() if end else None)


@router.get('/prices/{market_id}')
def prices(market_id: str, start: date | None=None, end: date | None=None):
    if start and end and start > end:
        raise HTTPException(400, 'Start date is after end date')
    with db.database() as conn:
        market = conn.execute('SELECT * FROM dbmf_markets WHERE id=?', (market_id,)).fetchone()
        if not market:
            raise HTTPException(404, 'Unknown market')
        bars = [dict(row) for row in conn.execute('SELECT date AS time,open,high,low,close,volume FROM dbmf_prices WHERE market_id=? AND date>=? AND date<=? ORDER BY date',
                (market_id, start.isoformat() if start else INCEPTION, end.isoformat() if end else '9999-12-31'))]
        return dict(market=dict(market), bars=bars, completed_sessions_only=True,
                    omitted_bars=db.setting(conn, 'dbmf_price_gaps_' + market_id, []),
                    quality_note='Rejected bars cannot form consistent candles. This does not establish which individual quote is wrong. Original provider values are shown before any currency inversion; they are not chart prices.',
                    adjustment='Unadjusted market prices; ETF distributions are not reinvested',
                    completion_rule='US equity exchange close' if market['price_kind'] == 'ETF proxy' else '18:00 New York on trade date' if market['price_kind'] == 'futures' else '01:00 UTC after provider bar date' if market['price_kind'] == 'currency' else None,
                    warning='Futures references may use different expirations from DBMF and contain roll-related price changes.' if market['price_kind'] == 'futures' else 'ETF proxy for the exposure market.' if market['price_kind'] == 'ETF proxy' else None,
                    missing_data='Missing prices are omitted. Cached prices survive provider failures.',
                    latest_price_date=bars[-1]['time'] if bars else None)


@router.get('/reports')
def report_list(limit: int=Query(200, ge=1, le=2000), before_id: int | None=Query(None, ge=1, le=db.MAX_ID)):
    with db.database() as conn:
        return [report_dict(row) for row in conn.execute('SELECT * FROM dbmf_reports WHERE id<? ORDER BY id DESC LIMIT ?', (before_id or 2**63-1, limit))]


@router.get('/reports/{report_id}')
def report(report_id: int = ApiPath(ge=1, le=db.MAX_ID)):
    with db.database() as conn:
        row = conn.execute('SELECT * FROM dbmf_reports WHERE id=?', (report_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Report not found')
        return snapshot(conn, row) if row['status'] == 'accepted' else report_dict(row)


@router.get('/reports/{report_id}/source')
def source(report_id: int = ApiPath(ge=1, le=db.MAX_ID)):
    with db.database() as conn:
        row = conn.execute('SELECT * FROM dbmf_reports WHERE id=?', (report_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Report not found')
        try:
            raw = gzip.decompress((db.DATA_DIR / row['raw_path']).read_bytes())
        except FileNotFoundError as exc:
            raise HTTPException(503, 'Archived report is unavailable') from exc
        pdf = row['source_kind'] == 'historical'
        return Response(raw, media_type='application/pdf' if pdf else 'text/plain; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="dbmf-{row["source_date"] or "rejected"}-{report_id}.{"pdf" if pdf else "html.txt"}"'})


@router.get('/export/history.csv')
def export():
    with db.database() as conn:
        return Response(csv_export(conn), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="dbmf-exposure-history.csv"'})
