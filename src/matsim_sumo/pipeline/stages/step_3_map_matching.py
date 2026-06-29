from __future__ import annotations

import gzip
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from matsim_sumo.io.csv_writer import CsvWriter
from matsim_sumo.pipeline.state import PipelineState
from matsim_sumo.utils.cache import disk_cache, file_dep, output_needs_refresh, param_dep

DISTANCE_DIAGNOSTIC_THRESHOLDS_M = (50.0, 100.0, 150.0, 250.0)
MATCH_SUMO_EDGES_LOGIC_VERSION = 9
BOUNDARY_START_TYPES = {"entering", "pass_through"}
BOUNDARY_END_TYPES = {"leaving", "pass_through"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_road_class(raw: object) -> str | None:
    if raw is None or pd.isna(raw):
        return None
    value = str(raw).strip().lower()
    if not value or value in {"nan", "none", "<na>"}:
        return None
    if value.startswith("highway."):
        value = value.removeprefix("highway.")
    if value.endswith("_link"):
        value = value.removesuffix("_link")
    return value or None


def _normalize_name(raw: object) -> str | None:
    if raw is None or pd.isna(raw):
        return None
    value = str(raw).strip().lower()
    return None if not value or value in {"nan", "none", "<na>"} else value


def _normalize_osm_id(raw: object) -> str | None:
    if raw is None or pd.isna(raw):
        return None
    value = str(raw).strip()
    return None if not value or value in {"nan", "None", "<NA>"} else value


def _sumo_base_osm_id(edge_id: object) -> str:
    return str(edge_id).lstrip("-").split("#", 1)[0]


def _unit_vector_xy(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float]:
    dx = float(x1) - float(x0)
    dy = float(y1) - float(y0)
    n = float(np.hypot(dx, dy))
    return (np.nan, np.nan) if not np.isfinite(n) or n <= 0 else (dx / n, dy / n)


def _parse_route_links(value: object) -> list[str]:
    if value is None or pd.isna(value):
        return []
    route = str(value).strip()
    if not route or route.lower() in {"nan", "none", "<na>"}:
        return []
    return route.split()


def _collect_route_link_ids(trips: pd.DataFrame) -> set[str]:
    if "route_links" not in trips.columns:
        return set()
    out: set[str] = set()
    for route in trips["route_links"].dropna():
        out.update(_parse_route_links(route))
    return out


def _build_link_vectors(state: PipelineState) -> dict[str, tuple[float, float]]:
    if state.network is None:
        raise ValueError("state.network must be available before Step 3.")
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


def _read_matsim_link_metadata(
    network_path: Path,
    link_ids: Iterable[object],
    link_vectors: dict[str, tuple[float, float]],
) -> dict[str, dict[str, object]]:
    wanted = {str(link_id) for link_id in link_ids if not pd.isna(link_id)}
    if not wanted:
        return {}

    opener = gzip.open if str(network_path).endswith(".gz") else open
    out: dict[str, dict[str, object]] = {}
    with opener(network_path, "rb") as src:
        for _, elem in ET.iterparse(src, events=("end",)):
            if _local_name(elem.tag) != "link":
                continue
            link_id = elem.get("id")
            if link_id not in wanted:
                elem.clear()
                continue

            attrs: dict[str, object] = {
                "road_class": None,
                "osm_id": None,
                "name_norm": None,
                "from_node": elem.get("from"),
                "to_node": elem.get("to"),
            }
            for child in elem:
                if _local_name(child.tag) != "attributes":
                    continue
                for attr in child:
                    if _local_name(attr.tag) != "attribute":
                        continue
                    name = attr.get("name")
                    if name == "osm:way:highway":
                        attrs["road_class"] = _normalize_road_class(attr.text)
                    elif name == "osm:way:id":
                        attrs["osm_id"] = _normalize_osm_id(attr.text)
                    elif name == "osm:way:name":
                        attrs["name_norm"] = _normalize_name(attr.text)

            vx, vy = link_vectors.get(str(link_id), (np.nan, np.nan))
            out[str(link_id)] = {**attrs, "vx": vx, "vy": vy}
            elem.clear()
            if len(out) == len(wanted):
                break
    return out


def _build_matsim_route_metadata(
    state: PipelineState,
    route_link_ids: set[str],
    link_vectors: dict[str, tuple[float, float]],
) -> dict[str, dict[str, object]]:
    if state.network_geo is None:
        raise ValueError("state.network_geo must be available before Step 3.")
    meta = _read_matsim_link_metadata(state.network_path, route_link_ids, link_vectors)
    geo = state.network_geo.loc[
        state.network_geo["link_id"].astype(str).isin(route_link_ids),
        ["link_id", "geometry"],
    ].copy()
    geo["link_id"] = geo["link_id"].astype(str)
    geometries = geo.drop_duplicates("link_id").set_index("link_id")["geometry"].to_dict()

    out: dict[str, dict[str, object]] = {}
    for link_id in route_link_ids:
        geom = geometries.get(link_id)
        vx, vy = link_vectors.get(link_id, (np.nan, np.nan))
        row = dict(meta.get(link_id, {}))
        row.setdefault("road_class", None)
        row.setdefault("osm_id", None)
        row.setdefault("name_norm", None)
        row.setdefault("from_node", None)
        row.setdefault("to_node", None)
        row.setdefault("vx", vx)
        row.setdefault("vy", vy)
        row["geometry"] = geom
        out[link_id] = row
    return out


def _build_sumo_edge_metadata(state: PipelineState, edge_ids: Iterable[object]) -> dict[str, dict[str, str | None]]:
    if state.sumo_net is None:
        raise ValueError("state.sumo_net must be available before Step 3.")
    needed = {str(edge_id) for edge_id in edge_ids if not pd.isna(edge_id)}
    out: dict[str, dict[str, str | None]] = {}
    for edge in state.sumo_net.getEdges(withInternal=False):
        edge_id = edge.getID()
        if edge_id not in needed:
            continue
        out[edge_id] = {
            "road_class": _normalize_road_class(edge.getType()),
            "sumo_name": edge.getName(),
            "from_node": edge.getFromNode().getID(),
            "to_node": edge.getToNode().getID(),
        }
        if len(out) == len(needed):
            break
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


def _attach_sumo_metadata(state: PipelineState, edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = edges.copy()
    out["edge_id"] = out["edge_id"].astype(str)
    meta = _build_sumo_edge_metadata(state, set(out["edge_id"]))
    for col in ("road_class", "sumo_name", "from_node", "to_node"):
        out[col] = out["edge_id"].map({edge_id: row[col] for edge_id, row in meta.items()})
    vec = out.geometry.map(
        lambda g: _unit_vector_xy(g.coords[0][0], g.coords[0][1], g.coords[-1][0], g.coords[-1][1])
    )
    out["edge_dx"] = vec.map(lambda v: v[0]).to_numpy(dtype=float)
    out["edge_dy"] = vec.map(lambda v: v[1]).to_numpy(dtype=float)
    return out


def _edge_neighbors(state: PipelineState, valid_edge_ids: set[str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    if state.sumo_net is None:
        raise ValueError("state.sumo_net must be available before Step 3.")
    out_neighbors: dict[str, set[str]] = {}
    in_neighbors: dict[str, set[str]] = {}
    for edge in state.sumo_net.getEdges(withInternal=False):
        edge_id = edge.getID()
        if edge_id not in valid_edge_ids:
            continue
        out_neighbors[edge_id] = {n.getID() for n in edge.getOutgoing().keys() if n.getID() in valid_edge_ids}
        in_neighbors[edge_id] = {n.getID() for n in edge.getIncoming().keys() if n.getID() in valid_edge_ids}
    return out_neighbors, in_neighbors


def _eligible_edge_ids(state: PipelineState, valid_edge_ids: set[str]) -> tuple[set[str], set[str]]:
    out_neighbors, in_neighbors = _edge_neighbors(state, valid_edge_ids)

    def _fixed_point(neighbors: dict[str, set[str]]) -> set[str]:
        keep = set(neighbors)
        while True:
            nxt = {edge_id for edge_id in keep if any(n in keep for n in neighbors[edge_id])}
            if len(nxt) == len(keep):
                return nxt
            keep = nxt

    return _fixed_point(out_neighbors), _fixed_point(in_neighbors)


def _route_candidates_for_link(
    link_id: str,
    meta: dict[str, object],
    edges: gpd.GeoDataFrame,
    radius_m: float,
    min_dot: float,
    road_type_enabled: bool,
) -> list[dict[str, object]]:
    line = meta.get("geometry")
    if line is None or getattr(line, "is_empty", True):
        return []
    mvx = float(meta.get("vx", np.nan))
    mvy = float(meta.get("vy", np.nan))
    if not np.isfinite(mvx) or not np.isfinite(mvy):
        return []

    hits = edges.sindex.query(line.buffer(radius_m), predicate="intersects")
    candidates: list[dict[str, object]] = []
    for edge in edges.iloc[np.asarray(hits, dtype=int)].itertuples():
        dist = float(line.distance(edge.geometry))
        if dist > radius_m:
            continue
        dot = mvx * float(edge.edge_dx) + mvy * float(edge.edge_dy)
        if not np.isfinite(dot) or dot < min_dot:
            continue

        matsim_class = meta.get("road_class")
        sumo_class = getattr(edge, "road_class", None)
        type_match = bool(road_type_enabled and matsim_class and sumo_class and matsim_class == sumo_class)
        osm_match = bool(meta.get("osm_id") and _sumo_base_osm_id(edge.edge_id) == str(meta["osm_id"]))
        name_match = bool(
            meta.get("name_norm")
            and getattr(edge, "sumo_name", None)
            and _normalize_name(edge.sumo_name) == meta["name_norm"]
        )
        midpoint = edge.geometry.interpolate(0.5, normalized=True)
        try:
            projection = float(line.project(midpoint, normalized=True))
        except Exception:
            projection = 0.5

        candidates.append(
            {
                "edge_id": str(edge.edge_id),
                "route_link": link_id,
                "dist": dist,
                "dot": float(dot),
                "projection": projection,
                "matsim_road_class": matsim_class,
                "sumo_road_class": sumo_class,
                "type_match": type_match,
                "osm_match": osm_match,
                "name_match": name_match,
            }
        )
    return candidates


def _build_corridor_candidates(
    route_metadata: dict[str, dict[str, object]],
    edges: gpd.GeoDataFrame,
    radius_m: float,
    min_dot: float,
    road_type_enabled: bool,
) -> dict[str, list[dict[str, object]]]:
    return {
        link_id: _route_candidates_for_link(link_id, meta, edges, radius_m, min_dot, road_type_enabled)
        for link_id, meta in route_metadata.items()
    }


def _route_lengths_m(route_metadata: dict[str, dict[str, object]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for link_id, meta in route_metadata.items():
        geom = meta.get("geometry")
        out[link_id] = float(geom.length) if geom is not None and not getattr(geom, "is_empty", True) else np.nan
    return out


def _candidate_sort_key(candidate: dict[str, object], side: str) -> tuple[float, float, float, int, int, int, str]:
    projection = float(candidate["projection"])
    if side == "to":
        projection = -projection
    return (
        projection,
        float(candidate["dist"]),
        -float(candidate["dot"]),
        0 if candidate["type_match"] else 1,
        0 if candidate["osm_match"] else 1,
        0 if candidate["name_match"] else 1,
        str(candidate["edge_id"]),
    )


def _select_endpoint_candidate(
    route_links: list[str],
    candidates_by_link: dict[str, list[dict[str, object]]],
    allowed_edge_ids: set[str],
    side: str,
) -> tuple[dict[str, object] | None, str | None]:
    seen_candidates = False
    link_iter = route_links if side == "from" else reversed(route_links)
    for link_id in link_iter:
        candidates = candidates_by_link.get(link_id, [])
        if not candidates:
            continue
        seen_candidates = True
        routeable = [c for c in candidates if c["edge_id"] in allowed_edge_ids]
        if routeable:
            return min(routeable, key=lambda c: _candidate_sort_key(c, side)), None
    return None, "not_routeable" if seen_candidates else "no_corridor_edge"


def _route_distance(route_links: list[str], route_lengths_m: dict[str, float]) -> float:
    lengths = [route_lengths_m.get(link_id, np.nan) for link_id in route_links]
    valid = [length for length in lengths if np.isfinite(length)]
    return float(np.sum(valid)) if valid else np.nan


def _link_value(row: pd.Series, side: str) -> object:
    if side == "from":
        return row.get("new_start_link", row.get("start_link", pd.NA))
    return row.get("new_end_link", row.get("end_link", pd.NA))


def _endpoint_link_ids(trips: pd.DataFrame) -> set[str]:
    cols = [c for c in ("new_start_link", "new_end_link", "start_link", "end_link") if c in trips.columns]
    if not cols:
        return set()
    values = pd.concat([trips[col].astype("string") for col in cols], ignore_index=True).dropna()
    return {str(value) for value in values if str(value).strip()}


def _nearest_point_candidate(
    candidate_edges: gpd.GeoDataFrame,
    x: object,
    y: object,
    route_link: object,
    link_metadata: dict[str, dict[str, object]],
    max_distance_m: float | None,
    min_dot: float,
    road_type_enabled: bool,
) -> dict[str, object] | None:
    px = pd.to_numeric(pd.Series([x]), errors="coerce").iloc[0]
    py = pd.to_numeric(pd.Series([y]), errors="coerce").iloc[0]
    if pd.isna(px) or pd.isna(py) or candidate_edges.empty:
        return None

    point = Point(float(px), float(py))
    max_distance_m = float(max_distance_m) if max_distance_m is not None else None
    if max_distance_m is None or max_distance_m <= 0:
        nearest, dist = candidate_edges.sindex.nearest([point], return_all=False, return_distance=True)
        hits = [(int(nearest[1][0]), float(dist[0]))] if nearest.size else []
    else:
        edge_idx = candidate_edges.sindex.query(point.buffer(max_distance_m), predicate="intersects")
        hits = []
        for idx in np.asarray(edge_idx, dtype=int):
            dist = float(point.distance(candidate_edges.geometry.iloc[idx]))
            if dist <= max_distance_m:
                hits.append((idx, dist))
    if not hits:
        return None

    route_link_id = None if pd.isna(route_link) else str(route_link)
    meta = link_metadata.get(route_link_id, {}) if route_link_id else {}
    mvx = float(meta.get("vx", np.nan))
    mvy = float(meta.get("vy", np.nan))
    matsim_class = meta.get("road_class")
    scored: list[tuple[tuple[int, float, float, str], dict[str, object]]] = []

    for edge_idx, dist in hits:
        edge = candidate_edges.iloc[edge_idx]
        dot = np.nan
        direction_match = False
        if np.isfinite(mvx) and np.isfinite(mvy):
            dot = mvx * float(edge.edge_dx) + mvy * float(edge.edge_dy)
            direction_match = bool(np.isfinite(dot) and dot >= min_dot)

        sumo_class = edge.road_class
        type_match = bool(road_type_enabled and matsim_class and sumo_class and matsim_class == sumo_class)
        rec = {
            "edge_id": str(edge.edge_id),
            "route_link": pd.NA if route_link_id is None else route_link_id,
            "dist": float(dist),
            "dot": float(dot) if np.isfinite(dot) else np.nan,
            "matsim_road_class": matsim_class if matsim_class else pd.NA,
            "sumo_road_class": sumo_class,
        }
        scored.append(
            (
                (
                    0 if direction_match else 1,
                    0 if type_match else 1,
                    float(dist),
                    -float(dot) if np.isfinite(dot) else 0.0,
                    str(edge.edge_id),
                ),
                rec,
            )
        )

    return min(scored, key=lambda item: item[0])[1] if scored else None


def _match_trips(
    trips: pd.DataFrame,
    edges: gpd.GeoDataFrame,
    candidates_by_link: dict[str, list[dict[str, object]]],
    start_edge_ids: set[str],
    end_edge_ids: set[str],
    route_lengths_m: dict[str, float] | None = None,
    link_metadata: dict[str, dict[str, object]] | None = None,
    inside_max_endpoint_distance_m: float | None = None,
    min_dot: float = 0.3,
    road_type_enabled: bool = True,
) -> pd.DataFrame:
    out = trips.copy()
    edge_meta = edges.set_index(edges["edge_id"].astype(str))
    crossing = out.get("crossing_type", pd.Series("", index=out.index)).astype(str)
    route_lengths_m = route_lengths_m or {}
    link_metadata = link_metadata or {}
    start_edges = edges[edges["edge_id"].astype(str).isin(start_edge_ids)].copy()
    end_edges = edges[edges["edge_id"].astype(str).isin(end_edge_ids)].copy()

    for col in (
        "from_edge",
        "to_edge",
        "from_junction",
        "to_junction",
        "from_match_policy",
        "to_match_policy",
        "from_route_match_link",
        "to_route_match_link",
        "from_matsim_road_class",
        "to_matsim_road_class",
        "from_sumo_road_class",
        "to_sumo_road_class",
        "sumo_match_drop_reason",
    ):
        out[col] = pd.NA
    for col in ("from_match_dot", "to_match_dot", "from_match_dist_m", "to_match_dist_m"):
        out[col] = np.nan
    out["route_link_count"] = 0
    out["route_length_m"] = np.nan

    for idx, row in out.iterrows():
        route_links = _parse_route_links(row.get("route_links"))
        out.at[idx, "route_link_count"] = len(route_links)
        out.at[idx, "route_length_m"] = _route_distance(route_links, route_lengths_m)

        if not route_links:
            if crossing.loc[idx] == "inside":
                start = _nearest_point_candidate(
                    start_edges,
                    row.get("start_x"),
                    row.get("start_y"),
                    _link_value(row, "from"),
                    link_metadata,
                    inside_max_endpoint_distance_m,
                    min_dot,
                    road_type_enabled,
                )
                end = _nearest_point_candidate(
                    end_edges,
                    row.get("end_x"),
                    row.get("end_y"),
                    _link_value(row, "to"),
                    link_metadata,
                    inside_max_endpoint_distance_m,
                    min_dot,
                    road_type_enabled,
                )
                if start is not None:
                    _assign_endpoint(out, idx, "from", start, policy="inside_nearest")
                if end is not None:
                    _assign_endpoint(out, idx, "to", end, policy="inside_nearest")
                if start is not None and end is not None:
                    continue
                out.at[idx, "sumo_match_drop_reason"] = "inside_nearest_unmatched"
                continue
            out.at[idx, "sumo_match_drop_reason"] = "no_route_links"
            continue

        start, start_reason = _select_endpoint_candidate(route_links, candidates_by_link, start_edge_ids, "from")
        end, end_reason = _select_endpoint_candidate(route_links, candidates_by_link, end_edge_ids, "to")
        if start is not None:
            _assign_endpoint(out, idx, "from", start)
        if end is not None:
            _assign_endpoint(out, idx, "to", end)

        if start is None or end is None:
            out.at[idx, "sumo_match_drop_reason"] = _drop_reason(start_reason, end_reason)
            continue

        ctype = crossing.loc[idx]
        if ctype in BOUNDARY_START_TYPES:
            out.at[idx, "from_junction"] = edge_meta.at[str(start["edge_id"]), "from_node"]
        if ctype in BOUNDARY_END_TYPES:
            out.at[idx, "to_junction"] = edge_meta.at[str(end["edge_id"]), "to_node"]

    out["from_edge_dist_m"] = out["from_match_dist_m"]
    out["to_edge_dist_m"] = out["to_match_dist_m"]
    out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna()
    out.loc[out["match_ok"], "sumo_match_drop_reason"] = pd.NA
    return out


def _assign_endpoint(
    out: pd.DataFrame,
    idx: object,
    prefix: str,
    candidate: dict[str, object],
    policy: str = "route_corridor",
) -> None:
    out.at[idx, f"{prefix}_edge"] = candidate["edge_id"]
    out.at[idx, f"{prefix}_match_policy"] = policy
    out.at[idx, f"{prefix}_route_match_link"] = candidate["route_link"]
    out.at[idx, f"{prefix}_match_dot"] = candidate["dot"]
    out.at[idx, f"{prefix}_match_dist_m"] = candidate["dist"]
    out.at[idx, f"{prefix}_matsim_road_class"] = candidate["matsim_road_class"]
    out.at[idx, f"{prefix}_sumo_road_class"] = candidate["sumo_road_class"]


def _drop_reason(start_reason: str | None, end_reason: str | None) -> str:
    if start_reason == "no_corridor_edge" and end_reason == "no_corridor_edge":
        return "no_start_and_end_corridor_edge"
    if start_reason == "not_routeable" or end_reason == "not_routeable":
        if start_reason == "not_routeable" and end_reason == "not_routeable":
            return "not_routeable"
        return "start_not_routeable" if start_reason == "not_routeable" else "end_not_routeable"
    if start_reason is not None and end_reason is not None:
        return "no_start_and_end_corridor_edge"
    if start_reason is not None:
        return "no_start_corridor_edge"
    return "no_end_corridor_edge"


def _endpoint_distance_diagnostics(matched: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    series = {
        "from": pd.to_numeric(matched.get("from_match_dist_m"), errors="coerce"),
        "to": pd.to_numeric(matched.get("to_match_dist_m"), errors="coerce"),
    }
    series["max"] = pd.concat([series["from"], series["to"]], axis=1).max(axis=1)

    out: dict[str, dict[str, float | int]] = {}
    for label, values in series.items():
        valid = values.dropna().astype(float)
        row: dict[str, float | int] = {"count": int(valid.size)}
        if valid.empty:
            for pct in (50, 90, 95, 99, 100):
                row[f"p{pct}_m"] = float("nan")
        else:
            for pct in (50, 90, 95, 99, 100):
                row[f"p{pct}_m"] = float(np.percentile(valid.to_numpy(dtype=float), pct))
        for threshold in DISTANCE_DIAGNOSTIC_THRESHOLDS_M:
            row[f"gt_{int(threshold)}m"] = int(valid.gt(threshold).sum())
        out[label] = row
    return out


def _format_distance_diagnostics(diagnostics: dict[str, dict[str, float | int]]) -> str:
    parts = []
    for label in ("from", "to", "max"):
        row = diagnostics.get(label, {})
        parts.append(
            f"{label}: p50={row.get('p50_m', float('nan')):.1f}m "
            f"p90={row.get('p90_m', float('nan')):.1f}m "
            f"p95={row.get('p95_m', float('nan')):.1f}m "
            f"p99={row.get('p99_m', float('nan')):.1f}m "
            f"max={row.get('p100_m', float('nan')):.1f}m"
        )
    return "; ".join(parts)


def _apply_endpoint_distance_guardrail(
    matched: pd.DataFrame,
    max_distance_m: float | None = None,
    min_pass_through_route_distance_m: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = matched.copy()
    from_dist = pd.to_numeric(out.get("from_match_dist_m"), errors="coerce")
    to_dist = pd.to_numeric(out.get("to_match_dist_m"), errors="coerce")
    out["sumo_endpoint_max_dist_m"] = pd.concat([from_dist, to_dist], axis=1).max(axis=1)
    out["match_ok"] = out["from_edge"].notna() & out["to_edge"].notna()
    if "sumo_match_drop_reason" not in out.columns:
        out["sumo_match_drop_reason"] = pd.NA

    keep = out["match_ok"].copy()
    pass_through = out.get("crossing_type", pd.Series("", index=out.index)).astype(str).eq("pass_through")
    too_short = pd.Series(False, index=out.index)
    same_junction = pd.Series(False, index=out.index)

    if float(min_pass_through_route_distance_m or 0.0) > 0:
        route_dist = pd.to_numeric(out.get("route_length_m"), errors="coerce")
        too_short = pass_through & route_dist.lt(float(min_pass_through_route_distance_m))

    if {"from_junction", "to_junction"}.issubset(out.columns):
        from_j = out["from_junction"].astype("string")
        to_j = out["to_junction"].astype("string")
        same_junction = pass_through & from_j.notna() & to_j.notna() & from_j.eq(to_j)

    same_junction_pass_through = keep & same_junction
    if same_junction_pass_through.any():
        out.loc[same_junction_pass_through, "sumo_match_drop_reason"] = "same_boundary_junction_pass_through"
        keep &= ~same_junction_pass_through

    short_pass_through = keep & too_short
    if short_pass_through.any():
        out.loc[short_pass_through, "sumo_match_drop_reason"] = "short_pass_through_distance"
        keep &= ~short_pass_through

    return out.loc[keep].copy(), out.loc[~keep].copy()


def _match_sumo_edges_deps(state: PipelineState):
    deps = [
        file_dep(state.plans_path),
        file_dep(state.trips_path),
        file_dep(state.events_path),
        file_dep(state.extract_events_script_path),
        file_dep(state.network_path),
        file_dep(state.boundary_path),
        file_dep(state.sumo_network_path),
        file_dep(state.scaled_trips_path),
        param_dep("modes_keep", tuple(state.config["modes_keep"])),
        param_dep("network_crs", state.config.get("network_crs", "EPSG:2056")),
        param_dep("scale_factor", float(state.config["scale_factor"])),
        param_dep("dep_time_jitter_seconds", int(state.config["dep_time_jitter_seconds"])),
        param_dep("coord_jitter_radius_m", float(state.config["coord_jitter_radius_m"])),
        param_dep("demand_scaling_seed", int(state.config["demand_scaling_seed"])),
        param_dep("amenity_kdtree_enabled", bool(state.config["amenity_kdtree_enabled"])),
        param_dep("sumo_vclass_map", state.config["sumo_vclass_map"]),
        param_dep("sumo_match_road_type_enabled", bool(state.config["sumo_match_road_type_enabled"])),
        param_dep("sumo_match_corridor_radius_m", float(state.config["sumo_match_corridor_radius_m"])),
        param_dep("sumo_match_min_dot", float(state.config["sumo_match_min_dot"])),
        param_dep(
            "sumo_match_inside_max_endpoint_distance_m",
            float(state.config["sumo_match_inside_max_endpoint_distance_m"]),
        ),
        param_dep(
            "sumo_match_pass_through_min_route_distance_m",
            float(state.config["sumo_match_pass_through_min_route_distance_m"]),
        ),
        param_dep("match_sumo_edges_logic_version", MATCH_SUMO_EDGES_LOGIC_VERSION),
    ]
    if state.config["amenity_kdtree_enabled"]:
        deps.append(file_dep(state.config["amenities_path"]))
    return deps


@disk_cache(depends_on=lambda state: _match_sumo_edges_deps(state), step_name="match_sumo_edges")
def _compute_match_sumo_edges_cached(
    state: PipelineState,
) -> tuple[pd.DataFrame, dict[str, dict[str, float | int]], int, dict[str, int]]:
    if state.scaled_trips is None:
        state.scaled_trips = pd.read_csv(state.scaled_trips_path, sep=";", low_memory=False)

    edges = _attach_sumo_metadata(state, _load_sumo_edges(state))
    valid_edge_ids = set(edges["edge_id"].astype(str))
    start_edge_ids, end_edge_ids = _eligible_edge_ids(state, valid_edge_ids)

    link_vectors = _build_link_vectors(state)
    route_link_ids = _collect_route_link_ids(state.scaled_trips)
    endpoint_link_ids = _endpoint_link_ids(state.scaled_trips)
    link_ids = route_link_ids | endpoint_link_ids
    route_metadata = _build_matsim_route_metadata(state, link_ids, link_vectors)
    route_lengths = _route_lengths_m(route_metadata)
    candidates_by_link = _build_corridor_candidates(
        route_metadata,
        edges,
        radius_m=float(state.config["sumo_match_corridor_radius_m"]),
        min_dot=float(state.config["sumo_match_min_dot"]),
        road_type_enabled=bool(state.config["sumo_match_road_type_enabled"]),
    )
    matched = _match_trips(
        state.scaled_trips,
        edges,
        candidates_by_link,
        start_edge_ids,
        end_edge_ids,
        route_lengths_m=route_lengths,
        link_metadata=route_metadata,
        inside_max_endpoint_distance_m=float(state.config["sumo_match_inside_max_endpoint_distance_m"]),
        min_dot=float(state.config["sumo_match_min_dot"]),
        road_type_enabled=bool(state.config["sumo_match_road_type_enabled"]),
    )
    diagnostics = _endpoint_distance_diagnostics(matched)
    matched, dropped = _apply_endpoint_distance_guardrail(
        matched,
        min_pass_through_route_distance_m=float(state.config["sumo_match_pass_through_min_route_distance_m"]),
    )

    dropped_count = int(len(dropped))
    dropped_by_type = (
        dropped["sumo_match_drop_reason"].fillna("unmatched").astype(str).value_counts().sort_index().to_dict()
        if not dropped.empty
        else {}
    )
    if "dep_time" in matched.columns:
        matched = matched.sort_values("dep_time").reset_index(drop=True)
    else:
        matched = matched.reset_index(drop=True)
    return matched, diagnostics, dropped_count, dropped_by_type


def _log_match_sumo_edges_result(state: PipelineState) -> None:
    print(f"SUMO route-corridor distances: {_format_distance_diagnostics(state.sumo_endpoint_distance_diagnostics)}")
    if state.sumo_endpoint_guardrail_dropped_count:
        dropped = ", ".join(f"{key}={value}" for key, value in state.sumo_endpoint_guardrail_dropped_by_type.items())
        print(f"Dropped {state.sumo_endpoint_guardrail_dropped_count} trips by strict route-corridor matching ({dropped}).")
    else:
        print("Dropped 0 trips by strict route-corridor matching.")


def run_match_sumo_edges_stage(state: PipelineState) -> None:
    (
        state.scaled_trips_matched,
        state.sumo_endpoint_distance_diagnostics,
        state.sumo_endpoint_guardrail_dropped_count,
        state.sumo_endpoint_guardrail_dropped_by_type,
    ) = _compute_match_sumo_edges_cached(state, no_cache=bool(state.config.get("no_cache", False)))
    _log_match_sumo_edges_result(state)
    if output_needs_refresh(_compute_match_sumo_edges_cached, state.scaled_trips_matched_path):
        CsvWriter.write_df(state.scaled_trips_matched, state.scaled_trips_matched_path)
