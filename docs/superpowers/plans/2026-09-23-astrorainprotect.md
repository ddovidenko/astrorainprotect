# astrorainprotect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the radar-based rain alarm described in the spec: one container that polls NOAA MRMS radar (plus Pirate Weather as a secondary trigger) and pushes to ntfy with the legacy alert semantics.

**Architecture:** One Python 3.12 process, one poll loop. Pure modules (`detect`, `motion`, `alarm`) carry all decision logic and are tested against synthetic grids and state tables. Thin I/O modules (`mrms`, `pirate`, `notify`, `state`, `scope`) wrap S3, HTTP, files, and sockets. `app.py` wires them; `__main__.py` only calls `app.main()`.

**Tech Stack:** Python 3.12 (container) / 3.14 (local venv), numpy, `eccodes` (PyPI, bundles `eccodeslib` binary wheel), httpx, pytest, ruff, hatchling, Docker, GitHub Actions, GHCR, Portainer.

**Spec:** `docs/superpowers/specs/2026-09-23-astrorainprotect-design.md`

## Spike results that this plan bakes in

Measured on 2026-09-23 against real files from `s3://noaa-mrms-pds` (throwaway venv, nothing committed):

| Fact | Value |
|---|---|
| Decoder choice | `eccodes` PyPI package used directly. `cfgrib`/`xarray` rejected: MRMS params decode as `shortName=unknown`, and they add 17 MB for nothing. |
| Package sizes | numpy 43 MB, eccodeslib 49 MB. No system `libeccodes` needed. |
| File sizes | PrecipRate ≈ 0.75 MB gz, reflectivity ≈ 1.5 MB gz. Full-CONUS decode 0.8 s. |
| Grid | `Ni=7000, Nj=3500`, first point lat 54.995 lon 230.005 (0–360 convention), increments 0.01°, `scanningMode=0` (row 0 is north, columns increase eastward), `gridType=regular_ll`. Reshape values to `(Nj, Ni)`. |
| Missing values | PrecipRate: `-3` no coverage, `-1` missing. Reflectivity: `-999` missing, `-99` no echo, and real weak echoes down to `-20` dBZ. So "missing" is `< 0` for PrecipRate and `< -990` for reflectivity. All values are clamped to `≥ 0` after the missing fraction is computed. |
| Timestamps | Reflectivity filenames carry seconds (`-011040`); the GRIB `dataTime` key truncates them. Parse the valid time from the filename, never from the GRIB keys. |
| Phase correlation | `ifft2(fft2(newest) * conj(fft2(oldest)) / |·|)` peaks at `(dy, dx)` where `newest == roll(oldest, (dy, dx))`. Peak is 1.0 for a clean shift, 0.0 for empty frames, ≈0.04 for noise. |

## Global Constraints

