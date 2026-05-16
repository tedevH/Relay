from __future__ import annotations
import difflib
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relay_core.constants import INSTALL_HINTS, ALL_COMMANDS


def normalize_agent_name(agent: str) -> str:
    return "Claude" if agent == "claude" else "Codex"


def timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True, text=True)


def cli_available(name: str) -> bool:
    return shutil.which(name) is not None


def all_required_dependencies() -> list[tuple[str, bool]]:
    return [(name, cli_available(name)) for name in ("claude", "codex", "git")]


def missing_required_dependencies() -> list[str]:
    return [name for name, ok in all_required_dependencies() if not ok]


def print_install_hints(missing: list[str]) -> None:
    for name in missing:
        label = normalize_agent_name(name) if name in {"claude", "codex"} else "Git"
        print(f"{label} install hint:")
        print(INSTALL_HINTS[name])


def trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def safe_text(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def extract_extensions(text: str) -> list[str]:
    return [match.lower() for match in re.findall(r"\.[a-zA-Z0-9]+", text)]


def tokenize_text(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_./-]+", text.lower()))


def contains_phrase(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower()


def detect_rate_limit(output: str) -> bool:
    from relay_core.constants import RATE_LIMIT_PATTERNS
    lowered = output.lower()
    return any(pattern in lowered for pattern in RATE_LIMIT_PATTERNS)


def fuzzy_match_command(command: str, known: list[str] | None = None) -> str | None:
    candidates = known if known is not None else ALL_COMMANDS
    matches = difflib.get_close_matches(command, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None
