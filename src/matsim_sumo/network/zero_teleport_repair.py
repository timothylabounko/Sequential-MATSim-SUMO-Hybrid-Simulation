"""Repair the junctions behind the teleports left at full traffic.

Reads a repaired network, the log of one simulation on it and the routes that
run used, and writes one patched network:

1. Priority against OpenStreetMap. Every junction where at least
   ``--min-yields`` vehicles waited ``--teleport`` seconds to give way is
   compared with OSM. A traffic light in OSM becomes an actuated signal; an OSM
   light that is closer to another SUMO signal belongs to that one, and a
   junction within ``JOIN_SIGNAL_M`` of an existing signal joins it. An
   edge whose priority differs from its OSM road class gets the priority of
   that class from SUMO's OSM type map. A right-before-left junction between
   roads of different class, one of them tertiary or higher, becomes a
   priority junction. When OSM has a stop or give-way sign on the waiting road,
   or agrees with the network, the junction is left alone: that yield is a
   capacity limit and is left to the behaviour search.
2. Lane continuations. A vehicle stopped with "wrong lane" or "no connection"
   was on a lane that does not reach the next edge of its route, while the lane
   next to it does. That lane gets the same movement. When no lane of the edge
   reaches the next edge, the movement is missing and is only reported.
3. Signal programs. New connections and new signals renumber a signal's links.
   Every program in ``--signals`` is rewritten link by link for the new network,
   so the actuated timings stay. A program that cannot be carried over is
   dropped.
4. Webster. Every signal left without a program (new, joined, dropped, or
   added in an earlier round) is timed as in the signal stage: Webster's
   cycle and splits from the routed demand (``--demand``), actuated with
   maximum green 1.25 times Webster's green.

Nothing is simulated here. The behaviour search on the written network
includes the SUMO default driver, which is the plain-driver check.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import sumolib
from sumolib.geomhelper import distancePointToPolygon

from matsim_sumo.network.osm_connectivity import _overpass, osm_way_id
from matsim_sumo.network.signal_timing import NETCONVERT_OPTIONS, plan_programs, webster, write_programs
from matsim_sumo.network.turn_evolution import parse_gridlocks

CONTROL_RADIUS_M = 30.0
SIGN_TO_APPROACH_M = 15.0
JOIN_SIGNAL_M = 25.0
# The signal stage's defaults, so new signals are timed like the existing ones.
MIN_CYCLE_S = 60
MAX_CYCLE_S = 120
MAX_GREEN_FACTOR = 1.25
RIGHT_BEFORE_LEFT_LIMIT = "highway.tertiary"
WRONG_LANE = re.compile(r"Teleporting vehicle '([^']+)'; waited too long \(wrong lane\), lane='([^']+)'")
NO_CONNECTION = re.compile(
    r"Vehicle '([^']+)' performs emergency stop at the end of lane '([^']+)' because there is no connection"
)


def type_priorities(sumo_home: str | None = None) -> dict[str, int]:
    """Edge priority of each OSM road class in SUMO's OSM type map."""
    home = Path(sumo_home or os.environ["SUMO_HOME"])
    root = ET.parse(home / "data" / "typemap" / "osmNetconvert.typ.xml").getroot()
    return {item.get("id"): int(item.get("priority")) for item in root.iter("type") if item.get("priority")}


def _node_of_lane(net, lane: str):
    if lane.startswith(":"):
        node_id = lane[1:].rsplit("_", 2)[0]
        return (net.getNode(node_id) if net.hasNode(node_id) else None), None
    edge_id = lane.rsplit("_", 1)[0]
    if not net.hasEdge(edge_id):
        return None, None
    edge = net.getEdge(edge_id)
    return edge.getToNode(), edge_id


def yield_sites(net, log_text: str, min_yields: int) -> list[dict]:
    """Junctions where at least ``min_yields`` vehicles gave up waiting to give way."""
    sites: dict[str, dict] = {}
    for item in parse_gridlocks(log_text):
        if item.kind != "yield":
            continue
        node, edge_id = _node_of_lane(net, item.lane)
        if node is None:
            continue
        site = sites.setdefault(node.getID(), {"node": node.getID(), "vehicles": 0, "approaches": Counter()})
        site["vehicles"] += 1
        if edge_id:
            site["approaches"][edge_id] += 1
    rows = [site for site in sites.values() if site["vehicles"] >= min_yields]
    rows.sort(key=lambda site: -site["vehicles"])
    return rows


