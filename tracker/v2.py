"""Expanded research API. Legacy endpoints continue to read legacy evidence."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
import gzip
from .calls import CallDocument, Transition, revise, get_call
from .research import sync_legacy, positions, review_position
from . import db
from .auth import require_owner

router = APIRouter(prefix="/api/v2", tags=["Research Desk"])


def history_rows(conn, response, table, where, params, scope, cursor, limit):
    """Stable descending IDs, scoped opaque cursors, and explicit restore resets."""
    import base64

    epoch = db.setting(conn, "activity_epoch")
    before = None
    if cursor:
        try:
            if len(cursor) > 1000:
                raise ValueError()
            saved_epoch, saved_scope, before = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )
            if saved_scope != scope or not isinstance(before, (int, str)):
                raise ValueError()
            if saved_epoch != epoch:
                before = None
                response.headers["X-Cursor-Reset"] = "true"
        except Exception as exc:
            raise HTTPException(422, "Invalid cursor for this list") from exc
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE ({where}) AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
        (*params, before, before, limit + 1),
    ).fetchall()
    if len(rows) > limit:
        response.headers["X-Next-Cursor"] = (
            base64.urlsafe_b64encode(
                json.dumps(
                    [epoch, scope, rows[limit - 1]["id"]], separators=(",", ":")
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
    return rows[:limit]


@router.get("/contributors")
def contributors():
    with db.database() as conn:
        return [
            dict(r, has_data=bool(r["has_data"]))
            for r in conn.execute(
                """SELECT c.*,s.coverage,s.reason,s.url AS source_url,
                (EXISTS(SELECT 1 FROM disclosures d WHERE d.contributor_id=c.id AND d.status='accepted')
                 OR EXISTS(SELECT 1 FROM calls call JOIN call_revisions r ON r.id=call.current_revision
                           WHERE call.contributor_id=c.id AND r.status='approved')) AS has_data
                FROM contributors c JOIN sources s ON s.contributor_id=c.id ORDER BY c.name"""
            )
        ]


@router.post("/calls", status_code=201)
def create_call(payload: CallDocument, owner=Depends(require_owner)):
    with db.database() as conn:
        return revise(conn, payload)


@router.get("/calls")
def calls(
    response: Response,
    contributor_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(200, ge=1, le=200),
):
    with db.database() as conn:
        rows = history_rows(
            conn,
            response,
            "calls",
            "? IS NULL OR contributor_id=?",
            (contributor_id, contributor_id),
            "calls:" + str(contributor_id),
            cursor,
            limit,
        )
        return [get_call(conn, r["id"]) for r in rows]


@router.get("/calls/{call_id}/revisions")
def call_revisions(call_id: int):
    with db.database() as conn:
        if not conn.execute("SELECT 1 FROM calls WHERE id=?", (call_id,)).fetchone():
            raise HTTPException(404, "Call not found")
        return [
            dict(r, document=json.loads(r["document_json"]))
            for r in conn.execute(
                "SELECT * FROM call_revisions WHERE call_id=? ORDER BY revision DESC",
                (call_id,),
            )
        ]


@router.get("/calls/{call_id}")
def call_detail(call_id: int):
    with db.database() as conn:
        return get_call(conn, call_id)


@router.put("/calls/{call_id}")
def edit_call(call_id: int, payload: CallDocument, owner=Depends(require_owner)):
    with db.database() as conn:
        return revise(conn, payload, call_id)


@router.post("/calls/{call_id}/{action}")
def transition_call(
    call_id: int, action: str, payload: Transition, owner=Depends(require_owner)
):
    if action not in ("approve", "retract"):
        raise HTTPException(404, "Unknown action")
    with db.database() as conn:
        return revise(
            conn,
            call_id=call_id,
            transition=payload,
            status="approved" if action == "approve" else "retracted",
        )


@router.get("/contributors/{contributor_id}/positions")
def contributor_positions(contributor_id: str):
    with db.database() as conn:
        if not conn.execute(
            "SELECT 1 FROM contributors WHERE id=?", (contributor_id,)
        ).fetchone():
            raise HTTPException(404, "Contributor not found")
        return positions(conn, contributor_id)


@router.get("/contributors/{contributor_id}/events")
def contributor_events(
    contributor_id: str,
    response: Response,
    evidence_type: str = "disclosure",
    cursor: str | None = None,
    limit: int = Query(200, ge=1, le=200),
):
    with db.database() as conn:
        return [
            dict(r)
            for r in history_rows(
                conn,
                response,
                "research_events",
                "contributor_id=? AND evidence_type=?",
                (contributor_id, evidence_type),
                "events:" + contributor_id + ":" + evidence_type,
                cursor,
                limit,
            )
        ]


@router.get("/disclosures/{disclosure_id}/source")
def disclosure_source(disclosure_id: int):
    with db.database() as conn:
        row = conn.execute(
            "SELECT raw_path FROM disclosures WHERE id=?", (disclosure_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Disclosure not found")
    path = (db.DATA_DIR / row[0]).resolve()
    if not path.is_relative_to(db.DATA_DIR.resolve()):
        raise HTTPException(400, "Invalid source path")
    try:
        data = gzip.decompress(path.read_bytes())
    except OSError:
        raise HTTPException(404, "Source archive unavailable")
    # Never render untrusted provider HTML in the app origin.
    return Response(
        data,
        media_type="text/plain",
        headers={"Content-Disposition": 'inline; filename="disclosure.txt"'},
    )


import json
from datetime import datetime, timedelta
from typing import Literal
from pydantic import BaseModel, Field
from .auth import audit
from . import funds as fund_store
from .fund_scope import TRACKED_FUNDS, FUND_IDS_SQL, SOURCE_SCOPE_SQL, ACTIVITY_SCOPE_SQL
from .briefing import activity_page, alerts
from .analytics import enqueue, freeze_inputs


@router.get("/funds")
def funds():
    with db.database() as conn:
        result = []
        for r in conn.execute(
            f"SELECT f.*,s.coverage,s.reason,s.url,s.id AS source_id FROM funds f JOIN sources s ON s.fund_id=f.id WHERE f.id IN ({FUND_IDS_SQL}) ORDER BY f.id"
        ):
            latest = conn.execute(
                "SELECT id,source_date,acquired_at FROM fund_reports WHERE fund_id=? AND status='accepted' ORDER BY source_date DESC,revision DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            result.append(
                dict(
                    r,
                    latest=dict(latest) if latest else None,
                    qualified=fund_store.qualify(conn, r["source_id"]),
                )
            )
        return result


@router.get("/funds/{fund_id}/reports")
def fund_reports(
    fund_id: str,
    response: Response,
    cursor: str | None = None,
    limit: int = Query(200, ge=1, le=200),
):
    with db.database() as conn:
        return [
            dict(r)
            for r in history_rows(
                conn,
                response,
                "fund_reports",
                "fund_id=?",
                (fund_id,),
                "reports:" + fund_id,
                cursor,
                limit,
            )
        ]


@router.get("/funds/{fund_id}/exposures")
def fund_exposures(fund_id: str, report_id: str | None = None):
    with db.database() as conn:
        report = conn.execute(
            "SELECT * FROM fund_reports WHERE fund_id=? AND (? IS NULL OR id=?) AND status='accepted' ORDER BY source_date DESC,revision DESC LIMIT 1",
            (fund_id, report_id, report_id),
        ).fetchone()
        holdings = (
            [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM fund_holdings WHERE report_id=? ORDER BY measure,name",
                    (report["id"],),
                )
            ]
            if report
            else []
        )
        weights = sorted(
            (
                float(h["value"])
                for h in holdings
                if h["measure"] == "equity_weight_pct"
            ),
            reverse=True,
        )
        source = conn.execute(
            "SELECT coverage FROM sources WHERE fund_id=?", (fund_id,)
        ).fetchone()
        return dict(
            report=dict(report) if report else None,
            holdings=holdings,
            coverage=source[0] if source else "unavailable",
            changes=fund_store.report_changes(conn, report["id"]) if report else [],
            equity_summary={
                "holdings": len(weights),
                "top_five_weight_pct": sum(weights[:5]),
                "sector_coverage": "Unavailable: verified dated classifications are required",
            }
            if weights
            else None,
        )


@router.get("/fund-comparisons")
def fund_comparisons(
    funds: str = "DBMF,KMLM,CTA,WTMF",
    measure: str = "notional_pct_nav",
    mode: Literal["latest", "aligned"] = "latest",
):
    with db.database() as conn:
        try:
            return fund_store.comparison(
                conn,
                [f for f in dict.fromkeys(funds.split(",")) if f in TRACKED_FUNDS],
                measure,
                mode == "aligned",
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))


@router.get("/sources/status")
def sources_status():
    with db.database() as conn:
        result = []
        for row in conn.execute("SELECT * FROM sources WHERE " + SOURCE_SCOPE_SQL + " ORDER BY id"):
            last = conn.execute(
                "SELECT * FROM source_runs WHERE source_id=? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            success = conn.execute(
                "SELECT finished_at FROM source_runs WHERE source_id=? AND status='success' ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            result.append(
                dict(
                    row,
                    last_run=dict(last) if last else None,
                    last_success=success[0] if success else None,
                    stale=not success
                    or db.utcnow() - datetime.fromisoformat(success[0])
                    > timedelta(hours=36 if row["contributor_id"] else 26),
                )
            )
        return result


class SourceSetting(BaseModel):
    enabled: bool


@router.put("/sources/{source_id}")
def configure_source(
    source_id: str, payload: SourceSetting, owner=Depends(require_owner)
):
    with db.database() as conn, conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
        if payload.enabled and row["fund_id"] and row["fund_id"] not in TRACKED_FUNDS:
            raise HTTPException(422, "Fund is outside active coverage")
        if payload.enabled and not row["adapter"]:
            raise HTTPException(422, "Source automation is unavailable")
        conn.execute(
            "UPDATE sources SET enabled=? WHERE id=?", (int(payload.enabled), source_id)
        )
        audit(conn, "configure", "source", source_id, payload.model_dump())
    return {"enabled": payload.enabled}


class Review(BaseModel):
    symbol: str = Field(max_length=20)
    direction: str = Field(max_length=30)
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/contributors/{contributor_id}/reviews")
def review(contributor_id: str, payload: Review, owner=Depends(require_owner)):
    with db.database() as conn:
        try:
            review_position(
                conn, contributor_id, payload.symbol, payload.direction, payload.reason
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    return {"status": "reviewed"}


@router.get("/assets")
def asset_search(
    q: str = Query("", max_length=100), limit: int = Query(30, ge=1, le=100)
):
    with db.database() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM assets WHERE instr(lower(symbol),lower(?)) OR instr(lower(name),lower(?)) ORDER BY verified DESC,symbol LIMIT ?",
                (q, q, limit),
            )
        ]


@router.get("/assets/{asset_id}")
def asset(asset_id: str):
    with db.database() as conn:
        from .research import canonical_id, related_ids

        asset_id = canonical_id(conn, asset_id)
        linked = related_ids(conn, asset_id)
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Asset not found")
        result = dict(row, related_ids=linked)
        result["events"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM research_events WHERE asset_id IN (SELECT value FROM json_each(?)) ORDER BY available_at DESC,id DESC LIMIT 100",
                (json.dumps(linked),),
            )
        ]
        result["holdings"] = [
            dict(r)
            for r in conn.execute(
                f"""SELECT h.*,f.fund_id,f.source_date FROM fund_holdings h JOIN fund_reports f ON f.id=h.report_id
          WHERE f.fund_id IN ({FUND_IDS_SQL}) AND h.asset_id IN (SELECT value FROM json_each(?)) AND f.id=(SELECT r.id FROM fund_reports r WHERE r.fund_id=f.fund_id AND r.status='accepted' ORDER BY source_date DESC,revision DESC LIMIT 1)""",
                (json.dumps(linked),),
            )
        ]
        result["prices"] = (
            [
                dict(r)
                for r in conn.execute(
                    "SELECT date,open,high,low,close,fetched_at FROM prices WHERE symbol=? AND complete=1 ORDER BY date",
                    (asset_id.removeprefix("legacy:"),),
                )
            ]
            if asset_id.startswith("legacy:")
            else []
        )
        result["agreement"] = {}
        for kind in ("disclosure", "call"):
            votes = []
            for c in conn.execute("SELECT id FROM contributors"):
                if kind == "disclosure":
                    snap = conn.execute(
                        "SELECT * FROM disclosures WHERE contributor_id=? AND status='accepted' ORDER BY id DESC LIMIT 1",
                        (c[0],),
                    ).fetchone()
                    if (
                        not snap
                        or db.utcnow() - datetime.fromisoformat(snap["acquired_at"])
                        > timedelta(hours=36)
                        or db.utcnow() - datetime.fromisoformat(snap["source_as_of"])
                        > timedelta(days=7)
                    ):
                        continue
                    from .parser import aggregate

                    direction = aggregate(
                        [
                            p
                            for p in positions(conn, c[0])
                            if "legacy:" + p["symbol"] in linked
                        ]
                    )
                else:
                    e = conn.execute(
                        """SELECT e.* FROM research_events e JOIN calls c ON c.current_revision=e.call_revision_id
                      WHERE e.contributor_id=? AND e.asset_id IN (SELECT value FROM json_each(?)) AND e.evidence_type='call' ORDER BY e.available_at DESC,e.id DESC LIMIT 1""",
                        (c[0], json.dumps(linked)),
                    ).fetchone()
                    if (
                        not e
                        or not e["eligible"]
                        or db.utcnow() - datetime.fromisoformat(e["available_at"])
                        > timedelta(days=30)
                    ):
                        continue
                    direction = e["direction"]
                if direction in ("bullish", "bearish"):
                    votes.append({"contributor_id": c[0], "direction": direction})
            result["agreement"][kind] = {
                "votes": votes,
                "denominator": len(votes),
                "tracked_contributors": conn.execute(
                    "SELECT count(*) FROM contributors"
                ).fetchone()[0],
                "bullish": sum(v["direction"] == "bullish" for v in votes),
                "bearish": sum(v["direction"] == "bearish" for v in votes),
            }
        return result


@router.get("/assets/{asset_id}/timeline")
def asset_timeline(asset_id: str, response: Response, cursor: str | None = None):
    from .research import canonical_id, related_ids

    with db.database() as conn:
        asset_id = canonical_id(conn, asset_id)
        rows = history_rows(
            conn,
            response,
            "activity",
            ACTIVITY_SCOPE_SQL + " AND asset_id IN (SELECT value FROM json_each(?))",
            (json.dumps(related_ids(conn, asset_id)),),
            "asset:" + asset_id,
            cursor,
            100,
        )
        next_cursor = response.headers.get("X-Next-Cursor")
        return {
            "items": [dict(row) for row in rows],
            "cursor": next_cursor,
            "has_more": bool(next_cursor),
            "cursor_reset": response.headers.get("X-Cursor-Reset") == "true",
        }


class Benchmark(BaseModel):
    benchmark_id: str | None = Field(None, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


@router.put("/assets/{asset_id}/benchmark")
def benchmark(asset_id: str, payload: Benchmark, owner=Depends(require_owner)):
    with db.database() as conn, conn:
        if (
            payload.benchmark_id
            and not conn.execute(
                "SELECT 1 FROM assets WHERE id=? AND verified=1",
                (payload.benchmark_id,),
            ).fetchone()
        ):
            raise HTTPException(422, "Benchmark must be a verified asset")
        if not conn.execute(
            "UPDATE assets SET benchmark_id=? WHERE id=?",
            (payload.benchmark_id, asset_id),
        ).rowcount:
            raise HTTPException(404, "Asset not found")
        db.set_setting(conn, "default_benchmark:" + asset_id, True)
        audit(conn, "benchmark", "asset", asset_id, payload.model_dump())
    return payload.model_dump()


class ModelConfig(BaseModel):
    capital: float = Field(100000, gt=0, le=1e10, allow_inf_nan=False)
    weight: float = Field(0.1, gt=0, le=1, allow_inf_nan=False)
    max_positions: int = Field(10, ge=1, le=10)
    max_gross: float = Field(1, gt=0, le=1, allow_inf_nan=False)
    cost_bps: float = Field(5, ge=0, le=1000, allow_inf_nan=False)
    borrow_rate: float = Field(0.05, ge=0, le=5, allow_inf_nan=False)
    horizon: Literal[1, 5, 20, 60] = 20
    exit: Literal["fixed", "signal"] = "fixed"


class AnalysisRequest(BaseModel):
    contributor_id: str = Field(max_length=80)
    evidence_type: Literal["disclosure", "call"]
    config: ModelConfig = Field(default_factory=ModelConfig)
    mode: Literal["historical", "prospective"] = "historical"
    previous_id: int | None = Field(None, ge=1)


@router.post("/simulations", status_code=202)
def simulation(payload: AnalysisRequest, owner=Depends(require_owner)):
    with db.database() as conn:
        if not conn.execute(
            "SELECT 1 FROM contributors WHERE id=?", (payload.contributor_id,)
        ).fetchone():
            raise HTTPException(422, "Unknown contributor")
        start = db.iso() if payload.mode == "prospective" else None
        if (
            payload.previous_id
            and not conn.execute(
                "SELECT 1 FROM simulation_definitions WHERE id=? AND contributor_id=? AND evidence_type=?",
                (payload.previous_id, payload.contributor_id, payload.evidence_type),
            ).fetchone()
        ):
            raise HTTPException(422, "Prior definition does not match this cohort")
        with conn:
            definition = conn.execute(
                "INSERT INTO simulation_definitions(created_at,contributor_id,evidence_type,mode,forward_start,previous_id,config_json) VALUES(?,?,?,?,?,?,?)",
                (
                    db.iso(),
                    payload.contributor_id,
                    payload.evidence_type,
                    payload.mode,
                    start,
                    payload.previous_id,
                    payload.config.model_dump_json(),
                ),
            ).lastrowid
            audit(conn, "create", "simulation", definition, payload.model_dump())
        inputs = freeze_inputs(
            conn,
            payload.contributor_id,
            payload.evidence_type,
            payload.config.model_dump(),
            kind="simulation",
            forward_start=start,
        )
        inputs["definition_id"] = definition
        job = enqueue(conn, "simulation", inputs)
    return {
        "job_id": job,
        "definition_id": definition,
        "mode": payload.mode,
        "forward_start": start,
    }


@router.get("/simulations")
def simulations():
    with db.database() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT d.*,(SELECT r.id FROM analysis_runs r WHERE r.kind='simulation' AND json_extract(r.inputs_json,'$.definition_id')=d.id ORDER BY r.created_at DESC LIMIT 1) AS latest_run FROM simulation_definitions d ORDER BY d.id DESC LIMIT 100"
            )
        ]


@router.post("/scorecards", status_code=202)
def scorecard_job(payload: AnalysisRequest, owner=Depends(require_owner)):
    with db.database() as conn:
        if not conn.execute(
            "SELECT 1 FROM contributors WHERE id=?", (payload.contributor_id,)
        ).fetchone():
            raise HTTPException(422, "Unknown contributor")
        job = enqueue(
            conn,
            "scorecard",
            freeze_inputs(
                conn,
                payload.contributor_id,
                payload.evidence_type,
                payload.config.model_dump(),
            ),
        )
        with conn:
            audit(conn, "run", "scorecard", job, payload.model_dump())
    return {"job_id": job}


@router.get("/scorecards")
def scorecards():
    with db.database() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id,kind,status,created_at,finished_at,error FROM analysis_runs WHERE kind='scorecard' ORDER BY created_at DESC LIMIT 100"
            )
        ]


@router.get("/simulation-runs/{job_id}")
def analysis_run(job_id: str):
    with db.database() as conn:
        row = conn.execute(
            "SELECT * FROM analysis_runs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Run not found")
        result = dict(row)
        result["inputs"] = json.loads(result.pop("inputs_json"))
        result["result"] = json.loads(result.pop("result_json") or "null")
        return result


@router.get("/activity")
def recent_activity(
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    current: bool = False,
    source_id: str | None = None,
):
    with db.database() as conn:
        try:
            result = activity_page(
                conn, cursor, limit, current=current, source_id=source_id
            )
            result["alerts"] = alerts(conn, result["items"])
            return result
        except ValueError as exc:
            raise HTTPException(422, str(exc))


@router.get("/briefings")
def briefings():
    with db.database() as conn:
        row = conn.execute(
            "SELECT * FROM briefings ORDER BY cutoff DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {
                "briefing": None,
                "items": [],
                "live": [],
                "status": "awaiting_worker",
            }
        ids = json.loads(row["activity_ids_json"])
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM activity WHERE " + ACTIVITY_SCOPE_SQL + " AND id IN (SELECT value FROM json_each(?)) ORDER BY id DESC",
                (json.dumps(ids),),
            )
        ]
        live = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM activity WHERE " + ACTIVITY_SCOPE_SQL + " AND recorded_at>? ORDER BY id DESC LIMIT 200",
                (row["cutoff"],),
            )
        ]
        return {
            "briefing": dict(row),
            "items": items,
            "live": live,
            "status": "available",
        }


class AlertRule(BaseModel):
    source_id: str | None = Field(None, max_length=80)
    asset_id: str | None = Field(None, max_length=160)
    enabled: bool = True
    contributor_changes: bool = True
    source_health: bool = True
    futures_pp: float = Field(5, ge=0, le=1000, allow_inf_nan=False)
    equity_pp: float = Field(1, ge=0, le=100, allow_inf_nan=False)
    flip_min: float = Field(1, ge=0, le=100, allow_inf_nan=False)


@router.get("/alert-rules")
def alert_rules():
    with db.database() as conn:
        return [
            dict(r, config=json.loads(r["config_json"]))
            for r in conn.execute("SELECT * FROM alert_rules ORDER BY id")
        ]


@router.put("/alert-rules/{rule_id}")
def save_alert_rule(rule_id: int, payload: AlertRule, owner=Depends(require_owner)):
    with db.database() as conn, conn:
        if (
            payload.source_id
            and not conn.execute(
                "SELECT 1 FROM sources WHERE id=?", (payload.source_id,)
            ).fetchone()
        ):
            raise HTTPException(422, "Unknown source")
        if (
            payload.asset_id
            and not conn.execute(
                "SELECT 1 FROM assets WHERE id=?", (payload.asset_id,)
            ).fetchone()
        ):
            raise HTTPException(422, "Unknown asset")
        conn.execute(
            "INSERT INTO alert_rules(id,source_id,asset_id,enabled,config_json) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,asset_id=excluded.asset_id,enabled=excluded.enabled,config_json=excluded.config_json",
            (
                rule_id,
                payload.source_id,
                payload.asset_id,
                int(payload.enabled),
                payload.model_dump_json(exclude={"source_id", "asset_id", "enabled"}),
            ),
        )
        audit(conn, "configure", "alert_rule", rule_id, payload.model_dump())
    return {"id": rule_id}


@router.get("/funds/{fund_id}/changes")
def fund_changes(fund_id: str):
    with db.database() as conn:
        row = conn.execute(
            "SELECT id FROM fund_reports WHERE fund_id=? AND status='accepted' ORDER BY source_date DESC,revision DESC LIMIT 1",
            (fund_id,),
        ).fetchone()
        return fund_store.report_changes(conn, row[0]) if row else []


@router.get("/fund-reports/{report_id}/source")
def fund_source(report_id: str):
    with db.database() as conn:
        row = conn.execute(
            "SELECT raw_path,content_hash FROM fund_reports WHERE id=?", (report_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Fund report not found")
    path = (db.DATA_DIR / row["raw_path"]).resolve()
    if not path.is_relative_to(db.DATA_DIR.resolve()):
        raise HTTPException(400, "Invalid source path")
    try:
        raw = gzip.decompress(path.read_bytes())
    except OSError:
        raise HTTPException(404, "Source archive unavailable")
    import hashlib

    if hashlib.sha256(raw).hexdigest() != row["content_hash"]:
        raise HTTPException(409, "Source archive hash mismatch")
    is_workbook = raw.startswith(b"PK")
    return Response(
        raw,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if is_workbook
        else "text/plain",
        headers={
            "Content-Disposition": 'attachment; filename="holdings.xlsx"'
            if is_workbook
            else 'inline; filename="holdings.txt"'
        },
    )


class AssetMapping(BaseModel):
    canonical_id: str = Field(max_length=160)
    source_url: str = Field(max_length=2048)
    reason: str = Field(min_length=1, max_length=2000)


@router.put("/assets/{asset_id}/mapping")
def map_asset(asset_id: str, payload: AssetMapping, owner=Depends(require_owner)):
    from .calls import CallDocument

    try:
        CallDocument.valid_link(payload.source_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with db.database() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if asset_id == payload.canonical_id:
            raise HTTPException(422, "Choose two distinct identities")
        for identifier in (asset_id, payload.canonical_id):
            if not conn.execute(
                "SELECT 1 FROM assets WHERE id=?", (identifier,)
            ).fetchone():
                raise HTTPException(422, "Unknown asset identity")
        if not conn.execute(
            "SELECT 1 FROM assets WHERE id=? AND verified=1", (payload.canonical_id,)
        ).fetchone():
            raise HTTPException(422, "Canonical identity must be verified")
        if conn.execute(
            "SELECT 1 FROM asset_links WHERE alias_id=? OR canonical_id=?",
            (payload.canonical_id, asset_id),
        ).fetchone():
            raise HTTPException(422, "Chained or cyclic mappings are not allowed")
        conn.execute(
            "INSERT INTO asset_links VALUES(?,?,?,?,?) ON CONFLICT(alias_id) DO UPDATE SET canonical_id=excluded.canonical_id,source_url=excluded.source_url,reason=excluded.reason,created_at=excluded.created_at",
            (
                asset_id,
                payload.canonical_id,
                payload.source_url,
                payload.reason,
                db.iso(),
            ),
        )
        audit(conn, "map", "asset", asset_id, payload.model_dump())
    return payload.model_dump()
