from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path

from relay_core.types import RepoState
from relay_core.utils import run_command


def get_repo_state() -> RepoState:
    cwd = Path.cwd()
    if shutil.which("git") is None:
        return RepoState(cwd=cwd, repo_root=None)
    result = run_command(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if result.returncode != 0:
        return RepoState(cwd=cwd, repo_root=None)
    return RepoState(cwd=cwd, repo_root=Path(result.stdout.strip()))


def ensure_local_git_exclude(repo: RepoState) -> None:
    if not repo.repo_root:
        return
    exclude_path = repo.repo_root / ".git" / "info" / "exclude"
    if not exclude_path.exists():
        return
    existing_lines = exclude_path.read_text(encoding="utf-8").splitlines()
    if any(line.strip() == ".relay/" for line in existing_lines):
        return
    updated = existing_lines + [".relay/"]
    exclude_path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def git_output(repo: RepoState, *args: str) -> str:
    if not repo.repo_root:
        return ""
    result = run_command(["git", *args], cwd=repo.repo_root)
    if result.returncode != 0:
        return ""
    return result.stdout


def current_diff(repo: RepoState) -> str:
    return git_output(repo, "diff", "--binary")


def changed_files(repo: RepoState) -> list[str]:
    output = git_output(repo, "diff", "--name-only")
    return [line.strip() for line in output.splitlines() if line.strip()]


def git_status_lines(repo: RepoState) -> list[str]:
    output = git_output(repo, "status", "--short")
    return [line.rstrip() for line in output.splitlines() if line.strip()]


def status_changed_files(repo: RepoState) -> list[str]:
    files: list[str] = []
    for line in git_status_lines(repo):
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip())
    return files


def diff_summary(repo: RepoState) -> str:
    return git_output(repo, "diff", "--stat").strip()


def current_branch(repo: RepoState) -> str:
    return git_output(repo, "branch", "--show-current").strip()


def remote_origin_url(repo: RepoState) -> str:
    return git_output(repo, "remote", "get-url", "origin").strip()


def latest_commit_hash(repo: RepoState) -> str:
    return git_output(repo, "rev-parse", "--short", "HEAD").strip()


def latest_commit_message(repo: RepoState) -> str:
    return git_output(repo, "log", "-1", "--pretty=%s").strip()


def has_uncommitted_changes(repo: RepoState) -> bool:
    return bool(git_status_lines(repo))


def run_agent(agent: str, prompt: str, cwd: Path, relay_dir: Path | None = None) -> int:
    """Run Claude or Codex with full native terminal, then return to Relay.

    Uses subprocess.run (not execvp) so Relay gets control back after the
    agent exits — allowing session ID capture and post-run bookkeeping.

    Claude: uses --continue to automatically resume the last session in this
            directory. No session ID tracking needed.
    Codex:  saves the session ID printed on exit and uses 'codex resume <id>'
            on the next run for full context continuity.

    Task is copied to clipboard — user presses Cmd+V to paste.
    """
    import re as _re

    # Copy task to clipboard
    try:
        subprocess.run(["pbcopy"], input=prompt.encode(), check=False)
    except FileNotFoundError:
        try:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=prompt.encode(), check=False)
        except FileNotFoundError:
            pass

    os.chdir(str(cwd))

    if agent == "claude":
        # --continue resumes the most recent Claude session in this directory
        # automatically — no session ID needed, full context preserved
        result = subprocess.run(
            ["claude", "--permission-mode", "acceptEdits", "--continue"],
        )
        return result.returncode

    else:
        # Codex: use exec mode so it auto-exits when the task is done.
        # Resume previous session if available so it has prior context.
        # exec mode = non-interactive, runs task and exits cleanly.
        session_id = _load_codex_session(relay_dir)

        if session_id:
            # Resume session context + exec mode for auto-exit
            command = [
                "codex", "resume", session_id,
                "--ask-for-approval", "never",
                "exec", "--sandbox", "workspace-write", prompt,
            ]
        else:
            # Fresh session in exec mode
            command = [
                "codex",
                "--ask-for-approval", "never",
                "exec", "--sandbox", "workspace-write", prompt,
            ]

        # Record output to capture new session ID for next run
        import tempfile, platform
        session_log = None
        if platform.system() in ("Darwin", "Linux"):
            try:
                tf = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
                session_log = tf.name
                tf.close()
                result = subprocess.run(["script", "-q", session_log] + command)
            except Exception:
                session_log = None
                result = subprocess.run(command)
        else:
            result = subprocess.run(command)

        # Save new session ID for next run
        if session_log and relay_dir:
            try:
                with open(session_log, "rb") as f:
                    content = f.read().decode("utf-8", errors="replace")
                m = _re.search(r"codex resume ([a-f0-9-]{36})", content)
                if m:
                    _save_codex_session(relay_dir, m.group(1))
            except Exception:
                pass
            try:
                os.unlink(session_log)
            except Exception:
                pass

        return result.returncode


def _load_codex_session(relay_dir: Path | None) -> str | None:
    if not relay_dir:
        return None
    p = relay_dir / "codex-session.json"
    if not p.exists():
        return None
    try:
        import json as _j
        return _j.loads(p.read_text())["session_id"]
    except Exception:
        return None


def _save_codex_session(relay_dir: Path, session_id: str) -> None:
    import json as _j
    p = relay_dir / "codex-session.json"
    p.write_text(_j.dumps({"session_id": session_id}, indent=2))


# Keep exec_agent as a thin alias for backward compat
def exec_agent(agent: str, prompt: str, cwd: Path, relay_dir: Path | None = None) -> None:
    run_agent(agent, prompt, cwd, relay_dir=relay_dir)


def capture_agent_output(agent: str, prompt: str, cwd: Path) -> tuple[int, str]:
    """Run agent and capture output — used for review/audit where we process output.
    Shows a live spinner while waiting. This is the ONLY place agents are run
    without full terminal handoff (review/audit need to read and reformat output).
    """
    import threading
    import time
    from rich.live import Live
    from rich.text import Text
    from relay_core.tui import console

    if agent == "claude":
        command = ["claude", "--permission-mode", "acceptEdits", "-p", prompt]
    else:
        command = ["codex", "--ask-for-approval", "never", "exec",
                   "--sandbox", "workspace-write", prompt]

    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None

    output_lines: list[str] = []
    reading_done = threading.Event()

    def _reader() -> None:
        for line in process.stdout:  # type: ignore[union-attr]
            output_lines.append(line)
        reading_done.set()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    start = time.time()
    agent_name = "Claude" if agent == "claude" else "Codex"

    with Live(console=console, refresh_per_second=10) as live:
        while not reading_done.is_set():
            elapsed = int(time.time() - start)
            live.update(Text(f"  ⏳ {agent_name} is thinking... {elapsed}s", style="dim"))
            time.sleep(0.1)
        live.update(Text(""))

    reader.join()
    process.wait()
    return process.returncode, "".join(output_lines)
