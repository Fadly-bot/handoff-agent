# Troubleshooting Guide

Every failure mode here is reproduced and covered by the test suite so the
diagnosis below is authoritative.

## "Unknown provider / unknown model"

Providers and models are validated, and there is **no automatic fallback**.

- Check `handoff config` — a provider must be installed in your config.
- Check the provider name against `src/handoff_agent/adapters/platforms.py`
  (`PLATFORM_SPECS`) or run an integration check (the integration report lists
  all registered specs).
- Pass `--provider NAME --model NAME`. Model overrides are validated against
  the provider's model catalog; free-form models are accepted only for
  providers that declare no fixed catalog.

## "Provider authentication failed"

- The provider expects an API key in the documented env var (see
  docs/SECURITY.md). Only the env-var *name* is ever shown — the value is
  never printed or persisted.
- In the mock/CI test mode (`HANDOFF_LIVE_TESTS` unset) authentication is
  verified structurally (env var present) and always passes; live checks only
  run when `HANDOFF_LIVE_TESTS=1` **and** a credential is present.

## "Checkpoint changed" / stale checkpoint (AdapterPermissionError)

- The checkpoint on disk differs from the base you expected. It was updated
  by another agent (or externally) since you read it.
- Refuse to overwrite: re-fetch the latest checkpoint, merge the delta
  consciously, then write with the new base. See docs/CONFLICT_RESOLUTION.md.

## "Conflict detected" / continuation refused

- Your image of the project diverged from the latest checkpoint (objective,
  decisions, or Git state differ).
- Re-verify against the current checkpoint; if it moved, re-checkpoint your
  changes and re-request the handoff.

## "Expected protocol v1" / ProtocolValidationError

- The file is not a valid `universal-handoff-protocol` v1 document. It may be
  truncated, corrupted, or manually edited.
- Recovery is non-destructive: the most recent valid checkpoint + changelog
  archive is used. The corrupted file is preserved and reported, never
  silently overwritten. See docs/RECOVERY.md.

## "Capability not granted" / AdapterPermissionError going native

- You asked an adapter to do something outside its capability grant. This is
  enforcement, not a corrupt state: check the adapter's declared
  capabilities (`capability` lists) before calling it.

## "No automatic fallback" (Unknown*Error)

- Unknown platforms/providers/adapter names raise deliberately. To use a
  generic, read-only file adapter for an unregistered platform, construct one
  explicitly with a file contract (integration tests show how) — never
  silently fall back.

## "Secret-like content refused"

- Handoff refuses to read or write content that resembles a hardcoded secret
  (API key, token, password, private key). Remove the literal secret from the
  checkpoint; use an env-var reference or note the key *name* only.

## "Handoff requests human approval"

- `complete()` requires `human_approved=True`. Handoff acceptance requires the
  explicit token returned by `request_handoff`. These are deliberate
  boundaries — see docs/WORKFLOW.md.

## Slow or heavy operations

- The full validation/stress/release suite (`pytest tests/ -q`) takes several
  minutes and builds many isolated repositories. Run subsets while iterating,
  e.g. `pytest tests/test_audit.py -q`, and give the suite a generous timeout
  (≥ 900 s) when running everything.