def _road_edges(node) -> list:
    edges = list(node.getIncoming()) + list(node.getOutgoing())
    return [edge for edge in edges if (edge.getType() or "").startswith("highway") and not edge.getID().startswith(":")]


def osm_query(net, node, radius: float = CONTROL_RADIUS_M) -> str:
    ways = sorted({way for way in (osm_way_id(edge.getID()) for edge in _road_edges(node)) if way is not None})
    lon, lat = net.convertXY2LonLat(*node.getCoord())
    parts = ["[out:json][timeout:25];"]
    if ways:
        parts.append(f"way(id:{','.join(str(way) for way in ways)});out tags;")
    parts.append(f'node(around:{radius:g},{lat:.7f},{lon:.7f})[highway~"^(traffic_signals|stop|give_way)$"];out body;')
    return "".join(parts)


def fetch_osm(net, sites: list[dict], cache_path: Path, radius: float = CONTROL_RADIUS_M) -> dict[str, dict]:
    """OSM way tags and nearby signals and signs per junction, kept in ``cache_path``."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    for index, site in enumerate(sites, 1):
        if site["node"] in cache:
            continue
        payload = _overpass(osm_query(net, net.getNode(site["node"]), radius))
        ways, controls = {}, []
        for element in payload.get("elements", []):
            if element.get("type") == "way":
                ways[str(element["id"])] = element.get("tags", {})
            elif element.get("type") == "node":
                controls.append({"lat": element["lat"], "lon": element["lon"], "tags": element.get("tags", {})})
        cache[site["node"]] = {"ways": ways, "controls": controls}
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        print(f"  osm {index}/{len(sites)} {site['node']}: {len(ways)} ways, {len(controls)} signals or signs", flush=True)
    return cache


def junction_signal(controls: list[dict]) -> bool:
    """A traffic light for the junction itself, not a pedestrian crossing light."""
    return bool(junction_lights(controls))


def junction_lights(controls: list[dict]) -> list[dict]:
    lights = []
    for control in controls:
        tags = control["tags"]
        if tags.get("highway") != "traffic_signals":
            continue
        if "crossing" in tags or tags.get("traffic_signals") in ("crossing_only", "crossing", "pedestrian_crossing"):
            continue
        lights.append(control)
    return lights


def signal_nodes(net) -> list[tuple[str, float, float]]:
    """(signal id, x, y) of every junction a SUMO signal controls."""
    found = {}
    for tls in net.getTrafficLights():
        for in_lane, _out_lane, _index in tls.getConnections():
            node = in_lane.getEdge().getToNode()
            found.setdefault(node.getID(), (tls.getID(), *node.getCoord()))
    return list(found.values())


def own_lights(net, node, lights: list[dict], signals: list[tuple[str, float, float]]) -> list[dict]:
    """OSM lights that are closer to ``node`` than to any junction a SUMO signal already controls."""
    x, y = node.getCoord()
    own = []
    for light in lights:
        lx, ly = net.convertLonLat2XY(light["lon"], light["lat"])
        to_node = math.hypot(lx - x, ly - y)
        if all(math.hypot(lx - sx, ly - sy) > to_node for _tls, sx, sy in signals):
            own.append(light)
    return own


def nearby_signal(node, signals: list[tuple[str, float, float]], limit: float = JOIN_SIGNAL_M) -> str | None:
    x, y = node.getCoord()
    near = [(math.hypot(sx - x, sy - y), tls) for tls, sx, sy in signals]
    near = [item for item in near if item[0] <= limit]
    return min(near)[1] if near else None


def signed_approaches(net, node, controls: list[dict]) -> dict[str, str]:
    """Incoming edges with an OSM stop or give-way sign close to their end."""
    signed = {}
    incoming = [edge for edge in node.getIncoming() if not edge.getID().startswith(":")]
    for control in controls:
        sign = control["tags"].get("highway")
        if sign not in ("stop", "give_way") or not incoming:
            continue
        point = net.convertLonLat2XY(control["lon"], control["lat"])
        nearest = min(incoming, key=lambda edge: distancePointToPolygon(point, edge.getShape()))
        if distancePointToPolygon(point, nearest.getShape()) <= SIGN_TO_APPROACH_M:
            signed[nearest.getID()] = sign
    return signed


def priority_decisions(net, sites: list[dict], osm: dict[str, dict], priorities: dict[str, int]):
    """Edge priorities, node types and one report row per junction."""
    edge_patches: dict[str, int] = {}
    node_patches: dict[str, dict[str, str]] = {}
    rows = []
    signals = signal_nodes(net)
    for site in sites:
        node = net.getNode(site["node"])
        found = osm.get(site["node"], {"ways": {}, "controls": []})
        approaches = ", ".join(
            f"{net.getEdge(edge_id).getName() or edge_id} {count}" for edge_id, count in site["approaches"].most_common()
        )
        row = {
            "junction": node.getID(),
            "type": node.getType(),
            "yields": site["vehicles"],
            "approaches": approaches,
            "decision": "",
            "changes": "",
        }
        lon, lat = net.convertXY2LonLat(*node.getCoord())
        row["lat"], row["lon"] = round(lat, 6), round(lon, 6)
        if node.getType().startswith("traffic_light"):
            row["decision"] = "already a signal; left to the behaviour search"
            rows.append(row)
            continue
        if own_lights(net, node, junction_lights(found["controls"]), signals):
            joined = nearby_signal(node, signals)
            node_patches[node.getID()] = {"type": "traffic_light", "tlType": "actuated"}
            if joined:
                node_patches[node.getID()]["tl"] = joined
                row["decision"] = f"OSM has a traffic light: joins the actuated signal {joined}"
            else:
                row["decision"] = "OSM has a traffic light: actuated signal"
            rows.append(row)
            continue
        changes = []
        classes = {}
        for edge in _road_edges(node):
            way = osm_way_id(edge.getID())
            tags = found["ways"].get(str(way)) if way is not None else None
            if not tags or "highway" not in tags:
                continue
            osm_type = f"highway.{tags['highway']}"
            want = priorities.get(osm_type)
            if want is None:
                continue
            if edge in node.getIncoming():
                classes[edge.getID()] = want
            if want != edge.getPriority():
                edge_patches[edge.getID()] = want
                changes.append(f"{edge.getID()} {edge.getPriority()}->{want} ({osm_type})")
        signed = signed_approaches(net, node, found["controls"])
        limit = priorities.get(RIGHT_BEFORE_LEFT_LIMIT, 10)
        if node.getType() == "right_before_left" and len(set(classes.values())) > 1 and max(classes.values()) >= limit:
            node_patches[node.getID()] = {"type": "priority"}
            changes.append("right_before_left -> priority")
        row["changes"] = "; ".join(changes)
        waiting_signed = [f"{edge_id} {sign}" for edge_id, sign in signed.items() if edge_id in site["approaches"]]
        if changes:
            row["decision"] = "priority set from OSM"
        elif waiting_signed:
            row["decision"] = "OSM sign on the waiting road (" + ", ".join(waiting_signed) + "); capacity limit"
        elif not found["ways"]:
            row["decision"] = "no OSM ways for these edges; unchanged"
        else:
            row["decision"] = "OSM agrees with the network; capacity limit"
        rows.append(row)
    return edge_patches, node_patches, rows


def route_edges(routes: Path, vehicles: set[str]) -> dict[str, list[str]]:
    """Route of each vehicle; the last route when the file is SUMO's vehroute output."""
    found = {}
    with (gzip.open(routes, "rb") if routes.suffix == ".gz" else routes.open("rb")) as handle:
        for _event, element in ET.iterparse(handle, events=("end",)):
            if element.tag == "vehicle":
                if element.get("id") in vehicles:
                    routes_of_vehicle = element.findall("route") + element.findall("routeDistribution/route")
                    if routes_of_vehicle:
                        found[element.get("id")] = routes_of_vehicle[-1].get("edges", "").split()
                element.clear()
    return found


