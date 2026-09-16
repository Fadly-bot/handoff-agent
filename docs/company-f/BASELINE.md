# Development Company F — Baseline (revised roadmap)

Status: **PASS**.

## Repository audit performed

Read: README.md, PHASE30-36.md (old roadmap), REVISIPHASE30-36.md (revised
roadmap), CHANGELOG.md, docs/ (PROTOCOL, ADAPTERS, WORKFLOW, RECOVERY,
CONFLICT_RESOLUTION, SECURITY, CLI_GUIDE, MCP, AI_INTEGRATION, COMPATIBILITY,
CONFORMANCE, INTEROPERABILITY, ORCHESTRATION, DELEGATION, MIGRATION,
RELEASE, VERSIONING), install.sh, bin/handoff, and the modular sources
(adapters, capability, cli, conformance, delegation, integration, interop,
mcp, messaging, orchestration, persistence, providers, protocol, registry,
remote, security, sync, telemetry, workflow, workflow_engine).

Phase 1–29 checkpoint evidence: CHANGELOG.md lineage from `feat: phases 11-22`
(5b01d61) through `feat(phase 29)` (1dc2a0a), plus the passing full test suite.

## Full test suite before fixes

```
1567 collected, 1564 passed, 3 failed
```

## Identified failures

1. `tests/test_release.py::TestInstallUninstallFlow::test_clean_install`
2. `tests/test_release.py::TestInstallUninstallFlow::test_fresh_environment_gets_default_config`
3. `tests/test_release.py::TestInstallUninstallFlow::test_reinstall_preserves_existing_config`

Root cause: this host's `python3` build has **no `ensurepip`** (system
`python3.14-venv` unavailable, no apt candidate, no sudo on this account), so
`python3 -m venv` aborts during installation.

Secondary issue found during baseline: the integration certification test
`TestLiveSeparation::test_mock_mode_sets_live_case_skipped` (and three related
live-separation tests + one suite test in `test_audit.py`) provisioned the file
adapter against the **repository root**, so running the regression itself mutated
`docs/HANDOFF.md` and `docs/CHANGELOG.md` — making the per-phase "Git working
tree bersih" checkpoint unachievable by the suite it claims.

## Fixes applied

- `install.sh` — creates the virtual environment with the standard `venv`
  builder; when `ensurepip` is unavailable the same builder runs with
  `with_pip=False` (a stdlib-only venv). The runtime has no third-party
  dependencies, so this is functionally equivalent for install; no `pip`,
  `curl`, `wget`, or network tooling is introduced (the offline-install audit
  still passes). On host with `ensurepip` the behavior is unchanged.
- `tests/test_integration.py` — the four live-separation certification tests
  and the secret-serialization test now run against an isolated temporary git
  repo (`project_root=tmp repo`) instead of the real repository.
- `tests/test_audit.py::test_integration_suite_never_serializes_secret_values`
  — same hermetic project_root fix.

## Full test suite after fixes

```
1567 collected, 1606 passed*, 0 failed   (* includes Phase 30 tests added later)
```

After the three env fixes alone: `1567 passed in 85.91s`.

## Security audit (baseline)

Passed via the existing release/security/conformance suites:

- Secret/credential scan of source, install/uninstall scripts, README, docs,
  requirements: `TestSecretLeakAudit`, `test_security.py` — PASS
- Dependency audit: stdlib-only runtime, `requirements.txt` pins pytest only —
  PASS
- Git-safety audit: forbidden commands declared, no destructive subprocess
  argv — PASS
- Filesystem-boundary audit: no `shell=True`, no `eval/exec`, path
  containment — PASS
- API-key handling: env names only, values never in config/errors — PASS
- Network boundary: only provider adapters open HTTPS; no arbitrary URLs — PASS
- Approval/permission/trust: enforced by the existing capability contract and
  the new Company F coordination contract (Phase 30).

## Git status at baseline

Clean working tree for all tracked files; only intended baseline fixes and new
untracked scope docs (AUDIT.md, REVISIPHASE30-36.md, audit1.md) present.
No unrelated changes introduced.

## Known limitations / risks

- This host cannot execute the venv-with-`ensurepip` install path; the
  `--without-pip` fallback is covered by the passing install tests on this
  host. A full-pip install should be re-verified on a standard host in
  Phase 36 (installation/upgrade validation).
- Baseline is on `master` at `22a5cf9` (Phase 30 old-roadmap telemetry was the
  last push; the revised roadmap continues from here).