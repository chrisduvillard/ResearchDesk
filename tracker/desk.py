"""Shared, read-only activity and data-health views. No notification delivery here."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query

from . import db
from .calendar import NY, completed, schedule
from .history import current_positions, event_dict, latest_snapshot
from .dbmf.collector import schedule as dbmf_schedule
from .dbmf.recovery import pending_review
from .dbmf.store import reports

router = APIRouter(prefix='/api', tags=['Desk'])


@router.get('/changes')
def disclosure_changes(after_id: int | None = Query(None, ge=0, le=db.MAX_ID)):
    with db.database() as conn:
        snapshots = conn.execute("SELECT * FROM snapshots WHERE status='accepted' ORDER BY id DESC LIMIT 2").fetchall()
        current = snapshots[0] if snapshots else None
        previous = snapshots[1] if len(snapshots) > 1 else None
        latest_event = conn.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
        cursor_reset = after_id is not None and after_id > latest_event
        if cursor_reset:
            after_id = None  # A restored older database invalidates a browser cursor.
        # IDs include audited interpretation corrections as well as source events.
        if after_id is not None:
            where, args = 'e.id>?', [after_id]
        elif previous:
            where, args = 'e.snapshot_id>? OR e.observed_at>?', [previous['id'], previous['fetched_at']]
        else:
            where, args = '1=1', []
        rows = conn.execute(f'''SELECT e.*,i.name FROM events e JOIN instruments i USING(symbol)
            WHERE {where} ORDER BY e.id DESC LIMIT 201''', args).fetchall()
        total = conn.execute(f'SELECT count(*) FROM events e WHERE {where}', args).fetchone()[0]
        point = lambda s: dict(id=s['id'], source_as_of=s['source_as_of'], fetched_at=s['fetched_at']) if s else None
        return dict(current=point(current), previous=point(previous), items=[event_dict(r) for r in rows[:200]],
                    total=total, has_more=total > 200, cursor=dict(after_id=latest_event),
                    mode='visit' if after_id is not None else 'previous', cursor_reset=cursor_reset)


def _age(stamp, now):
    return now - datetime.fromisoformat(stamp) if stamp else timedelta.max


def _last_equity_day(now):
    for days in range(15):
        day = (now.astimezone(NY).date() - timedelta(days=days)).isoformat()
        if completed(day, now):
            return day
    return None


def health_view(conn, now):
    issues, desks = [], []

    def issue(key, title, detail, link, notify=True):
        issues.append(dict(key=key, title=title, detail=detail, link=link, notify=notify))

    snap = latest_snapshot(conn)
    available = reports(conn)
    report = available[-1] if available else None
    active_symbols = {p['symbol'] for p in current_positions(conn)}
    for key, name, source, source_date, collected, scheduler, stale_hours, old_source in (
        ('dan', 'Dan Nathan', snap, snap['source_as_of'] if snap else None,
         snap['fetched_at'] if snap else None, schedule, 36,
         bool(snap and _age(snap['source_as_of'], now) > timedelta(days=7))),
        ('dbmf', 'DBMF', report, report['source_date'] if report else None,
         None, dbmf_schedule, 26,
         bool(report and (now.astimezone(NY).date() - date.fromisoformat(report['source_date'])).days > 4)),
    ):
        link = '/dbmf' if key == 'dbmf' else '/'
        if key == 'dbmf':
            runs = conn.execute('''SELECT r.*,p.status AS report_status,
                EXISTS(SELECT 1 FROM dbmf_replay_attempts a WHERE a.source_report_id=r.report_id AND a.status='accepted') AS recovered
                FROM dbmf_runs r LEFT JOIN dbmf_reports p ON p.id=r.report_id
                WHERE kind='live' ORDER BY r.id DESC LIMIT 200''').fetchall()
            accepted = conn.execute('''SELECT r.finished_at FROM dbmf_runs r JOIN dbmf_reports p ON p.id=r.report_id
                WHERE kind='live' AND r.finished_at IS NOT NULL AND (p.status='accepted' OR EXISTS
                (SELECT 1 FROM dbmf_replay_attempts a WHERE a.source_report_id=p.id AND a.status='accepted'))
                ORDER BY r.id DESC LIMIT 1''').fetchone()
            collected = accepted[0] if accepted else None
            prices = [dict(r) for r in conn.execute('''SELECT m.id,m.name,m.price_kind,m.price_checked_at,m.price_error,
                max(p.date) AS latest_date FROM dbmf_markets m LEFT JOIN dbmf_prices p ON p.market_id=m.id
                WHERE m.provider_symbol IS NOT NULL GROUP BY m.id ORDER BY m.sort_order''')]
        else:
            runs = conn.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 200').fetchall()
            prices = [dict(r, price_kind='ETF proxy') for r in conn.execute('''SELECT i.symbol AS id,i.name,
                i.price_checked_at,i.price_error,max(p.date) AS latest_date FROM instruments i
                LEFT JOIN prices p ON p.symbol=i.symbol AND p.complete=1 GROUP BY i.symbol ORDER BY i.name''') if r['id'] in active_symbols]
        heartbeat = db.setting(conn, 'dbmf_worker_heartbeat' if key == 'dbmf' else 'worker_heartbeat')
        healthy = _age(heartbeat, now) < timedelta(minutes=10)
        stale = _age(collected, now) > timedelta(hours=stale_hours)
        running = bool(runs and runs[0]['status'] == 'running')
        failures = 0
        for run in runs:
            if run['status'] == 'running':
                continue  # Do not announce recovery while a retry is still running.
            recovered = key == 'dbmf' and run['recovered'] and run['price_errors'] in ('{}', None)
            if run['status'] == 'success' or recovered:
                break
            failures += 1
        if failures:
            issue(key + ':collection', name + ' collection needs attention',
                  f'{failures} consecutive incomplete or failed checks. Last valid data is retained.', link, failures >= 3)
        if stale:
            issue(key + ':stale', name + ' collection is overdue',
                  f'No successful source check in the last {stale_hours} hours.' if collected else 'No successful source check has been recorded.', link)
        if old_source:
            issue(key + ':source', name + ' source date is old',
                  'The published source date is more than ' + ('four days' if key == 'dbmf' else 'seven days') + ' old. A successful download does not make the source newer.', link)
        if not healthy:
            issue(key + ':worker', name + ' collector has stopped checking in', 'No worker heartbeat in the last ten minutes.', link)
        # Allow the scheduled run and retries to finish before expecting a new
        # equity bar. Holidays and early closes use the actual exchange calendar.
        due = scheduler(now - timedelta(minutes=45))[0]
        expected_equity = _last_equity_day(due)
        for price in prices:
            latest = price['latest_date']
            price['expected_date'] = expected_equity if price['price_kind'] == 'ETF proxy' else None
            price['stale'] = not latest or (latest < expected_equity if price['expected_date'] else
                                           (now.astimezone(NY).date() - date.fromisoformat(latest)).days > 4)
        bad_prices = [p for p in prices if p['stale'] or p['price_error']]
        if bad_prices:
            issue(key + ':prices', name + ' prices need attention',
                  ', '.join(p['name'] for p in bad_prices) + '. Cached prices remain available.', link,
                  any(p['stale'] for p in bad_prices) or failures >= 3)
        desks.append(dict(id=key, name=name, source_date=source_date, last_collected=collected,
                          next_check=db.iso(scheduler(now)[1]), worker_healthy=healthy, running=running,
                          consecutive_failures=failures, collection_stale=stale, source_stale=old_source,
                          prices=prices, link=link))
    review = pending_review(conn)
    if review['items']:
        issue('dbmf:review', 'DBMF has unfamiliar instruments',
              f"{review['unmapped_names']} unfamiliar names in {review['pending_reports']} saved reports need verification.", '/dbmf#mapping-review')
    if db.setting(conn, 'backup_error'):
        issue('backup', 'The daily backup needs attention', str(db.setting(conn, 'backup_error')), '/')
    return dict(server_time=db.iso(now), desks=desks, issues=issues,
                price_rule='Equities: latest completed exchange session due at the scheduled check, with 45 minutes for retries. Futures and currencies: warn after four calendar days without a bar.',
                last_backup=db.setting(conn, 'last_backup'))


@router.get('/desk/health')
def health():
    with db.database() as conn:
        return health_view(conn, db.utcnow())
