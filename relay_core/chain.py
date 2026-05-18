from __future__ import annotations
from typing import Any

from relay_core.constants import COMPLEX_TASK_SIGNALS, COMPLEX_TASK_VERBS
from relay_core.types import RepoState
import relay_core.tui as tui


def decompose_task(task: str) -> list[str] | None:
    words = task.split()
    if len(words) < 8:
        return None

    task_lower = task.lower()
    has_signal = any(signal.strip() in task_lower for signal in COMPLEX_TASK_SIGNALS)
    has_verb = any(verb in task_lower for verb in COMPLEX_TASK_VERBS)

    if not (has_signal and has_verb):
        return None

    # Split on the strongest connector present
    for connector in [" and then ", " then ", " and ", " also ", " plus ", " as well as ", " including "]:
        if connector in task_lower:
            parts = task_lower.split(connector)
            steps = [p.strip().capitalize() for p in parts if p.strip()]
            if len(steps) >= 2:
                return steps

    return None


def run_chain(task: str, repo: RepoState) -> int:
    from relay_core.git import capture_agent_output, run_agent
    from relay_core.cleaner import save_output

    steps = [
        ("Claude", "Design — plan implementation and output a concise spec"),
        ("Codex", "Build — implement the spec"),
        ("Claude", "Review — review the diff for bugs and risks"),
    ]
    tui.show_chain_pipeline(steps)
    tui.console.print()

    # Step 1: Design with Claude
    tui.show_chain_step(1, 3, "Claude", "Designing implementation plan")
    design_prompt = (
        f"Design a concise implementation plan for: {task}\n"
        "Output numbered steps and key technical decisions only. Be brief (under 200 words)."
    )
    exit_code, spec_output = capture_agent_output("claude", design_prompt, repo.repo_root or repo.cwd)
    tui.show_review_output("claude", spec_output, exit_code)
    if exit_code != 0:
        tui.show_error("Design step failed. Aborting chain.")
        return exit_code

    # Step 2: Build with Codex
    tui.show_chain_step(2, 3, "Codex", "Implementing from spec")
    build_prompt = (
        f"Implement the following spec:\n\n{spec_output[:3000]}\n\nOriginal task: {task}"
    )
    result, build_output, _ = run_agent("codex", build_prompt, repo.repo_root or repo.cwd, relay_dir=repo.relay_dir)
    clean_build = save_output(repo.relay_dir or (repo.cwd / ".relay"), "codex", build_output)
    tui.show_review_output("codex", clean_build, result)

    # Step 3: Review with Claude
    tui.show_chain_step(3, 3, "Claude", "Reviewing the diff")
    from relay_core.git import current_diff, changed_files as cfiles
    from relay_core.constants import MAX_DIFF_PROMPT_CHARS
    diff_text = current_diff(repo)
    if diff_text.strip():
        review_prompt = (
            "Review this git diff for bugs, broken logic, missing tests, security risks, "
            "and unnecessary edits. Return concise findings.\n\n"
            f"Diff:\n{diff_text[:MAX_DIFF_PROMPT_CHARS]}"
        )
        review_exit, review_output = capture_agent_output("claude", review_prompt, repo.repo_root or repo.cwd)
        tui.show_review_output("claude", review_output, review_exit)
    else:
        tui.show_info("No diff to review after build step.")

    tui.console.print(
        "\n[bold green]Chain complete.[/bold green] [dim]Run 'relay summary' or 'relay commit' next.[/dim]\n"
    )
    return result
