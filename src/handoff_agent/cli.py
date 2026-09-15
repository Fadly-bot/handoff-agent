"""CLI interface for Handoff Agent."""

import argparse
import os
import sys
from pathlib import Path

from handoff_agent import __version__
from handoff_agent.config import load_config
from handoff_agent.providers import (
    ProviderConfigError,
    ProviderRequestError,
    available_providers,
    create_provider,
    resolve_provider_name,
)


def _provider_status_line(provider_name: str, config: dict) -> str:
    """Return a diagnostic line for a provider without revealing the API key."""
    providers_cfg = config.get("providers", {})
    pcfg = providers_cfg.get(provider_name, {})
    try:
        p = create_provider(provider_name, pcfg)
        if not p.is_configured():
            status = "missing API key"
        elif not p.validate_config(pcfg):
            status = "invalid config"
        else:
            status = "configured"
    except Exception:
        status = "not installed"
    model_display = getattr(p, "model", "") or "(default)"
    return f"  {provider_name}: {status}  [model: {model_display}]"


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show detection + read-only git inspection without writing anything."""
    from handoff_agent.detector import NotARepositoryError, detect_project
    from handoff_agent.git_inspector import inspect_repository

    try:
        project = detect_project(args.path)
    except NotARepositoryError as exc:
        print(f"[handoff] error: {exc}")
        return 1

    repo = inspect_repository(project.project_root)

    print("[handoff] Inspection (read-only)")
    print()
    print(f"  Project root : {project.project_root}")
    print(f"  Project type : {project.project_type}")
    print(f"  Name         : {project.name}")
    print()
    print(f"  Current branch : {repo.current_branch or '(detached)'}")
    print(f"  HEAD commit    : {repo.head_commit}")
    print(f"  Working tree   : {'clean' if repo.clean else 'dirty'}")
    print()
    print(f"  Modified files : {len(repo.modified_files)}")
    for f in repo.modified_files:
        print(f"    - {f}")
    print(f"  Untracked files: {len(repo.untracked_files)}")
    for f in repo.untracked_files:
        print(f"    - {f}")
    print(f"  Staged files   : {len(repo.staged_files)}")
    for f in repo.staged_files:
        print(f"    - {f}")
    print(f"  Deleted files  : {len(repo.deleted_files)}")
    for f in repo.deleted_files:
        print(f"    - {f}")
    print()
    print(f"  Diff stat      : {repo.diff_stat.files_changed} file(s), "
          f"{repo.diff_stat.insertions} ++ / {repo.diff_stat.deletions} --")
    print(f"  Remotes        : {', '.join(repo.remotes) if repo.remotes else '(none)'}")
    print()
    print(f"  Recent commits : {len(repo.recent_commits)}")
    for c in repo.recent_commits:
        print(f"    {c.short_hash} {c.subject} ({c.author})")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show provider configuration status."""
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)
    print("[handoff] Provider status")
    for pname in available_providers():
        print(_provider_status_line(pname, config))
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    """Show the universal agent registry status (Phase 24)."""
    from handoff_agent.registry import AgentRegistry

    try:
        registry = AgentRegistry()
    except Exception as exc:
        print(f"[handoff] error: registry unavailable: {exc}", file=sys.stderr)
        return 1
    payload = registry.cli_payload()
    action = getattr(args, "registry_action", "status") or "status"
    if action == "list":
        for agent in payload["agents"]:
            print(f"{agent['agent_id']}\t{agent['name']}\t{agent['status']}")
    else:
        print(f"[handoff] registry: {payload['count']} agent(s)")
        for agent in payload["agents"]:
            caps = ",".join(sorted(agent["capabilities"])) or "-"
            print(
                f"  {agent['agent_id']} ({agent['name']}) "
                f"{agent['status']} platform={agent['platform'] or '-'} "
                f"provider={agent['provider'] or '-'} model={agent['model'] or '-'} "
                f"caps=[{caps}]"
            )
    return 0


