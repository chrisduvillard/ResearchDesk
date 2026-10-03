"""Source-isolated schedules, bounded retry and official download discovery."""

import fcntl
import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlencode
from urllib.request import Request, urlopen
from bs4 import BeautifulSoup
from . import db
from .calendar import schedule as contributor_schedule
from .dbmf.collector import schedule as fund_schedule
from .research import ingest_disclosure, sync_legacy, archive_bytes, activity
from .funds import parse_cta, parse_kmlm, parse_ark, accept_report, qualify, sync_dbmf

log = logging.getLogger(__name__)


def download(url):
    with urlopen(
        Request(
            url,
            headers={
                "User-Agent": "ResearchDesk/2.0 personal research",
                "Cache-Control": "no-cache",
            },
        ),
        timeout=35,
    ) as response:
        data = response.read(15_000_001)
    if len(data) > 15_000_000:
        raise ValueError("Source exceeds 15 MB limit")
    return data


def official_link(url, domains):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in domains
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Download link is outside the configured official source")
    return url


def prepare_sources(conn):
    with conn:
        conn.execute(
            "UPDATE sources SET adapter='kmlm',enabled=1,coverage='observation',reason='Awaiting five scheduled successes across two dates' WHERE fund_id='KMLM' AND adapter IS NULL"
        )
        conn.execute(
            "UPDATE sources SET adapter='cta',enabled=1,coverage='observation',reason='Awaiting five scheduled successes across two dates' WHERE fund_id='CTA' AND adapter IS NULL"
        )
        conn.execute(
            "UPDATE sources SET adapter='ark',enabled=1,coverage='observation',reason='Awaiting five scheduled successes across two dates',url='https://www.ark-funds.com/funds/'||lower(fund_id) WHERE fund_id LIKE 'ARK%' AND adapter IS NULL"
        )
        conn.execute(
            "UPDATE sources SET reason='Official endpoint returned HTTP 403; automatic holdings unavailable' WHERE fund_id='WTMF' AND adapter IS NULL"
        )


def fetch_fund(source, fetch):
    fund = source["fund_id"]
    page = fetch(source["url"])
    archive_bytes(page, "funds")
    soup = BeautifulSoup(page, "html.parser")
    if fund == "CTA":
        links = {
            urljoin(source["url"], a["href"])
            for a in soup.find_all("a", href=True)
            if "Simplify_Portfolio_EOD_Tracker.xlsx" in a["href"]
        }
        if len(links) != 1:
            raise ValueError("CTA official workbook link missing or ambiguous")
        url = official_link(links.pop(), {"www.simplify.us"})
        match = re.search(r"(\d{4})_(\d{2})_(\d{2})_Simplify", url)
        if not match:
            raise ValueError("CTA reporting date missing from workbook link")
        raw = fetch(url)
        archive_bytes(raw, "funds")
        return parse_cta(raw, expected_date="-".join(match.groups())), raw, url
    if fund == "KMLM":
        links = {
            a["href"]
            for a in soup.find_all("a", href=True)
            if "_kmlm_holdings.csv" in a["href"]
        }
        if len(links) != 1:
            raise ValueError("KMLM official CSV link missing or ambiguous")
        url = official_link(links.pop(), {"kraneshares.com", "www.kraneshares.com"})
        match = re.search(r"(\d{2})_(\d{2})_(\d{4})_kmlm", url)
        nav = re.search(
            r"Fund Details\s+Data as of\s+(\d{2}/\d{2}/\d{4}).*?Net Assets\s*\$([\d,]+)",
            soup.get_text(" ", strip=True),
        )
        if not match or not nav:
            raise ValueError("KMLM NAV or source date missing")
        if (
            datetime.strptime(nav[1], "%m/%d/%Y").date().isoformat()
            != f"{match[3]}-{match[1]}-{match[2]}"
        ):
            raise ValueError("KMLM NAV and holdings dates differ")
        # The page full holdings table independently checks CSV completeness.
        table = next(
            (t for t in soup.find_all("table") if "Notional Value($)" in t.get_text()),
            None,
        )
        if table is None:
            raise ValueError("KMLM full holdings table missing")
        count = sum(
            bool(re.match(r"^\d+$", tr.find(["td", "th"]).get_text(strip=True)))
            for tr in table.find_all("tr")
            if tr.find(["td", "th"])
        )
        raw = fetch(url)
        archive_bytes(raw, "funds")
        return (
            parse_kmlm(
                raw,
                net_assets=nav[2],
                expected_date=f"{match[3]}-{match[1]}-{match[2]}",
                expected_rows=count,
            ),
            raw,
            url,
        )
    if source["adapter"] == "ark":
        match = re.search(rb"/api/fund/holdings/\d+", page)
        if not match:
            raise ValueError("ARK public holdings endpoint missing")
        data = {
            "Heading": "Top 10 Holdings",
            "PdfLinkText": "Full Holdings PDF",
            "CsvLinkText": "Full Holdings CSV",
            "Link": {"Style": "", "Href": "", "Aria": "", "Target": "", "Text": ""},
        }
        partial = fetch(
            "https://www.ark-funds.com"
            + match[0].decode()
            + "?"
            + urlencode({"fundHoldingData": json.dumps(data)})
        )
        archive_bytes(partial, "funds")
        links = {
            a["href"]
            for a in BeautifulSoup(partial, "html.parser").find_all("a", href=True)
            if ".csv" in a["href"].lower()
        }
        if len(links) != 1:
            raise ValueError("ARK full holdings CSV missing or ambiguous")
        url = official_link(links.pop(), {"assets.ark-funds.com"})
        raw = fetch(url)
        archive_bytes(raw, "funds")
        return parse_ark(raw, fund), raw, url
    raise ValueError("No qualified official adapter")


