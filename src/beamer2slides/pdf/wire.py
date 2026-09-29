"""The wire format between `sandbox`'s client and its worker: data only, nothing executable.

A frame is `<u32 json length><u32 blob count>`, the JSON, then each blob as `<u64 length><bytes>`.
JSON carries None, booleans, numbers, strings and lists as themselves; everything else is a
one-key object:

    {"T": [...]}                      tuple
    {"D": [[key, value], ...]}        dict (any keys the wire can carry)
    {"B": n}                          bytes: blob n
    {"A": [dtype, shape, n]}          numpy array of numbers: blob n (dtype like "|u1", "<f8")
    {"C": [name, {field: value}]}     one of the api dataclasses (DATACLASSES), by name

Decoding builds only those types - never pickle - so a worker that a hostile PDF took over can
send wrong answers, but no code, to the process that reads them. A decoded value is a `Wire`; the
narrowings below (`as_int`, `as_box`, ...) read it as what the reader expects or raise WireError,
and the api dataclasses are rebuilt field by field, each field read as its declared type."""

from __future__ import annotations

import dataclasses
import json
import struct
from pathlib import PurePath
from typing import Callable, Protocol, Union

import numpy as np
import numpy.typing as npt

from ..arrays import Pixels
from ..json_types import Json
from .api import Box, Char, EmbeddedImage, Mark, MarkParams, Matrix, PageObject, Point

DATACLASSES = {cls.__name__: cls for cls in (Char, PageObject, EmbeddedImage)}
MAX_FRAME = 1 << 31          # bytes in one JSON part or blob
_HEAD = struct.Struct("<II")
_BLOB = struct.Struct("<Q")

Record = Union[Char, PageObject, EmbeddedImage]
# What a frame decodes into, and what a decoded dict's keys can be (a hashable value).
WireKey = Union[None, bool, int, float, str, bytes, tuple["Wire", ...]]
Wire = Union[None, bool, int, float, str, bytes, npt.NDArray[np.generic], Char, PageObject, EmbeddedImage,
             list["Wire"], tuple["Wire", ...], dict[WireKey, "Wire"]]


class WireError(Exception):
    """A frame that breaks the format, or a value that is not what its reader expects."""


class Reader(Protocol):
    def read(self, size: int, /) -> bytes: ...


class Writer(Protocol):
    def write(self, data: bytes, /) -> object: ...

    def flush(self) -> object: ...


# ---------------------------------------------------------------------- encoding


def encode(value: object, blobs: list[bytes]) -> Json:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.generic):
        return _scalar(value.item())
    if isinstance(value, list):
        return [encode(v, blobs) for v in value]
    if isinstance(value, tuple):
        return {"T": [encode(v, blobs) for v in value]}
    if isinstance(value, dict):
        return {"D": [[encode(k, blobs), encode(v, blobs)] for k, v in value.items()]}
    if isinstance(value, (bytes, bytearray, memoryview)):
        blobs.append(bytes(value))
        return {"B": len(blobs) - 1}
    if isinstance(value, np.ndarray):
        if value.dtype.kind not in "biuf":
            raise WireError(f"arrays of {value.dtype} cannot cross the wire")
        blobs.append(np.ascontiguousarray(value).tobytes())
        return {"A": [value.dtype.str, [int(n) for n in value.shape], len(blobs) - 1]}
    if isinstance(value, (Char, PageObject, EmbeddedImage)) and type(value).__name__ in DATACLASSES:
        return {"C": [type(value).__name__, {f.name: encode(getattr(value, f.name), blobs)
                                             for f in dataclasses.fields(value)}]}
    if isinstance(value, PurePath):
        return str(value)
    raise WireError(f"{type(value).__name__} cannot cross the wire")


