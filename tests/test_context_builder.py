"""Tests for ContextBuilder — Phase 4 structured context assembly.

Note: This environment has slow git subprocess operations, so read-only
tests share a single built context via a module-scoped fixture to keep the
suite tractable. Tests that modify the repository build fresh contexts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from handoff_agent.context_builder import (
    OMITTED_FILE_COUNT_LIMIT,
    OMITTED_SIZE_LIMIT,
    OMITTED_TOTAL_LIMIT,
    ContextBuilder,
    DEFAULT_MAX_FILE_COUNT,
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_MAX_TOTAL_BYTES,
    FileEntry,
    FullContext,
    GitInfo,
    ProjectInfo,
    SecurityInfo,
)
from handoff_agent.context_builder import _apply_limits, _merge_deterministic
from conftest import commit_all, git, init_repo, write_file


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Fresh git repo per test (proper isolation for mutating tests)."""
    r = tmp_path / "repo"
    init_repo(r)
    write_file(r, "src/main.py", "def main():\n    return 'hello'\n")
    write_file(r, "src/util.py", "def util():\n    pass\n")
    write_file(r, "README.md", "# Test Repo\n")
    commit_all(r, "initial commit")
    return r


@pytest.fixture(scope="module")
def read_ctx(tmp_path_factory: pytest.TempPathFactory) -> FullContext:
    """Reusable context for pure read-only tests (builds its own repo once)."""
    r = tmp_path_factory.mktemp("readrepo") / "repo"
    init_repo(r)
    write_file(r, "src/main.py", "def main():\n    return 'hello'\n")
    write_file(r, "src/util.py", "def util():\n    pass\n")
    write_file(r, "README.md", "# Test Repo\n")
    commit_all(r, "initial commit")
    return ContextBuilder(project_root=r).build()


# ===================================================================
# Basic context
# ===================================================================

class TestBasicContext:
    def test_project_metadata(self, read_ctx: FullContext) -> None:
        assert isinstance(read_ctx, FullContext)
        assert isinstance(read_ctx.project, ProjectInfo)
        assert read_ctx.project.name == "repo"
        assert read_ctx.project.project_type in {"Python", "unknown"}

    def test_git_metadata(self, read_ctx: FullContext) -> None:
        assert isinstance(read_ctx.git, GitInfo)
        assert read_ctx.git.branch == "main"
        assert read_ctx.git.head is not None
        assert read_ctx.git.clean is True
        assert len(read_ctx.git.recent_commits) >= 1

    def test_included_safe_files(self, read_ctx: FullContext) -> None:
        paths = {f.path for f in read_ctx.files}
        assert "src/main.py" in paths
        assert "src/util.py" in paths
        assert "README.md" in paths
        assert all(isinstance(f, FileEntry) for f in read_ctx.files)

    def test_file_content_included(self, read_ctx: FullContext) -> None:
        main = next(f for f in read_ctx.files if f.path == "src/main.py")
        assert "def main" in main.content
        assert main.size > 0

    def test_security_info_populated(self, read_ctx: FullContext) -> None:
        assert isinstance(read_ctx.security, SecurityInfo)
        assert read_ctx.security.included_count == len(read_ctx.files) >= 1
        assert read_ctx.security.excluded_count == 0


# ===================================================================
# Security integration
# ===================================================================

class TestSecurityIntegration:
    """Context Builder must never include sensitive files."""

    def _paths(self, repo: Path) -> set[str]:
        return {f.path for f in ContextBuilder(project_root=repo).build().files}

    def test_env_excluded(self, repo: Path) -> None:
        write_file(repo, ".env", "SECRET_KEY=abcdef1234567890")
        assert ".env" not in self._paths(repo)

    def test_private_key_excluded(self, repo: Path) -> None:
        write_file(repo, "id_rsa", "-----BEGIN RSA PRIVATE KEY-----")
        assert "id_rsa" not in self._paths(repo)

    def test_credential_file_excluded(self, repo: Path) -> None:
        write_file(repo, "credentials.json", '{"user": "x"}')
        assert "credentials.json" not in self._paths(repo)

    def test_sensitive_directory_excluded(self, repo: Path) -> None:
        write_file(repo, "node_modules/lib/index.js", "module.exports = 1;")
        assert not any("node_modules" in p for p in self._paths(repo))

    def test_tracked_secret_excluded(self, repo: Path) -> None:
        """Even tracked secret files must be excluded."""
        write_file(repo, "config/.env.production", "DB_PASSWORD=Sup3rSecret123")
        git(repo, "add", "config/.env.production")
        commit_all(repo, "add env")
        ctx = ContextBuilder(project_root=repo).build()
        assert "config/.env.production" not in {f.path for f in ctx.files}
        assert ctx.security.excluded_count >= 1

    def test_safe_source_file_included(self, repo: Path) -> None:
        write_file(repo, "src/app.py", "print('safe')\n")
        assert "src/app.py" in self._paths(repo)

    def test_content_heuristic_excludes_secret(self, repo: Path) -> None:
        write_file(repo, "settings.py", 'SECRET_KEY = "abcdefghijklmnopqrstuvwxyz123456"')
        ctx = ContextBuilder(project_root=repo).build()
        assert "settings.py" not in {f.path for f in ctx.files}
        assert ctx.security.exclusion_summary.get("sensitive content detected", 0) >= 1


