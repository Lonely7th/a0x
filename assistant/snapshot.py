from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import duckdb

from .config import Config, now_utc_iso, strategy_manifest
from .worker import singleton


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_snapshot(config: Config, output: Path) -> dict:
    """Copy a stopped worker's DuckDB and a consistent SQLite snapshot."""
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Snapshot destination already exists: {output}")
    if not config.state_db.is_file() or not config.market_db.is_file():
        raise FileNotFoundError("Both state and market databases must exist")

    # The worker uses the same lock. Hold it until both databases are copied.
    with singleton(config.state_db.with_suffix(".worker.lock")):
        market = duckdb.connect(str(config.market_db))
        try:
            market.execute("CHECKPOINT")
        finally:
            market.close()
        if Path(f"{config.market_db}.wal").exists():
            raise RuntimeError("Market database WAL remains; stop other database users first")

        output.mkdir(parents=True)
        state_copy = output / "state.sqlite"
        market_copy = output / "market_full.duckdb"
        with sqlite3.connect(config.state_db) as source:
            with sqlite3.connect(state_copy) as destination:
                source.backup(destination)
        with sqlite3.connect(state_copy) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite snapshot failed integrity check")
        shutil.copy2(config.market_db, market_copy)
        market_check = duckdb.connect(str(market_copy), read_only=True)
        try:
            market_check.execute("SELECT 1").fetchone()
        finally:
            market_check.close()

        strategy_sha, _ = strategy_manifest()
        manifest = {
            "created_at_utc": now_utc_iso(),
            "strategy_sha256": strategy_sha,
            "files": {
                path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
                for path in (state_copy, market_copy)
            },
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return manifest
