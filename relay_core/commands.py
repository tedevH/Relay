from __future__ import annotations
import sys
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.constants import MAX_HANDOFF_WORDS, MAX_DIFF_PROMPT_CHARS, MAX_HANDOFF_PROMPT_CHARS
from relay_core.utils import (
    normalize_agent_name, trim_words, cli_available,
    missing_required_dependencies, detect_rate_limit, timestamp_now,
)
from relay_core.git import (
    build_agent_command, stream_subprocess, current_diff,
    changed_files, git_status_lines, status_changed_files,
    diff_summary, current_branch, remote_origin_url,
    latest_commit_hash, latest_commit_message, has_uncommitted_changes,
    git_output, run_command,
)
from relay_core.memory import (
    load_repo_tasks, append_repo_task, append_history_entry,
    read_handoff, write_handoff, append_decision, save_last_diff,
    latest_task, latest_agent_task, ensure_relay_files, load_config,
    update_memory_after_task,
)
from relay_core.routing import route_task, scan_project
from relay_core.diff import classify_files_risk, smart_commit_message, detect_contradictions, diff_trend
import relay_core.tui as tui


def require_task_dependencies(repo: RepoState) -> None:
    missing = missing_required_dependencies()
    if missing:
        raise RelayError("Missing required dependencies: " + ", ".join(missing))
    if not repo.in_git_repo:
        raise RelayError("Relay AI tasks require running inside a git repository.")


def warning_paths(files: list[str]) -> list[str]:
    from relay_core.constants import SENSITIVE_PATH_PATTERNS
    warnings: list[str] = []
    for path in files:
        if any(pattern in path.lower() for pattern in SENSITIVE_PATH_PATTERNS):
            warnings.append(path)
    return sorted(set(warnings))


