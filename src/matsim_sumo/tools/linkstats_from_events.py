from __future__ import annotations

import argparse
import gzip
import sys
from time import perf_counter
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

import matsim

DEFAULT_EVENTS_PATH = Path("data/inputs/matsim_output_50pct/output_events.xml.gz")
DEFAULT_OUTPUT_PATH = Path("data/inputs/matsim_output_50pct/linkstats_from_events_car_truck.txt.gz")
ENTERED_LINK_EVENT = "entered link"
ROAD_VEHICLE_SUFFIXES = (":car", ":truck")


def is_counted_vehicle(vehicle: object) -> bool:
    return isinstance(vehicle, str) and vehicle.endswith(ROAD_VEHICLE_SUFFIXES)


def hour_bucket(time_seconds: object) -> int:
    hour = int(float(time_seconds) // 3600)
    return max(0, min(23, hour))


def link_sort_key(link_id: str) -> tuple[int, int | str]:
    try:
        return (0, int(link_id))
    except ValueError:
        return (1, link_id)


def aggregate_entered_link_counts(
    events: Iterable[Mapping[str, object]],
    progress_every: int = 0,
) -> dict[str, list[int]]:
    counts: defaultdict[str, list[int]] = defaultdict(lambda: [0] * 24)
    seen = 0
    kept = 0
    t0 = perf_counter()
    for event in events:
        if event.get("type") != ENTERED_LINK_EVENT:
            continue
        seen += 1

        link_id = event.get("link")
        vehicle = event.get("vehicle")
        if not isinstance(link_id, str) or not is_counted_vehicle(vehicle):
            continue

        counts[link_id][hour_bucket(event["time"])] += 1
        kept += 1

        if progress_every > 0 and kept % progress_every == 0:
            elapsed = perf_counter() - t0
            print(
                f"counted {kept:,} car/truck entered-link events "
                f"from {seen:,} entered-link events in {elapsed:.1f}s",
                file=sys.stderr,
                flush=True,
            )

    return dict(counts)


def read_counts_from_events(events_path: Path, progress_every: int = 0) -> dict[str, list[int]]:
    return aggregate_entered_link_counts(
        matsim.event_reader(events_path, types=[ENTERED_LINK_EVENT]),
        progress_every=progress_every,
    )


def linkstats_header() -> list[str]:
    header = ["LINK"]
    for hour in range(24):
        label = f"HRS{hour}-{hour + 1}"
        header.extend([f"{label}min", f"{label}avg", f"{label}max"])
    header.extend(["HRS0-24min", "HRS0-24avg", "HRS0-24max"])
    return header


def linkstats_row(link_id: str, hourly_counts: list[int]) -> list[str]:
    total = sum(hourly_counts)
    row = [link_id]
    for count in hourly_counts:
        value = str(count)
        row.extend([value, value, value])
    total_value = str(total)
    row.extend([total_value, total_value, total_value])
    return row


def write_linkstats(counts: Mapping[str, list[int]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wt") as dst:
        dst.write("\t".join(linkstats_header()) + "\n")
        for link_id in sorted(counts, key=link_sort_key):
            dst.write("\t".join(linkstats_row(link_id, counts[link_id])) + "\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build MATSim-like linkstats counts from entered-link events for car/truck vehicles."
    )
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS_PATH, help="MATSim output_events.xml.gz path.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="Output linkstats .txt.gz path.")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1_000_000,
        help="Print progress every N counted car/truck entered-link events. Use 0 to disable.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    counts = read_counts_from_events(args.events, progress_every=max(0, args.progress_every))
    write_linkstats(counts, args.output)

    total_events = sum(sum(hourly) for hourly in counts.values())
    print(f"Wrote {args.output} ({len(counts)} links, {total_events} entered-link car/truck events)")


if __name__ == "__main__":
    main()
