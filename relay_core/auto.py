"""
Relay Automation Brain
======================

The brain loop is intentionally agent-neutral:
  plan/context -> route -> execute -> verify -> diagnose -> retry/fallback -> checkpoint

Claude and Codex are peers. Routing chooses by task evidence; ties are balanced
by recent usage/config; retries can fall back to the other agent when useful.
Auto-commit is allowed after verification succeeds and policy permits it.
"""
from __future__ import annotations
import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.utils import timestamp_now, cli_available
from relay_core.git import run_agent, changed_files, current_diff, current_branch, run_command
from relay_core.cleaner import save_output
from relay_core.models import classify_task, infer_done_condition, session_cost, flush_costs
from relay_core.diagnose import diagnose_failure
from relay_core.verifiers import run_verification, verification_passed
from relay_core.memory import (
    ensure_relay_files, load_config, load_memory, save_memory,
    append_repo_task, save_last_diff, update_project_knowledge,
)
from relay_core.routing import route_task
from relay_core.outcome import build_outcome, save_outcome
from relay_core.brain_auto import refresh_brain, learn_from_run
import relay_core.tui as tui


AGENTS = ("claude", "codex")
AUTO_MODES = {"safe", "edit", "commit", "pr"}
AGENT_POLICIES = {"balanced", "route", "alternate"}


@dataclass
class BrainPermissions:
    mode: str
    can_execute: bool
    can_edit: bool
    can_commit: bool
    can_push: bool


@dataclass
class BrainAttempt:
    attempt: int
    agent: str
    prompt_kind: str
    exit_code: int
    files_changed: list[str]
    verification: dict[str, Any]
    diagnosis: dict[str, Any] | None = None
    started_at: str = ""
    finished_at: str = ""


