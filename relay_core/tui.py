from __future__ import annotations
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich.prompt import Prompt, Confirm
from rich import box

from relay_core.constants import VERSION

console = Console()

AGENT_COLORS = {"claude": "bold cyan", "codex": "bold blue", "Claude": "bold cyan", "Codex": "bold blue"}
RISK_COLORS = {"HIGH": "bold red", "MEDIUM": "bold yellow", "LOW": "bold green"}
STATUS_COLORS = {"ok": "bold green", "failed": "bold red", "success": "bold green"}


def _agent_badge(agent: str) -> Text:
    name = "Claude" if agent.lower() == "claude" else "Codex"
    color = AGENT_COLORS.get(name, "white")
    return Text(f" {name} ", style=f"{color} on grey15")


def show_home(repo: Any, missing_deps: list[str]) -> None:
    from relay_core.utils import cli_available

    title = Text()
    title.append("⚡ Relay ", style="bold white")
    title.append(f"v{VERSION}", style="dim white")

    status_table = Table(box=None, show_header=False, padding=(0, 1))
    status_table.add_column(style="dim white")
    status_table.add_column()

    for dep in ("claude", "codex", "git"):
        ok = cli_available(dep)
        label = {"claude": "Claude Code", "codex": "Codex CLI", "git": "Git"}[dep]
        status_table.add_row(label, Text("✓ installed", style="bold green") if ok else Text("✗ missing", style="bold red"))

    git_row = f"[dim]{repo.repo_root}[/dim]" if repo.in_git_repo else "[dim red]not detected[/dim red]"
    status_table.add_row("Git repo", git_row)

    cmd_table = Table(box=None, show_header=False, padding=(0, 1))
    cmd_table.add_column(style="bold cyan", no_wrap=True)
    cmd_table.add_column(style="dim white")
    commands = [
        ('relay "task"', "auto-route task to Claude or Codex"),
        ("relay chain \"task\"", "design → build → review pipeline"),
        ("relay review  (r)", "review current diff with opposite agent"),
        ("relay summary (s)", "smart diff summary with risk levels"),
        ("relay commit  (c)", "safe commit with confirmation"),
        ("relay push    (p)", "safe push with confirmation"),
        ("relay dashboard", "open web dashboard at localhost:7432"),
        ("relay why \"task\"", "explain routing without running"),
        ("relay scan", "fingerprint project for smarter routing"),
        ("relay audit", "CI-style review with risk exit codes"),
        ("relay history", "view recent task history"),
        ("relay doctor", "check environment and dependencies"),
    ]
    for cmd, desc in commands:
        cmd_table.add_row(cmd, desc)

    console.print()
    console.print(Panel(title, border_style="cyan", padding=(0, 1)))
    console.print(Panel(status_table, title="[bold white]Status[/bold white]", border_style="dim", padding=(0, 1)))
    console.print(Panel(cmd_table, title="[bold white]Commands[/bold white]", border_style="dim", padding=(0, 1)))

    if missing_deps:
        show_install_hints(missing_deps)

    console.print()
    console.print("[dim]Type a task or command below. 'exit' to quit.[/dim]")


def show_doctor(repo: Any, deps: list[tuple[str, bool]], missing: list[str]) -> None:
    table = Table(box=box.ROUNDED, border_style="dim", show_header=True, header_style="bold white")
    table.add_column("Dependency", style="white")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    labels = {"claude": "Claude Code CLI", "codex": "Codex CLI", "git": "Git"}
    for dep, ok in deps:
        status = Text("✓ installed", style="bold green") if ok else Text("✗ missing", style="bold red")
        table.add_row(labels.get(dep, dep), status, "")

    git_repo_row = Text("✓ detected", style="bold green") if repo.in_git_repo else Text("✗ not found", style="dim red")
    details = str(repo.repo_root) if repo.in_git_repo else "Run inside a git repo to use AI tasks"
    table.add_row("Git repo", git_repo_row, details)

    if repo.relay_dir:
        relay_ok = repo.relay_dir.exists()
        table.add_row(".relay dir", Text("✓ present", style="bold green") if relay_ok else Text("— not yet created", style="dim"), "")

    console.print()
    console.print(Panel(table, title="[bold white]⚕  Relay Doctor[/bold white]", border_style="cyan", padding=(0, 1)))

    if missing:
        show_install_hints(missing)
    elif not repo.in_git_repo:
        console.print(Panel("[yellow]Not inside a git repository — AI task workflows require one.[/yellow]", border_style="yellow"))
    else:
        console.print(Panel("[bold green]Environment looks ready.[/bold green]", border_style="green"))


