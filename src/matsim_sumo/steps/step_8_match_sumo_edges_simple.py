from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState


def _unit_vector_xy(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float]:
    dx = float(x1) - float(x0)
    dy = float(y1) - float(y0)
    n = float(np.hypot(dx, dy))
    return (np.nan, np.nan) if not np.isfinite(n) or n <= 0 else (dx / n, dy / n)


def _build_link_vectors(state: PipelineState) -> dict[str, tuple[float, float]]:
    if state.network is None:
        raise ValueError("state.network must be available before step 8.")
    links = state.network.links.loc[:, ["link_id", "from_node", "to_node"]].copy()
    nodes = state.network.nodes.loc[:, ["node_id", "x", "y"]].copy()
    links = links.merge(
        nodes.rename(columns={"node_id": "from_node", "x": "fx", "y": "fy"}),
        on="from_node",
        how="left",
    ).merge(
        nodes.rename(columns={"node_id": "to_node", "x": "tx", "y": "ty"}),
        on="to_node",
        how="left",
    )
    vecs: dict[str, tuple[float, float]] = {}
    for row in links.itertuples(index=False):
        vx, vy = _unit_vector_xy(row.fx, row.fy, row.tx, row.ty)
        if np.isfinite(vx) and np.isfinite(vy):
            vecs[str(row.link_id)] = (vx, vy)
    return vecs


def _nearest_edges(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    x_col: str,
    y_col: str,
    max_radius_m: float,
    point_type: str,
    link_vectors: dict[str, tuple[float, float]],
) -> tuple[pd.Series, pd.Series]:
    xy = trips[[x_col, y_col]].apply(pd.to_numeric, errors="coerce")
    valid = xy[x_col].notna() & xy[y_col].notna()
    edge = pd.Series(index=trips.index, dtype=object)
    dist = pd.Series(index=trips.index, dtype=float)
    if not valid.any():
        return edge, dist

    link_col = "start_link" if point_type == "start" else "end_link"
    matsim_vec = trips.loc[valid, link_col].astype(str).map(link_vectors)
    pts = gpd.GeoDataFrame(
        {
            "row_id": trips.index[valid],
            "mvx": matsim_vec.map(lambda v: v[0] if isinstance(v, tuple) else np.nan).to_numpy(dtype=float),
            "mvy": matsim_vec.map(lambda v: v[1] if isinstance(v, tuple) else np.nan).to_numpy(dtype=float),
        },
        geometry=gpd.points_from_xy(xy.loc[valid, x_col], xy.loc[valid, y_col]),
        crs=edges.crs,
    )

    nearest = gpd.sjoin_nearest(
        pts,
        edges[["edge_id", "edge_dx", "edge_dy", "geometry"]],
        how="left",
        max_distance=float(max_radius_m),
        distance_col="dist_m",
    )
    nearest = nearest.sort_values(["row_id", "dist_m"]).drop_duplicates("row_id", keep="first")
    edge.loc[nearest["row_id"].to_numpy()] = nearest["edge_id"].to_numpy()
    dist.loc[nearest["row_id"].to_numpy()] = nearest["dist_m"].to_numpy(dtype=float)

    pairs = edges.sindex.query(pts.geometry, predicate="dwithin", distance=float(max_radius_m))
    if pairs.size == 0:
        return edge, dist

    q_idx = np.asarray(pairs[0], dtype=int)
    e_idx = np.asarray(pairs[1], dtype=int)
    cand = pd.DataFrame(
        {
            "row_id": pts["row_id"].to_numpy()[q_idx],
            "mvx": pts["mvx"].to_numpy()[q_idx],
            "mvy": pts["mvy"].to_numpy()[q_idx],
            "edge_id": edges["edge_id"].to_numpy()[e_idx],
            "edge_dx": edges["edge_dx"].to_numpy()[e_idx],
            "edge_dy": edges["edge_dy"].to_numpy()[e_idx],
        }
    )
    cand["dist_m"] = pts.geometry.iloc[q_idx].distance(edges.geometry.iloc[e_idx], align=False).to_numpy(dtype=float)
    cand = cand.sort_values(["row_id", "dist_m"]).groupby("row_id", sort=False).head(5)
    cand["dot"] = cand["mvx"] * cand["edge_dx"] + cand["mvy"] * cand["edge_dy"]
    cand = cand[(cand["dot"].isna()) | (cand["dot"] >= -0.1)]
    if cand.empty:
        return edge, dist

    best = cand.sort_values(["row_id", "dist_m"]).drop_duplicates("row_id", keep="first")
    edge.loc[best["row_id"].to_numpy()] = best["edge_id"].to_numpy()
    dist.loc[best["row_id"].to_numpy()] = best["dist_m"].to_numpy(dtype=float)
    return edge, dist


def _match_trips(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    max_radius_m: float,
    link_vectors: dict[str, tuple[float, float]],
) -> pd.DataFrame:
    out = trips.copy()
    edge_vec = edges.geometry.map(
        lambda g: _unit_vector_xy(g.coords[0][0], g.coords[0][1], g.coords[-1][0], g.coords[-1][1])
    )
    edges = edges.copy()
    edges["edge_dx"] = edge_vec.map(lambda v: v[0]).to_numpy(dtype=float)
    edges["edge_dy"] = edge_vec.map(lambda v: v[1]).to_numpy(dtype=float)
    out["from_edge"], out["from_edge_dist_m"] = _nearest_edges(
        out,
        edges,
        "start_x",
        "start_y",
        max_radius_m,
        point_type="start",
        link_vectors=link_vectors,
    )
    out["to_edge"], out["to_edge_dist_m"] = _nearest_edges(
        out,
        edges,
        "end_x",
        "end_y",
        max_radius_m,
        point_type="end",
        link_vectors=link_vectors,
    )
    out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna()
    return out


def _load_sumo_edges(state: PipelineState) -> gpd.GeoDataFrame:
    if state.sumo_network_geo is not None:
        geo = state.sumo_network_geo
    elif Path(state.sumo_network_lv95_path).exists():
        geo = gpd.read_file(state.sumo_network_lv95_path)
    else:
        raise ValueError("SUMO network GeoDataFrame not available in state and LV95 GeoJSON is missing.")
    if "edge_id" not in geo.columns:
        raise ValueError("SUMO network GeoDataFrame must contain an 'edge_id' column.")
    return geo[["edge_id", "geometry"]].copy()


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    out_path = Path(state.scaled_trips_matched_path)
    if state.scaled_trips is None:
        state.scaled_trips = pd.read_csv(state.scaled_trips_path, sep=";", low_memory=False)

    if out_path.exists():
        matched = pd.read_csv(out_path, sep=";", low_memory=False)
    else:
        edges = _load_sumo_edges(state)
        link_vectors = _build_link_vectors(state)
        matched = _match_trips(
            state.scaled_trips,
            edges=edges,
            max_radius_m=float(state.config["sumo_match_max_radius_m"]),
            link_vectors=link_vectors,
        )

    if "dep_time" in matched.columns:
        matched = matched.sort_values("dep_time").reset_index(drop=True)
    else:
        matched = matched.reset_index(drop=True)
    state.scaled_trips_matched = matched
    CsvWriter.write_df(state.scaled_trips_matched, out_path)
