from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.templating import Jinja2Templates

from .config import Config, SHANGHAI, now_utc_iso
from .money import cents
from .storage import (alert, append_event, apply_real_strategy, available_budget, cash_balance,
                      connect, next_effective_calendar_date, real_strategy_for,
                      setting_for, transaction)
from .strategy import first_target_cents, initial_stop_cents, stop_distance

ROOT = Path(__file__).resolve().parents[1]


def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _event_cursor(db, after: int, last_event_id: str | None,
                  explicit_after: bool) -> int:
    if last_event_id and last_event_id.isdigit():
        return max(after, int(last_event_id))
    if explicit_after:
        return after
    # The initial page load already fetches current state. Only stream new changes.
    return db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]


def _write_guard(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin != expected:
            raise HTTPException(403, "Cross-origin write refused")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site write refused")


class SettingsIn(BaseModel):
    capital: Decimal = Field(gt=0)
    budget: Decimal = Field(gt=0)
    commission_rate: Decimal = Field(ge=0)
    minimum_commission: Decimal = Field(ge=0)
    transfer_rate: Decimal = Field(ge=0)
    stamp_rate: Decimal = Field(ge=0)
    slippage_rate: Decimal = Field(ge=0)


class RealStrategyIn(BaseModel):
    strategy_id: str


def _virtual_tables(account: str) -> tuple[str, str]:
    if account == "stable":
        return "positions", "virtual_legs"
    if account == "aggressive":
        return "aggressive_positions", "aggressive_legs"
    raise HTTPException(400, "Unknown virtual account")


def _real_switch_allowed(now: datetime, is_session: bool) -> bool:
    clock = (now.hour, now.minute)
    return not (is_session and (9, 30) <= clock < (15, 0))


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="A0x", docs_url="/api/docs")
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request, "dashboard_v2.html", {"title": "A0x",
                                    "observe_only": config.observe_only}
        )

    @app.get("/m", response_class=HTMLResponse)
    def mobile_virtual_board(request: Request):
        return templates.TemplateResponse(request, "mobile_virtual.html")

    @app.get("/api/health")
    def health():
        with connect(config.state_db) as db:
            status = {r["key"]: {"value": json.loads(r["value"]), "at": r["updated_at"]}
                      for r in db.execute("SELECT * FROM system_status")}
            day = datetime.now(SHANGHAI).date().isoformat()
            quote_stats = _rows(db, """
                SELECT provider,COUNT(*) AS samples,
                  COUNT(DISTINCT source_time || ':' || code) AS new_symbol_timestamps,
                  ROUND(AVG(age_ms)) AS mean_age_ms,
                  MAX(age_ms) AS max_age_ms
                FROM quote_samples WHERE substr(source_time,1,10)=?
                GROUP BY provider ORDER BY provider
            """, (day,))
        heartbeat = status.get("worker_heartbeat")
        age = None
        if heartbeat:
            age = (datetime.fromisoformat(now_utc_iso())
                   - datetime.fromisoformat(heartbeat["at"])).total_seconds()
        return {"status": status, "worker_age_seconds": age,
                "worker_healthy": age is not None and age <= 35,
                "observe_only": heartbeat["value"]["observe_only"] if heartbeat else config.observe_only,
                "quote_stats_today": quote_stats}

    @app.get("/api/quote-quality")
    def quote_quality(days: int = 5):
        if not 1 <= days <= 30:
            raise HTTPException(400, "days must be between 1 and 30")
        cutoff = (datetime.now(SHANGHAI).date() - timedelta(days=days - 1)).isoformat()
        with connect(config.state_db) as db:
            polls = _rows(db, """
                SELECT trading_date AS day,provider,COUNT(*) AS polls,
                  SUM(requested) AS requested_quotes,SUM(received) AS received_quotes,
                  SUM(healthy) AS fully_healthy_polls
                FROM quote_polls WHERE trading_date>=? AND requested>0
                GROUP BY trading_date,provider ORDER BY trading_date DESC,provider
            """, (cutoff,))
            samples = _rows(db, """
                SELECT substr(source_time,1,10) AS day,provider,
                  COUNT(*) AS samples,COUNT(DISTINCT code) AS symbols,
                  COUNT(DISTINCT code || ':' || source_time) AS distinct_updates,
                  ROUND(AVG(age_ms)) AS mean_age_ms,MAX(age_ms) AS max_age_ms
                FROM quote_samples WHERE received_at>=?
                GROUP BY day,provider ORDER BY day DESC,provider
            """, (cutoff,))
        return {"days": days, "polls": polls, "samples": samples,
                "note": "仅记录本程序收到的快照；应与券商终端逐笔抽查，不能仅凭覆盖率启用交易提醒"}

    @app.get("/api/today")
    def today():
        current = datetime.now(SHANGHAI).date().isoformat()
        with connect(config.state_db) as db:
            setting = setting_for(db, current)
            watch = _rows(db, """SELECT w.*,s.budget_cents AS settings_budget_cents
                FROM watchlist AS w JOIN settings AS s ON s.id=w.settings_id
                WHERE w.entry_date=? ORDER BY w.rank""", (current,))
            focus = []
            latest = _rows(db, "SELECT * FROM quotes ORDER BY code")
            alerts = _rows(db, "SELECT * FROM alerts ORDER BY id DESC LIMIT 30")
            open_pos = _rows(db, "SELECT * FROM positions WHERE status='open' ORDER BY entry_date")
            heartbeat = db.execute(
                "SELECT value FROM system_status WHERE key='worker_heartbeat'"
            ).fetchone()
            return {"date": current, "watchlist": watch, "focus": focus, "quotes": latest,
                    "alerts": alerts, "positions": open_pos,
                    "budget_available_cents": available_budget(db, current),
                    "cash_cents": cash_balance(db, current),
                    "capital_cents": setting["capital_cents"],
                    "budget_cents": setting["budget_cents"],
                    "signal_candidate_count": (watch[0]["candidate_count"]
                                                if watch else None),
                    "planned_order_budget_cents": (watch[0]["order_budget_cents"]
                                                   if watch else None),
                    "observe_only": (json.loads(heartbeat[0]).get("observe_only")
                                     if heartbeat else config.observe_only)}

    @app.get("/api/watchlist")
    def watchlist(on_date: str | None = None):
        day = on_date or datetime.now(SHANGHAI).date().isoformat()
        with connect(config.state_db) as db:
            return _rows(db, """SELECT w.*,s.budget_cents AS settings_budget_cents
                FROM watchlist AS w JOIN settings AS s ON s.id=w.settings_id
                WHERE w.entry_date=? ORDER BY w.rank""", (day,))

    @app.get("/api/focus")
    def focus(on_date: str | None = None):
        return []

    @app.get("/api/virtual")
    def virtual_board(account: str = "stable"):
        positions_table, legs_table = _virtual_tables(account)
        day = datetime.now(SHANGHAI).date().isoformat()
        with connect(config.state_db) as db:
            setting = setting_for(db, day)
            if account == "aggressive":
                setting = {**setting, "capital_cents": 100_000_000}
            positions = _rows(db, f"""
                SELECT * FROM {positions_table} WHERE status='open' ORDER BY entry_date,id
            """)
            quote_rows = {row["code"]: row for row in _rows(db, f"""
                SELECT q.code,q.last_cents,q.source_time,q.healthy FROM quotes AS q
                WHERE q.code IN (SELECT code FROM {positions_table} WHERE status='open')
            """)}
            quotes = {code: row["last_cents"] for code, row in quote_rows.items()
                      if row["last_cents"] > 0}
            trade_flows = {row["position_id"]: row for row in _rows(db, f"""
                SELECT l.position_id,
                  SUM(CASE WHEN l.side='sell' THEN l.cash_delta_cents ELSE 0 END)
                    AS sold_proceeds_cents,
                  MIN(CASE WHEN l.side='buy' THEN l.at END) AS buy_time
                FROM {legs_table} AS l
                JOIN {positions_table} AS p ON p.id=l.position_id
                WHERE p.status='open' GROUP BY l.position_id
            """)}
            for position in positions:
                quote = quote_rows.get(position["code"])
                flow = trade_flows.get(position["id"])
                position["buy_time"] = flow["buy_time"] if flow else None
                valid_quote = (quote and quote["healthy"] and quote["last_cents"] > 0
                               and flow and flow["buy_time"]
                               and datetime.fromisoformat(quote["source_time"])
                               >= datetime.fromisoformat(flow["buy_time"]))
                position["current_pnl_pct"] = (
                    (flow["sold_proceeds_cents"]
                     + position["remaining_shares"] * quote["last_cents"]
                     - position["buy_cash_cents"]) / position["buy_cash_cents"]
                    if valid_quote and position["buy_cash_cents"] > 0 else None)
                position["latest_price_cents"] = quote["last_cents"] if valid_quote else None
                position["pnl_quote_time"] = quote["source_time"] if valid_quote else None
            unquoted = [position["code"] for position in positions
                        if position["code"] not in quotes]
            market_value = sum(position["remaining_shares"] *
                               quotes.get(position["code"], position["entry_price_cents"])
                               for position in positions)
            cash = cash_balance(db, day, account)
            watchlist = _rows(db, """
                SELECT * FROM watchlist WHERE entry_date=? ORDER BY rank
            """, (day,))
            if account == "aggressive":
                orders = {row["watch_id"]: row for row in _rows(db, """
                    SELECT * FROM aggressive_orders
                    WHERE watch_id IN (SELECT id FROM watchlist WHERE entry_date=?)
                """, (day,))}
                watchlist = [dict(w, **{k: value for k, value in orders[w["id"]].items()
                                       if k != "watch_id"}) for w in watchlist
                             if w["id"] in orders]
            for watch in watchlist:
                watch["first_target_cents"] = (first_target_cents(
                    watch["buy_limit_cents"], watch["target_pct"])
                    if watch["target_pct"] is not None else None)
            return {
                "account": account,
                "date": day,
                "capital_cents": setting["capital_cents"],
                "available_budget_cents": available_budget(db, day, account),
                "cash_cents": cash,
                "positions_market_value_cents": market_value,
                "total_asset_cents": cash + market_value,
                "valuation_fallback_codes": unquoted,
                "settings": setting,
                "watchlist": watchlist,
                "positions": positions,
                "alerts": _rows(db, """
                    SELECT * FROM alerts
                    WHERE kind IN ('virtual_buy','virtual_sell','opportunity_only',
                                   'entry_blocked','sell_blocked',
                                   'data_failure','action_review')
                      AND event_key NOT LIKE 'actual_%'
                      AND kind != 'focus_triggered'
                    ORDER BY id DESC LIMIT 100
                """) if account == "stable" else [],
            }

    @app.get("/api/real")
    def real_board():
        now = datetime.now(SHANGHAI)
        day = now.date().isoformat()
        with connect(config.state_db) as db:
            calendar_row = db.execute("""
                SELECT value,updated_at FROM system_status
                WHERE key='trading_calendar_today'
            """).fetchone()
            heartbeat_row = db.execute("""
                SELECT updated_at FROM system_status WHERE key='worker_heartbeat'
            """).fetchone()
            calendar = json.loads(calendar_row["value"]) if calendar_row else {}
            calendar_fresh = bool(calendar_row and calendar.get("date") == day and
                heartbeat_row and
                (datetime.fromisoformat(now_utc_iso()) -
                 datetime.fromisoformat(heartbeat_row["updated_at"])).total_seconds() <= 35)
            can_switch = calendar_fresh and _real_switch_allowed(
                now, bool(calendar.get("is_session")))
            positions = _rows(db, """
                SELECT p.*,
                  (SELECT a.created_at FROM alerts AS a
                   WHERE a.real_position_id=p.id AND a.kind='real_buy'
                     AND a.action_status='completed'
                   ORDER BY a.id LIMIT 1) AS buy_signal_at
                FROM real_positions AS p WHERE p.status='open'
                ORDER BY p.entry_date,p.id
            """)
            for position in positions:
                position["first_target_cents"] = (
                    first_target_cents(position["entry_price_cents"], position["target_pct"])
                    if position["target_pct"] is not None and not position["no_partial"] else None
                )
            watchlist = _rows(db, """
                    SELECT w.id,w.entry_date,w.signal_date,w.code,w.rank,
                      w.ret5,w.ret20,w.buy_limit_cents,
                      CASE WHEN w.real_no_partial THEN NULL
                           ELSE COALESCE(w.real_target_pct,w.target_pct) END AS target_pct,
                      w.real_no_partial AS no_partial,w.real_strategy_id AS strategy_id,
                      w.planned_shares,w.candidate_count,w.status AS watch_status,
                      w.real_status,
                      COALESCE(w.order_budget_cents,s.budget_cents) AS order_budget_cents,
                      e.at AS buy_signal_at,a.action_status AS buy_action_status
                    FROM watchlist AS w JOIN settings AS s ON s.id=w.settings_id
                    LEFT JOIN events AS e
                      ON e.event_key='real_buy:' || w.entry_date || ':' || w.code
                    LEFT JOIN alerts AS a ON a.event_key=e.event_key
                    WHERE w.entry_date=? ORDER BY w.rank
                """, (day,))
            for watch in watchlist:
                watch["first_target_cents"] = (first_target_cents(
                    watch["buy_limit_cents"], watch["target_pct"])
                    if watch["target_pct"] is not None and not watch["no_partial"] else None)
            pending_filter = """
                (kind='real_buy' AND action_status='pending'
                 AND substr(event_key,10,10)=?)
                OR (kind='actual_sell' AND action_status='pending')
                OR (event_key LIKE 'actual_%'
                    AND kind NOT IN ('real_buy','actual_sell')
                    AND read_at IS NULL)
            """
            pending_count = db.execute(
                f"SELECT COUNT(*) FROM alerts WHERE {pending_filter}", (day,)
            ).fetchone()[0]
            return {
                "date": day,
                "can_switch_strategy": can_switch,
                "strategy_id": real_strategy_for(db, day),
                "watchlist": watchlist,
                "positions": positions,
                "pending_alert_count": pending_count,
                "alerts": _rows(db, f"""
                    SELECT * FROM alerts WHERE {pending_filter}
                    ORDER BY CASE WHEN kind IN ('real_buy','actual_sell')
                                  THEN 0 ELSE 1 END,id DESC LIMIT 100
                """, (day,)),
            }

    @app.get("/api/quotes")
    def quotes():
        with connect(config.state_db) as db:
            return _rows(db, "SELECT * FROM quotes ORDER BY code")

    @app.get("/api/market-indices")
    def market_indices():
        now = datetime.now(SHANGHAI)
        with connect(config.state_db) as db:
            rows = _rows(db, "SELECT * FROM market_indices ORDER BY code")
            calendar_row = db.execute("""
                SELECT value FROM system_status WHERE key='trading_calendar_today'
            """).fetchone()
        calendar = json.loads(calendar_row[0]) if calendar_row else {}
        is_session = calendar.get("date") == now.date().isoformat() and calendar.get("is_session")
        if not is_session:
            market_mode = "休市"
        elif (now.hour, now.minute) < (9, 30):
            market_mode = "盘前"
        elif (now.hour, now.minute) < (11, 30) or (13, 0) <= (now.hour, now.minute) < (15, 0):
            market_mode = "交易中"
        elif (now.hour, now.minute) < (13, 0):
            market_mode = "午间休市"
        else:
            market_mode = "已收盘"
        for row in rows:
            seen = datetime.fromisoformat(row["source_time"])
            row["stale"] = (seen.date() != now.date() or
                            (market_mode == "交易中" and
                             (now - seen).total_seconds() > 120))
            row["change_pct"] = (row["last_cents"] / row["preclose_cents"] - 1
                                  if row["preclose_cents"] > 0 else None)
        return {"indices": rows, "asof": now.isoformat(), "market_mode": market_mode}

    @app.post("/api/real/strategy")
    def switch_real_strategy(data: RealStrategyIn, request: Request):
        _write_guard(request)
        if data.strategy_id not in ("stable", "aggressive"):
            raise HTTPException(400, "Unknown strategy")
        with connect(config.state_db) as db, transaction(db):
            now = datetime.now(SHANGHAI)
            day = now.date().isoformat()
            calendar = db.execute("""
                SELECT value,updated_at FROM system_status
                WHERE key='trading_calendar_today'
            """).fetchone()
            heartbeat = db.execute("""
                SELECT updated_at FROM system_status WHERE key='worker_heartbeat'
            """).fetchone()
            if calendar is None:
                raise HTTPException(503, "Trading calendar is not ready")
            detail = json.loads(calendar["value"])
            heartbeat_age = ((datetime.fromisoformat(now_utc_iso()) -
                              datetime.fromisoformat(heartbeat["updated_at"])).total_seconds()
                             if heartbeat else float("inf"))
            if detail.get("date") != day or heartbeat_age > 35:
                raise HTTPException(503, "Trading calendar is stale")
            if not _real_switch_allowed(now, bool(detail.get("is_session"))):
                raise HTTPException(409, "Trading strategy cannot change during market hours")
            updated_plans = apply_real_strategy(db, day, data.strategy_id)
            append_event(db, f"real_strategy:{uuid4().hex}", "real_strategy_changed",
                         None, None, None,
                         {"effective_date": day, "strategy_id": data.strategy_id,
                          "updated_plans": updated_plans})
        return {"strategy_id": data.strategy_id, "effective_date": day,
                "updated_plans": updated_plans}

    @app.get("/api/positions/virtual")
    def virtual_positions(status: str = "open", account: str = "stable"):
        positions_table, _ = _virtual_tables(account)
        if status not in ("open", "closed", "all"):
            raise HTTPException(400, "Invalid status")
        with connect(config.state_db) as db:
            sql = f"SELECT * FROM {positions_table}"
            if status != "all":
                sql += " WHERE status=?"
                return _rows(db, sql + " ORDER BY id DESC", (status,))
            return _rows(db, sql + " ORDER BY id DESC")

    @app.get("/api/positions/actual")
    def actual_positions(status: str = "open"):
        if status not in ("open", "closed", "all"):
            raise HTTPException(400, "Invalid status")
        with connect(config.state_db) as db:
            sql = "SELECT * FROM real_positions"
            if status != "all":
                return _rows(db, sql + " WHERE status=? ORDER BY id DESC", (status,))
            return _rows(db, sql + " ORDER BY id DESC")

    @app.get("/api/alerts")
    def alerts(limit: int = 100):
        count = max(1, min(limit, 500))
        with connect(config.state_db) as db:
            return _rows(db, "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (count,))

    @app.post("/api/alerts/{alert_id}/read")
    def read_alert(alert_id: int, request: Request):
        _write_guard(request)
        with connect(config.state_db) as db, transaction(db):
            db.execute("UPDATE alerts SET read_at=COALESCE(read_at,?) WHERE id=?",
                       (now_utc_iso(), alert_id))
            if not db.execute("SELECT 1 FROM alerts WHERE id=?", (alert_id,)).fetchone():
                raise HTTPException(404, "Alert not found")
        return {"ok": True}

    def handle_real_alert(alert_id: int, choice: str, request: Request):
        _write_guard(request)
        with connect(config.state_db) as db, transaction(db):
            item = db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
            if item is None or item["kind"] not in ("real_buy", "actual_sell"):
                raise HTTPException(404, "Real trading alert not found")
            if item["action_status"] == choice:
                return {"ok": True, "duplicate": True,
                        "position_id": item["real_position_id"]}
            if item["action_status"] != "pending" or not item["reference_price_cents"]:
                raise HTTPException(409, "This alert is no longer actionable")
            position_id = item["real_position_id"]
            if item["kind"] == "real_buy":
                event = db.execute("SELECT watch_id FROM events WHERE event_key=?",
                                   (item["event_key"],)).fetchone()
                watch = db.execute("SELECT * FROM watchlist WHERE id=?",
                                   (event["watch_id"],)).fetchone() if event else None
                if watch is None or watch["code"] != item["code"]:
                    raise HTTPException(409, "Buy alert has no matching plan")
                if watch["entry_date"] != datetime.now(SHANGHAI).date().isoformat():
                    raise HTTPException(409, "Buy alert expired after its trading day")
                if choice == "completed":
                    if db.execute("SELECT 1 FROM real_positions WHERE code=? AND status='open'",
                                  (item["code"],)).fetchone():
                        raise HTTPException(409, "This stock already has an open real position")
                    entry = item["reference_price_cents"]
                    distance = stop_distance(watch["atr14"], watch["q_close"],
                                             entry, watch["raw_close"])
                    stop = initial_stop_cents(entry, distance)
                    db.execute("""
                        INSERT INTO real_positions(
                          code,watch_id,entry_date,entry_price_cents,target_pct,
                          initial_stop_cents,prior_peak_cents,peak_asof_date,created_at,
                          strategy_id,no_partial
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """, (item["code"], watch["id"], watch["entry_date"], entry,
                          (watch["real_target_pct"] or watch["target_pct"])
                          if not watch["real_no_partial"] else None,
                          stop, entry, watch["entry_date"],
                          now_utc_iso(), watch["real_strategy_id"],
                          watch["real_no_partial"]))
                    position_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                pos = db.execute("""
                    SELECT * FROM real_positions WHERE id=? AND status='open'
                      AND management_mode='system'
                """, (position_id,)).fetchone()
                if pos is None:
                    raise HTTPException(409, "Managed real position is no longer open")
                if choice == "skipped":
                    db.execute("UPDATE real_positions SET management_mode='user_managed' WHERE id=?",
                               (position_id,))
                elif item["action_kind"] == "partial_profit":
                    if pos["partial_done"]:
                        raise HTTPException(409, "First profit sale was already confirmed")
                    db.execute("UPDATE real_positions SET partial_done=1 WHERE id=?",
                               (position_id,))
                elif item["action_kind"] in ("stop", "expiry"):
                    db.execute("""
                        UPDATE real_positions SET status='closed',closed_at=? WHERE id=?
                    """, (now_utc_iso(), position_id))
                else:
                    raise HTTPException(409, "Unknown sell action")
            acted_at = now_utc_iso()
            db.execute("""
                UPDATE alerts SET action_status=?,acted_at=?,read_at=COALESCE(read_at,?),
                  real_position_id=? WHERE id=?
            """, (choice, acted_at, acted_at, position_id, alert_id))
            append_event(db, f"real_action:{alert_id}", "real_alert_action", item["code"],
                         watch["id"] if item["kind"] == "real_buy" else pos["watch_id"],
                         position_id, {"choice": choice, "alert_id": alert_id,
                                       "reference_price_cents": item["reference_price_cents"],
                                       "action_kind": item["action_kind"]})
            return {"ok": True, "duplicate": False, "position_id": position_id}

    @app.post("/api/real/alerts/{alert_id}/complete")
    def complete_real_alert(alert_id: int, request: Request):
        return handle_real_alert(alert_id, "completed", request)

    @app.post("/api/real/alerts/{alert_id}/skip")
    def skip_real_alert(alert_id: int, request: Request):
        return handle_real_alert(alert_id, "skipped", request)

    @app.post("/api/real/positions/{position_id}/finish-manual")
    def finish_manual_real_position(position_id: int, request: Request):
        _write_guard(request)
        with connect(config.state_db) as db, transaction(db):
            pos = db.execute("SELECT * FROM real_positions WHERE id=?",
                             (position_id,)).fetchone()
            if pos is None:
                raise HTTPException(404, "Real position not found")
            if pos["management_mode"] != "user_managed":
                raise HTTPException(409, "Only user-managed positions can be finished here")
            if pos["status"] == "closed":
                return {"ok": True, "duplicate": True}
            db.execute("UPDATE real_positions SET status='closed',closed_at=? WHERE id=?",
                       (now_utc_iso(), position_id))
            append_event(db, f"real_manual_finish:{position_id}", "real_manual_finish",
                         pos["code"], pos["watch_id"], position_id, {})
        return {"ok": True, "duplicate": False}

    @app.get("/api/trades/{trade_id}/events")
    def trade_events(trade_id: int):
        with connect(config.state_db) as db:
            position = db.execute("SELECT 1 FROM positions WHERE id=?", (trade_id,)).fetchone()
            if not position:
                raise HTTPException(404, "Trade not found")
            return {
                "legs": _rows(db, "SELECT * FROM virtual_legs WHERE position_id=? ORDER BY id",
                              (trade_id,)),
                "events": _rows(db, "SELECT * FROM events WHERE position_id=? ORDER BY seq",
                                (trade_id,)),
            }

    @app.get("/api/reports")
    def reports():
        day = datetime.now(SHANGHAI).date().isoformat()
        with connect(config.state_db) as db:
            closed = _rows(db, "SELECT * FROM positions WHERE status='closed'")
            pnl = sum(r["closed_pnl_cents"] or 0 for r in closed)
            wins = sum((r["closed_pnl_cents"] or 0) > 0 for r in closed)
            opportunities = db.execute(
                "SELECT COUNT(*) FROM watchlist WHERE status='opportunity_only'"
            ).fetchone()[0]
            return {"virtual_closed_trades": len(closed),
                    "virtual_wins": wins,
                    "virtual_win_rate": wins / len(closed) if closed else None,
                    "virtual_realized_pnl_cents": pnl,
                    "virtual_cash_cents": cash_balance(db, day),
                    "virtual_available_budget_cents": available_budget(db, day),
                    "opportunities_without_virtual_entry": opportunities,
                    "virtual_trades": list(reversed(closed))[-100:][::-1]}

    @app.get("/api/virtual/report")
    def virtual_report(account: str = "stable"):
        if account == "aggressive":
            day = datetime.now(SHANGHAI).date().isoformat()
            with connect(config.state_db) as db:
                closed = _rows(db, "SELECT * FROM aggressive_positions WHERE status='closed'")
                wins = sum((row["closed_pnl_cents"] or 0) > 0 for row in closed)
                return {"virtual_closed_trades": len(closed), "virtual_wins": wins,
                        "virtual_win_rate": wins / len(closed) if closed else None,
                        "virtual_realized_pnl_cents": sum(row["closed_pnl_cents"] or 0
                                                          for row in closed),
                        "virtual_cash_cents": cash_balance(db, day, account),
                        "virtual_available_budget_cents": available_budget(db, day, account),
                        "opportunities_without_virtual_entry": db.execute("""
                            SELECT COUNT(*) FROM aggressive_orders
                            WHERE status='opportunity_only'
                        """).fetchone()[0]}
        _virtual_tables(account)
        report = reports()
        return {key: value for key, value in report.items()
                if key.startswith("virtual_") or key == "opportunities_without_virtual_entry"}

    @app.get("/api/virtual/actions")
    def virtual_actions(page: int = 1, page_size: int = 20,
                        account: str = "stable"):
        positions_table, legs_table = _virtual_tables(account)
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(400, "Invalid history page or page size")
        with connect(config.state_db) as db:
            total = db.execute(f"SELECT COUNT(*) FROM {legs_table}").fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            actions = _rows(db, f"""
                SELECT l.id,l.position_id,p.code,l.at,l.side,l.shares,
                       l.gross_price_cents,l.fee_cents,l.cash_delta_cents,l.reason
                FROM {legs_table} AS l JOIN {positions_table} AS p ON p.id=l.position_id
                ORDER BY l.at DESC,l.id DESC LIMIT ? OFFSET ?
            """, (page_size, (page - 1) * page_size))
            return {"actions": actions, "total": total, "page": page,
                    "page_size": page_size, "pages": pages}

    @app.get("/api/virtual/positions/history")
    def virtual_position_history(page: int = 1, page_size: int = 20,
                                 account: str = "stable"):
        positions_table, legs_table = _virtual_tables(account)
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(400, "Invalid history page or page size")
        with connect(config.state_db) as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM {positions_table} WHERE status='closed'").fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            positions = _rows(db, f"""
                SELECT id,code,entry_date,entry_price_cents,bought_shares,
                       hold_days,buy_cash_cents,closed_pnl_cents,closed_at
                FROM {positions_table} WHERE status='closed'
                ORDER BY closed_at DESC,id DESC LIMIT ? OFFSET ?
            """, (page_size, (page - 1) * page_size))
            for position in positions:
                position["transactions"] = _rows(db, f"""
                    SELECT at,side,shares,gross_price_cents,fee_cents,
                           cash_delta_cents,reason
                    FROM {legs_table} WHERE position_id=? ORDER BY at,id
                """, (position["id"],))
                buys = [leg for leg in position["transactions"] if leg["side"] == "buy"]
                position["buy_time"] = buys[0]["at"] if buys else None
                invested = -sum(leg["cash_delta_cents"] for leg in buys)
                position["net_return_pct"] = (
                    position["closed_pnl_cents"] / invested
                    if invested > 0 and position["closed_pnl_cents"] is not None else None)
            return {"positions": positions, "total": total, "page": page,
                    "page_size": page_size, "pages": pages}

    @app.get("/api/real/positions/history")
    def real_position_history(page: int = 1, page_size: int = 20):
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(400, "Invalid history page or page size")
        with connect(config.state_db) as db:
            total = db.execute("SELECT COUNT(*) FROM real_positions WHERE status='closed'").fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            positions = _rows(db, """
                SELECT id,code,entry_date,entry_price_cents,management_mode,closed_at
                FROM real_positions WHERE status='closed'
                ORDER BY closed_at DESC,id DESC LIMIT ? OFFSET ?
            """, (page_size, (page - 1) * page_size))
            for position in positions:
                actions = _rows(db, """
                    SELECT created_at AS signal_at,acted_at,action_status,
                           action_kind,reference_price_cents
                    FROM alerts WHERE real_position_id=?
                      AND kind IN ('real_buy','actual_sell')
                      AND action_status IN ('completed','skipped')
                    ORDER BY created_at,id
                """, (position["id"],))
                manual = _rows(db, """
                    SELECT at AS signal_at,at AS acted_at,
                           'completed' AS action_status,'manual_finish' AS action_kind,
                           NULL AS reference_price_cents
                    FROM events WHERE position_id=? AND kind='real_manual_finish'
                """, (position["id"],))
                actions.extend(manual)
                actions.sort(key=lambda action: (action["signal_at"] or "", action["acted_at"] or ""))
                position["transactions"] = actions
                buy = next((action for action in actions if action["action_kind"] == "buy"
                            and action["action_status"] == "completed"), None)
                position["buy_time"] = buy["signal_at"] if buy else None
                start = (datetime.fromisoformat(position["buy_time"]).astimezone(SHANGHAI).date()
                         if position["buy_time"] else date.fromisoformat(position["entry_date"]))
                end = (datetime.fromisoformat(position["closed_at"]).astimezone(SHANGHAI).date()
                       if position["closed_at"] else None)
                position["hold_calendar_days"] = (
                    max(0, (end - start).days) if end else None)
                sells = [action for action in actions if action["action_kind"] != "buy"]
                completed = [action for action in sells if action["action_status"] == "completed"]
                final = next((action for action in reversed(completed)
                              if action["action_kind"] not in ("partial_profit", "manual_finish")), None)
                partial = next((action for action in completed
                                if action["action_kind"] == "partial_profit"), None)
                entry = position["entry_price_cents"]
                if (entry > 0 and final and final["reference_price_cents"]
                        and not any(action["action_status"] == "skipped" for action in sells)
                        and not manual):
                    final_return = final["reference_price_cents"] / entry - 1
                    position["estimated_gross_return_pct"] = (
                        (partial["reference_price_cents"] / entry - 1 + final_return) / 2
                        if partial and partial["reference_price_cents"] else final_return)
                else:
                    position["estimated_gross_return_pct"] = None
            return {"positions": positions, "total": total, "page": page,
                    "page_size": page_size, "pages": pages}

    @app.get("/api/real/report")
    def real_report(page: int = 1, page_size: int = 20):
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(400, "Invalid history page or page size")
        with connect(config.state_db) as db:
            total = db.execute("""
                SELECT
                  (SELECT COUNT(*) FROM alerts
                   WHERE kind IN ('real_buy','actual_sell')
                     AND action_status IN ('completed','skipped'))
                  + (SELECT COUNT(*) FROM events WHERE kind='real_manual_finish')
            """).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            actions = _rows(db, """
                SELECT id,kind,code,title,body,created_at,acted_at,action_status,
                       reference_price_cents,real_position_id,action_kind
                FROM (
                    SELECT id,kind,code,title,body,created_at,acted_at,
                           action_status,reference_price_cents,real_position_id,
                           action_kind
                    FROM alerts WHERE kind IN ('real_buy','actual_sell')
                      AND action_status IN ('completed','skipped')
                    UNION ALL
                    SELECT seq AS id,kind,code,'自行清仓' AS title,'' AS body,
                           at AS created_at,at AS acted_at,'completed' AS action_status,
                           NULL AS reference_price_cents,position_id AS real_position_id,
                           'manual_finish' AS action_kind
                    FROM events WHERE kind='real_manual_finish'
                ) ORDER BY acted_at DESC,id DESC LIMIT ? OFFSET ?
            """, (page_size, (page - 1) * page_size))
            return {"actions": actions, "total": total, "page": page,
                    "page_size": page_size, "pages": pages}

    @app.get("/api/settings")
    def settings():
        day = datetime.now(SHANGHAI).date().isoformat()
        with connect(config.state_db) as db:
            return {"current": setting_for(db, day),
                    "history": _rows(db, "SELECT * FROM settings ORDER BY effective_date DESC")}

    @app.post("/api/settings")
    def update_settings(data: SettingsIn, request: Request):
        _write_guard(request)
        today_date = datetime.now(SHANGHAI).date()
        effective = next_effective_calendar_date(today_date)
        if data.budget > data.capital:
            raise HTTPException(400, "Budget cannot exceed allocated capital")
        with connect(config.state_db) as db, transaction(db):
            if db.execute("SELECT 1 FROM settings WHERE effective_date=?", (effective,)).fetchone():
                raise HTTPException(409, "A settings version already starts on that date")
            db.execute("""
                INSERT INTO settings VALUES(NULL,?,?,?,?,?,?,?,?,?)
            """, (effective, cents(data.capital), cents(data.budget),
                  str(data.commission_rate), str(data.minimum_commission),
                  str(data.transfer_rate), str(data.stamp_rate),
                  str(data.slippage_rate), now_utc_iso()))
            append_event(db, f"settings:{effective}", "settings_changed", None,
                         None, None, {"effective_date": effective,
                                      "capital_cents": cents(data.capital),
                                      "budget_cents": cents(data.budget)})
        return {"effective_date": effective}

    @app.get("/api/events/stream")
    async def event_stream(request: Request, after: int = 0):
        with connect(config.state_db) as db:
            cursor = _event_cursor(db, after, request.headers.get("last-event-id"),
                                   "after" in request.query_params)

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                with connect(config.state_db) as db:
                    row = db.execute(
                        "SELECT seq FROM events WHERE seq>? ORDER BY seq DESC LIMIT 1",
                        (cursor,),
                    ).fetchone()
                if row:
                    cursor = row["seq"]
                    yield f'id: {cursor}\nevent: update\ndata: {{"latest_seq":{cursor}}}\n\n'
                else:
                    yield ": ping\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    return app