def show_status(repo: Any, tasks: list[dict], rate_limits: dict[str, bool]) -> None:
    from relay_core.utils import cli_available, normalize_agent_name
    from relay_core.memory import latest_task

    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column(style="dim white", width=28)
    table.add_column()

    for dep in ("claude", "codex", "git"):
        ok = cli_available(dep)
        label = {"claude": "Claude Code", "codex": "Codex CLI", "git": "Git"}[dep]
        table.add_row(label, Text("available", style="bold green") if ok else Text("missing", style="bold red"))

    for agent in ("claude", "codex"):
        rl = rate_limits.get(agent, False)
        table.add_row(f"Rate limit ({agent})", Text("yes", style="bold yellow") if rl else Text("no", style="dim green"))

    last = latest_task(tasks)
    if last:
        ct = last.get("command_type", "task")
        success = "success" if last.get("success") else "failed"
        color = "bold green" if last.get("success") else "bold red"
        if ct == "commit":
            desc = f"commit / {success}"
        elif ct == "push":
            desc = f"push / {success}"
        else:
            agent = normalize_agent_name(last.get("selected_agent", "codex"))
            desc = f"{agent} / {success}"
        table.add_row("Last task", Text(desc, style=color))
    else:
        table.add_row("Last task", Text("none", style="dim"))

    console.print()
    console.print(Panel(table, title="[bold white]Status[/bold white]", border_style="cyan", padding=(0, 1)))


def show_why(decision: Any) -> None:
    table = Table(box=box.ROUNDED, border_style="dim", show_header=False, padding=(0, 1))
    table.add_column(style="dim white", width=28)
    table.add_column()

    from relay_core.utils import normalize_agent_name
    agent_text = Text(normalize_agent_name(decision.agent), style=AGENT_COLORS.get(decision.agent, "white"))
    table.add_row("Routes to", agent_text)
    table.add_row("Reason", decision.reason)
    table.add_row("Claude score", str(decision.claude_score))
    table.add_row("Codex score", str(decision.codex_score))
    table.add_row("Claude keywords", ", ".join(decision.matched_claude_keywords) or "none")
    table.add_row("Codex keywords", ", ".join(decision.matched_codex_keywords) or "none")
    table.add_row("Claude hints", ", ".join(decision.matched_claude_hints) or "none")
    table.add_row("Codex hints", ", ".join(decision.matched_codex_hints) or "none")
    table.add_row("Override", decision.manual_override or "none")
    table.add_row(
        "Rate-limit penalty",
        f"Claude -{decision.rate_limit_penalty['claude']}  Codex -{decision.rate_limit_penalty['codex']}",
    )
    table.add_row("Handoff influence", decision.handoff_influence or "none")

    console.print()
    console.print(Panel(table, title="[bold white]Routing Explanation[/bold white]", border_style="cyan", padding=(0, 1)))


def show_routing_decision(decision: Any) -> None:
    from relay_core.utils import normalize_agent_name
    name = normalize_agent_name(decision.agent)
    color = AGENT_COLORS.get(decision.agent, "white")
    matches = decision.matched_claude_keywords + decision.matched_codex_keywords
    matched_str = (", ".join(matches[:6])) if matches else "—"

    lines = Text()
    lines.append(f"Agent  ", style="dim white")
    lines.append(f"{name}\n", style=color)
    lines.append(f"Reason ", style="dim white")
    lines.append(f"{decision.reason}\n", style="white")
    if matched_str != "—":
        lines.append(f"Matched ", style="dim white")
        lines.append(matched_str, style="dim cyan")

    console.print()
    console.print(Panel(lines, title="[bold white]Routing[/bold white]", border_style="cyan", padding=(0, 1)))
    console.print()


