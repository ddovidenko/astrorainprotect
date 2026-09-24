"""NOAA MRMS on AWS Open Data: listing, download, decode, subset."""

from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
import zlib
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import eccodes
import httpx
import numpy as np

from astrorainprotect.frame import Frame

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
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MrmsError(f"S3 listing returned unparseable XML: {exc}") from exc
    return [el.text for el in root.iter(f"{_S3_NS}Key") if el.text]


def key_time(key: str) -> datetime:
    m = _KEY_TIME.search(key)
    if not m:
        raise MrmsError(f"cannot parse timestamp from key {key!r}")
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise MrmsError(f"cannot parse timestamp from key {key!r}") from exc


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
    except (OSError, EOFError, zlib.error) as exc:
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
