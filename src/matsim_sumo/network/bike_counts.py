"""Hourly bicycle counts from the Zurich pedestrian and bicycle counter file.

The parquet holds a 15-minute row per counter: ``FK_STANDORT``, ``DATUM``,
``VELO_IN``, ``VELO_OUT``, ``OST``, ``NORD``. Pedestrian columns are ignored.
The reference for one counter is the median hour 07:00-08:00 over measured
Tuesdays to Thursdays, without July, August and 20 December-6 January, the
same days as the car counts. In and out are added, because the file does not
say which compass direction "in" is.

2025 is the default year. 2026 in this file stops in November, so its median
is not a full year.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
import statistics
from collections import defaultdict
from pathlib import Path

QUARTERS = (0, 15, 30, 45)


def typical_weekday(day: dt.date) -> bool:
    if day.weekday() not in (1, 2, 3) or day.month in (7, 8):
        return False
    return not ((day.month == 12 and day.day >= 20) or (day.month == 1 and day.day <= 6))


def geh(simulated: float, counted: float) -> float:
    total = simulated + counted
    return math.sqrt(2 * (simulated - counted) ** 2 / total) if total > 0 else 0.0


def _hour_count(quarters: dict[int, tuple[int, int]]) -> int | None:
    if any(minute not in quarters for minute in QUARTERS):
        return None
    return sum(incoming + outgoing for incoming, outgoing in quarters.values())


def reference_hour(path: Path, year: int = 2025, hour: int = 7) -> list[dict]:
    """Median bicycle volume of ``hour`` at each counter that measured it."""
    import pyarrow.parquet as pq

    days: dict[int, dict[dt.date, dict[int, tuple[int, int]]]] = defaultdict(lambda: defaultdict(dict))
    places: dict[int, tuple[int, int]] = {}
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=500_000, columns=["FK_STANDORT", "DATUM", "VELO_IN", "VELO_OUT", "OST", "NORD"]):
        sites = batch.column("FK_STANDORT").to_pylist()
        stamps = batch.column("DATUM").to_pylist()
        incoming = batch.column("VELO_IN").to_pylist()
        outgoing = batch.column("VELO_OUT").to_pylist()
        east = batch.column("OST").to_pylist()
        north = batch.column("NORD").to_pylist()
        for site, stamp, velo_in, velo_out, ost, nord in zip(sites, stamps, incoming, outgoing, east, north, strict=True):
            if velo_in is None or velo_out is None:
                continue
            when = dt.datetime.fromisoformat(stamp)
            if when.year != year or when.hour != hour or when.minute not in QUARTERS:
                continue
            if not typical_weekday(when.date()):
                continue
            days[int(site)][when.date()][when.minute] = (int(velo_in), int(velo_out))
            places[int(site)] = (int(ost), int(nord))
    rows = []
    for site, by_day in sorted(days.items()):
        totals = [count for quarters in by_day.values() if (count := _hour_count(quarters)) is not None]
        if not totals:
            continue
        ost, nord = places[site]
        rows.append({
            "site": str(site),
            "east": ost,
            "north": nord,
            "days": len(totals),
            "count": statistics.median(totals),
        })
    return rows


def write_reference(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["site", "east", "north", "days", "count"])
        writer.writeheader()
        writer.writerows(rows)


def read_reference(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append({
                "site": row["site"],
                "east": int(float(row["east"])),
                "north": int(float(row["north"])),
                "days": int(row["days"]),
                "count": float(row["count"]),
            })
    return rows
