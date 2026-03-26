import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from matsim_sumo.pipeline_state import PipelineState


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _collect_ids(path: Path, tags: set[str]) -> set[str]:
    out: set[str] = set()
    for _, elem in ET.iterparse(path, events=("end",)):
        if _local_name(elem.tag) in tags:
            trip_id = (elem.get("id") or "").strip()
            if trip_id:
                out.add(trip_id)
        elem.clear()
    return out


def _write_unrouted_trips_xml(input_trips_xml: Path, unrouted_trip_ids: set[str], output_xml: Path) -> int:
    output_xml.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(output_xml, "w", encoding="utf-8", newline="\n") as dst:
        dst.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        if unrouted_trip_ids:
            for _, elem in ET.iterparse(input_trips_xml, events=("end",)):
                if _local_name(elem.tag) == "trip":
                    trip_id = (elem.get("id") or "").strip()
                    if trip_id in unrouted_trip_ids:
                        dst.write(f"    {ET.tostring(elem, encoding='unicode').strip()}\n")
                        written += 1
                elem.clear()
        dst.write("</routes>\n")
    return written


def run_duarouter_stage(state: PipelineState) -> None:
    state.duarouter_log_path.parent.mkdir(parents=True, exist_ok=True)
    state.sumo_unrouted_trips_path.parent.mkdir(parents=True, exist_ok=True)
    state.sumo_routes_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(state.config["duarouter_bin"]),
        "-n",
        str(state.sumo_network_path),
        "-r",
        str(state.sumo_trips_path),
        "-o",
        str(state.sumo_routes_path),
        "--ignore-errors",
        "true",
        "--no-step-log",
        "true",
    ]
    with open(state.duarouter_log_path, "w", encoding="utf-8", newline="\n") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"duarouter failed with exit code {proc.returncode}. See log: {state.duarouter_log_path}")

    input_trip_ids = _collect_ids(state.sumo_trips_path, {"trip"})
    routed_trip_ids = (
        _collect_ids(state.sumo_routes_path, {"vehicle", "trip", "flow"}) if state.sumo_routes_path.exists() else set()
    )
    unrouted_trip_ids = input_trip_ids - routed_trip_ids
    _write_unrouted_trips_xml(state.sumo_trips_path, unrouted_trip_ids, state.sumo_unrouted_trips_path)

    total = len(input_trip_ids)
    routed = len(routed_trip_ids & input_trip_ids)
    unrouted = len(unrouted_trip_ids)
    pct = (100.0 * routed / total) if total else 0.0
    unrouted_pct = (100.0 * unrouted / total) if total else 0.0
    state.sumo_routes_input_count = total
    state.sumo_routes_count = routed
    state.sumo_routes_pct = pct
    state.sumo_unrouted_count = unrouted
    state.sumo_unrouted_pct = unrouted_pct
    print(f"INFO Step 10 duarouter routed {routed}/{total} trips ({pct:.2f}%).")
    print(f"INFO Step 10 duarouter unrouted {unrouted}/{total} trips ({unrouted_pct:.2f}%).")
    print(f"INFO Step 10 duarouter log: {state.duarouter_log_path}")
    print(f"INFO Step 10 unrouted trips XML: {state.sumo_unrouted_trips_path}")
