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


VERSION = "0.2.0"
MAX_HISTORY_DISPLAY = 20
MAX_HANDOFF_WORDS = 250
MAX_DIFF_PROMPT_CHARS = 6000
MAX_HANDOFF_PROMPT_CHARS = 1800
MAX_DECISIONS_LOG = 50

INSTALL_HINTS = {
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "npm i -g @openai/codex\ncodex",
    "git": "https://git-scm.com/install",
}

SENSITIVE_PATH_PATTERNS = (
    ".env",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "migrations",
    "auth",
    "stripe",
    "payment",
)

FRONTEND_KEYWORDS = [
    "frontend",
    "ui",
    "ux",
    "react",
    "next.js",
    "nextjs",
    "component",
    "components",
    "tailwind",
    "css",
    "html",
    "landing",
    "landing page",
    "dashboard",
    "copy",
    "animation",
    "responsive",
    "layout",
    "design",
    "hero",
    "navbar",
    "pricing",
    "form styling",
]

BACKEND_KEYWORDS = [
    "backend",
    "api",
    "route",
    "server",
    "database",
    "sql",
    "postgres",
    "supabase",
    "auth",
    "migration",
    "schema",
    "test",
    "tests",
    "bug",
    "error",
    "performance",
    "security",
    "script",
    "worker",
    "cron",
    "queue",
    "endpoint",
    "validation",
]

FRONTEND_EXTENSIONS = [".tsx", ".jsx", ".css", ".scss", ".html"]
BACKEND_EXTENSIONS = [".py", ".go", ".rs", ".sql", ".java", ".rb", ".php"]

FRONTEND_PATHS = ["components/", "app/page.tsx", "styles/", "public/"]
BACKEND_PATHS = ["app/api/", "api/", "server/", "lib/db/", "db/", "migrations/", "tests/", "schema.prisma"]

RATE_LIMIT_PATTERNS = (
    "rate limit",
    "usage limit",
    "quota",
    "quota exceeded",
    "too many requests",
    "try again later",
    "exceeded limit",
)

CLEAR_UI_WORDS = {"ui", "ux", "design", "responsive", "layout", "hero", "navbar", "pricing", "css"}


@dataclass
class RouteDecision:
    agent: str
    reason: str
    claude_score: int
    codex_score: int
    matched_claude_keywords: list[str]
    matched_codex_keywords: list[str]
    matched_claude_hints: list[str]
    matched_codex_hints: list[str]
    manual_override: str | None
    rate_limit_penalty: dict[str, int]
    handoff_influence: str | None


@dataclass
class RepoState:
    cwd: Path
    repo_root: Path | None

    @property
    def in_git_repo(self) -> bool:
        return self.repo_root is not None

    @property
    def relay_dir(self) -> Path | None:
        return self.repo_root / ".relay" if self.repo_root else None

    @property
    def tasks_path(self) -> Path | None:
        return self.relay_dir / "tasks.json" if self.relay_dir else None

    @property
    def handoff_path(self) -> Path | None:
        return self.relay_dir / "handoff.md" if self.relay_dir else None

    @property
    def decisions_path(self) -> Path | None:
        return self.relay_dir / "decisions.md" if self.relay_dir else None

    @property
    def diff_path(self) -> Path | None:
        return self.relay_dir / "last-diff.patch" if self.relay_dir else None

    @property
    def config_path(self) -> Path | None:
        return self.relay_dir / "config.json" if self.relay_dir else None


class RelayError(RuntimeError):
    pass


def normalize_agent_name(agent: str) -> str:
    return "Claude" if agent == "claude" else "Codex"


def timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True, text=True)


