from __future__ import annotations

import os
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


def now_cn() -> datetime:
    return datetime.now(SHANGHAI)


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def strategy_manifest() -> tuple[str, dict]:
    path = ROOT / "strategy.json"
    payload = path.read_bytes()
    manifest = json.loads(payload)
    if manifest.get("id") != "oversold_limit_ret20_exit_switch_hold20":
        raise ValueError("Unexpected strategy manifest")
    return hashlib.sha256(payload).hexdigest(), manifest


@dataclass(frozen=True)
class Config:
    state_db: Path
    market_db: Path
    host: str = "127.0.0.1"
    port: int = 8765
    quote_interval_seconds: float = 3.0
    quote_timeout_seconds: float = 2.0
    max_quote_age_seconds: float = 10.0
    observe_only: bool = False
    strategy_id: str = "oversold_limit_ret20_exit_switch_hold20"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            state_db=Path(os.getenv("TA_STATE_DB", str(ROOT / "runtime" / "state.sqlite"))).resolve(),
            market_db=Path(os.getenv("TA_MARKET_DB", str(ROOT.parent / "data" / "market_full.duckdb"))).resolve(),
            host=os.getenv("TA_HOST", "127.0.0.1"),
            port=int(os.getenv("TA_PORT", "8765")),
            quote_interval_seconds=float(os.getenv("TA_QUOTE_INTERVAL", "3")),
            quote_timeout_seconds=float(os.getenv("TA_QUOTE_TIMEOUT", "2")),
            max_quote_age_seconds=float(os.getenv("TA_MAX_QUOTE_AGE", "10")),
            observe_only=os.getenv("TA_OBSERVE_ONLY", "0") != "0",
        )

    def validate(self) -> None:
        if self.quote_interval_seconds < 1 or self.quote_timeout_seconds <= 0:
            raise ValueError("Invalid quote interval or timeout")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("Invalid quote age")
        if not self.market_db.exists():
            raise FileNotFoundError(f"Market database missing: {self.market_db}")
