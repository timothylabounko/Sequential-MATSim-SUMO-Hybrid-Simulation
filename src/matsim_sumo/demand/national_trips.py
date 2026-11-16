"""Turn the EQASIM ``output_trips.csv.gz`` table into SUMO car trips.

The table is the one in ``~/Downloads/National``. Rows are the simulated
trips, not a random sample. Only ``main_mode`` car is kept. Coordinates are
Swiss LV95 and are placed on the SUMO network, which is stored in UTM zone 32.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import math
from pathlib import Path


def parse_depart(value: str) -> float | None:
    """Return seconds after midnight, from ``HH:MM:SS`` or a numeric second count."""
    text = (value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 3:
            return None
        try:
            hour, minute, second = (float(part) for part in parts)
        except ValueError:
            return None
        if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60:
            return None
        return hour * 3600 + minute * 60 + second
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def parse_clock(value: str) -> float:
    """Parse a clock time used as a window bound."""
    seconds = parse_depart(value if ":" in value else value)
    if seconds is None:
        raise ValueError(f"Not a clock time: {value}")
    return seconds


def lv95_to_wgs84(east: float, north: float) -> tuple[float, float]:
    """Approximate LV95 metres as latitude, longitude (swisstopo)."""
    y = (east - 2_600_000) / 1_000_000
    x = (north - 1_200_000) / 1_000_000
    lon = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y**3
    lat = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x**3
    return lat * 100 / 36, lon * 100 / 36


def wgs84_to_utm32(lat: float, lon: float) -> tuple[float, float]:
    """Project WGS84 degrees into UTM zone 32 north, in metres."""
    a = 6378137.0
    f = 1 / 298.257223563
    k0 = 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lon0 = math.radians((32 - 1) * 6 - 180 + 3)
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    normal = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    tan2 = math.tan(lat_r) ** 2
    curve = ep2 * math.cos(lat_r) ** 2
    along = (lon_r - lon0) * math.cos(lat_r)
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


def lv95_to_sumo(east: float, north: float, net_offset: tuple[float, float]) -> tuple[float, float]:
    """Place an LV95 point into the SUMO network's coordinate system."""
    lat, lon = lv95_to_wgs84(east, north)
    utm_east, utm_north = wgs84_to_utm32(lat, lon)
    return utm_east + net_offset[0], utm_north + net_offset[1]


def _cell(row: dict[str, str], *names: str) -> str:
    """Return the first populated column, ignoring capitalisation and a BOM."""
    wanted = {name.lower() for name in names}
    for key, value in row.items():
        if key is None:
            continue
        if key.replace("\ufeff", "").strip().lower() in wanted and (value or "").strip():
            return value.strip()
    return ""


def car_trip_in_window(row: dict[str, str], start_s: float, end_s: float) -> float | None:
    """Return the departure second when the row is a car trip inside the window.

    EQASIM writes either MATSim names (``main_mode``, ``dep_time``) or the
    analysis names (``mode``, ``departure_time``). Both are accepted.
    """
    mode = _cell(row, "main_mode", "mode").lower()
    if mode != "car":
        return None
    depart = parse_depart(_cell(row, "dep_time", "departure_time", "departure"))
    if depart is None or not start_s <= depart < end_s:
        return None
    return depart


def trip_xy(row: dict[str, str]) -> tuple[float, float, float, float] | None:
    """Return start and end coordinates from either MATSim or EQASIM column names."""
    try:
        start_x = float(_cell(row, "start_x", "origin_x"))
        start_y = float(_cell(row, "start_y", "origin_y"))
        end_x = float(_cell(row, "end_x", "destination_x"))
        end_y = float(_cell(row, "end_y", "destination_y"))
    except ValueError:
        return None
    return start_x, start_y, end_x, end_y


def to_sumo_xy(x: float, y: float, net_offset: tuple[float, float]) -> tuple[float, float]:
    """Place a trip endpoint on the SUMO network.

    Values near 8, 47 are longitude and latitude. Values under 1.5 million are
    the older Swiss LV03 grid and are shifted into LV95. Larger values are LV95.
    """
    if -180 <= x <= 180 and -90 <= y <= 90:
        utm_east, utm_north = wgs84_to_utm32(y, x)
        return utm_east + net_offset[0], utm_north + net_offset[1]
    east, north = (x, y) if x >= 1_500_000 else (x + 2_000_000, y + 1_000_000)
    return lv95_to_sumo(east, north, net_offset)


def shift_depart(depart: float, start_s: float) -> float:
    """Seconds after the window start. 07:00:00 in a 07:00 window is 0, and 07:00:01 is 1."""
    return depart - start_s


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay)
    share = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + share * dx), py - (ay + share * dy))


def _shape_distance(px: float, py: float, shape: list[tuple[float, float]]) -> float:
    if len(shape) == 1:
        return math.hypot(px - shape[0][0], py - shape[0][1])
    return min(_segment_distance(px, py, x1, y1, x2, y2) for (x1, y1), (x2, y2) in zip(shape, shape[1:]))


