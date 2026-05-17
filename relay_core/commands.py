from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.constants import MAX_HANDOFF_WORDS, MAX_DIFF_PROMPT_CHARS, MAX_HANDOFF_PROMPT_CHARS
from relay_core.utils import (
    normalize_agent_name, trim_words, cli_available,
    missing_required_dependencies, timestamp_now,
)
from relay_core.git import (
    exec_agent, capture_agent_output,
    current_diff, changed_files, git_status_lines, status_changed_files,
    diff_summary, current_branch, remote_origin_url,
    latest_commit_hash, latest_commit_message, has_uncommitted_changes,
    run_command,
)
from relay_core.memory import (
    load_repo_tasks, append_repo_task, append_history_entry,
    read_handoff, write_handoff, append_decision, save_last_diff,
    latest_task, latest_agent_task, ensure_relay_files, load_config,
    load_memory, load_project_profile, update_memory_after_task,
)
from relay_core.routing import route_task, scan_project
from relay_core.diff import (
    classify_files_risk, classify_file_risk,
    smart_commit_message, detect_contradictions,
)
import relay_core.tui as tui


def require_dependencies(repo: RepoState, agent: str = "claude") -> None:
    missing = [name for name in [agent, "git"] if not cli_available(name)]
    if missing:
        raise RelayError("Missing required tools: " + ", ".join(missing))
    if not repo.in_git_repo:
        raise RelayError("Relay requires running inside a git repository.")


def warning_paths(files: list[str]) -> list[str]:
    from relay_core.constants import SENSITIVE_PATH_PATTERNS
    return sorted({f for f in files if any(p in f.lower() for p in SENSITIVE_PATH_PATTERNS)})


# ── Task runner ───────────────────────────────────────────────────────────────

def run_task(
    task: str,
    repo: RepoState,
    forced_agent: str | None = None,
    diagnose_on_fail: bool = False,
) -> int:
    """Route the task, show routing decision, inject context, then hand off
    completely to Claude or Codex via os.execvp. Relay is gone after this.
    The git post-commit hook handles logging when the user commits.
    """
    missing = missing_required_dependencies()
    if missing:
        from relay_core.utils import print_install_hints
        tui.show_error("Missing required dependencies: " + ", ".join(missing))
        print_install_hints(missing)
        return 1
    if not repo.in_git_repo:
        tui.show_error("Relay requires running inside a git repository.")
        return 1

    ensure_relay_files(repo)

    decision = route_task(task, repo, forced_agent=forced_agent)
    agent = decision.agent

    tui.show_routing_decision(decision)

    # Save pending task so the git hook can attribute it to this relay run
    from relay_core.hooks import save_pending_task
    save_pending_task(repo, task, agent)

    # Generate compressed structured context and write CLAUDE.md + context.md
    _write_context(repo, task)

    cwd = repo.repo_root or repo.cwd
    tui.show_handoff_note(agent, task)

    # Run agent — non-interactive with live elapsed counter.
    # Developer watches the timer, not a blank terminal.
    # Output shown in a clean panel when done.
    from relay_core.git import run_agent
    exit_code, output, resumed = run_agent(agent, task, cwd, relay_dir=repo.relay_dir)

    # Show session continuity notice — only when actually resuming
    if resumed:
        from relay_core.utils import normalize_agent_name
        tui.show_info(f"↩  Resumed previous {normalize_agent_name(agent)} session — full prior context available")

    # Phase 0 — clean the output
    from relay_core.cleaner import save_output
    clean = save_output(repo.relay_dir or Path(".relay"), agent, output)

    # Show result
    files = changed_files(repo)
    diff_text = current_diff(repo)
    diff_stat = diff_summary(repo)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed")
    save_last_diff(repo, diff_text)

    # Show clean output panel
    if clean.strip():
        tui.show_review_output(agent, clean, exit_code)

    # Phase 2.5 — diagnose on fail
    if diagnose_on_fail and exit_code != 0 and repo.repo_root:
        _run_diagnose(
            task=task,
            agent=agent,
            files=files,
            output=clean,
            repo=repo,
        )

    if warnings:
        tui.show_warnings(warnings)

    tui.show_result(agent, exit_code, files, "normal")

    # Update memory
    from relay_core.memory import append_repo_task
    from relay_core.utils import timestamp_now, detect_rate_limit
    rate_limited = detect_rate_limit(output)
    append_repo_task(repo, {
        "command_type": "task",
        "timestamp": timestamp_now(),
        "original_task": task,
        "selected_agent": agent,
        "exit_code": exit_code,
        "success": exit_code == 0,
        "rate_limit_detected": rate_limited,
        "changed_files": files,
        "workstream": "",
    })
    update_memory_after_task(repo, agent, files, exit_code == 0)

    return exit_code

    # Never reached — exec_agent replaces this process
    return 0


