"""File-based adapter — direct, safe access to the checkpoint on disk.

This is the default generic adapter: an agent (or tool) operating on a local
Git repository reads and writes ``docs/HANDOFF.md`` directly.

Security guarantees:
  - All reads/writes stay inside the resolved project root (traversal and
    symlink escapes rejected).
  - Content is secret-scanned before reading (refuse to expose) and before
    writing (refuse to persist).
  - Checkpoint writes go through the existing ``CheckpointManager`` lifecycle
    (atomic writes, previous checkpoint archived to ``docs/CHANGELOG.md``).
  - Git operations are read-only (``inspect_repository``); no writes or
    restricted Git commands are ever issued.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from handoff_agent.adapters.base import (
    AdapterContentError,
    AdapterError,
    AdapterWriteResult,
    BaseAdapter,
    contains_secret_like,
    resolve_within_root,
)
from handoff_agent.capability import AgentIdentity
from handoff_agent.context_builder import ContextBuilder
from handoff_agent.detector import NotARepositoryError
from handoff_agent.persistence import (
    ChangelogError,
    CheckpointManager,
    HandoffContentError,
    HandoffPathError,
    HandoffWriteError,
)
from handoff_agent.protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    ProtocolValidationError,
    ProtocolVersionError,
    parse_handoff_document,
    validate_checkpoint,
    verify_identity,
)


class FileAdapter(BaseAdapter):
    """Read/write the checkpoint inside a project root on disk."""

    adapter_name = "file"
    adapter_version = "0.4.0"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        project_root: str | Path | None = None,
        agent: AgentIdentity | None = None,
        read_only: bool = False,
        contract=None,
    ) -> None:
        root = Path(project_root or Path.cwd()).resolve()
        self.project_root = root
        self.handoff_rel = "docs/HANDOFF.md"
        self.changelog_rel = "docs/CHANGELOG.md"
        super().__init__(
            config,
            agent=agent,
            read_only=read_only,
            contract=contract,
        )

    # -- read --------------------------------------------------------------

    def _do_read_handoff(self) -> str | None:
        path = self._resolve(self.handoff_rel)
        if not path.is_file():
            return None
        content = self._read_text(path, self.handoff_rel)
        if contains_secret_like(content):
            raise AdapterContentError(
                "Refused to read checkpoint: content looks like it embeds "
                "secret values."
            )
        return content

    def _do_read_changelog(self) -> str | None:
        path = self._resolve(self.changelog_rel)
        if not path.is_file():
            return None
        content = self._read_text(path, self.changelog_rel)
        if contains_secret_like(content):
            raise AdapterContentError(
                "Refused to read changelog: content looks like it embeds "
                "secret values."
            )
        return content

    # -- write -------------------------------------------------------------

    def _do_write_checkpoint(self, content: str) -> AdapterWriteResult:
        if contains_secret_like(content):
            raise AdapterContentError(
                "Refused to write checkpoint: content looks like it embeds "
                "secret values."
            )
        try:
            manager = CheckpointManager(project_root=self.project_root)
            result = manager.write_checkpoint(content)
        except HandoffContentError as exc:
            raise AdapterContentError(str(exc)) from exc
        except (HandoffPathError, HandoffWriteError, ChangelogError) as exc:
            raise AdapterError(str(exc)) from exc

        identity = ""
        from handoff_agent.protocol import parse_handoff_document

        cp = parse_handoff_document(content)
        if cp is not None:
            identity = cp.identity.id
        return AdapterWriteResult(
            rel_path=self.handoff_rel,
            created=result.created,
            modified=result.modified,
            unchanged=result.unchanged,
            history_recorded=result.history_recorded,
            identity=identity,
        )

    # -- project state ------------------------------------------------------

    def _do_project_state(self) -> dict[str, Any]:
        try:
            ctx = ContextBuilder(project_root=str(self.project_root)).build()
        except NotARepositoryError as exc:
            raise AdapterError(str(exc)) from exc
        except Exception as exc:
            raise AdapterError(f"Failed to build project state: {exc}") from exc
        state = ctx.to_dict()
        state["protocol"] = {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION}
        return state

    # -- validation ----------------------------------------------------------

    def _do_validate_checkpoint(self) -> dict[str, Any]:
        content = self._do_read_handoff()
        if content is None:
            return {
                "valid": False,
                "errors": ["No checkpoint found (docs/HANDOFF.md does not exist)."],
                "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            }
        try:
            cp = parse_handoff_document(content)
        except ProtocolVersionError as exc:
            return {
                "valid": False,
                "errors": [str(exc)],
                "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            }
        except ProtocolValidationError as exc:
            return {
                "valid": False,
                "errors": list(exc.errors or [str(exc)]),
                "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            }
        if cp is None:
            return {
                "valid": False,
                "errors": [
                    "No handoff-protocol block found (legacy checkpoint)."
                ],
                "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            }
        errors = validate_checkpoint(cp)
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "identity": {
                "id": cp.identity.id,
                "generated_at": cp.identity.generated_at,
                "sequence": cp.identity.sequence,
            },
            "identity_verified": verify_identity(cp),
            "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
        }

    # -- helpers -------------------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        return resolve_within_root(self.project_root, rel)

    @staticmethod
    def _read_text(path: Path, rel: str) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AdapterError(f"Failed to read {rel}: {exc}") from exc