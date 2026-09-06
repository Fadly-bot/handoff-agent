"""Tests for constants."""

from handoff_agent.constants import (
    APP_NAME,
    CACHE_DIR,
    CONFIG_FILE,
    DEFAULT_OUTPUT,
    DEFAULT_PROVIDER,
    HANDOFF_HOME,
    VERSION,
)


class TestVersion:
    def test_version_is_string(self) -> None:
        assert isinstance(VERSION, str)

    def test_version_format(self) -> None:
        parts = VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


class TestPaths:
    def test_handoff_home_is_path(self) -> None:
        assert str(HANDOFF_HOME).endswith(".handoff")

    def test_config_file_inside_home(self) -> None:
        assert str(CONFIG_FILE).startswith(str(HANDOFF_HOME))

    def test_cache_dir_inside_home(self) -> None:
        assert str(CACHE_DIR).startswith(str(HANDOFF_HOME))

    def test_app_name(self) -> None:
        assert APP_NAME == "handoff"

    def test_default_output(self) -> None:
        assert DEFAULT_OUTPUT == "docs/HANDOFF.md"

    def test_default_provider(self) -> None:
        assert DEFAULT_PROVIDER == "claude"
