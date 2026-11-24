"""Compare simulated traffic with the Zürich traffic counts, 07:00-08:00.

Reference: the hourly motor-traffic counts of the city of Zürich
(``sid_dav_verkehrszaehlung_miv_OD2031``, open data, CC0), one file per year.
For every measurement site the reference volume is the median of the hour
07:00-08:00 over the measured (status ``Gemessen``) Tuesdays to Thursdays,
without July, August and 20 December-6 January (school holidays). The median
keeps single holidays and closures from changing the reference.

A counting cross-section (``ZSID``) is the sum of its measurement sites, one per
direction. It is matched to the SUMO edges within ``RADIUS_M`` of its
coordinates that carry the street of the count (``Achse``): the nearest such
edge and, if the street is two-way and the count has two directions, the
nearest edge in the opposite direction. Cross-sections without such an edge
are left out and listed.

The simulated volume is the number of vehicles entering the matched edges in
the simulated hour (SUMO ``edgeData``). The agreement is reported with the
GEH statistic, GEH = sqrt(2 (M - C)^2 / (M + C)), with M the simulated and C the
counted hourly volume; GEH < 5 on at least 85 % of the cross-sections is the
usual acceptance level (UK Design Manual for Roads and Bridges, TAG M3.1).

``--daily`` compares the whole day instead. The reference of a measurement site
is the median, over the same typical weekdays, of its daily total: the sum of
the 24 hourly counts of a day on which every hour was measured (``Gemessen``);
a day with a missing or estimated hour is left out. The simulated volume is the
number of vehicles entering the matched edges in 00:00-24:00. The reference
file also keeps the median of every hour over those days (``h00``-``h23``) for
the hourly profile.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import re
import statistics
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import sumolib

DATA_URL = (
    "https://data.stadt-zuerich.ch/dataset/sid_dav_verkehrszaehlung_miv_od2031/download/"
    "sid_dav_verkehrszaehlung_miv_OD2031_{year}.csv"
)
HOUR = 7
RADIUS_M = 50.0
GEH_LIMIT = 5.0
DAY_S = 86400
HOUR_KEYS = tuple(f"h{hour:02d}" for hour in range(24))


def lv95_to_wgs84(east: float, north: float) -> tuple[float, float]:
    """Swisstopo's approximate LV95 -> WGS84 formulas (about 1 m), as (lat, lon)."""
    y = (east - 2_600_000) / 1e6
    x = (north - 1_200_000) / 1e6
    lon = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y**3
    lat = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x**3
    return lat * 100 / 36, lon * 100 / 36


def typical_weekday(day: dt.date) -> bool:
    if day.weekday() not in (1, 2, 3) or day.month in (7, 8):
        return False
    return not ((day.month == 12 and day.day >= 20) or (day.month == 1 and day.day <= 6))


def reference_counts(raw: Path, hour: int = HOUR) -> list[dict]:
    """Median count of ``hour`` per measurement site over typical weekdays."""
    values: dict[str, list[int]] = defaultdict(list)
    sites: dict[str, dict] = {}
    with raw.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["AnzFahrzeugeStatus"] != "Gemessen" or not row["AnzFahrzeuge"]:
                continue
            stamp = dt.datetime.fromisoformat(row["MessungDatZeit"])
            if stamp.hour != hour or not typical_weekday(stamp.date()):
                continue
            values[row["MSID"]].append(int(float(row["AnzFahrzeuge"])))
            if row["MSID"] not in sites:
                lat, lon = lv95_to_wgs84(float(row["EKoord"]), float(row["NKoord"]))
                sites[row["MSID"]] = {
                    "msid": row["MSID"],
                    "zsid": row["ZSID"],
                    "name": row["ZSName"],
                    "street": row["Achse"],
                    "direction": row["Richtung"],
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                }
    return [
        {**sites[msid], "days": len(counts), "count": statistics.median(counts)}
        for msid, counts in sorted(values.items())
    ]


