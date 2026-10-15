from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from .config import now_cn, now_utc_iso, strategy_manifest
from .strategy import exit_plan

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effective_date TEXT NOT NULL UNIQUE,
  capital_cents INTEGER NOT NULL CHECK(capital_cents>0),
  budget_cents INTEGER NOT NULL CHECK(budget_cents>0),
  commission_rate TEXT NOT NULL,
  minimum_commission TEXT NOT NULL,
  transfer_rate TEXT NOT NULL,
  stamp_rate TEXT NOT NULL,
  slippage_rate TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
  sha256 TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  installed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_date TEXT NOT NULL,
  signal_date TEXT NOT NULL,
  code TEXT NOT NULL,
  rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 5),
  ret5 REAL NOT NULL,
  ret20 REAL NOT NULL,
  atr14 REAL NOT NULL,
  q_close REAL NOT NULL,
  raw_close REAL NOT NULL,
  liquidity20 REAL NOT NULL,
  buy_limit_cents INTEGER NOT NULL,
  target_pct TEXT NOT NULL,
  planned_shares INTEGER NOT NULL DEFAULT 0,
  reserved_cents INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER,
  order_budget_cents INTEGER,
  eligibility TEXT NOT NULL DEFAULT 'pending',
  status TEXT NOT NULL DEFAULT 'watching',
  block_reason TEXT,
  triggered_at TEXT,
  settings_id INTEGER NOT NULL REFERENCES settings(id),
  strategy_hash TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(entry_date, code), UNIQUE(entry_date, rank)
);
CREATE TABLE IF NOT EXISTS focus_watchlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_date TEXT NOT NULL,
  signal_date TEXT NOT NULL,
  code TEXT NOT NULL,
  rank INTEGER NOT NULL CHECK(rank>5),
  ret5 REAL NOT NULL,
  ret20 REAL NOT NULL,
  atr14 REAL NOT NULL,
  q_close REAL NOT NULL,
  raw_close REAL NOT NULL,
  liquidity20 REAL NOT NULL,
  buy_limit_cents INTEGER NOT NULL,
  target_pct TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'watching',
  block_reason TEXT,
  triggered_at TEXT,
  strategy_hash TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(entry_date, code), UNIQUE(entry_date, rank)
);
CREATE TABLE IF NOT EXISTS quotes (
  code TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  source_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  last_cents INTEGER NOT NULL,
  open_cents INTEGER NOT NULL,
  preclose_cents INTEGER,
  bid_cents INTEGER,
  ask_cents INTEGER,
  bid_volume INTEGER,
  ask_volume INTEGER,
  healthy INTEGER NOT NULL,
  issue TEXT,
  raw_hash TEXT
);
CREATE TABLE IF NOT EXISTS quote_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  provider TEXT NOT NULL,
  source_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  age_ms INTEGER NOT NULL,
  last_cents INTEGER NOT NULL,
  healthy INTEGER NOT NULL,
  raw_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quote_polls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  trading_date TEXT NOT NULL,
  requested INTEGER NOT NULL,
  received INTEGER NOT NULL,
  provider TEXT NOT NULL,
  healthy INTEGER NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  code TEXT,
  watch_id INTEGER,
  position_id INTEGER,
  at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id INTEGER NOT NULL UNIQUE REFERENCES watchlist(id),
  code TEXT NOT NULL,
  entry_date TEXT NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  bought_shares INTEGER NOT NULL,
  remaining_shares INTEGER NOT NULL,
  target_pct TEXT NOT NULL,
  initial_stop_cents INTEGER NOT NULL,
  trailing_line_cents INTEGER NOT NULL DEFAULT 0,
  prior_peak_cents INTEGER NOT NULL,
  peak_asof_date TEXT NOT NULL,
  partial_done INTEGER NOT NULL DEFAULT 0,
  hold_days INTEGER NOT NULL DEFAULT 0,
  reserved_cents INTEGER NOT NULL,
  buy_cash_cents INTEGER NOT NULL,
  closed_pnl_cents INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  block_reason TEXT,
  settings_id INTEGER NOT NULL REFERENCES settings(id),
  strategy_hash TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT,
  CHECK(remaining_shares>=0 AND remaining_shares<=bought_shares)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_positions_code_open
  ON positions(code) WHERE status='open';
