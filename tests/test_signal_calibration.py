import gzip
import tempfile
from pathlib import Path

from matsim_sumo.network import pipeline_progress as pp
from matsim_sumo.network import signal_calibration as sc

PHASES = [
    {"state": "GGrr", "duration": 30.0, "minDur": 5.0, "maxDur": 38.0},
    {"state": "yyrr", "duration": 3.0},
    {"state": "rrGG", "duration": 30.0, "minDur": 5.0, "maxDur": 38.0},
    {"state": "rryy", "duration": 3.0},
]
LANES = {0: "a_0", 1: "a_1", 2: "b_0", 3: "b_1"}


def test_webster_gives_the_busier_approach_the_longer_green():
    greens = sc.webster_greens(PHASES, LANES, {0: 600, 1: 600, 2: 150, 3: 150}, 60, 120)
    assert greens[0] > greens[2] >= 5
    cycle = sum(greens.values()) + 6
    assert abs(cycle - 60) < 1e-6
    assert abs(greens[0] / greens[2] - 4) < 1e-6


def test_saturated_signal_runs_the_longest_cycle():
    greens = sc.webster_greens(PHASES, LANES, {0: 1500, 1: 1500, 2: 900, 3: 900}, 60, 120)
    assert abs(sum(greens.values()) + 6 - 120) < 1e-6


def test_no_demand_keeps_the_program():
    assert sc.webster_greens(PHASES, LANES, {}, 60, 120) is None


def test_step_moves_part_of_the_way():
    programs = {"t": PHASES}
    target = sc.webster_greens(PHASES, LANES, {0: 600, 1: 600, 2: 150, 3: 150}, 60, 120)
    half, changed = sc.candidate_programs(programs, {"t": LANES}, {"t": {0: 600, 1: 600, 2: 150, 3: 150}}, 0.5, 60, 120, 1.25)
    assert changed == 1
    assert half["t"][0]["duration"] == round(30 + 0.5 * (target[0] - 30))
    assert half["t"][0]["maxDur"] == round(1.25 * half["t"][0]["duration"])
    assert half["t"][1]["duration"] == 3.0


def test_programs_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "signals.add.xml"
        source.write_text(
            '<additional>\n    <tlLogic id="t" programID="webster" type="actuated" offset="0">\n'
            '        <phase duration="30" state="GGrr" minDur="5" maxDur="38"/>\n'
            '        <phase duration="3" state="yyrr"/>\n    </tlLogic>\n</additional>\n'
        )
        programs = sc.read_programs(source)
        programs["t"][0]["duration"] = 44.0
        programs["t"][0]["maxDur"] = 55.0
        out = sc.write_programs(source, programs, Path(tmp) / "out.add.xml")
        again = sc.read_programs(out)
        assert again["t"][0] == {"state": "GGrr", "duration": 44.0, "minDur": 5.0, "maxDur": 55.0}
        assert again["t"][1] == {"state": "yyrr", "duration": 3.0}


def test_demand_counts_vehicles_that_did_not_enter_with_their_plan():
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp) / "vehroutes.xml.gz"
        with gzip.open(run, "wt") as handle:
            handle.write('<routes><vehicle id="v1" type="car"><route edges="a b"/></vehicle></routes>')
        planned = Path(tmp) / "planned.rou.xml"
        planned.write_text(
            '<routes><vehicle id="v1" type="car"><route edges="a c"/></vehicle>'
            '<vehicle id="v2" type="truck"><route edges="a b"/></vehicle></routes>'
        )
        by_pair = {("a", "b"): [("t", 0), ("t", 1)], ("a", "c"): [("t", 2)]}
        demand = sc.link_demand(by_pair, [run], planned)
        assert demand["t"][0] == demand["t"][1] == (1 + sc.TRUCK_PCU) / 2
        assert demand["t"].get(2, 0) == 0


def test_progress_rows_give_the_best_candidate_of_each_iteration():
    report = {"iterations": [
        {"iteration": 0, "step": 0, "teleports": 180, "inserted": 73000, "not_inserted_percent": 8.4, "score": 10.9, "accepted": True},
        {"iteration": 1, "step": 1, "teleports": 150, "inserted": 74000, "not_inserted_percent": 7.0, "score": 9.0, "accepted": True,
         "geh": {"under_5_percent": 40.0}},
        {"iteration": 1, "step": 0.5, "teleports": 160, "inserted": 73500, "not_inserted_percent": 7.5, "score": 9.7, "accepted": False},
    ]}
    rows = pp.calibration_rows(report)
    assert len(rows) == 1
    assert rows[0]["stage"] == 5
    assert rows[0]["teleported"] == 150
    assert rows[0]["geh_under_5_percent"] == 40.0
    assert "accepted" in rows[0]["step"]
