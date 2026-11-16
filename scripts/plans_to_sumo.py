"""Cut EQASIM/MATSim car and truck routes from output_plans.xml.gz to the SUMO Zurich network.

Every selected car or truck leg is followed link by link through the MATSim
network. The part of the route that lies on the SUMO network becomes one SUMO
trip, departing when the MATSim vehicle reaches the network. That way traffic
that only crosses Zurich is kept, not just trips that start and end in it.
"""

from __future__ import annotations

import argparse
import gzip
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ATTR = re.compile(r'(\w+)="([^"]*)"')
ACTIVITY_S = 60.0


def clock(value: str | None) -> float | None:
    if not value or value == "undefined":
        return None
    try:
        if ":" in value:
            parts = [float(p) for p in value.split(":")]
            while len(parts) < 3:
                parts.append(0.0)
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return float(value)
    except ValueError:
        return None


def lv95_to_wgs84(east: float, north: float) -> tuple[float, float]:
    y = (east - 2_600_000) / 1_000_000
    x = (north - 1_200_000) / 1_000_000
    lon = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y**3
    lat = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x**3
    return lat * 100 / 36, lon * 100 / 36


def wgs84_to_utm32(lat: float, lon: float) -> tuple[float, float]:
    a = 6378137.0
    f = 1 / 298.257223563
    k0 = 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lon0 = math.radians(9)
    lat_r = math.radians(lat)
    normal = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    tan2 = math.tan(lat_r) ** 2
    curve = ep2 * math.cos(lat_r) ** 2
    along = (math.radians(lon) - lon0) * math.cos(lat_r)
    meridian = a * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * lat_r
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * lat_r)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * lat_r)
        - (35 * e2**3 / 3072) * math.sin(6 * lat_r)
    )
    east = k0 * normal * (
        along
        + (1 - tan2 + curve) * along**3 / 6
        + (5 - 18 * tan2 + tan2 * tan2 + 72 * curve - 58 * ep2) * along**5 / 120
    ) + 500000
    north = k0 * (
        meridian
        + normal
        * math.tan(lat_r)
        * (
            along**2 / 2
            + (5 - tan2 + 9 * curve + 4 * curve * curve) * along**4 / 24
            + (61 - 58 * tan2 + tan2 * tan2 + 600 * curve - 330 * ep2) * along**6 / 720
        )
    )
    return east, north


def _allows_passenger(lane: ET.Element) -> bool:
    allow = lane.get("allow")
    if allow is not None:
        return "passenger" in allow.split() or allow == "all"
    return "passenger" not in (lane.get("disallow") or "").split()


def main_component(nodes: list[str], successors: dict[str, set[str]]) -> set[str]:
    """Largest set of edges that can all be driven to from one another."""
    order: list[str] = []
    seen: set[str] = set()
    for root in nodes:
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(successors.get(root, ())))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                order.append(node)
            elif child not in seen:
                seen.add(child)
                stack.append((child, iter(successors.get(child, ()))))
    predecessors: dict[str, set[str]] = defaultdict(set)
    for node, children in successors.items():
        for child in children:
            predecessors[child].add(node)
    best: set[str] = set()
    assigned: set[str] = set()
    for root in reversed(order):
        if root in assigned:
            continue
        component = {root}
        assigned.add(root)
        stack = [root]
        while stack:
            for parent in predecessors.get(stack.pop(), ()):
                if parent not in assigned:
                    assigned.add(parent)
                    component.add(parent)
                    stack.append(parent)
        if len(component) > len(best):
            best = component
    return best


def is_reverse(a: str, b: str) -> bool:
    return a == f"-{b}" or b == f"-{a}"


def trim_piece(piece: list[tuple], main: set[str]) -> tuple[list[tuple] | None, str]:
    """Keep the part of a cut route that starts and ends on the main drivable network.

    A piece whose ends are the two directions of one street only touched the
    boundary, and is dropped.
    """
    inside = [index for index, entry in enumerate(piece) if entry[1] in main]
    if not inside:
        return None, "off the main network"
    kept = piece[inside[0] : inside[-1] + 1]
    if len(kept) < 2 or kept[0][1] == kept[-1][1]:
        return None, "too short after trimming"
    if is_reverse(kept[0][1], kept[-1][1]):
        return None, "only touches the boundary"
    return kept, "trimmed" if len(kept) < len(piece) else "kept"


