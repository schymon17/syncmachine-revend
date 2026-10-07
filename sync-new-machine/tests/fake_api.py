"""A local stand-in for Machine API v2, strict where the real one is.

Verifies the HMAC signature, the timestamp window, nonce replay and
idempotency exactly like VerifyMachineApiSignature and
EnsureMachineApiIdempotency, and applies the /trans contract checks of
ApiTransController. Everything accepted is recorded for assertions.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY_ID = "mch_live_testkey00000000000001"
SECRET = "test-secret-with-at-least-32-random-characters"
MACHINE_ID = "RVM_3000_1234567890"


class FakeApi:
    def __init__(self, clock_skew: float = 0.0):
        self.clock_skew = clock_skew  # server clock = real clock + skew
        self.received: dict[str, list[Any]] = {}
        self.nonces: set[str] = set()
        self.idempotency: dict[tuple[str, str], tuple[str, int, bytes]] = {}
        self.fail_with: int | None = None  # force this status for every request
        self.reject_seals: set[str] = set()
        self.eans: list[dict[str, Any]] = []
        self.coupons: list[str] = []
        self.release: dict[str, Any] | None = None
        self.adverts: dict[str, Any] = {}
        self.files: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def date_time_string(self, timestamp: float | None = None) -> str:
                return email.utils.formatdate(time.time() + api.clock_skew, usegmt=True)

            def do_GET(self) -> None:  # noqa: N802
                name = self.path.rsplit("/", 1)[-1]
                data = api.files.get(name)
                api.downloads.append(name)
                self.send_response(200 if data is not None else 404)
                self.send_header("Content-Length", str(len(data or b"")))
                self.end_headers()
                self.wfile.write(data or b"")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                status, payload, extra = api.handle(self.path, dict(self.headers), body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> FakeApi:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def got(self, endpoint: str) -> list[Any]:
        return self.received.get(endpoint, [])

    # --- request handling ---------------------------------------------------

    def handle(self, path: str, headers: dict[str, str], body: bytes) -> tuple[int, Any, dict[str, str]]:
        headers = {k.lower(): v for k, v in headers.items()}
        if self.fail_with is not None:
            return self.fail_with, {"error": {"code": "forced", "message": "forced"}}, {}

        timestamp = headers.get("x-timestamp", "")
        nonce = headers.get("x-nonce", "")
        if headers.get("x-api-key") != KEY_ID:
            return 401, {"error": {"code": "unauthorized"}}, {}
        if not timestamp.isdigit() or abs(int(timestamp) - (time.time() + self.clock_skew)) > 300:
            return 401, {"error": {"code": "stale_timestamp"}}, {}
        canonical = b"\n".join([b"POST", path.encode(), body, timestamp.encode(), nonce.encode()])
        expected = base64.b64encode(hmac.new(SECRET.encode(), canonical, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(expected, headers.get("x-signature", "")):
            return 401, {"error": {"code": "invalid_signature"}}, {}
        with self.lock:
            if nonce in self.nonces:
                return 409, {"error": {"code": "replay_detected"}}, {}
            self.nonces.add(nonce)

        payload = json.loads(body)
        if payload.get("machineId", (payload.get("data") or {}).get("mid")) != MACHINE_ID:
            return 403, {"error": {"code": "machine_mismatch"}}, {}

        endpoint = path.rsplit("/", 1)[-1] if "/agent/" not in path else "agent/" + path.rsplit("/", 1)[-1]
        key = headers.get("idempotency-key")
        digest = hashlib.sha256(body).hexdigest()
        if key:
            stored = self.idempotency.get((path, key))
            if stored:
                if stored[0] != digest:
                    return 409, {"error": {"code": "idempotency_conflict"}}, {}
                return stored[1], json.loads(stored[2]), {"Idempotency-Replayed": "true"}

        status, response = self.route(endpoint, payload)
        if key:
            self.idempotency[(path, key)] = (digest, status, json.dumps(response).encode())
        return status, response, {}

    def route(self, endpoint: str, payload: dict[str, Any]) -> tuple[int, Any]:
        if endpoint == "trans":
            error = check_transactions(payload)
            if error:
                return 422, {"error": {"code": error}}
        if endpoint == "bins":
            seal = payload["data"]["empty_records"][0]["barcode"]
            if seal in self.reject_seals:
                return 409, {"error": {"code": "seal_already_used"}, "sealNumber": seal}
            if payload["data"]["empty_records"][0]["bin_type"] not in ("pet", "can"):
                return 422, {"message": "bin_type"}
        if endpoint == "eans":
            return 200, {"data": {"attributes": self.eans}}
        if endpoint == "coupons":
            if not self.coupons:
                return 404, {"errors": [{"status": "404"}]}
            return 200, {"data": {"attributes": [{"barcode": c} for c in self.coupons]}}
        if endpoint == "adverts":
            return 200, self.adverts
        if endpoint == "register":
            return 200, {"data": {"attributes": {"registered": True, "integration": "kaucja"}}}
        if endpoint == "agent/release":
            newer = self.release is not None and self.release["version"] != payload["currentVersion"]
            return 200, {"data": {"updateAvailable": newer, "release": self.release if newer else None}}

        with self.lock:
            self.received.setdefault(endpoint, []).append(payload)
        return (202 if endpoint in ("trans", "bins", "heartbeat", "logs") else 200), {
            "data": {"accepted": True}
        }


def check_transactions(payload: dict[str, Any]) -> str | None:
    """The ApiTransController checks that a wrong agent payload would fail."""
    transactions = payload["data"]["transactions"]
    if not 1 <= len(transactions) <= 120 or sum(len(t["details"]) for t in transactions) > 3000:
        return "invalid_batch"
    items = set()
    for t in transactions:
        if not t["couponId"] or len(t["couponId"]) > 32 or t["couponId"] == "non":
            return "invalid_coupon"
        if t["status"] != "completed" or t["currency"] != "PLN" or not 1 <= len(t["details"]) <= 500:
            return "invalid_transaction"
        if t["finishedAt"] < t["startedAt"]:
            return "invalid_transaction_time_range"
        accepted = rejected = total = 0
        for d in t["details"]:
            if d["itemId"] in items:
                return "duplicate_item"
            items.add(d["itemId"])
            if not t["startedAt"] <= d["eventAt"] <= t["finishedAt"]:
                return "item_time_outside_transaction"
            if not 0 <= d["recognitionStatus"] <= 255:
                return "invalid_status"
            if d.get("materialType") not in ("pet", "can", None):
                return "invalid_material"
            if d["recognitionStatus"] == 1:
                if d.get("materialType") is None:
                    return "material_type_required"
                accepted += 1
                total += d["depositAmount"]
            else:
                rejected += 1
        if (accepted, rejected, total) != (
            t["acceptedItemsCount"],
            t["rejectedItemsCount"],
            t["totalDepositAmount"],
        ):
            return "transaction_summary_mismatch"
    return None