def show_agent_running(command: list[str]) -> None:
    console.print(f"[dim]$ {' '.join(command[:3])} ...[/dim]")
    console.print()


def stream_line(line: str) -> None:
    console.print(line, highlight=False, markup=False)


def show_agent_completion_note(agent: str, output: str, exit_code: int) -> None:
    from relay_core.utils import normalize_agent_name
    if output.strip():
        return
    if exit_code == 0:
        console.print(f"\n[dim]{normalize_agent_name(agent)} finished with no terminal output. Check the diff below.[/dim]")
    else:
        console.print(f"\n[bold red]{normalize_agent_name(agent)} exited with code {exit_code} and no output.[/bold red]")


def show_result(agent: str, exit_code: int, files: list[str], prompt_type: str) -> None:
    from relay_core.utils import normalize_agent_name
    name = normalize_agent_name(agent)
    color = AGENT_COLORS.get(agent, "white")
    success = exit_code == 0

    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column(style="dim white", width=14)
    table.add_column()

    table.add_row("Agent", Text(name, style=color))
    table.add_row("Exit code", str(exit_code))
    table.add_row("Success", Text("yes", style="bold green") if success else Text("no", style="bold red"))

    if files:
        for i, f in enumerate(files[:8]):
            table.add_row("Changed" if i == 0 else "", f"[dim]{f}[/dim]")
        if len(files) > 8:
            table.add_row("", f"[dim]... and {len(files) - 8} more[/dim]")
    else:
        table.add_row("Changed", "[dim]none[/dim]")

    border = "green" if success else "red"
    console.print()
    console.print(Panel(table, title="[bold white]Result[/bold white]", border_style=border, padding=(0, 1)))

    next_cmds = ["relay summary", "relay review"] if prompt_type != "review" else ["relay summary"]
    console.print(f"\n[dim]Next: {' · '.join(next_cmds)}[/dim]")


def show_history(tasks: list[dict]) -> None:
    from relay_core.utils import normalize_agent_name
    if not tasks:
        console.print(Panel("[dim]No task history yet.[/dim]", border_style="dim"))
        return

    table = Table(box=box.ROUNDED, border_style="dim", show_header=True, header_style="bold white")
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Agent / Type", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Files", justify="right", style="dim")
    table.add_column("Task / Info")

    for entry in reversed(tasks):
        ts = (entry.get("timestamp") or "")[:19].replace("T", " ")
        ct = entry.get("command_type", "task")
        success = entry.get("success", False)
        status_text = Text("ok", style="bold green") if success else Text("fail", style="bold red")

        if ct == "commit":
            agent_cell = Text("commit", style="dim white")
            info = entry.get("commit_message", "")
        elif ct == "push":
            agent_cell = Text("push", style="dim white")
            info = f"{entry.get('branch', '')} → {entry.get('remote', '')}"
        else:
            ag = entry.get("selected_agent", "codex")
            agent_cell = _agent_badge(ag)
            info = entry.get("original_task", "")

        files_count = str(len(entry.get("changed_files", [])))
        table.add_row(ts, agent_cell, status_text, files_count, info[:70])

    console.print()
    console.print(Panel(table, title="[bold white]Task History[/bold white]", border_style="cyan", padding=(0, 1)))


def show_summary(files: list[str], diff_stat: str, warnings: list[str], commit_msg: str, risk_levels: dict[str, str], contradictions: list[str]) -> None:
    file_table = Table(box=box.SIMPLE, show_header=True, header_style="bold white", border_style="dim")
    file_table.add_column("File")
    file_table.add_column("Risk", justify="center")

    for f in files:
        risk = risk_levels.get(f, "LOW")
        color = RISK_COLORS.get(risk, "white")
        file_table.add_row(f"[dim]{f}[/dim]", Text(risk, style=color))

    console.print()
    console.print(Panel(file_table, title="[bold white]Summary[/bold white]", border_style="cyan", padding=(0, 1)))

    if diff_stat:
        console.print(Panel(f"[dim]{diff_stat}[/dim]", title="[bold white]Diff Stat[/bold white]", border_style="dim", padding=(0, 1)))

    if warnings:
        warn_text = "\n".join(f"[yellow]⚠  {w}[/yellow]" for w in warnings)
        console.print(Panel(warn_text, title="[bold yellow]Warnings[/bold yellow]", border_style="yellow", padding=(0, 1)))

    if contradictions:
        contra_text = "\n".join(f"[red]⚡ {c}[/red]" for c in contradictions)
        console.print(Panel(contra_text, title="[bold red]Contradictions Detected[/bold red]", border_style="red", padding=(0, 1)))

    console.print(Panel(f"[bold cyan]{commit_msg}[/bold cyan]", title="[bold white]Suggested Commit[/bold white]", border_style="dim", padding=(0, 1)))


