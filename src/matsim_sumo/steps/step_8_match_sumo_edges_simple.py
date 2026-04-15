from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState

SECOND_NEAREST_EXTRA_M = 50.0


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
    point_type: str,
    link_vectors: dict[str, tuple[float, float]],
) -> tuple[pd.Series, pd.Series]:
    xy = trips[[x_col, y_col]].apply(pd.to_numeric, errors="coerce")
    valid = xy[x_col].notna() & xy[y_col].notna()
    edge = pd.Series(index=trips.index, dtype=object)
    dist = pd.Series(index=trips.index, dtype=float)
    if not valid.any() or edges.empty:
        return edge, dist

    link_col = "new_start_link" if point_type == "start" else "new_end_link"
    if link_col not in trips.columns:
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
    sidx = edges.sindex
    nearest, nearest_dist = sidx.nearest(pts.geometry, return_all=False, return_distance=True)
    if nearest.size == 0:
        return edge, dist

    n = len(pts)
    p_idx = np.asarray(nearest[0], dtype=int)
    e1_idx = np.full(n, -1, dtype=int)
    d1 = np.full(n, np.nan, dtype=float)
    e1_idx[p_idx] = np.asarray(nearest[1], dtype=int)
    d1[p_idx] = np.asarray(nearest_dist, dtype=float)

    e2_idx = np.full(n, -1, dtype=int)
    d2 = np.full(n, np.nan, dtype=float)
    pairs = sidx.query(
        pts.geometry.iloc[p_idx],
        predicate="dwithin",
        distance=np.maximum(d1[p_idx] + SECOND_NEAREST_EXTRA_M, 1e-6),
    )
    if pairs.size:
        p2 = p_idx[np.asarray(pairs[0], dtype=int)]
        e2 = np.asarray(pairs[1], dtype=int)
        d = pts.geometry.iloc[p2].distance(edges.geometry.iloc[e2], align=False).to_numpy(dtype=float)
        keep = e2 != e1_idx[p2]
        if keep.any():
            p2, e2, d = p2[keep], e2[keep], d[keep]
            order = np.lexsort((d, p2))
            p2, e2, d = p2[order], e2[order], d[order]
            first = np.ones(p2.shape[0], dtype=bool)
            first[1:] = p2[1:] != p2[:-1]
            sel = p2[first]
            e2_idx[sel] = e2[first]
            d2[sel] = d[first]

    mvx = pts["mvx"].to_numpy(dtype=float)
    mvy = pts["mvy"].to_numpy(dtype=float)
    edge_dx = edges["edge_dx"].to_numpy(dtype=float)
    edge_dy = edges["edge_dy"].to_numpy(dtype=float)
    dot1 = np.full(n, np.nan, dtype=float)
    dot2 = np.full(n, np.nan, dtype=float)
    m1 = (e1_idx >= 0) & np.isfinite(mvx) & np.isfinite(mvy)
    m2 = (e2_idx >= 0) & np.isfinite(mvx) & np.isfinite(mvy)
    dot1[m1] = mvx[m1] * edge_dx[e1_idx[m1]] + mvy[m1] * edge_dy[e1_idx[m1]]
    dot2[m2] = mvx[m2] * edge_dx[e2_idx[m2]] + mvy[m2] * edge_dy[e2_idx[m2]]
    use2 = (e2_idx >= 0) & np.isfinite(dot2) & (~np.isfinite(dot1) | (dot2 > dot1))

    pick_idx = e1_idx.copy()
    pick_dist = d1.copy()
    pick_idx[use2] = e2_idx[use2]
    pick_dist[use2] = d2[use2]

    row_ids = pts["row_id"].to_numpy(dtype=int)
    ok = pick_idx >= 0
    edge_ids = edges["edge_id"].to_numpy(dtype=object)
    edge.loc[row_ids[ok]] = edge_ids[pick_idx[ok]]
    dist.loc[row_ids[ok]] = pick_dist[ok]
    return edge, dist


def _match_trips(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    link_vectors: dict[str, tuple[float, float]],
    start_edge_ids: set[str],
    end_edge_ids: set[str],
) -> pd.DataFrame:
    out = trips.copy()
    edge_vec = edges.geometry.map(
        lambda g: _unit_vector_xy(g.coords[0][0], g.coords[0][1], g.coords[-1][0], g.coords[-1][1])
    )
    edges = edges.copy()
    edges["edge_dx"] = edge_vec.map(lambda v: v[0]).to_numpy(dtype=float)
    edges["edge_dy"] = edge_vec.map(lambda v: v[1]).to_numpy(dtype=float)
    start_edges = edges[edges["edge_id"].isin(start_edge_ids)].copy()
    end_edges = edges[edges["edge_id"].isin(end_edge_ids)].copy()
    out["from_edge"], out["from_edge_dist_m"] = _nearest_edges(
        out,
        start_edges,
        "start_x",
        "start_y",
        point_type="start",
        link_vectors=link_vectors,
    )
    out["to_edge"], out["to_edge_dist_m"] = _nearest_edges(
        out,
        end_edges,
        "end_x",
        "end_y",
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


def _eligible_edge_ids(state: PipelineState, valid_edge_ids: set[str]) -> tuple[set[str], set[str]]:
    if state.sumo_net is None:
        raise ValueError("state.sumo_net must be available before step 8.")
    out_neighbors: dict[str, set[str]] = {}
    in_neighbors: dict[str, set[str]] = {}
    for e in state.sumo_net.getEdges(withInternal=False):
        eid = e.getID()
        if eid not in valid_edge_ids:
            continue
        out_neighbors[eid] = {n.getID() for n in e.getOutgoing().keys() if n.getID() in valid_edge_ids}
        in_neighbors[eid] = {n.getID() for n in e.getIncoming().keys() if n.getID() in valid_edge_ids}

    def _fixed_point(neighbors: dict[str, set[str]]) -> set[str]:
        keep = set(neighbors)
        while True:
            nxt = {eid for eid in keep if any(n in keep for n in neighbors[eid])}
            if len(nxt) == len(keep):
                return nxt
            keep = nxt

    start_ids = _fixed_point(out_neighbors)
    end_ids = _fixed_point(in_neighbors)
    return start_ids, end_ids


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    if state.scaled_trips is None:
        state.scaled_trips = pd.read_csv(state.scaled_trips_path, sep=";", low_memory=False)
    edges = _load_sumo_edges(state)
    link_vectors = _build_link_vectors(state)
    valid_edge_ids = set(edges["edge_id"].astype(str))
    start_edge_ids, end_edge_ids = _eligible_edge_ids(state, valid_edge_ids)
    matched = _match_trips(
        state.scaled_trips,
        edges=edges,
        link_vectors=link_vectors,
        start_edge_ids=start_edge_ids,
        end_edge_ids=end_edge_ids,
    )

    if "dep_time" in matched.columns:
        matched = matched.sort_values("dep_time").reset_index(drop=True)
    else:
        matched = matched.reset_index(drop=True)
    state.scaled_trips_matched = matched
    CsvWriter.write_df(state.scaled_trips_matched, state.scaled_trips_matched_path)
