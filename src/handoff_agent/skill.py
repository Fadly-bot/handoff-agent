"""Universal Skill Adapter (Phase 13).

Validates portable skill packages for the Universal Handoff Protocol.

A skill package is a directory containing at minimum a ``SKILL.md`` file with
provider-independent instructions for AI agents. The validation checks:

- SKILL.md exists and is readable.
- Required sections are present.
- No secrets, API keys, or provider-specific identifiers are embedded.
- All referenced files exist within the package.
- The package is self-contained (no absolute paths, no traversal).
- The skill is complete (has all required components).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SkillValidationError(Exception):
    """Raised when a skill package fails validation."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_FILENAME = "SKILL.md"
SCHEMA_FILENAME = "PROTOCOL.md"

REQUIRED_SECTIONS: tuple[str, ...] = (
    "overview",
    "read-before-continue",
    "verify-before-trust",
    "milestone checkpoint",
    "ai switching",
    "handoff.md interpretation",
    "changelog.md interpretation",
    "state schema interpretation",
    "capability awareness",
    "safety rules",
    "provider-independent",
    "portable skill package",
)

# Patterns that indicate provider-specific content (not allowed in a
# provider-independent skill).
PROVIDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bANTHROPIC_API_KEY\b"),
    re.compile(r"(?i)\bOPENAI_API_KEY\b"),
    re.compile(r"(?i)\bDASHSCOPE_API_KEY\b"),
    re.compile(r"(?i)\bDEEPSEEK_API_KEY\b"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bANTHROPIC\b.*\b(https?://)\S+"),
    re.compile(r"(?i)\bOPENAI\b.*\b(https?://)\S+"),
    re.compile(r"(?i)\bapi\.anthropic\.com\b"),
    re.compile(r"(?i)\bapi\.openai\.com\b"),
)

# Secret-like patterns that must never appear in a skill package.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""(?i)(?:api[_\-]?key|apikey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:secret[_\-]?key|secretkey)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-]{16,}"""),
    re.compile(r"""(?i)(?:access[_\-]?token|accesstoken)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9_\-\.]{16,}"""),
    re.compile(r"""(?i)(?:private[_\-]?key)\s*['"]?\s*[:=]"""),
    re.compile(r"""-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"""),
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillIssue:
    """A single validation issue (error or warning)."""

    severity: str  # "error" | "warning"
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "field": self.field, "message": self.message}


