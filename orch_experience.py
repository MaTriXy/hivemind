"""Experience & task ledger management for the orchestrator.

Extracted from orchestrator.py to reduce file size.
All functions operate on an OrchestratorManager instance passed as `mgr`.
This module handles:
  - Task ledger (.hivemind/todo.md) — persistent task state tracking
  - Experience ledger (.hivemind/.experience.md) — cross-session memory
  - Reflection generation (Reflexion pattern)
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Task Ledger (.hivemind/todo.md) ──────────────────────────────────────


def get_todo_path(mgr) -> Path:
    """Get the path to the task ledger file."""
    forge_dir = Path(mgr.project_dir) / ".hivemind"
    forge_dir.mkdir(parents=True, exist_ok=True)
    return forge_dir / "todo.md"


def read_todo(mgr) -> str:
    """Read the current task ledger. Returns empty string if not found."""
    todo_path = get_todo_path(mgr)
    if todo_path.exists():
        try:
            return todo_path.read_text(encoding="utf-8").strip()
        except Exception as _exc:
            logger.debug("[Orchestrator] non-fatal exception suppressed: %s", _exc)
    return ""


def write_todo(mgr, content: str):
    """Write the task ledger. Creates .hivemind/ dir if needed."""
    try:
        todo_path = get_todo_path(mgr)
        todo_path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.warning(f"[{mgr.project_id}] Failed to write todo.md: {e}")


def init_todo(mgr, user_message: str, complexity: str):
    """Initialize the task ledger at the start of a new session.

    Creates a structured todo.md with the original goal, phases,
    and a checklist that the orchestrator updates each round.
    """
    existing = read_todo(mgr)
    if existing:
        return  # Don't overwrite — this is a continuation

    phase_templates = {
        "SIMPLE": ("- [ ] Phase 1: Implement the fix/change\n- [ ] Phase 2: Verify it works\n"),
        "MEDIUM": (
            "- [ ] Phase 1: Understand the codebase and plan\n"
            "- [ ] Phase 2: Implement the changes\n"
            "- [ ] Phase 3: Review the code\n"
            "- [ ] Phase 4: Test and verify\n"
        ),
        "LARGE": (
            "- [ ] Phase 1: Architecture and planning\n"
            "- [ ] Phase 2: Core implementation\n"
            "- [ ] Phase 3: Feature implementation\n"
            "- [ ] Phase 4: Integration\n"
            "- [ ] Phase 5: Review and testing\n"
            "- [ ] Phase 6: Polish and deployment\n"
        ),
        "EPIC": (
            "- [ ] Phase 1: Architecture + read existing code + plan file structure (rounds 1-3)\n"
            "- [ ] Phase 2: Core foundation — models, DB, config (rounds 4-8)\n"
            "- [ ] Phase 3: Feature implementation — one feature at a time (rounds 9-13)\n"
            "- [ ] Phase 4: Integration — connect all pieces, error handling (rounds 14-17)\n"
            "- [ ] Phase 5: Testing — comprehensive tests, fix failures (rounds 18-22)\n"
            "- [ ] Phase 6: Polish — error handling, docs, deployment config (rounds 23+)\n"
        ),
    }

    phases = phase_templates.get(complexity, phase_templates["MEDIUM"])
    content = (
        f"# Task Ledger\n\n"
        f"## Goal\n{user_message[:1000]}\n\n"
        f"## Complexity\n{complexity}\n\n"
        f"## Phases\n{phases}\n"
        f"## Current Phase\nPhase 1\n\n"
        f"## Completed Work\n(none yet)\n\n"
        f"## Open Issues\n(none yet)\n\n"
        f"## Blocked Items\n(none yet)\n"
    )
    write_todo(mgr, content)


def update_todo_after_round(
    mgr, round_num: int, round_summary: str, findings: list[dict] | None = None
):
    """Update the task ledger after a round completes.

    Appends the round summary to 'Completed Work' and updates
    'Open Issues' based on findings from the review prompt.
    """
    current = read_todo(mgr)
    if not current:
        return

    completed_marker = "## Completed Work"
    if completed_marker in current:
        idx = current.find(completed_marker) + len(completed_marker)
        next_section = current.find("\n## ", idx)
        before = current[:idx]
        existing_work = (
            current[idx:next_section].strip() if next_section > idx else current[idx:].strip()
        )
        after = current[next_section:] if next_section > idx else ""

        if existing_work == "(none yet)":
            existing_work = ""
        new_entry = f"- Round {round_num}: {round_summary[:200]}"
        updated_work = f"{existing_work}\n{new_entry}".strip()
        current = f"{before}\n{updated_work}\n{after}"

    if findings:
        issues_marker = "## Open Issues"
        if issues_marker in current:
            idx = current.find(issues_marker) + len(issues_marker)
            next_section = current.find("\n## ", idx)
            before = current[:idx]
            after = current[next_section:] if next_section > idx else ""

            issue_lines = []
            for f in findings[:10]:
                severity = f.get("severity", "MEDIUM")
                desc = f.get("description", "")[:150]
                file_hint = f" in {f['file']}" if f.get("file") else ""
                issue_lines.append(f"- [{severity}] {desc}{file_hint}")
            issues_text = "\n".join(issue_lines) if issue_lines else "(none)"
            current = f"{before}\n{issues_text}\n{after}"

    write_todo(mgr, current)


# ── Experience Ledger (.hivemind/.experience.md) ─────────────────────────


def get_experience_path(mgr) -> Path:
    """Get the path to the experience ledger file."""
    forge_dir = Path(mgr.project_dir) / ".hivemind"
    forge_dir.mkdir(parents=True, exist_ok=True)
    return forge_dir / ".experience.md"


def read_experience(mgr) -> str:
    """Read the experience ledger. Returns empty string if not found."""
    exp_path = get_experience_path(mgr)
    if exp_path.exists():
        try:
            content = exp_path.read_text(encoding="utf-8").strip()
            if len(content) > 3000:
                lines = content.split("\n")
                header = "\n".join(lines[:5])
                lesson_starts = [i for i, l in enumerate(lines) if l.startswith("### Lesson")]
                if lesson_starts:
                    keep_from = lesson_starts[-5] if len(lesson_starts) >= 5 else lesson_starts[0]
                    recent = "\n".join(lines[keep_from:])
                    content = f"{header}\n\n... (older lessons trimmed)\n\n{recent}"
            return content
        except Exception as _exc:
            logger.debug("[Orchestrator] non-fatal exception suppressed: %s", _exc)
    return ""


def write_experience(mgr, content: str):
    """Write the experience ledger."""
    try:
        exp_path = get_experience_path(mgr)
        exp_path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.warning(f"[{mgr.project_id}] Failed to write experience ledger: {e}")


def append_experience(mgr, lesson: str):
    """Append a new lesson to the experience ledger."""
    existing = read_experience(mgr)
    if not existing:
        existing = (
            "# Experience Ledger\n\n"
            "This file stores lessons learned from past task executions.\n"
            "The orchestrator uses these to avoid repeating mistakes.\n"
        )
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lesson_count = existing.count("### Lesson ")
    new_entry = f"\n### Lesson {lesson_count + 1} ({timestamp})\n{lesson}\n"
    write_experience(mgr, existing + new_entry)


async def generate_reflection(mgr, task: str, outcome: str, start_time: float) -> str | None:
    """Generate a reflection on the task execution using the LLM.

    This is the core of the Reflexion pattern: after a task completes,
    the orchestrator analyzes what happened and extracts reusable lessons.
    """
    duration = time.monotonic() - start_time
    rounds_summary = (
        "\n".join(f"  {r}" for r in mgr._completed_rounds[-15:])
        if mgr._completed_rounds
        else "  (no rounds)"
    )
    agents_used = sorted(mgr._agents_used)

    todo = read_todo(mgr)

    reflection_prompt = (
        f"You are reflecting on a completed task to extract lessons for future tasks.\n\n"
        f"TASK: {task[:500]}\n"
        f"OUTCOME: {outcome}\n"
        f"DURATION: {int(duration)}s ({len(mgr._completed_rounds)} rounds)\n"
        f"COST: ${mgr.total_cost_usd:.4f}\n"
        f"AGENTS USED: {', '.join(agents_used)}\n\n"
        f"ROUND HISTORY:\n{rounds_summary}\n\n"
    )
    if todo:
        reflection_prompt += f"TASK LEDGER:\n{todo[:1000]}\n\n"

    reflection_prompt += (
        "Based on this execution, extract 2-4 CONCRETE lessons. Focus on:\n"
        "1. What strategy worked well? (e.g., 'sequential developer->tester pipeline was effective')\n"
        "2. What went wrong? (e.g., 'developer kept failing on X because Y')\n"
        "3. What should be done differently next time? (e.g., 'always run lint before tests')\n"
        "4. Any project-specific knowledge? (e.g., 'this project uses pnpm not npm')\n\n"
        "Format each lesson as a single line starting with '- '.\n"
        "Be specific and actionable. Do NOT be vague.\n"
        "Output ONLY the lessons, nothing else."
    )

    try:
        response = await mgr._query_agent("orchestrator", reflection_prompt)
        if response and not response.is_error and response.text.strip():
            return response.text.strip()
    except Exception as e:
        logger.warning(f"[{mgr.project_id}] Reflection generation failed: {e}")
    return None


async def store_lessons(mgr, task: str, reflection: str, outcome: str):
    """Store lessons from a reflection in both the file system and the database."""
    append_experience(mgr, reflection)

    lessons = []
    for line in reflection.split("\n"):
        line = line.strip()
        if line.startswith("- ") and len(line) > 10:
            lessons.append(line[2:].strip())

    if not lessons:
        lessons = [reflection[:500]]

    task_lower = task.lower()
    tags = []
    tag_keywords = [
        "react",
        "python",
        "typescript",
        "javascript",
        "node",
        "fastapi",
        "django",
        "flask",
        "next",
        "vue",
        "angular",
        "docker",
        "postgres",
        "sqlite",
        "redis",
        "api",
        "auth",
        "test",
        "deploy",
        "css",
        "html",
        "database",
        "websocket",
        "graphql",
        "rest",
        "frontend",
        "backend",
    ]
    for kw in tag_keywords:
        if kw in task_lower:
            tags.append(kw)

    for lesson_text in lessons:
        lesson_lower = lesson_text.lower()
        if any(w in lesson_lower for w in ["error", "fail", "crash", "bug", "wrong"]):
            lesson_type = "error_pattern"
        elif any(w in lesson_lower for w in ["strategy", "pipeline", "approach", "pattern"]):
            lesson_type = "strategy"
        elif any(w in lesson_lower for w in ["tool", "command", "npm", "pip", "git"]):
            lesson_type = "tool_usage"
        else:
            lesson_type = "general"

        try:
            await mgr.session_mgr.add_lesson(
                project_id=mgr.project_id,
                user_id=mgr.user_id,
                task_description=task[:500],
                lesson=lesson_text[:500],
                lesson_type=lesson_type,
                tags=",".join(tags[:10]),
                outcome=outcome,
                rounds_used=len(mgr._completed_rounds),
                cost_usd=mgr.total_cost_usd,
            )
        except Exception as e:
            logger.warning(f"[{mgr.project_id}] Failed to store lesson in DB: {e}")

    logger.info(
        f"[{mgr.project_id}] Stored {len(lessons)} lessons (outcome={outcome}, tags={tags})"
    )


async def inject_experience_context(mgr, task: str) -> str:
    """Build an experience context block to inject into the orchestrator's initial prompt."""
    sections = []

    experience = read_experience(mgr)
    if experience:
        sections.append(
            "📚 PROJECT EXPERIENCE (lessons from previous tasks in this project):\n"
            f"{experience[:1500]}"
        )

    try:
        task_words = re.sub(r"[^a-zA-Z0-9 ]", " ", task.lower()).split()
        stop_words = {
            "the",
            "and",
            "for",
            "that",
            "this",
            "with",
            "from",
            "have",
            "will",
            "should",
            "would",
            "could",
        }
        keywords = [w for w in task_words if len(w) > 3 and w not in stop_words][:8]

        if keywords:
            db_lessons = await mgr.session_mgr.search_lessons(
                user_id=mgr.user_id,
                keywords=keywords,
                limit=5,
            )
            if db_lessons:
                lesson_lines = []
                for l in db_lessons:
                    project_name = l.get("project_name", "unknown")
                    lesson_text = l.get("lesson", "")
                    outcome = l.get("outcome", "")
                    icon = "✅" if outcome == "success" else "⚠️" if outcome == "partial" else "❌"
                    lesson_lines.append(f"  {icon} [{project_name}] {lesson_text[:200]}")
                if lesson_lines:
                    sections.append(
                        "📚 CROSS-PROJECT LESSONS (relevant experience from other projects):\n"
                        + "\n".join(lesson_lines)
                    )
    except Exception as e:
        logger.debug(f"[{mgr.project_id}] Failed to search lessons DB: {e}")

    if not sections:
        return ""

    return (
        "\n\n═══ EXPERIENCE MEMORY ═══\n"
        "These are lessons learned from previous tasks. Use them to avoid repeating mistakes.\n\n"
        + "\n\n".join(sections)
        + "\n═══════════════════════\n"
    )
