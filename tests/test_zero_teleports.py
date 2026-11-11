import random

import pytest

from matsim_sumo.network import behaviour_calibration as bc
from matsim_sumo.network import zero_teleport_repair as zt


def test_only_collisions_above_the_default_driver_cost_fitness():
    plain = bc.score(0.3, 9.6, 0.0, 10.0, collisions=1, collision_weight=1.0, collision_baseline=1)
    aggressive = bc.score(0.05, 9.0, 0.0, 10.0, collisions=6, collision_weight=1.0, collision_baseline=1)
    assert plain == bc.score(0.3, 9.6, 0.0, 10.0)
    assert plain < aggressive
    assert bc.score(0.3, 9.6, 0.0, 10.0) == bc.score(0.3, 9.6, 0.0, 10.0, collisions=5)


def test_teleport_weight_lets_fewer_teleports_win():
    plain = bc.score(0.31, 9.98, -2.0, 10.0, teleport_weight=10.0)
    fewer_teleports = bc.score(0.21, 9.36, 2.13, 10.0, teleport_weight=10.0)
    assert fewer_teleports < plain


def test_runs_of_several_seeds_are_averaged():
    runs = [
        {"label": "a", "seed": 23, "teleports": 226, "teleported_percent": 0.31, "by_kind": {"jam": 42},
         "travel_time": {"bias_percent": -2.0, "trips": 57}},
        {"label": "a-seed42", "seed": 42, "teleports": 222, "teleported_percent": 0.31, "by_kind": {"jam": 65},
         "travel_time": {"bias_percent": 6.54, "trips": 57}},
    ]
    mean = bc.mean_result(runs)
    assert mean["teleports"] == 224 and mean["by_kind"]["jam"] == 53.5
    assert mean["travel_time"]["bias_percent"] == 2.27
    assert "seed" not in mean and "label" not in mean


def test_only_named_genes_are_searched():
    active = bc.active_genes("impatience,jmTimegapMinor")
    assert [bc.GENES[index].name for index in active] == ["impatience", "jmTimegapMinor"]
    assert bc.active_genes("") == tuple(range(len(bc.GENES)))
    with pytest.raises(ValueError):
        bc.active_genes("impatience,red-light")


def test_a_missing_value_in_one_seed_is_not_averaged():
    runs = [{"travel_time": {"bias_percent": None}}, {"travel_time": {"bias_percent": 3.0}}]
    assert bc.mean_result(runs)["travel_time"]["bias_percent"] is None


class _TwoSeedSearch(bc.Search):
    """Search with SUMO replaced by fixed results: individual 0 has one more collision, but fewer teleports."""

    def evaluate(self, individual, seed=bc.SUMO_SEED, label=None):
        impatience = bc.as_dict(individual)["impatience"]
        result = {
            "teleported_percent": 0.31 - 0.1 * impatience,
            "not_inserted_percent": 9.9,
            "collisions": 1 + impatience + (1 if seed == 42 else 0),
            "teleports": 226,
            "inserted": 72000,
            "teleports_jam": 42,
            "teleports_yield": 171,
            "teleports_wrongLane": 12,
            "teleports_collision": 1,
            "waiting": 7900,
            "loaded": 79978,
            "travel_time": {"bias_percent": 0.0},
        }
        result["fitness"] = self.fitness(result)
        self.runs[(individual, seed)] = result
        return result


def test_two_seed_search_scores_the_mean_and_frees_the_default_collisions():
    import argparse
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        routed = Path(folder) / "routed.rou.xml"
        routed.write_text("<routes/>", encoding="utf-8")
        args = argparse.Namespace(
            additional=[], tolerance=10.0, workers=2, collision_weight=1.0, teleport_weight=10.0, work_dir=Path(folder)
        )
        search = _TwoSeedSearch(args, {}, routed, seeds=(23, 42))
        default = bc.default_individual()
        search.evaluate_all([default])
        assert search.results[default]["collisions"] == 1.5
        assert search.set_collision_baseline(default) == 1.5
        assert search.results[default]["fitness"] == bc.score(0.31, 9.9, 0.0, 10.0, teleport_weight=10.0)
        bolder = tuple(1.0 if gene.name == "impatience" else gene.default for gene in bc.GENES)
        result = search.evaluate_all([bolder])[0]
        assert result["fitness"] == bc.score(0.21, 9.9, 0.0, 10.0, 2.5, 1.0, 1.5, 10.0)
        assert set(search.runs) == {(default, 23), (default, 42), (bolder, 23), (bolder, 42)}


class _Search:
    def __init__(self):
        self.results = {}

    def evaluate_all(self, individuals):
        out = []
        for individual in individuals:
            result = {"label": f"ind{len(self.results):03d}", "fitness": sum(individual)}
            self.results[individual] = result
            out.append(result)
        return out


def test_evolution_keeps_the_other_genes_at_their_default():
    active = bc.active_genes("impatience,jmTimegapMinor")
    search = _Search()
    bc.evolve(search, random.Random(1), population=6, generations=3, active=active)
    default = bc.default_individual()
    assert default in search.results
    for individual in search.results:
        for index, value in enumerate(individual):
            if index not in active:
                assert value == default[index]


def test_pedestrian_lights_do_not_make_a_junction_signal():
    crossing = {"tags": {"highway": "traffic_signals", "crossing": "traffic_signals"}}
    only_crossing = {"tags": {"highway": "traffic_signals", "traffic_signals": "crossing_only"}}
    junction = {"tags": {"highway": "traffic_signals"}}
    assert not zt.junction_signal([crossing, only_crossing, {"tags": {"highway": "stop"}}])
    assert zt.junction_signal([crossing, junction])


def test_signal_links_follow_the_new_numbering():
    old = {0: ("a", 0, "x", 0), 1: ("a", 1, "y", 0), 2: ("b", 0, "x", 0)}
    new = {0: ("a", 0, "x", 0), 1: ("a", 0, "y", 0), 2: ("a", 1, "y", 0), 3: ("b", 0, "x", 0)}
    assert zt.link_mapping(old, new) == [0, 1, 1, 2]
    assert zt.link_mapping(old, {0: ("c", 0, "x", 0)}) is None
    matches = zt.link_matches(old, new)
    assert zt.carried_state("GGr", matches) == "GgGr"
    assert zt.carried_state("ryG", matches) == "ryyG"
