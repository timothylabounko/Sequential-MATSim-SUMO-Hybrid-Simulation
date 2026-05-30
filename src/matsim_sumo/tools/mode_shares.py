#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import os
import tempfile

import geopandas as gpd
import numpy as np
import pandas as pd

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MAIN_MODES_RAW = [
    "car",
    "car_passenger",
    "pt",
    "walk",
    "bike"
]
MODE_MAP = {
    "car": "car",
    "car_passenger": "car_passenger",
    "pt": "pt",
    "walk": "walk",
    "bike": "bike"
}
MAIN_MODES_COMPARISON = ["car", "car_passenger", "pt", "walk", "bike"]


def resolve_matsim_dir(arg: str, repo_root: Path) -> Path:
    p = Path(arg).expanduser()
    candidates = [p, repo_root / p, repo_root / "data" / p]
    for c in candidates:
        if c.exists():
            return c.resolve()
    raise FileNotFoundError(f"MATSim output dir not found: {arg}")


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


def within_mask(df: pd.DataFrame, x_col: str, y_col: str, geom) -> pd.Series:
    valid = df[x_col].notna() & df[y_col].notna()
    out = pd.Series(False, index=df.index)
    if valid.any():
        pts = gpd.GeoSeries(
            gpd.points_from_xy(df.loc[valid, x_col], df.loc[valid, y_col]),
            index=df.index[valid],
            crs=2056,
        )
        out.loc[valid] = pts.within(geom).to_numpy()
    return out


def weighted_share(df: pd.DataFrame, mode_col: str, weight_col: str, out_col: str) -> pd.DataFrame:
    out = df.groupby(mode_col, as_index=False)[weight_col].sum().rename(columns={weight_col: "weighted_trips"})
    total = out["weighted_trips"].sum()
    out[out_col] = 0.0 if total == 0 else 100.0 * out["weighted_trips"] / total
    return out[[mode_col, out_col]].rename(columns={mode_col: "mode"})


def unweighted_share(df: pd.DataFrame, mode_col: str, out_col: str) -> pd.DataFrame:
    out = df[mode_col].value_counts(normalize=True).mul(100.0).rename_axis("mode").reset_index(name=out_col)
    return out


def compare(mz_df: pd.DataFrame, mz_col: str, matsim_df: pd.DataFrame, matsim_col: str) -> pd.DataFrame:
    out = (
        pd.DataFrame({"mode": MAIN_MODES_COMPARISON})
        .merge(mz_df, on="mode", how="left")
        .merge(matsim_df, on="mode", how="left")
        .fillna(0.0)
    )
    out["diff_pp_MATSim_minus_MZ"] = out[matsim_col] - out[mz_col]
    return out


