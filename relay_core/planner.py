"""
Phase 4 — Plan-Once, Execute-Cheaply
======================================
relay plan "add user authentication with email and Google OAuth"

One Sonnet call decomposes the goal into subtasks.
Each subtask executes as a relay auto invocation with its own
done-condition and tier (which drives the execute model).

Context between steps: cleaned diff of previous step only.
Not the full plan. Not all prior output.

Budget and retry caps apply at both subtask and plan level.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

from relay_core.types import RepoState, RelayError
from relay_core.utils import timestamp_now, cli_available
from relay_core.models import call
from relay_core.memory import ensure_relay_files
import relay_core.tui as tui


# ── Plan decomposition (Sonnet) ────────────────────────────────────────────

PLAN_SYSTEM = """You are a development task planner. You decompose a high-level goal into a minimal ordered list of concrete, independently executable subtasks.

Rules:
- Each subtask must be completable by a single AI coding session
- Each subtask must have a verifiable done-condition (test passes, file exists, endpoint responds)
- Order matters — later tasks may depend on earlier ones
- 3-7 subtasks maximum. If you need more, the goal is too large.
- No subtask should require human judgment to verify completion

Output valid JSON only:
{
  "goal": "the original goal restated clearly",
  "overall_done_condition": "how the entire plan is verified complete",
  "subtasks": [
    {
      "id": 1,
      "description": "specific, actionable task description",
      "done_condition": "verifiable completion check",
      "tier": "trivial|feature|architectural",
      "depends_on": []
    }
  ]
}

Tier definitions:
- trivial: single-line change, rename, config update
- feature: new function/endpoint/component, bug fix with logic change
- architectural: cross-cutting refactor, schema change, new module"""


def decompose(goal: str, relay_dir: Path | None = None) -> dict:
    """Decompose a goal into subtasks via Sonnet. Returns plan dict."""
    cache_key = _plan_cache_key(goal, relay_dir)
    cache = _load_plan_cache(relay_dir)
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("subtasks"):
        return cached

    raw = call(
        "plan_decompose",
        f"Decompose this development goal into subtasks:\n\n{goal}",
        system=PLAN_SYSTEM,
        relay_dir=relay_dir,
        use_cache=False,
    )
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            plan = json.loads(m.group())
            cache[cache_key] = plan
            _save_plan_cache(relay_dir, cache)
            return plan
        except json.JSONDecodeError:
            pass
    raise RelayError(f"Planner produced unparseable output:\n{raw[:500]}")


def _plan_cache_path(relay_dir: Path | None) -> Path | None:
    return relay_dir / "plan-cache.json" if relay_dir else None


def _load_plan_cache(relay_dir: Path | None) -> dict[str, dict]:
    path = _plan_cache_path(relay_dir)
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_plan_cache(relay_dir: Path | None, cache: dict[str, dict]) -> None:
    path = _plan_cache_path(relay_dir)
    if not path:
        return
    path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")


def _plan_cache_key(goal: str, relay_dir: Path | None) -> str:
    if not relay_dir:
        fingerprint = "global"
    else:
        repo_root = relay_dir.parent
        markers = [
            "package.json", "pyproject.toml", "go.mod", "Cargo.toml",
            "requirements.txt", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
        ]
        parts: list[str] = []
        for marker in markers:
            path = repo_root / marker
            if path.exists():
                stat = path.stat()
                parts.append(f"{marker}:{stat.st_mtime_ns}:{stat.st_size}")
        fingerprint = "|".join(parts) if parts else repo_root.name
    return f"{fingerprint}:{goal.strip().lower()}"


# ── Plan display ───────────────────────────────────────────────────────────

def show_plan(plan: dict) -> None:
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    from relay_core.tui import console

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold white", border_style="dim")
    table.add_column("#", width=3, justify="right", style="dim")
    table.add_column("Task")
    table.add_column("Done when", style="dim")
    table.add_column("Tier", width=14, justify="center")

    tier_colors = {"trivial": "dim", "feature": "bold cyan", "architectural": "bold magenta"}
    for st in plan.get("subtasks", []):
        tier = st.get("tier", "feature")
        table.add_row(
            str(st["id"]),
            st["description"][:60],
            st["done_condition"][:50],
            f"[{tier_colors.get(tier, 'white')}]{tier}[/{tier_colors.get(tier, 'white')}]",
        )

    console.print()
    console.print(Panel(
        table,
        title=f"[bold white]Plan: {plan.get('goal', '')[:60]}[/bold white]",
        border_style="cyan", padding=(0, 1),
    ))
    console.print(f"[dim]Overall: {plan.get('overall_done_condition', '')}[/dim]\n")


# ── Plan executor ──────────────────────────────────────────────────────────

def run_plan(
    goal: str,
    repo: RepoState,
    max_retries_per_task: int = 2,
    max_cost_per_task: float = 0.50,
    max_total_cost: float = 3.00,
    dry_run: bool = False,
) -> int:
    """Phase 4 — decompose goal and execute each subtask as relay auto."""
    if not cli_available("git"):
        raise RelayError("relay plan requires git.")
    if not repo.in_git_repo:
        raise RelayError("relay plan requires running inside a git repository.")

    ensure_relay_files(repo)

    tui.show_info("Planning...")
    plan = decompose(goal, relay_dir=repo.relay_dir)
    show_plan(plan)

    if dry_run:
        tui.show_info("Dry run — no tasks executed.")
        return 0

    if not tui.ask_confirm("Execute this plan?"):
        tui.show_info("Plan cancelled.")
        return 0

    from relay_core.auto import run_auto
    from relay_core.git import current_diff

    subtasks = plan.get("subtasks", [])
    total_cost = 0.0
    completed = 0
    prev_diff_context = ""

    for i, subtask in enumerate(subtasks):
        tui.console.print(
            f"\n[bold white]Step {i+1}/{len(subtasks)}[/bold white]  "
            f"[dim]{subtask['description'][:70]}[/dim]\n"
        )

        # Budget check at plan level
        if total_cost >= max_total_cost:
            tui.show_error(f"Plan budget cap ${max_total_cost:.2f} reached. Stopping at step {i+1}.")
            break

        # Inject prev step diff as context (not full plan — per spec)
        task_with_context = subtask["description"]
        if prev_diff_context:
            task_with_context = (
                f"{subtask['description']}\n\n"
                f"Context from previous step:\n{prev_diff_context[:1500]}"
            )

        result = run_auto(
            task=task_with_context,
            repo=repo,
            until=subtask.get("done_condition"),
            max_retries=max_retries_per_task,
            max_cost=max_cost_per_task,
        )

        # Capture diff for next step's context
        diff = current_diff(repo)
        if diff.strip():
            # Summarise diff via Haiku
            prev_diff_context = call(
                "summarize_output",
                f"Summarise what changed in this git diff in 3 sentences:\n\n{diff[:3000]}",
                relay_dir=repo.relay_dir,
            )

        # Track cost (rough)
        total_cost += max_cost_per_task * 0.5  # rough estimate

        if result == 0:
            completed += 1
            tui.show_success(f"Step {i+1} complete.")
        else:
            tui.show_error(f"Step {i+1} failed. Stopping plan.")
            break

    # Summary
    tui.console.print()
    if completed == len(subtasks):
        tui.show_success(f"Plan complete. {completed}/{len(subtasks)} steps succeeded.")
    else:
        tui.show_info(f"Plan stopped. {completed}/{len(subtasks)} steps completed.")

    return 0 if completed == len(subtasks) else 1