def lane_continuations(net, log_text: str, routes: Path):
    """Connections that let a stuck lane follow the movement of the lane next to it."""
    stuck = {}
    for pattern, kind in ((WRONG_LANE, "wrong lane"), (NO_CONNECTION, "no connection")):
        for vehicle, lane in pattern.findall(log_text):
            if not lane.startswith(":"):
                stuck[vehicle] = (lane, kind)
    paths = route_edges(routes, set(stuck))
    added: dict[tuple[str, str, int, int], int] = {}
    rows = []
    for vehicle, (lane_id, kind) in stuck.items():
        edge_id, index = lane_id.rsplit("_", 1)
        index = int(index)
        row = {"vehicle": vehicle, "kind": kind, "lane": lane_id, "next_edge": "", "decision": ""}
        rows.append(row)
        path = paths.get(vehicle, [])
        if not net.hasEdge(edge_id) or edge_id not in path or path.index(edge_id) + 1 >= len(path):
            row["decision"] = "route changed by rerouting; unknown next edge"
            continue
        next_edge = path[path.index(edge_id) + 1]
        row["next_edge"] = next_edge
        edge = net.getEdge(edge_id)
        reaching = {}
        for lane in edge.getLanes():
            targets = [c.getToLane().getIndex() for c in lane.getOutgoing() if c.getToLane().getEdge().getID() == next_edge]
            if targets:
                reaching[lane.getIndex()] = targets
        if index in reaching:
            row["decision"] = "lane already reaches the next edge"
            continue
        if not reaching:
            row["decision"] = "no lane reaches the next edge; missing movement, not added"
            continue
        neighbour = min(reaching, key=lambda other: abs(other - index))
        if abs(neighbour - index) > 1:
            row["decision"] = f"only lane {neighbour} reaches it; not next to lane {index}"
            continue
        to_lane = min(reaching[neighbour]) if index < neighbour else max(reaching[neighbour])
        key = (edge_id, next_edge, index, to_lane)
        added[key] = added.get(key, 0) + 1
        row["decision"] = f"lane {index} -> {next_edge}_{to_lane}, as lane {neighbour}"
    return added, rows


