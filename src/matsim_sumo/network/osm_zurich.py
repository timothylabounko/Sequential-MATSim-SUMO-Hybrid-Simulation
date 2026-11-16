"""Build a SUMO road network for the City of Zürich from OpenStreetMap."""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "matsim-sumo-pipeline/zurich-osm (SUMO network extract)"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Administrative bounding box of Zürich (OSM relation 1682248), west/south/east/north.
ZURICH_BBOX = {
    "west": 8.4480182,
    "south": 47.3202184,
    "east": 8.6254529,
    "north": 47.4346650,
}
# Highways that are not part of the drivable road network shown in SUMO.
EXCLUDED_HIGHWAYS = (
    "footway|path|cycleway|steps|pedestrian|bridleway|corridor|proposed|"
    "construction|raceway|platform|service|track|bus_guideway|escape"
)
DEFAULT_OUTPUT = Path("networks/zurich/zurich.net.xml")
# Join a tram way into the road lane only when the two geometries are this close.
# On a Stauffacher sample, 2 m joined embedded street-running track and left
# reserved track separate. 8 m also joined tram ways into roads that are not
# tagged embedded_rails, which lets cars onto reserved track.
TRAM_JOIN_METERS = 2.0
# Vehicle classes added after SUMO 1.18. Leaving them in the network makes older
# sumo-gui builds refuse to open the file.
_SUMO_118_UNKNOWN_VCLASSES = (
    "container",
    "cable_car",
    "subway",
    "aircraft",
    "wheelchair",
    "scooter",
    "drone",
)


def _has_typemap(home: Path) -> bool:
    return (home / "data" / "typemap" / "osmNetconvert.typ.xml").is_file()


def find_sumo_home() -> Path:
    """Prefer SUMO_HOME, then the eclipse-sumo wheel, then a system install.

    Ubuntu's SUMO 1.18 netconvert aborts while importing this Zürich extract.
    A current SUMO, including the ``eclipse-sumo`` wheel, completes the import.
    """
    candidates: list[Path] = []
    env_home = os.environ.get("SUMO_HOME")
    if env_home:
        candidates.append(Path(env_home))
    try:
        import sumo
    except ImportError:
        pass
    else:
        candidates.append(Path(sumo.__file__).resolve().parent)
    candidates.extend((Path("/usr/local/share/sumo"), Path("/usr/share/sumo")))
    for home in candidates:
        if _has_typemap(home):
            return home
    raise SystemExit(
        "SUMO typemaps were not found. Install SUMO (pip install eclipse-sumo, or a current "
        "SUMO package) and set SUMO_HOME to that installation."
    )


def find_netconvert(sumo_home: Path) -> str:
    bundled = sumo_home / "bin" / "netconvert"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    found = shutil.which("netconvert")
    if found:
        return found
    raise SystemExit("netconvert is not on PATH. Install SUMO and set SUMO_HOME.")


def zurich_overpass_query(bbox: dict[str, float] | None = None) -> str:
    box = bbox or ZURICH_BBOX
    # Overpass order is south,west,north,east.
    coords = f"{box['south']},{box['west']},{box['north']},{box['east']}"
    return f"""[out:xml][timeout:300];
(
  way["highway"]["highway"!~"{EXCLUDED_HIGHWAYS}"]({coords});
  way["railway"="tram"]({coords});
  way["railway"="light_rail"]({coords});
  relation["type"="restriction"]({coords});
);
(._;>;);
out meta;
"""


def merge_missing_osm_elements(base: Path, extra: Path, destination: Path) -> tuple[int, int]:
    """Copy ``base`` and append nodes and ways from ``extra`` whose ids are absent.

    Used to add ``railway=tram`` ways to a road extract without downloading the
    roads again. Restriction relations already in ``base`` are kept.
    """
    present_nodes: set[str] = set()
    present_ways: set[str] = set()
    for _event, elem in ET.iterparse(base, events=("end",)):
        if elem.tag == "node" and elem.get("id"):
            present_nodes.add(elem.get("id") or "")
        elif elem.tag == "way" and elem.get("id"):
            present_ways.add(elem.get("id") or "")
        elem.clear()
    new_nodes: list[str] = []
    new_ways: list[str] = []
    # Clear only finished nodes and ways. Clearing a child ``nd`` or ``tag``
    # first would drop its attributes before the parent is serialized.
    for _event, elem in ET.iterparse(extra, events=("end",)):
        if elem.tag not in {"node", "way"}:
            continue
        element_id = elem.get("id") or ""
        if elem.tag == "node" and element_id and element_id not in present_nodes:
            new_nodes.append(ET.tostring(elem, encoding="unicode"))
        elif elem.tag == "way" and element_id and element_id not in present_ways:
            new_ways.append(ET.tostring(elem, encoding="unicode"))
        elem.clear()
    text = base.read_text(encoding="utf-8")
    if "</osm>" not in text:
        raise SystemExit(f"{base} is not an OSM document")
    # netconvert reads nodes, then ways, then relations. Ways appended after the
    # relations are ignored, so tram nodes go before the first node and tram
    # ways go before the first relation.
    if new_nodes:
        anchor = text.find("<node ")
        if anchor < 0:
            anchor = text.find("</osm>")
        text = text[:anchor] + "".join(new_nodes) + text[anchor:]
    if new_ways:
        anchor = text.find("<relation ")
        if anchor < 0:
            anchor = text.find("</osm>")
        text = text[:anchor] + "".join(new_ways) + text[anchor:]
    destination.write_text(text, encoding="utf-8")
    return len(new_nodes), len(new_ways)


