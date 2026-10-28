"""Track business progress independently of the heartbeat thread."""
from __future__ import annotations

import logging
import threading
import time
from functools import wraps

from .config import now_utc_iso

LOG = logging.getLogger(__name__)
STALL_TIMEOUT_SECONDS = 600


class ProgressTracker:
    def __init__(self, timeout=STALL_TIMEOUT_SECONDS):
        self.timeout = timeout
        self._lock = threading.Lock()
        self.touch("starting")

    def touch(self, stage):
        with self._lock:
            self._stage = stage
            self._at = now_utc_iso()
            self._monotonic = time.monotonic()

    def snapshot(self):
        with self._lock:
            age = time.monotonic() - self._monotonic
            return {"stage": self._stage, "at": self._at,
                    "age_seconds": round(age, 1), "timeout_seconds": self.timeout,
                    "stalled": age > self.timeout}


def market_stage(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        stage = f"{method.__name__} args={args} kwargs={kwargs}"
        self.progress(stage)
        LOG.info("Stage started: %s", stage)
        try:
            result = method(self, *args, **kwargs)
        except BaseException:
            LOG.exception("Stage failed: %s", stage)
            raise
        self.progress(stage + " finished")
        LOG.info("Stage finished: %s", stage)
        return result
    return wrapped
