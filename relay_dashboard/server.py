from __future__ import annotations
import json
import webbrowser
from pathlib import Path
from typing import Any

from relay_core.types import RepoState
import relay_core.tui as tui


def _load_json(path: Path | None, default: Any = None) -> Any:
    if not path or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def start_dashboard(repo: RepoState) -> int:
    try:
        from flask import Flask, render_template, jsonify, request
    except ImportError:
        tui.show_error("Flask not installed. Run: pip install flask")
        return 1

    if not repo.in_git_repo or not repo.relay_dir:
        tui.show_error("Relay dashboard requires running inside a git repository.")
        return 1

    relay_dir = repo.relay_dir
    port = 7432

    # Try to load port from config
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
    app.config["REPO_ROOT"] = repo.repo_root

    @app.route("/")
    def index():
        return render_template("index.html", port=port)

    @app.route("/api/tasks")
    def api_tasks():
        tasks = _load_json(relay_dir / "tasks.json", [])
        return jsonify(tasks)

    @app.route("/api/memory")
    def api_memory():
        mem = _load_json(relay_dir / "memory.json", {})
        return jsonify(mem)

    @app.route("/api/project")
    def api_project():
        profile = _load_json(relay_dir / "project.json", {})
        return jsonify(profile)

    @app.route("/api/diff")
    def api_diff():
        diff_path = relay_dir / "last-diff.patch"
        if not diff_path.exists():
            return jsonify({"diff": "", "files": []})
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        files = []
        for line in diff_text.splitlines():
            if line.startswith("diff --git"):
                parts = line.split(" b/")
                if len(parts) > 1:
                    files.append(parts[1].strip())
        return jsonify({"diff": diff_text[:50000], "files": files})

    @app.route("/api/config")
    def api_config():
        cfg = _load_json(relay_dir / "config.json", {})
        return jsonify(cfg)

    tui.console.print()
    tui.console.print(f"[bold cyan]⚡ Relay Dashboard[/bold cyan]  [dim]http://localhost:{port}[/dim]")
    tui.console.print("[dim]Ctrl+C to stop[/dim]")
    tui.console.print()

    webbrowser.open(f"http://localhost:{port}")

    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    return 0
