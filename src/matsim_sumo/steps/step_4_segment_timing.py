import subprocess
from pathlib import Path

import pandas as pd
from shapely.geometry import LineString, Point

from matsim_sumo.pipeline_state import PipelineState


def sec_to_hhmmss(seconds) -> str | float:
    if pd.isna(seconds):
        return float("nan")

    total = int(round(float(seconds)))
    if total < 0:
        total = 0

    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def run_event_extraction_script(state: PipelineState) -> None:
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


def _extract_points(geom) -> list[Point]:
    if geom.is_empty:
        return []

    gtype = geom.geom_type
    if gtype == "Point":
        return [geom]
    if gtype == "MultiPoint":
        return list(geom.geoms)
    if gtype in {"LineString", "LinearRing"}:
        coords = list(geom.coords)
        if not coords:
            return []
        return [Point(coords[0]), Point(coords[-1])]
    if gtype == "MultiLineString":
        points = []
        for line in geom.geoms:
            coords = list(line.coords)
            if coords:
                points.extend([Point(coords[0]), Point(coords[-1])])
        return points
    if gtype == "GeometryCollection":
        points = []
        for part in geom.geoms:
            points.extend(_extract_points(part))
        return points

    return []


def _first_crossing_fraction(line: LineString, boundary) -> float:
    inter = line.intersection(boundary.boundary)
    points = _extract_points(inter)
    if not points:
        return 0.5

    fracs = []
    for pt in points:
        try:
            fracs.append(float(line.project(pt, normalized=True)))
        except Exception:
            continue

    if not fracs:
        return 0.5

    fracs = sorted(max(0.0, min(1.0, f)) for f in fracs)
    interior = [f for f in fracs if 1e-9 < f < 1 - 1e-9]
    if interior:
        return interior[0]

    non_zero = [f for f in fracs if f > 1e-9]
    if non_zero:
        return non_zero[0]

    return fracs[0]


def build_cut_link_info(state: PipelineState) -> pd.DataFrame:
    if state.network_geo is None or state.cut_link_ids is None:
        raise ValueError("network_geo and cut_link_ids must be available before building cut_link_info.")
    if state.boundary is None:
        raise ValueError("boundary must be available before building cut_link_info.")

    cut_set = set(state.cut_link_ids.astype(str))
    cut_geo = state.network_geo[state.network_geo["link_id"].astype(str).isin(cut_set)].copy()

    rows = []
    for _, row in cut_geo.iterrows():
        link_id = str(row["link_id"])
        geom = row["geometry"]
        frac = _first_crossing_fraction(geom, state.boundary)
        rows.append({"link_id": link_id, "frac": frac})

    return pd.DataFrame.from_records(rows)


def read_cut_events(events_cut_csv_path: Path | str) -> pd.DataFrame:
    ev = pd.read_csv(events_cut_csv_path)
    if ev.empty:
        return ev

    ev = ev.copy()
    ev["vehicleRefId"] = ev["vehicle"].astype(str)
    ev["link_id"] = ev["link_id"].astype(str)
    ev["time"] = pd.to_numeric(ev["time"], errors="coerce")
    ev = ev.dropna(subset=["time"])
    return ev


def build_crossing_event_times(events_df: pd.DataFrame, cut_link_info: pd.DataFrame) -> pd.DataFrame:
    entered = events_df.loc[events_df["type"].eq("entered link"), ["vehicleRefId", "link_id", "time"]].copy()
    entered = entered.sort_values(["vehicleRefId", "link_id", "time"]).reset_index(drop=True)
    entered["occ"] = entered.groupby(["vehicleRefId", "link_id"]).cumcount()
    entered = entered.rename(columns={"time": "t_enter"})

    lefted = events_df.loc[events_df["type"].eq("left link"), ["vehicleRefId", "link_id", "time"]].copy()
    lefted = lefted.sort_values(["vehicleRefId", "link_id", "time"]).reset_index(drop=True)
    lefted["occ"] = lefted.groupby(["vehicleRefId", "link_id"]).cumcount()
    lefted = lefted.rename(columns={"time": "t_left"})

    cross = entered.merge(lefted, on=["vehicleRefId", "link_id", "occ"], how="left")
    cross = cross.merge(cut_link_info[["link_id", "frac"]], on="link_id", how="left")
    cross["frac"] = cross["frac"].fillna(0.5).clip(lower=0.0, upper=1.0)

    cross["dep_time_fill"] = cross["t_enter"] + cross["frac"] * (cross["t_left"] - cross["t_enter"])
    cross["dep_time_fill"] = cross["dep_time_fill"].where(cross["t_left"].notna(), cross["t_enter"])
    return cross


def fill_segment_dep_time(df_segments: pd.DataFrame, cross_times: pd.DataFrame) -> pd.DataFrame:
    out = df_segments.copy()
    miss = out["dep_time"].isna()
    if not miss.any():
        return out

    fill = out.loc[miss, ["vehicleRefId", "start_link"]].copy()
    fill["vehicleRefId"] = fill["vehicleRefId"].astype(str)
    fill["start_link"] = fill["start_link"].astype(str)
    fill["occ"] = fill.groupby(["vehicleRefId", "start_link"]).cumcount()

    fill = fill.merge(
        cross_times[["vehicleRefId", "link_id", "occ", "dep_time_fill"]],
        left_on=["vehicleRefId", "start_link", "occ"],
        right_on=["vehicleRefId", "link_id", "occ"],
        how="left",
    )

    out.loc[miss, "dep_time"] = fill["dep_time_fill"].map(sec_to_hhmmss).to_numpy()
    return out


def write_segments_csv(df: pd.DataFrame, out_path: Path | str) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, sep=";", index=False, compression="infer")


def run_segment_timing_stage(state: PipelineState) -> None:
    if state.crossing_segments is None:
        raise ValueError("crossing_segments must be available before segment timing stage.")

    run_event_extraction_script(state)
    state.cut_link_info = build_cut_link_info(state)
    state.events_cut = read_cut_events(state.events_cut_csv_path)
    state.crossing_events = build_crossing_event_times(state.events_cut, state.cut_link_info)
    state.crossing_segments_timed = fill_segment_dep_time(state.crossing_segments, state.crossing_events)

    write_segments_csv(state.crossing_segments_timed, state.crossing_segments_timed_path)