- Package and image name: `astrorainprotect`; image `ghcr.io/ddovidenko/astrorainprotect`.
- Container base `python:3.12-slim`, non-root uid 1000, `HEALTHCHECK` present.
- Runtime dependencies only: `numpy`, `eccodes`, `httpx`. Nothing else.
- All legacy env var names preserved exactly as in the spec table; new vars are `MIN_DBZ`, `NTFY_PRIORITY`, `REPLAY_DIR`, `NOW_RADIUS_KM`, `STATE_DIR` (default `/state`, needed so tests and local runs don't touch `/state`).
- ntfy headers: `Title`, `Priority`, `Tags: loud_sound,bell`, `Authorization: Bearer <token>` only when token non-empty. Titles: `Rain incoming`, `Rain incoming (still)`, `Rain alert test`.
- Pirate Weather is never called more often than every 300 s.
- Logs in local `TZ`; frame timestamps in UTC.
- Every poll logs exactly one summary line.
- Conventional commit messages; `CHANGELOG.md` updated in the task that ships user-visible behaviour.
- **Never merge.** Each phase ends with a pull request; the user merges.
- Branching: phase 1 branches from `main` as `phase-1-detector`. Each later phase branches from the previous phase branch (`phase-2-mrms`, `phase-3-loop`, `phase-4-pirate`, `phase-5-container`, `phase-6-motion`). PRs target the previous phase branch until it is merged, then are retargeted to `main`.
- Local commands run inside `.venv` (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`). Every `pytest`/`ruff` command below means `.venv/bin/pytest` / `.venv/bin/ruff`.

## Review Focus

Inputs the spec implies but a naive implementation breaks. Each has a pinned test in the owning task.

1. **House longitude in 0–360 vs −180–180.** MRMS uses 230–300; the user supplies `-95.67`. Subsetting must convert, else the box lands in the Indian Ocean. Pinned in Task 6 (`test_subset_negative_longitude`).
2. **Latch file survives a container restart but the test marker must not.** A restart mid-event must not re-alert, yet DEBUG=2 must send one test per start. Pinned in Task 9 (`test_latch_persists_across_instances`, `test_clear_test_marker_on_start`).
3. **Radar outage must not re-arm the latch.** If the frame is stale or mostly missing we know nothing; clearing the latch would cause a duplicate alert when radar returns. Pinned in Task 12 (`test_radar_unavailable_leaves_latch`).
4. **Midnight UTC listing.** At 00:03 UTC today's prefix may be empty; the newest file is under yesterday. Pinned in Task 5 (`test_day_prefixes_near_midnight`).
5. **ntfy failure must not set the latch.** If the POST fails the alert was not delivered; the next poll must retry. Pinned in Task 12 (`test_send_failure_does_not_latch`).

---

## Phase 1 — skeleton, config, detector

### Task 1: Project skeleton and branch

**Files:**
- Create: `pyproject.toml`, `astrorainprotect/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py`, `README.md`, `CHANGELOG.md`, `.env.example`, `CLAUDE.md`, `legacy/` (copied)
- Modify: `.gitignore` (add `.venv/`, `.env`)

**Interfaces:**
- Produces: the package `astrorainprotect` importable; `pytest` and `ruff` runnable.

- [ ] **Step 1: Create the phase branch**

```bash
cd /home/dmitr/astrorainprotect && git checkout -b phase-1-detector
```

- [ ] **Step 2: Write pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "astrorainprotect"
version = "0.1.0"
description = "Radar-based rain alarm that pushes to ntfy"
readme = "README.md"
requires-python = ">=3.12"
license = { file = "LICENSE" }
dependencies = [
  "numpy>=2.0",
  "eccodes>=2.40",
  "httpx>=0.27",
]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.6"]

[project.scripts]
astrorainprotect = "astrorainprotect.app:main"

[tool.hatch.build.targets.wheel]
packages = ["astrorainprotect"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: needs network access to S3; skipped when offline"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 3: Write package init, smoke test, gitignore additions**

`astrorainprotect/__init__.py`:
```python
"""Radar-based rain alarm that pushes to ntfy."""

__version__ = "0.1.0"
```

`tests/__init__.py`: empty file.

`tests/test_smoke.py`:
```python
import astrorainprotect


def test_version():
    assert astrorainprotect.__version__ == "0.1.0"
```

Append to `.gitignore`:
```
.venv/
.env
```

- [ ] **Step 4: Copy legacy and adapt CLAUDE.md**

```bash
cp -r /home/dmitr/rain-radar-alert/legacy /home/dmitr/astrorainprotect/legacy
cp /home/dmitr/rain-radar-alert/CLAUDE.md /home/dmitr/astrorainprotect/CLAUDE.md
sed -i 's/rain-radar-alert/astrorainprotect/g; s/rain_radar_alert/astrorainprotect/g; s#ghcr.io/<me>/#ghcr.io/ddovidenko/#g' /home/dmitr/astrorainprotect/CLAUDE.md
```

Then edit `CLAUDE.md` by hand: replace the "GRIB decoding" bullet under "Notes for the implementation" with:
```
- GRIB decoding: the `eccodes` PyPI package (bundled `eccodeslib` wheel), used
  directly. cfgrib/xarray were evaluated and rejected: MRMS parameters decode as
  `shortName=unknown` and they add 17 MB. Decided 2026-09-23, see
  docs/superpowers/plans/2026-09-23-astrorainprotect.md.
```
and add under "Development workflow":
```
- Design spec: docs/superpowers/specs/2026-09-23-astrorainprotect-design.md
- Never merge a branch or PR without asking the user first.
```

- [ ] **Step 5: Write README.md placeholder, CHANGELOG.md and .env.example**

`README.md` (rewritten fully in Task 18; hatchling and the Dockerfile need it to exist now):
```markdown
# astrorainprotect

Radar-based rain alarm that pushes to ntfy. Setup and tuning guide coming with the
container release; see `CLAUDE.md` for the brief and `docs/superpowers/` for the design.
```

`CHANGELOG.md`:
```markdown
# Changelog

All notable changes to this project are documented here. Format follows
Keep a Changelog; versions follow SemVer.

## [Unreleased]
```

`.env.example`:
```
LAT=29.9700
LON=-95.6700
NTFY_URL=https://ntfy.example.net/rain
NTFY_TOKEN=
# NTFY_PRIORITY=high
# DEBUG=0            0 normal, 1 detail, 2 detail + one test notification per start
# POLL_SEC=180
# ALERT_RADIUS_KM=20
# NOW_RADIUS_KM=1
# MIN_INTENSITY=0.2  mm/h for a PrecipRate cell to count (also Pirate Weather)
# MIN_DBZ=30         dBZ for a reflectivity cell to count
# MIN_CELLS=3
# RAINING_NOW=0.05   mm/h at the house = already raining
# DIRECTION_FILTER=0
# LOOKAHEAD_MIN=60
# REPEAT_MIN=0
# SCOPE_HOSTS=       comma-separated host[:port], default port 4700
# PW_KEY=            enables the Pirate Weather secondary trigger
# MIN_PROB=0.3
# STATE_DIR=/state
# TZ=America/Chicago
```

- [ ] **Step 6: Create venv, install, run tests and lint**

```bash
cd /home/dmitr/astrorainprotect && python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' && .venv/bin/pytest -q && .venv/bin/ruff check .
```
Expected: `1 passed`, ruff reports no errors.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "chore: project skeleton, legacy reference, adapted brief"
```

---

### Task 2: config.py

**Files:**
- Create: `astrorainprotect/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config` frozen dataclass; `load_config(env: Mapping[str, str]) -> Config`; `ConfigError(ValueError)`; `describe(cfg: Config) -> str` with `NTFY_TOKEN` and `PW_KEY` masked.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:
```python
import pytest

from astrorainprotect.config import Config, ConfigError, describe, load_config

BASE = {"LAT": "29.97", "LON": "-95.67", "NTFY_URL": "https://ntfy.example.net/rain"}


def test_defaults():
    cfg = load_config(BASE)
    assert cfg.lat == 29.97
    assert cfg.lon == -95.67
    assert cfg.ntfy_url == "https://ntfy.example.net/rain"
    assert cfg.ntfy_token == ""
    assert cfg.ntfy_priority == "high"
    assert cfg.debug == 0
    assert cfg.poll_sec == 180
    assert cfg.alert_radius_km == 20.0
    assert cfg.now_radius_km == 1.0
    assert cfg.min_intensity == 0.2
    assert cfg.min_dbz == 30.0
    assert cfg.min_cells == 3
    assert cfg.raining_now == 0.05
    assert cfg.direction_filter is False
    assert cfg.lookahead_min == 60
    assert cfg.repeat_min == 0
    assert cfg.scope_hosts == ""
    assert cfg.pw_key == ""
    assert cfg.min_prob == 0.3
    assert cfg.replay_dir == ""
    assert cfg.state_dir == "/state"


def test_overrides():
    cfg = load_config({**BASE, "DEBUG": "2", "DIRECTION_FILTER": "1", "POLL_SEC": "120",
                       "NTFY_TOKEN": "tk_abc", "PW_KEY": "k", "SCOPE_HOSTS": "a,b:1",
                       "STATE_DIR": "/tmp/s", "NTFY_PRIORITY": "urgent"})
    assert cfg.debug == 2
    assert cfg.direction_filter is True
    assert cfg.poll_sec == 120
    assert cfg.ntfy_token == "tk_abc"
    assert cfg.pw_key == "k"
    assert cfg.scope_hosts == "a,b:1"
    assert cfg.state_dir == "/tmp/s"
    assert cfg.ntfy_priority == "urgent"


@pytest.mark.parametrize("missing", ["LAT", "LON", "NTFY_URL"])
def test_required(missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


@pytest.mark.parametrize("key,value", [
    ("LAT", "abc"), ("LAT", "95"), ("LON", "200"), ("DEBUG", "3"), ("POLL_SEC", "0"),
    ("MIN_CELLS", "0"), ("MIN_PROB", "1.5"), ("LOOKAHEAD_MIN", "0"), ("REPEAT_MIN", "-1"),
    ("NTFY_PRIORITY", "loud"), ("ALERT_RADIUS_KM", "0"),
])
def test_invalid(key, value):
    with pytest.raises(ConfigError, match=key):
        load_config({**BASE, key: value})


def test_describe_masks_secrets():
    cfg = load_config({**BASE, "NTFY_TOKEN": "tk_secret123", "PW_KEY": "pwsecret"})
    text = describe(cfg)
    assert "tk_secret123" not in text
    assert "pwsecret" not in text
    assert "NTFY_TOKEN=***" in text
    assert "PW_KEY=***" in text
    assert "LAT=29.97" in text


def test_describe_shows_empty_secret():
    text = describe(load_config(BASE))
    assert "NTFY_TOKEN=(unset)" in text
    assert "PW_KEY=(unset)" in text


def test_config_is_frozen():
    cfg = load_config(BASE)
    with pytest.raises(AttributeError):
        cfg.lat = 1.0  # type: ignore[misc]


def test_config_type():
    assert isinstance(load_config(BASE), Config)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: ImportError on `astrorainprotect.config`.

- [ ] **Step 3: Implement config.py**

```python
"""Environment-variable configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

PRIORITIES = ("min", "low", "default", "high", "urgent")


class ConfigError(ValueError):
    """Raised when a required variable is missing or a value is out of range."""


@dataclass(frozen=True)
class Config:
    lat: float
    lon: float
    ntfy_url: str
    ntfy_token: str = ""
    ntfy_priority: str = "high"
    debug: int = 0
    poll_sec: int = 180
    alert_radius_km: float = 20.0
    now_radius_km: float = 1.0
    min_intensity: float = 0.2
    min_dbz: float = 30.0
    min_cells: int = 3
    raining_now: float = 0.05
    direction_filter: bool = False
    lookahead_min: int = 60
    repeat_min: int = 0
    scope_hosts: str = ""
    pw_key: str = ""
    min_prob: float = 0.3
    replay_dir: str = ""
    state_dir: str = "/state"


def _float(env: Mapping[str, str], key: str, default: float | None, lo: float, hi: float) -> float:
    raw = env.get(key, "").strip()
    if raw == "":
        if default is None:
            raise ConfigError(f"{key} is required")
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    return value


def _int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    return value


def _str(env: Mapping[str, str], key: str, default: str | None = "") -> str:
    raw = env.get(key, "").strip()
    if raw == "":
        if default is None:
            raise ConfigError(f"{key} is required")
        return default
    return raw


def load_config(env: Mapping[str, str]) -> Config:
    priority = _str(env, "NTFY_PRIORITY", "high")
    if priority not in PRIORITIES:
        raise ConfigError(f"NTFY_PRIORITY must be one of {PRIORITIES}, got {priority!r}")
    return Config(
        lat=_float(env, "LAT", None, -90, 90),
        lon=_float(env, "LON", None, -180, 180),
        ntfy_url=_str(env, "NTFY_URL", None),
        ntfy_token=_str(env, "NTFY_TOKEN"),
        ntfy_priority=priority,
        debug=_int(env, "DEBUG", 0, 0, 2),
        poll_sec=_int(env, "POLL_SEC", 180, 30, 3600),
        alert_radius_km=_float(env, "ALERT_RADIUS_KM", 20.0, 0.5, 50.0),
        now_radius_km=_float(env, "NOW_RADIUS_KM", 1.0, 0.1, 10.0),
        min_intensity=_float(env, "MIN_INTENSITY", 0.2, 0.0, 100.0),
        min_dbz=_float(env, "MIN_DBZ", 30.0, 0.0, 80.0),
        min_cells=_int(env, "MIN_CELLS", 3, 1, 1000),
        raining_now=_float(env, "RAINING_NOW", 0.05, 0.0, 100.0),
        direction_filter=_int(env, "DIRECTION_FILTER", 0, 0, 1) == 1,
        lookahead_min=_int(env, "LOOKAHEAD_MIN", 60, 1, 60),
        repeat_min=_int(env, "REPEAT_MIN", 0, 0, 1440),
        scope_hosts=_str(env, "SCOPE_HOSTS"),
        pw_key=_str(env, "PW_KEY"),
        min_prob=_float(env, "MIN_PROB", 0.3, 0.0, 1.0),
        replay_dir=_str(env, "REPLAY_DIR"),
        state_dir=_str(env, "STATE_DIR", "/state"),
    )


_SECRETS = {"ntfy_token", "pw_key"}


def describe(cfg: Config) -> str:
    """One line per setting, secrets masked, for the startup log."""
    parts = []
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        if f.name in _SECRETS:
            value = "***" if value else "(unset)"
        elif isinstance(value, bool):
            value = int(value)
        parts.append(f"{f.name.upper()}={value}")
    return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check . && git add astrorainprotect/config.py tests/test_config.py && git commit -m "feat: env config parsing with validation and masked startup dump"
```

---

### Task 3: frame.py and detect geometry

**Files:**
- Create: `astrorainprotect/frame.py`, `astrorainprotect/detect.py`
- Test: `tests/test_frame.py`, `tests/test_detect.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `Frame` dataclass (`product: str`, `valid_time: datetime` UTC-aware, `lats: np.ndarray` 1-D descending, `lons: np.ndarray` 1-D ascending in −180..180, `values: np.ndarray` 2-D `(len(lats), len(lons))`, `missing_fraction: float`), `Frame.save(path)`, `Frame.load(path) -> Frame`; `detect.distance_bearing_grid(lats, lons, home_lat, home_lon) -> tuple[np.ndarray, np.ndarray]` (km, degrees 0–360); `detect.compass(bearing_deg) -> str` (8-point).
- `tests/conftest.py` produces `make_frame(values, *, product="preciprate", center=(29.97, -95.67), step=0.01, valid_time=...)` used by every later test.

- [ ] **Step 1: Write conftest helper**

`tests/conftest.py`:
```python
from datetime import UTC, datetime

import numpy as np
import pytest

from astrorainprotect.frame import Frame

HOME = (29.97, -95.67)


def make_frame(values, *, product="preciprate", center=HOME, step=0.01,
               valid_time=datetime(2026, 9, 23, 20, 0, tzinfo=UTC), missing_fraction=0.0):
    """Build a Frame whose grid is centred on `center` with `step` degree spacing."""
    values = np.asarray(values, dtype=np.float32)
    nj, ni = values.shape
    lat0, lon0 = center
    lats = lat0 + step * (nj // 2 - np.arange(nj))   # row 0 is north
    lons = lon0 + step * (np.arange(ni) - ni // 2)
    return Frame(product=product, valid_time=valid_time, lats=lats, lons=lons,
                 values=values, missing_fraction=missing_fraction)


@pytest.fixture
def frame_factory():
    return make_frame
```

- [ ] **Step 2: Write failing frame tests**

`tests/test_frame.py`:
```python
from datetime import UTC, datetime

import numpy as np

from astrorainprotect.frame import Frame
from tests.conftest import make_frame


def test_make_frame_geometry():
    f = make_frame(np.zeros((5, 7)))
    assert f.values.shape == (5, 7)
    assert f.lats[0] > f.lats[-1]            # descending, row 0 north
    assert f.lons[0] < f.lons[-1]            # ascending
    assert abs(f.lats[2] - 29.97) < 1e-9
    assert abs(f.lons[3] + 95.67) < 1e-9


def test_save_load_roundtrip(tmp_path):
    f = make_frame(np.arange(12, dtype=np.float32).reshape(3, 4), product="reflectivity",
                   valid_time=datetime(2026, 9, 23, 1, 10, 40, tzinfo=UTC), missing_fraction=0.25)
    path = tmp_path / "x.npz"
    f.save(path)
    g = Frame.load(path)
    assert g.product == "reflectivity"
    assert g.valid_time == f.valid_time
    assert g.valid_time.tzinfo is not None
    assert g.missing_fraction == 0.25
    np.testing.assert_array_equal(g.values, f.values)
    np.testing.assert_array_equal(g.lats, f.lats)
    np.testing.assert_array_equal(g.lons, f.lons)


def test_filename_for():
    f = make_frame(np.zeros((1, 1)), product="reflectivity",
                   valid_time=datetime(2026, 9, 23, 1, 10, 40, tzinfo=UTC))
    assert f.filename() == "reflectivity_20260923-011040.npz"
```

- [ ] **Step 3: Implement frame.py**

```python
"""A subset radar grid around the house."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Frame:
    product: str            # "preciprate" or "reflectivity"
    valid_time: datetime    # UTC-aware
    lats: np.ndarray        # 1-D, descending (row 0 is north)
    lons: np.ndarray        # 1-D, ascending, -180..180
    values: np.ndarray      # 2-D (len(lats), len(lons)); mm/h or dBZ, >= 0
    missing_fraction: float  # fraction of cells that were missing before clamping

    def filename(self) -> str:
        return f"{self.product}_{self.valid_time.strftime('%Y%m%d-%H%M%S')}.npz"

    def save(self, path: Path | str) -> None:
        np.savez_compressed(
            path,
            product=np.array(self.product),
            valid_time=np.array(self.valid_time.timestamp()),
            lats=self.lats,
            lons=self.lons,
            values=self.values,
            missing_fraction=np.array(self.missing_fraction),
        )

    @classmethod
    def load(cls, path: Path | str) -> Frame:
        with np.load(path) as z:
            return cls(
                product=str(z["product"]),
                valid_time=datetime.fromtimestamp(float(z["valid_time"]), tz=UTC),
                lats=z["lats"],
                lons=z["lons"],
                values=z["values"],
                missing_fraction=float(z["missing_fraction"]),
            )
```

- [ ] **Step 4: Run frame tests**

Run: `.venv/bin/pytest tests/test_frame.py -q`
Expected: 3 passed.

- [ ] **Step 5: Write failing geometry tests**

`tests/test_detect.py` (first part; more added in Task 4):
```python
import numpy as np
import pytest

from astrorainprotect.detect import compass, distance_bearing_grid

HOME = (29.97, -95.67)


def test_distance_zero_at_home():
    lats = np.array([29.98, 29.97, 29.96])
    lons = np.array([-95.68, -95.67, -95.66])
    dist, _ = distance_bearing_grid(lats, lons, *HOME)
    assert dist.shape == (3, 3)
    assert dist[1, 1] == pytest.approx(0.0, abs=1e-6)


def test_distance_one_degree_north():
    lats = np.array([30.97])
    lons = np.array([-95.67])
    dist, bearing = distance_bearing_grid(lats, lons, *HOME)
    assert dist[0, 0] == pytest.approx(111.2, abs=0.5)
    assert bearing[0, 0] == pytest.approx(0.0, abs=1e-6)


def test_distance_east_uses_cos_lat():
    lats = np.array([29.97])
    lons = np.array([-94.67])
    dist, bearing = distance_bearing_grid(lats, lons, *HOME)
    assert dist[0, 0] == pytest.approx(111.2 * np.cos(np.radians(29.97)), abs=0.5)
    assert bearing[0, 0] == pytest.approx(90.0, abs=1e-6)


@pytest.mark.parametrize("dlat,dlon,expected", [
    (0.1, 0.0, 0.0), (0.0, 0.1, 90.0), (-0.1, 0.0, 180.0), (0.0, -0.1, 270.0),
])
def test_bearing_quadrants(dlat, dlon, expected):
    _, bearing = distance_bearing_grid(np.array([HOME[0] + dlat]), np.array([HOME[1] + dlon]), *HOME)
    assert bearing[0, 0] == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("deg,name", [
    (0, "N"), (44, "N"), (45, "NE"), (90, "E"), (135, "SE"), (180, "S"),
    (225, "SW"), (270, "W"), (315, "NW"), (359, "N"),
])
def test_compass(deg, name):
    assert compass(deg) == name
```

- [ ] **Step 6: Run to verify failure**

Run: `.venv/bin/pytest tests/test_detect.py -q`
Expected: ImportError on `astrorainprotect.detect`.

- [ ] **Step 7: Implement geometry in detect.py**

```python
"""Pure radar detection logic. No I/O."""

from __future__ import annotations

import numpy as np

KM_PER_DEG = 111.195  # mean earth radius * pi / 180

_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def distance_bearing_grid(lats: np.ndarray, lons: np.ndarray, home_lat: float, home_lon: float):
    """Equirectangular distance (km) and bearing (deg, 0=N, 90=E) from home to every cell."""
    lat_g, lon_g = np.meshgrid(lats, lons, indexing="ij")
    dy = (lat_g - home_lat) * KM_PER_DEG
    dx = (lon_g - home_lon) * KM_PER_DEG * np.cos(np.radians(home_lat))
    dist = np.hypot(dx, dy)
    bearing = np.degrees(np.arctan2(dx, dy)) % 360.0
    return dist, bearing


def compass(bearing_deg: float) -> str:
    return _COMPASS[int(((bearing_deg + 22.5) % 360) // 45)]
```

- [ ] **Step 8: Run tests, lint, commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/frame.py astrorainprotect/detect.py tests/ && git commit -m "feat: Frame container with npz round-trip and grid geometry helpers"
```

---

### Task 4: detect.detect

**Files:**
- Modify: `astrorainprotect/detect.py`
- Test: `tests/test_detect.py` (append)

**Interfaces:**
- Produces: `Detection` frozen dataclass (`product: str`, `raining_now: bool`, `rate_at_house: float`, `qualifying_cells: int`, `nearby: bool`, `nearest_km: float | None`, `nearest_bearing_deg: float | None`, `max_value: float`) with property `describe() -> str` (e.g. `"12.3 km to the SW"`); `detect(frame, home_lat, home_lon, *, now_radius_km, alert_radius_km, now_threshold, nearby_threshold, min_cells) -> Detection`.

- [ ] **Step 1: Append failing tests**

Append to `tests/test_detect.py`:
```python
from astrorainprotect.detect import Detection, detect  # noqa: E402
from tests.conftest import make_frame  # noqa: E402

KW = dict(now_radius_km=1.0, alert_radius_km=20.0, now_threshold=0.05,
          nearby_threshold=0.2, min_cells=3)


def grid(n=101):
    return np.zeros((n, n), dtype=np.float32)


def test_empty_grid():
    d = detect(make_frame(grid()), *HOME, **KW)
    assert isinstance(d, Detection)
    assert d.product == "preciprate"
    assert not d.raining_now and not d.nearby
    assert d.qualifying_cells == 0
    assert d.nearest_km is None and d.nearest_bearing_deg is None
    assert d.max_value == 0.0
    assert d.rate_at_house == 0.0


def test_single_pixel_below_min_cells():
    g = grid(); g[40, 55] = 5.0                  # one loud pixel ~12 km NE
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 1
    assert not d.nearby
    assert d.nearest_km == pytest.approx(np.hypot(10 * 1.112, 5 * 1.112 * np.cos(np.radians(29.97))), rel=0.02)
    assert d.max_value == 5.0


def test_exactly_min_cells_is_nearby():
    g = grid(); g[40, 55] = g[40, 56] = g[41, 55] = 0.2   # threshold is inclusive
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 3
    assert d.nearby


def test_just_below_threshold_not_counted():
    g = grid(); g[40, 55] = g[40, 56] = g[41, 55] = 0.19
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 0


def test_cells_outside_radius_ignored():
    g = grid()
    g[0:3, 50] = 5.0                               # 50 rows north ≈ 55 km
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 0
    assert d.max_value == 0.0                      # max is inside the alert radius only


def test_raining_now_uses_now_radius():
    g = grid(); g[50, 50] = 0.05                   # at the house, exactly at threshold
    d = detect(make_frame(g), *HOME, **KW)
    assert d.raining_now
    assert d.rate_at_house == pytest.approx(0.05)
    g2 = grid(); g2[50, 53] = 5.0                  # 3 cells east ≈ 2.9 km, outside 1 km
    d2 = detect(make_frame(g2), *HOME, **KW)
    assert not d2.raining_now
    assert d2.rate_at_house == 0.0


def test_nearest_and_bearing_southwest():
    g = grid(); g[60:63, 40:43] = 1.0              # SW of the house
    d = detect(make_frame(g), *HOME, **KW)
    assert d.nearby
    assert 200 < d.nearest_bearing_deg < 250
    assert d.describe().endswith("to the SW")
    assert "km" in d.describe()


def test_reflectivity_thresholds():
    g = grid(); g[40:43, 50:53] = 35.0           # ~10 km north
    d = detect(make_frame(g, product="reflectivity"), *HOME,
               now_radius_km=1.0, alert_radius_km=20.0, now_threshold=0.0,
               nearby_threshold=30.0, min_cells=3)
    assert d.product == "reflectivity"
    assert d.nearby and d.qualifying_cells == 9
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_detect.py -q`
Expected: ImportError for `Detection`.

- [ ] **Step 3: Implement**

Append to `astrorainprotect/detect.py`:
```python
from dataclasses import dataclass  # noqa: E402  (move to top of file with other imports)

from astrorainprotect.frame import Frame  # noqa: E402  (move to top of file)


@dataclass(frozen=True)
class Detection:
    product: str
    raining_now: bool
    rate_at_house: float
    qualifying_cells: int
    nearby: bool
    nearest_km: float | None
    nearest_bearing_deg: float | None
    max_value: float

    def describe(self) -> str:
        if self.nearest_km is None or self.nearest_bearing_deg is None:
            return "no echo in range"
        return f"{self.nearest_km:.1f} km to the {compass(self.nearest_bearing_deg)}"


def detect(frame: Frame, home_lat: float, home_lon: float, *, now_radius_km: float,
           alert_radius_km: float, now_threshold: float, nearby_threshold: float,
           min_cells: int) -> Detection:
    dist, bearing = distance_bearing_grid(frame.lats, frame.lons, home_lat, home_lon)
    values = frame.values
    in_now = dist <= now_radius_km
    in_alert = dist <= alert_radius_km

    rate_at_house = float(values[in_now].max()) if in_now.any() else 0.0
    raining_now = in_now.any() and rate_at_house >= now_threshold

    qualifying = in_alert & (values >= nearby_threshold)
    n = int(qualifying.sum())
    max_value = float(values[in_alert].max()) if in_alert.any() else 0.0

    nearest_km = nearest_bearing = None
    if n:
        idx = np.argmin(np.where(qualifying, dist, np.inf))
        j, i = np.unravel_index(idx, dist.shape)
        nearest_km, nearest_bearing = float(dist[j, i]), float(bearing[j, i])

    return Detection(
        product=frame.product,
        raining_now=bool(raining_now),
        rate_at_house=rate_at_house,
        qualifying_cells=n,
        nearby=n >= min_cells,
        nearest_km=nearest_km,
        nearest_bearing_deg=nearest_bearing,
        max_value=max_value,
    )
```
Then move the two imports to the top of the file (ruff `I` will insist).

- [ ] **Step 4: Run tests, lint, commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/detect.py tests/test_detect.py && git commit -m "feat: radius detection with MIN_CELLS, nearest echo distance and bearing"
```

- [ ] **Step 5: Open the phase 1 PR (do not merge)**

```bash
git push -u origin phase-1-detector
gh pr create --base main --title "Phase 1: skeleton, config, detector" --body "$(cat <<'EOF'
Implements phase 1 of docs/superpowers/plans/2026-09-23-astrorainprotect.md:
project skeleton, env config with validation, Frame container, pure radius detector with tests.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Tell the user the PR is open and wait for their merge decision before treating `main` as updated. Continue phase 2 on a branch off `phase-1-detector` regardless.

---

## Phase 2 — MRMS fetch, decode, subset

### Task 5: mrms listing and key selection

**Files:**
- Create: `astrorainprotect/mrms.py`
- Test: `tests/test_mrms.py`, `tests/fixtures/listing.xml`

**Interfaces:**
- Produces: `PRODUCTS = {"reflectivity": "MergedReflectivityQCComposite_00.50", "preciprate": "PrecipRate_00.00"}`; `BUCKET_URL = "https://noaa-mrms-pds.s3.amazonaws.com"`; `day_prefixes(product: str, now: datetime) -> list[str]`; `parse_listing(xml: str) -> list[str]`; `key_time(key: str) -> datetime`; `newest_key(keys: Iterable[str]) -> str | None`; `list_keys(client: httpx.Client, prefix: str) -> list[str]`; `MrmsError(Exception)`.

- [ ] **Step 1: Create branch**

```bash
git checkout -b phase-2-mrms
```

- [ ] **Step 2: Write fixture and failing tests**

`tests/fixtures/listing.xml`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>noaa-mrms-pds</Name><Prefix>CONUS/PrecipRate_00.00/20260924/</Prefix><KeyCount>3</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated><Contents><Key>CONUS/PrecipRate_00.00/20260924/MRMS_PrecipRate_00.00_20260924-000000.grib2.gz</Key><LastModified>2026-09-24T00:01:10.000Z</LastModified><ETag>"a"</ETag><Size>743650</Size><StorageClass>STANDARD</StorageClass></Contents><Contents><Key>CONUS/PrecipRate_00.00/20260924/MRMS_PrecipRate_00.00_20260924-000400.grib2.gz</Key><LastModified>2026-09-24T00:05:10.000Z</LastModified><ETag>"b"</ETag><Size>743650</Size><StorageClass>STANDARD</StorageClass></Contents><Contents><Key>CONUS/PrecipRate_00.00/20260924/MRMS_PrecipRate_00.00_20260924-000200.grib2.gz</Key><LastModified>2026-09-24T00:03:10.000Z</LastModified><ETag>"c"</ETag><Size>743650</Size><StorageClass>STANDARD</StorageClass></Contents></ListBucketResult>
```

`tests/test_mrms.py`:
```python
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from astrorainprotect.mrms import (
    BUCKET_URL,
    PRODUCTS,
    MrmsError,
    day_prefixes,
    key_time,
    list_keys,
    newest_key,
    parse_listing,
)

FIX = Path(__file__).parent / "fixtures"


def test_products():
    assert PRODUCTS["preciprate"] == "PrecipRate_00.00"
    assert PRODUCTS["reflectivity"] == "MergedReflectivityQCComposite_00.50"


def test_day_prefixes_midday():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    assert day_prefixes("preciprate", now) == ["CONUS/PrecipRate_00.00/20260924/"]


def test_day_prefixes_near_midnight():
    now = datetime(2026, 9, 24, 0, 3, tzinfo=UTC)
    assert day_prefixes("reflectivity", now) == [
        "CONUS/MergedReflectivityQCComposite_00.50/20260924/",
        "CONUS/MergedReflectivityQCComposite_00.50/20260923/",
    ]


def test_day_prefixes_ten_minutes_after_midnight_is_single():
    now = datetime(2026, 9, 24, 0, 10, 1, tzinfo=UTC)
    assert len(day_prefixes("preciprate", now)) == 1


def test_parse_listing():
    keys = parse_listing((FIX / "listing.xml").read_text())
    assert len(keys) == 3
    assert keys[0].endswith("20260924-000000.grib2.gz")


def test_parse_listing_empty():
    xml = ('<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
           '<KeyCount>0</KeyCount></ListBucketResult>')
    assert parse_listing(xml) == []


def test_key_time():
    k = "CONUS/MergedReflectivityQCComposite_00.50/20260924/MRMS_MergedReflectivityQCComposite_00.50_20260924-011040.grib2.gz"
    assert key_time(k) == datetime(2026, 9, 24, 1, 10, 40, tzinfo=UTC)


def test_key_time_bad():
    with pytest.raises(MrmsError):
        key_time("CONUS/x/whatever.grib2.gz")


def test_newest_key_by_timestamp_not_order():
    keys = parse_listing((FIX / "listing.xml").read_text())
    assert newest_key(keys).endswith("20260924-000400.grib2.gz")
    assert newest_key([]) is None


def test_list_keys_uses_list_type_2():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text=(FIX / "listing.xml").read_text())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    keys = list_keys(client, "CONUS/PrecipRate_00.00/20260924/")
    assert len(keys) == 3
    assert seen["url"].startswith(BUCKET_URL + "/?")
    assert "list-type=2" in seen["url"]
    assert "prefix=CONUS%2FPrecipRate_00.00%2F20260924%2F" in seen["url"]
    assert "max-keys=1000" in seen["url"]


def test_list_keys_http_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="slow down")))
    with pytest.raises(MrmsError, match="503"):
        list_keys(client, "CONUS/PrecipRate_00.00/20260924/")


