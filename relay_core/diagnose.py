"""
Phase 2.5 — Failure Diagnosis
==============================
Runs when a task fails verification. Produces structured guidance for the
executor to try again — without writing code itself.

Diagnose/execute split:
  diagnose_failure() → guidance (Sonnet analytical call)
  executor           → writes the fix (separate Claude/Codex call)

The diagnose prompt is the highest-leverage artifact in the automation brain.
It is stored as a versioned constant so it can be tuned against benchmarks.

Usage (Phase 2.5 — manual mode):
  relay @claude "task" --diagnose-on-fail
    → runs task → if failed → one diagnose call → prints guidance → stops

Usage (Phase 2 — auto-loop, not yet built):
  relay auto "task" --until "condition" --max-retries 3
    → runs task → if failed → diagnose → execute-with-guidance → loop
"""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from relay_core.models import call

# ── Diagnose system prompt (versioned, tune against benchmarks) ────────────

DIAGNOSE_PROMPT_V1 = """You are a failure diagnostician for an automated coding system. You do not write code. You analyze why an automated coding attempt failed and produce structured guidance for a separate executor to try again.

Your role is analytical, not corrective. Another model will write the fix based on your guidance. If you write code, you have failed your job.

## Inputs you will receive

- TASK: what the executor was asked to do
- DONE_CONDITION: how success is measured
- DIFF: what the executor produced (cleaned, no file dumps)
- ERROR: what went wrong (test failure, compile error, lint output, or assertion that done-condition was not met)
- PRIOR_DIAGNOSIS: optional, present on retry — your previous analysis and what guidance was given

## Your job

Read the inputs. Identify the single most likely root cause of failure. Produce guidance that tells the executor what to change in approach, not what to type.

## Output format

Respond with valid JSON only. No prose before or after. Schema:
{
  "root_cause": "string, one sentence, plain English, names the specific mismatch between what was produced and what is required",
  "category": "wrong_approach | incomplete | regression | flaky | misunderstood_task",
  "guidance": "string, 1-3 sentences, instructs the executor on what to change conceptually, contains no code, no line numbers, no variable names from the diff unless naming them is unavoidable",
  "confidence": "float between 0.0 and 1.0, your honest probability that this diagnosis is correct",
  "should_retry": "boolean",
  "escalate_reason": "string or null, required when should_retry is false"
}

## Category definitions

- wrong_approach: the executor solved a different problem than the task asked for, or chose an implementation strategy that cannot satisfy the done-condition
- incomplete: the executor's approach is correct but partial — missing a case, a file, a step
- regression: the executor's change broke something that was working before
- flaky: the failure appears to be in the test or environment, not the code under test
- misunderstood_task: the task description is ambiguous enough that any executor would struggle — the problem is upstream of the executor

## When to set should_retry to false

Set should_retry to false and populate escalate_reason when any of these hold:
1. category is "flaky" — the test or environment is the issue, not something the executor can fix
2. category is "misunderstood_task" — retrying with a clearer task is needed, not another execute attempt
3. confidence is below 0.4 — you are guessing, and guessing burns tokens
4. PRIOR_DIAGNOSIS exists and its root_cause is substantially the same as yours — the loop is stuck and more retries will not help
Otherwise set should_retry to true and escalate_reason to null.

## Guidance rules — read carefully

Guidance is instructions for a human-like agent, not a patch.

Allowed:
- "The function returns Unix timestamps but the test expects ISO 8601 strings. Change the return format."
- "The new endpoint was added but no test was written. Add a test that exercises the success path."
- "The refactor renamed the symbol but did not update its callers. Find and update all call sites."

Not allowed:
- Any code block, in any language
- Specific variable names, line numbers, or file paths copied from the diff (unless naming a file is the only way to be clear)
- "Change X to Y" where Y is a literal value or expression
- Multi-step procedures longer than 3 sentences — if the fix needs a plan, set category to wrong_approach and describe the conceptual shift

If you find yourself wanting to write code in guidance, stop. The executor will write that code. Your job is to point at the right hill, not climb it.

## Confidence calibration

- 0.9+: error message names the exact mismatch, diff clearly shows it, fix direction is unambiguous
- 0.7-0.9: strong inference from error + diff, one likely root cause
- 0.5-0.7: plausible diagnosis but other causes possible
- 0.4-0.5: educated guess
- below 0.4: you do not actually know — set should_retry to false

Do not inflate confidence to keep the loop running. A halted loop with honest diagnosis is more useful than a retried loop with bad guidance.

## Behavior under retry

When PRIOR_DIAGNOSIS is present:
- If your new root_cause matches the prior one: the loop is stuck. Set should_retry to false, escalate_reason should explain that the same diagnosis recurred.
- If your new root_cause is different: the prior fix surfaced a new problem. Proceed normally, but note in root_cause that this is a follow-on failure.
- If your category flips between retries more than once: set category to misunderstood_task and should_retry to false.

## Final reminder

You are not the executor. You are not allowed to fix the code. You read, you diagnose, you point. Another model writes."""

