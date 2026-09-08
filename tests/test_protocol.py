"""Tests for Phase 11 — Universal Handoff Protocol.

Covers the protocol specification, versioning, canonical checkpoint model,
machine-readable state schema, checkpoint identity, objective/state/
completed/in-progress/next actions, decisions & constraints, validation state,
Git state, artifacts & risks, backward compatibility, and validation.
"""

from __future__ import annotations

import json

import pytest

from handoff_agent.protocol import (
    Checkpoint,
    CheckpointDiff,
    CheckpointState,
    GitState,
    ProtocolIdentity,
    ProtocolValidationError,
    ProtocolVersionError,
    ValidationState,
    build_checkpoint,
    checkpoint_identity,
    diff_checkpoints,
    extract_state_block,
    is_supported_version,
    load_checkpoint,
    parse_handoff_document,
    protocol_version_string,
    render_state_block,
    state_block_marker,
    state_schema,
    validate_checkpoint,
    validate_checkpoint_data,
    verify_identity,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
)


def _sample_checkpoint(**overrides) -> Checkpoint:
    return build_checkpoint(
        objective="Ship the protocol",
        completed=["defined protocol", "built schema"],
        in_progress=["adapter"],
        next_actions=["MCP server"],
        decisions=["use stdlib only"],
        constraints=["no new deps"],
        project_name="handoff-agent",
        project_type="Python",
        git=GitState(branch="main", head="abc123", clean=True, status="clean"),
        **overrides,
    )


class TestVersioning:
    def test_name_and_version(self) -> None:
        assert PROTOCOL_NAME == "universal-handoff-protocol"
        assert PROTOCOL_VERSION == 1

    def test_version_string(self) -> None:
        assert protocol_version_string() == "universal-handoff-protocol/1"

    def test_supported_versions(self) -> None:
        assert is_supported_version(1)
        assert not is_supported_version(2)
        assert not is_supported_version("1")
        assert not is_supported_version(None)

    def test_state_schema_is_object(self) -> None:
        schema = state_schema()
        assert schema["type"] == "object"
        assert "protocol" in schema["properties"]
        assert "state" in schema["properties"]


class TestCanonicalModel:
    def test_build_roundtrip(self) -> None:
        cp = _sample_checkpoint()
        assert cp.state.objective == "Ship the protocol"
        assert cp.state.completed == ("defined protocol", "built schema")
        assert cp.git.branch == "main"

    def test_to_dict_matches_schema(self) -> None:
        cp = _sample_checkpoint()
        errors = validate_checkpoint_data(cp.to_dict())
        assert errors == []

    def test_to_json_is_parseable(self) -> None:
        cp = _sample_checkpoint()
        parsed = json.loads(cp.to_json())
        assert parsed["protocol"]["version"] == PROTOCOL_VERSION

    def test_defaults_empty(self) -> None:
        cp = build_checkpoint(objective="x")
        assert cp.state.completed == ()
        assert cp.state.decisions == ()
        assert cp.artifacts == ()
        assert cp.risks == ()
        assert cp.validation.status == "pending"


class TestIdentity:
    def test_identity_is_64_hex(self) -> None:
        cp = _sample_checkpoint()
        assert len(cp.identity.id) == 64
        int(cp.identity.id, 16)

    def test_same_state_same_identity(self) -> None:
        a = _sample_checkpoint()
        b = build_checkpoint(
            objective="Ship the protocol",
            completed=["built schema", "defined protocol"],  # reordered
            in_progress=["adapter"],
            next_actions=["MCP server"],
            decisions=["use stdlib only"],
            constraints=["no new deps"],
            project_name="handoff-agent",
            project_type="Python",
        )
        assert a.identity.id == b.identity.id

    def test_diff_state_diff_identity(self) -> None:
        a = _sample_checkpoint()
        b = build_checkpoint(
            objective="Ship the protocol",
            completed=["defined protocol"],
            in_progress=["adapter"],
            next_actions=["MCP server"],
            project_name="handoff-agent",
        )
        assert a.identity.id != b.identity.id

    def test_verify_identity(self) -> None:
        cp = _sample_checkpoint()
        assert verify_identity(cp)

    def test_identity_excludes_timestamp(self) -> None:
        a = _sample_checkpoint(sequence=0)
        b = _sample_checkpoint(sequence=5)
        assert a.to_dict()["identity"]["id"] == b.to_dict()["identity"]["id"]


