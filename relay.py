#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_HISTORY_DISPLAY = 20

CLAUDE_KEYWORDS = {
    "frontend",
    "ui",
    "ux",
    "react",
    "next.js",
    "nextjs",
    "tailwind",
    "css",
    "landing",
    "dashboard",
    "dashboards",
    "copy",
    "layout",
    "animation",
    "responsive",
    "component",
    "components",
    "page",
    "pages",
    "hero",
}

CODEX_KEYWORDS = {
    "backend",
    "api",
    "route",
    "routes",
    "database",
    "databases",
    "sql",
    "postgres",
    "postgresql",
    "auth",
    "authentication",
    "migration",
    "migrations",
    "test",
    "tests",
    "script",
    "scripts",
    "bug",
    "bugs",
    "performance",
    "security",
    "cron",
    "worker",
    "workers",
    "server",
    "endpoint",
}

CLAUDE_EXTENSIONS = {".tsx", ".jsx", ".css", ".scss", ".html"}
CODEX_EXTENSIONS = {".py", ".go", ".rs", ".sql", ".java", ".rb", ".php"}

RATE_LIMIT_PATTERNS = (
    "rate limit",
    "usage limit",
    "quota exceeded",
    "too many requests",
    "try again later",
    "capacity",
    "limit reached",
)


@dataclass
class RouteDecision:
    agent: str
    reason: str
    claude_score: int
    codex_score: int
    matched_claude: list[str]
    matched_codex: list[str]


class RelayStorageError(RuntimeError):
    pass


def app_dir() -> Path:
    override = os.environ.get("RELAY_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".relay"


def history_path() -> Path:
    return app_dir() / "history.json"


def ensure_app_dir() -> None:
    try:
        app_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RelayStorageError(f"Unable to create Relay data directory at {app_dir()}: {exc}") from exc


def load_history() -> list[dict[str, Any]]:
    ensure_app_dir()
    path = history_path()
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        raise RelayStorageError(f"Unable to read Relay history at {path}: {exc}") from exc
    return []


def save_history(entries: list[dict[str, Any]]) -> None:
    ensure_app_dir()
    path = history_path()
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2)
    except OSError as exc:
        raise RelayStorageError(f"Unable to write Relay history at {path}: {exc}") from exc


def append_history(entry: dict[str, Any]) -> None:
    history = load_history()
    history.append(entry)
    save_history(history)


def safe_load_history() -> tuple[list[dict[str, Any]], str | None]:
    try:
        return load_history(), None
    except RelayStorageError as exc:
        return [], str(exc)


def safe_append_history(entry: dict[str, Any]) -> None:
    try:
        append_history(entry)
    except RelayStorageError as exc:
        print(f"Warning: {exc}", file=sys.stderr)


def timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_agent_name(agent: str) -> str:
    return "Claude" if agent == "claude" else "Codex"


def tokenize_task(task: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_.+-]+", task.lower()))


def extract_extensions(task: str) -> list[str]:
    return [match.lower() for match in re.findall(r"\.[a-zA-Z0-9]+", task)]


def route_task(task: str, forced_agent: str | None = None) -> RouteDecision:
    if forced_agent in {"claude", "codex"}:
        reason = f"Forced via @{forced_agent}"
        return RouteDecision(
            agent=forced_agent,
            reason=reason,
            claude_score=0,
            codex_score=0,
            matched_claude=[],
            matched_codex=[],
        )

    tokens = tokenize_task(task)
    extensions = extract_extensions(task)

    matched_claude = sorted(token for token in CLAUDE_KEYWORDS if token in tokens)
    matched_codex = sorted(token for token in CODEX_KEYWORDS if token in tokens)

    claude_ext_hits = sorted(ext for ext in CLAUDE_EXTENSIONS if ext in extensions)
    codex_ext_hits = sorted(ext for ext in CODEX_EXTENSIONS if ext in extensions)

    claude_score = len(matched_claude) * 2 + len(claude_ext_hits) * 3
    codex_score = len(matched_codex) * 2 + len(codex_ext_hits) * 3

    matched_claude.extend(f"extension:{ext}" for ext in claude_ext_hits)
    matched_codex.extend(f"extension:{ext}" for ext in codex_ext_hits)

    if claude_score > codex_score:
        reason = f"Frontend/UI signals scored higher ({claude_score} vs {codex_score})"
        agent = "claude"
    elif codex_score > claude_score:
        reason = f"Backend/implementation signals scored higher ({codex_score} vs {claude_score})"
        agent = "codex"
    else:
        reason = (
            "Scores were tied, so Relay defaulted to Codex for implementation-heavy reliability "
            f"({claude_score} vs {codex_score})"
        )
        agent = "codex"

    return RouteDecision(
        agent=agent,
        reason=reason,
        claude_score=claude_score,
        codex_score=codex_score,
        matched_claude=matched_claude,
        matched_codex=matched_codex,
    )


def detect_rate_limit(output: str) -> bool:
    lowered = output.lower()
    return any(pattern in lowered for pattern in RATE_LIMIT_PATTERNS)


def cli_available(agent: str) -> bool:
    command = "claude" if agent == "claude" else "codex"
    return shutil.which(command) is not None


