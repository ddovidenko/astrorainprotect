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
        return make_frame(np.zeros((3, 3)), product=product,
                           valid_time=T0 + timedelta(minutes=2 * step))


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
