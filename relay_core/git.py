from __future__ import annotations
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


def build_agent_command(agent: str, prompt: str, interactive: bool = False) -> tuple[list[str], str]:
    """Return (command, stdin_prompt).

    interactive=True (normal tasks via PTY):
      Claude: run without -p so it streams tokens live; prompt sent via stdin.
      Codex:  prompt stays as a CLI arg (Codex always streams its own UI).

    interactive=False (review/audit quiet capture):
      Both agents use -p / arg prompt so output can be captured and processed.
    """
    if agent == "claude":
        if interactive:
            return ["claude", "--permission-mode", "acceptEdits"], prompt
        return ["claude", "--permission-mode", "acceptEdits", "-p", prompt], ""
    # Codex takes the prompt as a positional arg regardless
    return ["codex", "--ask-for-approval", "never", "exec", "--sandbox", "workspace-write", prompt], ""


def _run_with_pty(command: list[str], cwd: Path, stdin_prompt: str = "") -> int:
    """Run command inside a PTY so the agent sees a real interactive terminal.

    stdin_prompt: if set, written to the agent's stdin once it has started up.
    This is how Claude receives its task in interactive (non -p) mode, which
    makes it stream tokens live word-by-word as they arrive from the API.
    """
    import os
    import select
    import sys
    import termios
    import time
    import tty

    try:
        import pty
        pid, master_fd = pty.fork()
    except (ImportError, OSError):
        # PTY unavailable (e.g. Windows) — fall back to plain run
        result = subprocess.run(command, cwd=str(cwd))
        return result.returncode

    if pid == 0:
        # Child process — become the agent
        try:
            os.chdir(str(cwd))
            os.execvp(command[0], command)
        except Exception:
            os._exit(1)

    # Parent process — bridge PTY ↔ our real terminal
    old_settings = None
    try:
        old_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
    except Exception:
        pass

    prompt_sent = not bool(stdin_prompt)
    prompt_bytes = (stdin_prompt.strip() + "\n").encode() if stdin_prompt else b""
    bytes_received = 0

    try:
        while True:
            try:
                r, _, _ = select.select([master_fd, sys.stdin], [], [], 0.05)
            except (ValueError, OSError):
                break

            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                    if data:
                        os.write(sys.stdout.fileno(), data)
                        sys.stdout.flush()
                        bytes_received += len(data)
                        # Once Claude has printed its startup banner, send the task.
                        # We wait for >50 bytes so Claude is fully ready to accept input.
                        if not prompt_sent and bytes_received > 50:
                            time.sleep(0.05)
                            os.write(master_fd, prompt_bytes)
                            prompt_sent = True
                except OSError:
                    break  # child exited

            if sys.stdin in r:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if data:
                        os.write(master_fd, data)
                except OSError:
                    break

    finally:
        if old_settings:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            except Exception:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    try:
        _, status = os.waitpid(pid, 0)
        return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
    except ChildProcessError:
        return 0


def stream_subprocess(command: list[str], cwd: Path, quiet: bool = False, **kwargs: str) -> tuple[int, str]:
    """Run a subprocess.

    quiet=False (default, normal tasks):
        Runs the agent with full terminal control — no capturing, no piping.
        Claude Code and Codex show their native UI exactly as if you ran them
        directly: live thinking steps, tool calls, spinners, everything.
        Output is NOT captured (we read the git diff afterward instead).

    quiet=True (review/audit):
        Captures output silently and returns it for display in a Rich panel.
        Used when we need to process and reformat the output.
    """
    if not quiet:
        # Run inside a PTY so the agent sees a real interactive terminal.
        # stdin_prompt is the task text for Claude interactive mode (no -p).
        stdin_prompt = kwargs.get("stdin_prompt", "")
        return _run_with_pty(command, cwd, stdin_prompt=stdin_prompt), ""

    # quiet=True: capture output for panel display (review / audit)
    import threading
    import time
    from rich.live import Live
    from rich.text import Text
    from relay_core.tui import console

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
    agent_name = "Claude" if "claude" in command[0] else "Codex"

    with Live(console=console, refresh_per_second=10) as live:
        while not reading_done.is_set():
            elapsed = int(time.time() - start)
            live.update(Text(f"  ⏳ {agent_name} is thinking... {elapsed}s", style="dim"))
            time.sleep(0.1)
        live.update(Text(""))

    reader.join()
    process.wait()
    return process.returncode, "".join(output_lines)
