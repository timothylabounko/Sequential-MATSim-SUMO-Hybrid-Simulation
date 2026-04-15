import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from matsim_sumo.pipeline_state import PipelineState

SAMPLE_FRACTION = 1


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


def _write_first_trip_fraction_xml(input_xml: Path, output_xml: Path, fraction: float) -> tuple[int, int, set[str]]:
    total = 0
    for _, elem in ET.iterparse(input_xml, events=("end",)):
        if _local_name(elem.tag) == "trip":
            total += 1
        elem.clear()
    keep = max(1, int(total * float(fraction))) if total else 0
    kept_ids: set[str] = set()
    output_xml.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(output_xml, "w", encoding="utf-8", newline="\n") as dst:
        dst.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        if keep:
            for _, elem in ET.iterparse(input_xml, events=("end",)):
                if _local_name(elem.tag) == "trip":
                    if written >= keep:
                        elem.clear()
                        break
                    trip_id = (elem.get("id") or "").strip()
                    if trip_id:
                        kept_ids.add(trip_id)
                    dst.write(f"    {ET.tostring(elem, encoding='unicode').strip()}\n")
                    written += 1
                elem.clear()
        dst.write("</routes>\n")
    return total, written, kept_ids


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
    sampled_trips_path = state.sumo_trips_path.with_name(f"{state.sumo_trips_path.stem}_first5pct.xml")
    total_all, total, input_trip_ids = _write_first_trip_fraction_xml(
        state.sumo_trips_path,
        sampled_trips_path,
        SAMPLE_FRACTION,
    )
    cmd = [
        str(state.config["duarouter_bin"]),
        "-n",
        str(state.sumo_network_path),
        "-r",
        str(sampled_trips_path),
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

    routed_trip_ids = (
        _collect_ids(state.sumo_routes_path, {"vehicle", "trip", "flow"}) if state.sumo_routes_path.exists() else set()
    )
    unrouted_trip_ids = input_trip_ids - routed_trip_ids
    _write_unrouted_trips_xml(sampled_trips_path, unrouted_trip_ids, state.sumo_unrouted_trips_path)

    routed = len(routed_trip_ids & input_trip_ids)
    unrouted = len(unrouted_trip_ids)
    pct = (100.0 * routed / total) if total else 0.0
    unrouted_pct = (100.0 * unrouted / total) if total else 0.0
    state.sumo_routes_input_count = total
    state.sumo_routes_count = routed
    state.sumo_routes_pct = pct
    state.sumo_unrouted_count = unrouted
    state.sumo_unrouted_pct = unrouted_pct
    sample_pct = (100.0 * total / total_all) if total_all else 0.0
    print(f"INFO Step 10 sampled {total}/{total_all} trips ({sample_pct:.2f}%).")
    print(f"INFO Step 10 duarouter routed {routed}/{total} trips ({pct:.2f}%).")
    print(f"INFO Step 10 duarouter unrouted {unrouted}/{total} trips ({unrouted_pct:.2f}%).")
    print(f"INFO Step 10 duarouter log: {state.duarouter_log_path}")
    print(f"INFO Step 10 unrouted trips XML: {state.sumo_unrouted_trips_path}")
