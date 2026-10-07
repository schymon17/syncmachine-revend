"""Taking a machine over from the old PHP agent (``bin/sync.php`` + ``daemon.bat``).

The installer reads the old agent's database settings and its last cursor,
so the new agent starts where the old one stopped, with an overlap that the
server deduplicates by coupon (a resent coupon never loses its redeemed
state). The old agent's outbox triggers are removed: they keep writing to
``sync_outbox`` and a broken trigger would fail the machine's own writes.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .machine_db import DbSettings, MachineDb

log = logging.getLogger(__name__)

# Resend this much before the old agent's last cursor; duplicates are harmless.
OVERLAP_SECONDS = 3600
# Without a usable cursor, resend the last day.
FALLBACK_SECONDS = 24 * 3600


@dataclass
class LegacyAgent:
    root: Path
    config: dict[str, Any]
    snapshot: dict[str, Any]
    queue_lines: list[str]

    @property
    def daemon_bat(self) -> Path:
        return self.root / "daemon.bat"

    def db_settings(self) -> DbSettings | None:
        db = self.config.get("db")
        if not isinstance(db, dict) or not db.get("database"):
            return None
        return DbSettings(
            host=str(db.get("host") or "127.0.0.1"),
            port=int(db.get("port") or 3306),
            database=str(db["database"]),
            user=str(db.get("username") or db.get("user") or "root"),
            password=str(db.get("password") or ""),
        )


def is_legacy_root(path: Path) -> bool:
    return (path / "bin" / "sync.php").is_file() and (path / "data" / "app.config.json").is_file()


def find(candidates: Iterable[Path]) -> LegacyAgent | None:
    for candidate in candidates:
        for path in (candidate, *candidate.parents):
            if is_legacy_root(path):
                return load(path)
    return None


def load(root: Path) -> LegacyAgent:
    config = _read_json(root / "data" / "app.config.json") or {}
    paths = config.get("paths") if isinstance(config.get("paths"), dict) else {}
    snapshot = _read_json(_resolve(root, paths.get("snapshot"), "data/snapshot.json")) or {}
    queue_file = _resolve(root, paths.get("queue"), "data/offline-queue.json")
    lines = []
    if queue_file.is_file():
        lines = [
            line
            for line in queue_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    return LegacyAgent(root, config, snapshot, lines)


def cursors(legacy: LegacyAgent | None, db: MachineDb, now: float | None = None) -> tuple[int, int]:
    """First ``user_transaction`` and ``empty_record`` ids the new agent sends."""
    now = now if now is not None else time.time()
    since = now - FALLBACK_SECONDS

    if legacy is not None:
        last_sync = _number(legacy.snapshot.get("user_transaction_lastSync"))
        if last_sync:
            since = last_sync - OVERLAP_SECONDS
        # Transactions the old agent queued offline and never delivered.
        for line in legacy.queue_lines:
            queued_at = _queued_at(line)
            if queued_at is not None:
                since = min(since, queued_at - OVERLAP_SECONDS)

    row = db.query("SELECT MIN(id) AS id FROM user_transaction WHERE dateline >= %s", (int(since),))[0]
    if row["id"] is None:
        row = db.query("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM user_transaction")[0]
    transactions_from = int(row["id"])

    last_bin = _number(legacy.snapshot.get("empty_records_last_id")) if legacy is not None else None
    if last_bin is not None:
        bins_from = int(last_bin) + 1
    else:
        row = db.query(
            "SELECT MIN(id) AS id FROM empty_record WHERE dateline >= %s", (int(now - 7 * 86400),)
        )[0]
        if row["id"] is None:
            row = db.query("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM empty_record")[0]
        bins_from = int(row["id"])

    return transactions_from, bins_from


def drop_triggers(db: MachineDb) -> list[str]:
    """Remove the old agents' triggers that write to ``sync_outbox``; the machine's own triggers stay."""
    rows = db.query(
        "SELECT TRIGGER_NAME AS name, ACTION_STATEMENT AS body FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME LIKE 'sync\\_%%'"
    )
    dropped = []
    for row in rows:
        if "sync_outbox" in str(row["body"]):
            db.execute(f"DROP TRIGGER IF EXISTS `{row['name']}`")
            dropped.append(str(row["name"]))
    return dropped


def _resolve(root: Path, configured: Any, default: str) -> Path:
    path = Path(str(configured or default).replace("\\", "/"))
    return path if path.is_absolute() else root / path


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _queued_at(line: str) -> float | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    value = entry.get("queuedAt")
    number = _number(value)
    if number is not None:
        return number
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
