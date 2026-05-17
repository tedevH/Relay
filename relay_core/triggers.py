from __future__ import annotations
import json
import subprocess
from pathlib import Path
from typing import Any

from relay_core.types import RepoState, RelayError
from relay_core.utils import timestamp_now, cli_available
from relay_core.memory import ensure_relay_files
import relay_core.tui as tui


def run_watch(task: str, repo: RepoState) -> int:
    return _add_trigger(repo, "watch", {"task": task, "event": "manual-watch"})


def run_every(spec: str, repo: RepoState) -> int:
    parts = spec.split(maxsplit=1)
    if len(parts) != 2:
        raise RelayError('Usage: relay every "1h" "task"')
    interval, task = parts
    return _add_trigger(repo, "every", {"interval": interval, "task": task})


def run_on(spec: str, repo: RepoState) -> int:
    parts = spec.split(maxsplit=1)
    if len(parts) != 2:
        raise RelayError('Usage: relay on "ci-fail" "task"')
    event, task = parts
    return _add_trigger(repo, "on", {"event": event, "task": task})


def run_triggers(repo: RepoState) -> int:
    _require_repo(repo)
    ensure_relay_files(repo)
    triggers = _load_triggers(repo)
    if not triggers:
        tui.show_info("No Relay triggers configured.")
        return 0
    for item in triggers:
        label = item.get("kind", "trigger")
        detail = item.get("interval") or item.get("event") or ""
        tui.console.print(f"[bold]{item.get('id')}[/bold]  {label} {detail}  [dim]{item.get('task')}[/dim]")
    return 0


def run_trigger_check(repo: RepoState) -> int:
    """Evaluate simple local triggers once.

    This is intentionally a one-shot check so users can wire it into cron,
    launchd, or CI without Relay needing a daemon.
    """
    _require_repo(repo)
    ensure_relay_files(repo)
    triggers = _load_triggers(repo)
    if not triggers:
        tui.show_info("No triggers to check.")
        return 0
    fired = 0
    for item in triggers:
        if item.get("status") != "active":
            continue
        if not _should_fire(item, repo):
            continue
        fired += 1
        item["last_run_at"] = timestamp_now()
        tui.show_info(f"Trigger {item.get('id')} fired: {item.get('task')}")
        from relay_core.brain import run_brain
        result = run_brain(item.get("task", ""), repo)
        item["last_result"] = result
        if result != 0:
            break
    _triggers_path(repo).write_text(json.dumps(triggers, indent=2) + "\n", encoding="utf-8")
    if fired == 0:
        tui.show_info("No triggers fired.")
    return 0 if fired == 0 or all(item.get("last_result", 0) == 0 for item in triggers if item.get("last_run_at")) else 1


def _add_trigger(repo: RepoState, kind: str, data: dict[str, Any]) -> int:
    _require_repo(repo)
    ensure_relay_files(repo)
    triggers = _load_triggers(repo)
    item = {
        "id": str(len(triggers) + 1),
        "kind": kind,
        "created_at": timestamp_now(),
        "status": "active",
        **data,
    }
    triggers.append(item)
    _triggers_path(repo).write_text(json.dumps(triggers, indent=2) + "\n", encoding="utf-8")
    tui.show_success(f"Saved {kind} trigger {item['id']}.")
    return 0


def _load_triggers(repo: RepoState) -> list[dict[str, Any]]:
    path = _triggers_path(repo)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _triggers_path(repo: RepoState) -> Path:
    if not repo.relay_dir:
        raise RelayError("Relay trigger state is unavailable outside a git repository.")
    return repo.relay_dir / "triggers.json"


def _require_repo(repo: RepoState) -> None:
    if not repo.in_git_repo:
        raise RelayError("Relay triggers require running inside a git repository.")


def _should_fire(item: dict[str, Any], repo: RepoState) -> bool:
    kind = item.get("kind")
    if kind == "watch":
        return True
    if kind == "every":
        return _interval_due(item)
    if kind == "on" and item.get("event") == "ci-fail":
        return _latest_ci_failed(repo)
    return False


def _interval_due(item: dict[str, Any]) -> bool:
    interval = item.get("interval", "")
    seconds = _parse_interval(interval)
    if seconds <= 0:
        return False
    last = item.get("last_run_at")
    if not last:
        return True
    from datetime import datetime, timezone
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= seconds


def _parse_interval(value: str) -> int:
    import re
    match = re.fullmatch(r"(\d+)([mhd])", value.strip().lower())
    if not match:
        return 0
    amount = int(match.group(1))
    unit = match.group(2)
    return amount * {"m": 60, "h": 3600, "d": 86400}[unit]


def _latest_ci_failed(repo: RepoState) -> bool:
    if not cli_available("gh"):
        tui.show_info("GitHub CLI `gh` is not installed; ci-fail trigger skipped.")
        return False
    result = subprocess.run(
        ["gh", "run", "list", "--limit", "1", "--json", "conclusion"],
        cwd=str(repo.repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        runs = json.loads(result.stdout)
    except Exception:
        return False
    if not runs:
        return False
    return runs[0].get("conclusion") in {"failure", "cancelled", "timed_out"}
