from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .money import dec

SURGE_CANDIDATE_THRESHOLD = 50
SURGE_ORDER_BUDGET_CENTS = 20_000_000


def order_budget_cents(normal_budget_cents: int, candidate_count: int) -> int:
    return (SURGE_ORDER_BUDGET_CENTS if candidate_count >= SURGE_CANDIDATE_THRESHOLD
            else normal_budget_cents)


def buy_limit_cents(raw_close: object) -> int:
    return int((dec(raw_close) * Decimal("96")).to_integral_value(rounding=ROUND_FLOOR))


def target_pct(ret20: float) -> Decimal:
    return Decimal("0.10") if ret20 <= -0.10 else Decimal("0.01")


def strict_crossing(open_cents: int, last_cents: int, limit_cents: int) -> bool:
    return open_cents > limit_cents and last_cents <= limit_cents - 1


def stop_distance(atr14_adjusted: object, q_close: object,
                  raw_entry_cents: int, raw_signal_close: object) -> Decimal:
    # Signal-day raw/adjusted ratio maps the adjusted ATR into original yuan.
    factor = dec(q_close) / dec(raw_signal_close)
    if factor <= 0:
        raise ValueError("Invalid adjustment factor")
    entry_adjusted = Decimal(raw_entry_cents) / 100 * factor
    raw = Decimal("1.5") * dec(atr14_adjusted) / entry_adjusted
    return max(Decimal("0.02"), min(Decimal("0.08"), raw))


def initial_stop_cents(entry_cents: int, distance: Decimal) -> int:
    return int((Decimal(entry_cents) * (1 - distance)).to_integral_value(rounding=ROUND_FLOOR))


def first_target_cents(entry_cents: int, pct: object) -> int:
    return int((Decimal(entry_cents) * (1 + dec(pct))).to_integral_value(rounding=ROUND_CEILING))


def intraday_profit_crossed(last_cents: int, entry_cents: int, pct: object) -> bool:
    theoretical = Decimal(entry_cents) * (1 + dec(pct))
    return Decimal(last_cents) >= theoretical + 1


def partial_shares(remaining: int) -> int:
    return (remaining // 200) * 100


def trailing_cents(entry_cents: int, prior_peak_cents: int,
                   previous_line_cents: int) -> int:
    calculated = int((Decimal(prior_peak_cents) * Decimal("0.92")).to_integral_value(rounding=ROUND_FLOOR))
    return max(entry_cents, previous_line_cents, calculated)


@dataclass(frozen=True)
class ExitDecision:
    kind: str
    shares: int
    proxy_price_cents: int
    line_cents: int


def virtual_exit(
    *, entry_cents: int, remaining: int, target: object, initial_stop: int,
    trailing_line: int, partial_done: bool, hold_days: int,
    open_cents: int, last_cents: int, is_opening_snapshot: bool,
) -> ExitDecision | None:
    if hold_days <= 0 or remaining <= 0:
        return None
    if hold_days >= 20:
        return ExitDecision("expiry", remaining, last_cents, 0)
    stop = max(initial_stop, trailing_line if partial_done else 0)
    if last_cents <= stop:
        return ExitDecision("stop", remaining, min(last_cents, stop), stop)
    if partial_done:
        return None
    target_line = first_target_cents(entry_cents, target)
    hit = (open_cents >= target_line if is_opening_snapshot
           else intraday_profit_crossed(last_cents, entry_cents, target))
    if not hit:
        return None
    qty = partial_shares(remaining)
    if qty <= 0:
        return None
    proxy = last_cents if is_opening_snapshot else target_line
    return ExitDecision("partial_profit", qty, proxy, target_line)
