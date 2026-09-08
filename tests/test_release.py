"""Phase 18 — release & security audit suite.

Covers the final audits required for the Universal Handoff release candidate:
secret-leak, filesystem-boundary, Git-safety, dependency, API-key handling,
CLI/configuration, Python version compatibility, and the install/uninstall
flow (clean, offline, fresh-environment, reinstall, uninstall, refusal).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TESTS = REPO / "tests"


def all_source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in str(p))


def source_text() -> str:
    parts = []
    for path in all_source_files():
        parts.append(f"### {path.relative_to(REPO)}\n" + path.read_text(encoding="utf-8"))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Secret-leak audit
# ---------------------------------------------------------------------------

class TestSecretLeakAudit:
    SECRET_ASSIGNMENT = re.compile(
        r"(api[_-]?key|access[_-]?token|secret|password)\s*=\s*['\"]([^'\"]{6,})",
        re.IGNORECASE,
    )

    @pytest.mark.parametrize(
        "scan_path",
        [SRC, REPO / "install.sh", REPO / "uninstall.sh", REPO / "README.md",
         REPO / "CHANGELOG.md", REPO / "docs", REPO / "requirements.txt"],
    )
    def test_no_secret_values_in_release_surfaces(self, scan_path: Path) -> None:
        if scan_path.is_dir():
            files = [
                p for p in scan_path.rglob("*")
                if p.is_file() and "__pycache__" not in str(p)
                and (p.suffix in (".py", ".md", ".txt") or scan_path.name.endswith(".sh"))
            ]
            text = "\n".join(
                f.read_text(encoding="utf-8", errors="replace") for f in files
            )
        else:
            text = scan_path.read_text(encoding="utf-8", errors="replace")
        for m in self.SECRET_ASSIGNMENT.finditer(text):
            value = m.group(2)
            # Deliberately fake test probes are exempt; real leaks are not.
            assert "thisisasecret" in value and value.islower(), (
                f"secret-like assignment found under {scan_path}: {m.group(0)!r}"
            )
        for m in re.finditer(r"sk-[A-Za-z0-9\[\]{}, ]+", text.lower().replace(" ", "")):
            # Only detection regex *patterns* (e.g. "sk-[A-Za-z0-9]{20,}") are
            # allowed — never a concrete inline token value.
            assert "{" in m.group(0) or "[" in m.group(0), (
                f"concrete sk- token-like value under {scan_path}: {m.group(0)!r}"
            )

    def test_no_bearer_literal_tokens(self) -> None:
        text = source_text()
        assert not re.search(r"Bearer\s+[A-Za-z0-9_\-]{12,}", text)


# ---------------------------------------------------------------------------
# Dependency audit
# ---------------------------------------------------------------------------

class TestDependencyAudit:
    THIRD_PARTY = (
        "requests", "httpx", "aiohttp", "dotenv", "pydantic", "fastapi",
        "flask", "click", "typer", "rich", "openai", "anthropic", "google",
        "xai", "dashscope", "zhipu", "moonshot", "numpy", "yaml",
    )

    def test_requirements_only_pins_pytest(self) -> None:
        text = (REPO / "requirements.txt").read_text().strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        assert lines == ["pytest>=7.0,<9.0"]

    def test_no_third_party_imports_in_src(self) -> None:
        for path in all_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    assert name not in self.THIRD_PARTY, (path, name)

    def test_runtime_is_stdlib_only(self) -> None:
        # Highest-level module name of every import must be in stdlib /
        # local package (handoff_agent).
        stdlib = set(sys.stdlib_module_names)
        for path in all_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        top = a.name.split(".")[0]
                        assert top == "handoff_agent" or top in stdlib, (path, top)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    assert top == "handoff_agent" or top in stdlib, (path, top)


# ---------------------------------------------------------------------------
# Git-safety audit
# ---------------------------------------------------------------------------

class TestGitSafetyAudit:
    DANGEROUS = (
        "push", "fetch", "pull", "reset", "clean", "checkout", "restore",
        "switch", "merge", "rebase", "stash", "cherry-pick",
    )

    def test_forbidden_git_commands_declared(self) -> None:
        import handoff_agent.git_helper as gh
        for cmd in self.DANGEROUS:
            assert cmd in gh.FORBIDDEN_GIT_COMMANDS, cmd

    def test_commit_and_add_only_via_dedicated_helpers(self) -> None:
        import handoff_agent.git_helper as gh
        assert gh.ALLOWED_GIT_COMMANDS
        for cmd in gh.ALLOWED_GIT_COMMANDS:
            assert cmd not in gh.FORBIDDEN_GIT_COMMANDS

    def test_no_dangerous_literal_outside_forbidden_set(self) -> None:
        for path in all_source_files():
            for num, line in enumerate(path.read_text().splitlines(), 1):
                for cmd in self.DANGEROUS:
                    if f'"{cmd}",' in line:
                        # Only the FORBIDDEN_GIT_COMMANDS literal may name them
                        # as list/set entries.
                        assert path.name == "git_helper.py", f"{path}:{num}: {line}"

    def test_no_destructive_git_in_subprocess_argv(self) -> None:
        # No argv list like ["git", "push", ...] exists anywhere in src.
        text = source_text()
        for cmd in self.DANGEROUS:
            assert f'"git", "{cmd}"' not in text, cmd


# ---------------------------------------------------------------------------
# Filesystem-boundary audit
# ---------------------------------------------------------------------------

class TestFilesystemBoundaryAudit:
    def test_no_shell_true(self) -> None:
        assert "shell=True" not in source_text()

    def test_no_eval_or_exec(self) -> None:
        text = source_text()
        assert re.search(r"\beval\s*\(", text) is None
        assert re.search(r"\bexec\s*\(", text) is None

    def test_no_unrestricted_path_scanning_patterns(self) -> None:
        import handoff_agent.security as sec
        assert sec.SENSITIVE_FILENAMES
        assert sec.SENSITIVE_FILENAME_GLOBS


# ---------------------------------------------------------------------------
# API-key handling audit
# ---------------------------------------------------------------------------

class TestApiKeyHandlingAudit:
    def test_environ_keys_are_env_names(self) -> None:
        # Every os.environ access uses an UPPERCASE env-var NAME.
        pattern = re.compile(r"os\.environ(?:\.get)?\(\s*['\"]([^'\"]+)['\"]")
        for path in all_source_files():
            for m in pattern.finditer(path.read_text(encoding="utf-8")):
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*", m.group(1)), (path, m.group(1))

    def test_api_key_env_config_references_are_valid_names(self) -> None:
        install = (REPO / "install.sh").read_text()
        envs = re.findall(r'"api_key_env":\s*"([A-Z0-9_]+)"', install)
        assert sorted(envs) == [
            "ANTHROPIC_API_KEY",
            "DASHSCOPE_API_KEY",
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
        ]

    def test_config_template_version_matches_package(self) -> None:
        from handoff_agent.constants import VERSION
        install = (REPO / "install.sh").read_text()
        m = re.search(r'"version":\s*"([0-9.]+)"', install)
        assert m is not None
        assert m.group(1) == VERSION


# ---------------------------------------------------------------------------
# Python version compatibility
# ---------------------------------------------------------------------------

class TestPythonCompatibility:
    @pytest.mark.parametrize("path", all_source_files(), ids=lambda p: p.name)
    def test_source_parses_as_python_3_11(self, path: Path) -> None:
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))

    def test_installer_supports_python_311_only(self) -> None:
        install = (REPO / "install.sh").read_text()
        assert 'minor" -ge 11' in install or "minor -ge 11" in install
        assert "3.13" in install  # candidate ordering prefers newest


# ---------------------------------------------------------------------------
# CLI & configuration audit
# ---------------------------------------------------------------------------

class TestCliConfigAudit:
    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        base_env = os.environ.copy()
        base_env["PYTHONPATH"] = str(SRC)
        if env:
            base_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "handoff_agent", *args],
            capture_output=True,
            text=True,
            env=base_env,
            cwd=str(REPO),
        )

    def test_help_exits_zero(self) -> None:
        result = self.run("--help")
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "usage" in result.stderr.lower()

    def test_version_is_release_version(self) -> None:
        from handoff_agent.constants import VERSION
        result = self.run("--version")
        assert result.returncode == 0
        assert VERSION in result.stdout

    def test_unknown_flag_is_an_error(self) -> None:
        result = self.run("--definitely-not-a-flag")
        assert result.returncode != 0

    def test_config_output_never_prints_keys(self) -> None:
        result = self.run("config")
        assert result.returncode == 0
        assert "sk-" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# Installation & uninstallation flow
# ---------------------------------------------------------------------------

class TestInstallUninstallFlow:
    @staticmethod
    def fake_home(tmp_path: Path) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        return home

    def run_install(self, home: Path) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(home)
        return subprocess.run(
            ["bash", str(REPO / "install.sh")],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO),
        )

    def run_uninstall(self, home: Path, foreign: str | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["HOME"] = str(home)
        if foreign:
            env["HANDOFF_HOME"] = foreign
        return subprocess.run(
            ["bash", str(REPO / "uninstall.sh")],
            capture_output=True,
            text=True,
            input="y\n",
            env=env,
            cwd=str(REPO),
        )

    def test_clean_install(self, tmp_path: Path) -> None:
        home = self.fake_home(tmp_path)
        result = self.run_install(home)
        assert result.returncode == 0, result.stdout + result.stderr

        symlink = home / ".local" / "bin" / "handoff"
        assert symlink.is_symlink()
        env = os.environ.copy()
        env["HOME"] = str(home)
        version = subprocess.run(
            [str(symlink), "--version"], capture_output=True, text=True, env=env
        )
        assert version.returncode == 0, version.stderr
        assert "0.4.0" in version.stdout

        config = json.loads((home / ".handoff" / "config.json").read_text())
        assert config["version"] == "0.4.0"

    def test_offline_install_invokes_no_network_tooling(self) -> None:
        install = (REPO / "install.sh").read_text()
        # "pip" may appear as a substring (e.g. "pipefail") — flag only real
        # pip invocations and network tooling.
        assert not re.search(r"\bpip(?:3)?\b", install)
        assert not re.search(r"\bcurl\b|\bwget\b|\bgit clone\b", install)
        assert "requirements.txt" in install  # copied only, never installed

    def test_fresh_environment_gets_default_config(self, tmp_path: Path) -> None:
        home = self.fake_home(tmp_path)
        assert not (home / ".handoff").exists()
        result = self.run_install(home)
        assert result.returncode == 0
        assert (home / ".handoff" / "config.json").is_file()

    def test_reinstall_preserves_existing_config(self, tmp_path: Path) -> None:
        home = self.fake_home(tmp_path)
        self.run_install(home)
        config = home / ".handoff" / "config.json"
        data = json.loads(config.read_text())
        data["custom"] = True
        config.write_text(json.dumps(data, indent=2))
        result = self.run_install(home)
        assert result.returncode == 0
        reloaded = json.loads(config.read_text())
        assert reloaded["custom"] is True
        assert reloaded["version"] == "0.4.0"

    def test_uninstall_removes_installation(self, tmp_path: Path) -> None:
        home = self.fake_home(tmp_path)
        self.run_install(home)
        assert (home / ".handoff").exists()
        result = self.run_uninstall(home)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (home / ".handoff").exists()
        assert not (home / ".local" / "bin" / "handoff").exists()

    def test_uninstall_refuses_foreign_home(self, tmp_path: Path) -> None:
        home = self.fake_home(tmp_path)
        result = self.run_uninstall(home, foreign="/opt/somewhere-else")
        assert result.returncode != 0
        assert "Refusing" in result.stderr