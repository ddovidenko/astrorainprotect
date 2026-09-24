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
        msg = f"Pirate Weather returned HTTP {r.status_code} (bad key, quota, or outage?)"
        raise PirateError(msg)
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
        data = fetch_forecast(self._client, self._key, self._lat, self._lon)
        self._last = evaluate(data, **self._kw)
        return self._last
