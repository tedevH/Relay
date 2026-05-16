from __future__ import annotations
import json
from typing import Any

from relay_core.types import RepoState
from relay_core.memory import default_config, ensure_relay_files, save_project_profile
from relay_core.routing import scan_project
import relay_core.tui as tui


def run_first_time_wizard(repo: RepoState) -> dict[str, Any]:
    tui.show_welcome_wizard()

    from relay_core.utils import all_required_dependencies, missing_required_dependencies
    deps = all_required_dependencies()
    missing = missing_required_dependencies()

    tui.show_doctor(repo, deps, missing)

    project_type = tui.ask_choice(
        "What type of project is this?",
        choices=["auto", "frontend", "backend", "fullstack"],
        default="auto",
    )
    default_agent = tui.ask_choice(
        "Preferred default agent?",
        choices=["auto", "claude", "codex"],
        default="auto",
    )
    require_review = tui.ask_confirm("Require review before commit?")

    config = default_config()
    config["project_type"] = project_type
    config["default_agent"] = default_agent
    config["require_review_before_commit"] = require_review

    ensure_relay_files(repo)
    if repo.config_path:
        repo.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    tui.show_info("Scanning project for smarter routing...")
    profile = scan_project(repo)
    if profile:
        tui.show_fingerprint_result(profile)

    tui.show_success("\nRelay is ready. Run 'relay' to start your first task.\n")
    return config
