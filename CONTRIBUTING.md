# Contributing

Thanks for helping improve Relay.

## Development Setup

```bash
git clone https://github.com/tedevH/Relay.git
cd Relay
python3 -m pip install -e .
relay --version
```

## Before Opening a PR

Run:

```bash
python3 -m py_compile relay_core/*.py relay.py
git diff --check
```

Keep changes focused, local-first, and safe around Git operations. Relay should not auto-push or perform destructive actions unless the command explicitly asks for that behavior.

## Agent Neutrality

Relay treats Claude and Codex as peer executors. Avoid hard-coded defaults that favor one agent when task evidence is tied. Use routing evidence, local config, recent success, or balanced tie-breaking.