def signal_links(net) -> dict[str, dict[int, tuple]]:
    links: dict[str, dict[int, tuple]] = {}
    for tls in net.getTrafficLights():
        for in_lane, out_lane, index in tls.getConnections():
            links.setdefault(tls.getID(), {})[index] = (
                in_lane.getEdge().getID(),
                in_lane.getIndex(),
                out_lane.getEdge().getID(),
                out_lane.getIndex(),
            )
    return links


def link_mapping(old: dict[int, tuple], new: dict[int, tuple]) -> list[int] | None:
    """Old link index for each new link index, or None when a new link has no counterpart."""
    found = link_matches(old, new)
    return None if found is None else [index for index, _exact in found]


def link_matches(old: dict[int, tuple], new: dict[int, tuple]) -> list[tuple[int, bool]] | None:
    """(old link index, same lanes) for each new link index, or None when a new link has no counterpart."""
    mapping = []
    for index in range(max(new) + 1 if new else 0):
        link = new.get(index)
        if link is None:
            return None
        from_edge, from_lane, to_edge, to_lane = link
        candidates = [
            [i for i, o in old.items() if o == link],
            sorted(
                (i for i, o in old.items() if o[0] == from_edge and o[2] == to_edge),
                key=lambda i: (abs(old[i][1] - from_lane), abs(old[i][3] - to_lane)),
            ),
            [i for i, o in old.items() if o[0] == from_edge and o[1] == from_lane],
        ]
        match = next((group[0] for group in candidates if group), None)
        if match is None:
            return None
        mapping.append((match, old[match] == link))
    return mapping


def carried_state(state: str, matches: list[tuple[int, bool]]) -> str:
    """A phase state for the new links. A copied link only gets a yielding green, so it never has
    priority over the movement it was copied from."""
    out = []
    for index, exact in matches:
        signal = state[index] if index < len(state) else "r"
        out.append("g" if signal == "G" and not exact else signal)
    return "".join(out)


def carry_signals(old_net, new_net, signals_in: Path, signals_out: Path) -> dict:
    old_links, new_links = signal_links(old_net), signal_links(new_net)
    tree = ET.parse(signals_in)
    root = tree.getroot()
    report = {"kept": 0, "remapped": [], "dropped": []}
    for logic in list(root.iter("tlLogic")):
        tls_id = logic.get("id")
        if tls_id not in new_links:
            root.remove(logic)
            report["dropped"].append(f"{tls_id}: not a signal any more")
            continue
        old, new = old_links.get(tls_id, {}), new_links[tls_id]
        if old == new:
            report["kept"] += 1
            continue
        matches = link_matches(old, new)
        if matches is None:
            root.remove(logic)
            report["dropped"].append(f"{tls_id}: a new link has no counterpart; timed again below")
            continue
        for phase in logic.findall("phase"):
            phase.set("state", carried_state(phase.get("state", ""), matches))
        report["remapped"].append(tls_id)
    tree.write(signals_out, encoding="UTF-8", xml_declaration=True)
    return report


