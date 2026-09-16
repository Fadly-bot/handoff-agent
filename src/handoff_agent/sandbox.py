"""Phase 33 — Sandbox boundary: containment, isolation, process & env limits.

The sandbox is the security boundary every tool invocation crosses. It
enforces:

- **Working-directory containment**: every path resolved inside the sandbox
  must stay under the configured root (absolute paths, ``..`` traversal, and
  symlink escapes are rejected).
- **Filesystem isolation**: read/write/delete only operate on contained paths;
  content that looks secret-like is refused.
- **Process execution restriction**: commands run only through a strict
  allowlist, never a shell, with fixed argv, a timeout, and bounded output.
- **Network allowlist enforcement**: no network target is reachable unless its
  host matches an explicit allowlist pattern.
- **Environment and credential isolation**: only allowlisted environment
  variable names may be read; values never enter tool output.
- **Resource limits**: bounded reads/writes/output and timeouts.

Runtime uses only the Python standard library.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from handoff_agent.adapters.base import contains_secret_like


class SandboxError(Exception):
    """Base error for all sandbox failures."""


class SandboxPathError(SandboxError):
    """A path escapes the sandbox root or is otherwise invalid."""


class SandboxContentError(SandboxError):
    """Content (read or write) was refused."""


class SandboxSecretError(SandboxContentError):
    """Content looks like it embeds secret values."""


class SandboxCommandError(SandboxError):
    """A command was refused or failed to execute safely."""


class SandboxNetworkError(SandboxError):
    """A network target is not in the allowlist."""


class SandboxEnvironmentError(SandboxError):
    """An environment variable is not in the allowlist."""


class SandboxResourceError(SandboxError):
    """A resource limit was exceeded (size, time, retries)."""


@dataclass(frozen=True)
class SandboxLimits:
    """Resource limits applied across the sandbox."""

    max_read_bytes: int = 1_048_576  # 1 MiB per file read
    max_write_bytes: int = 1_048_576  # 1 MiB per file write
    max_output_bytes: int = 1_048_576  # 1 MiB per process output
    default_timeout_seconds: float = 30.0
    max_listing_entries: int = 4096

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_read_bytes": self.max_read_bytes,
            "max_write_bytes": self.max_write_bytes,
            "max_output_bytes": self.max_output_bytes,
            "default_timeout_seconds": self.default_timeout_seconds,
            "max_listing_entries": self.max_listing_entries,
        }


# Default environment allowlist: only these variable names may cross the
# sandbox. Names are uppercase; values never appear in tool output.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "USER",
    "TERM",
)


class Sandbox:
    """Filesystem + process + network + environment boundary.

    Args:
        root: The working directory to contain. Must exist.
        limits: Resource limits. Defaults to :class:`SandboxLimits`.
        env_allowlist: Env var names readable through ``env()``.
        command_allowlist: Commands allowed by ``run_command()``.
        network_allowlist: ``fnmatch`` host patterns allowed by ``check_network``.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        limits: SandboxLimits | None = None,
        env_allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
        command_allowlist: Sequence[str] = (),
        network_allowlist: Sequence[str] = (),
    ) -> None:
        root_path = Path(root)
        self._root = root_path.resolve()
        if not self._root.is_dir():
            raise SandboxPathError(f"Sandbox root is not a directory: {self._root}")
        self.limits = limits or SandboxLimits()
        self._env_allowlist: tuple[str, ...] = tuple(env_allowlist)
        self._command_allowlist: frozenset[str] = frozenset(command_allowlist)
        self._network_allowlist: list[str] = list(network_allowlist)

    # -- identity ----------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def status(self) -> dict[str, Any]:
        return {
            "root": str(self._root),
            "limits": self.limits.to_dict(),
            "env_allowlist": list(self._env_allowlist),
            "command_allowlist": sorted(self._command_allowlist),
            "network_allowlist": list(self._network_allowlist),
        }

    # -- containment -------------------------------------------------------

    def contain(self, rel_path: str | Path) -> Path:
        """Resolve ``rel_path`` inside the root; reject escapes and symlinks."""
        rel = Path(rel_path)
        if rel.is_absolute():
            raise SandboxPathError(
                f"Path must be relative to the sandbox root, got absolute: {rel_path}"
            )
        for part in rel.parts:
            if part == "..":
                raise SandboxPathError(
                    f"Path must not contain '..' traversal: {rel_path}"
                )
        candidate = self._root / rel
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise SandboxPathError(
                f"Resolved path escapes the sandbox root (symlink or traversal): {rel_path}"
            ) from exc
        return resolved

    # -- filesystem -------------------------------------------------------

    def read_file(self, rel_path: str | Path, *, max_bytes: int = 0) -> str:
        """Read a contained file, bounded by the read limit."""
        resolved = self.contain(rel_path)
        limit = max_bytes or self.limits.max_read_bytes
        try:
            size = resolved.stat().st_size
        except OSError:
            raise SandboxPathError(f"Cannot stat contained path: {rel_path}") from None
        if size > limit:
            raise SandboxResourceError(
                f"Read exceeds limit: {size} bytes > {limit}"
            )
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SandboxContentError(f"Cannot read contained path: {rel_path}") from exc
        if contains_secret_like(content):
            raise SandboxSecretError(f"Content of {rel_path!r} is secret-like — refused")
        return content

    def write_file(
        self,
        rel_path: str | Path,
        content: str,
        *,
        max_bytes: int = 0,
    ) -> Path:
        """Write ``content`` to a contained path (secret-safe)."""
        if len(content.encode("utf-8")) > (max_bytes or self.limits.max_write_bytes):
            raise SandboxResourceError("Write content exceeds the sandbox write limit")
        if contains_secret_like(content):
            raise SandboxSecretError("Wrote content is secret-like — refused")
        resolved = self.contain(rel_path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise SandboxContentError(f"Cannot write contained path: {rel_path}") from exc
        return resolved

    def delete_file(self, rel_path: str | Path) -> bool:
        """Delete a contained path; refuses to delete the root itself."""
        resolved = self.contain(rel_path)
        if resolved == self._root:
            raise SandboxPathError("Refusing to delete the sandbox root")
        if resolved.is_dir():
            raise SandboxPathError("Refusing to delete a directory via a file delete")
        try:
            resolved.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SandboxContentError(f"Cannot delete contained path: {rel_path}") from exc

    def list_recursive(self, rel_dir: str | Path = ".") -> tuple[str, ...]:
        """List contained file paths recursively, sorted and bounded."""
        resolved = self.contain(rel_dir)
        if not resolved.is_dir():
            raise SandboxPathError(f"Not a directory inside the sandbox: {rel_dir}")
        results: list[str] = []
        for path in resolved.rglob("*"):
            if path.is_file():
                rel = path.relative_to(self._root)
                results.append(str(rel))
        results.sort()
        if len(results) > self.limits.max_listing_entries:
            raise SandboxResourceError("Directory listing exceeds the sandbox limit")
        return tuple(results)

    def list_all(self) -> tuple[str, ...]:
        """Shorthand for listing the whole sandbox root."""
        return self.list_recursive(".")

    # -- secret detection --------------------------------------------------

    def check_secret(self, payload: str) -> bool:
        """Return True if *payload* looks secret-like (before it leaves the sandbox)."""
        return contains_secret_like(payload)

    # -- process execution -------------------------------------------------

    def run_command(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        timeout: float = 0.0,
        max_output_bytes: int = 0,
        env_allowlist: Sequence[str] | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        """Run an allowlisted command with fixed argv (never a shell).

        The command must be in the allowlist, every argument is passed
        verbatim (no options are interpreted by the sandbox), output is
        bounded and scanned for secrets, and the process runs inside the
        sandbox root.
        """
        if command not in self._command_allowlist:
            raise SandboxCommandError(
                f"Command {command!r} is not in the sandbox allowlist"
            )
        for arg in args:
            if not isinstance(arg, str) or arg == "":
                raise SandboxCommandError("Command arguments must be non-empty strings")
        cap = max_output_bytes or self.limits.max_output_bytes
        t_out = timeout or self.limits.default_timeout_seconds
        env = self._isolation_env(env_allowlist)
        try:
            proc = subprocess.run(
                [command, *args],
                capture_output=True,
                text=True,
                cwd=str(cwd or self._root),
                env=env,
                timeout=t_out,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SandboxCommandError(
                f"Command {command!r} exceeded the {t_out}s timeout"
            ) from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxCommandError(f"Command {command!r} failed to run: {exc}") from exc
        stdout = self._bounded(proc.stdout, cap, "stdout")
        stderr = self._bounded(proc.stderr, cap, "stderr")
        _scan = stdout + "\n" + stderr
        if contains_secret_like(_scan):
            raise SandboxSecretError("Command output contains secret-like content — refused")
        return {
            "command": command,
            "args": list(args),
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _bounded(self, text: str, cap: int, label: str) -> str:
        enc = text.encode("utf-8")
        if len(enc) > cap:
            raise SandboxResourceError(f"Command {label} exceeds {cap} bytes")
        return text

    def _isolation_env(self, env_allowlist: Sequence[str] | None) -> dict[str, str]:
        names = tuple(env_allowlist or self._env_allowlist)
        isolated: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is not None:
                isolated[name] = value
        return isolated

    # -- network -----------------------------------------------------------

    def allow_network(self, *patterns: str) -> None:
        for pattern in patterns:
            if pattern not in self._network_allowlist:
                self._network_allowlist.append(pattern)

    def check_network(self, host: str) -> str:
        """Raise if *host* is not in the network allowlist; else return it."""
        if not host:
            raise SandboxNetworkError("Empty network target refused")
        if "/" in host or ":" in host:
            raise SandboxNetworkError("Network target must be a bare hostname")
        if not any(fnmatch.fnmatch(host, pattern) for pattern in self._network_allowlist):
            raise SandboxNetworkError(f"Network target {host!r} is not allowlisted")
        return host

    def network_allowed(self, host: str) -> bool:
        try:
            self.check_network(host)
            return True
        except SandboxNetworkError:
            return False

    # -- environment -------------------------------------------------------

    def env(self, name: str) -> str:
        """Return an env value only if *name* is allowlisted."""
        if name not in self._env_allowlist:
            raise SandboxEnvironmentError(f"Environment variable {name!r} is not allowlisted")
        value = os.environ.get(name)
        if value is None:
            raise SandboxEnvironmentError(f"Environment variable {name!r} is not set")
        return value

    def env_names(self) -> tuple[str, ...]:
        """Names an actor may read from the environment (the allowlist)."""
        return self._env_allowlist

    def reads_env(self, name: str) -> bool:
        return name in self._env_allowlist