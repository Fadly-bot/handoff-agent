"""CLI adapter — integrates with the Handoff Agent command-line interface.

This adapter models an AI agent that interacts with Handoff through the
``handoff`` CLI (its tool executor). It invokes the CLI with a *fixed*,
pre-approved argument list — never a shell, never user-controlled commands.

Supported operations:
  - ``project_state()``  → runs ``handoff inspect --path <root>`` (read-only).
  - ``read_handoff()`` / ``write_checkpoint()`` / ``validate_checkpoint()`` /
    ``read_changelog()`` → explicitly unsupported with a safe error, because
    the current CLI does not expose those endpoints.

This adapter demonstrates CLI integration together with safe
unsupported-feature handling: it never silently falls back to another
adapter and it never fabricates data.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from handoff_agent.adapters.base import (
    AdapterConfigError,
    AdapterError,
    AdapterUnsupportedError,
    AdapterWriteResult,
    BaseAdapter,
)

_UNSUPPORTED_NOTE = (
    "the handoff CLI does not expose an endpoint for this operation; "
    "use the file or API adapter instead"
)


class CliAdapter(BaseAdapter):
    """Adapter that drives the ``handoff`` CLI through a fixed argv."""

    adapter_name = "cli"
    adapter_version = "0.4.0"

    #: Subcommands we are allowed to invoke (whitelist, no arbitrary commands).
    _ALLOWED_COMMANDS: frozenset[str] = frozenset({"inspect"})

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        command: list[str] | None = None,
        project_root: str | Path | None = None,
        timeout: float = 30.0,
        read_only: bool = False,
        contract=None,
    ) -> None:
        if command is not None and not command:
            raise AdapterConfigError("Adapter command must not be empty.")
        self._argv = command or [sys.executable, "-m", "handoff_agent"]
        self._project_root = Path(project_root or Path.cwd()).resolve()
        self._timeout = timeout
        super().__init__(
            config,
            agent=None,
            read_only=read_only,
            contract=contract,
        )

    # -- operations --------------------------------------------------------

    def _do_project_state(self) -> dict[str, Any]:
        result = self._run(["inspect", "--path", str(self._project_root)])
        return {
            "command": "handoff inspect --path <root>",
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "safe": result["returncode"] == 0,
        }

    def _do_read_handoff(self) -> str | None:
        raise AdapterUnsupportedError(
            f"CLI adapter: {_UNSUPPORTED_NOTE} (read_handoff)."
        )

    def _do_write_checkpoint(self, content: str) -> AdapterWriteResult:
        raise AdapterUnsupportedError(
            f"CLI adapter: {_UNSUPPORTED_NOTE} (write_checkpoint)."
        )

    def _do_validate_checkpoint(self) -> dict[str, Any]:
        raise AdapterUnsupportedError(
            f"CLI adapter: {_UNSUPPORTED_NOTE} (validate_checkpoint)."
        )

    def _do_read_changelog(self) -> str | None:
        raise AdapterUnsupportedError(
            f"CLI adapter: {_UNSUPPORTED_NOTE} (read_changelog)."
        )

    # -- runner --------------------------------------------------------------

    def _run(self, args: list[str]) -> dict[str, str | int]:
        """Run a whitelisted CLI invocation (fixed argv, no shell)."""
        if not args or args[0] not in self._ALLOWED_COMMANDS:
            raise AdapterConfigError(
                f"Command not in whitelist: {args[0] if args else '(empty)'}"
            )
        try:
            proc = subprocess.run(
                [*self._argv, *args],
                capture_output=True,
                text=True,
                cwd=str(self._project_root),
                env=dict(os.environ),
                timeout=self._timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdapterError(f"CLI invocation failed: {exc}") from exc
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }