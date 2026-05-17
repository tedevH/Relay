from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.utils import timestamp_now, cli_available
from relay_core.memory import ensure_relay_files, load_config
from relay_core.planner import decompose, show_plan
from relay_core.auto import run_auto
from relay_core.git import current_diff, run_command
import relay_core.tui as tui


def run_brain(goal: str, repo: RepoState, **options: Any) -> int:
    """Run a high-level goal through plan -> auto steps -> final checkpoint."""
    _require_repo(repo, "relay brain")
    ensure_relay_files(repo)
    assert repo.relay_dir is not None

    config = load_config(repo)
    brain_id = _new_brain_id()
    run_dir = repo.relay_dir / "runs" / f"brain-{brain_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    tui.show_info("Planning brain run...")
    plan = decompose(goal, relay_dir=repo.relay_dir)
    show_plan(plan)

    state = {
        "id": brain_id,
        "goal": goal,
        "status": "running",
        "created_at": timestamp_now(),
        "updated_at": timestamp_now(),
        "current_step": 0,
        "completed_steps": [],
        "failed_step": None,
        "plan": plan,
        "run_dir": str(run_dir),
    }
    _write_json(run_dir / "plan.json", plan)
    _save_active_brain(repo, state)

    return _execute_plan(repo, state, options)


def run_brain_status(repo: RepoState) -> int:
    _require_repo(repo, "relay brain status")
    ensure_relay_files(repo)
    state = _load_brain(repo)
    active = state.get("active_brain") or {}
    if not active:
        tui.show_info("No active brain run.")
        return 0
    _print_brain_state(active)
    return 0


def run_brain_resume(repo: RepoState, **options: Any) -> int:
    _require_repo(repo, "relay brain resume")
    ensure_relay_files(repo)
    state = _load_brain(repo)
    active = state.get("active_brain") or {}
    if not active:
        tui.show_info("No active brain run to resume.")
        return 0
    if active.get("status") == "success":
        tui.show_info("Latest brain run already succeeded.")
        return 0

    completed = len(active.get("completed_steps", []))
    plan = active.get("plan") or {}
    remaining = plan.get("subtasks", [])[completed:]
    if not remaining:
        active["status"] = "success"
        _save_active_brain(repo, active)
        tui.show_success("Brain run marked complete.")
        return 0

    active["status"] = "running"
    _save_active_brain(repo, active)

    tui.show_info(f"Resuming brain run: {active.get('goal', '')}")
    return _execute_plan(repo, active, options, start_index=completed)


