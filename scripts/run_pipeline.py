#!/usr/bin/env python3
"""Compatibility wrapper for the packaged command.

Prefer:
    matsim-sumo run --config configs/my_run.yaml
"""

import sys

from matsim_sumo.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["run", *sys.argv[1:]]))
