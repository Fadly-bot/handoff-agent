"""Tests for handoff_agent.certification (Phase 36 — Production Certification)."""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from handoff_agent import certification as cert
from handoff_agent.certification import (
    GateResult,
    certification_summary,
    run_production_certification,
)


# ---------------------------------------------------------------------------
# GateResult dataclass
# ---------------------------------------------------------------------------

class TestGateResult:
    def test_default_ok_is_false(self) -> None:
        gr = GateResult("test_gate")
        assert gr.ok is False

    def test_to_dict_keys(self) -> None:
        gr = GateResult("sample", ok=True, evidence=["e1"], notes=["n1"], blocker="b1")
        d = gr.to_dict()
        assert d["gate"] == "sample"
        assert d["ok"] is True
        assert d["evidence"] == ["e1"]
        assert d["notes"] == ["n1"]
        assert d["blocker"] == "b1"

    def test_to_dict_default_lists(self) -> None:
        gr = GateResult("x")
        d = gr.to_dict()
        assert d["evidence"] == []
        assert d["notes"] == []
        assert d["blocker"] == ""


# ---------------------------------------------------------------------------
# Individual gates
# ---------------------------------------------------------------------------

class TestIndividualGates:
    def test_freeze(self) -> None:
        g = cert.gate_freeze()
        assert g.ok, f"gate_freeze failed: {g.blocker}"

    def test_architecture(self) -> None:
        g = cert.gate_architecture()
        assert g.ok, f"gate_architecture failed: {g.blocker}"

    def test_security_language(self) -> None:
        g = cert.gate_security_language()
        assert g.ok, f"gate_security_language failed: {g.blocker}"

    def test_secret_leak(self) -> None:
        g = cert.gate_secret_leak()
        assert g.ok, f"gate_secret_leak failed: {g.blocker}"

    def test_policy_approval(self) -> None:
        g = cert.gate_policy_approval()
        assert g.ok, f"gate_policy_approval failed: {g.blocker}"

    def test_boundaries(self) -> None:
        g = cert.gate_boundaries()
        assert g.ok, f"gate_boundaries failed: {g.blocker}"

    def test_reliability(self) -> None:
        g = cert.gate_reliability()
        assert g.ok, f"gate_reliability failed: {g.blocker}"

    def test_adapters_and_tools(self) -> None:
        g = cert.gate_adapters_and_tools()
        assert g.ok, f"gate_adapters_and_tools failed: {g.blocker}"

    def test_messaging_workflow_remote_sync(self) -> None:
        g = cert.gate_messaging_workflow_remote_sync()
        assert g.ok, f"gate_messaging_workflow_remote_sync failed: {g.blocker}"

    def test_scenarios(self) -> None:
        g = cert.gate_scenarios()
        assert g.ok, f"gate_scenarios failed: {g.blocker}"

    def test_install_upgrade(self) -> None:
        g = cert.gate_install_upgrade()
        assert g.ok, f"gate_install_upgrade failed: {g.blocker}"

    def test_documentation(self) -> None:
        g = cert.gate_documentation()
        assert g.ok, f"gate_documentation failed: {g.blocker}"


# ---------------------------------------------------------------------------
# Secret-scan exemption
# ---------------------------------------------------------------------------

class TestSecretScan:
    def test_probe_value_exempt(self) -> None:
        line = '    paths.write_text(\'access_token = "thisisasecretvalue1"\\n\', encoding="utf-8")'
        leaks, tokens = cert._secret_scan_text(line)
        assert leaks == []
        assert tokens == []

    def test_real_assignment_flagged(self) -> None:
        line = 'api_key = "supersecretvalue123"'
        leaks, tokens = cert._secret_scan_text(line)
        assert len(leaks) == 1

    def test_sk_token_with_braces_exempt(self) -> None:
        text = 'PATTERN = r"sk-[A-Za-z0-9\\[\\]{}, ]+"'
        leaks, tokens = cert._secret_scan_text(text)
        assert tokens == []

    def test_sk_token_concrete_flagged(self) -> None:
        text = 'token = "sk-proj-abc123def456ghi789"'
        leaks, tokens = cert._secret_scan_text(text)
        assert len(tokens) == 1


