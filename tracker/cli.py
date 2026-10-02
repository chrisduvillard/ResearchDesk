import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from . import db
from .collector import collect, worker, backup
from .history import review, attach_details
from .strategies import DIRECTIONS


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Operate the private disclosure tracker")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "collect", "worker", "worker-health", "backup"):
        sub.add_parser(command)
    for command in ('dbmf-collect', 'dbmf-worker', 'dbmf-health'):
        sub.add_parser(command)
    command = sub.add_parser('dbmf-backfill')
    command.add_argument('--force', action='store_true', help='Recheck previously accepted historical sources')
    command = sub.add_parser('dbmf-map')
    command.add_argument('--file', required=True, help='JSON with reviewed market/aliases, source_url and reason')
    command = sub.add_parser('dbmf-replay')
    command.add_argument('--force', action='store_true', help='Retry unresolved archived reports with the current adapter')
    sub.add_parser('dbmf-unmapped')
    command = sub.add_parser("review")
    command.add_argument("symbol")
    command.add_argument("direction", choices=DIRECTIONS)
    command.add_argument("--reason", required=True)
    command = sub.add_parser("instrument")
    command.add_argument("symbol")
    command.add_argument("--name")
    command.add_argument("--tradingview")
    command.add_argument("--reason", required=True)
    command = sub.add_parser("details")
    command.add_argument("symbol")
    command.add_argument("--file", required=True, help="JSON model with option legs and optional total net entry cost")
    command.add_argument("--source", required=True, help="Source URL or reference supporting the supplied details")
    command.add_argument("--reason", required=True)
    command.add_argument("--signature", help="Required if the instrument has multiple current strategies")
    args = parser.parse_args()
    if args.command.startswith('dbmf-'):
        from .dbmf import collector as dbmf_collector
        if args.command == 'dbmf-worker':
            dbmf_collector.worker()
        elif args.command == 'dbmf-collect':
            result = dbmf_collector.collect()
            print(json.dumps(result, indent=2))
            sys.exit(0 if result['status'] in ('success', 'busy') else 1)
        elif args.command == 'dbmf-backfill':
            from .dbmf.backfill import backfill
            result = backfill(args.force)
            with db.database() as conn:
                backup(conn)
            print(json.dumps(result, indent=2))
            sys.exit(1 if any(item['error'] for item in result) else 0)
        elif args.command == 'dbmf-map':
            from pathlib import Path
            from .dbmf.catalog import import_mapping
            from .dbmf.recovery import replay_pending
            document = json.loads(Path(args.file).read_text())
            with db.database() as conn:
                db.initialize(conn)
                backup(conn)
                result = import_mapping(conn, document)
                backup(conn)
            result['replay'] = replay_pending()
            print(json.dumps(result, indent=2))
        elif args.command == 'dbmf-replay':
            from .dbmf.recovery import replay_pending
            print(json.dumps(replay_pending(force=args.force, limit=200), indent=2))
        elif args.command == 'dbmf-unmapped':
            from .dbmf.recovery import pending_review
            with db.database() as conn:
                db.initialize(conn)
                print(json.dumps(pending_review(conn), indent=2))
        else:
            with db.database() as conn:
                last = db.setting(conn, 'dbmf_worker_heartbeat')
                sys.exit(0 if last and db.utcnow() - datetime.fromisoformat(last) < timedelta(minutes=10) else 1)
        return
    if args.command == "worker":
        worker()
        return
    if args.command == "collect":
        result = collect()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["status"] in ("success", "busy") else 1)
    with db.database() as conn:
        db.initialize(conn)
        if args.command == "worker-health":
            last = db.setting(conn, "worker_heartbeat")
            sys.exit(0 if last and db.utcnow() - datetime.fromisoformat(last) < timedelta(minutes=10) else 1)
        if args.command == "backup":
            print(backup(conn))
        if args.command == "details":
            from pathlib import Path
            attach_details(conn, args.symbol.upper(), json.loads(Path(args.file).read_text()),
                           args.source, args.reason, args.signature)
            print("Sourced legs saved for this wording. Raw evidence is unchanged; a correction event was recorded.")
        if args.command == "review":
            review(conn, args.symbol.upper(), args.direction, args.reason)
            print("Interpretation recorded; original source preserved and prospective scores excluded for this reviewed wording.")
        if args.command == "instrument":
            import re
            item = conn.execute("SELECT * FROM instruments WHERE symbol=?", (args.symbol.upper(),)).fetchone()
            if not item:
                raise SystemExit("Unknown instrument")
            if not args.name and not args.tradingview:
                raise SystemExit("Provide --name and/or --tradingview")
            if args.tradingview and not re.fullmatch(r"[A-Z0-9_]+:[A-Z0-9.\-]+", args.tradingview):
                raise SystemExit("TradingView mapping must be EXCHANGE:SYMBOL")
            with conn:
                new_name = args.name or item["name"]
                conn.execute("INSERT INTO name_revisions(symbol,old_name,new_name,reason,created_at) VALUES(?,?,?,?,?)", (item["symbol"], item["name"], new_name, args.reason + (f"; TradingView mapping: {args.tradingview}" if args.tradingview else ""), db.iso()))
                conn.execute("UPDATE instruments SET name=?,tv_symbol=? WHERE symbol=?", (new_name, args.tradingview or item["tv_symbol"], item["symbol"]))
            print("Instrument metadata updated; no position event created.")


if __name__ == "__main__":
    main()
