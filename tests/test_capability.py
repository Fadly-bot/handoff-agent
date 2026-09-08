"""Tests for Phase 12 — Capability & Agent Contract.

Covers agent identity, adapter identity, capability discovery, capability
declaration, read/write capabilities, project/git/checkpoint/validation
capabilities, capability negotiation, unsupported capability handling,
permission boundaries, security constraints, and deterministic output.
"""

from __future__ import annotations

import pytest

from handoff_agent.capability import (
    ALL_CAPABILITIES,
    CAP_CHANGELOG_READ,
    CAP_CHECKPOINT_CREATE,
    CAP_CHECKPOINT_READ,
    CAP_CHECKPOINT_UPDATE,
    CAP_GIT_INSPECTION,
    CAP_PROJECT_INSPECTION,
    CAP_VALIDATION,
    READ_CAPABILITIES,
    WRITE_CAPABILITIES,
    AgentContract,
    AgentIdentity,
    AdapterIdentity,
    CapabilityDeclaration,
    CapabilityDeniedError,
    CapabilityError,
    NegotiationResult,
    PermissionBoundary,
    SecurityConstraints,
    UnsupportedCapabilityError,
    build_contract,
    capabilities_for_category,
    capability_catalog,
    discover_capabilities,
    identity_hash,
    is_known_capability,
    negotiate_capabilities,
)


class TestCapabilityDiscovery:
    def test_all_capabilities(self) -> None:
        caps = discover_capabilities()
        assert isinstance(caps, tuple)
        assert len(caps) > 0
        assert caps == tuple(sorted(caps))

    def test_read_capabilities_subset(self) -> None:
        assert READ_CAPABILITIES < ALL_CAPABILITIES

    def test_write_capabilities_subset(self) -> None:
        assert WRITE_CAPABILITIES < ALL_CAPABILITIES

    def test_is_known(self) -> None:
        assert is_known_capability(CAP_CHECKPOINT_READ)
        assert not is_known_capability("bogus.cap")

    def test_category_filter(self) -> None:
        caps = capabilities_for_category("checkpoint")
        assert CAP_CHECKPOINT_READ in caps
        assert CAP_CHECKPOINT_CREATE in caps
        assert CAP_CHECKPOINT_UPDATE in caps
        assert all(c.startswith("checkpoint.") for c in caps)


class TestAgentIdentity:
    def test_defaults(self) -> None:
        id_ = AgentIdentity(name="test")
        assert id_.version == "0.0.1"
        assert id_.kind == "ai"

    def test_roundtrip(self) -> None:
        id_ = AgentIdentity(name="claude", version="2.0.0", kind="tool")
        d = id_.to_dict()
        restored = AgentIdentity.from_dict(d)
        assert restored == id_


class TestAdapterIdentity:
    def test_roundtrip(self) -> None:
        a = AdapterIdentity(name="openai", version="1.0")
        d = a.to_dict()
        restored = AdapterIdentity.from_dict(d)
        assert restored == a


class TestDeclaration:
    def test_valid_declaration(self) -> None:
        agent = AgentIdentity(name="test")
        decl = CapabilityDeclaration(
            agent=agent,
            capabilities=frozenset({CAP_CHECKPOINT_READ, CAP_VALIDATION}),
        )
        assert CAP_CHECKPOINT_READ in decl.capabilities

    def test_rejects_unknown_capability(self) -> None:
        agent = AgentIdentity(name="test")
        with pytest.raises(CapabilityError, match="unknown"):
            CapabilityDeclaration(
                agent=agent,
                capabilities=frozenset({"bogus.foo"}),
            )

    def test_roundtrip(self) -> None:
        agent = AgentIdentity(name="test")
        adapter = AdapterIdentity(name="claude", version="1.0")
        decl = CapabilityDeclaration(
            agent=agent,
            capabilities=frozenset({CAP_CHECKPOINT_READ}),
            adapter=adapter,
        )
        d = decl.to_dict()
        restored = CapabilityDeclaration.from_dict(d)
        assert restored.agent == decl.agent
        assert restored.adapter == decl.adapter


class TestNegotiation:
    def test_all_granted(self) -> None:
        result = negotiate_capabilities(frozenset({CAP_CHECKPOINT_READ}))
        assert CAP_CHECKPOINT_READ in result.granted
        assert not result.denied
        assert not result.unknown
        assert result.ok

    def test_denied_when_not_allowed(self) -> None:
        read_only = PermissionBoundary(
            allowed=READ_CAPABILITIES,
        )
        result = negotiate_capabilities(
            frozenset({CAP_CHECKPOINT_READ, CAP_CHECKPOINT_CREATE}),
            read_only.allowed,
        )
        assert CAP_CHECKPOINT_READ in result.granted
        assert CAP_CHECKPOINT_CREATE in result.denied
        assert not result.ok

    def test_unknown_capability(self) -> None:
        result = negotiate_capabilities(frozenset({"fake.cap"}))
        assert "fake.cap" in result.unknown
        assert not result.ok

    def test_empty_is_valid(self) -> None:
        result = negotiate_capabilities(frozenset())
        assert result.ok
        assert result.granted == frozenset()


