import datetime as dt
import tempfile
from pathlib import Path

from matsim_sumo.network import behaviour_calibration as bc
from matsim_sumo.network import city_config
from matsim_sumo.network import persistent_junctions as pj

CITY = """
[city]
name = "testcity"
timezone = "Europe/London"
lefthand = true

[area]
west = -0.2
south = 51.4
east = 0.0
north = 51.6

[inputs]
work_dir = "work"
matsim_dir = "matsim"

[demand]
levels = [0.25, 0.5, 1.0]
sample_share = 0.1
"""


def test_a_city_file_fills_defaults_and_makes_paths_absolute():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "city.toml"
        path.write_text(CITY)
        config = city_config.load(path)
        exports = "\n".join(city_config.shell_exports(config))
    assert config["inputs"]["work_dir"] == str((Path(tmp) / "work").resolve())
    assert config["inputs"]["matsim_plans"].endswith("matsim/output_plans.xml.gz")
    assert "export CITY_LEFTHAND=1" in exports
    assert "export DEMAND_LEVELS='0.25 0.5 1'" in exports
    assert "export CITY_AREA=-0.2,51.4,0.0,51.6" in exports


def test_a_city_file_must_end_at_full_demand_and_name_a_time_zone():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "city.toml"
        path.write_text(CITY.replace("levels = [0.25, 0.5, 1.0]", "levels = [0.25, 0.5]").replace("Europe/London", "Nowhere"))
        try:
            city_config.load(path)
        except ValueError as exc:
            message = str(exc)
        else:
            message = ""
    assert "end with 1.0" in message and "time zone" in message


def test_google_morning_is_in_the_city_time_zone():
    now = dt.datetime(2026, 11, 11, 12, 0, tzinfo=dt.timezone.utc)
    morning = bc.next_weekday_morning(now, "America/New_York", dt.time(8, 30))
    assert morning.weekday() == 1 and morning.hour == 8 and morning.minute == 30
    assert morning.utcoffset() == dt.timedelta(hours=-5)


class Node:
    def __init__(self, node_id, kind="priority"):
        self.node_id, self.kind = node_id, kind

    def getID(self):
        return self.node_id

    def getType(self):
        return self.kind


class Edge:
    def __init__(self, edge_id, a, b, length, name=""):
        self.edge_id, self.a, self.b, self.length, self.name = edge_id, a, b, length, name

    def getID(self):
        return self.edge_id

    def getFunction(self):
        return ""

    def getLength(self):
        return self.length

    def getFromNode(self):
        return self.a

    def getToNode(self):
        return self.b

    def getName(self):
        return self.name


class Net:
    def __init__(self, edges):
        self.edges = {edge.getID(): edge for edge in edges}
        self.nodes = {n.getID(): n for edge in edges for n in (edge.a, edge.b)}

    def getEdges(self):
        return list(self.edges.values())

    def hasEdge(self, edge_id):
        return edge_id in self.edges

    def getEdge(self, edge_id):
        return self.edges[edge_id]

    def hasNode(self, node_id):
        return node_id in self.nodes


def test_a_short_link_is_joined_only_where_both_runs_stopped_vehicles():
    a, b, c, signal = Node("a"), Node("b"), Node("c"), Node("s", "traffic_light")
    net = Net([
        Edge("approach", c, a, 120), Edge("stub", a, b, 3.5, "Dufourplatz"),
        Edge("back", b, a, 3.5), Edge("to_signal", b, signal, 8),
    ])
    stuck_a = [{"lane": "approach_0"}, {"lane": ":b_0_0"}]
    stuck_b = [{"lane": "approach_0"}, {"lane": "stub_0"}, {"lane": "approach_1"}]
    joins = pj.short_link_joins(net, [stuck_a, stuck_b])
    assert [row["nodes"] for row in joins] == [("a", "b")]
    assert joins[0]["stuck_per_run"] == [2, 3]
    assert pj.short_link_joins(net, [stuck_a, [{"lane": "approach_0"}]]) == []


def test_join_groups_merge_chains_of_short_links():
    groups = pj.join_groups([("a", "b"), ("b", "c"), ("x", "y")])
    assert sorted(map(sorted, groups)) == [["a", "b", "c"], ["x", "y"]]


def test_lane_change_bans_are_lifted_up_to_the_distance_before_the_stop():
    a, b = Node("a"), Node("b")
    net = Net([Edge(name, a, b, 300) for name in ("e1", "e2", "e3", "e4")])
    banned = {"e1", "e3", "e4"}
    assert pj.bans_before(net, ["e1", "e2", "e3", "e4"], "e4", banned, upstream=500) == ["e4", "e3"]
    assert pj.bans_before(net, [], "e4", banned) == ["e4"]
