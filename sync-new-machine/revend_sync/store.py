"""Durable local state in SQLite: the outbox, dead letters, cursors, log buffer.

The outbox is the only way data leaves the machine. A message is written
here - together with the cursor that produced it, in one SQLite
transaction - before anything is sent, so a crash, a power cut or a week
offline loses nothing and never sends the same rows twice.

Each message keeps its exact body bytes and idempotency key for every retry,
as Machine API v2 requires.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    body BLOB NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (status, next_attempt_at, id);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_transactions (
    coupon TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    queued_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS log_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    context TEXT
);
"""

# Rejected messages are kept this long for diagnosis, then dropped.
DEAD_RETENTION_SECONDS = 30 * 86400
# Fingerprints of sent transactions - long enough to cover any resend window.
SENT_RETENTION_SECONDS = 14 * 86400
# Logs are useful, but must never fill the disk during a long outage.
LOG_BUFFER_MAX = 5000


@dataclass
class Message:
    id: int
    kind: str
    url: str
    body: bytes
    idempotency_key: str
    created_at: float
    attempts: int


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL + FULL: survives a power cut on the machine without a corrupt file.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    # --- outbox -----------------------------------------------------------

    def enqueue(
        self,
        kind: str,
        url: str,
        body: bytes,
        *,
        db: sqlite3.Connection | None = None,
        replace_pending: bool = False,
    ) -> int:
        """Queue a message. ``replace_pending`` keeps only the newest unsent one of the kind (status)."""
        conn = db or self._db
        now = time.time()
        if replace_pending:
            conn.execute("DELETE FROM outbox WHERE kind = ? AND status = 'pending'", (kind,))
        cursor = conn.execute(
            "INSERT INTO outbox (kind, url, body, idempotency_key, created_at, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, url, body, uuid.uuid4().hex, now, now),
        )
        return int(cursor.lastrowid)

    def due(self, limit: int = 20, now: float | None = None) -> list[Message]:
        rows = self._db.execute(
            "SELECT id, kind, url, body, idempotency_key, created_at, attempts FROM outbox "
            "WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?",
            (now if now is not None else time.time(), limit),
        ).fetchall()
        return [
            Message(
                r["id"],
                r["kind"],
                r["url"],
                bytes(r["body"]),
                r["idempotency_key"],
                r["created_at"],
                r["attempts"],
            )
            for r in rows
        ]

    def delivered(self, message_id: int) -> None:
        self._db.execute("DELETE FROM outbox WHERE id = ?", (message_id,))

    def retry_later(self, message_id: int, delay: float, error: str, *, new_key: bool = False) -> None:
        if new_key:
            self._db.execute(
                "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?, idempotency_key = ? "
                "WHERE id = ?",
                (time.time() + delay, error[:1000], uuid.uuid4().hex, message_id),
            )
        else:
            self._db.execute(
                "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?, last_error = ? WHERE id = ?",
                (time.time() + delay, error[:1000], message_id),
            )

    def reject(self, message_id: int, error: str) -> None:
        self._db.execute(
            "UPDATE outbox SET status = 'dead', attempts = attempts + 1, last_error = ?, next_attempt_at = ? "
            "WHERE id = ?",
            (error[:2000], time.time(), message_id),
        )

    def stats(self) -> dict[str, Any]:
        now = time.time()
        row = self._db.execute(
            "SELECT "
            " SUM(status = 'pending' AND attempts = 0) AS pending,"
            " SUM(status = 'pending' AND attempts > 0) AS failed,"
            " SUM(status = 'dead') AS dead,"
            " MIN(CASE WHEN status = 'pending' THEN created_at END) AS oldest "
            "FROM outbox"
        ).fetchone()
        return {
            "pending": int(row["pending"] or 0),
            "failed": int(row["failed"] or 0),
            "dead": int(row["dead"] or 0),
            "oldest_pending_at": row["oldest"],
            "checked_at": now,
        }

    def dead_letters(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, kind, created_at, attempts, last_error FROM outbox WHERE status = 'dead' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def requeue_dead(self) -> int:
        """Service action: send rejected messages again (after a server-side fix)."""
        cursor = self._db.execute(
            "UPDATE outbox SET status = 'pending', next_attempt_at = ?, idempotency_key = lower(hex(randomblob(16))) "
            "WHERE status = 'dead'",
            (time.time(),),
        )
        return cursor.rowcount

    # --- state ------------------------------------------------------------

    def get(self, key: str, default: Any = None, *, db: sqlite3.Connection | None = None) -> Any:
        row = (db or self._db).execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any, *, db: sqlite3.Connection | None = None) -> None:
        (db or self._db).execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # --- sent transaction fingerprints ------------------------------------

    def sent_transaction(
        self, coupon: str, *, db: sqlite3.Connection | None = None
    ) -> tuple[str, int] | None:
        row = (
            (db or self._db)
            .execute("SELECT fingerprint, item_count FROM sent_transactions WHERE coupon = ?", (coupon,))
            .fetchone()
        )
        return (row["fingerprint"], int(row["item_count"])) if row else None

    def remember_transaction(
        self, coupon: str, fingerprint: str, item_count: int, *, db: sqlite3.Connection
    ) -> None:
        db.execute(
            "INSERT INTO sent_transactions (coupon, fingerprint, item_count, queued_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(coupon) DO UPDATE SET fingerprint = excluded.fingerprint, item_count = excluded.item_count, "
            "queued_at = excluded.queued_at",
            (coupon, fingerprint, item_count, time.time()),
        )

    # --- logs -------------------------------------------------------------

    def buffer_log(self, logged_at: str, level: str, message: str, context: dict[str, Any] | None) -> None:
        self._db.execute(
            "INSERT INTO log_buffer (logged_at, level, message, context) VALUES (?, ?, ?, ?)",
            (logged_at, level, message[:2000], json.dumps(context, default=str) if context else None),
        )

    def take_logs(self, limit: int, *, db: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT id, logged_at, level, message, context FROM log_buffer ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        if rows:
            db.execute("DELETE FROM log_buffer WHERE id <= ?", (rows[-1]["id"],))
        return [
            {
                "loggedAt": r["logged_at"],
                "level": r["level"],
                "message": r["message"],
                "context": json.loads(r["context"]) if r["context"] else None,
            }
            for r in rows
        ]

    # --- housekeeping -----------------------------------------------------

    def prune(self) -> None:
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "DELETE FROM outbox WHERE status = 'dead' AND next_attempt_at < ?",
                (now - DEAD_RETENTION_SECONDS,),
            )
            db.execute("DELETE FROM sent_transactions WHERE queued_at < ?", (now - SENT_RETENTION_SECONDS,))
            db.execute(
                "DELETE FROM log_buffer WHERE id <= (SELECT MAX(id) FROM log_buffer) - ?", (LOG_BUFFER_MAX,)
            )
