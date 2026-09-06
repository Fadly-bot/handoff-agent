"""Tests for Git Inspector (read-only) using temporary git repositories."""

import pytest

from handoff_agent.git_helper import GitCommandError, GitForbiddenError, GitRunner
from handoff_agent.git_inspector import inspect_repository

from conftest import commit_all, git, init_repo, write_file  # noqa: F401


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    init_repo(r)
    write_file(r, "a.txt", "one")
    write_file(r, "b.txt", "two")
    commit_all(r, "initial commit")
    return r


class TestBranchDetection:
    def test_current_branch(self, repo):
        info = inspect_repository(repo)
        assert info.current_branch is not None
        assert info.current_branch == "main"


class TestHeadDetection:
    def test_head_commit_present(self, repo):
        info = inspect_repository(repo)
        assert info.head_commit
        assert len(info.head_commit) == 40


class TestCleanStatus:
    def test_clean_repo(self, repo):
        info = inspect_repository(repo)
        assert info.clean is True
        assert info.modified_files == []
        assert info.untracked_files == []
        assert info.staged_files == []
        assert info.deleted_files == []


class TestModifiedFile:
    def test_modified_file_detected(self, repo):
        write_file(repo, "a.txt", "ONE")
        info = inspect_repository(repo)
        assert info.clean is False
        assert "a.txt" in info.modified_files


class TestUntrackedFile:
    def test_untracked_file_detected(self, repo):
        write_file(repo, "new.txt", "new")
        info = inspect_repository(repo)
        assert "new.txt" in info.untracked_files


class TestStagedFile:
    def test_staged_file_detected(self, repo):
        write_file(repo, "c.txt", "three")
        git(repo, "add", "c.txt")
        info = inspect_repository(repo)
        assert "c.txt" in info.staged_files


class TestDeletedFile:
    def test_deleted_file_detected(self, repo):
        (repo / "b.txt").unlink()
        info = inspect_repository(repo)
        assert "b.txt" in info.deleted_files


class TestRecentLog:
    def test_recent_commits_present(self, repo):
        write_file(repo, "a.txt", "one v2")
        commit_all(repo, "second commit")
        info = inspect_repository(repo)
        subjects = [c.subject for c in info.recent_commits]
        assert "second commit" in subjects
        assert "initial commit" in subjects

    def test_commit_fields(self, repo):
        info = inspect_repository(repo)
        assert len(info.recent_commits) >= 1
        commit = info.recent_commits[0]
        assert commit.short_hash
        assert commit.full_hash
        assert commit.subject
        assert commit.author
        assert commit.date


class TestDiffStat:
    def test_diff_stat_tracks_changes(self, repo):
        write_file(repo, "a.txt", "one\n" + "another line\n")
        write_file(repo, "b.txt", "two\n" + "new line\n")
        info = inspect_repository(repo)
        assert info.diff_stat.files_changed >= 1

    def test_diff_stat_clean_is_zero(self, repo):
        info = inspect_repository(repo)
        assert info.diff_stat.files_changed == 0


class TestRemoteDetection:
    def test_no_remotes(self, repo):
        info = inspect_repository(repo)
        assert info.remotes == []

    def test_remote_detected(self, repo):
        git(repo, "remote", "add", "origin", "https://example.com/repo.git")
        info = inspect_repository(repo)
        assert "origin" in info.remotes


class TestForbiddenCommandProtection:
    def _runner(self, repo):
        return GitRunner(git_binary="git", cwd=str(repo))

    @pytest.mark.parametrize(
        "forbidden",
        [
            ["add"],
            ["commit"],
            ["push"],
            ["reset"],
            ["clean"],
            ["checkout"],
            ["restore"],
            ["switch"],
            ["merge"],
            ["rebase"],
            ["init"],
        ],
    )
    def test_forbidden_commands_raise(self, repo, forbidden):
        runner = self._runner(repo)
        with pytest.raises(GitForbiddenError):
            runner.run(forbidden)

    def test_non_whitelisted_raises(self, repo):
        runner = self._runner(repo)
        with pytest.raises(GitForbiddenError):
            runner.run(["fetch"])

    @pytest.mark.parametrize(
        "allowed",
        [
            ["status"],
            ["log", "--oneline", "-5"],
            ["diff"],
            ["diff", "--stat"],
            ["branch", "-a"],
            ["remote", "-v"],
            ["rev-parse", "HEAD"],
        ],
    )
    def test_allowed_commands_succeed(self, repo, allowed):
        runner = self._runner(repo)
        result = runner.run(allowed)
        assert result.ok

    def test_empty_command_raises(self, repo):
        runner = self._runner(repo)
        with pytest.raises(GitCommandError):
            runner.run([])

    def test_audit_log_records_commands(self, repo):
        runner = self._runner(repo)
        runner.run(["status"])
        runner.run(["log", "--oneline"])
        assert len(runner.audit_log) == 2
        assert all("git" in entry for entry in runner.audit_log)
