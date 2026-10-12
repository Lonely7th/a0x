from __future__ import annotations

from datetime import date, datetime, timedelta

from assistant.config import SHANGHAI, UTC
from assistant.quotes import PublicQuoteProvider, Quote, QuoteRouter


def sample(code: str, *, age: int = 1) -> Quote:
    source = datetime(2026, 4, 2, 10, 0, tzinfo=SHANGHAI)
    return Quote(
        code=code, provider="test", source_time=source,
        received_at=source.astimezone(UTC) + timedelta(seconds=age),
        last_cents=1000, open_cents=1010, preclose_cents=1000,
        bid_cents=1000, ask_cents=1000, bid_volume=10000,
        ask_volume=10000, raw_hash=code, name="测试股份",
    )


class Provider:
    def __init__(self, name: str, quotes: dict[str, Quote]):
        self.name = name
        self.quotes = quotes
        self.calls: list[list[str]] = []

    def fetch(self, codes: list[str], timeout: float) -> dict[str, Quote]:
        self.calls.append(codes)
        return {code: self.quotes[code] for code in codes if code in self.quotes}


def test_missing_stock_does_not_block_valid_stocks():
    a, b, c = "sh.600001", "sz.000001", "sz.300001"
    primary = Provider("tencent", {a: sample(a), b: sample(b, age=30)})
    backup = Provider("sina", {b: sample(b)})
    router = QuoteRouter(primary, backup, 10, 2)
    found, health = router.fetch([a, b, c], date(2026, 4, 2).isoformat())
    assert set(found) == {a, b}
    assert found[a].provider == "test"
    assert backup.calls == [[b, c]]
    assert not health["healthy"]
    assert health["missing_codes"] == [c]


def test_full_fallback_recovers_batch():
    codes = ["sh.600001"]
    primary = Provider("tencent", {})
    backup = Provider("sina", {codes[0]: sample(codes[0])})
    router = QuoteRouter(primary, backup, 10, 2)
    found, health = router.fetch(codes, "2026-04-02")
    assert set(found) == set(codes)
    assert health["healthy"]
    assert health["provider"] == "sina"


def test_public_provider_batches_large_monitoring_list(monkeypatch):
    provider = PublicQuoteProvider("tencent")
    calls = []
    def fake_batch(codes, timeout):
        calls.append(codes)
        return {code: sample(code) for code in codes}
    monkeypatch.setattr(provider, "_fetch_batch", fake_batch)
    codes = [f"sh.{i:06d}" for i in range(125)]
    assert len(provider.fetch(codes, 2)) == 125
    assert [len(chunk) for chunk in calls] == [60, 60, 5]
