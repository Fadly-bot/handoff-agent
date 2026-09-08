# Release Guide

This is the release procedure for Handoff Agent. **The installer and CI never
push and never publish.** A human runs the release checklist.

## Release checklist (`v0.4.0` — Universal Handoff production release)

Required gates, all green before tagging:

- [x] Full test suite passes: `PYTHONPATH=$PWD/src:$PWD/tests .venv/bin/python -m pytest tests/`
- [x] Security suite passes (included above): secret-leak, filesystem-boundary,
      Git-safety, API-key handling, credential isolation, error handling.
- [x] Interoperability suite passes: adapter conformance, platform adapters,
      interop, conformance, **AI integration certification**.
- [x] Workflow suite passes: universal AI-to-AI state machine, continuity,
      recovery, approval boundary, audit trail.
- [x] Stress suite passes: parallel agents, stale-write races, conflicts,
      corruption, fuzz, large checkpoints/repos, long-running workflows.
- [x] Clean-install test (fresh `HANDOFF_HOME`, offline) passes.
- [x] Reinstall / uninstall flows pass; refusal of foreign `HANDOFF_HOME`.
- [x] Dependency audit: `requirements.txt` contains only `pytest`; runtime is
      standard-library only.
- [x] Secret-leak audit: no API-key values anywhere in `src/`, docs, or tests.
- [x] Python 3.11+ compatibility audit.
- [x] License verified: `LICENSE` (MIT) present.
- [x] Version mirrored at 0.4.0: `__init__.py`, `constants.py`, MCP handshake,
      adapter version, `install.sh` template, README, MCP docs.
- [x] Changelog finalized: `CHANGELOG.md` `[v0.4.0]` section present and dated.
- [x] Release notes drafted: `docs/RELEASE_NOTES.md`.

## Tagging & notes

- Tag: `v0.4.0` (release candidate: `v0.4.0-rc`).
- Release notes: see [docs/RELEASE_NOTES.md](./RELEASE_NOTES.md);
  summarize the AI integration certification, universal workflow, stress &
  recovery validation, docs, and security hardening added in v0.3.0 → v0.4.0.
  Reference [CHANGELOG.md](../CHANGELOG.md).
- GitHub release: prepared with the release notes and install instructions
  (`bash install.sh`), but **never triggered automatically**.

## Explicit "no automatic" guarantees

- **No automatic push.** Nothing in the codebase pushes; the only Git write
  operation in the tool is `git add -- docs/HANDOFF.md` + `git commit` under
  `--commit`, gated on explicit user request.
- **No automatic release.** Version bumps, tags, and releases are human
  decisions; the installer creates no tags or releases.

## Final release report

After tagging, produce a short report confirming:

1. tests green (count),
2. audit results (secrets, deps, git-safety, containment, isolation),
3. version consistency,
4. changelog + docs present,
5. no pushes/releases performed.