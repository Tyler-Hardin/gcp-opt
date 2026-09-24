"""Minimal, injectable JSON-over-HTTP transport.

Only the standard library is used, and the transport is a ``Protocol`` so tests
can inject a fake without monkeypatching.  Google Cloud APIs return actionable
error bodies, so those are surfaced verbatim in :class:`~gcp_opt.errors.ApiError`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from gcp_opt.errors import ApiAuthError, ApiError

_DEFAULT_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


@runtime_checkable
class JsonTransport(Protocol):
    """Fetches JSON documents from absolute URLs."""

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform a GET and decode the JSON body."""
        ...  # pragma: no cover - protocol definition


class UrllibJsonTransport:
    """A retrying :class:`JsonTransport` built on :mod:`urllib.request`."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_attempts: int = 4,
        backoff_seconds: float = 0.5,
        user_agent: str = "gcp-opt/0.1",
    ) -> None:
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff_seconds
        self._user_agent = user_agent

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform a GET with bounded retries on transient failures.

        Raises:
            ApiAuthError: on HTTP 401/403.
            ApiError: on any other non-2xx response or malformed JSON.
        """
        if params:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{urllib.parse.urlencode(params)}"
        request_headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)

        last_error: ApiError | None = None
        for attempt in range(self._max_attempts):
            request = urllib.request.Request(url, headers=request_headers)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    body = response.read().decode("utf-8")
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise ApiError(
                        f"expected a JSON object from {url}, got {type(parsed).__name__}", url=url
                    )
                return parsed
            except urllib.error.HTTPError as error:
                status = error.code
                detail = _error_detail(error)
                if status in {401, 403}:
                    raise ApiAuthError(
                        f"authentication/authorization failed (HTTP {status}) for {url}: {detail}",
                        status=status,
                        url=url,
                    ) from error
                last_error = ApiError(
                    f"HTTP {status} for {url}: {detail}", status=status, url=url
                )
                if status not in _DEFAULT_RETRY_STATUS or attempt == self._max_attempts - 1:
                    raise last_error from error
            except urllib.error.URLError as error:
                last_error = ApiError(f"network error for {url}: {error.reason}", url=url)
                if attempt == self._max_attempts - 1:
                    raise last_error from error
            except json.JSONDecodeError as error:
                raise ApiError(f"invalid JSON from {url}: {error}", url=url) from error
            time.sleep(self._backoff * (2**attempt))
        raise last_error or ApiError(f"request failed: {url}", url=url)


def _error_detail(error: urllib.error.HTTPError) -> str:
    """Best-effort extraction of a Google API error message."""
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except Exception:
        return error.reason or "unknown error"
    if isinstance(payload, dict):
        inner = payload.get("error")
        if isinstance(inner, dict) and "message" in inner:
            return str(inner["message"])
        if "message" in payload:
            return str(payload["message"])
    return json.dumps(payload)[:500]
