"""Tests for Phase 17 — Cross-AI Interoperability helpers (interop.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import init_repo, commit_all, write_file

from handoff_agent.adapters import create_adapter
from handoff_agent.interop import (
    CheckpointSnapshot,
    compare_states,
    cross_adapter_round_trip,
    detect_conflict,
    interop_summary,
    is_stale,
    snapshot_from_adapter,
    snapshot_from_text,
    state_consistency,
)
from handoff_agent.protocol import build_checkpoint, render_state_block


def checkpoint_markdown(objective: str, project: str = "repo") -> str:
    cp = build_checkpoint(objective=objective, project_name=project)
    return "# Test\n\n" + render_state_block(cp)


class TestSnapshot:
    def test_snapshot_from_text(self) -> None:
        snap = snapshot_from_text(checkpoint_markdown("alpha"))
        assert snap is not None
        assert snap.objective == "alpha"
        assert snap.protocol == "universal-handoff-protocol"
        assert snap.protocol_version == 1
        assert snap.identity
        assert snap.sequence >= 0
        assert snap.state  # canonical schema present

    def test_snapshot_none_for_missing(self) -> None:
        assert snapshot_from_text(None) is None
        assert snapshot_from_text("# plain markdown") is None

    def test_snapshot_identity_stable(self) -> None:
        a = snapshot_from_text(checkpoint_markdown("alpha"))
        b = snapshot_from_text(checkpoint_markdown("alpha"))
        assert a is not None and b is not None
        assert a.identity == b.identity

    def test_snapshot_from_adapter(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = create_adapter("file", project_root=str(repo))
        ad.start()
        assert snapshot_from_adapter(ad) is None
        ad.write_checkpoint(checkpoint_markdown("alpha"))
        snap = snapshot_from_adapter(ad)
        assert snap is not None
        assert snap.objective == "alpha"

    def test_snapshot_to_dict(self) -> None:
        snap = snapshot_from_text(checkpoint_markdown("x"))
        assert snap is not None
        d = snap.to_dict()
        assert set(d) == {
            "identity", "sequence", "objective",
            "protocol", "protocol_version", "state",
        }


class TestStaleAndConflict:
    def test_is_stale(self) -> None:
        assert is_stale(None, "abc") is False
        assert is_stale("abc", "abc") is False
        assert is_stale("abc", "xyz") is True

    def test_detect_conflict_no_common_base_change(self) -> None:
        base = "b"
        r = detect_conflict(base, checkpoint_markdown("ours"), checkpoint_markdown("ours"))
        assert r.conflicting is False

    def test_detect_conflict_fast_forward(self) -> None:
        base_md = checkpoint_markdown("base")
        base = snapshot_from_text(base_md)
        assert base is not None
        # Ours has not advanced (still equals base); theirs has.
        r = detect_conflict(base.identity, base_md, checkpoint_markdown("their-new"))
        assert not r.conflicting

    def test_detect_conflict_real_conflict(self) -> None:
        base = "base-identity"
        ours = checkpoint_markdown("ours")
        theirs = checkpoint_markdown("theirs")
        r = detect_conflict(base, ours, theirs)
        assert r.conflicting is True
        assert "manual merge" in r.reason

    def test_detect_conflict_missing_side(self) -> None:
        r = detect_conflict("base", checkpoint_markdown("ours"), None)
        assert r.conflicting is False

    def test_conflict_report_to_dict(self) -> None:
        r = detect_conflict("base", checkpoint_markdown("ours"), checkpoint_markdown("theirs"))
        d = r.to_dict()
        assert d["conflicting"] is True
        assert set(d) == {"base_identity", "ours", "theirs", "conflicting", "reason"}


class TestStateComparison:
    def test_equal_states(self) -> None:
        r = compare_states({"a": 1, "b": "x"}, {"a": 1, "b": "x"})
        assert r["equal"] is True

    def test_changed_values(self) -> None:
        r = compare_states({"a": 1}, {"a": 2})
        assert r["equal"] is False
        assert r["changed"]["a"] == {"before": 1, "after": 2}

    def test_only_in_side_keys(self) -> None:
        r = compare_states({"a": 1}, {"b": 2})
        assert r["only_in_first"] == {"a": 1}
        assert r["only_in_second"] == {"b": 2}


class TestConsistency:
    def test_state_consistency_valid(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = create_adapter("file", project_root=str(repo))
        ad.start()
        ad.write_checkpoint(checkpoint_markdown("alpha"))
        result = state_consistency(ad)
        assert result["consistent"] is True
        assert result["valid"] is True
        assert result["identity_verified"] is True

    def test_state_consistency_corrupt(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = create_adapter("file", project_root=str(repo))
        ad.start()
        ad.write_checkpoint(checkpoint_markdown("alpha"))
        (repo / "docs" / "HANDOFF.md").write_text("# bad", encoding="utf-8")
        result = state_consistency(ad)
        assert result["consistent"] is False
        assert result["valid"] is False


class TestRoundTrip:
    def test_cross_adapter_round_trip(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        init_repo(src)
        init_repo(dst)
        a = create_adapter("file", project_root=str(src))
        b = create_adapter("file", project_root=str(dst))
        a.start()
        b.start()
        a.write_checkpoint(checkpoint_markdown("alpha"))
        result = cross_adapter_round_trip(a, b)
        assert result["round_trip"] is True
        assert result["created"] is True
        assert result["identity"]
        assert b.read_handoff() == a.read_handoff()

    def test_interop_summary(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        init_repo(repo)
        ad = create_adapter("file", project_root=str(repo))
        ad.start()
        ad.write_checkpoint(checkpoint_markdown("alpha"))
        s = interop_summary(ad)
        assert s["protocol"] == {"name": "universal-handoff-protocol", "version": 1}
        assert s["snapshot"]["objective"] == "alpha"
        assert s["consistency"]["consistent"] is True
        assert s["adapter"] == "file"