import random
import tempfile
import threading
import time
import unittest
from pathlib import Path

from matsim_sumo.network.carry_edits import best_network, edits_between
from matsim_sumo.network.google_turn_search import network_errors_first
from matsim_sumo.network.national_repair import carry_signals_onto, signal_links_of
from matsim_sumo.network.turn_evolution import evolve, line_kind, parse_gridlocks, weighted_score

LOG = """
Warning: Teleporting vehicle '1'; waited too long (jam), lane='a_0', time=100.00.
Warning: Teleporting vehicle '2'; waited too long (jam), lane='a_0', time=105.00.
Warning: Teleporting vehicle '3'; waited too long (yield), lane='b_0', time=110.00.
Warning: Teleporting vehicle '4'; waited too long (wrong lane), lane='c_1', time=120.00.
Warning: Vehicle '6' performs emergency stop at the end of lane 'd_1' because there is no connection to the next edge (decel=-12.00, offset=0.10), time=140.00.
"""


def net_text(connections: str, lanes: str = "") -> str:
    return f"<net>\n{lanes}\n{connections}\n</net>\n"


class LevelRepairTest(unittest.TestCase):
    def test_network_errors_and_unroutable_trips_count_the_weight(self) -> None:
        gridlocks = parse_gridlocks(LOG)
        self.assertEqual(weighted_score(gridlocks, 0), 5)
        self.assertEqual(weighted_score(gridlocks, 2), 7)
        self.assertEqual(weighted_score(gridlocks, 2, network_weight=10), 3 + 2 * 10 + 2 * 10)

    def test_each_log_line_has_the_kind_parse_gridlocks_gives_it(self) -> None:
        kinds = [line_kind(line) for line in LOG.splitlines() if line_kind(line)]
        self.assertEqual(kinds, [item.kind for item in parse_gridlocks(LOG)])
        self.assertIsNone(line_kind("Warning: Vehicle '9' ends teleporting on edge 'x', time=1.00."))

    def test_approaches_with_a_network_error_are_searched_first(self) -> None:
        self.assertEqual(network_errors_first(LOG), ["c", "d", "a", "b"])

    def test_parallel_scoring_finds_the_same_individuals(self) -> None:
        active = []
        lock = threading.Lock()

        def fitness(chromosome: tuple[bool, ...]) -> float:
            with lock:
                active.append(1)
                most = len(active)
            time.sleep(0.01)
            with lock:
                active.pop()
            fitness.most = max(getattr(fitness, "most", 0), most)
            return 10 - 3 * chromosome[0] - 2 * chromosome[2] + chromosome[3]

        serial = evolve(5, fitness, random.Random(4), population_size=8, generations=4, patience=3)
        fitness.most = 0
        parallel = evolve(5, fitness, random.Random(4), population_size=8, generations=4, patience=3, workers=4)
        self.assertEqual(serial, parallel)
        self.assertGreater(fitness.most, 1)

    def test_edits_are_the_connections_and_bans_that_differ(self) -> None:
        base = net_text(
            '<connection from="a" to="b" fromLane="0" toLane="0"/>\n<connection from="a" to="c" fromLane="0" toLane="0"/>',
            '<lane id="a_0" changeLeft="none"/>\n<lane id="b_0"/>',
        )
        edited = net_text(
            '<connection from="a" to="b" fromLane="0" toLane="0"/>\n<connection from="b" to="c" fromLane="0" toLane="0"/>\n'
            '<connection from=":j_0" to="b" fromLane="0" toLane="0"/>',
            '<lane id="a_0"/>\n<lane id="b_0"/>',
        )
        edits = edits_between(base, edited)
        self.assertEqual(edits["added"], [("b", "c", 0, 0)])
        self.assertEqual(edits["deleted"], [("a", "c", 0, 0)])
        self.assertEqual(edits["bans_lifted"], ["a"])

    def test_an_interrupted_level_carries_its_best_round(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            level = Path(folder)
            for number, log in ((1, LOG), (2, LOG.splitlines()[1] + "\n"), (3, LOG)):
                round_dir = level / f"round{number}"
                round_dir.mkdir()
                (round_dir / "census.log").write_text(log, encoding="utf-8")
                (round_dir / "osm.net.xml").write_text("<net/>", encoding="utf-8")
            self.assertEqual(best_network(level), level / "round2" / "osm.net.xml")

    def test_signals_follow_a_renumbered_junction(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            base = folder / "base.net.xml"
            base.write_text(
                net_text(
                    '<connection from="a" to="b" fromLane="0" toLane="0" tl="J" linkIndex="0"/>\n'
                    '<connection from="c" to="b" fromLane="0" toLane="0" tl="J" linkIndex="1"/>'
                ),
                encoding="utf-8",
            )
            edited = folder / "edited.net.xml"
            edited.write_text(
                net_text(
                    '<connection from="c" to="b" fromLane="0" toLane="0" tl="J" linkIndex="0"/>\n'
                    '<connection from="a" to="b" fromLane="0" toLane="0" tl="J" linkIndex="1"/>'
                ),
                encoding="utf-8",
            )
            signals = folder / "signals.add.xml"
            signals.write_text(
                '<additional><tlLogic id="J" type="actuated" programID="w" offset="0">'
                '<phase duration="30" state="Gr"/><phase duration="30" state="rG"/></tlLogic></additional>',
                encoding="utf-8",
            )
            links = signal_links_of(base)
            self.assertIs(carry_signals_onto(links, signals, base, folder / "same.add.xml"), signals)
            carried = carry_signals_onto(links, signals, edited, folder / "carried.add.xml")
            text = carried.read_text(encoding="utf-8")
            self.assertIn('state="rG"', text.split("<phase")[1])
            self.assertIn('state="Gr"', text.split("<phase")[2])


if __name__ == "__main__":
    unittest.main()
