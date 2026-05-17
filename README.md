# Relay

**Persistent memory and autonomous execution for Claude Code and Codex CLI.**

AI coding agents forget everything when you close the terminal. Relay fixes that - and goes further, running tasks end-to-end without you watching.

Every task you run gets logged. Every commit is tracked. Before every session, Relay injects what your agent needs to know - which files are active, what changed recently, what decisions were made - so it stops starting cold.

```bash
curl -fsSL https://raw.githubusercontent.com/tedevH/Relay/main/install.sh | bash
```

---

## The problem

You open Claude Code or Codex. It reads your entire codebase to understand the project - again. You explain the context - again. It makes the same mistakes it already made last week - because it doesn't remember last week.

This happens on every session, every switch between agents, every time you close the terminal.

## What Relay does

Relay sits between you and your AI agents. It builds a memory of your project over time and feeds it back automatically before every session.

- **Tracks every task and commit** - via a git post-commit hook that logs silently in the background
- **Extracts symbols** - functions, routes, constants with exact file and line numbers
- **Tracks workstreams** - groups related tasks into named feature threads
- **Injects context before every session** - agents read `.relay/context.md` on startup and know your project without exploring it

The result: your agent starts sessions already knowing what changed, what's risky, and what the active workstream is.

Relay also runs fully autonomous loops - give it a goal, it plans, executes, verifies, diagnoses failures, and retries until done. No human involvement between steps.

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/tedevH/Relay/main/install.sh | bash
```

Installs a standalone binary to `~/.local/bin/relay`. No Python install required.

**Requirements:** [Claude Code](https://claude.ai/download) · [Codex CLI](https://github.com/openai/codex) · Git

---

## Quick start

```bash
cd your-project
relay init          # set up memory and git hooks (one time)
relay "your task"   # runs Claude with full project context
relay review        # instant local risk check
relay commit        # safe commit with confirmation
relay push          # push with confirmation
```

---

## Commands

### Daily workflow

| Command | What it does |
|---|---|
| `relay "task"` | Routes to Claude or Codex, injects project context, runs |
| `relay review` | Instant local risk check - files, contradictions, suggested commit message |
| `relay ai-review` | Deep AI review of the current diff |
| `relay summary` | Diff summary with risk levels |
| `relay commit` | Safe commit with smart message suggestion |
| `relay push` | Safe push with confirmation |

### Memory and context

| Command | What it does |
|---|---|
| `relay init` | Set up git hooks and memory for this repo |
| `relay context` | Show what Relay knows about this project |
| `relay digest` | Full project health report |
| `relay history` | Recent task history |

### Autonomous mode

| Command | What it does |
|---|---|
| `relay auto "task" --until "condition"` | Execute → verify → diagnose → retry loop |
| `relay plan "goal"` | Decompose goal into subtasks and execute each |

```bash
# Examples
relay auto "fix the failing auth test" --until "pytest passes"
relay auto "add pagination to the API" --max-retries 3 --max-cost 1.00
relay plan "add user authentication with email and Google OAuth"
```

### Overrides

```bash
relay @claude "task"   # force Claude
relay @codex "task"    # force Codex
relay why "task"       # explain routing without running
relay doctor           # check dependencies
```

---

## How memory works

Running `relay init` installs a git post-commit hook. After every commit - whether you used Claude, Codex, or typed it yourself - Relay silently:

1. Extracts new symbols from the diff (functions, routes, constants)
2. Classifies the active workstream
3. Updates `.relay/memory.json` with hot files and agent stats

Before every `relay "task"`, Relay writes `.relay/context.md` and `CLAUDE.md` with:
- The current task
- Relevant symbols with exact file locations
- Active workstream and its status
- The specific files most likely to need editing

Both Claude Code and Codex read this context on startup and go directly to the relevant files instead of exploring the codebase.

---

## Local memory structure

```
.relay/
  tasks.json        task history
  memory.json       agent stats, hot files
  symbols.json      tracked symbols with file locations
  workstreams.json  active feature threads
  context.md        injected before every session
  last-diff.patch   last saved diff
  config.json       routing and behavior config
```

Memory stays local and is never committed (added to `.git/info/exclude` automatically).

---

## Autonomous loop

`relay auto` closes the verification loop so tasks run end-to-end without human involvement between steps:

```
execute → verify (tests, build) → pass: commit → done
                                → fail: diagnose → execute with guidance → loop
```

Budget cap and retry limit are required flags. Every run creates its own branch and is one-click revertable.

```bash
relay auto "fix the broken login flow" \
  --until "npm test passes" \
  --max-retries 3 \
  --max-cost 1.00
```

---

## Safety

- Never auto-commits without verification passing
- Never auto-pushes unless `--mode pr` is explicitly set
- `relay commit` and `relay push` always ask before running
- No API keys, no cloud sync, no data sent anywhere
- All data stays in `.relay/` inside your repo

---

## License

MIT - [LICENSE](LICENSE)
