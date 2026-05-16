from __future__ import annotations
import json
import stat
import sys
from pathlib import Path
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.utils import run_command, timestamp_now
from relay_core.diff import classify_file_risk


HOOK_SCRIPT = """\
#!/bin/sh
# Relay post-commit hook — logs every commit silently for memory and dashboard
python3 "{relay_py}" _hook-post-commit 2>/dev/null || true
"""


def install_hooks(repo: RepoState, relay_py: Path) -> None:
    """Install git hooks into the repo so Relay observes every commit."""
    if not repo.repo_root:
        raise RelayError("Not inside a git repository.")

    hooks_dir = repo.repo_root / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    hook_path = hooks_dir / "post-commit"
    script = HOOK_SCRIPT.format(relay_py=str(relay_py.resolve()))

    # If hook already exists and isn't ours, append rather than overwrite
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
        if "_hook-post-commit" in existing:
            return  # already installed
        # Append to existing hook
        updated = existing.rstrip() + "\n\n# Relay observer\n" + script
        hook_path.write_text(updated, encoding="utf-8")
    else:
        hook_path.write_text(script, encoding="utf-8")

    # Make executable
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def hooks_installed(repo: RepoState) -> bool:
    if not repo.repo_root:
        return False
    hook_path = repo.repo_root / ".git" / "hooks" / "post-commit"
    return hook_path.exists() and "_hook-post-commit" in hook_path.read_text(encoding="utf-8")


def run_post_commit_hook(repo: RepoState) -> None:
    """Called by the git post-commit hook after every commit.
    Silently logs the commit to .relay/ for memory and dashboard.
    """
    if not repo.in_git_repo or not repo.relay_dir:
        return

    try:
        from relay_core.memory import ensure_relay_files, load_memory, save_memory, load_repo_tasks, save_repo_tasks
        ensure_relay_files(repo)

        # Read commit data from git
        files = _committed_files(repo)
        commit_msg = _git_output(repo, "log", "-1", "--pretty=%s").strip()
        commit_hash = _git_output(repo, "rev-parse", "--short", "HEAD").strip()
        diff_text = _git_output(repo, "show", "--binary", "HEAD")
        diff_stat = _git_output(repo, "show", "--stat", "HEAD").strip()

        # Risk classification
        risk_levels = {f: classify_file_risk(f) for f in files}
        high_risk = [f for f, r in risk_levels.items() if r == "HIGH"]

        # Check if this commit was triggered by a relay task
        pending = _read_pending_task_full(repo)
        pending_task = pending.get("task", "") if pending else ""
        pending_agent = pending.get("agent", "unknown") if pending else "manual"

        # ── Intelligence: extract symbols and classify workstream ──
        from relay_core.intelligence import (
            extract_symbols_from_diff, classify_workstream,
            merge_symbols, update_workstream,
        )

        new_symbols = extract_symbols_from_diff(diff_text)
        symbol_names = list(new_symbols.keys())
        ws_name = classify_workstream(pending_task or commit_msg, files, symbol_names)

        # Persist symbols
        merge_symbols(repo, new_symbols)

        # Persist workstream
        if pending_task or commit_msg:
            update_workstream(
                repo,
                ws_name=ws_name,
                task=pending_task or commit_msg,
                agent=pending_agent,
                files=files,
                new_symbols=symbol_names,
                commit_msg=commit_msg,
            )

        entry: dict[str, Any] = {
            "command_type": "commit",
            "timestamp": timestamp_now(),
            "commit_hash": commit_hash,
            "commit_message": commit_msg,
            "changed_files": files,
            "risk_levels": risk_levels,
            "high_risk_files": high_risk,
            "diff_stat": diff_stat,
            "symbols_added": symbol_names[:20],
            "workstream": ws_name,
            "success": True,
            "source": "relay-task" if pending_task else "manual",
            "original_task": pending_task,
        }

        # Save diff
        if repo.diff_path:
            repo.diff_path.write_text(diff_text, encoding="utf-8")

        # Append to task history
        tasks = load_repo_tasks(repo)
        tasks.append(entry)
        save_repo_tasks(repo, tasks)

        # Update memory
        mem = load_memory(repo)
        mem["total_tasks"] = mem.get("total_tasks", 0) + 1
        hot = mem.get("hot_files", {})
        for f in files:
            hot[f] = hot.get(f, 0) + 1
        mem["hot_files"] = dict(sorted(hot.items(), key=lambda x: x[1], reverse=True)[:50])
        if high_risk:
            mem["last_risk_flags"] = high_risk[:5]
        save_memory(repo, mem)

        # Clear pending task
        _clear_pending_task(repo)

    except Exception:
        pass  # hooks must never fail loudly


def save_pending_task(repo: RepoState, task: str, agent: str) -> None:
    """Save the task that's about to run so the hook can attribute it."""
    if not repo.relay_dir:
        return
    repo.relay_dir.mkdir(parents=True, exist_ok=True)
    pending = {"task": task, "agent": agent, "timestamp": timestamp_now()}
    (repo.relay_dir / "pending-task.json").write_text(
        json.dumps(pending, indent=2), encoding="utf-8"
    )


def _read_pending_task(repo: RepoState) -> str:
    d = _read_pending_task_full(repo)
    return d.get("task", "") if d else ""


def _read_pending_task_full(repo: RepoState) -> dict | None:
    pending_path = repo.relay_dir / "pending-task.json" if repo.relay_dir else None
    if not pending_path or not pending_path.exists():
        return None
    try:
        return json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_pending_task(repo: RepoState) -> None:
    pending_path = repo.relay_dir / "pending-task.json" if repo.relay_dir else None
    if pending_path and pending_path.exists():
        pending_path.unlink()


def _git_output(repo: RepoState, *args: str) -> str:
    if not repo.repo_root:
        return ""
    result = run_command(["git", *args], cwd=repo.repo_root)
    return result.stdout if result.returncode == 0 else ""


def _committed_files(repo: RepoState) -> list[str]:
    output = _git_output(repo, "show", "--name-only", "--pretty=format:", "HEAD")
    return [line.strip() for line in output.splitlines() if line.strip()]
