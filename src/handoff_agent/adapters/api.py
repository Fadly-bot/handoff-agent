"""API-compatible adapter — talks to a remote Handoff JSON service over HTTP.

This adapter models any remote agent that reaches a Handoff deployment through
the HTTP/JSON interface instead of filesystem or CLI access.

Security model:
  - Authentication is read from the environment at request time only
    (``auth_env``); the key is never stored in config and never logged.
  - Only HTTPS endpoints are accepted, except loopback (127.0.0.1 /
    localhost) for local development and testing.
  - All responses are size-limited and parsed as JSON; unexpected data raises
    a safe ``AdapterError``.
  - No arbitrary filesystem access, no Git operations: this adapter only
    issues the documented JSON-REST calls.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from handoff_agent.adapters.base import (
    AdapterConfigError,
    AdapterError,
    AdapterWriteResult,
    BaseAdapter,
)

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MB


def _is_loopback(host: str) -> bool:
    host = host.strip().lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return socket.inet_aton(host)[0] == 127
    except OSError:
        return False


def _validate_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in ("https", "http"):
        raise AdapterConfigError(
            "API adapter base_url must use https (or http for loopback)."
        )
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname or ""):
        raise AdapterConfigError(
            "API adapter refuses plain-http for non-loopback endpoints."
        )
    if not parsed.path.rstrip("/") in ("", "/api", "/handoff"):
        raise AdapterConfigError(
            "API adapter base_url path is unexpected; expected '/', "
            "'/api', or '/handoff'."
        )
    if parsed.query:
        raise AdapterConfigError("API adapter base_url must not contain a query string.")
    return base_url.rstrip("/")


class ApiAdapter(BaseAdapter):
    """Client for a remote Handoff JSON service."""

    adapter_name = "api"
    adapter_version = "0.4.0"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        base_url: str | None = None,
        auth_env: str = "HANDOFF_API_KEY",
        timeout: float = 10.0,
        read_only: bool = False,
        contract=None,
    ) -> None:
        if not base_url:
            raise AdapterConfigError("API adapter requires a base_url.")
        self.base_url = _validate_url(base_url)
        self.auth_env = auth_env
        self._timeout = timeout
        super().__init__(config, read_only=read_only, contract=contract)

    # -- HTTP plumbing -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"handoff-callback/{self.adapter_version}",
        }
        key = os.environ.get(self.auth_env)
        if key:
            # Isolation: the key is attached to the request object only.
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = _safe_http_detail(exc.code)
            raise AdapterError(f"API request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AdapterError(f"API request failed (network): {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise AdapterError(f"API request failed: {exc}") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AdapterError("API response exceeded the size limit.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdapterError("API response was not valid JSON.") from exc

    # -- operations --------------------------------------------------------

    def _do_read_handoff(self) -> str | None:
        data = self._request("GET", "/handoff")
        return _expect_optional(data, "content")

    def _do_write_checkpoint(self, content: str) -> AdapterWriteResult:
        data = self._request("POST", "/checkpoint", {"content": content})
        rel = _expect_string(data, "rel_path")
        return AdapterWriteResult(
            rel_path=rel,
            created=_expect_bool(data, "created"),
            modified=_expect_bool(data, "modified"),
            unchanged=_expect_bool(data, "unchanged"),
            history_recorded=_expect_bool(data, "history_recorded", default=False),
            identity=_expect_string(data, "identity", default=""),
        )

    def _do_project_state(self) -> dict[str, Any]:
        data = self._request("GET", "/project-state")
        if not isinstance(data, dict):
            raise AdapterError("API project-state response was not an object.")
        return data

    def _do_validate_checkpoint(self) -> dict[str, Any]:
        data = self._request("GET", "/validate")
        if not isinstance(data, dict):
            raise AdapterError("API validate response was not an object.")
        return data

    def _do_read_changelog(self) -> str | None:
        data = self._request("GET", "/changelog")
        if "content" not in data:
            return None
        value = data["content"]
        if value is None:
            return None
        if not isinstance(value, str):
            raise AdapterError("API changelog response field 'content' was not a string.")
        return value


def _expect_string(data: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = data.get(key)
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise AdapterError(f"API response field {key!r} was not a string.")
    return value


def _expect_optional(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdapterError(f"API response field {key!r} was not a string or null.")
    return value


def _expect_bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise AdapterError(f"API response field {key!r} was not a boolean.")
    return value


def _safe_http_detail(code: int) -> str:
    if code == 401:
        return "unauthorized (check HANDOFF_API_KEY)"
    if code == 403:
        return "permission denied"
    if code == 404:
        return "endpoint not found"
    return "server error"