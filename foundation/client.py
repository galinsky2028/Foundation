"""Calling another Foundation-based service's JSON API (an app calling its engine).

    from foundation.client import ServiceClient
    engine = ServiceClient("https://audit-engine.onrender.com", api_key=os.environ["AUDIT_ENGINE_KEY"])
    rule = engine.post("/api/v1/rules", {"name": "Encrypt at rest"})

Transient failures (connection errors, 429, 5xx) are retried with backoff. Other errors raise
ServiceError with the status code and the service's error message.
"""
from __future__ import annotations

import time
from typing import Any

import httpx


class ServiceError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class ServiceClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30, retries: int = 3,
                 transport: httpx.BaseTransport | None = None):
        self.retries = retries
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                 headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    def request(self, method: str, path: str, json: Any = None, params: dict | None = None) -> Any:
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                r = self.http.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise ServiceError(0, f"cannot reach service: {exc}") from exc
            else:
                if r.status_code < 400:
                    return r.json() if r.content else None
                if r.status_code not in (429, 502, 503, 504) or attempt == self.retries:
                    try:
                        detail = r.json().get("detail", r.text)
                    except ValueError:
                        detail = r.text
                    raise ServiceError(r.status_code, str(detail)[:500])
            time.sleep(delay)
            delay *= 2
        raise ServiceError(0, "unreachable")

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, data: Any = None) -> Any:
        return self.request("POST", path, json=data)

    def put(self, path: str, data: Any = None) -> Any:
        return self.request("PUT", path, json=data)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)
