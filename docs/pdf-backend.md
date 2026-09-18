# The PDF backend

Everything beamer2slides knows about a PDF comes through one small interface,
`beamer2slides.pdf` (`src/beamer2slides/pdf/`). The library behind it is a module the caller
chooses, from outside the package: PDFium in this process (the default), PDFium in a sandboxed
worker process, or anything else that keeps the contract.

| module | what it is |
|---|---|
| `api.py` | the contract: `PdfBackend`, `PdfDocument`, `PdfPage` (protocols), the data they return (`Char`, `PageObject`, `EmbeddedImage`, `Drawing`, `ImageInfo`, `Link`), `PdfError`, and the conventions every backend shares (`char_box`, `trace`, `pixel_bounds`) |
| `pdfium_backend.py` | the reference implementation (pypdfium2); the only module that imports it |
| `sandbox.py` | a backend that runs another backend in a worker process, and that worker (`python -m beamer2slides.pdf.sandbox`) |
| `wire.py` | the worker's wire format: data only |
| `__init__.py` | `Document(path)`, and choosing the backend |

## Choosing the backend

```python
from beamer2slides import pdf
pdf.set_backend(my_backend)            # any object with open(source) -> PdfDocument
with pdf.use_backend("sandbox"):       # or a spec, for a while
    ...
```

or, without touching code, `B2S_PDF_BACKEND`:

| value | backend |
|---|---|
| `pdfium` (default) | PDFium in this process |
| `sandbox` | PDFium in a worker process |
| `sandbox:<spec>` | the backend `<spec>` in a worker process |
| `package.module:attr` | `attr` is a backend object, or a factory returning one |

## The contract

A backend opens a PDF from a path **or its bytes** and answers with plain data: numbers,
strings, tuples, lists, dicts, bytes, numpy arrays and the dataclasses of `api.py`. A page object
is named by its **id** (its index in `PdfPage.objects()`, painting order, form contents after
their form); `Char.obj`, drawings' and images' `object`, `set_active`, `embedded_image` all take
or give ids. No handle, pointer or library object crosses the boundary. That is what lets a
backend live in another process, container or machine.

Coordinates are PDF points from the top left corner of the crop box, y down. The methods:

- document: `len`, `[i]`, iteration, `metadata`, `label(i)`, `named_dests()`, `save(pages, boxes)
  -> bytes` (a new file with some pages, some cut to an area: `notes.py` writes slides.pdf with it),
  `close()`;
- page: `index`, `width`, `height`, `rect`, `objects()`, `object_bounds()`, `set_active(ids, on)`,
  `chars()`, `glyph_widths(requests)`, `drawings()`, `images()`, `embedded_image(id)`, `links()`,
  `render(zoom, clip, transparent)`.

`api.py` documents each one exactly; the conventions the pipeline was tuned on (character boxes
from the font's ascent and descent, `re`/`qu` path items, curves bounded by their extremes,
content order for characters, shadings reported as images on whole points) are part of the
contract, and `api.trace` implements the path ones for any backend that can list path segments.

The requests are batched where the pipeline would otherwise make one call per item:
`object_bounds()` gives every object's box at once (render's eraser looks at all of them) and
`glyph_widths` takes a list (small-caps detection asks per character).

## The sandbox

`B2S_PDF_BACKEND=sandbox` runs the PDF library in a worker process. The client reads the PDF and
sends the bytes, so the worker needs no file system, no network and no credentials; `save` sends
the new file back. How the worker is started is `B2S_PDF_SANDBOX_CMD` (a JSON list or a shell-like
string), which is where a jail goes:

```
B2S_PDF_SANDBOX_CMD='bwrap --ro-bind / / --unshare-all --die-with-parent python -m beamer2slides.pdf.sandbox'
B2S_PDF_SANDBOX_CMD='["docker", "run", "-i", "--rm", "--network=none", "--read-only", "b2s-pdf", "python", "-m", "beamer2slides.pdf.sandbox"]'
```

Requests and answers are `wire` frames on the worker's stdin and stdout: JSON for the structure
plus binary blobs for bytes and arrays, and decoding builds only the api's own types (never
pickle). A worker a hostile PDF took over can answer wrongly, but it cannot send code to the
process that reads it. The worker serves the api's method names and nothing else.

Failure is contained: a request that runs longer than `B2S_PDF_SANDBOX_TIMEOUT` seconds (120) kills
the worker, a dead worker costs the documents it held (`PdfError`), and the next `open` starts a
new one.

Cost, measured on the 48 test PDFs (extract, classify and render): 19 s in process, 25 s through
the sandbox, and **byte-identical** raw.json, deck.json, backgrounds and figures.

## Writing another backend

Implement `api.PdfBackend` and run the conformance suite against it:

```
B2S_TEST_PDF_BACKENDS=pdfium,sandbox,mypkg.mypdf:backend python -m pytest tests/test_pdf_backend.py
```

The suite checks the contract on the test decks (ids, parents and children, character order and
boxes, drawings and images naming the right objects, render sizes, switching objects off and on,
`save`, bytes opening like the file, unreadable input raising `PdfError`). The sandbox is held to
more: every answer must equal PDFium's, value for value. A backend that passes the contract but
answers differently from PDFium (another library measures glyphs and rasterises differently) will
still move classify's decisions; `tests/test_invariants.py` and the decks' digests show how far.
