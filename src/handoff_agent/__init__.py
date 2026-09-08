"""Handoff Agent - AI-powered project handoff generator."""

__version__ = "0.4.0"

from handoff_agent.protocol import (  # noqa: F401
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    Checkpoint,
    CheckpointState,
    GitState,
    ProtocolIdentity,
    ProtocolVersionError,
    ValidationState,
    build_checkpoint,
    checkpoint_identity,
    load_checkpoint,
    parse_handoff_document,
    protocol_version_string,
    render_state_block,
    state_schema,
    validate_checkpoint,
    verify_identity,
)

__all__ = [
    "__version__",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "Checkpoint",
    "CheckpointState",
    "GitState",
    "ProtocolIdentity",
    "ProtocolVersionError",
    "ValidationState",
    "build_checkpoint",
    "checkpoint_identity",
    "load_checkpoint",
    "parse_handoff_document",
    "protocol_version_string",
    "render_state_block",
    "state_schema",
    "validate_checkpoint",
    "verify_identity",
]
