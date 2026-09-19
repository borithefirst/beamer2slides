"""Transfer functions as PDFium applies them (chromium/7999).

- ExtGState /TR2, else /TR (cpdf_allstates.cpp; a name clears it): CPDF_DocRenderData::
  CreateTransferFunc samples the function(s) at `float(v) / 255` into three 256-byte tables, and
  CPDF_RenderStatus::GetFillArgb / GetStrokeArgb run the colour through them (TranslateColor).
  Shadings and shading patterns are drawn without it. Its quirks are kept: the three functions of an
  array land in the tables in reverse (the first one maps blue), one `output[16]` is shared by every
  call (a call that fails keeps what the last one wrote), and the rounded sample is stored as its
  low byte.
- A soft mask's /TR (CPDF_RenderStatus::LoadSMask): one function, only when it is a dictionary or a
  stream, sampled at `i / 255` into `transfers`, which maps the luminosity or the alpha.
"""

from __future__ import annotations

from .render_shading import Unsupported, _Access, load_function, roundf
from .syntax import Stream
from .syntax import float32 as F

MAX_OUTPUTS = 16


class Transfer:
    """CPDF_TransferFunc: `identity` and the r, g, b sample tables."""

    def __init__(self, identity: bool, r: list, g: list, b: list):
        self.identity, self.r, self.g, self.b = identity, r, g, b

    def translate(self, rgb: int) -> int:
        """TranslateColor on a 0xRRGGBB colour."""
        return (self.r[(rgb >> 16) & 0xFF] << 16) | (self.g[(rgb >> 8) & 0xFF] << 8) | self.b[rgb & 0xFF]


def _call(f, x: float, out: list) -> None:
    try:
        f.call([x], out, 0)
    except (IndexError, ValueError, ZeroDivisionError, OverflowError, RecursionError) as e:
        raise Unsupported(f"a transfer function PDFium would read past its outputs ({type(e).__name__})")


def create(doc, obj) -> Transfer | None:
    """CPDF_DocRenderData::CreateTransferFunc."""
    a = _Access(doc)
    if isinstance(obj, list):
        if len(obj) < 3:
            return None
        funcs = [None, None, None]
        for i in range(3):
            funcs[2 - i] = load_function(a, a.r(obj[i]), set())
            if funcs[2 - i] is None:
                return None
    else:
        f = load_function(a, obj, set())
        if f is None:
            return None
        funcs = [f]
    output = [0.0] * MAX_OUTPUTS
    identity = True
    samples = [[0] * 256 for _ in range(3)]
    for v in range(256):
        x = F(v / 255.0)
        if len(funcs) == 3:
            for i in range(3):
                if funcs[i].outputs > MAX_OUTPUTS:
                    samples[i][v] = v
                    continue
                _call(funcs[i], x, output)
                o = roundf(F(F(output[0]) * 255.0))
                if o != v:
                    identity = False
                samples[i][v] = o & 0xFF
        else:
            if funcs[0].outputs <= MAX_OUTPUTS:
                _call(funcs[0], x, output)
            o = roundf(F(F(output[0]) * 255.0))
            if o != v:
                identity = False
            for ch in samples:
                ch[v] = o & 0xFF
    return Transfer(identity, *samples)


def of(doc, obj) -> Transfer | None:
    """CPDF_DocRenderData::GetTransferFunc: `create`, once per object of the document."""
    cache = getattr(doc, "_b2s_transfer_cache", None)
    if cache is None:
        cache = {}
        try:
            doc._b2s_transfer_cache = cache
        except AttributeError:
            pass
    hit = cache.get(id(obj))
    if hit is not None and hit[0] is obj:
        return hit[1]
    t = create(doc, obj)
    cache[id(obj)] = (obj, t)
    return t


def smask_table(doc, tr) -> list | None:
    """LoadSMask's `transfers` for a soft mask whose /TR is `tr` (resolved), or None for none."""
    if not isinstance(tr, (dict, Stream)):
        return None
    f = load_function(_Access(doc), tr, set())
    if f is None:
        return None
    if f.outputs < 1:
        raise Unsupported("a soft mask transfer function with no outputs")
    results = [0.0] * f.outputs
    table = []
    for i in range(256):
        _call(f, F(i / 255.0), results)
        table.append(roundf(F(F(results[0]) * 255.0)) & 0xFF)
    return table
