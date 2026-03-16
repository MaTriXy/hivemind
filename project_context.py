"""
project_context.py — Loads project-specific context for agent system prompts.

Reads CLAUDE.md, README.md, or .hivemind/PROJECT_MANIFEST.md from a project
directory and injects it into agent system prompts so agents understand
the codebase they're working in.

Validation: PROJECT_MANIFEST.md is checked for contamination — if it contains
concepts from the hivemind orchestration system itself (agents, schedules,
WSEvent, etc.) it is rejected and the next file in priority order is tried.
This prevents a manifest written by a confused agent from poisoning future agents.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Files to check (in priority order).
# CLAUDE.md always wins — it is human-authored and authoritative.
_CONTEXT_FILES = [
    "CLAUDE.md",
    ".hivemind/PROJECT_MANIFEST.md",
    "README.md",
]

_MAX_CHARS = 3000  # Don't bloat prompts

# Keywords that indicate a PROJECT_MANIFEST.md was written with hivemind
# domain knowledge instead of the actual project's domain. If found, we reject
# the manifest and fall through to the next context file.
_MANIFEST_CONTAMINATION_MARKERS = [
    "AgentState",
    "LiveState",
    "WSEvent",
    "ScheduleFrequency",
    "/api/projects",
    "/api/schedules",
    "ProjectCard",
    "useProjects",
    "dag_executor",
    "orchestrator",
    "skill_registry",
    "pm_agent",
    "hivemind_agent",
]


def _is_manifest_contaminated(content: str) -> bool:
    """Return True if the manifest appears to contain hivemind domain concepts."""
    for marker in _MANIFEST_CONTAMINATION_MARKERS:
        if marker in content:
            logger.warning(
                f"[context] PROJECT_MANIFEST.md rejected — contains contamination marker: {marker!r}"
            )
            return True
    return False


def load_project_context(project_dir: str) -> str:
    """
    Load project-specific context from the project directory.

    Returns a formatted string ready to inject into a system prompt,
    or empty string if nothing found.
    """
    proj = Path(project_dir)

    for filename in _CONTEXT_FILES:
        filepath = proj / filename
        if not filepath.exists():
            continue
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")

            # Reject contaminated manifests — try the next file instead.
            if filename == ".hivemind/PROJECT_MANIFEST.md" and _is_manifest_contaminated(content):
                continue

            if len(content) > _MAX_CHARS:
                content = content[:_MAX_CHARS] + "\n... (truncated)"
            logger.debug(f"[context] Loaded project context from {filename} ({len(content)} chars)")
            return f"\n## Project Context ({filename})\n\n{content}\n"
        except Exception as e:
            logger.warning(f"[context] Could not read {filepath}: {e}")
            continue

    return ""


def build_project_header(project_name: str, project_dir: str) -> str:
    """Build the full project boundary + context header for system prompts."""
    context = load_project_context(project_dir)
    header = (
        f"⚠️ PROJECT BOUNDARY: You are working exclusively on '{project_name}'.\n"
        f"ALL file operations MUST stay within: {project_dir}\n"
        "Never read, write, or reference files outside this directory.\n"
        "Never use git commands that affect other repositories.\n"
    )
    if context:
        header += context
    return header + "\n"
