from datetime import datetime

from assistant.config import SHANGHAI
from assistant.worker import afterclose_deadline


def test_afterclose_sync_can_catch_up_after_cutoff():
    before = datetime(2026, 10, 12, 19, 0, tzinfo=SHANGHAI)
    after = datetime(2026, 10, 12, 21, 0, tzinfo=SHANGHAI)
    assert afterclose_deadline(before) == before.replace(hour=20, minute=30)
    assert afterclose_deadline(after) is None
