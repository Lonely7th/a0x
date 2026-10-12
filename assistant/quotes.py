from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import requests

from .config import SHANGHAI, UTC
from .money import cents


@dataclass(frozen=True)
class Quote:
    code: str
    provider: str
    source_time: datetime
    received_at: datetime
    last_cents: int
    open_cents: int
    preclose_cents: int
    bid_cents: int
    ask_cents: int
    bid_volume: int
    ask_volume: int
    raw_hash: str
    name: str = ""

    def age_seconds(self) -> float:
        return (self.received_at - self.source_time.astimezone(UTC)).total_seconds()


class QuoteProvider(Protocol):
    name: str

    def fetch(self, codes: list[str], timeout: float) -> dict[str, Quote]: ...


def wire_symbol(code: str) -> str:
    if code.startswith(("sh.", "sz.")) and len(code) == 9:
        return code.replace(".", "")
    raise ValueError(f"Invalid code: {code}")


def _integer(value: str) -> int:
    try:
        return max(0, int(float(value)))
    except (ValueError, TypeError):
        return 0


class PublicQuoteProvider:
    def __init__(self, name: str, session: requests.Session | None = None):
        if name not in ("tencent", "sina"):
            raise ValueError(name)
        self.name = name
        self.session = session or requests.Session()
        self.session.trust_env = False

    def fetch(self, codes: list[str], timeout: float) -> dict[str, Quote]:
        if not codes:
            return {}
        result = {}
        errors = []
        for offset in range(0, len(codes), 60):
            batch = codes[offset:offset + 60]
            try:
                result.update(self._fetch_batch(batch, timeout))
            except requests.RequestException as exc:
                errors.append(exc)
        if errors and not result:
            raise errors[0]
        return result

    def _fetch_batch(self, codes: list[str], timeout: float) -> dict[str, Quote]:
        symbols = [wire_symbol(c) for c in codes]
        if self.name == "tencent":
            url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
            response = self.session.get(url, timeout=timeout)
        else:
            url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
            response = self.session.get(
                url, timeout=timeout,
                headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
            )
        response.raise_for_status()
        received = datetime.now(UTC)
        payload = response.content.decode("gbk", errors="replace")
        parsed = self._tencent(payload, received) if self.name == "tencent" else self._sina(payload, received)
        return {c: parsed[c] for c in codes if c in parsed}

    def _tencent(self, payload: str, received: datetime) -> dict[str, Quote]:
        result = {}
        for record in payload.split(";"):
            if "~" not in record or "=" not in record:
                continue
            name, values = record.split("=", 1)
            sym = name.strip().removeprefix("v_")
            fields = values.strip('"').split("~")
            if len(fields) < 31 or not sym.startswith(("sh", "sz")):
                continue
            try:
                source_time = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
                code = sym[:2] + "." + sym[2:]
                result[code] = Quote(
                    code, self.name, source_time, received,
                    cents(fields[3]), cents(fields[5]), cents(fields[4]),
                    cents(fields[9]), cents(fields[19]),
                    _integer(fields[10]) * 100, _integer(fields[20]) * 100,
                    hashlib.sha256(record.encode("utf-8")).hexdigest()[:20],
                    fields[1].strip(),
                )
            except (ValueError, IndexError):
                continue
        return result

    def _sina(self, payload: str, received: datetime) -> dict[str, Quote]:
        result = {}
        for record in payload.split(";"):
            if '="' not in record:
                continue
            name, values = record.split('="', 1)
            sym = name.strip().removeprefix("var hq_str_")
            fields = values.rstrip('"').split(",")
            if len(fields) < 32 or not sym.startswith(("sh", "sz")):
                continue
            try:
                source_time = datetime.strptime(
                    fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=SHANGHAI)
                code = sym[:2] + "." + sym[2:]
                result[code] = Quote(
                    code, self.name, source_time, received,
                    cents(fields[3]), cents(fields[1]), cents(fields[2]),
                    cents(fields[11]), cents(fields[21]),
                    _integer(fields[10]), _integer(fields[20]),
                    hashlib.sha256(record.encode("utf-8")).hexdigest()[:20],
                    fields[0].strip(),
                )
            except (ValueError, IndexError):
                continue
        return result


class QuoteRouter:
    def __init__(self, primary: QuoteProvider, backup: QuoteProvider,
                 max_age_seconds: float, timeout_seconds: float):
        self.primary = primary
        self.backup = backup
        self.max_age_seconds = max_age_seconds
        self.timeout_seconds = timeout_seconds
        self.active = "tencent"
        self.failures = 0
        self.last_switch = 0.0
        self.primary_recoveries = 0

    def _valid(self, quotes: dict[str, Quote], codes: list[str],
               trading_date: str) -> dict[str, Quote]:
        return {
            code: quote for code in codes
            if (quote := quotes.get(code)) is not None
            and quote.source_time.date().isoformat() == trading_date
            and 0 <= quote.age_seconds() <= self.max_age_seconds
            and quote.last_cents > 0 and quote.open_cents > 0
        }

    def _try(self, provider: QuoteProvider, codes: list[str], trading_date: str
             ) -> tuple[dict[str, Quote], str]:
        try:
            quotes = self._valid(provider.fetch(codes, self.timeout_seconds),
                                 codes, trading_date)
            return quotes, "" if len(quotes) == len(codes) else "missing_or_stale"
        except (requests.RequestException, ValueError) as exc:
            return {}, type(exc).__name__

    def fetch(self, codes: list[str], trading_date: str) -> tuple[dict[str, Quote], dict]:
        if not codes:
            return {}, {"provider": self.active, "healthy": True, "reason": "no_symbols"}
        preferred = self.primary if self.active == "tencent" else self.backup
        quotes, reason = self._try(preferred, codes, trading_date)
        if len(quotes) == len(codes):
            self.failures = 0
            if self.active == "sina" and time.monotonic() - self.last_switch >= 60:
                probe, _ = self._try(self.primary, codes, trading_date)
                self.primary_recoveries = self.primary_recoveries + 1 if len(probe) == len(codes) else 0
                if self.primary_recoveries >= 3:
                    self.active = "tencent"
                    self.last_switch = time.monotonic()
                    return probe, {"provider": "tencent", "healthy": True, "reason": "recovered"}
            return quotes, {"provider": self.active, "healthy": True, "reason": ""}
        self.failures += 1
        alternate = self.backup if self.active == "tencent" else self.primary
        missing = [code for code in codes if code not in quotes]
        fallback, backup_reason = self._try(alternate, missing, trading_date)
        combined = {**quotes, **fallback}
        if fallback:
            if (not quotes and len(fallback) == len(codes)
                    and self.failures >= 2
                    and time.monotonic() - self.last_switch >= 60):
                self.active = alternate.name
                self.last_switch = time.monotonic()
                self.failures = 0
        uncovered = [code for code in codes if code not in combined]
        return combined, {
            "provider": (preferred.name if not fallback else
                         alternate.name if not quotes else "mixed"),
            "healthy": not uncovered,
            "reason": ("" if not uncovered else
                       f"{preferred.name}:{reason};{alternate.name}:{backup_reason}"),
            "missing_codes": uncovered,
        }
