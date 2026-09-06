"""Tests for ClaudeProvider — Phase 5A Claude provider implementation.

All HTTP tests use unittest.mock to mock urllib.request.urlopen. No real
API calls are ever made.

Fake secrets are used to verify that API keys never leak into exceptions,
logs, or return values.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from handoff_agent.providers.base import ProviderAdapter, ProviderConfigError
from handoff_agent.providers.claude import (
    ClaudeBadResponseError,
    ClaudeHttpError,
    ClaudeMissingApiKeyError,
    ClaudeNetworkError,
    ClaudeProvider,
    ClaudeProviderError,
)
from handoff_agent.context_builder import (
    FileEntry,
    FullContext,
    GitInfo,
    ProjectInfo,
    SecurityInfo,
)

FAKE_KEY = "sk-test-abcdefghijklmnopqrstuvwxyz1234567890"
FAKE_MODEL = "claude-3-sonnet-20240229"


def _fake_context() -> FullContext:
    return FullContext(
        project=ProjectInfo(root="/tmp", name="t", project_type="Python"),
        git=GitInfo(
            branch="main", head="abc", clean=True, status="clean",
            modified_files=(), untracked_files=(), staged_files=(),
            deleted_files=(), recent_commits=(), diff_stat={"files_changed": 0, "insertions": 0, "deletions": 0},
            remotes=(),
        ),
        files=(FileEntry(path="a.py", content="x = 1", size=5),),
        security=SecurityInfo(excluded_count=0, exclusion_summary={}, included_count=1, omitted_count=0, omission_reasons={}),
    )


def _mock_response(body: dict[str, Any] | str) -> MagicMock:
    """Create a mock urllib response object."""
    mock_resp = MagicMock()
    if isinstance(body, dict):
        raw = json.dumps(body).encode("utf-8")
    else:
        raw = body.encode("utf-8")
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _mock_http_error(code: int, body: str = "") -> Any:
    """Create a urllib.error.HTTPError."""
    import urllib.error
    resp = MagicMock()
    resp.read.return_value = body.encode("utf-8") if body else b""
    return urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=code,
        msg=f"HTTP {code}",
        hdrs={},
        fp=resp,
    )


def _provider(config: dict[str, Any] | None = None) -> ClaudeProvider:
    """Create a ClaudeProvider with optional config."""
    return ClaudeProvider(config=config)


# ===================================================================
# Provider abstraction
# ===================================================================

class TestProviderAbstraction:
    def test_base_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            ProviderAdapter()  # type: ignore[abstract]

    def test_claude_satisfies_interface(self) -> None:
        assert issubclass(ClaudeProvider, ProviderAdapter)

    def test_provider_name(self) -> None:
        assert _provider().name() == "claude"

    def test_claude_is_provider_adapter(self) -> None:
        p = _provider()
        assert isinstance(p, ProviderAdapter)


# ===================================================================
# Configuration
# ===================================================================

class TestConfiguration:
    def test_configurable_model(self) -> None:
        p = _provider({"model": FAKE_MODEL})
        assert p.model == FAKE_MODEL

    def test_configurable_api_key_env(self) -> None:
        p = _provider({"api_key_env": "MY_CUSTOM_KEY"})
        assert p.api_key_env == "MY_CUSTOM_KEY"

    def test_default_model_used(self) -> None:
        p = _provider()
        assert p.model == ClaudeProvider.DEFAULT_MODEL

    def test_default_api_key_env(self) -> None:
        p = _provider()
        assert p.api_key_env == "ANTHROPIC_API_KEY"

    def test_validate_config_defaults_model(self) -> None:
        p = _provider()
        assert p.validate_config({}) is True
        assert p.validate_config({"model": ""}) is True
        assert p.validate_config({"model": FAKE_MODEL}) is True

    def test_validate_config_with_api_key_env(self) -> None:
        p = _provider()
        assert p.validate_config({"model": FAKE_MODEL, "api_key_env": "X"}) is True
        assert p.validate_config({"model": FAKE_MODEL, "api_key_env": ""}) is False

    def test_validate_config_rejects_non_https_endpoint(self) -> None:
        p = _provider()
        assert p.validate_config({"endpoint": "http://insecure.example.com"}) is False
        assert p.validate_config({"endpoint": "https://api.example.com"}) is True

    def test_generate_rejects_non_https_endpoint(self) -> None:
        p = _provider(
            {"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999",
             "endpoint": "http://insecure.example.com"}
        )
        with patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}):
            with pytest.raises(ProviderConfigError, match="https"):
                p.generate(_fake_context(), "hello")
        assert p.model == FAKE_MODEL

    def test_is_configured_false_when_env_unset(self) -> None:
        p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
        assert p.is_configured() is False

    def test_is_configured_true_when_env_set(self) -> None:
        p = _provider({"api_key_env": "FAKE_TEST_KEY_999"})
        with patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}):
            assert p.is_configured() is True


# ===================================================================
# API key handling
# ===================================================================

class TestApiKeyHandling:
    def test_missing_key_safe_error(self) -> None:
        p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
        with pytest.raises(ClaudeMissingApiKeyError, match="is not configured"):
            p.generate(_fake_context(), "hello")

    def test_missing_key_message_contains_env_name(self) -> None:
        env = "CUSTOM_ANTHROPIC_KEY"
        p = _provider({"api_key_env": env})
        with pytest.raises(ClaudeMissingApiKeyError, match=env):
            p.generate(_fake_context(), "hello")

    def test_api_key_never_in_exception(self) -> None:
        """The actual secret value must not appear in any exception."""
        p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
        with pytest.raises(ClaudeMissingApiKeyError) as exc_info:
            p.generate(_fake_context(), "test")
        assert FAKE_KEY not in str(exc_info.value)

    def test_api_key_not_in_generate_output(self) -> None:
        """API key must never appear in the return value."""
        p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
        with pytest.raises(ClaudeMissingApiKeyError):
            result = p.generate(_fake_context(), "prompt")
            assert FAKE_KEY not in str(result)

    def test_api_key_not_in_config(self) -> None:
        """API key should not be stored in config."""
        p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
        # The provider stores api_key_env name, not the key itself
        assert not hasattr(p, "_api_key")
        assert p.api_key_env != FAKE_KEY


# ===================================================================
# HTTP tests — mock transport
# ===================================================================

class TestClaudeHTTP:
    def test_generate_success(self) -> None:
        """Mock returns valid response → generate returns text."""
        response_body = {
            "content": [{"type": "text", "text": "Hello from Claude"}]
        }
        mock_resp = _mock_response(response_body)
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            result = p.generate(_fake_context(), "hello")
        assert result == "Hello from Claude"

    def test_generate_multiple_text_blocks(self) -> None:
        response_body = {
            "content": [
                {"type": "text", "text": "Line 1"},
                {"type": "text", "text": "Line 2"},
            ]
        }
        mock_resp = _mock_response(response_body)
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            result = p.generate(_fake_context(), "hello")
        assert "Line 1" in result
        assert "Line 2" in result

    def test_generate_http_error(self) -> None:
        """HTTP 401 → ClaudeHttpError."""
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=_mock_http_error(401, "unauthorized")),
        ):
            with pytest.raises(ClaudeHttpError, match="HTTP 401"):
                p.generate(_fake_context(), "hello")

    def test_generate_http_429_rate_limit(self) -> None:
        """HTTP 429 → ClaudeHttpError."""
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=_mock_http_error(429, "rate limited")),
        ):
            with pytest.raises(ClaudeHttpError, match="HTTP 429"):
                p.generate(_fake_context(), "hello")

    def test_generate_malformed_response(self) -> None:
        """Non-JSON response → ClaudeBadResponseError."""
        mock_resp = _mock_response("not json at all {{{")
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            with pytest.raises(ClaudeBadResponseError, match="unreadable"):
                p.generate(_fake_context(), "hello")

    def test_generate_unexpected_response_structure(self) -> None:
        """JSON but wrong structure → ClaudeBadResponseError."""
        mock_resp = _mock_response({"wrong_key": "value"})
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            with pytest.raises(ClaudeBadResponseError, match="unexpected"):
                p.generate(_fake_context(), "hello")

    def test_generate_empty_content_list(self) -> None:
        """Content is empty list → ClaudeBadResponseError."""
        mock_resp = _mock_response({"content": []})
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            with pytest.raises(ClaudeBadResponseError, match="no text"):
                p.generate(_fake_context(), "hello")

    def test_generate_network_error(self) -> None:
        """URLError → ClaudeNetworkError."""
        import urllib.error
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS failed")),
        ):
            with pytest.raises(ClaudeNetworkError, match="network"):
                p.generate(_fake_context(), "hello")

    def test_generate_timeout_error(self) -> None:
        """TimeoutError → ClaudeNetworkError."""
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")),
        ):
            with pytest.raises(ClaudeNetworkError, match="network"):
                p.generate(_fake_context(), "hello")

    def test_generate_oserror(self) -> None:
        """OSError → ClaudeNetworkError."""
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=OSError("connection refused")),
        ):
            with pytest.raises(ClaudeNetworkError, match="network"):
                p.generate(_fake_context(), "hello")


# ===================================================================
# HTTP headers
# ===================================================================

class TestRequestHeaders:
    def test_request_sends_correct_headers(self) -> None:
        """Verify x-api-key, anthropic-version, content-type headers."""
        response_body = {"content": [{"type": "text", "text": "ok"}]}
        mock_resp = _mock_response(response_body)
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen,
        ):
            p.generate(_fake_context(), "hello")
            request = mock_urlopen.call_args[0][0]
            hdrs = request.headers
            assert hdrs.get("X-api-key") == FAKE_KEY
            assert hdrs.get("Anthropic-version") == "2023-06-01"
            assert hdrs.get("Content-type") == "application/json"

    def test_request_method_is_post(self) -> None:
        response_body = {"content": [{"type": "text", "text": "ok"}]}
        mock_resp = _mock_response(response_body)
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen,
        ):
            p.generate(_fake_context(), "hello")
            request = mock_urlopen.call_args[0][0]
            assert request.method == "POST"

    def test_request_payload_contains_model(self) -> None:
        response_body = {"content": [{"type": "text", "text": "ok"}]}
        mock_resp = _mock_response(response_body)
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen,
        ):
            p.generate(_fake_context(), "hello")
            request = mock_urlopen.call_args[0][0]
            payload = json.loads(request.data)
            assert payload["model"] == FAKE_MODEL
            assert "messages" in payload
            assert payload["messages"][0]["content"] == "hello"


# ===================================================================
# Error handling — secrets must not leak
# ===================================================================

class TestErrorSecurity:
    FAKE_SECRET_VALUE = "sk-supersecret-abcdefghijklmnopqrstuvwxyz123456"

    def test_http_error_never_leaks_key(self) -> None:
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": self.FAKE_SECRET_VALUE}),
            patch("urllib.request.urlopen", side_effect=_mock_http_error(403, "forbidden")),
        ):
            with pytest.raises(ClaudeHttpError) as exc_info:
                p.generate(_fake_context(), "test")
            assert self.FAKE_SECRET_VALUE not in str(exc_info.value)

    def test_network_error_never_leaks_key(self) -> None:
        import urllib.error
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": self.FAKE_SECRET_VALUE}),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("fail")),
        ):
            with pytest.raises(ClaudeNetworkError) as exc_info:
                p.generate(_fake_context(), "test")
            assert self.FAKE_SECRET_VALUE not in str(exc_info.value)

    def test_malformed_response_error_never_leaks_key(self) -> None:
        mock_resp = _mock_response("garbage")
        p = _provider({"model": FAKE_MODEL, "api_key_env": "FAKE_TEST_KEY_999"})
        with (
            patch.dict("os.environ", {"FAKE_TEST_KEY_999": self.FAKE_SECRET_VALUE}),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            with pytest.raises(ClaudeBadResponseError) as exc_info:
                p.generate(_fake_context(), "test")
            assert self.FAKE_SECRET_VALUE not in str(exc_info.value)

    def test_api_key_not_in_str_representation(self) -> None:
        p = _provider({"api_key_env": "FAKE_TEST_KEY_999"})
        with patch.dict("os.environ", {"FAKE_TEST_KEY_999": self.FAKE_SECRET_VALUE}):
            # Provider should not expose API key in any representation
            assert self.FAKE_SECRET_VALUE not in str(p)


# ===================================================================
# Side effects — provider must not modify anything
# ===================================================================

class TestNoSideEffects:
    def test_provider_does_not_modify_files(self) -> None:
        """generate() must not create or modify any files."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            before = set(Path(td).iterdir())
            p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
            with pytest.raises(ClaudeMissingApiKeyError):
                p.generate(_fake_context(), "hello")
            after = set(Path(td).iterdir())
            assert before == after

    def test_provider_does_not_run_git(self) -> None:
        """generate() must not execute git commands."""
        import unittest.mock
        p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
        with patch("subprocess.run") as mock_run:
            with pytest.raises(ClaudeMissingApiKeyError):
                p.generate(_fake_context(), "hello")
            mock_run.assert_not_called()

    def test_provider_does_not_write_commits(self) -> None:
        """generate() must not create HANDOFF.md or commit."""
        import unittest.mock
        p = _provider({"api_key_env": "FAKE_MISSING_KEY_XYZ_999"})
        with pytest.raises(ClaudeMissingApiKeyError):
            p.generate(_fake_context(), "generate a handoff doc")
        # If we reach here without writing files, the test passes
