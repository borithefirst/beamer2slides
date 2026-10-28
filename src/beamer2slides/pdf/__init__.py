"""PDF access through a swappable backend (docs/pdf-backend.md).

The pipeline only ever calls `Document(path)`, which opens the file with the backend in use, and
then the methods of `api.PdfDocument` / `api.PdfPage`. Which backend that is decides the caller,
from outside the library:

- `set_backend(backend)` / `with use_backend(backend):` - any object with `open(source)`;
- `$B2S_PDF_BACKEND`, read on first use: `pdfium` (the default, in this process), `sandbox`
  (PDFium in a worker process, `sandbox.py`; `sandbox:<spec>` runs another backend there), `pure`
  (pure Python, `pure/`: no rendering, docs/pdf-from-scratch.md), or `package.module:attribute`
  naming a backend object or a factory that returns one.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from .api import (COLOR_SPACES, JPEG_MAGIC, LIGATURES, NO_OBJECT, OBJ_FORM, OBJ_IMAGE, OBJ_PATH,  # noqa: F401
                  OBJ_SHADING, OBJ_TEXT, PNG_MAGIC, Box, Char, Drawing, EmbeddedImage, ImageInfo, Link,
                  PageObject, PdfBackend, PdfDocument, PdfError, PdfPage, char_box, renders)

Page = PdfPage  # for annotations

ENV = "B2S_PDF_BACKEND"
_backend: PdfBackend | None = None


def resolve(spec: str | None) -> PdfBackend:
    """A backend from its name: see the module docstring."""
    spec = (spec or "pdfium").strip()
    if spec == "pdfium":
        from .pdfium_backend import PdfiumBackend
        return PdfiumBackend()
    if spec == "pure":
        from .pure.backend import PureBackend
        return PureBackend()
    if spec == "sandbox" or spec.startswith("sandbox:"):
        from .sandbox import SandboxBackend
        return SandboxBackend(inner=spec.partition(":")[2] or "pdfium", command=None, timeout=None)
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise ValueError(f"{ENV}={spec!r}: expected pdfium, sandbox[:<spec>] or package.module:attribute")
    found = getattr(importlib.import_module(module), attr)
    backend = found if callable(getattr(found, "open", None)) else found()
    if not callable(getattr(backend, "open", None)):
        raise TypeError(f"{spec} gives {backend!r}, which has no open(source)")
    return backend


def backend() -> PdfBackend:
    global _backend
    if _backend is None:
        _backend = resolve(os.environ.get(ENV))
    return _backend


def set_backend(new: PdfBackend | str | None) -> PdfBackend | None:
    """Use `new` from now on (a backend, a spec as for $B2S_PDF_BACKEND, or None for the
    environment's choice again). Returns the backend that was in use."""
    global _backend
    old = _backend
    _backend = resolve(new) if isinstance(new, str) else new
    return old


@contextmanager
def use_backend(new: PdfBackend | str):
    old = set_backend(new)
    try:
        yield _backend
    finally:
        set_backend(old)


def join_files(paths: Sequence[Path], out: Path) -> int:
    """The PDFs at `paths` written as one to `out`, page after page (`pure.join`, docs/pdf-backend.md):
    its page count. A file that does not read, or holds outlines, raises PdfError."""
    from .pure.document import PdfFileError
    from .pure.join import JoinError, join
    from .pure.syntax import PdfSyntaxError
    try:
        joined = join([str(p) for p in paths])
    except (OSError, PdfFileError, PdfSyntaxError, JoinError) as e:
        raise PdfError(f"cannot join {len(paths)} files: {e}") from e
    out.write_bytes(joined.data)
    return joined.pages


def join_file_pages(paths: Sequence[Path], order: Sequence[tuple[int, int]], out: Path) -> int:
    """Pages of the PDFs at `paths` written as one to `out`, `order` naming each (file, its page
    index) (`pure.join.join_pages`): its page count. `out` may be one of `paths`. A file that does
    not read, holds outlines, or a kept page naming one left out raises PdfError."""
    from .pure.document import PdfFileError
    from .pure.join import JoinError, join_pages
    from .pure.syntax import PdfSyntaxError
    try:
        joined = join_pages([str(p) for p in paths], order)
    except (OSError, PdfFileError, PdfSyntaxError, JoinError) as e:
        raise PdfError(f"cannot join pages of {len(paths)} files: {e}") from e
    out.write_bytes(joined.data)
    return joined.pages


def Document(source: str | Path | bytes) -> PdfDocument:  # noqa: N802 - it stands for the document type
    """Open a PDF (path or bytes) with the backend in use."""
    return backend().open(source)
