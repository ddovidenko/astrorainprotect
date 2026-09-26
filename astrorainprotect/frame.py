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
