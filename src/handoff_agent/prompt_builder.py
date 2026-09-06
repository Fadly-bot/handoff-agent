"""Prompt Builder — converts a FullContext into a deterministic prompt.

The PromptBuilder ONLY transforms a ``FullContext`` into a prompt string. It
MUST NOT:
  - access the filesystem;
  - run Git commands;
  - read environment variables;
  - discover secrets;
  - scan files.

Only source content that already passed the SecurityFilter (available in
``FullContext.files[].content``) is allowed into the prompt. File contents
appear in ``FullContext`` only after being confirmed safe.

Output is deterministic: the same ``FullContext`` always produces the same
prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from handoff_agent.context_builder import FullContext

SYSTEM_INSTRUCTION = (
    "You are an expert software engineering assistant generating a project "
    "handoff document. Use the repository context below to produce an accurate, "
    "well-structured handoff summary. Do not invent files or facts not present "
    "in the provided context."
)


class PromptBuilder:
    """Builds a deterministic prompt from a ``FullContext``."""

    def build_system(self) -> str:
        """Return the static system instructions."""
        return SYSTEM_INSTRUCTION

    def build_user(self, context: "FullContext") -> str:
        """Build the user-facing repository context prompt.

        Deterministic: iterates files in their stored (already sorted) order
        and never touches the filesystem.
        """
        lines: list[str] = []

        # --- Project metadata ---
        lines.append("# Project")
        lines.append(f"- name: {context.project.name}")
        lines.append(f"- type: {context.project.project_type}")
        lines.append(f"- root: {context.project.root}")
        lines.append("")

        # --- Git metadata ---
        lines.append("# Repository state")
        lines.append(f"- branch: {context.git.branch or '(detached)'}")
        lines.append(f"- head commit: {context.git.head or '(none)'}")
        lines.append(f"- working tree: {'clean' if context.git.clean else context.git.status}")
        lines.append("")
        if context.git.untracked_files:
            lines.append(f"- untracked files ({len(context.git.untracked_files)}):")
            for f in context.git.untracked_files:
                lines.append(f"    - {f}")
            lines.append("")
        if context.git.recent_commits:
            lines.append(f"- recent commits ({len(context.git.recent_commits)}):")
            for c in context.git.recent_commits:
                lines.append(f"    - {c.get('short_hash', '')} {c.get('subject', '')}")
            lines.append("")

        # --- Files context ---
        lines.append("# File contents")
        lines.append("")
        if not context.files:
            lines.append("(no file contents provided)")
            lines.append("")
        for idx, file_entry in enumerate(context.files, start=1):
            separator = "-" * 4
            lines.append(f"[{idx}] {separator} {file_entry.path} {separator}")
            lines.append(file_entry.content)
            lines.append("")

        return "\n".join(lines).rstrip("\n")

    def build(self, context: "FullContext") -> str:
        """Return the full combined prompt (system + user content)."""
        system = self.build_system()
        user = self.build_user(context)
        return f"{system}\n\n---\n\n{user}"