def show_commit_preview(files: list[str], message: str, warnings: list[str], risk_levels: dict[str, str]) -> None:
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column(style="dim white", width=8)
    table.add_column()

    for i, f in enumerate(files[:15]):
        risk = risk_levels.get(f, "LOW")
        color = RISK_COLORS.get(risk, "dim")
        table.add_row("file" if i == 0 else "", f"[{color}]{f}[/{color}]")
    if len(files) > 15:
        table.add_row("", f"[dim]... and {len(files)-15} more[/dim]")

    table.add_row("", "")
    table.add_row("message", f"[bold cyan]{message}[/bold cyan]")

    border = "yellow" if warnings else "cyan"
    console.print()
    console.print(Panel(table, title="[bold white]Commit Preview[/bold white]", border_style=border, padding=(0, 1)))

    if warnings:
        console.print(Panel("\n".join(f"[yellow]⚠  {w}[/yellow]" for w in warnings), border_style="yellow", padding=(0, 1)))


def show_push_preview(remote: str, branch: str, commit_hash: str, commit_message: str) -> None:
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column(style="dim white", width=12)
    table.add_column()
    table.add_row("remote", f"[dim]{remote}[/dim]")
    table.add_row("branch", f"[bold cyan]{branch}[/bold cyan]")
    table.add_row("commit", f"[dim]{commit_hash}[/dim]  {commit_message}")
    console.print()
    console.print(Panel(table, title="[bold white]Push Preview[/bold white]", border_style="cyan", padding=(0, 1)))


def show_warnings(warnings: list[str]) -> None:
    if not warnings:
        return
    text = "\n".join(f"⚠  {w}" for w in warnings)
    console.print(Panel(f"[yellow]{text}[/yellow]", title="[bold yellow]Warnings[/bold yellow]", border_style="yellow", padding=(0, 1)))


def show_error(message: str) -> None:
    console.print(Panel(f"[bold red]{message}[/bold red]", title="[bold red]Error[/bold red]", border_style="red", padding=(0, 1)))


def show_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def show_success(message: str) -> None:
    console.print(f"[bold green]{message}[/bold green]")


def show_chain_pipeline(steps: list[tuple[str, str]]) -> None:
    text = Text()
    for i, (agent, desc) in enumerate(steps):
        color = AGENT_COLORS.get(agent.lower(), "white")
        text.append(f"Step {i+1}: ", style="bold white")
        text.append(agent, style=color)
        text.append(f" — {desc}")
        if i < len(steps) - 1:
            text.append("\n         ↓\n")
    console.print()
    console.print(Panel(text, title="[bold white]⛓  Chain Pipeline[/bold white]", border_style="cyan", padding=(0, 1)))


def show_chain_step(step_num: int, total: int, agent: str, description: str) -> None:
    color = AGENT_COLORS.get(agent.lower(), "white")
    console.print(f"\n[bold white][ Step {step_num}/{total} ][/bold white] [{color}]{agent}[/{color}] — [dim]{description}[/dim]\n")


def show_decomposition_plan(steps: list[str]) -> None:
    text = Text()
    for i, step in enumerate(steps, 1):
        text.append(f"{i}. ", style="bold cyan")
        text.append(step)
        if i < len(steps):
            text.append("\n")
    console.print()
    console.print(Panel(text, title="[bold white]Task Decomposition[/bold white]", border_style="cyan", padding=(0, 1)))