def daily_reference_counts(raw: Path) -> list[dict]:
    """Median daily total per measurement site over typical weekdays with all 24 hours measured."""
    hours: dict[tuple[str, dt.date], dict[int, int]] = defaultdict(dict)
    incomplete: set[tuple[str, dt.date]] = set()
    sites: dict[str, dict] = {}
    with raw.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            stamp = dt.datetime.fromisoformat(row["MessungDatZeit"])
            if not typical_weekday(stamp.date()):
                continue
            key = (row["MSID"], stamp.date())
            if row["AnzFahrzeugeStatus"] != "Gemessen" or not row["AnzFahrzeuge"]:
                incomplete.add(key)
                continue
            hours[key][stamp.hour] = int(float(row["AnzFahrzeuge"]))
            if row["MSID"] not in sites:
                lat, lon = lv95_to_wgs84(float(row["EKoord"]), float(row["NKoord"]))
                sites[row["MSID"]] = {
                    "msid": row["MSID"],
                    "zsid": row["ZSID"],
                    "name": row["ZSName"],
                    "street": row["Achse"],
                    "direction": row["Richtung"],
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                }
    days: dict[str, list[dict[int, int]]] = defaultdict(list)
    for (msid, day), counts in hours.items():
        if (msid, day) not in incomplete and len(counts) == 24:
            days[msid].append(counts)
    return [
        {
            **sites[msid],
            "days": len(complete),
            "count": statistics.median(sum(day.values()) for day in complete),
            **{f"h{hour:02d}": statistics.median(day[hour] for day in complete) for hour in range(24)},
        }
        for msid, complete in sorted(days.items())
    ]


def download(year: int, target: Path, attempts: int = 4) -> Path:
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        for attempt in range(1, attempts + 1):
            print(f"downloading the {year} Zürich traffic counts (attempt {attempt} of {attempts})", flush=True)
            try:
                urllib.request.urlretrieve(DATA_URL.format(year=year), partial)
                break
            except OSError as exc:
                partial.unlink(missing_ok=True)
                if attempt == attempts:
                    raise
                print(f"  download interrupted ({exc}); trying again in {30 * attempt} s", flush=True)
                time.sleep(30 * attempt)
        partial.rename(target)
    return target


def _street_key(name: str) -> str:
    return re.sub(r"[^a-z0-9äöü]", "", name.lower().replace("str.", "strasse"))


def _heading(edge) -> float:
    (ax, ay), (bx, by) = edge.getShape()[0], edge.getShape()[-1]
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360


def match_sections(net, sites: list[dict], radius: float = RADIUS_M) -> tuple[list[dict], list[dict]]:
    """One row per counting cross-section with its SUMO edges, and the cross-sections left out."""
    sections: dict[str, list[dict]] = defaultdict(list)
    for site in sites:
        sections[site["zsid"]].append(site)
    matched, unmatched = [], []
    for zsid, members in sorted(sections.items()):
        first = members[0]
        x, y = net.convertLonLat2XY(first["lon"], first["lat"])
        key = _street_key(first["street"])
        near = sorted(
            (
                (distance, edge)
                for edge, distance in net.getNeighboringEdges(x, y, radius)
                if edge.getFunction() != "internal"
                and edge.allows("passenger")
                and key
                and (key in _street_key(edge.getName()) or _street_key(edge.getName()) in key)
                and _street_key(edge.getName())
            ),
            key=lambda pair: pair[0],
        )
        row = {
            "zsid": zsid,
            "name": first["name"],
            "street": first["street"],
            "directions": len(members),
            "count": sum(member["count"] for member in members),
            "days": min(member["days"] for member in members),
            "lat": first["lat"],
            "lon": first["lon"],
            **{key: sum(member[key] for member in members) for key in HOUR_KEYS if key in first},
        }
        if not near:
            unmatched.append({**row, "reason": f"no {first['street']} edge within {radius:g} m"})
            continue
        edges = [near[0][1]]
        heading = _heading(edges[0])
        opposite = [edge for _d, edge in near if 120 <= abs(_heading(edge) - heading) % 360 <= 240]
        if len(members) > 1 and not opposite:
            unmatched.append({**row, "reason": "two directions counted, one-way in SUMO"})
            continue
        if len(members) == 1 and opposite:
            unmatched.append({**row, "reason": "one direction counted on a two-way street"})
            continue
        if len(members) > 1:
            edges.append(opposite[0])
        matched.append({**row, "edges": " ".join(edge.getID() for edge in edges)})
    return matched, unmatched


def geh(simulated: float, counted: float) -> float:
    total = simulated + counted
    return math.sqrt(2 * (simulated - counted) ** 2 / total) if total > 0 else 0.0


