from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import wkt


COMPARISON_MODES = ["car", "pt", "walk", "bike", "car_passenger"]
MZ_COLOR = "#a6611a"
MATSIM_COLOR = "#80cdc1"
CHUNKSIZE = 1_000_000


def resolve_matsim_dir(arg: str, repo_root: Path) -> Path:
    p = Path(arg).expanduser()
    candidates = [p, repo_root / p, repo_root / "data" / p]
    for c in candidates:
        if c.exists():
            return c.resolve()
    raise FileNotFoundError(f"MATSim output dir not found: {arg}")


def resolve_output_dir(arg: str | None, matsim_dir: Path, repo_root: Path) -> Path:
    if not arg:
        return matsim_dir / f"mode_shares_{datetime.now():%Y%m%d_%H%M%S}"
    p = Path(arg).expanduser()
    return (repo_root / p).resolve() if not p.is_absolute() else p


def find_matsim_file(matsim_dir: Path, canonical: str) -> Path:
    direct = matsim_dir / canonical
    if direct.exists():
        return direct
    matches = sorted(matsim_dir.glob(f"*{canonical}"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"Missing {canonical} (or *{canonical}) in {matsim_dir}")
    raise RuntimeError(f"Multiple matches for *{canonical} in {matsim_dir}: {[m.name for m in matches]}")


def read_boundary_geom(path: Path):
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
        if "WKT" not in df.columns:
            raise ValueError(f"Boundary CSV must contain a WKT column: {path}")
        return gpd.GeoDataFrame(geometry=df["WKT"].map(wkt.loads), crs=4326).to_crs(2056).union_all()

    gdf = gpd.read_file(path)
    if gdf.crs is not None and str(gdf.crs) not in {"EPSG:2056", "2056"}:
        gdf = gdf.to_crs(2056)
    return gdf.union_all()


def within_mask(df: pd.DataFrame, x_col: str, y_col: str, geom) -> pd.Series:
    valid = df[x_col].notna() & df[y_col].notna()
    out = pd.Series(False, index=df.index)
    if valid.any():
        pts = gpd.GeoSeries(gpd.points_from_xy(df.loc[valid, x_col], df.loc[valid, y_col]), index=df.index[valid], crs=2056)
        out.loc[valid] = pts.within(geom).to_numpy()
    return out


def endpoint_mask(df: pd.DataFrame, x_col: str, y_col: str, geom) -> pd.Series:
    return within_mask(df, x_col, y_col, geom)


def share_table(counts: Counter, count_col: str, share_col: str, modes: list[str] | None = None) -> pd.DataFrame:
    rows = [{"mode": mode, count_col: float(count)} for mode, count in counts.items()]
    out = pd.DataFrame(rows)
    if out.empty:
        out = pd.DataFrame(columns=["mode", count_col])
    if modes is not None:
        out = pd.DataFrame({"mode": modes}).merge(out, on="mode", how="left").fillna({count_col: 0.0})
    else:
        out = out.sort_values(count_col, ascending=False)
    total = out[count_col].sum()
    out[share_col] = 0.0 if total == 0 else out[count_col] / total
    return out


def comparison_table(mz: pd.DataFrame, matsim: pd.DataFrame) -> pd.DataFrame:
    out = (
        pd.DataFrame({"mode": COMPARISON_MODES})
        .merge(mz, on="mode", how="left")
        .merge(matsim, on="mode", how="left")
        .fillna(0.0)
    )
    out["diff_pp_MATSim_minus_Microcensus"] = 100.0 * (out["MATSim_share"] - out["Microcensus_share"])
    return out
