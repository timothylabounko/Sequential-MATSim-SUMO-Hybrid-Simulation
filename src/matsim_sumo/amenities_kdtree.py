from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import covers, points
from scipy.spatial import KDTree


class AmenitiesKDTree:
    def __init__(self, amenities: pd.DataFrame, x_col: str = "x", y_col: str = "y", weight_col: str = "weight"):
        df = amenities.loc[:, [x_col, y_col, weight_col]].copy()
        df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
        df[y_col] = pd.to_numeric(df[y_col], errors="coerce")
        df[weight_col] = pd.to_numeric(df[weight_col], errors="coerce")
        df = df.dropna(subset=[x_col, y_col, weight_col])
        df = df[df[weight_col] > 0]
        if df.empty:
            raise ValueError("Amenities table is empty after filtering valid x/y/weight.")
        self.xy = df[[x_col, y_col]].to_numpy(dtype=float)
        self.w = df[weight_col].to_numpy(dtype=float)
        self.tree = KDTree(self.xy)

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        sep: str = ",",
        x_col: str = "x",
        y_col: str = "y",
        weight_col: str = "weight",
        boundary_path: str | Path | None = None,
    ) -> "AmenitiesKDTree":
        df = pd.read_csv(path, sep=sep)
        if boundary_path is not None:
            b = gpd.read_file(boundary_path).union_all()
            xy = df[[x_col, y_col]].apply(pd.to_numeric, errors="coerce")
            valid = xy[x_col].notna() & xy[y_col].notna()
            inside = pd.Series(False, index=df.index)
            if valid.any():
                inside.loc[valid] = np.asarray(
                    covers(b, points(xy.loc[valid, x_col].to_numpy(dtype=float), xy.loc[valid, y_col].to_numpy(dtype=float))),
                    dtype=bool,
                )
            df = df.loc[inside].copy()
        return cls(df, x_col=x_col, y_col=y_col, weight_col=weight_col)

    def sample_within_radius(
        self,
        x: np.ndarray,
        y: np.ndarray,
        radius_m: float,
        rng,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        q = np.column_stack([x, y]).astype(float, copy=False)
        out = q.copy()
        hit = np.zeros(len(q), dtype=bool)
        for i, ids in enumerate(self.tree.query_ball_point(q, r=float(radius_m))):
            if not ids:
                continue
            idx = np.asarray(ids, dtype=int)
            ww = self.w[idx]
            p = (ww / ww.sum()) if ww.sum() > 0 else None
            pick = int(rng.choice(idx, p=p))
            out[i] = self.xy[pick]
            hit[i] = True
        return out[:, 0], out[:, 1], hit