class SumoNet:
    def __init__(self, path: Path, cell: float = 100.0):
        self.cell = cell
        self.offset = (0.0, 0.0)
        self.boundary = (0.0, 0.0, 0.0, 0.0)
        self.shapes: dict[str, list[tuple[float, float]]] = {}
        self.grid: dict[tuple[int, int], set[str]] = defaultdict(set)
        passenger_lanes: dict[str, set[str]] = {}
        links: list[tuple[str, str, str, str]] = []
        for _, el in ET.iterparse(path, events=("end",)):
            if el.tag == "location":
                ox, oy = (float(v) for v in el.get("netOffset").split(","))
                self.offset = (ox, oy)
                self.boundary = tuple(float(v) for v in el.get("convBoundary").split(","))
            elif el.tag == "edge":
                if el.get("function") != "internal":
                    lanes = el.findall("lane")
                    if lanes and any(_allows_passenger(lane) for lane in lanes):
                        shape = [tuple(map(float, p.split(",")[:2])) for p in lanes[0].get("shape").split()]
                        self.shapes[el.get("id")] = shape
                        passenger_lanes[el.get("id")] = {lane.get("index") for lane in lanes if _allows_passenger(lane)}
                el.clear()
            elif el.tag == "connection" and not el.get("from", ":").startswith(":"):
                links.append((el.get("from"), el.get("fromLane"), el.get("to"), el.get("toLane")))
        successors: dict[str, set[str]] = defaultdict(set)
        for frm, frm_lane, to, to_lane in links:
            if frm_lane in passenger_lanes.get(frm, ()) and to_lane in passenger_lanes.get(to, ()):
                successors[frm].add(to)
        self.main = main_component(list(self.shapes), successors)
        for edge, shape in self.shapes.items():
            for (ax, ay), (bx, by) in zip(shape, shape[1:]):
                steps = max(1, int(math.hypot(bx - ax, by - ay) / (cell / 2)))
                for i in range(steps + 1):
                    t = i / steps
                    self.grid[self._key(ax + t * (bx - ax), ay + t * (by - ay))].add(edge)

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return int(x // self.cell), int(y // self.cell)

    def to_xy(self, east: float, north: float) -> tuple[float, float]:
        ux, uy = wgs84_to_utm32(*lv95_to_wgs84(east, north))
        return ux + self.offset[0], uy + self.offset[1]

    def candidates(self, x: float, y: float) -> set[str]:
        cx, cy = self._key(x, y)
        found: set[str] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                found |= self.grid.get((cx + dx, cy + dy), set())
        return found

    def snap(self, ax: float, ay: float, bx: float, by: float, radius: float) -> str | None:
        """Closest edge to the middle of segment a-b that runs the same way."""
        mx, my = (ax + bx) / 2, (ay + by) / 2
        heading = math.atan2(by - ay, bx - ax)
        best, best_d = None, radius
        for edge in self.candidates(mx, my):
            shape = self.shapes[edge]
            for (px, py), (qx, qy) in zip(shape, shape[1:]):
                dx, dy = qx - px, qy - py
                seg = dx * dx + dy * dy
                t = 0.0 if seg == 0 else max(0.0, min(1.0, ((mx - px) * dx + (my - py) * dy) / seg))
                d = math.hypot(mx - px - t * dx, my - py - t * dy)
                if d >= best_d:
                    continue
                turn = abs((math.atan2(dy, dx) - heading + math.pi) % (2 * math.pi) - math.pi)
                if turn < math.radians(45):
                    best, best_d = edge, d
        return best


def read_matsim_network(path: Path, net: SumoNet, margin: float = 3000.0):
    """Return links and the set of MATSim nodes that lie on the SUMO network."""
    x0, y0, x1, y1 = net.boundary
    nodes: dict[str, tuple[float, float]] = {}
    links: dict[str, tuple[str, str, float]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            s = line.lstrip()
            if s.startswith("<node "):
                a = dict(ATTR.findall(s))
                x, y = net.to_xy(float(a["x"]), float(a["y"]))
                if x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin:
                    nodes[a["id"]] = (x, y)
            elif s.startswith("<link "):
                a = dict(ATTR.findall(s))
                if "car" not in a.get("modes", "car"):
                    continue
                free = float(a.get("freespeed") or 13.9) or 13.9
                links[a["id"]] = (a["from"], a["to"], float(a.get("length") or 0) / free)
    return nodes, links


def vehicle_type(person: str, mode: str) -> str:
    """eqasim light commercial vehicles (``lcv_*`` agents) are vans, not 12 m lorries, though their mode is truck."""
    return "van" if mode == "truck" and person.startswith("lcv_") else mode


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    home = Path.home() / "Downloads" / "National"
    p.add_argument("--plans", type=Path, default=home / "output_plans.xml.gz")
    p.add_argument("--network", type=Path, default=home / "output_network.xml.gz")
    p.add_argument("--net", type=Path, required=True, help="SUMO net.xml")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--from", dest="start", default="07:00:00")
    p.add_argument("--to", dest="end", default="08:00:00")
    p.add_argument("--radius", type=float, default=40.0, help="Snap distance in metres")
    p.add_argument("--modes", default="car,truck")
    p.add_argument(
        "--margin",
        type=float,
        default=0.0,
        help="Also keep trips entering this many seconds before and after the window (negative or late departures), "
        "so scaled copies can be jittered into it",
    )
    p.add_argument("--endpoints", type=Path, help="CSV: per trip, whether each end is an activity location and where")
    p.add_argument("--amenities", type=Path, help="CSV: home, work and education locations weighted by agents")
    args = p.parse_args(argv)
    start_s, end_s = clock(args.start), clock(args.end)
    modes = set(args.modes.split(","))
    lo_s, hi_s = start_s - args.margin, end_s + args.margin

    print("reading SUMO network", flush=True)
    net = SumoNet(args.net)
    print(f"  {len(net.shapes)} drivable edges, {len(net.main)} in the main connected network", flush=True)
    print("reading MATSim network", flush=True)
    nodes, links = read_matsim_network(args.network, net)
    print(f"  {len(links)} car links, {len(nodes)} nodes near Zurich", flush=True)

    link_edge: dict[str, str | None] = {}

    def edge_of(link: str) -> str | None:
        if link not in link_edge:
            info = links.get(link)
            a = nodes.get(info[0]) if info else None
            b = nodes.get(info[1]) if info else None
            link_edge[link] = net.snap(*a, *b, args.radius) if a and b else None
        return link_edge[link]

    trips: list[tuple[float, str, str, str, str]] = []
    ends: dict[str, list] = {}
    pending: list[str] = []
    amenities: dict[tuple[int, int], int] = defaultdict(int)
    x0, y0, x1, y1 = net.boundary
    outcomes: dict[str, int] = defaultdict(int)
    person = ""
    selected = False
    leg_mode = None
    leg_dep = None
    leg_tt = None
    last_end = None
    last_xy = None
    leg_no = 0
    legs_seen = 0
    with gzip.open(args.plans, "rt", encoding="utf-8") as handle:
        for line in handle:
            s = line.lstrip()
            if s.startswith("<person "):
                person = dict(ATTR.findall(s)).get("id", "")
                leg_no = 0
                pending = []
            elif s.startswith("<plan"):
                selected = 'selected="yes"' in s
            elif not selected:
                continue
            elif s.startswith("<activity "):
                a = dict(ATTR.findall(s))
                last_end = clock(a.get("end_time"))
                wanted = args.endpoints or args.amenities
                last_xy = net.to_xy(float(a["x"]), float(a["y"])) if wanted and "x" in a and "y" in a else None
                for vid in pending:
                    ends[vid][4] = last_xy
                pending = []
                kind = a.get("type", "").split("_")[0]
                if args.amenities and last_xy and kind in ("home", "work", "education"):
                    if x0 <= last_xy[0] <= x1 and y0 <= last_xy[1] <= y1:
                        amenities[(round(last_xy[0]), round(last_xy[1]))] += 1
            elif s.startswith("<leg "):
                a = dict(ATTR.findall(s))
                leg_no += 1
                leg_mode = a.get("mode")
                leg_dep = clock(a.get("dep_time")) or last_end
                leg_tt = clock(a.get("trav_time"))
            elif s.startswith("<route ") and leg_mode in modes and leg_dep is not None:
                if leg_dep >= hi_s or leg_dep < lo_s - 3 * 3600:
                    continue
                body = s.split(">", 1)[1].split("<", 1)[0].split()
                if not body:
                    continue
                legs_seen += 1
                if legs_seen % 20000 == 0:
                    print(f"  {legs_seen} {', '.join(sorted(modes))} legs read, {len(trips)} trips so far", flush=True)
                free = [links[l][2] if l in links else 0.0 for l in body]
                total = sum(free)
                factor = max(1.0, leg_tt / total) if leg_tt and total > 0 else 1.0
                elapsed = 0.0
                seg: list[tuple[float, str]] = []
                gap = 0.0
                pieces: list[list[tuple[float, str]]] = []
                link_start: list[float] = []
                for index, (link, t) in enumerate(zip(body, free)):
                    link_start.append(elapsed)
                    edge = edge_of(link)
                    if edge:
                        if seg and gap > 300:
                            pieces.append(seg)
                            seg = []
                        if not seg or seg[-1][1] != edge:
                            seg.append((leg_dep + elapsed, edge, index))
                        elif len(seg) > 1:
                            # The first entry keeps its index so a start on the activity link stays recognisable.
                            seg[-1] = (seg[-1][0], edge, index)
                        gap = 0.0
                    elif seg:
                        gap += t * factor
                    elapsed += t * factor
                if seg:
                    pieces.append(seg)
                for n, piece in enumerate(pieces):
                    kept, outcome = trim_piece(piece, net.main)
                    if kept is None:
                        if lo_s <= piece[0][0] < hi_s:
                            outcomes[outcome] += 1
                        continue
                    enter = kept[0][0]
                    if not lo_s <= enter < hi_s:
                        continue
                    outcomes[outcome] += 1
                    vid = f"{person}_{leg_no}_{n}"
                    trips.append((enter - start_s, vid, vehicle_type(person, leg_mode), kept[0][1], kept[-1][1]))
                    if args.endpoints:
                        # An end is free when the car is on the SUMO network within ACTIVITY_S of leaving or
                        # reaching its activity; otherwise it is a fixed boundary crossing.
                        first, last = kept[0][2], kept[-1][2]
                        start_free = n == 0 and link_start[first] <= ACTIVITY_S
                        end_free = n == len(pieces) - 1 and elapsed - link_start[last] - free[last] * factor <= ACTIVITY_S
                        ends[vid] = [start_free, last_xy if start_free else None, end_free, leg_mode, None]
                        if end_free:
                            pending.append(vid)

    trips.sort()
    if args.endpoints:
        with open(args.endpoints, "w", encoding="utf-8") as out:
            out.write("id,start_free,start_x,start_y,end_free,end_x,end_y\n")
            for vid, (start_free, sxy, end_free, _mode, exy) in ends.items():
                start_free = start_free and sxy is not None
                end_free = end_free and exy is not None
                sx, sy = (f"{sxy[0]:.1f}", f"{sxy[1]:.1f}") if start_free else ("", "")
                ex, ey = (f"{exy[0]:.1f}", f"{exy[1]:.1f}") if end_free else ("", "")
                out.write(f"{vid},{int(start_free)},{sx},{sy},{int(end_free)},{ex},{ey}\n")
        free = sum(1 for value in ends.values() if value[0] and value[1] is not None)
        print(f"wrote trip ends to {args.endpoints}: {free} of {len(ends)} trips start at an activity")
    if args.amenities:
        with open(args.amenities, "w", encoding="utf-8") as out:
            out.write("x,y,weight\n")
            for (x, y), weight in amenities.items():
                out.write(f"{x},{y},{weight}\n")
        print(f"wrote {len(amenities)} home, work and education locations to {args.amenities}")
    counts = defaultdict(int)
    with open(args.output, "w", encoding="utf-8") as out:
        out.write("<routes>\n")
        out.write('    <vType id="car" vClass="passenger" color="255,220,0"/>\n')
        out.write('    <vType id="van" vClass="passenger" length="6.5" guiShape="delivery" color="255,140,0"/>\n')
        out.write('    <vType id="truck" vClass="passenger" length="12" accel="1.0" maxSpeed="25" color="255,60,60"/>\n')
        for depart, vid, mode, frm, to in trips:
            counts[mode] += 1
            out.write(f'    <trip id="{vid}" type="{mode}" depart="{depart:.2f}" from="{frm}" to="{to}"/>\n')
        out.write("</routes>\n")
    print(f"wrote {len(trips)} trips to {args.output}: " + ", ".join(f"{m} {c}" for m, c in sorted(counts.items())))
    print("route pieces in the window: " + ", ".join(f"{name} {count}" for name, count in sorted(outcomes.items())))


if __name__ == "__main__":
    main()
