from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import data_path


CONFIG_FILE = data_path("config.json")


@dataclass
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    database: str = "qcs"
    username: str = "root"
    password: str = "chushengfeng123"


@dataclass
class ApiConfig:
    base_url: str = ""
    token: str = ""
    registration_url: str = "https://panel.revend.pl/api/revend/machine/v2/register"
    config_url: str = "https://panel.revend.pl/api/revend/machine/v2/config"
    timeout_seconds: int = 30
    connect_timeout_seconds: int = 10
    max_payload_bytes: int = 5 * 1024 * 1024


@dataclass
class SyncConfig:
    enabled_transactions: bool = True
    enabled_bins: bool = True
    enabled_status: bool = True
    enabled_heartbeat: bool = True
    enabled_eans: bool = True
    enabled_coupons: bool = True
    enabled_adverts: bool = True
    outbox_poll_seconds: float = 2.0
    heartbeat_interval_seconds: int = 300
    remote_refresh_seconds: int = 300
    transaction_overlap_seconds: int = 300
    transaction_batch: int = 500
    max_transactions_per_payload: int = 120
    max_rows_per_payload: int = 3000
    bins_batch: int = 1000
    install_triggers: bool = True


@dataclass
class AppConfig:
    config_done: bool = False
    machine_id: str = ""
    integration: str = ""
    address: str = ""
    notification_emails: list[str] = field(default_factory=list)
    notification_emails_bcc: list[dict[str, str]] = field(default_factory=list)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)


def _merge_dataclass(cls: type, payload: dict[str, Any]):
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in payload.items() if k in known})


def _from_php_shape(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "configDone" in data:
        out["config_done"] = bool(data["configDone"])
    if "machineId" in data:
        out["machine_id"] = data["machineId"]
    for key in ["integration", "address", "notification_emails", "notification_emails_bcc"]:
        if key in data:
            out[key] = data[key]
    if "db" in data and isinstance(data["db"], dict):
        db = data["db"]
        out["database"] = {
            "host": db.get("host", "127.0.0.1"),
            "port": int(db.get("port", 3306) or 3306),
            "database": db.get("database", ""),
            "username": db.get("username", ""),
            "password": db.get("password", ""),
        }
    if "api" in data and isinstance(data["api"], dict):
        api = data["api"]
        api_out = {
            "base_url": api.get("baseUrl") or api.get("base_url", ""),
            "token": api.get("token", ""),
            "timeout_seconds": int(api.get("timeoutSeconds", 30) or 30),
            "connect_timeout_seconds": int(api.get("connectTimeoutSeconds", 10) or 10),
            "max_payload_bytes": int(api.get("maxPayloadBytes", 5 * 1024 * 1024) or 5 * 1024 * 1024),
        }
        if api.get("registrationUrl") or api.get("registration_url"):
            api_out["registration_url"] = api.get("registrationUrl") or api.get("registration_url")
        if api.get("configUrl") or api.get("config_url"):
            api_out["config_url"] = api.get("configUrl") or api.get("config_url")
        out["api"] = api_out
    if "sync" in data and isinstance(data["sync"], dict):
        sync = data["sync"]
        out["sync"] = {
            "enabled_transactions": bool(sync.get("enabledTrans", True)),
            "enabled_bins": bool(sync.get("enabledBins", True)),
            "enabled_status": bool(sync.get("enabledStatus", True)),
            "enabled_heartbeat": bool(sync.get("enabledHeartbeat", True)),
            "enabled_eans": bool(sync.get("enabledEans", True)),
            "enabled_coupons": bool(sync.get("enabledCoupons", True)),
            "enabled_adverts": bool(sync.get("enabledAdverts", True)),
            "outbox_poll_seconds": float(sync.get("outboxPollSeconds", 2) or 2),
            "heartbeat_interval_seconds": int(sync.get("heartbeatIntervalSeconds", 300) or 300),
            "remote_refresh_seconds": int(sync.get("remoteRefreshSeconds", 300) or 300),
            "transaction_overlap_seconds": int(sync.get("transOverlapBufferSeconds", 300) or 300),
            "transaction_batch": int(sync.get("transBatch", 500) or 500),
            "max_transactions_per_payload": int(sync.get("transMaxTransactionsPerPayload", 120) or 120),
            "max_rows_per_payload": int(sync.get("transMaxRowsPerPayload", 3000) or 3000),
            "bins_batch": int(sync.get("binsBatch", 1000) or 1000),
            "install_triggers": bool(sync.get("installTriggers", True)),
        }
    return out


def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    if not path.exists():
        return AppConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return AppConfig()
    data = _from_php_shape(data) if ("machineId" in data or "configDone" in data or "db" in data) else data
    cfg = AppConfig()
    for key, value in data.items():
        if key == "database" and isinstance(value, dict):
            cfg.database = _merge_dataclass(DatabaseConfig, value)
        elif key == "api" and isinstance(value, dict):
            current = asdict(cfg.api)
            current.update(value)
            cfg.api = _merge_dataclass(ApiConfig, current)
        elif key == "sync" and isinstance(value, dict):
            current = asdict(cfg.sync)
            current.update(value)
            cfg.sync = _merge_dataclass(SyncConfig, current)
        elif hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg


def save_config(cfg: AppConfig, path: Path = CONFIG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