def summarize_output(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "No notable agent output captured."
    return trim_words(" ".join(lines[-8:]), 80)


def compact_diff_for_prompt(repo: RepoState) -> str:
    diff_text = current_diff(repo)
    if not diff_text.strip():
        return "No current git diff."
    return diff_text[:MAX_DIFF_PROMPT_CHARS]


def create_handoff(agent: str, task: str, files: list[str], output: str, diff_stat: str, warnings: list[str]) -> str:
    sections = [
        f"Previous agent: {normalize_agent_name(agent)}",
        f"Previous task: {task}",
        "Changed files: " + (", ".join(files) if files else "none"),
        "Important context: " + summarize_output(output),
        "Assumptions: Relay preserved in-progress work and did not auto-commit.",
        "Suggested next steps: relay review, relay summary, or relay continue \"next task\".",
    ]
    if diff_stat:
        sections.insert(3, "Diff summary: " + trim_words(diff_stat.replace("\n", " "), 40))
    if warnings:
        sections.append("Risk flags: " + ", ".join(warnings))
    return trim_words("\n".join(sections), MAX_HANDOFF_WORDS)


def task_entry(
    *,
    original_task: str,
    prompt_type: str,
    selected_agent: str,
    command_used: list[str],
    decision: Any,
    exit_code: int,
    success: bool,
    rate_limit_detected: bool,
    changed_files_list: list[str],
    handoff_summary: str,
) -> dict[str, Any]:
    return {
        "command_type": "task",
        "timestamp": timestamp_now(),
        "original_task": original_task,
        "final_prompt_type": prompt_type,
        "selected_agent": selected_agent,
        "command_used": command_used,
        "routing_reason": decision.reason,
        "claude_score": decision.claude_score,
        "codex_score": decision.codex_score,
        "exit_code": exit_code,
        "success": success,
        "rate_limit_detected": rate_limit_detected,
        "changed_files": changed_files_list,
        "handoff_summary": handoff_summary,
    }


def execute_agent_run(
    *,
    repo: RepoState,
    user_task: str,
    prompt: str,
    prompt_type: str,
    forced_agent: str | None = None,
    extra_context: str = "",
    skip_decompose: bool = False,
) -> int:
    require_task_dependencies(repo)
    ensure_relay_files(repo)

    # Task decomposition check
    if not skip_decompose and prompt_type == "normal":
        from relay_core.chain import decompose_task
        steps = decompose_task(user_task)
        if steps:
            tui.show_decomposition_plan(steps)
            choice = tui.ask_choice(
                "How would you like to run this?",
                choices=["all", "first", "single"],
                default="single",
            )
            if choice == "all":
                last_code = 0
                for step in steps:
                    last_code = execute_agent_run(
                        repo=repo, user_task=step, prompt=step,
                        prompt_type="normal", skip_decompose=True,
                    )
                return last_code
            elif choice == "first":
                return execute_agent_run(
                    repo=repo, user_task=steps[0], prompt=steps[0],
                    prompt_type="normal", skip_decompose=True,
                )
            # else: fall through and run as single task

    decision = route_task(user_task, repo, forced_agent=forced_agent, extra_context=extra_context)
    agent = decision.agent
    command = build_agent_command(agent, prompt)

    tui.show_routing_decision(decision)
    tui.show_agent_running(command)

    # review/audit: run quietly and show output in a clean panel
    quiet_mode = prompt_type in {"review", "chain-review", "audit"}
    exit_code, output = stream_subprocess(command, cwd=repo.repo_root or repo.cwd, quiet=quiet_mode)

    if quiet_mode:
        tui.show_review_output(agent, output, exit_code)
    else:
        tui.show_agent_completion_note(agent, output, exit_code)

    rate_limited = detect_rate_limit(output)
    if rate_limited:
        tui.show_warnings(["Rate limit or usage limit detected from agent output."])

    files = changed_files(repo)
    diff_text = current_diff(repo)
    diff_stat = diff_summary(repo)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed")
    save_last_diff(repo, diff_text)

    handoff = create_handoff(agent, user_task, files, output, diff_stat, warnings)
    write_handoff(repo, handoff)
    append_decision(repo, f"[{timestamp_now()}] {normalize_agent_name(agent)} | {prompt_type} | {decision.reason}")
    update_memory_after_task(repo, agent, files, exit_code == 0)

    append_repo_task(repo, task_entry(
        original_task=user_task,
        prompt_type=prompt_type,
        selected_agent=agent,
        command_used=command,
        decision=decision,
        exit_code=exit_code,
        success=exit_code == 0,
        rate_limit_detected=rate_limited,
        changed_files_list=files,
        handoff_summary=handoff,
    ))

    if warnings:
        tui.show_warnings(warnings)

    tui.show_result(agent, exit_code, files, prompt_type)
    return exit_code


def run_main_task(task: str, repo: RepoState, forced_agent: str | None = None) -> int:
    return execute_agent_run(repo=repo, user_task=task, prompt=task, prompt_type="normal", forced_agent=forced_agent)


def run_continue(task: str, repo: RepoState) -> int:
    require_task_dependencies(repo)
    ensure_relay_files(repo)
    tasks = load_repo_tasks(repo)
    last = latest_task(tasks)
    handoff = read_handoff(repo)
    files = changed_files(repo)
    compact_context = "\n".join([
        f"Current user task: {task}",
        "Latest handoff:",
        trim_words(handoff, 180) if handoff else "No prior handoff.",
        "Changed files:",
        ", ".join(files) if files else "none",
        "Relevant diff summary:",
        trim_words(diff_summary(repo) or "No current diff.", 80),
        "Preserve prior work and continue from the existing state.",
    ])
    if last:
        compact_context += f"\nPrevious Relay task: {last.get('original_task', '')}"
    prompt = trim_words(compact_context, 500)
    return execute_agent_run(
        repo=repo, user_task=task, prompt=prompt, prompt_type="continue",
        extra_context=" ".join(files) + " " + handoff[:MAX_HANDOFF_PROMPT_CHARS],
        skip_decompose=True,
    )


def run_review(repo: RepoState) -> int:
    """Instant local review — no AI, no tokens, no waiting.
    Uses diff intelligence already built in: risk classification,
    contradiction detection, pattern checks, commit suggestion.
    Run 'relay ai-review' for a deep AI-powered review.
    """
    if not cli_available("git"):
        raise RelayError("Relay review requires git.")
    if not repo.in_git_repo:
        raise RelayError("Relay review requires running inside a git repository.")

    diff_text = current_diff(repo)
    if not diff_text.strip():
        tui.show_info("No changes to review.")
        return 0

    files = changed_files(repo)
    risk_levels = classify_files_risk(files)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed — large diff, review carefully")
    contradictions = detect_contradictions(files, diff_text)
    commit_msg = smart_commit_message(files, diff_text)
    stat = diff_summary(repo)

    # Additional local checks
    extra_findings: list[str] = []
    diff_lower = diff_text.lower()
    if "todo" in diff_lower or "fixme" in diff_lower or "hack" in diff_lower:
        extra_findings.append("TODO / FIXME / HACK found in diff — intentional?")
    if "console.log" in diff_lower or "print(" in diff_lower:
        extra_findings.append("Debug statements (console.log / print) found in diff")
    if "hardcode" in diff_lower or "password" in diff_lower or "secret" in diff_lower:
        extra_findings.append("Possible hardcoded secret or credential in diff")
    added_lines = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed_lines = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))
    if added_lines > 300:
        extra_findings.append(f"Large diff: {added_lines} lines added — consider splitting into smaller commits")

    tui.show_local_review(
        files=files,
        risk_levels=risk_levels,
        warnings=warnings,
        contradictions=contradictions,
        extra_findings=extra_findings,
        commit_msg=commit_msg,
        stat=stat,
        added=added_lines,
        removed=removed_lines,
    )
    tui.show_info("Run 'relay ai-review' for a deep AI-powered review.")
    return 0