def _scalar(value: object) -> Json:
    """A numpy scalar's Python value (`np.generic.item`): numbers and booleans cross, the rest not."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise WireError(f"{type(value).__name__} cannot cross the wire")


# ---------------------------------------------------------------------- decoding


def decode(value: Json, blobs: list[bytes]) -> Wire:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [decode(v, blobs) for v in value]
    if len(value) != 1:
        raise WireError(f"not a wire value: {str(value)[:80]}")
    (tag, body), = value.items()
    try:
        if tag == "T":
            return tuple(decode(v, blobs) for v in _json_list(body, tag))
        if tag == "D":
            pairs = [_json_list(pair, tag) for pair in _json_list(body, tag)]
            return {_key(decode(k, blobs)): decode(v, blobs) for k, v in pairs}
        if tag == "B":
            return blobs[_json_int(body, tag)]
        if tag == "A":
            dtype, shape, n = _json_list(body, tag)
            dt = np.dtype(_json_str(dtype, tag))
            if dt.kind not in "biuf" or dt.hasobject:
                raise WireError(f"arrays of {dt} are refused")
            dims = [_json_int(s, tag) for s in _json_list(shape, tag)]
            return np.frombuffer(blobs[_json_int(n, tag)], dtype=dt).reshape(dims).copy()
        if tag == "C":
            name, fields = _json_list(body, tag)
            if not isinstance(fields, dict):
                raise WireError(f"the fields of a C value are {type(fields).__name__}")
            return _record(_json_str(name, tag), fields, blobs)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise WireError(f"bad {tag} value: {e}") from e
    raise WireError(f"unknown tag {tag!r}")


def _json_list(value: Json, tag: str) -> list[Json]:
    if not isinstance(value, list):
        raise WireError(f"bad {tag} value: {type(value).__name__} where a list goes")
    return value


def _json_int(value: Json, tag: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WireError(f"bad {tag} value: {type(value).__name__} where an integer goes")
    return value


def _json_str(value: Json, tag: str) -> str:
    if not isinstance(value, str):
        raise WireError(f"bad {tag} value: {type(value).__name__} where a string goes")
    return value


def _key(k: Wire) -> WireKey:
    if k is None or isinstance(k, (bool, int, float, str, bytes, tuple)):
        return k
    raise WireError("unhashable dict key")


def _record(name: str, fields: dict[str, Json], blobs: list[bytes]) -> Record:
    """One of the api dataclasses, every field read as its declared type (fields it does not
    have are left out)."""

    def field(key: str) -> Wire:
        if key not in fields:
            raise WireError(f"a {name} without {key}")
        return decode(fields[key], blobs)

    if name == "Char":
        return _char(field)
    if name == "PageObject":
        return _page_object(field)
    if name == "EmbeddedImage":
        return _embedded_image(field)
    raise WireError(f"no dataclass {name!r} crosses the wire")


def _char(field: Callable[[str], Wire]) -> Char:
    return Char(c=as_str(field("c"), "Char.c"), font=as_str(field("font"), "Char.font"),
                size=as_float(field("size"), "Char.size"), color=as_int(field("color"), "Char.color"),
                alpha=as_int(field("alpha"), "Char.alpha"), origin=as_point(field("origin"), "Char.origin"),
                box=as_box(field("box"), "Char.box"), dir=as_point(field("dir"), "Char.dir"),
                obj=as_int(field("obj"), "Char.obj"), font_id=as_int(field("font_id"), "Char.font_id"),
                advance=as_float(field("advance"), "Char.advance"),
                synthetic=as_bool(field("synthetic"), "Char.synthetic"),
                ascent=as_float(field("ascent"), "Char.ascent"), descent=as_float(field("descent"), "Char.descent"),
                exact_advance=as_bool(field("exact_advance"), "Char.exact_advance"))


def _page_object(field: Callable[[str], Wire]) -> PageObject:
    parent = field("parent")
    clip = field("clip")
    return PageObject(id=as_int(field("id"), "PageObject.id"), type=as_int(field("type"), "PageObject.type"),
                      matrix=as_matrix(field("matrix"), "PageObject.matrix"),
                      parent=None if parent is None else as_int(parent, "PageObject.parent"),
                      children=[as_int(c, "PageObject.children") for c in as_list(field("children"),
                                                                                    "PageObject.children")],
                      clip=None if clip is None else as_box(clip, "PageObject.clip"),
                      marks=tuple(_mark(m) for m in as_tuple(field("marks"), "PageObject.marks")))


def _mark(value: Wire) -> Mark:
    tag, params = _pair(value, "a mark")
    out: MarkParams = {}
    for k, v in as_dict(params, "a mark's parameters").items():
        out[as_str(k, "a mark parameter's key")] = v if isinstance(v, str) else as_int(v, "a mark parameter")
    return as_str(tag, "a mark's tag"), out


def _embedded_image(field: Callable[[str], Wire]) -> EmbeddedImage:
    w, h = _pair(field("px"), "EmbeddedImage.px")
    pixels, rendered = field("pixels"), field("rendered")
    return EmbeddedImage(
        px=(as_int(w, "EmbeddedImage.px"), as_int(h, "EmbeddedImage.px")),
        box=as_box(field("box"), "EmbeddedImage.box"), matrix=as_matrix(field("matrix"), "EmbeddedImage.matrix"),
        filters=[as_str(f, "EmbeddedImage.filters") for f in as_list(field("filters"), "EmbeddedImage.filters")],
        colorspace=as_str(field("colorspace"), "EmbeddedImage.colorspace"),
        bpp=as_int(field("bpp"), "EmbeddedImage.bpp"), dpi=as_point(field("dpi"), "EmbeddedImage.dpi"),
        raw=as_bytes(field("raw"), "EmbeddedImage.raw"),
        decoded_size=as_int(field("decoded_size"), "EmbeddedImage.decoded_size"),
        clipped=as_bool(field("clipped"), "EmbeddedImage.clipped"),
        upright=as_bool(field("upright"), "EmbeddedImage.upright"),
        blended=as_bool(field("blended"), "EmbeddedImage.blended"),
        transparent=as_bool(field("transparent"), "EmbeddedImage.transparent"),
        pixels=None if pixels is None else as_pixels(pixels, "EmbeddedImage.pixels"),
        rendered=None if rendered is None else as_pixels(rendered, "EmbeddedImage.rendered"))


# ---------------------------------------------------------------------- reading a decoded value
# Each returns the value it was given (numbers are not converted: an int stays an int where a
# float goes, as it was sent) or raises WireError naming `what`.


def _refused(value: Wire, what: str, expected: str) -> WireError:
    return WireError(f"{what}: {type(value).__name__} where {expected} goes")


def as_str(value: Wire, what: str) -> str:
    if not isinstance(value, str):
        raise _refused(value, what, "a string")
    return value


def as_int(value: Wire, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _refused(value, what, "an integer")
    return value


def as_float(value: Wire, what: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _refused(value, what, "a number")
    return value


def as_bool(value: Wire, what: str) -> bool:
    if not isinstance(value, bool):
        raise _refused(value, what, "a boolean")
    return value


def as_bytes(value: Wire, what: str) -> bytes:
    if not isinstance(value, bytes):
        raise _refused(value, what, "bytes")
    return value


def as_list(value: Wire, what: str) -> list[Wire]:
    if not isinstance(value, list):
        raise _refused(value, what, "a list")
    return value


def as_tuple(value: Wire, what: str) -> tuple[Wire, ...]:
    if not isinstance(value, tuple):
        raise _refused(value, what, "a tuple")
    return value


def as_dict(value: Wire, what: str) -> dict[WireKey, Wire]:
    if not isinstance(value, dict):
        raise _refused(value, what, "a dict")
    return value


def item(value: dict[WireKey, Wire], key: str, what: str) -> Wire:
    """`value[key]`, or WireError naming `what` when it has none."""
    if key not in value:
        raise WireError(f"{what} without {key!r}")
    return value[key]


def _numbers(value: Wire, what: str, count: int) -> list[float]:
    if not isinstance(value, (tuple, list)) or len(value) != count:
        raise _refused(value, what, f"{count} numbers")
    return [as_float(v, what) for v in value]


def _pair(value: Wire, what: str) -> tuple[Wire, Wire]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise _refused(value, what, "a pair")
    return value[0], value[1]


def as_point(value: Wire, what: str) -> Point:
    x, y = _numbers(value, what, 2)
    return x, y


def as_box(value: Wire, what: str) -> Box:
    x0, y0, x1, y1 = _numbers(value, what, 4)
    return x0, y0, x1, y1


def as_matrix(value: Wire, what: str) -> Matrix:
    a, b, c, d, e, f = _numbers(value, what, 6)
    return a, b, c, d, e, f


def as_pixels(value: Wire, what: str) -> Pixels:
    """An image's bytes: a uint8 array, given back as it came."""
    if not isinstance(value, np.ndarray) or value.dtype != np.uint8:
        raise _refused(value, what, "a uint8 array")
    return value.astype(np.uint8, copy=False)