def _write_context(repo: RepoState, task: str, tier: str = "feature") -> None:
    """Write compressed structured context to .relay/context.md and CLAUDE.md.

    Uses the intelligence layer to produce minimal, task-relevant state:
    relevant symbols, active workstreams, likely files.
    Never dumps raw history or verbose logs.
    """
    if not repo.relay_dir or not repo.repo_root:
        return

    from relay_core.intelligence import generate_context
    context = generate_context(repo, task)

    # .relay/context.md — full structured context
    context_lines = [
        "# Relay Context",
        f"Task: {task}",
        "",
        context,
    ]
    (repo.relay_dir / "context.md").write_text("\n".join(context_lines), encoding="utf-8")

    # CLAUDE.md — what Claude reads on session start
    import re as _re
    claude_section = (
        "## Relay Memory\n\n"
        "**Read `.relay/context.md` immediately.** It contains:\n"
        "- The current task\n"
        "- Relevant symbols and their exact file locations\n"
        "- Active workstreams and their status\n"
        "- The specific files most likely to need editing\n\n"
        "**Do not explore the codebase broadly.** Go directly to the files listed in `.relay/context.md`.\n\n"
        f"```\n{context[:1500]}\n```\n"
    )

    claude_md = repo.repo_root / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## Relay Memory" in existing:
            updated = _re.sub(
                r"## Relay Memory.*",
                claude_section.rstrip(),
                existing,
                flags=_re.DOTALL,
            )
            claude_md.write_text(updated, encoding="utf-8")
        else:
            claude_md.write_text(existing.rstrip() + "\n\n" + claude_section, encoding="utf-8")
    else:
        claude_md.write_text(claude_section, encoding="utf-8")


def _inject_context(repo: RepoState, task: str) -> None:
    """Write Relay's accumulated memory to .relay/context.md so Claude
    can read it if referenced in CLAUDE.md."""
    if not repo.relay_dir:
        return
    tasks = load_repo_tasks(repo)
    mem = load_memory(repo)
    profile = load_project_profile(repo)

    recent = [t for t in reversed(tasks[-10:]) if t.get("original_task") or t.get("commit_message")][:5]
    hot_files = list(mem.get("hot_files", {}).items())[:5]
    risk_flags = mem.get("last_risk_flags", [])

    lines = [
        "# Relay Context",
        f"Current task: {task}",
        "",
    ]

    if profile:
        lines += [
            f"Project: {profile.get('framework', 'unknown')} · {profile.get('primary_language', 'unknown')}",
            "",
        ]

    if recent:
        lines.append("## Recent activity")
        for t in recent:
            msg = t.get("original_task") or t.get("commit_message", "")
            ts = (t.get("timestamp") or "")[:10]
            lines.append(f"- {ts}: {msg[:80]}")
        lines.append("")

    if hot_files:
        lines.append("## Most touched files")
        for f, n in hot_files:
            risk = classify_file_risk(f)
            lines.append(f"- {f} ({n}x) [{risk}]")
        lines.append("")

    if risk_flags:
        lines.append("## Recent risk flags")
        for f in risk_flags:
            lines.append(f"- {f}")
        lines.append("")

    # Phase 3 — tiered context: inject only what the task tier needs
    if repo.repo_root and tier == "trivial":
        # Trivial: find and include only the single most relevant file
        relevant = _relevant_files_for_task(task, repo)
        if relevant:
            f = repo.repo_root / relevant[0]
            if f.exists() and f.stat().st_size < 80_000:
                content = f.read_text(encoding="utf-8", errors="replace")
                lines += [f"## File: {relevant[0]}", "```", content[:4000], "```", ""]

    elif repo.repo_root and tier == "architectural":
        # Architectural: include workstreams, symbols, and multiple files
        from relay_core.intelligence import load_workstreams, load_symbols
        workstreams = load_workstreams(repo)
        symbols = load_symbols(repo)
        if workstreams:
            lines += ["## All workstreams"]
            for ws_name, ws in list(workstreams.items())[:5]:
                lines.append(f"- {ws_name}: {ws.get('goal', '')[:60]}")
            lines.append("")
        if symbols:
            lines += ["## All tracked symbols"]
            for sym, loc in list(symbols.items())[:30]:
                lines.append(f"- `{sym}` — {loc['file']}:{loc.get('line','?')} [{loc.get('type','?')}]")
            lines.append("")

    else:
        # Feature (default): relevant workstream + symbols only (already injected above)
        pass

    context_path = repo.relay_dir / "context.md"
    context_path.write_text("\n".join(lines), encoding="utf-8")


