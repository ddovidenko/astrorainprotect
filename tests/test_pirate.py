import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from astrorainprotect.pirate import (
    PirateError,
    PirateResult,
    PirateSource,
    evaluate,
    fetch_forecast,
)

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
    def handler401(r):
        return httpx.Response(401, text="bad key")

    client = httpx.Client(transport=httpx.MockTransport(handler401))
    with pytest.raises(PirateError, match="401"):
        fetch_forecast(client, "K", 1, 2)

    def handler_json_error(r):
        return httpx.Response(200, text="not json")

    client = httpx.Client(transport=httpx.MockTransport(handler_json_error))
    with pytest.raises(PirateError, match="JSON"):
        fetch_forecast(client, "K", 1, 2)


def test_source_respects_min_interval():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=DATA)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    src = PirateSource(client, "K", 29.97, -95.67, **KW)
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

    client = httpx.Client(transport=httpx.MockTransport(handler))
    src = PirateSource(client, "K", 29.97, -95.67, **KW)
    t0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    with pytest.raises(PirateError):
        src.check(t0)
    assert src.check(t0 + timedelta(seconds=1)) is None  # still inside the interval
    assert src.check(t0 + timedelta(seconds=300)).eta_min == 10