def cmd_telemetry(args: argparse.Namespace) -> int:
    """Show telemetry diagnostics (health, degraded state, traces, timeline)."""
    from handoff_agent.telemetry import default_collector

    collector = default_collector()
    payload = collector.cli_payload()
    health = payload["health"]
    print("[handoff] Telemetry diagnostics")
    print(f"  enabled            : {collector.enabled}")
    print(f"  local_only         : {health.get('local_only', True)}")
    print(f"  events collected   : {health.get('total_events', 0)}")
    print(f"  success / failure  : {health.get('success_count', 0)} / {health.get('failure_count', 0)}")
    print(f"  timeouts / cancel  : {health.get('timeout_count', 0)} / {health.get('cancellation_count', 0)}")
    print(f"  failure_rate       : {health.get('failure_rate', 0.0)}")
    print(f"  degraded count     : {health.get('degraded_count', 0)}")
    degraded = health.get("degraded", []) or []
    for d in degraded[:10]:
        print(f"    - {d['key']}: {d['reason']} (rate={d['failure_rate']})")
    anomalies = health.get("anomalies", []) or []
    for a in anomalies[:10]:
        print(f"    anomaly: {a.get('type')} domain={a.get('domain')} rate={a.get('rate')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff",
        description="Handoff Agent — AI-powered project handoff generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  handoff                    Generate handoff checkpoint (docs/HANDOFF.md);\n"
            "                              the previous checkpoint is archived to docs/CHANGELOG.md\n"
            "  handoff --dry-run          Preview output without writing files or calling AI\n"
            "  handoff --inspect          Show project + git inspection (read-only)\n"
            "  handoff --provider openai --model gpt-4o\n"
            "                             Use a specific provider and model\n"
            "  handoff config             Show provider configuration status\n"
            "  handoff mcp                Run the Handoff MCP server over stdio\n"
            "  handoff registry           Show agent registry status; use --registry-action list\n"
            "  handoff --commit           Generate, then commit ONLY docs/HANDOFF.md (never pushes)\n"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["inspect", "config", "mcp", "registry", "telemetry"],
        help="Subcommand. 'inspect' shows project + git state. 'config' shows provider status. "
        "'mcp' runs the Handoff MCP server over stdio. 'registry' shows agent registry status. "
        "'telemetry' shows telemetry diagnostics.",
    )
    parser.add_argument(
        "--registry-action",
        type=str,
        default="status",
        choices=["status", "list"],
        help="For 'registry': 'status' (default) shows a summary; 'list' prints agent ids.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Directory to inspect (default: current directory)",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="AI provider to use (default: from config)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model to use for the selected provider (default: provider's configured/default model)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: docs/HANDOFF.md)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: ~/.handoff/config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Build context and prompt, print to stdout without calling AI",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="After generating, commit ONLY docs/HANDOFF.md (never stages other files, never pushes)",
    )
    return parser