def get_repo_state() -> RepoState:
    cwd = Path.cwd()
    if shutil.which("git") is None:
        return RepoState(cwd=cwd, repo_root=None)

    result = run_command(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if result.returncode != 0:
        return RepoState(cwd=cwd, repo_root=None)
    return RepoState(cwd=cwd, repo_root=Path(result.stdout.strip()))


def default_config() -> dict[str, Any]:
    return {
        "frontend_paths": FRONTEND_PATHS,
        "backend_paths": BACKEND_PATHS,
        "frontend_keywords": FRONTEND_KEYWORDS,
        "backend_keywords": BACKEND_KEYWORDS,
        "review_agent_preference": "opposite",
        "max_handoff_words": MAX_HANDOFF_WORDS,
    }


def ensure_relay_files(repo: RepoState) -> None:
    if not repo.relay_dir or not repo.tasks_path or not repo.handoff_path or not repo.decisions_path or not repo.diff_path or not repo.config_path:
        raise RelayError("Relay repo state is unavailable outside a git repository.")

    repo.relay_dir.mkdir(parents=True, exist_ok=True)

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


def load_repo_tasks(repo: RepoState) -> list[dict[str, Any]]:
    if not repo.tasks_path or not repo.tasks_path.exists():
        return []
    try:
        data = json.loads(repo.tasks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RelayError(f"Unable to read {repo.tasks_path}: {exc}") from exc
    if isinstance(data, list):
        return data
    return []


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
    lines = [line for line in repo.decisions_path.read_text(encoding="utf-8").splitlines() if line.strip()] if repo.decisions_path.exists() else []
    lines.append(text.strip())
    repo.decisions_path.write_text("\n\n".join(lines[-MAX_DECISIONS_LOG:]) + ("\n" if lines else ""), encoding="utf-8")


def save_last_diff(repo: RepoState, diff_text: str) -> None:
    if not repo.diff_path:
        return
    repo.diff_path.write_text(diff_text, encoding="utf-8")


def trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def safe_text(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def cli_available(name: str) -> bool:
    return shutil.which(name) is not None


def all_required_dependencies() -> list[tuple[str, bool]]:
    return [(name, cli_available(name)) for name in ("claude", "codex", "git")]


def missing_required_dependencies() -> list[str]:
    return [name for name, ok in all_required_dependencies() if not ok]


def print_install_hints(missing: list[str]) -> None:
    for name in missing:
        print(f"{normalize_agent_name(name) if name in {'claude', 'codex'} else 'Git'} install hint:")
        print(INSTALL_HINTS[name])


def extract_extensions(text: str) -> list[str]:
    return [match.lower() for match in re.findall(r"\.[a-zA-Z0-9]+", text)]


def tokenize_text(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_./-]+", text.lower()))


def contains_phrase(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower()


def recent_rate_limit(tasks: list[dict[str, Any]], agent: str) -> bool:
    for entry in reversed(tasks[-MAX_HISTORY_DISPLAY:]):
        if entry.get("selected_agent") == agent and entry.get("rate_limit_detected"):
            return True
    return False


def latest_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    return tasks[-1] if tasks else None


def score_matches(text: str, values: list[str]) -> list[str]:
    return sorted(value for value in values if contains_phrase(text, value))


def path_hint_matches(text: str, values: list[str]) -> list[str]:
    lowered = text.lower()
    return sorted(value for value in values if value.lower() in lowered)


def route_task(
    task: str,
    repo: RepoState,
    forced_agent: str | None = None,
    extra_context: str = "",
) -> RouteDecision:
    tasks = load_repo_tasks(repo) if repo.in_git_repo and repo.tasks_path and repo.tasks_path.exists() else []
    latest = latest_task(tasks)
    handoff = read_handoff(repo) if repo.in_git_repo else ""

    if forced_agent in {"claude", "codex"}:
        return RouteDecision(
            agent=forced_agent,
            reason=f"Manual override detected via @{forced_agent}.",
            claude_score=0,
            codex_score=0,
            matched_claude_keywords=[],
            matched_codex_keywords=[],
            matched_claude_hints=[],
            matched_codex_hints=[],
            manual_override=forced_agent,
            rate_limit_penalty={"claude": 0, "codex": 0},
            handoff_influence=None,
        )

    routing_text = " ".join(part for part in [task, extra_context, handoff] if part).strip()
    extensions = extract_extensions(routing_text)

    matched_claude_keywords = score_matches(routing_text, FRONTEND_KEYWORDS)
    matched_codex_keywords = score_matches(routing_text, BACKEND_KEYWORDS)

    matched_claude_hints = sorted([ext for ext in FRONTEND_EXTENSIONS if ext in extensions] + path_hint_matches(routing_text, FRONTEND_PATHS))
    matched_codex_hints = sorted([ext for ext in BACKEND_EXTENSIONS if ext in extensions] + path_hint_matches(routing_text, BACKEND_PATHS))

    claude_score = len(matched_claude_keywords) * 2 + len(matched_claude_hints) * 3
    codex_score = len(matched_codex_keywords) * 2 + len(matched_codex_hints) * 3

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
        reason = f"Backend/implementation signals scored higher ({codex_score} vs {claude_score})."
    elif clear_ui_detected:
        agent = "claude"
        reason = f"Scores tied at {claude_score}, but clear UI/design language broke the tie for Claude."
    else:
        agent = "codex"
        reason = f"Scores tied at {claude_score}, so Relay defaulted to Codex for implementation-heavy ambiguity."

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


def detect_rate_limit(output: str) -> bool:
    lowered = output.lower()
    return any(pattern in lowered for pattern in RATE_LIMIT_PATTERNS)


def require_task_dependencies(repo: RepoState) -> None:
    missing = missing_required_dependencies()
    if missing:
        raise RelayError(
            "Relay cannot run AI tasks until all required dependencies are installed: "
            + ", ".join(missing)
        )
    if not repo.in_git_repo:
        raise RelayError("Relay AI tasks require running inside a git repository.")


def git_output(repo: RepoState, *args: str) -> str:
    if not repo.repo_root:
        return ""
    result = run_command(["git", *args], cwd=repo.repo_root)
    if result.returncode != 0:
        return ""
    return result.stdout


def current_diff(repo: RepoState) -> str:
    return git_output(repo, "diff", "--binary")


def changed_files(repo: RepoState) -> list[str]:
    output = git_output(repo, "diff", "--name-only")
    return [line.strip() for line in output.splitlines() if line.strip()]


def diff_summary(repo: RepoState) -> str:
    output = git_output(repo, "diff", "--stat")
    return output.strip()


def warning_paths(files: list[str]) -> list[str]:
    lowered = [path.lower() for path in files]
    warnings: list[str] = []
    for path in lowered:
        if any(pattern in path for pattern in SENSITIVE_PATH_PATTERNS):
            warnings.append(path)
    return sorted(set(warnings))


def build_agent_command(agent: str, prompt: str) -> list[str]:
    if agent == "claude":
        return ["claude", "--permission-mode", "acceptEdits", "-p", prompt]
    return [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "workspace-write",
        prompt,
    ]


def stream_subprocess(command: list[str], cwd: Path) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        captured.append(line)
        print(line, end="")
    process.wait()
    return process.returncode, "".join(captured)


def compact_diff_for_prompt(repo: RepoState) -> str:
    diff_text = current_diff(repo)
    if not diff_text.strip():
        return "No current git diff."
    return diff_text[:MAX_DIFF_PROMPT_CHARS]


def summarize_output(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "No notable agent output captured."
    return trim_words(" ".join(lines[-8:]), 80)


def create_handoff(
    agent: str,
    task: str,
    files: list[str],
    output: str,
    diff_stat: str,
    warnings: list[str],
) -> str:
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
    decision: RouteDecision,
    exit_code: int,
    success: bool,
    rate_limit_detected: bool,
    changed_files_list: list[str],
    handoff_summary: str,
) -> dict[str, Any]:
    return {
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


def print_relay_home(repo: RepoState) -> None:
    print("Relay")
    print(f"Version: {VERSION}")
    print(f"Working directory: {repo.cwd}")
    print(f"Git repo: {repo.repo_root if repo.repo_root else 'not detected'}")
    print()
    print("Status")
    print(f"Claude Code: {'installed' if cli_available('claude') else 'missing'}")
    print(f"Codex CLI: {'installed' if cli_available('codex') else 'missing'}")
    print(f"Git: {'installed' if cli_available('git') else 'missing'}")
    print()
    print("Commands")
    print('relay "task"')
    print('relay @claude "task"')
    print('relay @codex "task"')
    print('relay continue "task"')
    print("relay review")
    print("relay summary")
    print('relay why "task"')
    print("relay doctor")
    print("relay status")
    print("relay history")
    missing = missing_required_dependencies()
    if missing:
        print()
        print("Install hints")
        print_install_hints(missing)
    return None


def print_doctor(repo: RepoState) -> int:
    print("Relay")
    print(f"Version: {VERSION}")
    print()
    print("Doctor")
    print(f"Current working directory: {repo.cwd}")
    print(f"Inside git repo: {'yes' if repo.in_git_repo else 'no'}")
    if repo.in_git_repo:
        print(f"Git root: {repo.repo_root}")

    for dep in ("claude", "codex", "git"):
        print(f"{dep}: {'ok' if cli_available(dep) else 'missing'}")

    relay_exists = repo.relay_dir.exists() if repo.relay_dir else False
    print(f".relay directory: {'present' if relay_exists else 'missing'}")

    if repo.in_git_repo and repo.tasks_path:
        writable = repo.tasks_path.exists() and os.access(repo.tasks_path, os.W_OK)
        if not repo.tasks_path.exists():
            writable = os.access(repo.repo_root, os.W_OK)  # type: ignore[arg-type]
        print(f"History file writable: {'yes' if writable else 'no'}")

    missing = missing_required_dependencies()
    if missing:
        print()
        print("Issues")
        for dep in missing:
            print(f"- Missing dependency: {dep}")
        print()
        print("Install hints")
        print_install_hints(missing)
    elif not repo.in_git_repo:
        print()
        print("Issues")
        print("- Not inside a git repository, so Relay cannot run task/review workflows yet.")
    else:
        print()
        print("Relay environment looks ready.")
    return 0


def print_status(repo: RepoState) -> int:
    tasks = load_repo_tasks(repo) if repo.in_git_repo and repo.tasks_path and repo.tasks_path.exists() else []
    last = latest_task(tasks)
    print("Status")
    print(f"Claude: {'available' if cli_available('claude') else 'missing'}")
    print(f"Codex: {'available' if cli_available('codex') else 'missing'}")
    print(f"Git: {'available' if cli_available('git') else 'missing'}")
    print("Recent Claude rate-limit detected: " + ("yes" if recent_rate_limit(tasks, "claude") else "no"))
    print("Recent Codex rate-limit detected: " + ("yes" if recent_rate_limit(tasks, "codex") else "no"))
    if last:
        status = "success" if last.get("success") else "failed"
        print(f"Last task: {normalize_agent_name(last.get('selected_agent', 'codex'))} / {status}")
    else:
        print("Last task: none")
    return 0


def print_why(task: str, repo: RepoState, forced_agent: str | None = None, extra_context: str = "") -> int:
    decision = route_task(task, repo, forced_agent=forced_agent, extra_context=extra_context)
    print(f"Would route to: {normalize_agent_name(decision.agent)}")
    print(f"Reason: {decision.reason}")
    print(f"Claude score: {decision.claude_score}")
    print(f"Codex score: {decision.codex_score}")
    print("Claude keywords: " + (", ".join(decision.matched_claude_keywords) if decision.matched_claude_keywords else "none"))
    print("Codex keywords: " + (", ".join(decision.matched_codex_keywords) if decision.matched_codex_keywords else "none"))
    print("Claude file/path hints: " + (", ".join(decision.matched_claude_hints) if decision.matched_claude_hints else "none"))
    print("Codex file/path hints: " + (", ".join(decision.matched_codex_hints) if decision.matched_codex_hints else "none"))
    print("Manual override detected: " + (decision.manual_override if decision.manual_override else "no"))
    print(
        "Recent rate-limit penalty: "
        + f"Claude -{decision.rate_limit_penalty['claude']}, Codex -{decision.rate_limit_penalty['codex']}"
    )
    print("Previous handoff influence: " + (decision.handoff_influence if decision.handoff_influence else "none"))
    return 0


def final_result_summary(agent: str, exit_code: int, files: list[str], prompt_type: str) -> None:
    print()
    print("Result")
    print(f"Agent: {normalize_agent_name(agent)}")
    print(f"Exit code: {exit_code}")
    print(f"Success: {'yes' if exit_code == 0 else 'no'}")
    print("Changed files:")
    if files:
        for path in files:
            print(f"- {path}")
    else:
        print("- none")
    print()
    print("Next")
    if prompt_type == "review":
        print("relay summary")
    else:
        print("relay review")
        print("relay summary")


def execute_agent_run(
    *,
    repo: RepoState,
    user_task: str,
    prompt: str,
    prompt_type: str,
    forced_agent: str | None = None,
    extra_context: str = "",
) -> int:
    require_task_dependencies(repo)
    ensure_relay_files(repo)

    decision = route_task(user_task, repo, forced_agent=forced_agent, extra_context=extra_context)
    agent = decision.agent
    command = build_agent_command(agent, prompt)

    print(f"Routing to: {normalize_agent_name(agent)}")
    print(f"Reason: {decision.reason}")
    matches = decision.matched_claude_keywords + decision.matched_codex_keywords
    if matches:
        print("Matched: " + ", ".join(matches[:8]))
    print()
    print("Running")
    print(" ".join(command))
    print()

    exit_code, output = stream_subprocess(command, cwd=repo.repo_root or repo.cwd)
    rate_limited = detect_rate_limit(output)
    if rate_limited:
        print("\nWarning: Relay detected likely rate-limit or usage-limit output.", file=sys.stderr)

    files = changed_files(repo)
    diff_text = current_diff(repo)
    diff_stat = diff_summary(repo)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed")
    save_last_diff(repo, diff_text)

    handoff = create_handoff(agent, user_task, files, output, diff_stat, warnings)
    write_handoff(repo, handoff)
    append_decision(
        repo,
        f"[{timestamp_now()}] {normalize_agent_name(agent)} | {prompt_type} | {decision.reason}",
    )

    append_repo_task(
        repo,
        task_entry(
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
        ),
    )

    if warnings:
        print()
        print("Warnings")
        for item in warnings:
            print(f"- {item}")

    final_result_summary(agent, exit_code, files, prompt_type)
    return exit_code


def run_main_task(task: str, repo: RepoState, forced_agent: str | None = None) -> int:
    return execute_agent_run(
        repo=repo,
        user_task=task,
        prompt=task,
        prompt_type="normal",
        forced_agent=forced_agent,
    )


def run_continue(task: str, repo: RepoState) -> int:
    require_task_dependencies(repo)
    ensure_relay_files(repo)
    tasks = load_repo_tasks(repo)
    last = latest_task(tasks)
    handoff = read_handoff(repo)
    files = changed_files(repo)
    compact_context = "\n".join(
        [
            f"Current user task: {task}",
            "Latest handoff:",
            trim_words(handoff, 180) if handoff else "No prior handoff.",
            "Changed files:",
            ", ".join(files) if files else "none",
            "Relevant diff summary:",
            trim_words(diff_summary(repo) or "No current diff.", 80),
            "Preserve prior work and continue from the existing state.",
        ]
    )
    if last:
        compact_context += f"\nPrevious Relay task: {last.get('original_task', '')}"
    prompt = trim_words(compact_context, 500)
    return execute_agent_run(
        repo=repo,
        user_task=task,
        prompt=prompt,
        prompt_type="continue",
        extra_context=" ".join(files) + " " + handoff[:MAX_HANDOFF_PROMPT_CHARS],
    )


def run_review(repo: RepoState) -> int:
    require_task_dependencies(repo)
    ensure_relay_files(repo)
    diff_text = current_diff(repo)
    if not diff_text.strip():
        print("No changes to review.")
        return 0

    tasks = load_repo_tasks(repo)
    last = latest_task(tasks)
    last_agent = last.get("selected_agent") if last else None
    forced_agent = "codex"
    if last_agent == "claude":
        forced_agent = "codex"
    elif last_agent == "codex":
        forced_agent = "claude"

    prompt = (
        "Review this git diff for bugs, broken logic, missing tests, security risks, "
        "risky file changes, and unnecessary edits. Do not rewrite code unless asked. "
        "Return concise findings.\n\n"
        f"Changed files: {', '.join(changed_files(repo)) or 'none'}\n\n"
        f"Diff:\n{diff_text[:MAX_DIFF_PROMPT_CHARS]}"
    )
    return execute_agent_run(
        repo=repo,
        user_task="Review current git diff",
        prompt=prompt,
        prompt_type="review",
        forced_agent=forced_agent,
        extra_context="review " + " ".join(changed_files(repo)),
    )


def run_summary(repo: RepoState) -> int:
    if not cli_available("git"):
        raise RelayError("Relay summary requires git.")
    if not repo.in_git_repo:
        raise RelayError("Relay summary requires running inside a git repository.")

    files = changed_files(repo)
    if not files:
        print("Summary")
        print("No current git diff.")
        return 0

    tasks = load_repo_tasks(repo) if repo.tasks_path and repo.tasks_path.exists() else []
    last = latest_task(tasks)
    warnings = warning_paths(files)
    if len(files) > 20:
        warnings.append("more than 20 files changed")

    print("Summary")
    print("Changed files:")
    for path in files:
        print(f"- {path}")
    print()
    print("What changed")
    print(trim_words(diff_summary(repo).replace("\n", " "), 60) or "Git diff is present but stat summary is unavailable.")
    print()
    print("Potential risks")
    if warnings:
        for item in warnings:
            print(f"- {item}")
    else:
        print("- No obvious high-risk paths detected.")
    print()
    print("Suggested commit message")
    if last:
        print(f"- {last.get('selected_agent', 'codex')}: {trim_words(last.get('original_task', 'update project'), 12)}")
    else:
        print("- update project changes")
    return 0


def print_history(repo: RepoState) -> int:
    if not repo.in_git_repo:
        print("No local Relay history because this directory is not inside a git repo.")
        return 0
    ensure_relay_files(repo)
    tasks = load_repo_tasks(repo)
    if not tasks:
        print("No Relay task history yet.")
        return 0

    for entry in reversed(tasks[-MAX_HISTORY_DISPLAY:]):
        status = "ok" if entry.get("success") else "failed"
        rate_flag = "yes" if entry.get("rate_limit_detected") else "no"
        changed_count = len(entry.get("changed_files", []))
        print(
            f"{entry.get('timestamp')} | "
            f"{normalize_agent_name(entry.get('selected_agent', 'codex')):<6} | "
            f"{status:<6} | files={changed_count:<2} | rate-limit={rate_flag:<3} | "
            f"{entry.get('original_task', '')}"
        )
    return 0


def usage() -> int:
    print("Usage:")
    print("  relay")
    print("  relay doctor")
    print("  relay status")
    print('  relay why "task"')
    print('  relay "task"')
    print('  relay @claude "task"')
    print('  relay @codex "task"')
    print('  relay continue "task"')
    print("  relay review")
    print("  relay summary")
    print("  relay history")
    return 1


def parse_args(argv: list[str]) -> tuple[str, str | None]:
    if not argv:
        return "home", None

    command = argv[0]
    if command in {"doctor", "status", "review", "summary", "history"}:
        return command, None

    if command == "why":
        task = " ".join(argv[1:]).strip()
        if not task:
            raise ValueError("missing task")
        return "why", task

    if command == "continue":
        task = " ".join(argv[1:]).strip()
        if not task:
            raise ValueError("missing task")
        return "continue", task

    if command in {"@claude", "@codex"}:
        task = " ".join(argv[1:]).strip()
        if not task:
            raise ValueError("missing task")
        return command, task

    task = " ".join(argv).strip()
    if not task:
        raise ValueError("missing task")
    return "run", task


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo = get_repo_state()
    try:
        command, value = parse_args(args)
    except ValueError:
        return usage()

    try:
        if command == "home":
            print_relay_home(repo)
            return 0
        if command == "doctor":
            return print_doctor(repo)
        if command == "status":
            return print_status(repo)
        if command == "why":
            return print_why(value or "", repo)
        if command == "continue":
            return run_continue(value or "", repo)
        if command == "review":
            return run_review(repo)
        if command == "summary":
            return run_summary(repo)
        if command == "history":
            return print_history(repo)
        if command == "@claude":
            return run_main_task(value or "", repo, forced_agent="claude")
        if command == "@codex":
            return run_main_task(value or "", repo, forced_agent="codex")
        return run_main_task(value or "", repo)
    except RelayError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        missing = missing_required_dependencies()
        if missing:
            print_install_hints(missing)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
