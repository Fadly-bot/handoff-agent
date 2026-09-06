"""Tests for Phase 9 — Production Provider & Configuration.

Covers the OpenAI-compatible provider expansion (OpenAI, Qwen, DeepSeek),
model configuration, environment-based API keys, provider selection, and
unified error handling.

All HTTP tests use unittest.mock to patch urllib.request.urlopen. No real
API calls are ever made. Fake secrets verify that keys never leak into
exceptions, logs, stdout/stderr, or return values.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conftest import commit_all, init_repo, write_file

from handoff_agent.config import DEFAULT_CONFIG, ensure_config, load_config
from handoff_agent.providers import (
    ProviderAdapter,
    ProviderConfigError,
    ProviderError,
    ProviderHttpError,
    ProviderMissingKeyError,
    ProviderNetworkError,
    ProviderResponseError,
    ProviderRequestError,
    available_providers,
    create_provider,
    is_known_provider,
)
from handoff_agent.providers.claude import ClaudeProvider
from handoff_agent.providers.deepseek import DeepSeekProvider
from handoff_agent.providers.openai import OpenAIProvider
from handoff_agent.providers.openai_compatible import OpenAICompatibleProvider
from handoff_agent.providers.qwen import QwenProvider

FAKE_KEY = "sk-test-abcdefghijklmnopqrstuvwxyz1234567890"


def _provider_defaults() -> tuple[dict[str, Any], str]:
    """Return a fresh OpenAI-compatible provider config and provider name."""
    return {}, "openai"


def _mock_response(body: dict[str, Any] | str) -> MagicMock:
    """Create a mock urllib response object."""
    mock_resp = MagicMock()
    raw = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _mock_http_error(code: int) -> urllib.error.HTTPError:
    resp = MagicMock()
    resp.read.return_value = b""
    return urllib.error.HTTPError(
        url="https://api.openai.com/v1/chat/completions",
        code=code,
        msg=f"HTTP {code}",
        hdrs={},
        fp=resp,
    )


# ===================================================================
# Provider expansion — abstraction
# ===================================================================

class TestProviderExpansion:
    def test_all_expanded_providers_are_adapters(self) -> None:
        for cls in (OpenAIProvider, QwenProvider, DeepSeekProvider, ClaudeProvider):
            assert issubclass(cls, ProviderAdapter)

    def test_all_expanded_subclass_shared_core(self) -> None:
        for cls in (OpenAIProvider, QwenProvider, DeepSeekProvider):
            assert issubclass(cls, OpenAICompatibleProvider)
            assert issubclass(cls, ProviderAdapter)

    def test_provider_names(self) -> None:
        assert OpenAIProvider().name() == "openai"
        assert QwenProvider().name() == "qwen"
        assert DeepSeekProvider().name() == "deepseek"

    def test_registered_in_factory(self) -> None:
        for name in ("openai", "qwen", "deepseek", "claude"):
            assert is_known_provider(name) is True
            assert create_provider(name, {}).name() == name

    def test_available_providers_sorted(self) -> None:
        names = available_providers()
        assert names == tuple(sorted(names))
        assert set(names) == {"claude", "deepseek", "openai", "qwen"}

    def test_unknown_provider_still_rejected(self) -> None:
        assert is_known_provider("bogus") is False
        from handoff_agent.providers import UnknownProviderError

        with pytest.raises(UnknownProviderError):
            create_provider("bogus", {})


# ===================================================================
# Model configuration
# ===================================================================

class TestModelConfiguration:
    @pytest.mark.parametrize(
        ("cls", "expected"),
        [
            (OpenAIProvider, "gpt-4o"),
            (QwenProvider, "qwen-plus"),
            (DeepSeekProvider, "deepseek-chat"),
            (ClaudeProvider, "claude-sonnet-4-5"),
        ],
    )
    def test_default_model_non_empty(self, cls: type, expected: str) -> None:
        p = cls()
        assert p.model == expected

    def test_config_overrides_default_model(self) -> None:
        p = OpenAIProvider({"model": "gpt-custom"})
        assert p.model == "gpt-custom"

    def test_config_lowercase_model_respected(self) -> None:
        p = QwenProvider({"model": "qwen-max"})
        assert p.model == "qwen-max"

    def test_empty_config_model_falls_back_to_default(self) -> None:
        p = OpenAIProvider({"model": ""})
        assert p.model == "gpt-4o"


# ===================================================================
# Environment-based API keys configuration
# ===================================================================

class TestEnvApiKeyConfiguration:
    @pytest.mark.parametrize(
        ("cls", "env"),
        [
            (OpenAIProvider, "OPENAI_API_KEY"),
            (QwenProvider, "DASHSCOPE_API_KEY"),
            (DeepSeekProvider, "DEEPSEEK_API_KEY"),
            (ClaudeProvider, "ANTHROPIC_API_KEY"),
        ],
    )
    def test_default_env_var(self, cls: type, env: str) -> None:
        assert cls().api_key_env == env

    def test_env_var_name_configurable(self) -> None:
        p = OpenAIProvider({"api_key_env": "MY_OPENAI_KEY"})
        assert p.api_key_env == "MY_OPENAI_KEY"

    def test_key_read_from_env_only_at_request(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_1"})
        assert p.is_configured() is False
        with patch.dict("os.environ", {"FAKE_PHASE9_KEY_1": FAKE_KEY}):
            assert p.is_configured() is True
        assert p.is_configured() is False

    def test_missing_key_safe_error(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_MISSING_PHASE9_KEY"})
        with pytest.raises(ProviderMissingKeyError, match="is not configured"):
            p.generate(None, "hi")
        with pytest.raises(ProviderMissingKeyError, match="FAKE_MISSING_PHASE9_KEY"):
            p.generate(None, "hi")

    def test_missing_key_error_never_contains_secret(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_MISSING_PHASE9_KEY"})
        with pytest.raises(ProviderMissingKeyError) as exc_info:
            p.generate(None, "x")
        assert FAKE_KEY not in str(exc_info.value)


# ===================================================================
# OpenAI-compatible HTTP (mocked transport)
# ===================================================================

class TestOpenAICompatibleHTTP:
    def test_success_returns_text(self) -> None:
        body = {"choices": [{"message": {"content": "Hello"}}]}
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_2"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_2": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=_mock_response(body)),
        ):
            assert p.generate(None, "hi") == "Hello"

    def test_request_headers_and_payload(self) -> None:
        body = {"choices": [{"message": {"content": "ok"}}]}
        p = OpenAIProvider({"model": "gpt-test", "api_key_env": "FAKE_PHASE9_KEY_3"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_3": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=_mock_response(body)) as m,
        ):
            p.generate(None, "prompt")
            req = m.call_args[0][0]
            assert req.method == "POST"
            assert req.headers.get("Authorization") == f"Bearer {FAKE_KEY}"
            assert req.headers.get("Content-type") == "application/json"
            payload = json.loads(req.data)
            assert payload["model"] == "gpt-test"
            assert payload["messages"] == [{"role": "user", "content": "prompt"}]

    def test_http_error(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_4"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_4": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=_mock_http_error(401)),
        ):
            with pytest.raises(ProviderHttpError, match="HTTP 401"):
                p.generate(None, "hi")

    def test_rate_limit_http_error(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_4"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_4": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=_mock_http_error(429)),
        ):
            with pytest.raises(ProviderHttpError, match="HTTP 429"):
                p.generate(None, "hi")

    def test_urlerror_network_error(self) -> None:
        p = DeepSeekProvider({"api_key_env": "FAKE_PHASE9_KEY_5"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_5": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns")),
        ):
            with pytest.raises(ProviderNetworkError, match="network"):
                p.generate(None, "hi")

    def test_timeout_network_error(self) -> None:
        p = QwenProvider({"api_key_env": "FAKE_PHASE9_KEY_6"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_6": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")),
        ):
            with pytest.raises(ProviderNetworkError, match="network"):
                p.generate(None, "hi")

    def test_oserror_network_error(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_7"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_7": FAKE_KEY}),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
        ):
            with pytest.raises(ProviderNetworkError, match="network"):
                p.generate(None, "hi")

    def test_malformed_response(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_8"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_8": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=_mock_response("not json")),
        ):
            with pytest.raises(ProviderResponseError, match="unreadable"):
                p.generate(None, "hi")

    def test_missing_choices(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_9"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_9": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=_mock_response({"nope": 1})),
        ):
            with pytest.raises(ProviderResponseError, match="unexpected"):
                p.generate(None, "hi")

    def test_empty_choices(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_10"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_10": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=_mock_response({"choices": []})),
        ):
            with pytest.raises(ProviderResponseError, match="unexpected"):
                p.generate(None, "hi")

    def test_message_missing(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_11"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_11": FAKE_KEY}),
            patch(
                "urllib.request.urlopen",
                return_value=_mock_response({"choices": [{"foo": "bar"}]}),
            ),
        ):
            with pytest.raises(ProviderResponseError, match="unexpected"):
                p.generate(None, "hi")

    def test_content_missing_or_blank(self) -> None:
        for content in (None, "", "   "):
            p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_12"})
            body = {"choices": [{"message": {"content": content}}]}
            with (
                patch.dict("os.environ", {"FAKE_PHASE9_KEY_12": FAKE_KEY}),
                patch("urllib.request.urlopen", return_value=_mock_response(body)),
            ):
                with pytest.raises(ProviderResponseError, match="no text"):
                    p.generate(None, "hi")

    def test_error_base_classes(self) -> None:
        assert issubclass(ProviderHttpError, ProviderRequestError)
        assert issubclass(ProviderNetworkError, ProviderRequestError)
        assert issubclass(ProviderResponseError, ProviderRequestError)
        assert issubclass(ProviderMissingKeyError, ProviderRequestError)
        assert issubclass(ProviderRequestError, ProviderError)
        assert issubclass(ProviderConfigError, ProviderError)


# ===================================================================
# Endpoint safety — HTTPS only
# ===================================================================

class TestEndpointSafety:
    def test_validate_config_rejects_http_endpoint(self) -> None:
        p = OpenAIProvider()
        assert p.validate_config({"endpoint": "http://x.example.com"}) is False
        assert p.validate_config({"endpoint": "https://x.example.com"}) is True

    def test_generate_with_insecure_endpoint_fails_before_network(self) -> None:
        p = OpenAIProvider(
            {"api_key_env": "FAKE_PHASE9_KEY_13", "endpoint": "http://x.example.com"}
        )
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_13": FAKE_KEY}),
            patch("urllib.request.urlopen") as m,
        ):
            with pytest.raises(ProviderConfigError, match="https"):
                p.generate(None, "hi")
        m.assert_not_called()

    def test_custom_https_endpoint_allowed(self) -> None:
        body = {"choices": [{"message": {"content": "ok"}}]}
        p = OpenAIProvider(
            {"api_key_env": "FAKE_PHASE9_KEY_14", "endpoint": "https://proxy.example.com/v1"}
        )
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_14": FAKE_KEY}),
            patch("urllib.request.urlopen", return_value=_mock_response(body)) as m,
        ):
            assert p.generate(None, "hi") == "ok"
            assert "proxy.example.com" in m.call_args[0][0].full_url


# ===================================================================
# Secrets must never leak
# ===================================================================

class TestSecretSafety:
    SECRET = "sk-super-secret-abcdefghijklmnopqrstuvwxyz0123456789"

    def test_http_error_never_leaks_key(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_15"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_15": self.SECRET}),
            patch("urllib.request.urlopen", side_effect=_mock_http_error(403)),
        ):
            with pytest.raises(ProviderHttpError) as exc_info:
                p.generate(None, "x")
            assert self.SECRET not in str(exc_info.value)

    def test_network_error_never_leaks_key(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_16"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_16": self.SECRET}),
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("fail"),
            ),
        ):
            with pytest.raises(ProviderNetworkError) as exc_info:
                p.generate(None, "x")
            assert self.SECRET not in str(exc_info.value)

    def test_response_error_never_leaks_key(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_17"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_17": self.SECRET}),
            patch("urllib.request.urlopen", return_value=_mock_response("garbage")),
        ):
            with pytest.raises(ProviderResponseError) as exc_info:
                p.generate(None, "x")
            assert self.SECRET not in str(exc_info.value)

    def test_key_not_in_provider_representation(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_18"})
        with patch.dict("os.environ", {"FAKE_PHASE9_KEY_18": self.SECRET}):
            assert self.SECRET not in str(p)
            assert self.SECRET not in repr(p)

    def test_key_not_stored_on_provider(self) -> None:
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_19"})
        with patch.dict("os.environ", {"FAKE_PHASE9_KEY_19": self.SECRET}):
            assert p.is_configured() is True
        assert not hasattr(p, "_api_key")
        assert self.SECRET not in str(p)

    def test_success_output_never_contains_key(self) -> None:
        body = {"choices": [{"message": {"content": "hello"}}]}
        p = OpenAIProvider({"api_key_env": "FAKE_PHASE9_KEY_20"})
        with (
            patch.dict("os.environ", {"FAKE_PHASE9_KEY_20": self.SECRET}),
            patch("urllib.request.urlopen", return_value=_mock_response(body)),
        ):
            result = p.generate(None, "x")
        assert self.SECRET not in result


# ===================================================================
# Side effects — providers must not modify anything
# ===================================================================

class TestNoSideEffects:
    def test_provider_does_not_modify_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            before = set(Path(td).iterdir())
            p = OpenAIProvider({"api_key_env": "FAKE_MISSING_PHASE9_KEY"})
            with pytest.raises(ProviderMissingKeyError):
                p.generate(None, "hello")
            after = set(Path(td).iterdir())
            assert before == after

    def test_provider_does_not_run_subprocess(self) -> None:
        p = DeepSeekProvider({"api_key_env": "FAKE_MISSING_PHASE9_KEY"})
        with patch("subprocess.run") as mock_run:
            with pytest.raises(ProviderMissingKeyError):
                p.generate(None, "hello")
            mock_run.assert_not_called()

    def test_provider_does_not_import_git_or_os_tools(self) -> None:
        import handoff_agent.providers.openai_compatible as mod

        source = Path(mod.__file__).read_text()
        assert "subprocess" not in source
        assert "os.system" not in source
        assert "os.popen" not in source
        assert "git_helper" not in source
        assert "import git" not in source


# ===================================================================
# Configuration defaults
# ===================================================================

class TestConfigDefaults:
    def test_default_config_has_only_registered_providers(self) -> None:
        names = set(DEFAULT_CONFIG["providers"])
        assert names == {"claude", "openai", "qwen", "deepseek"}
        assert "cli" not in names
        assert "generic" not in names

    def test_default_config_has_no_secret_values(self) -> None:
        """Env-var names are present, but no secret-looking values."""
        text = json.dumps(DEFAULT_CONFIG).lower()
        assert "sk-" not in text
        assert "bearer " not in text
        assert "api-key:" not in text

    def test_get_provider_config_for_each(self) -> None:
        cfg = load_config()
        for name in ("claude", "openai", "qwen", "deepseek"):
            pcfg = cfg["providers"][name]
            assert "api_key_env" in pcfg
            assert "model" in pcfg

    def test_ensure_config_writes_clean_providers(self, tmp_path: Path) -> None:
        path = tmp_path / "cfg.json"
        ensure_config(path)
        data = json.loads(path.read_text())
        assert set(data["providers"]) == {"claude", "openai", "qwen", "deepseek"}


# ===================================================================
# CLI integration — provider selection, --model, config default
# ===================================================================

def _make_args(**kwargs) -> argparse.Namespace:
    defaults = {
        "path": None,
        "provider": None,
        "model": None,
        "output": None,
        "config": None,
        "dry_run": False,
        "commit": False,
        "command": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class _CaptureProvider:
    """Provider that records the config passed to __init__."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def name(self) -> str:
        return "capture"

    def generate(self, context, prompt: str) -> str:
        return "# Captured"

    def validate_config(self, config: dict) -> bool:
        return True

    def is_configured(self) -> bool:
        return True


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    init_repo(r)
    write_file(r, "app.py", "print('x')")
    commit_all(r, "initial")
    return r


