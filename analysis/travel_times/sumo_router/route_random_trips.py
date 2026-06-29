import argparse
import gzip
import re
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree.ElementTree import ParseError, iterparse, tostring

import pandas as pd
import sumolib
from shapely.geometry import LineString, Point


SUMO_SCHEDULED_STATE_MARKER = "9223372036854774807"


def _nearest_edge(net, lon, lat, vclass="passenger", radius_m=1000):
    x, y = net.convertLonLat2XY(float(lon), float(lat))
    for radius in (50, 100, 250, 500, radius_m, radius_m * 2):
        candidates = [(edge, dist) for edge, dist in net.getNeighboringEdges(x, y, radius) if edge.allows(vclass)]
        if candidates:
            return min(candidates, key=lambda item: item[1])[0].getID()
    return None


def _edge_position(net, edge_id, lon, lat):
    edge = net.getEdge(str(edge_id))
    x, y = net.convertLonLat2XY(float(lon), float(lat))
    line = LineString(edge.getShape())
    if line.length <= 0:
        return 0.0
    pos = float(line.project(Point(x, y)))
    length = float(edge.getLength())
    if length > 0 and line.length > 0:
        pos *= length / line.length
    return min(max(pos, 0.0), length)


def _needs_positions(df):
    return (
        "from_pos" not in df.columns
        or "to_pos" not in df.columns
        or df[["from_pos", "to_pos"]].isna().any().any()
    )


def _shortest_path_length(net, from_edge, to_edge, vclass="passenger"):
    try:
        path, length = net.getShortestPath(net.getEdge(str(from_edge)), net.getEdge(str(to_edge)), vClass=vclass)
    except Exception:
        return None
    if not path:
        return None
    return float(length)


def prepare_trips(trips_csv, network_path, vclass="passenger", check_paths=True):
    df = pd.read_csv(trips_csv)
    net = sumolib.net.readNet(str(network_path), withInternal=False)
    edge_ids = {edge.getID() for edge in net.getEdges(withInternal=False) if edge.allows(vclass)}

    from_edges, to_edges, from_pos, to_pos, has_path, path_lengths, failed = [], [], [], [], [], [], 0
    has_edges = {"from_edge", "to_edge"}.issubset(df.columns)
    has_positions = {"from_pos", "to_pos"}.issubset(df.columns)
    for row in df.itertuples(index=False):
        path_length = None
        if has_edges:
            from_edge = str(getattr(row, "from_edge", "") or "")
            to_edge = str(getattr(row, "to_edge", "") or "")
            from_edge = from_edge if from_edge in edge_ids else None
            to_edge = to_edge if to_edge in edge_ids else None
        else:
            from_edge = to_edge = None
        if from_edge is None:
            from_edge = _nearest_edge(net, row.from_lon, row.from_lat, vclass)
        if to_edge is None:
            to_edge = _nearest_edge(net, row.to_lon, row.to_lat, vclass)
        if not from_edge or not to_edge:
            failed += 1
            fpos = tpos = None
        else:
            fpos = getattr(row, "from_pos", None) if has_positions else None
            tpos = getattr(row, "to_pos", None) if has_positions else None
            if pd.isna(fpos):
                fpos = _edge_position(net, from_edge, row.from_lon, row.from_lat)
            if pd.isna(tpos):
                tpos = _edge_position(net, to_edge, row.to_lon, row.to_lat)
            if check_paths:
                path_length = _shortest_path_length(net, from_edge, to_edge, vclass)
        from_edges.append(from_edge)
        to_edges.append(to_edge)
        from_pos.append(fpos)
        to_pos.append(tpos)
        has_path.append((path_length is not None if check_paths else True) if from_edge and to_edge else False)
        path_lengths.append(path_length if from_edge and to_edge else None)

    out = df.copy()
    out["from_edge"] = from_edges
    out["to_edge"] = to_edges
    out["from_pos"] = from_pos
    out["to_pos"] = to_pos
    out["sumo_has_path"] = has_path
    out["sumo_shortest_path_m"] = path_lengths
    if _needs_positions(df):
        out.to_csv(trips_csv, index=False)
    out = out.dropna(subset=["trip_id", "depart_time", "from_edge", "to_edge", "from_pos", "to_pos"]).copy()
    out = out[out["sumo_has_path"]].copy()
    out["depart_time"] = pd.to_numeric(out["depart_time"], errors="coerce")
    out = out.dropna(subset=["depart_time"]).sort_values("depart_time")
    return out, failed, len(df)


