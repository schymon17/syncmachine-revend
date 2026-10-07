"""The agent's main loop: independent periodic tasks around the outbox."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pymysql

from . import __version__, adverts, catalog, logs, updates
from .api import ApiError, Client, encode_body
from .collectors import BinCollector, StatusCollector, TransactionCollector
from .config import Config
from .machine_db import MachineDb
from .sender import Sender
from .store import Store

log = logging.getLogger(__name__)

# Clock drift the API tolerates is 300 s; warn well before that.
CLOCK_WARN_SECONDS = 60


@dataclass
class Task:
    name: str
    interval: float
    run: Callable[[], object]
    next_at: float = 0.0
    needs_db: bool = False
    last_error: str | None = None
    last_error_logged_at: float = 0.0


class Agent:
    """Runs every task on its own schedule; one failing task never stops the others.

    Uses the monotonic clock for scheduling, so a machine clock jump does not
    stall or burst the tasks.
    """

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        db: MachineDb | None = None,
        client: Client | None = None,
        data_dir: Path | None = None,
    ):
        self.config = config
        self.store = store
        self.db = db or MachineDb(config.db)
        self.client = client or Client(config.key_id, config.secret)
        self.sender = Sender(store, self.client)
        self.data_dir = data_dir or Path(".")
        self.started_at = time.time()
        self.db_ok: bool | None = None
        self._stop = threading.Event()
        self._ready = False
        self._clock_warned_at = 0.0

        self.transactions = TransactionCollector(self.db, store, config.machine_id, config.url("trans"))
        self.bins = BinCollector(self.db, store, config.machine_id, config.url("bins"))
        self.status = StatusCollector(self.db, store, config.machine_id, config.url("status"))

        self.tasks = [
            Task("prepare", 30, self._prepare, needs_db=True),
            Task("transactions", 2, self.transactions.poll, needs_db=True),
            Task("transactions-sweep", 300, self.transactions.sweep, needs_db=True),
            Task("bins", 5, self.bins.poll, needs_db=True),
            Task("status", 60, self.status.poll, needs_db=True),
            Task("send", 1, self.sender.run_once),
            Task("logs", 30, lambda: logs.queue_upload(store, config.machine_id, config.url("logs"))),
            Task("heartbeat", 300, self.heartbeat),
            Task("updates", 6 * 3600, lambda: updates.check(self.client, config, self.data_dir / "updates")),
            Task("housekeeping", 3600, store.prune),
        ]
        if config.eans_enabled:
            self.tasks.append(
                Task(
                    "eans",
                    6 * 3600,
                    lambda: catalog.sync_eans(self.client, self.db, store, config),
                    next_at=60,
                    needs_db=True,
                )
            )
        if config.adverts_enabled:
            self.tasks.append(
                Task(
                    "adverts",
                    300,
                    lambda: adverts.sync_adverts(self.client, self.db, store, config),
                    next_at=90,
                    needs_db=True,
                )
            )
        if config.coupons_enabled:
            self.tasks.append(
                Task(
                    "coupons", 60, lambda: catalog.top_up_coupons(self.client, self.db, config), needs_db=True
                )
            )

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("ReVend sync agent %s started for %s", __version__, self.config.machine_id)
        start = time.monotonic()
        for task in self.tasks:
            task.next_at = start + task.next_at
        while not self._stop.is_set():
            self.tick()
            next_due = min(task.next_at for task in self.tasks)
            self._stop.wait(max(0.05, min(1.0, next_due - time.monotonic())))
        self.db.close()
        log.info("ReVend sync agent stopped")

    def tick(self) -> None:
        now = time.monotonic()
        for task in self.tasks:
            if task.next_at > now:
                continue
            task.next_at = now + task.interval
            if task.needs_db and task.name != "prepare" and not self._ready:
                continue
            self._run(task)
        self._check_clock()

    def _run(self, task: Task) -> None:
        try:
            task.run()
            if task.needs_db:
                self._db_state(True)
            if task.last_error is not None:
                log.info("Task %s recovered", task.name)
            task.last_error = None
        except pymysql.err.MySQLError as error:
            self._db_state(False, error)
            self._report(task, error)
        except ApiError as error:
            # Reads (EANs, coupons, updates) simply retry on their next run.
            self._report(task, error, level=logging.WARNING if error.retryable else logging.ERROR)
        except Exception as error:  # noqa: BLE001 - keep the agent alive whatever a task does
            self._report(task, error, exc_info=True)

    def _report(
        self, task: Task, error: Exception, level: int = logging.ERROR, exc_info: bool = False
    ) -> None:
        """Log a task failure once, and again only every 10 minutes while it persists."""
        message = f"{type(error).__name__}: {error}"[:500]
        now = time.monotonic()
        if message != task.last_error or now - task.last_error_logged_at > 600:
            log.log(
                level,
                "Task %s failed",
                task.name,
                exc_info=exc_info,
                extra={"context": {"task": task.name, "error": message}},
            )
            task.last_error_logged_at = now
        task.last_error = message

    def _prepare(self) -> None:
        """Check the machine database and set the cursors; retried until it works."""
        if self._ready:
            return
        problems = self.db.check_schema()
        if problems:
            raise RuntimeError("machine database does not fit: " + "; ".join(problems))
        self.transactions.initialise(self.config.transactions_from_id)
        self.bins.initialise(self.config.bins_from_id)
        self._ready = True
        log.info("Machine database ready")

    def _db_state(self, ok: bool, error: Exception | None = None) -> None:
        if ok and self.db_ok is False:
            log.info("Machine database reachable again")
        if not ok and self.db_ok is not False:
            log.error("Machine database unreachable", extra={"context": {"error": str(error)[:300]}})
        self.db_ok = ok

    def _check_clock(self) -> None:
        offset = self.client.clock_offset
        if abs(offset) > CLOCK_WARN_SECONDS and time.monotonic() - self._clock_warned_at > 3600:
            log.warning(
                "Machine clock is %+.0f s off the server; signatures use server time, but fix NTP", -offset
            )
            self._clock_warned_at = time.monotonic()

    def heartbeat(self) -> None:
        """Sent live, never queued: a heartbeat only means something when it is fresh."""
        stats = self.store.stats()
        payload = {
            "machineId": self.config.machine_id,
            "timestamp": _iso(self.client.server_now()),
            "kind": "heartbeat",
            "softwareVersion": __version__,
            "uptimeSeconds": int(time.time() - self.started_at),
            "agent": {
                "queuePending": stats["pending"],
                "queueFailed": stats["failed"],
                "queueDeadLetter": stats["dead"],
                "oldestPendingAt": _iso(stats["oldest_pending_at"]) if stats["oldest_pending_at"] else None,
                "lastSentAt": _iso(self.sender.last_sent_at) if self.sender.last_sent_at else None,
                "machineDbConnected": bool(self.db_ok),
            },
        }
        self.client.post(self.config.url("heartbeat"), encode_body(payload), uuid.uuid4().hex)


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
