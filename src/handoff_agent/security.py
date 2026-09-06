"""Security Filter — defends against sensitive file exposure.

Defense in depth layers:
  1. Path / filename exclusion (secrets, keys, env files)
  2. Directory exclusion (.git, node_modules, venv, etc.)
  3. Extension / type exclusion (binary, images, archives)
  4. Content heuristic (last resort — only for files not caught above)
  5. Path traversal protection (no escapes outside project root)

IMPORTANT: Files already identified as sensitive by path/name/type are NEVER
opened for content inspection.  Content heuristic is only applied to files that
passed the first three layers without triggering an exclusion.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layer 1 — Sensitive filenames / basename patterns
# ---------------------------------------------------------------------------

SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.staging",
        ".env.test",
        ".env.backup",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        "id_ecdsa",
        "credentials",
        "credentials.json",
        "secrets",
        "secret.json",
        "secret.yaml",
        "secret.yml",
        "secret.toml",
        "token.json",
        "token.yaml",
        "token.yml",
        ".netrc",
        ".htpasswd",
        "keystore.jks",
        "keystore.properties",
    }
)

# Glob-style patterns for filenames (matched with fnmatch, case-insensitive).
SENSITIVE_FILENAME_GLOBS: tuple[str, ...] = (
    ".env.*",
    ".env-*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.cert",
    "*.crt",
    "secret.*",
    "token.*",
    "password*",
    "credential*",
)

# ---------------------------------------------------------------------------
# Layer 2 — Excluded directories
# ---------------------------------------------------------------------------

EXCLUDED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        "*.egg-info",
        ".cache",
        ".parcel-cache",
        ".next",
        ".nuxt",
        "coverage",
        ".nyc_output",
    }
)

# ---------------------------------------------------------------------------
# Layer 3 — Excluded extensions (binary / non-text artifacts)
# ---------------------------------------------------------------------------

EXCLUDED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".svg",
        ".webp",
        ".mp4",
        ".mp3",
        ".wav",
        ".avi",
        ".mov",
        ".mkv",
        ".flac",
        ".ogg",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".war",
        ".jar",
        ".class",
        ".o",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".pyc",
        ".pyo",
        ".whl",
        ".apk",
        ".ipa",
        ".dmg",
        ".iso",
        ".img",
        ".DS_Store",
        "Thumbs.db",
    }
)

# ---------------------------------------------------------------------------
# Layer 4 — Content heuristic patterns
# ---------------------------------------------------------------------------

# Each pattern is compiled once.  They scan the first portion of a file to
# detect likely secrets without revealing the actual matched text.
_CONTENT_HEURISTIC_MAX_SCAN = 8192  # bytes — never read more than this

_CONTENT_PATTERNS: list[re.Pattern[str]] = [
    # api_key / apikey — handles Python, JS, JSON, YAML, env var formats
    re.compile(r"""(?i)(?:api[_\-]?key|apikey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    # secret_key / secretkey
    re.compile(r"""(?i)(?:secret[_\-]?key|secretkey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    # access_token / accesstoken
    re.compile(r"""(?i)(?:access[_\-]?token|accesstoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-\.]{16,}"""),
    # password / passwd / pwd
    re.compile(r"""(?i)(?:password|passwd|pwd)\s*['"]?\s*[:=]\s*['"]?['"]?[^\s'"'\n]{8,}"""),
    # client_secret / clientsecret
    re.compile(r"""(?i)(?:client[_\-]?secret|clientsecret)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    # auth_token / authtoken
    re.compile(r"""(?i)(?:auth[_\-]?token|authtoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    # plain token assignment — catches token = "..." and "token": "..."
    re.compile(r"""(?i)\btoken\b\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-\.]{16,}"""),
    # private_key assignment
    re.compile(r"""(?i)(?:private[_\-]?key)\s*['"]?\s*[:=]"""),
    # PEM private key headers
    re.compile(r"""-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"""),
    # Common secret value prefixes (catch tokens by their value prefix)
    re.compile(r"""['"](?:ghp_[A-Za-z0-9]{36,}|sk-[A-Za-z0-9]{20,}|xox[bpsar]-[A-Za-z0-9\-]{10,})"""),
]

# ---------------------------------------------------------------------------
# Safe extensions — files that are generally safe for AI context
# ---------------------------------------------------------------------------

SAFE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".php",
        ".css",
        ".scss",
        ".less",
        ".html",
        ".htm",
        ".md",
        ".markdown",
        ".rst",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".xml",
        ".csv",
        ".txt",
        ".cfg",
        ".ini",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".bat",
        ".cmd",
        ".ps1",
        ".sql",
        ".graphql",
        ".gql",
        ".proto",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".rb",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".makefile",
        ".dockerfile",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".prettierrc",
        ".eslintrc",
        ".env.example",
    }
)

