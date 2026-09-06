"""Handoff Agent constants and paths."""

import os
from pathlib import Path

VERSION = "0.1.0"

APP_NAME = "handoff"

HANDOFF_HOME = Path(os.environ.get("HANDOFF_HOME", Path.home() / ".handoff"))

CONFIG_FILE = HANDOFF_HOME / "config.json"
CACHE_DIR = HANDOFF_HOME / "cache"
DEFAULT_OUTPUT = "docs/HANDOFF.md"
DEFAULT_PROVIDER = "claude"
