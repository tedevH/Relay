"""
Relay Intelligence Layer
========================
Converts raw git activity into compressed, structured, task-relevant state.

Four memory types:
  1. Repo memory     — long-term structural understanding (project.json)
  2. Workstream memory — active feature threads (workstreams.json)
  3. Symbol memory   — important identifiers and their locations (symbols.json)
  4. Task memory     — individual agent runs (tasks.json)
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any

from relay_core.types import RepoState
from relay_core.utils import timestamp_now


# ── Workstream patterns ────────────────────────────────────────────────────

WORKSTREAM_PATTERNS: dict[str, list[str]] = {
    "terminal":          ["terminal", "term-", "stream", "cli"],
    "auth":              ["auth", "login", "logout", "session", "token", "signin"],
    "memory_system":     ["memory", "context", "handoff", "tasks.json", "workstream", "symbol"],
    "routing":           ["routing", "route_task", "route_decision", "codex", "claude", "agent"],
    "git_ops":           ["commit", "push", "diff", "hook", "post-commit"],
    "ci":                ["audit", "ci", "github", "action", "relay-audit"],
    "tui":               ["tui", "rich", "panel", "console", "spinner"],
}


def classify_workstream(task: str, files: list[str], symbols: list[str]) -> str:
    text = " ".join([task] + files + symbols).lower()
    scores: dict[str, int] = {}
    for ws, keywords in WORKSTREAM_PATTERNS.items():
        scores[ws] = sum(1 for kw in keywords if kw in text)
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "general"


# ── Symbol extraction ──────────────────────────────────────────────────────

def extract_symbols_from_diff(diff_text: str) -> dict[str, dict[str, Any]]:
    """Parse a git diff and extract added/modified symbols with their locations."""
    symbols: dict[str, dict[str, Any]] = {}
    current_file = ""
    line_num = 0

    for line in diff_text.splitlines():
        # Track current file
        if line.startswith("diff --git"):
            m = re.search(r" b/(.+)$", line)
            current_file = m.group(1).strip() if m else ""
            line_num = 0
            continue

        # Parse hunk header for line numbers
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_num = int(m.group(1)) - 1 if m else line_num
            continue

        # Only look at added lines
        if not line.startswith("+") or line.startswith("+++"):
            if not line.startswith("-"):
                line_num += 1
            continue

        line_num += 1
        content = line[1:]

        # Python: def
        m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", content)
        if m:
            symbols[m.group(1)] = {"file": current_file, "line": line_num, "type": "function"}
            continue

        # Python: class
        m = re.match(r"\s*class\s+(\w+)", content)
        if m:
            symbols[m.group(1)] = {"file": current_file, "line": line_num, "type": "class"}
            continue

        # Python/JS: UPPER_CASE constant
        m = re.match(r"\s*([A-Z][A-Z0-9_]{2,})\s*=\s*", content)
        if m:
            symbols[m.group(1)] = {"file": current_file, "line": line_num, "type": "constant"}
            continue

        # Flask/Express route decorator
        m = re.match(r'\s*@\w+\.route\(\s*["\']([^"\']+)', content)
        if m:
            symbols[m.group(1)] = {"file": current_file, "line": line_num, "type": "route"}
            continue

        # JS/TS: function declarations and arrow functions
        m = re.match(r"\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", content)
        if m:
            symbols[m.group(1)] = {"file": current_file, "line": line_num, "type": "function"}
            continue

        m = re.match(r"\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", content)
        if m:
            symbols[m.group(1)] = {"file": current_file, "line": line_num, "type": "function"}
            continue

    return symbols


# ── Workstream memory ──────────────────────────────────────────────────────

def load_workstreams(repo: RepoState) -> dict[str, Any]:
    if not repo.workstreams_path or not repo.workstreams_path.exists():
        return {}
    try:
        return json.loads(repo.workstreams_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_workstreams(repo: RepoState, data: dict[str, Any]) -> None:
    if not repo.workstreams_path:
        return
    repo.workstreams_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def update_workstream(
    repo: RepoState,
    ws_name: str,
    task: str,
    agent: str,
    files: list[str],
    new_symbols: list[str],
    commit_msg: str = "",
) -> None:
    workstreams = load_workstreams(repo)
    ws = workstreams.get(ws_name, {
        "name": ws_name,
        "goal": task,
        "status": "in_progress",
        "files": [],
        "symbols": [],
        "agents": [],
        "tasks": [],
        "last_updated": "",
    })

    # Merge files and symbols
    existing_files = set(ws.get("files", []))
    existing_files.update(files)
    ws["files"] = sorted(existing_files)[:10]

    existing_symbols = set(ws.get("symbols", []))
    existing_symbols.update(new_symbols)
    ws["symbols"] = sorted(existing_symbols)[:20]

    # Track agents used
    agents = ws.get("agents", [])
    if agent not in agents:
        agents.append(agent)
    ws["agents"] = agents

    # Track tasks
    task_entries = ws.get("tasks", [])
    task_entries.append({
        "task": task[:100],
        "agent": agent,
        "commit": commit_msg[:80],
        "timestamp": timestamp_now()[:10],
    })
    ws["tasks"] = task_entries[-10:]  # keep last 10

    ws["last_updated"] = timestamp_now()[:10]
    ws["status"] = "in_progress"

    workstreams[ws_name] = ws
    save_workstreams(repo, workstreams)


# ── Symbol memory ──────────────────────────────────────────────────────────

def load_symbols(repo: RepoState) -> dict[str, Any]:
    if not repo.symbols_path or not repo.symbols_path.exists():
        return {}
    try:
        return json.loads(repo.symbols_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_symbols(repo: RepoState, data: dict[str, Any]) -> None:
    if not repo.symbols_path:
        return
    repo.symbols_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def merge_symbols(repo: RepoState, new_symbols: dict[str, Any]) -> None:
    existing = load_symbols(repo)
    existing.update(new_symbols)
    # Keep to a reasonable size
    if len(existing) > 500:
        # Keep most recently added
        existing = dict(list(existing.items())[-500:])
    save_symbols(repo, existing)


# ── Smart context generation ───────────────────────────────────────────────

def generate_context(repo: RepoState, task: str) -> str:
    """Generate minimal, structured, task-relevant context for the next agent.

    Output is compressed and implementation-focused.
    Never dumps raw history. Always prefers current repo truth.
    """
    from relay_core.memory import load_project_profile

    task_lower = task.lower()
    workstreams = load_workstreams(repo)
    symbols = load_symbols(repo)
    profile = load_project_profile(repo)

    lines: list[str] = []

    # ── Find relevant workstreams ──
    relevant_workstreams = []
    for ws_name, ws_data in workstreams.items():
        score = 0
        # Score by task keyword overlap with workstream name + symbols
        for part in ws_name.split("_"):
            if part in task_lower:
                score += 3
        for sym in ws_data.get("symbols", []):
            if sym.lower() in task_lower:
                score += 2
        for f in ws_data.get("files", []):
            if any(part in task_lower for part in Path(f).stem.split("_")):
                score += 1
        if score > 0:
            relevant_workstreams.append((score, ws_data))

    relevant_workstreams.sort(key=lambda x: x[0], reverse=True)

    # ── Relevant symbols ──
    relevant_symbols: dict[str, Any] = {}
    if relevant_workstreams:
        top_ws = relevant_workstreams[0][1]
        for sym in top_ws.get("symbols", []):
            if sym in symbols:
                relevant_symbols[sym] = symbols[sym]

    # Also check symbols directly matching task words
    for word in task_lower.split():
        if len(word) > 3:
            for sym, loc in symbols.items():
                if word in sym.lower() and sym not in relevant_symbols:
                    relevant_symbols[sym] = loc

    if relevant_symbols:
        lines.append("## Relevant repo state")
        for sym, loc in list(relevant_symbols.items())[:8]:
            file_ref = f"{loc['file']}:{loc.get('line', '?')}"
            sym_type = loc.get("type", "symbol")
            lines.append(f"- `{sym}` — {file_ref} [{sym_type}]")
        lines.append("")

    # ── Active workstreams ──
    if relevant_workstreams:
        lines.append("## Active workstreams")
        for _, ws in relevant_workstreams[:2]:
            lines.append(f"**{ws['name']}** ({ws.get('status', 'in_progress')})")
            lines.append(f"Goal: {ws.get('goal', '')[:80]}")
            if ws.get("tasks"):
                last = ws["tasks"][-1]
                lines.append(f"Last: [{last['agent']}] {last['task'][:60]}")
            lines.append("")

    # ── Likely files ──
    likely_files: list[str] = []
    if relevant_workstreams:
        for _, ws in relevant_workstreams[:1]:
            likely_files = ws.get("files", [])[:5]

    if likely_files:
        lines.append("## Likely files")
        for f in likely_files:
            lines.append(f"- `{f}`")
        lines.append("")

    # ── Repo structure (minimal) ──
    if profile:
        test_commands = profile.get("test_commands", [])
        lint_commands = profile.get("lint_commands", [])
        build_commands = profile.get("build_commands", [])
        lines.append("## Repo")
        lines.append(f"Framework: {profile.get('framework', 'unknown')} · {profile.get('primary_language', 'unknown')}")
        if test_commands:
            lines.append(f"Tests: {', '.join(test_commands[:2])}")
        if lint_commands:
            lines.append(f"Lint: {', '.join(lint_commands[:2])}")
        if build_commands:
            lines.append(f"Build: {', '.join(build_commands[:2])}")
        known_failures = profile.get("known_failures", [])
        if known_failures:
            lines.append(f"Last failure: {known_failures[-1].get('summary', '')[:160]}")

    return "\n".join(lines) if lines else "No relevant prior state found."