class _EdgeIndex:
    def __init__(self, edges: list[tuple[str, list[tuple[float, float]]]], cell: float = 200.0):
        self.cell = cell
        self.shapes = {edge_id: shape for edge_id, shape in edges}
        self.buckets: dict[tuple[int, int], list[str]] = {}
        for edge_id, shape in edges:
            xs = [point[0] for point in shape]
            ys = [point[1] for point in shape]
            for ix in range(math.floor(min(xs) / cell), math.floor(max(xs) / cell) + 1):
                for iy in range(math.floor(min(ys) / cell), math.floor(max(ys) / cell) + 1):
                    self.buckets.setdefault((ix, iy), []).append(edge_id)

    def nearest(self, x: float, y: float, radius: float) -> str | None:
        span = math.floor(radius / self.cell) + 1
        ix = math.floor(x / self.cell)
        iy = math.floor(y / self.cell)
        best_id = None
        best_distance = radius
        seen: set[str] = set()
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for edge_id in self.buckets.get((ix + dx, iy + dy), ()):
                    if edge_id in seen:
                        continue
                    seen.add(edge_id)
                    distance = _shape_distance(x, y, self.shapes[edge_id])
                    if distance < best_distance:
                        best_id = edge_id
                        best_distance = distance
        return best_id


def _passenger_shapes(net) -> list[tuple[str, list[tuple[float, float]]]]:
    shapes = []
    for edge in net.getEdges():
        if edge.getID().startswith(":") or edge.getFunction() not in ("", "normal") or not edge.allows("passenger"):
            continue
        shapes.append((edge.getID(), list(edge.getShape())))
    return shapes


def _open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", newline="", encoding="utf-8", errors="replace")
    return path.open("rt", newline="", encoding="utf-8", errors="replace")


def write_national_trips(
    trips_path: Path,
    net,
    out_path: Path,
    *,
    start_s: float,
    end_s: float,
    radius_m: float = 80.0,
    shift_to_zero: bool = True,
) -> dict[str, int]:
    """Write one SUMO trip per car row in the EQASIM table that lands on the network.

    With ``shift_to_zero`` (the default), ``depart`` is seconds after ``start_s``,
    so a 07:00:00 trip in a 07:00 window is written as ``depart="0"``.
    """
    offset = net.getLocationOffset()
    index = _EdgeIndex(_passenger_shapes(net))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = unmatched = 0
    with _open_text(trips_path) as src, out_path.open("w", encoding="utf-8", newline="\n") as dst:
        reader = csv.DictReader(src, delimiter=";")
        dst.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        dst.write('    <vType id="car" vClass="passenger"/>\n')
        for row_number, row in enumerate(reader, start=1):
            depart = car_trip_in_window(row, start_s, end_s)
            if depart is None:
                continue
            coords = trip_xy(row)
            if coords is None:
                unmatched += 1
                continue
            origin = to_sumo_xy(coords[0], coords[1], offset)
            destination = to_sumo_xy(coords[2], coords[3], offset)
            from_edge = index.nearest(*origin, radius_m)
            to_edge = index.nearest(*destination, radius_m)
            if from_edge is None or to_edge is None or from_edge == to_edge:
                unmatched += 1
                continue
            trip_id = (row.get("trip_id") or row.get("person") or f"trip{row_number}").strip()
            trip_number = (row.get("trip_number") or str(row_number)).strip()
            shown = shift_depart(depart, start_s) if shift_to_zero else depart
            dst.write(
                f'    <trip id="{trip_id}_{trip_number}" depart="{shown:.0f}" '
                f'from="{from_edge}" to="{to_edge}" type="car"/>\n'
            )
            written += 1
        dst.write("</routes>\n")
    return {"written": written, "unmatched": unmatched}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Write SUMO trips from the National EQASIM output_trips table. No random trips are generated."
    )
    parser.add_argument("--trips", type=Path, default=Path.home() / "Downloads" / "National" / "output_trips.csv.gz")
    parser.add_argument("--net", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--from", dest="start", default="07:00:00", help="First departure kept, clock time")
    parser.add_argument("--to", dest="end", default="08:00:00", help="Departure at this time is not kept")
    parser.add_argument("--radius", type=float, default=250.0, help="Maximum snap distance in metres")
    parser.add_argument(
        "--shift-to-zero",
        action="store_true",
        default=True,
        help="Write depart as seconds after --from. This is the default.",
    )
    parser.add_argument(
        "--keep-clock",
        action="store_true",
        help="Keep departures as seconds after midnight instead of shifting them to 0.",
    )
    args = parser.parse_args(argv)
    if not args.trips.is_file():
        raise SystemExit(f"Trip table not found: {args.trips}")
    import sumolib

    net = sumolib.net.readNet(str(args.net))
    counts = write_national_trips(
        args.trips,
        net,
        args.output,
        start_s=parse_clock(args.start),
        end_s=parse_clock(args.end),
        radius_m=args.radius,
        shift_to_zero=not args.keep_clock,
    )
    _write_view(args.output.with_name("view.settings.xml"), net)
    clock = "departures start at 0" if not args.keep_clock else "departures stay seconds after midnight"
    print(
        f"Wrote {counts['written']} car trips to {args.output} ({clock}; "
        f"{counts['unmatched']} car trips had no road within range)"
    )
    if counts["written"] == 0:
        raise SystemExit("No car trips were written, so SUMO would show an empty road.")


def _write_view(path: Path, net) -> None:
    """Open the GUI zoomed on the centre of the network, with cars drawn large."""
    xmin, ymin, xmax, ymax = net.getBoundary()
    center_x = (xmin + xmax) / 2
    center_y = (ymin + ymax) / 2
    path.write_text(
        "\n".join(
            [
                "<viewsettings>",
                f'    <viewport zoom="180" x="{center_x:.1f}" y="{center_y:.1f}"/>',
                '    <scheme name="real world">',
                '        <vehicles vehicleQuality="2" vehicle_exaggeration="12"/>',
                "    </scheme>",
                "</viewsettings>",
                "",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
