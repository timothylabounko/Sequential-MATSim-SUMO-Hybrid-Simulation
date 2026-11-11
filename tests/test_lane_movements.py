import unittest

from matsim_sumo.network.google_turn_search import blocked_movement, vehicle_routes


class FakeConnection:
    def __init__(self, from_lane, to_edge, to_lane) -> None:
        self.from_lane = from_lane
        self.to_edge = to_edge
        self.to_lane = to_lane

    def getFromLane(self):
        return self.from_lane

    def getTo(self):
        return self.to_edge

    def getToLane(self):
        return self.to_lane


class FakeLane:
    def __init__(self, index: int) -> None:
        self.index = index
        self.outgoing: list[FakeConnection] = []

    def getIndex(self) -> int:
        return self.index

    def allows(self, _vclass: str) -> bool:
        return True

    def getOutgoing(self) -> list[FakeConnection]:
        return self.outgoing


class FakeEdge:
    def __init__(self, edge_id: str, lanes: int, length: float = 50.0) -> None:
        self.edge_id = edge_id
        self.lanes = [FakeLane(index) for index in range(lanes)]
        self.length = length

    def getID(self) -> str:
        return self.edge_id

    def getLanes(self) -> list[FakeLane]:
        return self.lanes

    def getLength(self) -> float:
        return self.length


class FakeNet:
    def __init__(self, lanes: dict[str, int], connections: list[tuple[str, int, str, int]]) -> None:
        self.edges = {edge_id: FakeEdge(edge_id, count) for edge_id, count in lanes.items()}
        for from_id, from_lane, to_id, to_lane in connections:
            source = self.edges[from_id].lanes[from_lane]
            target = self.edges[to_id]
            source.outgoing.append(FakeConnection(source, target, target.lanes[to_lane]))

    def hasEdge(self, edge_id: str) -> bool:
        return edge_id in self.edges

    def getEdge(self, edge_id: str) -> FakeEdge:
        return self.edges[edge_id]


def merge_net() -> FakeNet:
    """Two banned edges whose right lane only leads to the exit ``x``."""
    return FakeNet(
        {"u": 2, "m": 2, "s": 1, "x": 1},
        [("u", 0, "m", 0), ("u", 1, "m", 1), ("m", 0, "x", 0), ("m", 1, "s", 0)],
    )


class BlockedMovementTest(unittest.TestCase):
    def test_a_vehicle_held_in_the_exit_lane_by_bans(self) -> None:
        self.assertEqual(blocked_movement(merge_net(), ["u", "m", "s"], "u_0", {"u", "m"}), ("m", 0, "s"))

    def test_no_movement_when_the_vehicle_may_change_lanes(self) -> None:
        self.assertIsNone(blocked_movement(merge_net(), ["u", "m", "s"], "u_0", {"u"}))

    def test_no_movement_when_the_lane_leads_along_the_route(self) -> None:
        self.assertIsNone(blocked_movement(merge_net(), ["u", "m", "x"], "u_0", {"u", "m"}))

    def test_vehicle_routes_reads_duarouter_output(self) -> None:
        import tempfile
        from pathlib import Path

        text = (
            '<routes>\n    <vehicle id="a" depart="0.00">\n        <route edges="u m s"/>\n    </vehicle>\n'
            '    <vehicle id="b" depart="1.00">\n        <route edges="u m x"/>\n    </vehicle>\n</routes>\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "r.rou.xml"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(vehicle_routes(path, {"a"}), {"a": ["u", "m", "s"]})


if __name__ == "__main__":
    unittest.main()
