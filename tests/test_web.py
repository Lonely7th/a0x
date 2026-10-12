from __future__ import annotations

from pathlib import Path
from datetime import datetime

from fastapi.testclient import TestClient

from assistant.config import Config, SHANGHAI
from assistant.storage import connect, init_db
from assistant.web import create_app


def test_dashboard_actual_fills_and_report(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    config = Config(state_db=state, market_db=tmp_path / "unused.duckdb")
    client = TestClient(create_app(config))
    assert client.get("/").status_code == 200
    assert client.get("/api/today").status_code == 200
    buy = {
        "side": "buy", "code": "sh.600001", "shares": 200, "price": "10.00",
        "fee": "5.00", "happened_at": "2026-04-02T10:00:00+08:00",
        "idempotency_key": "buy-fixture-0001",
    }
    response = client.post("/api/actual-fills", json=buy)
    assert response.status_code == 200, response.text
    position_id = response.json()["position_id"]
    assert client.post("/api/actual-fills", json=buy).json()["duplicate"]
    sell = {
        "side": "sell", "position_id": position_id, "shares": 200,
        "price": "11.00", "fee": "6.00",
        "happened_at": "2026-04-03T10:00:00+08:00",
        "idempotency_key": "sell-fixture-0001",
    }
    response = client.post("/api/actual-fills", json=sell)
    assert response.status_code == 200, response.text
    fills = client.get(f"/api/positions/actual/{position_id}/fills").json()
    assert [fill["side"] for fill in fills] == ["buy", "sell"]
    report = client.get("/api/reports").json()
    assert report["actual_closed_realized_pnl_cents"] == 18_900
    assert report["actual_account_equity"] is None
    with connect(state) as db:
        assert db.execute("SELECT COUNT(*) FROM actual_fills").fetchone()[0] == 2
        assert db.execute(
            "SELECT status FROM actual_positions WHERE id=?", (position_id,)
        ).fetchone()[0] == "closed"


def test_same_day_actual_sale_rejected(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    buy = {
        "side": "buy", "code": "sz.000001", "shares": 100, "price": "8.00",
        "happened_at": "2026-04-02T10:00:00+08:00",
        "idempotency_key": "buy-fixture-0002",
    }
    position_id = client.post("/api/actual-fills", json=buy).json()["position_id"]
    sell = {
        "side": "sell", "position_id": position_id, "shares": 100, "price": "8.20",
        "happened_at": "2026-04-02T14:00:00+08:00",
        "idempotency_key": "sell-fixture-0002",
    }
    assert client.post("/api/actual-fills", json=sell).status_code == 400


def test_naive_actual_fill_time_rejected(tmp_path: Path):
    state = tmp_path / "state.sqlite"
    init_db(state)
    client = TestClient(create_app(Config(state_db=state, market_db=tmp_path / "unused.duckdb")))
    response = client.post("/api/actual-fills", json={
        "side": "buy", "code": "sz.000001", "shares": 100, "price": "8.00",
        "happened_at": "2026-04-02T10:00:00",
        "idempotency_key": "naive-time-fixture",
    })
    assert response.status_code == 400


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
    assert virtual["settings"]["commission_rate"] == "0.00025"
    assert virtual["settings"]["slippage_rate"] == "0"
    assert "capital_cents" not in real
    assert real["watchlist"] == []
    assert "actual_closed_realized_pnl_cents" not in client.get("/api/virtual/report").json()
    assert "virtual_realized_pnl_cents" not in client.get("/api/real/report").json()
    assert "OPENAI-A" in client.get("/").text
