"""Read-only compatibility projections for shared research dashboards."""

from datetime import date
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from . import db, contributor_desk as people, fund_desk as funds

router = APIRouter(prefix="/api/v2")


def csv_response(body, name):
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


@router.get("/contributors/{contributor}/desk/{operation:path}")
def contributor_desk(
    contributor: str,
    operation: str,
    symbol: str | None = None,
    before_id: int | None = Query(None, ge=1),
    after_id: int | None = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=1000),
):
    with db.database() as conn:
        people.profile(conn, contributor)
        if operation == "health":
            return health(conn, contributor, False)
        if operation == "status":
            return people.status(conn, contributor)
        if operation == "positions":
            return people.positions(conn, contributor)
        if operation == "instruments":
            return people.instruments(conn, contributor)
        if operation == "changes":
            return people.changes(conn, contributor, after_id)
        if operation == "events":
            rows = [
                e
                for e in reversed(people.events(conn, contributor, symbol))
                if before_id is None or e["id"] < before_id
            ]
            return dict(items=rows[:limit], has_more=len(rows) > limit)
        if operation.startswith("timeline/"):
            return people.events(conn, contributor, operation.split("/", 1)[1])
        if operation.startswith("analysis/"):
            return people.analysis(conn, contributor, operation.split("/", 1)[1])
        if operation.startswith("prices/"):
            from .chart_prices import prices

            selected = operation.split("/", 1)[1]
            item = next(
                (
                    i
                    for i in people.instruments(conn, contributor)
                    if i["symbol"] == selected
                ),
                None,
            )
            if not item:
                raise HTTPException(404, "Instrument not disclosed by this contributor")
            return prices(conn, "legacy:" + selected)
        if operation == "scorecard":
            return people.scores(conn, contributor, symbol)
        if operation == "export/history.csv":
            return csv_response(
                people.export_csv(conn, contributor), contributor + "-history"
            )
        if operation.startswith("export/pine/"):
            return PlainTextResponse(
                people.export_pine(conn, contributor, operation.split("/", 2)[2])
            )
    raise HTTPException(404, "Dashboard resource not found")


@router.get("/funds/{fund}/desk/{operation:path}")
def fund_desk(
    fund: str,
    operation: str,
    compare: Literal["previous", "week", "month", "date"] = "previous",
    compare_date: date | None = None,
    report_id: str | None = None,
    baseline_id: str | None = None,
    since_id: int | None = Query(None, ge=0),
    before_id: str | None = None,
):
    with db.database() as conn:
        funds.profile(conn, fund)
        if compare == "date" and not compare_date and not report_id:
            raise HTTPException(400, "Choose a comparison date")
        if operation == "health":
            return health(conn, fund, True)
        if operation == "status":
            return funds.status(conn, fund)
        if operation == "exposures":
            return funds.exposures(
                conn,
                fund,
                compare,
                compare_date.isoformat() if compare_date else None,
                report_id,
            )
        if operation == "history":
            return funds.history(conn, fund)
        if operation == "changes":
            return funds.changes(conn, fund, baseline_id, since_id)
        if operation == "revisions":
            return funds.revisions(conn, fund)
        if operation.startswith("revisions/"):
            return funds.revision(conn, fund, operation.split("/", 1)[1])
        if operation.startswith("reports/"):
            identifier = operation.split("/", 1)[1]
            if identifier.endswith("/source"):
                from .v2 import fund_source

                identifier = identifier.removesuffix("/source")
                funds.report(conn, fund, identifier)
                return fund_source(identifier)
            return funds.snapshot(conn, fund, funds.report(conn, fund, identifier))
        if operation.startswith("prices/"):
            from .chart_prices import prices

            key = operation.split("/", 1)[1]
            market = next(
                (m for m in funds.catalog(conn, fund) if m["id"] == key), None
            )
            if not market:
                raise HTTPException(404, "Asset not held by this fund")
            result = (
                prices(conn, market["asset_id"])
                if market["asset_id"]
                else dict(
                    bars=[],
                    error="No verified security mapping",
                    fetched_at=None,
                    adjustment="Unavailable",
                )
            )
            market.update(
                price_error=result.get("error"),
                price_checked_at=result.get("fetched_at"),
            )
            return dict(
                result,
                market=market,
                latest_price_date=(
                    result["bars"][-1]["time"] if result["bars"] else None
                ),
                omitted_bars=result.get("omitted_bars", []),
            )
        if operation == "export/history.csv":
            return csv_response(funds.export_csv(conn, fund), fund + "-history")
    raise HTTPException(404, "Dashboard resource not found")