def test_list_keys_network_error():
    def boom(request):
        raise httpx.ConnectError("no route")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(MrmsError, match="no route"):
        list_keys(client, "CONUS/PrecipRate_00.00/20260924/")
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_mrms.py -q`
Expected: ImportError.

- [ ] **Step 4: Implement listing part of mrms.py**

```python
"""NOAA MRMS on AWS Open Data: listing, download, decode, subset."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import httpx

BUCKET_URL = "https://noaa-mrms-pds.s3.amazonaws.com"
PRODUCTS = {
    "reflectivity": "MergedReflectivityQCComposite_00.50",
    "preciprate": "PrecipRate_00.00",
}
MIDNIGHT_GRACE = timedelta(minutes=10)
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_KEY_TIME = re.compile(r"_(\d{8})-(\d{6})\.grib2\.gz$")


class MrmsError(Exception):
    """Listing, download, or decode failure. Message says which."""


def day_prefixes(product: str, now: datetime) -> list[str]:
    """Today's prefix, plus yesterday's within MIDNIGHT_GRACE of 00:00 UTC."""
    now = now.astimezone(UTC)
    path = PRODUCTS[product]
    days = [now]
    if now - now.replace(hour=0, minute=0, second=0, microsecond=0) < MIDNIGHT_GRACE:
        days.append(now - timedelta(days=1))
    return [f"CONUS/{path}/{d:%Y%m%d}/" for d in days]


def parse_listing(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [el.text for el in root.iter(f"{_S3_NS}Key") if el.text]


def key_time(key: str) -> datetime:
    m = _KEY_TIME.search(key)
    if not m:
        raise MrmsError(f"cannot parse timestamp from key {key!r}")
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def newest_key(keys: Iterable[str]) -> str | None:
    keys = list(keys)
    return max(keys, key=key_time) if keys else None


def list_keys(client: httpx.Client, prefix: str) -> list[str]:
    try:
        r = client.get(f"{BUCKET_URL}/", params={"list-type": "2", "prefix": prefix,
                                                  "max-keys": "1000"}, timeout=20.0)
    except httpx.HTTPError as exc:
        raise MrmsError(f"S3 listing failed for {prefix}: {exc}") from exc
    if r.status_code != 200:
        raise MrmsError(f"S3 listing returned HTTP {r.status_code} for {prefix}")
    return parse_listing(r.text)
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/mrms.py tests/test_mrms.py tests/fixtures/listing.xml && git commit -m "feat: MRMS bucket listing and newest-key selection with midnight handling"
```

---

### Task 6: GRIB decode and subset

**Files:**
- Modify: `astrorainprotect/mrms.py`
- Test: `tests/test_mrms.py` (append)

**Interfaces:**
- Produces: `GridMeta` dataclass (`lat0, lon0, dlat, dlon: float; ni, nj: int`); `decode_grib(data: bytes) -> tuple[GridMeta, np.ndarray]` (values shaped `(nj, ni)`, raw, unclamped); `subset(values, meta, lat, lon, half_deg) -> tuple[np.ndarray, np.ndarray, np.ndarray]` (lats, lons, sub); `MISSING_BELOW = {"preciprate": 0.0, "reflectivity": -990.0}`; `to_frame(product, valid_time, lats, lons, sub) -> Frame`; `fetch_grib(client, key) -> bytes` (gunzipped).

- [ ] **Step 1: Append failing tests**

```python
import gzip  # noqa: E402

import numpy as np  # noqa: E402

from astrorainprotect.mrms import (  # noqa: E402
    MISSING_BELOW,
    GridMeta,
    decode_grib,
    fetch_grib,
    subset,
    to_frame,
)

CONUS = GridMeta(lat0=54.995, lon0=230.005, dlat=0.01, dlon=0.01, ni=7000, nj=3500)


def test_subset_negative_longitude():
    values = np.zeros((CONUS.nj, CONUS.ni), dtype=np.float32)
    # mark the cell nearest the house
    j = round((54.995 - 29.97) / 0.01)
    i = round(((-95.67 % 360) - 230.005) / 0.01)
    values[j, i] = 7.0
    lats, lons, sub = subset(values, CONUS, 29.97, -95.67, 0.5)
    assert sub.shape == (101, 101)
    assert sub[50, 50] == 7.0
    assert lats[50] == pytest.approx(29.97, abs=0.011)   # nearest grid row/col, half a cell off at most
    assert lons[50] == pytest.approx(-95.67, abs=0.011)
    assert lats[0] > lats[-1]
    assert lons[0] < lons[-1]
    assert -180 <= lons.min() and lons.max() <= 180


def test_subset_clipped_at_grid_edge():
    values = np.zeros((CONUS.nj, CONUS.ni), dtype=np.float32)
    lats, lons, sub = subset(values, CONUS, 54.9, -129.9, 0.5)
    assert sub.shape[0] < 101 and sub.shape[1] < 101
    assert sub.shape == (len(lats), len(lons))


def test_to_frame_preciprate_missing():
    sub = np.array([[-3.0, -1.0], [0.5, 2.0]], dtype=np.float32)
    f = to_frame("preciprate", datetime(2026, 9, 24, tzinfo=UTC), np.array([1.0, 0.0]), np.array([0.0, 1.0]), sub)
    assert f.missing_fraction == pytest.approx(0.5)
    assert f.values.min() >= 0
    assert f.values[1, 1] == 2.0
    assert f.values.dtype == np.float32


def test_to_frame_reflectivity_missing():
    sub = np.array([[-999.0, -99.0], [-5.0, 42.0]], dtype=np.float32)
    f = to_frame("reflectivity", datetime(2026, 9, 24, tzinfo=UTC), np.array([1.0, 0.0]), np.array([0.0, 1.0]), sub)
    assert f.missing_fraction == pytest.approx(0.25)     # only -999 is missing
    assert f.values.min() >= 0                            # -99 and -5 clamp to 0
    assert f.values[1, 1] == 42.0


def test_missing_below_table():
    assert MISSING_BELOW == {"preciprate": 0.0, "reflectivity": -990.0}


def test_fetch_grib_gunzips():
    payload = gzip.compress(b"GRIB-bytes")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)))
    assert fetch_grib(client, "CONUS/x/y.grib2.gz") == b"GRIB-bytes"


def test_fetch_grib_404():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(MrmsError, match="404"):
        fetch_grib(client, "CONUS/x/y.grib2.gz")


def test_fetch_grib_bad_gzip():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"nope")))
    with pytest.raises(MrmsError, match="gunzip"):
        fetch_grib(client, "CONUS/x/y.grib2.gz")


def test_decode_garbage():
    with pytest.raises(MrmsError, match="decode"):
        decode_grib(b"not a grib")


@pytest.mark.integration
def test_decode_real_file():
    """Downloads the newest PrecipRate file. Skipped when S3 is unreachable."""
    client = httpx.Client()
    try:
        keys = []
        for p in day_prefixes("preciprate", datetime.now(UTC)):
            keys += list_keys(client, p)
    except MrmsError as exc:
        pytest.skip(f"offline: {exc}")
    key = newest_key(keys)
    assert key
    meta, values = decode_grib(fetch_grib(client, key))
    assert (meta.ni, meta.nj) == (7000, 3500)
    assert meta.lat0 == pytest.approx(54.995)
    assert meta.lon0 == pytest.approx(230.005)
    assert values.shape == (3500, 7000)
    lats, lons, sub = subset(values, meta, 29.97, -95.67, 0.5)
    f = to_frame("preciprate", key_time(key), lats, lons, sub)
    assert f.values.shape == (101, 101)
    assert 0 <= f.values.max() < 500
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_mrms.py -q`
Expected: ImportError for `GridMeta`.

- [ ] **Step 3: Implement decode, subset, to_frame, fetch_grib**

Append to `astrorainprotect/mrms.py` (add the new imports at the top: `import gzip`, `from dataclasses import dataclass`, `import numpy as np`, `import eccodes`, `from astrorainprotect.frame import Frame`):
```python
MISSING_BELOW = {"preciprate": 0.0, "reflectivity": -990.0}


@dataclass(frozen=True)
class GridMeta:
    lat0: float   # latitude of row 0 (north edge)
    lon0: float   # longitude of column 0, 0-360 convention
    dlat: float
    dlon: float
    ni: int       # columns
    nj: int       # rows


def fetch_grib(client: httpx.Client, key: str) -> bytes:
    try:
        r = client.get(f"{BUCKET_URL}/{key}", timeout=60.0)
    except httpx.HTTPError as exc:
        raise MrmsError(f"S3 download failed for {key}: {exc}") from exc
    if r.status_code != 200:
        raise MrmsError(f"S3 download returned HTTP {r.status_code} for {key}")
    try:
        return gzip.decompress(r.content)
    except (OSError, EOFError) as exc:
        raise MrmsError(f"gunzip failed for {key}: {exc}") from exc


def decode_grib(data: bytes) -> tuple[GridMeta, np.ndarray]:
    try:
        gid = eccodes.codes_new_from_message(data)
    except Exception as exc:  # eccodes raises several types
        raise MrmsError(f"GRIB decode failed: {exc}") from exc
    try:
        meta = GridMeta(
            lat0=float(eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")),
            lon0=float(eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")),
            dlat=float(eccodes.codes_get(gid, "jDirectionIncrementInDegrees")),
            dlon=float(eccodes.codes_get(gid, "iDirectionIncrementInDegrees")),
            ni=int(eccodes.codes_get(gid, "Ni")),
            nj=int(eccodes.codes_get(gid, "Nj")),
        )
        if int(eccodes.codes_get(gid, "scanningMode")) != 0:
            raise MrmsError("GRIB decode failed: unexpected scanningMode")
        values = eccodes.codes_get_values(gid).reshape(meta.nj, meta.ni)
    except MrmsError:
        raise
    except Exception as exc:
        raise MrmsError(f"GRIB decode failed: {exc}") from exc
    finally:
        eccodes.codes_release(gid)
    return meta, values


def subset(values: np.ndarray, meta: GridMeta, lat: float, lon: float, half_deg: float):
    """Cut a (2*half_deg) box around lat/lon. Returns (lats, lons, sub) with lons in -180..180."""
    lon360 = lon % 360.0
    jc = int(round((meta.lat0 - lat) / meta.dlat))
    ic = int(round((lon360 - meta.lon0) / meta.dlon))
    h = int(round(half_deg / meta.dlat))
    j0, j1 = max(jc - h, 0), min(jc + h + 1, meta.nj)
    i0, i1 = max(ic - h, 0), min(ic + h + 1, meta.ni)
    lats = meta.lat0 - meta.dlat * np.arange(j0, j1)
    lons = ((meta.lon0 + meta.dlon * np.arange(i0, i1)) + 180.0) % 360.0 - 180.0
    return lats, lons, np.array(values[j0:j1, i0:i1], dtype=np.float32)


def to_frame(product: str, valid_time: datetime, lats: np.ndarray, lons: np.ndarray,
             sub: np.ndarray) -> Frame:
    missing = sub < MISSING_BELOW[product]
    return Frame(
        product=product,
        valid_time=valid_time,
        lats=lats,
        lons=lons,
        values=np.maximum(sub, 0.0).astype(np.float32),
        missing_fraction=float(missing.mean()) if sub.size else 1.0,
    )
```

- [ ] **Step 4: Run all tests including integration, lint, commit**

Run: `.venv/bin/pytest -q -m "not integration" && .venv/bin/pytest -q -m integration && .venv/bin/ruff check .`
Expected: unit tests pass; the integration test passes online (takes a few seconds).

```bash
git add astrorainprotect/mrms.py tests/test_mrms.py && git commit -m "feat: GRIB decode with eccodes, house-box subset, missing-value rules"
```

---

### Task 7: RadarSource with per-product cache

**Files:**
- Modify: `astrorainprotect/mrms.py`
- Test: `tests/test_mrms.py` (append)

**Interfaces:**
- Produces: `class RadarSource(client, lat, lon, *, half_deg=0.5, cache=5)` with `fetch_latest(product: str, now: datetime) -> Frame | None` (returns the cached newest frame when the bucket's newest key has the same timestamp; `None` when the listing is empty; raises `MrmsError` on failure) and `frames(product) -> list[Frame]` (oldest first, up to `cache`).

- [ ] **Step 1: Append failing tests**

```python
from astrorainprotect.mrms import RadarSource  # noqa: E402


def _listing(keys):
    items = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"{items}</ListBucketResult>")


class FakeBucket:
    """Serves listings and one canned gzipped payload; decode is monkeypatched."""

    def __init__(self, keys):
        self.keys = keys
        self.downloads = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text=_listing(self.keys))
        self.downloads.append(request.url.path.lstrip("/"))
        return httpx.Response(200, content=gzip.compress(b"fake"))


def _fake_decode(data):
    assert data == b"fake"
    values = np.zeros((CONUS.nj, CONUS.ni), dtype=np.float32)
    return CONUS, values


@pytest.fixture
def fake_decode(monkeypatch):
    monkeypatch.setattr("astrorainprotect.mrms.decode_grib", _fake_decode)


K1 = "CONUS/PrecipRate_00.00/20260924/MRMS_PrecipRate_00.00_20260924-120000.grib2.gz"
K2 = "CONUS/PrecipRate_00.00/20260924/MRMS_PrecipRate_00.00_20260924-120200.grib2.gz"
NOW = datetime(2026, 9, 24, 12, 3, tzinfo=UTC)


def test_radar_source_downloads_newest_once(fake_decode):
    bucket = FakeBucket([K1])
    src = RadarSource(httpx.Client(transport=httpx.MockTransport(bucket)), 29.97, -95.67)
    f1 = src.fetch_latest("preciprate", NOW)
    f2 = src.fetch_latest("preciprate", NOW)
    assert f1 is f2
    assert bucket.downloads == [K1]
    assert f1.valid_time == datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    assert f1.values.shape == (101, 101)
    assert src.frames("preciprate") == [f1]


def test_radar_source_cache_order_and_limit(fake_decode):
    bucket = FakeBucket([K1])
    src = RadarSource(httpx.Client(transport=httpx.MockTransport(bucket)), 29.97, -95.67, cache=2)
    a = src.fetch_latest("preciprate", NOW)
    bucket.keys = [K1, K2]
    b = src.fetch_latest("preciprate", NOW)
    assert src.frames("preciprate") == [a, b]
    bucket.keys = [K1, K2, K2.replace("120200", "120400")]
    c = src.fetch_latest("preciprate", NOW)
    assert src.frames("preciprate") == [b, c]


def test_radar_source_empty_listing(fake_decode):
    src = RadarSource(httpx.Client(transport=httpx.MockTransport(FakeBucket([]))), 29.97, -95.67)
    assert src.fetch_latest("preciprate", NOW) is None
    assert src.frames("preciprate") == []


def test_radar_source_products_are_independent(fake_decode):
    kr = "CONUS/MergedReflectivityQCComposite_00.50/20260924/MRMS_MergedReflectivityQCComposite_00.50_20260924-120040.grib2.gz"
    bucket = FakeBucket([K1, kr])
    src = RadarSource(httpx.Client(transport=httpx.MockTransport(bucket)), 29.97, -95.67)
    p = src.fetch_latest("preciprate", NOW)
    r = src.fetch_latest("reflectivity", NOW)
    assert p.product == "preciprate" and r.product == "reflectivity"
    assert src.frames("reflectivity") == [r]


def test_radar_source_propagates_errors(fake_decode):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    src = RadarSource(client, 29.97, -95.67)
    with pytest.raises(MrmsError):
        src.fetch_latest("preciprate", NOW)
```

Note: `FakeBucket` returns every key for any prefix; `RadarSource` must filter listing results by the product path so the reflectivity key is not picked for preciprate. The independence test pins that.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_mrms.py -q`
Expected: ImportError for `RadarSource`.

- [ ] **Step 3: Implement RadarSource**

Append to `astrorainprotect/mrms.py` (add `from collections import deque` at top):
```python
class RadarSource:
    """Fetches the newest frame per product and keeps a short history for motion estimation."""

    def __init__(self, client: httpx.Client, lat: float, lon: float, *,
                 half_deg: float = 0.5, cache: int = 5) -> None:
        self._client = client
        self._lat, self._lon, self._half = lat, lon, half_deg
        self._frames: dict[str, deque[Frame]] = {p: deque(maxlen=cache) for p in PRODUCTS}

    def frames(self, product: str) -> list[Frame]:
        return list(self._frames[product])

    def fetch_latest(self, product: str, now: datetime) -> Frame | None:
        path = f"CONUS/{PRODUCTS[product]}/"
        keys: list[str] = []
        for prefix in day_prefixes(product, now):
            keys += [k for k in list_keys(self._client, prefix) if k.startswith(path)]
        key = newest_key(keys)
        if key is None:
            return None
        valid_time = key_time(key)
        history = self._frames[product]
        if history and history[-1].valid_time == valid_time:
            return history[-1]
        meta, values = decode_grib(fetch_grib(self._client, key))
        lats, lons, sub = subset(values, meta, self._lat, self._lon, self._half)
        del values
        frame = to_frame(product, valid_time, lats, lons, sub)
        history.append(frame)
        return frame
```

- [ ] **Step 4: Run tests, lint, commit, open PR**

Run: `.venv/bin/pytest -q -m "not integration" && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/mrms.py tests/test_mrms.py && git commit -m "feat: RadarSource with newest-frame download and per-product frame cache"
git push -u origin phase-2-mrms
gh pr create --base phase-1-detector --title "Phase 2: MRMS listing, decode, subset, frame cache" --body "$(cat <<'EOF'
Implements phase 2 of docs/superpowers/plans/2026-09-23-astrorainprotect.md.
Decoder: eccodes PyPI package used directly (see plan for spike results).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Do not merge. If phase 1 was merged already, use `--base main`.

---

## Phase 3 — alarm logic, state, scope gate, notifier, poll loop

### Task 8: alarm.py state machine

**Files:**
- Create: `astrorainprotect/alarm.py`
- Test: `tests/test_alarm.py`

**Interfaces:**
- Produces: `Action` enum (`SEND, REPEAT, SKIP, REARM, NONE`); `Trigger(source: str, eta_min: float | None, detail: str)` frozen; `AlarmInputs(triggers: tuple[Trigger, ...], raining_now: bool, latched: bool, latch_age_sec: float | None, repeat_min: int)` frozen; `Decision(action: Action, title: str = "", message: str = "")` frozen; `decide(inputs: AlarmInputs) -> Decision`; `compose_message(triggers) -> str`.
- Consumers: Task 12 builds `Trigger`s from detections and Pirate Weather results and applies the `Decision`.

- [ ] **Step 1: Create branch and write failing tests**

```bash
git checkout -b phase-3-loop
```

`tests/test_alarm.py`:
```python
import pytest

from astrorainprotect.alarm import Action, AlarmInputs, Decision, Trigger, compose_message, decide

RADAR = Trigger(source="radar", eta_min=None, detail="reflectivity 41 dBZ, 8.2 km to the SW")
RADAR_ETA = Trigger(source="radar", eta_min=12.0, detail="reflectivity 41 dBZ, 8.2 km to the SW")
PW = Trigger(source="pirate weather", eta_min=25.0, detail="prob 60%")


def inputs(triggers=(), raining_now=False, latched=False, latch_age_sec=None, repeat_min=0):
    return AlarmInputs(triggers=tuple(triggers), raining_now=raining_now, latched=latched,
                       latch_age_sec=latch_age_sec, repeat_min=repeat_min)


def test_first_alert():
    d = decide(inputs([RADAR]))
    assert d.action is Action.SEND
    assert d.title == "Rain incoming"
    assert "8.2 km to the SW" in d.message


def test_latched_skips():
    d = decide(inputs([RADAR], latched=True, latch_age_sec=60, repeat_min=0))
    assert d.action is Action.SKIP


def test_repeat_after_interval():
    d = decide(inputs([RADAR_ETA], latched=True, latch_age_sec=601, repeat_min=10))
    assert d.action is Action.REPEAT
    assert d.title == "Rain incoming (still)"
    assert "12 min" in d.message


def test_repeat_exactly_at_interval():
    assert decide(inputs([RADAR], latched=True, latch_age_sec=600, repeat_min=10)).action is Action.REPEAT


def test_no_repeat_before_interval():
    assert decide(inputs([RADAR], latched=True, latch_age_sec=599, repeat_min=10)).action is Action.SKIP


def test_no_repeat_when_disabled():
    assert decide(inputs([RADAR], latched=True, latch_age_sec=99999, repeat_min=0)).action is Action.SKIP


def test_rearm_when_no_trigger():
    assert decide(inputs([], latched=True, latch_age_sec=5)).action is Action.REARM


def test_rearm_when_raining_now_even_with_triggers():
    assert decide(inputs([RADAR, PW], raining_now=True, latched=True, latch_age_sec=5)).action is Action.REARM


def test_raining_now_unlatched_is_none():
    assert decide(inputs([RADAR], raining_now=True, latched=False)).action is Action.NONE


def test_nothing_to_do():
    assert decide(inputs([])).action is Action.NONE


@pytest.mark.parametrize("age", [None, 0.0])
def test_latched_with_unknown_or_zero_age_never_repeats(age):
    assert decide(inputs([RADAR], latched=True, latch_age_sec=age, repeat_min=1)).action is Action.SKIP


def test_message_uses_smallest_eta_and_lists_sources():
    msg = compose_message((PW, RADAR_ETA))
    assert msg.startswith("Rain expected in about 12 min")
    assert "radar: reflectivity 41 dBZ, 8.2 km to the SW" in msg
    assert "pirate weather: prob 60%" in msg


def test_message_without_eta_uses_detail():
    msg = compose_message((RADAR,))
    assert msg.startswith("Rain nearby: reflectivity 41 dBZ, 8.2 km to the SW")


def test_decision_is_frozen():
    d = Decision(Action.NONE)
    with pytest.raises(AttributeError):
        d.title = "x"  # type: ignore[misc]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_alarm.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement alarm.py**

```python
"""The alert state machine, ported from legacy/rain-check.sh. Pure."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class Action(Enum):
    SEND = auto()
    REPEAT = auto()
    SKIP = auto()
    REARM = auto()
    NONE = auto()