@dataclass
class AutoRun:
    id: str
    task: str
    done_condition: str
    initial_agent: str
    current_agent: str
    agent_policy: str
    tier: str
    branch: str
    status: str
    permissions: dict[str, Any]
    retries_used: int
    max_retries: int
    cost_usd: float
    max_cost: float
    auto_commit: bool
    files_changed: list[str]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    commit_hash: str = ""
    escalate_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_auto(
    task: str,
    repo: RepoState,
    until: str | None = None,
    max_retries: int = 3,
    max_cost: float = 1.00,
    forced_agent: str | None = None,
    mode: str | None = None,
    agent_policy: str | None = None,
    auto_commit: bool | None = None,
    max_steps: int | None = None,
) -> int:
    if not cli_available("git"):
        raise RelayError("relay auto requires git.")
    if not repo.in_git_repo:
        raise RelayError("relay auto requires running inside a git repository.")

    ensure_relay_files(repo)
    relay_dir = repo.relay_dir
    assert relay_dir is not None
    refresh_brain(repo, task)

    config = load_config(repo)
    mode = _normalize_mode(mode or config.get("automation_mode", "edit"))
    agent_policy = _normalize_agent_policy(agent_policy or config.get("agent_policy", "balanced"))
    permissions = _permissions_for_mode(mode)
    auto_commit = bool(config.get("auto_commit_on_success", True) if auto_commit is None else auto_commit)
    max_steps = max_steps or int(config.get("max_auto_steps", 6))

    if not permissions.can_execute:
        tui.show_info("Safe mode selected: Relay will not execute an agent or edit files.")
        return 0

    _require_agent_tools(forced_agent)

    tui.show_info("Classifying task...")
    classification = classify_task(task, relay_dir=relay_dir)
    tier = classification.get("tier", "feature")

    if not until:
        tui.show_info("Inferring done-condition...")
        until = infer_done_condition(task, relay_dir=relay_dir)

    decision = route_task(task, repo, forced_agent=forced_agent)
    initial_agent = _select_initial_agent(decision.agent, repo, agent_policy, forced_agent, task)
    current_agent = initial_agent

    original_branch = current_branch(repo)
    branch = _create_branch(repo, task)
    run_id = str(uuid.uuid4())[:8]
    run_dir = _prepare_run_dir(relay_dir, run_id)

    run = AutoRun(
        id=run_id,
        task=task,
        done_condition=until,
        initial_agent=initial_agent,
        current_agent=current_agent,
        agent_policy=agent_policy,
        tier=tier,
        branch=branch,
        status="running",
        permissions=asdict(permissions),
        retries_used=0,
        max_retries=max_retries,
        cost_usd=0.0,
        max_cost=max_cost,
        auto_commit=auto_commit,
        files_changed=[],
        started_at=timestamp_now(),
    )
    _save_run(relay_dir, run_dir, run)
    _show_auto_header(run, decision.reason)

    prior_diagnosis: dict[str, Any] | None = None

    try:
        for attempt_index in range(min(max_retries + 1, max_steps)):
            if run.cost_usd >= max_cost:
                return _halt(relay_dir, run_dir, run, "capped", f"Budget cap ${max_cost:.2f} reached.")

            prompt_kind = "guided-retry" if prior_diagnosis else "initial"
            prompt = _build_prompt(task, until, prior_diagnosis)
            started = timestamp_now()
            tui.console.print(
                f"\n[bold white]Attempt {attempt_index + 1}/{max_retries + 1}[/bold white]  "
                f"[dim]{current_agent.capitalize()}[/dim]\n"
            )

            from relay_core.commands import _write_context
            _write_context(repo, prompt)

            exit_code, output, _resumed = run_agent(
                current_agent,
                prompt,
                repo.repo_root or repo.cwd,
                relay_dir=relay_dir,
            )
            clean = save_output(relay_dir, current_agent, output)
            _write_attempt_log(run_dir, attempt_index + 1, current_agent, clean)
            run.cost_usd += session_cost()

            if clean.strip():
                tui.show_review_output(current_agent, clean, exit_code)

            files = changed_files(repo)
            diff = current_diff(repo)
            diff_stat = run_command(["git", "diff", "--stat"], cwd=repo.repo_root).stdout.strip() if repo.repo_root else ""
            save_last_diff(repo, diff)
            run.files_changed = files

            if _must_stop_for_risk(config, files, diff):
                return _halt(relay_dir, run_dir, run, "escalated", "Risk policy matched changed files or diff.")

            tui.show_info("Verifying...")
            verification = run_verification(repo.repo_root or repo.cwd, until, files, config)
            learn_from_run(
                repo,
                task=task,
                agent=current_agent,
                files=files,
                diff_text=diff,
                success=exit_code == 0 and verification_passed(verification),
                verification=verification,
            )
            update_project_knowledge(
                repo,
                verify_commands=[check.get("command", "") for check in verification.get("commands", []) if check.get("command")],
                risky_files=[file for file in files if _risky_file(file)],
                known_failure=(verification.get("tests", {}).get("output") or "")[:240] if not verification_passed(verification) else None,
            )
            attempt = BrainAttempt(
                attempt=attempt_index + 1,
                agent=current_agent,
                prompt_kind=prompt_kind,
                exit_code=exit_code,
                files_changed=files,
                verification=verification,
                started_at=started,
                finished_at=timestamp_now(),
            )

            if exit_code == 0 and verification_passed(verification):
                run.attempts.append(asdict(attempt))
                commit_hash = ""
                if auto_commit and permissions.can_commit:
                    commit_hash = _commit_branch(repo, task)
                pr_target = ""
                if commit_hash and permissions.can_push:
                    from relay_core.brain import maybe_push_pr
                    pr_target = maybe_push_pr(repo, branch)
                run.status = "success"
                run.commit_hash = commit_hash
                if pr_target:
                    run.escalate_reason = f"PR target: {pr_target}"
                run.finished_at = timestamp_now()
                _record_task(repo, run, success=True)
                _update_agent_memory(repo, current_agent, files)
                flush_costs(relay_dir)
                _save_run(relay_dir, run_dir, run)
                _show_auto_result(run)
                outcome = build_outcome(
                    repo,
                    task=task,
                    command_type="auto",
                    agent=current_agent,
                    exit_code=0,
                    verified=True,
                    verification=verification,
                    branch=branch,
                    commit_hash=commit_hash,
                    run_id=run.id,
                    files_override=files,
                    diff_stat_override=diff_stat,
                )
                save_outcome(repo, outcome)
                tui.show_outcome(outcome)
                return 0

            if attempt_index >= max_retries:
                attempt.diagnosis = None
                run.attempts.append(asdict(attempt))
                break

            tui.show_info("Diagnosing failure...")
            error_output = _failed_verification_report(verification) or clean[-2000:]
            diagnosis = diagnose_failure(
                task=task,
                done_condition=until,
                diff=diff[:4000],
                error=error_output,
                prior_diagnosis=prior_diagnosis,
                relay_dir=relay_dir,
            )
            run.cost_usd += session_cost()
            failure_report = _failed_verification_report(verification)
            if failure_report:
                diagnosis["verification_output"] = failure_report
            attempt.diagnosis = diagnosis
            run.attempts.append(asdict(attempt))
            _save_run(relay_dir, run_dir, run)

            if not diagnosis.get("should_retry"):
                _show_failed_verification(verification)
                return _halt(
                    relay_dir,
                    run_dir,
                    run,
                    "escalated",
                    diagnosis.get("escalate_reason") or "Diagnosis recommended manual review.",
                )

            prior_diagnosis = diagnosis
            run.retries_used += 1
            current_agent = _next_agent(current_agent, agent_policy, forced_agent)
            run.current_agent = current_agent
            next_prompt = _build_prompt(task, until, prior_diagnosis)
            _show_retry_header(
                run.retries_used,
                max_retries,
                current_agent,
                diagnosis.get("guidance", ""),
                next_prompt,
                failure_report,
            )

        run.status = "failed"
        run.finished_at = timestamp_now()
        _record_task(repo, run, success=False)
        flush_costs(relay_dir)
        _save_run(relay_dir, run_dir, run)
        _show_auto_result(run)
        last_verification = run.attempts[-1].get("verification", {}) if run.attempts else {}
        outcome = build_outcome(
            repo,
            task=task,
            command_type="auto",
            agent=run.current_agent,
            exit_code=1,
            verified=False if last_verification else None,
            verification=last_verification,
            branch=branch,
            run_id=run.id,
            files_override=run.files_changed,
        )
        save_outcome(repo, outcome)
        tui.show_outcome(outcome)
        return 1

    except KeyboardInterrupt:
        run.status = "failed"
        run.finished_at = timestamp_now()
        run.escalate_reason = "Interrupted by user."
        _save_run(relay_dir, run_dir, run)
        tui.show_info(f"\nInterrupted. Branch preserved: {branch}")
        return 1
    finally:
        _return_to_original(repo, original_branch)


