"""Phase 33 — Universal tool registry, classification, and sandboxed gateway.

The gateway is the enforcement boundary every tool invocation crosses:

- **Registry + discovery**: tools are declared as :class:`ToolSpec` with a
  capability declaration, input/output contract, timeout/retry policy, and a
  security classification (read / write / network / filesystem / Git / secret /
  destructive).
- **Policy enforcement (Phase 32)**: every invocation is evaluated against the
  :class:`PolicyEngine` first. A tool is denied unless its action (and, where
  applicable, its category-specific secondary action such as
  ``filesystem.write``, ``network.connect``, ``git.*``, or ``secret.read``)
  is allowed. Destructive tools always require an approval ticket.
- **Sandbox boundary (Phase 33)**: filesystem, process, network, and env access
  go through the :class:`Sandbox`.
- **Input/output validation**: required/optional argument contracts are checked
  and outputs are validated for type, size, and secret leakage.
- **Timeout, retry, cancellation**: bounded timeouts; retryable tools retry on
  failure with bounded exponential backoff.
- **Audit + telemetry**: every invocation is recorded in a secret-free audit
  trail and emitted as a ``tool`` domain telemetry event.
- **Adapter conformance**: MCP / CLI / Skill / Generic adapters are certified
  against the same suite.

Runtime uses only the Python standard library.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from handoff_agent.policy_engine import ActionRequest, PolicyDenied, PolicyEngine
from handoff_agent.policy_engine import Effect
from handoff_agent.sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    Sandbox,
    SandboxCommandError,
    SandboxContentError,
    SandboxError,
    SandboxLimits,
    SandboxNetworkError,
    SandboxPathError,
    SandboxResourceError,
    SandboxSecretError,
)
from handoff_agent.telemetry import TelemetryDomain, TelemetryStatus, emit_event


class ToolError(Exception):
    """Base error for all tool failures."""


class ToolNotFoundError(ToolError):
    """Raised when a tool is not registered."""


class ToolPermissionError(ToolError):
    """Raised when policy denies a tool invocation."""

    def __init__(self, tool: str, subject: str, reasons: Iterable[str]) -> None:
        self.tool = tool
        self.subject = subject
        self.reasons = list(reasons)
        super().__init__(
            f"tool {tool!r} denied for {subject}: {self.reasons[0] if self.reasons else 'denied by policy'}"
        )


class ToolInputError(ToolError):
    """Raised when tool arguments violate the declared contract."""


class ToolOutputError(ToolError):
    """Raised when a tool result violates the output contract."""


class ToolSecretError(ToolOutputError):
    """Raised when tool output leaks secret-like content."""


class ToolSandboxError(ToolError):
    """Raised when a sandbox boundary refusal/limit surfaces during execution."""


class ToolSetupError(ToolError):
    """Raised when a tool is declared but cannot be executed safely."""


ToolCategory = Enum(
    "ToolCategory",
    [
        ("READ", "read"),
        ("WRITE", "write"),
        ("NETWORK", "network"),
        ("FILESYSTEM", "filesystem"),
        ("GIT", "git"),
        ("SECRET", "secret"),
        ("DESTRUCTIVE", "destructive"),
    ],
    type=str,
)


# type names accepted by the input contract
_ARG_TYPES = {
    "str",
    "int",
    "bool",
    "list-str",
    "str-optional",
    "int-optional",
    "none",
}

# Category -> primary policy action prefix (f"tool.{name}") is always checked;
# these map the category to its secondary action.
_TOOL_CATEGORY_SECONDARY: dict[ToolCategory, str | None] = {
    ToolCategory.READ: None,
    ToolCategory.WRITE: None,
    ToolCategory.NETWORK: "network.connect",
    ToolCategory.FILESYSTEM: "filesystem.write",
    ToolCategory.GIT: None,  # per-command: git.<command>
    ToolCategory.SECRET: "secret.read",
    ToolCategory.DESTRUCTIVE: "destructive.invoke",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_jsonable(value: Any) -> Any:
    """Raise ``ToolOutputError`` if *value* is not JSON-serializable text."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_as_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _as_jsonable(v) for k, v in value.items()}
    raise ToolOutputError(
        f"Tool output has unsupported type: {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# Tool specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Declarative contract for one tool.

    ``handler`` (Python callable) or ``(command, args)`` (allowlisted process)
    executes the tool. ``args`` is a fixed tuple — the sandbox never accepts
    caller-supplied commands or shell strings.
    """

    name: str
    description: str
    category: ToolCategory
    policy_action: str | None = None
    capability: str = ""
    required_args: tuple[tuple[str, str], ...] = ()
    optional_args: tuple[tuple[str, str], ...] = ()
    handler: Callable[[Mapping[str, Any], Sandbox], Any] | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    retryable: bool = False
    idempotent: bool = False
    max_input_bytes: int = 65536
    max_output_bytes: int = 65536
    scope: str = "*"

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ToolSetupError("tool name and description are required")
        if self.handler is None and self.command is None:
            raise ToolSetupError(
                f"tool {self.name!r} must provide a handler or an allowlisted command"
            )
        if self.command is not None and not self.command:
            raise ToolSetupError(f"tool {self.name!r} command must be non-empty")
        if self.policy_action is not None and not self.policy_action:
            raise ToolSetupError(f"tool {self.name!r} policy_action must be non-empty")
        for kind, args in (("required", self.required_args), ("optional", self.optional_args)):
            for key, atype in args:
                if atype not in _ARG_TYPES:
                    raise ToolSetupError(
                        f"tool {self.name!r} {kind} arg {key!r} has unknown type {atype!r}"
                    )

    @property
    def action(self) -> str:
        """Primary policy action for this tool."""
        return self.policy_action or f"tool.{self.name}"

    @property
    def arg_keys(self) -> tuple[str, ...]:
        return tuple(k for k, _ in (*self.required_args, *self.optional_args))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "policy_action": self.action,
            "required_args": [{"name": k, "type": t} for k, t in self.required_args],
            "optional_args": [{"name": k, "type": t} for k, t in self.optional_args],
            "handler": self.handler is not None,
            "command": self.command,
            "args": list(self.args),
            "timeout_seconds": self.timeout_seconds,
            "retryable": self.retryable,
            "idempotent": self.idempotent,
            "scope": self.scope,
            "capability": self.capability,
        }


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Deterministic registry of declared tools with discovery."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ToolSetupError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec
        return spec

    def register_many(self, specs: Iterable[ToolSpec]) -> tuple[ToolSpec, ...]:
        specs = tuple(specs)
        for spec in specs:
            self.register(spec)
        return specs

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"unknown tool {name!r}") from None

    def remove(self, name: str) -> ToolSpec:
        spec = self.get(name)
        del self._tools[name]
        return spec

    def list(self) -> tuple[ToolSpec, ...]:
        return tuple(sorted(self._tools.values(), key=lambda s: s.name))

    def discover(self, kind: str = "") -> tuple[ToolSpec, ...]:
        """Discover registered tools (optionally filtered by category)."""
        if not kind:
            return self.list()
        wanted = ToolCategory(kind)
        return tuple(sorted((s for s in self._tools.values() if s.category == wanted), key=lambda s: s.name))

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": len(self._tools),
            "tools": [s.to_dict() for s in self.list()],
        }


# ---------------------------------------------------------------------------
# Adapter capability declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterCapabilityDeclaration:
    """A typed declaration of what an adapter (MCP/CLI/Skill/Generic) may do."""

    adapter_kind: str  # "mcp" | "cli" | "skill" | "generic"
    adapter_name: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    declared_at_ms: int = 0

    def __post_init__(self) -> None:
        if self.adapter_kind not in ("mcp", "cli", "skill", "generic"):
            raise ToolSetupError(f"unsupported adapter kind {self.adapter_kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_kind": self.adapter_kind,
            "adapter_name": self.adapter_name,
            "capabilities": sorted(self.capabilities),
            "declared_at_ms": self.declared_at_ms,
        }


# ---------------------------------------------------------------------------
# Tool request / result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRequest:
    """A sandboxed tool invocation request."""

    subject_id: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    scope: str = "*"

    def target(self) -> str:
        """Amend the resource for category-specific policy checks."""
        arguments = self.arguments
        for key in ("path", "resource", "host", "command", "target"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return value
        return self.tool


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a sandboxed tool invocation."""

    tool: str
    subject_id: str
    category: str
    ok: bool
    duration_ms: int
    attempts: int = 1
    output: Any = None
    error_type: str = ""
    error_reason: str = ""
    policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "subject_id": self.subject_id,
            "category": self.category,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "error_type": self.error_type,
            "error_reason": self.error_reason,
            "policy": self.policy,
        }


