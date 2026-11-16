"""Give the eqasim light commercial vehicles in an existing trips file the van type of plans_to_sumo.py.

Trips written before the van type existed drove ``lcv_*`` agents as 12 m trucks.
The file is changed in place; a file that already has the van type is left as it is.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VAN = '    <vType id="van" vClass="passenger" length="6.5" guiShape="delivery" color="255,140,0"/>\n'
LCV_TRIP = re.compile(r'(<trip id="lcv_[^"]*" type=")truck"')


def retype(text: str) -> tuple[str, int]:
    if '<vType id="van"' in text:
        return text, 0
    text, count = LCV_TRIP.subn(r'\1van"', text)
    return text.replace("<routes>\n", "<routes>\n" + VAN, 1), count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trips", type=Path)
    path = parser.parse_args().trips
    text, count = retype(path.read_text(encoding="utf-8"))
    if count:
        path.write_text(text, encoding="utf-8")
    print(f"{path}: {count} light commercial vehicle trips set to the van type")


if __name__ == "__main__":
    main()