def _normalize_mode(mode: str) -> str:
    if mode not in AUTO_MODES:
        raise RelayError(f"Unknown automation mode '{mode}'. Use: {', '.join(sorted(AUTO_MODES))}.")
    return mode


def _normalize_agent_policy(policy: str) -> str:
    if policy not in AGENT_POLICIES:
        raise RelayError(f"Unknown agent policy '{policy}'. Use: {', '.join(sorted(AGENT_POLICIES))}.")
    return policy


def _permissions_for_mode(mode: str) -> BrainPermissions:
    return BrainPermissions(
        mode=mode,
        can_execute=mode != "safe",
        can_edit=mode in {"edit", "commit", "pr"},
        can_commit=mode in {"edit", "commit", "pr"},
        can_push=mode == "pr",
    )


def _require_agent_tools(forced_agent: str | None) -> None:
    required = [forced_agent] if forced_agent in AGENTS else list(AGENTS)
    missing = [agent for agent in required if not cli_available(agent)]
    if missing:
        raise RelayError("Missing required agent tools: " + ", ".join(missing))


def _select_initial_agent(agent: str, repo: RepoState, policy: str, forced_agent: str | None, task: str) -> str:
    if forced_agent in AGENTS:
        return forced_agent
    if policy == "alternate":
        return _least_recent_agent(repo, task)
    return agent


def _next_agent(current_agent: str, policy: str, forced_agent: str | None) -> str:
    if forced_agent in AGENTS or policy == "route":
        return current_agent
    return "codex" if current_agent == "claude" else "claude"


def _least_recent_agent(repo: RepoState, task: str) -> str:
    mem = load_memory(repo)
    stats = mem.get("agent_stats", {})
    claude = int(stats.get("claude", 0))
    codex = int(stats.get("codex", 0))
    if claude < codex:
        return "claude"
    if codex < claude:
        return "codex"
    return "claude" if sum(ord(ch) for ch in task) % 2 == 0 else "codex"


def _create_branch(repo: RepoState, task: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()[:48]).strip("-") or "task"
    ts = re.sub(r"[^0-9]", "", timestamp_now())[:14]
    branch = f"relay/auto/{ts}/{slug}"
    result = run_command(["git", "checkout", "-b", branch], cwd=repo.repo_root)
    if result.returncode != 0:
        raise RelayError(result.stderr.strip() or "Unable to create automation branch.")
    return branch


def _commit_branch(repo: RepoState, task: str) -> str:
    if not changed_files(repo):
        return ""
    run_command(["git", "add", "."], cwd=repo.repo_root)
    msg = f"relay auto: {task[:72]}"
    result = run_command(["git", "commit", "-m", msg], cwd=repo.repo_root)
    if result.returncode != 0:
        tui.show_error(result.stderr.strip() or "Auto-commit failed.")
        return ""
    hash_result = run_command(["git", "rev-parse", "--short", "HEAD"], cwd=repo.repo_root)
    return hash_result.stdout.strip()