@dataclass(frozen=True)
class Trigger:
    source: str            # "radar" or "pirate weather"
    eta_min: float | None  # minutes until rain, when known
    detail: str            # human text for the message body


@dataclass(frozen=True)
class AlarmInputs:
    triggers: tuple[Trigger, ...]
    raining_now: bool
    latched: bool
    latch_age_sec: float | None
    repeat_min: int


@dataclass(frozen=True)
class Decision:
    action: Action
    title: str = ""
    message: str = ""


TITLE_FIRST = "Rain incoming"
TITLE_REPEAT = "Rain incoming (still)"


def compose_message(triggers: tuple[Trigger, ...]) -> str:
    etas = [t.eta_min for t in triggers if t.eta_min is not None]
    sources = "; ".join(f"{t.source}: {t.detail}" for t in triggers)
    if etas:
        return f"Rain expected in about {round(min(etas))} min ({sources})"
    return f"Rain nearby: {sources}"


def decide(inputs: AlarmInputs) -> Decision:
    # Already raining: nothing left to warn about. Clearing the latch lets the next cell alert.
    active = () if inputs.raining_now else inputs.triggers
    if active:
        if not inputs.latched:
            return Decision(Action.SEND, TITLE_FIRST, compose_message(active))
        age = inputs.latch_age_sec
        if inputs.repeat_min > 0 and age is not None and age >= inputs.repeat_min * 60:
            return Decision(Action.REPEAT, TITLE_REPEAT, compose_message(active))
        return Decision(Action.SKIP)
    if inputs.latched:
        return Decision(Action.REARM)
    return Decision(Action.NONE)
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `.venv/bin/pytest tests/test_alarm.py -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/alarm.py tests/test_alarm.py && git commit -m "feat: alarm state machine ported from the legacy shell script"
```

---

### Task 9: state.py

**Files:**
- Create: `astrorainprotect/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `class State(state_dir: Path | str, tmp_dir: Path | str = "/tmp")` with `latched() -> bool`, `latch_age_sec(now: float) -> float | None`, `set_latch(now: float)`, `clear_latch()`, `test_sent() -> bool`, `mark_test_sent()`, `clear_test_marker()`, `heartbeat(now: float)`, `heartbeat_age_sec(now: float) -> float | None`. `now` is a POSIX timestamp (`time.time()`), so tests can pass fixed values.

- [ ] **Step 1: Write failing tests**

`tests/test_state.py`:
```python
from astrorainprotect.state import State


