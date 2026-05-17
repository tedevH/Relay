# Relay

**A local automation brain for Claude Code, Codex CLI, and Git.**

Relay gives your coding agents memory, chooses the right agent for the job, runs a verification loop, and shows you a clear outcome card so you know what changed and what to do next.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/tedevH/Relay/main/install.sh | bash
```

Installs a standalone binary to `~/.local/bin/relay`. No Python install required.

Requirements for live AI workflows:

- [Claude Code](https://claude.ai/download)
- [Codex CLI](https://github.com/openai/codex)
- Git

Check your setup:

```bash
relay doctor
```

## Daily Use

Use `relay go` when you want Relay to do more than route between Claude and Codex:

```bash
cd your-project
relay init
relay go "fix failing tests"
relay last
relay push
```

`relay go`:

- infers a done condition
- routes to Claude or Codex by task fit
- creates a dedicated branch
- runs the agent
- verifies the result
- diagnoses and retries when verification fails
- can fall back to the other agent under the balanced policy
- auto-commits verified success
- prints and saves a Relay Outcome card

`relay last` shows the latest outcome again:

- task
- agent
- success and verification status
- changed files and risk levels
- diff stat
- suggested commit message
- next recommended commands

## Why Use Relay Instead Of Switching Agents Yourself?

Manual switching only chooses who starts the task. Relay handles the rest of the workflow:

- persistent repo memory across sessions
- automatic Claude/Codex routing with balanced tie-breaks
- focused context injection before every agent run
- verification and retry loops
- local risk review and commit message suggestions
- saved outcome cards you can inspect later
- safe Git branch/commit/push flow

The point is not just choosing Claude or Codex. The point is getting from task to verified Git outcome with less babysitting.

## Commands

### Daily workflow

| Command | What it does |
|---|---|
| `relay go "task"` | Daily driver: route, run, verify, retry, commit, summarize |
| `relay last` | Show the latest Relay Outcome card |
| `relay review` | Instant local risk check |
| `relay summary` | Diff summary with risk levels |
| `relay commit` | Safe commit with smart message suggestion |
| `relay push` | Safe push with confirmation |

### Quick runs

| Command | What it does |
|---|---|
| `relay "task"` | Quick routed agent run without the full verification loop |
| `relay @claude "task"` | Force Claude |
| `relay @codex "task"` | Force Codex |
| `relay why "task"` | Explain routing without running |

### Memory and context

| Command | What it does |
|---|---|
| `relay init` | Set up git hooks and memory for this repo |
| `relay context` | Show what Relay knows about this project |
| `relay digest` | Full project health report |
| `relay history` | Recent task history |

### Automation

| Command | What it does |
|---|---|
| `relay auto "task" --until "condition"` | Custom execute -> verify -> diagnose -> retry loop |
| `relay plan "goal"` | Decompose goal into subtasks and execute each |
| `relay brain "goal"` | Multi-step automation brain with resume/log/rollback commands |

Examples:

```bash
relay auto "fix the broken login flow" --until "npm test passes"
relay plan "add user authentication with email and Google OAuth"
relay brain "ship auth polish"
```

## How Memory Works

Running `relay init` installs a git post-commit hook. After every commit, Relay silently:

1. extracts new symbols from the diff
2. classifies the active workstream
3. updates `.relay/memory.json` with hot files and agent stats

Before every run, Relay writes `.relay/context.md` and updates `CLAUDE.md` with:

- the current task
- relevant symbols with exact file locations
- active workstream status
- the files most likely to need editing

Both Claude Code and Codex can use this context instead of starting cold.

## Local Memory Structure

```text
.relay/
  tasks.json
  memory.json
  symbols.json
  workstreams.json
  context.md
  last-diff.patch
  last-outcome.json
  config.json
  brain.json
  runs/
```

Memory stays local and is never committed. Relay adds `.relay/` to `.git/info/exclude` automatically when possible.

## Safety

- No web app
- No backend
- No cloud sync
- No API keys requested by Relay
- Normal task, review, and summary commands do not auto-commit
- `relay go` and `relay auto` auto-commit only after verification succeeds
- Relay only auto-pushes in explicit `--mode pr`
- `relay commit` and `relay push` always ask before running

Relay warns when diffs touch risky areas like:

- `.env`
- lockfiles
- migrations
- auth files
- Stripe/payment files
- very large diffs

## Publishing Releases

Relay ships as standalone binaries through GitHub Releases.

```bash
git tag v0.5.4
git push origin v0.5.4
```

The release workflow currently builds:

```text
relay-darwin-arm64
relay-linux-amd64
relay-windows-amd64.exe
```

## Source Install

```bash
git clone https://github.com/tedevH/Relay.git
cd Relay
python3 -m pip install -e .
relay --version
```

## License

MIT - [LICENSE](LICENSE)
