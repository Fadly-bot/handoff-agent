"""Context Builder — assembles a structured FullContext for AI providers.

Collects project metadata, repository state, and safe file contents,
routing every candidate path through SecurityFilter before inclusion.

This module does NOT generate prompts or call AI providers.
It produces a structured FullContext data object only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from handoff_agent.detector import Project, detect_project
from handoff_agent.git_helper import GitRunner
from handoff_agent.git_inspector import RepositoryInfo, inspect_repository
from handoff_agent.security import FilterResult, SecurityFilter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default limits
# ---------------------------------------------------------------------------

DEFAULT_MAX_FILE_SIZE = 256 * 1024       # 256 KB per file
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024  # 4 MB total context
DEFAULT_MAX_FILE_COUNT = 200             # max included files


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectInfo:
    """Project-level metadata."""

    root: str
    name: str
    project_type: str


@dataclass(frozen=True)
class GitInfo:
    """Repository state snapshot."""

    branch: str | None
    head: str | None
    clean: bool
    status: str
    modified_files: tuple[str, ...]
    untracked_files: tuple[str, ...]
    staged_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    recent_commits: tuple[dict[str, str], ...]
    diff_stat: dict[str, int]
    remotes: tuple[str, ...]


@dataclass(frozen=True)
class FileEntry:
    """A single included file with its content."""

    path: str
    content: str
    size: int
    metadata: str = ""


@dataclass(frozen=True)
class SecurityInfo:
    """Summary of security filtering."""

    excluded_count: int
    exclusion_summary: dict[str, int]
    included_count: int
    omitted_count: int
    omission_reasons: dict[str, int]


@dataclass(frozen=True)
class FullContext:
    """Complete structured context ready for AI provider consumption.

    No secrets, no API keys, no raw binary data.
    """

    project: ProjectInfo
    git: GitInfo
    files: tuple[FileEntry, ...]
    security: SecurityInfo

    def to_dict(self) -> dict[str, object]:
        """Serialization-friendly dict (no secrets, no sensitive paths).

        Raw untracked-file names can themselves be sensitive metadata
        (``.env``, ``id_rsa``), so the serialized form lists only paths that
        survived the SecurityFilter (i.e. appear in ``self.files``).
        """
        safe_untracked = tuple(
            f.path for f in self.files if f.path in set(self.git.untracked_files)
        )
        return {
            "project": {
                "root": self.project.root,
                "name": self.project.name,
                "project_type": self.project.project_type,
            },
            "git": {
                "branch": self.git.branch,
                "head": self.git.head,
                "clean": self.git.clean,
                "status": self.git.status,
                "modified_files": self.git.modified_files,
                "untracked_files": safe_untracked,
                "staged_files": self.git.staged_files,
                "deleted_files": self.git.deleted_files,
                "recent_commits": self.git.recent_commits,
                "diff_stat": self.git.diff_stat,
                "remotes": self.git.remotes,
            },
            "files": [
                {"path": f.path, "size": f.size, "metadata": f.metadata}
                for f in self.files
            ],
            "security": {
                "excluded_count": self.security.excluded_count,
                "exclusion_summary": self.security.exclusion_summary,
                "included_count": self.security.included_count,
                "omitted_count": self.security.omitted_count,
                "omission_reasons": self.security.omission_reasons,
            },
        }


# ---------------------------------------------------------------------------
# Omission reason labels (safe — no secrets)
# ---------------------------------------------------------------------------

OMITTED_SIZE_LIMIT = "omitted: size_limit"
OMITTED_TOTAL_LIMIT = "omitted: total_context_limit"
OMITTED_FILE_COUNT_LIMIT = "omitted: file_count_limit"
OMITTED_READ_ERROR = "omitted: read_error"
OMITTED_ENCODING_ERROR = "omitted: encoding_error"


# ---------------------------------------------------------------------------
# Git candidate collection
# ---------------------------------------------------------------------------

def _get_tracked_files(runner: GitRunner) -> list[str]:
    """Return list of tracked file paths via ``git ls-files``."""
    result = runner.run(["ls-files"])
    if not result.ok:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _get_untracked_candidate_files(runner: GitRunner) -> list[str]:
    """Return list of untracked file paths via ``git ls-files --others``.

    Only files not ignored by .gitignore are returned.
    """
    result = runner.run(["ls-files", "--others", "--exclude-standard"])
    if not result.ok:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Safe file reading
# ---------------------------------------------------------------------------

def _read_file_safe(path: Path, max_size: int) -> tuple[str | None, str]:
    """Read a file as text with safe fallback.

    Returns (content, "") on success or (None, reason) on failure.
    Never raises exceptions for encoding/permission issues.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None, OMITTED_READ_ERROR

    if size > max_size:
        return None, OMITTED_SIZE_LIMIT

    try:
        raw = path.read_bytes()
    except (OSError, PermissionError):
        return None, OMITTED_READ_ERROR

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:
            return None, OMITTED_ENCODING_ERROR

    return text, ""


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

