from __future__ import annotations
import json
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 120


def discover_verify_commands(repo_root: Path, config: dict[str, Any] | None = None) -> list[list[str]]:
    """Return project-appropriate verification commands.

    Configured commands win. Otherwise Relay infers a small, safe set from
    common project files. The commands are intentionally local and bounded.
    """
    config = config or {}
    configured = config.get("verify_commands") or []
    if configured:
        return [_split_command(cmd) for cmd in configured if str(cmd).strip()]

    if (repo_root / "package.json").exists():
        package = _read_package_json(repo_root / "package.json")
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        commands: list[list[str]] = []
        if "test" in scripts:
            commands.append(["npm", "test", "--", "--passWithNoTests"])
        if "lint" in scripts:
            commands.append(["npm", "run", "lint"])
        if "build" in scripts:
            commands.append(["npm", "run", "build"])
        return commands

    if (repo_root / "pytest.ini").exists() or (repo_root / "pyproject.toml").exists():
        return [["python", "-m", "pytest", "--tb=short", "-q"]]

    if (repo_root / "go.mod").exists():
        return [["go", "test", "./..."]]

    if (repo_root / "Cargo.toml").exists():
        return [["cargo", "test"]]

    return []


def run_verification(
    repo_root: Path,
    done_condition: str,
    changed_files: list[str],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commands = discover_verify_commands(repo_root, config)
    checks: list[dict[str, Any]] = []
    all_passed = True if commands else None

    for command in commands:
        check = _run_check(command, repo_root)
        checks.append(check)
        if check["returncode"] != 0:
            all_passed = False

    output = "\n\n".join(check["output"] for check in checks if check.get("output"))
    done_checks = _done_condition_checks(repo_root, done_condition, changed_files, output)
    done_met = done_checks["passed"]

    return {
        "changed": len(changed_files) > 0,
        "commands": checks,
        "done_checks": done_checks["checks"],
        "tests": {"passed": all_passed, "output": output[:3000]},
        "done_condition_met": done_met,
    }


def verification_passed(result: dict[str, Any]) -> bool:
    tests = result.get("tests", {})
    if tests.get("passed") is True:
        return True
    return result.get("done_condition_met") is True


def _run_check(command: list[str], cwd: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return {
            "command": " ".join(command),
            "returncode": completed.returncode,
            "passed": completed.returncode == 0,
            "output": output[:3000],
        }
    except FileNotFoundError:
        return {
            "command": " ".join(command),
            "returncode": 127,
            "passed": False,
            "output": f"Command not found: {command[0]}",
        }
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, str) else ""
        return {
            "command": " ".join(command),
            "returncode": 124,
            "passed": False,
            "output": (output + "\nVerification timed out.").strip()[:3000],
        }


def _read_package_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _split_command(command: str) -> list[str]:
    import shlex
    return shlex.split(command)


def _done_condition_checks(
    repo_root: Path,
    done_condition: str,
    changed_files: list[str],
    output: str,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    file_check = _file_exists_check(repo_root, done_condition)
    if file_check:
        checks.append(file_check)
    endpoint_check = _endpoint_check(done_condition)
    if endpoint_check:
        checks.append(endpoint_check)
    tests_added = _tests_added_check(done_condition, changed_files)
    if tests_added:
        checks.append(tests_added)
    keyword = _done_condition_hint(done_condition, output)
    if keyword is not None:
        checks.append({"name": "done-condition-keyword", "passed": keyword})

    if not checks:
        return {"passed": None, "checks": []}
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def _done_condition_hint(done_condition: str, output: str) -> bool | None:
    if not done_condition or not output:
        return None
    lowered = done_condition.lower()
    if any(word in lowered for word in ("pass", "passes", "passing", "green")):
        return None
    import re
    keywords = [kw for kw in re.findall(r"\b\w{4,}\b", lowered) if kw not in {"when", "done", "condition"}]
    if not keywords:
        return None
    output_lower = output.lower()
    return any(keyword in output_lower for keyword in keywords)


def _file_exists_check(repo_root: Path, done_condition: str) -> dict[str, Any] | None:
    import re
    match = re.search(r"(?:file|path)\s+[`'\"]?([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)[`'\"]?\s+(?:exists|exist|created)", done_condition, re.I)
    if not match:
        return None
    rel = match.group(1).strip("/")
    return {
        "name": "file-exists",
        "target": rel,
        "passed": (repo_root / rel).exists(),
    }


def _endpoint_check(done_condition: str) -> dict[str, Any] | None:
    import re
    match = re.search(r"(https?://(?:localhost|127\.0\.0\.1)[^\s`'\"]+)", done_condition)
    if not match:
        return None
    url = match.group(1)
    try:
        from urllib.request import urlopen
        with urlopen(url, timeout=5) as response:
            ok = 200 <= response.status < 400
    except Exception:
        ok = False
    return {"name": "endpoint-responds", "target": url, "passed": ok}


def _tests_added_check(done_condition: str, changed_files: list[str]) -> dict[str, Any] | None:
    lowered = done_condition.lower()
    if "test" not in lowered or not any(word in lowered for word in ("add", "added", "exist", "cover")):
        return None
    test_files = [
        path for path in changed_files
        if "test" in path.lower() or path.lower().endswith(("_test.go", ".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx"))
    ]
    return {"name": "tests-added", "passed": bool(test_files), "files": test_files}
