"""Phase 32 — Policy Engine: identity, trust, capability, approval.

Deterministic policy engine ensuring every agent, device, endpoint, workflow,
tool, and deployment action only runs according to identity, capability,
trust, scope, and approval.

Design invariants:

- **Deny by default**: an action with no matching rule / grant is DENY.
- **Most restrictive wins**: REQUIRE_APPROVAL > DENY > ALLOW, resolved
  deterministically (rule order is stable; policy version is recorded).
- **No bypass**: hard gates (unknown/revoked subject, protected path, missing
  capability, disallowed Git/network/secret) cannot be overridden by an ALLOW
  rule. Destructive operations always REQUIRE_APPROVAL.
- **Deterministic**: rule ordering, scope inheritance (``scope`` → ``*``), and
  effect resolution are fully deterministic; decisions carry a policy version.
- **Auditable**: every evaluation is recorded in the policy audit trail and,
  when a telemetry collector is attached, emitted as a ``policy`` domain event
  (secret-free, consistent with Phase 31 trace/audit evidence).
- **Secret-free**: subjects are identifiers, not credentials; recounts never
  echo secrets; protected paths are matched by pattern only.

Runtime uses only the Python standard library.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from typing import Any, Iterable

from handoff_agent.telemetry import TelemetryDomain, TelemetryStatus, emit_event


class PolicyError(Exception):
    """Base error for policy failures."""


class PolicyDenied(PolicyError):
    """Raised when an action request is denied."""

    def __init__(self, subject_id: str, action: str, reasons: Iterable[str]) -> None:
        self.subject_id = subject_id
        self.action = action
        self.reasons = list(reasons)
        super().__init__(f"policy denied {action} for {subject_id}: {self.reasons[0] if self.reasons else 'denied by default'}")


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


# Most restrictive first — used to resolve conflicting rules deterministically.
_EFFECT_PRECEDENCE: tuple[Effect, ...] = (
    Effect.REQUIRE_APPROVAL,
    Effect.DENY,
    Effect.ALLOW,
)

DEFAULT_SCOPE = "*"


def _now_ms() -> int:
    return int(time.time() * 1000)



def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Request / decision model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionRequest:
    """A request to evaluate under policy.

    ``action`` uses a dotted category (e.g. ``filesystem.write``,
    ``git.push``, ``network.connect``, ``secret.read``, ``deployment.release``,
    ``tool.exec``, ``destructive.rm``, ``capability.checkpoint.create``).
    """

    subject_id: str
    action: str
    resource: str = ""
    scope: str = DEFAULT_SCOPE

    def canonical(self) -> str:
        return json.dumps(
            {
                "subject_id": self.subject_id,
                "action": self.action,
                "resource": self.resource,
                "scope": self.scope,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class ApprovalTicket:
    """A ticket for an action that requires approval."""

    ticket_id: str
    signature: str
    subject_id: str
    action: str
    resource: str
    scope: str
    created_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "subject_id": self.subject_id,
            "action": self.action,
            "resource": self.resource,
            "scope": self.scope,
            "created_at_ms": self.created_at_ms,
            "satisfied": False,
        }


@dataclass(frozen=True)
class ApprovalGrant:
    """Recorded satisfaction of an approval ticket."""

    ticket_id: str
    approver: str
    note: str
    granted_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "approver": self.approver,
            "note": self.note,
            "granted_at_ms": self.granted_at_ms,
        }


@dataclass(frozen=True)
class PolicyDecision:
    """Deterministic result of a policy evaluation."""

    allowed: bool
    effect: Effect
    action: str
    subject_id: str
    resource: str
    scope: str
    rule_id: str
    reasons: tuple[str, ...]
    policy_version: int
    approval_required: bool = False
    approval_ticket: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "effect": self.effect.value,
            "action": self.action,
            "subject_id": self.subject_id,
            "resource": self.resource,
            "scope": self.scope,
            "rule_id": self.rule_id,
            "reasons": list(self.reasons),
            "policy_version": self.policy_version,
            "approval_required": self.approval_required,
            "approval_ticket": self.approval_ticket,
        }

    def explain(self) -> list[str]:
        return list(self.reasons)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """A single declarative policy rule.

    ``action`` supports ``*`` wildcard segments: ``filesystem.*`` matches every
    ``filesystem.<sub>`` action. ``scope`` supports inheritance: a rule scoped
    to ``prod`` also applies to ``prod.eu`` but not to ``stage``; a rule scoped
    to ``*`` applies everywhere.
    """

    rule_id: str
    action: str
    effect: Effect
    scope: str = DEFAULT_SCOPE
    note: str = ""
    immutable: bool = False

    def matches(self, action: str, scope: str) -> bool:
        if not _pattern_matches(self.action, action):
            return False
        if self.scope == DEFAULT_SCOPE:
            return True
        if scope == self.scope:
            return True
        return scope.startswith(self.scope + ".")


def _pattern_matches(pattern: str, value: str) -> bool:
    """Match a dotted action pattern (``*`` per segment) against a value."""
    if pattern == value:
        return True
    p_parts = pattern.split(".")
    v_parts = value.split(".")
    if pattern.endswith(".*") and len(p_parts) - 1 <= len(v_parts):
        return p_parts[:-1] == v_parts[: len(p_parts) - 1]
    return fnmatch(value, pattern)


# ---------------------------------------------------------------------------
# Subject profiles
# ---------------------------------------------------------------------------


@dataclass
class SubjectProfile:
    subject_id: str
    kind: str  # agent | device | endpoint | workflow | tool | human
    trust_level: int = 0
    capabilities: set[str] = field(default_factory=set)
    revoked: bool = False
    registered_at_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "kind": self.kind,
            "trust_level": self.trust_level,
            "capabilities": sorted(self.capabilities),
            "revoked": self.revoked,
        }


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """Deterministic, deny-by-default policy engine."""

    def __init__(
        self,
        *,
        policy_version: int = 1,
        tracer: Any | None = None,
        default_scope: str = DEFAULT_SCOPE,
    ) -> None:
        if policy_version < 1:
            raise PolicyError("policy_version must be positive")
        self.policy_version = policy_version
        self.default_scope = default_scope
        self._tracer = tracer
        self._rules: dict[str, PolicyRule] = {}
        self._subjects: dict[str, SubjectProfile] = {}
        self._protected_paths: list[str] = []
        self._path_grants: dict[str, list[str]] = {}  # subject_id -> [patterns]
        self._git_allowed: set[str] = set()
        self._network_allowed: list[str] = []
        self._secret_granted: set[str] = set()
        self._destructive_actions: set[str] = set()
        self._approval_tickets: dict[str, ApprovalTicket] = {}
        self._approval_grants: dict[str, ApprovalGrant] = {}
        self._audit: list[dict[str, Any]] = []

    # -- subject management ------------------------------------------------

    def register_subject(
        self,
        subject_id: str,
        kind: str,
        *,
        trust_level: int = 0,
        capabilities: Iterable[str] = (),
    ) -> None:
        if not subject_id or not kind:
            raise PolicyError("subject_id and kind are required")
        self._subjects[subject_id] = SubjectProfile(
            subject_id=subject_id,
            kind=kind,
            trust_level=max(0, int(trust_level)),
            capabilities=set(capabilities),
            registered_at_ms=_now_ms(),
        )

    def revoke_subject(self, subject_id: str) -> None:
        profile = self._subjects.get(subject_id)
        if profile is not None:
            profile.revoked = True

    def grant_capability(self, subject_id: str, capability: str) -> None:
        profile = self._require_subject(subject_id)
        profile.capabilities.add(capability)

    def revoke_capability(self, subject_id: str, capability: str) -> None:
        profile = self._require_subject(subject_id)
        profile.capabilities.discard(capability)

    def subject(self, subject_id: str) -> SubjectProfile | None:
        return self._subjects.get(subject_id)

    # -- rules ---------------------------------------------------------------

    def add_rule(
        self,
        action: str,
        effect: Effect,
        *,
        scope: str = DEFAULT_SCOPE,
        note: str = "",
        immutable: bool = False,
    ) -> str:
        rule_id = new_id("rule")
        self._rules[rule_id] = PolicyRule(
            rule_id=rule_id,
            action=action,
            effect=effect,
            scope=scope,
            note=note,
            immutable=immutable,
        )
        return rule_id

    def remove_rule(self, rule_id: str) -> None:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise PolicyError(f"unknown rule {rule_id}")
        if rule.immutable:
            raise PolicyError(f"rule {rule_id} is immutable")
        del self._rules[rule_id]

    # -- scope-based defaults -----------------------------------------------

    def protect_paths(self, *patterns: str) -> None:
        for pattern in patterns:
            if pattern not in self._protected_paths:
                self._protected_paths.append(pattern)

    def grant_path(self, subject_id: str, *patterns: str) -> None:
        self._require_subject(subject_id)
        self._path_grants.setdefault(subject_id, []).extend(patterns)

    def allow_git(self, *command_names: str) -> None:
        self._git_allowed.update(command_names)

    def allow_network(self, *host_patterns: str) -> None:
        self._network_allowed.extend(host_patterns)

    def grant_secret(self, subject_id: str) -> None:
        self._require_subject(subject_id)
        self._secret_granted.add(subject_id)

    def require_approval_for_destructive(self, *action_patterns: str) -> None:
        self._destructive_actions.update(action_patterns)

    # -- approval lifecycle ---------------------------------------------------

    def request_approval(self, request: ActionRequest) -> ApprovalTicket:
        """Create an approval ticket for a request that requires approval."""
        signature = hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()
        ticket = ApprovalTicket(
            ticket_id=new_id("appr"),
            signature=signature,
            subject_id=request.subject_id,
            action=request.action,
            resource=request.resource,
            scope=request.scope,
            created_at_ms=_now_ms(),
        )
        self._approval_tickets[ticket.ticket_id] = ticket
        return ticket

    def grant_approval(
        self,
        ticket: ApprovalTicket,
        *,
        approver: str,
        note: str = "",
    ) -> ApprovalGrant:
        """Record approval for the ticket; returns the grant."""
        if ticket.ticket_id in self._approval_grants:
            raise PolicyError(f"ticket {ticket.ticket_id} already approved")
        grant = ApprovalGrant(
            ticket_id=ticket.ticket_id,
            approver=approver,
            note=note,
            granted_at_ms=_now_ms(),
        )
        self._approval_grants[ticket.ticket_id] = grant
        return grant

    def is_approved(self, tick: ApprovalTicket | str) -> bool:
        ticket_id = tick.ticket_id if isinstance(tick, ApprovalTicket) else tick
        return ticket_id in self._approval_grants

    # -- evaluation ------------------------------------------------------------

    def evaluate(
        self,
        request: ActionRequest,
        *,
        approval_ticket: ApprovalTicket | str | None = None,
    ) -> PolicyDecision:
        """Evaluate *request* deterministically; returns a decision."""
        reasons: list[str] = []
        effect = Effect.DENY  # deny by default
        rule_id = "default"
        gated = False  # True when a hard gate decided the effect

        # Layer 0: subject resolution (no bypass).
        profile = self._subjects.get(request.subject_id)
        if profile is None:
            return self._finalize(
                request, Effect.DENY, "subject-denied", ["unknown subject — deny by default"], approval_ticket
            )
        if profile.revoked:
            return self._finalize(
                request, Effect.DENY, "subject-revoked", ["subject is revoked"], approval_ticket
            )

        # Layer 1: capability-typed actions (no bypass).
        if request.action.startswith("capability."):
            cap = request.action.split(".", 1)[1]
            if cap not in profile.capabilities:
                reasons.append(f"subject lacks capability {cap!r}")
                effect = Effect.DENY
                rule_id = "capability"
            else:
                reasons.append(f"capability {cap!r} granted")
                effect = Effect.ALLOW
                rule_id = "capability"
            gated = True

        # Layer 2: protected path (no bypass for filesystem mutation).
        if request.action in ("filesystem.write", "filesystem.delete", "filesystem.rename"):
            if self._is_protected(request.resource):
                grants = self._path_grants.get(request.subject_id, [])
                if not any(_pattern_matches(g, request.resource) for g in grants):
                    reasons.append(
                        f"resource {request.resource!r} is a protected path without a grant"
                    )
                    effect = Effect.DENY
                    rule_id = "protected-path"
                else:
                    reasons.append("protected path write granted")
                    effect = Effect.ALLOW
                    rule_id = "protected-path"
            else:
                reasons.append(f"filesystem write to {request.resource!r} is within scope")
                effect = Effect.ALLOW
                rule_id = "filesystem"
            gated = True

        # Layer 3: Git operations (no bypass).
        if request.action.startswith("git."):
            command = request.resource or request.action.split(".", 1)[1]
            if command in self._git_allowed:
                reasons.append(f"git operation {command!r} allowed by allowlist")
                effect = Effect.ALLOW
                rule_id = "git"
            else:
                reasons.append(f"git operation {command!r} not in allowlist")
                effect = Effect.DENY
                rule_id = "git"
            gated = True

        # Layer 4: network allowlist (no bypass).
        if request.action.startswith("network."):
            host = request.resource
            if host and any(fnmatch(host, pattern) for pattern in self._network_allowed):
                reasons.append(f"network target {host!r} allowed by allowlist")
                effect = Effect.ALLOW
                rule_id = "network"
            else:
                reasons.append(f"network target {host!r} not in allowlist")
                effect = Effect.DENY
                rule_id = "network"
            gated = True

        # Layer 5: secret access (no bypass).
        if request.action == "secret.read":
            if request.subject_id in self._secret_granted:
                reasons.append("secret access granted")
                effect = Effect.ALLOW
                rule_id = "secret"
            else:
                reasons.append("secret access not granted")
                effect = Effect.DENY
                rule_id = "secret"
            gated = True

        # Layer 6: destructive operations always REQUIRE_APPROVAL.
        destructive_match = any(
            _pattern_matches(pattern, request.action)
            for pattern in self._destructive_actions
        )
        if destructive_match:
            reasons.append(f"destructive operation {request.action!r} requires approval")
            effect = Effect.REQUIRE_APPROVAL
            rule_id = "destructive"
            gated = True

        # Layer 7: explicit rules — resolved behind hard gates only when the
        # gate did not already produce a terminal DENY / REQUIRE_APPROVAL.
        matched: list[PolicyRule] = [
            rule
            for rule in self._rules.values()
            if rule.matches(request.action, request.scope)
        ]
        matched.sort(key=lambda r: r.rule_id)
        if not gated:
            # uncategorized action: matched rules resolve most-restrictive-wins;
            # no match at all falls back to deny-by-default.
            if matched:
                best = min(
                    matched,
                    key=lambda r: (_EFFECT_PRECEDENCE.index(r.effect), r.rule_id),
                )
                effect = best.effect
                rule_id = best.rule_id
                for rule in matched:
                    explanation = f"rule {rule.rule_id} ({rule.effect.value})"
                    if rule.note:
                        explanation += f": {rule.note}"
                    reasons.append(explanation)
            else:
                reasons.append("no explicit allow — deny by default")
        elif effect in (Effect.DENY, Effect.REQUIRE_APPROVAL):
            for rule in matched:
                reasons.append(
                    f"rule {rule.rule_id} ({rule.effect.value})"
                    + (f": {rule.note}" if rule.note else "")
                    + " — not applicable (hard gate already decided)"
                )
        else:
            # gate allowed; explicit rules may still be more restrictive.
            for rule in matched:
                explanation = f"rule {rule.rule_id} ({rule.effect.value})"
                if rule.note:
                    explanation += f": {rule.note}"
                reasons.append(explanation)
                idx = _EFFECT_PRECEDENCE.index(rule.effect)
                if idx < _EFFECT_PRECEDENCE.index(effect):
                    effect = rule.effect
                    rule_id = rule.rule_id

        # approved ticket check for REQUIRE_APPROVAL
        approved = False
        if effect == Effect.REQUIRE_APPROVAL:
            ticket = self._resolve_ticket(approval_ticket)
            if ticket is None:
                reasons.append("approval required")
            elif not self.is_approved(ticket):
                reasons.append("approval required")
            elif ticket.signature != hashlib.sha256(request.canonical().encode("utf-8")).hexdigest():
                reasons.append("approval ticket does not match this request")
            else:
                approved = True
                reasons.append("approval ticket satisfied")

        return self._finalize(
            request,
            effect,
            rule_id,
            reasons,
            approval_ticket,
            approved=approved,
        )

    def _resolve_ticket(
        self, approval_ticket: ApprovalTicket | str | None
    ) -> ApprovalTicket | None:
        if approval_ticket is None:
            return None
        if isinstance(approval_ticket, ApprovalTicket):
            return self._approval_tickets.get(approval_ticket.ticket_id)
        return self._approval_tickets.get(approval_ticket)

    def _finalize(
        self,
        request: ActionRequest,
        effect: Effect,
        rule_id: str,
        reasons: list[str],
        approval_ticket: ApprovalTicket | str | None,
        approved: bool = False,
    ) -> PolicyDecision:
        resolved_effect = effect
        if effect == Effect.REQUIRE_APPROVAL and approved:
            resolved_effect = Effect.ALLOW
        allowed = resolved_effect == Effect.ALLOW
        ticket_id = approval_ticket.ticket_id if isinstance(approval_ticket, ApprovalTicket) else (approval_ticket or "")
        decision = PolicyDecision(
            allowed=allowed,
            effect=resolved_effect,
            action=request.action,
            subject_id=request.subject_id,
            resource=request.resource,
            scope=request.scope,
            rule_id=rule_id,
            reasons=tuple(reasons),
            policy_version=self.policy_version,
            approval_required=effect == Effect.REQUIRE_APPROVAL and not approved,
            approval_ticket=ticket_id,
        )
        self._record_decision(decision)
        return decision

    # -- policy explanation & audit --------------------------------------------

    def explain(self, decision: PolicyDecision) -> list[str]:
        return decision.explain()

    def audit_trail(self) -> list[dict[str, Any]]:
        return list(self._audit)

    def policy_report(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "default_scope": self.default_scope,
            "rule_count": len(self._rules),
            "subject_count": len(self._subjects),
            "protected_path_count": len(self._protected_paths),
            "git_allowlist": sorted(self._git_allowed),
            "network_allowlist": self._network_allowed,
            "destructive_actions": sorted(self._destructive_actions),
            "audit_count": len(self._audit),
        }

    # -- integration: strict helper -----------------------------------------------

    def assert_allowed(self, request: ActionRequest, *, approval_ticket: Any = None) -> PolicyDecision:
        """Evaluate and raise ``PolicyDenied`` if not allowed."""
        decision = self.evaluate(request, approval_ticket=approval_ticket)
        if not decision.allowed:
            raise PolicyDenied(request.subject_id, request.action, decision.reasons)
        return decision

    # -- internal helpers --------------------------------------------------------

    def _require_subject(self, subject_id: str) -> SubjectProfile:
        profile = self._subjects.get(subject_id)
        if profile is None:
            raise PolicyError(f"unknown subject {subject_id}")
        return profile

    def _is_protected(self, resource: str) -> bool:
        if not resource:
            return False
        path = resource.replace("\\", "/")
        if any(_pattern_matches(pattern, path) for pattern in self._protected_paths):
            return True
        return any(
            part in path.split("/")
            for part in (".git",)
        )

    def _record_decision(self, decision: PolicyDecision) -> None:
        entry = {
            "timestamp_ms": _now_ms(),
            **decision.to_dict(),
        }
        self._audit.append(entry)
        if self._tracer is not None:
            emit_event(
                self._tracer,
                domain=TelemetryDomain.POLICY.value,
                operation=f"policy.{decision.effect.value}",
                status=(
                    TelemetryStatus.OK.value
                    if decision.allowed
                    else TelemetryStatus.BLOCKED.value
                ),
                resource=decision.action,
                actor=decision.subject_id,
                metadata={
                    "action": decision.action,
                    "resource": decision.resource,
                    "scope": decision.scope,
                    "rule_id": decision.rule_id,
                    "policy_version": decision.policy_version,
                    "approval_required": decision.approval_required,
                },
            )


def _exact_destructive_override(action: str) -> str:
    """Return a sentinel so destructive always routes through the gate."""
    return f"{action}.override"


def protect_default_sensitive_files(engine: PolicyEngine) -> None:
    """Protect a conservative default set of sensitive files/paths."""
    engine.protect_paths(
        ".git/**",
        "**/.env",
        "**/.env.*",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/id_rsa",
        "**/id_ed25519",
        "**/credentials*",
        "**/secrets/*",
        "**/*secret*",
        "**/*token*",
    )


def allow_default_git_operations(engine: PolicyEngine) -> None:
    """Allow only read-only Git operations by default; deny everything else."""
    engine.allow_git(
        "git.status",
        "git.diff",
        "git.log",
        "git.show",
        "git.branch",
        "git.rev-parse",
        "git.ls-files",
    )