from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from assistant.config import Config, now_cn
from assistant.engine import Engine
from assistant.market import MarketStore
from assistant.storage import init_db
from assistant.worker import run_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Local A-share manual trading assistant")
    parser.add_argument("command", choices=("init", "bootstrap", "ready", "worker", "web", "preopen", "sync", "status", "snapshot"))
    parser.add_argument("--date", help="Trading day, YYYY-MM-DD; default today")
    parser.add_argument("--no-sync", action="store_true", help="Preopen using existing audited data")
    parser.add_argument("--once", action="store_true", help="Run one worker loop")
    parser.add_argument("--output", help="New directory for the snapshot command")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.from_env()
    target = date.fromisoformat(args.date) if args.date else now_cn().date()
    if args.command == "init":
        MarketStore.initialize(config.market_db)
        init_db(config.state_db)
        print(f"Market database: {config.market_db}")
        print(f"State database: {config.state_db}")
        return
    if args.command == "bootstrap":
        from assistant.bootstrap import bootstrap_history

        config.validate()
        init_db(config.state_db)
        market = MarketStore(config.market_db)
        try:
            result = bootstrap_history(market, target)
        finally:
            market.close()
        print(result)
        if result["daily_errors"] or result["st_errors"] or not result["audit"]["ok"]:
            raise SystemExit("Historical bootstrap incomplete; rerun this command to resume")
        from assistant.storage import connect, set_status, transaction

        with connect(config.state_db) as db, transaction(db):
            set_status(db, "bootstrap_complete", result)
        return
    if args.command == "ready":
        from assistant.storage import connect

        config.validate()
        with connect(config.state_db) as db:
            row = db.execute("SELECT value FROM system_status WHERE key='bootstrap_complete'").fetchone()
        if not row:
            raise SystemExit("Historical bootstrap has not completed")
        print("Historical bootstrap completed")
        return
    if args.command == "web":
        import uvicorn
        from assistant.web import create_app
        init_db(config.state_db)
        uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
        return
    if args.command == "worker":
        run_worker(config, once=args.once)
        return
    if args.command == "snapshot":
        if not args.output:
            parser.error("snapshot requires --output")
        from pathlib import Path
        from assistant.snapshot import create_snapshot

        manifest = create_snapshot(config, Path(args.output))
        print(f"Snapshot created: {args.output}")
        print(f"Files: {', '.join(manifest['files'])}")
        return
    config.validate()
    init_db(config.state_db)
    if args.command == "status":
        from assistant.storage import connect
        db = connect(config.state_db)
        try:
            for row in db.execute("SELECT key,value,updated_at FROM system_status ORDER BY key"):
                print(dict(row))
        finally:
            db.close()
        return
    market = MarketStore(config.market_db)
    engine = Engine(config, market)
    try:
        if args.command == "preopen":
            market.sync_calendar(target - timedelta(days=1200), target)
            print(engine.preopen(target, sync=not args.no_sync))
        elif args.command == "sync":
            market.sync_calendar(target - timedelta(days=1200), target)
            market.sync_catalog()
            print(market.sync_daily(target))
            print(market.sync_st_and_classify(target))
            print(market.reconcile_missing(target))
            print(market.reconcile_invalid(target))
            print(market.audit(target))
    finally:
        engine.close()
        market.close()


if __name__ == "__main__":
    main()
