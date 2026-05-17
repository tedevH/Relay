"""
Phase 2 — Autonomous Task Loop
================================
relay auto "task" --until "condition" --max-retries 3 --max-cost 1.00

Loop:
  execute → verify (cheap) → pass: commit+exit | fail: diagnose → guided-execute → loop

Critical constraints:
  - --until is required. No task runs without a done-condition.
  - Each retry = one diagnose + one execute. Budget tracks both.
  - execute_with_guidance receives ONLY: task, done-condition, guidance, file context.
    Not the prior error. Not the prior diff. Not the full diagnosis chain.
  - Every run lands on its own branch or commit. One-click revertable.
  - Hard stop on budget cap — leave branch for human review.
"""
from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.utils import timestamp_now, cli_available
from relay_core.git import (
    run_agent, changed_files, current_diff,
    current_branch, git_output, run_command,
)
from relay_core.cleaner import save_output
from relay_core.models import call, classify_task, infer_done_condition, session_cost, flush_costs
from relay_core.diagnose import diagnose_failure, verify_task
from relay_core.memory import ensure_relay_files, load_repo_tasks, save_repo_tasks
import relay_core.tui as tui


# ── Run record (persisted to .relay/auto-runs.json) ───────────────────────

@dataclass
class AutoRun:
    id: str
    task: str
    done_condition: str
    agent: str
    tier: str
    branch: str
    status: str          # running | success | failed | escalated | capped
    retries_used: int
    max_retries: int
    cost_usd: float
    max_cost: float
    files_changed: list[str]
    diagnoses: list[dict]
    started_at: str
    finished_at: str = ""
    commit_hash: str = ""
    escalate_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _load_runs(relay_dir: Path) -> list[dict]:
    p = relay_dir / "auto-runs.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save_run(relay_dir: Path, run: AutoRun) -> None:
    runs = _load_runs(relay_dir)
    # Update existing or append
    for i, r in enumerate(runs):
        if r.get("id") == run.id:
            runs[i] = run.to_dict()
            break
    else:
        runs.append(run.to_dict())
    (relay_dir / "auto-runs.json").write_text(json.dumps(runs[-50:], indent=2))


# ── Branch management ──────────────────────────────────────────────────────

def _create_branch(repo: RepoState, task: str) -> str:
    """Create a dedicated branch for this autonomous run."""
    slug = task.lower()[:40]
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
    ts = timestamp_now()[:10]
    branch = f"relay/auto/{ts}/{slug}"
    run_command(["git", "checkout", "-b", branch], cwd=repo.repo_root)
    return branch


def _commit_branch(repo: RepoState, task: str, branch: str) -> str:
    """Commit all changes on the auto branch."""
    run_command(["git", "add", "."], cwd=repo.repo_root)
    msg = f"relay auto: {task[:72]}"
    result = run_command(["git", "commit", "-m", msg], cwd=repo.repo_root)
    if result.returncode != 0:
        return ""
    hash_result = run_command(["git", "rev-parse", "--short", "HEAD"], cwd=repo.repo_root)
    return hash_result.stdout.strip()


def _return_to_original(repo: RepoState, original_branch: str) -> None:
    run_command(["git", "checkout", original_branch], cwd=repo.repo_root)


# ── Guided prompt builder ──────────────────────────────────────────────────

def _build_guided_prompt(task: str, done_condition: str, guidance: str) -> str:
    """Build the execute-with-guidance prompt.

    Per spec: receives ONLY task + done-condition + guidance.
    Not the prior error, not the diff, not the diagnosis chain.
    """
    return (
        f"Task: {task}\n\n"
        f"Done when: {done_condition}\n\n"
        f"Guidance from previous attempt: {guidance}"
    )


# ── Tier → agent mapping ───────────────────────────────────────────────────

def _tier_to_agent(tier: str) -> str:
    return {"trivial": "codex", "feature": "claude", "architectural": "claude"}.get(tier, "claude")


# ── Done-condition check ───────────────────────────────────────────────────

