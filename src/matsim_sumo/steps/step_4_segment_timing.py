import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
from shapely import covers, distance, points
from shapely.geometry import LineString

from matsim_sumo.csv_writer import CsvWriter
from matsim_sumo.disk_cache import disk_cache, file_dep, output_needs_refresh, param_dep
from matsim_sumo.pipeline_state import PipelineState


# Convert seconds to HH:MM:SS format
def sec_to_hhmmss(seconds) -> str | float:
    if pd.isna(seconds):
        return float("nan")

    total = int(round(float(seconds)))
    if total < 0:
        total = 0

    day_seconds = total % 86400
    h = day_seconds // 3600
    m = (day_seconds % 3600) // 60
    s = day_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# Runs shell script to extract cut link events from the full events file
def run_event_extraction_script(state: PipelineState, force: bool = False) -> None:
    out_csv = Path(state.events_cut_csv_path)
    # Skip to save time if already exists
    if out_csv.exists() and not force:
        return

    script = Path(state.extract_events_script_path)
    if not script.exists():
        raise FileNotFoundError(f"Event extraction script not found: {script}")

    cmd = [
        "bash",
        str(script),
        str(state.events_path),
        str(state.events_cut_csv_path),
        str(state.cut_links_csv_path),
    ]
    subprocess.run(cmd, check=True)


# Finds where along a cut link the boundary crossing occurs, as a fraction of the link length
def _first_crossing_fraction(line: LineString, boundary_line) -> float:
    inter = line.intersection(boundary_line)
    if inter.is_empty:
        return 0.5

    if inter.geom_type == "Point":
        points = [inter]
    elif inter.geom_type == "MultiPoint":
        points = list(inter.geoms)
    else:
        return 0.5

    fracs = sorted(max(0.0, min(1.0, float(line.project(pt, normalized=True)))) for pt in points)
    if not fracs:
        return 0.5

    interior = [f for f in fracs if 1e-9 < f < 1 - 1e-9]
    if interior:
        return interior[0]

    non_zero = [f for f in fracs if f > 1e-9]
    if non_zero:
        return non_zero[0]

    return fracs[0]


# Build per-cut-link crossing fractions from network geometry
def build_cut_link_info(state: PipelineState) -> pd.DataFrame:
    cut_set = set(state.cut_link_ids.astype(str))
    link_id = state.network_geo["link_id"].astype(str)
    cut_geo = state.network_geo.loc[link_id.isin(cut_set), ["link_id", "geometry"]]
    boundary_line = state.boundary.boundary
    rows = []
    for row in cut_geo.itertuples(index=False):
        frac = _first_crossing_fraction(row.geometry, boundary_line)
        pt = row.geometry.interpolate(frac, normalized=True)
        rows.append({"link_id": str(row.link_id), "frac": frac, "x_cross": float(pt.x), "y_cross": float(pt.y)})

    return pd.DataFrame.from_records(rows)


# Build start/end node coordinates for each network link.
def build_link_endpoint_info(state: PipelineState) -> pd.DataFrame:
    rows = []
    for row in state.network_geo.loc[:, ["link_id", "geometry"]].itertuples(index=False):
        coords = list(row.geometry.coords)
        if not coords:
            continue
        (sx, sy), (ex, ey) = coords[0], coords[-1]
        rows.append({"link_id": str(row.link_id), "sx": sx, "sy": sy, "ex": ex, "ey": ey})
    return pd.DataFrame.from_records(rows)


# Read extracted cut-link events and normalize key columns
def read_cut_events(events_cut_csv_path: Path | str) -> pd.DataFrame:
    ev = pd.read_csv(events_cut_csv_path)
    if ev.empty:
        return ev

    ev["vehicleRefId"] = ev["vehicle"].astype(str)
    ev["link_id"] = ev["link_id"].astype(str)
    ev["time"] = pd.to_numeric(ev["time"], errors="coerce")
    return ev.dropna(subset=["time"])