CREATE TABLE IF NOT EXISTS virtual_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES positions(id),
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  shares INTEGER NOT NULL CHECK(shares>0),
  gross_price_cents INTEGER NOT NULL,
  cash_delta_cents INTEGER NOT NULL,
  fee_cents INTEGER NOT NULL,
  quote_event_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  at TEXT NOT NULL,
  UNIQUE(position_id, quote_event_key)
);
CREATE TABLE IF NOT EXISTS actual_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  watch_id INTEGER REFERENCES watchlist(id),
  entry_date TEXT NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  bought_shares INTEGER NOT NULL,
  remaining_shares INTEGER NOT NULL,
  target_pct TEXT,
  initial_stop_cents INTEGER,
  trailing_line_cents INTEGER NOT NULL DEFAULT 0,
  prior_peak_cents INTEGER,
  peak_asof_date TEXT,
  partial_done INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  closed_at TEXT,
  CHECK(remaining_shares>=0 AND remaining_shares<=bought_shares)
);
CREATE TABLE IF NOT EXISTS actual_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES actual_positions(id),
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  shares INTEGER NOT NULL CHECK(shares>0),
  price_cents INTEGER NOT NULL,
  fee_cents INTEGER NOT NULL DEFAULT 0,
  happened_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS real_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  watch_id INTEGER REFERENCES watchlist(id),
  entry_date TEXT NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  target_pct TEXT,
  initial_stop_cents INTEGER,
  trailing_line_cents INTEGER NOT NULL DEFAULT 0,
  prior_peak_cents INTEGER,
  peak_asof_date TEXT,
  partial_done INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
  management_mode TEXT NOT NULL DEFAULT 'system' CHECK(management_mode IN ('system','user_managed')),
  created_at TEXT NOT NULL,
  closed_at TEXT,
  legacy_actual_id INTEGER UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_real_positions_code_open
  ON real_positions(code) WHERE status='open';
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  level TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  code TEXT,
  created_at TEXT NOT NULL,
  read_at TEXT,
  resolved_at TEXT,
  action_status TEXT,
  acted_at TEXT,
  reference_price_cents INTEGER,
  real_position_id INTEGER REFERENCES real_positions(id),
  action_kind TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  job_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  target_date TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS system_status (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aggressive_orders (
  watch_id INTEGER PRIMARY KEY REFERENCES watchlist(id),
  planned_shares INTEGER NOT NULL DEFAULT 0,
  reserved_cents INTEGER NOT NULL DEFAULT 0,
  order_budget_cents INTEGER NOT NULL,
  target_pct TEXT,
  no_partial INTEGER NOT NULL DEFAULT 0,
  eligibility TEXT NOT NULL DEFAULT 'pending',
  status TEXT NOT NULL DEFAULT 'watching',
  block_reason TEXT,
  triggered_at TEXT
);
CREATE TABLE IF NOT EXISTS aggressive_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id INTEGER NOT NULL UNIQUE REFERENCES watchlist(id),
  code TEXT NOT NULL,
  entry_date TEXT NOT NULL,
  entry_price_cents INTEGER NOT NULL,
  bought_shares INTEGER NOT NULL,
  remaining_shares INTEGER NOT NULL,
  target_pct TEXT,
  no_partial INTEGER NOT NULL DEFAULT 0,
  initial_stop_cents INTEGER NOT NULL,
  trailing_line_cents INTEGER NOT NULL DEFAULT 0,
  prior_peak_cents INTEGER NOT NULL,
  peak_asof_date TEXT NOT NULL,
  partial_done INTEGER NOT NULL DEFAULT 0,
  hold_days INTEGER NOT NULL DEFAULT 0,
  reserved_cents INTEGER NOT NULL,
  buy_cash_cents INTEGER NOT NULL,
  closed_pnl_cents INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  block_reason TEXT,
  settings_id INTEGER NOT NULL REFERENCES settings(id),
  strategy_hash TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT,
  CHECK(remaining_shares>=0 AND remaining_shares<=bought_shares)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_aggressive_positions_code_open
  ON aggressive_positions(code) WHERE status='open';
CREATE TABLE IF NOT EXISTS aggressive_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES aggressive_positions(id),
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  shares INTEGER NOT NULL CHECK(shares>0),
  gross_price_cents INTEGER NOT NULL,
  cash_delta_cents INTEGER NOT NULL,
  fee_cents INTEGER NOT NULL,
  quote_event_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  at TEXT NOT NULL,
  UNIQUE(position_id, quote_event_key)
);
CREATE INDEX IF NOT EXISTS ix_aggressive_legs_at ON aggressive_legs(at DESC,id DESC);
CREATE TABLE IF NOT EXISTS real_strategy_schedule (
  effective_date TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL CHECK(strategy_id IN ('stable','aggressive')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS virtual_funding_modes (
  account TEXT PRIMARY KEY CHECK(account IN ('stable','aggressive')),
  mode TEXT NOT NULL CHECK(mode IN ('cash_100w','leveraged_5x')),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_indices (
  code TEXT PRIMARY KEY,
  last_cents INTEGER NOT NULL,
  preclose_cents INTEGER NOT NULL,
  source_time TEXT NOT NULL,
  received_at TEXT NOT NULL,
  provider TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_watch_entry_date ON watchlist(entry_date, rank);
CREATE INDEX IF NOT EXISTS ix_focus_entry_date ON focus_watchlist(entry_date, rank);
CREATE INDEX IF NOT EXISTS ix_events_at ON events(at);
CREATE INDEX IF NOT EXISTS ix_alerts_unread ON alerts(read_at, id);
CREATE INDEX IF NOT EXISTS ix_positions_open ON positions(status, code);
CREATE INDEX IF NOT EXISTS ix_virtual_legs_at_id ON virtual_legs(at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_quote_samples_received ON quote_samples(received_at);
CREATE INDEX IF NOT EXISTS ix_quote_polls_day ON quote_polls(trading_date);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def init_db(path: Path) -> None:
    with closing(connect(path)) as db:
        db.executescript(SCHEMA)
        def add_column(table: str, name: str, definition: str) -> None:
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError as exc:
                # Worker and web may initialize against the same existing DB.
                if "duplicate column name" not in str(exc).lower():
                    raise
        for table in ("watchlist", "positions"):
            names = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            if "strategy_hash" not in names:
                add_column(table, "strategy_hash", "TEXT")
        watch_names = {r["name"] for r in db.execute("PRAGMA table_info(watchlist)")}
        for name in ("candidate_count", "order_budget_cents"):
            if name not in watch_names:
                add_column("watchlist", name, "INTEGER")
        for name, definition in (("real_strategy_id", "TEXT NOT NULL DEFAULT 'stable'"),
                                 ("real_target_pct", "TEXT"),
                                 ("real_no_partial", "INTEGER NOT NULL DEFAULT 0"),
                                 ("real_status", "TEXT NOT NULL DEFAULT 'watching'")):
            if name not in watch_names:
                add_column("watchlist", name, definition)
        db.execute("UPDATE watchlist SET real_target_pct=target_pct WHERE real_target_pct IS NULL AND real_no_partial=0")
        real_names = {r["name"] for r in db.execute("PRAGMA table_info(real_positions)")}
        for name, definition in (("strategy_id", "TEXT NOT NULL DEFAULT 'stable'"),
                                 ("no_partial", "INTEGER NOT NULL DEFAULT 0")):
            if name not in real_names:
                add_column("real_positions", name, definition)
        db.execute("INSERT OR IGNORE INTO real_strategy_schedule VALUES('1900-01-01','stable',?)",
                   (now_utc_iso(),))
        alert_names = {r["name"] for r in db.execute("PRAGMA table_info(alerts)")}
        if "resolved_at" not in alert_names:
            add_column("alerts", "resolved_at", "TEXT")
        for column, definition in (
            ("action_status", "TEXT"), ("acted_at", "TEXT"),
            ("reference_price_cents", "INTEGER"),
            ("real_position_id", "INTEGER REFERENCES real_positions(id)"),
            ("action_kind", "TEXT"),
        ):
            if column not in alert_names:
                add_column("alerts", column, definition)
        db.execute("""
            CREATE INDEX IF NOT EXISTS ix_alerts_real_history
            ON alerts(action_status,acted_at DESC,id DESC)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS ix_events_kind_at
            ON events(kind,at DESC,seq DESC)
        """)
        if not db.execute("SELECT 1 FROM system_status WHERE key='legacy_actual_import'").fetchone():
            db.execute("""
                INSERT OR IGNORE INTO real_positions(
                  code,watch_id,entry_date,entry_price_cents,target_pct,initial_stop_cents,
                  trailing_line_cents,prior_peak_cents,peak_asof_date,partial_done,
                  status,management_mode,created_at,closed_at,legacy_actual_id
                ) SELECT code,watch_id,entry_date,entry_price_cents,target_pct,initial_stop_cents,
                  trailing_line_cents,prior_peak_cents,peak_asof_date,partial_done,
                  status,CASE WHEN watch_id IS NULL THEN 'user_managed' ELSE 'system' END,
                  created_at,closed_at,id FROM actual_positions
            """)
            db.execute("INSERT OR IGNORE INTO system_status VALUES('legacy_actual_import',?,?)",
                       (json.dumps({"done": True}), now_utc_iso()))
        # Old sell alerts have no frozen price, so they cannot be confirmed as proxy fills.
        db.execute("""
            UPDATE alerts SET action_status='legacy'
            WHERE kind='actual_sell' AND action_status IS NULL
        """)
        db.execute("""
            UPDATE alerts SET reference_price_cents=(
              SELECT w.buy_limit_cents FROM events e JOIN watchlist w ON w.id=e.watch_id
              WHERE e.event_key=alerts.event_key
            ), action_kind='buy', action_status=CASE
              WHEN substr(event_key,10,10)=? THEN 'pending' ELSE 'expired' END
            WHERE kind='real_buy' AND action_status IS NULL
        """, (now_cn().date().isoformat(),))
        db.execute("""
            UPDATE alerts SET action_status='legacy'
            WHERE kind='real_buy' AND action_status='pending'
              AND (reference_price_cents IS NULL OR EXISTS(
                SELECT 1 FROM events e JOIN real_positions p ON p.watch_id=e.watch_id
                WHERE e.event_key=alerts.event_key
              ))
        """)
        digest, manifest = strategy_manifest()
        db.execute(
            "INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?,?)",
            (digest, manifest["id"], json.dumps(manifest, ensure_ascii=False),
             now_utc_iso()),
        )
        existing = db.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
        if not existing:
            db.execute(
                """INSERT INTO settings VALUES(NULL,?,?,?,?,?,?,?,?,?)""",
                ("1900-01-01", 100_000_000, 7_500_000, "0.00025", "5",
                 "0.00001", "0.0005", "0", now_utc_iso()),
            )
        # One-time upgrade: honor the latest old-style reservation immediately
        # and remove the future schedule so it cannot silently override it later.
        with transaction(db):
            if not db.execute("""
                SELECT 1 FROM system_status WHERE key='real_strategy_immediate_migrated'
            """).fetchone():
                today = now_cn().date().isoformat()
                pending = db.execute("""
                    SELECT strategy_id FROM real_strategy_schedule
                    WHERE effective_date>? ORDER BY created_at DESC,effective_date DESC LIMIT 1
                """, (today,)).fetchone()
                if pending:
                    apply_real_strategy(db, today, pending["strategy_id"])
                set_status(db, "real_strategy_immediate_migrated",
                           {"migrated_pending": bool(pending)})


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


def setting_for(db: sqlite3.Connection, on_date: str) -> dict:
    row = db.execute(
        "SELECT * FROM settings WHERE effective_date<=? ORDER BY effective_date DESC LIMIT 1",
        (on_date,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Settings have not been initialized")
    return dict(row)


def available_budget(db: sqlite3.Connection, on_date: str, account: str = "stable") -> int:
    if account not in ("stable", "aggressive"):
        raise ValueError(account)
    current = setting_for(db, on_date)
    if account == "aggressive":
        closed = db.execute(
            "SELECT COALESCE(SUM(closed_pnl_cents),0) FROM aggressive_positions WHERE status='closed'"
        ).fetchone()[0]
        held = db.execute(
            "SELECT COALESCE(SUM(reserved_cents),0) FROM aggressive_positions WHERE status='open'"
        ).fetchone()[0]
        pending = db.execute("""
            SELECT COALESCE(SUM(o.reserved_cents),0) FROM aggressive_orders o
            JOIN watchlist w ON w.id=o.watch_id
            WHERE w.entry_date=? AND o.status='watching'
        """, (on_date,)).fetchone()[0]
        return int(100_000_000 + closed - held - pending)
    closed = db.execute(
        "SELECT COALESCE(SUM(closed_pnl_cents),0) FROM positions WHERE status='closed'"
    ).fetchone()[0]
    held = db.execute(
        "SELECT COALESCE(SUM(reserved_cents),0) FROM positions WHERE status='open'"
    ).fetchone()[0]
    pending = db.execute(
        "SELECT COALESCE(SUM(reserved_cents),0) FROM watchlist WHERE entry_date=? AND status='watching'",
        (on_date,),
    ).fetchone()[0]
    return int(current["capital_cents"] + closed - held - pending)


def cash_balance(db: sqlite3.Connection, on_date: str, account: str = "stable") -> int:
    if account not in ("stable", "aggressive"):
        raise ValueError(account)
    if account == "aggressive":
        flows = db.execute("SELECT COALESCE(SUM(cash_delta_cents),0) FROM aggressive_legs").fetchone()[0]
        return int(100_000_000 + flows)
    capital = setting_for(db, on_date)["capital_cents"]
    flows = db.execute("SELECT COALESCE(SUM(cash_delta_cents),0) FROM virtual_legs").fetchone()[0]
    return int(capital + flows)


def virtual_funding_mode(db: sqlite3.Connection, account: str) -> str:
    if account not in ("stable", "aggressive"):
        raise ValueError(account)
    row = db.execute("SELECT mode FROM virtual_funding_modes WHERE account=?",
                     (account,)).fetchone()
    return str(row[0]) if row else "leveraged_5x"


def real_strategy_for(db: sqlite3.Connection, on_date: str) -> str:
    row = db.execute("""
        SELECT strategy_id FROM real_strategy_schedule
        WHERE effective_date<=? ORDER BY effective_date DESC LIMIT 1
    """, (on_date,)).fetchone()
    return str(row[0]) if row else "stable"


def apply_real_strategy(db: sqlite3.Connection, day: str, strategy_id: str) -> int:
    """Set the active real strategy and refresh today's untriggered buy plans.

    Caller holds an immediate transaction. Existing alerts and positions remain frozen.
    """
    if strategy_id not in ("stable", "aggressive"):
        raise ValueError(strategy_id)
    db.execute("DELETE FROM real_strategy_schedule WHERE effective_date>?", (day,))
    db.execute("""
        INSERT INTO real_strategy_schedule(effective_date,strategy_id,created_at)
        VALUES(?,?,?) ON CONFLICT(effective_date) DO UPDATE SET
          strategy_id=excluded.strategy_id,created_at=excluded.created_at
    """, (day, strategy_id, now_utc_iso()))
    updated_plans = 0
    for watch in db.execute("""
        SELECT w.id,w.ret20,w.candidate_count FROM watchlist AS w
        WHERE w.entry_date=? AND w.real_status='watching'
          AND NOT EXISTS (
            SELECT 1 FROM alerts AS a
            WHERE a.event_key='real_buy:' || w.entry_date || ':' || w.code
          )
          AND NOT EXISTS (
            SELECT 1 FROM real_positions AS p WHERE p.watch_id=w.id
          )
    """, (day,)).fetchall():
        target, no_partial = exit_plan(
            float(watch["ret20"]), watch["candidate_count"] or 0, strategy_id)
        db.execute("""
            UPDATE watchlist
            SET real_strategy_id=?,real_target_pct=?,real_no_partial=?
            WHERE id=?
        """, (strategy_id, str(target) if target is not None else None,
              int(no_partial), watch["id"]))
        updated_plans += 1
    return updated_plans


def append_event(db: sqlite3.Connection, key: str, kind: str, code: str | None,
                 watch_id: int | None, position_id: int | None, payload: dict) -> int:
    db.execute(
        "INSERT OR IGNORE INTO events(event_key,kind,code,watch_id,position_id,at,payload) VALUES(?,?,?,?,?,?,?)",
        (key, kind, code, watch_id, position_id, now_utc_iso(),
         json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )
    return int(db.execute("SELECT seq FROM events WHERE event_key=?", (key,)).fetchone()[0])


def alert(db: sqlite3.Connection, key: str, kind: str, level: str, title: str,
          body: str, code: str | None = None, *, reference_price_cents: int | None = None,
          real_position_id: int | None = None, action_kind: str | None = None) -> None:
    db.execute(
        """INSERT OR IGNORE INTO alerts(
             event_key,kind,level,title,body,code,created_at,action_status,
             reference_price_cents,real_position_id,action_kind
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (key, kind, level, title, body, code, now_utc_iso(),
         "pending" if action_kind else None,
         reference_price_cents, real_position_id, action_kind),
    )


def set_status(db: sqlite3.Connection, key: str, value: object) -> None:
    db.execute(
        """INSERT INTO system_status(key,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), now_utc_iso()),
    )


def next_effective_calendar_date(today: date) -> str:
    return (today + timedelta(days=1)).isoformat()
