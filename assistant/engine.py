from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .config import Config, SHANGHAI, now_utc_iso, strategy_manifest
from .market import MarketStore
from .money import buy_cost, dec, planned_shares, sell_proceeds, yuan
from .quotes import Quote, QuoteRouter
from .storage import (alert, append_event, available_budget, cash_balance, connect, set_status,
                      setting_for, transaction)
from .strategy import (SURGE_CANDIDATE_THRESHOLD, SURGE_ORDER_BUDGET_CENTS,
                       buy_limit_cents, first_target_cents, initial_stop_cents,
                       order_budget_cents,
                       stop_distance, strict_crossing, target_pct, trailing_cents,
                       virtual_exit)

LOG = logging.getLogger(__name__)


def continuous_session(moment: datetime) -> bool:
    t = moment.astimezone(SHANGHAI).time()
    return time(9, 30) <= t < time(11, 30) or time(13, 0) <= t < time(14, 57)


def eligible_name(name: str) -> bool:
    value = name.strip().upper()
    return bool(value) and not (value.startswith(("ST", "*ST", "S*ST", "SST")))


class Engine:
    def __init__(self, config: Config, market: MarketStore, router: QuoteRouter | None = None):
        self.config = config
        self.market = market
        self.router = router
        self.db = connect(config.state_db)
        self.strategy_hash, _ = strategy_manifest()
        self.quote_failures = 0

    def close(self) -> None:
        self.db.close()

    def preopen(self, today: date, *, deadline: datetime | None = None,
                sync: bool = True) -> dict:
        day = today.isoformat()
        existing = self.db.execute(
            "SELECT COUNT(*) FROM watchlist WHERE entry_date=?", (day,)
        ).fetchone()[0]
        if existing:
            return {"date": day, "status": "already_published", "watch_count": existing,
                    "focus_count": 0}
        if not self.market.session_on(today):
            return {"date": day, "status": "closed_market"}
        signal_day = self.market.last_session_before(today)
        job_key = f"preopen:{day}"
        with transaction(self.db):
            self.db.execute("""
                INSERT INTO jobs VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_key) DO UPDATE SET status='running',
                  started_at=excluded.started_at,detail=NULL
            """, (job_key, "preopen", day, "running", now_utc_iso(), None, None))
        try:
            sync_result = {}
            if sync:
                sync_result["daily"] = self.market.sync_daily(signal_day, deadline)
                sync_result["st"] = self.market.sync_st_and_classify(signal_day, deadline)
                sync_result["gaps"] = self.market.reconcile_missing(signal_day, deadline)
                sync_result["invalid"] = self.market.reconcile_invalid(signal_day, deadline)
            audit = self.market.audit(signal_day)
            with transaction(self.db):
                set_status(self.db, "daily_quality", audit)
            if not audit["ok"]:
                raise RuntimeError(f"Daily quality gate failed: {audit}")
            candidates = self.market.signals(signal_day)
            settings = setting_for(self.db, day)
            candidate_count = len(candidates)
            order_budget = order_budget_cents(settings["budget_cents"], candidate_count)
            published = []
            with transaction(self.db):
                for rank, candidate in enumerate(candidates[:5], 1):
                    code = candidate["code"]
                    limit = buy_limit_cents(candidate["raw_close"])
                    target = target_pct(float(candidate["ret20"]))
                    held = self.db.execute(
                        "SELECT id FROM positions WHERE code=? AND status='open'", (code,)
                    ).fetchone()
                    shares = planned_shares(yuan(limit), order_budget, settings)
                    budget_left = available_budget(self.db, day)
                    if held:
                        eligibility, reserve, reason = "held", 0, "已有同股虚拟持仓"
                    elif shares < 100:
                        eligibility, reserve, reason = "lot_too_small", 0, "预算不足购买一手"
                    elif budget_left < order_budget:
                        eligibility, reserve, reason = "cash_insufficient", 0, "虚拟资金不足"
                    else:
                        eligibility, reserve, reason = "funded", order_budget, None
                    self.db.execute("""
                        INSERT INTO watchlist(
                          entry_date,signal_date,code,rank,ret5,ret20,atr14,q_close,
                          raw_close,liquidity20,buy_limit_cents,target_pct,
                          planned_shares,reserved_cents,candidate_count,order_budget_cents,
                          eligibility,status,
                          block_reason,settings_id,strategy_hash,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (day, signal_day.isoformat(), code, rank,
                          candidate["ret5"], candidate["ret20"], candidate["atr14"],
                          candidate["q_close"], candidate["raw_close"],
                          candidate["liquidity20"], limit, str(target),
                          shares, reserve, candidate_count, order_budget,
                          eligibility, "watching",
                          reason, settings["id"], self.strategy_hash, now_utc_iso()))
                    watch_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
                    append_event(self.db, f"watch:{day}:{code}", "watch_published",
                                 code, watch_id, None,
                                 {"rank": rank, "limit_cents": limit,
                                  "eligibility": eligibility, "reserved_cents": reserve,
                                  "candidate_count": candidate_count,
                                  "order_budget_cents": order_budget})
                    published.append({"code": code, "rank": rank,
                                      "eligibility": eligibility,
                                      "order_budget_cents": order_budget})
                focus_count = 0
                set_status(self.db, "last_signal_day", signal_day.isoformat())
                self.db.execute(
                    "UPDATE jobs SET status='ok',finished_at=?,detail=? WHERE job_key=?",
                    (now_utc_iso(), json.dumps({"audit": audit, "sync": sync_result}), job_key),
                )
            return {"date": day, "signal_date": signal_day.isoformat(),
                    "status": "published", "watch": published,
                    "candidate_count": candidate_count,
                    "order_budget_cents": order_budget,
                    "focus_count": focus_count, "audit": audit}
        except Exception as exc:
            with transaction(self.db):
                self.db.execute(
                    "UPDATE jobs SET status='failed',finished_at=?,detail=? WHERE job_key=?",
                    (now_utc_iso(), str(exc)[:1000], job_key),
                )
                set_status(self.db, "daily_quality", {"target": signal_day.isoformat(),
                                                       "ok": False, "error": str(exc)[:500]})
                alert(self.db, f"data_failed:{day}", "data_failure", "critical",
                      "今日新买入暂停", str(exc)[:400])
            raise

    def _publish_focus(self, day: str, signal_day: str, candidates: list[dict]) -> int:
        for rank, candidate in enumerate(candidates[5:], 6):
            code = candidate["code"]
            limit = buy_limit_cents(candidate["raw_close"])
            self.db.execute("""
                INSERT OR IGNORE INTO focus_watchlist(
                  entry_date,signal_date,code,rank,ret5,ret20,atr14,q_close,
                  raw_close,liquidity20,buy_limit_cents,target_pct,
                  strategy_hash,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (day, signal_day, code, rank, candidate["ret5"], candidate["ret20"],
                  candidate["atr14"], candidate["q_close"], candidate["raw_close"],
                  candidate["liquidity20"], limit,
                  str(target_pct(float(candidate["ret20"]))), self.strategy_hash,
                  now_utc_iso()))
            append_event(self.db, f"focus:{day}:{code}", "focus_published", code,
                         None, None, {"rank": rank, "limit_cents": limit})
        return max(0, len(candidates) - 5)

    def ensure_focus(self, today: date) -> int:
        return 0

    def monitored_codes(self, today: date) -> list[str]:
        watch = self.db.execute(
            "SELECT code FROM watchlist WHERE entry_date=? AND status='watching' ORDER BY rank",
            (today.isoformat(),),
        ).fetchall()
        virtual = self.db.execute(
            "SELECT code FROM positions WHERE status='open'"
        ).fetchall()
        actual = self.db.execute(
            "SELECT code FROM real_positions WHERE status='open' AND management_mode='system'"
        ).fetchall()
        return list(dict.fromkeys([r[0] for r in [*watch, *virtual, *actual]]))

    def poll(self, today: date) -> dict:
        if self.router is None:
            raise RuntimeError("Quote router unavailable")
        codes = self.monitored_codes(today)
        quotes, health = self.router.fetch(codes, today.isoformat())
        with transaction(self.db):
            set_status(self.db, "quote_health", health)
            cursor = self.db.execute("""
                INSERT INTO quote_polls(at,trading_date,requested,received,provider,healthy,reason)
                VALUES(?,?,?,?,?,?,?)
            """, (now_utc_iso(), today.isoformat(), len(codes), len(quotes),
                  health["provider"], int(health["healthy"]), health.get("reason")))
            if codes and not health["healthy"]:
                self.quote_failures += 1
                if self.quote_failures == 3:
                    key = f"quote_failure:{today.isoformat()}:{cursor.lastrowid}"
                    alert(self.db, key, "quote_failure", "critical",
                          "盘中行情持续缺报", health.get("reason") or "部分报价缺失")
                    append_event(self.db, key, "quote_failure", None, None, None,
                                 {"requested": len(codes), "received": len(quotes),
                                  "reason": health.get("reason")})
            elif codes and health["healthy"]:
                self.quote_failures = 0
                recovered = self.db.execute("""
                    UPDATE alerts SET resolved_at=?
                    WHERE kind='quote_failure' AND resolved_at IS NULL
                      AND event_key LIKE ?
                """, (now_utc_iso(), f"quote_failure:{today.isoformat()}%"))
                if recovered.rowcount:
                    append_event(self.db, f"quote_recovered:{today}:{cursor.lastrowid}",
                                 "quote_recovered", None, None, None,
                                 {"requested": len(codes), "received": len(quotes)})
        for quote in quotes.values():
            self.process_quote(quote, allow_triggers=not self.config.observe_only)
        return {"codes": len(codes), "quotes": len(quotes), **health}

    def process_quote(self, quote: Quote, *, allow_triggers: bool) -> None:
        day = quote.source_time.date()
        health = (quote.received_at.astimezone(SHANGHAI).date() == day
                  and 0 <= quote.age_seconds() <= self.config.max_quote_age_seconds
                  and quote.last_cents > 0 and quote.open_cents > 0
                  and continuous_session(quote.source_time))
        with transaction(self.db):
            self.db.execute("""
                INSERT INTO quote_samples(
                  code,provider,source_time,received_at,age_ms,last_cents,healthy,raw_hash
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (quote.code, quote.provider, quote.source_time.isoformat(),
                  quote.received_at.isoformat(), round(quote.age_seconds() * 1000),
                  quote.last_cents, int(health), quote.raw_hash))
            self.db.execute("""
                INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                  provider=excluded.provider,source_time=excluded.source_time,
                  received_at=excluded.received_at,last_cents=excluded.last_cents,
                  open_cents=excluded.open_cents,preclose_cents=excluded.preclose_cents,
                  bid_cents=excluded.bid_cents,ask_cents=excluded.ask_cents,
                  bid_volume=excluded.bid_volume,ask_volume=excluded.ask_volume,
                  healthy=excluded.healthy,issue=excluded.issue,raw_hash=excluded.raw_hash
            """, (quote.code, quote.provider, quote.source_time.isoformat(),
                  quote.received_at.isoformat(), quote.last_cents, quote.open_cents,
                  quote.preclose_cents, quote.bid_cents, quote.ask_cents,
                  quote.bid_volume, quote.ask_volume, int(health),
                  None if health else "stale_or_outside_session", quote.raw_hash))
        if not health or not allow_triggers:
            return
        self._real_entry_quote(quote)
        self._entry_quote(quote)
        self._exit_quote(quote)
        self._actual_sell_signal(quote)

    def _focus_quote(self, quote: Quote) -> None:
        day = quote.source_time.date().isoformat()
        with transaction(self.db):
            focus = self.db.execute(
                "SELECT * FROM focus_watchlist WHERE entry_date=? AND code=? AND status='watching'",
                (day, quote.code),
            ).fetchone()
            if focus is None or not strict_crossing(
                    quote.open_cents, quote.last_cents, focus["buy_limit_cents"]):
                return
            key = f"focus_trigger:{day}:{quote.code}"
            evidence = {"rank": focus["rank"], "provider": quote.provider,
                        "source_time": quote.source_time.isoformat(),
                        "open_cents": quote.open_cents, "last_cents": quote.last_cents,
                        "limit_cents": focus["buy_limit_cents"], "raw_hash": quote.raw_hash}
            if not eligible_name(quote.name):
                reason = "当日证券名称包含 ST 或名称不可核对"
            elif (quote.preclose_cents <= 0 or abs(
                    quote.preclose_cents / (float(focus["raw_close"]) * 100) - 1) > 0.02):
                reason = "除权参考价与信号日收盘价偏离超过 2% 或昨收缺失"
            else:
                reason = None
            if reason:
                self.db.execute(
                    "UPDATE focus_watchlist SET status='blocked',block_reason=?,triggered_at=? WHERE id=?",
                    (reason, quote.source_time.isoformat(), focus["id"]),
                )
                append_event(self.db, key, "focus_blocked", quote.code, None, None,
                             {**evidence, "reason": reason})
                alert(self.db, key, "focus_blocked", "warning",
                      "重点关注买点触发，资格待核对", reason, quote.code)
                return
            self.db.execute(
                "UPDATE focus_watchlist SET status='triggered',triggered_at=? WHERE id=?",
                (quote.source_time.isoformat(), focus["id"]),
            )
            append_event(self.db, key, "focus_triggered", quote.code, None, None, evidence)
            alert(self.db, key, "focus_triggered", "info", "重点关注：参考买点已触发",
                  f"排名第 {focus['rank']}，仅供关注；不占用前 5 只名额，也不记录虚拟买入。请核对行情与实际账户。",
                  quote.code)

    def _real_entry_quote(self, quote: Quote) -> None:
        day = quote.source_time.date().isoformat()
        watch = self.db.execute(
            "SELECT * FROM watchlist WHERE entry_date=? AND code=?",
            (day, quote.code),
        ).fetchone()
        if (watch is None or watch["status"] in ("blocked", "expired")
                or not strict_crossing(quote.open_cents, quote.last_cents,
                                       watch["buy_limit_cents"])):
            return
        if not eligible_name(quote.name):
            return
        if (quote.preclose_cents <= 0 or
            abs(quote.preclose_cents / (float(watch["raw_close"]) * 100) - 1) > 0.02):
            return
        if self.db.execute(
            "SELECT 1 FROM real_positions WHERE code=? AND status='open'",
            (quote.code,),
        ).fetchone():
            return
        key = f"real_buy:{day}:{quote.code}"
        with transaction(self.db):
            append_event(self.db, key, "real_buy_signal", quote.code,
                         watch["id"], None,
                         {"rank": watch["rank"],
                          "limit_cents": watch["buy_limit_cents"],
                          "reference_price_cents": watch["buy_limit_cents"],
                          "order_budget_cents": watch["order_budget_cents"],
                          "source_time": quote.source_time.isoformat()})
            surge = (watch["candidate_count"] >= SURGE_CANDIDATE_THRESHOLD
                     if watch["candidate_count"] is not None
                     else (watch["order_budget_cents"] or 0) >= SURGE_ORDER_BUDGET_CENTS)
            allocation = ("重仓（建议20%）" if surge
                          else "轻仓（建议7.5%）")
            alert(self.db, key, "real_buy", "action", "买入",
                  f"提醒价 {yuan(watch['buy_limit_cents'])} 元；{allocation}",
                  quote.code, reference_price_cents=watch["buy_limit_cents"],
                  action_kind="buy")

    def _entry_quote(self, quote: Quote) -> None:
        day = quote.source_time.date().isoformat()
        with transaction(self.db):
            watch = self.db.execute(
                "SELECT * FROM watchlist WHERE entry_date=? AND code=? AND status='watching'",
                (day, quote.code),
            ).fetchone()
            if watch is None:
                return
            if not strict_crossing(quote.open_cents, quote.last_cents,
                                   watch["buy_limit_cents"]):
                return
            key = f"buy:{day}:{quote.code}"
            evidence = {"provider": quote.provider, "source_time": quote.source_time.isoformat(),
                        "received_at": quote.received_at.isoformat(),
                        "open_cents": quote.open_cents, "last_cents": quote.last_cents,
                        "limit_cents": watch["buy_limit_cents"], "raw_hash": quote.raw_hash}
            if not eligible_name(quote.name):
                reason = "当日证券名称包含 ST 或名称不可核对"
                self.db.execute(
                    "UPDATE watchlist SET status='blocked',block_reason=?,reserved_cents=0 WHERE id=?",
                    (reason, watch["id"]),
                )
                append_event(self.db, key, "entry_blocked", quote.code, watch["id"], None,
                             {**evidence, "reason": reason})
                alert(self.db, key, "entry_blocked", "warning", "买点已触发，交易资格待核对",
                      reason, quote.code)
                return
            if (quote.preclose_cents <= 0 or
                abs(quote.preclose_cents / (float(watch["raw_close"]) * 100) - 1) > 0.02):
                reason = "除权参考价与信号日收盘价偏离超过 2% 或昨收缺失"
                self.db.execute(
                    "UPDATE watchlist SET status='blocked',block_reason=?,reserved_cents=0 WHERE id=?",
                    (reason, watch["id"]),
                )
                append_event(self.db, key, "entry_blocked", quote.code, watch["id"], None,
                             {**evidence, "reason": reason})
                alert(self.db, key, "entry_blocked", "warning", "买点已触发，入场取消",
                      reason, quote.code)
                return
            append_event(self.db, key, "price_triggered", quote.code, watch["id"], None, evidence)
            eligibility = watch["eligibility"]
            if eligibility != "funded":
                message = ("已有同股虚拟持仓，不重复开仓" if eligibility == "held"
                           else "价格已触发但虚拟账户仓位不足；请核对盘口与实际账户")
                self.db.execute(
                    "UPDATE watchlist SET status='opportunity_only',triggered_at=?,reserved_cents=0 WHERE id=?",
                    (quote.source_time.isoformat(), watch["id"]),
                )
                append_event(self.db, key + ":opportunity", "opportunity_only",
                             quote.code, watch["id"], None,
                             {"eligibility": eligibility, **evidence})
                alert(self.db, key, "opportunity_only", "info",
                      "买点已触发，仅记录机会", message, quote.code)
                return
            # Virtual execution succeeds when the rule's price trigger is observed.
            settings = dict(self.db.execute(
                "SELECT * FROM settings WHERE id=?", (watch["settings_id"],)
            ).fetchone())
            shares = watch["planned_shares"]
            cost, fee = buy_cost(yuan(watch["buy_limit_cents"]), shares,
                                 dec(settings["commission_rate"]),
                                 dec(settings["minimum_commission"]),
                                 dec(settings["transfer_rate"]),
                                 dec(settings["slippage_rate"]))
            duplicate = self.db.execute(
                "SELECT id FROM positions WHERE code=? AND status='open'", (quote.code,)
            ).fetchone()
            if duplicate or cash_balance(self.db, day) < cost:
                self.db.execute(
                    "UPDATE watchlist SET status='opportunity_only',reserved_cents=0,triggered_at=? WHERE id=?",
                    (quote.source_time.isoformat(), watch["id"]),
                )
                alert(self.db, key, "opportunity_only", "info",
                      "买点已触发，仅记录机会",
                      "盘中持仓或虚拟现金状态发生变化，未新增虚拟持仓", quote.code)
                append_event(self.db, key + ":opportunity", "opportunity_only",
                             quote.code, watch["id"], None, evidence)
                return
            distance = stop_distance(watch["atr14"], watch["q_close"],
                                     watch["buy_limit_cents"], watch["raw_close"])
            stop = initial_stop_cents(watch["buy_limit_cents"], distance)
            self.db.execute("""
                INSERT INTO positions(
                  watch_id,code,entry_date,entry_price_cents,bought_shares,remaining_shares,
                  target_pct,initial_stop_cents,prior_peak_cents,peak_asof_date,
                  reserved_cents,buy_cash_cents,settings_id,strategy_hash,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (watch["id"], quote.code, day, watch["buy_limit_cents"],
                  shares, shares, watch["target_pct"], stop, watch["buy_limit_cents"],
                  day, watch["reserved_cents"], cost, watch["settings_id"],
                  watch["strategy_hash"], now_utc_iso()))
            position_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.db.execute("""
                INSERT INTO virtual_legs(
                  position_id,side,shares,gross_price_cents,cash_delta_cents,
                  fee_cents,quote_event_key,reason,at
                ) VALUES(?,?,?,?,?,?,?,?,?)
            """, (position_id, "buy", shares, watch["buy_limit_cents"],
                  -cost, fee, key, "strict_crossing", quote.source_time.isoformat()))
            self.db.execute(
                "UPDATE watchlist SET status='virtual_bought',reserved_cents=0,triggered_at=? WHERE id=?",
                (quote.source_time.isoformat(), watch["id"]),
            )
            append_event(self.db, key + ":filled", "virtual_entry_filled",
                         quote.code, watch["id"], position_id,
                         {"shares": shares, "cost_cents": cost, "fee_cents": fee,
                          "initial_stop_cents": stop, **evidence})
            alert(self.db, key, "virtual_buy", "action", "买入价已触发",
                  f"虚拟买入 {shares} 股；请在券商端核对行情、资金及委托成交", quote.code)

    def _update_peak(self, position: sqlite3.Row, today: date) -> tuple[int, int]:
        hold = self.market.sessions_since(date.fromisoformat(position["entry_date"]), today)
        peak = int(position["prior_peak_cents"])
        asof = date.fromisoformat(position["peak_asof_date"])
        if hold > 0:
            previous = self.market.last_session_before(today)
            if previous > asof and previous > date.fromisoformat(position["entry_date"]):
                high = self.market.close_price(position["code"], previous)
                if high is None:
                    raise RuntimeError("Previous day's high is unavailable")
                peak = max(peak, round(high * 100))
                asof = previous
        trail = int(position["trailing_line_cents"])
        if position["partial_done"]:
            trail = trailing_cents(position["entry_price_cents"], peak, trail)
        self.db.execute("""
            UPDATE positions SET hold_days=?,prior_peak_cents=?,peak_asof_date=?,
              trailing_line_cents=? WHERE id=?
        """, (hold, peak, asof.isoformat(), trail, position["id"]))
        return hold, trail

    def _exit_quote(self, quote: Quote) -> None:
        positions = self.db.execute(
            "SELECT id FROM positions WHERE code=? AND status='open'", (quote.code,)
        ).fetchall()
        for item in positions:
            with transaction(self.db):
                position = self.db.execute(
                    "SELECT * FROM positions WHERE id=? AND status='open'", (item["id"],)
                ).fetchone()
                if position is None:
                    continue
                today = quote.source_time.date()
                try:
                    hold, trail = self._update_peak(position, today)
                except Exception as exc:
                    alert(self.db, f"peak_missing:{today}:{position['id']}",
                          "data_failure", "critical", "卖出阈值待核对",
                          str(exc), quote.code)
                    continue
                if hold == 0:
                    continue
                previous = self.market.last_session_before(today)
                previous_close = self.market.raw_close(quote.code, previous)
                if (not previous_close or quote.preclose_cents <= 0
                    or abs(quote.preclose_cents / (previous_close * 100) - 1) > 0.02):
                    alert(self.db, f"action_review:{today}:{position['id']}",
                          "action_review", "critical", "持仓公司行为待核对",
                          "昨收参考价相对上一交易日原始收盘价异常，暂停自动卖出",
                          quote.code)
                    continue
                opening = time(9, 30) <= quote.source_time.time() <= time(9, 30, 15)
                decision = virtual_exit(
                    entry_cents=position["entry_price_cents"],
                    remaining=position["remaining_shares"],
                    target=position["target_pct"],
                    initial_stop=position["initial_stop_cents"],
                    trailing_line=trail, partial_done=bool(position["partial_done"]),
                    hold_days=hold, open_cents=quote.open_cents,
                    last_cents=quote.last_cents, is_opening_snapshot=opening,
                )
                if decision is None:
                    continue
                settings = dict(self.db.execute(
                    "SELECT * FROM settings WHERE id=?", (position["settings_id"],)
                ).fetchone())
                proxy_cents = decision.proxy_price_cents
                proceeds, fee = sell_proceeds(
                    yuan(proxy_cents), decision.shares,
                    dec(settings["commission_rate"]), dec(settings["minimum_commission"]),
                    dec(settings["transfer_rate"]), dec(settings["stamp_rate"]),
                    dec(settings["slippage_rate"]),
                )
                event_key = f"sell:{position['id']}:{'partial' if decision.kind == 'partial_profit' else 'final'}"
                already = self.db.execute(
                    "SELECT 1 FROM virtual_legs WHERE position_id=? AND quote_event_key=?",
                    (position["id"], event_key),
                ).fetchone()
                if already:
                    continue
                self.db.execute("""
                    INSERT INTO virtual_legs(
                      position_id,side,shares,gross_price_cents,cash_delta_cents,
                      fee_cents,quote_event_key,reason,at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                """, (position["id"], "sell", decision.shares,
                      proxy_cents, proceeds, fee,
                      event_key, decision.kind, quote.source_time.isoformat()))
                remaining = position["remaining_shares"] - decision.shares
                if remaining == 0:
                    net = self.db.execute(
                        "SELECT SUM(cash_delta_cents) FROM virtual_legs WHERE position_id=?",
                        (position["id"],),
                    ).fetchone()[0]
                    self.db.execute("""
                        UPDATE positions SET remaining_shares=0,status='closed',
                          reserved_cents=0,closed_pnl_cents=?,closed_at=?,
                          hold_days=? WHERE id=?
                    """, (net, quote.source_time.isoformat(), hold, position["id"]))
                else:
                    self.db.execute("""
                        UPDATE positions SET remaining_shares=?,partial_done=1,
                          hold_days=? WHERE id=?
                    """, (remaining, hold, position["id"]))
                append_event(self.db, event_key, "virtual_exit",
                             quote.code, position["watch_id"], position["id"],
                             {"reason": decision.kind, "shares": decision.shares,
                              "proxy_price_cents": proxy_cents,
                              "fee_cents": fee, "cash_in_cents": proceeds,
                              "source_time": quote.source_time.isoformat()})
                alert(self.db, event_key, "virtual_sell", "action", "卖出条件已触发",
                      f"{decision.kind}：虚拟卖出 {decision.shares} 股；请在券商端核对实际成交",
                      quote.code)

    def _actual_sell_signal(self, quote: Quote) -> None:
        rows = self.db.execute("""
            SELECT * FROM real_positions
            WHERE code=? AND status='open' AND management_mode='system'
              AND watch_id IS NOT NULL
        """, (quote.code,)).fetchall()
        for pos in rows:
            with transaction(self.db):
                hold = self.market.sessions_since(date.fromisoformat(pos["entry_date"]),
                                                  quote.source_time.date())
                if hold == 0:
                    continue
                previous_close = self.market.raw_close(
                    quote.code, self.market.last_session_before(quote.source_time.date())
                )
                if (not previous_close or quote.preclose_cents <= 0
                    or abs(quote.preclose_cents / (previous_close * 100) - 1) > 0.02):
                    alert(self.db, f"actual_action_review:{quote.source_time.date()}:{pos['id']}",
                          "action_review", "critical", "实际持仓公司行为待核对",
                          "昨收参考价异常，请人工核对调整后的持仓及卖出线", quote.code)
                    continue
                peak = pos["prior_peak_cents"] or pos["entry_price_cents"]
                asof = date.fromisoformat(pos["peak_asof_date"])
                previous = self.market.last_session_before(quote.source_time.date())
                if previous > asof and previous > date.fromisoformat(pos["entry_date"]):
                    high = self.market.close_price(quote.code, previous)
                    if high is None:
                        alert(self.db, f"actual_peak_missing:{pos['id']}:{quote.source_time.date()}",
                              "data_failure", "critical", "实际持仓退出线待核对",
                              "前一交易日高价缺失", quote.code)
                        continue
                    peak = max(peak, round(high * 100))
                    asof = previous
                trail = pos["trailing_line_cents"]
                if pos["partial_done"]:
                    trail = trailing_cents(pos["entry_price_cents"], peak, trail)
                self.db.execute("""
                    UPDATE real_positions SET prior_peak_cents=?,peak_asof_date=?,
                      trailing_line_cents=? WHERE id=?
                """, (peak, asof.isoformat(), trail, pos["id"]))
                if self.db.execute("""
                    SELECT 1 FROM alerts WHERE real_position_id=? AND kind='actual_sell'
                      AND action_status='pending'
                """, (pos["id"],)).fetchone():
                    continue
                decision = virtual_exit(
                    entry_cents=pos["entry_price_cents"],
                    remaining=100 if pos["partial_done"] else 200,
                    target=pos["target_pct"],
                    initial_stop=pos["initial_stop_cents"],
                    trailing_line=trail, partial_done=bool(pos["partial_done"]),
                    hold_days=hold, open_cents=quote.open_cents,
                    last_cents=quote.last_cents,
                    is_opening_snapshot=time(9, 30) <= quote.source_time.time() <= time(9, 30, 15),
                )
                if decision is None:
                    continue
                key = f"real_sell:{pos['id']}:{quote.source_time.date()}:{decision.kind}"
                append_event(self.db, key, "actual_sell_signal", quote.code,
                             pos["watch_id"], pos["id"],
                             {"reason": decision.kind,
                              "reference_price_cents": decision.proxy_price_cents,
                              "quote_time": quote.source_time.isoformat()})
                alert(self.db, key, "actual_sell", "action", "卖出",
                      f"{'首次止盈，卖出约半仓' if decision.kind == 'partial_profit' else '清仓，卖出剩余持仓'}；记录价 {yuan(decision.proxy_price_cents)} 元",
                      quote.code, reference_price_cents=decision.proxy_price_cents,
                      real_position_id=pos["id"], action_kind=decision.kind)

    def end_day(self, today: date) -> dict:
        with transaction(self.db):
            rows = self.db.execute("""
                SELECT id,code FROM watchlist WHERE entry_date=? AND status='watching'
            """, (today.isoformat(),)).fetchall()
            self.db.execute("""
                UPDATE watchlist SET status='expired',reserved_cents=0
                WHERE entry_date=? AND status='watching'
            """, (today.isoformat(),))
            for row in rows:
                append_event(self.db, f"expired:{today}:{row['code']}",
                             "watch_expired", row["code"], row["id"], None, {})
            focus_rows = self.db.execute(
                "SELECT code FROM focus_watchlist WHERE entry_date=? AND status='watching'",
                (today.isoformat(),),
            ).fetchall()
            self.db.execute(
                "UPDATE focus_watchlist SET status='expired' WHERE entry_date=? AND status='watching'",
                (today.isoformat(),),
            )
            for row in focus_rows:
                append_event(self.db, f"focus_expired:{today}:{row['code']}",
                             "focus_expired", row["code"], None, None, {})
        return {"expired": len(rows), "focus_expired": len(focus_rows)}
