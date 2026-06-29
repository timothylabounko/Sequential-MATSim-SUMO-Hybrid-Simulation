import csv
import gzip
from pathlib import Path
from xml.sax.saxutils import escape

from matsim_sumo.pipeline.state import PipelineState


MISSING_DEPART_VALUES = {"", "nan", "nat", "none", "null", "<na>"}


def _normalize_depart(value: str | None) -> str | None:
    depart = (value or "").strip()
    if depart.lower() in MISSING_DEPART_VALUES:
        return None

    if ":" in depart:
        parts = depart.split(":")
        if len(parts) != 3:
            return None
        try:
            hour, minute, second = (int(part) for part in parts)
        except ValueError:
            return None
        if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60:
            return None
        return depart

    try:
        seconds = float(depart)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return str(int(seconds)) if seconds.is_integer() else depart


def _clean(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value.lower() in MISSING_DEPART_VALUES else value


def _endpoint_attr(row: dict[str, str], edge_col: str, junction_col: str, edge_attr: str, junction_attr: str) -> tuple[str, str] | None:
    junction = _clean(row.get(junction_col))
    if junction:
        return junction_attr, junction
    edge = _clean(row.get(edge_col))
    if edge:
        return edge_attr, edge
    return None


def write_sumo_trips_xml(matched_csv: Path, out_xml: Path) -> tuple[int, int]:
    opener = gzip.open if str(matched_csv).endswith(".gz") else open
    out_xml.parent.mkdir(parents=True, exist_ok=True)
    total = written = 0
    with opener(matched_csv, "rt", newline="") as src, open(out_xml, "w", encoding="utf-8", newline="\n") as dst:
        reader = csv.DictReader(src, delimiter=";")
        dst.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        for row in reader:
            total += 1
            from_attr = _endpoint_attr(row, "from_edge", "from_junction", "from", "fromJunction")
            to_attr = _endpoint_attr(row, "to_edge", "to_junction", "to", "toJunction")
            if from_attr is None or to_attr is None:
                continue
            depart = _normalize_depart(row.get("dep_time"))
            if depart is None:
                continue
            trip_id = (row.get("new_trip_id") or row.get("trip_id") or f"trip_{written + 1}").strip()
            from_name, from_value = from_attr
            to_name, to_value = to_attr
            dst.write(
                f'    <trip id="{escape(trip_id)}" depart="{escape(depart)}" '
                f'{from_name}="{escape(from_value)}" {to_name}="{escape(to_value)}"/>\n'
            )
            written += 1
        dst.write("</routes>\n")
    return total, written


def run_write_sumo_trips_xml_stage(state: PipelineState) -> None:
    total, written = write_sumo_trips_xml(state.scaled_trips_matched_path, state.sumo_trips_path)
    state.sumo_trips_input_count = total
    state.sumo_trips_count = written
