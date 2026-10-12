from __future__ import annotations

import hashlib
import json
import sqlite3

import duckdb
import pytest

from assistant.config import Config
from assistant.snapshot import create_snapshot
from assistant.storage import init_db


def test_snapshot_copies_both_databases_and_verifies_them(tmp_path):
    state = tmp_path / "state.sqlite"
    market = tmp_path / "market.duckdb"
    init_db(state)
    connection = duckdb.connect(str(market))
    connection.execute("CREATE TABLE snapshot_probe AS SELECT 42 AS value")
    connection.close()
    output = tmp_path / "export"

    manifest = create_snapshot(Config(state_db=state, market_db=market), output)

    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest
    for filename, metadata in manifest["files"].items():
        data = (output / filename).read_bytes()
        assert metadata == {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with sqlite3.connect(output / "state.sqlite") as copied_state:
        assert copied_state.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copied_market = duckdb.connect(str(output / "market_full.duckdb"), read_only=True)
    assert copied_market.execute("SELECT value FROM snapshot_probe").fetchone()[0] == 42
    copied_market.close()
    with pytest.raises(FileExistsError):
        create_snapshot(Config(state_db=state, market_db=market), output)
