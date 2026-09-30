"""The deck read as target.json (`deck_ir.deck_ir`), with the arguments a test has no use for left out:
the package's reader takes every one, so a caller cannot forget one."""

from collections.abc import Sequence
from pathlib import Path

from beamer2slides import deck_ir as reader
from beamer2slides.deck_ir import Fetch, Thumbnails
from beamer2slides.json_types import JsonObject


def deck_ir(pres: JsonObject, pdf_size: Sequence[float] | None = None, base: JsonObject | None = None,
            fetch: Fetch | None = None, images: Path | None = None, foreign: bool = False,
            thumbnails: Thumbnails | None = None) -> JsonObject:
    return reader.deck_ir(pres, pdf_size, base, fetch, images, foreign, thumbnails)
