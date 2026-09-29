"""The deck read as target.json (`deck_ir.deck_ir`), with the arguments a test has no use for left out:
the package's reader takes every one, so a caller cannot forget one."""

from beamer2slides import deck_ir as reader


def deck_ir(pres, pdf_size=None, base=None, fetch=None, images=None, foreign=False, thumbnails=None) -> dict:
    return reader.deck_ir(pres, pdf_size, base, fetch, images, foreign, thumbnails)
