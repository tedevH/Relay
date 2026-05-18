"""
Phase 0.5 — Model Routing Layer
================================
Every LLM call in Relay passes through this module.
No code elsewhere invokes a model directly.

Routing philosophy:
  Haiku   — classification, tagging, short inference (< $0.001 per call)
  Sonnet  — analysis, planning, diagnosis (~$0.003-0.015 per call)
  Opus    — hard generation, architectural decisions (~$0.075+ per call)
  Codex   — trivial execution (uses OpenAI credits)

Override via .relay/models.toml in any repo. Falls back to defaults.
"""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

# ── Default routing table ──────────────────────────────────────────────────

ROUTING: dict[str, str] = {
    # Classification — Haiku (cheap, fast)
    "classify_task":        "claude-haiku-4-5",
    "tag_workstream":       "claude-haiku-4-5",
    "infer_done_condition": "claude-haiku-4-5",
    "detect_success":       "claude-haiku-4-5",
    "summarize_output":     "claude-haiku-4-5",

    # Analysis — Sonnet (reasoning)
    "plan_decompose":       "claude-sonnet-4-5",
    "diagnose_failure":     "claude-sonnet-4-5",
    "review_diff":          "claude-sonnet-4-5",

    # Execution tiers — determined by task classifier
    "execute_trivial":      "codex",
    "execute_feature":      "claude-sonnet-4-5",
    "execute_architectural":"claude-opus-4-5",
}

# ── Cost tracking (per-session in-memory, flushed to .relay/costs.json) ───

_session_costs: dict[str, float] = {}

# Approximate cost per 1k tokens (input+output blended) in USD
_COST_PER_1K: dict[str, float] = {
    "claude-haiku-4-5":   0.00025,
    "claude-sonnet-4-5":  0.003,
    "claude-opus-4-5":    0.075,
    "codex":              0.002,
}

# ── Classification cache (hash-keyed, avoids redundant Haiku calls) ───────

_classify_cache: dict[str, Any] = {}


def _cache_key(job: str, prompt: str) -> str:
    return hashlib.sha256(f"{job}:{prompt}".encode()).hexdigest()[:16]


def _persistent_cache_path(relay_dir: Path | None) -> Path | None:
    return relay_dir / "model-cache.json" if relay_dir else None


def _load_persistent_cache(relay_dir: Path | None) -> dict[str, Any]:
    path = _persistent_cache_path(relay_dir)
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_persistent_cache(relay_dir: Path | None, cache: dict[str, Any]) -> None:
    path = _persistent_cache_path(relay_dir)
    if not path:
        return
    path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")


def _repo_fingerprint(relay_dir: Path | None) -> str:
    if not relay_dir:
        return "global"
    repo_root = relay_dir.parent
    markers = [
        "package.json", "pyproject.toml", "go.mod", "Cargo.toml",
        "requirements.txt", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
    ]
    parts: list[str] = []
    for marker in markers:
        path = repo_root / marker
        if path.exists():
            stat = path.stat()
            parts.append(f"{marker}:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16] if parts else repo_root.name


# ── Model loader (respects per-repo override) ─────────────────────────────

def _load_routing(relay_dir: Path | None = None) -> dict[str, str]:
    routing = dict(ROUTING)
    if relay_dir:
        toml_path = relay_dir / "models.toml"
        if toml_path.exists():
            try:
                import tomllib  # Python 3.11+
                with open(toml_path, "rb") as f:
                    overrides = tomllib.load(f).get("routing", {})
                routing.update(overrides)
            except ImportError:
                pass  # tomllib not available — use defaults
    return routing


# ── Core call function ─────────────────────────────────────────────────────

def call(
    job: str,
    prompt: str,
    system: str = "",
    relay_dir: Path | None = None,
    use_cache: bool = True,
) -> str:
    """Route a job to the appropriate model and return the response.

    All LLM calls in Relay go through here. Never call claude/codex directly.
    """
    routing = _load_routing(relay_dir)
    model = routing.get(job, "claude-sonnet-4-5")

    # Check classification cache for cheap jobs
    cache_key = _cache_key(job, prompt)
    if use_cache and job in {
        "classify_task", "tag_workstream", "infer_done_condition"
    }:
        if cache_key in _classify_cache:
            return _classify_cache[cache_key]

    result = _dispatch(model, prompt, system)

    # Cache and track cost
    if use_cache:
        _classify_cache[cache_key] = result
    _track_cost(job, model, prompt, result)

    return result


def _dispatch(model: str, prompt: str, system: str) -> str:
    """Send prompt to the appropriate CLI."""
    if model == "codex":
        cmd = [
            "codex", "--ask-for-approval", "never",
            "exec", "--sandbox", "workspace-write", prompt,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout.strip()

    # Claude CLI
    cmd = ["claude", "-p", prompt, "--model", model]
    if system:
        cmd += ["--system-prompt", system]

    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip()


def _track_cost(job: str, model: str, prompt: str, response: str) -> None:
    # Rough token estimate: 4 chars ≈ 1 token
    tokens = (len(prompt) + len(response)) / 4 / 1000
    cost = tokens * _COST_PER_1K.get(model, 0.003)
    _session_costs[job] = _session_costs.get(job, 0.0) + cost


def session_cost() -> float:
    return sum(_session_costs.values())


def cost_report() -> dict[str, float]:
    return dict(_session_costs)


def flush_costs(relay_dir: Path) -> None:
    """Append session costs to .relay/costs.json."""
    costs_path = relay_dir / "costs.json"
    history: list[dict] = []
    if costs_path.exists():
        try:
            history = json.loads(costs_path.read_text())
        except Exception:
            pass

    history.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_usd": round(session_cost(), 6),
        "by_job": {k: round(v, 6) for k, v in _session_costs.items()},
    })
    costs_path.write_text(json.dumps(history[-100:], indent=2))  # keep last 100


