"""Turn new rows in the machine database into outbox messages.

Each collector writes its messages and its cursor in one SQLite
transaction: after a crash the agent resumes exactly where it stopped,
without losing or repeating anything.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from . import builders
from .api import encode_body
from .machine_db import MachineDb
from .store import Store

log = logging.getLogger(__name__)


class TransactionCollector:
    """Finished transactions from ``user_transaction``, a few seconds after they end.

    ``poll`` reads by primary key from the oldest row of a transaction still
    in progress (``trans_low_id``), so a transaction is picked up when its rows
    flip to finished. A transaction left unfinished for ``ABANDON_SECONDS``
    stops holding the cursor back. ``sweep`` re-reads the newest rows now and
    then and catches anything finished after that; local fingerprints make
    sure unchanged transactions are not queued again.
    """

    STATE_LOW = "trans_low_id"
    # The first id the agent is responsible for; nothing below it is ever sent.
    STATE_START = "trans_start_id"
    POLL_LIMIT = 5000
    SWEEP_ROWS = 20000
    ABANDON_SECONDS = 6 * 3600

    def __init__(
        self, db: MachineDb, store: Store, machine_id: str, url: str, now: Callable[[], float] = time.time
    ):
        self._db = db
        self._store = store
        self._machine_id = machine_id
        self._url = url
        self._now = now

    def initialise(self, from_id: int | None = None) -> None:
        """First start: begin at ``from_id`` or after the newest row - history is not resent."""
        if self._store.get(self.STATE_LOW) is not None:
            return
        if from_id is None:
            row = self._db.query("SELECT COALESCE(MAX(id), 0) AS max_id FROM user_transaction")[0]
            from_id = int(row["max_id"]) + 1
        with self._store.transaction() as db:
            self._store.set(self.STATE_LOW, int(from_id), db=db)
            self._store.set(self.STATE_START, int(from_id), db=db)
        log.info("Transactions start from id %s", from_id)

    def poll(self) -> int:
        low = int(self._store.get(self.STATE_LOW, 0))
        rows = self._db.query(
            f"SELECT {self._db.transaction_select()} FROM user_transaction WHERE id >= %s ORDER BY id LIMIT %s",
            (low, self.POLL_LIMIT),
        )
        return self._process(rows, low=low)

    def sweep(self) -> int:
        top = int(self._db.query("SELECT COALESCE(MAX(id), 0) AS max_id FROM user_transaction")[0]["max_id"])
        start = int(self._store.get(self.STATE_START, 0))
        rows = self._db.query(
            f"SELECT {self._db.transaction_select()} FROM user_transaction WHERE id >= %s ORDER BY id",
            (max(top - self.SWEEP_ROWS + 1, start),),
        )
        return self._process(rows, low=None)

    def _process(self, rows: list[dict[str, Any]], low: int | None) -> int:
        by_coupon: dict[str, list[dict[str, Any]]] = defaultdict(list)
        in_progress_ids = []
        abandon_before = self._now() - self.ABANDON_SECONDS

        for row in rows:
            if not builders.is_finished(row):
                if builders.to_int(row.get("dateline"), 0) >= abandon_before:
                    in_progress_ids.append(int(row["id"]))
                continue
            coupon = builders.coupon_of(row)
            if coupon is not None:
                by_coupon[coupon].append(row)

        # A coupon still having unfinished rows in this read is not done yet.
        unfinished_coupons = {builders.coupon_of(r) for r in rows if not builders.is_finished(r)}

        transactions = []
        remembered = []
        for coupon, coupon_rows in by_coupon.items():
            if coupon in unfinished_coupons:
                continue
            if len(coupon) > builders.MAX_COUPON_LENGTH:
                log.error(
                    "Coupon longer than %s characters, not sent",
                    builders.MAX_COUPON_LENGTH,
                    extra={"context": {"coupon": coupon}},
                )
                continue

            built = builders.build_transaction(coupon, coupon_rows)
            for problem in built.problems:
                log.warning(problem)

            transaction = built.value
            fingerprint = builders.fingerprint(transaction)
            previous = self._store.sent_transaction(coupon)
            if previous is not None:
                if previous[0] == fingerprint:
                    continue
                if len(transaction["details"]) < previous[1]:
                    # Only part of an already sent transaction is in view -
                    # sending it would replace the full one on the server.
                    continue
            transactions.append(transaction)
            remembered.append((coupon, fingerprint, len(transaction["details"])))

        batches = builders.transaction_batches(self._machine_id, transactions)

        new_low = None
        if low is not None:
            if in_progress_ids:
                new_low = min(in_progress_ids)
            elif rows:
                new_low = max(int(r["id"]) for r in rows) + 1
            else:
                new_low = low

        if not batches and (new_low is None or new_low == low):
            return 0

        with self._store.transaction() as db:
            for payload in batches:
                self._store.enqueue("trans", self._url, encode_body(payload), db=db)
            for coupon, fingerprint, count in remembered:
                self._store.remember_transaction(coupon, fingerprint, count, db=db)
            if new_low is not None:
                self._store.set(self.STATE_LOW, new_low, db=db)

        if transactions:
            log.info(
                "Queued %s transaction(s)",
                len(transactions),
                extra={"context": {"coupons": [t["couponId"] for t in transactions][:20]}},
            )
        return len(transactions)


class BinCollector:
    """Sealed bags from ``empty_record``, one request per seal.

    One per request on purpose: the API rejects a whole /bins batch when a
    single seal belongs to another machine, which would also drop the good
    seals sent with it.
    """

    STATE_LAST = "bins_last_id"
    POLL_LIMIT = 200

    def __init__(self, db: MachineDb, store: Store, machine_id: str, url: str):
        self._db = db
        self._store = store
        self._machine_id = machine_id
        self._url = url

    def initialise(self, from_id: int | None = None) -> None:
        if self._store.get(self.STATE_LAST) is not None:
            return
        if from_id is None:
            last = int(self._db.query("SELECT COALESCE(MAX(id), 0) AS max_id FROM empty_record")[0]["max_id"])
        else:
            last = int(from_id) - 1
        self._store.set(self.STATE_LAST, last)
        log.info("Bags start after id %s", last)

    def poll(self) -> int:
        last = int(self._store.get(self.STATE_LAST, 0))
        rows = self._db.query(
            "SELECT id, dateline, bin_type, barcode FROM empty_record WHERE id > %s ORDER BY id LIMIT %s",
            (last, self.POLL_LIMIT),
        )
        if not rows:
            return 0

        queued = 0
        with self._store.transaction() as db:
            for row in rows:
                built = builders.build_bin(self._machine_id, row)
                for problem in built.problems:
                    log.warning(problem)
                if built.value is not None:
                    self._store.enqueue("bins", self._url, encode_body(built.value), db=db)
                    queued += 1
            self._store.set(self.STATE_LAST, int(rows[-1]["id"]), db=db)

        if queued:
            log.info(
                "Queued %s sealed bag(s)",
                queued,
                extra={"context": {"seals": [str(r.get("barcode")) for r in rows][:20]}},
            )
        return queued


class StatusCollector:
    """Bag levels and error code from the latest ``command`` row.

    Queued when something changed or every ``REFRESH_SECONDS``; only the
    newest unsent status is kept, older ones are useless.
    """

    STATE_LAST = "status_last"
    REFRESH_SECONDS = 600

    def __init__(
        self, db: MachineDb, store: Store, machine_id: str, url: str, now: Callable[[], float] = time.time
    ):
        self._db = db
        self._store = store
        self._machine_id = machine_id
        self._url = url
        self._now = now

    def poll(self) -> bool:
        has_id = "id" in self._db.columns.get("command", set())
        rows = self._db.query("SELECT * FROM command" + (" ORDER BY id DESC" if has_id else "") + " LIMIT 1")
        if not rows:
            return False

        payload = builders.build_status(self._machine_id, rows[0], self._now())
        signature = json.dumps(payload["data"]["command"], sort_keys=True)
        last = self._store.get(self.STATE_LAST) or {}
        if (
            last.get("signature") == signature
            and self._now() - float(last.get("at", 0)) < self.REFRESH_SECONDS
        ):
            return False

        with self._store.transaction() as db:
            self._store.enqueue("status", self._url, encode_body(payload), db=db, replace_pending=True)
            self._store.set(self.STATE_LAST, {"signature": signature, "at": self._now()}, db=db)
        return True