DIAGNOSE_PROMPT_VERSION = "v1"

# ── User message template ──────────────────────────────────────────────────

def _build_user_message(
    task: str,
    done_condition: str,
    diff: str,
    error: str,
    prior_diagnosis: dict | None = None,
) -> str:
    prior_str = json.dumps(prior_diagnosis, indent=2) if prior_diagnosis else "none"
    return f"""TASK:
{task}

DONE_CONDITION:
{done_condition}

DIFF:
{diff[:4000] if diff else 'no diff — no files changed'}

ERROR:
{error[:3000] if error else 'unknown — verification failed with no error output'}

PRIOR_DIAGNOSIS:
{prior_str}"""


# ── Main diagnose function ─────────────────────────────────────────────────

def diagnose_failure(
    task: str,
    done_condition: str,
    diff: str,
    error: str,
    prior_diagnosis: dict | None = None,
    relay_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the diagnose prompt on a failed task.

    Returns a structured diagnosis dict:
    {
        "root_cause": str,
        "category": str,
        "guidance": str,
        "confidence": float,
        "should_retry": bool,
        "escalate_reason": str | None,
        "prompt_version": str,
    }
    """
    user_msg = _build_user_message(task, done_condition, diff, error, prior_diagnosis)

    raw = call(
        "diagnose_failure",
        user_msg,
        system=DIAGNOSE_PROMPT_V1,
        relay_dir=relay_dir,
        use_cache=False,  # never cache diagnoses
    )

    result = _parse_diagnosis(raw)
    result["prompt_version"] = DIAGNOSE_PROMPT_VERSION
    return result


def _parse_diagnosis(raw: str) -> dict[str, Any]:
    """Extract JSON from the model's response."""
    # Try to find JSON block
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            # Validate required fields
            required = {"root_cause", "category", "guidance", "confidence", "should_retry"}
            if required.issubset(d.keys()):
                # Ensure should_retry is bool
                d["should_retry"] = bool(d["should_retry"])
                d.setdefault("escalate_reason", None)
                return d
        except json.JSONDecodeError:
            pass

    # Fallback if parsing fails
    return {
        "root_cause": "Diagnosis parsing failed — raw output could not be parsed as JSON",
        "category": "misunderstood_task",
        "guidance": "The diagnostic model produced unparseable output. Review the task description for ambiguity.",
        "confidence": 0.0,
        "should_retry": False,
        "escalate_reason": "Diagnosis model output could not be parsed. Manual review required.",
    }


# ── Verification (cheap, no LLM) ──────────────────────────────────────────

def verify_task(
    repo_root: Path,
    done_condition: str,
    changed_files: list[str],
) -> dict[str, Any]:
    """Run cheap verification checks before any LLM retry decision.

    Returns:
    {
        "changed": bool,
        "compiles": bool | None,    # None if not checkable
        "tests": {"passed": bool | None, "output": str},
        "done_condition_met": bool | None,
    }
    """
    result: dict[str, Any] = {
        "changed": len(changed_files) > 0,
        "compiles": None,
        "tests": {"passed": None, "output": ""},
        "done_condition_met": None,
    }

    if not changed_files:
        return result

    # Detect test runner from repo root
    test_output = ""
    test_passed = None

    if (repo_root / "package.json").exists():
        r = subprocess.run(
            ["npm", "test", "--", "--passWithNoTests"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=60,
        )
        test_output = r.stdout + r.stderr
        test_passed = r.returncode == 0

    elif (repo_root / "pytest.ini").exists() or (repo_root / "pyproject.toml").exists():
        r = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=120,
        )
        test_output = r.stdout + r.stderr
        test_passed = r.returncode == 0

    elif (repo_root / "go.mod").exists():
        r = subprocess.run(
            ["go", "test", "./..."],
            cwd=str(repo_root), capture_output=True, text=True, timeout=60,
        )
        test_output = r.stdout + r.stderr
        test_passed = r.returncode == 0

    elif (repo_root / "Cargo.toml").exists():
        r = subprocess.run(
            ["cargo", "test"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=120,
        )
        test_output = r.stdout + r.stderr
        test_passed = r.returncode == 0

    result["tests"] = {"passed": test_passed, "output": test_output[:3000]}

    # Simple done-condition grep check (heuristic)
    if done_condition and "passes" not in done_condition.lower():
        keywords = re.findall(r'\b\w{4,}\b', done_condition.lower())
        result["done_condition_met"] = any(
            kw in test_output.lower() for kw in keywords
        ) if test_output else None

    return result
