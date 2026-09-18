"""The test harness that tests import: measurement on Google's renderer (`alignment`), deck edits and
checks for the sync scenarios (`deck_edits`, `sync_check`) and the sync fuzzers (`fuzz_sync`,
`fuzz_world`, `fuzz_labels`, `loss_oracle`).

They live in the package, not in tools/, so a test imports them like any other module, whatever
layout the sources are staged in. Each one still runs as a command: `python tools/<name>.py`
(a shim) or `python -m beamer2slides.devtools.<name>`.
"""
