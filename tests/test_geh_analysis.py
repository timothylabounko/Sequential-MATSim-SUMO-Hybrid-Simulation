import csv
import tempfile
from pathlib import Path

from matsim_sumo.network import count_check as cc
from matsim_sumo.network import geh_analysis as ga


def test_street_key_keeps_motorway_numbers():
    assert "a1l" not in cc._street_key("Wallisellenstrasse")
    assert cc._street_key("A1L") == "a1l"
    assert cc._street_key("Seebahnstr.") == cc._street_key("Seebahnstrasse")


def test_envelope_has_geh_five_at_both_ends():
    low, high = ga.geh_envelope(1000)
    assert abs(cc.geh(low, 1000) - 5) < 1e-9
    assert abs(cc.geh(high, 1000) - 5) < 1e-9
    assert low < 1000 < high


def test_fit_of_a_straight_line():
    result = ga.fit([100, 200, 300, 400], [150, 250, 350, 450])
    assert result["slope"] == 1.0
    assert result["intercept"] == 50.0
    assert result["r2"] == 1.0


def test_seeds_are_averaged_before_geh():
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, simulated in enumerate((800, 1200)):
            path = Path(tmp) / f"seed{i}.csv"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["zsid", "name", "street", "directions", "edges", "count", "simulated"])
                writer.writeheader()
                writer.writerow({"zsid": "Z1", "name": "A", "street": "A", "directions": 2, "edges": "e", "count": 1000, "simulated": simulated})
            paths.append(path)
        (row,) = ga.merge(paths)
        assert row["simulated"] == 1000
        assert row["geh"] == 0


FIELDS = ["MSID", "ZSID", "ZSName", "Achse", "EKoord", "NKoord", "Richtung", "MessungDatZeit", "AnzFahrzeuge", "AnzFahrzeugeStatus"]


def _hour(writer, msid, date, hour, value, status="Gemessen"):
    writer.writerow({
        "MSID": msid, "ZSID": "Z001", "ZSName": "Seestrasse (Test)", "Achse": "Seestrasse",
        "EKoord": 2683009.89, "NKoord": 1243936.2, "Richtung": "auswärts" if msid.endswith("1") else "einwärts",
        "MessungDatZeit": f"{date}T{hour:02d}:00:00", "AnzFahrzeuge": value, "AnzFahrzeugeStatus": status,
    })


def _day(writer, msid, date, value, estimated_hour=None):
    for hour in range(24):
        _hour(writer, msid, date, hour, value, "Imputiert" if hour == estimated_hour else "Gemessen")


def test_daily_reference_is_the_median_of_complete_typical_weekdays():
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.csv"
        with raw.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            _day(writer, "Z001M001", "2026-03-03", 10)  # Tuesday: 240
            _day(writer, "Z001M001", "2026-03-04", 20)  # Wednesday: 480
            _day(writer, "Z001M001", "2026-03-11", 30)  # Wednesday: 720
            _day(writer, "Z001M001", "2026-03-05", 1000, estimated_hour=5)  # Thursday with an estimated hour
            _day(writer, "Z001M001", "2026-03-02", 1000)  # Monday
            _day(writer, "Z001M001", "2026-07-07", 1000)  # Tuesday in July
            _day(writer, "Z001M002", "2026-03-03", 5)
            for hour in range(23):  # Thursday with the last hour missing
                _hour(writer, "Z001M002", "2026-03-12", hour, 1000)
        sites = {site["msid"]: site for site in cc.daily_reference_counts(raw)}
    assert sites["Z001M001"]["days"] == 3
    assert sites["Z001M001"]["count"] == 480
    assert sites["Z001M001"]["h07"] == 20
    assert sites["Z001M002"]["days"] == 1
    assert sites["Z001M002"]["count"] == 120


def test_daily_geh_is_divided_by_24_and_the_hourly_equivalent_by_sqrt_24():
    values = cc.daily_geh(30000, 20000)
    day = cc.geh(30000, 20000)
    assert abs(values["geh_day"] - day) < 1e-9
    assert abs(values["geh_day_24"] - day / 24) < 1e-9
    assert abs(values["geh_hourly_equivalent"] - day / 24 ** 0.5) < 1e-9
    assert abs(values["geh_hourly_equivalent"] - cc.geh(30000 / 24, 20000 / 24)) < 1e-9


def test_daily_band_edges_are_geh_day_120():
    low, high = ga.geh_envelope(20000, ga.DAILY_GEH_LIMIT)
    assert abs(cc.daily_geh(low, 20000)["geh_day_24"] - 5) < 1e-9
    assert abs(cc.daily_geh(high, 20000)["geh_day_24"] - 5) < 1e-9
    assert cc.daily_geh(high - 1, 20000)["geh_day_24"] < 5 < cc.daily_geh(high + 1, 20000)["geh_day_24"]
    low_eq, high_eq = ga.geh_envelope(20000, ga.HOURLY_EQUIVALENT_LIMIT)
    assert abs(cc.daily_geh(high_eq, 20000)["geh_hourly_equivalent"] - 5) < 1e-9
    assert low < low_eq < 20000 < high_eq < high


def test_daily_merge_and_summary():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "counts.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["zsid", "name", "street", "directions", "edges", "count", "simulated"])
            writer.writeheader()
            writer.writerow({"zsid": "Z1", "name": "A", "street": "A", "directions": 2, "edges": "e", "count": 20000, "simulated": 20500})
            writer.writerow({"zsid": "Z2", "name": "B", "street": "B", "directions": 2, "edges": "f", "count": 10000, "simulated": 30000})
            writer.writerow({"zsid": "Z3", "name": "C", "street": "C", "directions": 1, "edges": "g", "count": 5000, "simulated": 6000})
        rows = ga.merge_daily([path])
        result = ga.summary_daily(rows, 1)
    assert rows[0]["zsid"] == "Z2"
    assert rows[0]["geh"] == round(cc.geh(30000, 10000) / 24, 3)
    assert result["main_statistic"]["under_5"] == 2
    assert result["secondary_hourly_equivalent"]["under_5"] == 2
    assert result["counted"] == 35000 and result["simulated"] == 56500


def test_edge_counts_are_summed_over_intervals_and_split_by_hour():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "edgedata.xml"
        path.write_text(
            '<meandata><interval begin="0.00" end="3600.00" id="h"><edge id="e" entered="3"/><edge id="f" entered="1"/></interval>'
            '<interval begin="3600.00" end="7200.00" id="h"><edge id="e" entered="4"/></interval></meandata>',
            encoding="utf-8",
        )
        assert cc.read_edge_counts(path) == {"e": 7, "f": 1}
        hours = cc.read_hourly_edge_counts(path)
    assert hours[0]["e"] == 3 and hours[1]["e"] == 4
    profile = cc.hourly_profile([{"edges": "e f", "h00": 5, "h01": 4}], hours)
    assert profile[0]["counted"] == 5 and profile[0]["simulated"] == 4
    assert profile[1]["simulated"] == 4 and profile[1]["difference_percent"] == 0.0
