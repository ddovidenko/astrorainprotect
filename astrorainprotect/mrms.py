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
