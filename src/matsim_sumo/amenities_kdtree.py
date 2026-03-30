from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import covers, points
from scipy.spatial import cKDTree


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
        self.tree = cKDTree(self.xy)

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
        n = len(q)
        hit = np.zeros(n, dtype=bool)
        if n == 0:
            return out[:, 0], out[:, 1], hit

        uq, inv = np.unique(q, axis=0, return_inverse=True)
        cand_by_u = self.tree.query_ball_point(uq, r=float(radius_m))
        order = np.argsort(inv, kind="mergesort")
        inv_sorted = inv[order]
        starts = np.r_[0, np.flatnonzero(np.diff(inv_sorted)) + 1]
        ends = np.r_[starts[1:], len(order)]

        for s, e in zip(starts, ends):
            u = int(inv_sorted[s])
            ids = cand_by_u[u]
            if len(ids) == 0:
                continue
            rows = order[s:e]
            idx = np.asarray(ids, dtype=int)
            ww = self.w[idx]
            p = (ww / ww.sum()) if ww.sum() > 0 else None
            picks = np.asarray(rng.choice(idx, size=len(rows), p=p), dtype=int)
            out[rows] = self.xy[picks]
            hit[rows] = True
        return out[:, 0], out[:, 1], hit
