import gzip
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request, Path as ApiPath
from fastapi.responses import FileResponse, PlainTextResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from . import db, __version__
from .calendar import schedule
from .history import current_positions, latest_snapshot, grouped, event_dict, aggregate, confidence
from .scores import calculate
from .strategies import payoff_for
from .payoff import calculate as calculate_payoff
from .export import pine_export, csv_export

ROOT = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    with db.database() as conn:
        db.initialize(conn)
    yield


app = FastAPI(title="Research Desk · Dan Nathan and DBMF", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None)
from .dbmf.api import router as dbmf_router
app.include_router(dbmf_router)
from .desk import router as desk_router
app.include_router(desk_router)
from .auth import router as auth_router
from .v2 import router as v2_router
app.include_router(auth_router)
app.include_router(v2_router)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path in ("/", "/dan", "/research", "/dbmf", "/docs") or request.url.path.startswith(('/static/', '/contributors/', '/funds/')):
        # Revalidate the application shell/assets across deployments. Versioned
        # links alone cannot invalidate a previously cached HTML entry point.
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def homepage():
    return FileResponse(ROOT / "static" / "research.html")


@app.get('/research')
def research_homepage():
    return FileResponse(ROOT / 'static' / 'research.html')


@app.get('/dan')
def dan_homepage():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get("/dbmf")
def dbmf_homepage():
    return FileResponse(ROOT / "static" / "dbmf.html")


@app.get('/docs', include_in_schema=False)
def api_docs():
    return FileResponse(ROOT / 'static' / 'api-docs.html')


@app.get('/redoc', include_in_schema=False)
def legacy_docs():
    return RedirectResponse('/docs')


@app.get("/health")
def health():
    with db.database() as conn:
        conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
    return {"status": "ok", "version": __version__}


