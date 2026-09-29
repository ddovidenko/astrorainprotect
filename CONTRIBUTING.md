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
`httpx`. `eccodes` is pinned to 2.39.2 on x86_64 CPython 3.13 and older because that
wheel bundles an eckit-free library (the image is 324 MB with it versus 506 MB
with newer wheels); Python 3.14 and aarch64 use the newer binding since no
such wheel exists for them. Both decode the MRMS files identically; the integration
test exercises whichever is installed.

## Layout

- `astrorainprotect/detect.py`, `motion.py`, `alarm.py`, `snapshot.py` are
  pure: grids and triggers in, decisions or PNG bytes out, no I/O. Most
  behaviour tests live against these.
- `frame.py` is the radar frame dataclass and its `.npz` save/load.
- `mrms.py`, `pirate.py`, `notify.py`, `state.py`, `scope.py` wrap S3, HTTP,
  files and sockets.
- `app.py` wires them into the poll cycle; `replay.py` runs the same cycle over
  recorded frames; `healthcheck.py` is the Docker HEALTHCHECK.
- `legacy/` is the shell script this replaced, kept for reference.

## Private data: what must never be committed

This repository is public. Three things on a working machine identify the
site or grant access, and none of them belongs in a commit:

- **Recorded radar frames** (`frames/`, `frames-<date>/`). Each `.npz` stores
  the latitude/longitude grid of a box centred on the recording site, so the
  box centre gives the location to within a few hundred metres. Both folder
  patterns are gitignored. Record into a folder matching one of them.
- **`.env`**: coordinates, the ntfy topic and token, the Pirate Weather key.
- **`state/`**: the local state directory.

Test fixtures use a deliberately approximate location. If a real frame is
ever wanted as a fixture, re-centre its coordinates first.

If something private does get pushed, removing it in a later commit is not
enough: it stays in history and in the pull request's refs. Rewrite the branch,
then ask GitHub Support to purge the dangling commits and PR refs.

## Commits and pull requests

- Stage files by path (`git add path/to/file`) and read `git status --short`
  before committing. Do not use `git add -A` or `git add .`; that is how a
  capture folder reached `main` once.
- Before asking for a merge, check the pull request's file list. A change
  that should touch six files and shows three hundred is the warning sign.
- Open every pull request against `main`. Do not stack one PR on another
  PR's branch: squash-merging the first deletes its branch, and GitHub then
  closes the stacked PR, which cannot be retargeted afterwards.
- `main` is protected by a ruleset: changes arrive through pull requests,
  history is linear, force-pushes and deletion are blocked.
- A change to behaviour gets an independent review before merge; it has
  caught a real defect more often than not.

## Design documents

- Spec: `docs/superpowers/specs/2026-09-23-astrorainprotect-design.md` (the
  binding description of behaviour; amend it when behaviour changes).
- Implementation plan: `docs/superpowers/plans/2026-09-23-astrorainprotect.md`.
- `CLAUDE.md` is the project brief used with Claude Code.

## Release flow

Every pull request runs ruff, the unit tests and a Docker build. A push to
`main` additionally publishes `ghcr.io/ddovidenko/astrorainprotect:latest` and
a `:<git-sha>` tag. The repository allows squash merges only. The image is
built from `pyproject.toml`, `README.md`, `LICENSE` and the `astrorainprotect/`
package alone, so nothing else in the working tree can end up in it. The GHCR package
must be public (GitHub package settings) for Portainer to pull without
credentials.

Keep `CHANGELOG.md` current under `[Unreleased]`; use conventional commit
messages.
