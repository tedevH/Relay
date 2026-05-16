from __future__ import annotations
import sys

from relay_core.types import RepoState, RelayError
from relay_core.git import get_repo_state
from relay_core.utils import missing_required_dependencies, print_install_hints, fuzzy_match_command
from relay_core.memory import load_repo_tasks, recent_rate_limit
from relay_core.constants import ALL_COMMANDS, MAX_HISTORY_DISPLAY, VERSION
import relay_core.tui as tui
from relay_core.commands import (
    run_task, run_review, run_ai_review, run_summary,
    run_commit, run_push, run_scan, run_config,
    run_dashboard, run_audit, run_init, run_context, run_digest,
)


def _dispatch(command: str, value: str | None, repo: RepoState) -> int:
    if command == "doctor":       return _cmd_doctor(repo)
    if command == "status":       return _cmd_status(repo)
    if command == "why":          return _cmd_why(value or "", repo)
    if command == "review":       return run_review(repo)
    if command == "ai-review":    return run_ai_review(repo)
    if command == "summary":      return run_summary(repo)
    if command == "commit":       return run_commit(repo)
    if command == "push":         return run_push(repo)
    if command == "history":      return _cmd_history(repo)
    if command == "scan":         return run_scan(repo)
    if command == "config":       return run_config(repo)
    if command == "dashboard":    return run_dashboard(repo)
    if command == "audit":        return run_audit(repo, ci_mode=(value == "--ci"))
    if command == "init":         return run_init(repo)
    if command == "context":      return run_context(repo)
    if command == "digest":       return run_digest(repo)
    if command == "chain":
        from relay_core.chain import run_chain
        return run_chain(value or "", repo)
    if command == "@claude":      return run_task(value or "", repo, forced_agent="claude")
    if command == "@codex":       return run_task(value or "", repo, forced_agent="codex")
    return run_task(value or "", repo)


def parse_args(argv: list[str]) -> tuple[str, str | None]:
    if not argv:
        return "home", None

    command = argv[0]
    alias_map = {
        "r": "review", "s": "summary", "c": "commit", "p": "push",
        "h": "history", "help": "home", "d": "dashboard", "dash": "dashboard",
        "i": "interactive",
    }
    command = alias_map.get(command, command)

    parameterless = {
        "doctor", "status", "review", "ai-review", "summary", "history",
        "commit", "push", "scan", "config", "dashboard", "interactive",
        "init", "context", "digest",
    }
    if command in parameterless:
        return command, None

    if command == "audit":
        return "audit", "--ci" if "--ci" in argv[1:] else ""

    if command in {"why", "continue", "chain"}:
        task = " ".join(argv[1:]).strip()
        if not task:
            raise ValueError(f"'{command}' requires a task argument")
        return command, task

    if command in {"@claude", "@codex"}:
        task = " ".join(argv[1:]).strip()
        if not task:
            raise ValueError(f"'{command}' requires a task argument")
        return command, task

    task = " ".join(argv).strip()
    if not task:
        raise ValueError("missing task")
    return "run", task


def run_interactive(repo: RepoState) -> int:
    missing = missing_required_dependencies()
    tui.show_home(repo, missing)

    while True:
        try:
            raw = tui.ask_input("\n[bold cyan]relay[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            tui.console.print("\n[dim]Goodbye.[/dim]")
            break

        stripped = raw.strip()
        if not stripped or stripped.lower() in ("exit", "quit", "q"):
            tui.console.print("[dim]Goodbye.[/dim]")
            break

        try:
            args = stripped.split()
            first = args[0].lower()
            known = set(ALL_COMMANDS) | {"@claude", "@codex", "r", "s", "c", "p", "h", "d", "i"}
            if first not in known and not first.startswith("@"):
                suggestion = fuzzy_match_command(first)
                if suggestion:
                    tui.show_error(f"Unknown command '{first}'. Did you mean '{suggestion}'?")
                    continue
            command, value = parse_args(args)
            if command == "home":
                tui.show_home(repo, missing_required_dependencies())
                continue
            _dispatch(command, value, repo)
        except RelayError as exc:
            tui.show_error(str(exc))
        except ValueError as exc:
            tui.show_error(str(exc))

    return 0


def _cmd_doctor(repo: RepoState) -> int:
    from relay_core.utils import all_required_dependencies, missing_required_dependencies
    tui.show_doctor(repo, all_required_dependencies(), missing_required_dependencies())
    return 0


def _cmd_status(repo: RepoState) -> int:
    tasks = load_repo_tasks(repo) if repo.in_git_repo and repo.tasks_path and repo.tasks_path.exists() else []
    tui.show_status(repo, tasks, {
        "claude": recent_rate_limit(tasks, "claude"),
        "codex": recent_rate_limit(tasks, "codex"),
    })
    return 0


def _cmd_why(task: str, repo: RepoState) -> int:
    from relay_core.routing import route_task
    tui.show_why(route_task(task, repo))
    return 0


def _cmd_history(repo: RepoState) -> int:
    if not repo.in_git_repo:
        tui.show_info("Not inside a git repo — no history available.")
        return 0
    from relay_core.memory import ensure_relay_files
    ensure_relay_files(repo)
    tui.show_history(load_repo_tasks(repo)[-MAX_HISTORY_DISPLAY:])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    # Internal hook command — called by git post-commit hook silently
    if args and args[0] == "_hook-post-commit":
        try:
            repo = get_repo_state()
            from relay_core.hooks import run_post_commit_hook
            run_post_commit_hook(repo)
        except Exception:
            pass
        return 0

    if args == ["--version"]:
        print(f"relay v{VERSION}")
        return 0

    repo = get_repo_state()

    # First-time wizard — only when no args (interactive mode)
    if repo.in_git_repo and repo.config_path and not repo.config_path.exists() and not args:
        try:
            from relay_core.wizard import run_first_time_wizard
            run_first_time_wizard(repo)
        except (KeyboardInterrupt, EOFError):
            tui.console.print("\n[dim]Setup skipped.[/dim]")

    if not args:
        return run_interactive(repo)

    # Fuzzy match on unknown single-word first arg
    first_arg = args[0].lower()
    known = set(ALL_COMMANDS) | {"@claude", "@codex", "r", "s", "c", "p", "h", "d", "i", "help", "--version"}
    if (first_arg not in known and not first_arg.startswith("@")
            and len(args) == 1 and " " not in first_arg):
        suggestion = fuzzy_match_command(first_arg)
        if suggestion:
            tui.show_error(f"Unknown command '{first_arg}'. Did you mean '{suggestion}'?")
            return 1

    try:
        command, value = parse_args(args)
    except ValueError:
        tui.show_error("Usage: relay \"task\"  |  relay <command>  |  relay (interactive)")
        return 1

    if command in {"home", "interactive"}:
        return run_interactive(repo)

    try:
        return _dispatch(command, value, repo)
    except RelayError as exc:
        tui.show_error(str(exc))
        missing = missing_required_dependencies()
        if missing:
            print_install_hints(missing)
        return 1
