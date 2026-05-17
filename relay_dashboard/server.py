from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from relay_core.types import RepoState
from relay_core.diff import smart_commit_message
import relay_core.tui as tui

VERSION = "0.1.0"

# Relay subcommands allowed to run from the dashboard terminal
ALLOWED_COMMANDS = {
    "review", "ai-review", "summary", "doctor", "status",
    "history", "scan", "config", "audit", "why", "chain",
}

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _load_json(path: Path | None, default: Any = None) -> Any:
    if not path or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def start_dashboard(repo: RepoState) -> int:
    try:
        from flask import Flask, render_template, jsonify, request, Response, stream_with_context
    except ImportError:
        tui.show_error("Flask not installed. Run: pip install flask")
        return 1

    if not repo.in_git_repo or not repo.relay_dir:
        tui.show_error("Relay dashboard requires running inside a git repository.")
        return 1

    relay_dir = repo.relay_dir
    repo_root = repo.repo_root
    relay_py = Path(__file__).parent.parent / "relay.py"
    port = 7432

    config_path = relay_dir / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            port = cfg.get("dashboard_port", 7432)
        except (json.JSONDecodeError, OSError):
            pass

    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"

    app = Flask(__name__, template_folder=str(templates_dir), static_folder=str(static_dir))
    app.config["RELAY_DIR"] = relay_dir
    app.config["REPO_ROOT"] = repo_root

    # ── Data endpoints ────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html", port=port)

    @app.route("/api/tasks")
    def api_tasks():
        return jsonify(_load_json(relay_dir / "tasks.json", []))

    @app.route("/api/memory")
    def api_memory():
        return jsonify(_load_json(relay_dir / "memory.json", {}))

    @app.route("/api/project")
    def api_project():
        return jsonify(_load_json(relay_dir / "project.json", {}))

    @app.route("/api/version")
    def api_version():
        return jsonify({"version": VERSION})

    @app.route("/api/diff")
    def api_diff():
        diff_path = relay_dir / "last-diff.patch"
        if not diff_path.exists():
            return jsonify({"diff": "", "files": [], "commit_message": ""})
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        files = []
        for line in diff_text.splitlines():
            if line.startswith("diff --git"):
                parts = line.split(" b/")
                if len(parts) > 1:
                    files.append(parts[1].strip())
        try:
            commit_msg = smart_commit_message(files, diff_text) if files else ""
        except Exception:
            commit_msg = ""
        return jsonify({"diff": diff_text[:50000], "files": files, "commit_message": commit_msg})

    @app.route("/api/config")
    def api_config():
        return jsonify(_load_json(relay_dir / "config.json", {}))

    @app.route("/api/runs")
    def api_runs():
        return jsonify(_load_json(relay_dir / "auto-runs.json", []))

    @app.route("/api/costs")
    def api_costs():
        return jsonify(_load_json(relay_dir / "costs.json", []))

    @app.route("/api/rollback", methods=["POST"])
    def api_rollback():
        """Rollback by reverting the last commit on the current branch."""
        try:
            data = request.json or {}
            branch = data.get("branch", "")
            result = subprocess.run(
                ["git", "revert", "--no-commit", "HEAD"],
                cwd=str(repo_root), capture_output=True, text=True,
            )
            if result.returncode != 0:
                return jsonify({"ok": False, "error": result.stderr.strip()})
            subprocess.run(
                ["git", "commit", "-m", "relay: rollback auto run"],
                cwd=str(repo_root), capture_output=True, text=True,
            )
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    # ── Terminal endpoint — SSE streaming ─────────────────────────

    @app.route("/api/terminal/run")
    def api_terminal_run():
        """Stream relay command output via Server-Sent Events."""
        raw = request.args.get("cmd", "").strip()
        parts = raw.split()

        # Safety: only allow known relay subcommands
        if not parts or parts[0] not in ALLOWED_COMMANDS:
            def _err():
                allowed = ", ".join(sorted(ALLOWED_COMMANDS))
                yield f"data: {json.dumps({'line': f'Error: only these commands are allowed from the dashboard:'})}\n\n"
                yield f"data: {json.dumps({'line': f'  {allowed}'})}\n\n"
                yield f"data: {json.dumps({'done': True, 'exit_code': 1})}\n\n"
            return Response(stream_with_context(_err()), mimetype="text/event-stream")

        # For commands needing a task arg (why, chain) validate non-empty
        needs_arg = {"why", "chain"}
        if parts[0] in needs_arg and len(parts) < 2:
            def _err2():
                yield f"data: {json.dumps({'line': f'Error: {parts[0]} requires a task argument'})}\n\n"
                yield f"data: {json.dumps({'done': True, 'exit_code': 1})}\n\n"
            return Response(stream_with_context(_err2()), mimetype="text/event-stream")

        cmd = [sys.executable, str(relay_py)] + parts
        env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}

        def _generate():
            try:
                process = subprocess.Popen(
                    cmd,
                    cwd=str(repo_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    clean = _strip_ansi(line.rstrip())
                    if clean:
                        yield f"data: {json.dumps({'line': clean})}\n\n"
                process.wait()
                yield f"data: {json.dumps({'done': True, 'exit_code': process.returncode})}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'line': f'Error: {exc}'})}\n\n"
                yield f"data: {json.dumps({'done': True, 'exit_code': 1})}\n\n"

        return Response(
            stream_with_context(_generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ─────────────────────────────────────────────────────────────

    tui.console.print()
    tui.console.print(f"[bold cyan]⚡ Relay Dashboard[/bold cyan]  v{VERSION}  [dim]http://localhost:{port}[/dim]")
    tui.console.print("[dim]Ctrl+C to stop[/dim]")
    tui.console.print()

    webbrowser.open(f"http://localhost:{port}")

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
    return 0
