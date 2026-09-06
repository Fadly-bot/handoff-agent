"""Tests for Project Detector."""

import pytest

from handoff_agent.detector import (
    NotARepositoryError,
    detect_project,
    detect_project_type,
    find_repo_root,
)

from conftest import commit_all, init_repo, write_file  # noqa: F401


class TestGitRepositoryDetection:
    def test_finds_repo_in_repo_dir(self, tmp_path):
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "a.txt", "x")
        commit_all(repo, "initial")
        root = find_repo_root(repo)
        assert root == repo.resolve()

    def test_detect_project_in_repo(self, tmp_path):
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "a.txt", "x")
        commit_all(repo, "initial")
        project = detect_project(repo)
        assert project.project_root == repo.resolve()
        assert project.git_dir_exists is True


class TestParentRepositoryDetection:
    def test_finds_repo_in_parent_dir(self, tmp_path):
        repo = tmp_path / "repo"
        nested = repo / "src" / "nested" / "deeper"
        init_repo(repo)
        write_file(repo, "a.txt", "x")
        commit_all(repo, "initial")
        nested.mkdir(parents=True, exist_ok=True)
        assert find_repo_root(nested) == repo.resolve()


class TestNonGitDirectory:
    def test_raises_for_non_git_dir(self, tmp_path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        with pytest.raises(NotARepositoryError):
            find_repo_root(plain)

    def test_detect_project_raises_for_non_git(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(NotARepositoryError):
            detect_project(plain)


class TestProjectTypeDetection:
    @pytest.mark.parametrize(
        "marker,expected",
        [
            ("package.json", "Node.js"),
            ("pyproject.toml", "Python"),
            ("requirements.txt", "Python"),
            ("composer.json", "PHP"),
            ("go.mod", "Go"),
            ("Cargo.toml", "Rust"),
            ("pom.xml", "Java/Maven"),
            ("build.gradle", "Java/Gradle"),
            ("build.gradle.kts", "Java/Gradle"),
            ("pubspec.yaml", "Flutter/Dart"),
        ],
    )
    def test_detects_known_types(self, tmp_path, marker, expected):
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, marker, "# marker")
        commit_all(repo, "initial")
        assert detect_project_type(repo) == expected

    def test_unknown_project(self, tmp_path):
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "readme.txt", "hi")
        commit_all(repo, "initial")
        assert detect_project_type(repo) == "unknown"

    def test_detection_does_not_read_marker_contents(self, tmp_path):
        # ensure detection relies on presence, not reading content
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "package.json", '{"secret":"topsecret"}')
        commit_all(repo, "initial")
        assert detect_project_type(repo) == "Node.js"


class TestNestedDirectory:
    def test_nested_dir_detect(self, tmp_path):
        repo = tmp_path / "repo"
        init_repo(repo)
        write_file(repo, "package.json", "{}")
        commit_all(repo, "initial")
        nested = repo / "sub" / "dir"
        nested.mkdir(parents=True, exist_ok=True)
        project = detect_project(nested)
        assert project.project_root == repo.resolve()
        assert project.project_type == "Node.js"
