from __future__ import annotations

from typing import Any

from .config import AppConfig, ApiConfig, DatabaseConfig, SyncConfig
from .http_client import ApiClient


def _api(cfg: AppConfig) -> ApiClient:
    api = ApiClient(ApiConfig(
        base_url=cfg.api.base_url,
        token=cfg.api.token,
        registration_url=cfg.api.registration_url,
        config_url=cfg.api.config_url,
        timeout_seconds=cfg.api.timeout_seconds,
        connect_timeout_seconds=cfg.api.connect_timeout_seconds,
    ))
    return api


def _attrs(body: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if isinstance(body, dict):
        attrs = body.get("data", {}).get("attributes", {}) if isinstance(body.get("data"), dict) else {}
    return attrs if isinstance(attrs, dict) else {}


def check_remote_registration(cfg: AppConfig) -> dict[str, Any]:
    body = _api(cfg).post_ok(
        cfg.api.registration_url,
        {
            "machineId": cfg.machine_id,
            "integration": cfg.integration or None,
        },
        absolute=True,
    )
    attrs = _attrs(body)
    if attrs.get("registered") and attrs.get("machineId"):
        cfg.machine_id = str(attrs["machineId"])
    return attrs


def bootstrap_remote_config(cfg: AppConfig, machine_id: str) -> AppConfig:
    api = _api(cfg)
    body = api.post_ok(cfg.api.config_url, {"machineId": machine_id}, absolute=True)
    attrs = _attrs(body)
    if not isinstance(attrs, dict) or not attrs:
        raise RuntimeError("API config response has no data.attributes")

    cfg.machine_id = attrs.get("machineId", machine_id)
    cfg.integration = attrs.get("integration", cfg.integration)
    cfg.address = attrs.get("address", cfg.address)
    cfg.notification_emails = attrs.get("notification_emails", cfg.notification_emails) or []
    cfg.notification_emails_bcc = attrs.get("notification_emails_bcc", cfg.notification_emails_bcc) or []

    db = attrs.get("db", {})
    if isinstance(db, dict):
        cfg.database = DatabaseConfig(
            host=str(db.get("host", cfg.database.host)),
            port=int(db.get("port", cfg.database.port) or cfg.database.port),
            database=str(db.get("database", cfg.database.database)),
            username=str(db.get("username", cfg.database.username)),
            password=str(db.get("password", cfg.database.password)),
        )

    remote_api = attrs.get("api", {})
    if isinstance(remote_api, dict):
        cfg.api.base_url = str(remote_api.get("baseUrl") or remote_api.get("base_url") or cfg.api.base_url)
        cfg.api.token = str(remote_api.get("token") or cfg.api.token)
        cfg.api.registration_url = str(remote_api.get("registrationUrl") or remote_api.get("registration_url") or cfg.api.registration_url)
        cfg.api.config_url = str(remote_api.get("configUrl") or remote_api.get("config_url") or cfg.api.config_url)

    sync = attrs.get("sync", {})
    if isinstance(sync, dict):
        cfg.sync = SyncConfig(
            enabled_transactions=bool(sync.get("enabledTrans", cfg.sync.enabled_transactions)),
            enabled_bins=bool(sync.get("enabledBins", cfg.sync.enabled_bins)),
            enabled_status=bool(sync.get("enabledStatus", cfg.sync.enabled_status)),
            enabled_heartbeat=bool(sync.get("enabledHeartbeat", cfg.sync.enabled_heartbeat)),
            enabled_eans=bool(sync.get("enabledEans", cfg.sync.enabled_eans)),
            enabled_coupons=bool(sync.get("enabledCoupons", cfg.sync.enabled_coupons)),
            enabled_adverts=bool(sync.get("enabledAdverts", cfg.sync.enabled_adverts)),
            outbox_poll_seconds=float(sync.get("outboxPollSeconds", cfg.sync.outbox_poll_seconds)),
            heartbeat_interval_seconds=int(sync.get("heartbeatIntervalSeconds", cfg.sync.heartbeat_interval_seconds)),
            remote_refresh_seconds=int(sync.get("remoteRefreshSeconds", cfg.sync.remote_refresh_seconds)),
            transaction_overlap_seconds=int(sync.get("transOverlapBufferSeconds", cfg.sync.transaction_overlap_seconds)),
            transaction_batch=int(sync.get("transBatch", cfg.sync.transaction_batch)),
            max_transactions_per_payload=int(sync.get("transMaxTransactionsPerPayload", cfg.sync.max_transactions_per_payload)),
            max_rows_per_payload=int(sync.get("transMaxRowsPerPayload", cfg.sync.max_rows_per_payload)),
            bins_batch=int(sync.get("binsBatch", cfg.sync.bins_batch)),
            install_triggers=bool(sync.get("installTriggers", cfg.sync.install_triggers)),
        )

    cfg.config_done = True
    return cfg
