from __future__ import annotations
import fnmatch
from pathlib import Path
from typing import Any

from relay_core.constants import (
    FRONTEND_KEYWORDS, BACKEND_KEYWORDS,
    FRONTEND_EXTENSIONS, BACKEND_EXTENSIONS,
    FRONTEND_PATHS, BACKEND_PATHS,
    CLEAR_UI_WORDS, FRAMEWORK_MARKERS,
    MAX_HISTORY_DISPLAY,
)
from relay_core.types import RouteDecision, RepoState
from relay_core.utils import contains_phrase, extract_extensions, tokenize_text
from relay_core.memory import (
    load_repo_tasks, latest_task, latest_agent_task, recent_rate_limit,
    read_handoff, load_config, load_project_profile, save_project_profile,
)
from relay_core.utils import timestamp_now


def score_matches(text: str, values: list[str]) -> list[str]:
    return sorted(value for value in values if contains_phrase(text, value))


def path_hint_matches(text: str, values: list[str]) -> list[str]:
    lowered = text.lower()
    return sorted(value for value in values if value.lower() in lowered)


def scan_project(repo: RepoState) -> dict[str, Any]:
    if not repo.repo_root:
        return {}

    skip_dirs = {".git", "node_modules", "__pycache__", ".relay", ".next", "dist", "build", ".venv", "venv"}
    ext_counts: dict[str, int] = {}
    total = 0
    framework = "unknown"

    root_files = {f.name for f in repo.repo_root.iterdir() if f.is_file()}
    for marker, fw in FRAMEWORK_MARKERS.items():
        if marker in root_files:
            framework = fw
            break

    for path in repo.repo_root.rglob("*"):
        if any(skip in path.parts for skip in skip_dirs):
            continue
        if path.is_file() and path.suffix:
            ext = path.suffix.lower()
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            total += 1

    frontend_count = sum(ext_counts.get(ext, 0) for ext in FRONTEND_EXTENSIONS)
    backend_count = sum(ext_counts.get(ext, 0) for ext in BACKEND_EXTENSIONS)
    denominator = max(total, 1)

    primary = max(ext_counts, key=lambda e: ext_counts[e]) if ext_counts else "unknown"

    lang_map = {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript", ".go": "go",
        ".rs": "rust", ".java": "java", ".rb": "ruby", ".php": "php",
        ".css": "css", ".scss": "scss", ".html": "html", ".sql": "sql",
    }

    profile: dict[str, Any] = {
        "scanned_at": timestamp_now(),
        "framework": framework,
        "primary_language": lang_map.get(primary, primary),
        "extension_counts": dict(sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)),
        "frontend_ratio": round(frontend_count / denominator, 3),
        "backend_ratio": round(backend_count / denominator, 3),
        "total_files": total,
    }

    save_project_profile(repo, profile)
    return profile


