"""Shared test fixtures/helpers."""

import os
import subprocess
import sys
from pathlib import Path

# Ensure ``handoff_agent`` is importable in subprocesses spawned by tests
# (e.g. CliAdapter) even when the working directory changes to a temporary
# repository.  The absolute path avoids PYTHONPATH breakage from relative
# ``src`` references.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_src_dir = str(_PROJECT_ROOT / "src")
if _src_dir not in sys.path[:1]:
    sys.path.insert(0, _src_dir)
os.environ["PYTHONPATH"] = _src_dir


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
             "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
             "GIT_COMMITTER_EMAIL": "test@example.com"},
    )


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "main")


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)


def write_file(repo: Path, rel: str, content: str = "") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p