def _http_post(url: str, data: dict[str, str], timeout: int) -> bytes:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_zurich_osm(destination: Path, bbox: dict[str, float] | None = None) -> None:
    query = zurich_overpass_query(bbox)
    last_error = "no Overpass endpoint responded"
    destination.parent.mkdir(parents=True, exist_ok=True)
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(1, 4):
            print(f"Downloading Zürich OSM roads from {endpoint} (attempt {attempt})...", flush=True)
            try:
                raw = _http_post(endpoint, {"data": query}, timeout=320)
            except urllib.error.HTTPError as exc:
                last_error = f"{endpoint} HTTP {exc.code}"
                print(last_error, flush=True)
                time.sleep(3 * attempt)
                continue
            except urllib.error.URLError as exc:
                last_error = f"{endpoint} {exc.reason}"
                print(last_error, flush=True)
                time.sleep(3 * attempt)
                continue
            text = raw.decode("utf-8", errors="replace")
            if "<osm" not in text[:2000]:
                snippet = " ".join(text[:240].split())
                last_error = f"{endpoint} returned a non-OSM response: {snippet}"
                print(last_error, flush=True)
                time.sleep(3 * attempt)
                continue
            destination.write_bytes(raw)
            print(f"Wrote {destination} ({destination.stat().st_size / 1_048_576:.1f} MiB)", flush=True)
            return
    raise SystemExit(f"Could not download the Zürich OSM extract: {last_error}")


def _typemaps(sumo_home: Path, german_urban_speeds: bool = True) -> str:
    names = ("osmNetconvert.typ.xml", "osmNetconvertUrbanDe.typ.xml") if german_urban_speeds else ("osmNetconvert.typ.xml",)
    paths = [sumo_home / "data" / "typemap" / name for name in names]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("Missing SUMO typemap files:\n" + "\n".join(missing))
    return ",".join(str(path) for path in paths)


def convert_osm_to_sumo(
    osm_path: Path,
    net_path: Path,
    log_path: Path,
    sumo_home: Path,
    lefthand: bool = False,
    german_urban_speeds: bool = True,
) -> None:
    netconvert = find_netconvert(sumo_home)
    net_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        netconvert,
        "--osm-files",
        str(osm_path),
        "--output-file",
        str(net_path),
        "--type-files",
        _typemaps(sumo_home, german_urban_speeds),
        "--geometry.remove",
        "--roundabouts.guess",
        "--ramps.guess",
        "--junctions.join",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        "--tls.default-type",
        "actuated",
        "--osm.turn-lanes",
        "--no-turnarounds.except-deadend",
        "--output.street-names",
        "--output.original-names",
        "--remove-edges.isolated",
        "--keep-edges.by-vclass",
        "passenger,tram",
        "--edges.join-tram-dist",
        str(TRAM_JOIN_METERS),
    ]
    if lefthand:
        command.append("--lefthand")
    print("Running netconvert...", flush=True)
    completed = subprocess.run(command, text=True, capture_output=True)
    log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not net_path.is_file() or net_path.stat().st_size == 0:
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"netconvert failed. See {log_path}")
    print(f"Wrote {net_path}", flush=True)


def edges_outside_bbox(net, bbox: dict[str, float]) -> list[str]:
    """Drop streets Overpass returned in full when most of the street is outside Zürich."""
    west, south, east, north = bbox["west"], bbox["south"], bbox["east"], bbox["north"]

    def inside(lon: float, lat: float) -> bool:
        return west <= lon <= east and south <= lat <= north

    drop: list[str] = []
    for edge in net.getEdges():
        if edge.getFunction():
            continue
        shape = edge.getShape()
        if len(shape) < 2:
            lon, lat = net.convertXY2LonLat(shape[0][0], shape[0][1])
            if not inside(lon, lat):
                drop.append(edge.getID())
            continue
        total = 0.0
        segments: list[tuple[float, float, float, float, float]] = []
        for (x1, y1), (x2, y2) in zip(shape, shape[1:]):
            length = math.hypot(x2 - x1, y2 - y1)
            segments.append((x1, y1, x2, y2, length))
            total += length
        if total == 0:
            lon, lat = net.convertXY2LonLat(shape[0][0], shape[0][1])
            if not inside(lon, lat):
                drop.append(edge.getID())
            continue
        target = total / 2.0
        walked = 0.0
        mid_x, mid_y = shape[0]
        for x1, y1, x2, y2, length in segments:
            if walked + length >= target:
                fraction = 0.0 if length == 0 else (target - walked) / length
                mid_x = x1 + fraction * (x2 - x1)
                mid_y = y1 + fraction * (y2 - y1)
                break
            walked += length
        mid_lon, mid_lat = net.convertXY2LonLat(mid_x, mid_y)
        outside = 0.0
        for x1, y1, x2, y2, length in segments:
            lon, lat = net.convertXY2LonLat((x1 + x2) / 2.0, (y1 + y2) / 2.0)
            if not inside(lon, lat):
                outside += length
        if not inside(mid_lon, mid_lat) or outside / total > 0.5:
            drop.append(edge.getID())
    return drop