def safe_greens(net, signals_file: Path) -> list[str]:
    """Where two priority greens ('G') of a phase enter the same lane, all but the link that changes
    lanes least yield ('g'). SUMO warns about these phases; links copied in a repair cause them."""
    links = signal_links(net)
    tree = ET.parse(signals_file)
    fixed = []
    for logic in tree.getroot().iter("tlLogic"):
        tls_links = links.get(logic.get("id"), {})
        changed = False
        for phase in logic.findall("phase"):
            state = list(phase.get("state", ""))
            into: dict[tuple[str, int], list[int]] = {}
            for index, signal in enumerate(state):
                if signal == "G" and index in tls_links:
                    _from_edge, _from_lane, to_edge, to_lane = tls_links[index]
                    into.setdefault((to_edge, to_lane), []).append(index)
            for (_to_edge, to_lane), indices in into.items():
                if len(indices) < 2:
                    continue
                keep = min(indices, key=lambda i: (abs(tls_links[i][1] - to_lane), i))
                for index in indices:
                    if index != keep:
                        state[index] = "g"
                        changed = True
            phase.set("state", "".join(state))
        if changed:
            fixed.append(logic.get("id"))
    tree.write(signals_file, encoding="UTF-8", xml_declaration=True)
    return fixed


def untimed_signals(net, signals_file: Path) -> list[str]:
    """Signals of ``net`` without a program in ``signals_file``: they run netconvert's guessed program."""
    timed = {logic.get("id") for logic in ET.parse(signals_file).getroot().iter("tlLogic")}
    return sorted(tls.getID() for tls in net.getTrafficLights() if tls.getID() not in timed)


def time_signals(net_path: Path, demand: Path, signals_file: Path, work: Path, ids: list[str]) -> list[str]:
    """Webster programs, actuated as in the signal stage, for ``ids``, appended to ``signals_file``."""
    raw = webster(net_path, demand, work / "webster.raw.add.xml", MIN_CYCLE_S, MAX_CYCLE_S)
    net = sumolib.net.readNet(str(net_path), withPrograms=True)
    plans = {tls_id: plan for tls_id, plan in plan_programs(net, raw, MAX_GREEN_FACTOR).items() if tls_id in ids}
    if not plans:
        return []
    added = write_programs(plans, work / "signals.webster-new.add.xml", actuated=True)
    tree = ET.parse(signals_file)
    for logic in ET.parse(added).getroot().iter("tlLogic"):
        tree.getroot().append(logic)
    tree.write(signals_file, encoding="UTF-8", xml_declaration=True)
    return sorted(plans)


