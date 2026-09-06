"""Safe persistence of the generated handoff document (checkpoint lifecycle).

Phase 7 — writes the generated handoff to ``docs/HANDOFF.md`` inside the
detected project root.

Phase 8 — checkpoint lifecycle & history handling:

  - ``docs/HANDOFF.md`` always holds the CURRENT checkpoint only. It is
    overwritten on each successful generation, never appended to.
  - When a new checkpoint replaces an existing (different) one, the previous
    checkpoint is archived into ``docs/CHANGELOG.md`` before the new content
    is persisted. CHANGELOG.md is the ONLY file that is appended to; it keeps
    the history of replaced checkpoints, oldest first.

Security guarantees (Phases 3–7 preserved):
  - Only this exact project-relative path (``docs/HANDOFF.md`` by default)
    may be written.
  - The final path is resolved and verified to be contained inside the
    detected project root. Symlink escapes are rejected.
  - The generated AI response is treated purely as CONTENT, never as a
    filesystem path or command.
  - Generated content is scanned for secret-like patterns and rejected with
    a safe error if any are found (never persisted, never printed).
  - History entries are scanned for secret-like patterns too; secret-tainted
    history is never archived.
  - All writes are atomic (write to a temp file, then rename) to avoid a
    partially-written checkpoint or changelog.
  - If archiving the previous checkpoint fails, the current checkpoint is
    left untouched (no partial update).
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class HandoffPersistenceError(Exception):
    """Base error for handoff persistence failures."""


class HandoffPathError(HandoffPersistenceError):
    """Raised when a handoff target path is unsafe / escapes the project."""


class HandoffWriteError(HandoffPersistenceError):
    """Raised when the handoff document cannot be written."""


class HandoffContentError(HandoffPersistenceError):
    """Raised when generated content looks like it contains secrets."""


class ChangelogError(HandoffPersistenceError):
    """Raised when a checkpoint history entry cannot be archived."""


DEFAULT_HANDOFF_REL = "docs/HANDOFF.md"
DEFAULT_CHANGELOG_REL = "docs/CHANGELOG.md"
CHANGELOG_TITLE = "# Handoff Checkpoint History"

# Secret-like patterns reused to scan AI-generated content before persistence.
# The generated content is not trusted input; reject rather than persist any
# content that looks like a hardcoded secret. Patterns mirror the general
# secret shape (key=value assignments / private-key blocks), not specific
# values, so no secret is echoed in any error message.
_CONTENT_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"""(?i)(?:api[_\-]?key|apikey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:secret[_\-]?key|secretkey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:access[_\-]?token|accesstoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-\.]{16,}"""),
    re.compile(r"""(?i)(?:password|passwd|pwd)\s*['"]?\s*[:=]\s*['"]?['"]?[^\s'"'\n]{8,}"""),
    re.compile(r"""(?i)(?:client[_\-]?secret|clientsecret)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:auth[_\-]?token|authtoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:private[_\-]?key)\s*['"]?\s*[:=]"""),
    re.compile(r"""-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"""),
]


def _contains_secret_like_content(content: str) -> bool:
    """Return True if *content* looks like it embeds secret values."""
    for pattern in _CONTENT_SECRET_PATTERNS:
        if pattern.search(content):
            return True
    return False


@dataclass(frozen=True)
class WriteResult:
    """Result of persisting the handoff document."""

    path: Path
    rel_path: str
    created: bool
    modified: bool
    unchanged: bool


def _resolve_within_root(root: Path, rel: str) -> Path:
    """Resolve ``*rel*`` inside ``*root*`` and verify containment.

    Raises ``HandoffPathError`` if the path escapes the root (via ``..``,
    absolute path, or a symlink that points outside the root).
    """
    root_resolved = root.resolve()
    rel_path = Path(rel)

    if rel_path.is_absolute():
        raise HandoffPathError(
            f"Handoff target must be a project-relative path, got absolute: {rel}"
        )

    # Reject traversal components outright.
    for part in rel_path.parts:
        if part == "..":
            raise HandoffPathError(
                "Handoff target path must not contain '..' traversal."
            )

    candidate = root_resolved / rel_path

    # Create parent directories if needed (before resolution so the resolved
    # path reflects the final target).
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HandoffPathError(
            f"Could not create handoff parent directory: {exc}"
        ) from exc

    # Resolve the final target, following any symlinks that exist.
    # If the target itself is a symlink pointing outside the root, this
    # resolves to an external location and is rejected.
    resolved = candidate.resolve()

    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise HandoffPathError(
            "Handoff target escapes the project root (via symlink or traversal)."
        ) from exc

    return resolved


