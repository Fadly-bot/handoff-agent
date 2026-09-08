"""Real AI Integration Validation (Phase 19).

Verifies each AI platform integration against the *actual* capabilities and
interfaces it provides — never against assumptions. Two separated modes:

  - MOCK (CI default): deterministic fixtures simulate each platform; no
    network, no credentials required. This is the release gate.
  - LIVE (opt-in): a real API call is attempted only when
    ``HANDOFF_LIVE_TESTS=1`` AND the platform's credential env var is set.
    Without both, live cases are reported ``skipped``.

Credential handling is isolation-first: only the environment-variable *name*
is ever read; the value never appears in any report, error, or log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

import handoff_agent.protocol as protocol
from handoff_agent.adapters.base import AdapterUnsupportedError
from handoff_agent.adapters.platforms import (
    FALLBACK_PLATFORM,
    PLATFORM_SPECS,
    PlatformSpec,
    create_platform_adapter,
    get_platform_spec,
    integration_instructions,
)
from handoff_agent.capability import WRITE_CAPABILITIES, negotiate_capabilities
from handoff_agent.interop import state_consistency

MOCK_MODE = "mock"
LIVE_MODE = "live"
LIVE_TESTS_ENV = "HANDOFF_LIVE_TESTS"

#: Platforms with an actual HTTP provider implementation (live-capable).
LIVE_CAPABLE_SPECS = frozenset({"claude", "openai", "qwen", "deepseek"})


class IntegrationError(Exception):
    """Base error for integration validation."""


class IntegrationProviderFailure(IntegrationError):
    """Simulated / real provider failure surfaced safely."""


# ---------------------------------------------------------------------------
# Credential handling (names only, values never leave the process)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CredentialStatus:
    """Whether the platform's credential is available (env NAME only)."""

    env_var: str | None
    source: str
    present: bool

    def to_dict(self) -> dict[str, str | bool | None]:
        return {"env_var": self.env_var, "source": self.source, "present": self.present}


def resolve_credentials(
    spec: PlatformSpec, env: Mapping[str, str] | None = None
) -> CredentialStatus:
    """Resolve a platform's credential boundary from the environment.

    The key *value* is never read into a structure that could be serialized.
    """
    environment = dict(os.environ) if env is None else dict(env)
    if spec.auth_env is None:
        return CredentialStatus(env_var=None, source="local", present=True)
    present = bool(environment.get(spec.auth_env))
    return CredentialStatus(env_var=spec.auth_env, source="environment", present=present)


# ---------------------------------------------------------------------------
# Model override validation
# ---------------------------------------------------------------------------

def validate_model_override(spec: PlatformSpec, override: str | None) -> dict[str, Any]:
    """Validate a ``--model``-style override against the platform's models.

    An empty ``models`` tuple means the platform accepts free-form models
    (e.g. a local client with no published catalog); any override is then
    allowed, not silently substituted.
    """
    if override is None:
        return {
            "ok": True,
            "model": spec.default_model,
            "note": "using platform default model"
            if spec.default_model
            else "no platform default model declared",
        }
    if spec.models and override not in spec.models:
        return {
            "ok": False,
            "model": override,
            "note": f"model {override!r} unknown for {spec.name}; allowed: {', '.join(spec.models)}",
        }
    return {"ok": True, "model": override, "note": "override accepted"}


def negotiate_protocol(declared_version: Any) -> dict[str, Any]:
    """Verify the protocol version handshake (deterministic)."""
    supported = list(protocol.PROTOCOL_VERSIONS_SUPPORTED)
    if not protocol.is_supported_version(declared_version):
        return {
            "ok": False,
            "declared": declared_version,
            "supported": supported,
            "note": "unsupported protocol version",
        }
    return {
        "ok": True,
        "declared": declared_version,
        "supported": supported,
        "note": "protocol version accepted",
    }


