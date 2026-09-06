"""Tests for configuration management."""

import json
from pathlib import Path

import pytest

from handoff_agent.config import (
    ensure_config,
    get_provider_config,
    load_config,
    save_config,
)
from handoff_agent.constants import DEFAULT_OUTPUT, DEFAULT_PROVIDER, VERSION


class TestEnsureConfig:
    def test_creates_default_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        result = ensure_config(config_path)
        assert result == config_path
        assert config_path.exists()

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        original = {"version": "9.9.9", "default_provider": "test"}
        config_path.write_text(json.dumps(original))
        ensure_config(config_path)
        data = json.loads(config_path.read_text())
        assert data["version"] == "9.9.9"

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        config_path = tmp_path / "sub" / "dir" / "config.json"
        ensure_config(config_path)
        assert config_path.exists()


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        ensure_config(config_path)
        config = load_config(config_path)
        assert config["version"] == VERSION
        assert config["default_provider"] == DEFAULT_PROVIDER
        assert config["output"] == DEFAULT_OUTPUT

    def test_creates_default_if_missing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        config = load_config(config_path)
        assert config_path.exists()
        assert config["version"] == VERSION

    def test_merges_with_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        partial = {"default_provider": "openai"}
        config_path.write_text(json.dumps(partial))
        config = load_config(config_path)
        assert config["default_provider"] == "openai"
        assert config["output"] == DEFAULT_OUTPUT

    def test_raises_on_invalid_json(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text("not valid json {{{")
        with pytest.raises(RuntimeError, match="Failed to load config"):
            load_config(config_path)


class TestSaveConfig:
    def test_saves_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        data = {"version": "0.1.0", "custom": True}
        save_config(data, config_path)
        loaded = json.loads(config_path.read_text())
        assert loaded == data

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        config_path = tmp_path / "a" / "b" / "config.json"
        save_config({"key": "value"}, config_path)
        assert config_path.exists()


class TestGetProviderConfig:
    def test_returns_provider_config(self) -> None:
        config = load_config()
        provider = get_provider_config(config, "claude")
        assert provider is not None
        assert "api_key_env" in provider

    def test_returns_none_for_missing(self) -> None:
        config = load_config()
        provider = get_provider_config(config, "nonexistent")
        assert provider is None