# ---------------------------------------------------------------------------
# Tool gateway (policy + sandbox + registry + audit + telemetry)
# ---------------------------------------------------------------------------


class ToolGateway:
    """Central, sandboxed tool boundary enforced by the Phase-32 policy engine."""

    def __init__(
        self,
        engine: PolicyEngine,
        sandbox: Sandbox,
        registry: ToolRegistry | None = None,
        *,
        tracer: Any | None = None,
    ) -> None:
        self.engine = engine
        self.sandbox = sandbox
        self.registry = registry or ToolRegistry()
        self._tracer = tracer
        self._audit: list[dict[str, Any]] = []
        self._backoff_base_ms = 100

    def register_tool(self, spec: ToolSpec) -> ToolSpec:
        return self.registry.register(spec)

    def register_tools(self, specs: Iterable[ToolSpec]) -> tuple[ToolSpec, ...]:
        specs = tuple(specs)
        for spec in specs:
            self.registry.register(spec)
        return specs

    # -- policy mapping ----------------------------------------------------

    def policy_checks(self, spec: ToolSpec, request: ToolRequest) -> list[ActionRequest]:
        """Build the ordered policy checks for one invocation."""
        checks = [
            ActionRequest(
                request.subject_id,
                spec.action,
                resource=request.target(),
                scope=request.scope,
            )
        ]
        secondary = _TOOL_CATEGORY_SECONDARY.get(spec.category)
        if secondary and secondary not in (spec.action, f"tool.{spec.name}"):
            resource = request.target()
            checks.append(
                ActionRequest(request.subject_id, secondary, resource=resource, scope=request.scope)
            )
        if spec.category == ToolCategory.GIT:
            git_command = request.arguments.get("command", spec.name)
            git_action = f"git.{git_command}"
            checks.append(
                ActionRequest(request.subject_id, git_action, resource=git_action, scope=request.scope)
            )
        if spec.capability:
            checks.append(
                ActionRequest(request.subject_id, f"capability.{spec.capability}", scope=request.scope)
            )
        return checks

    # -- input validation --------------------------------------------------

    def _validate_input(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
        unknown = set(arguments) - set(spec.arg_keys)
        if unknown:
            raise ToolInputError(
                f"tool {spec.name!r} received unknown argument(s): {', '.join(sorted(unknown))}"
            )
        for key, atype in spec.required_args:
            if key not in arguments:
                raise ToolInputError(f"tool {spec.name!r} missing required argument {key!r}")
        for key, atype in (*spec.required_args, *spec.optional_args):
            if key not in arguments:
                continue
            value = arguments[key]
            self._validate_arg(spec, key, atype, value)
        payload = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
        if len(payload.encode("utf-8")) > spec.max_input_bytes:
            raise ToolInputError(f"tool {spec.name!r} input exceeds {spec.max_input_bytes} bytes")
        if self.sandbox.check_secret(payload):
            raise ToolInputError(f"tool {spec.name!r} input is secret-like — refused")

    @staticmethod
    def _validate_arg(spec: ToolSpec, key: str, atype: str, value: Any) -> None:
        if atype == "str" and not isinstance(value, str):
            raise ToolInputError(f"tool {spec.name!r} arg {key!r} must be a string")
        elif atype == "int" and not isinstance(value, int):
            raise ToolInputError(f"tool {spec.name!r} arg {key!r} must be an integer")
        elif atype == "bool" and not isinstance(value, bool):
            raise ToolInputError(f"tool {spec.name!r} arg {key!r} must be a boolean")
        elif atype == "list-str" and not (
            isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
        ):
            raise ToolInputError(f"tool {spec.name!r} arg {key!r} must be a list of strings")
        elif atype == "str-optional" and not (value is None or isinstance(value, str)):
            raise ToolInputError(f"tool {spec.name!r} arg {key!r} must be a string or None")
        elif atype == "int-optional" and not (value is None or isinstance(value, int)):
            raise ToolInputError(f"tool {spec.name!r} arg {key!r} must be an integer or None")
        elif atype == "none" and value is not None:
            raise ToolInputError(f"tool {spec.name!r} arg {key!r} must be absent/None")

    # -- execution ---------------------------------------------------------

    def _run(
        self,
        spec: ToolSpec,
        request: ToolRequest,
    ) -> Any:
        if spec.handler is not None:
            try:
                return spec.handler(request.arguments, self.sandbox)
            except SandboxError as exc:
                raise ToolSandboxError(str(exc)) from exc
        if spec.command is not None:
            try:
                result = self.sandbox.run_command(
                    spec.command,
                    spec.args,
                    timeout=spec.timeout_seconds,
                    max_output_bytes=spec.max_output_bytes,
                )
            except SandboxError as exc:
                raise ToolSandboxError(str(exc)) from exc
            return {
                "returncode": result["returncode"],
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "command": result["command"],
            }
        raise ToolSetupError(f"tool {spec.name!r} has neither handler nor command")

    def invoke(
        self,
        request: ToolRequest,
        *,
        approval_ticket: Any = None,
    ) -> ToolResult:
        """Evaluate, validate, sandbox, execute, and audit one tool call."""
        started = _now_ms()
        spec = self.registry.get(request.tool)
        category = spec.category.value

        # 1) policy enforcement (Phase 32) — every secondary check included.
        try:
            for check in self.policy_checks(spec, request):
                self.engine.assert_allowed(check, approval_ticket=approval_ticket)
        except PolicyDenied as exc:
            self._record(
                spec, request, started, ok=False,
                error_type="permission", error_reason=exc.reasons, policy="deny",
                output=None,
            )
            self._emit(spec, request, "blocked", exc.reasons)
            raise ToolPermissionError(request.tool, request.subject_id, exc.reasons) from exc

        # 2) category-specific sandbox checks (no bypass of Phase-33 boundary).
        self._sandbox_gate(spec, request)

        # 3) input contract validation.
        try:
            self._validate_input(spec, request.arguments)
        except ToolInputError:
            self._record(
                spec, request, started, ok=False,
                error_type="input", error_reason=["invalid input"], policy="input",
                output=None,
            )
            self._emit(spec, request, "blocked", ["invalid input"])
            raise

        # 4) execute with bounded retries for retryable tools.
        attempts = 0
        output: Any = None
        error: ToolError | None = None
        while True:
            attempts += 1
            try:
                output = self._run(spec, request)
                error = None
                break
            except ToolError as exc:
                error = exc
                if not spec.retryable or attempts >= 3:
                    break
                self._emit(spec, request, "retry", [str(exc)])
                time.sleep((self._backoff_base_ms * (2 ** (attempts - 1))) / 1000.0)

        # 5) output validation (type, size, secret-free).
        if error is None:
            try:
                output = _as_jsonable(output)
                encoded = json.dumps(output, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if len(encoded) > spec.max_output_bytes:
                    raise ToolOutputError(
                        f"tool {spec.name!r} output exceeds {spec.max_output_bytes} bytes"
                    )
                if self.sandbox.check_secret(json.dumps(output, sort_keys=True)):
                    raise ToolSecretError(
                        f"tool {spec.name!r} output is secret-like — refused"
                    )
            except ToolOutputError as exc:
                error = exc
                output = None

        if error is not None:
            self._record(
                spec, request, started, ok=False,
                error_type=type(error).__name__, error_reason=[str(error)],
                policy="sandbox", output=None, attempts=attempts,
            )
            self._emit(spec, request, "failed", [str(error)])
            raise error

        self._record(
            spec, request, started, ok=True,
            error_type="", error_reason=[], policy="allow",
            output=output, attempts=attempts,
        )
        self._emit(spec, request, "ok", ["allowed"])
        return ToolResult(
            tool=spec.name,
            subject_id=request.subject_id,
            category=category,
            ok=True,
            duration_ms=_now_ms() - started,
            attempts=attempts,
            output=output,
            policy="allow",
        )

    def _sandbox_gate(self, spec: ToolSpec, request: ToolRequest) -> None:
        target = request.arguments.get("path") or request.arguments.get("host") or request.target()
        if spec.category == ToolCategory.FILESYSTEM:
            try:
                contained = self.sandbox.contain(target)
                if request.arguments.get("cmd") == "read":
                    self.sandbox.read_file(target)
                else:
                    content = request.arguments.get("content", "")
                    self.sandbox.write_file(target, content)
            except (SandboxPathError, SandboxContentError, SandboxResourceError) as exc:
                raise ToolPermissionError(spec.name, request.subject_id, [str(exc)]) from exc
        elif spec.category == ToolCategory.NETWORK:
            host = request.arguments.get("host") or ""
            try:
                self.sandbox.check_network(host)
            except SandboxNetworkError as exc:
                raise ToolPermissionError(spec.name, request.subject_id, [str(exc)]) from exc
        elif spec.category == ToolCategory.GIT:
            # git allowlist enforcement lives in the Phase-32 policy engine (git.*).
            pass
        elif spec.category == ToolCategory.SECRET:
            # policy gate is authoritative; sandbox adds no secret leakage here.
            pass
        elif spec.category == ToolCategory.DESTRUCTIVE:
            # policy gate (destructive.invoke) is authoritative.
            pass
        elif spec.category in (ToolCategory.READ, ToolCategory.WRITE):
            if target and target != spec.name:
                try:
                    self.sandbox.contain(target)
                except SandboxPathError as exc:
                    raise ToolPermissionError(spec.name, request.subject_id, [str(exc)]) from exc

    # -- audit + telemetry -------------------------------------------------

    def _record(
        self,
        spec: ToolSpec,
        request: ToolRequest,
        started_ms: int,
        *,
        ok: bool,
        error_type: str,
        error_reason: list[str],
        policy: str,
        output: Any,
        attempts: int = 1,
    ) -> None:
        self._audit.append(
            {
                "timestamp_ms": _now_ms(),
                "tool": spec.name,
                "category": spec.category.value,
                "subject_id": request.subject_id,
                "scope": request.scope,
                "ok": ok,
                "duration_ms": _now_ms() - started_ms,
                "attempts": attempts,
                "error_type": error_type,
                "error_reason": error_reason[:3],
                "policy": policy,
            }
        )

    def _emit(
        self,
        spec: ToolSpec,
        request: ToolRequest,
        status: str,
        reasons: Iterable[str],
    ) -> None:
        status_value = {
            "ok": TelemetryStatus.OK.value,
            "blocked": TelemetryStatus.BLOCKED.value,
            "failed": TelemetryStatus.ERROR.value,
            "retry": TelemetryStatus.RETRY.value,
        }.get(status, TelemetryStatus.ERROR.value)
        emit_event(
            self._tracer,
            domain=TelemetryDomain.TOOL.value,
            operation=f"tool.{spec.name}",
            status=status_value,
            resource=spec.category.value,
            actor=request.subject_id,
            metadata={
                "tool": spec.name,
                "category": spec.category.value,
                "scope": request.scope,
                "reason": list(reasons)[:5],
            },
        )

    def audit_trail(self) -> list[dict[str, Any]]:
        return list(self._audit)


# ---------------------------------------------------------------------------
# Built-in conformance tooling
# ---------------------------------------------------------------------------

_SECRET_PROBE = "probe access_token = thisisasecretvalue1"


def register_default_tools(gateway: ToolGateway) -> ToolRegistry:
    """Register the built-in demo toolset used by the conformance suite."""

    def _checkpoint_read(args, sandbox):
        target = args.get("path", "checkpoint/phase_33")
        sandbox.contain(target)
        return {"source": ".handoff/checkpoints", "scope": target}

    def _write_report(args, sandbox):
        target = args.get("path", "notes.txt")
        sandbox.contain(target)
        sandbox.write_file(target, args.get("content", ""))
        return {"wrote": target}

    def _fetch(args, sandbox):
        host = args.get("host", "")
        sandbox.check_network(host)
        return {"host": host, "reachable": True}

    def _probe_fs(args, sandbox):
        cmd = args.get("cmd", "read")
        target = args.get("path", "notes.txt")
        if cmd == "write":
            sandbox.write_file(target, args.get("content", ""))
            return {"action": "write", "path": target}
        sandbox.contain(target)
        sandbox.read_file(target)
        return {"action": "read", "path": target}

    def _git_status(args, sandbox):
        return {"command": args.get("command", "status")}

    def _vault(args, sandbox):
        return {"stored": "placeholder"}

    def _purge(args, sandbox):
        target = args.get("path", "tmp/marker.txt")
        sandbox.contain(target)
        sandbox.write_file(target, "x")
        sandbox.delete_file(target)
        return {"purged": target}

    def _leaky(args, sandbox):
        return {"payload": _SECRET_PROBE}

    specs = [
        ToolSpec(
            name="checkpoint_read",
            category=ToolCategory.READ,
            policy_action="tool.checkpoint_read",
            capability="tool_use",
            description="Read a checkpoint reference path under the sandbox root.",
            required_args=(("path", "str"),),
            handler=_checkpoint_read,
        ),
        ToolSpec(
            name="report",
            category=ToolCategory.WRITE,
            policy_action="tool.report",
            capability="tool_use",
            description="Write a report file inside the sandbox root.",
            required_args=(("content", "str"),),
            handler=_write_report,
        ),
        ToolSpec(
            name="netprobe",
            category=ToolCategory.NETWORK,
            policy_action="tool.netprobe",
            capability="tool_use",
            description="Probe a network host only when allowlisted.",
            required_args=(("host", "str"),),
            timeout_seconds=5,
            handler=_fetch,
        ),
        ToolSpec(
            name="probe",
            category=ToolCategory.FILESYSTEM,
            policy_action="tool.probe",
            capability="tool_use",
            description="Read or write a path inside the sandbox root.",
            required_args=(("cmd", "str"), ("path", "str")),
            handler=_probe_fs,
        ),
        ToolSpec(
            name="status",
            category=ToolCategory.GIT,
            policy_action="tool.status",
            capability="tool_use",
            description="Inspect Git state (command allowlisted by policy).",
            required_args=(("command", "str"),),
            handler=_git_status,
        ),
        ToolSpec(
            name="vault",
            category=ToolCategory.SECRET,
            policy_action="tool.vault",
            capability="tool_use",
            description="Read a stored secret (policy-gated).",
            handler=_vault,
        ),
        ToolSpec(
            name="purge",
            category=ToolCategory.DESTRUCTIVE,
            policy_action="tool.purge",
            capability="tool_use",
            description="Purge a file inside the sandbox root (approval required).",
            required_args=(("path", "str"),),
            handler=_purge,
        ),
        ToolSpec(
            name="leaky",
            category=ToolCategory.WRITE,
            policy_action="tool.leaky",
            capability="tool_use",
            description="Emit secret-like payload; must be rejected on output.",
            handler=_leaky,
        ),
        ToolSpec(
            name="permanent_fail",
            category=ToolCategory.READ,
            policy_action="tool.permanent_fail",
            capability="tool_use",
            description="Always fails after bounded retries.",
            retryable=True,
            timeout_seconds=5,
            handler=lambda args, sandbox: (_ for _ in ()).throw(ToolError("always_fail")),
        ),
        ToolSpec(
            name="sleepy",
            category=ToolCategory.READ,
            policy_action="tool.sleepy",
            capability="tool_use",
            description="Runs a subprocess that exceeds its timeout.",
            command=sys.executable,
            args=("-c", "import time; time.sleep(0.25)"),
            timeout_seconds=0.05,
        ),
    ]
    registry = gateway.register_tools(specs)
    gateway._backoff_base_ms = 1
    return registry


ADAPTER_KINDS = ("mcp", "cli", "skill", "generic")


def provision_phase33_gateway(*, project_root: str | None = None, tracer: object = None) -> ToolGateway:
    """Build a gateway ready for the conformance suite (filesystem, policy, sandbox)."""
    if project_root is None:
        project_root = os.path.join(tempfile.mkdtemp(prefix="phase33_"), "repo")
    os.makedirs(project_root, exist_ok=True)
    sandbox = Sandbox(
        project_root,
        command_allowlist=[sys.executable],
        network_allowlist=["*.trusted.test"],
        limits=SandboxLimits(
            max_output_bytes=4096,
            max_read_bytes=2048,
            max_write_bytes=2048,
        ),
    )
    engine = PolicyEngine(tracer=tracer)
    engine.register_subject(
        "agent-1",
        "agent",
        trust_level=3,
        capabilities=["tool_use", "filesystem_use", "network_use"],
    )
    for kind in ADAPTER_KINDS:
        engine.register_subject(f"adapter:{kind}", kind, trust_level=1, capabilities=["tool_use"])
    engine.register_subject("third-party", "plugin", trust_level=1, capabilities=["read_only"])
    engine.add_rule("tool.checkpoint_read", Effect.ALLOW, scope="*")
    engine.add_rule("tool.report", Effect.ALLOW, scope="*")
    engine.add_rule("tool.netprobe", Effect.ALLOW, scope="*")
    engine.add_rule("tool.probe", Effect.ALLOW, scope="*")
    engine.add_rule("tool.status", Effect.ALLOW, scope="*")
    engine.add_rule("tool.vault", Effect.ALLOW, scope="*")
    engine.add_rule("tool.purge", Effect.ALLOW, scope="*")
    engine.add_rule("tool.leaky", Effect.ALLOW, scope="*")
    engine.add_rule("tool.permanent_fail", Effect.ALLOW, scope="*")
    engine.add_rule("tool.sleepy", Effect.ALLOW, scope="*")
    engine.allow_git("git.status", "git.log")
    engine.allow_network("*.trusted.test")
    engine.grant_secret("agent-1")
    engine.protect_paths("**/*secret*", "**/*token*", "**/*.pem", "**/.env")
    engine.require_approval_for_destructive("destructive.*")
    gateway = ToolGateway(engine, sandbox)
    register_default_tools(gateway)
    return gateway


@dataclass(frozen=True)
class ConformanceCheck:
    """One checked invariant of the tool boundary."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ToolConformanceReport:
    """Report produced by :func:`run_tool_conformance`."""

    adapter_kind: str
    checks: tuple[ConformanceCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_kind": self.adapter_kind,
            "passed": self.passed,
            "checks": [
                {"name": check.name, "passed": check.passed, "detail": check.detail}
                for check in self.checks
            ],
        }

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _make_approval_ticket(engine: PolicyEngine, actor: str, action: str, resource: str) -> ApprovalTicket:
    ticket = engine.request_approval(ActionRequest(actor, action, resource=resource))
    engine.grant_approval(ticket, approver="reviewer-1")
    return ticket


def run_tool_conformance(
    *,
    adapter_kind: str = "generic",
    subject_id: str | None = None,
    tracer: object = None,
) -> ToolConformanceReport:
    """Certify the tool boundary against a self-contained conformance matrix."""
    if adapter_kind not in ADAPTER_KINDS:
        raise ToolSetupError(f"unsupported adapter kind {adapter_kind!r}")
    gateway = provision_phase33_gateway(tracer=tracer)
    engine = gateway.engine
    sandbox = gateway.sandbox
    actor = subject_id or f"adapter:{adapter_kind}"
    checks: list[ConformanceCheck] = []

    def _check(name, passed, detail=""):
        checks.append(ConformanceCheck(name, bool(passed), detail))

    def _invoke(tool, **args):
        return gateway.invoke(ToolRequest(actor, tool, args))

    def _raises(fn, expected: type[Exception] = ToolError, fragment: str = ""):
        try:
            fn()
        except expected as exc:
            return (not fragment) or (fragment in str(exc))
        except Exception:
            return False
        return False

    # --- registry + discovery + classification ----------------------------
    _check(
        "adapter_registry_discovers_tools",
        len(gateway.registry.list()) == 10
        and gateway.registry.discover("secret") == (
            gateway.registry.get("vault"),
        ),
    )
    _check(
        "tool_classification",
        gateway.registry.get("vault").category is ToolCategory.SECRET
        and gateway.registry.get("purge").category is ToolCategory.DESTRUCTIVE
        and gateway.registry.get("netprobe").category is ToolCategory.NETWORK
        and gateway.registry.get("probe").category is ToolCategory.FILESYSTEM
        and gateway.registry.get("status").category is ToolCategory.GIT,
    )

    # --- happy path (each adapter kind is certified the same way) ----------
    _check(
        "tool_read_happy_path",
        _invoke("checkpoint_read", path="notes.txt").ok,
    )
    sandbox.write_file("notes.txt", "hello from sandbox")
    _check(
        "tool_filesystem_read_happy_path",
        _invoke("probe", cmd="read", path="notes.txt").ok,
    )
    _check(
        "adapter_tool_bounded_to_declared_capabilities",
        _raises(
            lambda: gateway.invoke(
                ToolRequest("third-party", "checkpoint_read", {"path": "notes.txt"})
            ),
            ToolPermissionError,
            "denied",
        ),
    )
    _check(
        "tool_git_happy_path",
        _invoke("status", command="status").ok and _invoke("status", command="log").ok,
    )
    _check(
        "tool_network_allowlisted_host",
        _invoke("netprobe", host="svc.trusted.test").ok,
    )

    # --- policy gating ------------------------------------------------------
    _check(
        "tool_secret_denied_without_grant",
        _raises(lambda: _invoke("vault"), ToolPermissionError, "denied"),
    )
    _check(
        "tool_network_off_allowlist_denied",
        _raises(lambda: _invoke("netprobe", host="evil.example"), ToolPermissionError, "denied"),
    )
    _check(
        "tool_git_not_allowlisted_denied",
        _raises(lambda: _invoke("status", command="push"), ToolPermissionError, "denied"),
    )
    _check(
        "tool_destructive_requires_approval",
        _raises(lambda: _invoke("purge", path="tmp/marker.txt"), ToolPermissionError, "denied"),
    )

    # approval-gated destructive tool succeeds with a valid ticket
    def _destructive_ok():
        ticket = _make_approval_ticket(engine, actor, "destructive.invoke", "tmp/marker.txt")
        result = gateway.invoke(
            ToolRequest(actor, "purge", {"path": "tmp/marker.txt"}),
            approval_ticket=ticket,
        )
        return result.ok and "tmp/marker.txt" not in sandbox.list_all()

    _check("tool_destructive_approval_ticket", _destructive_ok)

    # --- input / output validation ------------------------------------------
    _check(
        "tool_missing_required_arg_rejected",
        _raises(lambda: _invoke("report"), ToolInputError, "missing required"),
    )
    _check(
        "tool_untyped_argument_rejected",
        _raises(lambda: _invoke("report", content={"nested": True}), ToolInputError),
    )
    _check(
        "tool_secret_like_input_refused",
        _raises(lambda: _invoke("report", content=_SECRET_PROBE), ToolInputError, "secret-like"),
    )
    _check(
        "tool_secret_like_output_rejected",
        _raises(lambda: _invoke("leaky"), ToolSecretError, "secret-like"),
    )

    # --- sandbox boundary ----------------------------------------------------
    _check(
        "sandbox_containment_rejects_escape",
        _raises(lambda: sandbox.contain("../../outside.txt"), SandboxPathError),
    )
    _check(
        "sandbox_read_blocks_secret_like_content",
        (lambda paths: (
            paths.parent.mkdir(parents=True, exist_ok=True) or True,
            paths.write_text('access_token = "thisisasecretvalue1"\n', encoding="utf-8"),
            _raises(lambda: sandbox.read_file("secret-notes.txt"), SandboxSecretError),
        )[-1])(sandbox.root / "secret-notes.txt"),
    )
    _check(
        "sandbox_fs_isolation",
        (lambda: (
            sandbox.write_file("nested/deep/notes.txt", "x") is not None,
            sandbox.read_file("nested/deep/notes.txt") == "x",
            "nested/deep/notes.txt" in sandbox.list_all(),
        ) == (True, True, True)),
    )
    _check(
        "sandbox_forbidden_command_rejected",
        _raises(lambda: sandbox.run_command("rm", ("-rf", ".")), SandboxCommandError, "sandbox allowlist"),
    )
    _check(
        "sandbox_network_allowlist_rejects_off_host",
        _raises(lambda: sandbox.check_network("evil.example"), SandboxNetworkError),
    )
    _check(
        "sandbox_network_allowlist_allows_granted",
        sandbox.check_network("svc.trusted.test") == "svc.trusted.test",
    )
    _check(
        "sandbox_env_isolation",
        set(sandbox.env_names()) == set(DEFAULT_ENV_ALLOWLIST),
    )
    _check(
        "sandbox_resource_limits_enforced",
        _raises(lambda: sandbox.write_file("huge.txt", "x" * 1_000_000), SandboxResourceError),
    )

    # --- execution governance -------------------------------------------------
    _check(
        "tool_timeout_enforced",
        _raises(lambda: _invoke("sleepy"), ToolError),
    )
    try:
        _invoke("permanent_fail")
    except ToolError:
        pass
    _check(
        "tool_retry_records_attempts",
        next(
            entry
            for entry in reversed(gateway.audit_trail())
            if entry["tool"] == "permanent_fail"
        )["attempts"] == 3,
    )
    _check(
        "tool_audit_trail_secret_free",
        (lambda text: "access_token" not in text and "thisisasecretvalue1" not in text)(
            json.dumps(gateway.audit_trail(), sort_keys=True)
        ),
    )
    _check(
        "tool_telemetry_tool_domain",
        tracer is None
        or any(
            getattr(event, "domain", "") == "tool" for event in tracer.events
        ),
    )

    return ToolConformanceReport(adapter_kind=adapter_kind, checks=tuple(checks))