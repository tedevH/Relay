from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from relay_core.types import RepoState
from relay_core.utils import timestamp_now


def refresh_brain(repo: RepoState, task: str = "", *, force: bool = False) -> dict[str, Any]:
    """Quietly keep Relay's repo brain current before agent work.

    This is deliberately automatic. Humans should not need to remember to run
    `relay digest`, `relay context`, or `relay scan` before asking for work.
    """
    if not repo.in_git_repo or not repo.relay_dir:
        return {}

    from relay_core.memory import ensure_relay_files, load_project_profile, save_project_profile
    from relay_core.routing import scan_project

    ensure_relay_files(repo)
    profile = load_project_profile(repo) or {}
    if force or _profile_is_stale(profile):
        profile = scan_project(repo)

    profile.update(_detect_repo_workflows(repo.repo_root))
    profile["brain_refreshed_at"] = timestamp_now()
    save_project_profile(repo, profile)

    _write_agent_runbook(repo, profile, task)
    _write_brain_state(repo, profile)
    return profile


def learn_from_run(
    repo: RepoState,
    *,
    task: str,
    agent: str,
    files: list[str],
    diff_text: str,
    success: bool,
    verification: dict[str, Any] | None = None,
) -> None:
    """Quietly fold an agent run back into Relay memory.

    The post-commit hook catches committed work. This catches normal task runs,
    failed automation attempts, and successful auto-runs before/around commits.
    """
    if not repo.in_git_repo or not repo.relay_dir:
        return

    from relay_core.diff import classify_file_risk
    from relay_core.intelligence import (
        extract_symbols_from_diff, classify_workstream, merge_symbols, update_workstream,
    )
    from relay_core.memory import update_project_knowledge, load_memory, save_memory

    new_symbols = extract_symbols_from_diff(diff_text)
    symbol_names = list(new_symbols.keys())
    if new_symbols:
        merge_symbols(repo, new_symbols)

    ws_name = classify_workstream(task, files, symbol_names)
    update_workstream(
        repo,
        ws_name=ws_name,
        task=task,
        agent=agent,
        files=files,
        new_symbols=symbol_names,
        commit_msg="",
    )

    verify_commands = [
        str(check.get("command", ""))
        for check in (verification or {}).get("commands", [])
        if check.get("command")
    ]
    risky_files = [file for file in files if classify_file_risk(file) == "HIGH"]
    failed_output = _first_failed_output(verification or {})
    update_project_knowledge(
        repo,
        verify_commands=verify_commands,
        risky_files=risky_files,
        known_failure=failed_output if not success else None,
    )

    mem = load_memory(repo)
    lessons = mem.get("lessons", [])
    lessons.append({
        "timestamp": timestamp_now(),
        "task": task[:160],
        "agent": agent,
        "success": success,
        "workstream": ws_name,
        "files": files[:8],
        "verification_failed": bool(failed_output),
    })
    mem["lessons"] = lessons[-50:]
    save_memory(repo, mem)

    refresh_brain(repo, task)


def _profile_is_stale(profile: dict[str, Any]) -> bool:
    if not profile:
        return True
    scanned = str(profile.get("scanned_at") or profile.get("brain_refreshed_at") or "")[:10]
    return scanned != timestamp_now()[:10]