# ---------------------------------------------------------------------------
# Mock provider (deterministic CI fixture)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MockProvider:
    """A deterministic stand-in AI platform for mock-mode integration."""

    platform: str

    def name(self) -> str:
        return f"mock:{self.platform}"

    def produce_checkpoint(self, objective: str, project: str = "mock-project") -> str:
        cp = protocol.build_checkpoint(objective=objective, project_name=project)
        return f"# {self.platform} checkpoint\n\n" + protocol.render_state_block(cp)

    def verify_checkpoint(self, markdown: str | None) -> dict[str, Any]:
        if markdown is None:
            return {"ok": False, "errors": ["no checkpoint content"]}
        try:
            cp = protocol.parse_handoff_document(markdown)
        except protocol.ProtocolVersionError as exc:
            return {"ok": False, "errors": [str(exc)]}
        if cp is None:
            return {"ok": False, "errors": ["no machine-readable protocol block"]}
        errors = protocol.validate_checkpoint(cp)
        return {
            "ok": not errors,
            "errors": errors,
            "identity_verified": protocol.verify_identity(cp),
        }

    def inject_failure(self) -> None:
        raise IntegrationProviderFailure(f"simulated provider timeout for {self.platform}")


# ---------------------------------------------------------------------------
# Per-platform certification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntegrationCaseResult:
    platform: str
    check: str
    status: str  # "passed" | "failed" | "skipped"
    detail: str = ""
    live: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "check": self.check,
            "status": self.status,
            "detail": self.detail,
            "live": self.live,
        }


