from pathlib import Path

import numpy as np
import pandas as pd
from shapely.geometry import Point

from matsim_sumo.pipeline_state import PipelineState


def _ordered_unique_hits(route_links: str, cut_set: set[str]) -> list[str]:
    seen = set()
    hits = []
    for link_id in route_links.split():
        if link_id in cut_set and link_id not in seen:
            seen.add(link_id)
            hits.append(link_id)
    return hits


def filter_legs_touching_cut_links(df_legs: pd.DataFrame, cut_link_ids: pd.Series) -> pd.DataFrame:
    cut_set = set(cut_link_ids.astype(str))
    routes = df_legs["route_links"].fillna("").astype(str).to_numpy()

    keep_idx = []
    hit_links = []
    for idx, route in enumerate(routes):
        hits = _ordered_unique_hits(route, cut_set)
        if hits:
            keep_idx.append(idx)
            hit_links.append(" ".join(hits))

    out = df_legs.iloc[keep_idx].copy()
    out["cut_links_hit"] = hit_links
    return out


def _build_link_start_inside_map(network_geo: pd.DataFrame, boundary) -> pd.Series:
    link_id = network_geo["link_id"].astype(str)
    starts_inside = network_geo["geometry"].apply(lambda geom: boundary.covers(Point(geom.coords[0])))
    return pd.Series(starts_inside.to_numpy(), index=link_id)


def annotate_leg_entry_side(df_legs_cut: pd.DataFrame, network_geo: pd.DataFrame, boundary) -> pd.DataFrame:
    link_starts_inside = _build_link_start_inside_map(network_geo, boundary)
    out = df_legs_cut.copy()
    out["starts_inside"] = (
        out["route_links"]
        .fillna("")
        .str.split()
        .str[0]
        .map(link_starts_inside)
        .fillna(False)
        .astype(bool)
    )
    return out


def _cut_positions_in_route(route: list[str], cuts: list[str]) -> list[int]:
    pos = []
    j = 0
    for cut in cuts:
        while j < len(route) and route[j] != cut:
            j += 1
        if j < len(route):
            pos.append(j)
            j += 1
    return pos


def _inside_intervals(route_len: int, cut_positions: list[int], starts_inside: bool) -> list[tuple[int, int]]:
    """
    Return [start, end) index intervals that are inside the boundary.
    """
    intervals = []
    inside = starts_inside
    start_idx = 0 if inside else None

    for cut_idx in cut_positions:
        if inside:
            intervals.append((start_idx, cut_idx + 1))
            start_idx = None
            inside = False
        else:
            start_idx = cut_idx
            inside = True

    if inside and start_idx is not None and start_idx < route_len:
        intervals.append((start_idx, route_len))

    return intervals


def _classify_inside_segment(
    interval_start: int,
    interval_end: int,
    route_len: int,
    starts_inside: bool,
) -> str:
    """
    Classify one inside interval by how it connects to outside parts of the leg.
    """
    entered_from_outside = not (starts_inside and interval_start == 0)
    leaves_to_outside = interval_end < route_len

    if entered_from_outside and leaves_to_outside:
        return "pass_through"
    if entered_from_outside:
        return "entering"
    if leaves_to_outside:
        return "leaving"
    return "leaving"  # Boundary-edge fallback


def split_inside_segments(df_legs_cut: pd.DataFrame) -> pd.DataFrame:
    rows = []
    drop_cols = {"leg_trav_time", "route_trav_time", "distance"}

    for _, leg in df_legs_cut.iterrows():
        route = str(leg.get("route_links") or "").split()
        cuts = str(leg.get("cut_links_hit") or "").split()
        if not route or not cuts:
            continue

        starts_inside = bool(leg.get("starts_inside", False))
        cut_positions = _cut_positions_in_route(route, cuts)
        if not cut_positions:
            continue

        segs = _inside_intervals(len(route), cut_positions, starts_inside)

        base = {k: v for k, v in leg.to_dict().items() if k not in drop_cols}

        for seg_no, (a, b) in enumerate(segs, 1):
            part = route[a:b]
            if not part:
                continue

            segment_crossing_type = _classify_inside_segment(
                interval_start=a,
                interval_end=b,
                route_len=len(route),
                starts_inside=starts_inside,
            )

            rows.append(
                {
                    **base,
                    "segment_no": seg_no,
                    "crossing_type": segment_crossing_type,
                    "route_links": " ".join(part),
                    "dep_time": leg["dep_time"] if (seg_no == 1 and starts_inside) else np.nan,
                    "start_link": part[0],
                    "end_link": part[-1],
                }
            )

    return pd.DataFrame.from_records(rows)


def write_segments_csv(df: pd.DataFrame, out_path: Path | str) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, sep=";", index=False, compression="infer")


def run_crossing_segments_stage(state: PipelineState) -> None:
    """
    Build inside-boundary segments for trips crossing the boundary.
    """
    if state.car_legs is None or state.cut_link_ids is None:
        raise ValueError("car_legs and cut_link_ids must be available before crossing stage.")
    if state.network_geo is None or state.boundary is None:
        raise ValueError("network_geo and boundary must be available before crossing stage.")

    state.car_legs_cut = filter_legs_touching_cut_links(state.car_legs, state.cut_link_ids)
    state.car_legs_cut = annotate_leg_entry_side(state.car_legs_cut, state.network_geo, state.boundary)
    state.crossing_segments = split_inside_segments(state.car_legs_cut)

    write_segments_csv(state.car_legs_cut, state.car_legs_cut_path)
    write_segments_csv(state.crossing_segments, state.crossing_segments_path)
