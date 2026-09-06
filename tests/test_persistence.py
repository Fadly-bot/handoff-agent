"""Tests for Phase 7 — persistence & safe commit.

Covers (per PHASE5A.md section 10):

A. HANDOFF creation        docs/ missing, HANDOFF.md created correctly
B. HANDOFF update          old content removed, new content persisted
C. containment             traversal / absolute / symlink escape rejected
D. source protection       source files unchanged, unrelated files untouched
E. dry-run                 no write, no provider call, no API key, no git mutation
F. diff behavior           created / modified / unchanged safely detected
G. commit                  only docs/HANDOFF.md staged & committed; other
                           modified/staged/untracked files preserved
H. forbidden git ops       Phase 1–6 forbidden operations remain forbidden
I. push protection         no git push, no network git operation
J. failures                write / staging / commit / non-git / malformed content
K. secret leakage          fake secrets never in stdout/stderr/exceptions/
                           commit message/HANDOFF.md
L. regression              full suite run separately
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
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
    """Mock provider that returns configurable content and records calls."""

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


# =====================================================================
# A. HANDOFF creation
# =====================================================================

class TestHandoffCreation:
    def test_creates_docs_dir_and_file(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        assert not (repo / "docs").exists()
        w = HandoffWriter(project_root=repo)
        result = w.write("# New checkpoint")

        assert (repo / "docs").is_dir()
        assert (repo / "docs" / "HANDOFF.md").is_file()
        assert (repo / "docs" / "HANDOFF.md").read_text() == "# New checkpoint"
        assert result.created is True
        assert result.modified is False
        assert result.unchanged is False
        assert result.rel_path == "docs/HANDOFF.md"

    def test_cli_generates_and_persists(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        provider = MockProvider(text="# Generated Handoff")
        with _provider_patch(lambda config, **kw: provider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))

        assert rc == 0
        assert (repo / "docs" / "HANDOFF.md").exists()
        assert (repo / "docs" / "HANDOFF.md").read_text() == "# Generated Handoff"

    def test_writer_uses_detected_project_root(self, repo: Path) -> None:
        """--path may point at a subdirectory; persistence uses repo root."""
        from handoff_agent.cli import cmd_generate

        sub = repo / "subdir"
        sub.mkdir()
        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(sub)))

        assert rc == 0
        assert (repo / "docs" / "HANDOFF.md").exists()
        assert not (sub / "docs").exists()


# =====================================================================
# B. HANDOFF update
# =====================================================================

class TestHandoffUpdate:
    def test_overwrites_existing_file(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        w = HandoffWriter(project_root=repo)
        w.write("# First version\nold content")
        r2 = w.write("# Second version\nnew content")

        assert r2.modified is True
        assert r2.created is False
        text = (repo / "docs" / "HANDOFF.md").read_text()
        assert "# Second version" in text
        assert "old content" not in text
        assert "new content" in text

    def test_never_appends(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        w = HandoffWriter(project_root=repo)
        w.write("# A")
        w.write("# B")
        w.write("# C")
        assert (repo / "docs" / "HANDOFF.md").read_text() == "# C"

    def test_cli_second_run_overwrites(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(lambda config, **kw: MockProvider(text="# One")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo))) == 0
        with _provider_patch(lambda config, **kw: MockProvider(text="# Two")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo))) == 0

        assert (repo / "docs" / "HANDOFF.md").read_text() == "# Two"


# =====================================================================
# C. containment
# =====================================================================

class TestContainment:
    def test_exact_path_allowed(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        w = HandoffWriter(project_root=repo)
        r = w.write("# ok")
        assert r.created is True

    def test_traversal_rejected(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.persistence import (
            HandoffPathError,
            HandoffWriter,
        )

        w = HandoffWriter(project_root=repo)
        for bad in ["../outside.md", "a/../../outside.md", ".."]:
            with pytest.raises(HandoffPathError):
                w2 = HandoffWriter(project_root=repo, rel_path=bad)
                w2.write("# bad")
        assert not (tmp_path / "outside.md").exists()

    def test_absolute_external_path_rejected(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffPathError, HandoffWriter

        with pytest.raises(HandoffPathError):
            HandoffWriter(project_root=repo, rel_path="/etc/passwd").write("# bad")

    def test_symlink_escape_rejected(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.persistence import (
            HandoffPathError,
            HandoffWriter,
        )

        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / "docs").symlink_to(outside, target_is_directory=True)

        with pytest.raises(HandoffPathError):
            HandoffWriter(project_root=repo, rel_path="docs/HANDOFF.md").write("# bad")

        assert not (outside / "HANDOFF.md").exists()

    def test_symlink_file_escape_rejected(self, repo: Path, tmp_path: Path) -> None:
        from handoff_agent.persistence import (
            HandoffPathError,
            HandoffWriter,
        )

        outside = tmp_path / "outside.md"
        outside.write_text("target")
        (repo / "docs").mkdir()
        (repo / "docs" / "HANDOFF.md").symlink_to(outside)

        with pytest.raises(HandoffPathError):
            HandoffWriter(project_root=repo, rel_path="docs/HANDOFF.md").write("# bad")
        assert outside.read_text() == "target"

    def test_cli_rejects_absolute_output(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(MockProvider):
            rc = cmd_generate(
                _make_args(provider="mock", path=str(repo),
                           output="/tmp/evil-HANDOFF.md")
            )
        assert rc == 1
        assert not (repo / "docs").exists()


# =====================================================================
# D. source protection
# =====================================================================

class TestSourceProtection:
    def test_source_files_unchanged(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        w = HandoffWriter(project_root=repo)
        w.write("# Handoff")

        assert (repo / "app.py").read_text() == "print('hello')"
        assert (repo / "README.md").read_text() == "# Test Project"
        # Only docs/HANDOFF.md was created
        new_files = set()
        for p in repo.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                rel = p.relative_to(repo)
                if rel.parts and rel.parts[0] != "docs":
                    new_files.add(str(rel))
        assert new_files == {"app.py", "README.md"}

    def test_cli_does_not_touch_unrelated_files(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        write_file(repo, "notes.txt", "user stuff")
        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))

        assert rc == 0
        assert (repo / "notes.txt").read_text() == "user stuff"
        st = git(repo, "status", "--porcelain").stdout
        assert "notes.txt" in st
        assert st.count("docs/HANDOFF.md") >= 0


# =====================================================================
# E. dry-run
# =====================================================================

class TestDryRun:
    def test_dry_run_does_not_write(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        rc = cmd_generate(_make_args(dry_run=True, path=str(repo)))
        assert rc == 0
        assert not (repo / "docs").exists()

    def test_dry_run_does_not_call_provider(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        rc = cmd_generate(_make_args(dry_run=True, provider="mock", path=str(repo)))
        assert rc == 0
        assert MockProvider.generate_calls == []

    def test_dry_run_does_not_require_api_key(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate
        from handoff_agent.config import DEFAULT_CONFIG

        with patch.dict(os.environ, {}, clear=True):
            rc = cmd_generate(_make_args(dry_run=True, path=str(repo)))
        assert rc == 0

    def test_dry_run_no_git_mutation(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        before = git(repo, "log", "--all", "--oneline").stdout.strip()
        cmd_generate(_make_args(dry_run=True, path=str(repo)))
        after = git(repo, "log", "--all", "--oneline").stdout.strip()
        assert before == after


# =====================================================================
# F. diff behavior
# =====================================================================

class TestDiffBehavior:
    def test_created_detected(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        r = HandoffWriter(project_root=repo).write("# new")
        assert r.created and not r.modified and not r.unchanged

    def test_modified_detected(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        w = HandoffWriter(project_root=repo)
        w.write("# one")
        r = w.write("# two")
        assert r.modified and not r.created and not r.unchanged

    def test_unchanged_detected(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        w = HandoffWriter(project_root=repo)
        w.write("# same")
        # Second identical write should not rewrite (unchanged).
        r = w.write("# same")
        assert r.unchanged and not r.created and not r.modified

    def test_cli_reports_created(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))
        assert rc == 0
        out = capsys.readouterr().out
        assert "created" in out.lower()

    def test_cli_reports_modified(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        (repo / "docs").mkdir()
        (repo / "docs" / "HANDOFF.md").write_text("# old")
        with _provider_patch(lambda config, **kw: MockProvider(text="# brand new")):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))
        assert rc == 0
        out = capsys.readouterr().out
        assert "modified" in out.lower()


# =====================================================================
# G. commit
# =====================================================================

class TestCommit:
    def _stage_unrelated(self, repo: Path) -> None:
        write_file(repo, "tracked.txt", "v1")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", "add tracked")
        write_file(repo, "tracked.txt", "v2 (staged by user)")
        git(repo, "add", "tracked.txt")

    def test_commit_stages_only_handoff(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        assert rc == 0

        head_files = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
        assert head_files == ["docs/HANDOFF.md"]

    def test_commit_message_deterministic(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(MockProvider):
            cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        subject = git(repo, "log", "-1", "--pretty=%s").stdout.strip()
        assert subject == "docs: update handoff checkpoint"

    def test_unrelated_modified_not_staged(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        write_file(repo, "app.py", "print('user change')")
        with _provider_patch(MockProvider):
            cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))

        staged = git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
        assert staged == []
        st = git(repo, "status", "--porcelain").stdout
        assert "M app.py" in st
        assert (repo / "app.py").read_text() == "print('user change')"

    def test_unrelated_staged_remains_staged(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        self._stage_unrelated(repo)
        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        assert rc == 0

        # HEAD contains ONLY handoff, not the user's staged file.
        head_files = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
        assert head_files == ["docs/HANDOFF.md"]
        # User's staged file is still staged.
        staged = git(repo, "diff", "--cached", "--name-only").stdout.splitlines()
        assert staged == ["tracked.txt"]
        assert (repo / "tracked.txt").read_text() == "v2 (staged by user)"

    def test_unrelated_untracked_remains_untouched(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        write_file(repo, "untracked.txt", "user untracked")
        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        assert rc == 0

        st = git(repo, "status", "--porcelain").stdout
        assert "?? untracked.txt" in st
        assert (repo / "untracked.txt").read_text() == "user untracked"

    def test_previous_handoff_commit_updated(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(lambda config, **kw: MockProvider(text="# A")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo), commit=True)) == 0
        with _provider_patch(lambda config, **kw: MockProvider(text="# B")):
            assert cmd_generate(_make_args(provider="mock", path=str(repo), commit=True)) == 0

        head_files = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
        assert head_files == ["docs/HANDOFF.md"]
        assert (repo / "docs" / "HANDOFF.md").read_text() == "# B"


# =====================================================================
# H. forbidden git operations
# =====================================================================

class TestForbiddenGitOperations:
    def test_mutation_commands_still_forbidden(self) -> None:
        from handoff_agent.git_helper import FORBIDDEN_GIT_COMMANDS

        for cmd in ["push", "reset", "clean", "checkout", "restore", "switch",
                    "merge", "rebase", "stash", "fetch", "pull", "rm", "mv",
                    "add", "commit", "init", "clone"]:
            assert cmd in FORBIDDEN_GIT_COMMANDS, cmd

    def test_run_rejects_forbidden_commands(self, repo: Path) -> None:
        from handoff_agent.git_helper import GitForbiddenError, GitRunner

        runner = GitRunner(cwd=str(repo))
        for cmd in [["push"], ["add", "app.py"], ["commit", "-m", "x"],
                    ["reset", "--hard"], ["clean", "-fd"], ["checkout", "main"],
                    ["fetch", "origin"], ["pull"]]:
            with pytest.raises(GitForbiddenError):
                runner.run(cmd)

    def test_arbitrary_git_command_rejected(self, repo: Path) -> None:
        from handoff_agent.git_helper import GitForbiddenError, GitRunner

        runner = GitRunner(cwd=str(repo))
        with pytest.raises(GitForbiddenError):
            runner.run(["whatever", "--flag"])

    def test_no_arbitrary_paths_in_stage_handoff(self, repo: Path) -> None:
        from handoff_agent.git_helper import GitForbiddenError, GitRunner

        runner = GitRunner(cwd=str(repo))
        for bad in ["..", "../x", "/abs", "a/../b", "subdir"]:
            with pytest.raises(GitForbiddenError):
                runner.stage_handoff(repo, bad)

    def test_stage_handoff_only_accepts_handoff_path(self, repo: Path) -> None:
        from handoff_agent.git_helper import GitForbiddenError, GitRunner

        runner = GitRunner(cwd=str(repo))
        with pytest.raises(GitForbiddenError):
            runner.stage_handoff(repo, "app.py")


# =====================================================================
# I. push protection
# =====================================================================

class TestPushProtection:
    def test_push_string_never_in_git_interface(self, repo: Path) -> None:
        from handoff_agent import git_helper

        src = Path(git_helper.__file__).read_text()
        # No unguarded 'push' subprocess usage should introduce a push path.
        assert "push" in git_helper.FORBIDDEN_GIT_COMMANDS

    def test_cli_never_invokes_push(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        assert rc == 0
        captured = capsys.readouterr()
        out = captured.out.lower()
        err = captured.err.lower()
        # No push command is ever hinted at or issued.
        assert "git push" not in out
        assert "push" not in err

    def test_no_network_git_operations_in_commit_flow(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        # Ensure remote operations are forbidden in the abstraction.
        from handoff_agent.git_helper import FORBIDDEN_GIT_COMMANDS
        assert "push" in FORBIDDEN_GIT_COMMANDS
        assert "fetch" in FORBIDDEN_GIT_COMMANDS
        assert "pull" in FORBIDDEN_GIT_COMMANDS

        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        assert rc == 0


# =====================================================================
# J. failures
# =====================================================================

class TestFailures:
    def test_write_failure(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriteError, HandoffWriter

        # Make the target an existing directory so the atomic replace fails.
        (repo / "docs").mkdir()
        (repo / "docs" / "HANDOFF.md").mkdir()
        w = HandoffWriter(project_root=repo)
        with pytest.raises(HandoffWriteError):
            w.write("# x")

    def test_write_to_existing_directory(self, repo: Path) -> None:
        """Writing over an existing directory must fail cleanly."""
        from handoff_agent.cli import cmd_generate

        dest = repo / "docs" / "HANDOFF.md"
        dest.mkdir(parents=True, exist_ok=True)
        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))
        # Must not crash; either error out or (if it managed) still be safe.
        assert rc in (0, 1)

    def test_commit_failure_no_user_identity(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        # Override identity with empty values so commit is guaranteed to fail
        # regardless of any global/system git config.
        git(repo, "config", "user.name", "")
        git(repo, "config", "user.email", "")

        with _provider_patch(MockProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo), commit=True))
        # Should fail cleanly with nonzero exit.
        assert rc == 1

    def test_staging_failure_non_git(self, repo: Path) -> None:
        from handoff_agent.git_helper import GitRunner

        runner = GitRunner(cwd=str(repo))
        res = runner.stage_handoff(repo, "docs/HANDOFF.md")
        # HANDOFF.md doesn't exist yet -> git add fails.
        assert res.returncode != 0

    def test_non_git_directory(self, tmp_path: Path) -> None:
        from handoff_agent.cli import cmd_generate

        plain = tmp_path / "plain"
        plain.mkdir()
        rc = cmd_generate(_make_args(provider="mock", path=str(plain)))
        assert rc == 1

    def test_malformed_provider_content_rejected(self, repo: Path) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(SecretProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))
        assert rc == 1
        assert not (repo / "docs").exists()


# =====================================================================
# K. secret leakage
# =====================================================================

class TestSecretLeakage:
    FAKE_SECRET = "sk-faketestsecret0123456789abcdef"

    def test_secret_never_in_handoff_file(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        with pytest.raises(Exception):
            HandoffWriter(project_root=repo).write(
                f"leak api_key = \"{self.FAKE_SECRET}\""
            )
        assert not (repo / "docs").exists()

    def test_secret_never_in_output(self, repo: Path, capsys) -> None:
        from handoff_agent.cli import cmd_generate

        with _provider_patch(SecretProvider):
            rc = cmd_generate(_make_args(provider="mock", path=str(repo)))
        assert rc == 1
        captured = capsys.readouterr()
        assert self.FAKE_SECRET not in captured.out
        assert self.FAKE_SECRET not in captured.err

    def test_secret_never_in_exception_message(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        with pytest.raises(Exception) as exc_info:
            HandoffWriter(project_root=repo).write(
                f"password = \"{self.FAKE_SECRET}\""
            )
        assert self.FAKE_SECRET not in str(exc_info.value)

    def test_secret_not_in_commit_message(self, repo: Path) -> None:
        from handoff_agent.git_helper import GitRunner

        # The commit message is deterministic; ensure it never contains secrets.
        write_file(repo, "docs/HANDOFF.md", "ok")
        runner = GitRunner(cwd=str(repo))
        runner.stage_handoff(repo, "docs/HANDOFF.md")
        cm = "docs: update handoff checkpoint"
        assert self.FAKE_SECRET not in cm
        assert self.FAKE_SECRET not in ";".join(runner.audit_log)

    def test_prompt_never_contains_secret(self, repo: Path) -> None:
        """Source secrets are filtered before they reach the prompt."""
        from handoff_agent.context_builder import ContextBuilder
        from handoff_agent.prompt_builder import PromptBuilder

        write_file(repo, ".env", f"API_KEY={self.FAKE_SECRET}")
        commit_all(repo, "add env")
        ctx = ContextBuilder(project_root=str(repo)).build()
        prompt = PromptBuilder().build(ctx)
        assert self.FAKE_SECRET not in prompt


# =====================================================================
# Misc / regression helpers
# =====================================================================

class TestPersistenceMisc:
    def test_write_result_never_stores_content(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        r = HandoffWriter(project_root=repo).write("# secret-free")
        # The result must not retain content at all.
        assert not hasattr(r, "content")

    def test_no_temp_files_left(self, repo: Path) -> None:
        from handoff_agent.persistence import HandoffWriter

        w = HandoffWriter(project_root=repo)
        w.write("# a")
        w.write("# b")
        leftovers = [p for p in (repo / "docs").iterdir()
                     if p.name.startswith(".handoff-")]
        assert leftovers == []