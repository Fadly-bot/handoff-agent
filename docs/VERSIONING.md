# Versioning Policy

## Package versioning

Handoff Agent uses semantic versioning (`MAJOR.MINOR.PATCH`).

- **MAJOR** — breaking protocol state format, breaking CLI, or removal of a
  documented interface.
- **MINOR** — additive, backward-compatible features (new adapters, platforms,
  tools, capabilities, docs).
- **PATCH** — bug fixes and security hardening only.

Version policy rules:

- The version lives in exactly one place per surface: `__version__`
  (`src/handoff_agent/__init__.py`) and `VERSION` (`constants.py`). MCP
  handshake, installer template, README, and docs must mirror it.
- Every release bumps the `CHANGELOG.md` with the same version.
- Never change a released version in place. If a release-candidate problem is
  found, cut a new `PATCH`/`MINOR` version.
- A release candidate (e.g. `v0.4.0-rc`) is never auto-released or auto-pushed.

## Protocol versioning

The protocol is `universal-handoff-protocol`, currently version `1`
(`PROTOCOL_VERSION = 1`; `PROTOCOL_VERSIONS_SUPPORTED = (1,)`).

- Increment `PROTOCOL_VERSION` only for a **breaking** change to the
  machine-readable schema or the identity algorithm.
- Additive schema fields are backward compatible and do **not** bump the
  version; validators must ignore unknown fields.
- Readers must refuse a checkpoint whose protocol version is unsupported
  (`ProtocolVersionError`).
- `checkpoint_identity()` must stay deterministic: the identity is computed
  from state + metadata, never from timestamps — so identities are stable
  across acknowledgements, writers, and platforms.
- Compatibility with legacy `docs/HANDOFF.md` (no machine-readable block)
  is preserved indefinitely: legacy checkpoints are read and flagged
  "unvalidated legacy", never written back silently as if canonical.

## Adapter versioning

Every adapter carries `adapter_name` + `adapter_version` (semver).

- A MINOR version bump of an adapter is backward compatible: existing
  registration names (`file`, `cli`, `api`) never change semantics between
  MINOR releases.
- Adapter registration is **strictly non-overwritable**: registering an
  already-registered name raises `AdapterConfigError` — a new adapter needs a
  new name.
- Platform adapter declarations (name + display name + interface support) are
  contract; changes to interface support are a MINOR+ bump for the platform
  and MUST be reflected in `docs/COMPATIBILITY.md`.
- There is **no automatic fallback**: an unknown platform/adapter name is an
  error, never a silent substitute.

## Tooling support

`src/handoff_agent/adapters/platforms.py` exposes
`PLATFORM_SCHEMA_VERSION = "1"` so tooling can validate the declarative
platform table independently of the package version.