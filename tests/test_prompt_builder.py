"""Tests for PromptBuilder — Phase 5A deterministic prompt generation.

All tests use manually constructed FullContext objects so they are fast and
do not require a real git repository or filesystem access.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from handoff_agent.context_builder import (
    FileEntry,
    FullContext,
    GitInfo,
    ProjectInfo,
    SecurityInfo,
)
from handoff_agent.prompt_builder import PromptBuilder, SYSTEM_INSTRUCTION


# ---------------------------------------------------------------------------
# Helpers — build minimal FullContext objects without any git or filesystem
# ---------------------------------------------------------------------------

def _make_context(
    project_name: str = "test-project",
    project_type: str = "Python",
    project_root: str = "/tmp/repo",
    branch: str = "main",
    head: str = "abc123",
    clean: bool = True,
    files: list[FileEntry] | None = None,
    untracked_files: tuple[str, ...] = (),
    recent_commits: tuple[dict[str, str], ...] = (),
) -> FullContext:
    return FullContext(
        project=ProjectInfo(
            root=project_root,
            name=project_name,
            project_type=project_type,
        ),
        git=GitInfo(
            branch=branch,
            head=head,
            clean=clean,
            status="clean" if clean else "dirty",
            modified_files=(),
            untracked_files=untracked_files,
            staged_files=(),
            deleted_files=(),
            recent_commits=recent_commits,
            diff_stat={"files_changed": 0, "insertions": 0, "deletions": 0},
            remotes=(),
        ),
        files=tuple(files or []),
        security=SecurityInfo(
            excluded_count=0,
            exclusion_summary={},
            included_count=len(files or []),
            omitted_count=0,
            omission_reasons={},
        ),
    )


def _file(path: str, content: str) -> FileEntry:
    return FileEntry(path=path, content=content, size=len(content.encode("utf-8")))


# ===================================================================
# Deterministic output
# ===================================================================

class TestDeterministicOutput:
    def test_same_context_same_prompt(self) -> None:
        ctx = _make_context(files=[_file("a.py", "x = 1")])
        pb = PromptBuilder()
        p1 = pb.build(ctx)
        p2 = pb.build(ctx)
        assert p1 == p2

    def test_same_data_different_calls(self) -> None:
        ctx = _make_context(
            files=[_file("main.py", "print('hi')")],
            recent_commits=({"short_hash": "abc", "subject": "init"},),
        )
        pb = PromptBuilder()
        results = [pb.build(ctx) for _ in range(5)]
        assert len(set(results)) == 1


# ===================================================================
# System instruction
# ===================================================================

class TestSystemInstruction:
    def test_build_system_returns_string(self) -> None:
        pb = PromptBuilder()
        assert isinstance(pb.build_system(), str)

    def test_system_instruction_non_empty(self) -> None:
        pb = PromptBuilder()
        assert len(pb.build_system()) > 0

    def test_system_instruction_is_constant(self) -> None:
        pb = PromptBuilder()
        s1 = pb.build_system()
        s2 = pb.build_system()
        assert s1 == s2
        assert s1 is SYSTEM_INSTRUCTION


# ===================================================================
# Project metadata in prompt
# ===================================================================

class TestProjectMetadata:
    def test_project_name_present(self) -> None:
        ctx = _make_context(project_name="my-app")
        prompt = PromptBuilder().build_user(ctx)
        assert "my-app" in prompt

    def test_project_type_present(self) -> None:
        ctx = _make_context(project_type="Node.js")
        prompt = PromptBuilder().build_user(ctx)
        assert "Node.js" in prompt

    def test_project_root_present(self) -> None:
        ctx = _make_context(project_root="/home/user/repo")
        prompt = PromptBuilder().build_user(ctx)
        assert "/home/user/repo" in prompt


# ===================================================================
# Git metadata in prompt
# ===================================================================

class TestGitMetadata:
    def test_branch_present(self) -> None:
        ctx = _make_context(branch="develop")
        prompt = PromptBuilder().build_user(ctx)
        assert "develop" in prompt

    def test_head_present(self) -> None:
        ctx = _make_context(head="deadbeef123456")
        prompt = PromptBuilder().build_user(ctx)
        assert "deadbeef123456" in prompt

    def test_clean_status(self) -> None:
        ctx = _make_context(clean=True)
        prompt = PromptBuilder().build_user(ctx)
        assert "clean" in prompt

    def test_dirty_status(self) -> None:
        ctx = _make_context(clean=False)
        prompt = PromptBuilder().build_user(ctx)
        assert "dirty" in prompt

    def test_detached_head(self) -> None:
        ctx = _make_context(branch=None, head=None)
        prompt = PromptBuilder().build_user(ctx)
        assert "(detached)" in prompt

    def test_untracked_files_listed(self) -> None:
        # Only untracked files that survived the SecurityFilter (present in
        # context.files) may be listed in the prompt; raw names are dropped.
        ctx = _make_context(
            files=[_file("new.py", "x = 1")],
            untracked_files=("new.py", ".env"),
        )
        prompt = PromptBuilder().build_user(ctx)
        assert "new.py" in prompt
        assert ".env" not in prompt

    def test_recent_commits_present(self) -> None:
        commits = (
            {"short_hash": "abc1234", "subject": "first commit"},
            {"short_hash": "def5678", "subject": "second commit"},
        )
        ctx = _make_context(recent_commits=commits)
        prompt = PromptBuilder().build_user(ctx)
        assert "abc1234" in prompt
        assert "first commit" in prompt
        assert "def5678" in prompt
        assert "second commit" in prompt


# ===================================================================
# Safe file content included
# ===================================================================

class TestSafeFileContent:
    def test_file_content_in_prompt(self) -> None:
        ctx = _make_context(files=[_file("app.py", "print('hello')")])
        prompt = PromptBuilder().build_user(ctx)
        assert "print('hello')" in prompt

    def test_multiple_files_in_prompt(self) -> None:
        files = [
            _file("main.py", "def main(): pass"),
            _file("utils.py", "def util(): pass"),
        ]
        ctx = _make_context(files=files)
        prompt = PromptBuilder().build_user(ctx)
        assert "def main" in prompt
        assert "def util" in prompt

    def test_file_path_shown_with_content(self) -> None:
        ctx = _make_context(files=[_file("src/api.py", "x = 1")])
        prompt = PromptBuilder().build_user(ctx)
        assert "src/api.py" in prompt
        assert "x = 1" in prompt

    def test_file_index_numbered(self) -> None:
        files = [_file("a.py", "a"), _file("b.py", "b")]
        ctx = _make_context(files=files)
        prompt = PromptBuilder().build_user(ctx)
        assert "[1]" in prompt
        assert "[2]" in prompt


# ===================================================================
# Excluded content absent
# ===================================================================

class TestExcludedContentAbsent:
    def test_files_not_in_context_absent(self) -> None:
        ctx = _make_context(files=[_file("safe.py", "ok")])
        prompt = PromptBuilder().build_user(ctx)
        assert "secret_data" not in prompt
        assert ".env" not in prompt

    def test_empty_files_no_content(self) -> None:
        ctx = _make_context(files=[])
        prompt = PromptBuilder().build_user(ctx)
        assert "(no file contents provided)" in prompt


# ===================================================================
# No filesystem access
# ===================================================================

class TestNoFilesystemAccess:
    def test_no_path_import_in_prompt_builder(self) -> None:
        """prompt_builder.py should not import pathlib or os for file ops."""
        src = Path(__file__).resolve().parent.parent / "src" / "handoff_agent" / "prompt_builder.py"
        source = src.read_text()
        assert "import os" not in source
        assert "from pathlib" not in source
        assert "open(" not in source

    def test_no_subprocess_import(self) -> None:
        """prompt_builder.py should not import subprocess."""
        src = Path(__file__).resolve().parent.parent / "src" / "handoff_agent" / "prompt_builder.py"
        source = src.read_text()
        assert "import subprocess" not in source
        assert "from subprocess" not in source

    def test_no_environ_access(self) -> None:
        """prompt_builder.py should not access os.environ."""
        src = Path(__file__).resolve().parent.parent / "src" / "handoff_agent" / "prompt_builder.py"
        source = src.read_text()
        assert "os.environ" not in source
        assert "os.getenv" not in source

    def test_prompt_builder_only_uses_context(self) -> None:
        """PromptBuilder.build_user should work with only the passed context."""
        ctx = _make_context(
            project_name="x",
            project_type="Go",
            branch="feature",
            head="aaa111",
            files=[_file("main.go", "package main")],
        )
        pb = PromptBuilder()
        user = pb.build_user(ctx)
        assert "x" in user
        assert "Go" in user
        assert "feature" in user
        assert "aaa111" in user
        assert "package main" in user


# ===================================================================
# Combined build
# ===================================================================

class TestCombinedBuild:
    def test_build_contains_system_and_user(self) -> None:
        ctx = _make_context(files=[_file("a.py", "x")])
        pb = PromptBuilder()
        full = pb.build(ctx)
        system = pb.build_system()
        assert system in full
        assert "a.py" in full

    def test_separator_between_system_and_user(self) -> None:
        ctx = _make_context()
        full = PromptBuilder().build(ctx)
        assert "\n\n---\n\n" in full
