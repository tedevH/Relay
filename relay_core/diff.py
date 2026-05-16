from __future__ import annotations
import re
from pathlib import Path
from typing import Any

from relay_core.constants import SENSITIVE_PATH_PATTERNS


def classify_file_risk(path: str) -> str:
    lowered = path.lower()
    if any(pattern in lowered for pattern in SENSITIVE_PATH_PATTERNS):
        return "HIGH"
    if any(x in lowered for x in ("test", "spec", "readme", ".md", "__test__", ".test.", ".spec.")):
        return "LOW"
    if any(x in lowered for x in ("config", ".json", ".yaml", ".yml", ".toml", ".env.")):
        return "MEDIUM"
    return "MEDIUM"


def classify_files_risk(files: list[str]) -> dict[str, str]:
    return {f: classify_file_risk(f) for f in files}


def smart_commit_message(files: list[str], diff_text: str, fallback: str = "") -> str:
    lowered_files = [f.lower() for f in files]
    added_lines = [line[1:].strip().lower() for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")]

    def has_keyword(*words: str) -> bool:
        return any(w in line for w in words for line in added_lines)

    # Detect commit type from diff content and files
    if any("test" in f or "spec" in f for f in lowered_files):
        prefix = "test"
    elif any(f.endswith(".md") or "readme" in f for f in lowered_files):
        prefix = "docs"
    elif any("migration" in f for f in lowered_files):
        prefix = "feat"
    elif has_keyword("fix", "bug", "error", "exception", "crash"):
        prefix = "fix"
    elif has_keyword("refactor", "cleanup", "simplify", "reorganize"):
        prefix = "refactor"
    elif any(f.endswith((".lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")) for f in lowered_files):
        prefix = "chore"
    elif has_keyword("add", "create", "implement", "introduce", "new"):
        prefix = "feat"
    else:
        prefix = "chore"

    # Detect subject from files
    if any("auth" in f for f in lowered_files):
        subject = "update authentication"
    elif any("api" in f for f in lowered_files):
        subject = "update API logic"
    elif any(f.endswith((".css", ".scss")) for f in lowered_files):
        subject = "update styles"
    elif any(f.endswith((".tsx", ".jsx", ".html")) for f in lowered_files):
        subject = "update UI components"
    elif any("migration" in f for f in lowered_files):
        subject = "add database migration"
    elif any("test" in f or "spec" in f for f in lowered_files):
        subject = "add tests"
    elif any(f.endswith(".md") or "readme" in f for f in lowered_files):
        subject = "update documentation"
    elif len(files) == 1:
        stem = Path(files[0]).stem.replace("-", " ").replace("_", " ").strip()
        subject = f"update {stem}" if stem else "update project files"
    else:
        subject = fallback or "update project files"

    return f"{prefix}: {subject}"


def detect_contradictions(files: list[str], diff_text: str) -> list[str]:
    issues: list[str] = []
    source_files = [f for f in files if not any(x in f.lower() for x in ("test", "spec", ".md", "readme"))]
    test_files = [f for f in files if any(x in f.lower() for x in ("test", "spec"))]

    for src in source_files:
        stem = Path(src).stem
        has_test = any(stem.lower() in t.lower() for t in test_files)
        if not has_test and any(src.endswith(ext) for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go")):
            issues.append(f"'{src}' changed but no corresponding test file updated")

    # Detect deleted functions still referenced
    deleted_funcs = re.findall(r"^-\s*(?:def|function|func|fn)\s+(\w+)", diff_text, re.MULTILINE)
    added_refs = set(re.findall(r"^\+.*?(\w+)\s*\(", diff_text, re.MULTILINE))
    for fn in deleted_funcs:
        if fn in added_refs:
            issues.append(f"Function '{fn}' removed but still referenced in diff")

    return issues


def diff_trend(tasks: list[dict[str, Any]]) -> str:
    count = len(tasks)
    if count == 0:
        return "first task"
    if count == 1:
        return "2nd task this session"
    return f"{count + 1} tasks — high activity" if count >= 7 else f"{count + 1}th task this session"