def run_ai_review(repo: RepoState) -> int:
    """Deep AI-powered review using the opposite agent. Uses tokens — run selectively."""
    require_task_dependencies(repo)
    ensure_relay_files(repo)
    diff_text = current_diff(repo)
    if not diff_text.strip():
        tui.show_info("No changes to review.")
        return 0

    tasks = load_repo_tasks(repo)
    last = latest_task(tasks)
    last_agent = last.get("selected_agent") if last else None
    forced_agent = "claude" if last_agent == "codex" else "codex"

    files = changed_files(repo)
    prompt = (
        "Review this git diff for bugs, broken logic, missing tests, security risks, "
        "risky file changes, and unnecessary edits. Do not rewrite code unless asked. "
        "Return concise, numbered findings only.\n\n"
        f"Changed files: {', '.join(files) or 'none'}\n\n"
        f"Diff:\n{diff_text[:MAX_DIFF_PROMPT_CHARS]}"
    )
    return execute_agent_run(
        repo=repo, user_task="Deep AI review of current git diff",
        prompt=prompt, prompt_type="review",
        forced_agent=forced_agent,
        extra_context="review " + " ".join(files),
        skip_decompose=True,
    )


def run_summary(repo: RepoState) -> int:
    if not cli_available("git"):
        raise RelayError("Relay summary requires git.")
    if not repo.in_git_repo:
        raise RelayError("Relay summary requires running inside a git repository.")

    files = changed_files(repo)
    if not files:
        tui.show_info("No current git diff.")
        return 0

    tasks = load_repo_tasks(repo) if repo.tasks_path and repo.tasks_path.exists() else []
    diff_text = current_diff(repo)
    risk_levels = classify_files_risk(files)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed")
    contradictions = detect_contradictions(files, diff_text)
    commit_msg = smart_commit_message(files, diff_text)
    stat = diff_summary(repo)

    tui.show_summary(files, stat, warnings, commit_msg, risk_levels, contradictions)
    return 0


