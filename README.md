# Relay

Relay is a terminal-native AI coding-agent router. You type a coding task once, and Relay decides whether to send it to Claude Code or Codex CLI.

Relay is local-first and free to use as an app:

- It does not use API keys.
- It does not run a server.
- It only calls your already-installed local CLI tools as subprocesses.
- It stores only local JSON history in `~/.relay/history.json`.

Relay itself is free. Any limits or costs come from the CLI tools you already use locally.

## Features

- Routes frontend/UI work to Claude by default
- Routes backend/implementation work to Codex by default
- Supports `@claude` and `@codex` overrides
- Streams agent output back to your terminal
- Saves local task history
- Detects likely rate-limit or usage-limit output from the agent CLI
- Includes `history`, `status`, and `why` commands

## Routing Rules

Relay uses simple keyword scoring and file-extension hints.

Claude gets higher scores for tasks that mention things like:

- frontend
- UI / UX
- React
- Next.js
- Tailwind
- CSS
- landing pages
- dashboards
- copy
- layout
- animation
- responsive design
- file hints like `.tsx`, `.jsx`, `.css`, `.scss`, `.html`

Codex gets higher scores for tasks that mention things like:

- backend
- API routes
- databases
- SQL / Postgres
- authentication
- migrations
- tests
- scripts
- bugs
- performance
- security
- cron jobs
- workers
- file hints like `.py`, `.go`, `.rs`, `.sql`, `.java`, `.rb`, `.php`

If scoring is tied, Relay defaults to Codex.

## Requirements

- Python 3.10+
- `claude` installed if you want to route to Claude Code
- `codex` installed if you want to route to Codex CLI

Expected subprocess commands:

- Claude: `claude -p "<task>"`
- Codex: `codex exec "<task>"`

## Install

Clone the repo, make the wrapper executable, and symlink it into your PATH:

```bash
chmod +x relay
mkdir -p "$HOME/.local/bin"
ln -sf "$(pwd)/relay" "$HOME/.local/bin/relay"
export PATH="$HOME/.local/bin:$PATH"
```

After that, `relay` is available as a normal command.

You can also run it directly without installing:

```bash
./relay status
```

## Usage

```bash
relay "make the dashboard mobile responsive"
relay "add a users API route"
relay @claude "redesign the homepage hero"
relay @codex "write tests for the auth route"
relay why "add a users API route"
relay history
relay status
```

## Commands

### `relay "<task>"`

Routes automatically, prints the selected agent, then runs the CLI.

Example:

```bash
relay "make the dashboard mobile responsive"
```

Output starts with:

```text
Routing to: Claude
```

### `relay @claude "<task>"`

Forces Claude Code.

### `relay @codex "<task>"`

Forces Codex CLI.

### `relay why "<task>"`

Explains which agent Relay would choose and why, without running anything.

### `relay history`

Prints recent local task history from `~/.relay/history.json`.

Each entry includes:

- timestamp
- original task
- selected agent
- success/failure
- exit code
- whether a likely rate limit was detected

### `relay status`

Shows:

- whether Claude is available
- whether Codex is available
- whether recent Claude history suggests a rate limit
- whether recent Codex history suggests a rate limit

## Error Handling

Relay handles these basic cases:

- selected CLI is not installed
- selected CLI exits with a non-zero code
- CLI output appears to contain rate-limit, usage-limit, or quota-exceeded text

## Data Storage

Relay writes local history to:

```text
~/.relay/history.json
```

If you want to override the storage location, set `RELAY_HOME`.

No database is used.

## Project Structure

```text
relay.py
README.md
relay
```

## Notes

- This is an MVP CLI, not a hosted product.
- The routing logic is intentionally simple and easy to edit later.
- Relay never asks for or stores API keys.
