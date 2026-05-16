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


def stream_subprocess(command: list[str], cwd: Path, quiet: bool = False) -> tuple[int, str]:
    """Run a subprocess and stream its output.

    quiet=True: suppress all live output, just capture and return.
                Used for review/audit where output is shown in a panel afterward.
    quiet=False: stream each line live with a spinner shown while waiting.
    """
    import threading
    import time

    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text
    from relay_core.tui import console, stream_line

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
    last_idx = 0
    agent_name = "Claude" if "claude" in command[0] else "Codex"

    with Live(console=console, refresh_per_second=10) as live:
        while not reading_done.is_set() or last_idx < len(output_lines):
            # Drain new lines
            while last_idx < len(output_lines):
                line = output_lines[last_idx].rstrip()
                last_idx += 1
                if not quiet and line:
                    live.console.print(line, highlight=False, markup=False)

            elapsed = int(time.time() - start)
            if not reading_done.is_set():
                live.update(
                    Text(f"  ⏳ {agent_name} is running... {elapsed}s", style="dim")
                )
            else:
                live.update(Text(""))

            if not reading_done.is_set():
                time.sleep(0.1)

    reader.join()
    process.wait()
    return process.returncode, "".join(output_lines)