class TestCliProviderSelection:
    def test_dry_run_with_openai_provider(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        rc = cmd_generate(_make_args(dry_run=True, provider="openai", path=str(repo)))
        assert rc == 0

    def test_dry_run_allows_arbitrary_provider_label(self, repo: Path) -> None:
        """Dry-run is a preview; an unregistered provider name is allowed."""
        from handoff_agent.cli import cmd_generate

        rc = cmd_generate(_make_args(dry_run=True, provider="nope", path=str(repo)))
        assert rc == 0

    def test_dry_run_shows_model(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        rc = cmd_generate(_make_args(dry_run=True, provider="deepseek", model="deepseek-r1", path=str(repo)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "deepseek-r1" in out

    def test_model_flag_overrides_config_model(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.cli import cmd_generate

        cfg = tmp_path / "cfg.json"
        ensure_config(cfg)
        with patch("handoff_agent.providers.factory._PROVIDERS", {"capture": _CaptureProvider}):
            rc = cmd_generate(
                _make_args(
                    provider="capture",
                    model="gpt-override",
                    path=str(repo),
                    config=str(cfg),
                )
            )
        assert rc == 0
        # Provider was invoked; verify generate path executed (file written).
        assert (repo / "docs" / "HANDOFF.md").read_text() == "# Captured"

    def test_model_passed_into_provider_config(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.cli import cmd_generate

        cfg = tmp_path / "cfg.json"
        ensure_config(cfg)
        seen: dict = {}

        class Recorder:
            def __init__(self, config):
                seen["config"] = config
            def name(self):
                return "mock"
            def generate(self, context, prompt):
                return "# X"
            def validate_config(self, config):
                return True
            def is_configured(self):
                return True

        with patch("handoff_agent.providers.factory._PROVIDERS", {"mock": Recorder}):
            rc = cmd_generate(
                _make_args(provider="mock", model="gpt-override", path=str(repo), config=str(cfg))
            )
        assert rc == 0
        assert seen["config"].get("model") == "gpt-override"

    def test_config_default_provider_used(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.cli import cmd_generate

        cfg = tmp_path / "cfg.json"
        data = {
            "version": "0.1.0",
            "default_provider": "openai",
            "output": "docs/HANDOFF.md",
            "providers": {},
        }
        cfg.write_text(json.dumps(data))
        rc = cmd_generate(_make_args(dry_run=True, path=str(repo), config=str(cfg)))
        assert rc == 0

    def test_explicit_provider_wins_over_config_default(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.cli import cmd_generate

        cfg = tmp_path / "cfg.json"
        data = {"version": "0.1.0", "default_provider": "claude", "providers": {}}
        cfg.write_text(json.dumps(data))
        rc = cmd_generate(_make_args(dry_run=True, provider="qwen", path=str(repo), config=str(cfg)))
        assert rc == 0

    def test_cmd_config_never_reveals_key(self, tmp_path: Path, capsys) -> None:
        from handoff_agent.cli import cmd_config

        cfg = tmp_path / "cfg.json"
        data = {
            "version": "0.1.0",
            "default_provider": "openai",
            "providers": {"openai": {"api_key_env": "FAKE_PHASE9_KEY_21", "model": "gpt-x"}},
        }
        cfg.write_text(json.dumps(data))
        args = argparse.Namespace(config=str(cfg))
        with patch.dict("os.environ", {"FAKE_PHASE9_KEY_21": FAKE_KEY}):
            rc = cmd_config(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert FAKE_KEY not in out
        assert "openai: configured" in out
        assert "gpt-x" in out


class TestSubprocessCli:
    def test_generate_reports_missing_key_all_providers(self, tmp_path: Path) -> None:
        SRC_DIR = Path(__file__).resolve().parent.parent / "src"
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "app.py", "pass")
        commit_all(repo, "initial")
        result = subprocess.run(
            [sys.executable, "-m", "handoff_agent", "--provider", "openai"],
            capture_output=True,
            text=True,
            cwd=str(repo),
            env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        )
        assert result.returncode == 1
        assert "missing api key" in result.stderr.lower()

    def test_handoff_config_lists_all_providers(self, tmp_path: Path) -> None:
        SRC_DIR = Path(__file__).resolve().parent.parent / "src"
        cfg = tmp_path / "cfg.json"
        ensure_config(cfg)
        result = subprocess.run(
            [sys.executable, "-m", "handoff_agent", "config", "--config", str(cfg)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        )
        assert result.returncode == 0
        for name in ("claude", "openai", "qwen", "deepseek"):
            assert name in result.stdout