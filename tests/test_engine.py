from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb
import pytest

from assistant.config import Config, SHANGHAI, UTC
from assistant.engine import Engine
from assistant import market as market_module
from assistant.market import MarketStore
from assistant.quotes import Quote
from assistant.storage import connect, init_db
from assistant.strategy import (buy_limit_cents, intraday_profit_crossed,
                                partial_shares, strict_crossing)


def weekdays(start: date, count: int) -> list[date]:
    days = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def app_parts(tmp_path: Path):
    market_path = tmp_path / "market.duckdb"
    state_path = tmp_path / "state.sqlite"
    days = weekdays(date(2026, 1, 5), 66)
    signal_day, entry_day, exit_day, final_day = days[-4:]
    c = duckdb.connect(str(market_path))
    c.execute("""
        CREATE TABLE securities(
          code VARCHAR PRIMARY KEY,board VARCHAR,ipo_date DATE,delist_date DATE,
          catalog_source VARCHAR,catalog_seen_at TIMESTAMP)
    """)
    c.execute("""
        CREATE TABLE daily_bars(
          code VARCHAR,trade_date DATE,open DOUBLE,high DOUBLE,low DOUBLE,
          close DOUBLE,preclose DOUBLE,q_open DOUBLE,q_high DOUBLE,q_low DOUBLE,
          q_close DOUBLE,volume DOUBLE,amount DOUBLE,trade_status SMALLINT,
          is_st BOOLEAN,source VARCHAR,acquired_at TIMESTAMP,
          PRIMARY KEY(code,trade_date))
    """)
    c.execute("""
        CREATE TABLE minute_bars(
          code VARCHAR,bar_time TIMESTAMP,frequency_minutes SMALLINT,
          open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,
          volume DOUBLE,amount DOUBLE,source VARCHAR,acquired_at TIMESTAMP,
          PRIMARY KEY(code,bar_time,frequency_minutes))
    """)
    c.execute("""
        CREATE TABLE ingest_log(run_time TIMESTAMP,source VARCHAR,dataset VARCHAR,
          code VARCHAR,first_date DATE,last_date DATE,rows_written BIGINT,status VARCHAR)
    """)
    for code in ("sh.600001", "sz.000001"):
        c.execute("INSERT INTO securities VALUES(?,?,?,?,?,?)",
                  (code, "sh_main" if code.startswith("sh") else "sz_main",
                   date(2020, 1, 1), None, "test", datetime.now()))
        rows = []
        previous = 10.0
        for day in days[:-3]:
            value = 8.79 if day == signal_day else 10.0
            rows.append((code, day, value, value + 0.1, value - 0.1,
                         value, previous, value, value + 0.1, value - 0.1,
                         value, 2_000_000.0, 200_000_000.0, 1, False,
                         "test", datetime.now()))
            previous = value
        c.executemany("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.close()
    init_db(state_path)
    with connect(state_path) as db:
        db.execute("UPDATE settings SET capital_cents=10000000")
    config = Config(state_db=state_path, market_db=market_path, observe_only=False)
    market = MarketStore(market_path)
    market.db.executemany(
        "INSERT INTO trading_calendar VALUES(?,?,?,?)",
        [(d, True, "test", datetime.now()) for d in days],
    )
    engine = Engine(config, market)
    yield engine, market, state_path, signal_day, entry_day, exit_day, final_day
    engine.close()
    market.close()


def quote(code: str, day: date, last: int, opening: int, preclose: int,
          *, hour: int = 10) -> Quote:
    source = datetime.combine(day, time(hour, 0, 0), SHANGHAI)
    return Quote(
        code=code, provider="test", source_time=source,
        received_at=source.astimezone(UTC) + timedelta(seconds=1),
        last_cents=last, open_cents=opening, preclose_cents=preclose,
        bid_cents=last, ask_cents=last, bid_volume=10000,
        ask_volume=10000, raw_hash=f"{code}:{day}:{last}", name="测试股份",
    )


def add_bar(market: MarketStore, code: str, day: date, close: float, high: float):
    market.db.execute("""
        INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (code, day, close, high, min(close, high) - 0.1, close, close,
          close, high, min(close, high) - 0.1, close,
          2_000_000.0, 200_000_000.0, 1, False, "test", datetime.now()))


def test_rule_boundaries():
    assert buy_limit_cents(10) == 960
    assert strict_crossing(961, 959, 960)
    assert not strict_crossing(960, 959, 960)
    assert not strict_crossing(961, 960, 960)
    assert partial_shares(300) == 100
    assert not intraday_profit_crossed(969, 960, "0.01")
    assert intraday_profit_crossed(971, 960, "0.01")


@pytest.mark.parametrize("candidate_count,expected_budget", [(49, 7_500_000),
                                                               (50, 20_000_000)])
def test_surge_budget_uses_full_candidate_count(app_parts, monkeypatch,
                                                candidate_count, expected_budget):
    engine, market, state_path, _, entry_day, _, _ = app_parts
    sample = market.signals(market.last_session_before(entry_day))[0]
    candidates = [{**sample, "code": f"sh.{600000 + index:06d}"}
                  for index in range(candidate_count)]
    monkeypatch.setattr(market, "signals", lambda _: candidates)
    with connect(state_path) as db:
        db.execute("UPDATE settings SET capital_cents=100000000")
    result = engine.preopen(entry_day, sync=False)
    assert result["candidate_count"] == candidate_count
    assert result["order_budget_cents"] == expected_budget
    with connect(state_path) as db:
        watches = [dict(row) for row in db.execute(
            "SELECT * FROM watchlist ORDER BY rank")]
        assert len(watches) == 5
        assert all(row["candidate_count"] == candidate_count for row in watches)
        assert all(row["order_budget_cents"] == expected_budget for row in watches)
        assert all(row["eligibility"] == "funded" for row in watches)
        assert all(row["reserved_cents"] == expected_budget for row in watches)


def test_signal_rank_uses_liquidity_then_nearest_five_day_decline(app_parts):
    _, market, _, signal_day, _, _, _ = app_parts
    five_sessions_prior = weekdays(date(2026, 1, 5), 66)[-9]
    market.db.execute(
        "UPDATE daily_bars SET close=10.05,high=10.15,low=9.95,"
        "q_close=10.05,q_high=10.15,q_low=9.95 "
        "WHERE code='sz.000001' AND trade_date=?", [five_sessions_prior])
    market.db.execute(
        "UPDATE daily_bars SET amount=500000000 WHERE code='sz.000001'")
    ranked = market.signals(signal_day)
    assert [row['code'] for row in ranked] == ['sz.000001', 'sh.600001']
    assert ranked[0]['ret5'] < ranked[1]['ret5']
    assert ranked[0]['liquidity20'] > ranked[1]['liquidity20']

    market.db.execute(
        "UPDATE daily_bars SET amount=200000000 WHERE code='sz.000001'")
    tied_liquidity = market.signals(signal_day)
    assert [row['code'] for row in tied_liquidity] == ['sh.600001', 'sz.000001']


def test_five_watch_budget_opportunity_and_restart(app_parts):
    engine, market, state_path, signal_day, entry_day, exit_day, final_day = app_parts
    result = engine.preopen(entry_day, sync=False)
    assert result["status"] == "published"
    assert len(result["watch"]) == 2
    with connect(state_path) as db:
        watches = [dict(r) for r in db.execute(
            "SELECT * FROM watchlist ORDER BY rank"
        )]
    assert watches[0]["eligibility"] == "funded"
    assert watches[1]["eligibility"] == "cash_insufficient"
    limit = watches[0]["buy_limit_cents"]
    assert limit == 843
    for watch in watches:
        signal = quote(watch["code"], entry_day, limit - 1, limit + 1, 879)
        if watch["eligibility"] == "cash_insufficient":
            signal = Quote(**{**signal.__dict__, "ask_cents": limit + 1})
        engine.process_quote(signal, allow_triggers=True)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM watchlist WHERE status='opportunity_only'"
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM virtual_legs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind='real_buy'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind IN ('virtual_buy','opportunity_only')").fetchone()[0] == 2
    engine.process_quote(quote(watches[0]["code"], entry_day, limit - 1,
                               limit + 1, 879), allow_triggers=True)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM virtual_legs").fetchone()[0] == 1
    assert engine.preopen(entry_day, sync=False)["status"] == "already_published"

    # A stock cannot be virtually sold on its buy date, even when price jumps.
    engine.process_quote(quote(watches[0]["code"], entry_day, 1000, 1000, 879),
                         allow_triggers=True)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM virtual_legs").fetchone()[0] == 1

    add_bar(market, watches[0]["code"], entry_day, 8.4, 8.5)
    # ret20 was -12%, so the 10% first target is crossed on the next session.
    engine.process_quote(quote(watches[0]["code"], exit_day, 940, 930, 840),
                         allow_triggers=True)
    with connect(state_path) as db:
        pos = db.execute("SELECT * FROM positions").fetchone()
        assert pos["partial_done"] == 1
        assert 0 < pos["remaining_shares"] < pos["bought_shares"]
        assert db.execute("SELECT COUNT(*) FROM virtual_legs").fetchone()[0] == 2
    add_bar(market, watches[0]["code"], exit_day, 9.2, 9.4)
    engine.process_quote(quote(watches[0]["code"], final_day, 820, 820, 920),
                         allow_triggers=True)
    with connect(state_path) as db:
        pos = db.execute("SELECT * FROM positions").fetchone()
        assert pos["status"] == "closed"
        assert pos["remaining_shares"] == 0
        assert pos["closed_pnl_cents"] is not None
        assert db.execute("SELECT COUNT(*) FROM virtual_legs").fetchone()[0] == 3


def test_virtual_fill_ignores_opposing_order_book(app_parts):
    engine, _, state_path, _, entry_day, _, _ = app_parts
    engine.preopen(entry_day, sync=False)
    with connect(state_path) as db:
        watch = db.execute("SELECT * FROM watchlist ORDER BY rank LIMIT 1").fetchone()
        code, limit = watch["code"], watch["buy_limit_cents"]
    source = quote(code, entry_day, limit - 1, limit + 1, 879)
    blocked = Quote(
        **{**source.__dict__, "ask_cents": limit + 1, "raw_hash": "high-ask"}
    )
    engine.process_quote(blocked, allow_triggers=True)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert db.execute("SELECT status FROM watchlist WHERE code=?", (code,)).fetchone()[0] == "virtual_bought"
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='price_triggered'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind='real_buy'").fetchone()[0] == 1
    engine.process_quote(source, allow_triggers=True)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_virtual_partial_sale_ignores_opposing_order_book(app_parts):
    engine, market, state_path, _, entry_day, exit_day, _ = app_parts
    engine.preopen(entry_day, sync=False)
    with connect(state_path) as db:
        watch = db.execute("SELECT * FROM watchlist ORDER BY rank LIMIT 1").fetchone()
        code, limit = watch["code"], watch["buy_limit_cents"]
    buy = quote(code, entry_day, limit - 1, limit + 1, 879)
    engine.process_quote(Quote(**{**buy.__dict__, "ask_cents": 0,
                                 "ask_volume": 0}), allow_triggers=True)
    add_bar(market, code, entry_day, 8.4, 8.5)
    sell = quote(code, exit_day, 940, 930, 840)
    engine.process_quote(Quote(**{**sell.__dict__, "bid_cents": 0,
                                  "bid_volume": 0}), allow_triggers=True)
    with connect(state_path) as db:
        position = db.execute("SELECT * FROM positions").fetchone()
        assert position["partial_done"] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='virtual_sell'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='real_buy'"
        ).fetchone()[0] == 1


def test_real_sale_alert_does_not_depend_on_bid_depth(app_parts):
    engine, market, state_path, _, entry_day, exit_day, _ = app_parts
    engine.preopen(entry_day, sync=False)
    with connect(state_path) as db:
        watch = db.execute("SELECT * FROM watchlist ORDER BY rank LIMIT 1").fetchone()
        code = watch["code"]
        db.execute("""
            INSERT INTO actual_positions(
              code,watch_id,entry_date,entry_price_cents,bought_shares,
              remaining_shares,target_pct,initial_stop_cents,prior_peak_cents,
              peak_asof_date,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (code, watch["id"], entry_day.isoformat(), 843, 1000, 1000,
              watch["target_pct"], 800, 843, entry_day.isoformat(),
              datetime.now().isoformat()))
    add_bar(market, code, entry_day, 8.4, 8.5)
    signal = quote(code, exit_day, 940, 930, 840)
    engine.process_quote(Quote(**{**signal.__dict__, "bid_cents": 0,
                                  "bid_volume": 0}), allow_triggers=True)
    with connect(state_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind='actual_sell'"
        ).fetchone()[0] == 1


def test_extra_candidates_are_not_published_or_monitored(app_parts, monkeypatch):
    engine, market, state_path, signal_day, entry_day, _, _ = app_parts
    original = market.signals(signal_day)
    candidates = [dict(original[0], code=f"sh.60000{i}") for i in range(1, 8)]
    monkeypatch.setattr(market, "signals", lambda _: candidates)
    result = engine.preopen(entry_day, sync=False)
    assert len(result["watch"]) == 5
    assert result["focus_count"] == 0
    assert len(engine.monitored_codes(entry_day)) == 5
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 5
        assert db.execute("SELECT COUNT(*) FROM focus_watchlist").fetchone()[0] == 0
        limit = db.execute("SELECT buy_limit_cents FROM watchlist ORDER BY rank LIMIT 1").fetchone()[0]
    signal = quote(candidates[5]["code"], entry_day, limit - 1, limit + 1, 879)
    engine.process_quote(signal, allow_triggers=True)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind='focus_triggered'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind='real_buy'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM virtual_legs").fetchone()[0] == 0
    engine.end_day(entry_day)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM focus_watchlist").fetchone()[0] == 0


def test_existing_watchlist_does_not_receive_focus_after_upgrade(app_parts, monkeypatch):
    engine, market, state_path, signal_day, entry_day, _, _ = app_parts
    original = market.signals(signal_day)
    candidates = [dict(original[0], code=f"sh.60000{i}") for i in range(1, 7)]
    monkeypatch.setattr(market, "signals", lambda _: candidates)
    engine.preopen(entry_day, sync=False)
    with connect(state_path) as db:
        db.execute("DELETE FROM focus_watchlist")
        db.execute("DELETE FROM jobs WHERE job_key=?", (f"focus:{entry_day}",))
    assert engine.ensure_focus(entry_day) == 0
    assert engine.ensure_focus(entry_day) == 0
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM focus_watchlist").fetchone()[0] == 0


def test_quote_failure_requires_three_polls_and_resolves_on_recovery(app_parts):
    engine, _, state_path, _, entry_day, _, _ = app_parts
    engine.preopen(entry_day, sync=False)
    codes = engine.monitored_codes(entry_day)

    class Router:
        healthy = False
        def fetch(self, requested, trading_date):
            assert requested == codes
            if self.healthy:
                return {code: quote(code, entry_day, 900, 900, 879) for code in codes}, {
                    "provider": "tencent", "healthy": True, "reason": ""}
            return {}, {"provider": "mixed", "healthy": False,
                        "reason": "tencent:missing_or_stale;sina:missing_or_stale"}

    router = Router()
    engine.router = router
    engine.poll(entry_day)
    engine.poll(entry_day)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind='quote_failure'").fetchone()[0] == 0
    engine.poll(entry_day)
    with connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind='quote_failure'").fetchone()[0] == 1
        assert db.execute("SELECT resolved_at FROM alerts WHERE kind='quote_failure'").fetchone()[0] is None
    router.healthy = True
    engine.poll(entry_day)
    with connect(state_path) as db:
        assert db.execute("SELECT resolved_at FROM alerts WHERE kind='quote_failure'").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='quote_recovered'").fetchone()[0] == 1


def test_daily_adjustment_rebases_older_history(app_parts, monkeypatch):
    _, market, _, signal_day, entry_day, _, _ = app_parts
    code = "sh.600001"
    old_q = market.db.execute(
        "SELECT q_close FROM daily_bars WHERE code=? AND trade_date=?",
        [code, signal_day],
    ).fetchone()[0]
    earlier = market.db.execute(
        "SELECT MIN(trade_date) FROM daily_bars WHERE code=?", [code]
    ).fetchone()[0]
    def fake_fetch(symbol, _start, _end):
        if symbol != code:
            return symbol, [], "test"
        def row(day, raw, adjusted):
            return (
                code, day, raw, raw + 0.1, raw - 0.1, raw, raw,
                adjusted, adjusted + 0.1, adjusted - 0.1, adjusted,
                2_000_000.0, 200_000_000.0, 1, None, "test", datetime.now(),
            )
        return code, [row(signal_day, 8.79, 8.0), row(entry_day, 8.8, 8.1)], "test"
    monkeypatch.setattr(market_module, "_daily_fetch", fake_fetch)
    result = market.sync_daily(entry_day, workers=1)
    assert result["rows_written"] == 2
    rebased = market.db.execute(
        "SELECT q_close FROM daily_bars WHERE code=? AND trade_date=?",
        [code, earlier],
    ).fetchone()[0]
    assert rebased == pytest.approx(10 * 8.0 / old_q)
