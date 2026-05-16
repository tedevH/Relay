## Relay — Project Context

**Read `.relay/context.md` first** — it has the current task, recent activity, and hot files.

### Project structure
- `relay_core/` — CLI engine (routing, commands, git ops, memory, TUI)
- `relay_dashboard/templates/index.html` — the entire web dashboard UI
- `relay_dashboard/server.py` — Flask server + API endpoints
- `relay_ci/` — CI audit tooling
- `.relay/` — local memory (tasks.json, context.md, config.json)

### Files to focus on for this task
- `relay_dashboard/server.py`
- `relay_dashboard/__init__.py`
- `relay_dashboard/templates/index.html`

Go directly to these files. Skip broad codebase exploration.