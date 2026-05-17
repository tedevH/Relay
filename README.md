# Relay

Relay is a local-first terminal workflow layer for Claude Code, Codex CLI, and Git.

You give Relay one task. Relay decides whether Claude or Codex should handle it, runs the selected local CLI, saves compact handoff memory, tracks the git diff, and gives you review and summary commands afterward.

Relay is free to use as an app:

- No web app
- No backend
- No database
- No cloud sync
- No API keys
- No hidden auto-commit
- No auto-push

Relay only shells out to software you already have installed locally.

## What Relay Does

- Routes frontend/UI/design work to Claude by default
- Routes backend/API/logic/test work to Codex by default
- Balances tie-breaks and automation retries so Claude and Codex are treated as peers
- Supports manual overrides with `@claude` and `@codex`
- Refuses to run AI tasks unless `claude`, `codex`, and `git` are all installed
- Saves repo-local memory inside `.relay/`
- Tracks the current git diff after each run
- Lets you continue work with compact handoff context
- Lets you review changes with the opposite agent
- Summarizes the current diff locally
- Runs an automation brain loop with `relay auto`: route, execute, verify, diagnose, retry/fallback, checkpoint
- Runs multi-step goals with `relay brain`: plan, execute each step, resume, inspect logs, stop, or roll back
- Lets you commit and push only after explicit confirmation

## Requirements

- Claude Code CLI available as `claude`
- Codex CLI available as `codex`
- Git available as `git`

The standalone Relay installer does not require users to install Python, pipx, or git just to download Relay. Live AI workflows still need Claude Code, Codex CLI, and Git because Relay shells out to those tools.

Relay uses these local subprocess commands:

- Claude: `claude --permission-mode acceptEdits -p "<task>"`
- Codex: `codex --ask-for-approval never exec --sandbox workspace-write "<task>"`

If any required dependency is missing, Relay still allows:

- `relay`
- `relay doctor`
- `relay status`
- `relay why "task"`
- `relay history`

But it refuses to run live AI task workflows.

## Install

Fastest install for macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/tedevH/Relay/main/install.sh | bash
```

The installer downloads the correct standalone binary from the latest GitHub Release and places it in:

```text
~/.local/bin/relay
```

If the repository or release is private, authenticate both the installer fetch and the release download:

```bash
export GITHUB_TOKEN=YOUR_GITHUB_TOKEN
curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://raw.githubusercontent.com/tedevH/Relay/main/install.sh | bash
```

You can choose a different install directory:

```bash
RELAY_INSTALL_DIR=/usr/local/bin curl -fsSL https://raw.githubusercontent.com/tedevH/Relay/main/install.sh | bash
```

Source install for contributors:

```bash
git clone https://github.com/tedevH/Relay.git
cd Relay
python3 -m pip install -e .
relay --version
```

## Publishing Releases

Relay ships as standalone binaries through GitHub Releases.

To publish a release:

```bash
git tag v0.5.2
git push origin v0.5.2
```

The GitHub Actions release workflow builds:

```text
relay-darwin-arm64
relay-linux-amd64
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
  memory.json
  project.json
  brain.json
  runs/
```

Relay reads these optional files if you already use them in your project:

- `AGENTS.md`
- `CLAUDE.md`

This MVP does not create or modify those files on its own.

Relay also adds `.relay/` to the local repo's `.git/info/exclude` so Relay memory stays local and is not accidentally committed.

## Commands

### `relay`

Shows the simplest workflow first:

- `relay "task"`
- `relay review`
- `relay summary`
- `relay commit`
- `relay push`

It also shows dependency status, git repo detection, and install hints.

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
npm i -g @openai/codex
```

Codex CLI:

```bash
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
claude --permission-mode acceptEdits -p "<task>"
```

### `relay @codex "task"`

Forces Codex:

