"""Where, when and which vehicles get stuck in one SUMO run.

Reads the warnings of a run (``--log``) and the routes it simulated, and
splits the teleported vehicles by

* time: five-minute bins of the simulated hour,
* place: the approach (edge and street) and whether it ends at a traffic light,
* vehicle type: car, van and truck, against each type's share of the demand.

A type that is stuck far more often than its share of the demand points to
the vehicle (length, acceleration) rather than to the junction.
"""

from __future__ import annotations

import argparse
import csv
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import sumolib

EVENT = re.compile(
    r"Teleporting vehicle '([^']+)'; (?:waited too long \(([^)]+)\)|(?:junction )?collision with vehicle '[^']+'),"
    r" lane='([^']+)'.*?time=([0-9]+(?:\\.[0-9]+)?)"
)
BIN_S = 300


def vehicle_types(routes: Path) -> dict[str, str]:
    types = {}
    for _event, element in ET.iterparse(routes, events=("end",)):
        if element.tag in ("vehicle", "trip"):
            types[element.get("id")] = element.get("type", "DEFAULT_VEHTYPE")
            element.clear()
    return types


def events(log_text: str) -> list[dict]:
    return [
        {"vehicle": vehicle, "kind": kind or "collision", "lane": lane, "time": float(time)}
        for vehicle, kind, lane, time in EVENT.findall(log_text)
    ]


def _approach(net, lane: str) -> tuple[str, str, bool]:
    """Edge, street and traffic light of the approach a lane belongs to; internal lanes go to their junction."""
    if lane.startswith(":"):
        node_id = lane[1:].rsplit("_", 2)[0]
        if not net.hasNode(node_id):
            return lane, "", False
        node = net.getNode(node_id)
        return lane, f"junction {node_id}", node.getType().startswith("traffic_light")
    edge_id = lane.rsplit("_", 1)[0]
    if not net.hasEdge(edge_id):
        return edge_id, "", False
    edge = net.getEdge(edge_id)
    return edge_id, edge.getName(), edge.getToNode().getType().startswith("traffic_light")


def report(net, log_text: str, types: dict[str, str]) -> dict:
    found = events(log_text)
    by_time: dict[int, Counter] = defaultdict(Counter)
    by_place: dict[str, dict] = {}
    by_type: Counter = Counter()
    for event in found:
        by_time[int(event["time"] // BIN_S) * BIN_S][event["kind"]] += 1
        edge, street, signal = _approach(net, event["lane"])
        place = by_place.setdefault(edge, {"edge": edge, "street": street, "traffic_light": signal, "vehicles": 0, "kinds": Counter()})
        place["vehicles"] += 1
        place["kinds"][event["kind"]] += 1
        by_type[types.get(event["vehicle"], "unknown")] += 1
    demand = Counter(types.values())
    type_rows = [
        {
            "type": kind,
            "demand": demand.get(kind, 0),
            "demand_share_percent": round(100 * demand.get(kind, 0) / max(1, sum(demand.values())), 1),
            "stuck": by_type.get(kind, 0),
            "stuck_share_percent": round(100 * by_type.get(kind, 0) / max(1, len(found)), 1),
            "stuck_per_1000": round(1000 * by_type.get(kind, 0) / demand[kind], 1) if demand.get(kind) else None,
        }
        for kind in sorted(set(demand) | set(by_type))
    ]
    places = sorted(by_place.values(), key=lambda row: -row["vehicles"])
    for row in places:
        row["kinds"] = ", ".join(f"{kind} {count}" for kind, count in row["kinds"].most_common())
    kinds = sorted({event["kind"] for event in found})
    time_rows = [
        {"from_s": start, "to_s": start + BIN_S, "total": sum(counts.values()), **{kind: counts.get(kind, 0) for kind in kinds}}
        for start, counts in sorted(by_time.items())
    ]
    signal = sum(row["vehicles"] for row in places if row["traffic_light"])
    return {"total": len(found), "at_traffic_lights": signal, "time": time_rows, "places": places, "types": type_rows}


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--net", type=Path, required=True)
    parser.add_argument("--routes", type=Path, required=True, help="Routes or trips of the run")
    parser.add_argument("--log", type=Path, required=True, help="SUMO log of the run")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    net = sumolib.net.readNet(str(args.net))
    result = report(net, args.log.read_text(encoding="utf-8", errors="replace"), vehicle_types(args.routes))
    write_csv(args.output_dir / "stuck-by-time.csv", result["time"])
    write_csv(args.output_dir / "stuck-by-place.csv", result["places"])
    write_csv(args.output_dir / "stuck-by-type.csv", result["types"])

    print(f"teleported vehicles: {result['total']}, of them on approaches to traffic lights: {result['at_traffic_lights']}")
    print("by vehicle type (share of demand -> share of stuck vehicles):")
    for row in result["types"]:
        print(
            f"  {row['type']:8s} {row['demand']:7d} trips {row['demand_share_percent']:5.1f} % -> "
            f"{row['stuck']:5d} stuck {row['stuck_share_percent']:5.1f} % ({row['stuck_per_1000']} per 1000 trips)"
        )
    print("by time:")
    for row in result["time"]:
        print(f"  {row['from_s'] // 60:3.0f}-{row['to_s'] // 60:3.0f} min: {row['total']}")
    print("approaches with the most stuck vehicles:")
    for row in result["places"][:15]:
        print(f"  {row['vehicles']:4d}  {row['street'] or row['edge']} ({row['edge']}){' at a signal' if row['traffic_light'] else ''}: {row['kinds']}")
    print(f"tables: {args.output_dir}")


if __name__ == "__main__":
    main()