class TestFields:
    def test_validation_state_frozen(self) -> None:
        cp = _sample_checkpoint(validation_status="passed", validation_checks=["t1"])
        assert cp.validation.status == "passed"
        assert cp.validation.checks == ("t1",)

    def test_artifacts_risks(self) -> None:
        cp = _sample_checkpoint(artifacts=["A"], risks=["B"])
        assert cp.artifacts == ("A",)
        assert cp.risks == ("B",)
        assert cp.to_dict()["artifacts"] == ["A"]


class TestValidation:
    def test_valid_checkpoint_passes(self) -> None:
        cp = _sample_checkpoint()
        assert validate_checkpoint(cp) == []

    def test_missing_required_state(self) -> None:
        data = _sample_checkpoint().to_dict()
        del data["state"]["next_actions"]
        errors = validate_checkpoint_data(data)
        assert any("next_actions" in e for e in errors)

    def test_wrong_protocol_name(self) -> None:
        data = _sample_checkpoint().to_dict()
        data["protocol"]["name"] = "other"
        errors = validate_checkpoint_data(data)
        assert errors

    def test_bad_identity_length(self) -> None:
        data = _sample_checkpoint().to_dict()
        data["identity"]["id"] = "short"
        errors = validate_checkpoint_data(data)
        assert any("id" in e for e in errors)

    def test_load_rejects_unsupported_version(self) -> None:
        data = _sample_checkpoint().to_dict()
        data["protocol"]["version"] = 99
        with pytest.raises(ProtocolVersionError):
            load_checkpoint(data)

    def test_load_rejects_invalid(self) -> None:
        with pytest.raises(ProtocolValidationError):
            load_checkpoint({"protocol": {"name": PROTOCOL_NAME, "version": 1}})

    def test_load_invalid_json(self) -> None:
        with pytest.raises(ProtocolValidationError):
            load_checkpoint("not json{{{")


class TestRoundTrip:
    def test_load_checkpoint_from_dict(self) -> None:
        cp = _sample_checkpoint()
        loaded = load_checkpoint(cp.to_dict())
        assert loaded.identity.id == cp.identity.id
        assert loaded.state.objective == cp.state.objective
        assert loaded.git.branch == "main"

    def test_load_checkpoint_from_json(self) -> None:
        cp = _sample_checkpoint()
        loaded = load_checkpoint(cp.to_json())
        assert loaded.state.completed == cp.state.completed


class TestBackwardCompatibility:
    def test_bare_markdown_returns_none(self) -> None:
        text = "# Handoff\n\nSome legacy content."
        assert parse_handoff_document(text) is None

    def test_extract_none_when_absent(self) -> None:
        assert extract_state_block("no block here") is None

    def test_render_and_parse(self) -> None:
        cp = _sample_checkpoint()
        doc = "# Project\n\n" + render_state_block(cp)
        parsed = parse_handoff_document(doc)
        assert parsed is not None
        assert parsed.identity.id == cp.identity.id
        assert parsed.state.objective == cp.state.objective

    def test_marker(self) -> None:
        assert state_block_marker() == "handoff-protocol"


class TestDiff:
    def test_diff_forward(self) -> None:
        before = _sample_checkpoint()
        after = build_checkpoint(
            objective="Ship the protocol",
            completed=["defined protocol", "built schema", "wrote tests"],
            in_progress=["adapter"],
            next_actions=["MCP server"],
            decisions=["use stdlib only"],
            constraints=["no new deps"],
            project_name="handoff-agent",
        )
        diff = diff_checkpoints(before, after)
        assert diff.completed_added == ("wrote tests",)

    def test_diff_same_state_identical(self) -> None:
        a = _sample_checkpoint(generated_at="2026-01-01T00:00:00+00:00")
        b = _sample_checkpoint(generated_at="2026-01-01T00:00:00+00:00")
        diff = diff_checkpoints(a, b)
        assert diff.same_identity
        assert diff.identical

    def test_diff_before_none(self) -> None:
        after = _sample_checkpoint()
        diff = diff_checkpoints(None, after)
        assert not diff.same_identity
        assert diff.completed_added == after.state.completed
