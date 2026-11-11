"""Scale a sampled SUMO trip file up to the full population, following Baggenstos (2026, Section 3.3.3).

EQASIM scenarios simulate a sample of the population, so a trip file cut from
them carries only that share of the real traffic. The scale factor is
``1 / sample share``; with ``--matsim-config`` it is read from the EQASIM
``output_config.xml`` (``sampleSize``, else the QSim ``flowCapacityFactor``).

* Trip cloning: every original trip is kept unchanged, and ``scale - 1``
  clones are added (a fractional rest is drawn at random). A clone's id is the
  original id with ``_x<k>`` appended.
* Temporal perturbation: each clone departs at the original time plus a
  uniform offset in ``[-jitter, +jitter]`` (default 1800 s, ±30 min). Only
  departures inside the simulated window ``[0, --window)`` are kept, so the
  input should be cut with a margin of ``jitter`` seconds on both sides of the
  window (``plans_to_sumo.py --margin``); clones of trips just outside the
  window then move into it, as clones near midnight do in the daily run.
* Spatial perturbation: a trip end at an activity is moved within
  ``--radius`` metres (default 500 m); a boundary crossing stays fixed. So an
  inside trip moves both ends, an entering trip its end, a leaving trip its
  start, and a pass-through trip neither.
* Amenity-based relocation: the moved end is drawn from the home, work and
  education locations of the EQASIM agents within the radius, weighted by the
  number of agents using them (the STATPOP and STATENT locations EQASIM is
  built from). Without one, a uniform point in the circle is used. The point
  is placed on the nearest drivable edge of the main network within
  ``--snap`` metres; a point without one is drawn again.

Every trip enters on the best lane at the average speed of that lane
(``departLane="best"``, ``departSpeed="avg"``), as in his SUMO set-up.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import math
import random
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

DRAWS = 20


def sample_share(config: Path) -> tuple[float, str]:
    """Sample share of an EQASIM run and the config parameter it was read from."""
    opener = gzip.open if config.suffix == ".gz" else open
    with opener(config, "rt", encoding="utf-8") as handle:
        text = handle.read()
    for name in ("sampleSize", "flowCapacityFactor"):
        match = re.search(rf'name="{name}"\s+value="([0-9.eE+-]+)"', text)
        if match and 0 < float(match.group(1)) <= 1:
            return float(match.group(1)), name
    raise SystemExit(f"{config} has no sampleSize or flowCapacityFactor; pass --scale instead")


class Amenities:
    def __init__(self, path: Path, cell: float):
        self.cell = cell
        self.grid: dict[tuple[int, int], list[tuple[float, float, float]]] = defaultdict(list)
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                x, y = float(row["x"]), float(row["y"])
                self.grid[(int(x // cell), int(y // cell))].append((x, y, float(row["weight"])))

    def near(self, x: float, y: float, radius: float) -> list[tuple[float, float, float]]:
        cx, cy = int(x // self.cell), int(y // self.cell)
        reach = int(math.ceil(radius / self.cell))
        found = []
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for ax, ay, weight in self.grid.get((cx + dx, cy + dy), ()):
                    if math.hypot(ax - x, ay - y) <= radius:
                        found.append((ax, ay, weight))
        return found


class EdgeSnapper:
    """Nearest drivable edge of the main connected network to a point."""

    def __init__(self, net_path: Path):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from plans_to_sumo import SumoNet

        self.net = SumoNet(net_path)

    def nearest(self, x: float, y: float, radius: float) -> str | None:
        cell = self.net.cell
        cx, cy = int(x // cell), int(y // cell)
        reach = int(math.ceil(radius / cell))
        best, best_d = None, radius
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for edge in self.net.grid.get((cx + dx, cy + dy), ()):
                    if edge not in self.net.main:
                        continue
                    shape = self.net.shapes[edge]
                    for (px, py), (qx, qy) in zip(shape, shape[1:]):
                        sx, sy = qx - px, qy - py
                        seg = sx * sx + sy * sy
                        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((x - px) * sx + (y - py) * sy) / seg))
                        d = math.hypot(x - px - t * sx, y - py - t * sy)
                        if d < best_d:
                            best, best_d = edge, d
        return best


def relocate(
    xy: tuple[float, float],
    rng: random.Random,
    radius: float,
    snap: float,
    amenities: Amenities | None,
    snapper: EdgeSnapper,
    counts: dict[str, int],
) -> str | None:
    """Edge of a new end within ``radius`` of ``xy``, drawn from the amenities when there are any."""
    near = amenities.near(*xy, radius) if amenities else []
    for _ in range(DRAWS):
        if near:
            ax, ay, _w = rng.choices(near, weights=[w for _x, _y, w in near])[0]
            source = "amenity"
        else:
            r, angle = radius * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)
            ax, ay = xy[0] + r * math.cos(angle), xy[1] + r * math.sin(angle)
            source = "uniform"
        edge = snapper.nearest(ax, ay, snap)
        if edge:
            counts[source] += 1
            return edge
    counts["kept original"] += 1
    return None


def read_ends(path: Path) -> dict[str, tuple[tuple[float, float] | None, tuple[float, float] | None]]:
    ends = {}
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            start = (float(row["start_x"]), float(row["start_y"])) if row["start_free"] == "1" else None
            end = (float(row["end_x"]), float(row["end_y"])) if row["end_free"] == "1" else None
            ends[row["id"]] = (start, end)
    return ends


def scale_trips(
    root: ET.Element,
    scale: float,
    jitter: float,
    window: float,
    seed: int = 30,
    ends: dict | None = None,
    snapper: EdgeSnapper | None = None,
    amenities: Amenities | None = None,
    radius: float = 500.0,
    snap: float = 150.0,
    depart_lane: str | None = "best",
    depart_speed: str | None = "avg",
) -> tuple[list[ET.Element], list[ET.Element], dict[str, int]]:
    rng = random.Random(seed)
    whole = math.floor(scale)
    fraction = scale - whole
    types = [child for child in root if child.tag == "vType"]
    trips: list[ET.Element] = []
    counts: dict[str, int] = defaultdict(int)
    for trip in root.iter("trip"):
        depart = float(trip.get("depart", "0"))
        copies = whole + (1 if rng.random() < fraction else 0)
        start_xy, end_xy = (ends or {}).get(trip.get("id"), (None, None))
        for k in range(copies):
            clone = ET.Element("trip", dict(trip.attrib))
            depart_k = depart + rng.uniform(-jitter, jitter) if k else depart
            if not 0 <= depart_k < window:
                counts["original outside the window" if not k else "clone outside the window"] += 1
                continue
            if k:
                clone.set("id", f"{trip.get('id')}_x{k}")
                clone.set("depart", f"{depart_k:.2f}")
                if snapper is not None:
                    frm, to = trip.get("from"), trip.get("to")
                    if start_xy:
                        frm = relocate(start_xy, rng, radius, snap, amenities, snapper, counts) or frm
                    if end_xy:
                        to = relocate(end_xy, rng, radius, snap, amenities, snapper, counts) or to
                    if frm != to:
                        clone.set("from", frm)
                        clone.set("to", to)
            counts["original" if not k else "clone"] += 1
            if depart_lane:
                clone.set("departLane", depart_lane)
            if depart_speed:
                clone.set("departSpeed", depart_speed)
            trips.append(clone)
    trips.sort(key=lambda element: float(element.get("depart", "0")))
    return types, trips, counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", type=float, help="Copies of each trip including the original, 1 / sample share")
    parser.add_argument("--matsim-config", type=Path, help="EQASIM output_config.xml(.gz) to read the sample share from")
    parser.add_argument("--jitter", type=float, default=1800.0, help="Departure offset of a clone, ± seconds")
    parser.add_argument("--window", type=float, default=3600.0, help="Simulated window in seconds")
    parser.add_argument("--endpoints", type=Path, help="Trip ends written by plans_to_sumo.py --endpoints")
    parser.add_argument("--amenities", type=Path, help="Locations written by plans_to_sumo.py --amenities")
    parser.add_argument("--net", type=Path, help="SUMO network, needed to move trip ends")
    parser.add_argument("--radius", type=float, default=500.0, help="Spatial jitter radius in metres")
    parser.add_argument("--snap", type=float, default=150.0, help="Largest distance from a new end to its edge")
    parser.add_argument("--seed", type=int, default=30)
    args = parser.parse_args(argv)

    if args.scale is None:
        if args.matsim_config is None:
            raise SystemExit("pass --scale or --matsim-config")
        share, name = sample_share(args.matsim_config)
        args.scale = 1.0 / share
        print(f"sample share {share:g} ({name} in {args.matsim_config.name}), scale factor {args.scale:g}")
    if args.scale < 1:
        raise SystemExit("--scale must be at least 1")
    if bool(args.endpoints) != bool(args.net):
        raise SystemExit("--endpoints and --net go together")

    ends = read_ends(args.endpoints) if args.endpoints else None
    amenities = Amenities(args.amenities, args.radius) if args.amenities else None
    snapper = None
    if args.net:
        print(f"reading {args.net} to place moved trip ends", flush=True)
        snapper = EdgeSnapper(args.net)
    root = ET.parse(args.input).getroot()
    types, trips, counts = scale_trips(
        root, args.scale, args.jitter, args.window, args.seed, ends, snapper, amenities, args.radius, args.snap
    )
    out = ET.Element("routes")
    out.extend(types + trips)
    ET.indent(out, space="    ")
    ET.ElementTree(out).write(args.output, encoding="utf-8", xml_declaration=True)
    inside = counts["original"]
    print(f"scaled {inside} trips in the window by {args.scale:g} to {len(trips)} trips in {args.output}")
    print("  " + ", ".join(f"{name} {value}" for name, value in sorted(counts.items())))
    if not ends:
        print("  trip ends were not moved: pass --endpoints, --amenities and --net for the spatial perturbation")


if __name__ == "__main__":
    main()
