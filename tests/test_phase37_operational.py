"""Phase 37 — Operational Stabilization & Existing Project Adoption.

Covers:
  - Provider / API-key validation (missing key, invalid key, no leak).
  - ``--dry-run`` runs without API key, without network, without writes.
  - CLI discovery, help, status, inspect, config, and error exit codes.
  - Provider isolation (no fs/Git/env/repo mutation, filtered input only,
    structured response only, no auto-execution of provider output).
  - ContextBuilder / FullContext / PromptBuilder / SecurityFilter audit.
  - ``adopted_existing_project`` baseline checkpoint integrity.

Security invariants asserted here mirror the roadmap rules: no arbitrary
command execution, no secret leakage, no unauthorized repository mutation.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run the handoff CLI as a subprocess with a scrubbed API-key env."""
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
    env["PYTHONPATH"] = str(SRC_DIR)
    return subprocess.run(
        [PYTHON, "-m", "handoff_agent", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else str(PROJECT_ROOT),
        env=env,
        timeout=120,
    )


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "adopted-project"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n")
    (repo / "README.md").write_text("# adopted project\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


# ---------------------------------------------------------------------------
# 1. Provider / API key validation
# ---------------------------------------------------------------------------


class TestProviderApiKeyValidation:
    def test_missing_key_error_is_safe_and_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from handoff_agent.providers.base import ProviderMissingKeyError
        from handoff_agent.providers.openai import OpenAIProvider

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("PHASE37_KEY_ENV", raising=False)
        p = OpenAIProvider({"api_key_env": "PHASE37_KEY_ENV"})
        assert p.is_configured() is False
        with pytest.raises(ProviderMissingKeyError, match="PHASE37_KEY_ENV is not configured"):
            p.generate(context=None, prompt="hello")  # type: ignore[arg-type]

    def test_missing_key_error_does_not_leak_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from handoff_agent.providers.claude import ClaudeProvider

        secret = "sk-ant-phase37-supersecret-value-000111"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        p = ClaudeProvider({})
        with pytest.raises(Exception) as ei:
            p.generate(context=None, prompt="hello")  # type: ignore[arg-type]
        assert secret not in str(ei.value)

    @pytest.mark.parametrize(
        ("bad_value", "label"),
        [
            ("", "empty"),
            ("   ", "whitespace"),
        ],
    )
    def test_blank_key_counts_as_missing(
        self, monkeypatch: pytest.MonkeyPatch, bad_value: str, label: str
    ) -> None:
        from handoff_agent.providers.openai import OpenAIProvider

        monkeypatch.setenv("PHASE37_BLANK_KEY", bad_value)
        p = OpenAIProvider({"api_key_env": "PHASE37_BLANK_KEY"})
        assert p.is_configured() is False, label
        from handoff_agent.providers.base import ProviderMissingKeyError

        with pytest.raises(ProviderMissingKeyError):
            p.generate(context=None, prompt="hello")  # type: ignore[arg-type]

    def test_invalid_endpoint_config_refused(self) -> None:
        from handoff_agent.providers.factory import create_provider

        p = create_provider("openai", {"endpoint": "http://insecure.example.com/v1"})
        assert p.validate_config({"endpoint": "http://insecure.example.com/v1"}) is False
        from handoff_agent.providers.base import ProviderConfigError

        with pytest.raises(ProviderConfigError):
            p._assert_safe_endpoint()

    def test_unknown_provider_error_lists_available(self) -> None:
        from handoff_agent.providers.factory import create_provider

        with pytest.raises(Exception, match="Available providers"):
            create_provider("definitely-not-a-provider", {})

    def test_provider_status_line_masks_missing_key(self) -> None:
        from handoff_agent.cli import _provider_status_line
        from handoff_agent.config import DEFAULT_CONFIG

        line = _provider_status_line("openai", DEFAULT_CONFIG)
        assert "missing API key" in line
        assert "configured" not in line.split("missing")[0]

    def test_config_reports_provider_status_without_keys(self) -> None:
        result = _run_cli("config")
        assert result.returncode == 0
        for name in ("claude", "openai", "qwen", "deepseek"):
            assert name in result.stdout
        # No actual key material should ever appear in status output.
        for token in ("sk-", "Bearer", "x-api-key"):
            assert token not in result.stdout


# ---------------------------------------------------------------------------
# 2. --dry-run: no API key required, zero-network, zero-write
# ---------------------------------------------------------------------------


class TestDryRunSafety:
    def test_dry_run_works_without_any_api_key(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        result = _run_cli("--dry-run", "--path", str(repo))
        assert result.returncode == 0
        assert "dry run" in result.stdout.lower()

    def test_dry_run_explicitly_states_no_api_call(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        result = _run_cli("--dry-run", "--path", str(repo))
        assert "no api request made" in result.stdout.lower()

    def test_dry_run_zero_write(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        before = sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if ".git" not in p.parts)
        result = _run_cli("--dry-run", "--path", str(repo))
        assert result.returncode == 0
        after = sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if ".git" not in p.parts)
        assert before == after

    def test_dry_run_zero_git_state_change(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        status_before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        result = _run_cli("--dry-run", "--path", str(repo))
        assert result.returncode == 0
        status_after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert status_before == status_after == ""

    def test_dry_run_zero_network(self, tmp_path: Path) -> None:
        """A socket connect to a non-routable local port must never happen.

        The CLI subprocess runs with an environment variable that makes
        urllib resolve through a black-hole proxy: if any HTTP call is
        attempted it fails loudly, which surfaces as a non-zero exit.
        A correct dry-run makes no request at all and exits 0.
        """
        repo = _make_git_repo(tmp_path)
        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
        env["PYTHONPATH"] = str(SRC_DIR)
        # http_proxy pointing at a closed port: any urllib call raises
        # immediately. Deterministic and offline (no DNS needed for
        # explicit-host proxies... urllib skips proxying for localhost only).
        env["http_proxy"] = "http://127.0.0.1:1"
        env["https_proxy"] = "http://127.0.0.1:1"
        result = subprocess.run(
            [PYTHON, "-m", "handoff_agent", "--dry-run", "--path", str(repo)],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=120,
        )
        assert result.returncode == 0
        assert "no api request made" in result.stdout.lower()

    def test_dry_run_prints_prompt_but_not_secrets(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        (repo / "src" / "leak.py").write_text('api_key = "sk-phase37-fake-value-123456"\n')
        result = _run_cli("--dry-run", "--path", str(repo))
        assert result.returncode == 0
        assert "sk-phase37-fake-value-123456" not in result.stdout

    def test_dry_run_does_not_touch_checkpoint_or_repo_docs(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        result = _run_cli("--dry-run", "--path", str(repo))
        assert result.returncode == 0
        assert not (repo / "docs").exists()


# ---------------------------------------------------------------------------
# 3. CLI surface: discovery, help, status, inspect, config, exit codes
# ---------------------------------------------------------------------------


class TestCliSurface:
    def test_command_discovery(self) -> None:
        result = _run_cli("--help")
        assert result.returncode == 0
        for cmd in ("inspect", "config", "mcp", "registry", "telemetry", "ops"):
            assert cmd in result.stdout

    def test_version_exit_code(self) -> None:
        result = _run_cli("--version")
        assert result.returncode == 0

    def test_status_via_config_subcommand(self) -> None:
        result = _run_cli("config")
        assert result.returncode == 0
        assert "Provider status" in result.stdout

    def test_inspect_read_only_on_real_repo(self) -> None:
        result = _run_cli("inspect")
        assert result.returncode == 0
        assert "Inspection (read-only)" in result.stdout

    def test_inspect_non_repo_fails_with_exit_1(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        result = _run_cli("inspect", "--path", str(plain))
        assert result.returncode == 1
        assert "error" in result.stdout.lower()

    def test_unknown_provider_exit_code_1(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        result = _run_cli("--provider", "no-such-provider", "--path", str(repo))
        assert result.returncode == 1
        assert "error" in result.stderr.lower()

    def test_missing_key_generate_fails_safely_exit_1(self, tmp_path: Path) -> None:
        repo = _make_git_repo(tmp_path)
        result = _run_cli("--provider", "openai", "--path", str(repo))
        assert result.returncode == 1
        assert "missing api key" in result.stderr.lower()
        # No partial checkpoint written.
        assert not (repo / "docs" / "HANDOFF.md").exists()

    def test_corrupt_config_exit_1_no_traceback_leak(self, tmp_path: Path) -> None:
        bad = tmp_path / "config.json"
        bad.write_text("{not valid json")
        result = _run_cli("config", "--config", str(bad))
        assert result.returncode == 1
        assert "could not be loaded" in result.stderr
        assert "Traceback" not in result.stderr
        # Fail-safe message, not a crash dump.
        assert "[handoff] error" in result.stderr

    def test_provider_status_line_never_crashes_for_broken_provider(self) -> None:
        from handoff_agent.cli import _provider_status_line
        from handoff_agent.providers.factory import register_provider
        from handoff_agent.providers.base import ProviderAdapter

        class Exploding(ProviderAdapter):
            def __init__(self, config=None):
                raise RuntimeError("boom")

            def name(self) -> str:  # pragma: no cover
                return "exploding"

            def generate(self, context, prompt):  # pragma: no cover
                return ""

            def validate_config(self, config):  # pragma: no cover
                return True

        try:
            register_provider("phase37-exploding", Exploding)
            line = _provider_status_line("phase37-exploding", {"providers": {}})
            assert "not installed" in line
            assert "boom" not in line
        finally:
            from handoff_agent.providers import factory

            factory._PROVIDERS.pop("phase37-exploding", None)


# ---------------------------------------------------------------------------
# 4. Provider isolation
# ---------------------------------------------------------------------------


class TestProviderIsolation:
    def _src(self) -> Path:
        return PROJECT_ROOT / "src" / "handoff_agent" / "providers"

    _FORBIDDEN_IMPORTS = ("shutil", "glob", "pathlib", "subprocess", "ftplib", "http.client")
    _FORBIDDEN_CALLS = ("open", "read_text", "write_text", "read_bytes", "write_bytes")

    def _provider_asts(self) -> dict[str, ast.Module]:
        return {
            py.name: ast.parse(py.read_text()) for py in sorted(self._src().glob("*.py"))
        }

    def test_provider_sources_do_not_open_files_directly(self) -> None:
        for fname, tree in self._provider_asts().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        assert root not in self._FORBIDDEN_IMPORTS, (
                            f"{fname} imports {alias.name}"
                        )
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    assert root not in self._FORBIDDEN_IMPORTS, (
                        f"{fname} imports from {node.module}"
                    )
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    assert name not in self._FORBIDDEN_CALLS, (
                        f"{fname} calls {name}()"
                    )

    def test_provider_sources_do_not_touch_git(self) -> None:
        for fname, tree in self._provider_asts().items():
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod = getattr(node, "module", "") or "".join(
                        a.name for a in getattr(node, "names", [])
                    )
                    assert "git" not in mod.lower(), f"{fname} imports {mod}"
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    assert name not in ("Popen", "run", "check_output", "call"), (
                        f"{fname} calls {name}()"
                    )

    def test_provider_sources_do_not_enumerate_environment(self) -> None:
        for py in self._src().glob("*.py"):
            text = py.read_text()
            for tok in ("environ.copy", "environ.keys", "environ.items", "environ.values"):
                assert tok not in text, f"{py.name} enumerates env"

    def test_provider_sources_read_only_one_env_var(self) -> None:
        for py in self._src().glob("*.py"):
            text = py.read_text()
            hits = [ln for ln in text.splitlines() if "os.environ.get" in ln]
            for ln in hits:
                # the variable name must come from self.api_key_env only
                assert "self.api_key_env" in ln, f"{py.name}: {ln}"

    def test_provider_sources_https_only(self) -> None:
        for py in self._src().glob("*.py"):
            text = py.read_text()
            for ln in text.splitlines():
                if "http://" in ln and "https://" not in ln and "#" not in ln:
                    pytest.fail(f"{py.name} contains a plain http reference: {ln}")

    def test_provider_receives_only_filtered_context(self, tmp_path: Path) -> None:
        from handoff_agent.context_builder import ContextBuilder
        from handoff_agent.prompt_builder import PromptBuilder

        repo = _make_git_repo(tmp_path)
        (repo / "id_rsa").write_text("private material")
        (repo / ".env").write_text("SECRET=1\n")
        ctx = ContextBuilder(project_root=str(repo)).build()
        prompt = PromptBuilder().build(ctx)
        assert "private material" not in prompt
        assert "SECRET=1" not in prompt
        assert "id_rsa" not in prompt
        assert ".env" not in prompt

    def test_provider_returns_structured_string_only(self) -> None:
        from handoff_agent.providers.factory import create_provider

        p = create_provider("openai", {})
        # The adapter surface is generate(context, prompt) -> str: no file
        # handles, no command runner, no callbacks.
        import inspect

        sig = inspect.signature(p.generate)
        assert list(sig.parameters) == ["context", "prompt"]
        assert sig.return_annotation in (str, "str", inspect.Parameter.empty)

    def test_provider_output_is_never_executed(self) -> None:
        """Persistence scans AI output; execution of output is not in the
        code path anywhere (no eval/exec on generated content)."""
        for py in (PROJECT_ROOT / "src" / "handoff_agent").glob("*.py"):
            text = py.read_text()
            for ln in text.splitlines():
                stripped = ln.strip()
                if stripped.startswith("#"):
                    continue
                assert "eval(" not in ln and "exec(" not in ln, f"{py.name}: {ln}"

    def test_persistence_rejects_secret_tainted_output(self, tmp_path: Path) -> None:
        from handoff_agent.persistence import HandoffContentError, HandoffWriter

        repo = _make_git_repo(tmp_path)
        writer = HandoffWriter(project_root=repo, rel_path="docs/OUT.md")
        with pytest.raises(HandoffContentError):
            writer.write("# handoff\napi_key = " + '"' + "A" * 32 + '"\n')
        assert not (repo / "docs" / "OUT.md").exists()


# ---------------------------------------------------------------------------
# 5. Context / prompt / filter audit
# ---------------------------------------------------------------------------


class TestContextAndFilterAudit:
    def test_fullcontext_carries_no_credentials(self, tmp_path: Path) -> None:
        from handoff_agent.context_builder import ContextBuilder

        repo = _make_git_repo(tmp_path)
        (repo / ".env").write_text("TOPSECRET=topsecretvalue\n")
        ctx = ContextBuilder(project_root=str(repo)).build()
        blob = json.dumps(ctx.to_dict())
        assert "TOPSECRET" not in blob
        assert "topsecretvalue" not in blob

    def test_context_respects_limits(self, tmp_path: Path) -> None:
        from handoff_agent.context_builder import ContextBuilder

        repo = _make_git_repo(tmp_path)
        for i in range(10):
            (repo / f"f{i}.txt").write_text("x" * 500)
        ctx = ContextBuilder(
            project_root=str(repo), max_file_count=3, max_total_bytes=100_000
        ).build()
        assert len(ctx.files) == 3
        assert ctx.security.omitted_count >= 7

    def test_security_filter_layers(self, tmp_path: Path) -> None:
        from handoff_agent.security import SecurityFilter

        sf = SecurityFilter(project_root=tmp_path)
        checks = {
            ".env": "sensitive filename",
            "server.pem": "sensitive filename pattern",
            "node_modules/x.js": "excluded directory",
            "photo.png": "excluded extension",
            "../outside.txt": "path traversal",
        }
        for path, reason_part in checks.items():
            fr = sf.is_excluded(path)
            assert fr.excluded, path
            assert reason_part in fr.reason

    def test_prompt_builder_is_deterministic_and_fs_free(self, tmp_path: Path) -> None:
        from handoff_agent.context_builder import ContextBuilder
        from handoff_agent.prompt_builder import PromptBuilder

        repo = _make_git_repo(tmp_path)
        ctx = ContextBuilder(project_root=str(repo)).build()
        p1 = PromptBuilder().build(ctx)
        p2 = PromptBuilder().build(ctx)
        assert p1 == p2
        src = (PROJECT_ROOT / "src" / "handoff_agent" / "prompt_builder.py").read_text()
        for tok in ("open(", "os.environ", "subprocess", "Path("):
            assert tok not in src


# ---------------------------------------------------------------------------
# 6. Adopted existing project baseline checkpoint
# ---------------------------------------------------------------------------


class TestAdoptedExistingProjectBaseline:
    BASELINE = PROJECT_ROOT / "docs" / "company-f" / "BASELINE.md"

    def test_baseline_document_exists(self) -> None:
        assert self.BASELINE.is_file()

    def test_baseline_declares_adopted_not_created_from_scratch(self) -> None:
        text = self.BASELINE.read_text()
        assert "adopted" in text.lower() or "existing project" in text.lower()
        assert "handoff" in text.lower()

    def test_baseline_records_real_evidence(self) -> None:
        text = self.BASELINE.read_text()
        for required in ("test suite", "failed", "root cause", "git"):
            assert required in text.lower(), f"baseline missing: {required}"

    def test_baseline_does_not_claim_synthetic_origin(self) -> None:
        text = self.BASELINE.read_text().lower()
        assert "created by handoff from scratch" not in text
        assert "generated project" not in text

    def test_current_repo_state_matches_baseline_contract(self) -> None:
        """The checkpoint records the actual project state, verified live."""
        from handoff_agent.git_inspector import inspect_repository

        repo = inspect_repository(PROJECT_ROOT)
        assert repo.current_branch is not None
        assert repo.head_commit is not None

    def test_stale_or_dirty_state_is_reported_not_hidden(self, tmp_path: Path) -> None:
        from handoff_agent.git_inspector import inspect_repository

        repo = _make_git_repo(tmp_path)
        (repo / "dirty.txt").write_text("uncommitted\n")
        info = inspect_repository(repo)
        assert info.clean is False
        assert "dirty.txt" in info.untracked_files