def due(conn, source_id, now):
    schedule = fund_schedule if source_id.startswith("fund:") else contributor_schedule
    slot = db.iso(schedule(now)[0])
    rows = conn.execute(
        "SELECT * FROM source_runs WHERE source_id=? AND slot=? AND scheduled=1 ORDER BY id",
        (source_id, slot),
    ).fetchall()
    if any(r["status"] == "success" for r in rows):
        return False
    failures = [r for r in rows if r["status"] in ("error", "interrupted")]
    if len(failures) >= 3:
        return False
    if not failures:
        return True
    return now >= datetime.fromisoformat(
        failures[-1]["finished_at"] or failures[-1]["started_at"]
    ) + timedelta(minutes=5 if len(failures) == 1 else 30)


def collect_source(source_id, *, scheduled=False, now=None, download=download):
    now = now or db.utcnow()
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub("[^a-zA-Z0-9_-]", "_", source_id)
    with (db.DATA_DIR / ("source-" + safe + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        with db.database() as conn:
            db.initialize(conn)
            source = conn.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if not source or not source["adapter"]:
                return {
                    "status": "unavailable",
                    "error": "Source adapter not qualified",
                }
            slot = db.iso(
                (fund_schedule if source["fund_id"] else contributor_schedule)(now)[0]
            )
            with conn:
                conn.execute(
                    "UPDATE source_runs SET status='interrupted',finished_at=? WHERE source_id=? AND status='running'",
                    (db.iso(now), source_id),
                )
                run = conn.execute(
                    "INSERT INTO source_runs(source_id,started_at,slot,status,scheduled) VALUES(?,?,?,'running',?)",
                    (source_id, db.iso(now), slot, int(scheduled)),
                ).lastrowid
            status, error, report_date = "success", None, None
            try:
                if source_id == "cnbc:dan-nathan":
                    from .collector import collect

                    result = collect()
                    sync_legacy(conn)
                    if result["status"] not in ("success", "partial"):
                        raise ValueError(result.get("error") or result["status"])
                    row = conn.execute(
                        "SELECT source_as_of FROM snapshots WHERE status='accepted' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    report_date = row[0][:10] if row else None
                elif source_id == "fund:DBMF":
                    from .dbmf.collector import collect

                    result = collect()
                    sync_dbmf(conn)
                    if result["status"] not in ("success", "partial"):
                        raise ValueError(result.get("error") or result["status"])
                    report_date = (
                        result["report"]["source_date"]
                        if "source_date" in result.get("report", {})
                        else conn.execute(
                            "SELECT source_date FROM dbmf_reports WHERE status='accepted' ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
                    )
                elif source["contributor_id"]:
                    raw = download(source["url"])
                    acquired = db.utcnow()
                    result = ingest_disclosure(
                        conn, source["contributor_id"], raw.decode("utf-8"), acquired
                    )
                    if result["status"] != "accepted":
                        raise ValueError(result["error"])
                    report_date = conn.execute(
                        "SELECT source_as_of FROM disclosures WHERE id=?",
                        (result["id"],),
                    ).fetchone()[0][:10]
                else:
                    doc, raw, url = fetch_fund(source, download)
                    doc["metadata"]["download_url"] = url
                    result = accept_report(conn, source["fund_id"], doc, raw)
                    report_date = result["source_date"]
            except Exception as exc:
                log.warning("%s: %s", source_id, exc)
                status, error = "error", str(exc)[:1500]
            with conn:
                conn.execute(
                    "UPDATE source_runs SET finished_at=?,status=?,error=?,report_date=? WHERE id=?",
                    (db.iso(now), status, error, report_date, run),
                )
                if source["fund_id"]:
                    qualified = qualify(conn, source_id)
                    conn.execute(
                        "UPDATE sources SET coverage=?,reason=? WHERE id=?",
                        (
                            "qualified" if qualified else "observation",
                            None
                            if qualified
                            else error
                            or "Awaiting five scheduled successes across two reporting dates",
                            source_id,
                        ),
                    )
                elif status == "success":
                    conn.execute(
                        "UPDATE sources SET coverage='available',reason=NULL WHERE id=?",
                        (source_id,),
                    )
                failed_key = "source_failed:" + source_id
                failed = db.setting(conn, failed_key, False)
                count = conn.execute(
                    "SELECT count(*) FROM source_runs WHERE source_id=? AND slot=? AND status='error'",
                    (source_id, slot),
                ).fetchone()[0]
                if status == "error" and count >= 3 and not failed:
                    activity(
                        conn,
                        f"source-failure:{run}",
                        source_id,
                        None,
                        "source_failure",
                        None,
                        db.iso(now),
                        source_id + ": retries exhausted",
                        source["url"],
                        "source_run",
                        run,
                    )
                    db.set_setting(conn, failed_key, True)
                elif status == "success" and failed:
                    activity(
                        conn,
                        f"source-recovery:{run}",
                        source_id,
                        None,
                        "source_recovery",
                        None,
                        db.iso(now),
                        source_id + ": recovered",
                        source["url"],
                        "source_run",
                        run,
                    )
                    db.set_setting(conn, failed_key, False)
            return dict(id=run, status=status, error=error, report_date=report_date)


def worker(kind):
    with db.database() as conn:
        db.initialize(conn)
        prepare_sources(conn)
    while True:
        try:
            with db.database() as conn:
                with conn:
                    db.set_setting(conn, kind + "_worker_heartbeat", db.iso())
                rows = conn.execute(
                    "SELECT * FROM sources WHERE enabled=1 AND "
                    + ("fund_id" if kind == "fund" else "contributor_id")
                    + " IS NOT NULL"
                ).fetchall()
            for source in rows:
                with db.database() as conn:
                    ready = due(conn, source["id"], db.utcnow())
                    with conn:
                        db.set_setting(conn, kind + "_worker_heartbeat", db.iso())
                if ready:
                    collect_source(source["id"], scheduled=True)
        except Exception:
            log.exception("Source worker loop failed")
        time.sleep(30)
