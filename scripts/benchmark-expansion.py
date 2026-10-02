#!/usr/bin/env python3
"""Disposable five-year, fifty-contributor, ten-fund read benchmark.

Run from the repository with its Python environment. Never reads live data.
"""

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from tracker import db
from tracker.api import app
from tracker.briefing import generate
from tracker.migrations import FUNDS


def main():
    with tempfile.TemporaryDirectory(prefix="research-desk-performance-") as scratch:
        db.DATA_DIR = Path(scratch)
        with db.database() as conn:
            db.initialize(conn)
            contributors = [r[0] for r in conn.execute("SELECT id FROM contributors")]
            days = [date(2021, 10, 1) + timedelta(days=i) for i in range(1826)]
            days = [d.isoformat() for d in days if d.weekday() < 5]
            with conn:
                for i in range(50 - len(contributors)):
                    c = f"synthetic-{i}"
                    contributors.append(c)
                    conn.execute("INSERT INTO contributors VALUES(?,?)", (c, c))
                    conn.execute(
                        "INSERT INTO sources(id,contributor_id,url,coverage) VALUES(?,?,?,'available')",
                        ("cnbc:" + c, c, "https://example.test/"),
                    )
                for i in range(20):
                    conn.execute(
                        "INSERT INTO assets(id,symbol,name,kind,verified) VALUES(?,?,?,'futures_market',1)",
                        (f"market:test{i}", f"TEST{i}", f"Synthetic market {i}"),
                    )
                for day in days:
                    timestamp = day + "T22:00:00+00:00"
                    for c in contributors:
                        conn.execute(
                            "INSERT INTO disclosures(contributor_id,source_id,source_as_of,acquired_at,processed_at,raw_path,content_hash,parser_version,status,text,positions_json) VALUES(?,?,?,?,?,'synthetic','synthetic','benchmark','accepted','', '[]')",
                            (c, "cnbc:" + c, timestamp, timestamp, timestamp),
                        )
                        conn.execute(
                            "INSERT INTO research_events(contributor_id,evidence_type,asset_id,symbol,kind,direction,eligible,available_at,source_url,excerpt) VALUES(?,'disclosure','market:test0','TEST0','added','bullish',0,?,'https://example.test/','Synthetic')",
                            (c, timestamp),
                        )
                        conn.execute(
                            "INSERT INTO activity(dedup_key,source_id,asset_id,kind,available_at,recorded_at,title,evidence_url,reference_type,reference_id) VALUES(?,?,'market:test0','added',?,?,'Synthetic','https://example.test/','event','0')",
                            (c + day, "cnbc:" + c, timestamp, timestamp),
                        )
                    for fund in FUNDS:
                        report = fund + day
                        conn.execute(
                            "INSERT INTO fund_reports(id,fund_id,source_id,source_date,acquired_at,processed_at,raw_path,content_hash,parser_version,status,complete) VALUES(?,?,?,?,?,?,'synthetic','synthetic','benchmark','accepted',1)",
                            (report, fund, "fund:" + fund, day, timestamp, timestamp),
                        )
                        conn.executemany(
                            "INSERT INTO fund_holdings(report_id,asset_id,instrument_key,name,measure,value,unit,evidence) VALUES(?,?,?,?,'notional_pct_nav','5','percent_nav','Synthetic')",
                            [
                                (report, f"market:test{i}", str(i), f"Market {i}")
                                for i in range(20)
                            ],
                        )
            generate(conn)
            counts = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "contributors",
                    "funds",
                    "research_events",
                    "fund_reports",
                    "fund_holdings",
                    "activity",
                )
            }
        timings = {}
        with TestClient(app) as client:
            for route in (
                "/api/v2/contributors",
                "/api/v2/contributors/dan-nathan/events",
                "/api/v2/fund-comparisons",
                "/api/v2/funds",
                "/api/v2/assets/market:test0",
                "/api/v2/briefings",
                "/api/v2/sources/status",
            ):
                client.get(route).raise_for_status()
                samples = []
                for _ in range(3):
                    start = perf_counter()
                    response = client.get(route)
                    response.raise_for_status()
                    samples.append(perf_counter() - start)
                timings[route] = round(max(samples), 3)
        print(
            json.dumps(
                {"counts": counts, "worst_seconds_of_three_cached_reads": timings},
                indent=2,
            )
        )
        if max(timings.values()) >= 2:
            raise SystemExit("Read target of less than two seconds was missed")


if __name__ == "__main__":
    main()
