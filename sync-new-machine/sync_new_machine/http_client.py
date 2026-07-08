from __future__ import annotations

import json
from typing import Any

import requests

from .config import ApiConfig


class ApiClient:
    def __init__(self, cfg: ApiConfig):
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.token:
            headers["x-api-key-machine"] = self.cfg.token
        return headers

    def post(self, path: str, payload: dict[str, Any], absolute: bool = False) -> tuple[int, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > self.cfg.max_payload_bytes:
            raise RuntimeError("Payload too large")
        url = path if absolute else f"{self.cfg.base_url.rstrip('/')}/{path.lstrip('/')}"
        response = requests.post(
            url,
            data=encoded.encode("utf-8"),
            headers=self._headers(),
            timeout=(self.cfg.connect_timeout_seconds, self.cfg.timeout_seconds),
            verify=False,
        )
        body: Any
        if response.text:
            try:
                body = response.json()
            except ValueError:
                body = response.text
        else:
            body = None
        return response.status_code, body

    def post_ok(self, path: str, payload: dict[str, Any], absolute: bool = False) -> Any:
        status, body = self.post(path, payload, absolute=absolute)
        if status < 200 or status >= 300:
            raise RuntimeError(f"API {path} returned HTTP {status}: {str(body)[:300]}")
        return body