```bash
codex --ask-for-approval never exec --sandbox workspace-write "<task>"
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

### `relay auto "task"`

Runs the automation brain:

- infers a done-condition when `--until` is omitted
- routes by task evidence, not by hard-coded agent preference
- creates a dedicated `relay/auto/...` branch
- executes the selected agent
- runs local verification adapters such as `npm test`, `npm run build`, `pytest`, `go test`, or `cargo test`
- checks richer done conditions such as created files, localhost endpoints, and added tests when those are named
- diagnoses failures and retries with focused guidance
- can fall back to the other agent under the balanced policy
- saves durable state in `.relay/brain.json` and `.relay/runs/<run-id>/`
- auto-commits on success when verification passes and policy allows it

Examples:

```bash
relay auto "fix failing tests" --until "tests pass"
relay auto "ship the auth polish" --mode edit --agent-policy balanced
relay auto "try one agent only" --agent-policy route --no-auto-commit
```

Automation modes:

- `safe`: no agent execution
- `edit`: edits are allowed and verified success can auto-commit
- `commit`: same commit behavior, explicit about commit-capable automation
- `pr`: commit, push the automation branch, and open a PR when GitHub CLI `gh` is available

Agent policies:

- `balanced`: route the first attempt, then alternate on retries when useful
- `route`: keep the routed agent for all retries
- `alternate`: start with the less-used agent from local Relay memory

### `relay brain "goal"`

Runs a multi-step automation brain over a larger goal.

Flow:

```text
goal -> plan -> step auto-runs -> verify each step -> checkpoint -> final status
```

Useful commands:

```bash
relay brain "ship auth polish"
relay brain status
relay brain resume
relay brain logs
relay brain stop
relay brain rollback
```

`brain rollback` creates a normal Git revert commit for the latest Relay auto-created commit it can find.

### Local Triggers

Relay can persist local automation triggers without needing a hosted backend:

```bash
relay watch "if tests fail, diagnose and fix"
relay every "1h" "summarize repo health"
relay on "ci-fail" "repair failing GitHub Actions"
relay triggers
relay trigger-check
```

Triggers are saved in `.relay/triggers.json`. Wire `relay trigger-check` into cron, launchd, or CI to evaluate them from your own machine or runner.

### `relay commit`

Prepares a local git commit safely.

- requires git and a repo
- shows changed files first
- warns about risky files
- suggests a concise commit message
- asks for confirmation before running `git add .` and `git commit -m ...`
- never commits automatically after task, review, or summary

### `relay push`

Prepares a git push safely.

- requires git and a repo
- refuses when uncommitted changes are present
- shows remote, branch, latest commit hash, and latest commit message
- asks for confirmation before pushing
- suggests `git push -u origin <branch>` when upstream is missing
- never pushes automatically after task, review, or summary

## Shortcuts

Relay supports a few short aliases for the commands people use most:

- `relay r` for `relay review`
- `relay s` for `relay summary`
- `relay c` for `relay commit`
- `relay p` for `relay push`

The long forms still work and are the best ones to document for new users.

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
- app shell
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

If scores tie, Relay uses neutral balancing. A configured `default_agent` wins; otherwise Relay chooses the less-used recent agent, then alternates from the last agent when counts are equal.

## Safety

Relay will not:

- ask for API keys
- hide auto-commits from you
- auto-push except when `relay auto --mode pr` is explicitly selected
- modify `.env` files by itself

`relay auto` may create a local commit after verification succeeds. This is controlled by `auto_commit_on_success` in `.relay/config.json` and can be disabled per run with `--no-auto-commit`. `--mode pr` may also push the automation branch and open a PR.

Relay warns when diffs touch risky areas like:

- `.env`
- lockfiles
- migrations
- auth files
- Stripe/payment files
- very large diffs

## Example Workflow

```bash
relay "make the app shell mobile responsive"
relay r
relay s
relay c
relay p
```

## Troubleshooting

If `relay status` says a tool is missing, check that it runs directly in your terminal:

```bash
claude --permission-mode acceptEdits -p "say hello"
codex --ask-for-approval never exec --sandbox workspace-write "say hello"
git status
```

If those do not work directly, Relay cannot use them yet.

If Codex says `writing is blocked by read-only sandbox`, Relay should invoke Codex with workspace-write sandboxing:

```bash
codex --ask-for-approval never exec --sandbox workspace-write "your task"
```

If Claude is not allowed to edit files, Relay should invoke Claude with edit permissions enabled:

```bash
claude --permission-mode acceptEdits -p "your task"
```

Relay stays local-first and safe around Git:

- normal task, review, and summary commands do not auto-commit
- `relay auto` can auto-commit only after verification succeeds
- it only auto-pushes in explicit `--mode pr`
- `relay commit` always asks before creating a commit
- `relay push` always asks before pushing to GitHub

## License

Relay is open source under the MIT License. See [LICENSE](LICENSE).

## Future Monetization

A future Relay Pro could add licensing, richer memory controls, or convenience workflow features, but this MVP stays local-first and free.
