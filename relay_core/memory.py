from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

from relay_core.constants import (
    MAX_HANDOFF_WORDS, MAX_DECISIONS_LOG, MAX_HISTORY_DISPLAY,
    FRONTEND_PATHS, BACKEND_PATHS, FRONTEND_KEYWORDS, BACKEND_KEYWORDS,
)
from relay_core.types import RepoState, RelayError
from relay_core.utils import trim_words, timestamp_now


def default_config() -> dict[str, Any]:
    return {
        "version": 2,
        "project_type": "auto",
        "default_agent": "auto",
        "agent_rules": {},
        "custom_claude_keywords": [],
        "custom_codex_keywords": [],
        "require_review_before_commit": False,
        "automation_mode": "edit",
        "auto_commit_on_success": True,
        "agent_policy": "balanced",
        "verify_commands": [],
        "max_auto_steps": 6,
        "stop_on": ["secret_detected", "migration_changed", "auth_changed"],
        "max_handoff_words": MAX_HANDOFF_WORDS,
        "shortcuts": {},
        "dashboard_port": 7432,
        "review_agent_preference": "opposite",
        "frontend_paths": FRONTEND_PATHS,
        "backend_paths": BACKEND_PATHS,
        "frontend_keywords": FRONTEND_KEYWORDS,
        "backend_keywords": BACKEND_KEYWORDS,
    }


def default_memory() -> dict[str, Any]:
    return {
        "version": 1,
        "session_count": 0,
        "total_tasks": 0,
        "agent_stats": {"claude": 0, "codex": 0},
        "hot_files": {},
        "last_risk_flags": [],
        "review_count": 0,
    }


def ensure_relay_files(repo: RepoState) -> None:
    if not repo.relay_dir or not repo.tasks_path or not repo.handoff_path \
            or not repo.decisions_path or not repo.diff_path or not repo.config_path \
            or not repo.project_path or not repo.memory_path:
        raise RelayError("Relay repo state is unavailable outside a git repository.")

    repo.relay_dir.mkdir(parents=True, exist_ok=True)
    from relay_core.git import ensure_local_git_exclude
    ensure_local_git_exclude(repo)

    if not repo.tasks_path.exists():
        repo.tasks_path.write_text("[]\n", encoding="utf-8")
    if not repo.handoff_path.exists():
        repo.handoff_path.write_text("", encoding="utf-8")
    if not repo.decisions_path.exists():
        repo.decisions_path.write_text("", encoding="utf-8")
    if not repo.diff_path.exists():
        repo.diff_path.write_text("", encoding="utf-8")
    if not repo.config_path.exists():
        repo.config_path.write_text(json.dumps(default_config(), indent=2) + "\n", encoding="utf-8")
    if not repo.memory_path.exists():
        repo.memory_path.write_text(json.dumps(default_memory(), indent=2) + "\n", encoding="utf-8")
    if repo.symbols_path and not repo.symbols_path.exists():
        repo.symbols_path.write_text("{}\n", encoding="utf-8")
    if repo.workstreams_path and not repo.workstreams_path.exists():
        repo.workstreams_path.write_text("{}\n", encoding="utf-8")
    if repo.brain_path and not repo.brain_path.exists():
        repo.brain_path.write_text(json.dumps(default_brain(), indent=2) + "\n", encoding="utf-8")
    if repo.runs_dir:
        repo.runs_dir.mkdir(parents=True, exist_ok=True)


def default_brain() -> dict[str, Any]:
    return {
        "version": 1,
        "active_run_id": "",
        "runs": [],
        "agent_policy": "balanced",
        "last_agents": [],
    }


def load_config(repo: RepoState) -> dict[str, Any]:
    if not repo.config_path or not repo.config_path.exists():
        return default_config()
    try:
        data = json.loads(repo.config_path.read_text(encoding="utf-8"))
        base = default_config()
        base.update(data)
        return base
    except json.JSONDecodeError:
        return default_config()


