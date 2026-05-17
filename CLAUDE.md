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

## Relay Memory

**Read `.relay/context.md` immediately.** It contains:
- The current task
- Relevant symbols and their exact file locations
- Active workstreams and their status
- The specific files most likely to need editing

**Do not explore the codebase broadly.** Go directly to the files listed in `.relay/context.md`.

```
## Relevant repo state
- `_reader` — relay_core/git.py:143 [function]
- `run_agent` — relay_core/git.py:91 [function]

## Active workstreams
**dashboard_ui** (in_progress)
Goal: feat: fully automatic — live counter while AI works, result shown at end
Last: [codex] add a hover animation to the overview stats cards in relay_d

## Likely files
- `relay_core/commands.py`
- `relay_core/git.py`
- `relay_core/tui.py`

## Repo
Framework: python · python
```