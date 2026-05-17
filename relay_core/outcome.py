from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from relay_core.types import RepoState
from relay_core.git import current_diff, diff_summary, status_changed_files
from relay_core.diff import classify_files_risk, smart_commit_message, detect_contradictions
from relay_core.utils import timestamp_now


def build_outcome(
    repo: RepoState,
    *,
    task: str,
    command_type: str,
    agent: str = "",
    exit_code: int = 0,
    verified: bool | None = None,
    verification: dict[str, Any] | None = None,
    branch: str = "",
    commit_hash: str = "",
    run_id: str = "",
    files_override: list[str] | None = None,
    diff_stat_override: str = "",
) -> dict[str, Any]:
    files = files_override if files_override is not None else status_changed_files(repo)
    diff_text = current_diff(repo)
    risks = classify_files_risk(files)
    warnings = _warnings(files, diff_text)
    contradictions = detect_contradictions(files, diff_text) if diff_text else []
    commit_message = smart_commit_message(files, diff_text) if files else ""

    return {
        "version": 1,
        "timestamp": timestamp_now(),
        "task": task,
        "command_type": command_type,
        "agent": agent,
        "exit_code": exit_code,
        "success": exit_code == 0,
        "verified": verified,
        "verification": verification or {},
        "branch": branch,
        "commit_hash": commit_hash,
        "run_id": run_id,
        "files": files,
        "diff_stat": diff_stat_override or diff_summary(repo),
        "risk_levels": risks,
        "warnings": warnings,
        "contradictions": contradictions,
        "suggested_commit_message": commit_message,
        "next_steps": _next_steps(
            files=files,
            success=exit_code == 0,
            verified=verified,
            warnings=warnings,
            commit_hash=commit_hash,
        ),
    }


def save_outcome(repo: RepoState, outcome: dict[str, Any]) -> None:
    if not repo.relay_dir:
        return
    path = repo.relay_dir / "last-outcome.json"
    path.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")


def load_outcome(repo: RepoState) -> dict[str, Any] | None:
    if not repo.relay_dir:
        return None
    path = repo.relay_dir / "last-outcome.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _warnings(files: list[str], diff_text: str) -> list[str]:
    warnings: list[str] = []
    lowered_files = " ".join(files).lower()
    lowered_diff = diff_text.lower()
    if len(files) > 20:
        warnings.append("Large change set: more than 20 files changed")
    if any(marker in lowered_files for marker in (".env", "secret", "credential", ".pem", ".key")):
        warnings.append("Sensitive file path touched")
    if any(marker in lowered_files for marker in ("auth", "payment", "stripe", "migration")):
        warnings.append("High-risk area touched")
    if any(marker in lowered_diff for marker in ("api_key", "password", "private key", "secret=")):
        warnings.append("Possible credential-like text in diff")
    return warnings


def _next_steps(
    *,
    files: list[str],
    success: bool,
    verified: bool | None,
    warnings: list[str],
    commit_hash: str,
) -> list[str]:
    if not success:
        return ["Run relay last to inspect the failed outcome", "Retry with relay go after adjusting the task"]
    if not files and not commit_hash:
        return ["No file changes detected", "Try a more specific task or run relay why \"task\""]
    if warnings:
        return ["Run relay review", "Inspect risky files before pushing"]
    if verified is True and commit_hash:
        return ["Run relay push when ready"]
    if verified is True:
        return ["Run relay commit", "Run relay push after commit"]
    if verified is False:
        return ["Run relay review", "Run relay go with a clearer done condition"]
    return ["Run relay summary", "Run relay review", "Run relay commit when satisfied"]
