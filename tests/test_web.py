from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from assistant.config import Config, SHANGHAI
from assistant.storage import connect, init_db, set_status
from assistant.web import _event_cursor, _real_switch_allowed, create_app


def test_virtual_funding_switch_preserves_existing_trades_and_budget(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    day = datetime.now(SHANGHAI).date().isoformat()
    stamp = datetime.now(SHANGHAI).isoformat()
    with connect(state) as db:
        settings_id = db.execute("SELECT id FROM settings LIMIT 1").fetchone()[0]
        watch_id = db.execute("""
            INSERT INTO watchlist(entry_date,signal_date,code,rank,ret5,ret20,atr14,
                q_close,raw_close,liquidity20,buy_limit_cents,target_pct,
                settings_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (day, day, "sh.600001", 1, -.12, -.11, .2, 10, 10,
                200_000_000, 1000, ".10", settings_id, stamp)).lastrowid
        position_id = db.execute("""
            INSERT INTO positions(watch_id,code,entry_date,entry_price_cents,
                bought_shares,remaining_shares,target_pct,initial_stop_cents,
                prior_peak_cents,peak_asof_date,reserved_cents,buy_cash_cents,
                closed_pnl_cents,status,settings_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (watch_id, "sh.600001", day, 1000, 100, 0, ".10", 950,
                1000, day, 100_000, 100_000, 10_000, "closed", settings_id,
                stamp)).lastrowid
        for side, cash_delta, price in (("buy", -100_000, 1000),
                                        ("sell", 110_000, 1100)):
            db.execute("""
                INSERT INTO virtual_legs(position_id,side,shares,gross_price_cents,
                    cash_delta_cents,fee_cents,quote_event_key,reason,at)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (position_id, side, 100, price, cash_delta, 0,
                  f"test:{side}", side, stamp))
        original = (db.execute("SELECT * FROM positions").fetchall(),
                    db.execute("SELECT * FROM virtual_legs").fetchall(),
                    db.execute("SELECT * FROM settings").fetchall())

    init_db(state)  # Existing installs add the new display table at startup.
    stable = client.get("/api/virtual?account=stable").json()
    aggressive = client.get("/api/virtual?account=aggressive").json()
    assert stable["funding_mode"] == aggressive["funding_mode"] == "leveraged_5x"
    assert stable["capital_cents"] == 100_000_000
    assert stable["display_capital_cents"] == 20_000_000
    assert stable["book_pnl_cents"] == 10_000
    assert stable["book_pnl_pct"] == 10_000 / 20_000_000
    assert client.post("/api/virtual/funding-mode", json={
        "account": "stable", "mode": "cash_100w"}).status_code == 200
    cash = client.get("/api/virtual?account=stable").json()
    assert cash["display_capital_cents"] == 100_000_000
    assert cash["book_pnl_cents"] == stable["book_pnl_cents"]
    assert cash["book_pnl_pct"] == stable["book_pnl_pct"] / 5
    assert cash["available_budget_cents"] == stable["available_budget_cents"]
    assert cash["cash_cents"] == stable["cash_cents"]
    assert client.get("/api/virtual?account=aggressive").json()["funding_mode"] == "leveraged_5x"
    assert client.post("/api/virtual/funding-mode", json={
        "account": "aggressive", "mode": "cash_100w"}).status_code == 200
    assert client.post("/api/virtual/funding-mode", json={
        "account": "stable", "mode": "invalid"}).status_code == 422
    with connect(state) as db:
        assert db.execute("SELECT * FROM positions").fetchall() == original[0]
        assert db.execute("SELECT * FROM virtual_legs").fetchall() == original[1]
        assert db.execute("SELECT * FROM settings").fetchall() == original[2]


def test_event_stream_starts_at_latest_event_and_can_resume(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    with connect(state) as db:
        for number in range(3):
            db.execute(
                "INSERT INTO events(event_key,kind,at,payload) VALUES(?,?,?,?)",
                (f"event-{number}", "test", "2026-10-12T10:00:00+08:00", "{}"),
            )
        latest = db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        assert _event_cursor(db, 0, None, False) == latest
        assert _event_cursor(db, 0, None, True) == 0
        assert _event_cursor(db, 0, str(latest - 1), False) == latest - 1


def test_real_strategy_switch_is_immediate_and_intraday_is_locked(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    now = datetime.now(SHANGHAI)
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    with connect(state) as db:
        set_status(db, "trading_calendar_today", {"date": now.date().isoformat(),
                   "is_session": False, "next_session": tomorrow})
        set_status(db, "worker_heartbeat", {"date": now.date().isoformat(),
                   "observe_only": False})
        db.execute("INSERT INTO real_strategy_schedule VALUES(?,?,?)",
                   (tomorrow, "stable", now.isoformat()))
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    alert_id = _real_buy_alert(state)
    with connect(state) as db:
        settings_id = db.execute("SELECT id FROM settings LIMIT 1").fetchone()[0]
        db.execute("""
            INSERT INTO watchlist(
              entry_date,signal_date,code,rank,ret5,ret20,atr14,q_close,
              raw_close,liquidity20,buy_limit_cents,target_pct,planned_shares,
              candidate_count,settings_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (now.date().isoformat(), now.date().isoformat(), "sh.600002",
              2, -0.12, -0.11, 0.2, 10.0, 10.0, 200_000_000, 1000,
              "0.08", 1000, 20, settings_id, now.isoformat()))
    switched = client.post("/api/real/strategy", json={"strategy_id": "aggressive"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["effective_date"] == now.date().isoformat()
    assert switched.json()["updated_plans"] == 1
    board = client.get("/api/real").json()
    assert board["strategy_id"] == "aggressive"
    plans = {row["code"]: row for row in board["watchlist"]}
    assert plans["sh.600002"]["strategy_id"] == "aggressive"
    assert plans["sh.600002"]["no_partial"] == 1
    assert plans["sh.600002"]["first_target_cents"] is None
    assert plans["sh.600001"]["strategy_id"] == "stable"
    assert plans["sh.600001"]["first_target_cents"] == 1100
    with connect(state) as db:
        assert db.execute("SELECT COUNT(*) FROM real_strategy_schedule WHERE effective_date>?",
                          (now.date().isoformat(),)).fetchone()[0] == 0
    assert client.post(f"/api/real/alerts/{alert_id}/complete").status_code == 200
    assert client.get("/api/real").json()["positions"][0]["strategy_id"] == "stable"
    reverted = client.post("/api/real/strategy", json={"strategy_id": "stable"})
    assert reverted.status_code == 200, reverted.text
    assert client.get("/api/real").json()["strategy_id"] == "stable"
    with connect(state) as db:
        row = db.execute("SELECT real_target_pct,real_no_partial FROM watchlist WHERE code='sh.600002'").fetchone()
        assert row["real_target_pct"] == "0.08" and row["real_no_partial"] == 0
    assert not _real_switch_allowed(datetime(2026, 10, 15, 11, 45, tzinfo=SHANGHAI), True)
    assert _real_switch_allowed(datetime(2026, 10, 15, 15, 0, tzinfo=SHANGHAI), True)
    assert client.post("/api/real/strategy", json={"strategy_id": "invalid"}).status_code == 400


def test_old_pending_real_strategy_is_migrated_once(tmp_path: Path):
    from assistant.storage import real_strategy_for

    state = tmp_path / "state.sqlite"
    init_db(state)
    today = datetime.now(SHANGHAI).date().isoformat()
    tomorrow = (datetime.now(SHANGHAI).date() + timedelta(days=1)).isoformat()
    with connect(state) as db:
        db.execute("DELETE FROM system_status WHERE key='real_strategy_immediate_migrated'")
        db.execute("INSERT INTO real_strategy_schedule VALUES(?,?,?)",
                   (tomorrow, "aggressive", datetime.now().isoformat()))
    init_db(state)
    with connect(state) as db:
        assert real_strategy_for(db, today) == "aggressive"
        assert db.execute("SELECT COUNT(*) FROM real_strategy_schedule WHERE effective_date>?",
                          (today,)).fetchone()[0] == 0
    init_db(state)
    with connect(state) as db:
        assert real_strategy_for(db, today) == "aggressive"


def test_aggressive_real_buy_freezes_no_partial_exit(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    alert_id = _real_buy_alert(state)
    with connect(state) as db:
        db.execute("""
            UPDATE watchlist SET real_strategy_id='aggressive',
              real_target_pct=NULL,real_no_partial=1
        """)
    watch = client.get("/api/real").json()["watchlist"][0]
    assert watch["first_target_cents"] is None and watch["no_partial"] == 1
    result = client.post(f"/api/real/alerts/{alert_id}/complete")
    assert result.status_code == 200, result.text
    position = client.get("/api/real").json()["positions"][0]
    assert position["strategy_id"] == "aggressive"
    assert position["target_pct"] is None and position["no_partial"] == 1
    assert position["first_target_cents"] is None


def test_market_index_api_reports_level_and_change(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    stamp = datetime.now(SHANGHAI).isoformat()
    with connect(state) as db:
        db.execute("""
            INSERT INTO market_indices VALUES(?,?,?,?,?,?)
        """, ("sh.000001", 385000, 380000, stamp, stamp, "test"))
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    row = client.get("/api/market-indices").json()["indices"][0]
    assert row["last_cents"] == 385000
    assert abs(row["change_pct"] - (3850 / 3800 - 1)) < 1e-12


def _real_buy_alert(state: Path, code: str = "sh.600001") -> int:
    from assistant.storage import alert, append_event

    day = datetime.now(SHANGHAI).date().isoformat()
    with connect(state) as db:
        settings_id = db.execute("SELECT id FROM settings LIMIT 1").fetchone()[0]
        cursor = db.execute("""
            INSERT INTO watchlist(
              entry_date,signal_date,code,rank,ret5,ret20,atr14,q_close,
              raw_close,liquidity20,buy_limit_cents,target_pct,planned_shares,
              settings_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (day, day, code, 1, -0.12, -0.11, 0.2, 10.0, 10.0,
              200_000_000, 1000, "0.10", 1000, settings_id,
              datetime.now().isoformat()))
        key = f"real_buy:{day}:{code}"
        append_event(db, key, "real_buy_signal", code, cursor.lastrowid, None,
                     {"reference_price_cents": 1000})
        alert(db, key, "real_buy", "action", "买入提醒", "记录价 10.00 元",
              code, reference_price_cents=1000, action_kind="buy")
        return db.execute("SELECT id FROM alerts WHERE event_key=?", (key,)).fetchone()[0]


def test_real_alert_confirmation_tracks_price_and_phase_without_shares(tmp_path: Path):
    from assistant.storage import alert, append_event

    state = tmp_path / "state.sqlite"
    init_db(state)
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    alert_id = _real_buy_alert(state)
    assert client.get("/api/virtual").json()["watchlist"][0]["first_target_cents"] == 1100
    assert client.get("/api/real").json()["watchlist"][0]["first_target_cents"] == 1100
    signal_at = "2026-10-13T01:02:03+00:00"
    with connect(state) as db:
        db.execute("UPDATE alerts SET created_at=? WHERE id=?", (signal_at, alert_id))
    response = client.post(f"/api/real/alerts/{alert_id}/complete")
    assert response.status_code == 200, response.text
    position_id = response.json()["position_id"]
    assert client.post(f"/api/real/alerts/{alert_id}/complete").json()["duplicate"]
    assert client.get("/api/real").json()["positions"][0]["buy_signal_at"] == signal_at
    with connect(state) as db:
        pos = db.execute("SELECT * FROM real_positions WHERE id=?", (position_id,)).fetchone()
        assert pos["entry_price_cents"] == 1000
        assert pos["management_mode"] == "system"
        assert pos["initial_stop_cents"] < 1000
        assert "bought_shares" not in pos.keys()
        assert db.execute("SELECT COUNT(*) FROM actual_fills").fetchone()[0] == 0
        first_key = f"real_sell:{position_id}:first"
        append_event(db, first_key, "actual_sell_signal", pos["code"], pos["watch_id"],
                     position_id, {"reference_price_cents": 1100})
        alert(db, first_key, "actual_sell", "action", "首次止盈", "卖出约半仓",
              pos["code"], reference_price_cents=1100,
              real_position_id=position_id, action_kind="partial_profit")
        first_id = db.execute("SELECT id FROM alerts WHERE event_key=?", (first_key,)).fetchone()[0]
    assert client.post(f"/api/real/alerts/{first_id}/complete").status_code == 200
    live_position = client.get("/api/real").json()["positions"][0]
    assert live_position["partial_done"] == 1
    assert live_position["first_target_cents"] == 1100
    with connect(state) as db:
        final_key = f"real_sell:{position_id}:final"
        append_event(db, final_key, "actual_sell_signal", "sh.600001", None,
                     position_id, {"reference_price_cents": 990})
        alert(db, final_key, "actual_sell", "action", "清仓", "卖出剩余持仓",
              "sh.600001", reference_price_cents=990,
              real_position_id=position_id, action_kind="stop")
        final_id = db.execute("SELECT id FROM alerts WHERE event_key=?", (final_key,)).fetchone()[0]
    assert client.post(f"/api/real/alerts/{final_id}/complete").status_code == 200
    assert client.get("/api/real").json()["positions"] == []
    history = client.get("/api/real/positions/history").json()
    assert history["total"] == 1
    assert history["positions"][0]["buy_time"] == signal_at
    assert len(history["positions"][0]["transactions"]) == 3
    assert abs(history["positions"][0]["estimated_gross_return_pct"] - 0.045) < 1e-10
    with connect(state) as db:
        db.execute("""
            INSERT INTO real_positions(code,entry_date,entry_price_cents,status,
              created_at,closed_at)
            VALUES(?,?,?,?,?,?)
        """, ("sh.600002", "2026-10-12", 1000, "closed",
              "2026-10-12T09:30:00+08:00", "2026-10-14T10:00:00+08:00"))
    first_page = client.get("/api/real/positions/history?page_size=1").json()
    second_page = client.get("/api/real/positions/history?page=2&page_size=1").json()
    assert first_page["total"] == first_page["pages"] == 2
    assert first_page["positions"][0]["id"] != second_page["positions"][0]["id"]
    assert client.get("/api/real/positions/history?page=0").status_code == 400
    report = client.get("/api/real/report").json()
    assert len(report["actions"]) == 3
    assert "actual_closed_realized_pnl_cents" not in report
    assert "actual_closed_realized_pnl_cents" not in client.get("/api/reports").json()
    assert client.post(f"/api/real/alerts/{first_id}/skip").status_code == 409


def test_real_skip_buy_or_sell_never_leaves_system_managed(tmp_path: Path):
    from assistant.storage import alert

    state = tmp_path / "state.sqlite"
    init_db(state)
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    buy_id = _real_buy_alert(state)
    assert client.post(f"/api/real/alerts/{buy_id}/skip").status_code == 200
    assert client.get("/api/real").json()["positions"] == []
    assert client.post(f"/api/real/alerts/{buy_id}/complete").status_code == 409

    with connect(state) as db:
        watch_id = db.execute("SELECT id FROM watchlist LIMIT 1").fetchone()[0]
        day = datetime.now(SHANGHAI).date().isoformat()
        cursor = db.execute("""
            INSERT INTO real_positions(
              code,watch_id,entry_date,entry_price_cents,target_pct,
              initial_stop_cents,prior_peak_cents,peak_asof_date,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
        """, ("sh.600001", watch_id, day, 1000, "0.10", 950, 1000,
              day, datetime.now().isoformat()))
        position_id = cursor.lastrowid
        alert(db, "real_sell:test_skip", "actual_sell", "action", "首次止盈",
              "卖出约半仓", "sh.600001", reference_price_cents=1100,
              real_position_id=position_id, action_kind="partial_profit")
        sell_id = db.execute("SELECT id FROM alerts WHERE event_key='real_sell:test_skip'").fetchone()[0]
    assert client.post(f"/api/real/alerts/{sell_id}/skip").status_code == 200
    with connect(state) as db:
        pos = db.execute("SELECT * FROM real_positions WHERE id=?", (position_id,)).fetchone()
        assert pos["management_mode"] == "user_managed"
        assert pos["partial_done"] == 0
    assert client.post(f"/api/real/alerts/{sell_id}/complete").status_code == 409
    assert client.post(f"/api/real/positions/{position_id}/finish-manual").status_code == 200
    assert client.get("/api/real").json()["positions"] == []
    history = client.get("/api/real/positions/history").json()["positions"][0]
    assert history["estimated_gross_return_pct"] is None
    assert [action["action_kind"] for action in history["transactions"]] == ["partial_profit", "manual_finish"]


def test_real_pending_count_tracks_actions_and_unread_reviews(tmp_path: Path):
    from assistant.storage import alert

    state = tmp_path / "state.sqlite"
    init_db(state)
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    buy_id = _real_buy_alert(state)
    assert client.get("/api/real").json()["pending_alert_count"] == 1
    with connect(state) as db:
        alert(db, "actual_review:test", "action_review", "warning",
              "人工核对", "请核对行情", "sh.600001")
        review_id = db.execute(
            "SELECT id FROM alerts WHERE event_key='actual_review:test'").fetchone()[0]
    board = client.get("/api/real").json()
    assert board["pending_alert_count"] == len(board["alerts"]) == 2
    assert client.post(f"/api/alerts/{review_id}/read").status_code == 200
    assert client.get("/api/real").json()["pending_alert_count"] == 1
    assert client.post(f"/api/real/alerts/{buy_id}/skip").status_code == 200
    assert client.get("/api/real").json()["pending_alert_count"] == 0


def test_virtual_closed_positions_include_net_result_and_each_leg(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    _real_buy_alert(state)
    with connect(state) as db:
        watch_id, settings_id = db.execute(
            "SELECT id,settings_id FROM watchlist LIMIT 1").fetchone()
        position_id = db.execute("""
            INSERT INTO positions(
              watch_id,code,entry_date,entry_price_cents,bought_shares,
              remaining_shares,target_pct,initial_stop_cents,prior_peak_cents,
              peak_asof_date,hold_days,reserved_cents,buy_cash_cents,
              closed_pnl_cents,status,settings_id,created_at,closed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (watch_id, "sh.600001", "2026-10-12", 1000, 100,
              0, "0.10", 950, 1000, "2026-10-12", 3, 100000, 100000,
              8500, "closed", settings_id, "2026-10-12T09:40:00+08:00",
              "2026-10-14T10:00:00+08:00")).lastrowid
        for side, shares, gross, cash, reason, at in (
            ("buy", 100, 1000, -100000, "strict_crossing", "2026-10-12T09:40:00+08:00"),
            ("sell", 100, 1085, 108500, "expiry", "2026-10-14T10:00:00+08:00"),
        ):
            db.execute("""
                INSERT INTO virtual_legs(position_id,side,shares,gross_price_cents,
                  cash_delta_cents,fee_cents,quote_event_key,reason,at)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (position_id, side, shares, gross, cash, 0, side, reason, at))
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    history = client.get("/api/virtual/positions/history?page_size=1").json()
    assert history["total"] == history["pages"] == 1
    position = history["positions"][0]
    assert position["buy_time"] == "2026-10-12T09:40:00+08:00"
    assert position["hold_days"] == 3
    assert position["net_return_pct"] == 0.085
    assert [leg["side"] for leg in position["transactions"]] == ["buy", "sell"]
    assert client.get("/api/virtual/positions/history?page=0").status_code == 400


def test_virtual_open_position_pnl_includes_partial_sale_and_valuation(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    _real_buy_alert(state)
    with connect(state) as db:
        watch_id, settings_id = db.execute(
            "SELECT id,settings_id FROM watchlist LIMIT 1").fetchone()
        position_id = db.execute("""
            INSERT INTO positions(
              watch_id,code,entry_date,entry_price_cents,bought_shares,
              remaining_shares,target_pct,initial_stop_cents,prior_peak_cents,
              peak_asof_date,reserved_cents,buy_cash_cents,status,settings_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (watch_id, "sh.600001", "2026-10-12", 1000, 100,
              50, "0.10", 950, 1100, "2026-10-13", 100025, 100025,
              "open", settings_id, "2026-10-12T09:40:00+08:00")).lastrowid
        for side, shares, gross, cash, fee, at in (
            ("buy", 100, 1000, -100025, 25, "2026-10-12T09:40:00+08:00"),
            ("sell", 50, 1100, 54940, 60, "2026-10-13T10:00:00+08:00"),
        ):
            db.execute("""
                INSERT INTO virtual_legs(position_id,side,shares,gross_price_cents,
                  cash_delta_cents,fee_cents,quote_event_key,reason,at)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (position_id, side, shares, gross, cash, fee, side,
                  "strict_crossing" if side == "buy" else "partial_profit", at))
        db.execute("""
            INSERT INTO quotes(code,provider,source_time,received_at,last_cents,
                               open_cents,healthy)
            VALUES(?,?,?,?,?,?,?)
        """, ("sh.600001", "test", "2026-10-14T10:00:00+08:00",
              "2026-10-14T10:00:01+08:00", 980, 1000, 1))
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    position = client.get("/api/virtual").json()["positions"][0]
    assert position["current_pnl_pct"] == (54940 + 50 * 980 - 100025) / 100025
    assert position["latest_price_cents"] == 980
    assert position["pnl_quote_time"] == "2026-10-14T10:00:00+08:00"
    with connect(state) as db:
        db.execute("UPDATE quotes SET healthy=0 WHERE code='sh.600001'")
    position = client.get("/api/virtual").json()["positions"][0]
    assert position["current_pnl_pct"] is None
    assert position["latest_price_cents"] is None
    assert position["pnl_quote_time"] is None


def test_real_history_is_paginated_and_includes_manual_finish(tmp_path: Path):
    from assistant.storage import alert, append_event

    state = tmp_path / "state.sqlite"
    init_db(state)
    with connect(state) as db:
        for number in range(45):
            key = f"real_buy:history:{number}"
            alert(db, key, "real_buy", "action", "买入提醒", "记录价 10 元",
                  "sh.600001", reference_price_cents=1000, action_kind="buy")
            db.execute("""
                UPDATE alerts SET action_status='completed',acted_at=? WHERE event_key=?
            """, (f"2026-04-02T10:00:{number:02d}+08:00", key))
        append_event(db, "real_manual_finish:test", "real_manual_finish",
                     "sh.600001", None, None, {})
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    first = client.get("/api/real/report").json()
    second = client.get("/api/real/report?page=2").json()
    third = client.get("/api/real/report?page=3").json()
    assert [len(x["actions"]) for x in (first, second, third)] == [20, 20, 6]
    assert first["total"] == 46 and first["pages"] == 3
    assert any(row["action_kind"] == "manual_finish" for row in first["actions"])
    assert client.get("/api/real").json()["alerts"] == []
    keys = [(row["kind"], row["id"]) for page in (first, second, third)
            for row in page["actions"]]
    assert len(set(keys)) == 46
    assert client.get("/api/real/report?page=0").status_code == 400


def test_legacy_real_position_import_runs_once(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    with connect(state) as db:
        db.execute("""
            INSERT INTO actual_positions(
              code,entry_date,entry_price_cents,bought_shares,remaining_shares,
              prior_peak_cents,peak_asof_date,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
        """, ("sh.600001", "2026-04-02", 1000, 200, 200, 1000,
              "2026-04-02", "2026-04-02T10:00:00+08:00"))
        db.execute("DELETE FROM system_status WHERE key='legacy_actual_import'")
    init_db(state)
    with connect(state) as db:
        row = db.execute("SELECT * FROM real_positions").fetchone()
        assert row["legacy_actual_id"] == 1
        assert row["management_mode"] == "user_managed"
        db.execute("UPDATE real_positions SET status='closed' WHERE id=?", (row["id"],))
    init_db(state)
    with connect(state) as db:
        assert db.execute("SELECT COUNT(*) FROM real_positions").fetchone()[0] == 1
        assert db.execute("SELECT status FROM real_positions").fetchone()[0] == "closed"


def test_boards_are_separate_and_focus_is_hidden(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    today = datetime.now(SHANGHAI).date().isoformat()
    with connect(state) as db:
        db.execute("""
            INSERT INTO focus_watchlist(
              entry_date,signal_date,code,rank,ret5,ret20,atr14,q_close,
              raw_close,liquidity20,buy_limit_cents,target_pct,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (today, today, "sh.600006", 6, -0.13, -0.12, 0.2, 9.0,
              9.0, 200_000_000, 864, "0.10", datetime.now().isoformat()))
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    payload = client.get("/api/today").json()
    assert payload["focus"] == []
    assert client.get("/api/focus").json() == []
    virtual = client.get("/api/virtual").json()
    real = client.get("/api/real").json()
    assert virtual["capital_cents"] == 100_000_000
    assert virtual["total_asset_cents"] == virtual["cash_cents"]
    assert virtual["settings"]["commission_rate"] == "0.00025"
    assert virtual["settings"]["slippage_rate"] == "0"
    assert "capital_cents" not in real
    assert real["watchlist"] == []
    assert "actual_closed_realized_pnl_cents" not in client.get("/api/virtual/report").json()
    assert "virtual_realized_pnl_cents" not in client.get("/api/real/report").json()
    assert "OPENAI-A" in client.get("/").text