def write_trips_xml(trips, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        for row in trips.itertuples(index=False):
            f.write(
                f'    <trip id="{escape(str(row.trip_id))}" depart="{float(row.depart_time):.0f}" '
                f'from="{escape(str(row.from_edge))}" to="{escape(str(row.to_edge))}" '
                f'departPos="{float(row.from_pos):.2f}" arrivalPos="{float(row.to_pos):.2f}"/>\n'
            )
        f.write("</routes>\n")
    return output_path


def _resolve_binary(binary):
    exe = shutil.which(binary) if not Path(binary).exists() else str(Path(binary))
    if exe is None:
        raise FileNotFoundError(f"SUMO binary not found: {binary}")
    return exe


def run_sumo(
    network_path,
    route_files,
    tripinfo_path,
    summary_path,
    log_path,
    begin_s,
    end_s,
    sumo_bin="sumo",
    random_depart_offset_s=300,
    tls_all_off=False,
):
    exe = _resolve_binary(sumo_bin)
    cmd = [
        exe,
        "--net-file", str(network_path),
        "--route-files", ",".join(map(str, route_files)),
        "--begin", str(int(begin_s)),
        "--end", str(int(end_s)),
        "--step-length", "1",
        "--random-depart-offset", str(int(random_depart_offset_s)),
        "--ignore-route-errors", "true",
        "--ignore-junction-blocker", "10",
        "--time-to-impatience", "120",
        "--scale", "1",
        "--time-to-teleport.disconnected", "0",
        "--time-to-teleport", "120",
        "--default.departlane", "best",
        "--default.departspeed", "0",
        "--extrapolate-departpos", "true",
        "--routing-algorithm", "astar",
        "--weights.random-factor", "1.3",
        "--weights.priority-factor", "1.2",
        "--device.rerouting.probability", "1",
        "--device.rerouting.period", "180",
        "--device.rerouting.pre-period", "300",
        "--device.rerouting.adaptation-steps", "180",
        "--device.rerouting.adaptation-interval", "60",
        "--device.rerouting.with-taz", "false",
        "--device.rerouting.synchronize", "false",
        "--device.rerouting.threshold.factor", "1.25",
        "--device.rerouting.threshold.constant", "180",
        "--seed", "18",
        "--xml-validation", "never",
        "--no-step-log", "true",
        "--no-warnings", "true",
        "--error-log", str(log_path),
        "--tripinfo-output", str(tripinfo_path),
        "--summary-output", str(summary_path),
    ]
    if tls_all_off:
        cmd.extend(["--tls.all-off", "true"])
    subprocess.run(cmd, cwd=Path(network_path).resolve().parent, check=True)


def run_sumo_loaded_state(
    network_path,
    route_files,
    state_path,
    tripinfo_path,
    summary_path,
    log_path,
    begin_s,
    end_s,
    sumo_bin="sumo",
    thread_rngs=64,
    threads=8,
    tls_all_off=False,
):
    exe = _resolve_binary(sumo_bin)
    cmd = [
        exe,
        "--net-file", str(network_path),
        "--route-files", ",".join(map(str, route_files)),
        "--load-state", str(state_path),
        "--begin", str(int(begin_s)),
        "--end", str(int(end_s)),
        "--step-length", "1",
        "--thread-rngs", str(int(thread_rngs)),
        "--random-depart-offset", "0",
        "--ignore-route-errors", "true",
        "--ignore-junction-blocker", "10",
        "--time-to-impatience", "60",
        "--scale", "1",
        "--time-to-teleport.disconnected", "0",
        "--time-to-teleport", "300",
        "--default.departlane", "best",
        "--default.departspeed", "max",
        "--extrapolate-departpos", "true",
        "--routing-algorithm", "astar",
        "--weights.random-factor", "1.8",
        "--weights.priority-factor", "0.5",
        "--device.rerouting.probability", "1",
        "--device.rerouting.period", "60",
        "--device.rerouting.pre-period", "300",
        "--device.rerouting.adaptation-steps", "180",
        "--device.rerouting.adaptation-interval", "30",
        "--device.rerouting.with-taz", "false",
        "--device.rerouting.synchronize", "false",
        "--device.rerouting.threshold.factor", "1.05",
        "--device.rerouting.threshold.constant", "30",
        "--seed", "18",
        "--xml-validation", "never",
        "--no-step-log", "true",
        "--no-warnings", "true",
        "--error-log", str(log_path),
        "--tripinfo-output", str(tripinfo_path),
        "--summary-output", str(summary_path),
    ]
    if threads:
        cmd.extend(["--threads", str(int(threads)), "--device.rerouting.threads", str(int(threads))])
    if tls_all_off:
        cmd.extend(["--tls.all-off", "true"])
    subprocess.run(cmd, cwd=Path(network_path).resolve().parent, check=True)


def parse_tripinfo(tripinfo_path, identifiers):
    wanted = set(map(str, identifiers))
    rows = []
    try:
        for _, elem in iterparse(tripinfo_path, events=("end",)):
            if elem.tag == "tripinfo" and elem.get("id") in wanted:
                rows.append({
                    "trip_id": elem.get("id"),
                    "depart_time": float(elem.get("depart", 0.0)),
                    "arrival_time": float(elem.get("arrival", 0.0)),
                    "travel_time_seconds": float(elem.get("duration", 0.0)),
                    "route_length_m": float(elem.get("routeLength", 0.0)),
                    "depart_delay_seconds": float(elem.get("departDelay", 0.0)),
                    "time_loss_seconds": float(elem.get("timeLoss", 0.0)),
                    "waiting_time_seconds": float(elem.get("waitingTime", 0.0)),
                    "vaporized": elem.get("vaporized", ""),
                })
            elem.clear()
    except ParseError as exc:
        raise RuntimeError(f"Could not parse SUMO tripinfo output {tripinfo_path}: {exc}") from exc
    return pd.DataFrame(rows)


def parse_teleported_ids(log_path, identifiers=None):
    wanted = set(map(str, identifiers)) if identifiers is not None else None
    teleported = set()
    if not Path(log_path).exists():
        return teleported
    with open(log_path, errors="ignore") as f:
        for line in f:
            match = re.search(r"Teleporting vehicle '([^']+)'", line)
            if not match:
                continue
            vehicle_id = match.group(1)
            if wanted is None or vehicle_id in wanted:
                teleported.add(vehicle_id)
    return teleported


def _hourly_background_routes(sumo_output_dir, hour):
    path = Path(sumo_output_dir) / "state" / "hourly" / f"{int(hour * 3600):06d}" / "routes_VMOD.xml"
    if not path.exists():
        raise FileNotFoundError(f"Missing hourly SUMO background routes for hour {hour}: {path}")
    return path


def _hourly_state_file(sumo_output_dir, hour):
    begin_s = int(hour * 3600)
    state_root = Path(sumo_output_dir) / "state" / "hourly"
    path = state_root / f"{max(0, begin_s - 3600):06d}" / f"hourly_{begin_s:.2f}.xml.gz"
    if path.exists():
        return path
    matches = sorted(state_root.glob(f"*/hourly_{begin_s:.2f}.xml.gz"))
    if matches:
        return matches[0]
    raise FileNotFoundError(f"Missing SUMO state file for hour {hour}: {path}")


def _first_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return default


def _state_route_index(route_edges, current_edge, state_attr):
    candidates = [i for i, edge in enumerate(route_edges) if edge == current_edge]
    if not candidates:
        return None
    parts = str(state_attr or "").split()
    try:
        hint = int(parts[2])
    except (ValueError, IndexError):
        return candidates[0]
    return min(candidates, key=lambda i: min(abs(i - hint), abs(i - (hint - 1))))


def _is_future_state_vehicle(vehicle_elem):
    parts = str(vehicle_elem.get("state", "")).split()
    return len(parts) > 1 and parts[1] == SUMO_SCHEDULED_STATE_MARKER


def _reconstruct_active_state_vehicles(state_file, net):
    routes = {}
    vehicles = []
    skipped = 0
    with gzip.open(state_file, "rb") as f:
        for _, elem in iterparse(f, events=("end",)):
            if elem.tag == "route" and elem.get("id"):
                routes[elem.get("id")] = elem.get("edges", "").split()
                elem.clear()
                continue
            if elem.tag != "vehicle":
                continue
            if _is_future_state_vehicle(elem):
                elem.clear()
                continue

            vehroute = next(
                (child for child in elem if child.tag == "device" and child.get("id", "").startswith("vehroute_")),
                None,
            )
            current_edge = vehroute.get("edge") if vehroute is not None else None
            route_edges = routes.get(elem.get("route"), [])
            route_index = _state_route_index(route_edges, current_edge, elem.get("state"))
            if route_index is None or not current_edge or current_edge.startswith(":"):
                skipped += 1
                elem.clear()
                continue

            try:
                edge_length = float(net.getEdge(current_edge).getLength())
            except Exception:
                skipped += 1
                elem.clear()
                continue

            depart_pos = min(max(_first_float(elem.get("pos"), 0.0), 0.0), edge_length)
            vehicles.append({
                "id": elem.get("id"),
                "edges": route_edges[route_index:],
                "depart_pos": depart_pos,
                "speed_factor": elem.get("speedFactor"),
            })
            elem.clear()
    return vehicles, skipped


def _write_reconstructed_hourly_background(
    hourly_routes,
    state_file,
    output_path,
    begin_s,
    depart_end_s,
    net,
):
    active_vehicles, skipped_active = _reconstruct_active_state_vehicles(state_file, net)
    scheduled = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        f.write('    <vType id="DEFAULT_VEHTYPE"/>\n')
        for vehicle in active_vehicles:
            attrs = [
                f'id="state_{escape(str(vehicle["id"]))}"',
                f'depart="{begin_s:.2f}"',
                f'departPos="{vehicle["depart_pos"]:.2f}"',
            ]
            if vehicle["speed_factor"]:
                attrs.append(f'speedFactor="{escape(str(vehicle["speed_factor"]))}"')
            f.write(f'    <vehicle {" ".join(attrs)}>\n')
            f.write(f'        <route edges="{escape(" ".join(vehicle["edges"]))}"/>\n')
            f.write("    </vehicle>\n")

        for _, elem in iterparse(hourly_routes, events=("end",)):
            if elem.tag != "vehicle":
                continue
            depart = _first_float(elem.get("depart"), None)
            if depart is not None and begin_s <= depart < depart_end_s:
                scheduled.append((depart, tostring(elem, encoding="unicode").strip()))
            elem.clear()
        for _, vehicle_xml in sorted(scheduled, key=lambda item: item[0]):
            f.write("    ")
            f.write(vehicle_xml)
            f.write("\n")
        f.write("</routes>\n")
    return {
        "active_reconstructed": len(active_vehicles),
        "active_skipped": skipped_active,
        "scheduled_departures": len(scheduled),
    }


def _hourly_route_files_for_range(sumo_output_dir, begin_s, end_s):
    start_hour = int(begin_s // 3600)
    end_hour = int((max(begin_s, end_s - 1)) // 3600)
    return [_hourly_background_routes(sumo_output_dir, hour) for hour in range(start_hour, end_hour + 1)]


def _write_scheduled_hourly_background(route_files, output_path, begin_s, end_s):
    scheduled = []
    seen = set()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for routes_file in route_files:
        for _, elem in iterparse(routes_file, events=("end",)):
            if elem.tag != "vehicle":
                continue
            vehicle_id = elem.get("id")
            depart = _first_float(elem.get("depart"), None)
            if (
                vehicle_id
                and vehicle_id not in seen
                and depart is not None
                and begin_s <= depart < end_s
            ):
                scheduled.append((depart, vehicle_id, tostring(elem, encoding="unicode").strip()))
                seen.add(vehicle_id)
            elem.clear()

    with open(output_path, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        for _, _, vehicle_xml in sorted(scheduled, key=lambda item: (item[0], item[1])):
            f.write("    ")
            f.write(vehicle_xml)
            f.write("\n")
        f.write("</routes>\n")
    return {
        "scheduled_departures": len(scheduled),
        "source_route_files": len(route_files),
    }


def _write_background_counts(path, counts):
    with open(path, "w") as f:
        for key, value in counts.items():
            f.write(f"{key}: {value}\n")


def _route_with_hourly_background(
    network,
    sumo_output_dir,
    prepared,
    work_dir,
    sumo_bin,
    simulation_end_s,
    random_depart_offset_s,
    completion_buffer_s,
    reconstruct_state=False,
    vclass="passenger",
    tls_all_off=False,
):
    hourly_dir = work_dir / "hourly"
    shutil.rmtree(hourly_dir, ignore_errors=True)
    hourly_dir.mkdir(parents=True, exist_ok=True)
    net = sumolib.net.readNet(str(network), withInternal=False) if reconstruct_state else None

    frames = []
    for hour, hourly_trips in prepared.groupby((prepared["depart_time"] // 3600).astype(int)):
        begin_s = int(hour * 3600)
        depart_end_s = begin_s + 3600
        end_s = int(min(simulation_end_s or 86400, depart_end_s + completion_buffer_s))
        hour_dir = hourly_dir / f"{hour:02d}"
        hour_dir.mkdir(parents=True, exist_ok=True)
        trips_xml = write_trips_xml(hourly_trips, hour_dir / "trips.xml")
        tripinfo_path = hour_dir / "tripinfo.xml"
        summary_path = hour_dir / "summary.xml"
        log_path = hour_dir / "sumo_simulation.log"
        background_routes = _hourly_background_routes(sumo_output_dir, hour)
        if reconstruct_state:
            state_file = _hourly_state_file(sumo_output_dir, hour)
            background_routes = hour_dir / "background_reconstructed.xml"
            counts = _write_reconstructed_hourly_background(
                _hourly_background_routes(sumo_output_dir, hour),
                state_file,
                background_routes,
                begin_s,
                depart_end_s,
                net,
            )
            _write_background_counts(hour_dir / "background_counts.txt", counts)
        run_sumo(
            network,
            [background_routes, trips_xml],
            tripinfo_path,
            summary_path,
            log_path,
            begin_s,
            end_s,
            sumo_bin,
            random_depart_offset_s,
            tls_all_off,
        )
        results = parse_tripinfo(tripinfo_path, hourly_trips["trip_id"])
        if not results.empty:
            teleported_ids = parse_teleported_ids(log_path, hourly_trips["trip_id"])
            results["teleported"] = results["trip_id"].isin(teleported_ids)
            results["sumo_hour"] = hour
            if reconstruct_state:
                results["sumo_background_mode"] = "hourly_state_reconstructed"
            frames.append(results)

    if frames:
        return pd.concat(frames, ignore_index=True)
    return pd.DataFrame(columns=[
        "trip_id",
        "depart_time",
        "arrival_time",
        "travel_time_seconds",
        "route_length_m",
        "depart_delay_seconds",
        "time_loss_seconds",
        "waiting_time_seconds",
        "vaporized",
        "teleported",
        "sumo_hour",
    ])


def _route_with_loaded_state_background(
    network,
    sumo_output_dir,
    prepared,
    work_dir,
    sumo_bin,
    simulation_end_s,
    completion_buffer_s,
    thread_rngs=64,
    threads=8,
    tls_all_off=False,
):
    hourly_dir = work_dir / "hourly"
    shutil.rmtree(hourly_dir, ignore_errors=True)
    hourly_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for hour, hourly_trips in prepared.groupby((prepared["depart_time"] // 3600).astype(int)):
        begin_s = int(hour * 3600)
        depart_end_s = begin_s + 3600
        end_s = int(min(simulation_end_s or 86400, depart_end_s + completion_buffer_s))
        hour_dir = hourly_dir / f"{hour:02d}"
        hour_dir.mkdir(parents=True, exist_ok=True)

        state_file = _hourly_state_file(sumo_output_dir, hour)
        route_files = _hourly_route_files_for_range(sumo_output_dir, begin_s, end_s)
        background_xml = hour_dir / "background_scheduled_from_state_run.xml"
        counts = _write_scheduled_hourly_background(route_files, background_xml, begin_s, end_s)
        _write_background_counts(hour_dir / "background_counts.txt", counts)

        trips_xml = write_trips_xml(hourly_trips, hour_dir / "trips.xml")
        tripinfo_path = hour_dir / "tripinfo.xml"
        summary_path = hour_dir / "summary.xml"
        log_path = hour_dir / "sumo_simulation.log"
        run_sumo_loaded_state(
            network,
            [background_xml, trips_xml],
            state_file,
            tripinfo_path,
            summary_path,
            log_path,
            begin_s,
            end_s,
            sumo_bin,
            thread_rngs,
            threads,
            tls_all_off,
        )
        results = parse_tripinfo(tripinfo_path, hourly_trips["trip_id"])
        if not results.empty:
            teleported_ids = parse_teleported_ids(log_path, hourly_trips["trip_id"])
            results["teleported"] = results["trip_id"].isin(teleported_ids)
            results["sumo_hour"] = hour
            results["sumo_background_mode"] = "hourly_load_state"
            frames.append(results)

    if frames:
        return pd.concat(frames, ignore_index=True)
    return pd.DataFrame(columns=[
        "trip_id",
        "depart_time",
        "arrival_time",
        "travel_time_seconds",
        "route_length_m",
        "depart_delay_seconds",
        "time_loss_seconds",
        "waiting_time_seconds",
        "vaporized",
        "teleported",
        "sumo_hour",
        "sumo_background_mode",
    ])


def route_random_trips(
    network,
    sumo_output_dir,
    trips_csv,
    work_dir,
    sumo_bin="sumo",
    vclass="passenger",
    write_trips_only=False,
    background_routes=None,
    simulation_end_s=None,
    simulation_begin_s=0,
    include_background_routes=True,
    background_mode=None,
    random_depart_offset_s=300,
    completion_buffer_s=1800,
    load_state_thread_rngs=64,
    load_state_threads=8,
    tls_all_off=False,
    check_paths=True,
):
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    for legacy in ["routes.rou.xml", "routes.rou.alt.xml"]:
        (work_dir / legacy).unlink(missing_ok=True)
    shutil.rmtree(work_dir / "weights", ignore_errors=True)

    prepared, failed_snap, input_count = prepare_trips(trips_csv, network, vclass, check_paths)
    trips_xml = write_trips_xml(prepared, work_dir / "trips.xml")
    if write_trips_only:
        print(f"Wrote {len(prepared)} sorted trips to {trips_xml}; failed to snap: {failed_snap}")
        return None

    if background_mode is None:
        background_mode = "full_routes" if include_background_routes else "none"
    if background_mode not in {"none", "full_routes", "hourly_routes", "hourly_state_reconstructed", "hourly_load_state"}:
        raise ValueError(f"Unsupported SUMO background mode: {background_mode}")
    if background_mode == "full_routes" and background_routes is None:
        raise ValueError("background_routes is required for simulated SUMO travel times.")
    route_files = [trips_xml]
    if background_mode == "full_routes":
        route_files.insert(0, background_routes)
    begin_s = int(simulation_begin_s or 0)
    end_s = int(simulation_end_s or max(86400, prepared["depart_time"].max() + 3600))
    tripinfo_path = work_dir / "tripinfo.xml"
    summary_path = work_dir / "summary.xml"
    log_path = work_dir / "sumo_simulation.log"
    out_csv = work_dir / "sumo_travel_times.csv"
    for path in [tripinfo_path, summary_path, log_path, out_csv]:
        path.unlink(missing_ok=True)
    if background_mode == "hourly_load_state":
        results = _route_with_loaded_state_background(
            network,
            sumo_output_dir,
            prepared,
            work_dir,
            sumo_bin,
            end_s,
            completion_buffer_s,
            load_state_thread_rngs,
            load_state_threads,
            tls_all_off,
        )
    elif background_mode in {"hourly_routes", "hourly_state_reconstructed"}:
        results = _route_with_hourly_background(
            network,
            sumo_output_dir,
            prepared,
            work_dir,
            sumo_bin,
            end_s,
            random_depart_offset_s,
            completion_buffer_s,
            background_mode == "hourly_state_reconstructed",
            vclass,
            tls_all_off,
        )
    else:
        run_sumo(
            network,
            route_files,
            tripinfo_path,
            summary_path,
            log_path,
            begin_s,
            end_s,
            sumo_bin,
            random_depart_offset_s,
            tls_all_off,
        )
        results = parse_tripinfo(tripinfo_path, prepared["trip_id"])
        if not results.empty:
            teleported_ids = parse_teleported_ids(log_path, prepared["trip_id"])
            results["teleported"] = results["trip_id"].isin(teleported_ids)
    if "teleported" not in results.columns:
        results["teleported"] = False
    raw_routed = len(results)
    vaporized_mask = results["vaporized"].fillna("").ne("") if not results.empty else pd.Series(dtype=bool)
    teleported_mask = results["teleported"].fillna(False).astype(bool) if not results.empty else pd.Series(dtype=bool)
    invalid_mask = vaporized_mask | teleported_mask
    vaporized = int(vaporized_mask.sum())
    teleported = int(teleported_mask.sum())
    invalid = int(invalid_mask.sum())
    results = results[~invalid_mask].copy()
    results.to_csv(out_csv, index=False)
    routed = len(results)
    missing = len(prepared) - raw_routed
    print(f"Input trips: {input_count}")
    print(f"Failed to snap: {failed_snap}")
    print(f"No SUMO path: {input_count - failed_snap - len(prepared)}")
    print(f"Background mode: {background_mode}")
    print(f"Written to SUMO simulation: {len(prepared)}")
    print(f"Finished without teleport: {routed}")
    print(f"Vaporized tripinfos flagged: {vaporized}")
    print(f"Teleported tripinfos flagged: {teleported}")
    print(f"Invalid tripinfos excluded: {invalid}")
    print(f"Missing from SUMO tripinfo: {missing}")
    print(f"Wrote SUMO travel times: {out_csv}")
    return out_csv


def main():
    parser = argparse.ArgumentParser(description="Simulate random trips in SUMO and parse tripinfo durations.")
    parser.add_argument("--network", required=True)
    parser.add_argument("--sumo-output-dir", required=True)
    parser.add_argument("--trips-csv", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--sumo-bin", default="sumo")
    parser.add_argument("--background-routes", required=True)
    parser.add_argument("--simulation-end-s", type=int)
    parser.add_argument("--simulation-begin-s", type=int, default=0)
    parser.add_argument("--background-mode", choices=["none", "full_routes", "hourly_routes", "hourly_state_reconstructed", "hourly_load_state"])
    parser.add_argument("--random-depart-offset-s", type=int, default=300)
    parser.add_argument("--completion-buffer-s", type=int, default=1800)
    parser.add_argument("--load-state-thread-rngs", type=int, default=64)
    parser.add_argument("--load-state-threads", type=int, default=8)
    parser.add_argument("--tls-all-off", action="store_true")
    parser.add_argument("--skip-path-check", action="store_true")
    parser.add_argument("--without-background-routes", action="store_true")
    parser.add_argument("--vclass", default="passenger")
    parser.add_argument("--write-trips-only", action="store_true")
    args = parser.parse_args()

    route_random_trips(
        args.network,
        args.sumo_output_dir,
        args.trips_csv,
        args.work_dir,
        args.sumo_bin,
        args.vclass,
        args.write_trips_only,
        args.background_routes,
        args.simulation_end_s,
        args.simulation_begin_s,
        not args.without_background_routes,
        args.background_mode,
        args.random_depart_offset_s,
        args.completion_buffer_s,
        args.load_state_thread_rngs,
        args.load_state_threads,
        args.tls_all_off,
        not args.skip_path_check,
    )


if __name__ == "__main__":
    main()
