import numpy as np
import pandas as pd
from shapely.geometry import Point

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.pipeline_state import PipelineState


# Return cut links in route order without duplicates
def _ordered_unique_hits(route_links: str, cut_set: set[str]) -> list[str]:
    hits = []
    for link_id in route_links.split():
        if link_id in cut_set:
            hits.append(link_id)
    return hits


# Keep only legs whose route touches at least one cut link
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


# Add starts_inside by checking route start side against boundary
def annotate_leg_entry_side(
    df_legs_cut: pd.DataFrame,
    network_geo: pd.DataFrame,
    boundary,
    cut_link_ids: pd.Series,
) -> pd.DataFrame:
    out = df_legs_cut.copy()
    tokens = out["route_links"].fillna("").astype(str).str.split()
    first_links = tokens.str[0].fillna("")
    second_links = tokens.str[1].fillna("")
    needed = set(first_links[first_links.ne("")]).union(set(second_links[second_links.ne("")]))

    geo = network_geo.loc[network_geo["link_id"].astype(str).isin(needed), ["link_id", "geometry"]]
    start_node_inside = {
        str(link_id): boundary.covers(Point(geom.coords[0]))
        for link_id, geom in zip(geo["link_id"], geo["geometry"])
    }
    link_inside = pd.Series(geo["geometry"].within(boundary).to_numpy(), index=geo["link_id"].astype(str))
    cut_set = set(cut_link_ids.astype(str))

    starts_inside = first_links.map(start_node_inside).fillna(False)

    # Added fix: if first link is cut link and second link is inside, force start side to outside
    mask = first_links.isin(cut_set) & second_links.ne("")
    second_inside = second_links.map(link_inside)
    starts_inside.loc[mask & second_inside.eq(True)] = False

    out["starts_inside"] = starts_inside.astype(bool)
    return out


# Find where each cut link appears in the route
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


# Build the inside route intervals between crossings
def _inside_intervals(route_len: int, cut_positions: list[int], starts_inside: bool) -> list[tuple[int, int]]:
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


# Label segment as entering, leaving, or pass_through
def _classify_inside_segment(
    interval_start: int,
    interval_end: int,
    route_len: int,
    starts_inside: bool,
) -> str:
    entered_from_outside = not (starts_inside and interval_start == 0)
    leaves_to_outside = interval_end < route_len

    if entered_from_outside and leaves_to_outside:
        return "pass_through"
    if entered_from_outside:
        return "entering"
    if leaves_to_outside:
        return "leaving"
    return "leaving"  # Boundary-edge fallback


# Split each crossing leg into one or more inside segments
def split_inside_segments(df_legs_cut: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for leg in df_legs_cut.itertuples(index=False):
        route = str(getattr(leg, "route_links", "") or "").split()
        cuts = str(getattr(leg, "cut_links_hit", "") or "").split()
        if not route or not cuts:
            continue

        starts_inside = bool(getattr(leg, "starts_inside", False))
        cut_positions = _cut_positions_in_route(route, cuts)
        if not cut_positions:
            continue

        segs = _inside_intervals(len(route), cut_positions, starts_inside)
        base = leg._asdict()

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
                    "dep_time": getattr(leg, "dep_time") if (seg_no == 1 and starts_inside) else np.nan,
                    "start_link": part[0],
                    "end_link": part[-1],
                }
            )

    return pd.DataFrame.from_records(rows)
def run_crossing_segments_stage(state: PipelineState) -> None:  
    if state.car_legs is None or state.cut_link_ids is None:
        raise ValueError("car_legs and cut_link_ids must be available before crossing stage.")
    if state.network_geo is None or state.boundary is None:
        raise ValueError("network_geo and boundary must be available before crossing stage.")

    state.car_legs_cut = filter_legs_touching_cut_links(state.car_legs, state.cut_link_ids)
    state.car_legs_cut = annotate_leg_entry_side(
        state.car_legs_cut,
        state.network_geo,
        state.boundary,
        state.cut_link_ids,
    )
    state.crossing_segments = split_inside_segments(state.car_legs_cut)

    if state.config.get("write_intermediates", False):
        CsvWriter.write_df(state.crossing_segments, state.crossing_segments_path)
