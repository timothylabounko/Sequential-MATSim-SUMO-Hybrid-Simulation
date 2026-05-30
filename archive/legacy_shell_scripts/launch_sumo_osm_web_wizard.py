#!/usr/bin/env python3
"""Launch SUMO's OSM Web Wizard with an Overpass-compatible User-Agent."""

from __future__ import annotations

import http.client
import os
import sys


SUMO_ROOT = "/Library/Frameworks/EclipseSUMO.framework/Versions/Current/EclipseSUMO/share/sumo"
SUMO_TOOLS = os.path.join(SUMO_ROOT, "tools")
USER_AGENT = "matsim-sumo-pipeline osmWebWizard"


def install_user_agent_patch() -> None:
    original_request = http.client.HTTPConnection.request

    def request_with_user_agent(self, method, url, body=None, headers=None, *, encode_chunked=False):
        headers = dict(headers or {})
        headers.setdefault("User-Agent", USER_AGENT)
        return original_request(self, method, url, body, headers, encode_chunked=encode_chunked)

    http.client.HTTPConnection.request = request_with_user_agent


def main() -> None:
    os.environ.setdefault("SUMO_HOME", SUMO_ROOT)
    sys.path.insert(0, SUMO_TOOLS)
    install_user_agent_patch()

    import osmWebWizard

    osmWebWizard.main(osmWebWizard.get_options())


if __name__ == "__main__":
    main()
