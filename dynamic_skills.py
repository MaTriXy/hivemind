"""
Dynamic Skill Discovery — Allow PM Agent to discover skills dynamically.

Instead of relying solely on the static SKILL_AGENT_MAP in skills_registry.py,
this module enables the PM Agent to search for relevant skills by scanning
the skills directory based on task keywords and content matching.

Adding a new skill is as simple as dropping a SKILL.md file into the
.claude/skills/ directory — no code changes needed.

Suggested in code review: "Allow PM Agent to 'search' for skills dynamically
by task, instead of relying on a static mapping."
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def discover_skills_for_task(
    task: str,
    project_dir: str | None = None,
    max_results: int = 5,
) -> list[dict[str, str]]:
    """Dynamically discover skills relevant to a task.

    Scans all available SKILL.md files and ranks them by relevance
    to the given task. This allows new skills to be picked up automatically
    without modifying SKILL_AGENT_MAP.

    Args:
        task: The task description
        project_dir: Optional project directory to scan for project-specific skills
        max_results: Maximum number of skills to return

    Returns:
        List of dicts with 'name', 'description', 'score', 'path'
    """
    from skills_registry import _skills_cache, scan_skills

    # Ensure skills are loaded
    extra_dirs = [project_dir] if project_dir else None
    if not _skills_cache:
        scan_skills(extra_dirs=extra_dirs)

    task_lower = task.lower()
    task_words = set(re.sub(r"[^a-z0-9 ]", " ", task_lower).split())
    # Remove common stop words
    stop_words = {"the", "a", "an", "is", "are", "to", "for", "and", "or", "in", "on", "with", "it"}
    task_words -= stop_words

    results: list[dict[str, Any]] = []

    for skill_name, content in _skills_cache.items():
        score = _score_skill(skill_name, content, task_lower, task_words)
        if score > 0:
            # Extract description from first few lines
            description = _extract_description(content)
            results.append(
                {
                    "name": skill_name,
                    "description": description,
                    "score": score,
                }
            )

    # Sort by score descending
    results.sort(key=lambda x: -x["score"])
    return results[:max_results]


def discover_unmapped_skills(project_dir: str | None = None) -> list[str]:
    """Find skills that exist in the skills directory but aren't in SKILL_AGENT_MAP.

    These are "orphan" skills that were added by dropping a SKILL.md file
    but haven't been mapped to any agent role yet.

    Args:
        project_dir: Optional project directory to scan

    Returns:
        List of unmapped skill names
    """
    from skills_registry import SKILL_AGENT_MAP, _skills_cache, scan_skills

    extra_dirs = [project_dir] if project_dir else None
    if not _skills_cache:
        scan_skills(extra_dirs=extra_dirs)

    mapped_names = set(SKILL_AGENT_MAP.keys())
    all_names = set(_skills_cache.keys())
    return sorted(all_names - mapped_names)


def auto_assign_skill_to_agents(
    skill_name: str,
    skill_content: str,
) -> list[str]:
    """Suggest which agent roles should receive a skill based on its content.

    Uses keyword analysis to determine the best agent roles for an unmapped skill.

    Args:
        skill_name: The skill name
        skill_content: The SKILL.md content

    Returns:
        List of suggested agent role names
    """
    content_lower = (skill_name + " " + skill_content).lower()

    # Agent role detection keywords
    role_keywords: dict[str, list[str]] = {
        "frontend_developer": [
            "react",
            "vue",
            "angular",
            "css",
            "html",
            "tailwind",
            "component",
            "ui",
            "ux",
            "frontend",
            "front-end",
            "browser",
            "dom",
            "jsx",
            "tsx",
        ],
        "backend_developer": [
            "api",
            "server",
            "endpoint",
            "rest",
            "graphql",
            "fastapi",
            "express",
            "django",
            "flask",
            "backend",
            "back-end",
            "middleware",
            "route",
        ],
        "database_expert": [
            "database",
            "sql",
            "postgres",
            "mysql",
            "mongodb",
            "orm",
            "migration",
            "schema",
            "query",
            "index",
            "table",
        ],
        "test_engineer": [
            "test",
            "testing",
            "pytest",
            "jest",
            "e2e",
            "unit test",
            "integration",
            "coverage",
            "assertion",
            "mock",
            "fixture",
        ],
        "security_auditor": [
            "security",
            "auth",
            "authentication",
            "authorization",
            "jwt",
            "oauth",
            "vulnerability",
            "xss",
            "csrf",
            "injection",
            "encryption",
        ],
        "devops": [
            "docker",
            "kubernetes",
            "ci/cd",
            "deploy",
            "nginx",
            "terraform",
            "ansible",
            "monitoring",
            "logging",
            "infrastructure",
        ],
        "reviewer": [
            "review",
            "code quality",
            "lint",
            "best practice",
            "refactor",
            "clean code",
            "pattern",
            "architecture",
        ],
        "researcher": [
            "research",
            "documentation",
            "api docs",
            "tutorial",
            "guide",
            "analysis",
            "report",
            "scraping",
            "web search",
        ],
    }

    scores: dict[str, int] = {}
    for role, keywords in role_keywords.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[role] = score

    if not scores:
        # Default to developer if nothing matches
        return ["backend_developer"]

    # Return roles with score >= 2, or top 2 roles
    threshold = 2
    strong_matches = [r for r, s in scores.items() if s >= threshold]
    if strong_matches:
        return sorted(strong_matches, key=lambda r: -scores[r])[:3]

    # Return top 2
    ranked = sorted(scores.keys(), key=lambda r: -scores[r])
    return ranked[:2]


def build_dynamic_skill_context(
    task: str,
    agent_role: str,
    project_dir: str | None = None,
    max_skills: int = 2,
) -> str:
    """Build skill context using both static mapping and dynamic discovery.

    This combines the existing SKILL_AGENT_MAP with dynamic discovery
    to ensure new skills are picked up without code changes.

    Args:
        task: The task description
        agent_role: The agent role requesting skills
        project_dir: Optional project directory
        max_skills: Maximum skills to include

    Returns:
        Formatted skill context string
    """
    from skills_registry import build_skill_prompt, select_skills_for_task

    # Get statically mapped skills
    static_skills = select_skills_for_task(agent_role, task, max_skills=max_skills)

    # Discover additional skills dynamically
    discovered = discover_skills_for_task(task, project_dir=project_dir, max_results=3)
    dynamic_names = [d["name"] for d in discovered if d["name"] not in static_skills]

    # Combine, respecting max_skills limit
    combined = static_skills + dynamic_names[: max(0, max_skills - len(static_skills))]

    if combined:
        return build_skill_prompt(combined)
    return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _score_skill(
    skill_name: str,
    content: str,
    task_lower: str,
    task_words: set[str],
) -> int:
    """Score a skill's relevance to a task."""
    score = 0

    # 1. Skill name word overlap (high signal)
    name_words = set(skill_name.replace("-", " ").replace("_", " ").split())
    score += len(name_words & task_words) * 4

    # 2. Skill name appears verbatim in task
    if skill_name.replace("-", " ") in task_lower:
        score += 6

    # 3. Description match
    description = _extract_description(content)
    if description:
        desc_words = set(re.sub(r"[^a-z0-9 ]", " ", description.lower()).split())
        score += len(desc_words & task_words) * 2

    # 4. Content keyword match (lower signal, sample first 500 chars)
    content_sample = content[:500].lower()
    content_words = set(re.sub(r"[^a-z0-9 ]", " ", content_sample).split())
    score += min(len(content_words & task_words), 3)  # Cap at 3

    return score


def _extract_description(content: str) -> str:
    """Extract a description from SKILL.md content."""
    for line in content.splitlines()[:15]:
        stripped = line.strip()
        if stripped.lower().startswith("description:"):
            return stripped[12:].strip()
        if stripped.startswith("#") and len(stripped) > 2:
            return stripped.lstrip("# ").strip()
    return ""
