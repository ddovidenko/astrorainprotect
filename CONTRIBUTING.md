# Contributing

## Development setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                       # unit tests, no network
.venv/bin/pytest -m integration        # also downloads one live MRMS file from S3
.venv/bin/ruff check .
cp .env.example .env && docker compose up --build
```

The container targets Python 3.12 (`python:3.12-slim`); the local venv may be
newer. Runtime dependencies are deliberately just `numpy`, `eccodes` and
`httpx`.

## Layout

- `astrorainprotect/detect.py`, `motion.py`, `alarm.py` are pure: grids and
  triggers in, decisions out, no I/O. Most behaviour tests live against these.
- `mrms.py`, `pirate.py`, `notify.py`, `state.py`, `scope.py` wrap S3, HTTP,
  files and sockets.
- `app.py` wires them into the poll cycle; `replay.py` runs the same cycle over
  recorded frames; `healthcheck.py` is the Docker HEALTHCHECK.
- `legacy/` is the shell script this replaced, kept for reference.

## Design documents

- Spec: `docs/superpowers/specs/2026-09-23-astrorainprotect-design.md` (the
  binding description of behaviour; amend it when behaviour changes).
- Implementation plan: `docs/superpowers/plans/2026-09-23-astrorainprotect.md`.
- `CLAUDE.md` is the project brief used with Claude Code.

## Release flow

Every pull request runs ruff, the unit tests and a Docker build. A push to
`main` additionally publishes `ghcr.io/ddovidenko/astrorainprotect:latest` and
a `:<git-sha>` tag. The repository allows squash merges only. The GHCR package
must be public (GitHub package settings) for Portainer to pull without
credentials.

Keep `CHANGELOG.md` current under `[Unreleased]`; use conventional commit
messages.
