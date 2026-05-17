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
            return json.loads(m.group())
    except Exception:
        pass
    return {"tier": "feature", "reason": "classification failed, defaulting to feature"}


def infer_done_condition(task: str, relay_dir: Path | None = None) -> str:
    """Infer a verifiable done-condition from a task description (Haiku)."""
    prompt = f"""Given this development task, state the single most concrete verifiable done-condition.

Task: {task}

Rules:
- Must be checkable without human judgment (test passes, file exists, endpoint responds, etc.)
- One sentence maximum
- No code, no file paths unless unavoidable

Done-condition:"""

    return call("infer_done_condition", prompt, relay_dir=relay_dir)
