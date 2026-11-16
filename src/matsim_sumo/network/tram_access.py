"""Decide when a car may use a lane that also carries a tram.

Zurich maps the two cases as different OpenStreetMap objects. A reserved track is
its own ``railway=tram`` way, and cars are not allowed on it. Street-running track
is a separate tram way drawn on top of a road tagged ``embedded_rails=tram``, and
cars are allowed on that road. A lane is edited only when every way it cites
agrees. A reserved track joined into a road that is not tagged as embedded is
reported, unless another lane on the same edge still carries the cars.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

DRIVABLE_HIGHWAYS = frozenset(
    {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "living_street",
        "busway",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    }
)
# Private motor vehicles. Buses and emergency vehicles are left as imported.
CAR_CLASSES = (
    "passenger",
    "taxi",
    "hov",
    "motorcycle",
    "moped",
    "truck",
    "trailer",
    "delivery",
    "evehicle",
    "private",
    "custom1",
    "custom2",
)
CAR_CLASS_SET = frozenset(CAR_CLASSES)
TRAM_RAILWAYS = frozenset({"tram", "light_rail"})
EMBEDDED_VALUES = frozenset({"tram", "light_rail", "yes"})
RESTRICTED_ACCESS = frozenset({"destination", "customers", "delivery", "permissive", "permit", "designated"})
HARD_NO_ACCESS = frozenset({"no", "private"})


@dataclass(frozen=True)
class LaneDecision:
    edge_id: str
    lane_index: int
    lane_id: str
    action: str
    street: str
    evidence: str


def orig_way_ids(raw: str) -> list[str]:
    """OSM way ids stored by netconvert in a lane's ``origId`` parameter."""
    found: list[str] = []
    for part in (raw or "").replace(",", " ").split():
        way_id = part.split("#", 1)[0]
        if way_id.startswith("-"):
            way_id = way_id[1:]
        if way_id.isdigit() and way_id not in found:
            found.append(way_id)
    return found


def motor_restriction(tags: dict[str, str]) -> str:
    """Return ``yes``, ``no``, or ``restricted`` for ordinary car traffic."""
    motor_vehicle = tags.get("motor_vehicle", "")
    motorcar = tags.get("motorcar", "")
    if motor_vehicle in {"yes", "designated", "permissive"} or motorcar in {"yes", "designated", "permissive"}:
        return "yes"
    values = [tags.get(key, "") for key in ("motor_vehicle", "motorcar", "vehicle", "access")]
    values = [value for value in values if value]
    if any(value in HARD_NO_ACCESS for value in values):
        return "no"
    if any(value in RESTRICTED_ACCESS for value in values):
        return "restricted"
    return "yes"


def classify_way(tags: dict[str, str]) -> str:
    """Classify one OSM way. ``other`` means the way says nothing about trams."""
    if not tags:
        return "other"
    if any(tags.get(key) for key in ("embedded_rails:lanes", "embedded_rails:lanes:forward", "embedded_rails:lanes:backward")):
        return "restricted"
    railway = tags.get("railway", "")
    highway = tags.get("highway", "")
    embedded = tags.get("embedded_rails", "")
    tram_related = railway in TRAM_RAILWAYS or embedded in EMBEDDED_VALUES
    drivable = highway in DRIVABLE_HIGHWAYS
    if highway == "service":
        return "restricted"
    access = motor_restriction(tags)
    if tram_related and access == "restricted":
        return "restricted"
    if railway in TRAM_RAILWAYS and not drivable:
        return "exclusive_tram"
    if drivable and access == "no" and tram_related:
        return "forbidden"
    if drivable and access != "no" and (embedded in EMBEDDED_VALUES or railway in TRAM_RAILWAYS):
        return "shared_road"
    if drivable:
        return "car_road"
    return "other"


def decide_lane(
    kinds: list[str],
    *,
    allows_passenger: bool,
    allows_tram: bool,
    edge_type: str = "",
    other_lane_keeps_cars: bool = False,
) -> str:
    """Return ``keep``, ``remove_cars``, ``add_tram``, ``add_cars``, or ``suspect``.

    ``remove_cars`` is the reserved track that currently admits cars.
    ``add_tram`` and ``add_cars`` are the embedded street, where both are allowed.
    ``suspect`` is a reserved track joined into a road that is not tagged as embedded.
    """
    relevant = set(kinds)
    tram_type = edge_type.startswith("railway.tram") or edge_type.startswith("railway.light_rail")
    tram_related = bool(relevant & {"exclusive_tram", "shared_road", "forbidden"}) or allows_tram or tram_type
    if not tram_related:
        return "keep"
    if "restricted" in relevant:
        return "suspect"
    exclusive = "exclusive_tram" in relevant or (tram_type and not (relevant & {"shared_road", "car_road"}))
    shared = "shared_road" in relevant
    car = "car_road" in relevant
    forbidden = "forbidden" in relevant
    if exclusive and not shared and not car:
        return "remove_cars" if allows_passenger else "keep"
    if forbidden and not shared and not car:
        return "remove_cars" if allows_passenger else "keep"
    if shared and exclusive:
        # The reserved-track way was joined into this road lane, so both classes belong here.
        if allows_passenger and allows_tram:
            return "keep"
        if not allows_tram:
            return "add_tram"
        return "add_cars"
    if shared:
        # The tram has its own way. Cars stay on this road; tram permission is not copied onto it.
        if not allows_passenger:
            return "add_cars"
        return "keep"
    if exclusive and car:
        if other_lane_keeps_cars and allows_passenger:
            return "remove_cars"
        return "suspect"
    if tram_type and allows_passenger:
        return "remove_cars"
    return "keep"


