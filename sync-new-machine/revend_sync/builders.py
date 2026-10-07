"""Machine database rows -> Machine API v2 payloads. Pure functions.

Field meanings follow what machines actually write (production sample,
October 2026): ``bottlevalue`` 5 for a 0.50 zl deposit, ``metal`` 1 for a
can, 0 for PET and NULL for an unrecognised rejected item,
``recognitionstatus`` 1 = accepted and other codes up to 44 = rejected,
``transactiondone`` 2, 4 or 5 for a finished transaction, ``bin_type``
``left`` for PET and ``right`` for cans.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

FINISHED_STATES = {2, 4, 5}
MAX_DETAILS_PER_TRANSACTION = 500
MAX_TRANSACTIONS_PER_BATCH = 120
MAX_ROWS_PER_BATCH = 3000
MAX_COUPON_LENGTH = 32
MIN_DATELINE = 946684800  # 2000-01-01, the API's lower bound for seals

BIN_TYPES = {"left": "pet", "right": "can", "pet": "pet", "can": "can", "plastic": "pet", "metal": "can"}


@dataclass
class Built:
    """A payload part plus the problems found while building it (logged by the caller)."""

    value: Any
    problems: list[str] = field(default_factory=list)


def iso(timestamp: int | float) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def deposit_grosze(value: Any) -> int:
    """Machine deposit value to grosze: 0.5 or 5 or 50 all mean 0.50 zl (same rules as the server)."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return 50
    if number <= 0:
        return 0
    if number <= 1:
        grosze = round(number * 100)
    elif number <= 10:
        grosze = round(number * 10)
    else:
        grosze = round(number)
    return min(int(grosze), 100000)


def clean_barcode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "non" or len(text) > 64:
        return None
    return text


def coupon_of(row: dict[str, Any]) -> str | None:
    coupon = str(row.get("print_barcode") or "").strip()
    if coupon == "" or coupon.lower() == "non":
        return None
    return coupon


def is_finished(row: dict[str, Any]) -> bool:
    return to_int(row.get("transactiondone"), 0) in FINISHED_STATES


def material_of(row: dict[str, Any]) -> str | None:
    metal = to_int(row.get("metal"))
    if metal == 1:
        return "can"
    if metal == 0:
        return "pet"
    material = str(row.get("material") or "").strip().lower()
    if material in ("can", "metal", "alu", "aluminium"):
        return "can"
    if material in ("pet", "plastic"):
        return "pet"
    return None


def build_transaction(coupon: str, rows: list[dict[str, Any]]) -> Built:
    """One v2 transaction from all ``user_transaction`` rows of a coupon."""
    problems: list[str] = []
    rows = sorted(rows, key=lambda r: (to_int(r.get("dateline"), 0), to_int(r.get("id"), 0)))

    if len(rows) > MAX_DETAILS_PER_TRANSACTION:
        problems.append(
            f"coupon {coupon}: {len(rows)} items, API accepts {MAX_DETAILS_PER_TRANSACTION}; sending the first ones"
        )
        rows = rows[:MAX_DETAILS_PER_TRANSACTION]

    details = []
    accepted = rejected = total = 0
    for row in rows:
        status = to_int(row.get("recognitionstatus"), 0)
        status = status if 0 <= status <= 255 else 0
        material = material_of(row)
        is_accepted = status == 1

        if is_accepted and material is None:
            problems.append(f"coupon {coupon}: accepted item {row.get('id')} has no material, sent as pet")
            material = "pet"

        deposit = deposit_grosze(row.get("bottlevalue")) if is_accepted else 0
        weight = to_int(row.get("weight"))

        detail: dict[str, Any] = {
            "itemId": str(row.get("id")),
            "eventAt": iso(to_int(row.get("dateline"), 0)),
            "barcode": clean_barcode(row.get("barcode")),
            "recognitionStatus": status,
            "materialType": material,
            "depositAmount": deposit,
        }
        if weight is not None and 0 <= weight <= 10000:
            detail["weightGrams"] = weight
        details.append(detail)

        if is_accepted:
            accepted += 1
            total += deposit
        else:
            rejected += 1

    datelines = [to_int(r.get("dateline"), 0) for r in rows]
    transaction_id = next(
        (str(r.get("transactionid")).strip() for r in rows if str(r.get("transactionid") or "").strip()),
        coupon,
    )

    return Built(
        {
            "transactionId": transaction_id[:100],
            "couponId": coupon,
            "startedAt": iso(min(datelines)),
            "finishedAt": iso(max(datelines)),
            "status": "completed",
            "currency": "PLN",
            "totalDepositAmount": total,
            "acceptedItemsCount": accepted,
            "rejectedItemsCount": rejected,
            "details": details,
        },
        problems,
    )


def fingerprint(transaction: dict[str, Any]) -> str:
    """Identity of a transaction's content - unchanged content is never queued twice."""
    return hashlib.sha256(json.dumps(transaction["details"], sort_keys=True).encode()).hexdigest()


def transaction_batches(machine_id: str, transactions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split transactions into /trans payloads within the API limits (120 transactions, 3000 rows)."""
    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    rows = 0
    for transaction in transactions:
        size = len(transaction["details"])
        if current and (len(current) >= MAX_TRANSACTIONS_PER_BATCH or rows + size > MAX_ROWS_PER_BATCH):
            batches.append(_trans_payload(machine_id, current))
            current, rows = [], 0
        current.append(transaction)
        rows += size
    if current:
        batches.append(_trans_payload(machine_id, current))
    return batches


def _trans_payload(machine_id: str, transactions: list[dict[str, Any]]) -> dict[str, Any]:
    # No sentAt: the body must stay byte-identical across retries.
    return {
        "machineId": machine_id,
        "batchId": uuid.uuid4().hex,
        "data": {"mid": machine_id, "transactions": transactions},
    }


def build_bin(machine_id: str, row: dict[str, Any]) -> Built:
    """One sealed bag (``empty_record`` row) as a /bins payload, or None if unusable."""
    seal = str(row.get("barcode") or "").strip()
    raw_type = str(row.get("bin_type") or "").strip().lower()
    dateline = to_int(row.get("dateline"), 0)

    if seal == "" or len(seal) > 100:
        return Built(None, [f"bin record {row.get('id')}: missing or too long seal, skipped"])
    if dateline < MIN_DATELINE:
        return Built(None, [f"bin record {row.get('id')}: invalid dateline {row.get('dateline')}, skipped"])

    problems = []
    bin_type = BIN_TYPES.get(raw_type)
    if bin_type is None:
        problems.append(f"bin record {row.get('id')}: unknown bin_type {raw_type!r}, sent as pet")
        bin_type = "pet"

    return Built(
        {
            "machineId": machine_id,
            "data": {"empty_records": [{"barcode": seal, "bin_type": bin_type, "dateline": dateline}]},
        },
        problems,
    )


def build_status(machine_id: str, row: dict[str, Any], timestamp: float) -> dict[str, Any]:
    """Latest ``command`` row as a /status payload. Storage values are FREE space in percent."""
    command: dict[str, Any] = {}
    for key in ("storage", "storageplastic", "storagecan"):
        value = to_int(row.get(key))
        if value is not None:
            command[key] = max(0, min(100, value))
    errorcode = str(row.get("errorcode") if row.get("errorcode") is not None else "0").strip()[:8] or "0"
    command["errorcode"] = errorcode
    return {
        "machineId": machine_id,
        "timestamp": iso(timestamp),
        "kind": "status",
        "data": {"command": command},
    }
