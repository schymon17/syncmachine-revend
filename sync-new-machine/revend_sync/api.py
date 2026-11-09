"""Machine API v2 client: HMAC signing, clock correction, error classes."""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
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
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.body = body

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
    """

    def __init__(
        self,
        key_id: str,
        secret: str,
        *,
        session: requests.Session | None = None,
        now: Callable[[], float] = time.time,
        verify: bool | str = True,
    ):
        self.key_id = key_id
        self._secret = secret
        self._session = session or requests.Session()
        self._now = now
        self._verify = verify
        self.clock_offset = 0.0

    def server_now(self) -> float:
        return self._now() + self.clock_offset

    def post(self, url: str, body: bytes, idempotency_key: str | None = None) -> ApiResponse:
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
            raise ApiError(f"{type(exc).__name__}: {exc}", status=None, retryable=True) from exc

        self._update_clock(response.headers.get("Date"))
        data = _json_or_text(response)

        if 200 <= response.status_code < 300:
            return ApiResponse(response.status_code, data, dict(response.headers))

        code = _error_code(data)
        retryable = response.status_code in RETRYABLE_STATUSES or code in RETRYABLE_ERROR_CODES
        raise ApiError(
            f"HTTP {response.status_code}" + (f" {code}" if code else ""),
            status=response.status_code,
            code=code,
            retryable=retryable,
            retry_after=_retry_after(response.headers.get("Retry-After")),
            body=data,
        )

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
