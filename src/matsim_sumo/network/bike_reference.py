"""Official bicycle geometry used instead of OpenStreetMap or Google.

The Zurich routenplaner (``NETZ_VELO`` in ``Routing.gdb``) is the ridable
graph: one-way direction, a banned direction, cycle track, bike lane, speed
and the turn bans in ``NETZ_VELO_ABBIEGEVERBOTE``. An Aargau line file can
add the pieces that leave the city, clipped to a margin around that graph.

``velonetz1.xml`` from the Aargau open-data catalog is a DCAT description of
the cantonal Velonetz, not the lines. Passing it raises ``CatalogOnly``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

# Same conversion as scripts/plans_to_sumo.py, so a bike edge and a MATSim
# link land in one SUMO coordinate system (UTM zone 32, netOffset 0).
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


def lv95_to_sumo(east: float, north: float) -> tuple[float, float]:
    return wgs84_to_utm32(*lv95_to_wgs84(east, north))


DEFAULT_SPEED_MPS = 4.2
AARGAU_MARGIN_M = 5000.0


class CatalogOnly(ValueError):
    """The file describes a dataset and does not contain the network lines."""


@dataclass(frozen=True)
class BikeLink:
    """One official centreline, in LV95, before it is split into SUMO directions."""

    fid: str
    name: str
    parts: tuple[tuple[tuple[float, float], ...], ...]
    forward: bool
    backward: bool
    cycleway: bool
    bike_lane: bool
    speed_forward: float
    speed_backward: float
    attract_forward: float
    attract_backward: float
    source: str


@dataclass(frozen=True)
class TurnBan:
    """A banned turn from one ``NETZ_VELO`` feature to another.

    ``at_end`` is true when the ban sits at the digitised end of the origin
    feature (``EDGE1END`` = Y) and false when it sits at the digitised start.
    """

    from_fid: str
    at_end: bool
    to_fid: str


@dataclass
class BikeReference:
    links: list[BikeLink] = field(default_factory=list)
    bans: list[TurnBan] = field(default_factory=list)
    bounds: tuple[float, float, float, float] | None = None
    notes: list[str] = field(default_factory=list)


def directions(oneway: str | None, ban_forward: int, ban_backward: int) -> tuple[bool, bool]:
    """Which digitised directions a bicycle may use.

    ``FAHRVERBOT_*`` = 1 closes that direction. ``EINBAHN`` of FT or TF closes
    the other one. A link closed both ways is not ridable.
    """
    forward = int(ban_forward or 0) != 1
    backward = int(ban_backward or 0) != 1
    code = str("0" if oneway is None else oneway).strip().upper()
    if code == "FT":
        backward = False
    elif code == "TF":
        forward = False
    return forward, backward


def _speed(value: float | None) -> float:
    """``GESCHWINDIGKEIT`` is km/h. SUMO wants m/s."""
    try:
        speed_kmh = float(value or 0)
    except (TypeError, ValueError):
        return DEFAULT_SPEED_MPS
    return speed_kmh / 3.6 if speed_kmh > 0 else DEFAULT_SPEED_MPS


def clip_parts(
    parts: list[list[tuple[float, float]]],
    bounds: tuple[float, float, float, float],
    margin: float,
) -> list[list[tuple[float, float]]]:
    """Keep vertices inside ``bounds`` expanded by ``margin`` metres.

    A run of vertices outside the box is dropped, so a long cantonal corridor
    contributes only the stretch next to the city network.
    """
    xmin, ymin, xmax, ymax = bounds
    xmin -= margin
    ymin -= margin
    xmax += margin
    ymax += margin
    kept: list[list[tuple[float, float]]] = []
    for part in parts:
        run: list[tuple[float, float]] = []
        for x, y in part:
            inside = xmin <= x <= xmax and ymin <= y <= ymax
            if inside:
                run.append((x, y))
            elif len(run) >= 2:
                kept.append(run)
                run = []
            else:
                run = []
        if len(run) >= 2:
            kept.append(run)
    return kept


def assert_geometry_file(path: Path) -> None:
    """Reject the Aargau catalog XML. Geometry files are read further down."""
    if path.suffix.lower() not in {".xml", ".rdf"}:
        return
    head = path.read_text(encoding="utf-8", errors="replace")[:12000]
    if "dcat:Dataset" in head or "<rdf:RDF" in head:
        raise CatalogOnly(
            f"{path.name} is the open-data catalog entry for the Aargau Velonetz "
            "(AGIS.are_divradroutkant), not the lines. The Zurich graph is "
            "NETZ_VELO in Routing.gdb. An Aargau shapefile or geodatabase can "
            "be passed separately; this XML has no coordinates."
        )


def _line_parts(geom) -> list[list[tuple[float, float]]]:
    import shapely

    if geom is None:
        return []
    if isinstance(geom, (bytes, bytearray)):
        geom = shapely.from_wkb(geom)
    if geom.is_empty:
        return []
    geoms = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
    parts: list[list[tuple[float, float]]] = []
    for item in geoms:
        if item.geom_type != "LineString":
            continue
        coords = [(float(x), float(y)) for x, y, *_rest in item.coords]
        if len(coords) >= 2:
            parts.append(coords)
    return parts


def _column(data, name: str, columns: list[str]):
    return data[columns.index(name)]


def load_netz_velo(path: Path) -> BikeReference:
    """Read ``NETZ_VELO`` and ``NETZ_VELO_ABBIEGEVERBOTE`` from a FileGDB."""
    from pyogrio.raw import read

    gdb = Path(path)
    columns = [
        "NAME",
        "EINBAHN",
        "VELOWEG",
        "VELOSTREIFEN",
        "FAHRVERBOT_FT",
        "FAHRVERBOT_TF",
        "GESCHWINDIGKEIT_FT",
        "GESCHWINDIGKEIT_TF",
        "ATTRAKTIV_FT",
        "ATTRAKTIV_TF",
    ]
    meta, fids, geoms, data = read(gdb, layer="NETZ_VELO", columns=columns, return_fids=True)
    # pyogrio returns the columns in its own order, not the order requested.
    columns = [str(name) for name in meta["fields"]]
    links: list[BikeLink] = []
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for fid, geom, *values in zip(fids, geoms, *data, strict=True):
        record = dict(zip(columns, values, strict=True))
        forward, backward = directions(record["EINBAHN"], record["FAHRVERBOT_FT"], record["FAHRVERBOT_TF"])
        if not forward and not backward:
            continue
        parts = _line_parts(geom)
        if not parts:
            continue
        for part in parts:
            for x, y in part:
                xmin, ymin = min(xmin, x), min(ymin, y)
                xmax, ymax = max(xmax, x), max(ymax, y)
        lane = str(record["VELOSTREIFEN"] or "").strip().upper()
        links.append(
            BikeLink(
                fid=str(int(fid)),
                name=str(record["NAME"] or ""),
                parts=tuple(tuple(part) for part in parts),
                forward=forward,
                backward=backward,
                cycleway=int(record["VELOWEG"] or 0) == 1,
                bike_lane=lane in {"FT", "TF", "BOTH", "1"},
                speed_forward=_speed(record["GESCHWINDIGKEIT_FT"]),
                speed_backward=_speed(record["GESCHWINDIGKEIT_TF"]),
                attract_forward=float(record["ATTRAKTIV_FT"] or 0),
                attract_backward=float(record["ATTRAKTIV_TF"] or 0),
                source="zurich",
            )
        )
    bans = _load_bans(gdb)
    bounds = None if xmin is math.inf else (xmin, ymin, xmax, ymax)
    return BikeReference(links=links, bans=bans, bounds=bounds)


def _load_bans(gdb: Path) -> list[TurnBan]:
    from pyogrio.raw import read

    columns = ["EDGE1END", "EDGE1FID", "EDGE2FID"]
    _meta, _fids, _geoms, data = read(gdb, layer="NETZ_VELO_ABBIEGEVERBOTE", columns=columns, read_geometry=False)
    bans: list[TurnBan] = []
    for end, fid1, fid2 in zip(*data, strict=True):
        if fid1 is None or fid2 is None:
            continue
        bans.append(TurnBan(str(int(fid1)), str(end or "").strip().upper() == "Y", str(int(fid2))))
    return bans


def load_aargau_lines(
    path: Path,
    bounds: tuple[float, float, float, float],
    margin: float = AARGAU_MARGIN_M,
) -> list[BikeLink]:
    """Cantonal lines that fall inside the Zurich graph plus ``margin``.

    These are extra links at the city border. A catalog XML is refused.
    """
    from pyogrio.raw import read

    source = Path(path)
    assert_geometry_file(source)
    _meta, fids, geoms, data = read(source, return_fids=True)
    name_index = None
    fields = [str(item) for item in _meta.get("fields", [])]
    for candidate in ("NAME", "VORHABEN", "name"):
        if candidate in fields:
            name_index = fields.index(candidate)
            break
    links: list[BikeLink] = []
    for index, (fid, geom) in enumerate(zip(fids, geoms, strict=True)):
        parts = clip_parts(_line_parts(geom), bounds, margin)
        if not parts:
            continue
        name = ""
        if name_index is not None:
            name = str(data[name_index][index] or "")
        links.append(
            BikeLink(
                fid=f"ag{int(fid)}",
                name=name,
                parts=tuple(tuple(part) for part in parts),
                forward=True,
                backward=True,
                cycleway=True,
                bike_lane=False,
                speed_forward=DEFAULT_SPEED_MPS,
                speed_backward=DEFAULT_SPEED_MPS,
                attract_forward=1.0,
                attract_backward=1.0,
                source="aargau",
            )
        )
    return links
