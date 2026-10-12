"""Resumable first-time collection of the market history needed by the strategy."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from .market import MarketStore, _bs_rows, _daily_fetch

LOG = logging.getLogger(__name__)


def _windows(start: date, end: date):
    current = start
    while current <= end:
        last = min(current + timedelta(days=419), end)
        yield current, last
        if last == end:
            break
        current = last - timedelta(days=7)


def _save_daily(market: MarketStore, code: str, rows: list[tuple], start: date,
                end: date, source: str) -> None:
    db = market.db
    db.execute("BEGIN TRANSACTION")
    try:
        anchor = None
        for row in rows:
            old = db.execute(
                "SELECT q_close FROM daily_bars WHERE code=? AND trade_date=?",
                [code, row[1]],
            ).fetchone()
            if old and old[0] and row[10]:
                anchor = (row[1], float(row[10]) / float(old[0]))
                break
        if anchor and abs(anchor[1] - 1) > 0.000001:
            db.execute("""
                UPDATE daily_bars SET q_open=q_open*?,q_high=q_high*?,
                  q_low=q_low*?,q_close=q_close*?
                WHERE code=? AND trade_date<?
            """, [anchor[1]] * 4 + [code, anchor[0]])
        if rows:
            db.executemany("""
                INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code,trade_date) DO UPDATE SET
                  open=excluded.open,high=excluded.high,low=excluded.low,
                  close=excluded.close,
                  preclose=COALESCE(excluded.preclose,daily_bars.preclose),
                  q_open=excluded.q_open,q_high=excluded.q_high,
                  q_low=excluded.q_low,q_close=excluded.q_close,
                  volume=excluded.volume,amount=excluded.amount,
                  trade_status=excluded.trade_status,
                  is_st=COALESCE(daily_bars.is_st,excluded.is_st),
                  source=excluded.source,acquired_at=excluded.acquired_at
            """, rows)
        db.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?,?,?,?)",
                   [datetime.now(), source, "bootstrap_daily", code,
                    start, end, len(rows), "ok"])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def _sync_st_history(market: MarketStore, start: date, end: date) -> list[str]:
    import baostock as bs

    db = market.db
    codes = [row[0] for row in db.execute("""
        SELECT DISTINCT code FROM daily_bars
        WHERE trade_date BETWEEN ? AND ? AND is_st IS NULL ORDER BY code
    """, [start, end]).fetchall()]
    if not codes:
        return []
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(login.error_msg)
    errors = []
    try:
        for index, code in enumerate(codes, 1):
            try:
                rows = _bs_rows(bs.query_history_k_data_plus(
                    code, "date,tradestatus,isST",
                    start_date=start.isoformat(), end_date=end.isoformat(),
                    frequency="d", adjustflag="3",
                ))
                flags = [(code, date.fromisoformat(r["date"]), r["isST"] == "1")
                         for r in rows if r.get("isST") in ("0", "1")
                         and r.get("tradestatus") == "1"]
                if flags:
                    frame = pd.DataFrame(flags, columns=["code", "trade_date", "is_st"])
                    db.register("incoming_st", frame)
                    try:
                        db.execute("""
                            UPDATE daily_bars AS d SET is_st=s.is_st
                            FROM incoming_st s WHERE d.code=s.code
                              AND d.trade_date=s.trade_date AND d.is_st IS NULL
                        """)
                    finally:
                        db.unregister("incoming_st")
                suspended = [(code, date.fromisoformat(r["date"]), "suspended",
                              "BaoStock historical status", datetime.now())
                             for r in rows if r.get("tradestatus") == "0"]
                if suspended:
                    db.executemany("""
                        INSERT INTO daily_gap_checks VALUES(?,?,?,?,?)
                        ON CONFLICT(code,trade_date) DO UPDATE SET
                          status=excluded.status,detail=excluded.detail,
                          checked_at=excluded.checked_at
                    """, suspended)
            except Exception as exc:
                errors.append(code)
                LOG.warning("Historical ST/status failed %s: %s", code, exc)
            if index % 100 == 0 or index == len(codes):
                LOG.info("Historical ST/status %s/%s; errors=%s", index, len(codes),
                         len(errors))
    finally:
        bs.logout()
    return errors


def bootstrap_history(market: MarketStore, today: date, *,
                      lookback_days: int = 1200, workers: int = 4) -> dict:
    if lookback_days < 1100 or workers < 1:
        raise ValueError("Need at least 1100 calendar days and one worker")
    start = today - timedelta(days=lookback_days)
    market.sync_calendar(start, today + timedelta(days=30))
    market.sync_catalog()
    target = market.last_session_before(today)
    windows = list(_windows(start, target))
    errors = []
    completed = 0
    for first, last in windows:
        codes = [r[0] for r in market.db.execute("""
            SELECT code FROM securities WHERE ipo_date<=?
              AND (delist_date IS NULL OR delist_date>=?)
              AND board IN ('sh_main','sz_main','chinext') ORDER BY code
        """, [last, first]).fetchall()]
        done = {r[0] for r in market.db.execute("""
            SELECT code FROM ingest_log WHERE dataset='bootstrap_daily'
              AND first_date=? AND last_date=? AND status='ok'
        """, [first, last]).fetchall()}
        todo = [code for code in codes if code not in done]
        LOG.info("History %s to %s: %s complete, %s remaining", first, last,
                 len(done), len(todo))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0, len(todo), 24):
                batch = todo[offset:offset + 24]
                futures = {pool.submit(_daily_fetch, code, first, last): code
                           for code in batch}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        _, rows, source = future.result()
                        _save_daily(market, code, rows, first, last, source)
                        completed += 1
                    except Exception as exc:
                        errors.append(code)
                        LOG.warning("History download failed %s %s-%s: %s",
                                    code, first, last, exc)
                LOG.info("History %s to %s: %s/%s attempted; errors=%s",
                         first, last, min(offset + 24, len(todo)), len(todo),
                         len(errors))
    st_errors = _sync_st_history(market, start, target)
    gaps = market.reconcile_missing(target)
    invalid = market.reconcile_invalid(target)
    audit = market.audit(target)
    result = {"start": start.isoformat(), "target": target.isoformat(),
              "windows": len(windows), "downloaded": completed,
              "daily_errors": sorted(set(errors)),
              "st_errors": sorted(set(st_errors)), "gaps": gaps,
              "invalid": invalid, "audit": audit}
    LOG.info("History bootstrap result: %s", result)
    return result
