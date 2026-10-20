"""Commands from the panel, e.g. a service action from the machine's Service tab.

The panel never connects to the machine: every minute the agent reports its
coupon pool and asks for queued commands, runs them and sends the result back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from . import catalog
from .api import ApiError, Client, encode_body
from .config import Config
from .machine_db import MachineDb

log = logging.getLogger(__name__)

SAMPLE_SIZE = 5


def poll(client: Client, db: MachineDb, config: Config) -> int:
    """Report the coupon pool, run queued commands. Returns how many ran."""
    response = client.post(
        config.agent_url("commands"),
        encode_body({"machineId": config.machine_id, "couponPool": coupon_pool(db)}),
    )
    data = response.data.get("data", {}) if isinstance(response.data, dict) else {}
    commands = data.get("commands") or []

    handlers: dict[str, Callable[[], dict[str, Any]]] = {
        "clear_coupon_pool": lambda: clear_coupon_pool(client, db, config),
    }
    ran = 0
    for command in commands if isinstance(commands, list) else []:
        if not isinstance(command, dict) or "id" not in command:
            continue
        handler = handlers.get(str(command.get("type")))
        if handler is None:
            _report(client, config, command["id"], "failed", error=f"Unknown command {command.get('type')}")
            continue
        try:
            result = handler()
        except Exception as error:  # noqa: BLE001 - the panel must see why
            log.exception("Command %s failed", command.get("type"))
            _report(client, config, command["id"], "failed", error=f"{type(error).__name__}: {error}")
        else:
            log.info("Command %s done", command.get("type"), extra={"context": result})
            _report(client, config, command["id"], "done", result=result)
        ran += 1
    return ran


def coupon_pool(db: MachineDb) -> dict[str, Any]:
    count = int(db.query("SELECT COUNT(*) AS n FROM printer_barcode")[0]["n"])
    sample = [str(r["barcode"]) for r in db.query(f"SELECT barcode FROM printer_barcode LIMIT {SAMPLE_SIZE}")]
    return {"count": count, "sample": sample}


def clear_coupon_pool(client: Client, db: MachineDb, config: Config) -> dict[str, Any]:
    """Drop every unused coupon code (e.g. copied from another machine) and fetch new ones."""
    removed = db.execute("DELETE FROM printer_barcode")
    db.execute("UPDATE command SET printer_barcode = '0'")
    added = catalog.top_up_coupons(client, db, config)
    return {"removed": removed, "added": added, "count": coupon_pool(db)["count"]}


def _report(
    client: Client,
    config: Config,
    command_id: Any,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    body = {"machineId": config.machine_id, "status": status, "result": result, "error": error}
    try:
        client.post(config.agent_url(f"commands/{command_id}/result"), encode_body(body))
    except ApiError as failure:
        # The panel sends the command again after 10 minutes without a result.
        log.warning("Could not report command %s: %s", command_id, failure)