def _is_done(until: str, verify: dict) -> bool:
    tests = verify.get("tests", {})
    if tests.get("passed") is True:
        return True
    if verify.get("done_condition_met") is True:
        return True
    return False


# ── Display helpers ────────────────────────────────────────────────────────

def _show_auto_header(task: str, until: str, tier: str, agent: str,
                       max_retries: int, max_cost: float, branch: str) -> None:
    from rich.panel import Panel
    from rich.text import Text
    from relay_core.tui import console, AGENT_COLORS

    color = AGENT_COLORS.get(agent, "white")
    content = Text()
    content.append("Task         ", style="dim white")
    content.append(task + "\n", style="bold white")
    content.append("Done when    ", style="dim white")
    content.append(until + "\n", style="white")
    content.append("Agent        ", style="dim white")
    content.append(agent.capitalize() + f"  [{tier}]\n", style=color)
    content.append("Max retries  ", style="dim white")
    content.append(f"{max_retries}\n", style="white")
    content.append("Budget cap   ", style="dim white")
    content.append(f"${max_cost:.2f}\n", style="white")
    content.append("Branch       ", style="dim white")
    content.append(branch, style="dim cyan")
    console.print(Panel(content, title="[bold white]⚡ Relay Auto[/bold white]",
                         border_style="cyan", padding=(0, 1)))


def _show_retry_header(attempt: int, max_retries: int, guidance: str) -> None:
    tui.console.print(
        f"\n[bold yellow]Retry {attempt}/{max_retries}[/bold yellow]  "
        f"[dim]Guidance: {guidance[:80]}[/dim]\n"
    )


def _show_auto_result(run: AutoRun) -> None:
    from rich.panel import Panel
    from rich.text import Text
    from relay_core.tui import console

    success = run.status == "success"
    color = "green" if success else "red"
    icon = "✓" if success else "✗"

    content = Text()
    content.append(f"Status       ", style="dim white")
    content.append(f"{icon} {run.status}\n", style=f"bold {color}")
    content.append("Retries      ", style="dim white")
    content.append(f"{run.retries_used}/{run.max_retries}\n", style="white")
    content.append("Cost         ", style="dim white")
    content.append(f"${run.cost_usd:.4f}\n", style="white")
    content.append("Branch       ", style="dim white")
    content.append(run.branch + "\n", style="dim cyan")
    if run.commit_hash:
        content.append("Commit       ", style="dim white")
        content.append(run.commit_hash, style="dim")
    if run.files_changed:
        content.append("\nChanged      ", style="dim white")
        content.append(", ".join(run.files_changed[:5]), style="dim")

    if run.status == "escalated":
        content.append(f"\n\nEscalated    ", style="dim white")
        content.append(run.escalate_reason, style="bold yellow")

    console.print(Panel(content, title=f"[bold white]Auto Run {'Complete' if success else 'Halted'}[/bold white]",
                         border_style=color, padding=(0, 1)))

    if run.status == "success":
        tui.show_info(f"To merge: git merge {run.branch}")
    else:
        tui.show_info(f"To review: git checkout {run.branch}")
        tui.show_info(f"To discard: git branch -D {run.branch}")


# ── Main auto loop ─────────────────────────────────────────────────────────

