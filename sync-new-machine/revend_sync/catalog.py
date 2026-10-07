"""Data the machine needs from ReVend: product catalogue and coupon pool."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from .api import ApiError, Client, encode_body
from .config import Config
from .machine_db import MachineDb
from .store import Store

log = logging.getLogger(__name__)

BARCODE_COLUMNS = (
    "barcode",
    "brand",
    "bottleinfo",
    "value",
    "maxsdiam",
    "minsdiam",
    "maxbdiam",
    "minbdiam",
    "material_type",
    "metal",
    "capacity",
    "weight",
    "version",
)
STAGING = "barcode_revend_new"
RETIRED = "barcode_revend_old"
# A catalogue that suddenly shrinks this much is more likely a broken
# response than a real change - keep the current one and report it.
MIN_KEEP_RATIO = 0.5


def sync_eans(client: Client, db: MachineDb, store: Store, config: Config) -> bool:
    """Replace the machine's ``barcode`` table with the ReVend catalogue, atomically.

    The new catalogue is loaded into a staging copy of the table and swapped
    in with one RENAME TABLE: the machine sees the old catalogue or the new
    one, never an empty or half-filled table (the old agent truncated first).
    """
    response = client.post(config.url("eans"), encode_body({"machineId": config.machine_id}))
    data = response.data.get("data", {}) if isinstance(response.data, dict) else {}
    items = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        log.warning("EAN catalogue response is empty or malformed; keeping the current catalogue")
        return False

    digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
    if store.get("eans_hash") == digest:
        return False

    columns = [c for c in BARCODE_COLUMNS if c in db.table_columns("barcode")]
    rows = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("barcode"):
            continue
        barcode = str(item["barcode"]).strip()
        if barcode in seen:
            continue
        seen.add(barcode)
        rows.append(
            [
                _column_value(column, item.get(column)) if column != "barcode" else barcode
                for column in columns
            ]
        )

    current = int(db.query("SELECT COUNT(*) AS n FROM barcode")[0]["n"])
    if current and len(rows) < current * MIN_KEEP_RATIO:
        log.error("EAN catalogue would shrink from %s to %s products; not imported", current, len(rows))
        return False

    db.execute("SET SESSION lock_wait_timeout = 15")
    db.execute(f"DROP TABLE IF EXISTS `{STAGING}`")
    db.execute(f"DROP TABLE IF EXISTS `{RETIRED}`")
    db.execute(f"CREATE TABLE `{STAGING}` LIKE `barcode`")
    for column in ("bottleinfo", "brand"):
        if column in columns:
            try:
                db.execute(f"ALTER TABLE `{STAGING}` MODIFY `{column}` VARCHAR(255) NULL")
            except Exception:  # noqa: BLE001 - widening is best effort, the old agent did the same
                pass

    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT IGNORE INTO `{STAGING}` ({', '.join(f'`{c}`' for c in columns)}) VALUES ({placeholders})"
    for start in range(0, len(rows), 500):
        db.executemany(sql, rows[start : start + 500])

    loaded = int(db.query(f"SELECT COUNT(*) AS n FROM `{STAGING}`")[0]["n"])
    if loaded < len(rows) * 0.95:
        db.execute(f"DROP TABLE IF EXISTS `{STAGING}`")
        log.error("EAN import loaded %s of %s products; keeping the current catalogue", loaded, len(rows))
        return False

    db.execute(f"RENAME TABLE `barcode` TO `{RETIRED}`, `{STAGING}` TO `barcode`")
    db.execute(f"DROP TABLE IF EXISTS `{RETIRED}`")
    store.set("eans_hash", digest)
    log.info("EAN catalogue updated: %s products", loaded)
    return True


def _column_value(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column == "metal":
        try:
            return int(bool(int(value))) if not isinstance(value, bool) else int(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


COUPON_MIN = 25
COUPON_TARGET = 50


def top_up_coupons(client: Client, db: MachineDb, config: Config) -> int:
    """Keep 25-50 unused coupon numbers in ``printer_barcode`` for the printer.

    Numbers already queued, being printed (``command``) or used by a
    transaction are never queued again - the machine would print a second
    coupon under a number that was already used.
    """
    existing = int(db.query("SELECT COUNT(*) AS n FROM printer_barcode")[0]["n"])
    if existing >= COUPON_MIN:
        return 0
    need = COUPON_TARGET - existing

    try:
        response = client.post(
            config.url("coupons"), encode_body({"machineId": config.machine_id}), uuid.uuid4().hex
        )
    except ApiError as error:
        if error.status in (400, 404):
            # The integration has no coupon pool, or ReVend has none left.
            log.info("No coupons available from the API (%s)", error.status)
            return 0
        raise

    payload = response.data.get("data", response.data) if isinstance(response.data, dict) else response.data
    attributes = payload.get("attributes", payload) if isinstance(payload, dict) else payload
    candidates = []
    for item in attributes if isinstance(attributes, list) else []:
        code = str(item.get("barcode", "") if isinstance(item, dict) else item).strip()
        if code and code not in candidates:
            candidates.append(code)
    if not candidates:
        return 0

    marks = ", ".join(["%s"] * len(candidates))
    excluded = {
        str(r["v"])
        for r in db.query(f"SELECT barcode AS v FROM printer_barcode WHERE barcode IN ({marks})", candidates)
    }
    excluded |= {
        str(r["v"])
        for r in db.query(
            f"SELECT DISTINCT printer_barcode AS v FROM command WHERE printer_barcode IN ({marks})",
            candidates,
        )
    }
    excluded |= {
        str(r["v"])
        for r in db.query(
            f"SELECT DISTINCT print_barcode AS v FROM user_transaction WHERE print_barcode IN ({marks})",
            candidates,
        )
    }

    fresh = [c for c in candidates if c not in excluded][:need]
    if fresh:
        db.executemany("INSERT IGNORE INTO printer_barcode (barcode) VALUES (%s)", [[c] for c in fresh])
    if len(fresh) < need:
        log.warning(
            "Coupon pool below target after filtering",
            extra={
                "context": {
                    "existing": existing,
                    "added": len(fresh),
                    "target": COUPON_TARGET,
                    "excluded": len(excluded),
                }
            },
        )
    return len(fresh)