@app.get("/api/status")
def status():
    now = db.utcnow()
    with db.database() as conn:
        snap = latest_snapshot(conn)
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        heartbeat = db.setting(conn, "worker_heartbeat")
        due, upcoming = schedule(now)
        positions = current_positions(conn)
        dirs = [aggregate(p) for p in grouped(positions).values()]
        last_accepted = snap["fetched_at"] if snap else None
        last_complete = conn.execute("SELECT finished_at FROM runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
        result = dict(version=__version__, server_time=db.iso(now), source_url="https://www.cnbc.com/dan-nathan/", source_as_of=snap["source_as_of"] if snap else None, last_observed=last_accepted, tracking_started=conn.execute("SELECT min(fetched_at) FROM snapshots WHERE status='accepted'").fetchone()[0], next_scheduled_run=db.iso(upcoming), schedule="22:15 America/New_York, every day", worker_heartbeat=heartbeat, worker_healthy=bool(heartbeat and now - datetime.fromisoformat(heartbeat) < timedelta(minutes=10)), stale=not last_accepted or now - datetime.fromisoformat(last_accepted) > timedelta(hours=36), source_outdated=bool(snap and now - datetime.fromisoformat(snap["source_as_of"]) > timedelta(days=7)), last_run=dict(last) if last else None, last_complete_run=last_complete[0] if last_complete else None, last_backup=db.setting(conn, "last_backup"), backup_error=db.setting(conn, "backup_error"), active_instruments=len(dirs), bullish=dirs.count("bullish"), bearish=dirs.count("bearish"), other=sum(d not in ("bullish", "bearish") for d in dirs), event_count=conn.execute("SELECT count(*) FROM events").fetchone()[0], eligible_signals=conn.execute("SELECT count(*) FROM events WHERE eligible=1").fetchone()[0], disclosure=snap["disclosure"] if snap else None)
        if result["last_run"]:
            result["last_run"]["price_errors"] = json.loads(result["last_run"]["price_errors"] or "{}")
        return result


@app.get("/api/positions")
def positions():
    with db.database() as conn:
        items = []
        snap = latest_snapshot(conn)
        for symbol, strategies in grouped(current_positions(conn)).items():
            item = dict(conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone())
            last_event = conn.execute("SELECT * FROM events WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
            item.update(direction=aggregate(strategies), confidence=confidence(strategies), strategies=strategies, source_as_of=snap["source_as_of"], last_change=event_dict(last_event), expiry_note="Expiration day and year are not assumed when the source only gives a month.")
            items.append(item)
        return sorted(items, key=lambda x: x["name"])


@app.get("/api/instruments")
def instruments():
    with db.database() as conn:
        active = grouped(current_positions(conn))
        return [dict(row, active=row["symbol"] in active) for row in conn.execute("SELECT * FROM instruments WHERE symbol IN (SELECT symbol FROM events) ORDER BY name")]


@app.get("/api/events")
def events(symbol: str | None = None, before_id: int | None = Query(None, ge=1, le=db.MAX_ID), limit: int = Query(100, ge=1, le=1000)):
    with db.database() as conn:
        where, args = [], []
        if symbol:
            where.append("e.symbol=?")
            args.append(symbol)
        if before_id:
            where.append("e.id<?")
            args.append(before_id)
        sql = "SELECT e.*,i.name,i.asset_class FROM events e JOIN instruments i USING(symbol)"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = conn.execute(sql + " ORDER BY e.id DESC LIMIT ?", args + [limit + 1]).fetchall()
        return {"items": [event_dict(r) for r in rows[:limit]], "has_more": len(rows) > limit}


@app.get("/api/timeline/{symbol}")
def timeline(symbol: str):
    with db.database() as conn:
        return [event_dict(row) for row in conn.execute("SELECT e.*,i.name FROM events e JOIN instruments i USING(symbol) WHERE e.symbol=? ORDER BY e.observed_at,e.id", (symbol,))]


@app.get("/api/prices/{symbol}")
def prices(symbol: str):
    with db.database() as conn:
        item = conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()
        if not item:
            raise HTTPException(404, "Unknown instrument")
        bars = [dict(row) for row in conn.execute("SELECT date AS time,open,high,low,close,volume FROM prices WHERE symbol=? AND complete=1 ORDER BY date", (symbol,))]
        return dict(symbol=symbol, name=item["name"], bars=bars, error=item["price_error"], fetched_at=item["price_checked_at"], provider="Yahoo Finance", adjustment="Split and dividend adjusted", completed_sessions_only=True)


@app.get("/api/scorecard")
def scorecard(symbol: str | None = None):
    with db.database() as conn:
        return calculate(conn, symbol)


@app.get("/api/export/pine/{symbol}", response_class=PlainTextResponse)
def export_pine(symbol: str):
    with db.database() as conn:
        try:
            return pine_export(conn, symbol)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/export/history.csv")
def export_history():
    with db.database() as conn:
        return Response(csv_export(conn), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="dan-nathan-disclosure-history.csv"'})


@app.get("/api/snapshots/{snapshot_id}")
def snapshot(snapshot_id: int = ApiPath(ge=1, le=db.MAX_ID)):
    with db.database() as conn:
        row = conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Snapshot not found")
        result = dict(row)
        result["positions"] = json.loads(result.pop("positions_json") or "[]")
        return result


@app.get("/api/snapshots/{snapshot_id}/source", response_class=PlainTextResponse)
def raw_source(snapshot_id: int = ApiPath(ge=1, le=db.MAX_ID)):
    with db.database() as conn:
        row = conn.execute("SELECT raw_path FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Snapshot not found")
        return PlainTextResponse(gzip.decompress((db.DATA_DIR / row[0]).read_bytes()).decode(), headers={"Content-Disposition": f'attachment; filename="cnbc-snapshot-{snapshot_id}.html.txt"'})


@app.get("/downloads/dan-nathan.pine")
def pine_script():
    return FileResponse(ROOT / "pine" / "dan-nathan.pine", media_type="text/plain", filename="dan-nathan.pine")


@app.get("/downloads/guide")
def guide():
    return FileResponse(ROOT.parent / "README.md", media_type="text/plain", filename="Dan-Nathan-Tracker-Guide.md")


@app.get("/api/analysis/{symbol}")
def strategy_analysis(symbol: str):
    with db.database() as conn:
        instrument = conn.execute("SELECT name FROM instruments WHERE symbol=?", (symbol,)).fetchone()
        if not instrument:
            raise HTTPException(404, "Unknown instrument")
        positions = grouped(current_positions(conn)).get(symbol, [])
        return dict(name=instrument["name"], symbol=symbol, strategies=[
            dict(signature=p["key"], wording=p["raw_text"], side=p["side"],
                 analysis=p["analysis"], payoff=payoff_for(p["analysis"])) for p in positions])


@app.post("/api/payoff")
async def hypothetical_payoff(request: Request):
    # Pure calculation: this endpoint never creates disclosures, reviews or scores.
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 32000:
            raise HTTPException(413, "Payoff input is too large")
    try:
        return calculate_payoff(json.loads(payload))
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise HTTPException(400, str(exc)) from exc


from .dashboard_api import router as dashboard_router

app.include_router(dashboard_router)


@app.get("/contributors/{contributor_id}")
def contributor_page(contributor_id: str):
    from .contributor_desk import profile

    with db.database() as conn:
        profile(conn, contributor_id)
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/funds/{fund_id}")
def fund_page(fund_id: str):
    from .fund_desk import profile

    with db.database() as conn:
        profile(conn, fund_id)
    return FileResponse(ROOT / "static" / "dbmf.html")


@app.get("/downloads/research-desk.pine", response_class=PlainTextResponse)
def shared_pine_script():
    text = (
        (ROOT / "pine" / "dan-nathan.pine")
        .read_text()
        .replace("Dan Nathan ·", "Research Desk ·")
    )
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": 'attachment; filename="research-desk.pine"'},
    )
