"""`python -m beamer2slides ...`, and what its Google calls cost written to a JSON file.

    B2S_API_STATS=stats.json python -m beamer2slides.devtools.counted sync new.pdf --deck out/x

The file gets `gslides.STATS` when the command ends (whether it succeeded or not): calls
(attempts), retries, the seconds slept backing off, rate limits hit, and `call <methodId>` per
method. It is how devtools/fuzz_sync.py counts the calls of the convert and sync it runs in a
subprocess of their own. Nothing about the command changes.
"""

import json
import os
import runpy
import time


def main() -> None:
    from beamer2slides import gslides

    path = os.environ.get("B2S_API_STATS")
    started = time.monotonic()
    try:
        runpy.run_module("beamer2slides", run_name="__main__", alter_sys=True)
    finally:
        if path:
            stats = dict(gslides.STATS)
            stats["seconds"] = round(time.monotonic() - started, 2)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
