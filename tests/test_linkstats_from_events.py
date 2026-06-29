import gzip
from pathlib import Path

from matsim_sumo.tools.linkstats_from_events import (
    aggregate_entered_link_counts,
    hour_bucket,
    is_counted_vehicle,
    linkstats_header,
    write_linkstats,
)


def test_vehicle_filter_includes_only_car_and_truck_suffixes() -> None:
    assert is_counted_vehicle("202340005634129:car")
    assert is_counted_vehicle("202340005634129:truck")
    assert not is_counted_vehicle("202340005634129:bike")
    assert not is_counted_vehicle("202340005634129:lcv")
    assert not is_counted_vehicle("veh_16623_rail")
    assert not is_counted_vehicle("veh_203992_cable_car")
    assert not is_counted_vehicle(None)


def test_hour_bucket_clips_to_day_bounds() -> None:
    assert hour_bucket(0.0) == 0
    assert hour_bucket(3599.9) == 0
    assert hour_bucket(3600.0) == 1
    assert hour_bucket(25 * 3600) == 23
    assert hour_bucket(-10) == 0


def test_aggregate_entered_link_counts_filters_modes_and_event_types() -> None:
    events = [
        {"time": 1.0, "type": "vehicle enters traffic", "link": "1", "vehicle": "p1:car"},
        {"time": 2.0, "type": "entered link", "link": "1", "vehicle": "p1:car"},
        {"time": 3700.0, "type": "entered link", "link": "1", "vehicle": "p2:truck"},
        {"time": 3800.0, "type": "entered link", "link": "1", "vehicle": "p3:bike"},
        {"time": 3900.0, "type": "entered link", "link": "1", "vehicle": "p4:lcv"},
        {"time": 4000.0, "type": "entered link", "link": "2", "vehicle": "veh_1_rail"},
        {"time": 24 * 3600 + 100, "type": "entered link", "link": "2", "vehicle": "p5:car"},
    ]

    counts = aggregate_entered_link_counts(events)

    assert sorted(counts) == ["1", "2"]
    assert counts["1"][0] == 1
    assert counts["1"][1] == 1
    assert sum(counts["1"]) == 2
    assert counts["2"][23] == 1
    assert sum(counts["2"]) == 1


def test_header_contains_link_hourly_and_daily_columns() -> None:
    header = linkstats_header()

    assert header[0] == "LINK"
    assert "HRS0-1avg" in header
    assert "HRS23-24avg" in header
    assert header[-3:] == ["HRS0-24min", "HRS0-24avg", "HRS0-24max"]


def test_write_linkstats_writes_hourly_and_daily_totals(tmp_path: Path) -> None:
    output_path = tmp_path / "linkstats.txt.gz"
    hourly = [0] * 24
    hourly[0] = 2
    hourly[23] = 3

    write_linkstats({"10": hourly, "2": [1] * 24, "pt_link": [0] * 24}, output_path)

    with gzip.open(output_path, "rt") as src:
        rows = [line.rstrip("\n").split("\t") for line in src]

    header = rows[0]
    link_rows = {row[0]: row for row in rows[1:]}
    assert [row[0] for row in rows[1:]] == ["2", "10", "pt_link"]
    assert "HRS0-24avg" in header

    daily_idx = header.index("HRS0-24avg")
    hour0_idx = header.index("HRS0-1avg")
    hour23_idx = header.index("HRS23-24avg")

    assert link_rows["10"][hour0_idx] == "2"
    assert link_rows["10"][hour23_idx] == "3"
    assert link_rows["10"][daily_idx] == "5"
    assert link_rows["2"][daily_idx] == "24"