def write_patches(out_dir: Path, edge_patches, node_patches, connections) -> list[str]:
    options = []
    if edge_patches:
        path = out_dir / "priority.edg.xml"
        lines = [f'    <edge id="{edge_id}" priority="{value}"/>' for edge_id, value in sorted(edge_patches.items())]
        path.write_text("<edges>\n" + "\n".join(lines) + "\n</edges>\n", encoding="utf-8")
        options += ["-e", str(path)]
    if node_patches:
        path = out_dir / "priority.nod.xml"
        lines = []
        for node_id, attributes in sorted(node_patches.items()):
            values = " ".join(f'{key}="{value}"' for key, value in attributes.items())
            lines.append(f'    <node id="{node_id}" {values}/>')
        path.write_text("<nodes>\n" + "\n".join(lines) + "\n</nodes>\n", encoding="utf-8")
        options += ["-n", str(path)]
    if connections:
        path = out_dir / "lanes.con.xml"
        lines = [
            f'    <connection from="{frm}" to="{to}" fromLane="{fl}" toLane="{tl}"/>'
            for frm, to, fl, tl in sorted(connections)
        ]
        path.write_text("<connections>\n" + "\n".join(lines) + "\n</connections>\n", encoding="utf-8")
        options += ["--connection-files", str(path)]
    return options


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["decision"])
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--net", type=Path, required=True, help="Network of the last round (read only)")
    parser.add_argument("--log", type=Path, required=True, help="SUMO warnings of one run on that network")
    parser.add_argument("--routes", type=Path, required=True, help="Routes that run simulated")
    parser.add_argument("--signals", type=Path, required=True, help="Signal programs that run used")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--min-yields", type=int, default=2, help="Yield teleports at a junction before OSM is asked")
    parser.add_argument("--demand", type=Path, help="Routed demand for Webster's flows (default: --routes)")
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"reading {args.net}", flush=True)
    net = sumolib.net.readNet(str(args.net))
    log_text = args.log.read_text(encoding="utf-8", errors="replace")

    sites = yield_sites(net, log_text, args.min_yields)
    print(f"1 priority against OpenStreetMap at {len(sites)} junctions with {args.min_yields}+ yields", flush=True)
    osm = fetch_osm(net, sites, args.out_dir / "osm-junctions.json")
    edge_patches, node_patches, priority_rows = priority_decisions(net, sites, osm, type_priorities())
    write_csv(args.out_dir / "priority.csv", priority_rows)
    for row in priority_rows:
        detail = f" [{row['changes']}]" if row["changes"] else ""
        print(f"  {row['yields']:3d}  {row['approaches'] or row['junction']}: {row['decision']}{detail}", flush=True)

    print("2 lane continuations for wrong-lane and no-connection stops", flush=True)
    connections, lane_rows = lane_continuations(net, log_text, args.routes)
    write_csv(args.out_dir / "lanes.csv", lane_rows)
    for row in lane_rows:
        print(f"  {row['kind']:13} {row['lane']}: {row['decision']}", flush=True)

    output = args.out_dir / "repaired.net.xml"
    patches = write_patches(args.out_dir, edge_patches, node_patches, connections)
    if not patches:
        print("nothing to change; the network is copied", flush=True)
    command = ["netconvert", "-s", str(args.net), *patches, *NETCONVERT_OPTIONS, "-o", str(output)]
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=3600)
    (args.out_dir / "netconvert.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not output.is_file():
        raise SystemExit((completed.stdout + completed.stderr)[-2000:])

    print("3 signal programs carried over link by link", flush=True)
    new_net = sumolib.net.readNet(str(output))
    signals = carry_signals(net, new_net, args.signals, args.out_dir / "signals.add.xml")
    print(
        f"  {signals['kept']} unchanged, {len(signals['remapped'])} renumbered, {len(signals['dropped'])} dropped",
        flush=True,
    )
    for line in signals["dropped"]:
        print(f"  dropped {line}", flush=True)

    untimed = untimed_signals(new_net, args.out_dir / "signals.add.xml")
    timed: list[str] = []
    if untimed:
        print(f"4 Webster timing for {len(untimed)} signals without a timed program", flush=True)
        try:
            timed = time_signals(output, args.demand or args.routes, args.out_dir / "signals.add.xml", args.out_dir, untimed)
        except RuntimeError as exc:
            print(f"  Webster failed, netconvert's actuated programs stay: {str(exc)[-300:]}", flush=True)
        print(f"  {len(timed)} timed: {', '.join(timed) or '-'}", flush=True)
        for tls_id in sorted(set(untimed) - set(timed)):
            print(f"  {tls_id}: no traffic in the routes or a phase mismatch; netconvert's actuated program stays", flush=True)
    yielding = safe_greens(new_net, args.out_dir / "signals.add.xml")
    if yielding:
        print(f"  two priority greens into one lane, the extra link now yields: {', '.join(yielding)}", flush=True)

    summary = {
        "net": str(args.net),
        "log": str(args.log),
        "yield_junctions": len(sites),
        "edge_priorities": edge_patches,
        "node_types": node_patches,
        "lane_connections": [list(key) for key in sorted(connections)],
        "signals": signals,
        "webster_timed": timed,
        "yielding_greens": yielding,
        "untimed_after": sorted(set(untimed) - set(timed)),
        "output": str(output),
    }
    (args.out_dir / "repair.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {output}: {len(edge_patches)} edge priorities, {len(node_patches)} junction types, "
        f"{len(connections)} lane connections",
        flush=True,
    )


if __name__ == "__main__":
    main()
