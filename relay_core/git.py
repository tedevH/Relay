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


def exec_agent(agent: str, prompt: str, cwd: Path) -> None:
    """Launch Claude or Codex in native interactive mode and auto-type the task.

    Uses pty.fork() so the agent gets a real terminal (full streaming UI,
    native look and feel). Watches the output for the agent's input prompt,
    then writes the task automatically — user doesn't have to paste anything.

    Claude prompt indicator: ❯  (U+276F, UTF-8: e2 9d af)
    Codex prompt indicator:  >
    """
    import select
    import sys
    import time

    os.chdir(str(cwd))

    if agent == "claude":
        command = ["claude", "--permission-mode", "acceptEdits"]
        # ❯ is Claude's input caret — signals it's ready for input
        prompt_bytes = "❯".encode("utf-8")
    else:
        command = ["codex"]
        prompt_bytes = b"> "

    try:
        import pty
        pid, master_fd = pty.fork()
    except (ImportError, OSError):
        # PTY not available — fall back to plain exec (user pastes manually)
        os.execvp(command[0], command)
        return  # never reached

    if pid == 0:
        # Child — become the agent
        try:
            os.execvp(command[0], command)
        except Exception:
            os._exit(1)

    # Parent — bridge PTY ↔ real terminal, auto-type task on prompt detection.
    # Do NOT set raw mode — the PTY slave manages its own terminal settings.
    # Raw mode on the parent corrupts output (prints each byte on its own line).
    task_sent = False
    buf = b""

    try:
        while True:
            try:
                r, _, _ = select.select([master_fd, sys.stdin], [], [], 0.05)
            except (ValueError, OSError):
                break

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                    sys.stdout.flush()

                    if not task_sent:
                        buf += data
                        buf = buf[-512:]  # keep only recent bytes
                        if prompt_bytes in buf:
                            time.sleep(0.05)
                            os.write(master_fd, prompt.encode("utf-8") + b"\n")
                            task_sent = True
                            buf = b""
                except OSError:
                    break

            if sys.stdin in r:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if data:
                        os.write(master_fd, data)
                except OSError:
                    break

    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


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
