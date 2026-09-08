"""Universal Handoff Protocol (Phase 11).

This module defines the provider-agnostic, machine-readable Handoff protocol
that underpins Handoff Agent. It is deliberately independent of any specific
AI provider (Claude / OpenAI / Qwen / DeepSeek / Gemini / Kimi / GLM / Grok /
Manus / ...) and of any specific skill / MCP transport.

Goals:

  - A canonical, versioned checkpoint model that both humans and machines can
    read (``docs/HANDOFF.md`` remains the human-readable representation).
  - A machine-readable state schema that any tool / agent can consume without
    depending on Handoff Agent internals.
  - Deterministic checkpoint identity so a given checkpoint can be referenced,
    diffed, and audited across time and across agents.
  - Objective, state, completed/in-progress/next actions, decisions &
    constraints, validation state, Git state, artifacts & risks.
  - Backward compatibility: existing ``docs/HANDOFF.md`` content that does not
    carry protocol metadata is still accepted and validated.

The protocol does NOT execute filesystem or Git operations itself. Safe
persistence and read-only Git inspection remain owned by ``persistence`` and
``git_inspector`` respectively; protocol structures (the "state schema") are
plain, strict data.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Protocol versioning
# ---------------------------------------------------------------------------

#: The current protocol major version (v1). Breaking schema changes bump this.
PROTOCOL_NAME = "universal-handoff-protocol"
PROTOCOL_VERSION = 1
PROTOCOL_VERSIONS_SUPPORTED = (1,)
#: Separator used to split protocol fields from human-readable bodies.
PROTOCOL_BLOCK_KEY = "handoff-protocol"


class ProtocolError(Exception):
    """Base error for protocol handling."""


class ProtocolVersionError(ProtocolError):
    """Raised when a checkpoint uses an unsupported protocol version."""


class ProtocolValidationError(ProtocolError):
    """Raised when a checkpoint does not conform to the state schema."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


def protocol_version_string() -> str:
    """Return the canonical version string for the current protocol.

    e.g. ``"universal-handoff-protocol/1"``
    """
    return f"{PROTOCOL_NAME}/{PROTOCOL_VERSION}"


def is_supported_version(version: Any) -> bool:
    """Return True if *version* is a protocol version Handoff Agent supports.

    Only actual integers are considered valid versions. Booleans and numeric
    strings (e.g. ``"1"``) are rejected because the schema types ``version`` as
    an integer and accepting coerced values could mask schema violations.
    """
    if isinstance(version, bool):
        return False
    if not isinstance(version, int):
        return False
    return version in PROTOCOL_VERSIONS_SUPPORTED


# ---------------------------------------------------------------------------
# Canonical checkpoint model (state schema)
# ---------------------------------------------------------------------------
#
# The checkpoint is a plain JSON object with a fixed top-level shape. Content
# that is only human-relevant (markdown bodies) may additionally appear in the
# rendered HANDOFF.md, but the machine-readable ``state`` stays strict.

_OBJ = "object"
_STR = "string"
_ARR = "array"
_INT = "integer"
_NULL = "null"