# ---------------------------------------------------------------------------
# Binary magic bytes (first bytes of file to detect binary content)
# ---------------------------------------------------------------------------

_BINARY_MAGIC_BYTES: list[bytes] = [
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"%PDF",
    b"PK\x03\x04",
    b"\x1f\x8b",
    b"\xfd7zXZ",
    b"Rar!\x1a\x07",
    b"\x00\x00\x01\x00",
    b"\x00\x00\x00\x1c\x65\x78\x69\x66",
    b"\x42\x5a\x68",
    b"\x7fELF",
    b"MZ",
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class FilterResult:
    """Result of filtering a single path."""

    path: str
    excluded: bool
    reason: str = ""

    def __str__(self) -> str:
        if self.excluded:
            return f"SKIP: {self.path} — {self.reason}" if self.reason else f"SKIP: {self.path}"
        return f"INCLUDE: {self.path}"


# ---------------------------------------------------------------------------
# SecurityFilter
# ---------------------------------------------------------------------------

@dataclass
class SecurityFilter:
    """Multi-layer security filter for file paths.

    Usage::

        sf = SecurityFilter(project_root="/path/to/repo")
        result = sf.is_excluded("config/database.yml")
        filtered = sf.filter_paths(["src/main.py", ".env", "id_rsa"])
    """

    project_root: Path
    max_file_size: int = 1 * 1024 * 1024  # 1 MB default

    # Allow callers to extend exclusion lists.
    extra_excluded_names: frozenset[str] = field(default_factory=frozenset)
    extra_excluded_dirs: frozenset[str] = field(default_factory=frozenset)
    extra_excluded_extensions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self._root = self.project_root.resolve()
        if not self._root.is_dir():
            raise ValueError(f"Project root does not exist or is not a directory: {self._root}")

    # ------------------------------------------------------------------
    # Path normalization / traversal protection
    # ------------------------------------------------------------------

    def _resolve_within_root(self, path: str | Path) -> Path | None:
        """Resolve *path* and confirm it lives inside project_root.

        Returns the resolved Path on success, None if the path escapes the
        root or is a broken symlink.
        """
        raw = Path(path)
        if raw.is_absolute():
            resolved = raw.resolve()
        else:
            resolved = (self._root / raw).resolve()

        try:
            resolved.relative_to(self._root)
        except ValueError:
            return None
        return resolved

    # ------------------------------------------------------------------
    # Layer 1 — Sensitive filename check
    # ------------------------------------------------------------------

    def _is_sensitive_filename(self, basename: str) -> str | None:
        """Return a reason string if *basename* matches a sensitive pattern."""
        lower = basename.lower()

        if lower in SENSITIVE_FILENAMES:
            return "sensitive filename"

        for pattern in SENSITIVE_FILENAME_GLOBS:
            if fnmatch.fnmatch(lower, pattern):
                return "sensitive filename pattern"
        return None

    # ------------------------------------------------------------------
    # Layer 2 — Directory exclusion
    # ------------------------------------------------------------------

    def _is_excluded_dir(self, parts: tuple[str, ...]) -> str | None:
        """Check if any component of the path is an excluded directory."""
        all_dirs = EXCLUDED_DIRECTORIES | self.extra_excluded_dirs
        for part in parts:
            lower = part.lower()
            for excluded in all_dirs:
                if fnmatch.fnmatch(lower, excluded.lower()):
                    return f"excluded directory ({part})"
        return None

    # ------------------------------------------------------------------
    # Layer 3 — Extension exclusion
    # ------------------------------------------------------------------

    def _is_excluded_extension(self, basename: str) -> str | None:
        """Check if the file extension indicates a binary / non-text type."""
        lower = basename.lower()
        for ext in EXCLUDED_EXTENSIONS | self.extra_excluded_extensions:
            if lower.endswith(ext):
                return f"excluded extension ({ext})"
        return None

    # ------------------------------------------------------------------
    # Layer 4 — Content heuristic (lazy, only when not excluded above)
    # ------------------------------------------------------------------

    def _check_content_heuristic(self, resolved: Path) -> str | None:
        """Scan the first bytes of a file for secret indicators.

        This is only called for files that survived layers 1-3.  The file
        is opened in binary mode and limited to _CONTENT_HEURISTIC_MAX_SCAN
        bytes to avoid loading large files into memory.
        """
        try:
            with open(resolved, "rb") as fh:
                chunk = fh.read(_CONTENT_HEURISTIC_MAX_SCAN)
        except (OSError, PermissionError):
            return None

        if not chunk:
            return None

        # Quick binary check — if the file has null bytes in the first 512
        # bytes, it is likely binary and should be excluded anyway.
        if b"\x00" in chunk[:512]:
            return "binary content detected"

        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            return None

        for pattern in _CONTENT_PATTERNS:
            if pattern.search(text):
                return "sensitive content detected"
        return None

    # ------------------------------------------------------------------
    # Size check
    # ------------------------------------------------------------------

    def _is_too_large(self, resolved: Path) -> str | None:
        """Check if the file exceeds the configured size limit."""
        try:
            size = resolved.stat().st_size
        except OSError:
            return None
        if size > self.max_file_size:
            return f"file too large ({size} bytes > {self.max_file_size} limit)"
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_excluded(self, path: str | Path) -> FilterResult:
        """Determine whether *path* should be excluded from context.

        Applies all defense-in-depth layers in order.  Returns a
        FilterResult with the decision and a non-leaking reason string.
        """
        str_path = str(path)

        # --- Layer 5: Path traversal ---
        resolved = self._resolve_within_root(str_path)
        if resolved is None:
            return FilterResult(
                path=str_path,
                excluded=True,
                reason="path traversal — outside project root",
            )

        # Compute relative path from project root.
        try:
            rel = resolved.relative_to(self._root)
        except ValueError:
            return FilterResult(
                path=str_path,
                excluded=True,
                reason="path traversal — outside project root",
            )

        parts = rel.parts

        # --- Layer 2: Directory exclusion ---
        dir_reason = self._is_excluded_dir(parts)
        if dir_reason:
            return FilterResult(path=str_path, excluded=True, reason=dir_reason)

        basename = rel.name

        # --- Layer 1: Sensitive filename ---
        name_reason = self._is_sensitive_filename(basename)
        if name_reason:
            return FilterResult(path=str_path, excluded=True, reason=name_reason)

        # --- Layer 3: Extension exclusion ---
        ext_reason = self._is_excluded_extension(basename)
        if ext_reason:
            return FilterResult(path=str_path, excluded=True, reason=ext_reason)

        # --- Size guard before content heuristic ---
        size_reason = self._is_too_large(resolved)
        if size_reason:
            return FilterResult(path=str_path, excluded=True, reason=size_reason)

        # --- Layer 4: Content heuristic (only for files not caught above) ---
        content_reason = self._check_content_heuristic(resolved)
        if content_reason:
            return FilterResult(path=str_path, excluded=True, reason=content_reason)

        return FilterResult(path=str_path, excluded=False)

    def should_include(self, path: str | Path) -> bool:
        """Convenience inverse of :meth:`is_excluded`."""
        return not self.is_excluded(path).excluded

    def filter_paths(self, paths: list[str | Path]) -> list[FilterResult]:
        """Filter a batch of paths.  Returns one FilterResult per input."""
        return [self.is_excluded(p) for p in paths]
