import random
import unittest

from matsim_sumo.network.osm_connectivity import (
    connectivity_problems,
    junction_node_ids,
    matching_exits,
    osm_way_id,
)
from matsim_sumo.network.turn_evolution import evolve


class OsmConnectivityTest(unittest.TestCase):
    def test_way_and_node_ids_come_from_sumo_names(self) -> None:
        self.assertEqual(osm_way_id("-1056261634#0"), 1056261634)
        self.assertEqual(osm_way_id("plain"), None)
        self.assertEqual(junction_node_ids("cluster_12_34"), [12, 34])
        self.assertEqual(junction_node_ids(":internal"), [])

    def test_only_an_osm_exit_is_a_continuation(self) -> None:
        exits = [("main", 10), ("side", 20), ("back", None)]
        self.assertEqual(matching_exits(1, exits, {10, 99}), ["main"])
        self.assertEqual(matching_exits(10, exits, {10}), [])

    def test_a_split_or_a_dead_end_is_rejected(self) -> None:
        self.assertEqual(
            connectivity_problems(unresolved=["a"], components=8, baseline_components=7),
            ["1 links lead nowhere", "the network split from 7 pieces into 8"],
        )
        self.assertEqual(connectivity_problems(unresolved=[], components=7, baseline_components=7), [])
        self.assertEqual(
            connectivity_problems(unresolved=[], components=7, baseline_components=7, drive_components=9),
            ["the drivable network is in 9 pieces while the roads form 7"],
        )

    def test_a_generation_that_fails_the_osm_review_is_not_kept(self) -> None:
        def fitness(chromosome: tuple[bool, ...]) -> float:
            return 0 if chromosome == (True,) else 5

        result = evolve(
            1,
            fitness,
            random.Random(0),
            population_size=4,
            generations=4,
            patience=3,
            review_fn=lambda chromosome: chromosome == (True,),
        )
        self.assertEqual(result["best"], (True,))
        self.assertEqual(result["best_fitness"], 0)
        self.assertTrue(all(item["accepted"] for item in result["generations"] if item["best"] == 0))
        self.assertTrue(all(item["checked"] >= 1 for item in result["generations"]))

    def test_every_individual_is_reviewed_and_a_rejected_score_cannot_win(self) -> None:
        calls: list[tuple[str, tuple[bool, ...]]] = []

        def fitness(chromosome: tuple[bool, ...]) -> float:
            calls.append(("fitness", chromosome))
            return 0 if chromosome == (False,) else 3

        def review(chromosome: tuple[bool, ...]) -> bool:
            calls.append(("review", chromosome))
            return chromosome == (True,)

        result = evolve(
            1,
            fitness,
            random.Random(1),
            population_size=4,
            generations=3,
            patience=3,
            review_fn=review,
        )
        reviewed = [chromosome for kind, chromosome in calls if kind == "review"]
        self.assertIn((False,), reviewed)
        self.assertIn((True,), reviewed)
        self.assertEqual(len(calls) % 2, 0)
        for index in range(0, len(calls), 2):
            self.assertEqual(calls[index], ("fitness", calls[index + 1][1]))
            self.assertEqual(calls[index + 1][0], "review")
        self.assertEqual(result["best"], (True,))
        self.assertEqual(result["best_fitness"], 3)
        self.assertNotEqual(result["best"], (False,))
        self.assertTrue(all(item["checked"] == 4 for item in result["generations"]))
        self.assertGreaterEqual(sum(item["rejected"] for item in result["generations"]), 1)


if __name__ == "__main__":
    unittest.main()
