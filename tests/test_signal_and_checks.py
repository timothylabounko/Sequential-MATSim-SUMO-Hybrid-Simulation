import datetime as dt
import importlib.util
from pathlib import Path

import pytest

from matsim_sumo.network import count_check, lane_routes, signal_timing, simulation, stuck_report

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retype_vans.py"
spec = importlib.util.spec_from_file_location("retype_vans", SCRIPT)
retype_vans = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retype_vans)

# Two lanes on "a"; the left lane may not change right. "a" leads to "exit" from lane 0 only
# and to "main" from lane 1 only; "b" enters "a" on its left lane.
NET = """<net>
<edge id="b" from="0" to="1"><lane id="b_0" index="0" speed="10" length="100"/></edge>
<edge id="c" from="2" to="1"><lane id="c_0" index="0" speed="10" length="100"/></edge>
<edge id="a" from="1" to="3">
  <lane id="a_0" index="0" speed="10" length="100"/>
  <lane id="a_1" index="1" speed="10" length="100" changeRight="authority"/>
</edge>
<edge id="exit" from="3" to="4"><lane id="exit_0" index="0" speed="10" length="100"/></edge>
<edge id="main" from="3" to="5"><lane id="main_0" index="0" speed="10" length="100"/></edge>
<edge id="ramp" from="5" to="4"><lane id="ramp_0" index="0" speed="10" length="300"/></edge>
<connection from="b" to="a" fromLane="0" toLane="1"/>
<connection from="c" to="a" fromLane="0" toLane="0"/>
<connection from="a" to="exit" fromLane="0" toLane="0"/>
<connection from="a" to="main" fromLane="1" toLane="0"/>
<connection from="main" to="ramp" fromLane="0" toLane="0"/>
</net>
"""


@pytest.fixture()
def net(tmp_path):
    path = tmp_path / "x.net.xml"
    path.write_text(NET)
    return path


def test_lane_change_ban_makes_a_route_undrivable(net):
    graph = lane_routes.LaneGraph(net)
    assert graph.first_blocked(["b", "a", "exit"]) == 2
    assert graph.first_blocked(["c", "a", "exit"]) is None
    assert graph.fastest("b", "exit") is None
    assert graph.trap_edges() == {"b"}


def test_undrivable_routes_are_replaced_and_keep_their_route(net, tmp_path):
    routes = tmp_path / "r.rou.xml"
    routes.write_text(
        '<routes>\n    <vehicle id="v" depart="0">\n        <route edges="b a main"/>\n    </vehicle>\n'
        '    <vehicle id="w" depart="0">\n        <route edges="c a exit"/>\n    </vehicle>\n</routes>\n'
    )
    stats = lane_routes.fix_routes(net, routes)
    text = routes.read_text()
    assert stats["undrivable"] == 0 and stats["fixed_route"] == 1
    assert text.count("has.rerouting.device") == 1 and '<route edges="c a exit"/>' in text


def test_a_replaced_route_is_written_even_off_the_trap_edges(tmp_path):
    path = tmp_path / "y.net.xml"
    path.write_text(
        NET.replace("</net>", "")
        + '<edge id="d" from="6" to="1"><lane id="d_0" index="0" speed="10" length="100"/></edge>\n'
        + '<connection from="d" to="a" fromLane="0" toLane="0"/>\n'
        + '<connection from="b" to="d" fromLane="0" toLane="0"/>\n</net>\n'
    )
    routes = tmp_path / "r.rou.xml"
    routes.write_text('<routes>\n    <vehicle id="v" depart="0">\n        <route edges="b a exit"/>\n    </vehicle>\n</routes>\n')
    stats = lane_routes.fix_routes(path, routes)
    assert stats["rerouted"] == 1 and '<route edges="b d a exit"/>' in routes.read_text()


def test_statistics_give_shares_and_collision_teleports(tmp_path):
    stats = tmp_path / "s.xml"
    stats.write_text(
        '<statistics><vehicles loaded="200" inserted="100" running="5" waiting="100"/>'
        '<teleports total="10" jam="4" yield="2" wrongLane="3"/>'
        '<safety collisions="1" emergencyStops="0" emergencyBraking="2"/>'
        '<vehicleTripStatistics count="90" duration="300.50" waitingTime="20.00" timeLoss="80.00" departDelay="1.00"/>'
        "</statistics>"
    )
    result = simulation.read_statistics(stats)
    assert result["teleported_percent"] == 10.0 and result["not_inserted_percent"] == 50.0
    assert result["teleports_collision"] == 1 and result["trip_duration"] == 300.5


def test_config_repeats_the_simulation_options(tmp_path):
    config = simulation.write_config(tmp_path / "c.sumocfg", Path("n.net.xml"), Path("r.rou.xml"), 3600, 300, seed=23)
    text = config.read_text()
    assert '<device.rerouting.probability value="1"/>' in text and '<seed value="23"/>' in text
    assert "collision.action" not in text


def test_green_phases_are_recognised():
    assert signal_timing._is_green("rrGGGg")
    assert not signal_timing._is_green("rryyGg")
    assert not signal_timing._is_green("rrrr")


def test_collision_pairs_are_counted_once():
    line = "Warning: Teleporting vehicle 'x'; collision with vehicle 'y', lane='e_0', gap=-1.00, time=5.00, stage=move."
    assert signal_timing.COLLISION_PAIR_RE.findall(line * 3) == [("x", "y", "e_0")] * 3


def test_stuck_events_carry_vehicle_kind_and_time():
    log = (
        "Warning: Teleporting vehicle 'a'; waited too long (wrong lane), lane='e_0', time=589.00.\n"
        "Warning: Teleporting vehicle 'b'; collision with vehicle 'c', lane='f_1', gap=-2.76, time=895.00, stage=move.\n"
    )
    assert stuck_report.events(log) == [
        {"vehicle": "a", "kind": "wrong lane", "lane": "e_0", "time": 589.0},
        {"vehicle": "b", "kind": "collision", "lane": "f_1", "time": 895.0},
    ]


def test_geh_and_reference_days():
    assert count_check.geh(100, 100) == 0
    assert round(count_check.geh(150, 100), 2) == 4.47
    assert count_check.typical_weekday(dt.date(2024, 3, 12))
    assert not count_check.typical_weekday(dt.date(2024, 3, 11))
    assert not count_check.typical_weekday(dt.date(2024, 7, 9))


def test_lv95_conversion_of_zurich():
    lat, lon = count_check.lv95_to_wgs84(2683009.89, 1243936.2)
    assert abs(lat - 47.3411) < 0.001 and abs(lon - 8.5371) < 0.001


def test_vans_get_their_own_type():
    text = '<routes>\n    <trip id="lcv_1_2_0" type="truck" depart="1"/>\n    <trip id="9_1_0" type="truck" depart="1"/>\n</routes>\n'
    out, count = retype_vans.retype(text)
    assert count == 1 and '<vType id="van"' in out and '"9_1_0" type="truck"' in out
    assert retype_vans.retype(out) == (out, 0)
