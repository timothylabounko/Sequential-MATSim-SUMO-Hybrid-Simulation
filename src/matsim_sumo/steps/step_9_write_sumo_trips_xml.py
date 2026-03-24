import csv
import gzip
from pathlib import Path
from xml.sax.saxutils import escape

from matsim_sumo.pipeline_state import PipelineState


def write_sumo_trips_xml(matched_csv: Path, out_xml: Path) -> tuple[int, int]:
    opener = gzip.open if str(matched_csv).endswith(".gz") else open
    out_xml.parent.mkdir(parents=True, exist_ok=True)
    total = written = 0
    with opener(matched_csv, "rt", newline="") as src, open(out_xml, "w", encoding="utf-8", newline="\n") as dst:
        reader = csv.DictReader(src, delimiter=";")
        dst.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        for row in reader:
            total += 1
            from_edge, to_edge = (row.get("from_edge") or "").strip(), (row.get("to_edge") or "").strip()
            if not from_edge or not to_edge:
                continue
            trip_id = (row.get("new_trip_id") or row.get("trip_id") or f"trip_{written + 1}").strip()
            depart = (row.get("dep_time") or "0").strip() or "0"
            dst.write(f'    <trip id="{escape(trip_id)}" depart="{escape(depart)}" from="{escape(from_edge)}" to="{escape(to_edge)}"/>\n')
            written += 1
        dst.write("</routes>\n")
    return total, written


def run_write_sumo_trips_xml_stage(state: PipelineState) -> None:
    total, written = write_sumo_trips_xml(state.scaled_trips_matched_path, state.sumo_trips_path)
    state.sumo_trips_input_count = total
    state.sumo_trips_count = written