def run_brain_logs(repo: RepoState) -> int:
    _require_repo(repo, "relay brain logs")
    ensure_relay_files(repo)
    assert repo.runs_dir is not None
    run_files = sorted(repo.runs_dir.glob("**/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not run_files:
        tui.show_info("No brain or auto run logs found.")
        return 0
    for path in run_files[:10]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        tui.console.print(
            f"[bold]{data.get('id', path.parent.name)}[/bold]  "
            f"{data.get('status', 'unknown')}  "
            f"[dim]{data.get('task') or data.get('goal', '')[:80]}[/dim]"
        )
    return 0


def run_brain_stop(repo: RepoState) -> int:
    _require_repo(repo, "relay brain stop")
    ensure_relay_files(repo)
    state = _load_brain(repo)
    active = state.get("active_brain") or {}
    if not active:
        tui.show_info("No active brain run.")
        return 0
    active["status"] = "stopped"
    active["updated_at"] = timestamp_now()
    _save_active_brain(repo, active, active=False)
    tui.show_success("Brain run stopped.")
    return 0


def run_brain_rollback(repo: RepoState) -> int:
    _require_repo(repo, "relay brain rollback")
    ensure_relay_files(repo)
    state = _load_brain(repo)
    runs = state.get("runs", [])
    latest = runs[-1] if runs else {}
    commit = latest.get("commit_hash") or _latest_auto_commit(repo)
    if not commit:
        tui.show_info("No auto-created commit found to roll back.")
        return 0
    result = run_command(["git", "revert", "--no-edit", commit], cwd=repo.repo_root)
    if result.returncode != 0:
        raise RelayError(result.stderr.strip() or "Rollback failed.")
    tui.show_success(f"Created revert commit for {commit}.")
    return 0


def maybe_push_pr(repo: RepoState, branch: str) -> str:
    """Push a verified automation branch and create a PR when gh is available."""
    if not branch:
        return ""
    push = run_command(["git", "push", "-u", "origin", branch], cwd=repo.repo_root)
    if push.returncode != 0:
        tui.show_error(push.stderr.strip() or "Branch push failed.")
        return ""
    if not cli_available("gh"):
        tui.show_info("Branch pushed. Install GitHub CLI `gh` to auto-open PRs.")
        return branch
    title = f"Relay auto: {branch.split('/')[-1].replace('-', ' ')}"
    body = "Created by Relay automation after local verification passed."
    pr = run_command(["gh", "pr", "create", "--fill", "--title", title, "--body", body], cwd=repo.repo_root)
    if pr.returncode == 0:
        url = pr.stdout.strip()
        tui.show_success(f"Opened PR: {url}")
        return url
    tui.show_info("Branch pushed, but PR creation failed. You can open it manually.")
    return branch


def _execute_plan(
    repo: RepoState,
    state: dict[str, Any],
    options: dict[str, Any],
    start_index: int = 0,
) -> int:
    config = load_config(repo)
    plan = state.get("plan") or {}
    subtasks = plan.get("subtasks", [])
    max_steps = int(options.get("max_steps") or config.get("max_auto_steps", 6))
    max_retries = int(options.get("max_retries") or 2)
    max_cost = float(options.get("max_cost") or 1.00)
    mode = options.get("mode") or config.get("automation_mode", "edit")
    agent_policy = options.get("agent_policy") or config.get("agent_policy", "balanced")

    completed_this_run = 0
    previous_context = ""
    limit = min(len(subtasks), max_steps)
    for index in range(start_index, limit):
        subtask = subtasks[index]
        state["current_step"] = index + 1
        state["updated_at"] = timestamp_now()
        _save_active_brain(repo, state)

        description = subtask.get("description", "")
        if previous_context:
            description = f"{description}\n\nPrior step context:\n{previous_context[:1500]}"

        tui.console.print(
            f"\n[bold white]Brain step {index + 1}/{limit}[/bold white]  "
            f"[dim]{subtask.get('description', '')[:72]}[/dim]\n"
        )
        result = run_auto(
            task=description,
            repo=repo,
            until=subtask.get("done_condition"),
            max_retries=max_retries,
            max_cost=max_cost,
            mode=mode,
            agent_policy=agent_policy,
        )
        if result != 0:
            state["status"] = "failed"
            state["failed_step"] = subtask
            state["updated_at"] = timestamp_now()
            _save_active_brain(repo, state)
            tui.show_error(f"Brain stopped at step {index + 1}.")
            return 1

        completed_this_run += 1
        completed_steps = state.setdefault("completed_steps", [])
        if subtask not in completed_steps:
            completed_steps.append(subtask)
        previous_context = _summarize_diff(repo)

    total_completed = len(state.get("completed_steps", []))
    state["status"] = "success" if total_completed >= len(subtasks) else "capped"
    state["updated_at"] = timestamp_now()
    _save_active_brain(repo, state)
    tui.show_success(f"Brain run {state['status']}. {total_completed}/{len(subtasks)} steps completed.")
    return 0 if state["status"] == "success" else 1


def _require_repo(repo: RepoState, command: str) -> None:
    if not repo.in_git_repo:
        raise RelayError(f"{command} requires running inside a git repository.")


def _new_brain_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8]


def _load_brain(repo: RepoState) -> dict[str, Any]:
    if not repo.brain_path or not repo.brain_path.exists():
        return {"version": 1, "runs": []}
    try:
        data = json.loads(repo.brain_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"version": 1, "runs": []}
    except Exception:
        return {"version": 1, "runs": []}


def _save_active_brain(repo: RepoState, active_state: dict[str, Any], active: bool = True) -> None:
    if not repo.brain_path:
        return
    state = _load_brain(repo)
    runs = [run for run in state.get("runs", []) if run.get("id") != active_state.get("id")]
    runs.append({
        "id": active_state.get("id"),
        "goal": active_state.get("goal"),
        "status": active_state.get("status"),
        "updated_at": active_state.get("updated_at"),
        "current_step": active_state.get("current_step"),
    })
    state["version"] = 1
    state["active_brain"] = active_state if active else {}
    state["runs"] = runs[-20:]
    _write_json(repo.brain_path, state)


def _print_brain_state(active: dict[str, Any]) -> None:
    tui.console.print(f"[bold]Brain[/bold] {active.get('id', '')}")
    tui.console.print(f"Status: {active.get('status', 'unknown')}")
    tui.console.print(f"Goal: {active.get('goal', '')}")
    tui.console.print(f"Step: {active.get('current_step', 0)}")
    failed = active.get("failed_step")
    if failed:
        tui.console.print(f"Failed: {failed.get('description', failed)}")


def _summarize_diff(repo: RepoState) -> str:
    diff = current_diff(repo)
    if not diff.strip():
        return ""
    return diff[:1500]


def _latest_auto_commit(repo: RepoState) -> str:
    if not repo.tasks_path or not repo.tasks_path.exists():
        return ""
    try:
        tasks = json.loads(repo.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for entry in reversed(tasks):
        if entry.get("command_type") == "auto" and entry.get("commit_hash"):
            return entry["commit_hash"]
    return ""


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