def show_fingerprint_result(profile: dict) -> None:
    table = Table(box=box.ROUNDED, border_style="dim", show_header=False, padding=(0, 1))
    table.add_column(style="dim white", width=22)
    table.add_column()

    table.add_row("Framework", profile.get("framework", "unknown"))
    table.add_row("Primary language", profile.get("primary_language", "unknown"))
    table.add_row("Total files", str(profile.get("total_files", 0)))
    table.add_row("Frontend ratio", f"{profile.get('frontend_ratio', 0):.0%}")
    table.add_row("Backend ratio", f"{profile.get('backend_ratio', 0):.0%}")

    ext_counts = profile.get("extension_counts", {})
    top_exts = sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    table.add_row("Top extensions", "  ".join(f"{ext}:{n}" for ext, n in top_exts))

    console.print()
    console.print(Panel(table, title="[bold white]Project Fingerprint[/bold white]", border_style="cyan", padding=(0, 1)))


def show_config(config: dict) -> None:
    table = Table(box=box.ROUNDED, border_style="dim", show_header=False, padding=(0, 1))
    table.add_column(style="dim white", width=30)
    table.add_column()

    skip = {"frontend_paths", "backend_paths", "frontend_keywords", "backend_keywords"}
    for key, val in config.items():
        if key in skip:
            continue
        if isinstance(val, dict):
            display = json_inline(val) if val else "[dim]{}[/dim]"
        elif isinstance(val, list):
            display = ", ".join(str(v) for v in val) if val else "[dim]none[/dim]"
        else:
            display = str(val)
        table.add_row(key, display)

    console.print()
    console.print(Panel(table, title="[bold white]Current Config[/bold white]", border_style="cyan", padding=(0, 1)))


def json_inline(d: dict) -> str:
    parts = [f"{k}: {v}" for k, v in list(d.items())[:4]]
    return "  ".join(parts) or "[dim]{}[/dim]"


def show_review_output(agent: str, output: str, exit_code: int) -> None:
    """Display captured review output in a clean panel instead of raw terminal dump."""
    from relay_core.utils import normalize_agent_name
    name = normalize_agent_name(agent)
    color = AGENT_COLORS.get(agent, "white")

    # Strip internal tool noise from Codex (exec lines, file reads, etc.)
    cleaned_lines = []
    skip_prefixes = ("exec\n", "/bin/", "succeeded in", "exited ", " succeeded", " failed")
    in_noise_block = False
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip Codex internal exec/tool output noise
        if any(stripped.startswith(p) for p in ("exec", "/bin/zsh", "/bin/bash", "succeeded in", "exited ")):
            in_noise_block = True
            continue
        if in_noise_block and not stripped.startswith(name) and len(stripped) > 200:
            continue
        in_noise_block = False
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    if not cleaned:
        cleaned = output.strip() or "No review output captured."

    border = "green" if exit_code == 0 else "yellow"
    console.print()
    console.print(Panel(
        cleaned,
        title=f"[{color}]{name} Review Findings[/{color}]",
        border_style=border,
        padding=(1, 2),
    ))


def show_install_hints(missing: list[str]) -> None:
    from relay_core.constants import INSTALL_HINTS
    lines = []
    for name in missing:
        label = "Claude Code" if name == "claude" else ("Codex CLI" if name == "codex" else "Git")
        hint = INSTALL_HINTS.get(name, "")
        lines.append(f"[bold white]{label}:[/bold white]\n[dim]{hint}[/dim]")
    console.print(Panel("\n\n".join(lines), title="[bold yellow]Install Hints[/bold yellow]", border_style="yellow", padding=(0, 1)))


def show_welcome_wizard() -> None:
    console.print()
    console.print(Panel(
        "[bold white]Welcome to Relay![/bold white]\n\n"
        "[dim]First time setup — this takes about 30 seconds.[/dim]",
        border_style="cyan", padding=(1, 2)
    ))


def ask_confirm(message: str) -> bool:
    return Confirm.ask(f"[bold white]{message}[/bold white]", default=False)


def ask_input(prompt_text: str) -> str:
    return Prompt.ask(prompt_text)


def ask_choice(message: str, choices: list[str], default: str) -> str:
    return Prompt.ask(f"[bold white]{message}[/bold white]", choices=choices, default=default)