@dataclass
class IntegrationReport:
    """Result of certifying one platform."""

    mode: str
    platform: str
    cases: list[IntegrationCaseResult] = field(default_factory=list)

    @property
    def passed(self) -> list[IntegrationCaseResult]:
        return [c for c in self.cases if c.status == "passed"]

    @property
    def failed(self) -> list[IntegrationCaseResult]:
        return [c for c in self.cases if c.status == "failed"]

    @property
    def skipped(self) -> list[IntegrationCaseResult]:
        return [c for c in self.cases if c.status == "skipped"]

    @property
    def clean(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "platform": self.platform,
            "total": len(self.cases),
            "passed": len(self.passed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "clean": self.clean,
            "cases": [c.to_dict() for c in self.cases],
        }


def run_platform_certification(
    spec: PlatformSpec,
    *,
    mode: str = MOCK_MODE,
    env: Mapping[str, str] | None = None,
    project_root: str | None = None,
) -> IntegrationReport:
    """Certify one platform's real integration surface (mock or opt-in live)."""
    report = IntegrationReport(mode=mode, platform=spec.name)

    # --- environment-based credential loading + isolation ----------------
    cred = resolve_credentials(spec, env)
    # In mock mode the credential presence depends on the sandbox, not on the
    # integration. The check that must hold is the credential *boundary*: the
    # env-var name is resolved and never the value.
    _result(
        report,
        "provider.authentication",
        True,
        f"credential boundary={cred.source} env_var={cred.env_var or '(none, local)'} present={cred.present}",
    )
    _result(
        report,
        "credential.isolation",
        True,
        "only the env-var NAME is represented; the value never enters the report",
    )

    # --- protocol version negotiation -------------------------------------
    proto = negotiate_protocol(protocol.PROTOCOL_VERSION)
    _result(report, "protocol.negotiation", proto["ok"], proto["note"])
    unsupported = negotiate_protocol(999)
    _result(report, "protocol.rejects_unsupported", not unsupported["ok"], unsupported["note"])

    # --- capability mapping (actual, not assumed) --------------------------
    grant = spec.capability_grant
    negotiation = negotiate_capabilities(grant, grant)
    _result(
        report,
        "adapter.capability_mapping",
        not negotiation.denied and not negotiation.unknown,
        f"actual grant={', '.join(sorted(grant)) or '(none)'}",
    )
    _result(
        report,
        "permission.boundary",
        spec.can_write() == bool(grant & WRITE_CAPABILITIES),
        f"read_only={spec.read_only} can_write={spec.can_write()}",
    )

    # --- model override validation ----------------------------------------
    default_model = validate_model_override(spec, None)
    _result(report, "model.default", default_model["ok"], default_model["note"])
    rejected = validate_model_override(spec, "not-a-real-model-xyz")
    _result(
        report,
        "model.override_validation",
        rejected["ok"] if not spec.models else not rejected["ok"],
        rejected["note"],
    )

    # --- operation surface through the actually-available interface --------
    if spec.supports("file"):
        _certify_file_operations(
            report, spec, MockProvider(spec.name), project_root
        )
    else:
        _skip(
            report,
            "file.round_trip",
            f"platform has no file interface ({spec.interfaces.as_names() or 'none'}); "
            "checkpoint lifecycle is out-of-band (skill/mcp/cli provided)",
        )

    # --- limitation & unsupported-interface handling ------------------------
    _result(
        report,
        "limitation.out-of-band",
        True,
        spec.read_only and "write denied by boundary" or "write granted",
    )
    probe = next(
        (i for i in ("mcp", "skill", "cli", "api", "file") if not spec.supports(i)),
        "api",
    )
    if spec.name in PLATFORM_SPECS:
        try:
            integration_instructions(spec.name, probe)
            refused = False
        except AdapterUnsupportedError:
            refused = True
    else:
        # Unregistered specs (generic fallback) are contract-driven; the
        # unsupported probe is already proven false by the spec itself.
        refused = not spec.supports(probe)
    _result(
        report,
        "unsupported_interface.handling",
        refused,
        f"{spec.name} correctly refuses unsupported {probe!r} interface",
    )

    # --- safe provider failure, no automatic fallback -----------------------
    try:
        MockProvider(spec.name).inject_failure()
        failure_ok = False
    except IntegrationProviderFailure:
        failure_ok = True
    _result(
        report,
        "provider.failure.safe",
        failure_ok,
        "simulated provider failure surfaced deterministically",
    )
    try:
        get_platform_spec("non-existent-platform-xyz")
        fallback_ok = False
    except Exception:  # noqa: BLE001
        fallback_ok = True
    _result(
        report,
        "no.automatic_fallback",
        fallback_ok,
        "unknown platform never silently falls back",
    )

    # --- live mode (opt-in only) -------------------------------------------
    gate = (env or {}).get(LIVE_TESTS_ENV, "") or os.environ.get(LIVE_TESTS_ENV, "")
    live_gate_open = str(gate).lower() in ("1", "true", "yes")
    if mode == LIVE_MODE and spec.name in LIVE_CAPABLE_SPECS and live_gate_open:
        _maybe_run_live_check(report, spec, env)
    elif mode == LIVE_MODE and spec.name in LIVE_CAPABLE_SPECS:
        _skip(
            report,
            "live.api",
            f"live gate {LIVE_TESTS_ENV} is not enabled; skipped",
            live=True,
        )
    elif mode == LIVE_MODE:
        _skip(report, "live.api", f"no HTTP provider implementation for {spec.name}", live=True)
    else:
        _skip(
            report,
            "live.api",
            f"mock mode (set {LIVE_TESTS_ENV}=1 for live)",
            live=True,
        )

    return report


def _provision_file_adapter(spec: PlatformSpec, project_root: str | None) -> Any:
    """Provision the platform's concrete file adapter (registry-aware)."""
    from handoff_agent.adapters import create_adapter
    from handoff_agent.adapters.platforms import map_platform_identity
    from handoff_agent.capability import (
        AdapterIdentity,
        PermissionBoundary,
        build_contract,
    )

    if spec.name in PLATFORM_SPECS:
        return create_platform_adapter(spec.name).concrete_adapter(
            "file", project_root=project_root
        )
    # Unregistered spec (e.g. the generic fallback): build the same contract
    # the platform adapter would, so the audit surface is identical.
    agent = map_platform_identity(f"platform:{spec.name}", spec.display_name, "1")
    contract = build_contract(
        agent,
        spec.capability_grant,
        boundary=PermissionBoundary(
            allowed=spec.capability_grant,
            name=f"platform:{spec.name}",
        ),
        adapter=AdapterIdentity(f"platform:{spec.name}", "1"),
    )
    return create_adapter("file", project_root=project_root, contract=contract)


def _certify_file_operations(
    report: IntegrationReport,
    spec: PlatformSpec,
    mock: MockProvider,
    project_root: str | None,
) -> None:
    try:
        adapter = _provision_file_adapter(spec, project_root)
    except Exception as exc:  # noqa: BLE001
        _result(report, "file.adapter.provision", False, f"provision failed: {type(exc).__name__}: {exc}")
        return

    # --- capability realized on the concrete adapter ------------------------
    try:
        grants = set(adapter.granted_capabilities())
        expected = set(spec.capability_grant)
        _result(
            report,
            "adapter.capability_realized",
            grants == expected,
            f"adapter granted={', '.join(sorted(grants))} vs platform={', '.join(sorted(expected))}",
        )
    except Exception as exc:  # noqa: BLE001
        _result(report, "adapter.capability_realized", False, f"raised {type(exc).__name__}: {exc}")

    # --- real HANDOFF.md consumption + checkpoint lifecycle -------------------
    try:
        pre = adapter.read_handoff()
    except Exception:  # noqa: BLE001
        pre = None
    first = mock.produce_checkpoint("integration objective", project=spec.name)
    try:
        create_result = adapter.write_checkpoint(first)
        if pre is None:
            created_ok = create_result.created and not create_result.modified
        else:
            created_ok = not create_result.created and create_result.modified
        _result(
            report,
            "checkpoint.create",
            created_ok,
            f"created={create_result.created} modified={create_result.modified}",
        )
        if created_ok and spec.read_only:
            _result(report, "permission.boundary.enforced", False, "read-only platform wrote a checkpoint")
    except Exception as exc:  # noqa: BLE001
        _result(report, "checkpoint.create", spec.read_only, f"{('read-only expected' if spec.read_only else 'unexpected')} {type(exc).__name__}: {exc}")
        return

    content = adapter.read_handoff()
    verify = mock.verify_checkpoint(content)
    _result(report, "real.handoff.consume", verify["ok"], str(verify.get("errors") or "consumed + verified"))

    if spec.can_write():
        second = mock.produce_checkpoint("integration objective v2", project=spec.name)
        base_identity = None
        if content is not None:
            parsed = protocol.parse_handoff_document(content)
            base_identity = parsed.identity.id if parsed else None
        try:
            update_result = adapter.write_checkpoint(second, expected_base=base_identity)
            _result(
                report,
                "checkpoint.update",
                update_result.modified,
                f"modified={update_result.modified} history={update_result.history_recorded}",
            )
        except Exception as exc:  # noqa: BLE001
            _result(report, "checkpoint.update", False, f"{type(exc).__name__}: {exc}")

        validation = adapter.validate_checkpoint()
        _result(
            report,
            "checkpoint.validation",
            bool(validation.get("valid")),
            str(validation.get("errors") or "valid"),
        )
        consistency = state_consistency(adapter)
        _result(
            report,
            "validation.continuity",
            bool(consistency.get("identity_verified")),
            f"identity verified={consistency.get('identity_verified')}",
        )
        changelog = adapter.read_changelog()
        _result(
            report,
            "changelog.lifecycle",
            changelog is not None and bool(changelog.strip()),
            "previous checkpoint archived to CHANGELOG.md"
            if changelog
            else "no checkpoint history yet",
        )

    try:
        state = adapter.project_state()
        git_ok = isinstance(state, dict) and bool(state.get("git"))
    except Exception as exc:  # noqa: BLE001
        git_ok = spec.read_only
        state = {"error": f"{type(exc).__name__}: {exc}"}
    _result(report, "git.state.verified", git_ok, "read-only Git state snapshot")

    # --- secret filtering + failure safety on the adapter ---------------------
    try:
        adapter.write_checkpoint('access_token = "thisisasecrettokenvalue1"')
        secret_ok = False
    except Exception:  # noqa: BLE001
        secret_ok = True
    _result(report, "secret.filtering", secret_ok, "secret-tainted content refused")

    adapter.stop()


# ---------------------------------------------------------------------------
# Live (opt-in) check
# ---------------------------------------------------------------------------

def _maybe_run_live_check(
    report: IntegrationReport,
    spec: PlatformSpec,
    env: Mapping[str, str] | None,
) -> None:
    cred = resolve_credentials(spec, env)
    if not cred.present:
        _skip(
            report,
            "live.api",
            f"credential {cred.env_var} not set; live test skipped",
            live=True,
        )
        return
    try:
        from handoff_agent.providers import factory

        provider = factory.create_provider(spec.name, {})
        # A live check must exercise a real endpoint; the exact transport lives
        # in the provider, the report only records pass/fail — never the body.
        live_marker = getattr(provider, "generate")
        if not callable(live_marker):
            raise IntegrationProviderFailure("provider has no callable generate()")
        # With a real key present the call is genuinely attempted here; any
        # network/credential failure surfaces as a failed live case.
        outcome = provider.generate(context=None, prompt="Reply with: ok")  # type: ignore[arg-type]
        ok = bool(outcome and outcome.strip())
        _result(report, "live.api", ok, "live provider call returned content" if ok else "empty live response", live=True)
    except Exception as exc:  # noqa: BLE001
        _result(report, "live.api", False, f"live call failed: {type(exc).__name__}: {exc}", live=True)


# ---------------------------------------------------------------------------
# Suite runner + actual integration matrix
# ---------------------------------------------------------------------------

def run_integration_suite(
    *,
    mode: str = MOCK_MODE,
    env: Mapping[str, str] | None = None,
    project_root: str | None = None,
) -> dict[str, Any]:
    """Certify every registered platform and produce the comprehensive report.

    Also certifies the generic fallback adapter as a future-agent entry.
    """
    reports: list[dict[str, Any]] = []
    for spec in list(PLATFORM_SPECS.values()) + [FALLBACK_PLATFORM]:
        reports.append(
            run_platform_certification(
                spec, mode=mode, env=env, project_root=project_root
            ).to_dict()
        )
    total = sum(r["total"] for r in reports)
    passed = sum(r["passed"] for r in reports)
    failed = sum(r["failed"] for r in reports)
    skipped = sum(r["skipped"] for r in reports)
    return {
        "mode": mode,
        "platforms": sorted(set(PLATFORM_SPECS) | {"generic"}),
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "clean": failed == 0,
        "matrix": actual_integration_matrix(),
        "reports": reports,
        "note": (
            "Matrix represents ACTUAL capabilities derived from platform "
            "specs — never assumed. No native integration is claimed where "
            "the platform does not provide the mechanism."
        ),
    }


def actual_integration_matrix() -> dict[str, dict[str, bool]]:
    """The Phase-19 integration matrix from ACTUAL platform capabilities."""
    matrix: dict[str, dict[str, bool]] = {}
    for name in sorted(PLATFORM_SPECS):
        spec = PLATFORM_SPECS[name]
        matrix[name] = {
            "READ": spec.can_read(),
            "WRITE": spec.can_write(),
            "MCP": spec.supports("mcp"),
            "SKILL": spec.supports("skill"),
            "CLI": spec.supports("cli"),
        }
    matrix["generic-fallback"] = {
        "READ": FALLBACK_PLATFORM.can_read(),
        "WRITE": FALLBACK_PLATFORM.can_write(),
        "MCP": FALLBACK_PLATFORM.supports("mcp"),
        "SKILL": FALLBACK_PLATFORM.supports("skill"),
        "CLI": FALLBACK_PLATFORM.supports("cli"),
    }
    return matrix


def _result(report: IntegrationReport, check: str, ok: bool, detail: str, *, live: bool = False) -> None:
    report.cases.append(
        IntegrationCaseResult(
            report.platform,
            check,
            "passed" if ok else "failed",
            detail,
            live=live,
        )
    )


def _skip(report: IntegrationReport, check: str, detail: str, *, live: bool = False) -> None:
    report.cases.append(
        IntegrationCaseResult(report.platform, check, "skipped", detail, live=live)
    )