from __future__ import annotations
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
    load_memory, load_project_profile,
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

def run_task(task: str, repo: RepoState, forced_agent: str | None = None) -> int:
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

    # Update CLAUDE.md and context.md before handing off so Claude reads
    # project memory immediately without exploring the codebase first
    _inject_context(repo, task)
    _update_claude_md(repo)

    cwd = repo.repo_root or repo.cwd
    tui.show_handoff_note(agent, task)

    # Hand off completely — Relay process is replaced by Claude/Codex
    exec_agent(agent, task, cwd)

    # Never reached — exec_agent replaces this process
    return 0


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

    context_path = repo.relay_dir / "context.md"
    context_path.write_text("\n".join(lines), encoding="utf-8")


def _update_claude_md(repo: RepoState) -> None:
    """Write or update CLAUDE.md at the repo root so Claude reads project
    memory automatically on every session — no codebase exploration needed.

    Only writes the Relay section. If CLAUDE.md already exists with other
    content, the Relay section is appended or updated in-place.
    """
    if not repo.repo_root or not repo.relay_dir:
        return

    claude_md = repo.repo_root / "CLAUDE.md"
    relay_section = f"""## Relay Memory

This project uses Relay for AI-assisted development tracking.
Read `.relay/context.md` before starting any task — it contains:
- The current task description
- Recent activity on this repo
- Most frequently modified files and their risk levels
- Known risk flags from previous sessions

File structure:
- `relay_core/` — CLI engine (routing, commands, git, memory, TUI)
- `relay_dashboard/` — local web dashboard (Flask + HTML/CSS/JS)
- `relay_ci/` — CI audit tooling
- `.relay/` — local memory (tasks, diffs, context, config)

Always check `.relay/context.md` first. It saves you from exploring files you already know about.
"""

    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## Relay Memory" in existing:
            # Update existing Relay section
            import re
            updated = re.sub(
                r"## Relay Memory.*?(?=\n## |\Z)",
                relay_section.strip(),
                existing,
                flags=re.DOTALL,
            )
            claude_md.write_text(updated, encoding="utf-8")
        else:
            # Append Relay section
            claude_md.write_text(existing.rstrip() + "\n\n" + relay_section, encoding="utf-8")
    else:
        claude_md.write_text(relay_section, encoding="utf-8")


# ── Review commands ───────────────────────────────────────────────────────────

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