def agent_command(agent: str, task: str) -> list[str]:
    if agent == "claude":
        return ["claude", "-p", task]
    return ["codex", "exec", task]


def run_agent(task: str, forced_agent: str | None = None) -> int:
    decision = route_task(task, forced_agent=forced_agent)
    agent = decision.agent
    label = normalize_agent_name(agent)

    print(f"Routing to: {label}")

    if not cli_available(agent):
        command = "claude" if agent == "claude" else "codex"
        print(
            f"Error: {label} CLI is not installed or not on your PATH. Expected command: {command}",
            file=sys.stderr,
        )
        safe_append_history(
            {
                "timestamp": timestamp_now(),
                "original_task": task,
                "selected_agent": agent,
                "succeeded": False,
                "exit_code": None,
                "rate_limited": False,
                "error": "cli_not_installed",
            }
        )
        return 1

    command = agent_command(agent, task)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    captured_output: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        captured_output.append(line)
        print(line, end="")

    process.wait()
    combined_output = "".join(captured_output)
    rate_limited = detect_rate_limit(combined_output)

    if rate_limited:
        print(
            f"\nRelay detected a likely {label} rate/usage limit from CLI output.",
            file=sys.stderr,
        )

    if process.returncode != 0:
        print(
            f"\nError: {label} exited with code {process.returncode}.",
            file=sys.stderr,
        )

    safe_append_history(
        {
            "timestamp": timestamp_now(),
            "original_task": task,
            "selected_agent": agent,
            "succeeded": process.returncode == 0,
            "exit_code": process.returncode,
            "rate_limited": rate_limited,
        }
    )

    return process.returncode


def print_history() -> int:
    history, warning = safe_load_history()
    if warning:
        print(f"Warning: {warning}", file=sys.stderr)
    if not history:
        print("No Relay history yet.")
        return 0

    recent = history[-MAX_HISTORY_DISPLAY:]
    for entry in reversed(recent):
        status = "ok" if entry.get("succeeded") else "failed"
        if entry.get("rate_limited"):
            status += " (rate-limited)"
        timestamp = entry.get("timestamp", "unknown-time")
        agent = normalize_agent_name(entry.get("selected_agent", "codex"))
        task = entry.get("original_task", "")
        exit_code = entry.get("exit_code")
        print(f"{timestamp} | {agent:<6} | {status:<20} | exit={exit_code} | {task}")
    return 0


def recent_rate_limit(history: list[dict[str, Any]], agent: str) -> bool:
    agent_entries = [entry for entry in reversed(history) if entry.get("selected_agent") == agent]
    for entry in agent_entries[:MAX_HISTORY_DISPLAY]:
        if entry.get("rate_limited"):
            return True
    return False


def print_status() -> int:
    history, warning = safe_load_history()
    claude_available = "available" if cli_available("claude") else "not installed"
    codex_available = "available" if cli_available("codex") else "not installed"

    print(f"Claude: {claude_available}")
    print(f"Codex: {codex_available}")
    print(
        "Recent Claude rate-limit detected: "
        + ("yes" if recent_rate_limit(history, "claude") else "no")
    )
    print(
        "Recent Codex rate-limit detected: "
        + ("yes" if recent_rate_limit(history, "codex") else "no")
    )
    if warning:
        print(f"History warning: {warning}")
    return 0


def print_why(task: str, forced_agent: str | None = None) -> int:
    decision = route_task(task, forced_agent=forced_agent)
    print(f"Would route to: {normalize_agent_name(decision.agent)}")
    print(f"Reason: {decision.reason}")
    if forced_agent is None:
        print(f"Claude score: {decision.claude_score}")
        print(f"Codex score: {decision.codex_score}")
        print(
            "Claude matches: "
            + (", ".join(decision.matched_claude) if decision.matched_claude else "none")
        )
        print(
            "Codex matches: "
            + (", ".join(decision.matched_codex) if decision.matched_codex else "none")
        )
    return 0


def print_usage() -> int:
    print("Usage:")
    print('  relay "<task>"')
    print('  relay @claude "<task>"')
    print('  relay @codex "<task>"')
    print("  relay history")
    print("  relay status")
    print('  relay why "<task>"')
    return 1


def parse_args(argv: list[str]) -> tuple[str, str | None]:
    if not argv:
        raise ValueError("missing arguments")

    command = argv[0]
    if command in {"history", "status"}:
        return command, None

    if command == "why":
        task = " ".join(argv[1:]).strip()
        if not task:
            raise ValueError("missing task for why")
        return "why", task

    forced_agent = None
    if command in {"@claude", "@codex"}:
        forced_agent = command[1:]
        task = " ".join(argv[1:]).strip()
    else:
        task = " ".join(argv).strip()

    if not task:
        raise ValueError("missing task")

    return task, forced_agent


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        parsed, extra = parse_args(args)
    except ValueError:
        return print_usage()

    if parsed == "history":
        return print_history()
    if parsed == "status":
        return print_status()
    if parsed == "why":
        return print_why(extra or "")

    return run_agent(parsed, forced_agent=extra)


if __name__ == "__main__":
    raise SystemExit(main())
