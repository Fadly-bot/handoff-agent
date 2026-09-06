"""Tests for Phase 8 — Handoff lifecycle & checkpoint history.

Covers (per PHASE5A.md "PHASE 8 — Handoff Lifecycle"):

  - CHANGELOG.md: replaced checkpoints are archived into docs/CHANGELOG.md.
  - checkpoint lifecycle: docs/HANDOFF.md always holds the CURRENT checkpoint
    only; it is overwritten, never appended to.
  - history handling: CHANGELOG.md grows oldest-first, atomically, safely.
  - validation: lifecycle invariants, containment, secret protections, and
    failure behavior.
  - regression: Phase 7 guarantees preserved (dry-run zero writes, --commit
    stages ONLY docs/HANDOFF.md, unrelated user changes untouched).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import commit_all, git, init_repo, write_file


def _make_args(**kwargs) -> argparse.Namespace:
    defaults = {
        "path": None,
        "provider": None,
        "model": None,
        "output": None,
        "config": None,
        "dry_run": False,
        "commit": False,
        "command": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class MockProvider:
    """Mock provider returning configurable content and recording calls."""

    generate_calls: list = []

    def __init__(self, config: dict | None = None, *, text: str = "# Mock Handoff"):
        self.config = config or {}
        self._text = text

    def name(self) -> str:
        return "mock"

    def generate(self, context, prompt: str) -> str:
        MockProvider.generate_calls.append((context, prompt))
        return self._text

    def validate_config(self, config: dict) -> bool:
        return True

    def is_configured(self) -> bool:
        return True


class SecretProvider:
    """Provider whose output contains a fake secret (must be refused)."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def name(self) -> str:
        return "secret"

    def generate(self, context, prompt: str) -> str:
        return "api_key = \"sk-faketestsecret0123456789abcdef\""

    def validate_config(self, config: dict) -> bool:
        return True

    def is_configured(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clear_mock_calls():
    MockProvider.generate_calls.clear()
    yield
    MockProvider.generate_calls.clear()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    init_repo(r)
    write_file(r, "app.py", "print('hello')")
    write_file(r, "README.md", "# Test Project")
    commit_all(r, "initial")
    return r


def _provider_patch(provider_cls):
    return patch("handoff_agent.providers.factory._PROVIDERS", {"mock": provider_cls})


def _meta() -> dict:
    """Deterministic metadata for checkpoint history entries."""
    return {
        "project": "repo",
        "branch": "main",
        "commit": "c0ffee",
        "generated_at": "2026-09-06T00:00:00+00:00",
    }


# =====================================================================
# Checkpoint creation lifecycle
# =====================================================================

class TestCheckpointCreation:
    def test_first_checkpoint_creates_handoff_only(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        res = CheckpointManager(project_root=repo).write_checkpoint("# v1", _meta())

        assert (repo / "docs" / "HANDOFF.md").read_text() == "# v1"
        assert not (repo / "docs" / "CHANGELOG.md").exists()
        assert res.created is True
        assert res.modified is False
        assert res.unchanged is False
        assert res.history_recorded is False
        assert res.history_skipped is False

    def test_cli_first_generation_creates_no_history(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(lambda config, **kw: MockProvider(text="# Created")):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))

        assert rc == 0
        assert (repo / "docs" / "HANDOFF.md").read_text() == "# Created"
        assert not (repo / "docs" / "CHANGELOG.md").exists()


# =====================================================================
# Checkpoint update lifecycle & history handling
# =====================================================================

class TestCheckpointHistory:
    def test_previous_checkpoint_archived(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        CheckpointManager(project_root=repo).write_checkpoint("# v1", _meta())
        res = CheckpointManager(project_root=repo).write_checkpoint("# v2", _meta())

        assert (repo / "docs" / "HANDOFF.md").read_text() == "# v2"
        changelog = (repo / "docs" / "CHANGELOG.md").read_text()
        assert "# v1" in changelog
        assert "# v2" not in changelog
        assert changelog.count("## Checkpoint") == 1
        assert res.modified is True
        assert res.history_recorded is True

    def test_all_replaced_checkpoints_archived_in_order(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        mgr = CheckpointManager(project_root=repo)
        mgr.write_checkpoint("# v1", _meta())
        mgr.write_checkpoint("# v2", _meta())
        mgr.write_checkpoint("# v3", _meta())

        handoff = (repo / "docs" / "HANDOFF.md").read_text()
        assert handoff == "# v3"
        assert handoff.count("## Checkpoint") == 0

        changelog = (repo / "docs" / "CHANGELOG.md").read_text()
        assert changelog.count("## Checkpoint") == 2
        assert changelog.index("# v1") < changelog.index("# v2")
        assert "# v2" in changelog
        assert "# v3" not in changelog

    def test_unchanged_checkpoint_no_history(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        mgr = CheckpointManager(project_root=repo)
        mgr.write_checkpoint("# same", _meta())
        res = mgr.write_checkpoint("# same", _meta())

        assert res.unchanged is True
        assert res.modified is False
        assert res.history_recorded is False
        assert not (repo / "docs" / "CHANGELOG.md").exists()

    def test_handoff_never_appended(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        mgr = CheckpointManager(project_root=repo)
        mgr.write_checkpoint("# v1", _meta())
        mgr.write_checkpoint("# v2", _meta())

        handoff = (repo / "docs" / "HANDOFF.md").read_text()
        assert not handoff.startswith("# Handoff Checkpoint History")
        assert "# v1" not in handoff
        assert handoff == "# v2"

    def test_cli_archives_between_runs(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(lambda config, **kw: MockProvider(text="# One")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo))) == 0
        with _provider_patch(lambda config, **kw: MockProvider(text="# Two")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo))) == 0
        capsys.readouterr()

        assert (repo / "docs" / "HANDOFF.md").read_text() == "# Two"
        assert "# One" in (repo / "docs" / "CHANGELOG.md").read_text()

    def test_cli_reports_archival(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        (repo / "docs").mkdir()
        (repo / "docs" / "HANDOFF.md").write_text("# old")
        with _provider_patch(lambda config, **kw: MockProvider(text="# new")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo))) == 0
        out = capsys.readouterr().out
        assert "modified" in out.lower()
        assert "archived" in out.lower()
        assert "CHANGELOG.md" in out

    def test_custom_output_bypasses_lifecycle(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(lambda config, **kw: MockProvider(text="# Custom")):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), output="custom.md"))
        assert rc == 0
        assert (repo / "custom.md").read_text() == "# Custom"
        assert not (repo / "docs").exists()