def config_value(config: Path, name: str) -> str:
    element = ET.parse(config).getroot().find(f".//{name}")
    return element.get("value", "") if element is not None else ""


def edge_data_additional(output: Path, end: int, hourly: Path | None = None) -> str:
    """edgeData definitions: one interval ``0``-``end``, and with ``hourly`` one interval per hour."""
    lines = [f'    <edgeData id="whole" file="{output.resolve()}" begin="0" end="{end}" excludeEmpty="true"/>']
    if hourly is not None:
        lines.append(
            f'    <edgeData id="hourly" file="{hourly.resolve()}" begin="0" end="{end}" period="3600" excludeEmpty="true"/>'
        )
    return "<additional>\n" + "\n".join(lines) + "\n</additional>\n"


def simulate_edge_counts(config: Path, work: Path, seed: int | None = None, end: int = 3600) -> dict[str, int]:
    """Vehicles entering every edge in ``0``-``end``, from ``config`` with an edgeData output added."""
    work.mkdir(parents=True, exist_ok=True)
    output = work / "edgedata.xml"
    meandata = work / "edgedata.add.xml"
    hourly = work / "edgedata-hourly.xml" if end > 3600 else None
    meandata.write_text(edge_data_additional(output, end, hourly), encoding="utf-8")
    additional = ",".join(filter(None, [config_value(config, "additional-files"), str(meandata.resolve())]))
    command = ["sumo", "-c", str(config), "-a", additional, "--no-step-log", "true", "--log", str(work / "sim.log")]
    if end != 3600:
        command += ["-e", str(end)]
    if seed is not None:
        command += ["--seed", str(seed)]
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=max(14400, 3 * end))
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout + completed.stderr)[-1500:])
    return read_edge_counts(output)


def read_edge_counts(edge_data: Path) -> dict[str, int]:
    """Vehicles entering every edge, summed over the intervals of the file."""
    counts: dict[str, int] = defaultdict(int)
    for element in ET.parse(edge_data).getroot().iter("edge"):
        counts[element.get("id")] += int(float(element.get("entered", "0")))
    return dict(counts)