# Pair entered/left events and compute estimated boundary-crossing timestamps
def build_crossing_event_times(events_df: pd.DataFrame, cut_link_info: pd.DataFrame) -> pd.DataFrame:
    def _prep(kinds: list[str], time_col: str) -> pd.DataFrame:
        out = events_df.loc[events_df["type"].isin(kinds), ["vehicleRefId", "link_id", "time"]]
        out = out.drop_duplicates(subset=["vehicleRefId", "link_id", "time"])
        out = out.sort_values(["vehicleRefId", "link_id", "time"]).reset_index(drop=True)
        out["occ"] = out.groupby(["vehicleRefId", "link_id"]).cumcount()
        return out.rename(columns={"time": time_col})

    entered = _prep(["entered link", "vehicle enters traffic"], "t_enter")
    lefted = _prep(["left link", "vehicle leaves traffic"], "t_left")

    cross = entered.merge(lefted, on=["vehicleRefId", "link_id", "occ"], how="left")
    cross = cross.merge(cut_link_info[["link_id", "frac"]], on="link_id", how="left")
    cross["frac"] = cross["frac"].fillna(0.5).clip(lower=0.0, upper=1.0)

    cross["dep_time_fill"] = cross["t_enter"] + cross["frac"] * (cross["t_left"] - cross["t_enter"])
    cross["dep_time_fill"] = cross["dep_time_fill"].where(cross["t_left"].notna(), cross["t_enter"])
    return cross


# Fill missing segment departure times from matched crossing events
def fill_segment_dep_time(df_segments: pd.DataFrame, cross_times: pd.DataFrame) -> pd.DataFrame:
    out = df_segments.copy()
    miss = out["dep_time"].isna()
    if not miss.any():
        return out

    fill = out.loc[miss, ["vehicleRefId", "start_link"]].astype(str).copy()
    fill["occ"] = fill.groupby(["vehicleRefId", "start_link"]).cumcount()

    fill = fill.merge(
        cross_times[["vehicleRefId", "link_id", "occ", "dep_time_fill"]],
        left_on=["vehicleRefId", "start_link", "occ"],
        right_on=["vehicleRefId", "link_id", "occ"],
        how="left",
    )

    out.loc[miss, "dep_time"] = fill["dep_time_fill"].map(sec_to_hhmmss).astype("string").to_numpy()
    return out


# Add segment start/end coordinates using cut-point and link endpoint info.
def add_segment_coordinates(
    df_segments: pd.DataFrame,
    link_endpoints: pd.DataFrame,
    cut_link_info: pd.DataFrame,
) -> pd.DataFrame:
    out = df_segments.copy()
    out["start_link"] = out["start_link"].astype(str)
    out["end_link"] = out["end_link"].astype(str)

    ep = link_endpoints.set_index("link_id")
    cut = cut_link_info.set_index("link_id")

    out["start_x"] = out["start_link"].map(ep["sx"])
    out["start_y"] = out["start_link"].map(ep["sy"])
    out["end_x"] = out["end_link"].map(ep["ex"])
    out["end_y"] = out["end_link"].map(ep["ey"])

    use_cut_start = out["crossing_type"].isin(["entering", "pass_through"])
    use_cut_end = out["crossing_type"].isin(["leaving", "pass_through"])

    out.loc[use_cut_start, "start_x"] = out.loc[use_cut_start, "start_link"].map(cut["x_cross"])
    out.loc[use_cut_start, "start_y"] = out.loc[use_cut_start, "start_link"].map(cut["y_cross"])
    out.loc[use_cut_end, "end_x"] = out.loc[use_cut_end, "end_link"].map(cut["x_cross"])
    out.loc[use_cut_end, "end_y"] = out.loc[use_cut_end, "end_link"].map(cut["y_cross"])

    out["start_x"] = out["start_x"].fillna(out["start_link"].map(ep["sx"]))
    out["start_y"] = out["start_y"].fillna(out["start_link"].map(ep["sy"]))
    out["end_x"] = out["end_x"].fillna(out["end_link"].map(ep["ex"]))
    out["end_y"] = out["end_y"].fillna(out["end_link"].map(ep["ey"]))
    return out


