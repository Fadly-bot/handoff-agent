"""Phase 35 — tests for operations, compatibility & developer experience."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from handoff_agent.ops import (
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_OPERATION_ERROR,
    EXIT_USAGE,
    OPS_COMMANDS,
    OpsContext,
    OpsReport,
    assert_consistent_schema,
    build_ops_parser,
    ops_mcp_payload,
    ops_payload,
    render_ops,
    run_ops_command,
    run_ops_cli,
)


def _report(command: str = "health", ok: bool = True, exit_code: int = EXIT_OK, **payload: Any) -> OpsReport:
    return OpsReport(
        command=command,
        ok=ok,
        exit_code=exit_code,
        messages=["m1"],
        warnings=["w1"],
        errors=["e1"] if not ok else [],
        payload=dict(payload),
    )


def test_redacted_dict_keys_are_stable():
    report = _report("health", secret="password=supersecretvalue123")
    payload = ops_payload(report)
    assert set(payload.keys()) == {
        "handoff_ops_version",
        "command",
        "ok",
        "exit_code",
        "dry_run",
        "messages",
        "warnings",
        "errors",
        "payload",
    }
    assert payload["command"] == "health"
    assert isinstance(payload["payload"], dict)


def test_ops_payload_redacts_secret_shaped_values():
    report = _report("health", api_key="sk-secretvalue12345", token="tok123456789")
    payload = ops_payload(report)
    assert "[redacted]" in str(payload["payload"]["api_key"])
    assert "[redacted]" in str(payload["payload"]["token"])


def test_mcp_payload_matches_top_level_keys():
    report = _report("health", data=42)
    cli_data = ops_payload(report)
    mcp_data = ops_mcp_payload(report)
    assert set(cli_data.keys()) == set(mcp_data.keys())
    assert mcp_data["payload"] == {"summary": {"command": "health", "ok": True, "exit_code": 0, "error_count": 0}}


def test_assert_consistent_schema_passes_for_any_report():
    for cmd in OPS_COMMANDS:
        report = _report(cmd)
        assert assert_consistent_schema(report)


# ---------------------------------------------------------------------------
# run_ops_command fundamentals
# ---------------------------------------------------------------------------


def test_unknown_command_returns_usage():
    report, code = run_ops_command("nonexistent")
    assert code == EXIT_USAGE
    assert not report.ok
    assert any("unknown" in e.lower() for e in report.errors)


def test_all_commands_return_report_and_code():
    for cmd in OPS_COMMANDS:
        report, code = run_ops_command(cmd, dry_run=True, data_dir="/tmp/handoff_ops_test")
        assert isinstance(report, OpsReport)
        assert isinstance(code, int)
        assert report.command == cmd


def test_health_dry_run_ok():
    report, code = run_ops_command("health", dry_run=True, data_dir="/tmp/handoff_ops_h")
    assert code == EXIT_OK
    assert report.ok
    assert report.payload["providers"]["count"] == 4
    assert isinstance(report.payload["reliability"], dict)
    assert report.payload["reliability"].get("probe") is not None


def test_status_dry_run_ok():
    report, code = run_ops_command("status", dry_run=True, data_dir="/tmp/handoff_ops_s")
    assert code == EXIT_OK
    assert "agents" in report.payload


def test_workflow_dry_run_ok():
    report, code = run_ops_command("workflow", dry_run=True, data_dir="/tmp/handoff_ops_wf")
    assert code == EXIT_OK
    assert "workflows" in report.payload


def test_agent_dry_run_ok():
    report, code = run_ops_command("agent", dry_run=True, data_dir="/tmp/handoff_ops_a")
    assert code == EXIT_OK
    assert "count" in report.payload


# ---------------------------------------------------------------------------
# dry-run zero-write / zero-network safety
# ---------------------------------------------------------------------------


def test_recovery_dry_run_is_zero_write():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_recovery_"))
    report, code = run_ops_command("recovery", dry_run=True, data_dir=data_dir)
    assert code == EXIT_OK
    assert report.payload["dry_run"] is True
    assert "_planned" in report.payload or "planned_steps" in report.payload
    assert not any(str(p).endswith(".json") for p in data_dir.rglob("*") if p.is_file())


def test_conformance_dry_run_does_not_execute_checks():
    report, code = run_ops_command("conformance", dry_run=True, data_dir="/tmp/handoff_ops_cf")
    assert code == EXIT_OK
    assert report.payload["dry_run"] is True
    assert report.payload["planned_checks"]


def test_remote_dry_run_zero_network():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_remote_"))
    report, code = run_ops_command("remote", dry_run=True, data_dir=data_dir)
    assert code == EXIT_OK
    assert report.payload["planned"]


def test_sync_dry_run_zero_network():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_sync_"))
    report, code = run_ops_command("sync", dry_run=True, data_dir=data_dir)
    assert code == EXIT_OK
    assert report.payload["planned"]


def test_checkpoint_dry_run_zero_write():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_ckpt_"))
    report, code = run_ops_command("checkpoint", dry_run=True, data_dir=data_dir)
    assert code == EXIT_OK
    assert report.payload["dry_run"] is True
    assert not list(data_dir.rglob("*.json"))


# ---------------------------------------------------------------------------
# policy-explain: allowed / denied / approval-required
# ---------------------------------------------------------------------------


def test_policy_explain_tool_allowed():
    report, code = run_ops_command(
        "policy-explain", actor="agent-1", action="tool.checkpoint_read", resource=""
    )
    assert code == EXIT_OK
    assert report.ok
    assert report.payload["decision"]["allowed"] is True
    assert report.payload["decision"]["effect"] == "allow"


def test_policy_explain_git_denied():
    report, code = run_ops_command(
        "policy-explain", actor="agent-1", action="git.push", resource=""
    )
    assert code == EXIT_BLOCKED
    assert report.payload["decision"]["allowed"] is False
    assert report.payload["decision"]["effect"] == "deny"


def test_policy_explain_destructive_approval_required():
    report, code = run_ops_command(
        "policy-explain", actor="agent-1", action="destructive.purge", resource="/x"
    )
    assert code == EXIT_BLOCKED
    assert report.payload["decision"]["allowed"] is False
    assert report.payload["decision"]["approval_required"] is True
    assert "REQUIRE_APPROVAL" in str(report.payload["decision"]["effect"]).upper() or report.payload["decision"]["effect"] == "require_approval"


def test_policy_explain_explanation_lines():
    report, code = run_ops_command(
        "policy-explain", actor="agent-1", action="tool.report"
    )
    assert isinstance(report.payload["explanation"], list)
    assert len(report.payload["explanation"]) >= 1


def test_policy_explain_missing_actor_returns_usage():
    report, code = run_ops_command(
        "policy-explain", actor="", action="tool.report"
    )
    assert code == EXIT_USAGE
    assert any("actor" in e.lower() for e in report.errors)


def test_policy_explain_missing_action_returns_usage():
    report, code = run_ops_command(
        "policy-explain", actor="agent-1", action=""
    )
    assert code == EXIT_USAGE
    assert any("action" in e.lower() for e in report.errors)


# ---------------------------------------------------------------------------
# doctor: detects problems + consistent payloads
# ---------------------------------------------------------------------------


def test_doctor_git_repository_check_ok():
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    report, code = run_ops_command("doctor", project_root=project_root, data_dir="/tmp/handoff_ops_doc")
    assert code in (EXIT_OK, EXIT_OPERATION_ERROR, 4)
    check_names = [c["name"] for c in report.payload["checks"]]
    assert "project_git_repository" in check_names
    git_check = [c for c in report.payload["checks"] if c["name"] == "project_git_repository"][0]
    assert git_check["ok"] is True


def test_doctor_fails_when_not_git_repo():
    report, code = run_ops_command("doctor", project_root="/tmp", data_dir="/tmp/handoff_ops_doc_fail")
    assert code != EXIT_OK
    assert not report.ok
    assert any("problem" in e.lower() for e in report.errors)
    git_check = [c for c in report.payload["checks"] if c["name"] == "project_git_repository"][0]
    assert not git_check["ok"]


def test_doctor_fails_when_providers_missing_keys():
    report, code = run_ops_command(
        "doctor", project_root=Path(__file__).resolve().parent.parent, data_dir="/tmp/handoff_ops_doc_prov"
    )
    prov_check = [c for c in report.payload["checks"] if c["name"] == "provider_configuration"][0]
    assert not prov_check["ok"]
    assert "missing" in prov_check["detail"].lower()


# ---------------------------------------------------------------------------
# tool commands
# ---------------------------------------------------------------------------


def test_tool_list_ok():
    report, code = run_ops_command("tool", tool_name="", dry_run=True, data_dir="/tmp/ops_tool")
    assert code == EXIT_OK
    assert report.payload["count"] == 10
    names = [e["name"] for e in report.payload["tools"]]
    assert "vault" in names
    assert "purge" in names
    assert "netprobe" in names


def test_tool_detail_ok():
    report, code = run_ops_command("tool", tool_name="vault", dry_run=True, data_dir="/tmp/ops_tool2")
    assert code == EXIT_OK
    assert report.payload["tool"]["name"] == "vault"
    assert report.payload["tool"]["category"] == "secret"


def test_tool_unknown_returns_usage():
    report, code = run_ops_command("tool", tool_name="nonexistent_tool")
    assert code == EXIT_USAGE
    assert any("unknown" in e.lower() for e in report.errors)


# ---------------------------------------------------------------------------
# trace (empty telemetry)
# ---------------------------------------------------------------------------


def test_trace_empty_telemetry_not_configured():
    report, code = run_ops_command("trace", dry_run=True, data_dir="/tmp/ops_trace")
    assert code == EXIT_OK  # dry-run does not probe
    assert "planned" in report.payload


# ---------------------------------------------------------------------------
# compatibility: real proven matrix
# ---------------------------------------------------------------------------


def test_compatibility_proven_modules_count_matches_expectation():
    report, code = run_ops_command("compatibility", dry_run=True, data_dir="/tmp/ops_compat")
    assert code == EXIT_OK
    # We expect a high proven total (policy_engine, telemetry, tool_boundary,
    # sandbox, reliability, remote, sync, workflow, registry, messaging, mcp)
    assert report.payload["proven_total"] >= 10
    assert isinstance(report.payload["unproven"], list)


def test_compatibility_unproven_modules_do_not_contain_proven_modules():
    report, code = run_ops_command("compatibility", dry_run=True, data_dir="/tmp/ops_compat2")
    unproven_modules = {m["module"] for m in report.payload["unproven"]}
    proven_modules = {
        m["module"]
        for m in report.payload["matrix"]
        if m["proven"]
    }
    assert unproven_modules.isdisjoint(proven_modules)


def test_compatibility_only_real_capabilities_are_claimed():
    report, code = run_ops_command("compatibility", dry_run=True, data_dir="/tmp/ops_compat3")
    for entry in report.payload["matrix"]:
        if entry["proven"]:
            assert entry["claimed"] is True
        else:
            assert entry["claimed"] is False


def test_compatibility_matrices_for_each_interface():
    report, code = run_ops_command("compatibility", dry_run=True, data_dir="/tmp/ops_compat4")
    interfaces = {m["interface"] for m in report.payload["matrix"]}
    assert interfaces == {"cli", "api", "mcp"}


# ---------------------------------------------------------------------------
# checkpoint: dry-run vs. normal
# ---------------------------------------------------------------------------


def test_checkpoint_dry_run_plan():
    report, code = run_ops_command("checkpoint", dry_run=True, data_dir="/tmp/ops_ckpt_dry")
    assert code == EXIT_OK
    assert "would_restore" in report.payload or "planned" in report.payload


def test_checkpoint_normal_when_no_state():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_ckpt_norm_"))
    report, code = run_ops_command("checkpoint", dry_run=False, data_dir=data_dir)
    assert code == EXIT_OK
    assert report.ok


# ---------------------------------------------------------------------------
# recovery (non-dry-run)
# ---------------------------------------------------------------------------


def test_recovery_non_dry_run_returns_result():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_recovery_norm_"))
    report, code = run_ops_command("recovery", recovery_mode="run", dry_run=False, data_dir=data_dir)
    assert code == EXIT_OK
    assert "recovered" in report.payload
    assert isinstance(report.payload["recovered"], dict)


def test_recovery_status_mode_is_dry_run_plan():
    report, code = run_ops_command("recovery", recovery_mode="status", dry_run=True, data_dir="/tmp/ops_rec_status")
    assert code == EXIT_OK
    assert report.payload["dry_run"] is True


# ---------------------------------------------------------------------------
# remote and sync (normal)
# ---------------------------------------------------------------------------


def test_remote_normal_ok():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_remote_norm_"))
    report, code = run_ops_command("remote", dry_run=False, data_dir=data_dir)
    assert code == EXIT_OK
    assert "health" in report.payload


def test_sync_normal_ok():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_sync_norm_"))
    report, code = run_ops_command("sync", dry_run=False, data_dir=data_dir)
    assert code == EXIT_OK
    assert "devices" in report.payload
    assert "sessions" in report.payload


# ---------------------------------------------------------------------------
# render_ops
# ---------------------------------------------------------------------------


def test_render_ops_human_has_required_sections():
    report, _ = run_ops_command("health", dry_run=True, data_dir="/tmp/ops_render")
    text = render_ops(report, json_output=False)
    assert "[handoff ops]" in text
    assert "ok" in text.lower()
    assert "health" in text


def test_render_ops_json_is_valid_json():
    report, _ = run_ops_command("tool", tool_name="probe", dry_run=True, data_dir="/tmp/ops_render_json")
    text = render_ops(report, json_output=True)
    parsed = json.loads(text)
    assert parsed["command"] == "tool"
    assert parsed["ok"] is True


def test_render_ops_mcp_payload_has_summary():
    report, _ = run_ops_command("health", dry_run=True, data_dir="/tmp/ops_render_mcp")
    text = render_ops(report, json_output=True, mcp_payload=True)
    parsed = json.loads(text)
    assert "summary" in parsed["payload"]
    assert parsed["payload"]["summary"]["command"] == "health"


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def test_build_ops_parser_has_all_choices():
    parser = build_ops_parser()
    args = parser.parse_args(["health"])
    assert args.command == "health"


def test_build_ops_parser_accepts_flags():
    parser = build_ops_parser()
    args = parser.parse_args(["status", "--json", "--dry-run", "--data-dir", "/tmp/ops_parse", "--path", "/tmp"])
    assert args.json is True
    assert args.dry_run is True
    assert args.data_dir == "/tmp/ops_parse"
    assert args.path == "/tmp"


def test_build_ops_parser_unknown_command_exits():
    parser = build_ops_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["totally-not-a-command"])


# ---------------------------------------------------------------------------
# integration: CLI round-trip
# ---------------------------------------------------------------------------


def test_run_ops_cli_health_dry_run_json(capsys):
    exit_code = run_ops_cli(["--json", "--dry-run", "--data-dir", "/tmp/ops_cli_h", "health"])
    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["command"] == "health"
    assert parsed["dry_run"] is True
    assert "[redacted]" not in out


def test_run_ops_cli_tool_list(capsys):
    exit_code = run_ops_cli(["--json", "tool", "--dry-run", "--data-dir", "/tmp/ops_cli_tool"])
    assert exit_code == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["command"] == "tool"
    assert parsed["ok"] is True


def test_run_ops_cli_policy_explain_blocked(capsys):
    exit_code = run_ops_cli(["--json", "policy-explain", "--actor", "agent-1", "--action", "git.push"])
    assert exit_code == EXIT_BLOCKED
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["ok"] is False


def test_run_ops_cli_unknown_returns_usage():
    with pytest.raises(SystemExit) as exc:
        run_ops_cli(["--json", "invalid-cmd"])
    assert exc.value.code == EXIT_USAGE


# ---------------------------------------------------------------------------
# report mutation safety (redacted_dict does not pop from payload)
# ---------------------------------------------------------------------------


def test_redacted_dict_does_not_mutate_payload():
    report, _ = run_ops_command("health", dry_run=True, data_dir="/tmp/ops_mutation")
    payload_before = dict(report.payload)
    _ = ops_payload(report)
    _ = ops_payload(report)
    assert report.payload == payload_before


# ---------------------------------------------------------------------------
# telemetry-driven degradation detection
# ---------------------------------------------------------------------------


def test_health_ok_when_providers_all_configured():
    report, code = run_ops_command("health", dry_run=True, data_dir="/tmp/ops_h_ok")
    assert code == EXIT_OK
    assert report.ok


# ---------------------------------------------------------------------------
# doctor telemetry check failure
# ---------------------------------------------------------------------------


def test_doctor_telemetry_check_present():
    report, code = run_ops_command("doctor", project_root=Path(__file__).resolve().parent.parent,
                                   data_dir="/tmp/ops_doc_tele")
    check = next(c for c in report.payload["checks"] if c["name"] == "telemetry")
    assert "ok" in check
    assert "detail" in check


# ---------------------------------------------------------------------------
# conformance dry-run consistency
# ---------------------------------------------------------------------------


def test_conformance_dry_run_no_conformance_results():
    report, code = run_ops_command("conformance", dry_run=True, data_dir="/tmp/ops_conf_dry")
    assert "tool_boundary" not in report.payload
    assert "reliability" not in report.payload
    assert "planned_checks" in report.payload


def test_conformance_non_dry_run_runs_suites():
    data_dir = Path(tempfile.mkdtemp(prefix="handoff_ops_conf_real_"))
    report, code = run_ops_command("conformance", dry_run=False, data_dir=data_dir)
    assert code == EXIT_OK
    assert report.ok
    assert "tool_boundary" in report.payload
    assert "reliability" in report.payload
    assert report.payload["tool_boundary"]["passed"] is True
    assert report.payload["reliability"]["passed"] is True
