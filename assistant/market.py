from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import requests

LOG = logging.getLogger(__name__)
_SINA_FETCH_LOCK = threading.Lock()
_TENCENT_DAILY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def _bs_rows(result) -> list[dict]:
    if result.error_code != "0":
        raise RuntimeError(result.error_msg)
    out = []
    while result.next():
        out.append(dict(zip(result.fields, result.get_row_data())))
    return out


def _tencent_daily(symbol: str, start: date, end: date, adjust: str) -> pd.DataFrame:
    """Fetch a bounded daily window without AKShare's extra IPO-year lookup."""
    count = min(640, max(64, int((end - start).days * 5 / 7) + 25))
    response = requests.get(
        _TENCENT_DAILY_URL,
        params={
            "_var": f"kline_day{adjust}",
            "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},{count},{adjust}",
            "r": "0.8205512681390605",
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.text.split("=", 1)
    if len(payload) != 2:
        raise ValueError("Tencent daily response has no payload")
    body = json.loads(payload[1])
    if body.get("code") != 0:
        raise ValueError(f"Tencent daily error: {body.get('msg')}")
    item = body.get("data", {}).get(symbol, {})
    bars = item.get("qfqday" if adjust == "qfq" else "day") or item.get("day") or []
    volume_multiplier = (1 if symbol.startswith(("sh688", "sz399", "sh000", "sz000"))
                         else 100)
    rows = []
    for bar in bars:
        if len(bar) < 9:
            raise ValueError("Tencent daily row is incomplete")
        day = date.fromisoformat(bar[0])
        if day > end:
            continue
        rows.append({
            "date": day,
            "open": round(float(bar[1]), 2),
            "high": round(float(bar[3]), 2),
            "low": round(float(bar[4]), 2),
            "close": round(float(bar[2]), 2),
            "volume": float(bar[5]) * volume_multiplier,
            "amount": float(bar[8]) * 10000,
        })
    return pd.DataFrame(rows, columns=[
        "date", "open", "high", "low", "close", "volume", "amount"
    ])


def _daily_fetch(code: str, start: date, end: date) -> tuple[str, list[tuple], str]:
    symbol = code.replace(".", "")
    low = start.strftime("%Y%m%d")
    high = end.strftime("%Y%m%d")
    failures = []
    for source in ("Tencent", "Sina"):
        try:
            if source == "Sina":
                import akshare as ak
                # Sina's native V8 decoder can abort the entire Python process
                # when several MiniRacer instances initialize concurrently.
                with _SINA_FETCH_LOCK:
                    raw = ak.stock_zh_a_daily(
                        symbol=symbol, start_date=low, end_date=high, adjust=""
                    )
                    adjusted = ak.stock_zh_a_daily(
                        symbol=symbol, start_date=low, end_date=high, adjust="qfq"
                    )
            else:
                raw = _tencent_daily(symbol, start, end, "")
                adjusted = _tencent_daily(symbol, start, end, "qfq")
            if raw.empty and adjusted.empty:
                return code, [], source
            if raw.empty or adjusted.empty:
                raise ValueError("Raw and adjusted data differ")
            raw = raw.copy()
            adjusted = adjusted.copy()
            raw["date"] = pd.to_datetime(raw["date"])
            adjusted["date"] = pd.to_datetime(adjusted["date"])
            if set(raw["date"]) != set(adjusted["date"]):
                raise ValueError("Raw and adjusted dates differ")
            raw = raw.sort_values("date")
            # Extra history before start is requested to recover the previous close.
            raw["preclose"] = raw["close"].shift(1)
            q = adjusted[["date", "open", "high", "low", "close"]].rename(
                columns={"open": "q_open", "high": "q_high", "low": "q_low", "close": "q_close"}
            )
            merged = raw.merge(q, on="date", validate="one_to_one")
            rows = []
            for row in merged.itertuples(index=False):
                day = pd.Timestamp(row.date).date()
                if not start <= day <= end:
                    continue
                fields = [row.open, row.high, row.low, row.close,
                          row.q_open, row.q_high, row.q_low, row.q_close,
                          row.volume, row.amount]
                if (any(pd.isna(x) for x in fields) or row.low <= 0
                        or row.high < row.low or row.volume <= 0 or row.amount <= 0):
                    raise ValueError("Invalid daily OHLCV")
                rows.append((
                    code, day, float(row.open), float(row.high), float(row.low),
                    float(row.close), float(row.preclose) if pd.notna(row.preclose) else None,
                    float(row.q_open), float(row.q_high), float(row.q_low), float(row.q_close),
                    float(row.volume), float(row.amount), 1, None,
                    "Tencent/direct" if source == "Tencent" else "AKShare/Sina",
                    datetime.now(),
                ))
            return code, rows, source
        except Exception as exc:
            failures.append(f"{source}:{type(exc).__name__}:{str(exc)[:100]}")
            time.sleep(0.3)
    raise RuntimeError(f"{code} " + "; ".join(failures))


class MarketStore:
    def __init__(self, path: Path):
        if not path.exists():
            raise FileNotFoundError(path)
        self.path = path
        self.db = duckdb.connect(str(path))
        self._ensure_schema()

    @classmethod
    def initialize(cls, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(path)) as db:
            cls._create_schema(db)

    def _ensure_schema(self) -> None:
        self._create_schema(self.db)

    @staticmethod
    def _create_schema(db: duckdb.DuckDBPyConnection) -> None:
        db.execute("""
            CREATE TABLE IF NOT EXISTS securities (
              code VARCHAR PRIMARY KEY, board VARCHAR NOT NULL, ipo_date DATE,
              delist_date DATE, catalog_source VARCHAR NOT NULL,
              catalog_seen_at TIMESTAMP NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS daily_bars (
              code VARCHAR NOT NULL, trade_date DATE NOT NULL,
              open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, preclose DOUBLE,
              q_open DOUBLE, q_high DOUBLE, q_low DOUBLE, q_close DOUBLE,
              volume DOUBLE, amount DOUBLE, trade_status SMALLINT, is_st BOOLEAN,
              source VARCHAR NOT NULL, acquired_at TIMESTAMP NOT NULL,
              PRIMARY KEY(code, trade_date)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS minute_bars (
              code VARCHAR NOT NULL, bar_time TIMESTAMP NOT NULL,
              frequency_minutes SMALLINT NOT NULL, open DOUBLE, high DOUBLE,
              low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE,
              source VARCHAR NOT NULL, acquired_at TIMESTAMP NOT NULL,
              PRIMARY KEY(code, bar_time, frequency_minutes)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS ingest_log (
              run_time TIMESTAMP, source VARCHAR, dataset VARCHAR,
              code VARCHAR, first_date DATE, last_date DATE,
              rows_written BIGINT, status VARCHAR
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS trading_calendar (
              trade_date DATE PRIMARY KEY, is_open BOOLEAN NOT NULL,
              source VARCHAR NOT NULL, checked_at TIMESTAMP NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS daily_gap_checks (
              code VARCHAR, trade_date DATE, status VARCHAR, detail VARCHAR,
              checked_at TIMESTAMP, PRIMARY KEY(code,trade_date)
            )
        """)

    def close(self) -> None:
        self.db.close()

    def sync_calendar(self, start: date, end: date) -> int:
        import baostock as bs

        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(result.error_msg)
        try:
            rows = _bs_rows(bs.query_trade_dates(
                start_date=start.isoformat(), end_date=end.isoformat()
            ))
        finally:
            bs.logout()
        values = [(date.fromisoformat(r["calendar_date"]),
                   r["is_trading_day"] == "1", "BaoStock", datetime.now()) for r in rows]
        if not values:
            raise RuntimeError("Trade calendar source returned no dates")
        self.db.executemany("""
            INSERT INTO trading_calendar VALUES(?,?,?,?)
            ON CONFLICT(trade_date) DO UPDATE SET
              is_open=excluded.is_open,source=excluded.source,checked_at=excluded.checked_at
        """, values)
        return len(values)

    def session_on(self, day: date) -> bool:
        row = self.db.execute(
            "SELECT is_open FROM trading_calendar WHERE trade_date=?", [day]
        ).fetchone()
        return bool(row and row[0])

    def last_session_before(self, day: date) -> date:
        row = self.db.execute(
            "SELECT MAX(trade_date) FROM trading_calendar WHERE is_open AND trade_date<?", [day]
        ).fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Trading calendar has no previous open day")
        return row[0]

    def next_session(self, day: date) -> date:
        row = self.db.execute(
            "SELECT MIN(trade_date) FROM trading_calendar WHERE is_open AND trade_date>?", [day]
        ).fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Trading calendar has no next open day")
        return row[0]

    def sessions_since(self, entry: date, current: date) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM trading_calendar WHERE is_open AND trade_date>? AND trade_date<=?",
            [entry, current],
        ).fetchone()[0])

    def sync_catalog(self) -> int:
        import baostock as bs

        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(result.error_msg)
        try:
            rows = _bs_rows(bs.query_stock_basic())
        finally:
            bs.logout()
        values = []
        for r in rows:
            code = r["code"]
            if r.get("type") != "1":
                continue
            board = ("sh_main" if code.startswith("sh.60") else
                     "sz_main" if code.startswith(("sz.00", "sz.001", "sz.002")) else
                     "chinext" if code.startswith("sz.30") else None)
            if board:
                values.append((code, board, r.get("ipoDate") or None,
                               r.get("outDate") or None, "BaoStock", datetime.now()))
        if not values:
            raise RuntimeError("Empty stock catalog")
        self.db.executemany("""
            INSERT INTO securities VALUES(?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET board=excluded.board,
              ipo_date=excluded.ipo_date,delist_date=excluded.delist_date,
              catalog_source=excluded.catalog_source,catalog_seen_at=excluded.catalog_seen_at
        """, values)
        return len(values)

    def _active_codes(self, day: date) -> list[str]:
        return [r[0] for r in self.db.execute("""
            SELECT code FROM securities WHERE ipo_date<=?
              AND (delist_date IS NULL OR delist_date>=?)
              AND board IN ('sh_main','sz_main','chinext') ORDER BY code
        """, [day, day]).fetchall()]

    def sync_daily(self, target: date, deadline: datetime | None = None,
                   workers: int = 6) -> dict:
        # AKShare's per-symbol progress bars overwhelm the useful batch log.
        os.environ.setdefault("TQDM_DISABLE", "1")
        codes = self._active_codes(target)
        existing = {r[0] for r in self.db.execute(
            "SELECT code FROM daily_bars WHERE trade_date=?", [target]
        ).fetchall()}
        todo = [c for c in codes if c not in existing]
        latest_by_code = dict(self.db.execute("""
            SELECT code,MAX(trade_date) FROM daily_bars GROUP BY code
        """).fetchall())
        starts = {
            code: min(target - timedelta(days=21),
                      latest_by_code[code] - timedelta(days=7))
            if latest_by_code.get(code) else target - timedelta(days=21)
            for code in todo
        }
        failures = []
        written = 0
        # Keep all DuckDB writes in this process; only HTTP retrieval is parallel.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            offset = 0
            while offset < len(todo):
                if deadline and datetime.now(deadline.tzinfo) >= deadline:
                    break
                batch = todo[offset:offset + 24]
                futures = {pool.submit(_daily_fetch, code, starts[code], target): code
                           for code in batch}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        _, rows, source = future.result()
                        if rows:
                            # Forward-adjusted prices can be rebased after a
                            # corporate action. Rebase older stored bars using
                            # an overlapping day before replacing this window.
                            anchor = None
                            for new_row in rows:
                                old = self.db.execute(
                                    "SELECT q_close FROM daily_bars WHERE code=? AND trade_date=?",
                                    [code, new_row[1]],
                                ).fetchone()
                                if old and old[0] and new_row[10]:
                                    anchor = (new_row[1], float(new_row[10]) / float(old[0]))
                                    break
                            self.db.execute("BEGIN TRANSACTION")
                            try:
                                if anchor and abs(anchor[1] - 1) > 0.000001:
                                    self.db.execute("""
                                        UPDATE daily_bars SET
                                          q_open=q_open*?,q_high=q_high*?,
                                          q_low=q_low*?,q_close=q_close*?
                                        WHERE code=? AND trade_date<?
                                    """, [anchor[1]] * 4 + [code, anchor[0]])
                                self.db.executemany("""
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
                                self.db.execute("COMMIT")
                            except Exception:
                                self.db.execute("ROLLBACK")
                                raise
                            written += len(rows)
                        self.db.execute(
                            "INSERT INTO ingest_log VALUES(?,?,?,?,?,?,?,?)",
                            [datetime.now(), "Tencent/direct" if source == "Tencent"
                             else "AKShare/Sina", "daily", code,
                             starts[code], target, len(rows), "ok"],
                        )
                    except Exception as exc:
                        failures.append(code)
                        self.db.execute(
                            "INSERT INTO ingest_log VALUES(?,?,?,?,?,?,?,?)",
                            [datetime.now(), "Tencent/Sina", "daily", code, starts[code],
                             target, 0, str(exc)[:300]],
                        )
                offset += len(batch)
                LOG.info("Daily sync %s: %s/%s, errors=%s", target, offset, len(todo), len(failures))
        return {"target": target.isoformat(), "needed": len(todo),
                "processed": offset, "rows_written": written, "errors": failures}

    def sync_st_and_classify(self, target: date, deadline: datetime | None = None) -> dict:
        import baostock as bs

        codes = self._active_codes(target)
        missing = [r[0] for r in self.db.execute("""
            SELECT s.code FROM securities s LEFT JOIN daily_bars b
              ON s.code=b.code AND b.trade_date=?
            WHERE s.ipo_date<=? AND (s.delist_date IS NULL OR s.delist_date>=?)
              AND (b.code IS NULL OR b.is_st IS NULL) ORDER BY s.code
        """, [target, target, target]).fetchall()]
        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(result.error_msg)
        failures = []
        checked = 0
        try:
            for code in missing:
                if deadline and datetime.now(deadline.tzinfo) >= deadline:
                    break
                try:
                    rows = _bs_rows(bs.query_history_k_data_plus(
                        code, "date,tradestatus,isST,open,high,low,close,preclose,volume,amount",
                        start_date=target.isoformat(), end_date=target.isoformat(),
                        frequency="d", adjustflag="3",
                    ))
                    matching = next((r for r in rows if r["date"] == target.isoformat()), None)
                    if matching is None:
                        status = "source_missing"
                    elif matching["tradestatus"] == "0":
                        status = "suspended"
                    elif matching["tradestatus"] == "1":
                        status = "traded_unfilled"
                        if matching["isST"] in ("0", "1"):
                            self.db.execute(
                                "UPDATE daily_bars SET is_st=? WHERE code=? AND trade_date=?",
                                [matching["isST"] == "1", code, target],
                            )
                            has_bar = self.db.execute(
                                "SELECT 1 FROM daily_bars WHERE code=? AND trade_date=?",
                                [code, target],
                            ).fetchone()
                            if has_bar:
                                status = "present"
                    else:
                        status = "unknown_status"
                    self.db.execute("""
                        INSERT INTO daily_gap_checks VALUES(?,?,?,?,?)
                        ON CONFLICT(code,trade_date) DO UPDATE SET
                          status=excluded.status,detail=excluded.detail,
                          checked_at=excluded.checked_at
                    """, [code, target, status, "BaoStock dated status", datetime.now()])
                    checked += 1
                except Exception as exc:
                    failures.append(code)
                    LOG.warning("ST/status lookup failed %s: %s", code, exc)
        finally:
            bs.logout()
        return {"needed": len(missing), "checked": checked, "errors": failures}

    def reconcile_missing(self, target: date, deadline: datetime | None = None,
                          lookback_days: int = 1100) -> dict:
        """Classify lawful missing sessions and repair traded gaps with dated BaoStock data."""
        import baostock as bs

        start = target - timedelta(days=lookback_days)
        gaps = self.db.execute("""
            WITH eligible AS (
              SELECT s.code,c.trade_date,s.delist_date FROM securities s
              JOIN trading_calendar c ON c.is_open AND c.trade_date BETWEEN ? AND ?
                AND c.trade_date>=s.ipo_date
                AND (s.delist_date IS NULL OR c.trade_date<=s.delist_date)
              WHERE s.board IN ('sh_main','sz_main','chinext')
            )
            SELECT e.code,e.trade_date,e.delist_date FROM eligible e
            LEFT JOIN daily_bars b ON b.code=e.code AND b.trade_date=e.trade_date
            LEFT JOIN daily_gap_checks g ON g.code=e.code AND g.trade_date=e.trade_date
            WHERE b.code IS NULL
              AND (g.status IS NULL OR g.status NOT IN ('suspended','delist_day'))
            ORDER BY e.code,e.trade_date
        """, [start, target]).fetchall()
        grouped: dict[str, list[tuple]] = defaultdict(list)
        for code, day, delist in gaps:
            grouped[code].append((day, delist))
        if not grouped:
            return {"missing": 0, "repaired": 0, "classified": 0, "unresolved": 0}
        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(result.error_msg)
        repaired = classified = unresolved = 0
        try:
            for code, dates in grouped.items():
                if deadline and datetime.now(deadline.tzinfo) >= deadline:
                    break
                lo, hi = dates[0][0].isoformat(), dates[-1][0].isoformat()
                try:
                    raw_rows = _bs_rows(bs.query_history_k_data_plus(
                        code, "date,open,high,low,close,preclose,volume,amount,tradestatus,isST",
                        start_date=lo, end_date=hi, frequency="d", adjustflag="3"))
                    q_rows = _bs_rows(bs.query_history_k_data_plus(
                        code, "date,open,high,low,close",
                        start_date=lo, end_date=hi, frequency="d", adjustflag="2"))
                    raw = {r["date"]: r for r in raw_rows}
                    adjusted = {r["date"]: r for r in q_rows}
                    for day, delist in dates:
                        r = raw.get(day.isoformat())
                        q = adjusted.get(day.isoformat())
                        status = "source_missing"
                        detail = "BaoStock returned no dated row"
                        if r and r.get("tradestatus") == "0":
                            status, detail = "suspended", "BaoStock tradestatus=0"
                            classified += 1
                        elif r and r.get("tradestatus") == "1" and q:
                            try:
                                values = [float(r[k]) for k in
                                          ("open", "high", "low", "close", "volume", "amount")]
                                qvalues = [float(q[k]) for k in ("open", "high", "low", "close")]
                                if values[0] <= 0 or values[2] <= 0 or values[1] < values[2]:
                                    raise ValueError("invalid raw OHLC")
                                self.db.execute("""
                                    INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                    ON CONFLICT(code,trade_date) DO NOTHING
                                """, [code, day, values[0], values[1], values[2], values[3],
                                      float(r["preclose"]) if r.get("preclose") else None,
                                      *qvalues, values[4], values[5], 1,
                                      r.get("isST") == "1" if r.get("isST") in ("0", "1") else None,
                                      "BaoStock gap repair", datetime.now()])
                                status, detail = "filled", "BaoStock raw and adjusted daily"
                                repaired += 1
                            except (ValueError, TypeError) as exc:
                                status, detail = "traded_unfilled", str(exc)[:150]
                                unresolved += 1
                        elif day == delist:
                            status, detail = "delist_day", "Catalog delisting date"
                            classified += 1
                        else:
                            unresolved += 1
                        self.db.execute("""
                            INSERT INTO daily_gap_checks VALUES(?,?,?,?,?)
                            ON CONFLICT(code,trade_date) DO UPDATE SET
                              status=excluded.status,detail=excluded.detail,
                              checked_at=excluded.checked_at
                        """, [code, day, status, detail, datetime.now()])
                except Exception as exc:
                    unresolved += len(dates)
                    LOG.warning("Gap reconciliation failed for %s: %s", code, exc)
        finally:
            bs.logout()
        return {"missing": len(gaps), "repaired": repaired,
                "classified": classified, "unresolved": unresolved}

    def reconcile_invalid(self, target: date, deadline: datetime | None = None,
                          lookback_days: int = 1100) -> dict:
        """Repair malformed traded bars from dated BaoStock raw and adjusted rows."""
        import baostock as bs

        start = target - timedelta(days=lookback_days)
        bad = self.db.execute("""
            SELECT d.code,d.trade_date FROM daily_bars d JOIN securities s USING(code)
            WHERE d.trade_date BETWEEN ? AND ? AND d.trade_status=1
              AND s.board IN ('sh_main','sz_main','chinext')
              AND (d.is_st IS NULL OR d.close IS NULL OR d.q_close IS NULL OR
                   d.q_low IS NULL OR d.q_high IS NULL OR d.volume IS NULL OR
                   d.amount IS NULL OR d.close<=0 OR d.q_close<=0 OR
                   d.volume<=0 OR d.amount<=0 OR d.high<d.low)
            ORDER BY d.trade_date,d.code
        """, [start, target]).fetchall()
        if not bad:
            return {"needed": 0, "repaired": 0, "errors": []}
        result = bs.login()
        if result.error_code != "0":
            raise RuntimeError(result.error_msg)
        repaired = 0
        errors = []
        try:
            for code, day in bad:
                if deadline and datetime.now(deadline.tzinfo) >= deadline:
                    break
                try:
                    raw_rows = _bs_rows(bs.query_history_k_data_plus(
                        code, "date,open,high,low,close,preclose,volume,amount,tradestatus,isST",
                        start_date=day.isoformat(), end_date=day.isoformat(),
                        frequency="d", adjustflag="3",
                    ))
                    q_rows = _bs_rows(bs.query_history_k_data_plus(
                        code, "date,open,high,low,close",
                        start_date=day.isoformat(), end_date=day.isoformat(),
                        frequency="d", adjustflag="2",
                    ))
                    raw = next((r for r in raw_rows if r["date"] == day.isoformat()), None)
                    adjusted = next((r for r in q_rows if r["date"] == day.isoformat()), None)
                    if raw and raw["tradestatus"] == "0":
                        self.db.execute("""
                            UPDATE daily_bars SET trade_status=0,is_st=?,
                              source=?,acquired_at=? WHERE code=? AND trade_date=?
                        """, [raw["isST"] == "1" if raw["isST"] in ("0", "1") else None,
                              "BaoStock suspension confirmation", datetime.now(), code, day])
                        repaired += 1
                        continue
                    if not raw or not adjusted or raw["tradestatus"] != "1":
                        raise ValueError("Dated traded raw/adjusted row unavailable")
                    values = [float(raw[k]) for k in ("open", "high", "low", "close",
                                                      "volume", "amount")]
                    qvalues = [float(adjusted[k]) for k in ("open", "high", "low", "close")]
                    if (values[0] <= 0 or values[2] <= 0 or values[1] < values[2]
                            or values[4] <= 0 or values[5] <= 0
                            or min(qvalues) <= 0 or qvalues[1] < qvalues[2]
                            or raw["isST"] not in ("0", "1")):
                        raise ValueError("Dated repair values failed validation")
                    self.db.execute("""
                        UPDATE daily_bars SET open=?,high=?,low=?,close=?,
                          preclose=COALESCE(?,preclose),q_open=?,q_high=?,q_low=?,
                          q_close=?,volume=?,amount=?,is_st=?,source=?,
                          acquired_at=? WHERE code=? AND trade_date=?
                    """, [*values[:4], float(raw["preclose"]) if raw["preclose"] else None,
                          *qvalues, values[4], values[5], raw["isST"] == "1",
                          "BaoStock invalid repair", datetime.now(), code, day])
                    repaired += 1
                except Exception as exc:
                    errors.append({"code": code, "date": day.isoformat(),
                                   "reason": str(exc)[:160]})
        finally:
            bs.logout()
        return {"needed": len(bad), "repaired": repaired, "errors": errors}

    def audit(self, target: date, lookback_days: int = 1100) -> dict:
        start = target - timedelta(days=lookback_days)
        latest = self.db.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()[0]
        uncovered = self.db.execute("""
            WITH eligible AS (
              SELECT s.code,c.trade_date FROM securities s
              JOIN trading_calendar c ON c.is_open AND c.trade_date BETWEEN ? AND ?
                AND c.trade_date>=s.ipo_date
                AND (s.delist_date IS NULL OR c.trade_date<=s.delist_date)
              WHERE s.board IN ('sh_main','sz_main','chinext')
            )
            SELECT COUNT(*) FROM eligible e
            LEFT JOIN daily_bars b USING(code,trade_date)
            LEFT JOIN daily_gap_checks g USING(code,trade_date)
            WHERE b.code IS NULL AND (g.status IS NULL OR g.status NOT IN ('suspended','delist_day'))
        """, [start, target]).fetchone()[0]
        invalid = self.db.execute("""
            SELECT COUNT(*) FROM daily_bars d JOIN securities s USING(code)
            WHERE d.trade_date BETWEEN ? AND ? AND d.trade_status=1
              AND s.board IN ('sh_main','sz_main','chinext')
              AND (d.is_st IS NULL OR d.close IS NULL OR d.q_close IS NULL OR
                   d.q_low IS NULL OR d.q_high IS NULL OR d.volume IS NULL OR d.amount IS NULL OR
                   d.close<=0 OR d.q_close<=0 OR d.volume<=0 OR d.amount<=0 OR d.high<d.low)
        """, [start, target]).fetchone()[0]
        target_rows = self.db.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE trade_date=?", [target]
        ).fetchone()[0]
        okay = bool(latest and latest >= target and target_rows > 0
                    and uncovered == 0 and invalid == 0 and self.session_on(target))
        return {"target": target.isoformat(), "latest": str(latest),
                "target_rows": target_rows, "uncovered": int(uncovered),
                "invalid_rows": int(invalid), "ok": okay}

    def signals(self, signal_date: date) -> list[dict]:
        rows = self.db.execute("""
            WITH valid AS (
              SELECT d.code,d.trade_date,d.q_high,d.q_low,d.q_close,
                d.close AS raw_close,d.amount,s.board,s.ipo_date,
                row_number() OVER w AS bars_seen,
                lag(d.q_close,1) OVER w AS prev,
                lag(d.q_close,5) OVER w AS close5,
                lag(d.q_close,20) OVER w AS close20,
                median(d.amount) OVER
                  (PARTITION BY d.code ORDER BY d.trade_date
                   ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS liquidity20
              FROM daily_bars d JOIN securities s USING(code)
              WHERE d.trade_date<=? AND d.trade_status=1
                AND d.is_st IS NOT NULL AND d.volume>0 AND d.q_low>0
                AND d.q_high>=d.q_low
                AND d.q_close BETWEEN d.q_low-0.000001 AND d.q_high+0.000001
                AND s.board IN ('sh_main','sz_main','chinext')
              WINDOW w AS (PARTITION BY d.code ORDER BY d.trade_date)
            ), features AS (
              SELECT *,
                q_close/close5-1 AS ret5,
                q_close/close20-1 AS ret20,
                avg(greatest(q_high-q_low,abs(q_high-prev),abs(q_low-prev)))
                  OVER (PARTITION BY code ORDER BY trade_date
                        ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS atr14
              FROM valid
            )
            SELECT f.code,f.ret5,f.ret20,f.atr14,f.q_close,f.raw_close,
              f.liquidity20
            FROM features f JOIN daily_bars day
              ON day.code=f.code AND day.trade_date=f.trade_date
            WHERE f.trade_date=? AND NOT day.is_st AND f.bars_seen>=60
              AND date_diff('day',f.ipo_date,f.trade_date)>=180
              AND f.raw_close>=3 AND f.liquidity20>=100000000
              AND f.ret5<=-0.12 AND f.atr14/f.q_close<=0.06
            ORDER BY f.liquidity20 DESC,f.ret5 DESC,f.code ASC
        """, [signal_date, signal_date]).fetchall()
        keys = ["code", "ret5", "ret20", "atr14", "q_close", "raw_close", "liquidity20"]
        return [dict(zip(keys, row)) for row in rows]

    def close_price(self, code: str, day: date) -> float | None:
        row = self.db.execute(
            "SELECT high FROM daily_bars WHERE code=? AND trade_date=? AND trade_status=1",
            [code, day],
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def raw_close(self, code: str, day: date) -> float | None:
        row = self.db.execute(
            "SELECT close FROM daily_bars WHERE code=? AND trade_date=? AND trade_status=1",
            [code, day],
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def sync_minute_day(self, day: date, codes: list[str]) -> dict:
        import akshare as ak

        total = 0
        errors = []
        for code in sorted(set(codes)):
            try:
                frame = ak.stock_zh_a_minute(symbol=code.replace(".", ""), period="1", adjust="")
                if frame.empty:
                    raise ValueError("Empty minute response")
                frame["bar_time"] = pd.to_datetime(frame["day"])
                frame = frame[frame.bar_time.dt.date == day].copy()
                if frame.empty:
                    raise ValueError("Requested date absent in recent minute response")
                values = []
                for row in frame.itertuples(index=False):
                    values.append((
                        code, row.bar_time.to_pydatetime(), 1,
                        float(row.open), float(row.high), float(row.low), float(row.close),
                        float(row.volume), float(row.amount),
                        "AKShare/Sina", datetime.now(),
                    ))
                self.db.executemany("""
                    INSERT INTO minute_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(code,bar_time,frequency_minutes) DO UPDATE SET
                      open=excluded.open,high=excluded.high,low=excluded.low,
                      close=excluded.close,volume=excluded.volume,amount=excluded.amount,
                      source=excluded.source,acquired_at=excluded.acquired_at
                """, values)
                total += len(values)
            except Exception as exc:
                errors.append({"code": code, "reason": str(exc)[:160]})
        return {"date": day.isoformat(), "rows": total, "errors": errors}
