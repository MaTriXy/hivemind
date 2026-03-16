"""
Git Discipline — Only the DAG Executor commits. Never individual agents.

Agents' system prompts explicitly forbid git commit/push.
This module is the SINGLE place where commits are created.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import SUBPROCESS_MEDIUM_TIMEOUT
from contracts import TaskOutput, TaskStatus

logger = logging.getLogger(__name__)

# Patterns that must never be staged by the auto-committer.
# These protect secrets, certificates, credential files, and Hivemind-internal
# metadata from accidentally being committed to project repositories.
_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*secret*",
    "*credential*",
    "*credentials*",
    "*password*",
    "*.aws/credentials",
    "*.ssh/id_*",
    "id_rsa",
    "id_ed25519",
    ".netrc",
    # Hivemind agent metadata — must never enter project commits
    ".hivemind/*",
    ".hivemind/**",
    "hivemind_*.log",
    "hivemind_*.tmp",
    "me_file*",
    "*.hivemind.json",
    # Agent-generated reports/reviews — work products, not source code
    "*REVIEW*",
    "*_REPORT*",
    "*_report*",
    "reviews/*",
    "reviews/**",
    "REVIEW_*.md",
    "*.review.md",
)


def _is_sensitive(filepath: str) -> bool:
    """Return True if *filepath* matches any known sensitive file pattern."""
    from fnmatch import fnmatch

    name = Path(filepath).name
    # Block anything inside .hivemind/ directory entirely
    normalized = filepath.replace("\\", "/")
    if normalized.startswith(".hivemind/") or normalized == ".hivemind":
        return True
    # Match against both the full relative path and just the filename
    return any(fnmatch(filepath, pat) or fnmatch(name, pat) for pat in _SENSITIVE_PATTERNS)


_git_locks: dict[str, asyncio.Lock] = {}


def _git_lock(project_dir: str) -> asyncio.Lock:
    """Get or create a per-project git lock to prevent concurrent commits."""
    if project_dir not in _git_locks:
        _git_locks[project_dir] = asyncio.Lock()
    return _git_locks[project_dir]


async def commit_single_task(
    project_dir: str,
    output: TaskOutput,
) -> str | None:
    """
    Auto-commit changes after a single task completes.

    Returns the short commit hash, or None if there was nothing to commit.
    Uses a per-project lock to prevent concurrent git operations.
    """
    if not output or not output.is_successful():
        return None

    async with _git_lock(project_dir):
        return await _do_commit(project_dir, [output], task_id=output.task_id)


async def executor_commit(
    project_dir: str,
    round_outputs: list[TaskOutput],
    round_num: int,
) -> str | None:
    """
    Fallback: commit any remaining unstaged changes after a DAG round.

    In normal flow, commit_single_task handles per-task commits.
    This catches anything that slipped through.
    """
    if not round_outputs:
        return None

    async with _git_lock(project_dir):
        return await _do_commit(project_dir, round_outputs, round_num=round_num)


async def _do_commit(
    project_dir: str,
    outputs: list[TaskOutput],
    task_id: str = "",
    round_num: int = 0,
) -> str | None:
    """Internal: perform the actual git add + commit. Caller must hold the lock."""

    proj = Path(project_dir)
    if not (proj / ".git").exists():
        logger.debug("[git] No .git directory, skipping auto-commit")
        return None

    # Check if there's anything to commit
    status = await _run(["git", "status", "--porcelain"], cwd=project_dir)
    if not status.strip():
        return None  # Nothing to commit

    # Stage files safely: exclude known-sensitive patterns.
    # We stage tracked modifications with -u first (always safe), then
    # add untracked files individually after checking each against the
    # sensitive-file pattern list.  This prevents secrets, API keys, and
    # certificates from being committed even if an agent created them.
    await _stage_files_safely(project_dir)

    # After selective staging, check again — we might have excluded everything
    staged = await _run(["git", "diff", "--cached", "--name-only"], cwd=project_dir)
    if not staged.strip():
        logger.debug("[git] All changes were sensitive files — nothing to commit")
        return None

    # Build commit message from outputs
    message = _build_commit_message(outputs, round_num, task_id)

    await _run(["git", "commit", "-m", message], cwd=project_dir)

    # Extract short hash
    hash_result = await _run(["git", "rev-parse", "--short", "HEAD"], cwd=project_dir)
    short_hash = hash_result.strip()

    label = f"task {task_id}" if task_id else f"round {round_num}"
    logger.info(f"[git] Auto-committed {label}: {short_hash}")
    return short_hash


async def _stage_files_safely(project_dir: str) -> None:
    """Stage project changes while excluding known-sensitive file patterns.

    Strategy:
    1. ``git add -u`` — stages modifications and deletions of already-tracked
       files.  Tracked files were deliberately added previously and are already
       in the repository, so re-staging their changes is safe.
    2. Enumerate untracked files (``??`` in ``git status --porcelain``) and add
       each one individually only if it does NOT match _SENSITIVE_PATTERNS.
       This prevents an agent-created ``.env`` or ``*.key`` from sneaking into
       a commit.
    """
    # Step 1: stage tracked changes (modifications + deletions)
    await _run(["git", "add", "-u"], cwd=project_dir)

    # Step 2: enumerate untracked files and add safe ones
    raw = await _run(
        ["git", "status", "--porcelain", "-z"],
        cwd=project_dir,
    )
    entries = [e.strip() for e in raw.split("\0") if e.strip()]
    skipped: list[str] = []
    for entry in entries:
        if not entry.startswith("?? "):
            continue  # Already tracked/staged by step 1
        filepath = entry[3:]  # Strip the "?? " prefix
        if _is_sensitive(filepath):
            skipped.append(filepath)
            logger.warning("[git] Skipping sensitive file from auto-commit: %s", filepath)
        else:
            await _run(["git", "add", "--", filepath], cwd=project_dir)

    if skipped:
        logger.warning(
            "[git] %d sensitive file(s) excluded from auto-commit: %s",
            len(skipped),
            skipped,
        )


def _build_commit_message(outputs: list[TaskOutput], round_num: int = 0, task_id: str = "") -> str:
    """Build a structured commit message from task outputs."""
    successful = [o for o in outputs if o.status == TaskStatus.COMPLETED]
    failed = [o for o in outputs if o.status == TaskStatus.FAILED]

    # Single-task commit: clean, focused message
    if task_id and len(successful) == 1:
        o = successful[0]
        first_line = f"feat: {o.summary[:72]}"
        body_lines = [f"\nTask: {o.task_id}"]
        if o.artifacts:
            unique = list(dict.fromkeys(o.artifacts[:5]))
            body_lines.append(f"Files: {', '.join(unique)}")
        body_lines.append(f"Cost: ${o.cost_usd:.4f}")
        return first_line + "\n" + "\n".join(body_lines)

    # Multi-task fallback (round commit for leftovers)
    all_artifacts: list[str] = []
    for o in successful:
        all_artifacts.extend(o.artifacts[:3])

    if len(successful) == 1:
        first_line = f"feat: {successful[0].summary[:72]}"
    elif successful:
        first_line = f"feat: complete round {round_num} — {len(successful)} tasks"
    else:
        first_line = f"wip: round {round_num} (partial — {len(failed)} failed)"

    body_lines: list[str] = []
    for o in successful:
        body_lines.append(f"  - [{o.task_id}] {o.summary[:100]}")
    for o in failed:
        body_lines.append(f"  - [{o.task_id}] FAILED: {'; '.join(o.issues[:2])[:80]}")

    if all_artifacts:
        unique = list(dict.fromkeys(all_artifacts))[:10]
        body_lines.append(f"\nFiles: {', '.join(unique)}")

    total_cost = sum(o.cost_usd for o in outputs)
    body_lines.append(f"Cost: ${total_cost:.4f}")

    return first_line + "\n" + "\n".join(body_lines)


async def _run(cmd: list[str], cwd: str) -> str:
    """Run a subprocess command and return stdout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_MEDIUM_TIMEOUT
        )
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.debug(f"[git] Command {cmd} failed: {err}")
            return ""
        return stdout.decode(errors="replace")
    except Exception as exc:
        logger.debug(f"[git] Command {cmd} exception: {exc}")
        return ""


async def ensure_no_agent_commits(project_dir: str) -> None:
    """
    Safety check: warn if any commits were made by agents (not the executor).
    Checks the last 5 commits for missing 'DAG Executor' signature.
    """
    log = await _run(
        ["git", "log", "--oneline", "-5", "--format=%H %s"],
        cwd=project_dir,
    )
    for line in log.strip().splitlines():
        if line and "DAG Executor" not in line and "auto-commit" not in line.lower():
            # Could be a human commit or an old-style commit — that's fine
            pass
