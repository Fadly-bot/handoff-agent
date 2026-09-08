"""Tests for Phase 13 — Universal Skill Adapter.

Covers skill package validation, required sections, secret detection,
provider independence, portable package integrity, and skill discovery.
"""

from __future__ import annotations

import pytest

from handoff_agent.skill import (
    REQUIRED_SECTIONS,
    SCHEMA_FILENAME,
    SKILL_FILENAME,
    SkillIssue,
    SkillValidationReport,
    SkillValidationError,
    discover_skill_packages,
    load_skill_metadata,
    validate_skill,
)


# ---------------------------------------------------------------------------
# Fixture: locate the real skill package
# ---------------------------------------------------------------------------

import os
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILL_PACKAGE = SKILLS_DIR / "universal-handoff"


class TestSkillPackageExists:
    def test_skill_directory_exists(self) -> None:
        assert SKILL_PACKAGE.is_dir()

    def test_skill_md_exists(self) -> None:
        assert (SKILL_PACKAGE / SKILL_FILENAME).is_file()

    def test_protocol_md_exists(self) -> None:
        assert (SKILL_PACKAGE / SCHEMA_FILENAME).is_file()


class TestSkillValidation:
    def test_valid_package(self) -> None:
        report = validate_skill(SKILL_PACKAGE)
        assert report.ok
        assert len(report.errors) == 0
        assert len(report.missing_files) == 0

    def test_all_required_sections_found(self) -> None:
        report = validate_skill(SKILL_PACKAGE)
        assert len(report.sections_missing) == 0
        assert len(report.sections_found) > 0

    def test_no_secret_content(self) -> None:
        report = validate_skill(SKILL_PACKAGE)
        secret_errors = [i for i in report.errors if "secret" in i.message.lower()]
        assert len(secret_errors) == 0

    def test_no_provider_specific_content(self) -> None:
        report = validate_skill(SKILL_PACKAGE)
        provider_warnings = [i for i in report.warnings if "provider" in i.message.lower()]
        # Warnings are acceptable, but let's confirm none are errors
        provider_errors = [
            i for i in report.errors if "provider" in i.message.lower()
        ]
        assert len(provider_errors) == 0


class TestMissingFile:
    def test_missing_skill_md(self, tmp_path: Path) -> None:
        # Create a package dir without SKILL.md
        pkg = tmp_path / "empty-skill"
        pkg.mkdir()
        (pkg / "PROTOCOL.md").write_text("Content")
        report = validate_skill(pkg)
        assert not report.ok
        assert any(SKILL_FILENAME in i.field for i in report.errors)

    def test_missing_protocol_md(self, tmp_path: Path) -> None:
        pkg = tmp_path / "no-schema"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text("# My Skill\n\n## Overview\nSome text.\n")
        report = validate_skill(pkg)
        assert not report.ok
        assert any(SCHEMA_FILENAME in i.field for i in report.errors)


class TestSecretDetection:
    def test_secret_in_skill(self, tmp_path: Path) -> None:
        pkg = tmp_path / "leaky"
        pkg.mkdir()
        skill = pkg / SKILL_FILENAME
        skill.write_text(
            "# Leaky\n\napi_key = \"sk-supersecretkeyvalue1234567890\"\n"
            "## Overview\nok\n## Read-Before-Continue\nok\n"
            "## Verify-Before-Trust\nok\n## Milestone Checkpoints\nok\n"
            "## AI Switching\nok\n## HANDOFF.md Interpretation\nok\n"
            "## CHANGELOG.md Interpretation\nok\n## State Schema Interpretation\nok\n"
            "## Capability Awareness\nok\n## Safety Rules\nok\n"
            "## Provider-Independent\nok\n## Portable Skill Package\nok\n"
        )
        (pkg / SCHEMA_FILENAME).write_text("Schema content")
        report = validate_skill(pkg)
        assert not report.ok
        assert any("secret" in i.message.lower() for i in report.errors)