# =====================================================================
# History handling safety
# =====================================================================

class TestHistorySafety:
    def test_secret_tainted_old_checkpoint_not_archived(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        (repo / "docs").mkdir()
        (repo / "docs" / "HANDOFF.md").write_text(
            "api_key = \"sk-faketestsecret0123456789abcdef\""
        )
        res = CheckpointManager(project_root=repo).write_checkpoint("# clean", _meta())

        assert (repo / "docs" / "HANDOFF.md").read_text() == "# clean"
        assert not (repo / "docs" / "CHANGELOG.md").exists()
        assert res.modified is True
        assert res.history_recorded is False
        assert res.history_skipped is True

    def test_secret_new_content_rejected_before_history(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager, HandoffContentError

        mgr = CheckpointManager(project_root=repo)
        mgr.write_checkpoint("# v1", _meta())
        with pytest.raises(HandoffContentError):
            mgr.write_checkpoint("api_key = \"sk-faketestsecret0123456789abcdef\"", _meta())

        assert (repo / "docs" / "HANDOFF.md").read_text() == "# v1"
        assert not (repo / "docs" / "CHANGELOG.md").exists()

    def test_changelog_containment(self, repo: Path) -> None:
        from handoff_agent.persistence import ChangelogWriter, HandoffPathError

        for bad in ["../escape.md", "a/../../escape.md", "/tmp/escape.md"]:
            with pytest.raises(HandoffPathError):
                ChangelogWriter(project_root=repo, rel_path=bad).append("# x", _meta())

    def test_changelog_symlink_escape_rejected(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.persistence import (
            ChangelogWriter,
            HandoffPathError,
        )

        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / "docs").symlink_to(outside, target_is_directory=True)

        with pytest.raises(HandoffPathError):
            ChangelogWriter(project_root=repo).append("# x", _meta())
        assert not (outside / "CHANGELOG.md").exists()

    def test_metadata_sanitized_no_header_injection(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        mgr = CheckpointManager(project_root=repo)
        mgr.write_checkpoint("# v1", _meta())
        meta = {
            "project": "acme\n## Injected Header",
            "branch": "main",
            "commit": "abc",
            "generated_at": "2026-09-06T00:00:00+00:00",
        }
        mgr.write_checkpoint("# v2", meta)

        changelog = (repo / "docs" / "CHANGELOG.md").read_text()
        assert "\n## Injected Header" not in changelog
        assert changelog.count("## Checkpoint") == 1
        assert not any(
            line.startswith("## Injected Header") for line in changelog.splitlines()
        )

    def test_failing_history_aborts_overwrite(self, repo: Path) -> None:
        from handoff_agent.persistence import (
            ChangelogError,
            CheckpointManager,
        )

        mgr = CheckpointManager(project_root=repo)
        mgr.write_checkpoint("# v1", _meta())
        (repo / "docs" / "CHANGELOG.md").mkdir()

        with pytest.raises(ChangelogError):
            mgr.write_checkpoint("# v2", _meta())
        # The current checkpoint must be left untouched.
        assert (repo / "docs" / "HANDOFF.md").read_text() == "# v1"

    def test_no_temp_files_left(self, repo: Path) -> None:
        from handoff_agent.persistence import CheckpointManager

        mgr = CheckpointManager(project_root=repo)
        mgr.write_checkpoint("# v1", _meta())
        mgr.write_checkpoint("# v2", _meta())
        mgr.write_checkpoint("# v3", _meta())

        leftovers = [p for p in (repo / "docs").iterdir()
                     if p.name.startswith(".handoff-")]
        assert leftovers == []


# =====================================================================
# Validation: CLI lifecycle + Phase 7 regression guarantees
# =====================================================================

class TestCliLifecycleRegression:
    def test_dry_run_zero_writes(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        rc = cmd_generate(_make_args(dry_run=True, path=str(repo)))
        assert rc == 0
        assert not (repo / "docs").exists()

    def test_dry_run_no_history_writes(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        rc = cmd_generate(_make_args(dry_run=True, path=str(repo)))
        assert rc == 0
        assert not (repo / "docs" / "CHANGELOG.md").exists()

    def test_commit_stages_only_handoff_with_history(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(lambda config, **kw: MockProvider(text="# A")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo), commit=True)) == 0
        with _provider_patch(lambda config, **kw: MockProvider(text="# B")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo), commit=True)) == 0

        head_files = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
        assert head_files == ["docs/HANDOFF.md"]
        # CHANGELOG.md is generated but never auto-staged/committed.
        assert (repo / "docs" / "CHANGELOG.md").exists()
        assert "CHANGELOG.md" not in head_files
        staged = git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
        assert staged == []

    def test_unrelated_user_changes_preserved(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        write_file(repo, "app.py", "print('user change')")
        write_file(repo, "notes.txt", "user untracked")
        (repo / "docs").mkdir()
        (repo / "docs" / "HANDOFF.md").write_text("# old")
        with _provider_patch(lambda config, **kw: MockProvider(text="# new")):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        assert rc == 0

        assert (repo / "app.py").read_text() == "print('user change')"
        assert (repo / "notes.txt").read_text() == "user untracked"
        assert "# old" in (repo / "docs" / "CHANGELOG.md").read_text()

    def test_secret_never_leaked_through_lifecycle(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        secret = "sk-faketestsecret0123456789abcdef"
        with _provider_patch(SecretProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))
        assert rc == 1
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err
        assert not (repo / "docs").exists()

    def test_commit_message_deterministic(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(lambda config, **kw: MockProvider(text="# A")):
            cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        subject = git(repo, "log", "-1", "--pretty=%s").stdout.strip()
        assert subject == "docs: update handoff checkpoint"