def _detect_repo_workflows(root: Path | None) -> dict[str, Any]:
    if not root:
        return {}

    workflows: dict[str, Any] = {
        "test_commands": [],
        "build_commands": [],
        "lint_commands": [],
        "dev_commands": [],
        "package_manager": "",
    }

    package_json = root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            package_manager = _detect_package_manager(root)
            if "test" in scripts:
                workflows["test_commands"].append(_pm_run(package_manager, "test"))
            if "lint" in scripts:
                workflows["lint_commands"].append(_pm_run(package_manager, "lint"))
            if "build" in scripts:
                workflows["build_commands"].append(_pm_run(package_manager, "build"))
            if "dev" in scripts:
                workflows["dev_commands"].append(_pm_run(package_manager, "dev"))
            workflows["package_manager"] = package_manager
        except Exception:
            pass

    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        if (root / "pytest.ini").exists() or (root / "tests").exists():
            workflows["test_commands"].append("pytest")
        if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
            workflows["lint_commands"].append("ruff check .")
        workflows["package_manager"] = workflows["package_manager"] or "python"

    if (root / "go.mod").exists():
        workflows["test_commands"].append("go test ./...")
        workflows["package_manager"] = workflows["package_manager"] or "go"

    if (root / "Cargo.toml").exists():
        workflows["test_commands"].append("cargo test")
        workflows["build_commands"].append("cargo build")
        workflows["package_manager"] = workflows["package_manager"] or "cargo"

    return {key: sorted(set(val)) if isinstance(val, list) else val for key, val in workflows.items() if val}


def _detect_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "package-lock.json").exists():
        return "npm"
    return "npm"


def _pm_run(package_manager: str, script: str) -> str:
    if package_manager == "pnpm":
        return "pnpm " + script
    if package_manager == "yarn":
        return "yarn " + script
    return "npm test" if script == "test" else f"npm run {script}"


def _write_agent_runbook(repo: RepoState, profile: dict[str, Any], task: str) -> None:
    if not repo.relay_dir:
        return

    from relay_core.memory import load_memory
    from relay_core.intelligence import load_workstreams

    mem = load_memory(repo)
    workstreams = load_workstreams(repo)
    hot_files = list((mem.get("hot_files") or {}).keys())[:8]
    risky_files = profile.get("risky_files", [])[:8]

    lines = [
        "# Relay Agent Runbook",
        "",
        "This file is maintained automatically by Relay. Use it as operating context before editing.",
        "",
        f"Current task: {task or 'none'}",
        f"Framework: {profile.get('framework', 'unknown')}",
        f"Primary language: {profile.get('primary_language', 'unknown')}",
    ]

    _extend_section(lines, "Test commands", profile.get("test_commands", []))
    _extend_section(lines, "Lint commands", profile.get("lint_commands", []))
    _extend_section(lines, "Build commands", profile.get("build_commands", []))
    _extend_section(lines, "Hot files", hot_files)
    _extend_section(lines, "Risky files", risky_files)
    _extend_section(lines, "Active workstreams", list(workstreams.keys())[:8])

    lessons = mem.get("lessons", [])[-5:]
    if lessons:
        lines += ["", "## Recent lessons"]
        for lesson in lessons:
            status = "success" if lesson.get("success") else "failed"
            lines.append(f"- {status}: {lesson.get('task', '')}")

    (repo.relay_dir / "runbook.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_brain_state(repo: RepoState, profile: dict[str, Any]) -> None:
    if not repo.brain_path:
        return

    try:
        brain = json.loads(repo.brain_path.read_text(encoding="utf-8")) if repo.brain_path.exists() else {}
    except Exception:
        brain = {}
    brain["auto_brain"] = {
        "updated_at": timestamp_now(),
        "runbook": ".relay/runbook.md",
        "context": ".relay/context.md",
        "framework": profile.get("framework", "unknown"),
        "primary_language": profile.get("primary_language", "unknown"),
        "test_commands": profile.get("test_commands", []),
        "lint_commands": profile.get("lint_commands", []),
        "build_commands": profile.get("build_commands", []),
    }
    repo.brain_path.write_text(json.dumps(brain, indent=2) + "\n", encoding="utf-8")


def _extend_section(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    lines += ["", f"## {title}"]
    for item in items:
        lines.append(f"- {item}")


def _first_failed_output(verification: dict[str, Any]) -> str:
    for check in verification.get("commands", []) or []:
        if check.get("passed") is False and check.get("output"):
            return str(check.get("output"))[:240]
    tests = verification.get("tests") or {}
    if tests.get("passed") is False and tests.get("output"):
        return str(tests.get("output"))[:240]
    return ""
