"""Phase 19 — Real AI Integration Validation tests."""

from pathlib import Path
from unittest import mock

import pytest

import handoff_agent.protocol as protocol
from handoff_agent.adapters.platforms import (
    FALLBACK_PLATFORM,
    PLATFORM_SPECS,
    get_platform_spec,
)
from handoff_agent.capability import WRITE_CAPABILITIES
from handoff_agent.integration import (
    LIVE_MODE,
    LIVE_TESTS_ENV,
    MOCK_MODE,
    CredentialStatus,
    IntegrationProviderFailure,
    MockProvider,
    actual_integration_matrix,
    negotiate_protocol,
    resolve_credentials,
    run_integration_suite,
    run_platform_certification,
    validate_model_override,
)
from conftest import init_repo


class TestCredentialHandling:
    def test_present_env_key_is_detected_by_name_only(self) -> None:
        spec = get_platform_spec("claude")
        status = resolve_credentials(spec, {"ANTHROPIC_API_KEY": "sk-LIVE-SECRET-1234"})
        assert status.present is True
        assert status.env_var == "ANTHROPIC_API_KEY"
        assert status.source == "environment"

    def test_missing_env_key_reported_not_present(self) -> None:
        status = resolve_credentials(get_platform_spec("claude"), {})
        assert status.present is False
        assert status.env_var == "ANTHROPIC_API_KEY"

    def test_local_platform_needs_no_key(self) -> None:
        status = resolve_credentials(get_platform_spec("opencode"), {})
        assert status.present is True
        assert status.env_var is None
        assert status.source == "local"

    def test_credential_status_never_contains_the_value(self) -> None:
        secret = "sk-LIVE-SECRET-1234"
        spec = get_platform_spec("claude")
        status = resolve_credentials(spec, {"ANTHROPIC_API_KEY": secret})
        serialized = str(status.to_dict())
        assert secret not in serialized

    def test_reports_never_echo_the_value(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        secret = "sk-LIVE-SECRET-1234"
        env = {"ANTHROPIC_API_KEY": secret, "OPENAI_API_KEY": "sk-LIVE-SECRET-OPENAI"}
        report = run_platform_certification(
            get_platform_spec("claude"), env=env, project_root=str(repo)
        )
        serialized = str(report.to_dict())
        assert secret not in serialized
        assert "sk-LIVE-SECRET-OPENAI" not in serialized


class TestModelOverride:
    def test_default_model_used_when_no_override(self) -> None:
        spec = get_platform_spec("claude")
        result = validate_model_override(spec, None)
        assert result["ok"] is True
        assert result["model"] == "claude-sonnet-4-5"

    def test_known_model_accepted(self) -> None:
        assert validate_model_override(get_platform_spec("claude"), "claude-opus-4-1")["ok"]

    def test_unknown_model_rejected_for_catalogued_platform(self) -> None:
        result = validate_model_override(get_platform_spec("claude"), "claude-9999")
        assert result["ok"] is False
        assert "claude-9999" in result["note"]

    def test_free_form_platform_accepts_override(self) -> None:
        result = validate_model_override(get_platform_spec("manus"), "custom-model-x")
        assert result["ok"] is True


class TestProtocolNegotiation:
    def test_current_version_accepted(self) -> None:
        result = negotiate_protocol(protocol.PROTOCOL_VERSION)
        assert result["ok"] is True

    def test_unknown_version_rejected(self) -> None:
        result = negotiate_protocol(999)
        assert result["ok"] is False


class TestMockProvider:
    def test_produce_verify_round_trip(self) -> None:
        mock_provider = MockProvider("claude")
        markdown = mock_provider.produce_checkpoint("serve integration")
        assert "# claude checkpoint" in markdown
        result = mock_provider.verify_checkpoint(markdown)
        assert result["ok"] is True
        assert result["identity_verified"] is True

    def test_verify_rejects_none_and_garbage(self) -> None:
        mock_provider = MockProvider("claude")
        assert mock_provider.verify_checkpoint(None)["ok"] is False
        assert mock_provider.verify_checkpoint("no block here")["ok"] is False

    def test_inject_failure_raises_safely(self) -> None:
        with pytest.raises(IntegrationProviderFailure):
            MockProvider("claude").inject_failure()


class TestCertification:
    @pytest.mark.parametrize("name", sorted(PLATFORM_SPECS))
    def test_every_platform_certifies_clean_in_mock_mode(self, tmp_path: Path, name: str) -> None:
        repo = tmp_path / f"repo-{name}"
        init_repo(repo)
        report = run_platform_certification(
            get_platform_spec(name), mode=MOCK_MODE, project_root=str(repo)
        )
        assert report.clean, report.to_dict()
        assert report.mode == MOCK_MODE

    def test_generic_fallback_certifies_clean(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo-generic"
        init_repo(repo)
        report = run_platform_certification(
            FALLBACK_PLATFORM, mode=MOCK_MODE, project_root=str(repo)
        )
        assert report.clean, report.to_dict()

    def test_write_capable_file_platform_runs_full_lifecycle(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        report = run_platform_certification(
            get_platform_spec("claude"), mode=MOCK_MODE, project_root=str(repo)
        )
        checks = {c.check: c.status for c in report.cases}
        assert checks["checkpoint.create"] == "passed"
        assert checks["checkpoint.update"] == "passed"
        assert checks["checkpoint.validation"] == "passed"
        assert checks["changelog.lifecycle"] == "passed"
        assert checks["secret.filtering"] == "passed"

    def test_read_only_file_platform_refuses_writes(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        report = run_platform_certification(
            get_platform_spec("deepseek"), mode=MOCK_MODE, project_root=str(repo)
        )
        assert report.clean, report.to_dict()
        assert report.to_dict()["passed"] > 0

    def test_no_file_interface_platform_skips_out_of_band(self, tmp_path: Path) -> None:
        report = run_platform_certification(
            get_platform_spec("gemini"), mode=MOCK_MODE, project_root=str(tmp_path)
        )
        checks = {c.check: c.status for c in report.cases}
        assert checks["file.round_trip"] == "skipped"
        assert checks["unsupported_interface.handling"] == "passed"

    def test_unknown_platform_never_falls_back(self) -> None:
        with pytest.raises(Exception):  # noqa: BLE001
            get_platform_spec("does-not-exist")

    def test_permission_boundary_matches_actual_grant(self) -> None:
        for name in sorted(PLATFORM_SPECS):
            spec = get_platform_spec(name)
            assert spec.can_write() == bool(spec.capability_grant & WRITE_CAPABILITIES)
            assert spec.read_only == (not spec.can_write())


class TestLiveSeparation:
    def test_mock_mode_sets_live_case_skipped(self) -> None:
        report = run_platform_certification(get_platform_spec("claude"))
        live_cases = [c for c in report.cases if c.check == "live.api"]
        assert live_cases and live_cases[0].status == "skipped"
        assert live_cases[0].live is True

    def test_live_mode_without_key_is_skipped(self) -> None:
        report = run_platform_certification(
            get_platform_spec("claude"), mode=LIVE_MODE, env={}
        )
        live_cases = [c for c in report.cases if c.check == "live.api"]
        assert live_cases[0].status == "skipped"

    def test_live_mode_without_gate_env_is_skipped(self, monkeypatch) -> None:
        monkeypatch.setenv(LIVE_TESTS_ENV, "0")
        report = run_platform_certification(
            get_platform_spec("claude"),
            mode=LIVE_MODE,
            env={"ANTHROPIC_API_KEY": "sk-live"},
        )
        live_cases = [c for c in report.cases if c.check == "live.api"]
        assert live_cases[0].status == "skipped"
        assert "sk-live" not in str(report.to_dict())

    def test_live_call_failure_is_deterministic_and_never_leaks(self, monkeypatch) -> None:
        monkeypatch.setenv(LIVE_TESTS_ENV, "1")
        secret = "sk-live-boundary-0"
        with mock.patch(
            "handoff_agent.providers.factory.create_provider",
            side_effect=RuntimeError("network unreachable"),
        ):
            report = run_platform_certification(
                get_platform_spec("claude"),
                mode=LIVE_MODE,
                env={"ANTHROPIC_API_KEY": secret},
            )
        live_cases = [c for c in report.cases if c.check == "live.api"]
        assert live_cases[0].status == "failed"
        assert secret not in str(report.to_dict())


class TestMatrixAndSuite:
    def test_matrix_reflects_actual_specs(self) -> None:
        matrix = actual_integration_matrix()
        assert matrix["claude"] == {
            "READ": True,
            "WRITE": True,
            "MCP": True,
            "SKILL": True,
            "CLI": True,
        }
        assert matrix["deepseek"] == {
            "READ": True,
            "WRITE": False,
            "MCP": False,
            "SKILL": False,
            "CLI": False,
        }
        perplexity = matrix["perplexity"]
        assert perplexity["READ"] is True
        assert perplexity["WRITE"] is False
        assert not any(perplexity[k] for k in ("MCP", "SKILL", "CLI"))
        assert matrix["generic-fallback"]["WRITE"] is False

    def test_suite_is_clean_and_complete(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        suite = run_integration_suite(project_root=str(repo))
        assert suite["clean"] is True, {r["platform"]: r["failed"] for r in suite["reports"] if r["failed"]}
        assert suite["mode"] == MOCK_MODE
        assert "generic" in suite["platforms"]
        assert suite["passed"] > 0
        assert "matrix" in suite

    def test_suite_never_leaks_credential_values(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        secret = "sk-SUITE-SECRET-9876543210"
        env = {spec.auth_env: secret for spec in PLATFORM_SPECS.values() if spec.auth_env}
        suite = run_integration_suite(project_root=str(repo), env=env)
        serialized = str(suite)
        assert secret not in serialized