from __future__ import annotations
import sys
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.utils import cli_available, missing_required_dependencies
from relay_core.git import current_diff, changed_files, build_agent_command, stream_subprocess
from relay_core.memory import load_repo_tasks, latest_task, ensure_relay_files
from relay_core.diff import classify_file_risk, classify_files_risk
from relay_core.constants import MAX_DIFF_PROMPT_CHARS
import relay_core.tui as tui

FINDING_KEYWORDS = (
    "bug", "security", "risk", "broken", "missing", "vulnerability",
    "injection", "leak", "unsafe", "error", "crash", "race condition",
    "hardcoded", "secret", "password", "token", "unhandled",
)


def run_audit(repo: RepoState, ci_mode: bool = False) -> int:
    if not cli_available("git"):
        raise RelayError("Relay audit requires git.")
    if not repo.in_git_repo:
        raise RelayError("Relay audit requires running inside a git repository.")

    ensure_relay_files(repo)

    diff_text = current_diff(repo)
    if not diff_text.strip():
        if ci_mode:
            print("## Relay Audit\n\nNo changes to audit.")
        else:
            tui.show_info("No changes to audit.")
        return 0

    files = changed_files(repo)
    risk_levels = classify_files_risk(files)
    high_risk_files = [f for f, r in risk_levels.items() if r == "HIGH"]

    # Choose review agent
    tasks = load_repo_tasks(repo)
    last = latest_task(tasks)
    last_agent = last.get("selected_agent") if last else None
    review_agent = "claude" if last_agent != "claude" else "codex"

    prompt = (
        "You are performing a CI code review. Analyze this git diff for:\n"
        "- Bugs or broken logic\n"
        "- Security vulnerabilities (injection, hardcoded secrets, unvalidated input)\n"
        "- Missing error handling\n"
        "- Risky file changes (auth, env, migrations, payments)\n"
        "- Unnecessary or unintended changes\n\n"
        "For each finding, rate it HIGH, MEDIUM, or LOW severity.\n"
        "Return concise, actionable findings only. No praise.\n\n"
        f"Changed files: {', '.join(files) or 'none'}\n\n"
        f"Diff:\n{diff_text[:MAX_DIFF_PROMPT_CHARS]}"
    )

    if not ci_mode:
        tui.console.print()
        tui.console.print(f"[bold white]Audit[/bold white]  [dim]reviewing with {review_agent}[/dim]")
        tui.console.print()

    command = build_agent_command(review_agent, prompt)
    cwd = repo.repo_root or repo.cwd
    exit_code, output = stream_subprocess(command, cwd)

    # Count findings by severity
    output_lower = output.lower()
    high_findings = output_lower.count("high")
    medium_findings = output_lower.count("medium")
    low_findings = output_lower.count("low")
    keyword_hits = [kw for kw in FINDING_KEYWORDS if kw in output_lower]

    has_high_risk = bool(high_risk_files) or high_findings > 0

    if ci_mode:
        _print_ci_report(files, risk_levels, output, high_findings, medium_findings, low_findings, keyword_hits)

    if not ci_mode:
        from rich.table import Table
        from rich import box
        summary = Table(box=box.ROUNDED, border_style="dim", show_header=False, padding=(0, 1))
        summary.add_column(style="dim white", width=20)
        summary.add_column()
        summary.add_row("HIGH findings", str(high_findings) if high_findings else "[green]0[/green]")
        summary.add_row("MEDIUM findings", str(medium_findings))
        summary.add_row("LOW findings", str(low_findings))
        summary.add_row("High-risk files", ", ".join(high_risk_files) if high_risk_files else "[green]none[/green]")
        from rich.panel import Panel
        border = "red" if has_high_risk else "green"
        tui.console.print()
        tui.console.print(Panel(summary, title="[bold white]Audit Summary[/bold white]", border_style=border, padding=(0, 1)))
        if has_high_risk:
            tui.show_error("High-risk findings detected. Review before committing.")
        else:
            tui.show_success("No high-risk findings. Safe to proceed.")

    return 1 if has_high_risk else 0


def _print_ci_report(
    files: list[str],
    risk_levels: dict[str, str],
    output: str,
    high: int,
    medium: int,
    low: int,
    keywords: list[str],
) -> None:
    lines = [
        "## Relay Audit Report",
        "",
        f"**Findings:** HIGH: {high}  MEDIUM: {medium}  LOW: {low}",
        "",
        "### Changed Files",
        "",
    ]
    for f in files:
        risk = risk_levels.get(f, "LOW")
        lines.append(f"- `{f}` — **{risk}**")
    lines += ["", "### Review Findings", "", "```", output.strip(), "```", ""]
    if keywords:
        lines += ["### Keywords Detected", "", ", ".join(f"`{k}`" for k in keywords), ""]
    print("\n".join(lines))
