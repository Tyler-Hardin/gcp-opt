"""Credential discovery: explicit token, env vars, GCE metadata server."""

from __future__ import annotations

from urllib.error import URLError

import pytest

from gcp_opt.auth import (
    metadata_access_token,
    metadata_project_id,
    parse_metadata_token,
    resolve_access_token,
)

_TOKEN_ENV = ("GOOGLE_OAUTH_ACCESS_TOKEN", "GCP_ACCESS_TOKEN")


def _clear_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)


def test_parse_metadata_token() -> None:
    assert parse_metadata_token('{"access_token": "abc", "expires_in": 3599}') == "abc"
    assert parse_metadata_token("not json") is None
    assert parse_metadata_token('{"foo": 1}') is None


def test_metadata_access_token_sends_flavor_header() -> None:
    seen: dict[str, object] = {}

    def fetch(url: str, headers: dict[str, str], timeout: float) -> str:
        seen["url"] = url
        seen["headers"] = headers
        return '{"access_token": "tok"}'

    assert metadata_access_token(fetcher=fetch) == "tok"
    assert seen["headers"] == {"Metadata-Flavor": "Google"}
    assert "instance/service-accounts/default/token" in str(seen["url"])


def test_metadata_access_token_absent_is_none() -> None:
    def fetch(url: str, headers: dict[str, str], timeout: float) -> str:
        raise URLError("metadata server not reachable")

    assert metadata_access_token(fetcher=fetch) is None


def test_metadata_project_id_strips_whitespace() -> None:
    assert metadata_project_id(fetcher=lambda url, headers, timeout: "my-proj\n") == "my-proj"


def test_metadata_host_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCE_METADATA_HOST", "127.0.0.1:9999")
    seen: dict[str, str] = {}

    def fetch(url: str, headers: dict[str, str], timeout: float) -> str:
        seen["url"] = url
        return "proj"

    metadata_project_id(fetcher=fetch)
    assert seen["url"].startswith("http://127.0.0.1:9999/computeMetadata/v1/")


def test_resolve_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_token_env(monkeypatch)
    assert resolve_access_token("explicit") == ("explicit", "explicit")

    monkeypatch.setenv("GCP_ACCESS_TOKEN", "envtok")
    assert resolve_access_token(None) == ("envtok", "env:GCP_ACCESS_TOKEN")

    _clear_token_env(monkeypatch)
    assert resolve_access_token(None, use_metadata=False) == (None, "none")


def test_resolve_falls_back_to_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_token_env(monkeypatch)
    token, source = resolve_access_token(
        None, fetcher=lambda url, headers, timeout: '{"access_token": "meta"}'
    )
    assert (token, source) == ("meta", "gce-metadata")


def test_resolve_metadata_not_used_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_token_env(monkeypatch)
    token, source = resolve_access_token(
        None,
        use_metadata=False,
        fetcher=lambda url, headers, timeout: '{"access_token": "meta"}',
    )
    assert (token, source) == (None, "none")
