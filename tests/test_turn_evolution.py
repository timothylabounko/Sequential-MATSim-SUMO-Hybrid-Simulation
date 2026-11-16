import random
import unittest

from matsim_sumo.network.google_turn_search import point_along
from matsim_sumo.network.turn_evolution import (
    Maneuver,
    TomTomReading,
    direct_movement_possible,
    edits_from_readings,
    evolve,
    parse_gridlocks,
    search_turns,
)


LOG = """
Warning: Teleporting vehicle '1'; waited too long (jam), lane='198488290#0_0', time=100.00.
Warning: Teleporting vehicle '2'; waited too long (yield), lane='71167253_0', time=110.00.
Warning: Teleporting vehicle '3'; waited too long (wrong lane), lane='1242400745_0', time=120.00.
Warning: Vehicle '4'; collision with vehicle '5', lane='5198100#0_0', gap=-1.00, time=130.00 stage=move.
Warning: Vehicle '6' performs emergency stop at the end of lane '-566127208#1_1' because there is no connection to the next edge (decel=-12.00, offset=0.10), time=140.00.
"""


class TurnEvolutionTest(unittest.TestCase):
    def test_gridlock_log_keeps_the_five_event_kinds(self) -> None:
        found = parse_gridlocks(LOG)
        self.assertEqual([item.kind for item in found], ["jam", "yield", "wrong lane", "collision", "no connection"])
        self.assertEqual(found[0].edge, "198488290#0")
        self.assertEqual(found[4].edge, "-566127208#1")

    def test_a_short_route_is_the_direct_movement(self) -> None:
        self.assertTrue(direct_movement_possible(120, 100))
        self.assertFalse(direct_movement_possible(900, 100))

    def test_only_a_tomtom_disagreement_becomes_an_edit(self) -> None:
        missing = Maneuver("j", "Merkurstrasse", "a", "b", present=False)
        extra = Maneuver("j", "Zeltweg", "c", "d", present=True)
        kept = Maneuver("j", "Tunnelstrasse", "e", "f", present=True)
        unread = Maneuver("j", "Hegibachplatz", "g", "h", present=False)
        edits = edits_from_readings(
            [missing, extra, kept, unread],
            [
                TomTomReading(True, 80, 70),
                TomTomReading(False, 900, 70),
                TomTomReading(True, 90, 70),
                TomTomReading(None, error="TomTom key missing"),
            ],
        )
        self.assertEqual([(edit.action, edit.maneuver.from_edge) for edit in edits], [("add", "a"), ("delete", "c")])

    def test_missing_readings_stop_before_any_edit(self) -> None:
        maneuver = Maneuver("j", "Merkurstrasse", "a", "b", present=False)
        result = search_turns([maneuver], [TomTomReading(None)], lambda _chromosome: 0, random.Random(1))
        self.assertEqual(result["stopped"], "TomTom key missing")
        self.assertEqual(result["applied"], [])

    def test_point_along_stops_at_the_requested_distance(self) -> None:
        self.assertEqual(point_along([(0, 0), (100, 0)], 40), (40, 0))
        self.assertEqual(point_along([(0, 0), (100, 0)], 40, from_end=True), (60, 0))

    def test_search_keeps_the_subset_that_clears_the_gridlock(self) -> None:
        def fitness(chromosome: tuple[bool, ...]) -> float:
            return 0 if chromosome == (True, False) else 4

        result = evolve(2, fitness, random.Random(2), population_size=6, generations=8, patience=3)
        self.assertEqual(result["best"], (True, False))
        self.assertEqual(result["best_fitness"], 0)
        self.assertEqual(result["stopped"], "zero gridlocks")


if __name__ == "__main__":
    unittest.main()