def load_repo_tasks(repo: RepoState) -> list[dict[str, Any]]:
    if not repo.tasks_path or not repo.tasks_path.exists():
        return []
    try:
        data = json.loads(repo.tasks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RelayError(f"Unable to read {repo.tasks_path}: {exc}") from exc
    return data if isinstance(data, list) else []


def save_repo_tasks(repo: RepoState, tasks: list[dict[str, Any]]) -> None:
    if not repo.tasks_path:
        raise RelayError("Relay tasks path is unavailable.")
    repo.tasks_path.write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")


def append_repo_task(repo: RepoState, task_entry: dict[str, Any]) -> None:
    tasks = load_repo_tasks(repo)
    tasks.append(task_entry)
    save_repo_tasks(repo, tasks)


def read_handoff(repo: RepoState) -> str:
    if not repo.handoff_path or not repo.handoff_path.exists():
        return ""
    return repo.handoff_path.read_text(encoding="utf-8").strip()


def write_handoff(repo: RepoState, text: str) -> None:
    if not repo.handoff_path:
        raise RelayError("Relay handoff path is unavailable.")
    repo.handoff_path.write_text(trim_words(text, MAX_HANDOFF_WORDS).strip() + "\n", encoding="utf-8")


def append_decision(repo: RepoState, text: str) -> None:
    if not repo.decisions_path:
        return
    lines = [line for line in repo.decisions_path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if repo.decisions_path.exists() else []
    lines.append(text.strip())
    repo.decisions_path.write_text("\n\n".join(lines[-MAX_DECISIONS_LOG:]) + ("\n" if lines else ""), encoding="utf-8")


def save_last_diff(repo: RepoState, diff_text: str) -> None:
    if not repo.diff_path:
        return
    repo.diff_path.write_text(diff_text, encoding="utf-8")


def latest_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    return tasks[-1] if tasks else None


def latest_agent_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in reversed(tasks):
        if entry.get("command_type") in {None, "task"}:
            return entry
    return None


def recent_rate_limit(tasks: list[dict[str, Any]], agent: str) -> bool:
    for entry in reversed(tasks[-MAX_HISTORY_DISPLAY:]):
        if entry.get("selected_agent") == agent and entry.get("rate_limit_detected"):
            return True
    return False


def append_history_entry(repo: RepoState, entry: dict[str, Any]) -> None:
    entry.setdefault("timestamp", timestamp_now())
    append_repo_task(repo, entry)


def load_memory(repo: RepoState) -> dict[str, Any]:
    if not repo.memory_path or not repo.memory_path.exists():
        return default_memory()
    try:
        data = json.loads(repo.memory_path.read_text(encoding="utf-8"))
        base = default_memory()
        base.update(data)
        return base
    except json.JSONDecodeError:
        return default_memory()


def save_memory(repo: RepoState, data: dict[str, Any]) -> None:
    if not repo.memory_path:
        return
    repo.memory_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def update_memory_after_task(repo: RepoState, agent: str, files: list[str], success: bool) -> None:
    mem = load_memory(repo)
    mem["total_tasks"] = mem.get("total_tasks", 0) + 1
    stats = mem.get("agent_stats", {"claude": 0, "codex": 0})
    stats[agent] = stats.get(agent, 0) + 1
    mem["agent_stats"] = stats
    hot = mem.get("hot_files", {})
    for f in files:
        hot[f] = hot.get(f, 0) + 1
    mem["hot_files"] = dict(sorted(hot.items(), key=lambda x: x[1], reverse=True)[:50])
    save_memory(repo, mem)


def load_project_profile(repo: RepoState) -> dict[str, Any] | None:
    if not repo.project_path or not repo.project_path.exists():
        return None
    try:
        return json.loads(repo.project_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_project_profile(repo: RepoState, profile: dict[str, Any]) -> None:
    if not repo.project_path:
        return
    repo.project_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
