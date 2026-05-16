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


def build_agent_command(agent: str, prompt: str) -> list[str]:
    if agent == "claude":
        return ["claude", "--permission-mode", "acceptEdits", "-p", prompt]
    return ["codex", "--ask-for-approval", "never", "exec", "--sandbox", "workspace-write", prompt]


def _run_with_pty(command: list[str], cwd: Path) -> int:
    """Run command inside a pseudo-terminal so it gets a real interactive TTY.

    Without a PTY, Claude Code detects it's not in a terminal and switches to
    plain print mode — no thinking steps, no tool calls, output only at the end.
    With a PTY it behaves exactly as if the user typed the command themselves.
    """
    import os
    import select
    import sys
    import termios
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


def stream_subprocess(command: list[str], cwd: Path, quiet: bool = False) -> tuple[int, str]:
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
        # This is what makes Claude Code show its native thinking UI —
        # live tool calls, file reads, spinners — exactly as if run directly.
        return _run_with_pty(command, cwd), ""

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
