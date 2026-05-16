## Relay Memory

This project uses Relay for AI-assisted development tracking.
Read `.relay/context.md` before starting any task — it contains:
- The current task description
- Recent activity on this repo
- Most frequently modified files and their risk levels
- Known risk flags from previous sessions

File structure:
- `relay_core/` — CLI engine (routing, commands, git, memory, TUI)
- `relay_dashboard/` — local web dashboard (Flask + HTML/CSS/JS)
- `relay_ci/` — CI audit tooling
- `.relay/` — local memory (tasks, diffs, context, config)

Always check `.relay/context.md` first. It saves you from exploring files you already know about.