@dataclass
class ContextBuilder:
    """Assembles a FullContext from project, git, and file data.

    Usage::

        builder = ContextBuilder(project_root="/path/to/repo")
        ctx = builder.build()
    """

    project_root: str | Path
    max_file_size: int = DEFAULT_MAX_FILE_SIZE
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_file_count: int = DEFAULT_MAX_FILE_COUNT
    git_binary: str | None = None
    security_filter: SecurityFilter | None = None

    def __post_init__(self) -> None:
        self._root = Path(self.project_root).resolve()

    def _resolve_file(self, path_str: str) -> Path | None:
        """Resolve *path_str* relative to the project root.

        Returns the resolved Path if it exists, None otherwise.
        """
        try:
            p = Path(path_str)
            if p.is_absolute():
                resolved = p.resolve()
            else:
                resolved = (self._root / p).resolve()
            if resolved.is_file():
                return resolved
        except (OSError, ValueError):
            return None
        return None

    def _within_root(self, resolved: Path) -> bool:
        """Return True if *resolved* is inside the project root."""
        try:
            resolved.relative_to(self._root)
            return True
        except ValueError:
            return False

    def build(self) -> FullContext:
        """Build the complete structured context.

        Flow::

            detect_project → inspect_repository → candidate paths
            → SecurityFilter → safe file read → FullContext
        """
        # 1. Project detection
        project = detect_project(self._root)

        # 2. Repository inspection
        repo_info = inspect_repository(self._root, self.git_binary)

        # 3. Security filter
        sf = self.security_filter or SecurityFilter(project_root=self._root)

        # 4. Collect candidate paths from git
        runner = GitRunner(
            git_binary=self.git_binary or "git",
            cwd=str(self._root),
        )
        tracked = _get_tracked_files(runner)
        untracked = _get_untracked_candidate_files(runner)
        candidates = _merge_deterministic(tracked, untracked)

        # 5. Filter through SecurityFilter
        filter_results = sf.filter_paths(candidates)

        # 6. Collect excluded / omitted / included
        excluded_reasons: dict[str, int] = {}
        included_paths: list[tuple[str, str]] = []  # (path, filter_reason)
        omitted_reasons: dict[str, int] = {}
        included_count = 0
        omitted_count = 0
        excluded_count = 0

        for fr in filter_results:
            if fr.excluded:
                excluded_count += 1
                key = fr.reason or "unknown"
                excluded_reasons[key] = excluded_reasons.get(key, 0) + 1
                continue

            # Read the safe file
            resolved = self._resolve_file(fr.path)
            if resolved is None or not self._within_root(resolved):
                omitted_count += 1
                _inc(omitted_reasons, OMITTED_READ_ERROR)
                continue

            content, read_reason = _read_file_safe(resolved, self.max_file_size)
            if content is None:
                omitted_count += 1
                _inc(omitted_reasons, read_reason)
                continue

            included_paths.append((fr.path, content))

        # 7. Apply context limits and build FileEntry list
        files, file_omissions = _apply_limits(
            included_paths,
            self.max_total_bytes,
            self.max_file_count,
        )
        for reason, count in file_omissions.items():
            omitted_count += count
            _inc(omitted_reasons, reason)

        # 8. Build structured output
        project_info = ProjectInfo(
            root=str(self._root),
            name=project.name,
            project_type=project.project_type,
        )

        git_info = _build_git_info(repo_info)

        security_info = SecurityInfo(
            excluded_count=excluded_count,
            exclusion_summary=excluded_reasons,
            included_count=len(files),
            omitted_count=omitted_count,
            omission_reasons=omitted_reasons,
        )

        return FullContext(
            project=project_info,
            git=git_info,
            files=tuple(files),
            security=security_info,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _inc(d: dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1


def _merge_deterministic(tracked: list[str], untracked: list[str]) -> list[str]:
    """Merge tracked + untracked into a deterministic sorted list."""
    seen: set[str] = set()
    result: list[str] = []
    for p in sorted(tracked):
        if p not in seen:
            seen.add(p)
            result.append(p)
    for p in sorted(untracked):
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _build_git_info(repo: RepositoryInfo) -> GitInfo:
    """Convert RepositoryInfo into the frozen GitInfo dataclass."""
    def _status_label(r: RepositoryInfo) -> str:
        if r.clean:
            return "clean"
        parts: list[str] = []
        if r.modified_files:
            parts.append(f"{len(r.modified_files)} modified")
        if r.staged_files:
            parts.append(f"{len(r.staged_files)} staged")
        if r.untracked_files:
            parts.append(f"{len(r.untracked_files)} untracked")
        if r.deleted_files:
            parts.append(f"{len(r.deleted_files)} deleted")
        return ", ".join(parts) if parts else "clean"

    commits = tuple(
        {
            "short_hash": c.short_hash,
            "full_hash": c.full_hash,
            "subject": c.subject,
            "author": c.author,
            "date": c.date,
        }
        for c in repo.recent_commits
    )

    return GitInfo(
        branch=repo.current_branch,
        head=repo.head_commit,
        clean=repo.clean,
        status=_status_label(repo),
        modified_files=tuple(repo.modified_files),
        untracked_files=tuple(repo.untracked_files),
        staged_files=tuple(repo.staged_files),
        deleted_files=tuple(repo.deleted_files),
        recent_commits=commits,
        diff_stat={
            "files_changed": repo.diff_stat.files_changed,
            "insertions": repo.diff_stat.insertions,
            "deletions": repo.diff_stat.deletions,
        },
        remotes=tuple(repo.remotes),
    )


def _apply_limits(
    candidates: list[tuple[str, str]],
    max_total_bytes: int,
    max_file_count: int,
) -> tuple[list[FileEntry], dict[str, int]]:
    """Apply file count and total byte limits.

    Returns (files, omission_reasons_counts).
    """
    files: list[FileEntry] = []
    omission_reasons: dict[str, int] = {}
    total_bytes = 0

    for path_str, content in candidates:
        if len(files) >= max_file_count:
            _inc(omission_reasons, OMITTED_FILE_COUNT_LIMIT)
            continue

        size = len(content.encode("utf-8"))
        if total_bytes + size > max_total_bytes:
            _inc(omission_reasons, OMITTED_TOTAL_LIMIT)
            continue

        files.append(FileEntry(
            path=path_str,
            content=content,
            size=size,
        ))
        total_bytes += size

    return files, omission_reasons
