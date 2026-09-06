"""Controlled Git command execution.

Read-only Git inspection is permitted broadly (whitelist). Two narrowly
scoped mutation operations are also exposed for Phase 7 persistence:

  - ``stage_handoff``  → ``git add -- docs/HANDOFF.md``
  - ``commit_handoff`` → ``git commit -m <safe-message>``

These mutation methods are the ONLY git mutations allowed. They enforce
repository containment and an exact target path and reject all other
commands. Push / reset / clean / checkout / restore / switch / merge /
rebase / stash / fetch / pull and every other mutating or network command
remain forbidden.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# Read-only git subcommands that Handoff Agent is allowed to run.
ALLOWED_GIT_COMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "log",
        "diff",
        "branch",
        "remote",
        "rev-parse",
        "ls-files",
        "show",
        "config",
        "rev-list",
        "tag",
        "describe",
        "for-each-ref",
    }
)

# Mutating / forbidden commands that must never be executed.
FORBIDDEN_GIT_COMMANDS: frozenset[str] = frozenset(
    {
        "add",
        "commit",
        "push",
        "reset",
        "clean",
        "checkout",
        "restore",
        "switch",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "init",
        "clone",
        "rm",
        "mv",
        "stash",
        "filter-branch",
        "update-ref",
        "gc",
        "prune",
        "fetch",
        "pull",
        "apply",
        "am",
    }
)


class GitCommandError(Exception):
    """Raised when a git command fails or is forbidden."""


class GitForbiddenError(GitCommandError):
    """Raised when a forbidden (mutating) git command is attempted."""


@dataclass
class GitResult:
    """Result of a git command execution."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    cwd: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __post_init__(self) -> None:
        self._cmd_str = " ".join(self.command)

    @property
    def command_string(self) -> str:
        return self._cmd_str


@dataclass
class GitRunner:
    """Controlled runner for read-only git commands."""

    git_binary: str = field(default_factory=lambda: shutil.which("git") or "git")
    cwd: str | None = None
    audit_log: list[str] = field(default_factory=list)

    def _resolve_cmd(self, argv: Sequence[str]) -> list[str]:
        if not argv:
            raise GitCommandError("Empty git command.")
        if argv[0] == "git":
            argv = argv[1:]
        if not argv:
            raise GitCommandError("Empty git command.")
        sub = argv[0]
        if sub in FORBIDDEN_GIT_COMMANDS:
            raise GitForbiddenError(
                f"Forbidden git command is not allowed: 'git {sub}'"
            )
        if sub not in ALLOWED_GIT_COMMANDS:
            raise GitForbiddenError(
                f"Git subcommand '{sub}' is not whitelisted for read-only inspection."
            )
        return argv

    def run(self, argv: Sequence[str]) -> GitResult:
        """Run a read-only git command. Returns a GitResult."""
        resolved = self._resolve_cmd(argv)
        # Safe read-only overrides: keep inspection fast and side-effect free.
        cmd = [
            self.git_binary,
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            *resolved,
        ]
        entry = f"git {' '.join(resolved)}  # cwd={self.cwd}"
        self.audit_log.append(entry)
        logger.debug("git: %s", entry)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self.cwd,
            env={**os.environ},
        )
        return GitResult(
            command=cmd,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            cwd=self.cwd or os.getcwd(),
        )

    # ------------------------------------------------------------------
    # Phase 7 — narrowly scoped mutation operations
    # ------------------------------------------------------------------

    def _run_mutation(self, argv: list[str]) -> GitResult:
        """Run a mutation command with argument-list subprocess (no shell)."""
        cmd = [
            self.git_binary,
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            *argv,
        ]
        entry = f"git {' '.join(argv)}  # cwd={self.cwd}"
        self.audit_log.append(entry)
        logger.debug("git: %s", entry)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self.cwd,
            env={**os.environ},
        )
        return GitResult(
            command=cmd,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            cwd=self.cwd or os.getcwd(),
        )

    def _resolve_path_within(self, project_root: str | Path, rel: str) -> str:
        """Resolve ``*rel*`` inside ``*project_root*`` and return a path string.

        Enforces containment (no '..', no absolute path, no symlink escape).
        Returns the path in absolute form so git can operate on it regardless
        of the current working directory.
        """
        root = Path(project_root).resolve()
        rel_path = Path(rel)

        if rel_path.is_absolute():
            raise GitForbiddenError(
                "Handoff target must be project-relative; absolute paths rejected."
            )
        for part in rel_path.parts:
            if part == "..":
                raise GitForbiddenError(
                    "Handoff target path must not contain '..' traversal."
                )

        candidate = root / rel_path
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise GitForbiddenError(
                "Handoff target escapes the project root."
            ) from exc
        return str(resolved)

    def stage_handoff(
        self,
        project_root: str | Path,
        rel_path: str = "docs/HANDOFF.md",
    ) -> GitResult:
        """Stage ONLY the handoff file.

        Executes exactly: ``git add -- <abs path to docs/HANDOFF.md>``
        Uses ``--`` so the path is never interpreted as an option. The target
        path is fixed: only ``docs/HANDOFF.md`` is ever allowed, never an
        arbitrary path argument.
        """
        resolved_rel = self._enforce_handoff_path(rel_path)
        target = self._resolve_path_within(project_root, resolved_rel)
        result = self._run_mutation(["add", "--", target])
        return result

    def _enforce_handoff_path(self, rel_path: str) -> str:
        """Return the canonical handoff path or reject anything else.

        Phase 7 only allows the single exact target ``docs/HANDOFF.md``.
        Any other path is rejected to prevent arbitrary files from being
        staged or committed by the agent.
        """
        normalized = Path(rel_path).as_posix()
        if normalized != "docs/HANDOFF.md":
            raise GitForbiddenError(
                "Only 'docs/HANDOFF.md' may be staged by the handoff agent."
            )
        return normalized

    def commit_handoff(
        self,
        project_root: str | Path,
        message: str = "docs: update handoff checkpoint",
    ) -> GitResult:
        """Create a Git commit limited to the handoff file only.

        Executes ``git commit -m <message> -- <abs docs/HANDOFF.md>`` using an
        argument list (never a shell). The pathspec limits the commit to
        ``docs/HANDOFF.md`` so unrelated files — including files the user has
        already staged — are never included, never unstaged, and never
        overwritten. No amend / no push / no message file.
        """
        target = self._resolve_path_within(project_root, "docs/HANDOFF.md")
        result = self._run_mutation(["commit", "-m", message, "--", target])
        return result
