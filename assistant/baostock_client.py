"""Bound BaoStock login, queries and pagination in an isolated, disposable process."""
from __future__ import annotations

import logging
import multiprocessing as mp
from types import SimpleNamespace

LOG = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 60


class BaoStockUnavailable(RuntimeError):
    """The session failed; abort this synchronization and retry with a new session."""


def _serve(pipe):
    # Spawn deliberately: never inherit the parent's DuckDB connection or threads.
    import baostock as bs

    try:
        while True:
            name, args, kwargs = pipe.recv()
            try:
                result = getattr(bs, name)(*args, **kwargs)
                if result.error_code != "0":
                    raise RuntimeError(result.error_msg)
                rows = []
                if name not in ("login", "logout"):
                    while result.next():
                        rows.append(dict(zip(result.fields, result.get_row_data())))
                    if result.error_code != "0":
                        raise RuntimeError(result.error_msg)
                pipe.send((True, rows))
            except Exception as exc:
                pipe.send((False, f"{type(exc).__name__}: {exc}"))
            if name == "logout":
                break
    except (EOFError, BrokenPipeError):
        pass
    finally:
        pipe.close()


class BaoStockClient:
    def __init__(self, progress=None, timeout=REQUEST_TIMEOUT_SECONDS, target=_serve):
        self.progress = progress
        self.timeout = timeout
        self.target = target
        self.process = None
        self.pipe = None

    def close(self):
        if self.pipe is not None:
            self.pipe.close()
            self.pipe = None
        if self.process is not None:
            if self.process.pid is None:
                self.process.close()
                self.process = None
                return
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            self.process.close()
            self.process = None

    def login(self):
        self.close()
        ctx = mp.get_context("spawn")
        self.pipe, child = ctx.Pipe()
        self.process = ctx.Process(target=self.target, args=(child,), daemon=True)
        try:
            self.process.start()
            child.close()
            self._call("login")
        except BaseException:
            child.close()
            self.close()
            raise
        return SimpleNamespace(error_code="0", error_msg="")

    def logout(self):
        # No network logout on a broken session; always release local resources.
        try:
            if self.pipe is not None:
                self._call("logout")
        finally:
            self.close()

    def _call(self, name, *args, **kwargs):
        detail = f"BaoStock {name} args={args} kwargs={kwargs}"
        if self.progress:
            self.progress(detail)
        LOG.info("Request started: %s", detail)
        try:
            if self.pipe is None:
                raise BaoStockUnavailable("BaoStock session is closed")
            self.pipe.send((name, args, kwargs))
            if not self.pipe.poll(self.timeout):
                raise BaoStockUnavailable(f"{detail} timed out after {self.timeout}s")
            ok, payload = self.pipe.recv()
            if not ok:
                raise BaoStockUnavailable(f"{detail}: {payload}")
            LOG.info("Request finished: %s; rows=%s", detail, len(payload))
            if self.progress:
                self.progress(detail + " finished")
            return payload
        except (OSError, EOFError, BaoStockUnavailable) as exc:
            self.close()
            LOG.error("Request failed: %s: %s", detail, exc)
            raise BaoStockUnavailable(str(exc)) from exc

    def query_trade_dates(self, *args, **kwargs):
        return self._call("query_trade_dates", *args, **kwargs)

    def query_stock_basic(self, *args, **kwargs):
        return self._call("query_stock_basic", *args, **kwargs)

    def query_history_k_data_plus(self, *args, **kwargs):
        return self._call("query_history_k_data_plus", *args, **kwargs)
