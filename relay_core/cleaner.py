"""
Phase 0 — Output Hygiene
========================
Strips Codex tool-call boilerplate and file content dumps from raw agent output.

Keep:  errors, stack traces, test results, final agent summary, meaningful diffs
Drop:  file content echoes (sed/cat/nl/head), exec boilerplate, token usage lines,
       intermediate reasoning dumps, repeated input context

Storage contract:
  .relay/last-output-clean.txt  — always written (what everything reads)
  .relay/last-output-raw.txt    — written only with --verbose
"""
from __future__ import annotations
import re
from pathlib import Path


# Commands that are just reading files — drop their output if large
_FILE_READ_RE = re.compile(
    r'\b(sed|cat|nl|head|tail|bat|less|more|open)\b',
    re.IGNORECASE,
)

# Commands whose output is always useful regardless of size
_KEEP_OUTPUT_RE = re.compile(
    r'\b(pytest|jest|npm test|cargo test|go test|rspec|mocha|'
    r'python -m|unittest|make test|make check|git diff|git status)\b',
    re.IGNORECASE,
)

# Lines to always drop
_DROP_LINE_RE = re.compile(
    r'^(Token usage:|To continue this session|Session ID:|workdir:|model:|'
    r'provider:|approval:|sandbox:|reasoning effort:|session id:)',
    re.IGNORECASE,
)

# Role label lines emitted by Codex
_ROLE_LABELS = {'codex', 'user', 'assistant', '--------', ''}


def clean_codex(raw: str) -> str:
    """Produce a clean version of raw Codex output.

    Removes:
    - exec/command/result boilerplate blocks for file-reading commands
    - Token usage and session continuation footers
    - Role label lines (codex, user, --------)
    - File content dumps (large blocks from sed/cat/nl)

    Keeps:
    - Test runner output (pass/fail)
    - Error messages and stack traces
    - Agent's final reasoning and conclusion
    - Diffs and meaningful summaries
    """
    lines = raw.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Drop token usage / session footers
        if _DROP_LINE_RE.match(stripped):
            i += 1
            continue

        # Drop role labels
        if stripped in _ROLE_LABELS:
            i += 1
            continue

        # Handle exec blocks
        if stripped == 'exec':
            i += 1
            # Next line is the command
            if i >= len(lines):
                break
            cmd_line = lines[i].strip()
            i += 1

            # Next line should be "succeeded in Xms:" or "exited X in Xms:"
            if i < len(lines) and re.match(r'(succeeded in \d+|exited \d+ in \d+)', lines[i].strip()):
                result_header = lines[i].strip()
                i += 1

                # Collect the content block that follows
                content_lines: list[str] = []
                while i < len(lines):
                    peek = lines[i].strip()
                    if peek == 'exec' or peek in _ROLE_LABELS:
                        break
                    content_lines.append(lines[i])
                    i += 1

                content = '\n'.join(content_lines).strip()

                # Decide whether to keep this block
                is_file_read = bool(_FILE_READ_RE.search(cmd_line))
                is_test_run = bool(_KEEP_OUTPUT_RE.search(cmd_line))
                has_signal = any(
                    kw in content.lower()
                    for kw in ('error', 'fail', 'traceback', 'exception',
                               'assert', 'pass', 'warning', 'critical')
                )
                is_large = len(content_lines) > 20

                if is_test_run:
                    # Always keep test output
                    out.append(f"$ {cmd_line}")
                    out.append(content)
                elif is_file_read and is_large and not has_signal:
                    # Drop silent file reads — pure noise
                    pass
                elif content and (not is_large or has_signal):
                    # Keep small/meaningful output
                    out.append(content)
            continue

        out.append(line)
        i += 1

    # Collapse 3+ blank lines to 2
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(out))
    return result.strip()


def clean_claude(raw: str) -> str:
    """Claude output is already clean — just strip ANSI and trim.
    Kept as a separate function so the interface is symmetric.
    """
    ansi = re.compile(r'\x1b\[[0-9;]*[mGKHF]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]')
    return ansi.sub('', raw).strip()


def clean_output(agent: str, raw: str) -> str:
    if agent == 'codex':
        return clean_codex(raw)
    return clean_claude(raw)


def save_output(relay_dir: Path, agent: str, raw: str, verbose: bool = False) -> str:
    """Clean raw output, persist both versions, return the clean string."""
    clean = clean_output(agent, raw)
    relay_dir.mkdir(parents=True, exist_ok=True)
    (relay_dir / 'last-output-clean.txt').write_text(clean, encoding='utf-8')
    if verbose:
        (relay_dir / 'last-output-raw.txt').write_text(raw, encoding='utf-8')
    return clean