# ===================================================================
# Context limits
# ===================================================================

class TestContextLimits:
    def test_individual_file_size_limit(self, repo: Path) -> None:
        write_file(repo, "big.py", "x" * 500)
        write_file(repo, "small.py", "y" * 10)
        ctx = ContextBuilder(project_root=repo, max_file_size=100).build()
        paths = {f.path for f in ctx.files}
        assert "big.py" not in paths
        assert "small.py" in paths
        assert OMITTED_SIZE_LIMIT in ctx.security.omission_reasons

    def test_total_context_byte_limit(self, repo: Path) -> None:
        write_file(repo, "a.py", "a" * 300)
        write_file(repo, "b.py", "b" * 300)
        ctx = ContextBuilder(
            project_root=repo, max_file_size=1000, max_total_bytes=400,
        ).build()
        assert OMITTED_TOTAL_LIMIT in ctx.security.omission_reasons
        assert ctx.security.omitted_count >= 1

    def test_file_count_limit(self, repo: Path) -> None:
        write_file(repo, "extra1.py", "x\n")
        write_file(repo, "extra2.py", "y\n")
        ctx = ContextBuilder(project_root=repo, max_file_count=2).build()
        assert len(ctx.files) == 2
        assert OMITTED_FILE_COUNT_LIMIT in ctx.security.omission_reasons

    def test_omission_reasons_safe(self, repo: Path) -> None:
        write_file(repo, "big.py", "x" * 500)
        ctx = ContextBuilder(project_root=repo, max_file_size=100).build()
        for reason in ctx.security.omission_reasons:
            assert "x" * 10 not in reason

    def test_no_silent_exceed(self, repo: Path) -> None:
        write_file(repo, "a.py", "a" * 300)
        write_file(repo, "b.py", "b" * 300)
        ctx = ContextBuilder(
            project_root=repo, max_file_size=1000, max_total_bytes=400,
        ).build()
        assert sum(f.size for f in ctx.files) <= 400


# ===================================================================
# Deterministic ordering
# ===================================================================

class TestDeterministicOrdering:
    def test_same_order_across_builds(self, repo: Path) -> None:
        write_file(repo, "aaa.py", "x\n")
        write_file(repo, "zzz.py", "y\n")
        c1 = ContextBuilder(project_root=repo).build()
        c2 = ContextBuilder(project_root=repo).build()
        assert [f.path for f in c1.files] == [f.path for f in c2.files]

    def test_merge_deterministic(self) -> None:
        assert _merge_deterministic(["b.py", "a.py"], ["d.py", "c.py"]) == [
            "a.py", "b.py", "c.py", "d.py",
        ]

    def test_merge_dedupes(self) -> None:
        assert _merge_deterministic(["a.py"], ["a.py", "b.py"]) == ["a.py", "b.py"]


# ===================================================================
# Encoding handling
# ===================================================================

class TestEncoding:
    def test_utf8_file(self, repo: Path) -> None:
        write_file(repo, "unicode.py", "# héllo wörld\n")
        ctx = ContextBuilder(project_root=repo).build()
        uni = next(f for f in ctx.files if f.path == "unicode.py")
        assert "héllo" in uni.content

    def test_invalid_utf8_no_crash(self, repo: Path) -> None:
        (repo / "invalid.py").write_bytes(b"\xff\xfe\x00\x89\x00\x00binary\xff")
        ctx = ContextBuilder(project_root=repo).build()
        assert isinstance(ctx, FullContext)

    def test_latin1_fallback(self, repo: Path) -> None:
        (repo / "legacy.txt").write_bytes("caf\xe9".encode("latin-1"))
        ctx = ContextBuilder(project_root=repo).build()
        assert isinstance(ctx, FullContext)

    def test_empty_file(self, repo: Path) -> None:
        write_file(repo, "empty.py", "")
        ctx = ContextBuilder(project_root=repo).build()
        empty = next((f for f in ctx.files if f.path == "empty.py"), None)
        if empty is not None:
            assert empty.content == ""
        assert isinstance(ctx, FullContext)


# ===================================================================
# Paths
# ===================================================================

