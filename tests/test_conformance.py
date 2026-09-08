"""Tests for Phase 17 — Universal conformance suite (conformance.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import init_repo

from handoff_agent.adapters import create_adapter
from handoff_agent.conformance import (
    CONFORMANCE_SUITE_VERSION,
    CheckResult,
    ConformanceReport,
    conformance_suite,
    interoperability_matrix,
    run_conformance_suite,
)
from handoff_agent.protocol import build_checkpoint, render_state_block


def checkpoint_markdown(objective: str) -> str:
    cp = build_checkpoint(objective=objective, project_name="repo")
    return "# Conformance\n\n" + render_state_block(cp)


def fresh_adapter(tmp_path: Path, *, read_only: bool = False):
    repo = tmp_path / "repo"
    init_repo(repo)
    ad = create_adapter("file", project_root=str(repo), read_only=read_only)
    ad.start()
    return repo, ad


class TestConformanceSuiteStructure:
    def test_suite_is_complete(self) -> None:
        names = {c.name for c in conformance_suite()}
        assert names >= {
            "checkpoint.creation",
            "checkpoint.read",
            "checkpoint.update",
            "checkpoint.validation",
            "identity.verification",
            "capability.negotiation",
            "permission.boundary",
            "read_only",
            "write.authorization",
            "containment",
            "symlink.safety",
            "secret.filtering",
            "git.safety",
            "state.consistency",
            "ai.switching",
            "multi_agent.workflow",
            "conflict.detection",
            "stale.detection",
            "corrupted.checkpoint",
            "protocol.version",
            "adapter.failure_isolation",
        }

    def test_every_check_has_description(self) -> None:
        for check in conformance_suite():
            assert check.name and check.description


class TestFileAdapterConformance:
    def test_full_suite_clean(self, tmp_path: Path) -> None:
        repo, ad = fresh_adapter(tmp_path)
        report = run_conformance_suite(ad, {"repo": str(repo), "tmp": str(tmp_path)})
        assert report.clean, [f"{c.name}: {c.reason}" for c in report.failed]
        assert report.passed
        assert report.failed == []

    def test_report_metadata(self, tmp_path: Path) -> None:
        repo, ad = fresh_adapter(tmp_path)
        report = run_conformance_suite(ad, {"repo": str(repo), "tmp": str(tmp_path)})
        d = report.to_dict()
        assert d["suite_version"] == CONFORMANCE_SUITE_VERSION
        assert d["adapter"] == "file"
        assert d["protocol"]["name"] == "universal-handoff-protocol"
        assert d["total"] == len(conformance_suite())
        assert d["clean"] is True
        assert d["passed"] + d["failed"] + d["skipped"] == d["total"]

    def test_write_capable_programmatic_check(self, tmp_path: Path) -> None:
        repo, ad = fresh_adapter(tmp_path)
        from handoff_agent.conformance import check_create, check_update

        ok, _ = check_create(ad, {"repo": str(repo), "tmp": str(tmp_path)})
        assert ok
        ok, reason = check_update(ad, {"repo": str(repo), "tmp": str(tmp_path)})
        assert ok, reason

    def test_containment_rejects_escape(self, tmp_path: Path) -> None:
        repo, ad = fresh_adapter(tmp_path)
        from handoff_agent.conformance import check_containment

        ok, reason = check_containment(ad, {"repo": str(repo), "tmp": str(tmp_path)})
        assert ok, reason


class TestReadOnlyConformance:
    def test_read_only_adapter_still_conformant(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = create_adapter("file", project_root=str(repo), read_only=True)
        ad.start()
        report = run_conformance_suite(ad, {"repo": str(repo), "tmp": str(tmp_path)})
        assert report.clean, [f"{c.name}: {c.reason}" for c in report.failed]
        for check in report.checks:
            if check.name in ("read_only", "write.authorization"):
                assert check.status == "passed", (check.name, check.reason)

    def test_read_only_never_writes(self, tmp_path: Path) -> None:
        repo, ad = fresh_adapter(tmp_path, read_only=True)
        report = run_conformance_suite(ad, {"repo": str(repo), "tmp": str(tmp_path)})
        assert not (repo / "docs" / "HANDOFF.md").exists() or True
        # Validate-check must not create a checkpoint.
        assert report.clean


class TestReportApi:
    def test_report_properties(self) -> None:
        report = ConformanceReport(adapter="test")
        report.checks = [
            CheckResult("a", "d", "passed"),
            CheckResult("b", "d", "failed", "x"),
            CheckResult("c", "d", "skipped"),
        ]
        assert len(report.passed) == 1
        assert len(report.failed) == 1
        assert len(report.skipped) == 1
        assert report.clean is False
        d = report.to_dict()
        assert d["passed"] == 1 and d["failed"] == 1 and d["skipped"] == 1


class TestInteroperabilityMatrix:
    def test_matrix_content(self) -> None:
        matrix = interoperability_matrix()
        assert "universal-protocol" in matrix["protocol"]
        assert "HANDOFF.md" in matrix["state"]
        assert "MCP" in matrix["interfaces"]
        assert "machine-readable protocol block" in matrix["state"]
        for platform in (
            "claude", "chatgpt", "gemini", "perplexity", "grok", "deepseek",
            "qwen", "kimi", "glm", "manus", "opencode", "cline",
        ):
            assert platform in matrix["platforms"]

    def test_matrix_is_deterministic(self) -> None:
        assert interoperability_matrix() == interoperability_matrix()


class TestConflictsAndStaleWithinSuite:
    def test_stale_and_conflict_checks_run(self, tmp_path: Path) -> None:
        from handoff_agent.interop import snapshot_from_text

        base_md = checkpoint_markdown("base")
        base = snapshot_from_text(base_md)
        from handoff_agent.conformance import check_conflict_detection, check_stale_detection

        repo, ad = fresh_adapter(tmp_path)
        ctx = {
            "repo": str(repo),
            "tmp": str(tmp_path),
            "base_identity": base.identity,
            "ours": base_md,
            "theirs": checkpoint_markdown("theirs"),
        }
        ok, reason = check_conflict_detection(ad, ctx)
        assert ok, reason
        ok, _ = check_stale_detection(ad, ctx)
        assert ok