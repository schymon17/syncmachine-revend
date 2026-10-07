"""Logging: a rotating local file, plus warnings and errors for the panel.

Records at WARNING and above are buffered in SQLite and uploaded to
/v2/logs in batches - including those written while the machine was
offline. Anything secret must never be logged; ``redact`` masks known keys
in the structured context as a last line of defence.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import encode_body
from .store import Store

SECRET_KEYS = re.compile(r"secret|password|passwd|token|signature|authorization", re.IGNORECASE)
LOG_FILE_BYTES = 5 * 1024 * 1024
LOG_FILE_COUNT = 5
UPLOAD_BATCH = 200
# The API accepts context up to 8 KiB of JSON per entry.
CONTEXT_MAX_BYTES = 6000


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def fit_context(context: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(context, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) <= CONTEXT_MAX_BYTES:
        return context
    return {"truncated": encoded[: CONTEXT_MAX_BYTES // 2]}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if context:
            entry["context"] = redact(context)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class PanelHandler(logging.Handler):
    """Buffers WARNING+ records for upload. Never raises into the caller."""

    LEVELS = {"WARNING": "warning", "ERROR": "error", "CRITICAL": "critical"}

    def __init__(self, store: Store):
        super().__init__(level=logging.WARNING)
        self._store = store

    def emit(self, record: logging.LogRecord) -> None:
        # Problems with uploading logs themselves stay in the local file -
        # otherwise a rejected upload would produce a new upload forever.
        if getattr(record, "local_only", False):
            return
        try:
            context = redact(getattr(record, "context", None) or {})
            if record.exc_info:
                context["exception"] = logging.Formatter().formatException(record.exc_info)[-3000:]
            context = fit_context(context)
            self._store.buffer_log(
                datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                self.LEVELS.get(record.levelname, "warning"),
                record.getMessage(),
                context or None,
            )
        except Exception:  # noqa: BLE001 - logging must not break the agent
            self.handleError(record)


def setup(log_dir: Path, store: Store | None, debug: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "agent.log", maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(console)

    if store is not None:
        root.addHandler(PanelHandler(store))

    # Third-party chatter (urllib3 retries, pymysql) stays out of the panel.
    for noisy in ("urllib3", "requests", "pymysql"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.getLogger(noisy).propagate = False


def queue_upload(store: Store, machine_id: str, url: str) -> int:
    """Move buffered log records into the outbox as /logs messages."""
    with store.transaction() as db:
        entries = store.take_logs(UPLOAD_BATCH, db=db)
        if not entries:
            return 0
        for entry in entries:
            if entry["context"] is None:
                del entry["context"]
        store.enqueue("logs", url, encode_body({"machineId": machine_id, "entries": entries}), db=db)
    return len(entries)
