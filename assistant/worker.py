from __future__ import annotations

import faulthandler
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path

from .config import Config, now_cn, now_utc_iso
from .engine import Engine, continuous_session
from .market import MarketStore
from .progress import ProgressTracker
from .quotes import PublicQuoteProvider, QuoteRouter
from .storage import connect, init_db, set_status, transaction

LOG = logging.getLogger(__name__)


def restart_if_stalled(progress: dict) -> None:
    if progress["stalled"]:
        LOG.critical("Worker stalled for %ss at %s; exiting for supervisor restart",
                     progress["age_seconds"], progress["stage"])
        try:
            faulthandler.dump_traceback(all_threads=True)
        except Exception:
            LOG.exception("Could not dump stalled worker stacks")
        # A blocked main thread cannot unwind safely. The OS releases DB locks;
        # DuckDB/SQLite recover their WAL on restart. systemd Restart=always applies.
        os._exit(1)


def afterclose_deadline(current: datetime) -> datetime | None:
    cutoff = current.replace(hour=20, minute=30, second=0, microsecond=0)
    return cutoff if current < cutoff else None


@contextmanager
def singleton(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another worker is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another worker is already running") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_worker(config: Config, *, once: bool = False) -> None:
    config.validate()
    init_db(config.state_db)
    with singleton(config.state_db.with_suffix(".worker.lock")):
        heartbeat_stop = threading.Event()
        progress = ProgressTracker()

        def heartbeat() -> None:
            while not heartbeat_stop.is_set():
                snapshot = progress.snapshot()
                try:
                    with connect(config.state_db) as heartbeat_db, transaction(heartbeat_db):
                        set_status(heartbeat_db, "worker_progress", snapshot)
                        set_status(heartbeat_db, "worker_heartbeat",
                                   {"at": now_utc_iso(), "observe_only": config.observe_only,
                                    "date": now_cn().date().isoformat()})
                except Exception:
                    LOG.exception("Worker heartbeat update failed")
                restart_if_stalled(progress.snapshot())
                heartbeat_stop.wait(10)

        heartbeat_thread = threading.Thread(target=heartbeat, name="worker-heartbeat",
                                            daemon=True)
        heartbeat_thread.start()
        market = MarketStore(config.market_db, progress=progress.touch)
        router = QuoteRouter(
            PublicQuoteProvider("tencent"), PublicQuoteProvider("sina"),
            config.max_quote_age_seconds, config.quote_timeout_seconds,
        )
        engine = Engine(config, market, router)
        last_calendar = None
        last_catalog = None
        last_preopen_attempt = 0.0
        last_afterclose_attempt = 0.0
        last_catchup_attempt = float("-inf")
        last_poll = 0.0
        last_closed_notice = None
        LOG.info("Worker started; observe_only=%s, market_db=%s, state_db=%s",
                 config.observe_only, config.market_db, config.state_db)
        try:
            while True:
                progress.touch("worker loop")
                current = now_cn()
                today = current.date()
                day = today.isoformat()
                try:
                    if last_calendar != today:
                        market.sync_calendar(today - timedelta(days=1200),
                                             today + timedelta(days=30))
                        last_calendar = today
                    is_session = market.session_on(today)
                    with transaction(engine.db):
                        set_status(engine.db, "trading_calendar_today",
                                   {"date": day, "is_session": is_session,
                                    "next_session": market.next_session(today).isoformat()})
                    if not continuous_session(current):
                        engine._schedule_index_update(today, interval_seconds=1800)
                    if last_catalog != today and (not is_session or
                                                  current.time() < clock_time(9, 20)):
                        market.sync_catalog()
                        last_catalog = today
                    if (not is_session
                            and time.monotonic() - last_catchup_attempt >= 1800):
                        last_catchup_attempt = time.monotonic()
                        target = market.last_session_before(today)
                        if not market.audit(target)["ok"]:
                            LOG.info("Catch-up started for last completed session %s", target)
                            with transaction(engine.db):
                                set_status(engine.db, "catchup", {"target": str(target),
                                                                  "status": "running"})
                            try:
                                daily = market.sync_daily(target)
                                st = market.sync_st_and_classify(target)
                                gaps = market.reconcile_missing(target)
                                invalid = market.reconcile_invalid(target)
                                audit = market.audit(target)
                                with transaction(engine.db):
                                    set_status(engine.db, "catchup",
                                               {"target": str(target), "status": "ok" if audit["ok"] else "incomplete",
                                                "daily": daily, "st": st, "gaps": gaps,
                                                "invalid": invalid, "audit": audit})
                                LOG.info("Catch-up finished for %s: %s", target, audit)
                            except Exception as exc:
                                with transaction(engine.db):
                                    set_status(engine.db, "catchup",
                                               {"target": str(target), "status": "failed",
                                                "error": str(exc)[:500]})
                                LOG.exception("Catch-up failed for %s", target)
                    if is_session:
                        if (clock_time(8, 0) <= current.time() < clock_time(9, 20)
                            and time.monotonic() - last_preopen_attempt >= 300):
                            last_preopen_attempt = time.monotonic()
                            try:
                                deadline = current.replace(hour=9, minute=20, second=0,
                                                           microsecond=0)
                                progress.touch(f"preopen {today}")
                                result = engine.preopen(today, deadline=deadline)
                                LOG.info("Preopen %s", result)
                            except Exception:
                                LOG.exception("Preopen failed; will retry before cutoff")
                        if continuous_session(current):
                            if time.monotonic() - last_poll >= config.quote_interval_seconds:
                                last_poll = time.monotonic()
                                progress.touch(f"quote poll {today}")
                                result = engine.poll(today)
                                if not result["healthy"]:
                                    LOG.warning("Quote health: %s", result)
                        elif current.time() >= clock_time(15, 0):
                            engine.end_day(today)
                            if (current.time() >= clock_time(15, 30)
                                and time.monotonic() - last_afterclose_attempt >= 1800):
                                last_afterclose_attempt = time.monotonic()
                                try:
                                    target = today
                                    deadline = afterclose_deadline(current)
                                    daily = market.sync_daily(target, deadline)
                                    st = market.sync_st_and_classify(target, deadline)
                                    gaps = market.reconcile_missing(target, deadline)
                                    invalid = market.reconcile_invalid(target, deadline)
                                    audit = market.audit(target)
                                    watch_codes = [r[0] for r in engine.db.execute(
                                        "SELECT code FROM watchlist WHERE entry_date=?",
                                        (today.isoformat(),)
                                    ).fetchall()]
                                    codes = list(dict.fromkeys(
                                        watch_codes + engine.monitored_codes(today)
                                    ))
                                    minutes = market.sync_minute_day(today, codes)
                                    with transaction(engine.db):
                                        set_status(engine.db, "afterclose",
                                                   {"daily": daily, "st": st, "gaps": gaps,
                                                    "invalid": invalid,
                                                    "audit": audit, "minute": minutes})
                                except Exception as exc:
                                    with transaction(engine.db):
                                        set_status(engine.db, "afterclose", {
                                            "target": str(today), "status": "failed",
                                            "error": str(exc)[:500], "at": now_utc_iso()})
                                    LOG.exception("After-close synchronization failed")
                    elif last_closed_notice != today:
                        next_open = market.next_session(today)
                        LOG.info("Market closed on %s; waiting for next session %s",
                                 day, next_open.isoformat())
                        last_closed_notice = today
                    progress.touch("worker loop finished")
                    with transaction(engine.db):
                        set_status(engine.db, "worker_progress", progress.snapshot())
                        set_status(engine.db, "worker_heartbeat",
                                   {"at": now_utc_iso(), "observe_only": config.observe_only,
                                    "date": day})
                except Exception:
                    LOG.exception("Worker loop error")
                    with transaction(engine.db):
                        set_status(engine.db, "worker_error",
                                   {"at": now_utc_iso(), "message": "See worker log"})
                if once:
                    break
                time.sleep(1 if continuous_session(current) else 10)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            engine.close()
            market.close()
