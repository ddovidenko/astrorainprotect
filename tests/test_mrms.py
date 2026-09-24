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
    k = (
        "CONUS/MergedReflectivityQCComposite_00.50/20260924/"
        "MRMS_MergedReflectivityQCComposite_00.50_20260924-011040.grib2.gz"
    )
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
    def error_handler(r):
        return httpx.Response(503, text="slow down")

    client = httpx.Client(transport=httpx.MockTransport(error_handler))
    with pytest.raises(MrmsError, match="503"):
        list_keys(client, "CONUS/PrecipRate_00.00/20260924/")


def test_list_keys_network_error():
    def boom(request):
        raise httpx.ConnectError("no route")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(MrmsError, match="no route"):
        list_keys(client, "CONUS/PrecipRate_00.00/20260924/")
