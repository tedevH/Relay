# Changelog

## v0.5.0

- Added multi-step `relay brain` automation.
- Added brain status, resume, logs, stop, and rollback commands.
- Added local trigger scaffolding with `watch`, `every`, `on`, `triggers`, and `trigger-check`.
- Added richer verification checks for files, localhost endpoints, and tests.
- Added standalone binary installer and GitHub Release workflow.

## v0.4.0

- Added balanced automation brain loop for `relay auto`.
- Added durable run state in `.relay/brain.json` and `.relay/runs/`.
- Added project verifier adapters.
- Removed hard-coded Claude/Codex tie favoritism.