class HandoffWriter:
    """Writes generated handoff content safely into a project root."""

    def __init__(self, project_root: str | Path, rel_path: str = DEFAULT_HANDOFF_REL) -> None:
        self.project_root = Path(project_root).resolve()
        self.rel_path = rel_path

    def write(self, content: str) -> WriteResult:
        """Persist ``*content*`` to the handoff file.

        Returns a ``WriteResult`` describing whether the file was created,
        modified, or left unchanged.
        """
        if _contains_secret_like_content(content):
            raise HandoffContentError(
                "Refusing to persist handoff: generated content looks like it "
                "contains secret values. No file was written."
            )

        target = _resolve_within_root(self.project_root, self.rel_path)

        existed = target.exists()
        old_content: str | None = None
        if existed:
            try:
                old_content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                old_content = None

        # Preserve unchanged content without rewriting (avoids touching mtime
        # / tripping up diff detection).
        if old_content is not None and old_content == content:
            return WriteResult(
                path=target,
                rel_path=self.rel_path,
                created=False,
                modified=False,
                unchanged=True,
            )

        try:
            self._atomic_write(target, content)
        except OSError as exc:
            raise HandoffWriteError(
                f"Failed to write handoff document: {exc}"
            ) from exc

        created = not existed
        return WriteResult(
            path=target,
            rel_path=self.rel_path,
            created=created,
            modified=(not created),
            unchanged=False,
        )

    def _atomic_write(self, target: Path, content: str) -> None:
        """Atomically replace ``*target*`` with ``*content*``."""
        _atomic_write_file(target, content)


def _atomic_write_file(target: Path, content: str) -> None:
    """Atomically replace ``*target*`` with ``*content*``.

    Writes to a temporary file in the same directory, then fsyncs and
    renames over the destination. This prevents a partial/corrupt file
    on crash. The parent directory must already exist.
    """
    directory = target.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=".handoff-",
        suffix=".tmp",
        dir=str(directory),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Checkpoint history (docs/CHANGELOG.md)
# ---------------------------------------------------------------------------

def _sanitize_meta(value: object) -> str:
    """Coerce a metadata value into a safe single-line string.

    Collapses all whitespace (including newlines and tabs) so a value can
    never inject an extra line into a changelog header.
    """
    if value is None:
        return ""
    return " ".join(str(value).split())


