import gzip
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from astrorainprotect.mrms import (
    BUCKET_URL,
    MISSING_BELOW,
    PRODUCTS,
    GridMeta,
    MrmsError,
    RadarSource,
    day_prefixes,
    decode_grib,
    fetch_grib,
    key_time,
    list_keys,
    newest_key,
    parse_listing,
    subset,
    to_frame,
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


def test_parse_listing_invalid_xml_raises_mrms_error():
    with pytest.raises(MrmsError):
        parse_listing("<not xml")


def test_key_time():
    k = (
        "CONUS/MergedReflectivityQCComposite_00.50/20260924/"
        "MRMS_MergedReflectivityQCComposite_00.50_20260924-011040.grib2.gz"
    )
    assert key_time(k) == datetime(2026, 9, 24, 1, 10, 40, tzinfo=UTC)


def test_key_time_bad():
    with pytest.raises(MrmsError):
        key_time("CONUS/x/whatever.grib2.gz")


def test_key_time_invalid_date_raises_mrms_error():
    with pytest.raises(MrmsError):
        key_time("CONUS/x/MRMS_x_20261399-000000.grib2.gz")


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
    # nearest grid row/col, half a cell off at most
    assert lats[50] == pytest.approx(29.97, abs=0.011)
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
    f = to_frame(
        "preciprate", datetime(2026, 9, 24, tzinfo=UTC),
        np.array([1.0, 0.0]), np.array([0.0, 1.0]), sub,
    )
    assert f.missing_fraction == pytest.approx(0.5)
    assert f.values.min() >= 0
    assert f.values[1, 1] == 2.0
    assert f.values.dtype == np.float32


def test_to_frame_reflectivity_missing():
    sub = np.array([[-999.0, -99.0], [-5.0, 42.0]], dtype=np.float32)
    f = to_frame(
        "reflectivity", datetime(2026, 9, 24, tzinfo=UTC),
        np.array([1.0, 0.0]), np.array([0.0, 1.0]), sub,
    )
    assert f.missing_fraction == pytest.approx(0.25)     # only -999 is missing
    assert f.values.min() >= 0                            # -99 and -5 clamp to 0
    assert f.values[1, 1] == 42.0


def test_missing_below_table():
    assert MISSING_BELOW == {"preciprate": 0.0, "reflectivity": -990.0}


def test_fetch_grib_gunzips():
    payload = gzip.compress(b"GRIB-bytes")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload))
    )
    assert fetch_grib(client, "CONUS/x/y.grib2.gz") == b"GRIB-bytes"


def test_fetch_grib_404():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(MrmsError, match="404"):
        fetch_grib(client, "CONUS/x/y.grib2.gz")


def test_fetch_grib_bad_gzip():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"nope"))
    )
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
    kr = "CONUS/MergedReflectivityQCComposite_00.50/20260924/MRMS_MergedReflectivityQCComposite_00.50_20260924-120040.grib2.gz"  # noqa: E501
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
