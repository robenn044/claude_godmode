#!/usr/bin/env python3
"""GODMODE swarm engine entry point (Python 3.8+, standard library only).

    python3 godmode_engine.py preflight
    python3 godmode_engine.py validate --run-dir DIR [--baseline]
    python3 godmode_engine.py run      --run-dir DIR [--pilot N | --dry-run] [options]
    python3 godmode_engine.py status   --run-dir DIR
    python3 godmode_engine.py apply    --run-dir DIR
    python3 godmode_engine.py clean    --run-dir DIR

Run `python3 godmode_engine.py run --help` for every option.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from godmode.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
