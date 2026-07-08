from __future__ import annotations

import json
import queue
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import data_path


class LogStore:
    def __init__(self, path: Path | None = None):
        self.path = path or data_path("sync.log.ndjson")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()

    def log(self, level: str, message: str, **ctx: Any) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "message": message,
            "ctx": ctx,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self.events.put(row)

