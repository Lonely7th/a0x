from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

CENT = Decimal("0.01")


def dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def cents(value: object) -> int:
    return int((dec(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def yuan(value_cents: int) -> Decimal:
    return Decimal(value_cents) / 100


def money(value: object) -> Decimal:
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def floor_cent(value: object) -> Decimal:
    return dec(value).quantize(CENT, rounding=ROUND_DOWN)


def buy_cost(price: Decimal, shares: int, commission_rate: Decimal,
             min_commission: Decimal, transfer_rate: Decimal,
             slippage: Decimal) -> tuple[int, int]:
    gross = money(price * shares * (1 + slippage))
    fee = money(max(min_commission, gross * commission_rate) + gross * transfer_rate)
    return cents(gross + fee), cents(fee)


def sell_proceeds(price: Decimal, shares: int, commission_rate: Decimal,
                  min_commission: Decimal, transfer_rate: Decimal,
                  stamp_rate: Decimal, slippage: Decimal) -> tuple[int, int]:
    gross = money(price * shares * (1 - slippage))
    fee = money(max(min_commission, gross * commission_rate)
                + gross * (transfer_rate + stamp_rate))
    return cents(gross - fee), cents(fee)


def planned_shares(limit: Decimal, budget_cents: int, settings: dict) -> int:
    commission = dec(settings["commission_rate"])
    minimum = dec(settings["minimum_commission"])
    transfer = dec(settings["transfer_rate"])
    slip = dec(settings["slippage_rate"])
    guess = int(yuan(budget_cents) / (limit * (1 + slip)) / 100) * 100
    while guess > 0:
        cost, _ = buy_cost(limit, guess, commission, minimum, transfer, slip)
        if cost <= budget_cents:
            return guess
        guess -= 100
    return 0
