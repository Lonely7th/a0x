from __future__ import annotations

from datetime import date, datetime, timedelta

from assistant.bootstrap import _save_daily, _windows
from assistant.market import MarketStore


def test_new_market_database_has_all_tables_needed_by_worker(tmp_path):
    path = tmp_path / "market_full.duckdb"
    MarketStore.initialize(path)
    market = MarketStore(path)
    try:
        tables = {row[0] for row in market.db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()}
        assert {"securities", "daily_bars", "minute_bars", "ingest_log",
                "trading_calendar", "daily_gap_checks"} <= tables
    finally:
        market.close()


def test_bootstrap_windows_overlap_and_resume_log_is_written(tmp_path):
    first, second = list(_windows(date(2023, 1, 1), date(2024, 3, 1)))
    assert first[1] - second[0] == timedelta(days=7)
    path = tmp_path / "market_full.duckdb"
    MarketStore.initialize(path)
    market = MarketStore(path)
    try:
        day = date(2023, 12, 1)
        row = ("sh.600000", day, 10.0, 10.2, 9.8, 10.0, 9.9,
               10.0, 10.2, 9.8, 10.0, 1000000.0, 10000000.0,
               1, None, "test", datetime.now())
        _save_daily(market, "sh.600000", [row], day, day, "test")
        assert market.db.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE dataset='bootstrap_daily' AND status='ok'"
        ).fetchone()[0] == 1
        assert market.db.execute(
            "SELECT q_close FROM daily_bars WHERE code='sh.600000'"
        ).fetchone()[0] == 10.0
    finally:
        market.close()
