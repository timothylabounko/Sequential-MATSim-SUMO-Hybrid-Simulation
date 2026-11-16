import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plans_to_sumo.py"
spec = importlib.util.spec_from_file_location("plans_to_sumo", SCRIPT)
plans_to_sumo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plans_to_sumo)


class MainComponentTest(unittest.TestCase):
    def test_a_one_way_exit_is_not_in_the_main_network(self) -> None:
        successors = {"a": {"b"}, "b": {"c"}, "c": {"a", "out"}}
        self.assertEqual(plans_to_sumo.main_component(["a", "b", "c", "out"], successors), {"a", "b", "c"})


class TrimPieceTest(unittest.TestCase):
    main = {"a", "b", "c", "-c"}

    def test_ends_off_the_main_network_are_trimmed(self) -> None:
        piece = [(0.0, "in"), (5.0, "a"), (9.0, "b"), (12.0, "out")]
        self.assertEqual(plans_to_sumo.trim_piece(piece, self.main), ([(5.0, "a"), (9.0, "b")], "trimmed"))

    def test_both_directions_of_one_street_only_touch_the_boundary(self) -> None:
        piece = [(0.0, "c"), (4.0, "-c")]
        self.assertEqual(plans_to_sumo.trim_piece(piece, self.main), (None, "only touches the boundary"))

    def test_a_piece_left_with_one_edge_is_dropped(self) -> None:
        piece = [(0.0, "in"), (3.0, "a"), (6.0, "out")]
        self.assertEqual(plans_to_sumo.trim_piece(piece, self.main), (None, "too short after trimming"))

    def test_a_piece_entirely_off_the_network_is_dropped(self) -> None:
        self.assertEqual(plans_to_sumo.trim_piece([(0.0, "x"), (1.0, "y")], self.main), (None, "off the main network"))


if __name__ == "__main__":
    unittest.main()
