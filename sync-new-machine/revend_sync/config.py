"""Agent configuration and protected secrets.

``config.json`` holds non-secret settings. The API secret and the machine
database password live in ``secrets.bin``: on Windows encrypted with DPAPI
for the local machine (readable by the agent service and administrators,
useless when copied elsewhere); elsewhere (development) a 0600 file.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .machine_db import DbSettings


def home() -> Path:
    override = os.environ.get("REVEND_SYNC_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ReVend" / "Sync"
    return Path.home() / ".revend-sync"


@dataclass
class Config:
    machine_id: str
    api_base_url: str
    agent_base_url: str
    key_id: str
    secret: str = field(repr=False)
    db: DbSettings = field(default_factory=DbSettings)
    transactions_from_id: int | None = None
    bins_from_id: int | None = None
    coupons_enabled: bool = True
    eans_enabled: bool = True
    adverts_enabled: bool = True
    # The machine's web folders the screen shows adverts from (old agent's defaults).
    adverts_image_dir: str = r"C:\phpStudy\PHPTutorial\WWW\downadpic\img"
    adverts_video_dir: str = r"C:\phpStudy\PHPTutorial\WWW\advideo\video"

    def url(self, endpoint: str) -> str:
        return self.api_base_url.rstrip("/") + "/" + endpoint.lstrip("/")

    def agent_url(self, endpoint: str) -> str:
        return self.agent_base_url.rstrip("/") + "/" + endpoint.lstrip("/")


def load(directory: Path | None = None) -> Config:
    directory = directory or home()
    raw = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    secrets = read_secrets(directory)
    db = raw.get("db", {})
    return Config(
        machine_id=raw["machineId"],
        api_base_url=raw["apiBaseUrl"],
        agent_base_url=raw["agentBaseUrl"],
        key_id=raw["keyId"],
        secret=secrets["secret"],
        db=DbSettings(
            host=db.get("host", "127.0.0.1"),
            port=int(db.get("port", 3306)),
            database=db.get("database", "qcs"),
            user=db.get("user", "root"),
            password=secrets.get("dbPassword", ""),
        ),
        transactions_from_id=raw.get("transactionsFromId"),
        bins_from_id=raw.get("binsFromId"),
        coupons_enabled=bool(raw.get("couponsEnabled", True)),
        eans_enabled=bool(raw.get("eansEnabled", True)),
        adverts_enabled=bool(raw.get("advertsEnabled", True)),
        adverts_image_dir=raw.get("advertsImageDir") or Config.adverts_image_dir,
        adverts_video_dir=raw.get("advertsVideoDir") or Config.adverts_video_dir,
    )


def save(config: Config, directory: Path | None = None) -> None:
    directory = directory or home()
    directory.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {
        "machineId": config.machine_id,
        "apiBaseUrl": config.api_base_url,
        "agentBaseUrl": config.agent_base_url,
        "keyId": config.key_id,
        "db": {
            "host": config.db.host,
            "port": config.db.port,
            "database": config.db.database,
            "user": config.db.user,
        },
        "couponsEnabled": config.coupons_enabled,
        "eansEnabled": config.eans_enabled,
        "advertsEnabled": config.adverts_enabled,
        "advertsImageDir": config.adverts_image_dir,
        "advertsVideoDir": config.adverts_video_dir,
    }
    if config.transactions_from_id is not None:
        raw["transactionsFromId"] = config.transactions_from_id
    if config.bins_from_id is not None:
        raw["binsFromId"] = config.bins_from_id
    _atomic_write(directory / "config.json", json.dumps(raw, indent=2).encode("utf-8"))
    write_secrets(directory, {"secret": config.secret, "dbPassword": config.db.password})


def read_secrets(directory: Path) -> dict[str, str]:
    data = (directory / "secrets.bin").read_bytes()
    if sys.platform == "win32":
        data = _dpapi(data, protect=False)
    return json.loads(data.decode("utf-8"))


def write_secrets(directory: Path, secrets: dict[str, str]) -> None:
    data = json.dumps(secrets).encode("utf-8")
    if sys.platform == "win32":
        data = _dpapi(data, protect=True)
    _atomic_write(directory / "secrets.bin", data, private=True)


def _atomic_write(path: Path, data: bytes, private: bool = False) -> None:
    """Write via a temp file + replace: a crash leaves the old file, never a half-written one."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if private and sys.platform != "win32":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _dpapi(data: bytes, protect: bool) -> bytes:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    local_machine = 0x4  # CRYPTPROTECT_LOCAL_MACHINE
    ui_forbidden = 0x1
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            "revend-sync",
            None,
            None,
            None,
            local_machine | ui_forbidden,
            ctypes.byref(target),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, ui_forbidden, ctypes.byref(target)
        )
    if not ok:
        raise OSError(ctypes.GetLastError(), "DPAPI failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
