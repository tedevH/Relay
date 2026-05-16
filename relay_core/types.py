from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


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

    @property
    def project_path(self) -> Path | None:
        return self.relay_dir / "project.json" if self.relay_dir else None

    @property
    def memory_path(self) -> Path | None:
        return self.relay_dir / "memory.json" if self.relay_dir else None


class RelayError(RuntimeError):
    pass
