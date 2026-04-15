#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib
import pandas as pd
import sumolib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _parse_depths(raw: str) -> list[int]:
    depths = sorted({int(x.strip()) for x in raw.split(",") if x.strip()})
    if not depths or any(d < 1 for d in depths):
        raise ValueError("Depths must be a comma-separated list of positive integers.")
    return depths


def _build_neighbors(net, valid_edge_ids: set[str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    out_neighbors: dict[str, set[str]] = {}
    in_neighbors: dict[str, set[str]] = {}
    for edge in net.getEdges(withInternal=False):
        eid = edge.getID()
        if eid not in valid_edge_ids:
            continue
        out_neighbors[eid] = {n.getID() for n in edge.getOutgoing().keys() if n.getID() in valid_edge_ids}
        in_neighbors[eid] = {n.getID() for n in edge.getIncoming().keys() if n.getID() in valid_edge_ids}
    return out_neighbors, in_neighbors


def _sets_for_depth(
    out_neighbors: dict[str, set[str]],
    in_neighbors: dict[str, set[str]],
    depth: int,
) -> tuple[set[str], set[str]]:
    start_ids = {eid for eid, outs in out_neighbors.items() if outs}
    end_ids = {eid for eid, ins in in_neighbors.items() if ins}
    for _ in range(2, depth + 1):
        start_ids = {eid for eid, outs in out_neighbors.items() if any(n in start_ids for n in outs)}
        end_ids = {eid for eid, ins in in_neighbors.items() if any(n in end_ids for n in ins)}
    return start_ids, end_ids


def _fixed_point(neighbors: dict[str, set[str]]) -> tuple[set[str], int]:
    keep = set(neighbors)
    rounds = 0
    while True:
        rounds += 1
        nxt = {eid for eid in keep if any(n in keep for n in neighbors[eid])}
        if nxt == keep:
            return nxt, rounds
        keep = nxt


def _category_map(all_ids: set[str], start_ids: set[str], end_ids: set[str]) -> dict[str, str]:
    both = start_ids & end_ids
    start_only = start_ids - end_ids
    end_only = end_ids - start_ids
    neither = all_ids - (start_ids | end_ids)
    category = {eid: "both" for eid in both}
    category.update({eid: "start_only" for eid in start_only})
    category.update({eid: "end_only" for eid in end_only})
    category.update({eid: "neither" for eid in neither})
    return category


def _plot_categories(
    edges_gdf: gpd.GeoDataFrame,
    edge_to_category: dict[str, str],
    title: str,
    out_path: Path,
) -> dict[str, int]:
    base_lw = 0.42
    role_lw = {
        "both": 0.8,
        "start_only": 1.35,
        "end_only": 1.85,
        "neither": 1.35,
    }
    role_ls = {
        "both": "solid",
        "start_only": "solid",
        "end_only": (0, (0.5, 0.5)),
        "neither": "solid",
    }
    palette = {
        "both": "#4a4a4a",
        "start_only": "#1f77b4",
        "end_only": "#d62728",
        "neither": "#f0ad4e",
    }
    label = {
        "both": "Both start+end",
        "start_only": "Start only",
        "end_only": "End only",
        "neither": "Neither",
    }
    order = ["both", "start_only", "end_only", "neither"]

    gdf = edges_gdf.copy()
    gdf["category"] = gdf["edge_id"].map(edge_to_category).fillna("neither")
    counts = gdf["category"].value_counts().to_dict()

    fig, ax = plt.subplots(figsize=(11, 11))
    gdf.plot(ax=ax, color="#d9d9d9", linewidth=base_lw, alpha=0.5)
    for cat in order:
        part = gdf[gdf["category"] == cat]
        if part.empty:
            continue
        part.plot(
            ax=ax,
            color=palette[cat],
            linewidth=role_lw[cat],
            linestyle=role_ls[cat],
            alpha=0.98,
            label=f"{label[cat]} ({len(part):,})",
        )

    ax.set_title(title)
    ax.set_axis_off()
    ax.set_aspect("equal")
    ax.legend(loc="lower left", frameon=True, fontsize=9)
    fig.savefig(out_path, dpi=240, bbox_inches="tight")
    plt.close(fig)
    return {
        "both": int(counts.get("both", 0)),
        "start_only": int(counts.get("start_only", 0)),
        "end_only": int(counts.get("end_only", 0)),
        "neither": int(counts.get("neither", 0)),
    }


def _plot_removed_counts(summary: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    x = range(len(summary))
    ax.plot(x, summary["start_removed"], marker="o", color="#1f77b4", label="Start removed")
    ax.plot(x, summary["end_removed"], marker="s", color="#d62728", label="End removed")
    ax.set_xticks(list(x), summary["method"].tolist(), rotation=0)
    ax.set_ylabel("Removed edges")
    ax.set_title("Step 8 eligible-edge pruning by method")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.savefig(out_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot Step 8 start/end edge networks and their differences.")
    parser.add_argument(
        "--sumo-net",
        default="data/sumo_scenario/Zurich_Scenario_Transcality/VMOD.net.xml",
        help="Path to SUMO .net.xml",
    )
    parser.add_argument(
        "--sumo-geo",
        default="data/tmp/0_sumo_network_lv95.geojson",
        help="Path to SUMO LV95 edge GeoJSON",
    )
    parser.add_argument(
        "--out-dir",
        default="results/run_16_fixed-point_prune/plots",
        help="Output directory for maps and CSV summary",
    )
    parser.add_argument(
        "--depths",
        default="1,2,3,4,5,10",
        help="Comma-separated connection depths to plot",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    edges = gpd.read_file(args.sumo_geo)[["edge_id", "geometry"]].copy()
    edges["edge_id"] = edges["edge_id"].astype(str)
    valid_edge_ids = set(edges["edge_id"])

    net = sumolib.net.readNet(str(args.sumo_net), withInternal=False)
    out_neighbors, in_neighbors = _build_neighbors(net, valid_edge_ids)
    all_ids = set(out_neighbors)
    n_total = len(all_ids)
    depths = _parse_depths(args.depths)

    rows: list[dict[str, int | float | str]] = []
    for depth in depths:
        start_ids, end_ids = _sets_for_depth(out_neighbors, in_neighbors, depth)
        edge_to_category = _category_map(all_ids, start_ids, end_ids)
        counts = _plot_categories(
            edges,
            edge_to_category,
            title=f"Step 8 edge roles at connection depth {depth}",
            out_path=out_dir / f"step8_edge_roles_depth_{depth}.png",
        )
        rows.append(
            {
                "method": f"depth_{depth}",
                "depth": depth,
                "total_edges": n_total,
                "start_kept": len(start_ids),
                "start_removed": n_total - len(start_ids),
                "start_removed_pct": 100.0 * (n_total - len(start_ids)) / n_total,
                "end_kept": len(end_ids),
                "end_removed": n_total - len(end_ids),
                "end_removed_pct": 100.0 * (n_total - len(end_ids)) / n_total,
                "both_kept": counts["both"],
                "start_only_kept": counts["start_only"],
                "end_only_kept": counts["end_only"],
                "neither_kept": counts["neither"],
                "rounds_to_stable": depth,
            }
        )

    start_fp, start_rounds = _fixed_point(out_neighbors)
    end_fp, end_rounds = _fixed_point(in_neighbors)
    edge_to_category_fp = _category_map(all_ids, start_fp, end_fp)
    counts_fp = _plot_categories(
        edges,
        edge_to_category_fp,
        title="Step 8 edge roles at fixed-point prune",
        out_path=out_dir / "step8_edge_roles_fixed_point.png",
    )
    rows.append(
        {
            "method": "fixed_point",
            "depth": -1,
            "total_edges": n_total,
            "start_kept": len(start_fp),
            "start_removed": n_total - len(start_fp),
            "start_removed_pct": 100.0 * (n_total - len(start_fp)) / n_total,
            "end_kept": len(end_fp),
            "end_removed": n_total - len(end_fp),
            "end_removed_pct": 100.0 * (n_total - len(end_fp)) / n_total,
            "both_kept": counts_fp["both"],
            "start_only_kept": counts_fp["start_only"],
            "end_only_kept": counts_fp["end_only"],
            "neither_kept": counts_fp["neither"],
            "rounds_to_stable": max(start_rounds, end_rounds),
        }
    )

    summary = pd.DataFrame(rows)
    summary.to_csv(out_dir / "step8_edge_roles_summary.csv", index=False)
    _plot_removed_counts(summary, out_dir / "step8_removed_edges_summary.png")

    print(f"Total valid edges: {n_total}")
    print(f"Wrote summary CSV: {out_dir / 'step8_edge_roles_summary.csv'}")
    print(f"Wrote removed-count plot: {out_dir / 'step8_removed_edges_summary.png'}")
    print(f"Wrote role maps for depths {depths} and fixed_point in: {out_dir}")


if __name__ == "__main__":
    main()
