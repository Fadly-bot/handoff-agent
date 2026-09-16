"""Phase 32 — Policy, identity, trust, and approval engine tests."""

from __future__ import annotations

import pytest

from handoff_agent.policy_engine import (
    ActionRequest,
    Effect,
    PolicyDenied,
    PolicyEngine,
    PolicyError,
    allow_default_git_operations,
    protect_default_sensitive_files,
)
from handoff_agent.telemetry import TelemetryDomain, new_collector


def _engine(*, tracer=None) -> PolicyEngine:
    e = PolicyEngine(tracer=tracer)
    e.register_subject("agent-1", "agent", trust_level=3, capabilities=("checkpoint.create",))
    e.register_subject("device-1", "device", trust_level=2)
    e.register_subject("endpoint-1", "endpoint", trust_level=1)
    protect_default_sensitive_files(e)
    allow_default_git_operations(e)
    return e


class TestIdentityAndTrust:
    def test_unauthorized_agent_denied(self) -> None:
        e = _engine()
        d = e.evaluate(ActionRequest("unknown-agent", "filesystem.write", resource="/tmp/x"))
        assert d.allowed is False
        assert d.effect == Effect.DENY
        assert any("unknown subject" in r for r in d.reasons)

    def test_revoked_device_denied(self) -> None:
        e = _engine()
        e.revoke_subject("device-1")
        d = e.evaluate(ActionRequest("device-1", "network.connect", resource="api.example.com"))
        assert d.allowed is False
        assert d.effect == Effect.DENY
        assert any("revoked" in r for r in d.reasons)

    def test_unknown_endpoint_denied(self) -> None:
        e = _engine()
        d = e.evaluate(ActionRequest("endpoint-999", "network.connect", resource="x"))
        assert d.allowed is False

    def test_capability_revocation_denies(self) -> None:
        e = _engine()
        e.revoke_capability("agent-1", "checkpoint.create")
        d = e.evaluate(ActionRequest("agent-1", "capability.checkpoint.create"))
        assert d.allowed is False
        assert any("checkpoint.create" in r for r in d.reasons)

    def test_trust_level_recorded_and_checked(self) -> None:
        e = _engine()
        low = _engine()
        d = e.evaluate(ActionRequest("agent-1", "capability.checkpoint.create"))
        assert d.allowed is True
        low.revoke_subject("agent-1")
        d2 = low.evaluate(ActionRequest("agent-1", "capability.checkpoint.create"))
        assert d2.allowed is False


class TestProtectedPaths:
    def test_protected_path_write_denied_without_grant(self) -> None:
        e = _engine()
        d = e.evaluate(ActionRequest("agent-1", "filesystem.write", resource="/repo/.env"))
        assert d.allowed is False
        d = e.evaluate(ActionRequest("agent-1", "filesystem.write", resource="/repo/secret.txt"))
        assert d.allowed is False

    def test_granted_path_allows_write(self) -> None:
        e = _engine()
        e.grant_path("agent-1", "/repo/**/allowed/*")
        d = e.evaluate(ActionRequest("agent-1", "filesystem.write", resource="/repo/allowed/x.txt"))
        assert d.allowed is True
        d = e.evaluate(ActionRequest("agent-1", "filesystem.write", resource="/repo/secrets/x"))
        assert d.allowed is False


class TestGitNetworkSecret:
    def test_git_push_denied_default(self) -> None:
        e = _engine()
        d = e.evaluate(ActionRequest("agent-1", "git.push", resource="git.push"))
        assert d.allowed is False

    def test_git_status_allowed(self) -> None:
        e = _engine()
        d = e.evaluate(ActionRequest("agent-1", "git.status", resource="git.status"))
        assert d.allowed is True

    def test_network_not_allowlisted_denied(self) -> None:
        e = _engine()
        e.allow_network("*.trusted.example")
        d = e.evaluate(ActionRequest("device-1", "network.connect", resource="evil.com"))
        assert d.allowed is False
        d = e.evaluate(ActionRequest("device-1", "network.connect", resource="svc.trusted.example"))
        assert d.allowed is True

    def test_secret_access_denied_by_default(self) -> None:
        e = _engine()
        d = e.evaluate(ActionRequest("agent-1", "secret.read"))
        assert d.allowed is False
        e.grant_secret("agent-1")
        d = e.evaluate(ActionRequest("agent-1", "secret.read"))
        assert d.allowed is True