def cmd_generate(args: argparse.Namespace) -> int:
    """Handle the default generate command.

    Pipeline:  detect → inspect → security → context → prompt → provider → output
    """
    from handoff_agent.context_builder import ContextBuilder
    from handoff_agent.detector import NotARepositoryError
    from handoff_agent.prompt_builder import PromptBuilder

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    output_path = Path(args.output) if args.output else Path(config.get("output", "docs/HANDOFF.md"))
    config_default_provider = config.get("default_provider", "claude")

    # ── 1. Resolve provider name ──────────────────────────────────────
    try:
        provider_name = resolve_provider_name(args.provider, config_default_provider)
    except Exception as exc:
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1

    # ── 2. Dry-run: build context + prompt, skip API call ─────────────
    if args.dry_run:
        project_root = Path(args.path) if args.path else Path.cwd()
        try:
            ctx = ContextBuilder(project_root=str(project_root)).build()
        except NotARepositoryError as exc:
            print(f"[handoff] error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"[handoff] error: failed to build context: {exc}", file=sys.stderr)
            return 1

        prompt = PromptBuilder().build(ctx)

        providers_cfg = config.get("providers", {})
        pcfg = providers_cfg.get(provider_name, {})
        model = args.model or pcfg.get("model", "") or "(default)"

        print(f"[handoff] Dry run mode")
        print(f"[handoff] Provider: {provider_name}")
        print(f"[handoff] Model: {model}")
        print(f"[handoff] Project: {ctx.project.name} ({ctx.project.project_type})")
        print(f"[handoff] Files: {ctx.security.included_count} included, "
              f"{ctx.security.excluded_count} excluded")
        print(f"[handoff] Output would be: {output_path}")
        if args.commit:
            print(f"[handoff] Would stage and commit docs/HANDOFF.md")
        if output_path.as_posix() == "docs/HANDOFF.md":
            print("[handoff] Previous checkpoint would be archived to docs/CHANGELOG.md (if changed)")
        print(f"[handoff] Prompt length: {len(prompt)} chars")
        print()
        print(prompt)
        print()
        print(f"[handoff] (No API request made, no files written)")
        return 0

    # ── 3. Create and validate provider ───────────────────────────────
    try:
        providers_cfg = config.get("providers", {})
        pcfg = providers_cfg.get(provider_name, {})
        if args.model:
            pcfg = {**pcfg, "model": args.model}
        provider = create_provider(provider_name, pcfg)
    except Exception as exc:
        print(f"[handoff] error: provider '{provider_name}' could not be created: {exc}", file=sys.stderr)
        return 1

    if not provider.validate_config(pcfg):
        print(
            f"[handoff] error: {provider_name}: invalid provider configuration",
            file=sys.stderr,
        )
        return 1

    if not provider.is_configured():
        print(f"[handoff] error: {provider_name}: missing API key", file=sys.stderr)
        return 1

    # ── 4. Build context ──────────────────────────────────────────────
    project_root = Path(args.path) if args.path else Path.cwd()
    try:
        ctx = ContextBuilder(project_root=str(project_root)).build()
    except NotARepositoryError as exc:
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[handoff] error: failed to build context: {exc}", file=sys.stderr)
        return 1

    # Persistence always targets the DETECTED repository root, even when
    # --path points at a subdirectory of the repository.
    from handoff_agent.detector import find_repo_root

    repo_root = find_repo_root(project_root)

    # ── 5. Build prompt ───────────────────────────────────────────────
    prompt = PromptBuilder().build(ctx)

    # ── 6. Generate via provider ──────────────────────────────────────
    print(f"[handoff] Provider: {provider_name}")
    print(f"[handoff] Project: {ctx.project.name} ({ctx.project.project_type})")
    print(f"[handoff] Generating handoff document...")

    from handoff_agent.telemetry import (
        TelemetryDomain,
        TelemetryStatus,
        enable_default,
        end_trace_span,
        start_trace_span,
    )

    provider_span = start_trace_span(
        enable_default(),
        domain=TelemetryDomain.PROVIDER.value,
        operation="generate",
        resource=provider_name,
        actor="cli",
    )
    import time as _time

    _provider_started = _time.monotonic()
    try:
        result = provider.generate(ctx, prompt)
    except ProviderRequestError as exc:
        end_trace_span(
            enable_default(),
            provider_span,
            TelemetryStatus.ERROR.value,
            error=exc,
            error_reason=f"{provider_name} request failed",
        )
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1
    except ProviderConfigError as exc:
        end_trace_span(
            enable_default(),
            provider_span,
            TelemetryStatus.ERROR.value,
            error=exc,
            error_reason="provider configuration error",
        )
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        end_trace_span(
            enable_default(),
            provider_span,
            TelemetryStatus.ERROR.value,
            error=exc,
            error_reason="provider failed",
        )
        print(f"[handoff] error: provider failed: {exc}", file=sys.stderr)
        return 1
    _provider_elapsed_ms = int((_time.monotonic() - _provider_started) * 1000)
    model_name = args.model or pcfg.get("model", "") or "(default)"
    end_trace_span(
        enable_default(),
        provider_span,
        TelemetryStatus.OK.value,
        duration_ms=_provider_elapsed_ms,
        latency_ms=_provider_elapsed_ms,
        result={"provider": provider_name, "model": model_name},
    )

    # ── 7. Persist result (checkpoint lifecycle for docs/HANDOFF.md) ────
    from handoff_agent.persistence import (
        ChangelogError,
        CheckpointManager,
        HandoffContentError,
        HandoffPathError,
        HandoffWriteError,
        HandoffWriter,
    )

    print()
    print(f"[handoff] Persisting handoff to: {output_path}")

    created = modified = unchanged = False
    history_recorded = False
    history_skipped = False
    persisted_path = output_path

    try:
        if output_path.as_posix() == "docs/HANDOFF.md":
            manager = CheckpointManager(project_root=repo_root)
            checkpoint = manager.write_checkpoint(result, _checkpoint_metadata(ctx))
            created, modified, unchanged = (
                checkpoint.created,
                checkpoint.modified,
                checkpoint.unchanged,
            )
            history_recorded = checkpoint.history_recorded
            history_skipped = checkpoint.history_skipped
            persisted_path = checkpoint.path
        else:
            writer = HandoffWriter(project_root=repo_root, rel_path=str(output_path))
            wr = writer.write(result)
            created, modified, unchanged = wr.created, wr.modified, wr.unchanged
            persisted_path = wr.path
    except ChangelogError as exc:
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1
    except HandoffContentError as exc:
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1
    except HandoffPathError as exc:
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1
    except HandoffWriteError as exc:
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1

    # ── 8. Show diff (created / modified / unchanged) ─────────────────
    if created:
        print(f"[handoff] HANDOFF.md created: {persisted_path}")
        print(f"[handoff] New checkpoint written ({len(result)} chars).")
    elif modified:
        print(f"[handoff] HANDOFF.md modified: {persisted_path}")
        print(f"[handoff] Checkpoint updated; previous content replaced.")
        if history_recorded:
            print("[handoff] Previous checkpoint archived to docs/CHANGELOG.md.")
        elif history_skipped:
            print("[handoff] Previous checkpoint was not archived.", file=sys.stderr)
    elif unchanged:
        print(f"[handoff] HANDOFF.md unchanged: {persisted_path}")
        print(f"[handoff] Content identical to existing checkpoint; no rewrite.")

    print()
    print(f"[handoff] Generation complete ({len(result)} chars)")

    # ── 9. Commit (only when --commit explicitly supplied) ────────────
    if args.commit:
        rc = _commit_handoff(repo_root)
        if rc != 0:
            return rc

    return 0


def _checkpoint_metadata(ctx) -> dict[str, object]:
    """Build safe metadata for a checkpoint history entry from a FullContext."""
    from datetime import datetime, timezone

    return {
        "project": ctx.project.name,
        "branch": ctx.git.branch,
        "commit": ctx.git.head,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _commit_handoff(project_root: str | Path) -> int:
    """Stage and commit ONLY docs/HANDOFF.md (never any other file)."""
    from handoff_agent.git_helper import (
        GitCommandError,
        GitForbiddenError,
        GitRunner,
    )

    runner = GitRunner(cwd=str(project_root))

    print(f"[handoff] Staging ONLY docs/HANDOFF.md...")
    try:
        stage_result = runner.stage_handoff(project_root, "docs/HANDOFF.md")
    except (GitCommandError, GitForbiddenError) as exc:
        print(f"[handoff] error: staging failed: {exc}", file=sys.stderr)
        return 1

    if not stage_result.ok:
        msg = stage_result.stderr.strip() or f"git add exited {stage_result.returncode}"
        print(f"[handoff] error: staging failed: {msg}", file=sys.stderr)
        return 1

    print(f"[handoff] Committing with message: docs: update handoff checkpoint")
    try:
        commit_result = runner.commit_handoff(
            project_root,
            message="docs: update handoff checkpoint",
        )
    except (GitCommandError, GitForbiddenError) as exc:
        print(f"[handoff] error: commit failed: {exc}", file=sys.stderr)
        return 1

    if not commit_result.ok:
        msg = commit_result.stderr.strip() or f"git commit exited {commit_result.returncode}"
        print(f"[handoff] error: commit failed: {msg}", file=sys.stderr)
        return 1

    print(f"[handoff] Committed docs/HANDOFF.md")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the Handoff MCP server over stdio.

    The MCP server is a read/write server by default: clients that hold a
    write capability (``create_checkpoint``) may create checkpoints. Pass the
    environment variable ``HANDOFF_MCP_READ_ONLY=1`` (or ``true``) to force
    read-only mode, which disables ``create_checkpoint``.
    """
    from handoff_agent.mcp.adapter import HandoffMCPAdapter
    from handoff_agent.mcp.server import MCPServer

    read_only = os.environ.get("HANDOFF_MCP_READ_ONLY", "").lower() in ("1", "true", "yes")

    try:
        adapter = HandoffMCPAdapter(project_root=args.path, read_only=read_only)
    except Exception as exc:
        print(f"[handoff] error: {exc}", file=sys.stderr)
        return 1

    if read_only:
        print("[handoff] MCP server started (read-only)", file=sys.stderr)
    else:
        print("[handoff] MCP server started (read-write)", file=sys.stderr)

    server = MCPServer(adapter)
    server.serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "config":
        return cmd_config(args)
    if args.command == "mcp":
        return cmd_mcp(args)
    if args.command == "registry":
        return cmd_registry(args)
    if args.command == "telemetry":
        return cmd_telemetry(args)
    return cmd_generate(args)


if __name__ == "__main__":
    sys.exit(main())