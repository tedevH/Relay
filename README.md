# Relay

Relay is a local-first terminal workflow layer for Claude Code, Codex CLI, and Git.

You give Relay one task. Relay decides whether Claude or Codex should handle it, runs the selected local CLI, saves compact handoff memory, tracks the git diff, and gives you review and summary commands afterward.

Relay is free to use as an app:

- No web app
- No backend
- No database
- No cloud sync
- No API keys
- No auto-commit
- No auto-push

Relay only shells out to software you already have installed locally.

## What Relay Does

- Routes frontend/UI/design work to Claude by default
- Routes backend/API/logic/test work to Codex by default
- Supports manual overrides with `@claude` and `@codex`
- Refuses to run AI tasks unless `claude`, `codex`, and `git` are all installed
- Saves repo-local memory inside `.relay/`
- Tracks the current git diff after each run
- Lets you continue work with compact handoff context
- Lets you review changes with the opposite agent
- Summarizes the current diff locally

## Requirements

- Python 3.10+
- Claude Code CLI available as `claude`
- Codex CLI available as `codex`
- Git available as `git`

Relay uses these local subprocess commands:

- Claude: `claude -p "<task>"`
- Codex: `codex exec "<task>"`

If any required dependency is missing, Relay still allows:

- `relay`
- `relay doctor`
- `relay status`
- `relay why "task"`
- `relay history`

But it refuses to run live AI task workflows.

## Install

Clone the repo, make the wrapper executable, and symlink it into your PATH:

```bash
chmod +x relay
mkdir -p "$HOME/.local/bin"
ln -sf "$(pwd)/relay" "$HOME/.local/bin/relay"
export PATH="$HOME/.local/bin:$PATH"
```

You can also run it directly:

```bash
./relay
```

## Repo Memory

When you use Relay inside a git repo, it creates:

```text
.relay/
  tasks.json
  handoff.md
  decisions.md
  last-diff.patch
  config.json
```

Relay reads these optional files if you already use them in your project:

- `AGENTS.md`
- `CLAUDE.md`

This MVP does not create or modify those files on its own.

## Commands

### `relay`

Shows a friendly setup and status screen:

- Relay version
- dependency status
- git repo detection
- available commands
- install hints for missing tools

### `relay doctor`

Runs environment diagnostics:

- checks `claude`, `codex`, and `git`
- checks the current working directory
- checks git repo status
- checks whether `.relay` exists
- checks whether task history is writable

Install hints used by `doctor`:

Claude Code:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Codex CLI:

```bash
npm i -g @openai/codex
codex
```

Git:

```text
https://git-scm.com/install
```

### `relay status`

Shows a compact status summary:

- Claude available or missing
- Codex available or missing
- Git available or missing
- recent Claude rate-limit detected yes/no
- recent Codex rate-limit detected yes/no
- last task agent/status if available

### `relay why "task"`

Dry-run routing explanation without running any agent.

Shows:

- selected agent
- routing reason
- Claude score
- Codex score
- matched keywords
- matched file/path hints
- manual override if present
- recent rate-limit penalty
- previous handoff influence

### `relay "task"`

Main workflow:

- validates required dependencies
- initializes `.relay/`
- routes the task
- runs the selected CLI
- streams output live
- saves history
- saves handoff
- saves `git diff` to `.relay/last-diff.patch`
- prints a concise result summary

### `relay @claude "task"`

Forces Claude:

```bash
claude -p "<task>"
```

### `relay @codex "task"`

Forces Codex:

```bash
codex exec "<task>"
```

### `relay continue "task"`

Builds a compact continuation prompt from:

- your new task
- latest `.relay/handoff.md`
- changed files
- current git diff summary

Then routes and runs the next step.

### `relay review`

Reviews the current git diff.

- requires git and a repo
- uses the opposite agent from the previous task when possible
- streams concise review findings

### `relay summary`

Summarizes the current git diff locally.

Shows:

- changed files
- what changed
- potential risks
- suggested commit message

### `relay history`

Shows recent local Relay tasks from `.relay/tasks.json`.

Each task entry includes:

- timestamp
- task
- agent
- success/failure
- changed files count
- rate-limit detection

## Routing Rules

Relay uses keyword scoring and file/path hints.

Claude signals include:

- frontend
- UI / UX
- React
- Next.js
- component(s)
- Tailwind
- CSS / HTML
- landing page
- dashboard
- copy
- animation
- responsive
- layout
- design
- hero
- navbar
- pricing
- form styling
- hints like `.tsx`, `.jsx`, `.css`, `.scss`, `.html`, `components/`, `styles/`, `public/`

Codex signals include:

- backend
- API
- route
- server
- database
- SQL / Postgres / Supabase
- auth
- migration
- schema
- test(s)
- bug
- error
- performance
- security
- script
- worker
- cron
- queue
- endpoint
- validation
- hints like `.py`, `.go`, `.rs`, `.sql`, `.java`, `.rb`, `.php`, `api/`, `server/`, `db/`, `migrations/`, `tests/`

If scores tie, Relay defaults to Codex unless the task contains especially clear UI/design language.

## Safety

Relay will not:

- ask for API keys
- auto-commit
- auto-push
- modify `.env` files by itself

Relay warns when diffs touch risky areas like:

- `.env`
- lockfiles
- migrations
- auth files
- Stripe/payment files
- very large diffs

## Example Workflow

```bash
relay
relay doctor
relay "make the dashboard mobile responsive"
relay review
relay summary
relay continue "clean up the follow-up issues"
relay history
```

## Troubleshooting

If `relay status` says a tool is missing, check that it runs directly in your terminal:

```bash
claude -p "say hello"
codex exec "say hello"
git status
```

If those do not work directly, Relay cannot use them yet.

## Future Monetization

A future Relay Pro could add licensing, richer memory controls, or convenience workflow features, but this MVP stays local-first and free.
