"""Project Detector — locates the Git repository root and project type.

Read-only: only inspects the filesystem / git metadata. Never reads secret
files and never mutates anything.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_PROJECT_TYPE_MARKERS: list[tuple[str, str]] = [
    ("package.json", "Node.js"),
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
    ("composer.json", "PHP"),
    ("go.mod", "Go"),
    ("Cargo.toml", "Rust"),
    ("pom.xml", "Java/Maven"),
    ("build.gradle.kts", "Java/Gradle"),
    ("build.gradle", "Java/Gradle"),
    ("pubspec.yaml", "Flutter/Dart"),
]


class NotARepositoryError(Exception):
    """Raised when no Git repository can be found from the start directory."""


@dataclass
class Project:
    """Result of project detection."""

    project_root: Path
    git_dir_exists: bool
    project_type: str
    name: str

    def to_dict(self) -> dict[str, object]:
        return {
            "project_root": str(self.project_root),
            "git_dir_exists": self.git_dir_exists,
            "project_type": self.project_type,
            "name": self.name,
        }


def _git_dir_exists(path: Path) -> bool:
    return (path / ".git").exists()


def find_repo_root(start: str | Path | None = None) -> Path:
    """Walk upward from `start` (default cwd) to find a directory containing .git."""
    current = Path(start).resolve() if start else Path.cwd().resolve()
    candidate = current
    while True:
        if _git_dir_exists(candidate):
            return candidate
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    raise NotARepositoryError(
        f"Not inside a Git repository (searched from {current}). "
        "Run handoff from within a Git project."
    )


def detect_project_type(project_root: Path) -> str:
    """Detect a simple project type based on well-known marker files.

    Does NOT read file contents (avoids accidentally exposing secrets).
    """
    for marker, label in _PROJECT_TYPE_MARKERS:
        if (project_root / marker).exists():
            return label
    return "unknown"


def detect_project(start: str | Path | None = None) -> Project:
    """Detect the project root, type, and name starting from `start`."""
    project_root = find_repo_root(start)
    project_type = detect_project_type(project_root)
    name = project_root.name
    return Project(
        project_root=project_root,
        git_dir_exists=True,
        project_type=project_type,
        name=name or "root",
    )


def find_git_binary() -> str | None:
    """Locate the git executable. Returns None if unavailable."""
    return shutil.which("git")