class TestProviderIndependence:
    def test_provider_specific_content_warns(self, tmp_path: Path) -> None:
        pkg = tmp_path / "provider-aware"
        pkg.mkdir()
        skill_content = (
            "# Provider Skill\n\nThis works with ANTHROPIC_API_KEY.\n"
            "## Overview\nok\n## Read-Before-Continue\nok\n"
            "## Verify-Before-Trust\nok\n## Milestone Checkpoints\nok\n"
            "## AI Switching\nok\n## HANDOFF.md Interpretation\nok\n"
            "## CHANGELOG.md Interpretation\nok\n## State Schema Interpretation\nok\n"
            "## Capability Awareness\nok\n## Safety Rules\nok\n"
            "## Provider-Independent\nok\n## Portable Skill Package\nok\n"
        )
        (pkg / SKILL_FILENAME).write_text(skill_content)
        (pkg / SCHEMA_FILENAME).write_text("Schema")
        report = validate_skill(pkg)
        # Provider-specific content should produce a warning
        provider_issues = [
            i for i in report.issues if "provider" in i.message.lower()
        ]
        assert len(provider_issues) >= 1


class TestMissingSections:
    def test_incomplete_sections(self, tmp_path: Path) -> None:
        pkg = tmp_path / "incomplete"
        pkg.mkdir()
        # Minimal SKILL.md with only a few sections
        (pkg / SKILL_FILENAME).write_text(
            "# Incomplete\n\n## Overview\nSome text.\n"
        )
        (pkg / SCHEMA_FILENAME).write_text("Schema")
        report = validate_skill(pkg)
        assert not report.ok
        assert len(report.sections_missing) > 0
        # At minimum these critical sections must be missing
        missing_names = [s.lower() for s in report.sections_missing]
        assert "read-before-continue" in missing_names


class TestPathTraversal:
    def test_traversal_rejected(self, tmp_path: Path) -> None:
        pkg = tmp_path / "traversal"
        pkg.mkdir()
        (pkg / SKILL_FILENAME).write_text(
            "# Traversal\n\n## Overview\nok\n## Read-Before-Continue\nok\n"
            "## Verify-Before-Trust\nok\n## Milestone Checkpoints\nok\n"
            "## AI Switching\nok\n## HANDOFF.md Interpretation\nok\n"
            "## CHANGELOG.md Interpretation\nok\n## State Schema Interpretation\nok\n"
            "## Capability Awareness\nok\n## Safety Rules\nok\n"
            "## Provider-Independent\nok\n## Portable Skill Package\nok\n"
        )
        (pkg / SCHEMA_FILENAME).write_text("Schema")
        # Create a file outside the skill root via symlink
        external = tmp_path / "external.txt"
        external.write_text("external")
        link = pkg / "escape.txt"
        try:
            link.symlink_to(external)
        except OSError:
            pytest.skip("symlinks not supported")
        report = validate_skill(pkg)
        assert not report.ok
        assert any("escape" in i.message.lower() or "traversal" in i.message.lower() for i in report.errors)


class TestDiscovery:
    def test_discover_in_skills_dir(self) -> None:
        packages = discover_skill_packages(SKILLS_DIR)
        assert len(packages) >= 1
        names = [p.name for p in packages]
        assert "universal-handoff" in names

    def test_discover_empty_dir(self, tmp_path: Path) -> None:
        packages = discover_skill_packages(tmp_path)
        assert packages == []


class TestMetadata:
    def test_load_metadata(self) -> None:
        meta = load_skill_metadata(SKILL_PACKAGE)
        assert meta["name"] == "Universal Handoff Skill"
        assert len(meta["sections"]) > 0

    def test_load_metadata_nonexistent(self, tmp_path: Path) -> None:
        meta = load_skill_metadata(tmp_path / "nope")
        assert meta["name"] == "unknown"


class TestReportDeterministic:
    def test_report_dict_is_stable(self) -> None:
        r1 = validate_skill(SKILL_PACKAGE)
        r2 = validate_skill(SKILL_PACKAGE)
        assert r1.to_dict() == r2.to_dict()


class TestSkillPackageFileContent:
    def test_skill_md_not_empty(self) -> None:
        content = (SKILL_PACKAGE / SKILL_FILENAME).read_text(encoding="utf-8")
        assert len(content) > 500

    def test_protocol_md_not_empty(self) -> None:
        content = (SKILL_PACKAGE / SCHEMA_FILENAME).read_text(encoding="utf-8")
        assert len(content) > 200

    def test_skill_md_references_protocol(self) -> None:
        content = (SKILL_PACKAGE / SKILL_FILENAME).read_text(encoding="utf-8")
        assert "universal-handoff-protocol" in content