class TestPermissionBoundary:
    def test_all_allowed_by_default(self) -> None:
        boundary = PermissionBoundary()
        assert boundary.is_allowed(CAP_CHECKPOINT_READ)
        assert boundary.is_allowed(CAP_CHECKPOINT_CREATE)

    def test_deny_all(self) -> None:
        boundary = PermissionBoundary(deny_all=True)
        assert not boundary.is_allowed(CAP_CHECKPOINT_READ)

    def test_read_only_boundary(self) -> None:
        boundary = PermissionBoundary(allowed=READ_CAPABILITIES)
        assert boundary.is_allowed(CAP_CHECKPOINT_READ)
        assert not boundary.is_allowed(CAP_CHECKPOINT_CREATE)

    def test_check_raises_on_denied(self) -> None:
        boundary = PermissionBoundary(allowed=READ_CAPABILITIES)
        with pytest.raises(CapabilityDeniedError):
            boundary.check(CAP_CHECKPOINT_CREATE)

    def test_check_passes_on_allowed(self) -> None:
        boundary = PermissionBoundary(allowed=READ_CAPABILITIES)
        boundary.check(CAP_CHECKPOINT_READ)  # no exception


class TestSecurityConstraints:
    def test_defaults(self) -> None:
        sc = SecurityConstraints()
        assert sc.max_writes_per_session == 0
        assert sc.require_confirmation is False
        assert sc.audit_log is True

    def test_to_dict(self) -> None:
        sc = SecurityConstraints(max_writes_per_session=10, require_confirmation=True)
        d = sc.to_dict()
        assert d["max_writes_per_session"] == 10
        assert d["require_confirmation"] is True


class TestAgentContract:
    def test_build_contract(self) -> None:
        agent = AgentIdentity(name="test")
        contract = build_contract(
            agent,
            frozenset({CAP_CHECKPOINT_READ, CAP_VALIDATION}),
        )
        assert contract.can(CAP_CHECKPOINT_READ)
        assert contract.can(CAP_VALIDATION)
        assert not contract.can(CAP_CHECKPOINT_CREATE)

    def test_build_rejects_unknown(self) -> None:
        agent = AgentIdentity(name="test")
        with pytest.raises(CapabilityError, match="unknown"):
            build_contract(agent, frozenset({"bad.cap"}))

    def test_assert_can_ok(self) -> None:
        contract = build_contract(
            AgentIdentity(name="test"),
            frozenset({CAP_CHECKPOINT_READ}),
        )
        contract.assert_can(CAP_CHECKPOINT_READ)

    def test_assert_can_denied(self) -> None:
        contract = build_contract(
            AgentIdentity(name="test"),
            frozenset({CAP_CHECKPOINT_READ}),
        )
        with pytest.raises(CapabilityDeniedError):
            contract.assert_can(CAP_CHECKPOINT_CREATE)

    def test_assert_can_unsupported(self) -> None:
        contract = build_contract(
            AgentIdentity(name="test"),
            frozenset({CAP_CHECKPOINT_READ}),
        )
        # Manually add an unknown capability to the granted set to simulate
        # an invalid state; assert_can should raise UnsupportedCapabilityError.
        contract = AgentContract(
            agent=contract.agent,
            granted=frozenset({"fake.cap"}),
            boundary=PermissionBoundary(),
        )
        with pytest.raises(UnsupportedCapabilityError):
            contract.assert_can("fake.cap")


class TestDeterministicOutput:
    def test_catalog_is_sorted(self) -> None:
        cat = capability_catalog()
        keys = list(cat.keys())
        assert keys == sorted(keys)

    def test_identity_hash_deterministic(self) -> None:
        a = AgentIdentity(name="claude", version="1.0", kind="ai")
        b = AgentIdentity(name="claude", version="1.0", kind="ai")
        assert identity_hash(a) == identity_hash(b)
        assert len(identity_hash(a)) == 64

    def test_identity_hash_differs(self) -> None:
        a = AgentIdentity(name="claude", version="1.0", kind="ai")
        b = AgentIdentity(name="openai", version="1.0", kind="ai")
        assert identity_hash(a) != identity_hash(b)

    def test_negotiation_is_deterministic(self) -> None:
        r1 = negotiate_capabilities(frozenset({CAP_CHECKPOINT_READ}))
        r2 = negotiate_capabilities(frozenset({CAP_CHECKPOINT_READ}))
        assert r1.granted == r2.granted
        assert r1.denied == r2.denied
