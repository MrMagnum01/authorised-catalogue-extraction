"""Standalone fixture runner for manual poking: `python -m catalogue_fixture`."""
from __future__ import annotations

import signal
import sys

from .data import generate_products
from .site import FixtureServer, FixtureState


def main() -> int:
    state = FixtureState(products=generate_products())
    server = FixtureServer(state).start()
    print(f"fixture serving at {server.origin}  (Ctrl+C to stop)")
    print(f"  {server.origin}/PERMISSION.md")
    print(f"  {server.origin}/robots.txt")
    print(f"  {server.origin}/catalogue/")

    def _stop(*_a):
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    signal.pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