# ---------------------------------------------------------------------- frames


def write_frame(stream: Writer, value: object) -> None:
    blobs: list[bytes] = []
    text = json.dumps(encode(value, blobs), separators=(",", ":")).encode()
    parts = [_HEAD.pack(len(text), len(blobs)), text]
    for blob in blobs:
        parts += [_BLOB.pack(len(blob)), blob]
    stream.write(b"".join(parts))
    stream.flush()


def _exactly(stream: Reader, n: int) -> bytes:
    if n > MAX_FRAME:
        raise WireError(f"frame part of {n} bytes")
    chunks: list[bytes] = []
    left = n
    while left:
        chunk = stream.read(left)
        if not chunk:
            raise EOFError("the stream ended inside a frame")
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: Reader) -> Wire:
    """The next value, or EOFError when the stream ends between frames."""
    head = stream.read(_HEAD.size)
    if not head:
        raise EOFError("the stream ended")
    if len(head) < _HEAD.size:
        head += _exactly(stream, _HEAD.size - len(head))
    size, count = _HEAD.unpack(head)
    if count > 1 << 20:
        raise WireError(f"{count} blobs in one frame")
    text = _exactly(stream, size)
    blobs = [_exactly(stream, _BLOB.unpack(_exactly(stream, _BLOB.size))[0]) for _ in range(count)]
    try:
        tree: Json = json.loads(text)
    except ValueError as e:
        raise WireError(f"bad JSON: {e}") from e
    return decode(tree, blobs)
