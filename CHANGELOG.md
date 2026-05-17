# Changelog

## v0.5.4

- Added `relay go` as the daily-driver verified automation command.
- Added `relay last` and saved Relay Outcome cards.
- Improved post-run UX with verification, changed files, risks, and next steps.

## v0.5.3

- Restored the Windows AMD64 release binary while keeping Intel Mac disabled.

## v0.5.2

- Simplified the release matrix to Apple Silicon macOS and Linux binaries for faster first releases.

## v0.5.1

- Removed the local dashboard surface.
- Removed Flask from runtime/package dependencies.
- Added checksum verification for standalone binary installs.

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
