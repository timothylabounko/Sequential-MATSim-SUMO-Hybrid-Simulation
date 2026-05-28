import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from matsim_sumo.disk_cache import disk_cache, file_dep, output_needs_refresh, param_dep


class DiskCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.tmp.name)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _quiet(self, fn, *args, **kwargs):
        with redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def test_cache_miss_then_hit(self) -> None:
        calls = []

        @disk_cache(depends_on=lambda value: [param_dep("value", value)], step_name="basic")
        def compute(value):
            calls.append(value)
            return value * 2

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(compute(3), 6)
            self.assertEqual(compute(3), 6)

        self.assertEqual(calls, [3])
        self.assertIn("CACHE MISS basic", out.getvalue())
        self.assertIn("CACHE HIT basic", out.getvalue())

    def test_param_change_changes_key(self) -> None:
        calls = []

        @disk_cache(depends_on=lambda value: [param_dep("value", value)], step_name="param_change")
        def compute(value):
            calls.append(value)
            return len(calls)

        self.assertEqual(self._quiet(compute, "a"), 1)
        self.assertEqual(self._quiet(compute, "a"), 1)
        self.assertEqual(self._quiet(compute, "b"), 2)
        self.assertEqual(calls, ["a", "b"])

    def test_file_fingerprint_change_changes_key(self) -> None:
        dep = Path("input.txt")
        dep.write_text("one", encoding="utf-8")
        calls = []

        @disk_cache(depends_on=lambda: [file_dep(dep)], step_name="file_change")
        def compute():
            calls.append(dep.read_text(encoding="utf-8"))
            return len(calls)

        self.assertEqual(self._quiet(compute), 1)
        self.assertEqual(self._quiet(compute), 1)
        dep.write_text("changed contents", encoding="utf-8")
        self.assertEqual(self._quiet(compute), 2)
        self.assertEqual(calls, ["one", "changed contents"])

    def test_no_cache_bypasses_load_and_save(self) -> None:
        calls = []

        @disk_cache(depends_on=lambda value: [param_dep("value", value)], step_name="bypass")
        def compute(value):
            calls.append(value)
            return len(calls)

        self.assertEqual(self._quiet(compute, 1, no_cache=True), 1)
        self.assertEqual(self._quiet(compute, 1, no_cache=True), 2)
        self.assertEqual(self._quiet(compute, 1), 3)
        self.assertEqual(self._quiet(compute, 1), 3)

    def test_state_config_no_cache_bypasses_cache(self) -> None:
        class State:
            config = {"no_cache": True}

        calls = []

        @disk_cache(depends_on=lambda state: [param_dep("same", True)], step_name="state_bypass")
        def compute(state):
            calls.append(state)
            return len(calls)

        state = State()
        self.assertEqual(self._quiet(compute, state), 1)
        self.assertEqual(self._quiet(compute, state), 2)

    def test_corrupt_pickle_is_treated_as_miss(self) -> None:
        calls = []

        @disk_cache(depends_on=lambda: [param_dep("same", True)], step_name="corrupt")
        def compute():
            calls.append("call")
            return len(calls)

        self.assertEqual(self._quiet(compute), 1)
        [cache_file] = Path(".cache").glob("corrupt-*.pkl")
        cache_file.write_text("not a pickle", encoding="utf-8")
        self.assertEqual(self._quiet(compute), 2)
        self.assertEqual(calls, ["call", "call"])

    def test_output_needs_refresh_only_on_hit_when_missing(self) -> None:
        out_path = Path("artifact.txt")

        @disk_cache(depends_on=lambda: [param_dep("same", True)], step_name="artifact")
        def compute():
            return "value"

        self.assertEqual(self._quiet(compute), "value")
        self.assertTrue(output_needs_refresh(compute, out_path))
        out_path.write_text("exists", encoding="utf-8")
        self.assertEqual(self._quiet(compute), "value")
        self.assertFalse(output_needs_refresh(compute, out_path))
        out_path.unlink()
        self.assertTrue(output_needs_refresh(compute, out_path))


if __name__ == "__main__":
    unittest.main()