def _relevant_files_for_task(task: str, repo: RepoState) -> list[str]:
    """Return the specific files most relevant to this task so Claude
    can go straight to them without exploring the codebase."""
    task_lower = task.lower()
    candidates: list[tuple[int, str]] = []

    if not repo.repo_root:
        return []

    # Score every tracked file against task keywords
    for root, dirs, files in os.walk(repo.repo_root):
        # Skip hidden and irrelevant dirs
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "__pycache__", ".relay", ".next",
                    "dist", "build", ".venv", "venv"}]
        for fname in files:
            full = Path(root) / fname
            rel = str(full.relative_to(repo.repo_root))
            rel_lower = rel.lower()
            score = 0
            # Score by path match against task words
            for word in task_lower.split():
                if len(word) > 3 and word in rel_lower:
                    score += 3
            # Boost recently hot files
            if score > 0:
                candidates.append((score, rel))

    # Sort by score, return top 3
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [rel for _, rel in candidates[:3]]


def _update_claude_md(repo: RepoState, task: str = "") -> None:
    """Write/update CLAUDE.md so Claude knows exactly which files to edit.

    Instead of exploring the codebase, Claude reads this and goes straight
    to the relevant files. Updated before every relay task handoff.
    """
    if not repo.repo_root or not repo.relay_dir:
        return

    relevant = _relevant_files_for_task(task, repo) if task else []

    relay_section = "## Relay — Project Context\n\n"
    relay_section += "**Read `.relay/context.md` first** — it has the current task, recent activity, and hot files.\n\n"

    relay_section += "### Project structure\n"
    relay_section += "- `relay_core/` — CLI engine (routing, commands, git ops, memory, TUI)\n"
    relay_section += "- `relay_dashboard/templates/index.html` — the entire web dashboard UI\n"
    relay_section += "- `relay_dashboard/server.py` — Flask server + API endpoints\n"
    relay_section += "- `relay_ci/` — CI audit tooling\n"
    relay_section += "- `.relay/` — local memory (tasks.json, context.md, config.json)\n\n"

    if relevant:
        relay_section += "### Files to focus on for this task\n"
        for f in relevant:
            relay_section += f"- `{f}`\n"
        relay_section += "\nGo directly to these files. Skip broad codebase exploration.\n"

    import re
    claude_md = repo.repo_root / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## Relay" in existing:
            updated = re.sub(
                r"## Relay.*?(?=\n## |\Z)",
                relay_section.rstrip(),
                existing,
                flags=re.DOTALL,
            )
            claude_md.write_text(updated, encoding="utf-8")
        else:
            claude_md.write_text(existing.rstrip() + "\n\n" + relay_section, encoding="utf-8")
    else:
        claude_md.write_text(relay_section, encoding="utf-8")


# ── Review commands ───────────────────────────────────────────────────────────