def state_schema() -> dict[str, Any]:
    """Return the machine-readable JSON-schema (draft 2020-12) for a checkpoint.

    This is the canonical state schema. Any tool, agent, or transport that can
    validate JSON against this can read a Handoff checkpoint.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://handoff.local/schema/" + protocol_version_string(),
        "title": "Universal Handoff Checkpoint",
        "description": (
            "Provider-agnostic machine-readable checkpoint state for the "
            "Universal Handoff Protocol."
        ),
        "type": _OBJ,
        "required": ["protocol", "identity", "metadata", "state"],
        "properties": {
            "protocol": {
                "type": _OBJ,
                "required": ["name", "version"],
                "properties": {
                    "name": {"const": PROTOCOL_NAME},
                    "version": {
                        "type": _INT,
                        "enum": sorted(PROTOCOL_VERSIONS_SUPPORTED),
                    },
                },
            },
            "identity": {
                "type": _OBJ,
                "required": ["id", "generated_at", "sequence"],
                "properties": {
                    "id": {
                        "type": _STR,
                        "minLength": 64,
                        "maxLength": 64,
                        "pattern": "^[0-9a-f]{64}$",
                    },
                    "generated_at": {
                        "type": _STR,
                        "format": "date-time",
                        "pattern": (
                            "^[0-9]{4}-[0-9]{2}-[0-9]{2}"
                            "T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        ),
                    },
                    "sequence": {"type": _INT, "minimum": 0},
                },
            },
            "metadata": {
                "type": _OBJ,
                "required": ["project", "objective"],
                "properties": {
                    "project": {
                        "type": _OBJ,
                        "required": ["name"],
                        "properties": {
                            "name": {"type": _STR, "minLength": 1},
                            "type": {"type": _STR},
                        },
                    },
                    "objective": {"type": _STR},
                    "agents": {
                        "type": _ARR,
                        "items": {"type": _STR},
                    },
                },
            },
            "state": {
                "type": _OBJ,
                "required": ["completed", "in_progress", "next_actions"],
                "properties": {
                    "objective": {"type": _STR},
                    "completed": {
                        "type": _ARR,
                        "items": {"type": _STR},
                    },
                    "in_progress": {
                        "type": _ARR,
                        "items": {"type": _STR},
                    },
                    "next_actions": {
                        "type": _ARR,
                        "items": {"type": _STR},
                    },
                    "decisions": {
                        "type": _ARR,
                        "items": {"type": _STR},
                    },
                    "constraints": {
                        "type": _ARR,
                        "items": {"type": _STR},
                    },
                },
            },
            "validation": {
                "type": _OBJ,
                "properties": {
                    "status": {
                        "type": _STR,
                        "enum": ["pending", "passed", "failed"],
                    },
                    "checks": {
                        "type": _ARR,
                        "items": {"type": _STR},
                    },
                },
            },
            "git": {
                "type": _OBJ,
                "properties": {
                    "branch": {"type": [_STR, _NULL]},
                    "head": {"type": [_STR, _NULL]},
                    "clean": {"type": ["boolean", _NULL]},
                    "status": {"type": [_STR, _NULL]},
                },
            },
            "artifacts": {
                "type": _ARR,
                "items": {"type": _STR},
            },
            "risks": {
                "type": _ARR,
                "items": {"type": _STR},
            },
        },
    }


# ---------------------------------------------------------------------------
# Data classes that mirror the schema (strict, friendly)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProtocolIdentity:
    """Deterministic identity for a single checkpoint."""

    id: str
    generated_at: str
    sequence: int = 0


@dataclass(frozen=True)
class ProtocolMeta:
    """Protocol + identity header for a checkpoint."""

    name: str = PROTOCOL_NAME
    version: int = PROTOCOL_VERSION


@dataclass(frozen=True)
class CheckpointState:
    """The machine-readable state of a checkpoint (objective/actions/...)."""

    objective: str = ""
    completed: tuple[str, ...] = ()
    in_progress: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "completed": list(self.completed),
            "in_progress": list(self.in_progress),
            "next_actions": list(self.next_actions),
            "decisions": list(self.decisions),
            "constraints": list(self.constraints),
        }


@dataclass(frozen=True)
class ValidationState:
    """Validation snapshot attached to a checkpoint."""

    status: str = "pending"  # pending | passed | failed
    checks: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": list(self.checks)}


@dataclass(frozen=True)
class GitState:
    """Read-only Git snapshot embedded in a checkpoint."""

    branch: str | None = None
    head: str | None = None
    clean: bool | None = None
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "head": self.head,
            "clean": self.clean,
            "status": self.status,
        }


@dataclass(frozen=True)
class Checkpoint:
    """Complete canonical checkpoint (machine-readable state schema)."""

    identity: ProtocolIdentity
    metadata: dict[str, Any] = field(default_factory=dict)
    state: CheckpointState = field(default_factory=CheckpointState)
    validation: ValidationState = field(default_factory=ValidationState)
    git: GitState = field(default_factory=GitState)
    artifacts: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize exactly to the canonical state-schema shape."""
        metadata = dict(self.metadata)
        project = metadata.get("project") or {"name": "unknown"}
        if isinstance(project, dict) and "name" not in project:
            project = {"name": "unknown"}
        metadata = {**metadata, "project": project}
        return {
            "protocol": {"name": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            "identity": {
                "id": self.identity.id,
                "generated_at": self.identity.generated_at,
                "sequence": self.identity.sequence,
            },
            "metadata": metadata,
            "state": self.state.to_dict(),
            "validation": self.validation.to_dict(),
            "git": self.git.to_dict(),
            "artifacts": list(self.artifacts),
            "risks": list(self.risks),
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to pretty JSON."""
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Checkpoint identity
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return the current UTC time in ISO-8601 seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_identity_payload(payload: dict[str, Any]) -> str:
    """Return a canonical JSON string used to derive the identity hash.

    Object keys are sorted recursively so two semantically identical payloads
    always produce the same identity regardless of key insertion order.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def checkpoint_identity(state: CheckpointState, metadata: dict[str, Any]) -> str:
    """Compute the deterministic identity (sha256 hex) of a checkpoint state.

    The identity is derived ONLY from the machine state (objective + actions +
    decisions + constraints) and the metadata that affects continuation. It is
    NOT derived from timestamps, so the same state always shares the same
    identity across agents — enabling cross-agent diffing.
    """
    payload = {
        "objective": state.objective,
        "completed": sorted(state.completed),
        "in_progress": sorted(state.in_progress),
        "next_actions": sorted(state.next_actions),
        "decisions": sorted(state.decisions),
        "constraints": sorted(state.constraints),
        "project": metadata.get("project"),
    }
    canonical = _normalize_identity_payload(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_checkpoint(
    *,
    objective: str = "",
    completed: list[str] | tuple[str, ...] = (),
    in_progress: list[str] | tuple[str, ...] = (),
    next_actions: list[str] | tuple[str, ...] = (),
    decisions: list[str] | tuple[str, ...] = (),
    constraints: list[str] | tuple[str, ...] = (),
    project_name: str = "unknown",
    project_type: str = "",
    agents: list[str] | tuple[str, ...] = (),
    validation_status: str = "pending",
    validation_checks: list[str] | tuple[str, ...] = (),
    git: GitState | None = None,
    artifacts: list[str] | tuple[str, ...] = (),
    risks: list[str] | tuple[str, ...] = (),
    sequence: int = 0,
    generated_at: str | None = None,
) -> Checkpoint:
    """Build a canonical checkpoint from plain fields.

    The identity is computed deterministically from the state, independent of
    the generated_at timestamp.
    """
    state = CheckpointState(
        objective=objective,
        completed=tuple(completed),
        in_progress=tuple(in_progress),
        next_actions=tuple(next_actions),
        decisions=tuple(decisions),
        constraints=tuple(constraints),
    )
    metadata: dict[str, Any] = {
        "project": {
            "name": project_name,
            "type": project_type,
        },
        "objective": objective,
    }
    if agents:
        metadata["agents"] = list(agents)

    identity = checkpoint_identity(state, metadata)
    ts = generated_at or now_iso()
    return Checkpoint(
        identity=ProtocolIdentity(id=identity, generated_at=ts, sequence=sequence),
        metadata=metadata,
        state=state,
        validation=ValidationState(
            status=validation_status,
            checks=tuple(validation_checks),
        ),
        git=git or GitState(),
        artifacts=tuple(artifacts),
        risks=tuple(risks),
    )


# ---------------------------------------------------------------------------
# Protocol validation
# ---------------------------------------------------------------------------

def _type_of(value: Any) -> str:
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return _INT
    if isinstance(value, str):
        return _STR
    if isinstance(value, list):
        return _ARR
    if isinstance(value, dict):
        return _OBJ
    return type(value).__name__


def _check(schema: dict[str, Any], value: Any, errors: list[str], path: str) -> None:
    """Recursively validate *value* against a (limited) JSON-schema subset.

    Handles: type (single or list), required, properties, items, enum, const,
    minLength, maxLength, pattern, minimum.
    """
    t = schema.get("type")
    actual = _type_of(value)
    if t is not None:
        if isinstance(t, list):
            type_ok = (
                actual in t
                or (t == [_INT, _NULL] and isinstance(value, bool))
                or any(ti == _INT and actual == "int" for ti in t)
            )
            if not type_ok:
                errors.append(f"{path}: expected one of {t!r}, got {actual}")
                return
        else:
            type_ok = (
                actual == t
                or (t == _INT and actual == "int")
            )
            if not type_ok:
                errors.append(f"{path}: expected {t}, got {actual}")
                return

    if t == _STR or (isinstance(t, list) and _STR in t):
        if not isinstance(value, str):
            pass
        else:
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append(f"{path}: shorter than minLength {schema['minLength']}")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
            pattern = schema.get("pattern")
            if pattern and re.match(pattern, value) is None:
                errors.append(f"{path}: does not match pattern")

    if t == _INT or (isinstance(t, list) and _INT in t):
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: below minimum {schema['minimum']}")

    if t == _ARR or (isinstance(t, list) and _ARR in t):
        if isinstance(value, list):
            items = schema.get("items")
            if isinstance(items, dict):
                for i, item in enumerate(value):
                    _check(items, item, errors, f"{path}[{i}]")

    if t == _OBJ or (isinstance(t, list) and _OBJ in t):
        if not isinstance(value, dict):
            return
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: missing required property")
        sub = schema.get("properties", {})
        for key, sub_schema in sub.items():
            if key in value:
                _check(sub_schema, value[key], errors, f"{path}.{key}")

    const = schema.get("const")
    if const is not None and value != const:
        errors.append(f"{path}: expected const {const!r}")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: not one of {enum!r}")


def validate_checkpoint_data(data: dict[str, Any]) -> list[str]:
    """Validate a parsed checkpoint dict against the canonical schema.

    Returns a list of validation error strings (empty if valid).
    """
    if not isinstance(data, dict):
        return ["checkpoint: expected an object"]
    errors: list[str] = []
    _check(state_schema(), data, errors, "checkpoint")
    return errors


def validate_checkpoint(checkpoint: Checkpoint) -> list[str]:
    """Validate a ``Checkpoint`` instance against the canonical schema."""
    return validate_checkpoint_data(checkpoint.to_dict())


def load_checkpoint(data: str | bytes | dict[str, Any]) -> Checkpoint:
    """Parse and validate a checkpoint from JSON / dict.

    Raises ``ProtocolValidationError`` if the data is not a valid canonical
    checkpoint, or ``ProtocolVersionError`` for unsupported versions.
    """
    if isinstance(data, (str, bytes)):
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtocolValidationError(
                "Checkpoint is not valid JSON.", [str(exc)]
            ) from exc
    else:
        parsed = data

    if not isinstance(parsed, dict):
        raise ProtocolValidationError("Checkpoint must be a JSON object.")

    protocol = parsed.get("protocol") or {}
    version = protocol.get("version")
    if not is_supported_version(version):
        raise ProtocolVersionError(
            f"Unsupported protocol version: {version!r}. "
            f"Supported: {sorted(PROTOCOL_VERSIONS_SUPPORTED)}"
        )

    errors = validate_checkpoint_data(parsed)
    if errors:
        raise ProtocolValidationError(
            "Checkpoint failed protocol validation.", errors
        )

    state = parsed["state"]
    metadata = parsed.get("metadata", {})
    project = metadata.get("project") or {}
    validation = parsed.get("validation") or {}
    git = parsed.get("git") or {}
    return Checkpoint(
        identity=ProtocolIdentity(
            id=parsed["identity"]["id"],
            generated_at=parsed["identity"]["generated_at"],
            sequence=parsed["identity"].get("sequence", 0),
        ),
        metadata=metadata,
        state=CheckpointState(
            objective=state.get("objective", ""),
            completed=tuple(state.get("completed", [])),
            in_progress=tuple(state.get("in_progress", [])),
            next_actions=tuple(state.get("next_actions", [])),
            decisions=tuple(state.get("decisions", [])),
            constraints=tuple(state.get("constraints", [])),
        ),
        validation=ValidationState(
            status=validation.get("status", "pending"),
            checks=tuple(validation.get("checks", [])),
        ),
        git=GitState(
            branch=git.get("branch"),
            head=git.get("head"),
            clean=git.get("clean"),
            status=git.get("status"),
        ),
        artifacts=tuple(parsed.get("artifacts", [])),
        risks=tuple(parsed.get("risks", [])),
    )


def verify_identity(checkpoint: Checkpoint) -> bool:
    """Recompute the checkpoint identity and compare with the embedded one."""
    expected = checkpoint_identity(checkpoint.state, checkpoint.metadata)
    return expected == checkpoint.identity.id


# ---------------------------------------------------------------------------
# Machine-readable block embedded in human-readable HANDOFF.md
# ---------------------------------------------------------------------------
#
# Backward compatibility: HANDOFF.md remains markdown. The machine-readable
# state is embedded inside a fenced JSON block with a well-known info-string.
# Older HANDOFF.md files that lack the block are still accepted by consumers.

BLOCK_OPEN_RE = re.compile(r"^```\s*" + re.escape(PROTOCOL_BLOCK_KEY) + r"\s*$", re.MULTILINE)


def state_block_marker() -> str:
    """Return the fenced-code info string used to mark a protocol block."""
    return PROTOCOL_BLOCK_KEY


def render_state_block(checkpoint: Checkpoint) -> str:
    """Render the machine-readable handoff block for embedding in HANDOFF.md.

    The block is self-contained (protocol + identity + state + metadata +
    validation + git + artifacts + risks) so a consumer can extract and
    validate state without the surrounding markdown.
    """
    body = json.dumps(checkpoint.to_dict(), indent=2)
    return f"```{PROTOCOL_BLOCK_KEY}\n{body}\n```"


def extract_state_block(text: str) -> str | None:
    """Extract the first protocol block body from a HANDOFF.md document.

    Returns the JSON text inside the fenced block, or None if absent.
    """
    match = BLOCK_OPEN_RE.search(text)
    if not match:
        return None
    start = match.end()
    end = text.find("```", start)
    if end == -1:
        return None
    return text[start:end].strip()


def parse_handoff_document(text: str) -> Checkpoint | None:
    """Parse a HANDOFF.md document into a canonical ``Checkpoint``.

    Returns None if the document does not contain a valid protocol block
    (backward-compatible: a legacy markdown HANDOFF.md returns None instead of
    raising). Raises ``ProtocolValidationError`` / ``ProtocolVersionError`` if
    the block exists but is invalid.
    """
    block = extract_state_block(text)
    if block is None:
        return None
    return load_checkpoint(block)


# ---------------------------------------------------------------------------
# Difference / continuity helpers (cross-agent auditability)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckpointDiff:
    """Structural difference between two checkpoints."""

    same_identity: bool
    completed_added: tuple[str, ...]
    in_progress_added: tuple[str, ...]
    next_actions_added: tuple[str, ...]
    identical: bool


def diff_checkpoints(before: Checkpoint | None, after: Checkpoint) -> CheckpointDiff:
    """Return a structural diff between two checkpoints (any may be None)."""
    if before is None:
        return CheckpointDiff(
            same_identity=False,
            completed_added=after.state.completed,
            in_progress_added=after.state.in_progress,
            next_actions_added=after.state.next_actions,
            identical=False,
        )
    same = before.identity.id == after.identity.id
    return CheckpointDiff(
        same_identity=same,
        completed_added=tuple(
            a for a in after.state.completed if a not in before.state.completed
        ),
        in_progress_added=tuple(
            a for a in after.state.in_progress if a not in before.state.in_progress
        ),
        next_actions_added=tuple(
            a for a in after.state.next_actions if a not in before.state.next_actions
        ),
        identical=before.to_dict() == after.to_dict(),
    )
