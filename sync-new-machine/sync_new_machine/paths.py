from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "RevendSyncNew"


def app_dir() -> Path:
    root = os.environ.get("REVEND_SYNC_HOME")
    if root:
        return Path(root)
    return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME


def ensure_app_dir() -> Path:
    root = app_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def data_path(name: str) -> Path:
    return ensure_app_dir() / name