def _return_to_original(repo: RepoState, original_branch: str) -> None:
    if original_branch:
        run_command(["git", "checkout", original_branch], cwd=repo.repo_root)


def _build_prompt(task: str, done_condition: str, diagnosis: dict[str, Any] | None) -> str:
    if not diagnosis:
        return f"Task: {task}\n\nDone when: {done_condition}"
    guidance = diagnosis.get("guidance", "")
    prompt = (
        f"Task: {task}\n\n"
        f"Done when: {done_condition}\n\n"
        f"Guidance from verification diagnosis: {guidance}"
    )
    verification_output = diagnosis.get("verification_output", "")
    if verification_output:
        prompt += (
            "\n\nFailed verification output from the previous attempt:\n"
            "```text\n"
            f"{verification_output}\n"
            "```"
        )
    return prompt


def _prepare_run_dir(relay_dir: Path, run_id: str) -> Path:
    run_dir = relay_dir / "runs" / run_id
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    return run_dir


def _save_run(relay_dir: Path, run_dir: Path, run: AutoRun) -> None:
    data = run.to_dict()
    (run_dir / "run.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _save_run_index(relay_dir, data)
    _save_brain_state(relay_dir, run)


def _save_run_index(relay_dir: Path, data: dict[str, Any]) -> None:
    path = relay_dir / "auto-runs.json"
    runs = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            runs = loaded if isinstance(loaded, list) else []
        except Exception:
            runs = []
    for idx, item in enumerate(runs):
        if item.get("id") == data.get("id"):
            runs[idx] = data
            break
    else:
        runs.append(data)
    path.write_text(json.dumps(runs[-50:], indent=2) + "\n", encoding="utf-8")


def _save_brain_state(relay_dir: Path, run: AutoRun) -> None:
    path = relay_dir / "brain.json"
    state: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            state = loaded if isinstance(loaded, dict) else {}
        except Exception:
            state = {}
    runs = [r for r in state.get("runs", []) if r.get("id") != run.id]
    runs.append({"id": run.id, "task": run.task, "status": run.status, "branch": run.branch})
    state.update({
        "version": 1,
        "active_run_id": run.id if run.status == "running" else "",
        "runs": runs[-20:],
        "agent_policy": run.agent_policy,
        "last_agents": [a.get("agent") for a in run.attempts[-10:] if a.get("agent")],
    })
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _write_attempt_log(run_dir: Path, attempt: int, agent: str, output: str) -> None:
    path = run_dir / "logs" / f"attempt-{attempt}-{agent}.log"
    path.write_text(output, encoding="utf-8")


def _must_stop_for_risk(config: dict[str, Any], files: list[str], diff: str) -> bool:
    stop_on = set(config.get("stop_on", []))
    lowered_files = " ".join(files).lower()
    lowered_diff = diff.lower()
    if "secret_detected" in stop_on and any(word in lowered_diff for word in ("api_key", "secret", "password", "private key")):
        return True
    if "migration_changed" in stop_on and "migration" in lowered_files:
        return True
    if "auth_changed" in stop_on and "auth" in lowered_files:
        return True
    return False


def _risky_file(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in (".env", "auth", "payment", "stripe", "migration", "secret"))


def _halt(relay_dir: Path, run_dir: Path, run: AutoRun, status: str, reason: str) -> int:
    run.status = status
    run.escalate_reason = reason
    run.finished_at = timestamp_now()
    flush_costs(relay_dir)
    _save_run(relay_dir, run_dir, run)
    _show_auto_result(run)
    return 1


def _record_task(repo: RepoState, run: AutoRun, success: bool) -> None:
    append_repo_task(repo, {
        "command_type": "auto",
        "timestamp": timestamp_now(),
        "original_task": run.task,
        "selected_agent": run.current_agent,
        "initial_agent": run.initial_agent,
        "agent_policy": run.agent_policy,
        "exit_code": 0 if success else 1,
        "success": success,
        "changed_files": run.files_changed,
        "auto_run_id": run.id,
        "branch": run.branch,
        "commit_hash": run.commit_hash,
    })


def _update_agent_memory(repo: RepoState, agent: str, files: list[str]) -> None:
    mem = load_memory(repo)
    stats = mem.get("agent_stats", {"claude": 0, "codex": 0})
    stats[agent] = stats.get(agent, 0) + 1
    mem["agent_stats"] = stats
    hot = mem.get("hot_files", {})
    for file in files:
        hot[file] = hot.get(file, 0) + 1
    mem["hot_files"] = dict(sorted(hot.items(), key=lambda item: item[1], reverse=True)[:50])
    save_memory(repo, mem)


def _show_auto_header(run: AutoRun, routing_reason: str) -> None:
    from rich.panel import Panel
    from rich.text import Text
    from relay_core.tui import console, AGENT_COLORS

    color = AGENT_COLORS.get(run.initial_agent, "white")
    content = Text()
    content.append("Task         ", style="dim white")
    content.append(run.task + "\n", style="bold white")
    content.append("Done when    ", style="dim white")
    content.append(run.done_condition + "\n", style="white")
    content.append("Agent        ", style="dim white")
    content.append(run.initial_agent.capitalize() + f"  [{run.agent_policy}]\n", style=color)
    content.append("Routing      ", style="dim white")
    content.append(routing_reason + "\n", style="dim")
    content.append("Mode         ", style="dim white")
    content.append(run.permissions.get("mode", "edit") + "\n", style="white")
    content.append("Auto commit  ", style="dim white")
    content.append(("yes" if run.auto_commit else "no") + "\n", style="white")
    content.append("Branch       ", style="dim white")
    content.append(run.branch, style="dim cyan")
    console.print(Panel(content, title="[bold white]Relay Automation Brain[/bold white]",
                         border_style="cyan", padding=(0, 1)))


def _failed_verification_report(verification: dict[str, Any]) -> str:
    parts: list[str] = []
    for check in verification.get("commands", []) or []:
        if check.get("passed") is not False:
            continue
        command = str(check.get("command") or "").strip()
        returncode = check.get("returncode")
        output = str(check.get("output") or "").rstrip()
        header = f"$ {command}" if command else "$ <unknown verification command>"
        if returncode is not None:
            header += f"\nexit code: {returncode}"
        parts.append(f"{header}\n{output}".rstrip())

    if parts:
        return "\n\n".join(parts)
    return str((verification.get("tests") or {}).get("output") or "").rstrip()


def _show_failed_verification(verification: dict[str, Any] | None = None, report: str = "") -> None:
    from rich.panel import Panel
    from rich.text import Text

    if not report:
        report = _failed_verification_report(verification or {})
    if not report:
        return
    tui.console.print()
    tui.console.print(Panel(
        Text(report),
        title="[bold red]Failed Verification Output[/bold red]",
        border_style="red",
        padding=(1, 2),
    ))


def _show_retry_header(
    attempt: int,
    max_retries: int,
    agent: str,
    guidance: str,
    next_prompt: str = "",
    failure_report: str = "",
) -> None:
    tui.console.print(
        f"\n[bold yellow]Retry {attempt}/{max_retries}[/bold yellow]  "
        f"[dim]Next agent: {agent.capitalize()} | {guidance[:90]}[/dim]\n"
    )
    if failure_report:
        _show_failed_verification(report=failure_report)
    if next_prompt:
        from rich.panel import Panel
        from rich.text import Text

        tui.console.print(Panel(
            Text(next_prompt),
            title="[bold white]Next Retry Prompt[/bold white]",
            border_style="yellow",
            padding=(1, 2),
        ))


def _show_auto_result(run: AutoRun) -> None:
    from rich.panel import Panel
    from rich.text import Text
    from relay_core.tui import console

    success = run.status == "success"
    color = "green" if success else "red"
    icon = "OK" if success else "STOP"
    content = Text()
    content.append("Status       ", style="dim white")
    content.append(f"{icon} {run.status}\n", style=f"bold {color}")
    content.append("Attempts     ", style="dim white")
    content.append(f"{len(run.attempts)}\n", style="white")
    content.append("Agents       ", style="dim white")
    content.append(", ".join(a.get("agent", "") for a in run.attempts) or run.initial_agent, style="white")
    content.append("\nCost         ", style="dim white")
    content.append(f"${run.cost_usd:.4f}\n", style="white")
    content.append("Branch       ", style="dim white")
    content.append(run.branch + "\n", style="dim cyan")
    if run.commit_hash:
        content.append("Commit       ", style="dim white")
        content.append(run.commit_hash + "\n", style="dim")
    if run.files_changed:
        content.append("Changed      ", style="dim white")
        content.append(", ".join(run.files_changed[:6]) + "\n", style="dim")
    if run.escalate_reason:
        content.append("Reason       ", style="dim white")
        content.append(run.escalate_reason, style="bold yellow")

    console.print(Panel(content, title="[bold white]Automation Result[/bold white]",
                         border_style=color, padding=(0, 1)))
    if success and run.commit_hash:
        tui.show_info(f"Committed on {run.branch}. Merge when ready.")
    elif success:
        tui.show_info(f"Changes are on {run.branch}.")
    else:
        tui.show_info(f"Review branch: git checkout {run.branch}")