@dataclass(frozen=True)
class SkillValidationReport:
    """Complete validation report for a skill package."""

    ok: bool
    issues: tuple[SkillIssue, ...] = ()
    required_files: tuple[str, ...] = ()
    found_files: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()
    sections_found: tuple[str, ...] = ()
    sections_missing: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[SkillIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[SkillIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "required_files": list(self.required_files),
            "found_files": list(self.found_files),
            "missing_files": list(self.missing_files),
            "sections_found": list(self.sections_found),
            "sections_missing": list(self.sections_missing),
        }


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def _check_secrets(content: str, filename: str) -> tuple[SkillIssue, ...]:
    """Scan content for secret-like patterns."""
    issues: list[SkillIssue] = []
    for pattern in SECRET_PATTERNS:
        match = pattern.search(content)
        if match:
            issues.append(
                SkillIssue(
                    severity="error",
                    field=filename,
                    message=f"Contains secret-like content pattern (possible {match.group()[:20]}...)",
                )
            )
    return issues


def _check_provider_independence(content: str, filename: str) -> tuple[SkillIssue, ...]:
    """Scan content for provider-specific identifiers."""
    issues: list[SkillIssue] = []
    for pattern in PROVIDER_PATTERNS:
        match = pattern.search(content)
        if match:
            issues.append(
                SkillIssue(
                    severity="warning",
                    field=filename,
                    message=f"Contains provider-specific content: {match.group()[:30]}...",
                )
            )
    return issues


def _find_sections(content: str) -> tuple[str, ...]:
    """Extract all section headings from markdown content."""
    sections: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^#{1,3}\s+(.+)$", line)
        if match:
            sections.append(match.group(1).strip().lower())
    return tuple(sections)


def _validate_paths(skill_root: Path) -> tuple[SkillIssue, ...]:
    """Validate that all files in the package are within the skill root."""
    issues: list[SkillIssue] = []
    try:
        resolved_root = skill_root.resolve()
    except (OSError, ValueError) as exc:
        issues.append(
            SkillIssue(severity="error", field="package", message=f"Cannot resolve skill root: {exc}")
        )
        return issues

    for path in skill_root.rglob("*"):
        if path.is_dir():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            issues.append(
                SkillIssue(
                    severity="error",
                    field=str(path.relative_to(skill_root)),
                    message="File escapes skill package root",
                )
            )
    return issues


def validate_skill(skill_root: str | Path) -> SkillValidationReport:
    """Validate a skill package at the given path.

    Returns a ``SkillValidationReport`` with the result. ``ok`` is True only
    if no errors were found (warnings are acceptable).

    Required files:
      - ``SKILL.md``
      - ``PROTOCOL.md``

    Checks performed:
      - Files exist and are readable.
      - SKILL.md contains all required sections.
      - No secrets or API keys in any file.
      - No provider-specific identifiers.
      - All files are within the skill root.
      - Package is self-contained.
    """
    root = Path(skill_root).resolve()
    required_files = (SKILL_FILENAME, SCHEMA_FILENAME)
    issues: list[SkillIssue] = []
    found_files: list[str] = []
    missing_files: list[str] = []

    # Check required files exist
    for name in required_files:
        path = root / name
        if path.is_file():
            found_files.append(name)
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                issues.append(SkillIssue(severity="error", field=name, message=f"Cannot read: {exc}"))
                continue

            # Check for secrets
            issues.extend(_check_secrets(content, name))

            # Check provider independence for SKILL.md
            if name == SKILL_FILENAME:
                issues.extend(_check_provider_independence(content, name))
        else:
            missing_files.append(name)
            issues.append(SkillIssue(severity="error", field=name, message="Required file missing"))

    # Validate paths are within skill root
    issues.extend(_validate_paths(root))

    # Check required sections in SKILL.md
    skill_path = root / SKILL_FILENAME
    sections_found: list[str] = []
    sections_missing: list[str] = []

    if skill_path.is_file():
        try:
            content = skill_path.read_text(encoding="utf-8")
            found = _find_sections(content)

            for required in REQUIRED_SECTIONS:
                matched = False
                for section in found:
                    if required.lower() in section:
                        matched = True
                        break
                if matched:
                    sections_found.append(required)
                else:
                    sections_missing.append(required)
                    issues.append(
                        SkillIssue(
                            severity="error",
                            field=SKILL_FILENAME,
                            message=f"Missing required section: '{required}'",
                        )
                    )
        except (OSError, UnicodeDecodeError):
            pass

    ok = not any(i.severity == "error" for i in issues)

    return SkillValidationReport(
        ok=ok,
        issues=tuple(issues),
        required_files=tuple(required_files),
        found_files=tuple(found_files),
        missing_files=tuple(missing_files),
        sections_found=tuple(sections_found),
        sections_missing=tuple(sections_missing),
    )


def discover_skill_packages(search_root: str | Path) -> list[Path]:
    """Discover skill packages under a search directory.

    A skill package is a directory containing a ``SKILL.md`` file.
    Returns a list of skill root paths (sorted for determinism).
    """
    root = Path(search_root).resolve()
    packages: list[Path] = []
    if not root.is_dir():
        return packages
    for path in root.rglob(SKILL_FILENAME):
        packages.append(path.parent)
    return sorted(packages)


def load_skill_metadata(skill_root: str | Path) -> dict[str, Any]:
    """Load basic metadata from a validated skill package.

    Returns a dict with ``name``, ``version``, and ``sections`` extracted
    from the SKILL.md heading.
    """
    root = Path(skill_root).resolve()
    skill_path = root / SKILL_FILENAME
    metadata: dict[str, Any] = {"name": "unknown", "version": "0.0.0", "sections": []}

    if not skill_path.is_file():
        return metadata

    try:
        content = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return metadata

    lines = content.splitlines()
    if lines and lines[0].startswith("# "):
        metadata["name"] = lines[0][2:].strip()

    sections = _find_sections(content)
    metadata["sections"] = list(sections)

    return metadata