def _inside_or_near_boundary(boundary, x: pd.Series, y: pd.Series, tolerance_m: float) -> pd.Series:
    xy = pd.DataFrame({"x": pd.to_numeric(x, errors="coerce"), "y": pd.to_numeric(y, errors="coerce")})
    valid = xy["x"].notna() & xy["y"].notna()
    ok = pd.Series(False, index=x.index)
    if valid.any():
        pts = points(xy.loc[valid, "x"].to_numpy(dtype=float), xy.loc[valid, "y"].to_numpy(dtype=float))
        ok.loc[valid] = np.asarray(covers(boundary, pts) | (distance(pts, boundary) <= tolerance_m), dtype=bool)
    return ok


def drop_segments_outside_boundary(
    df_segments: pd.DataFrame,
    boundary,
    tolerance_m: float = 1.0,
) -> tuple[pd.DataFrame, int]:
    start_ok = _inside_or_near_boundary(boundary, df_segments["start_x"], df_segments["start_y"], tolerance_m)
    end_ok = _inside_or_near_boundary(boundary, df_segments["end_x"], df_segments["end_y"], tolerance_m)
    keep = start_ok & end_ok
    return df_segments.loc[keep].copy(), int((~keep).sum())


def _segment_timing_deps(state: PipelineState):
    return [
        file_dep(state.events_path),
        file_dep(state.extract_events_script_path),
        file_dep(state.cut_links_csv_path),
        file_dep(state.network_path),
        file_dep(state.boundary_path),
        file_dep(state.plans_path),
        param_dep("modes_keep", tuple(state.config["modes_keep"])),
        param_dep("network_crs", state.config.get("network_crs", "EPSG:2056")),
        param_dep("segment_endpoint_boundary_tolerance_m", state.config.get("segment_endpoint_boundary_tolerance_m", 1.0)),
    ]


@disk_cache(depends_on=lambda state: _segment_timing_deps(state), step_name="segment_timing")
def _compute_segment_timing_cached(
    state: PipelineState,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, int]:
    if state.crossing_segments is None:
        raise ValueError("crossing_segments must be available before segment timing stage.")

    run_event_extraction_script(state, force=True)
    cut_link_info = build_cut_link_info(state)
    link_endpoints = build_link_endpoint_info(state)
    events_cut = read_cut_events(state.events_cut_csv_path)
    crossing_events = build_crossing_event_times(events_cut, cut_link_info)
    crossing_segments_timed = fill_segment_dep_time(state.crossing_segments, crossing_events)
    crossing_segments_timed = add_segment_coordinates(
        crossing_segments_timed,
        link_endpoints=link_endpoints,
        cut_link_info=cut_link_info,
    )
    crossing_segments_timed, dropped = drop_segments_outside_boundary(
        crossing_segments_timed,
        state.boundary,
        tolerance_m=float(state.config.get("segment_endpoint_boundary_tolerance_m", 1.0)),
    )
    return cut_link_info, events_cut, crossing_events, crossing_segments_timed, dropped


def _write_events_cut_if_missing(events_cut: pd.DataFrame, out_path: Path | str) -> None:
    out = Path(out_path)
    if out.exists():
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = [col for col in ("time", "type", "link_id", "vehicle") if col in events_cut.columns]
    events_cut.loc[:, cols].to_csv(out, index=False)


def run_segment_timing_stage(state: PipelineState) -> None:
    (
        state.cut_link_info,
        state.events_cut,
        state.crossing_events,
        state.crossing_segments_timed,
        state.crossing_segments_outside_boundary_dropped_count,
    ) = _compute_segment_timing_cached(state, no_cache=bool(state.config.get("no_cache", False)))

    if state.crossing_segments_outside_boundary_dropped_count:
        print(
            "Dropped "
            f"{state.crossing_segments_outside_boundary_dropped_count} crossing segments outside boundary "
            f"(tolerance {float(state.config.get('segment_endpoint_boundary_tolerance_m', 1.0)):.2f} m)."
        )
    _write_events_cut_if_missing(state.events_cut, state.events_cut_csv_path)
    if output_needs_refresh(_compute_segment_timing_cached, state.crossing_segments_timed_path):
        CsvWriter.write_df(state.crossing_segments_timed, state.crossing_segments_timed_path)
