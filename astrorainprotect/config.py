"""Environment-variable configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

PRIORITIES = ("min", "low", "default", "high", "urgent")


# MRMS CONUS grid: lat 20-55 N, lon 130-60 W (0.01 deg cells). Outside it there is no radar.
MRMS_LAT = (20.0, 55.0)
MRMS_LON = (-130.0, -60.0)


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
    direction_filter: bool = True
    lookahead_min: int = 60
    repeat_min: int = 0
    scope_hosts: str = ""
    pw_key: str = ""
    min_prob: float = 0.3
    replay_dir: str = ""
    snapshot: bool = True
    auto_record: bool = True
    tz: str = "America/Chicago"   # for the HH:MM stamp in messages (#64) and log times
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
    lat = _float(env, "LAT", None, -90, 90)
    lon = _float(env, "LON", None, -180, 180)
    if not (MRMS_LAT[0] <= lat <= MRMS_LAT[1] and MRMS_LON[0] <= lon <= MRMS_LON[1]):
        raise ConfigError(
            f"LAT/LON {lat},{lon} is outside MRMS CONUS radar coverage "
            f"(lat {MRMS_LAT[0]:.0f} to {MRMS_LAT[1]:.0f} N, "
            f"lon {MRMS_LON[0]:.0f} to {MRMS_LON[1]:.0f})"
        )
    return Config(
        lat=lat,
        lon=lon,
        ntfy_url=_str(env, "NTFY_URL", None),
        ntfy_token=_str(env, "NTFY_TOKEN"),
        ntfy_priority=priority,
        debug=_int(env, "ASTRORAINPROTECT_DEBUG", 0, 0, 2),
        poll_sec=_int(env, "POLL_SEC", 180, 30, 3600),
        alert_radius_km=_float(env, "ALERT_RADIUS_KM", 20.0, 0.5, 50.0),
        now_radius_km=_float(env, "NOW_RADIUS_KM", 1.0, 0.1, 10.0),
        min_intensity=_float(env, "MIN_INTENSITY", 0.2, 0.0, 100.0),
        min_dbz=_float(env, "MIN_DBZ", 30.0, 0.0, 80.0),
        min_cells=_int(env, "MIN_CELLS", 3, 1, 1000),
        raining_now=_float(env, "RAINING_NOW", 0.05, 0.0, 100.0),
        direction_filter=_int(env, "DIRECTION_FILTER", 1, 0, 1) == 1,
        lookahead_min=_int(env, "LOOKAHEAD_MIN", 60, 1, 60),
        repeat_min=_int(env, "REPEAT_MIN", 0, 0, 1440),
        scope_hosts=_str(env, "SCOPE_HOSTS"),
        pw_key=_str(env, "PW_KEY"),
        min_prob=_float(env, "MIN_PROB", 0.3, 0.0, 1.0),
        replay_dir=_str(env, "REPLAY_DIR"),
        snapshot=_int(env, "SNAPSHOT", 1, 0, 1) == 1,
        auto_record=_int(env, "AUTO_RECORD", 1, 0, 1) == 1,
        tz=_str(env, "TZ", "America/Chicago") or "America/Chicago",
        state_dir=_str(env, "STATE_DIR", "/state"),
    )


_SECRETS = {"ntfy_token", "pw_key"}
# Fields whose env var is not simply the upper-cased field name.
_ENV_NAMES = {"debug": "ASTRORAINPROTECT_DEBUG"}


def ignored_legacy_debug(env: Mapping[str, str]) -> str | None:
    """A warning when the pre-#25 DEBUG variable is set but ASTRORAINPROTECT_DEBUG is not.

    DEBUG is left to eckit (loaded by eccodes), which treats it as its own switch; the app
    reads ASTRORAINPROTECT_DEBUG only.
    """
    if env.get("DEBUG", "").strip() and not env.get("ASTRORAINPROTECT_DEBUG", "").strip():
        return (f"DEBUG is ignored since it collides with eckit's own switch; set "
                f"ASTRORAINPROTECT_DEBUG={env['DEBUG'].strip()} instead")
    return None


def describe(cfg: Config) -> str:
    """One line per setting, secrets masked, for the startup log."""
    parts = []
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        if f.name in _SECRETS:
            value = "***" if value else "(unset)"
        elif isinstance(value, bool):
            value = int(value)
        parts.append(f"{_ENV_NAMES.get(f.name, f.name.upper())}={value}")
    return "\n".join(parts)