# ── Task classification (Haiku) ────────────────────────────────────────────

def classify_task(task: str, relay_dir: Path | None = None) -> dict[str, str]:
    """Classify a task into a tier for model routing.

    Returns: {"tier": "trivial|feature|architectural", "reason": "..."}
    """
    heuristic = _classify_task_heuristic(task)
    if heuristic is not None:
        return heuristic

    cache = _load_persistent_cache(relay_dir)
    cache_key = f"classify_task:{_repo_fingerprint(relay_dir)}:{task.strip().lower()}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("tier"):
        return cached

    prompt = f"""Classify this development task into exactly one tier.

Task: {task}

Tiers:
- trivial: single-line change, rename, typo fix, comment update, adding one import
- feature: new function/endpoint/component, bug fix requiring logic change, test addition
- architectural: cross-cutting refactor, database schema change, new system/module

Respond with JSON only:
{{"tier": "trivial|feature|architectural", "reason": "one sentence"}}"""

    raw = call("classify_task", prompt, relay_dir=relay_dir)
    try:
        # Extract JSON even if surrounded by text
        import re
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            cache[cache_key] = result
            _save_persistent_cache(relay_dir, cache)
            return result
    except Exception:
        pass
    return {"tier": "feature", "reason": "classification failed, defaulting to feature"}


def infer_done_condition(task: str, relay_dir: Path | None = None) -> str:
    """Infer a verifiable done-condition from a task description (Haiku)."""
    cache = _load_persistent_cache(relay_dir)
    cache_key = f"infer_done_condition:{_repo_fingerprint(relay_dir)}:{task.strip().lower()}"
    cached = cache.get(cache_key)
    if isinstance(cached, str) and cached.strip():
        return cached

    prompt = f"""Given this development task, state the single most concrete verifiable done-condition.

Task: {task}

Rules:
- Must be checkable without human judgment (test passes, file exists, endpoint responds, etc.)
- One sentence maximum
- No code, no file paths unless unavoidable

Done-condition:"""

    result = call("infer_done_condition", prompt, relay_dir=relay_dir)
    cache[cache_key] = result
    _save_persistent_cache(relay_dir, cache)
    return result


def _classify_task_heuristic(task: str) -> dict[str, str] | None:
    lowered = task.lower()
    trivial_keywords = (
        "rename", "typo", "spelling", "docs", "documentation", "readme",
        "comment", "comments", "format", "formatting", "lint", "config",
        "version bump", "bump version", "change config", "update docs",
    )
    architectural_keywords = (
        "refactor", "architecture", "cross-cutting", "schema", "migration",
        "new module", "new system", "restructure", "platform",
    )

    if any(keyword in lowered for keyword in trivial_keywords):
        return {"tier": "trivial", "reason": "matched a local trivial-task heuristic"}
    if any(keyword in lowered for keyword in architectural_keywords):
        return {"tier": "architectural", "reason": "matched a local architectural-task heuristic"}
    words = re.findall(r"\w+", lowered)
    if len(words) <= 4 and any(word in lowered for word in ("fix", "add", "change", "update")):
        return {"tier": "trivial", "reason": "short task with a narrowly scoped action verb"}
    if len(words) <= 2:
        return {"tier": "trivial", "reason": "very short task; defaulting to cheap local classification"}
    return None
