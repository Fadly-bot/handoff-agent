"""Tests for CLI interface."""

import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
PYTHON = sys.executable


def run_handoff(*args: str) -> subprocess.CompletedProcess:
    """Run handoff_agent as a module."""
    return subprocess.run(
        [PYTHON, "-m", "handoff_agent", *args],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={**__import__("os").environ, "PYTHONPATH": str(SRC_DIR)},
    )


class TestVersionFlag:
    def test_version_outputs_version(self) -> None:
        result = run_handoff("--version")
        assert result.returncode == 0
        assert "0.1.0" in result.stdout

    def test_version_contains_prog_name(self) -> None:
        result = run_handoff("--version")
        assert "handoff" in result.stdout.lower()


class TestHelpFlag:
    def test_help_outputs_usage(self) -> None:
        result = run_handoff("--help")
        assert result.returncode == 0
        assert "usage:" in result.stdout.lower() or "Usage:" in result.stdout

    def test_help_contains_options(self) -> None:
        result = run_handoff("--help")
        assert "--dry-run" in result.stdout
        assert "--provider" in result.stdout
        assert "--commit" in result.stdout
        assert "--version" in result.stdout

    def test_help_contains_description(self) -> None:
        result = run_handoff("--help")
        assert "Handoff Agent" in result.stdout


class TestDryRunFlag:
    def test_dry_run_exits_zero(self) -> None:
        result = run_handoff("--dry-run")
        assert result.returncode == 0

    def test_dry_run_mentions_dry_run(self) -> None:
        result = run_handoff("--dry-run")
        assert "dry run" in result.stdout.lower()

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        from conftest import commit_all, init_repo, write_file

        cwd = tmp_path / "project"
        init_repo(cwd)
        write_file(cwd, "app.py", "pass")
        commit_all(cwd, "initial")
        result = subprocess.run(
            [PYTHON, "-m", "handoff_agent", "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env={**__import__("os").environ, "PYTHONPATH": str(SRC_DIR)},
        )
        assert result.returncode == 0
        assert not (cwd / "docs" / "HANDOFF.md").exists()


class TestProviderFlag:
    def test_provider_flag_accepted(self) -> None:
        result = run_handoff("--provider", "openai", "--dry-run")
        assert result.returncode == 0
        assert "openai" in result.stdout.lower()

    def test_provider_overrides_default(self) -> None:
        result = run_handoff("--provider", "deepseek", "--dry-run")
        assert result.returncode == 0
        assert "deepseek" in result.stdout.lower()


class TestOutputFlag:
    def test_output_flag_accepted(self) -> None:
        result = run_handoff("--output", "custom.md", "--dry-run")
        assert result.returncode == 0
        assert "custom.md" in result.stdout


class TestCommitFlag:
    def test_commit_flag_accepted(self) -> None:
        result = run_handoff("--commit", "--dry-run")
        assert result.returncode == 0
        assert "commit" in result.stdout.lower()


class TestDefaultCommand:
    def test_no_args_fails_without_api_key(self) -> None:
        result = run_handoff()
        assert result.returncode == 1
        assert "missing API key" in result.stderr.lower() or "missing api key" in result.stderr.lower()

    def test_dry_run_bypasses_api_key_check(self) -> None:
        result = run_handoff("--dry-run")
        assert result.returncode == 0


class TestInspectSubcommand:
    def test_inspect_in_help(self) -> None:
        result = run_handoff("--help")
        assert "inspect" in result.stdout

    def test_inspect_requires_git_repo(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        result = subprocess.run(
            [PYTHON, "-m", "handoff_agent", "inspect"],
            capture_output=True,
            text=True,
            cwd=str(plain),
            env={**__import__("os").environ, "PYTHONPATH": str(SRC_DIR)},
        )
        assert result.returncode == 1
        assert "Not inside a Git repository" in result.stdout

    def test_inspect_on_git_repo(self, tmp_path: Path) -> None:
        from conftest import commit_all, init_repo, write_file

        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "package.json", "{}")
        commit_all(repo, "initial")
        result = subprocess.run(
            [PYTHON, "-m", "handoff_agent", "inspect"],
            capture_output=True,
            text=True,
            cwd=str(repo),
            env={**__import__("os").environ, "PYTHONPATH": str(SRC_DIR)},
        )
        assert result.returncode == 0
        assert "Project root" in result.stdout
        assert "Node.js" in result.stdout
        assert "main" in result.stdout

    def test_inspect_path_flag(self, tmp_path: Path) -> None:
        from conftest import commit_all, init_repo, write_file

        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "a.txt", "x")
        commit_all(repo, "initial")
        result = subprocess.run(
            [PYTHON, "-m", "handoff_agent", "inspect", "--path", str(repo)],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env={**__import__("os").environ, "PYTHONPATH": str(SRC_DIR)},
        )
        assert result.returncode == 0
        assert str(repo.resolve()) in result.stdout