def _format_changelog_entry(content: str, metadata: dict | None = None) -> str:
    """Render a single checkpoint-history entry.

    Metadata values are sanitized before being included so no value can
    alter the changelog structure. ``*content*`` is treated purely as
    historical content (it already passed a secret scan before being
    archived).
    """
    meta = metadata or {}
    generated_at = _sanitize_meta(
        meta.get("generated_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    project = _sanitize_meta(meta.get("project")) or "(unknown)"
    branch = _sanitize_meta(meta.get("branch")) or "(none)"
    commit = _sanitize_meta(meta.get("commit")) or "(none)"
    return "\n".join(
        [
            f"## Checkpoint {generated_at}",
            "",
            f"- project: {project}",
            f"- branch: {branch}",
            f"- commit: {commit}",
            "",
            "Previous `docs/HANDOFF.md` content (replaced by this checkpoint):",
            "",
            content.rstrip("\n"),
            "",
            "---",
            "",
        ]
    )


class ChangelogWriter:
    """Appends checkpoint history to ``docs/CHANGELOG.md``.

    The changelog is the ONLY persistence target that is appended to.
    ``docs/HANDOFF.md`` itself is never appended to; it always holds the
    current checkpoint. Changelog writes are atomic and contained inside the
    project root (symlink escapes rejected).
    """

    def __init__(self, project_root: str | Path, rel_path: str = DEFAULT_CHANGELOG_REL) -> None:
        self.project_root = Path(project_root).resolve()
        self.rel_path = rel_path

    def append(self, content: str, metadata: dict | None = None) -> bool:
        """Append ``*content*`` as one new history entry.

        Returns True if an entry was written, False if ``*content*`` is empty.
        """
        if not content or not content.strip():
            return False
        if _contains_secret_like_content(content):
            raise ChangelogError(
                "Refusing to archive checkpoint: content looks like it contains "
                "secret values. No changelog was written."
            )

        target = _resolve_within_root(self.project_root, self.rel_path)

        existing = ""
        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ChangelogError(
                    f"Could not read existing changelog: {exc}"
                ) from exc

        entry = _format_changelog_entry(content, metadata)
        if existing.strip():
            body = existing.rstrip("\n") + "\n\n" + entry
        else:
            body = CHANGELOG_TITLE + "\n\n" + entry

        try:
            _atomic_write_file(target, body)
        except OSError as exc:
            raise ChangelogError(
                f"Failed to write changelog: {exc}"
            ) from exc
        return True


@dataclass(frozen=True)
class CheckpointResult:
    """Result of a checkpoint lifecycle write."""

    path: Path
    rel_path: str
    created: bool
    modified: bool
    unchanged: bool
    history_recorded: bool = False
    history_skipped: bool = False


class CheckpointManager:
    """Coordinates the checkpoint lifecycle.

    ``docs/HANDOFF.md`` always holds the CURRENT checkpoint. When a new
    checkpoint replaces an existing (different) one, the previous checkpoint
    is archived to ``docs/CHANGELOG.md`` FIRST; only then is the new content
    persisted. If archiving fails, the current checkpoint is left untouched
    (no partial update, no data loss).

    A secret-tainted previous checkpoint is never archived: ``HANDOFF.md`` is
    still replaced by the clean new content, and ``history_skipped`` is
    reported to the caller.
    """

    def __init__(
        self,
        project_root: str | Path,
        handoff_rel: str = DEFAULT_HANDOFF_REL,
        changelog_rel: str = DEFAULT_CHANGELOG_REL,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.handoff_rel = handoff_rel
        self.changelog_rel = changelog_rel

    def write_checkpoint(
        self,
        content: str,
        metadata: dict | None = None,
    ) -> CheckpointResult:
        """Replace the current checkpoint with ``*content*``.

        Raises ``HandoffContentError`` if the new content looks like it
        contains secrets, ``ChangelogError`` if the previous checkpoint cannot
        be archived, ``HandoffPathError`` on unsafe paths, and
        ``HandoffWriteError`` if the final write fails.
        """
        if _contains_secret_like_content(content):
            raise HandoffContentError(
                "Refusing to persist handoff: generated content looks like it "
                "contains secret values. No file was written."
            )

        target = _resolve_within_root(self.project_root, self.handoff_rel)

        existed = target.exists()
        old_content: str | None = None
        if existed:
            try:
                old_content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                old_content = None

        if old_content is not None and old_content == content:
            return CheckpointResult(
                path=target,
                rel_path=self.handoff_rel,
                created=False,
                modified=False,
                unchanged=True,
            )

        history_recorded = False
        history_skipped = False
        if existed and old_content is not None and old_content.strip():
            if _contains_secret_like_content(old_content):
                history_skipped = True
            else:
                ChangelogWriter(self.project_root, self.changelog_rel).append(
                    old_content, metadata
                )
                history_recorded = True

        try:
            _atomic_write_file(target, content)
        except OSError as exc:
            raise HandoffWriteError(
                f"Failed to write handoff document: {exc}"
            ) from exc

        created = not existed
        return CheckpointResult(
            path=target,
            rel_path=self.handoff_rel,
            created=created,
            modified=not created,
            unchanged=False,
            history_recorded=history_recorded,
            history_skipped=history_skipped,
        )