def test_latch_lifecycle(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    assert not s.latched()
    assert s.latch_age_sec(1000.0) is None
    s.set_latch(1000.0)
    assert s.latched()
    assert s.latch_age_sec(1060.0) == 60.0
    s.set_latch(1100.0)                          # re-touch resets the timer
    assert s.latch_age_sec(1130.0) == 30.0
    s.clear_latch()
    assert not s.latched()
    s.clear_latch()                              # idempotent


def test_latch_persists_across_instances(tmp_path):
    State(tmp_path / "state").set_latch(5.0)
    assert State(tmp_path / "state").latched()


def test_state_dir_created(tmp_path):
    s = State(tmp_path / "missing" / "state")
    s.set_latch(1.0)
    assert (tmp_path / "missing" / "state" / "alerted").exists()


def test_test_marker(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    assert not s.test_sent()
    s.mark_test_sent()
    assert s.test_sent()


def test_clear_test_marker_on_start(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    s.mark_test_sent()
    s.clear_test_marker()
    assert not s.test_sent()
    assert State(tmp_path / "state", tmp_path / "tmp").latched() is False


def test_heartbeat(tmp_path):
    s = State(tmp_path / "state", tmp_path / "tmp")
    assert s.heartbeat_age_sec(10.0) is None
    s.heartbeat(10.0)
    assert s.heartbeat_age_sec(25.0) == 15.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_state.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement state.py**

```python
"""Latch, repeat timer, test marker, heartbeat. All file-backed so restarts keep the latch."""

from __future__ import annotations

import os
from pathlib import Path


class State:
    def __init__(self, state_dir: Path | str, tmp_dir: Path | str = "/tmp") -> None:
        self._dir = Path(state_dir)
        self._latch = self._dir / "alerted"
        self._heartbeat = self._dir / "heartbeat"
        self._test_marker = Path(tmp_dir) / "astrorainprotect_test_sent"

    def _touch(self, path: Path, now: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (now, now))

    @staticmethod
    def _age(path: Path, now: float) -> float | None:
        try:
            return now - path.stat().st_mtime
        except FileNotFoundError:
            return None

    def latched(self) -> bool:
        return self._latch.exists()

    def latch_age_sec(self, now: float) -> float | None:
        return self._age(self._latch, now)

    def set_latch(self, now: float) -> None:
        self._touch(self._latch, now)

    def clear_latch(self) -> None:
        self._latch.unlink(missing_ok=True)

    def test_sent(self) -> bool:
        return self._test_marker.exists()

    def mark_test_sent(self) -> None:
        self._test_marker.parent.mkdir(parents=True, exist_ok=True)
        self._test_marker.touch()

    def clear_test_marker(self) -> None:
        self._test_marker.unlink(missing_ok=True)

    def heartbeat(self, now: float) -> None:
        self._touch(self._heartbeat, now)

    def heartbeat_age_sec(self, now: float) -> float | None:
        return self._age(self._heartbeat, now)
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `.venv/bin/pytest tests/test_state.py -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/state.py tests/test_state.py && git commit -m "feat: file-backed latch, test marker and heartbeat"
```

---

### Task 10: scope.py

**Files:**
- Create: `astrorainprotect/scope.py`
- Test: `tests/test_scope.py`

**Interfaces:**
- Produces: `parse_hosts(spec: str) -> list[tuple[str, int]]` (default port 4700, blanks ignored); `online_hosts(hosts: list[tuple[str, int]], timeout: float = 2.0) -> list[str]` (names of hosts that accepted a TCP connect).

- [ ] **Step 1: Write failing tests**

`tests/test_scope.py`:
```python
import socket
import threading

from astrorainprotect.scope import online_hosts, parse_hosts


def test_parse_hosts():
    assert parse_hosts("") == []
    assert parse_hosts("10.0.0.5") == [("10.0.0.5", 4700)]
    assert parse_hosts("seestar.lan:5555, 10.0.0.6 ,,") == [("seestar.lan", 5555), ("10.0.0.6", 4700)]


def test_parse_hosts_bad_port_falls_back_to_default():
    assert parse_hosts("host:abc") == [("host", 4700)]


def test_online_hosts_detects_listening_socket():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except TimeoutError:
                pass

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        assert online_hosts([("127.0.0.1", port)], timeout=1.0) == ["127.0.0.1"]
    finally:
        stop.set()
        t.join()
        srv.close()


def test_online_hosts_closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                     # nothing listens here now
    assert online_hosts([("127.0.0.1", port)], timeout=0.5) == []


def test_online_hosts_unresolvable():
    assert online_hosts([("no-such-host.invalid", 4700)], timeout=0.5) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_scope.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement scope.py**

```python
"""Scope-online gate: is at least one Seestar reachable on its JSON-RPC port?"""

from __future__ import annotations

import socket

DEFAULT_PORT = 4700


def parse_hosts(spec: str) -> list[tuple[str, int]]:
    hosts: list[tuple[str, int]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.partition(":")
        try:
            hosts.append((host.strip(), int(port) if port else DEFAULT_PORT))
        except ValueError:
            hosts.append((host.strip(), DEFAULT_PORT))
    return hosts


def online_hosts(hosts: list[tuple[str, int]], timeout: float = 2.0) -> list[str]:
    online = []
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                online.append(host)
        except OSError:
            continue
    return online
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `.venv/bin/pytest tests/test_scope.py -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/scope.py tests/test_scope.py && git commit -m "feat: scope-online TCP gate"
```

---

### Task 11: notify.py

**Files:**
- Create: `astrorainprotect/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `class Notifier(client: httpx.Client, url: str, token: str = "", priority: str = "high")` with `send(title: str, message: str) -> bool`; logs at ERROR with status and up to 300 bytes of body on failure, never raises. `TAGS = "loud_sound,bell"`.

- [ ] **Step 1: Write failing tests**

`tests/test_notify.py`:
```python
import logging

import httpx

from astrorainprotect.notify import TAGS, Notifier


def make(handler, **kw):
    return Notifier(httpx.Client(transport=httpx.MockTransport(handler)), "https://ntfy.example.net/rain", **kw)


def test_send_headers_with_token():
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers), body=request.content, url=str(request.url))
        return httpx.Response(200, json={"id": "x"})

    assert make(handler, token="tk_abc", priority="urgent").send("Rain incoming", "hello") is True
    assert seen["url"] == "https://ntfy.example.net/rain"
    assert seen["body"] == b"hello"
    assert seen["headers"]["title"] == "Rain incoming"
    assert seen["headers"]["priority"] == "urgent"
    assert seen["headers"]["tags"] == TAGS == "loud_sound,bell"
    assert seen["headers"]["authorization"] == "Bearer tk_abc"


def test_send_without_token_has_no_auth_header():
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers))
        return httpx.Response(200)

    make(handler).send("t", "m")
    assert "authorization" not in seen["headers"]
    assert seen["headers"]["priority"] == "high"


def test_send_failure_logs_status_and_body(caplog):
    caplog.set_level(logging.ERROR)
    n = make(lambda r: httpx.Response(403, text="forbidden " + "x" * 500))
    assert n.send("t", "m") is False
    rec = caplog.records[-1].getMessage()
    assert "403" in rec and "forbidden" in rec and len(rec) < 450


def test_send_network_error(caplog):
    caplog.set_level(logging.ERROR)

    def boom(request):
        raise httpx.ConnectError("refused")

    assert make(boom).send("t", "m") is False
    assert "refused" in caplog.records[-1].getMessage()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_notify.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement notify.py**

```python
"""ntfy client. Same headers as legacy/rain-check.sh."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

TAGS = "loud_sound,bell"


class Notifier:
    def __init__(self, client: httpx.Client, url: str, token: str = "", priority: str = "high") -> None:
        self._client, self._url, self._token, self._priority = client, url, token, priority

    def send(self, title: str, message: str) -> bool:
        headers = {"Title": title, "Priority": self._priority, "Tags": TAGS}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            r = self._client.post(self._url, content=message.encode(), headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            log.error("ntfy send failed (%s): %s", self._url, exc)
            return False
        if r.status_code != 200:
            log.error("ntfy returned HTTP %d: %s", r.status_code, r.text[:300])
            return False
        return True
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `.venv/bin/pytest tests/test_notify.py -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/notify.py tests/test_notify.py && git commit -m "feat: ntfy notifier with legacy headers and failure logging"
```

---

### Task 12: app.py poll loop and __main__

**Files:**
- Create: `astrorainprotect/app.py`, `astrorainprotect/__main__.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `RADAR_MAX_AGE = timedelta(minutes=15)`, `RADAR_MAX_MISSING = 0.5`; `RadarStatus(available: bool, reason: str)`; `radar_status(frame: Frame | None, now: datetime) -> RadarStatus`; `class App` with fields `cfg: Config`, `radar` (anything with `fetch_latest(product, now)` and `frames(product)`), `notifier` (anything with `send(title, message) -> bool`), `state: State`, `scope_check: Callable[[], list[str]]`, `pirate` (`None` until Task 14; must have `check(now) -> PirateResult | None`), `clock: Callable[[], datetime]` (UTC-aware); `run_cycle(app: App) -> str` (returns the summary line it logged); `main(argv=None) -> int`.
- The direction filter hook is a no-op in this task: `app.py` calls `radar_trigger(...)` which Task 19 extends.

- [ ] **Step 1: Write failing tests**

`tests/test_app.py`:
```python
import logging
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from astrorainprotect.app import RADAR_MAX_AGE, App, radar_status, run_cycle
from astrorainprotect.config import load_config
from astrorainprotect.state import State
from tests.conftest import make_frame

NOW = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
BASE = {"LAT": "29.97", "LON": "-95.67", "NTFY_URL": "https://ntfy.example.net/rain"}


class FakeRadar:
    def __init__(self, preciprate=None, reflectivity=None, error=None):
        self.by_product = {"preciprate": preciprate, "reflectivity": reflectivity}
        self.error = error
        self.calls = 0

    def fetch_latest(self, product, now):
        self.calls += 1
        if self.error:
            raise self.error
        return self.by_product[product]

    def frames(self, product):
        f = self.by_product[product]
        return [f] if f else []


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, title, message):
        self.sent.append((title, message))
        return self.ok


def empty(product):
    return make_frame(np.zeros((101, 101)), product=product, valid_time=NOW - timedelta(minutes=2))


def storm(product, value):
    g = np.zeros((101, 101), dtype=np.float32)
    g[60:64, 40:44] = value                      # ~11 km SW
    return make_frame(g, product=product, valid_time=NOW - timedelta(minutes=2))


def raining(product, value):
    g = np.zeros((101, 101), dtype=np.float32)
    g[49:52, 49:52] = value
    return make_frame(g, product=product, valid_time=NOW - timedelta(minutes=2))


def build(tmp_path, env=None, radar=None, notifier=None, scope=None, now=NOW):
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path / "state"), **(env or {})})
    return App(cfg=cfg, radar=radar or FakeRadar(empty("preciprate"), empty("reflectivity")),
               notifier=notifier or FakeNotifier(), state=State(cfg.state_dir, tmp_path / "tmp"),
               scope_check=scope or (lambda: []), pirate=None, clock=lambda: now)


def test_radar_status():
    assert radar_status(None, NOW).available is False
    assert radar_status(empty("preciprate"), NOW).available is True
    old = make_frame(np.zeros((3, 3)), valid_time=NOW - RADAR_MAX_AGE - timedelta(seconds=1))
    assert radar_status(old, NOW).available is False and "stale" in radar_status(old, NOW).reason
    gappy = make_frame(np.zeros((3, 3)), valid_time=NOW, missing_fraction=0.6)
    assert radar_status(gappy, NOW).available is False and "missing" in radar_status(gappy, NOW).reason


def test_quiet_cycle_logs_summary(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    app = build(tmp_path)
    line = run_cycle(app)
    assert "frame=" in line and "age=" in line and "latched=0" in line
    assert app.notifier.sent == []
    assert app.state.heartbeat_age_sec(NOW.timestamp()) == 0.0


def test_reflectivity_storm_sends_alert(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    title, msg = app.notifier.sent[0]
    assert title == "Rain incoming"
    assert "SW" in msg and "reflectivity" in msg
    assert app.state.latched()


def test_preciprate_storm_sends_alert(tmp_path):
    app = build(tmp_path, radar=FakeRadar(storm("preciprate", 1.0), empty("reflectivity")))
    run_cycle(app)
    assert app.notifier.sent[0][0] == "Rain incoming"
    assert "preciprate" in app.notifier.sent[0][1]


def test_second_cycle_skips(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    run_cycle(app)
    assert len(app.notifier.sent) == 1


def test_repeat(tmp_path):
    app = build(tmp_path, env={"REPEAT_MIN": "10"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    app.clock = lambda: NOW + timedelta(minutes=11)
    app.radar.by_product["reflectivity"] = make_frame(app.radar.by_product["reflectivity"].values,
                                                      product="reflectivity", valid_time=NOW + timedelta(minutes=9))
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming", "Rain incoming (still)"]


def test_send_failure_does_not_latch(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)),
                notifier=FakeNotifier(ok=False))
    run_cycle(app)
    assert not app.state.latched()
    run_cycle(app)
    assert len(app.notifier.sent) == 2                # retried next poll


def test_raining_now_rearms(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.radar.by_product["preciprate"] = raining("preciprate", 1.0)
    line = run_cycle(app)
    assert not app.state.latched()
    assert "re-armed" in line or "rearm" in line.lower()


def test_radar_unavailable_leaves_latch(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.radar = FakeRadar(None, None)
    run_cycle(app)
    assert app.state.latched()
    assert any("radar unavailable" in r.getMessage() for r in caplog.records)


def test_radar_error_is_logged_and_loop_continues(tmp_path, caplog):
    from astrorainprotect.mrms import MrmsError
    caplog.set_level(logging.ERROR)
    app = build(tmp_path, radar=FakeRadar(error=MrmsError("S3 listing failed")))
    line = run_cycle(app)
    assert any("S3 listing failed" in r.getMessage() for r in caplog.records)
    assert "radar=unavailable" in line


def test_scope_offline_skips_and_clears_latch(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0))
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, radar=radar, scope=lambda: [])
    app.state.set_latch(NOW.timestamp())
    line = run_cycle(app)
    assert radar.calls == 0
    assert not app.state.latched()
    assert "no scope online" in line
    assert app.notifier.sent == []


def test_scope_online_runs_checks(tmp_path):
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0))
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, radar=radar, scope=lambda: ["10.0.0.5"])
    run_cycle(app)
    assert radar.calls == 2 and len(app.notifier.sent) == 1


def test_debug2_sends_one_test_notification(tmp_path):
    app = build(tmp_path, env={"DEBUG": "2"})
    run_cycle(app)
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain alert test"]


def test_debug2_test_marker_cleared_by_main_start(tmp_path):
    app = build(tmp_path, env={"DEBUG": "2"})
    app.state.mark_test_sent()
    app.state.clear_test_marker()
    run_cycle(app)
    assert app.notifier.sent[0][0] == "Rain alert test"


def test_main_missing_env_exits_nonzero(monkeypatch, capsys):
    from astrorainprotect.app import main
    monkeypatch.setattr("os.environ", {})
    assert main([]) == 2
    assert "LAT is required" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_app.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement app.py**

```python
"""Poll loop and wiring."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from astrorainprotect.alarm import Action, AlarmInputs, Trigger, decide
from astrorainprotect.config import Config, ConfigError, describe, load_config
from astrorainprotect.detect import Detection, detect
from astrorainprotect.frame import Frame
from astrorainprotect.mrms import MrmsError, RadarSource
from astrorainprotect.notify import Notifier
from astrorainprotect.scope import online_hosts, parse_hosts
from astrorainprotect.state import State

log = logging.getLogger("astrorainprotect")

RADAR_MAX_AGE = timedelta(minutes=15)
RADAR_MAX_MISSING = 0.5
TITLE_TEST = "Rain alert test"


@dataclass(frozen=True)
class RadarStatus:
    available: bool
    reason: str


def radar_status(frame: Frame | None, now: datetime) -> RadarStatus:
    if frame is None:
        return RadarStatus(False, "no frame")
    age = now - frame.valid_time
    if age > RADAR_MAX_AGE:
        return RadarStatus(False, f"stale frame ({age.total_seconds() / 60:.0f} min old)")
    if frame.missing_fraction > RADAR_MAX_MISSING:
        return RadarStatus(False, f"{frame.missing_fraction:.0%} of box missing")
    return RadarStatus(True, "ok")


@dataclass
class App:
    cfg: Config
    radar: Any            # RadarSource or a fake with the same two methods
    notifier: Any         # Notifier or a fake
    state: State
    scope_check: Callable[[], list[str]]
    pirate: Any           # PirateSource (Task 14) or None
    clock: Callable[[], datetime]
    failures: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = {"radar": 0, "pirate": 0, "ntfy": 0}


def _detect_product(app: App, product: str, now: datetime) -> tuple[Detection | None, RadarStatus]:
    frame = app.radar.fetch_latest(product, now)
    status = radar_status(frame, now)
    if not status.available:
        return None, status
    cfg = app.cfg
    if product == "reflectivity":
        d = detect(frame, cfg.lat, cfg.lon, now_radius_km=cfg.now_radius_km,
                   alert_radius_km=cfg.alert_radius_km, now_threshold=float("inf"),
                   nearby_threshold=cfg.min_dbz, min_cells=cfg.min_cells)
    else:
        d = detect(frame, cfg.lat, cfg.lon, now_radius_km=cfg.now_radius_km,
                   alert_radius_km=cfg.alert_radius_km, now_threshold=cfg.raining_now,
                   nearby_threshold=cfg.min_intensity, min_cells=cfg.min_cells)
    return d, status


def radar_trigger(app: App, dets: list[Detection], now: datetime) -> Trigger | None:
    """Build the radar Trigger from qualifying detections. Task 19 adds the direction filter."""
    hits = [d for d in dets if d.nearby]
    if not hits:
        return None
    units = {"reflectivity": "dBZ", "preciprate": "mm/h"}
    detail = ", ".join(f"{d.product} {d.max_value:.0f} {units[d.product]} {d.describe()}" for d in hits)
    return Trigger(source="radar", eta_min=None, detail=detail)


def _maybe_send_test(app: App) -> None:
    if app.cfg.debug >= 2 and not app.state.test_sent():
        if app.notifier.send(TITLE_TEST, "Test from astrorainprotect; ntfy path works."):
            app.state.mark_test_sent()
            log.info("TEST notification sent")
        else:
            log.error("TEST notification failed")


def run_cycle(app: App) -> str:
    cfg, now = app.cfg, app.clock()
    ts = now.timestamp()
    app.state.heartbeat(ts)

    if cfg.scope_hosts:
        online = app.scope_check()
        if not online:
            app.state.clear_latch()
            line = f"no scope online ({cfg.scope_hosts}), not checking"
            log.info(line)
            return line
        if cfg.debug >= 1:
            log.info("DEBUG scope(s) online: %s", " ".join(online))

    _maybe_send_test(app)

    dets: list[Detection] = []
    statuses: dict[str, RadarStatus] = {}
    newest: Frame | None = None
    try:
        for product in ("reflectivity", "preciprate"):
            d, status = _detect_product(app, product, now)
            statuses[product] = status
            if d is not None:
                dets.append(d)
            frames = app.radar.frames(product)
            if frames and (newest is None or frames[-1].valid_time > newest.valid_time):
                newest = frames[-1]
        app.failures["radar"] = 0
    except MrmsError as exc:
        app.failures["radar"] += 1
        log.error("ERROR radar: %s (consecutive failures: %d)", exc, app.failures["radar"])
        dets, statuses = [], {}

    radar_ok = bool(statuses) and any(s.available for s in statuses.values())
    if not radar_ok:
        reasons = "; ".join(f"{p}: {s.reason}" for p, s in statuses.items()) or "fetch failed"
        log.warning("radar unavailable (%s); latch untouched", reasons)

    triggers: list[Trigger] = []
    raining_now = any(d.raining_now for d in dets if d.product == "preciprate")
    rt = radar_trigger(app, dets, now)
    if rt:
        triggers.append(rt)

    if app.pirate is not None:
        try:
            pr = app.pirate.check(now)
            app.failures["pirate"] = 0
        except Exception as exc:  # PirateError; imported lazily in Task 14
            app.failures["pirate"] += 1
            log.error("ERROR pirate weather: %s (consecutive failures: %d)", exc, app.failures["pirate"])
            pr = None
        if pr is not None:
            if pr.raining_now:
                raining_now = True
            if pr.eta_min is not None:
                triggers.append(Trigger("pirate weather", float(pr.eta_min), pr.detail))

    if radar_ok or triggers or raining_now:
        decision = decide(AlarmInputs(tuple(triggers), raining_now, app.state.latched(),
                                      app.state.latch_age_sec(ts), cfg.repeat_min))
    else:
        decision = decide(AlarmInputs((), False, False, None, cfg.repeat_min))   # NONE

    outcome = decision.action.name.lower()
    if decision.action in (Action.SEND, Action.REPEAT):
        if app.notifier.send(decision.title, decision.message):
            app.state.set_latch(ts)
            app.failures["ntfy"] = 0
            log.info("%s: %s", "ALERT sent" if decision.action is Action.SEND else "REPEAT alert sent",
                     decision.message)
        else:
            app.failures["ntfy"] += 1
            outcome = "send-failed"
    elif decision.action is Action.REARM:
        app.state.clear_latch()
        outcome = "re-armed"

    frame_txt = "none"
    age_txt = "n/a"
    if newest is not None:
        frame_txt = newest.valid_time.strftime("%H:%M:%SZ")
        age_txt = f"{(now - newest.valid_time).total_seconds() / 60:.1f}min"
    per_product = " ".join(
        f"{d.product}=max:{d.max_value:.1f},cells:{d.qualifying_cells},nearest:{d.describe()}" for d in dets
    ) or "radar=unavailable"
    eta_txt = min([t.eta_min for t in triggers if t.eta_min is not None], default=None)
    line = (f"frame={frame_txt} age={age_txt} {per_product} raining_now={int(raining_now)} "
            f"eta={'none' if eta_txt is None else f'{eta_txt:.0f}min'} "
            f"sources={','.join(t.source for t in triggers) or 'none'} "
            f"latched={int(app.state.latched())} outcome={outcome}")
    log.info(line)
    return line


def _setup_logging(debug: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_app(cfg: Config) -> App:
    client = httpx.Client(headers={"User-Agent": "astrorainprotect"})
    hosts = parse_hosts(cfg.scope_hosts)
    return App(
        cfg=cfg,
        radar=RadarSource(client, cfg.lat, cfg.lon),
        notifier=Notifier(client, cfg.ntfy_url, cfg.ntfy_token, cfg.ntfy_priority),
        state=State(cfg.state_dir),
        scope_check=lambda: online_hosts(hosts),
        pirate=None,
        clock=lambda: datetime.now(UTC),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg.debug)
    log.info("astrorainprotect starting\n%s", describe(cfg))
    app = build_app(cfg)
    app.state.clear_test_marker()

    stop = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop["flag"]:
        try:
            run_cycle(app)
        except Exception:  # never let one bad cycle kill the alarm
            log.exception("unexpected error in poll cycle")
        for _ in range(cfg.poll_sec):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("stopped")
    return 0
```

`astrorainprotect/__main__.py`:
```python
import sys

from astrorainprotect.app import main

sys.exit(main())
```

- [ ] **Step 4: Run tests, lint, fix, commit**

Run: `.venv/bin/pytest -q -m "not integration" && .venv/bin/ruff check .`
Expected: all pass. If `test_repeat` fails because the reflectivity frame is stale, check that the test replaced the frame with a newer valid time as written.

```bash
git add astrorainprotect/app.py astrorainprotect/__main__.py tests/test_app.py && git commit -m "feat: poll loop wiring radar detection, alarm decisions, scope gate and ntfy"
```

- [ ] **Step 5: Manual smoke run against real S3 (no ntfy send)**

```bash
cd /home/dmitr/astrorainprotect && LAT=29.97 LON=-95.67 NTFY_URL=http://127.0.0.1:9/none STATE_DIR=/tmp/arp-state POLL_SEC=30 timeout 45 .venv/bin/python -m astrorainprotect; echo "exit=$?"
```
Expected: startup config dump, then at least one summary line with `frame=` and both products, exit 124 from `timeout` (or 0 if SIGTERM was handled). No traceback.

- [ ] **Step 6: Update CHANGELOG and open PR**

Add under `## [Unreleased]` in `CHANGELOG.md`:
```markdown
### Added
- Radar-based rain detection from NOAA MRMS (reflectivity + PrecipRate) with the
  legacy alert semantics: one alert per event, optional repeat, scope-online gate,
  DEBUG levels, test notification.
```

```bash
git add CHANGELOG.md && git commit -m "docs: changelog for radar alarm loop"
git push -u origin phase-3-loop
gh pr create --base phase-2-mrms --title "Phase 3: alarm state machine, state, scope gate, notifier, poll loop" --body "$(cat <<'EOF'
Implements phase 3 of docs/superpowers/plans/2026-09-23-astrorainprotect.md.
The container is functional from here (radar-only trigger).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Do not merge.

---

## Phase 4 — Pirate Weather secondary trigger

### Task 13: pirate.py evaluation logic and source

**Files:**
- Create: `astrorainprotect/pirate.py`, `tests/fixtures/pirate_weather.json`
- Test: `tests/test_pirate.py`

**Interfaces:**
- Produces: `PirateError(Exception)`; `PirateResult(raining_now: bool, now_rate: float, max_prob_pct: int, max_intensity: float, eta_min: int | None, minutes: int, detail: str, lookahead_min: int)` frozen with `summary() -> str` in the legacy format `now=…mm/h next60: max_prob=…% max_int=…mm/h eta=…`; `evaluate(data: dict, *, lookahead_min, min_prob, min_intensity, raining_now) -> PirateResult`; `fetch_forecast(client, key, lat, lon) -> dict`; `class PirateSource(client, key, lat, lon, *, lookahead_min, min_prob, min_intensity, raining_now, min_interval_sec=300)` with `check(now: datetime) -> PirateResult | None` (cached result inside the interval; raises `PirateError` on fetch failure).
- Consumed by Task 14 (`app.pirate.check(now)` returns an object with `raining_now`, `eta_min`, `detail`).

- [ ] **Step 1: Create branch and fixture**

```bash
git checkout -b phase-4-pirate
```

`tests/fixtures/pirate_weather.json` — synthetic, matching the Pirate Weather (Dark Sky compatible) shape. Replace with a real capture when a key is at hand; the tests only depend on the fields below. 60 minutely entries: minutes 0–9 dry, 10–19 prob 0.4 intensity 0.3, 20–59 prob 0.8 intensity 1.5.
```bash
.venv/bin/python - <<'EOF'
import json
data = [{"time": 1758657600 + 60 * i,
         "precipProbability": 0.0 if i < 10 else (0.4 if i < 20 else 0.8),
         "precipIntensity": 0.0 if i < 10 else (0.3 if i < 20 else 1.5)} for i in range(60)]
doc = {"latitude": 29.97, "longitude": -95.67, "timezone": "America/Chicago",
       "currently": {"time": 1758657600, "precipIntensity": 0.0, "precipProbability": 0.0},
       "minutely": {"summary": "Rain starting in 10 min.", "data": data}}
json.dump(doc, open("tests/fixtures/pirate_weather.json", "w"), indent=1)
EOF
```

- [ ] **Step 2: Write failing tests**

`tests/test_pirate.py`:
```python
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from astrorainprotect.pirate import PirateError, PirateResult, PirateSource, evaluate, fetch_forecast

DATA = json.loads((Path(__file__).parent / "fixtures" / "pirate_weather.json").read_text())
KW = dict(lookahead_min=60, min_prob=0.3, min_intensity=0.2, raining_now=0.05)


def test_evaluate_eta_first_qualifying_minute():
    r = evaluate(DATA, **KW)
    assert isinstance(r, PirateResult)
    assert r.eta_min == 10
    assert r.raining_now is False
    assert r.now_rate == 0.0
    assert r.max_prob_pct == 80
    assert r.max_intensity == 1.5
    assert r.minutes == 60
    assert r.summary() == "now=0.0mm/h next60: max_prob=80% max_int=1.5mm/h eta=10"
    assert "80%" in r.detail


def test_evaluate_thresholds_are_strict_like_jq():
    r = evaluate(DATA, lookahead_min=60, min_prob=0.4, min_intensity=0.3, raining_now=0.05)
    assert r.eta_min == 20                   # minutes 10-19 have prob == 0.4, not > 0.4


def test_evaluate_lookahead_window():
    r = evaluate(DATA, lookahead_min=10, min_prob=0.3, min_intensity=0.2, raining_now=0.05)
    assert r.eta_min is None
    assert r.max_prob_pct == 0
    assert r.summary().endswith("eta=none")


def test_evaluate_raining_now_gives_no_eta():
    d = json.loads(json.dumps(DATA))
    d["currently"]["precipIntensity"] = 0.5
    r = evaluate(d, **KW)
    assert r.raining_now is True and r.eta_min is None


def test_evaluate_missing_minutely():
    r = evaluate({"currently": {}}, **KW)
    assert r.eta_min is None and r.minutes == 0 and r.max_prob_pct == 0


def test_fetch_forecast_url_and_params():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=DATA)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_forecast(client, "KEY123", 29.97, -95.67)["minutely"]["data"]
    assert seen["url"].startswith("https://api.pirateweather.net/forecast/KEY123/29.97,-95.67?")
    assert "units=si" in seen["url"] and "exclude=hourly%2Cdaily%2Calerts" in seen["url"]


def test_fetch_forecast_errors():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(PirateError, match="401"):
        fetch_forecast(client, "K", 1, 2)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json")))
    with pytest.raises(PirateError, match="JSON"):
        fetch_forecast(client, "K", 1, 2)


def test_source_respects_min_interval():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=DATA)

    src = PirateSource(httpx.Client(transport=httpx.MockTransport(handler)), "K", 29.97, -95.67, **KW)
    t0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    a = src.check(t0)
    b = src.check(t0 + timedelta(seconds=299))
    c = src.check(t0 + timedelta(seconds=300))
    assert a is b and a is not c
    assert len(calls) == 2


def test_source_error_does_not_poison_cache():
    n = {"i": 0}

    def handler(request):
        n["i"] += 1
        return httpx.Response(500) if n["i"] == 1 else httpx.Response(200, json=DATA)

    src = PirateSource(httpx.Client(transport=httpx.MockTransport(handler)), "K", 29.97, -95.67, **KW)
    t0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    with pytest.raises(PirateError):
        src.check(t0)
    assert src.check(t0 + timedelta(seconds=1)) is None        # still inside the interval
    assert src.check(t0 + timedelta(seconds=300)).eta_min == 10
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_pirate.py -q`
Expected: ImportError.

- [ ] **Step 4: Implement pirate.py**

```python
"""Pirate Weather secondary trigger, ported from the jq in legacy/rain-check.sh."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import httpx

API = "https://api.pirateweather.net/forecast"


class PirateError(Exception):
    pass


@dataclass(frozen=True)
class PirateResult:
    raining_now: bool
    now_rate: float
    max_prob_pct: int
    max_intensity: float
    eta_min: int | None
    minutes: int
    detail: str
    lookahead_min: int

    def summary(self) -> str:
        eta = "none" if self.eta_min is None else str(self.eta_min)
        return (f"now={self.now_rate}mm/h next{self.lookahead_min}: max_prob={self.max_prob_pct}% "
                f"max_int={self.max_intensity}mm/h eta={eta}")


def evaluate(data: dict, *, lookahead_min: int, min_prob: float, min_intensity: float,
             raining_now: float) -> PirateResult:
    now_rate = float((data.get("currently") or {}).get("precipIntensity") or 0.0)
    minutely = list((data.get("minutely") or {}).get("data") or [])
    window = minutely[:lookahead_min]
    probs = [float(m.get("precipProbability") or 0.0) for m in window]
    ints = [float(m.get("precipIntensity") or 0.0) for m in window]
    max_prob_pct = math.floor(max(probs, default=0.0) * 100)
    max_intensity = max(ints, default=0.0)
    is_raining = now_rate >= raining_now
    eta = None
    if not is_raining:
        for i, (p, it) in enumerate(zip(probs, ints, strict=True)):
            if p > min_prob and it > min_intensity:       # strict, like the jq
                eta = i
                break
    detail = f"prob {max_prob_pct}%, up to {max_intensity}mm/h within {lookahead_min} min"
    return PirateResult(is_raining, now_rate, max_prob_pct, max_intensity, eta, len(minutely),
                        detail, lookahead_min)


def fetch_forecast(client: httpx.Client, key: str, lat: float, lon: float) -> dict:
    url = f"{API}/{key}/{lat},{lon}"
    try:
        r = client.get(url, params={"units": "si", "exclude": "hourly,daily,alerts"}, timeout=20.0)
    except httpx.HTTPError as exc:
        raise PirateError(f"Pirate Weather fetch failed: {exc}") from exc
    if r.status_code != 200:
        raise PirateError(f"Pirate Weather returned HTTP {r.status_code} (bad key, quota, or outage?)")
    try:
        return r.json()
    except ValueError as exc:
        raise PirateError(f"Pirate Weather returned invalid JSON: {exc}") from exc


class PirateSource:
    def __init__(self, client: httpx.Client, key: str, lat: float, lon: float, *,
                 lookahead_min: int, min_prob: float, min_intensity: float, raining_now: float,
                 min_interval_sec: int = 300) -> None:
        self._client, self._key, self._lat, self._lon = client, key, lat, lon
        self._kw = dict(lookahead_min=lookahead_min, min_prob=min_prob,
                        min_intensity=min_intensity, raining_now=raining_now)
        self._interval = min_interval_sec
        self._last_call: datetime | None = None
        self._last: PirateResult | None = None

    def check(self, now: datetime) -> PirateResult | None:
        if self._last_call is not None and (now - self._last_call).total_seconds() < self._interval:
            return self._last
        self._last_call = now
        self._last = None
        self._last = evaluate(fetch_forecast(self._client, self._key, self._lat, self._lon), **self._kw)
        return self._last
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `.venv/bin/pytest tests/test_pirate.py -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/pirate.py tests/test_pirate.py tests/fixtures/pirate_weather.json && git commit -m "feat: Pirate Weather secondary trigger with 5-minute call floor"
```

---

### Task 14: Wire Pirate Weather into the loop

**Files:**
- Modify: `astrorainprotect/app.py` (`build_app`, the `except Exception` in `run_cycle`)
- Test: `tests/test_app.py` (append)

- [ ] **Step 1: Append failing tests**

```python
from astrorainprotect.pirate import PirateError, PirateResult  # noqa: E402


class FakePirate:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def check(self, now):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def pw(eta, raining_now=False):
    return PirateResult(raining_now, 0.0, 70, 1.2, eta, 60, "prob 70%", 60)


def test_pirate_trigger_alone_sends(tmp_path):
    app = build(tmp_path)
    app.pirate = FakePirate(pw(15))
    run_cycle(app)
    assert app.notifier.sent[0][1].startswith("Rain expected in about 15 min")
    assert "pirate weather" in app.notifier.sent[0][1]


def test_pirate_error_logged_radar_still_alerts(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    app.pirate = FakePirate(error=PirateError("HTTP 429"))
    run_cycle(app)
    assert any("HTTP 429" in r.getMessage() for r in caplog.records)
    assert len(app.notifier.sent) == 1


def test_pirate_raining_now_rearms(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    app.pirate = FakePirate(pw(None, raining_now=True))
    run_cycle(app)
    assert not app.state.latched()


def test_build_app_creates_pirate_only_with_key(tmp_path):
    from astrorainprotect.app import build_app
    from astrorainprotect.pirate import PirateSource
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path)})
    assert build_app(cfg).pirate is None
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path), "PW_KEY": "k"})
    assert isinstance(build_app(cfg).pirate, PirateSource)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_app.py -q`
Expected: `test_build_app_creates_pirate_only_with_key` fails (pirate always None); the others may already pass.

- [ ] **Step 3: Implement**

In `app.py`: add `from astrorainprotect.pirate import PirateError, PirateSource`; change `except Exception as exc:  # PirateError...` to `except PirateError as exc:`; in `build_app` replace `pirate=None` with:
```python
        pirate=(PirateSource(client, cfg.pw_key, cfg.lat, cfg.lon, lookahead_min=cfg.lookahead_min,
                             min_prob=cfg.min_prob, min_intensity=cfg.min_intensity,
                             raining_now=cfg.raining_now) if cfg.pw_key else None),
```
Also, when `cfg.debug >= 1` and `pr is not None`, log `DEBUG pirate: <pr.summary()>`.

- [ ] **Step 4: Run, lint, changelog, commit, PR**

Run: `.venv/bin/pytest -q -m "not integration" && .venv/bin/ruff check .`
Expected: all pass.

Add to `CHANGELOG.md` under Added: `- Pirate Weather kept as a secondary trigger (PW_KEY), polled at most every 5 minutes.`

```bash
git add -A && git commit -m "feat: wire Pirate Weather secondary trigger into the poll loop"
git push -u origin phase-4-pirate
gh pr create --base phase-3-loop --title "Phase 4: Pirate Weather secondary trigger" --body "$(cat <<'EOF'
Implements phase 4 of docs/superpowers/plans/2026-09-23-astrorainprotect.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Do not merge.

---

## Phase 5 — container, CI, Portainer, docs

### Task 15: Healthcheck module

**Files:**
- Create: `astrorainprotect/healthcheck.py`
- Test: `tests/test_healthcheck.py`

**Interfaces:**
- Produces: `main(env: Mapping[str, str], now: float) -> int` returning 0 when the heartbeat in `STATE_DIR` is younger than `3 * POLL_SEC`, else 1. Runnable as `python -m astrorainprotect.healthcheck`.

- [ ] **Step 1: Branch and failing tests**

```bash
git checkout -b phase-5-container
```

`tests/test_healthcheck.py`:
```python
from astrorainprotect.healthcheck import main
from astrorainprotect.state import State


def test_healthy(tmp_path):
    State(tmp_path).heartbeat(1000.0)
    assert main({"STATE_DIR": str(tmp_path), "POLL_SEC": "180"}, now=1000.0 + 539) == 0


def test_unhealthy_when_stale(tmp_path):
    State(tmp_path).heartbeat(1000.0)
    assert main({"STATE_DIR": str(tmp_path), "POLL_SEC": "180"}, now=1000.0 + 541) == 1


def test_unhealthy_when_missing(tmp_path):
    assert main({"STATE_DIR": str(tmp_path)}, now=5.0) == 1
```

- [ ] **Step 2: Implement**

```python
"""Docker HEALTHCHECK entry point: is the poll loop still touching its heartbeat?"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping

from astrorainprotect.state import State


def main(env: Mapping[str, str], now: float) -> int:
    poll = int(env.get("POLL_SEC", "180") or 180)
    age = State(env.get("STATE_DIR", "/state") or "/state").heartbeat_age_sec(now)
    return 0 if age is not None and age <= 3 * poll else 1


if __name__ == "__main__":
    sys.exit(main(os.environ, time.time()))
```

- [ ] **Step 3: Run, lint, commit**

Run: `.venv/bin/pytest tests/test_healthcheck.py -q && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/healthcheck.py tests/test_healthcheck.py && git commit -m "feat: heartbeat-based healthcheck entry point"
```

---

### Task 16: Install Docker, Dockerfile, compose

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`

- [ ] **Step 1: Install Docker Engine in WSL (sudo, user pre-approved this)**

```bash
sudo apt-get update -qq && sudo apt-get install -y -qq docker.io docker-compose-v2 && sudo usermod -aG docker "$USER" && (sudo systemctl start docker 2>/dev/null || sudo service docker start) && sudo docker version --format '{{.Server.Version}}'
```
Expected: a server version prints. Group membership applies to new shells only; the commands below use `sudo docker` so they work immediately.

- [ ] **Step 2: Write the Dockerfile**

```dockerfile
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY astrorainprotect ./astrorainprotect
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=America/Chicago STATE_DIR=/state
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app \
    && mkdir -p /state && chown app:app /state
COPY --from=build /install /usr/local
USER app
VOLUME ["/state"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "astrorainprotect.healthcheck"]
CMD ["python", "-m", "astrorainprotect"]
```

`.dockerignore`:
```
.git
.venv
tests
docs
legacy
scripts
*.md
!README.md
.env
__pycache__
```

`docker-compose.yml`:
```yaml
services:
  astrorainprotect:
    build: .
    container_name: astrorainprotect
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./state:/state
```

- [ ] **Step 3: Build, check size, verify eccodes imports, run the healthcheck**

```bash
cd /home/dmitr/astrorainprotect && sudo docker build -t astrorainprotect:dev . && sudo docker image ls astrorainprotect:dev --format '{{.Size}}' && sudo docker run --rm astrorainprotect:dev python -c "import eccodes, numpy, httpx; print('eccodes', eccodes.codes_get_api_version())" && sudo docker run --rm astrorainprotect:dev python -m astrorainprotect.healthcheck; echo "healthcheck exit=$? (1 expected: no heartbeat yet)"
```
Expected: build succeeds, size roughly 250–300 MB, eccodes version prints, healthcheck exits 1. Record the measured size in the CHANGELOG entry in Task 18.

- [ ] **Step 4: Compose smoke test with a throwaway .env**

```bash
cd /home/dmitr/astrorainprotect && printf 'LAT=29.97\nLON=-95.67\nNTFY_URL=http://127.0.0.1:9/none\nPOLL_SEC=30\nDEBUG=1\n' > .env && mkdir -p state && sudo chown 1000:1000 state && sudo docker compose up --build -d && sleep 40 && sudo docker compose logs --tail 20 && sudo docker inspect --format '{{.State.Health.Status}}' astrorainprotect; sudo docker compose down
```
Expected: logs show the config dump and a summary line with both products; health is `healthy` or `starting`. Then delete the throwaway `.env` and `state/`:
```bash
rm -f .env && sudo rm -rf state && echo 'state/' >> .gitignore
```

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml .gitignore && git commit -m "build: python:3.12-slim image with non-root user and heartbeat healthcheck"
```

---

### Task 17: CI workflow and Portainer stack

**Files:**
- Create: `.github/workflows/ci.yml`, `portainer-stack.yml`

- [ ] **Step 1: Write ci.yml**

```yaml
name: ci

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read
  packages: write

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e '.[dev]'
      - run: ruff check .
      - run: pytest -q -m "not integration"

  image:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        if: github.event_name == 'push'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64
          push: ${{ github.event_name == 'push' }}
          tags: |
            ghcr.io/ddovidenko/astrorainprotect:latest
            ghcr.io/ddovidenko/astrorainprotect:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 2: Write portainer-stack.yml**

```yaml
# Portainer: Stacks > Add stack > Repository, pointing at this repo, compose path portainer-stack.yml.
# Set the variables in the stack's Environment variables section. Only LAT, LON, NTFY_URL are required.
# The host directory /opt/astrorainprotect/state must be writable by uid 1000.
services:
  astrorainprotect:
    image: ghcr.io/ddovidenko/astrorainprotect:latest
    container_name: astrorainprotect
    restart: unless-stopped
    environment:
      - TZ=${TZ:-America/Chicago}
      - LAT=${LAT}
      - LON=${LON}
      - NTFY_URL=${NTFY_URL}
      - NTFY_TOKEN=${NTFY_TOKEN:-}
      - NTFY_PRIORITY=${NTFY_PRIORITY:-high}
      - DEBUG=${DEBUG:-0}
      - POLL_SEC=${POLL_SEC:-180}
      - ALERT_RADIUS_KM=${ALERT_RADIUS_KM:-20}
      - NOW_RADIUS_KM=${NOW_RADIUS_KM:-1}
      - MIN_INTENSITY=${MIN_INTENSITY:-0.2}
      - MIN_DBZ=${MIN_DBZ:-30}
      - MIN_CELLS=${MIN_CELLS:-3}
      - RAINING_NOW=${RAINING_NOW:-0.05}
      - DIRECTION_FILTER=${DIRECTION_FILTER:-0}
      - LOOKAHEAD_MIN=${LOOKAHEAD_MIN:-60}
      - REPEAT_MIN=${REPEAT_MIN:-0}
      - SCOPE_HOSTS=${SCOPE_HOSTS:-}
      - PW_KEY=${PW_KEY:-}
      - MIN_PROB=${MIN_PROB:-0.3}
    volumes:
      - /opt/astrorainprotect/state:/state
```

- [ ] **Step 3: Validate locally and commit**

```bash
cd /home/dmitr/astrorainprotect && .venv/bin/python -c "import yaml" 2>/dev/null || .venv/bin/pip install -q pyyaml; .venv/bin/python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ('.github/workflows/ci.yml','portainer-stack.yml','docker-compose.yml')]; print('yaml ok')"
git add .github/workflows/ci.yml portainer-stack.yml && git commit -m "ci: test, build and push image to GHCR; add Portainer stack"
```
Expected: `yaml ok`.

---

### Task 18: README, CHANGELOG, phase 5 PR

**Files:**
- Modify: `README.md`, `CHANGELOG.md`

- [ ] **Step 1: Write README.md**

Sections, each a short paragraph or list:
1. **What it does.** Two sentences from the spec's purpose.
2. **Quick start (Portainer).** Create `/opt/astrorainprotect/state` on the Docker host, `chown 1000:1000` it, add a Git-repository stack with `portainer-stack.yml`, set `LAT`, `LON`, `NTFY_URL`, optionally `NTFY_TOKEN`, deploy. Note that the GHCR package must be public or Portainer needs registry credentials.
3. **Local development.** `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`, `pytest`, `pytest -m integration` for the live S3 test, `cp .env.example .env && docker compose up --build`.
4. **Configuration.** The env var table from the spec section 7, copied verbatim.
5. **How alerts behave.** Latch, repeat, scope gate, DEBUG levels, test notification, in the user's own words from the brief.
6. **Reading the logs.** One example summary line and what each field means.
7. **Tuning.** Start with defaults; lower `MIN_DBZ` to 25 for earlier but noisier alerts; raise `MIN_CELLS` if single-pixel noise triggers; set `REPEAT_MIN=10` during sessions; `DIRECTION_FILTER=1` once phase 6 is in.
8. **Gotchas.** Copy the "Known gotchas" list from `CLAUDE.md`.

- [ ] **Step 2: CHANGELOG**

Under `## [Unreleased]`, add:
```markdown
- Docker image (python:3.12-slim, non-root, heartbeat HEALTHCHECK), measured size: <fill from Task 16>.
- GitHub Actions CI: ruff + pytest on PRs, image push to ghcr.io/ddovidenko/astrorainprotect on main.
- Portainer Git stack file.
```
Replace `<fill from Task 16>` with the number printed in Task 16 step 3 before committing.

- [ ] **Step 3: Commit and open the PR**

```bash
git add README.md CHANGELOG.md && git commit -m "docs: README setup and tuning guide, changelog"
git push -u origin phase-5-container
gh pr create --base phase-4-pirate --title "Phase 5: Docker image, CI to GHCR, Portainer stack, README" --body "$(cat <<'EOF'
Implements phase 5 of docs/superpowers/plans/2026-09-23-astrorainprotect.md.
After this merges to main, CI publishes ghcr.io/ddovidenko/astrorainprotect:latest and the Portainer stack can replace the legacy alpine stack.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Do not merge. Tell the user: once phases 1–5 are on `main`, CI will publish the image; the GHCR package visibility must be set to public in GitHub package settings (or Portainer needs a registry credential) before the stack can pull.

---

## Phase 6 — motion estimation, direction filter, record and replay

### Task 19: motion.estimate

**Files:**
- Create: `astrorainprotect/motion.py`
- Test: `tests/test_motion.py`

**Interfaces:**
- Produces: `Motion(u_km_per_min: float, v_km_per_min: float, confidence: float, frames_used: int)` frozen (u east, v north); `MIN_CONFIDENCE = 0.3`; `estimate(frames: Sequence[Frame], *, min_confidence: float = MIN_CONFIDENCE) -> Motion | None`; `phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int, float]` returning `(dy, dx, peak)` where `b == roll(a, (dy, dx))`.

- [ ] **Step 1: Branch and failing tests**

```bash
git checkout -b phase-6-motion
```

`tests/test_motion.py`:
```python
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from astrorainprotect.detect import KM_PER_DEG
from astrorainprotect.motion import Motion, estimate, phase_shift
from tests.conftest import make_frame

T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)