def read_hourly_edge_counts(edge_data: Path) -> dict[int, dict[str, int]]:
    """Vehicles entering every edge per hour of the day (interval begin // 3600)."""
    hours: dict[int, dict[str, int]] = {}
    for interval in ET.parse(edge_data).getroot().iter("interval"):
        hour = int(float(interval.get("begin", "0")) // 3600)
        counts = hours.setdefault(hour, defaultdict(int))
        for element in interval.iter("edge"):
            counts[element.get("id")] += int(float(element.get("entered", "0")))
    return hours


def hourly_profile(sections: list[dict], hours: dict[int, dict[str, int]]) -> list[dict]:
    """Counted (median of every hour, summed over the cross-sections) against simulated vehicles per hour."""
    rows = []
    for hour in range(24):
        key = f"h{hour:02d}"
        counted = sum(float(section.get(key, 0) or 0) for section in sections)
        simulated = sum(hours.get(hour, {}).get(edge, 0) for section in sections for edge in section["edges"].split())
        rows.append({"hour": hour, "counted": round(counted), "simulated": simulated,
                     "difference_percent": round(100 * (simulated - counted) / counted, 1) if counted else None})
    return rows


def daily_geh(simulated: float, counted: float) -> dict[str, float]:
    """GEH of the daily totals, divided by 24 (main), and the hourly equivalent GEH(M/24, C/24) = GEH_day/sqrt(24)."""
    day = geh(simulated, counted)
    return {"geh_day": day, "geh_day_24": day / 24, "geh_hourly_equivalent": geh(simulated / 24, counted / 24)}


def compare(sections: list[dict], counts: dict[str, int], daily: bool = False) -> list[dict]:
    """Simulated volume and GEH per cross-section; with ``daily`` the GEH column is GEH_day/24."""
    rows = []
    for section in sections:
        simulated = sum(counts.get(edge, 0) for edge in section["edges"].split())
        if daily:
            values = daily_geh(simulated, section["count"])
            rows.append({
                **section,
                "simulated": simulated,
                "geh": round(values["geh_day_24"], 3),
                "geh_day": round(values["geh_day"], 2),
                "geh_hourly_equivalent": round(values["geh_hourly_equivalent"], 2),
            })
        else:
            rows.append({**section, "simulated": simulated, "geh": round(geh(simulated, section["count"]), 2)})
    return rows


def summary_line(rows: list[dict], label: str = "GEH") -> str:
    if not rows:
        return "no counting cross-section matched"
    good = sum(1 for row in rows if row["geh"] < GEH_LIMIT)
    counted = sum(row["count"] for row in rows)
    simulated = sum(row["simulated"] for row in rows)
    return (
        f"{good} of {len(rows)} cross-sections ({100 * good / len(rows):.0f} %) with {label} < {GEH_LIMIT:g}; "
        f"median {label} {statistics.median(row['geh'] for row in rows):.2f}; "
        f"simulated {simulated:.0f} vs counted {counted:.0f} vehicles ({100 * (simulated - counted) / counted:+.1f} %)"
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["zsid"])
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="SUMO configuration to check (final.sumocfg); not needed with --reduce-only")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--raw", type=Path, help="Downloaded count file; downloaded to the work directory if missing")
    parser.add_argument("--edge-data", type=Path, help="Existing edgeData output of the configuration; skips the simulation")
    parser.add_argument("--seed", type=int, help="Seed of the simulation instead of the configuration's")
    parser.add_argument("--reduce-only", action="store_true", help="Only write the reference counts of the hour")
    parser.add_argument("--daily", action="store_true", help="Compare the daily totals (00:00-24:00) instead of 07:00-08:00")
    parser.add_argument("--hourly-edge-data", type=Path, help="With --daily: hourly edgeData of the run, for the hourly profile")
    args = parser.parse_args(argv)
    work = args.work_dir
    work.mkdir(parents=True, exist_ok=True)

    reference = work / (f"counts-{args.year}-daily.csv" if args.daily else f"counts-{args.year}-{HOUR:02d}h.csv")
    if not reference.is_file():
        raw = args.raw or download(args.year, work / f"counts-{args.year}.raw.csv")
        if args.daily:
            print(f"reducing {raw} to the median daily total of typical weekdays measured in all 24 hours", flush=True)
            write_csv(reference, daily_reference_counts(raw))
        else:
            print(f"reducing {raw} to the {HOUR:02d}:00 median of typical weekdays", flush=True)
            write_csv(reference, reference_counts(raw))
    if args.reduce_only:
        print(f"reference counts: {reference}")
        return
    if args.config is None:
        parser.error("--config is required")
    with reference.open(encoding="utf-8") as handle:
        sites = [
            {
                **row,
                "count": float(row["count"]),
                "days": int(row["days"]),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                **{key: float(row[key]) for key in HOUR_KEYS if key in row},
            }
            for row in csv.DictReader(handle)
        ]

    net_file = Path(config_value(args.config, "net-file"))
    net = sumolib.net.readNet(str(net_file))
    sections, unmatched = match_sections(net, sites)
    print(f"{len(sections)} counting cross-sections matched to SUMO edges, {len(unmatched)} left out", flush=True)
    hourly = args.hourly_edge_data
    if args.edge_data:
        counts = read_edge_counts(args.edge_data)
    else:
        print("simulating with the edge counts", flush=True)
        counts = simulate_edge_counts(args.config, work / "sim", args.seed, DAY_S if args.daily else 3600)
        if args.daily:
            hourly = work / "sim" / "edgedata-hourly.xml"
    rows = compare(sections, counts, daily=args.daily)
    rows.sort(key=lambda row: -row["geh"])
    write_csv(work / "counts.csv", rows)
    write_csv(work / "counts-unmatched.csv", unmatched)
    if args.daily and hourly is not None and hourly.is_file():
        write_csv(work / "counts-by-hour.csv", hourly_profile(sections, read_hourly_edge_counts(hourly)))
        print(f"hourly profile: {work / 'counts-by-hour.csv'}")
    label = "GEH_day/24" if args.daily else "GEH"
    print(summary_line(rows, label))
    for row in rows[:15]:
        print(f"  {label} {row['geh']:5.1f}  {row['name']}: simulated {row['simulated']}, counted {row['count']:.0f}")
    print(f"per cross-section: {work / 'counts.csv'}")


if __name__ == "__main__":
    main()
