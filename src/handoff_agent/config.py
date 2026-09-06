"""Configuration management for Handoff Agent."""

import json
from pathlib import Path
from typing import Any

from handoff_agent.constants import CONFIG_FILE, DEFAULT_OUTPUT, DEFAULT_PROVIDER, VERSION

DEFAULT_CONFIG: dict[str, Any] = {
    "version": VERSION,
    "default_provider": DEFAULT_PROVIDER,
    "output": DEFAULT_OUTPUT,
    "auto_commit": False,
    "auto_push": False,
    "providers": {
        "claude": {"api_key_env": "ANTHROPIC_API_KEY", "model": ""},
        "openai": {"api_key_env": "OPENAI_API_KEY", "model": ""},
        "qwen": {"api_key_env": "DASHSCOPE_API_KEY", "model": ""},
        "deepseek": {"api_key_env": "DEEPSEEK_API_KEY", "model": ""},
    },
}


def ensure_config(config_path: Path | None = None) -> Path:
    """Create default config file if it does not exist. Returns config path."""
    path = config_path or CONFIG_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    return path


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load config from file, creating default if needed."""
    path = ensure_config(config_path)
    try:
        data = json.loads(path.read_text())
        merged = {**DEFAULT_CONFIG, **data}
        return merged
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Failed to load config from {path}: {exc}") from exc


def save_config(config: dict[str, Any], config_path: Path | None = None) -> Path:
    """Save config to file."""
    path = config_path or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def get_provider_config(config: dict[str, Any], provider_name: str) -> dict[str, Any] | None:
    """Get provider-specific config. Returns None if not found."""
    providers = config.get("providers", {})
    return providers.get(provider_name)