def _run_diagnose(
    task: str,
    agent: str,
    files: list[str],
    output: str,
    repo: RepoState,
) -> None:
    """Phase 2.5 — run one diagnose call after a failed task and show guidance."""
    from relay_core.diagnose import diagnose_failure, verify_task, infer_done_condition
    from relay_core.models import infer_done_condition as haiku_condition
    from relay_core.git import current_diff

    tui.console.print()
    tui.show_info("Running failure diagnosis...")

    diff = current_diff(repo)

    # Infer done-condition via Haiku if not provided
    done_condition = haiku_condition(task, relay_dir=repo.relay_dir)

    # Cheap verification first
    verify = verify_task(repo.repo_root or repo.cwd, done_condition, files)

    diagnosis = diagnose_failure(
        task=task,
        done_condition=done_condition,
        diff=diff[:4000],
        error=output[-2000:] if output else "No error output captured.",
        relay_dir=repo.relay_dir,
    )

    # Display diagnosis
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    from relay_core.tui import console

    conf = diagnosis.get("confidence", 0)
    conf_color = "bold green" if conf >= 0.7 else "bold yellow" if conf >= 0.4 else "bold red"
    should_retry = diagnosis.get("should_retry", False)

    content = Text()
    content.append("Root cause  ", style="dim white")
    content.append(diagnosis.get("root_cause", "unknown") + "\n", style="white")
    content.append("Category    ", style="dim white")
    content.append(diagnosis.get("category", "unknown") + "\n", style="bold white")
    content.append("Confidence  ", style="dim white")
    content.append(f"{conf:.0%}\n", style=conf_color)
    content.append("\nGuidance\n", style="dim white")
    content.append(diagnosis.get("guidance", ""), style="bold white")

    if not should_retry:
        content.append("\n\nEscalate    ", style="dim white")
        content.append(diagnosis.get("escalate_reason", ""), style="bold yellow")

    border = "green" if should_retry else "red"
    console.print(Panel(
        content,
        title=f"[bold white]{'↻ Retry recommended' if should_retry else '⚑ Escalate — do not retry'}[/bold white]",
        border_style=border,
        padding=(1, 2),
    ))

    if should_retry:
        tui.show_info(
            f"Re-run with this guidance: "
            f"relay @{agent} \"{task}\" --diagnose-on-fail"
        )


def run_review(repo: RepoState) -> int:
    """Instant local review — no AI, no tokens."""
    if not repo.in_git_repo:
        raise RelayError("Relay review requires a git repository.")

    diff_text = current_diff(repo)
    if not diff_text.strip():
        tui.show_info("No changes to review.")
        return 0

    files = changed_files(repo)
    risk_levels = classify_files_risk(files)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed")
    contradictions = detect_contradictions(files, diff_text)
    commit_msg = smart_commit_message(files, diff_text)
    stat = diff_summary(repo)

    extra_findings: list[str] = []
    diff_lower = diff_text.lower()
    if any(x in diff_lower for x in ("todo", "fixme", "hack")):
        extra_findings.append("TODO / FIXME / HACK found in diff")
    if "console.log" in diff_lower or "print(" in diff_lower:
        extra_findings.append("Debug statements found in diff")
    if any(x in diff_lower for x in ("password", "secret", "hardcode")):
        extra_findings.append("Possible hardcoded credential in diff")
    added = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))
    if added > 300:
        extra_findings.append(f"Large diff: {added} lines added")

    tui.show_local_review(
        files=files, risk_levels=risk_levels, warnings=warnings,
        contradictions=contradictions, extra_findings=extra_findings,
        commit_msg=commit_msg, stat=stat, added=added, removed=removed,
    )
    tui.show_info("Run 'relay ai-review' for a deep AI-powered review.")
    return 0


def run_ai_review(repo: RepoState) -> int:
    """Deep AI review — captures output and shows in a panel."""
    if not cli_available("claude"):
        raise RelayError("Claude Code not installed.")
    if not repo.in_git_repo:
        raise RelayError("Relay ai-review requires a git repository.")

    ensure_relay_files(repo)
    diff_text = current_diff(repo)
    if not diff_text.strip():
        tui.show_info("No changes to review.")
        return 0

    files = changed_files(repo)
    tasks = load_repo_tasks(repo)
    last = latest_task(tasks)
    last_agent = last.get("selected_agent") if last else None
    review_agent = "claude" if last_agent != "claude" else "codex"

    prompt = (
        "Review this git diff for bugs, broken logic, missing tests, security risks, "
        "and unnecessary edits. Return concise numbered findings only.\n\n"
        f"Changed files: {', '.join(files) or 'none'}\n\n"
        f"Diff:\n{diff_text[:MAX_DIFF_PROMPT_CHARS]}"
    )

    exit_code, output = capture_agent_output(review_agent, prompt, repo.repo_root or repo.cwd)
    tui.show_review_output(review_agent, output, exit_code)
    return exit_code


# ── Summary ───────────────────────────────────────────────────────────────────

def run_summary(repo: RepoState) -> int:
    if not repo.in_git_repo:
        raise RelayError("Relay summary requires a git repository.")
    files = changed_files(repo)
    if not files:
        tui.show_info("No current git diff.")
        return 0
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


# ── Git operations ────────────────────────────────────────────────────────────