def run_auto(
    task: str,
    repo: RepoState,
    until: str | None = None,
    max_retries: int = 3,
    max_cost: float = 1.00,
    forced_agent: str | None = None,
) -> int:
    """Phase 2 — autonomous task loop."""
    if not cli_available("git"):
        raise RelayError("relay auto requires git.")
    if not repo.in_git_repo:
        raise RelayError("relay auto requires running inside a git repository.")

    ensure_relay_files(repo)
    relay_dir = repo.relay_dir
    assert relay_dir is not None

    # Classify task tier (Haiku)
    tui.show_info("Classifying task...")
    classification = classify_task(task, relay_dir=relay_dir)
    tier = classification.get("tier", "feature")

    # Infer done-condition (Haiku) if not provided
    if not until:
        tui.show_info("Inferring done-condition...")
        until = infer_done_condition(task, relay_dir=relay_dir)

    # Determine agent
    agent = forced_agent or _tier_to_agent(tier)

    # Save original branch, create auto branch
    original_branch = current_branch(repo)
    branch = _create_branch(repo, task)

    import uuid
    run_id = str(uuid.uuid4())[:8]

    run = AutoRun(
        id=run_id,
        task=task,
        done_condition=until,
        agent=agent,
        tier=tier,
        branch=branch,
        status="running",
        retries_used=0,
        max_retries=max_retries,
        cost_usd=0.0,
        max_cost=max_cost,
        files_changed=[],
        diagnoses=[],
        started_at=timestamp_now(),
    )
    _save_run(relay_dir, run)

    _show_auto_header(task, until, tier, agent, max_retries, max_cost, branch)
    prior_diagnosis: dict | None = None

    try:
        for attempt in range(max_retries + 1):
            # Budget check
            if run.cost_usd >= max_cost:
                tui.show_error(f"Budget cap ${max_cost:.2f} reached after {attempt} attempts.")
                run.status = "capped"
                run.finished_at = timestamp_now()
                _save_run(relay_dir, run)
                _show_auto_result(run)
                return 1

            # Build prompt
            if prior_diagnosis and prior_diagnosis.get("guidance"):
                _show_retry_header(attempt, max_retries, prior_diagnosis["guidance"])
                prompt = _build_guided_prompt(task, until, prior_diagnosis["guidance"])
            else:
                tui.console.print(f"\n[bold white]Attempt {attempt + 1}/{max_retries + 1}[/bold white]\n")
                prompt = task

            # Execute
            from relay_core.commands import _write_context
            _write_context(repo, prompt)

            exit_code, output, resumed = run_agent(
                agent, prompt,
                repo.repo_root or repo.cwd,
                relay_dir=relay_dir,
            )
            clean = save_output(relay_dir, agent, output)
            run.cost_usd += session_cost()

            if clean.strip():
                tui.show_review_output(agent, clean, exit_code)

            files = changed_files(repo)
            run.files_changed = files

            # Verify (cheap — no LLM)
            tui.show_info("Verifying...")
            verify = verify_task(repo.repo_root or repo.cwd, until, files)

            if _is_done(until, verify):
                # Success — commit and exit
                commit_hash = _commit_branch(repo, task, branch)
                run.status = "success"
                run.commit_hash = commit_hash
                run.finished_at = timestamp_now()
                flush_costs(relay_dir)
                _save_run(relay_dir, run)
                _show_auto_result(run)
                return 0

            if attempt >= max_retries:
                break

            # Diagnose failure
            tui.show_info("Diagnosing failure...")
            diff = current_diff(repo)
            error_output = verify["tests"]["output"] or clean[-2000:]

            diagnosis = diagnose_failure(
                task=task,
                done_condition=until,
                diff=diff[:4000],
                error=error_output,
                prior_diagnosis=prior_diagnosis,
                relay_dir=relay_dir,
            )
            run.diagnoses.append(diagnosis)
            run.cost_usd += session_cost()
            _save_run(relay_dir, run)

            if not diagnosis["should_retry"]:
                run.status = "escalated"
                run.escalate_reason = diagnosis.get("escalate_reason", "")
                run.finished_at = timestamp_now()
                flush_costs(relay_dir)
                _save_run(relay_dir, run)
                _show_auto_result(run)
                return 1

            prior_diagnosis = diagnosis
            run.retries_used += 1

        # Max retries hit
        run.status = "failed"
        run.finished_at = timestamp_now()
        flush_costs(relay_dir)
        _save_run(relay_dir, run)
        _show_auto_result(run)
        return 1

    except KeyboardInterrupt:
        run.status = "failed"
        run.finished_at = timestamp_now()
        _save_run(relay_dir, run)
        tui.show_info(f"\nInterrupted. Branch preserved: {branch}")
        return 1
    finally:
        _return_to_original(repo, original_branch)