def clip_network_to_bbox(net_path: Path, bbox: dict[str, float], log_path: Path, sumo_home: Path) -> int:
    try:
        import pyproj  # noqa: F401  (sumolib inverts the network projection with pyproj)
    except ImportError as exc:
        raise SystemExit("pyproj is required to clip the Zürich network. Install the package dependencies.") from exc

    sys.path.insert(0, str(sumo_home / "tools"))
    import sumolib

    net = sumolib.net.readNet(str(net_path))
    drop = edges_outside_bbox(net, bbox)
    if not drop:
        return 0
    drop_path = net_path.with_name(f"{net_path.name}.outside.txt")
    clipped_path = net_path.with_name(f"{net_path.name}.clipped.xml")
    drop_path.write_text("\n".join(drop) + "\n", encoding="utf-8")
    netconvert = find_netconvert(sumo_home)
    command = [
        netconvert,
        "--sumo-net-file",
        str(net_path),
        "--remove-edges.input-file",
        str(drop_path),
        "--remove-edges.isolated",
        "--output.street-names",
        "--output-file",
        str(clipped_path),
    ]
    print(f"Removing {len(drop)} edges outside the Zürich boundary...", flush=True)
    completed = subprocess.run(command, text=True, capture_output=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n---- clip to Zurich bbox ----\n")
        log.write(completed.stdout)
        log.write(completed.stderr)
    drop_path.unlink(missing_ok=True)
    if completed.returncode != 0 or not clipped_path.is_file():
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"netconvert could not clip the network. See {log_path}")
    clipped_path.replace(net_path)
    return len(drop)


def strip_newer_vclasses(net_xml: str) -> str:
    """Remove vehicle classes that SUMO 1.18 cannot parse."""
    text = net_xml
    for name in _SUMO_118_UNKNOWN_VCLASSES:
        text = re.sub(rf"\b{name}\b ?", "", text)
    return re.sub(
        r'(allow|disallow)="([^"]*)"',
        lambda match: f'{match.group(1)}="{" ".join(match.group(2).split())}"',
        text,
    )


def extract_zurich_network(
    output_path: Path,
    sumo_home: Path | None = None,
    bbox: dict[str, float] | None = None,
    lefthand: bool = False,
    german_urban_speeds: bool = True,
) -> Path:
    """Download the road network of ``bbox`` (default: the City of Zürich) and write a SUMO .net.xml file."""
    home = sumo_home or find_sumo_home()
    os.environ["SUMO_HOME"] = str(home)
    box = bbox or ZURICH_BBOX
    output_path = Path(output_path)
    osm_path = output_path.with_name(output_path.name.replace(".net.xml", "") + ".osm.xml")
    log_path = output_path.with_name(output_path.name + ".netconvert.log")
    download_zurich_osm(osm_path, box)
    convert_osm_to_sumo(osm_path, output_path, log_path, home, lefthand, german_urban_speeds)
    removed = clip_network_to_bbox(output_path, box, log_path, home)
    output_path.write_text(strip_newer_vclasses(output_path.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"Removed {removed} edges outside the area.", flush=True)
    print(f"SUMO network: {output_path.resolve()}", flush=True)
    return output_path


def parse_bbox(text: str) -> dict[str, float]:
    """``west,south,east,north`` in degrees."""
    values = [float(part) for part in text.split(",")]
    if len(values) != 4 or not (values[0] < values[2] and values[1] < values[3]):
        raise argparse.ArgumentTypeError("--bbox is west,south,east,north with west < east and south < north")
    return dict(zip(("west", "south", "east", "north"), values))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Download a city's road network from OpenStreetMap and write a SUMO .net.xml file "
        "(default: the City of Zürich)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"SUMO network path (default: {DEFAULT_OUTPUT}); the OSM extract is written beside it",
    )
    parser.add_argument("--bbox", type=parse_bbox, help="Area as west,south,east,north (default: Zürich)")
    parser.add_argument("--lefthand", action="store_true", help="Left-hand traffic")
    parser.add_argument(
        "--no-german-urban-speeds",
        action="store_true",
        help="Leave out SUMO's German urban speed type map (osmNetconvertUrbanDe)",
    )
    args = parser.parse_args(argv)
    extract_zurich_network(
        args.output, bbox=args.bbox, lefthand=args.lefthand, german_urban_speeds=not args.no_german_urban_speeds
    )


if __name__ == "__main__":
    main()
