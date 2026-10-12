from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.templating import Jinja2Templates

from .config import Config, SHANGHAI, now_utc_iso
from .money import cents
from .storage import (alert, append_event, available_budget, cash_balance,
                      connect, next_effective_calendar_date, setting_for, transaction)
from .strategy import initial_stop_cents, stop_distance

ROOT = Path(__file__).resolve().parents[1]


def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _write_guard(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin != expected:
            raise HTTPException(403, "Cross-origin write refused")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site write refused")


class ActualFillIn(BaseModel):
    side: str
    code: str | None = None
    watch_id: int | None = None
    position_id: int | None = None
    shares: int = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal("0"), ge=0)
    happened_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=120)


class SettingsIn(BaseModel):
    capital: Decimal = Field(gt=0)
    budget: Decimal = Field(gt=0)
    commission_rate: Decimal = Field(ge=0)
    minimum_commission: Decimal = Field(ge=0)
    transfer_rate: Decimal = Field(ge=0)
    stamp_rate: Decimal = Field(ge=0)
    slippage_rate: Decimal = Field(ge=0)


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
    def virtual_board():
        day = datetime.now(SHANGHAI).date().isoformat()
        with connect(config.state_db) as db:
            setting = setting_for(db, day)
            return {
                "date": day,
                "capital_cents": setting["capital_cents"],
                "available_budget_cents": available_budget(db, day),
                "cash_cents": cash_balance(db, day),
                "settings": setting,
                "watchlist": _rows(db, """
                    SELECT * FROM watchlist WHERE entry_date=? ORDER BY rank
                """, (day,)),
                "positions": _rows(db, """
                    SELECT * FROM positions WHERE status='open' ORDER BY entry_date,id
                """),
                "alerts": _rows(db, """
                    SELECT * FROM alerts
                    WHERE kind IN ('virtual_buy','virtual_sell','opportunity_only',
                                   'entry_blocked','sell_blocked',
                                   'data_failure','action_review')
                      AND event_key NOT LIKE 'actual_%'
                      AND kind != 'focus_triggered'
                    ORDER BY id DESC LIMIT 100
                """),
            }

    @app.get("/api/real")
    def real_board():
        day = datetime.now(SHANGHAI).date().isoformat()
        with connect(config.state_db) as db:
            return {
                "date": day,
                "watchlist": _rows(db, """
                    SELECT w.id,w.entry_date,w.signal_date,w.code,w.rank,
                      w.ret5,w.ret20,w.buy_limit_cents,w.target_pct,
                      w.planned_shares,
                      COALESCE(w.order_budget_cents,s.budget_cents) AS order_budget_cents,
                      e.at AS buy_signal_at
                    FROM watchlist AS w JOIN settings AS s ON s.id=w.settings_id
                    LEFT JOIN events AS e
                      ON e.event_key='real_buy:' || w.entry_date || ':' || w.code
                    WHERE w.entry_date=? ORDER BY w.rank
                """, (day,)),
                "positions": _rows(db, """
                    SELECT * FROM actual_positions WHERE status='open'
                    ORDER BY entry_date,id
                """),
                "alerts": _rows(db, """
                    SELECT * FROM alerts
                    WHERE kind IN ('real_buy','actual_sell')
                       OR event_key LIKE 'actual_%'
                    ORDER BY id DESC LIMIT 100
                """),
            }

    @app.get("/api/quotes")
    def quotes():
        with connect(config.state_db) as db:
            return _rows(db, "SELECT * FROM quotes ORDER BY code")

    @app.get("/api/positions/virtual")
    def virtual_positions(status: str = "open"):
        if status not in ("open", "closed", "all"):
            raise HTTPException(400, "Invalid status")
        with connect(config.state_db) as db:
            sql = "SELECT * FROM positions"
            if status != "all":
                sql += " WHERE status=?"
                return _rows(db, sql + " ORDER BY id DESC", (status,))
            return _rows(db, sql + " ORDER BY id DESC")

    @app.get("/api/positions/actual")
    def actual_positions(status: str = "open"):
        if status not in ("open", "closed", "all"):
            raise HTTPException(400, "Invalid status")
        with connect(config.state_db) as db:
            sql = "SELECT * FROM actual_positions"
            if status != "all":
                return _rows(db, sql + " WHERE status=? ORDER BY id DESC", (status,))
            return _rows(db, sql + " ORDER BY id DESC")

    @app.get("/api/positions/actual/{position_id}/fills")
    def actual_position_fills(position_id: int):
        with connect(config.state_db) as db:
            if not db.execute("SELECT 1 FROM actual_positions WHERE id=?",
                              (position_id,)).fetchone():
                raise HTTPException(404, "Actual position not found")
            return _rows(db, """
                SELECT * FROM actual_fills WHERE position_id=?
                ORDER BY happened_at,id
            """, (position_id,))

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
            actual_closed = db.execute("""
                SELECT COALESCE(SUM(
                  CASE WHEN f.side='sell' THEN f.shares*f.price_cents-f.fee_cents
                       ELSE -f.shares*f.price_cents-f.fee_cents END),0)
                FROM actual_fills f JOIN actual_positions p ON p.id=f.position_id
                WHERE p.status='closed'
            """).fetchone()[0]
            actual_trades = _rows(db, """
                SELECT p.id,p.code,p.entry_date,p.closed_at,p.bought_shares,
                  SUM(CASE WHEN f.side='sell' THEN f.shares*f.price_cents-f.fee_cents
                           ELSE -f.shares*f.price_cents-f.fee_cents END) AS net_pnl_cents
                FROM actual_positions p JOIN actual_fills f ON f.position_id=p.id
                WHERE p.status='closed'
                GROUP BY p.id,p.code,p.entry_date,p.closed_at,p.bought_shares
                ORDER BY p.id DESC LIMIT 100
            """)
            return {"virtual_closed_trades": len(closed),
                    "virtual_wins": wins,
                    "virtual_win_rate": wins / len(closed) if closed else None,
                    "virtual_realized_pnl_cents": pnl,
                    "virtual_cash_cents": cash_balance(db, day),
                    "virtual_available_budget_cents": available_budget(db, day),
                    "opportunities_without_virtual_entry": opportunities,
                    "actual_closed_realized_pnl_cents": actual_closed,
                    "actual_account_equity": None,
                    "virtual_trades": list(reversed(closed))[-100:][::-1],
                    "actual_trades": actual_trades}

    @app.get("/api/virtual/report")
    def virtual_report():
        report = reports()
        return {key: value for key, value in report.items()
                if key.startswith("virtual_") or key == "opportunities_without_virtual_entry"}

    @app.get("/api/real/report")
    def real_report():
        report = reports()
        return {key: value for key, value in report.items()
                if key.startswith("actual_")}

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

    @app.post("/api/actual-fills")
    def actual_fill(data: ActualFillIn, request: Request):
        _write_guard(request)
        if data.side not in ("buy", "sell"):
            raise HTTPException(400, "Side must be buy or sell")
        if data.happened_at.tzinfo is None or data.happened_at.utcoffset() is None:
            raise HTTPException(400, "Happened_at must include a time zone")
        happened = data.happened_at.astimezone(SHANGHAI)
        price_cents = cents(data.price)
        fee_cents = cents(data.fee)
        if price_cents <= 0:
            raise HTTPException(400, "Price is too small")
        with connect(config.state_db) as db, transaction(db):
            existing = db.execute(
                "SELECT position_id FROM actual_fills WHERE idempotency_key=?",
                (data.idempotency_key,),
            ).fetchone()
            if existing:
                return {"position_id": existing["position_id"], "duplicate": True}
            if data.side == "buy":
                if not data.code or data.shares % 100 != 0:
                    raise HTTPException(400, "Buy requires code and 100-share lots")
                if not data.code.startswith(("sh.", "sz.")) or len(data.code) != 9:
                    raise HTTPException(400, "Invalid code")
                watch = None
                target = stop = None
                linked_watch_id = data.watch_id
                if linked_watch_id is None:
                    match = db.execute("""
                        SELECT id FROM watchlist WHERE code=? AND entry_date=?
                    """, (data.code, happened.date().isoformat())).fetchone()
                    linked_watch_id = match["id"] if match else None
                if linked_watch_id is not None:
                    watch = db.execute(
                        "SELECT * FROM watchlist WHERE id=? AND code=?",
                        (linked_watch_id, data.code),
                    ).fetchone()
                    if watch is None:
                        raise HTTPException(400, "Watchlist record does not match code")
                    if watch["entry_date"] != happened.date().isoformat():
                        raise HTTPException(400, "Watchlist record is for another trading day")
                    target = watch["target_pct"]
                    distance = stop_distance(watch["atr14"], watch["q_close"],
                                             price_cents, watch["raw_close"])
                    stop = initial_stop_cents(price_cents, distance)
                db.execute("""
                    INSERT INTO actual_positions(
                      code,watch_id,entry_date,entry_price_cents,bought_shares,
                      remaining_shares,target_pct,initial_stop_cents,prior_peak_cents,
                      peak_asof_date,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """, (data.code, linked_watch_id, happened.date().isoformat(),
                      price_cents, data.shares, data.shares, target, stop,
                      price_cents, happened.date().isoformat(), now_utc_iso()))
                position_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                if data.position_id is None:
                    raise HTTPException(400, "Sell requires position_id")
                pos = db.execute(
                    "SELECT * FROM actual_positions WHERE id=? AND status='open'",
                    (data.position_id,),
                ).fetchone()
                if pos is None:
                    raise HTTPException(404, "Open actual position not found")
                if data.shares > pos["remaining_shares"]:
                    raise HTTPException(400, "Sell quantity exceeds remaining shares")
                if happened.date() <= date.fromisoformat(pos["entry_date"]):
                    raise HTTPException(400, "A-share position cannot be sold on entry date")
                position_id = data.position_id
                event_code = pos["code"]
                event_watch_id = pos["watch_id"]
                remaining = pos["remaining_shares"] - data.shares
                db.execute("""
                    UPDATE actual_positions SET remaining_shares=?,partial_done=?,
                      status=?,closed_at=? WHERE id=?
                """, (remaining, int(remaining > 0 or bool(pos["partial_done"])),
                      "closed" if remaining == 0 else "open",
                      happened.isoformat() if remaining == 0 else None, position_id))
            db.execute("""
                INSERT INTO actual_fills VALUES(NULL,?,?,?,?,?,?,?)
            """, (position_id, data.side, data.shares, price_cents,
                  fee_cents, happened.isoformat(), data.idempotency_key))
            append_event(db, f"actual_fill:{data.idempotency_key}", "actual_fill",
                         data.code if data.side == "buy" else event_code,
                         linked_watch_id if data.side == "buy" else event_watch_id, None,
                         {"position_id": position_id, "side": data.side,
                          "shares": data.shares, "price_cents": price_cents,
                          "fee_cents": fee_cents})
        return {"position_id": position_id, "duplicate": False}

    @app.get("/api/events/stream")
    async def event_stream(request: Request, after: int = 0):
        header = request.headers.get("last-event-id")
        cursor = max(after, int(header)) if header and header.isdigit() else after

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                with connect(config.state_db) as db:
                    rows = _rows(db, "SELECT seq,kind,code,at FROM events WHERE seq>? ORDER BY seq LIMIT 100",
                                 (cursor,))
                if rows:
                    for row in rows:
                        cursor = row["seq"]
                        yield f"id: {cursor}\nevent: update\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                else:
                    yield ": ping\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    return app
