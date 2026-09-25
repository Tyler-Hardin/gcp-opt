"""Credential discovery for Google Cloud APIs (a small ADC subset).

`gcp-opt`'s live commands accept an explicit ``--access-token``, then fall back to
environment variables and finally to the **GCE metadata server**.  The metadata
fallback is what lets the tool run on a Compute Engine instance using that
instance's service account with no extra configuration (the same source Google's
client libraries call Application Default Credentials).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable

DEFAULT_METADATA_HOST = "metadata.google.internal"
METADATA_FLAVOR = {"Metadata-Flavor": "Google"}

#: Environment variables checked for an access token, in order.
TOKEN_ENV_VARS: tuple[str, ...] = ("GOOGLE_OAUTH_ACCESS_TOKEN", "GCP_ACCESS_TOKEN")

#: Fetcher signature: ``(url, headers, timeout) -> body text``.
Fetcher = Callable[[str, dict[str, str], float], str]


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> str:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return bytes(response.read()).decode("utf-8")


def _metadata_base_url() -> str:
    host = os.environ.get("GCE_METADATA_HOST") or DEFAULT_METADATA_HOST
    return f"http://{host}/computeMetadata/v1"


def _metadata_get(
    path: str,
    *,
    timeout: float = 2.0,
    fetcher: Fetcher | None = None,
) -> str | None:
    """GET a metadata path, returning ``None`` when the metadata server is absent."""
    fetch = fetcher or _default_fetcher
    url = f"{_metadata_base_url()}/{path.lstrip('/')}"
    try:
        return fetch(url, dict(METADATA_FLAVOR), timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def parse_metadata_token(body: str) -> str | None:
    """Extract ``access_token`` from a metadata token response body."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        token = payload.get("access_token")
        if token:
            return str(token)
    return None


def metadata_access_token(
    *,
    timeout: float = 2.0,
    fetcher: Fetcher | None = None,
) -> str | None:
    """Return a token from the GCE metadata server, or ``None`` if unavailable."""
    body = _metadata_get(
        "instance/service-accounts/default/token", timeout=timeout, fetcher=fetcher
    )
    return parse_metadata_token(body) if body is not None else None


def metadata_project_id(
    *,
    timeout: float = 2.0,
    fetcher: Fetcher | None = None,
) -> str | None:
    """Return the instance's project id, or ``None`` if unavailable."""
    body = _metadata_get("project/project-id", timeout=timeout, fetcher=fetcher)
    return body.strip() or None if body is not None else None


def resolve_access_token(
    explicit: str | None = None,
    *,
    env_vars: Iterable[str] = TOKEN_ENV_VARS,
    use_metadata: bool = True,
    timeout: float = 2.0,
    fetcher: Fetcher | None = None,
) -> tuple[str | None, str]:
    """Resolve an OAuth access token and report where it came from.

    Returns ``(token, source)`` where source is ``"explicit"``, ``"env:<VAR>"``,
    ``"gce-metadata"`` or ``"none"``.  Never raises: callers decide whether a
    missing token is fatal.
    """
    if explicit:
        return explicit, "explicit"
    for name in env_vars:
        value = os.environ.get(name)
        if value:
            return value, f"env:{name}"
    if use_metadata:
        token = metadata_access_token(timeout=timeout, fetcher=fetcher)
        if token:
            return token, "gce-metadata"
    return None, "none"