class TestPaths:
    def test_nested_directories(self, repo: Path) -> None:
        write_file(repo, "src/deep/nested/module.py", "x = 1\n")
        ctx = ContextBuilder(project_root=repo).build()
        assert "src/deep/nested/module.py" in {f.path for f in ctx.files}

    def test_spaces_in_filenames(self, repo: Path) -> None:
        write_file(repo, "my folder/file name.py", "print('x')\n")
        ctx = ContextBuilder(project_root=repo).build()
        assert any("my folder/file name.py" in f.path for f in ctx.files)

    def test_symlink_outside_root(self, repo: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("outside content should never leak\n")
        link = repo / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not supported")
        git(repo, "add", "linked.txt")
        commit_all(repo, "add symlink")
        ctx = ContextBuilder(project_root=repo).build()
        assert not any("outside content should never leak" in f.content for f in ctx.files)


# ===================================================================
# Immutability
# ===================================================================

class TestImmutability:
    def test_source_files_unchanged(self, repo: Path) -> None:
        main_before = (repo / "src/main.py").read_text()
        readme_before = (repo / "README.md").read_text()
        ContextBuilder(project_root=repo).build()
        assert (repo / "src/main.py").read_text() == main_before
        assert (repo / "README.md").read_text() == readme_before

    def test_git_index_unchanged(self, repo: Path) -> None:
        status_before = git(repo, "status", "--porcelain").stdout
        ContextBuilder(project_root=repo).build()
        status_after = git(repo, "status", "--porcelain").stdout
        assert status_after == status_before

    def test_no_new_commits(self, repo: Path) -> None:
        head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
        ContextBuilder(project_root=repo).build()
        head_after = git(repo, "rev-parse", "HEAD").stdout.strip()
        assert head_after == head_before


# ===================================================================
# Leakage
# ===================================================================

class TestLeakage:
    FAKE_KEY = "sk-fake-abcdefghijklmnopqrstuvwxyz123456"

    def test_secret_not_in_reasons(self, repo: Path) -> None:
        write_file(repo, "settings.py", f'API_KEY = "{self.FAKE_KEY}"')
        ctx = ContextBuilder(project_root=repo).build()
        for d in (ctx.security.omission_reasons, ctx.security.exclusion_summary):
            for reason in d:
                assert self.FAKE_KEY not in reason

    def test_secret_not_in_to_dict(self, repo: Path) -> None:
        write_file(repo, ".env", f"API_KEY={self.FAKE_KEY}\n")
        ctx = ContextBuilder(project_root=repo).build()
        assert self.FAKE_KEY not in str(ctx.to_dict())

    def test_env_value_not_included(self, repo: Path) -> None:
        env_value = "SuperSecretEnvValue123"
        write_file(repo, ".env", f"DB_PASSWORD={env_value}\n")
        ctx = ContextBuilder(project_root=repo).build()
        assert env_value not in str(ctx.security)
        assert env_value not in "".join(f.content for f in ctx.files)


# ===================================================================
# FullContext structure / API
# ===================================================================

class TestFullContextStructure:
    def test_to_dict_structure(self, read_ctx: FullContext) -> None:
        d = read_ctx.to_dict()
        assert set(d) == {"project", "git", "files", "security"}
        assert "root" in d["project"]
        assert "branch" in d["git"]
        assert "head" in d["git"]

    def test_files_are_tuple(self, read_ctx: FullContext) -> None:
        assert isinstance(read_ctx.files, tuple)

    def test_frozen_dataclasses(self, repo: Path) -> None:
        ctx = ContextBuilder(project_root=repo).build()
        with pytest.raises(Exception):
            ctx.project.name = "changed"  # type: ignore[misc]

    def test_provide_custom_security_filter(self, repo: Path) -> None:
        from handoff_agent.security import SecurityFilter
        sf = SecurityFilter(project_root=repo)
        assert isinstance(ContextBuilder(project_root=repo, security_filter=sf).build(), FullContext)


# ===================================================================
# Helper unit tests (no git — fast)
# ===================================================================

class TestApplyLimits:
    def test_apply_total_bytes(self) -> None:
        files, omissions = _apply_limits(
            [("a.py", "aaaa"), ("b.py", "bbbb")], 6, 10,
        )
        assert len(files) == 1
        assert OMITTED_TOTAL_LIMIT in omissions

    def test_apply_file_count(self) -> None:
        files, omissions = _apply_limits(
            [("a.py", "a"), ("b.py", "b"), ("c.py", "c")], 100, 2,
        )
        assert len(files) == 2
        assert OMITTED_FILE_COUNT_LIMIT in omissions

    def test_apply_no_limits_hit(self) -> None:
        files, omissions = _apply_limits([("a.py", "a"), ("b.py", "b")], 100, 10)
        assert len(files) == 2
        assert omissions == {}


# ===================================================================
# Defaults
# ===================================================================

class TestDefaults:
    def test_default_values(self) -> None:
        assert DEFAULT_MAX_FILE_SIZE == 256 * 1024
        assert DEFAULT_MAX_TOTAL_BYTES == 4 * 1024 * 1024
        assert DEFAULT_MAX_FILE_COUNT == 200

    def test_builder_defaults(self, repo: Path) -> None:
        b = ContextBuilder(project_root=repo)
        assert b.max_file_size == DEFAULT_MAX_FILE_SIZE
        assert b.max_total_bytes == DEFAULT_MAX_TOTAL_BYTES
        assert b.max_file_count == DEFAULT_MAX_FILE_COUNT