def plot_modeshare_comparison(df: pd.DataFrame, mz_col: str, matsim_col: str, title: str, out_path: Path) -> None:
    plot_df = df[["mode", mz_col, matsim_col]].copy().fillna(0.0)
    plot_df["mode"] = pd.Categorical(plot_df["mode"], categories=MAIN_MODES_COMPARISON, ordered=True)
    plot_df = plot_df.sort_values("mode")
    plot_df["diff"] = plot_df[matsim_col] - plot_df[mz_col]

    x = np.arange(len(plot_df))
    w = 0.38
    fig, axes = plt.subplots(1, 2, figsize=(14, 4.8), gridspec_kw={"width_ratios": [2.2, 1]})

    axes[0].bar(x - w / 2, plot_df[mz_col], width=w, color="#4C78A8", label="MZ")
    axes[0].bar(x + w / 2, plot_df[matsim_col], width=w, color="#F58518", label="MATSim")
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(plot_df["mode"].str.replace("_", " "))
    axes[0].set_ylabel("Mode share [%]")
    axes[0].set_title(title)
    axes[0].legend(frameon=False)
    axes[0].grid(axis="y", alpha=0.2)

    colors = np.where(plot_df["diff"] >= 0, "#F58518", "#54A24B")
    axes[1].barh(plot_df["mode"].astype(str).str.replace("_", " "), plot_df["diff"], color=colors)
    axes[1].axvline(0, color="black", linewidth=1)
    axes[1].set_title("MATSim - MZ [pp]")
    axes[1].grid(axis="x", alpha=0.2)

    plt.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mode-share analysis from notebook 10 and save all tables/plots.")
    parser.add_argument(
        "--matsim-output-dir",
        default="matsim_output_2",
        help="MATSim output folder (e.g. matsim_output_2, data/matsim_output_2, or absolute path).",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    matsim_dir = resolve_matsim_dir(args.matsim_output_dir, repo_root)
    out_dir = matsim_dir / f"mode_shares_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=False)

    zurich_city = gpd.read_file(repo_root / "data/boundaries/zurich_boundary.geojson")
    zurich_region = gpd.read_file(repo_root / "data/boundaries/zurich_region_cutter_epsg2056.json")
    mz_households = pd.read_pickle(repo_root / "data/MZ/data.microcensus.households__184cb99cc4380ea2dfc836a6cb1b6f9b.p")
    mz_persons = pd.read_pickle(repo_root / "data/MZ/data.microcensus.persons__184cb99cc4380ea2dfc836a6cb1b6f9b.p")
    mz_trips = pd.read_pickle(repo_root / "data/MZ/data.microcensus.trips__dea3ee4144d5b4ad94b57e124312a765.p")[0]
    matsim_trips = pd.read_csv(
        find_matsim_file(matsim_dir, "output_trips.csv.gz"),
        sep=";",
        compression="gzip",
        low_memory=False,
    )
    matsim_persons = pd.read_csv(
        find_matsim_file(matsim_dir, "output_persons.csv.gz"),
        sep=";",
        compression="gzip",
        low_memory=False,
    )

    mz_trips = mz_trips.loc[(mz_trips["crowfly_distance"] > 0) & (mz_trips["mode"].isin(MAIN_MODES_RAW))].copy()
    matsim_trips = matsim_trips.loc[
        (matsim_trips["euclidean_distance"] > 0) & (matsim_trips["main_mode"].isin(MAIN_MODES_RAW))
    ].copy()
    mz_trips["mode"] = mz_trips["mode"].map(MODE_MAP)
    matsim_trips["main_mode"] = matsim_trips["main_mode"].map(MODE_MAP)

    mz_trips["person_id"] = mz_trips["person_id"].astype(str)
    matsim_trips["person"] = matsim_trips["person"].astype(str)
    matsim_persons["person"] = matsim_persons["person"].astype(str)
    matsim_persons_valid = matsim_persons.loc[~matsim_persons["subpopulation"].isin(["crossborder", "freight"])].copy()
    matsim_valid_person_ids = set(matsim_persons_valid["person"])

    mz_weights = mz_persons[["person_id", "person_weight"]].copy()
    mz_weights["person_id"] = mz_weights["person_id"].astype(str)

    city_geom = zurich_city.union_all()
    region_geom = zurich_region.union_all()

    mz_home = mz_households[["person_id", "home_x", "home_y"]].dropna().drop_duplicates("person_id").copy()
    mz_home["person_id"] = mz_home["person_id"].astype(str)
    mz_city_ids = set(mz_home.loc[within_mask(mz_home, "home_x", "home_y", city_geom), "person_id"])
    mz_region_ids = set(mz_home.loc[within_mask(mz_home, "home_x", "home_y", region_geom), "person_id"])
    mz_living = weighted_share(
        mz_trips.loc[mz_trips["person_id"].isin(mz_city_ids)].merge(mz_weights, on="person_id", how="left"),
        "mode",
        "person_weight",
        "MZ_share_%",
    )

    matsim_home = matsim_persons_valid[["person", "home_x", "home_y"]].dropna(subset=["home_x", "home_y"]).copy()
    matsim_city_ids = set(matsim_home.loc[within_mask(matsim_home, "home_x", "home_y", city_geom), "person"])
    matsim_region_ids = set(matsim_home.loc[within_mask(matsim_home, "home_x", "home_y", region_geom), "person"])
    matsim_living = unweighted_share(
        matsim_trips.loc[matsim_trips["person"].isin(matsim_city_ids)],
        "main_mode",
        "MATSim_share_%",
    )
    comp_living = compare(mz_living, "MZ_share_%", matsim_living, "MATSim_share_%")

    mz_enter = mz_trips.loc[within_mask(mz_trips, "destination_x", "destination_y", city_geom)].merge(
        mz_weights, on="person_id", how="left"
    )
    mz_enter_share = weighted_share(mz_enter, "mode", "person_weight", "MZ_enter_share_%")
    matsim_enter = matsim_trips.loc[matsim_trips["person"].isin(matsim_valid_person_ids)]
    matsim_enter = matsim_enter.loc[within_mask(matsim_enter, "end_x", "end_y", city_geom)]
    matsim_enter_share = unweighted_share(matsim_enter, "main_mode", "MATSim_enter_share_%")
    comp_enter = compare(mz_enter_share, "MZ_enter_share_%", matsim_enter_share, "MATSim_enter_share_%")

    mz_overall = mz_trips.loc[mz_trips["person_id"].isin(mz_region_ids)].merge(mz_weights, on="person_id", how="left")
    mz_overall["person_weight"] = mz_overall["person_weight"].fillna(0.0)
    mz_overall_share = weighted_share(mz_overall, "mode", "person_weight", "MZ_overall_share_%")
    matsim_overall = matsim_trips.loc[matsim_trips["person"].isin(matsim_region_ids)]
    matsim_overall_share = unweighted_share(matsim_overall, "main_mode", "MATSim_overall_share_%")
    comp_overall = compare(mz_overall_share, "MZ_overall_share_%", matsim_overall_share, "MATSim_overall_share_%")

    comp_living.to_csv(out_dir / "comparison_living.csv", index=False)
    comp_enter.to_csv(out_dir / "comparison_entering.csv", index=False)
    comp_overall.to_csv(out_dir / "comparison_overall.csv", index=False)

    plot_modeshare_comparison(
        comp_living,
        "MZ_share_%",
        "MATSim_share_%",
        "Mode Share Comparison - Persons Living in City of Zurich",
        out_dir / "comparison_living.png",
    )
    plot_modeshare_comparison(
        comp_enter,
        "MZ_enter_share_%",
        "MATSim_enter_share_%",
        "Mode Share Comparison - Trips Entering City of Zurich",
        out_dir / "comparison_entering.png",
    )
    plot_modeshare_comparison(
        comp_overall,
        "MZ_overall_share_%",
        "MATSim_overall_share_%",
        "Mode Share Comparison - Persons Living in Zurich Region",
        out_dir / "comparison_overall.png",
    )

    print(f"Saved mode-share outputs to: {out_dir}")


if __name__ == "__main__":
    main()