class TestDestructiveApproval:
    def test_destructive_requires_approval(self) -> None:
        e = _engine()
        e.require_approval_for_destructive("destructive.*")
        d = e.evaluate(ActionRequest("agent-1", "destructive.rm", resource="/tmp/old"))
        assert d.allowed is False
        assert d.effect == Effect.REQUIRE_APPROVAL
        assert d.approval_required is True

    def test_destructive_approval_ticket_unlocks(self) -> None:
        e = _engine()
        e.require_approval_for_destructive("destructive.*")
        req = ActionRequest("agent-1", "destructive.rm", resource="/tmp/old")
        ticket = e.request_approval(req)
        grant = e.grant_approval(ticket, approver="human-1", note="approved")
        assert e.is_approved(ticket)
        d = e.evaluate(req, approval_ticket=ticket.ticket_id)
        assert d.allowed is True
        assert d.effect == Effect.ALLOW

    def test_destructive_approval_cannot_be_forged(self) -> None:
        e = _engine()
        e.require_approval_for_destructive("destructive.*")
        req = ActionRequest("agent-1", "destructive.rm", resource="/tmp/old")
        ticket = e.request_approval(req)
        e.grant_approval(ticket, approver="human-1", note="ok")
        # the approval is bound to the exact request signature: presenting the
        # ticket for a *different* resource must NOT unlock it.
        forged = ActionRequest("agent-1", "destructive.rm", resource="/tmp/other")
        d = e.evaluate(forged, approval_ticket=ticket.ticket_id)
        assert d.allowed is False
        assert d.effect == Effect.REQUIRE_APPROVAL


class TestDeterministicConflicts:
    def test_conflicting_rules_deny_by_default(self) -> None:
        e = _engine()
        e.add_rule("tool.exec", Effect.ALLOW, scope="prod", note="allow")
        e.add_rule("tool.exec", Effect.DENY, scope="prod", note="deny")
        d = e.evaluate(ActionRequest("agent-1", "tool.exec", resource="sh", scope="prod"))
        assert d.allowed is False
        assert d.effect == Effect.DENY

    def test_scope_inheritance(self) -> None:
        e = _engine()
        e.add_rule("deployment.release", Effect.ALLOW, scope="prod", note="allow prod")
        d_child = e.evaluate(ActionRequest("agent-1", "deployment.release", scope="prod.eu"))
        d_other = e.evaluate(ActionRequest("agent-1", "deployment.release", scope="stage"))
        assert d_child.allowed is True
        assert d_other.allowed is False

    def test_deterministic_repeated_evaluation(self) -> None:
        e = _engine()
        e.add_rule("tool.exec", Effect.ALLOW, scope="*")
        e.add_rule("tool.exec", Effect.DENY, scope="prod")
        first = e.evaluate(ActionRequest("agent-1", "tool.exec", scope="prod"))
        second = e.evaluate(ActionRequest("agent-1", "tool.exec", scope="prod"))
        assert first.to_dict() == second.to_dict()


class TestExplanationAndAudit:
    def test_explanation_available(self) -> None:
        e = _engine()
        d = e.evaluate(ActionRequest("agent-1", "filesystem.write", resource="/repo/.env"))
        reasons = e.explain(d)
        assert reasons
        assert d.rule_id

    def test_audit_trail_recorded(self) -> None:
        e = _engine()
        e.evaluate(ActionRequest("agent-1", "capability.checkpoint.create"))
        e.evaluate(ActionRequest("agent-1", "git.push", resource="git.push"))
        trail = e.audit_trail()
        assert len(trail) >= 2
        assert all("subject_id" in entry for entry in trail)
        assert all("timestamp_ms" in entry for entry in trail)

    def test_assert_allowed_raises_on_deny(self) -> None:
        e = _engine()
        with pytest.raises(PolicyDenied):
            e.assert_allowed(ActionRequest("agent-1", "git.push", resource="git.push"))


class TestTelemetryIntegration:
    def test_policy_events_emitted_to_tracer(self) -> None:
        collector = new_collector()
        e = _engine(tracer=collector)
        e.assert_allowed(ActionRequest("agent-1", "capability.checkpoint.create"))
        with pytest.raises(PolicyDenied):
            e.assert_allowed(ActionRequest("agent-1", "git.push", resource="git.push"))
        policy_events = [
            evt for evt in collector.events() if evt.domain == TelemetryDomain.POLICY.value
        ]
        assert len(policy_events) == 2
        ok_event = [evt for evt in policy_events if evt.status == "ok"]
        blocked_event = [evt for evt in policy_events if evt.status == "blocked"]
        assert ok_event and blocked_event
        serialized = str([evt.to_dict() for evt in policy_events])
        assert "rule_id" in serialized
        assert "git.push" in serialized


class TestNoBypass:
    def test_cannot_override_protected_path_with_allow_rule(self) -> None:
        e = _engine()
        e.add_rule("filesystem.write", Effect.ALLOW, scope="*", note="allow all writes")
        d = e.evaluate(ActionRequest("agent-1", "filesystem.write", resource="/repo/.env"))
        assert d.allowed is False  # protected path gate wins over ALLOW rule

    def test_cannot_override_revoked_subject(self) -> None:
        e = _engine()
        e.revoke_subject("agent-1")
        e.add_rule("anything", Effect.ALLOW, scope="*")
        d = e.evaluate(ActionRequest("agent-1", "anything"))
        assert d.allowed is False

    def test_policy_version_recorded(self) -> None:
        e = PolicyEngine(policy_version=7)
        e.register_subject("agent-1", "agent", trust_level=2, capabilities=("x",))
        d = e.evaluate(ActionRequest("agent-1", "capability.x"))
        assert d.policy_version == 7


class TestPolicyReport:
    def test_report_shape(self) -> None:
        e = _engine()
        report = e.policy_report()
        assert report["policy_version"] == 1
        assert report["subject_count"] >= 3
        assert "audit_count" in report