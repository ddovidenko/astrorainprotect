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


@pytest.mark.parametrize("lat,lon", [
    ("19.5", "-95.67"), ("56", "-95.67"), ("29.97", "-131"), ("29.97", "-59"), ("51.5", "-0.1"),
])
def test_coordinates_outside_mrms_coverage_rejected(lat, lon):
    """MRMS CONUS covers ~20-55 N, 130-60 W (issue #15)."""
    with pytest.raises(ConfigError, match="MRMS"):
        load_config({**BASE, "LAT": lat, "LON": lon})


def test_coordinates_inside_coverage_accepted():
    cfg = load_config({**BASE, "LAT": "47.6", "LON": "-122.3"})   # Seattle
    assert (cfg.lat, cfg.lon) == (47.6, -122.3)