def route_task(
    task: str,
    repo: RepoState,
    forced_agent: str | None = None,
    extra_context: str = "",
) -> RouteDecision:
    tasks = load_repo_tasks(repo) if repo.in_git_repo and repo.tasks_path and repo.tasks_path.exists() else []
    latest = latest_task(tasks)
    handoff = read_handoff(repo) if repo.in_git_repo else ""
    config = load_config(repo) if repo.in_git_repo else {}
    profile = load_project_profile(repo) if repo.in_git_repo else None

    if forced_agent in {"claude", "codex"}:
        return RouteDecision(
            agent=forced_agent,
            reason=f"Manual override via @{forced_agent}.",
            claude_score=0, codex_score=0,
            matched_claude_keywords=[], matched_codex_keywords=[],
            matched_claude_hints=[], matched_codex_hints=[],
            manual_override=forced_agent,
            rate_limit_penalty={"claude": 0, "codex": 0},
            handoff_influence=None,
        )

    # Check config agent_rules for path-based overrides
    agent_rules: dict[str, str] = config.get("agent_rules", {})
    for pattern, agent in agent_rules.items():
        if any(fnmatch.fnmatch(task, pattern) or pattern.lower() in task.lower() for _ in [1]):
            if agent in {"claude", "codex"}:
                return RouteDecision(
                    agent=agent,
                    reason=f"Config rule '{pattern}' matched → forced to {agent}.",
                    claude_score=0, codex_score=0,
                    matched_claude_keywords=[], matched_codex_keywords=[],
                    matched_claude_hints=[], matched_codex_hints=[],
                    manual_override=agent,
                    rate_limit_penalty={"claude": 0, "codex": 0},
                    handoff_influence=None,
                )

    # Merge custom keywords from config
    frontend_kws = list(FRONTEND_KEYWORDS) + config.get("custom_claude_keywords", [])
    backend_kws = list(BACKEND_KEYWORDS) + config.get("custom_codex_keywords", [])

    routing_text = " ".join(part for part in [task, extra_context, handoff] if part).strip()
    extensions = extract_extensions(routing_text)

    matched_claude_keywords = score_matches(routing_text, frontend_kws)
    matched_codex_keywords = score_matches(routing_text, backend_kws)

    matched_claude_hints = sorted(
        [ext for ext in FRONTEND_EXTENSIONS if ext in extensions]
        + path_hint_matches(routing_text, FRONTEND_PATHS)
    )
    matched_codex_hints = sorted(
        [ext for ext in BACKEND_EXTENSIONS if ext in extensions]
        + path_hint_matches(routing_text, BACKEND_PATHS)
    )

    claude_score = len(matched_claude_keywords) * 2 + len(matched_claude_hints) * 3
    codex_score = len(matched_codex_keywords) * 2 + len(matched_codex_hints) * 3

    # Project fingerprint influence
    if profile:
        fr = profile.get("frontend_ratio", 0.5)
        br = profile.get("backend_ratio", 0.5)
        if fr > 0.7:
            claude_score += 3
        elif br > 0.7:
            codex_score += 3

    # Handoff influence from recent changed files
    handoff_influence = None
    latest_changed_files = latest.get("changed_files", []) if latest else []
    if latest_changed_files:
        changed_blob = " ".join(latest_changed_files)
        claude_changed = path_hint_matches(changed_blob, FRONTEND_PATHS) + [ext for ext in FRONTEND_EXTENSIONS if ext in changed_blob]
        codex_changed = path_hint_matches(changed_blob, BACKEND_PATHS) + [ext for ext in BACKEND_EXTENSIONS if ext in changed_blob]
        if len(claude_changed) > len(codex_changed) and claude_changed:
            claude_score += 2
            handoff_influence = "Recent changed files leaned frontend/UI."
        elif len(codex_changed) > len(claude_changed) and codex_changed:
            codex_score += 2
            handoff_influence = "Recent changed files leaned backend/logic."

    # Rate limit penalties
    penalties = {"claude": 0, "codex": 0}
    if recent_rate_limit(tasks, "claude"):
        penalties["claude"] = 2
        claude_score -= 2
    if recent_rate_limit(tasks, "codex"):
        penalties["codex"] = 2
        codex_score -= 2

    task_tokens = tokenize_text(task)
    clear_ui_detected = any(word in task_tokens or contains_phrase(task, word) for word in CLEAR_UI_WORDS)

    if claude_score > codex_score:
        agent = "claude"
        reason = f"Frontend/UI signals scored higher ({claude_score} vs {codex_score})."
    elif codex_score > claude_score:
        agent = "codex"
        reason = f"Backend/logic signals scored higher ({codex_score} vs {claude_score})."
    else:
        agent = _neutral_tiebreak(task, config, tasks, clear_ui_detected)
        reason = (
            f"Scores tied at {claude_score}; neutral balanced policy selected "
            f"{agent.capitalize()}."
        )

    return RouteDecision(
        agent=agent,
        reason=reason,
        claude_score=claude_score,
        codex_score=codex_score,
        matched_claude_keywords=matched_claude_keywords,
        matched_codex_keywords=matched_codex_keywords,
        matched_claude_hints=matched_claude_hints,
        matched_codex_hints=matched_codex_hints,
        manual_override=None,
        rate_limit_penalty=penalties,
        handoff_influence=handoff_influence,
    )


def _neutral_tiebreak(task: str, config: dict[str, Any], tasks: list[dict[str, Any]], clear_ui_detected: bool) -> str:
    default_agent = config.get("default_agent")
    if default_agent in {"claude", "codex"}:
        return default_agent

    policy = config.get("agent_policy", "balanced")
    if policy == "ui_hint" and clear_ui_detected:
        return "claude"

    counts = {"claude": 0, "codex": 0}
    for entry in tasks[-20:]:
        agent = entry.get("selected_agent")
        if agent in counts:
            counts[agent] += 1
    if counts["claude"] < counts["codex"]:
        return "claude"
    if counts["codex"] < counts["claude"]:
        return "codex"

    latest = latest_agent_task(tasks)
    last_agent = latest.get("selected_agent") if latest else None
    if last_agent == "claude":
        return "codex"
    if last_agent == "codex":
        return "claude"
    return "claude" if sum(ord(ch) for ch in task) % 2 == 0 else "codex"
