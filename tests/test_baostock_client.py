from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

from assistant.baostock_client import BaoStockClient, BaoStockUnavailable, _serve


def _fake_server(pipe):
    """Use the real protocol with a fake SDK; never contact external services."""
    class Rows:
        error_code = "0"
        error_msg = ""
        fields = ["code", "date"]

        def __init__(self, mode):
            self.mode = mode
            self.index = 0

        def next(self):
            if self.mode == "page_hang":
                time.sleep(3600)
            self.index += 1
            return self.index <= 2

        def get_row_data(self):
            return ["sh.600000", f"2026-10-{self.index:02d}"]

    def query(code, *args, **kwargs):
        if code == "query_hang":
            time.sleep(3600)
        if code == "disconnect":
            raise ConnectionError("connection reset")
        return Rows(code)

    sys.modules["baostock"] = SimpleNamespace(
        login=lambda: SimpleNamespace(error_code="0"),
        logout=lambda: SimpleNamespace(error_code="0"),
        query_history_k_data_plus=query,
    )
    _serve(pipe)


def _login_hangs(pipe):
    time.sleep(3600)


@pytest.mark.parametrize("mode", ["query_hang", "page_hang"])
def test_timeout_kills_session_including_pagination_and_can_reconnect(mode):
    client = BaoStockClient(timeout=5, target=_fake_server)
    try:
        client.login()
        process = client.process
        client.timeout = 0.15
        started = time.monotonic()
        with pytest.raises(BaoStockUnavailable, match="timed out"):
            client.query_history_k_data_plus(mode)
        assert time.monotonic() - started < 3
        assert client.process is None and client.pipe is None
        assert process._closed
        client.logout()  # Broken connection must not attempt another network call.
        client.timeout = 5
        client.login()
        rows = client.query_history_k_data_plus("ok")
        assert rows == [{"code": "sh.600000", "date": "2026-10-01"},
                        {"code": "sh.600000", "date": "2026-10-02"}]
    finally:
        client.logout()
    assert client.process is None


def test_login_timeout_releases_process():
    client = BaoStockClient(timeout=0.15, target=_login_hangs)
    with pytest.raises(BaoStockUnavailable, match="timed out"):
        client.login()
    assert client.process is None and client.pipe is None


def test_connection_error_aborts_session_and_logs_symbol(caplog):
    client = BaoStockClient(timeout=5, target=_fake_server)
    try:
        client.login()
        with pytest.raises(BaoStockUnavailable, match="connection reset"):
            client.query_history_k_data_plus("disconnect")
        assert client.process is None
        assert "disconnect" in caplog.text
    finally:
        client.close()
