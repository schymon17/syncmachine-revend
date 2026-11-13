"""Machine API v2 client: HMAC signing, clock correction, error classes."""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from . import __version__

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

# Retry these HTTP statuses; any other 4xx is a permanent rejection.
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
# 401/409 codes that mean "try again" rather than "your data is wrong".
RETRYABLE_ERROR_CODES = {"stale_timestamp", "invalid_timestamp", "replay_detected", "request_in_progress"}
# 403 codes that mean the key itself is wrong for this machine, like a 401.
AUTH_ERROR_CODES = {"machine_mismatch"}
# A rejected key does not fix itself: after one, the agent sends nothing and
# probes once per this many seconds instead of hammering the API with 401s.
AUTH_PAUSE_SECONDS = 900
# A flapping connection reports one outage per hour at most; the outages in
# between are counted into the next report.
OUTAGE_REPORT_SECONDS = 3600

log = logging.getLogger(__name__)


@dataclass
class ApiResponse:
    status: int
    data: Any
    headers: dict[str, str]

    @property
    def replayed(self) -> bool:
        return self.headers.get("Idempotency-Replayed", "").lower() == "true"


class ApiError(Exception):
    """A request that did not succeed.

    ``retryable`` tells the sender whether to try again later with the same
    body. ``status`` is ``None`` when the server was not reached at all.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None,
        code: str | None = None,
        retryable: bool,
        retry_after: float | None = None,
        body: Any = None,
        auth: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.body = body
        # The API key was rejected: nothing is wrong with the data, so a
        # message must neither be retried at once nor become a dead letter.
        self.auth = auth

    @property
    def network(self) -> bool:
        return self.status is None


def sign(secret: str, method: str, path: str, body: bytes, timestamp: str, nonce: str) -> str:
    """base64(HMAC-SHA256(secret, METHOD\\n/path\\nbody\\ntimestamp\\nnonce)) - as VerifyMachineApiSignature."""
    canonical = b"\n".join([method.upper().encode(), path.encode(), body, timestamp.encode(), nonce.encode()])
    return base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()


def encode_body(payload: dict[str, Any]) -> bytes:
    """The exact bytes that are signed and, for queued messages, stored and resent unchanged."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Client:
    """Signed POST requests to Machine API v2 and to the agent endpoints.

    The server accepts a timestamp within 300 s of its own clock. Machine
    clocks drift, so every response's ``Date`` header updates ``clock_offset``
    and the next signature uses server time. A ``stale_timestamp`` error is
    therefore retryable: the retry is signed with the corrected clock.

    The client also owns the two states every task shares, so each is
    logged once by the client instead of once per task:

    - unreachable API: one warning per outage, and while the connection
      flaps at most one per ``OUTAGE_REPORT_SECONDS`` with the count of
      outages in between;
    - rejected key (401, or 403 ``machine_mismatch``): every request stops
      for ``AUTH_PAUSE_SECONDS``, then one probe; a deleted machine or a
      revoked key no longer produces a 401 every few seconds.
    """

    def __init__(
        self,
        key_id: str,
        secret: str,
        *,
        session: requests.Session | None = None,
        now: Callable[[], float] = time.time,
        verify: bool | str = True,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.key_id = key_id
        self._secret = secret
        self._session = session or requests.Session()
        self._now = now
        self._mono = monotonic
        self._verify = verify
        self.clock_offset = 0.0
        self.online: bool | None = None
        self._offline_since = 0.0
        self._outage_reported_at: float | None = None
        self._outages_unreported = 0
        self._offline_seconds_unreported = 0.0
        self.auth_rejected = False
        self._auth_paused_until = 0.0

    def server_now(self) -> float:
        return self._now() + self.clock_offset

    def post(self, url: str, body: bytes, idempotency_key: str | None = None) -> ApiResponse:
        if self.auth_rejected and self._mono() < self._auth_paused_until:
            raise ApiError(
                "API key rejected; requests paused",
                status=401,
                code="auth_paused",
                retryable=False,
                auth=True,
            )

        timestamp = str(int(self.server_now()))
        nonce = secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"revend-sync/{__version__}",
            "X-Api-Key": self.key_id,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": sign(self._secret, "POST", urlsplit(url).path, body, timestamp, nonce),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            response = self._session.post(
                url,
                data=body,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=False,
                verify=self._verify,
            )
        except requests.RequestException as exc:
            error = ApiError(f"{type(exc).__name__}: {exc}", status=None, retryable=True)
            self._went_offline(error)
            raise error from exc

        self._came_online()
        self._update_clock(response.headers.get("Date"))
        data = _json_or_text(response)
        code = _error_code(data)

        if 200 <= response.status_code < 300:
            self._key_accepted()
            return ApiResponse(response.status_code, data, dict(response.headers))

        retryable = response.status_code in RETRYABLE_STATUSES or code in RETRYABLE_ERROR_CODES
        auth = not retryable and (response.status_code == 401 or code in AUTH_ERROR_CODES)
        if auth:
            self._key_rejected(response.status_code, code)
        elif response.status_code < 500:
            # Any other answer means the server checked the key and took it.
            self._key_accepted()
        raise ApiError(
            f"HTTP {response.status_code}" + (f" {code}" if code else ""),
            status=response.status_code,
            code=code,
            retryable=retryable,
            retry_after=_retry_after(response.headers.get("Retry-After")),
            body=data,
            auth=auth,
        )

    def _went_offline(self, error: ApiError) -> None:
        if self.online is False:
            return
        self.online = False
        now = self._mono()
        self._offline_since = now
        if self._outage_reported_at is not None and now - self._outage_reported_at < OUTAGE_REPORT_SECONDS:
            self._outages_unreported += 1
            return
        log.warning(
            "API unreachable, queueing until it is back",
            extra={
                "context": {
                    "error": OBJECT_ADDRESS.sub("at 0x…", str(error))[:300],
                    # Outages since the previous report, which were not logged one by one.
                    "earlierOutages": self._outages_unreported,
                    "earlierOfflineSeconds": round(self._offline_seconds_unreported),
                }
            },
        )
        self._outage_reported_at = now
        self._outages_unreported = 0
        self._offline_seconds_unreported = 0.0

    def _came_online(self) -> None:
        if self.online is False:
            seconds = self._mono() - self._offline_since
            self._offline_seconds_unreported += seconds
            log.info("API reachable again after %.0f s", seconds)
        self.online = True

    def _key_rejected(self, status: int, code: str | None) -> None:
        self._auth_paused_until = self._mono() + AUTH_PAUSE_SECONDS
        if self.auth_rejected:
            return
        self.auth_rejected = True
        log.error(
            "API key rejected; every request is paused and retried once per %d min. "
            "If the machine was re-registered or deleted in the panel, install the agent again.",
            AUTH_PAUSE_SECONDS // 60,
            extra={"context": {"status": status, "code": code}},
        )

    def _key_accepted(self) -> None:
        if self.auth_rejected:
            self.auth_rejected = False
            log.warning("API key accepted again; sending resumes")

    def _update_clock(self, date_header: str | None) -> None:
        if not date_header:
            return
        try:
            server = email.utils.parsedate_to_datetime(date_header).timestamp()
        except (TypeError, ValueError):
            return
        # Date has one-second resolution; ignore sub-second noise.
        offset = server - self._now()
        self.clock_offset = 0.0 if abs(offset) < 2 else offset


# Object addresses in requests' connection errors differ on every attempt.
OBJECT_ADDRESS = re.compile(r"\bat 0x[0-9a-fA-F]+")


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:2000]


def _error_code(data: Any) -> str | None:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