def run_commit(repo: RepoState) -> int:
    if not cli_available("git"):
        raise RelayError("Relay commit requires git.")
    if not repo.in_git_repo:
        raise RelayError("Relay commit requires running inside a git repository.")

    ensure_relay_files(repo)
    config = load_config(repo)

    if config.get("require_review_before_commit"):
        tasks = load_repo_tasks(repo)
        last = latest_task(tasks)
        if not last or last.get("final_prompt_type") not in {"review", "chain-review"}:
            tui.show_error("Config requires a review before committing. Run 'relay review' first.")
            return 1

    status_lines = git_status_lines(repo)
    if not status_lines:
        tui.show_info("No changes to commit.")
        return 0

    files = status_changed_files(repo)
    diff_text = current_diff(repo)
    risk_levels = classify_files_risk(files)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed")
    message = smart_commit_message(files, diff_text)

    tui.show_commit_preview(files, message, warnings, risk_levels)

    if not tui.ask_confirm("Commit these changes?"):
        tui.show_info("Commit cancelled.")
        return 0

    add_result = run_command(["git", "add", "."], cwd=repo.repo_root)
    if add_result.returncode != 0:
        raise RelayError(add_result.stderr.strip() or "git add failed.")

    commit_result = run_command(["git", "commit", "-m", message], cwd=repo.repo_root)
    success = commit_result.returncode == 0
    if commit_result.stdout.strip():
        tui.console.print(commit_result.stdout.strip())
    if not success and commit_result.stderr.strip():
        tui.show_error(commit_result.stderr.strip())

    append_history_entry(repo, {
        "command_type": "commit",
        "commit_message": message,
        "changed_files": files,
        "success": success,
    })

    if success:
        tui.show_success("Commit complete.")
    return 0 if success else commit_result.returncode


def run_push(repo: RepoState) -> int:
    if not cli_available("git"):
        raise RelayError("Relay push requires git.")
    if not repo.in_git_repo:
        raise RelayError("Relay push requires running inside a git repository.")

    ensure_relay_files(repo)
    remote = remote_origin_url(repo)
    if not remote:
        tui.show_info("No remote origin found. Add one with: git remote add origin <url>")
        return 0

    if has_uncommitted_changes(repo):
        tui.show_error("You have uncommitted changes. Run 'relay commit' first.")
        return 1

    branch = current_branch(repo)
    commit_hash = latest_commit_hash(repo)
    commit_message = latest_commit_message(repo)

    tui.show_push_preview(remote, branch, commit_hash, commit_message)

    if not tui.ask_confirm("Push this branch?"):
        tui.show_info("Push cancelled.")
        return 0

    push_result = run_command(["git", "push"], cwd=repo.repo_root)
    success = push_result.returncode == 0
    combined = "\n".join(p for p in [push_result.stdout.strip(), push_result.stderr.strip()] if p)

    if not success and ("no upstream branch" in combined.lower() or "has no upstream branch" in combined.lower()):
        suggested = ["git", "push", "-u", "origin", branch]
        tui.show_info(f"Upstream missing. Suggested: {' '.join(suggested)}")
        if tui.ask_confirm("Push and set upstream?"):
            push_result = run_command(suggested, cwd=repo.repo_root)
            success = push_result.returncode == 0
            combined = "\n".join(p for p in [push_result.stdout.strip(), push_result.stderr.strip()] if p)
        else:
            tui.show_info("Push cancelled.")
            return 0

    if combined:
        tui.console.print(combined)

    append_history_entry(repo, {
        "command_type": "push",
        "remote": remote,
        "branch": branch,
        "commit_hash": commit_hash,
        "success": success,
    })

    if success:
        tui.show_success("Push complete.")
    return 0 if success else push_result.returncode


def run_scan(repo: RepoState) -> int:
    if not repo.in_git_repo:
        raise RelayError("Relay scan requires running inside a git repository.")
    ensure_relay_files(repo)
    tui.show_info("Scanning project...")
    profile = scan_project(repo)
    if profile:
        tui.show_fingerprint_result(profile)
    else:
        tui.show_info("No files found to scan.")
    return 0


def run_config(repo: RepoState) -> int:
    from relay_core.memory import load_config
    config = load_config(repo)
    tui.show_config(config)
    return 0


def run_dashboard(repo: RepoState) -> int:
    try:
        from relay_dashboard.server import start_dashboard
    except ImportError:
        tui.show_error("Flask not installed. Run: pip install flask")
        return 1
    return start_dashboard(repo)


def run_audit(repo: RepoState, ci_mode: bool = False) -> int:
    from relay_ci.audit import run_audit as _audit
    return _audit(repo, ci_mode=ci_mode)
