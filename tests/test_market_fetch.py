from __future__ import annotations

import sys
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pandas as pd

from assistant.market import _daily_fetch
from assistant import market as market_module


def _frame() -> pd.DataFrame:
    return pd.DataFrame([{
        "date": date(2026, 10, 9), "open": 10.0, "high": 10.2,
        "low": 9.9, "close": 10.1, "volume": 1000000,
        "amount": 10000000,
    }])


def test_direct_tencent_parser_units_and_ohlc(monkeypatch):
    payload = {
        "code": 0,
        "data": {"sh600000": {"day": [[
            "2026-10-09", "9.70", "9.54", "9.74", "9.50",
            "947958.00", {}, "0.28", "91530.98"
        ]]}}
    }
    class Response:
        text = "kline_day=" + json.dumps(payload)
        def raise_for_status(self):
            return None
    monkeypatch.setattr(market_module.requests, "get",
                        lambda *_args, **_kwargs: Response())
    frame = market_module._tencent_daily(
        "sh600000", date(2026, 10, 1), date(2026, 10, 9), ""
    )
    row = frame.iloc[0]
    assert row["close"] == 9.54
    assert row["high"] == 9.74
    assert row["low"] == 9.50
    assert row["volume"] == 94_795_800
    assert row["amount"] == 915_309_800


def test_daily_fetch_uses_tencent_before_native_sina(monkeypatch):
    def sina(**_kwargs):
        raise AssertionError("Sina should not be called when Tencent succeeds")
    fake = SimpleNamespace(stock_zh_a_daily=sina)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(market_module, "_tencent_daily",
                        lambda *_args: _frame())
    _, rows, source = _daily_fetch("sh.600000", date(2026, 10, 9),
                                   date(2026, 10, 9))
    assert source == "Tencent"
    assert len(rows) == 1


def test_sina_native_calls_are_serialized_on_fallback(monkeypatch):
    active = 0
    maximum = 0
    guard = threading.Lock()

    def primary(*_args):
        raise RuntimeError("primary unavailable")

    def sina(**_kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with guard:
            active -= 1
        return _frame()

    fake = SimpleNamespace(stock_zh_a_daily=sina)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(market_module, "_tencent_daily", primary)
    codes = [f"sh.60000{i}" for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(
            lambda code: _daily_fetch(code, date(2026, 10, 9),
                                      date(2026, 10, 9)), codes
        ))
    assert maximum == 1
    assert all(source == "Sina" and len(rows) == 1
               for _, rows, source in result)
