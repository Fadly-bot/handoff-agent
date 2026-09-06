"""Git Inspector — collects read-only information about the repository state.

All commands go through GitRunner which enforces a read-only whitelist and
keeps an audit log. No mutating git operation is ever performed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from handoff_agent.detector import find_repo_root
from handoff_agent.git_helper import GitRunner

MAX_RECENT_COMMITS = 20


@dataclass
class FileStatus:
    """A single file's git status (staged/unstaged/untracked/deleted)."""

    path: str
    staged: bool = False
    modified: bool = False
    untracked: bool = False
    deleted: bool = False


@dataclass
class RecentCommit:
    short_hash: str
    full_hash: str
    subject: str
    author: str
    date: str


@dataclass
class DiffStat:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


@dataclass
class RepositoryInfo:
    """Read-only snapshot of repository state."""

    project_root: Path
    current_branch: str | None = None
    head_commit: str | None = None
    clean: bool = False
    modified_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    staged_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    recent_commits: list[RecentCommit] = field(default_factory=list)
    diff_stat: DiffStat = field(default_factory=DiffStat)
    remotes: list[str] = field(default_factory=list)
    audit_log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "project_root": str(self.project_root),
            "current_branch": self.current_branch,
            "head_commit": self.head_commit,
            "clean": self.clean,
            "modified_files": self.modified_files,
            "untracked_files": self.untracked_files,
            "staged_files": self.staged_files,
            "deleted_files": self.deleted_files,
            "recent_commits": [
                {
                    "short_hash": c.short_hash,
                    "full_hash": c.full_hash,
                    "subject": c.subject,
                    "author": c.author,
                    "date": c.date,
                }
                for c in self.recent_commits
            ],
            "diff_stat": {
                "files_changed": self.diff_stat.files_changed,
                "insertions": self.diff_stat.insertions,
                "deletions": self.diff_stat.deletions,
            },
            "remotes": self.remotes,
        }


def _parse_status_porcelain(status_output: str, info: RepositoryInfo) -> None:
    """Parse `git status --porcelain` into file status buckets."""
    for line in status_output.splitlines():
        if not line.strip():
            continue
        xy = line[:2]
        path = line[3:]
        status = FileStatus(path=path)
        if "?" in xy:
            status.untracked = True
        else:
            if "A" in xy or "M" in xy or "D" in xy or "R" in xy or "C" in xy:
                status.staged = True
            if "M" in xy or "T" in xy:
                status.modified = True
            if "D" in xy:
                status.deleted = True

        if status.deleted:
            info.deleted_files.append(path)
        if status.modified:
            info.modified_files.append(path)
        if status.staged:
            info.staged_files.append(path)
        if status.untracked:
            info.untracked_files.append(path)


def _has_staged_changes(runner: GitRunner) -> bool:
    result = runner.run(["diff", "--cached", "--name-only"])
    if not result.ok:
        return False
    return bool(result.stdout.strip())


def inspect_repository(
    project_root: str | Path | None = None,
    git_binary: str | None = None,
) -> RepositoryInfo:
    """Inspect a repository (default: cwd) using only read-only git commands."""
    root = Path(project_root).resolve() if project_root else find_repo_root()
    runner = GitRunner(git_binary=git_binary or "git", cwd=str(root))

    info = RepositoryInfo(project_root=root)

    branch_result = runner.run(
        ["rev-parse", "--abbrev-ref", "HEAD"]
    )
    if branch_result.ok:
        branch = branch_result.stdout.strip()
        info.current_branch = branch if branch != "HEAD" else None

    head_result = runner.run(["rev-parse", "HEAD"])
    if head_result.ok:
        info.head_commit = head_result.stdout.strip()

    status_result = runner.run(["status", "--porcelain"])
    if status_result.ok:
        _parse_status_porcelain(status_result.stdout, info)

    info.staged_files = list(
        dict.fromkeys(info.staged_files)
    )
    info.clean = (
        not info.modified_files
        and not info.untracked_files
        and not info.staged_files
        and not info.deleted_files
    )

    commit_result = runner.run(
        ["log", "--max-count", str(MAX_RECENT_COMMITS), "--pretty=format:%h%x00%H%x00%s%x00%an%x00%ai"]
    )
    if commit_result.ok and commit_result.stdout.strip():
        for line in commit_result.stdout.splitlines():
            if "\x00" not in line:
                continue
            parts = line.split("\x00")
            if len(parts) >= 5:
                short_hash, full_hash, subject, author, date = parts[:5]
                info.recent_commits.append(
                    RecentCommit(
                        short_hash=short_hash,
                        full_hash=full_hash,
                        subject=subject,
                        author=author,
                        date=date,
                    )
                )

    diff_result = runner.run(["diff", "--stat"])
    if diff_result.ok:
        info.diff_stat = _parse_diff_stat(diff_result.stdout)

    remote_result = runner.run(["remote", "-v"])
    if remote_result.ok:
        for line in remote_result.stdout.splitlines():
            parts = line.split("\t")
            if parts:
                name = parts[0].strip()
                if name and name not in info.remotes:
                    info.remotes.append(name)

    info.audit_log = list(runner.audit_log)
    return info


def _parse_diff_stat(diff_stat_output: str) -> DiffStat:
    """Parse `git diff --stat` output into a DiffStat."""
    stat = DiffStat()
    for line in diff_stat_output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith("files changed"):
            continue
        stat.files_changed += 1
        import re

        ins = re.search(r"(\d+) insertions?[+]", line)
        if ins:
            stat.insertions += int(ins.group(1))
        dels = re.search(r"(\d+) deletions?[\-]", line)
        if dels:
            stat.deletions += int(dels.group(1))
    return stat
