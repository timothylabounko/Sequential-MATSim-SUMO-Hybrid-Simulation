from __future__ import annotations

import argparse
import os
import tempfile
import warnings
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matsim_sumo_matplotlib"))

import geopandas as gpd
import matplotlib
import networkx as nx
import pandas as pd
from matplotlib.lines import Line2D
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from matsim_sumo.visualization.plot_style import BOUNDARY_STYLE, NETWORK_STYLE, apply_spatial_axes

matplotlib.use("Agg")
import matplotlib.pyplot as plt


CROSSING_TYPES_FILENAME = "crossing_segment_types_sumo_network.png"
TYPE_COLORS = {
    "Passing-through": "#a6611a",
    "Inside": "#dfc27d",
    "Entering": "#80cdc1",
    "Leaving": "#018571",
}


def _read_boundary(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    return gpd.GeoDataFrame(geometry=[gdf.union_all()], crs=gdf.crs or "EPSG:2056")


def _load_matsim_geo(path: Path, bounds) -> gpd.GeoDataFrame:
    import matsim

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        geo = matsim.read_network(str(path)).as_geo(projection="EPSG:2056")
    x0, y0, x1, y1 = bounds
    return geo.cx[x0 - 5000 : x1 + 5000, y0 - 5000 : y1 + 5000].copy()


def _norm_dist(row, bounds, xy_cols, targets) -> float:
    x0, y0, x1, y1 = bounds
    vals = []
    for (x_col, y_col), (u, v) in zip(xy_cols, targets):
        vals.append(((float(row[x_col]) - (x0 + u * (x1 - x0))) / (x1 - x0)) ** 2)
        vals.append(((float(row[y_col]) - (y0 + v * (y1 - y0))) / (y1 - y0)) ** 2)
    return sum(vals)


def _best(df: pd.DataFrame, crossing_type: str, bounds, targets, swap: bool = False) -> pd.Series:
    rows = df.loc[df["crossing_type"].eq(crossing_type)].copy()
    cols = [("start_x", "start_y"), ("end_x", "end_y")]
    rows["_score"] = rows.apply(lambda r: _norm_dist(r, bounds, cols, targets), axis=1)
    if swap:
        rows["_score"] = rows.apply(lambda r: min(r["_score"], _norm_dist(r, bounds, cols, targets[::-1])), axis=1)
    return rows.sort_values("_score").iloc[0]


def _line_from_links(link_ids: object, link_geo: dict[str, LineString]) -> LineString:
    coords = []
    for link_id in str(link_ids).split():
        geom = link_geo.get(link_id)
        if geom is None:
            continue
        pts = list(geom.coords)
        coords.extend(pts if not coords or coords[-1] != pts[0] else pts[1:])
    return LineString(coords) if len(coords) > 1 else LineString()


def _snap(point: Point, boundary) -> Point:
    return nearest_points(point, boundary.boundary)[1]


def _outside(cut: Point, inside: Point, dist: float = 1800.0) -> tuple[float, float]:
    dx, dy = cut.x - inside.x, cut.y - inside.y
    scale = dist / max((dx * dx + dy * dy) ** 0.5, 1.0)
    return cut.x + dx * scale, cut.y + dy * scale


def _with_cut_endpoints(line: LineString, start: Point | None = None, end: Point | None = None) -> list[tuple[float, float]]:
    coords = list(line.coords)
    if start is not None:
        coords[0] = (start.x, start.y)
    if end is not None:
        coords[-1] = (end.x, end.y)
    return coords


def _inside_path(row: pd.Series, geo: gpd.GeoDataFrame, link_geo: dict[str, LineString]) -> LineString:
    start, end = str(row["start_link"]), str(row["end_link"])
    if start not in link_geo or end not in link_geo:
        return LineString([(row["start_x"], row["start_y"]), (row["end_x"], row["end_y"])])
    G = nx.DiGraph()
    for r in geo.loc[:, ["link_id", "from_node", "to_node", "length"]].itertuples(index=False):
        G.add_edge(str(r.from_node), str(r.to_node), link=str(r.link_id), weight=float(r.length))
    links = geo.set_index(geo["link_id"].astype(str))
    try:
        nodes = nx.shortest_path(G, str(links.loc[start, "to_node"]), str(links.loc[end, "from_node"]), weight="weight")
        mid = [G[u][v]["link"] for u, v in zip(nodes, nodes[1:])]
        return _line_from_links(" ".join([start, *mid, end]), link_geo)
    except (nx.NetworkXNoPath, nx.NodeNotFound, KeyError):
        return LineString([(row["start_x"], row["start_y"]), (row["end_x"], row["end_y"])])


def _plot_route(ax, coords, label: str) -> None:
    color = TYPE_COLORS[label]
    line = LineString(coords)
    ax.plot(*line.xy, color=color, lw=3.0, solid_capstyle="round", solid_joinstyle="round", zorder=5)


def plot_crossing_segment_types(
    sumo_gdf: gpd.GeoDataFrame,
    boundary_gdf: gpd.GeoDataFrame,
    crossing_df: pd.DataFrame,
    inside_df: pd.DataFrame,
    matsim_geo: gpd.GeoDataFrame,
    out_path: Path,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if sumo_gdf.crs is None:
        sumo_gdf = sumo_gdf.set_crs(boundary_gdf.crs or "EPSG:2056")
    if str(boundary_gdf.crs) != str(sumo_gdf.crs):
        boundary_gdf = boundary_gdf.to_crs(sumo_gdf.crs)

    boundary = boundary_gdf.union_all()
    bounds = boundary_gdf.total_bounds
    link_geo = dict(zip(matsim_geo["link_id"].astype(str), matsim_geo.geometry))
    rows = {
        "Passing-through": _best(crossing_df, "pass_through", bounds, [(0.14, 0.79), (0.76, 0.76)], swap=True),
        "Entering": _best(crossing_df, "entering", bounds, [(0.21, 0.18), (0.25, 0.42)]),
        "Leaving": _best(crossing_df, "leaving", bounds, [(0.73, 0.44), (0.78, 0.23)]),
    }
    inside = inside_df.copy()
    inside["crossing_type"] = "inside"
    rows["Inside"] = _best(inside, "inside", bounds, [(0.25, 0.56), (0.52, 0.48)])

    route_coords, starts, ends, cuts = {}, [], [], []
    for label, row in rows.items():
        if label == "Inside":
            coords = list(_inside_path(row, matsim_geo, link_geo).coords)
            route_coords[label] = coords
            starts.append(coords[0])
            ends.append(coords[-1])
            continue
        line = _line_from_links(row["route_links"], link_geo)
        start_cut = _snap(Point(float(row["start_x"]), float(row["start_y"])), boundary)
        end_cut = _snap(Point(float(row["end_x"]), float(row["end_y"])), boundary)
        if label == "Passing-through":
            coords = _with_cut_endpoints(line, start_cut, end_cut)
            coords = [_outside(start_cut, Point(coords[1])), *coords, _outside(end_cut, Point(coords[-2]))]
            cuts.extend([(start_cut.x, start_cut.y), (end_cut.x, end_cut.y)])
        elif label == "Entering":
            coords = _with_cut_endpoints(line, start_cut, None)
            coords = [_outside(start_cut, Point(coords[1])), *coords]
            cuts.append((start_cut.x, start_cut.y))
        else:
            coords = _with_cut_endpoints(line, None, end_cut)
            coords = [*coords, _outside(end_cut, Point(coords[-2]))]
            cuts.append((end_cut.x, end_cut.y))
        route_coords[label] = coords
        starts.append(coords[0])
        ends.append(coords[-1])

    fig, ax = plt.subplots(figsize=(10, 10))
    sumo_gdf.plot(ax=ax, label="SUMO network", **NETWORK_STYLE)
    boundary_gdf.boundary.plot(ax=ax, label="Boundary", **BOUNDARY_STYLE)
    ax.set_title("Crossing Segment Types")

    for label, coords in route_coords.items():
        _plot_route(ax, coords, label)
    ax.scatter(*zip(*starts), s=80, facecolors="white", edgecolors="#0b2a55", linewidths=1.3, zorder=10)
    ax.scatter(*zip(*ends), s=80, color="black", zorder=10)
    ax.scatter(*zip(*cuts), s=90, color="#e31a1c", zorder=11)
    apply_spatial_axes(ax, sumo_gdf.crs, legend=False)

    handles = [
        Line2D([0], [0], color=NETWORK_STYLE["color"], lw=0.8, alpha=NETWORK_STYLE["alpha"], label="SUMO network"),
        Line2D([0], [0], color=BOUNDARY_STYLE["color"], lw=1.6, label="Boundary"),
        *[Line2D([0], [0], color=c, lw=3, label=f"{k} segment") for k, c in TYPE_COLORS.items()],
        Line2D([0], [0], marker="o", color="none", markerfacecolor="white", markeredgecolor="#0b2a55", markersize=8, label="Trip origin"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="black", markeredgecolor="black", markersize=8, label="Trip destination"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#e31a1c", markeredgecolor="#e31a1c", markersize=8, label="Boundary crossing point"),
    ]
    ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.95, borderpad=0.45, labelspacing=0.35, handlelength=2.0)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Generate the report-only crossing segment type schematic.")
    p.add_argument("--run-dir", default="results/run_33_transcality_50pct")
    p.add_argument("--sumo-network", default="data/generated/sumo_network_lv95/transcality_lv95.geojson")
    p.add_argument("--boundary", default="data/generated/boundaries/transcality_boundary.geojson")
    p.add_argument("--matsim-network", default="data/inputs/matsim_output_50pct/output_network.xml.gz")
    p.add_argument("--output", default="")
    args = p.parse_args(argv)

    run_dir = Path(args.run_dir)
    boundary = _read_boundary(Path(args.boundary))
    matsim_geo = _load_matsim_geo(Path(args.matsim_network), boundary.total_bounds)
    out = Path(args.output) if args.output else run_dir / "plots" / CROSSING_TYPES_FILENAME
    plot_crossing_segment_types(
        gpd.read_file(args.sumo_network),
        boundary,
        pd.read_csv(run_dir / "4_crossing_segments_timed.csv.gz", sep=";", low_memory=False),
        pd.read_csv(run_dir / "5_trips_inside.csv.gz", sep=";", low_memory=False),
        matsim_geo,
        out,
    )
    print(out)


if __name__ == "__main__":
    main()
