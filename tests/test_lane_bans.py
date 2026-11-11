import unittest

from matsim_sumo.network.google_turn_search import banned_edges, lane_ban_groups, lift_lane_bans


class FakeEdge:
    def __init__(self, edge_id: str, length: float) -> None:
        self.edge_id = edge_id
        self.length = length
        self.outgoing: dict = {}
        self.incoming: list = []

    def getID(self) -> str:
        return self.edge_id

    def getLength(self) -> float:
        return self.length

    def getOutgoing(self) -> dict:
        return self.outgoing

    def getIncoming(self) -> list:
        return self.incoming


class FakeNet:
    def __init__(self, lengths: dict[str, float], links: list[tuple[str, str]]) -> None:
        self.edges = {edge_id: FakeEdge(edge_id, length) for edge_id, length in lengths.items()}
        for from_id, to_id in links:
            self.edges[from_id].outgoing[self.edges[to_id]] = []
            self.edges[to_id].incoming.append(self.edges[from_id])

    def hasEdge(self, edge_id: str) -> bool:
        return edge_id in self.edges

    def getEdge(self, edge_id: str) -> FakeEdge:
        return self.edges[edge_id]


NET_TEXT = """<net>
    <edge id="a">
        <lane id="a_0" index="0" speed="33.33" length="200.00" shape="0,0 200,0"/>
    </edge>
    <edge id="b">
        <lane id="b_0" index="0" changeLeft="authority" changeRight="authority" speed="33.33" length="30.00" shape="200,0 230,0"/>
        <lane id="b_1" index="1" changeRight="authority" speed="33.33" length="30.00" shape="200,3 230,3"/>
    </edge>
    <edge id="c">
        <lane id="c_0" index="0" changeLeft="authority" speed="33.33" length="40.00" shape="230,0 270,0"/>
    </edge>
    <edge id="far">
        <lane id="far_0" index="0" changeLeft="authority" speed="33.33" length="50.00" shape="2000,0 2050,0"/>
    </edge>
</net>
"""


class LaneBanTest(unittest.TestCase):
    def test_banned_edges_reads_change_attributes(self) -> None:
        self.assertEqual(banned_edges(NET_TEXT), {"b", "c", "far"})

    def test_a_contiguous_stretch_downstream_becomes_one_gene(self) -> None:
        net = FakeNet(
            {"a": 200, "b": 30, "c": 40, "d": 450, "far": 50},
            [("a", "b"), ("b", "c"), ("c", "d"), ("d", "far")],
        )
        groups = lane_ban_groups(net, {"b", "c", "far"}, ["a", "b", "missing"])
        self.assertEqual(groups, [("b", "c")])

    def test_a_ban_beyond_the_lookahead_is_not_a_gene(self) -> None:
        net = FakeNet({"a": 600, "far": 50}, [("a", "far")])
        self.assertEqual(lane_ban_groups(net, {"far"}, ["a"]), [])

    def test_lifting_only_touches_the_chosen_edges(self) -> None:
        lifted = lift_lane_bans(NET_TEXT, {"b", "c"})
        self.assertEqual(banned_edges(lifted), {"far"})
        self.assertIn('<lane id="b_0" index="0" speed="33.33" length="30.00" shape="200,0 230,0"/>', lifted)
        self.assertEqual(lift_lane_bans(NET_TEXT, set()), NET_TEXT)


if __name__ == "__main__":
    unittest.main()
