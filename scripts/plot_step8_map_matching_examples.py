#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
import pandas as pd
import sumolib
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from matsim_sumo.network_preprocessing.network_preprocessing import preprocess_sumo_network_to_lv95


def _fixed_point(neighbors: dict[str, set[str]]) -> set[str]:
    keep = set(neighbors)
    while True:
        nxt = {eid for eid in keep if any(n in keep for n in neighbors[eid])}
        if nxt == keep:
            return nxt
        keep = nxt


def _eligible_edge_sets(net, valid_edge_ids: set[str]) -> tuple[set[str], set[str]]:
    out_neighbors, in_neighbors = {}, {}
    for edge in net.getEdges(withInternal=False):
        eid = edge.getID()
        if eid not in valid_edge_ids:
            continue
        out_neighbors[eid] = {n.getID() for n in edge.getOutgoing().keys() if n.getID() in valid_edge_ids}
        in_neighbors[eid] = {n.getID() for n in edge.getIncoming().keys() if n.getID() in valid_edge_ids}
    return _fixed_point(out_neighbors), _fixed_point(in_neighbors)


def _sample_points(trips: pd.DataFrame, x_col: str, y_col: str, edge_col: str, n: int, seed: int) -> gpd.GeoDataFrame:
    xy = trips[[x_col, y_col]].apply(pd.to_numeric, errors="coerce")
    keep = trips[edge_col].notna() & xy[x_col].notna() & xy[y_col].notna()
    if not keep.any():
        return gpd.GeoDataFrame(columns=["edge_id", "geometry"], geometry="geometry", crs="EPSG:2056")
    s = trips.loc[keep, [edge_col]].copy()
    s["x"], s["y"] = xy.loc[keep, x_col].to_numpy(), xy.loc[keep, y_col].to_numpy()
    s = s.sample(n=min(n, len(s)), random_state=seed)
    return gpd.GeoDataFrame(
        {"edge_id": s[edge_col].astype(str).to_numpy()},
        geometry=gpd.points_from_xy(s["x"], s["y"]),
        crs="EPSG:2056",
    )


def _plot_examples(
    admissible: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    sample_pts: gpd.GeoDataFrame,
    color: str,
    title: str,
    out_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 10))
    admissible.plot(ax=ax, color="black", linewidth=0.15, alpha=0.7, label=f"Admissible edges ({len(admissible):,})")
    boundary.boundary.plot(ax=ax, color="black", linewidth=1.2, label="Boundary")
    if not sample_pts.empty:
        sample_edges = admissible[admissible["edge_id"].isin(set(sample_pts["edge_id"]))]
        if not sample_edges.empty:
            sample_edges.plot(ax=ax, color=color, linewidth=2.2, alpha=0.95, label=f"Matched edges ({len(sample_edges):,})")
        sample_pts.plot(ax=ax, color=color, markersize=22, alpha=0.9, label=f"Random points ({len(sample_pts):,})")
    ax.set_title(title)
    ax.set_aspect("equal")
    ax.legend()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def _load_sumo_geo(cfg: dict) -> gpd.GeoDataFrame:
    geo_path = PROJECT_ROOT / cfg["sumo_network_lv95_tmp_file"]
    if not geo_path.exists():
        preprocess_sumo_network_to_lv95(
            network_path=PROJECT_ROOT / cfg["sumo_network_path"],
            vclasses=tuple(dict.fromkeys(cfg["sumo_vclass_map"].values())),
            out_path=geo_path,
        )
    geo = gpd.read_file(geo_path)[["edge_id", "geometry"]].copy()
    geo["edge_id"] = geo["edge_id"].astype(str)
    if geo.crs is None:
        geo = geo.set_crs("EPSG:2056")
    return geo


def plot_map_matching_examples(cfg: dict, out_dir: Path, n: int = 3, seed: int = 31) -> tuple[Path, Path]:
    sumo_geo = _load_sumo_geo(cfg)
    boundary = gpd.GeoDataFrame(geometry=[gpd.read_file(PROJECT_ROOT / cfg["boundary_file"]).union_all()], crs="EPSG:2056")
    if str(boundary.crs) != str(sumo_geo.crs):
        boundary = boundary.to_crs(sumo_geo.crs)

    net = sumolib.net.readNet(str(PROJECT_ROOT / cfg["sumo_network_path"]), withInternal=False)
    start_ids, end_ids = _eligible_edge_sets(net, set(sumo_geo["edge_id"]))
    start_edges = sumo_geo[sumo_geo["edge_id"].isin(start_ids)].copy()
    end_edges = sumo_geo[sumo_geo["edge_id"].isin(end_ids)].copy()

    matched_path = PROJECT_ROOT / cfg["output_dir"] / cfg.get("scaled_trips_matched_file", "8_scaled_trips_matched.csv.gz")
    trips = pd.read_csv(matched_path, sep=";", low_memory=False)
    start_pts = _sample_points(trips, "start_x", "start_y", "from_edge", n=n, seed=seed)
    end_pts = _sample_points(trips, "end_x", "end_y", "to_edge", n=n, seed=seed + 1)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_start = out_dir / "8_map_matching_start_examples.png"
    out_end = out_dir / "8_map_matching_end_examples.png"
    _plot_examples(
        start_edges,
        boundary,
        start_pts,
        color="dodgerblue",
        title="Map Matching: Start admissible edges + random matched starts",
        out_path=out_start,
    )
    _plot_examples(
        end_edges,
        boundary,
        end_pts,
        color="tomato",
        title="Map Matching: End admissible edges + random matched ends",
        out_path=out_end,
    )
    return out_start, out_end


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot Step 8 map-matching examples (start/end).")
    parser.add_argument("--config", default="configs/base_config.yaml", help="Pipeline config YAML path")
    parser.add_argument("--out-dir", default="", help="Output directory (default: <config.output_dir>/plots)")
    parser.add_argument("--n", type=int, default=3, help="Number of random points per plot")
    parser.add_argument("--seed", type=int, default=31, help="Random seed")
    args = parser.parse_args()

    cfg = yaml.safe_load((PROJECT_ROOT / args.config).read_text())
    out_dir = PROJECT_ROOT / args.out_dir if args.out_dir else PROJECT_ROOT / cfg["output_dir"] / "plots"
    out_start, out_end = plot_map_matching_examples(cfg, out_dir, n=max(1, args.n), seed=args.seed)
    print(f"Wrote: {out_start.resolve()}")
    print(f"Wrote: {out_end.resolve()}")


if __name__ == "__main__":
    main()