def blob(cy, cx, n=101, value=40.0):
    g = np.zeros((n, n), dtype=np.float32)
    yy, xx = np.mgrid[:n, :n]
    g[(yy - cy) ** 2 + (xx - cx) ** 2 < 36] = value
    return g


def test_phase_shift_recovers_roll():
    a = blob(30, 40)
    b = np.roll(a, (7, -12), axis=(0, 1))
    dy, dx, peak = phase_shift(a, b)
    assert (dy, dx) == (7, -12)
    assert peak > 0.9


def test_phase_shift_empty_has_zero_peak():
    z = np.zeros((20, 20))
    assert phase_shift(z, z)[2] == 0.0


def test_estimate_needs_three_frames():
    f = make_frame(blob(30, 40), product="reflectivity", valid_time=T0)
    g = make_frame(blob(30, 40), product="reflectivity", valid_time=T0 + timedelta(minutes=2))
    assert estimate([f, g]) is None


def test_estimate_vector_units_and_sign():
    # blob moves 6 rows south and 8 columns east over 4 minutes (2 frames apart)
    frames = [make_frame(np.roll(blob(30, 40), (3 * k, 4 * k), axis=(0, 1)), product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    m = estimate(frames)
    assert isinstance(m, Motion)
    assert m.frames_used == 3
    km_per_cell_ns = 0.01 * KM_PER_DEG
    km_per_cell_ew = 0.01 * KM_PER_DEG * np.cos(np.radians(29.97))
    assert m.v_km_per_min == pytest.approx(-6 * km_per_cell_ns / 4, rel=0.01)   # southward => negative v
    assert m.u_km_per_min == pytest.approx(8 * km_per_cell_ew / 4, rel=0.01)    # eastward => positive u
    assert m.confidence > 0.9


def test_estimate_rejects_low_confidence():
    rng = np.random.default_rng(1)
    frames = [make_frame(rng.random((101, 101)) * 50, product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    assert estimate(frames) is None


def test_estimate_rejects_empty_frames():
    frames = [make_frame(np.zeros((101, 101)), product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    assert estimate(frames) is None


def test_estimate_rejects_identical_times():
    f = make_frame(blob(30, 40), product="reflectivity", valid_time=T0)
    assert estimate([f, f, f]) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_motion.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement estimate**

```python
"""Storm motion from consecutive frames (FFT phase correlation) and approach projection. Pure."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from astrorainprotect.detect import KM_PER_DEG, Detection
from astrorainprotect.frame import Frame

MIN_CONFIDENCE = 0.3


@dataclass(frozen=True)
class Motion:
    u_km_per_min: float   # eastward
    v_km_per_min: float   # northward
    confidence: float     # phase-correlation peak, 0..1
    frames_used: int


@dataclass(frozen=True)
class Approach:
    will_hit: bool
    eta_min: float | None
    closest_km: float


def phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int, float]:
    """(dy, dx, peak) such that b ≈ np.roll(a, (dy, dx), axis=(0, 1))."""
    fa, fb = np.fft.fft2(a), np.fft.fft2(b)
    cross = fb * np.conj(fa)
    mag = np.abs(cross)
    if not mag.any():
        return 0, 0, 0.0
    mag[mag == 0] = 1.0
    r = np.fft.ifft2(cross / mag).real
    dy, dx = np.unravel_index(int(np.argmax(r)), r.shape)
    if dy > a.shape[0] // 2:
        dy -= a.shape[0]
    if dx > a.shape[1] // 2:
        dx -= a.shape[1]
    return int(dy), int(dx), float(r.max())


def estimate(frames: Sequence[Frame], *, min_confidence: float = MIN_CONFIDENCE) -> Motion | None:
    if len(frames) < 3:
        return None
    first, last = frames[0], frames[-1]
    dt_min = (last.valid_time - first.valid_time).total_seconds() / 60.0
    if dt_min <= 0 or first.values.shape != last.values.shape:
        return None
    if not first.values.any() or not last.values.any():
        return None
    dy, dx, peak = phase_shift(first.values, last.values)
    if peak < min_confidence:
        return None
    lat = float(np.mean(last.lats))
    dlat = abs(float(last.lats[0] - last.lats[1])) if len(last.lats) > 1 else 0.01
    dlon = abs(float(last.lons[1] - last.lons[0])) if len(last.lons) > 1 else 0.01
    km_ns = dlat * KM_PER_DEG
    km_ew = dlon * KM_PER_DEG * np.cos(np.radians(lat))
    return Motion(
        u_km_per_min=dx * km_ew / dt_min,
        v_km_per_min=-dy * km_ns / dt_min,   # rows increase southward
        confidence=peak,
        frames_used=len(frames),
    )
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `.venv/bin/pytest tests/test_motion.py -q && .venv/bin/ruff check .`
Expected: all pass (the `Detection` import is used in Task 20; ruff may flag it unused until then, so add `project` in the same commit or keep the import out until Task 20).

```bash
git add astrorainprotect/motion.py tests/test_motion.py && git commit -m "feat: storm motion estimate via phase correlation"
```

---

### Task 20: motion.project and the direction filter in the loop

**Files:**
- Modify: `astrorainprotect/motion.py`, `astrorainprotect/app.py` (`radar_trigger`)
- Test: `tests/test_motion.py` (append), `tests/test_app.py` (append)

**Interfaces:**
- Produces: `project(detection: Detection, motion: Motion, *, hit_radius_km: float, lookahead_min: float) -> Approach`. Semantics (a deliberate refinement of the spec's wording, recorded in the spec's decision table): the nearest qualifying echo is already inside `ALERT_RADIUS_KM`, so "passes within the alert radius" would always be true. Instead: `will_hit` is true when the echo's projected path comes within `hit_radius_km` of the house within `lookahead_min`; `eta_min` is the time it first enters that radius (0 if already inside); `closest_km` is the minimum projected distance. The loop passes `hit_radius_km = max(cfg.now_radius_km, cfg.alert_radius_km / 4)` (5 km at defaults).

- [ ] **Step 1: Append failing tests to test_motion.py**

```python
from astrorainprotect.detect import Detection  # noqa: E402
from astrorainprotect.motion import Approach, project  # noqa: E402


def det(km, bearing):
    return Detection("reflectivity", False, 0.0, 9, True, km, bearing, 40.0)


def test_project_toward_house():
    # echo 12 km to the SW (bearing 225), moving NE at 1 km/min
    m = Motion(u_km_per_min=np.sin(np.radians(45)), v_km_per_min=np.cos(np.radians(45)), confidence=1, frames_used=3)
    a = project(det(12.0, 225.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert isinstance(a, Approach)
    assert a.will_hit
    assert a.eta_min == pytest.approx(7.0, abs=0.1)
    assert a.closest_km == pytest.approx(0.0, abs=1e-6)


def test_project_moving_away():
    m = Motion(u_km_per_min=-0.7, v_km_per_min=-0.7, confidence=1, frames_used=3)   # SW-bound
    a = project(det(12.0, 225.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.eta_min is None
    assert a.closest_km == pytest.approx(12.0)


def test_project_passes_wide():
    # echo 10 km due south, moving due east: closest approach is 10 km, outside 5 km
    m = Motion(u_km_per_min=1.0, v_km_per_min=0.0, confidence=1, frames_used=3)
    a = project(det(10.0, 180.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.closest_km == pytest.approx(10.0)


def test_project_too_slow_for_lookahead():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.1, confidence=1, frames_used=3)   # 6 km/h north
    a = project(det(15.0, 180.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit


def test_project_already_inside():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.0, confidence=1, frames_used=3)
    a = project(det(2.0, 90.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert a.will_hit and a.eta_min == 0.0


def test_project_stationary_outside():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.0, confidence=1, frames_used=3)
    a = project(det(8.0, 90.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.closest_km == pytest.approx(8.0)
```

- [ ] **Step 2: Implement project**

Append to `motion.py`:
```python
def project(detection: Detection, motion: Motion, *, hit_radius_km: float,
            lookahead_min: float) -> Approach:
    if detection.nearest_km is None or detection.nearest_bearing_deg is None:
        return Approach(False, None, float("inf"))
    b = np.radians(detection.nearest_bearing_deg)
    px, py = detection.nearest_km * np.sin(b), detection.nearest_km * np.cos(b)   # east, north
    vx, vy = motion.u_km_per_min, motion.v_km_per_min
    r0 = float(np.hypot(px, py))
    if r0 <= hit_radius_km:
        return Approach(True, 0.0, r0)
    v2 = vx * vx + vy * vy
    if v2 < 1e-9:
        return Approach(False, None, r0)
    t_closest = float(np.clip(-(px * vx + py * vy) / v2, 0.0, lookahead_min))
    closest = float(np.hypot(px + vx * t_closest, py + vy * t_closest))
    if closest > hit_radius_km:
        return Approach(False, None, closest)
    # smallest t >= 0 with |p + v t| == hit_radius: solve v2 t^2 + 2(p.v) t + (r0^2 - R^2) = 0
    bq = 2 * (px * vx + py * vy)
    cq = r0 * r0 - hit_radius_km * hit_radius_km
    disc = bq * bq - 4 * v2 * cq
    t_enter = (-bq - np.sqrt(max(disc, 0.0))) / (2 * v2)
    if t_enter < 0 or t_enter > lookahead_min:
        return Approach(False, None, closest)
    return Approach(True, float(t_enter), closest)
```

- [ ] **Step 3: Run motion tests**

Run: `.venv/bin/pytest tests/test_motion.py -q`
Expected: all pass.

- [ ] **Step 4: Append failing loop tests to test_app.py**

```python
def moving_storm(k, toward=True):
    """Reflectivity blob ~11 km SW that steps 3 cells per frame toward (or away from) the house."""
    g = np.zeros((101, 101), dtype=np.float32)
    off = -k if toward else k                      # one cell per 2-minute frame
    g[62 + off:66 + off, 38 - off:42 - off] = 40.0  # starts ~14 km SW; stays inside 20 km either way
    return make_frame(g, product="reflectivity", valid_time=NOW - timedelta(minutes=2 * (2 - k)))


class HistoryRadar(FakeRadar):
    def __init__(self, frames):
        super().__init__(empty("preciprate"), frames[-1])
        self._hist = frames

    def frames(self, product):
        return self._hist if product == "reflectivity" else [self.by_product["preciprate"]]


def test_direction_filter_suppresses_receding_storm(tmp_path):
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert app.notifier.sent == []
    assert "moving away" in line


def test_direction_filter_alerts_with_eta_for_approaching_storm(tmp_path):
    radar = HistoryRadar([moving_storm(k, toward=True) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    assert app.notifier.sent[0][1].startswith("Rain expected in about")


def test_direction_filter_falls_back_without_history(tmp_path):
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))   # one frame only
    run_cycle(app)
    assert len(app.notifier.sent) == 1                 # never suppress without motion data


def test_direction_filter_off_ignores_motion(tmp_path):
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, radar=radar)
    run_cycle(app)
    assert len(app.notifier.sent) == 1
```

- [ ] **Step 5: Extend radar_trigger in app.py**

Replace `radar_trigger` with:
```python
def radar_trigger(app: App, dets: list[Detection], now: datetime) -> Trigger | None:
    hits = [d for d in dets if d.nearby]
    if not hits:
        return None
    cfg = app.cfg
    units = {"reflectivity": "dBZ", "preciprate": "mm/h"}
    detail = ", ".join(f"{d.product} {d.max_value:.0f} {units[d.product]} {d.describe()}" for d in hits)
    eta: float | None = None
    if cfg.direction_filter:
        refl = next((d for d in hits if d.product == "reflectivity"), None)
        m = estimate(app.radar.frames("reflectivity")) if refl is not None else None
        if refl is not None and m is not None:
            a = project(refl, m, hit_radius_km=max(cfg.now_radius_km, cfg.alert_radius_km / 4),
                        lookahead_min=cfg.lookahead_min)
            if not a.will_hit:
                log.info("radar echo moving away (closest approach %.1f km, motion %.1f/%.1f km/min, "
                         "conf %.2f); not alerting", a.closest_km, m.u_km_per_min, m.v_km_per_min,
                         m.confidence)
                app.last_note = "moving away"
                return None
            eta = a.eta_min
            detail += f", eta {a.eta_min:.0f} min"
        elif cfg.debug >= 1:
            log.info("DEBUG direction filter: motion unknown, plain radius alerting")
    return Trigger(source="radar", eta_min=eta, detail=detail)
```
Add `from astrorainprotect.motion import estimate, project` to the imports, add `last_note: str = ""` to `App`, reset it to `""` at the top of `run_cycle`, and append `note={app.last_note or '-'}` to the summary line so the "moving away" test can see it.

- [ ] **Step 6: Run everything, lint, commit**

Run: `.venv/bin/pytest -q -m "not integration" && .venv/bin/ruff check .`
Expected: all pass.

```bash
git add astrorainprotect/motion.py astrorainprotect/app.py tests/ && git commit -m "feat: approach projection and DIRECTION_FILTER in the poll loop"
```

---

### Task 21: record_frames.py

**Files:**
- Create: `scripts/record_frames.py`
- Test: `tests/test_record.py`

**Interfaces:**
- Produces: `record(radar, products, out_dir: Path, *, minutes: int, interval_sec: int, clock, sleep) -> int` (number of frames written; skips duplicates by valid time) plus an argparse `main()` using env `LAT`/`LON`. Files are named by `Frame.filename()`.

- [ ] **Step 1: Failing test**

`tests/test_record.py`:
```python
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from record_frames import record  # noqa: E402

from astrorainprotect.frame import Frame  # noqa: E402
from tests.conftest import make_frame  # noqa: E402

T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)


class SeqRadar:
    def __init__(self):
        self.i = 0

    def fetch_latest(self, product, now):
        self.i += 1
        step = (self.i - 1) // 2 // 2            # same frame twice in a row, per product
        return make_frame(np.zeros((3, 3)), product=product, valid_time=T0 + timedelta(minutes=2 * step))


def test_record_writes_unique_frames(tmp_path):
    clock = {"t": T0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock["t"] += timedelta(seconds=s)

    n = record(SeqRadar(), ["reflectivity", "preciprate"], tmp_path, minutes=8, interval_sec=120,
               clock=lambda: clock["t"], sleep=sleep)
    files = sorted(p.name for p in tmp_path.glob("*.npz"))
    assert n == len(files) == 4                  # 2 products x 2 distinct times over 4 polls
    assert files[0] == "preciprate_20260923-200000.npz"
    assert Frame.load(tmp_path / files[0]).product == "preciprate"
    assert all(s == 120 for s in sleeps)
```

- [ ] **Step 2: Implement scripts/record_frames.py**

```python
#!/usr/bin/env python3
"""Record MRMS subset frames around LAT/LON for fixtures and replay.

Usage: LAT=.. LON=.. python scripts/record_frames.py OUT_DIR --minutes 60 --interval 120
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from astrorainprotect.mrms import PRODUCTS, MrmsError, RadarSource


def record(radar, products: list[str], out_dir: Path, *, minutes: int, interval_sec: int,
           clock: Callable[[], datetime], sleep: Callable[[float], None]) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written = 0
    end = clock().timestamp() + minutes * 60
    while clock().timestamp() < end:
        for product in products:
            try:
                frame = radar.fetch_latest(product, clock())
            except MrmsError as exc:
                print(f"ERROR {exc}", file=sys.stderr)
                continue
            if frame is None or frame.filename() in seen:
                continue
            frame.save(out_dir / frame.filename())
            seen.add(frame.filename())
            written += 1
            print(f"saved {frame.filename()} max={frame.values.max():.1f} missing={frame.missing_fraction:.0%}")
        sleep(interval_sec)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--products", default=",".join(PRODUCTS))
    args = ap.parse_args(argv)
    lat, lon = float(os.environ["LAT"]), float(os.environ["LON"])
    radar = RadarSource(httpx.Client(), lat, lon)
    n = record(radar, args.products.split(","), args.out_dir, minutes=args.minutes,
               interval_sec=args.interval, clock=lambda: datetime.now(UTC), sleep=time.sleep)
    print(f"wrote {n} frames to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run, lint, commit**

Run: `.venv/bin/pytest tests/test_record.py -q && .venv/bin/ruff check .`
Expected: pass.

```bash
git add scripts/record_frames.py tests/test_record.py && git commit -m "feat: record_frames script for fixtures and replay"
```

---

### Task 22: REPLAY_DIR mode

**Files:**
- Create: `astrorainprotect/replay.py`
- Modify: `astrorainprotect/app.py` (`main`)
- Test: `tests/test_replay.py`

**Interfaces:**
- Produces: `class ReplaySource(frames: list[Frame], cache=5)` with the `RadarSource` interface (`fetch_latest(product, now)` returns the newest frame of that product with `valid_time <= now`, keeping history; `frames(product)`); `class DryRunNotifier` whose `send` logs `WOULD SEND <title>: <message>` and returns True; `load_frames(dir) -> list[Frame]` sorted by time; `run_replay(cfg: Config, replay_dir: Path) -> int` that steps the clock through every distinct valid time, calls `run_cycle`, uses a temporary state dir, and returns the number of cycles.

- [ ] **Step 1: Failing tests**

`tests/test_replay.py`:
```python
import logging
from datetime import UTC, datetime, timedelta

import numpy as np

from astrorainprotect.config import load_config
from astrorainprotect.replay import DryRunNotifier, ReplaySource, load_frames, run_replay
from tests.conftest import make_frame

T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
BASE = {"LAT": "29.97", "LON": "-95.67", "NTFY_URL": "https://ntfy.example.net/rain"}


def save_sequence(d):
    for k in range(4):
        g = np.zeros((101, 101), dtype=np.float32)
        if k >= 2:
            g[60:64, 40:44] = 45.0                # storm appears at frame 2
        make_frame(g, product="reflectivity", valid_time=T0 + timedelta(minutes=2 * k)).save(
            d / f"reflectivity_{k}.npz")
        make_frame(np.zeros((101, 101)), product="preciprate",
                   valid_time=T0 + timedelta(minutes=2 * k)).save(d / f"preciprate_{k}.npz")


def test_load_frames_sorted(tmp_path):
    save_sequence(tmp_path)
    frames = load_frames(tmp_path)
    assert len(frames) == 8
    assert [f.valid_time for f in frames] == sorted(f.valid_time for f in frames)


def test_replay_source_time_travel(tmp_path):
    save_sequence(tmp_path)
    src = ReplaySource(load_frames(tmp_path))
    assert src.fetch_latest("reflectivity", T0 - timedelta(minutes=1)) is None
    f = src.fetch_latest("reflectivity", T0 + timedelta(minutes=3))
    assert f.valid_time == T0 + timedelta(minutes=2)
    src.fetch_latest("reflectivity", T0 + timedelta(minutes=7))
    assert [x.valid_time for x in src.frames("reflectivity")] == [T0 + timedelta(minutes=2), T0 + timedelta(minutes=6)]


def test_run_replay_reports_would_send(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    save_sequence(tmp_path)
    cfg = load_config({**BASE, "REPLAY_DIR": str(tmp_path), "STATE_DIR": str(tmp_path / "unused")})
    n = run_replay(cfg, tmp_path)
    assert n == 4
    would = [r.getMessage() for r in caplog.records if r.getMessage().startswith("WOULD SEND")]
    assert len(would) == 1 and "Rain incoming" in would[0]
    assert not (tmp_path / "unused").exists()     # replay never touches the real state dir


def test_dry_run_notifier(caplog):
    caplog.set_level(logging.INFO)
    assert DryRunNotifier().send("T", "M") is True
    assert "WOULD SEND T: M" in caplog.records[-1].getMessage()
```

- [ ] **Step 2: Implement replay.py**

```python
"""Run the detector over recorded frames without network or ntfy."""

from __future__ import annotations

import logging
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path

from astrorainprotect.config import Config
from astrorainprotect.frame import Frame
from astrorainprotect.mrms import PRODUCTS
from astrorainprotect.state import State

log = logging.getLogger("astrorainprotect")


class DryRunNotifier:
    def send(self, title: str, message: str) -> bool:
        log.info("WOULD SEND %s: %s", title, message)
        return True


def load_frames(directory: Path | str) -> list[Frame]:
    frames = [Frame.load(p) for p in sorted(Path(directory).glob("*.npz"))]
    return sorted(frames, key=lambda f: (f.valid_time, f.product))


class ReplaySource:
    def __init__(self, frames: list[Frame], cache: int = 5) -> None:
        self._all = {p: [f for f in frames if f.product == p] for p in PRODUCTS}
        self._hist: dict[str, deque[Frame]] = {p: deque(maxlen=cache) for p in PRODUCTS}

    def frames(self, product: str) -> list[Frame]:
        return list(self._hist[product])

    def fetch_latest(self, product: str, now: datetime) -> Frame | None:
        candidates = [f for f in self._all[product] if f.valid_time <= now]
        if not candidates:
            return None
        newest = candidates[-1]
        hist = self._hist[product]
        if not hist or hist[-1].valid_time != newest.valid_time:
            hist.append(newest)
        return newest


def run_replay(cfg: Config, replay_dir: Path | str) -> int:
    from astrorainprotect.app import App, run_cycle   # local import avoids a cycle

    frames = load_frames(replay_dir)
    times = sorted({f.valid_time for f in frames})
    log.info("replay: %d frames, %d distinct times from %s", len(frames), len(times), replay_dir)
    clock = {"t": times[0] if times else None}
    with tempfile.TemporaryDirectory() as tmp:
        app = App(cfg=cfg, radar=ReplaySource(frames), notifier=DryRunNotifier(),
                  state=State(Path(tmp) / "state", Path(tmp) / "tmp"), scope_check=lambda: [],
                  pirate=None, clock=lambda: clock["t"])
        for t in times:
            clock["t"] = t
            run_cycle(app)
    return len(times)
```

In `app.main`, after `_setup_logging(cfg.debug)` and the startup log, add:
```python
    if cfg.replay_dir:
        from astrorainprotect.replay import run_replay
        run_replay(cfg, cfg.replay_dir)
        return 0
```

- [ ] **Step 3: Run everything, lint, changelog, commit, PR**

Run: `.venv/bin/pytest -q -m "not integration" && .venv/bin/ruff check .`
Expected: all pass.

Add to `CHANGELOG.md` under Added:
```markdown
- DIRECTION_FILTER=1: storm motion from consecutive reflectivity frames; echoes moving away do not alert, ETA reported when known.
- scripts/record_frames.py and REPLAY_DIR for tuning against recorded frames.
```
Update `README.md` Tuning section to describe `DIRECTION_FILTER` and replay usage:
```
LAT=.. LON=.. .venv/bin/python scripts/record_frames.py frames/ --minutes 60
LAT=.. LON=.. NTFY_URL=x REPLAY_DIR=frames/ DIRECTION_FILTER=1 .venv/bin/python -m astrorainprotect
```

```bash
git add -A && git commit -m "feat: REPLAY_DIR mode over recorded frames"
git push -u origin phase-6-motion
gh pr create --base phase-5-container --title "Phase 6: motion estimation, direction filter, record and replay" --body "$(cat <<'EOF'
Implements phase 6 of docs/superpowers/plans/2026-09-23-astrorainprotect.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Do not merge.

---

## Spec amendments made by this plan

Recorded here and applied to the spec in the same commit as this plan:

1. Missing-value rule is per product (`< 0` PrecipRate, `< -990` reflectivity) rather than "negative means missing". Reflectivity legitimately goes negative.
2. `project` uses a hit radius of `max(NOW_RADIUS_KM, ALERT_RADIUS_KM / 4)` for `will_hit`, not `ALERT_RADIUS_KM`, because the nearest echo is by definition already inside the alert radius.
3. `STATE_DIR` env var added (default `/state`) so tests, local runs, and replay never touch `/state`.
4. Loop lives in `app.py`; `__main__.py` only calls `app.main()`. `healthcheck.py` and `replay.py` added.
5. Decoder: `eccodes` PyPI directly; no cfgrib/xarray.