def health(conn, identifier, is_fund):
    from .chart_prices import prices
    from datetime import datetime, timedelta

    if is_fund and identifier == "DBMF":
        from .desk import health_view

        result = health_view(conn, db.utcnow())
        result["desks"] = [d for d in result["desks"] if d["id"] == "dbmf"]
        status = funds.status(conn, identifier)
        result["desks"][0]["worker_healthy"] = status["worker_healthy"]
        if status["worker_healthy"]:
            result["issues"] = [
                i for i in result["issues"] if i["key"] != "dbmf:worker"
            ]
        result["issues"] = [
            i
            for i in result["issues"]
            if i["key"].startswith("dbmf:") or i["key"] == "backup"
        ]
        return result
    status = (
        funds.status(conn, identifier) if is_fund else people.status(conn, identifier)
    )
    assets = (
        [
            (m["asset_id"], m["name"])
            for m in funds.catalog(conn, identifier)
            if m["category"] != "Collateral" and m["asset_id"]
        ]
        if is_fund
        else [
            ("legacy:" + i["symbol"], i["name"])
            for i in people.instruments(conn, identifier)
            if i["active"]
        ]
    )
    checked = []
    for asset, name in assets:
        p = prices(conn, asset)
        latest = p["bars"][-1]["time"] if p["bars"] else None
        checked.append(
            dict(
                name=name,
                latest_date=latest,
                price_checked_at=p.get("fetched_at"),
                price_error=p.get("error"),
                stale=not latest
                or (db.utcnow().date() - date.fromisoformat(latest)).days > 4,
            )
        )
    link = ("/funds/" if is_fund else "/contributors/") + identifier
    issues = []
    for kind, problem, title in [
        (
            "collection",
            (
                status.get("last_run", {}).get("status") == "error"
                if status.get("last_run")
                else False
            ),
            "Collection failed",
        ),
        (
            "stale",
            status.get("collection_stale", status.get("stale")),
            "Collection is overdue",
        ),
        (
            "source",
            status.get("holdings_stale", status.get("source_outdated")),
            "Source date is old",
        ),
        ("worker", not status["worker_healthy"], "Collector heartbeat is missing"),
        (
            "prices",
            any(p["stale"] or p["price_error"] for p in checked),
            "Prices need attention",
        ),
    ]:
        if problem:
            issues.append(
                dict(
                    key=identifier + ":" + kind,
                    title=status["name"] + ": " + title,
                    detail="The last accepted data remains visible. Check Sources & alerts for details.",
                    link=link,
                    notify=True,
                )
            )
    desk = dict(
        id=identifier,
        name=status["name"],
        link=link,
        source_date=(
            status["current"]["source_date"]
            if is_fund and status["current"]
            else status.get("source_as_of")
        ),
        last_collected=status.get("last_collected", status.get("last_observed")),
        next_check=status["next_scheduled_run"],
        worker_healthy=status["worker_healthy"],
        running=bool(
            status.get("last_run") and status["last_run"]["status"] == "running"
        ),
        source_stale=status.get("holdings_stale", status.get("source_outdated")),
        collection_stale=status.get("collection_stale", status.get("stale")),
        prices=checked,
    )
    return dict(
        server_time=db.iso(),
        desks=[desk],
        issues=issues,
        price_rule="Charts show completed sessions with verified provider mappings. Missing prices remain unavailable.",
        last_backup=status["last_backup"],
    )
