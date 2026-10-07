"""Drains the outbox to the API."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from .api import ApiError, Client
from .store import Store

log = logging.getLogger(__name__)

# Seconds before the n-th retry (API guide: 1, 2, 4, 8, 16, 30 with jitter).
# Longer outages keep the 60 s ceiling so a machine catches up within a
# minute of the connection returning.
BACKOFF = [1, 2, 4, 8, 16, 30, 60]


def backoff(attempt: int, rng: Callable[[], float] = random.random) -> float:
    base = BACKOFF[min(attempt, len(BACKOFF) - 1)]
    return base * (0.8 + 0.4 * rng())


class Sender:
    """Sends due messages oldest first.

    - 2xx: delivered, removed from the outbox.
    - Network error: the whole sender pauses (no point hammering every
      message while offline); messages keep their place.
    - Retryable HTTP error (408/429/5xx, stale clock, request in progress):
      that message waits with backoff, others continue.
    - Any other 4xx: the API rejected the data; the message becomes a dead
      letter so it cannot block the rest, and an error is logged.
    """

    def __init__(self, store: Store, client: Client, now: Callable[[], float] = time.monotonic):
        self._store = store
        self._client = client
        self._now = now
        self._paused_until = 0.0
        self._offline_attempts = 0
        self.last_sent_at: float | None = store.get("last_sent_at")
        self.online: bool | None = None

    def run_once(self, limit: int = 20) -> int:
        """Send up to ``limit`` due messages. Returns how many were delivered."""
        if self._now() < self._paused_until:
            return 0

        delivered = 0
        for message in self._store.due(limit):
            try:
                self._client.post(message.url, message.body, message.idempotency_key)
            except ApiError as error:
                if error.network:
                    self._go_offline(error)
                    self._store.retry_later(message.id, 0, str(error))
                    return delivered
                self._handle_http_error(message.id, message.kind, message.attempts, error)
                continue

            self._store.delivered(message.id)
            delivered += 1
            self._mark_online()
            self.last_sent_at = time.time()
            self._store.set("last_sent_at", self.last_sent_at)

        return delivered

    def _handle_http_error(self, message_id: int, kind: str, attempts: int, error: ApiError) -> None:
        self._mark_online()
        if error.retryable:
            delay = error.retry_after if error.retry_after is not None else backoff(attempts)
            # The API stores the response of a completed request under its
            # idempotency key - a 5xx would be replayed for 24 h. Retry a
            # server error under a new key; transactions and seals are
            # deduplicated by their natural keys, so this is safe.
            new_key = error.status is not None and error.status >= 500
            self._store.retry_later(message_id, delay, str(error), new_key=new_key)
            log.warning(
                "Send failed, will retry",
                extra={
                    "local_only": kind == "logs",
                    "context": {
                        "kind": kind,
                        "status": error.status,
                        "code": error.code,
                        "attempt": attempts + 1,
                    },
                },
            )
            return

        self._store.reject(message_id, f"{error} {str(error.body)[:1500]}")
        log.error(
            "API rejected a message; kept as dead letter",
            extra={
                "local_only": kind == "logs",
                "context": {
                    "kind": kind,
                    "status": error.status,
                    "code": error.code,
                    "response": str(error.body)[:1500],
                },
            },
        )

    def _go_offline(self, error: ApiError) -> None:
        delay = backoff(self._offline_attempts)
        self._offline_attempts += 1
        self._paused_until = self._now() + delay
        if self.online is not False:
            log.warning(
                "API unreachable, queueing until it is back", extra={"context": {"error": str(error)[:300]}}
            )
        self.online = False

    def _mark_online(self) -> None:
        if self.online is False:
            log.info("API reachable again, sending queued messages")
        self.online = True
        self._offline_attempts = 0