# ---------------------------------------------------------------------------
# Security-language scan
# ---------------------------------------------------------------------------

class TestSecurityLanguage:
    def test_git_helper_subprocess_not_flagged(self) -> None:
        """git_helper.py subprocess import should not be flagged."""
        src = cert._iter_source_files()
        git_helper = [p for p in src if p.name == "git_helper.py"]
        assert git_helper, "git_helper.py not found in source"
        from handoff_agent.certification import _stdlib_only_report
        assert _stdlib_only_report(git_helper[0].read_text()) == []

    def test_sandbox_subprocess_not_flagged(self) -> None:
        src = cert._iter_source_files()
        sandbox = [p for p in src if p.name == "sandbox.py"]
        assert sandbox, "sandbox.py not found in source"
        from handoff_agent.certification import _stdlib_only_report
        assert _stdlib_only_report(sandbox[0].read_text()) == []


# ---------------------------------------------------------------------------
# Git boundary
# ---------------------------------------------------------------------------

class TestGitBoundary:
    def test_forbidden_command_raises(self) -> None:
        from handoff_agent.git_helper import FORBIDDEN_GIT_COMMANDS, GitRunner
        runner = GitRunner()
        forbidden = sorted(FORBIDDEN_GIT_COMMANDS)[0]
        with pytest.raises(Exception):
            runner._resolve_cmd([forbidden])

    def test_allowed_command_resolves(self) -> None:
        from handoff_agent.git_helper import ALLOWED_GIT_COMMANDS, GitRunner
        runner = GitRunner()
        allowed = sorted(ALLOWED_GIT_COMMANDS)[0]
        result = runner._resolve_cmd([allowed])
        assert isinstance(result, tuple)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class TestOrchestration:
    def test_full_certification_production_ready(self) -> None:
        rep = run_production_certification()
        assert rep["verdict"] == "production_ready"
        assert rep["passed_gates"] == rep["gate_count"]
        assert len(rep["unresolved_blockers"]) == 0

    def test_gate_count(self) -> None:
        rep = run_production_certification()
        assert rep["gate_count"] == 12

    def test_risk_register_length(self) -> None:
        rep = run_production_certification()
        assert len(rep["risk_register"]) == 12

    def test_evidence_by_gate_keys(self) -> None:
        rep = run_production_certification()
        expected = {
            "feature_freeze", "architecture_audit", "security_static",
            "secret_and_credential_audit", "identity_trust_policy_approval",
            "filesystem_process_git_network_ssrf", "queue_recovery_backup_dr",
            "adapter_tool_sandbox", "messaging_workflow_remote_sync",
            "multi_agent_multi_device_offline_failure",
            "install_uninstall_upgrade_compat",
            "documentation_release_artifacts",
        }
        assert set(rep["evidence_by_gate"].keys()) == expected

    def test_release_recommendation(self) -> None:
        rep = run_production_certification()
        assert rep["release_recommendation"] == "APPROVED for production use"


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

class TestCertificationSummary:
    def test_summary_secret_free(self) -> None:
        rep = run_production_certification()
        text = certification_summary(rep)
        assert "thisisasecret" not in text
        assert "sk-" not in text
        assert "api_key" not in text

    def test_summary_contains_verdict(self) -> None:
        rep = run_production_certification()
        text = certification_summary(rep)
        assert "production_ready" in text
        assert "APPROVED" in text


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------

class TestExceptionSafety:
    def test_crashed_gate_produces_blocked(self) -> None:
        def boom() -> GateResult:
            raise RuntimeError("gate boom")

        original = cert.GATE_FUNCTIONS
        cert.GATE_FUNCTIONS = [boom]
        try:
            rep = run_production_certification()
            assert rep["verdict"] == "blocked"
            assert any("crashed" in b for b in rep["unresolved_blockers"])
        finally:
            cert.GATE_FUNCTIONS = original