def exclusive_tram_crossing(from_passenger: bool, from_tram: bool, to_passenger: bool, to_tram: bool) -> bool:
    """True when a connection joins a car-only lane to a tram-only lane, or the reverse.

    A shared lane allows both classes, so a tram leaving the street onto a
    reserved track is kept. A car turning from the street onto that track is not.
    """
    car_onto_track = from_passenger and not from_tram and to_tram and not to_passenger
    track_onto_car_only = from_tram and not from_passenger and to_passenger and not to_tram
    return car_onto_track or track_onto_car_only


def rewrite_lane_attributes(allow: str | None, disallow: str | None, action: str) -> tuple[str | None, str | None]:
    """Return new ``allow`` and ``disallow`` values for one lane.

    ``None`` means the attribute should be removed. An empty string is not written.
    """
    if action not in {"remove_cars", "add_tram", "add_cars"}:
        raise ValueError(f"unknown lane action {action}")
    allowed = [part for part in (allow or "").split() if part]
    banned = [part for part in (disallow or "").split() if part]
    if action == "remove_cars":
        if allowed:
            allowed = [name for name in allowed if name not in CAR_CLASS_SET]
            if "tram" not in allowed:
                allowed.append("tram")
        else:
            for name in CAR_CLASSES:
                if name not in banned:
                    banned.append(name)
            banned = [name for name in banned if name != "tram"]
    elif action == "add_tram":
        if allowed and "tram" not in allowed:
            allowed.append("tram")
        banned = [name for name in banned if name != "tram"]
    else:
        if allowed and "passenger" not in allowed:
            allowed.append("passenger")
        banned = [name for name in banned if name != "passenger"]
    allow_value = " ".join(allowed) or None
    disallow_value = " ".join(banned) or None
    return allow_value, disallow_value


def parse_way_kinds(osm_path: Path) -> dict[str, str]:
    """Map OSM way id to the tram-access class. Ways of class ``other`` are omitted."""
    kinds: dict[str, str] = {}
    for _event, elem in ET.iterparse(osm_path, events=("end",)):
        if elem.tag != "way":
            continue
        tags = {tag.get("k") or "": tag.get("v") or "" for tag in elem.findall("tag")}
        kind = classify_way(tags)
        way_id = elem.get("id") or ""
        if way_id and kind != "other":
            kinds[way_id] = kind
        elem.clear()
    return kinds


def apply_lane_permissions(source: Path, destination: Path, decisions: list[LaneDecision]) -> int:
    """Write ``source`` with lane permissions changed for ``decisions``."""
    by_id = {decision.lane_id: decision for decision in decisions}
    tree = ET.parse(source)
    changed = 0
    for lane in tree.iter("lane"):
        decision = by_id.get(lane.get("id") or "")
        if decision is None:
            continue
        allow, disallow = rewrite_lane_attributes(lane.get("allow"), lane.get("disallow"), decision.action)
        if allow is None:
            lane.attrib.pop("allow", None)
        else:
            lane.set("allow", allow)
        if disallow is None:
            lane.attrib.pop("disallow", None)
        else:
            lane.set("disallow", disallow)
        changed += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="UTF-8", xml_declaration=True)
    return changed


def final_lane_flags(allows_passenger: bool, allows_tram: bool, action: str | None) -> tuple[bool, bool]:
    """Permissions after a decision. ``keep`` and ``suspect`` change nothing."""
    if action == "remove_cars":
        return False, True
    if action == "add_tram":
        return allows_passenger, True
    if action == "add_cars":
        return True, True
    return allows_passenger, allows_tram


def point_to_polyline_meters(lat: float, lon: float, line: list[tuple[float, float]]) -> float | None:
    """Minimum distance in metres from one WGS84 point to a lon/lat polyline."""
    if len(line) < 2:
        return None
    best = math.inf
    for (lon1, lat1), (lon2, lat2) in zip(line, line[1:]):
        best = min(best, _segment_distance_meters(lat, lon, lat1, lon1, lat2, lon2))
    return best


def _segment_distance_meters(
    lat: float,
    lon: float,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    # Equirectangular projection around the point. Accurate enough for a few kilometres.
    scale_lat = 111_320.0
    scale_lon = 111_320.0 * math.cos(math.radians(lat))
    px, py = 0.0, 0.0
    ax, ay = (lon1 - lon) * scale_lon, (lat1 - lat) * scale_lat
    bx, by = (lon2 - lon) * scale_lon, (lat2 - lat) * scale_lat
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(ax - px, ay - py)
    fraction = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    nearest_x = ax + fraction * dx
    nearest_y = ay + fraction * dy
    return math.hypot(nearest_x - px, nearest_y - py)