def run_commit(repo: RepoState) -> int:
    if not repo.in_git_repo:
        raise RelayError("Relay commit requires a git repository.")
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

    if success:
        tui.show_success("Commit complete.")
    return 0 if success else commit_result.returncode


def run_push(repo: RepoState) -> int:
    if not repo.in_git_repo:
        raise RelayError("Relay push requires a git repository.")
    ensure_relay_files(repo)

    remote = remote_origin_url(repo)
    if not remote:
        tui.show_info("No remote origin. Add one with: git remote add origin <url>")
        return 0
    if has_uncommitted_changes(repo):
        tui.show_error("Uncommitted changes. Run 'relay commit' first.")
        return 1

    branch = current_branch(repo)
    tui.show_push_preview(remote, branch, latest_commit_hash(repo), latest_commit_message(repo))

    if not tui.ask_confirm("Push this branch?"):
        tui.show_info("Push cancelled.")
        return 0

    push_result = run_command(["git", "push"], cwd=repo.repo_root)
    success = push_result.returncode == 0
    combined = "\n".join(p for p in [push_result.stdout.strip(), push_result.stderr.strip()] if p)

    if not success and "no upstream branch" in combined.lower():
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
    if success:
        tui.show_success("Push complete.")
    return 0 if success else push_result.returncode


# ── New commands ──────────────────────────────────────────────────────────────

def run_init(repo: RepoState) -> int:
    """Set up Relay in the current repo — hooks, config, fingerprint."""
    if not repo.in_git_repo:
        raise RelayError("relay init requires a git repository.")

    ensure_relay_files(repo)

    # Install git hook
    from relay_core.hooks import install_hooks, hooks_installed
    relay_py = Path(__file__).parent.parent / "relay.py"
    install_hooks(repo, relay_py)

    # Fingerprint project
    tui.show_info("Scanning project...")
    profile = scan_project(repo)

    tui.console.print()
    from rich.panel import Panel
    from rich.text import Text
    content = Text()
    content.append("✓  Git hook installed\n", style="bold green")
    content.append("✓  Project fingerprinted\n", style="bold green")
    content.append("✓  .relay/ initialised\n", style="bold green")
    content.append("\n", style="")
    content.append("Every commit is now logged automatically.\n", style="dim")
    content.append("Run ", style="dim")
    content.append("relay dashboard", style="bold cyan")
    content.append(" to see your project activity.", style="dim")
    tui.console.print(Panel(content, title="[bold white]Relay Initialised[/bold white]",
                            border_style="green", padding=(1, 2)))
    return 0


def run_context(repo: RepoState) -> int:
    """Show the context Relay has built up about this project."""
    if not repo.in_git_repo:
        raise RelayError("relay context requires a git repository.")

    tasks = load_repo_tasks(repo) if repo.tasks_path and repo.tasks_path.exists() else []
    mem = load_memory(repo) if repo.memory_path else {}
    profile = load_project_profile(repo)

    tui.show_context(tasks, mem, profile)
    return 0


def run_digest(repo: RepoState) -> int:
    """Full project health report — activity, risks, hot files, suggestions."""
    if not repo.in_git_repo:
        raise RelayError("relay digest requires a git repository.")

    tasks = load_repo_tasks(repo) if repo.tasks_path and repo.tasks_path.exists() else []
    mem = load_memory(repo) if repo.memory_path else {}
    profile = load_project_profile(repo)

    tui.show_digest(tasks, mem, profile)
    return 0


def run_scan(repo: RepoState) -> int:
    if not repo.in_git_repo:
        raise RelayError("relay scan requires a git repository.")
    ensure_relay_files(repo)
    tui.show_info("Scanning project...")
    profile = scan_project(repo)
    if profile:
        tui.show_fingerprint_result(profile)
    return 0


def run_config(repo: RepoState) -> int:
    config = load_config(repo)
    tui.show_config(config)
    return 0


def run_auto_cmd(
    task: str,
    repo: RepoState,
    until: str | None = None,
    max_retries: int = 3,
    max_cost: float = 1.00,
    forced_agent: str | None = None,
) -> int:
    from relay_core.auto import run_auto
    return run_auto(task, repo, until=until, max_retries=max_retries,
                    max_cost=max_cost, forced_agent=forced_agent)


def run_plan_cmd(goal: str, repo: RepoState, dry_run: bool = False) -> int:
    from relay_core.planner import run_plan
    return run_plan(goal, repo, dry_run=dry_run)


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
