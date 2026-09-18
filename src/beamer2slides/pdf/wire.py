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
send wrong answers, but no code, to the process that reads them."""

from __future__ import annotations

import dataclasses
import json
import struct
from pathlib import PurePath

import numpy as np

from .api import Char, EmbeddedImage, PageObject

DATACLASSES = {cls.__name__: cls for cls in (Char, PageObject, EmbeddedImage)}
MAX_FRAME = 1 << 31          # bytes in one JSON part or blob
_HEAD = struct.Struct("<II")
_BLOB = struct.Struct("<Q")


class WireError(Exception):
    """A frame that breaks the format."""


def encode(value, blobs: list) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.generic):
        return value.item()
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
        return {"A": [value.dtype.str, list(value.shape), len(blobs) - 1]}
    if dataclasses.is_dataclass(value) and type(value).__name__ in DATACLASSES:
        return {"C": [type(value).__name__, {f.name: encode(getattr(value, f.name), blobs)
                                             for f in dataclasses.fields(value)}]}
    if isinstance(value, PurePath):
        return str(value)
    raise WireError(f"{type(value).__name__} cannot cross the wire")


def decode(value, blobs: list[bytes]):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [decode(v, blobs) for v in value]
    if not isinstance(value, dict) or len(value) != 1:
        raise WireError(f"not a wire value: {str(value)[:80]}")
    (tag, body), = value.items()
    try:
        if tag == "T":
            return tuple(decode(v, blobs) for v in body)
        if tag == "D":
            return {_key(decode(k, blobs)): decode(v, blobs) for k, v in body}
        if tag == "B":
            return blobs[body]
        if tag == "A":
            dtype, shape, n = body
            dt = np.dtype(dtype)
            if dt.kind not in "biuf" or dt.hasobject:
                raise WireError(f"arrays of {dt} are refused")
            return np.frombuffer(blobs[n], dtype=dt).reshape([int(s) for s in shape]).copy()
        if tag == "C":
            name, fields = body
            cls = DATACLASSES[name]
            known = {f.name for f in dataclasses.fields(cls)}
            return cls(**{k: decode(v, blobs) for k, v in fields.items() if k in known})
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise WireError(f"bad {tag} value: {e}") from e
    raise WireError(f"unknown tag {tag!r}")


def _key(k):
    if isinstance(k, (list, dict)):
        raise WireError("unhashable dict key")
    return k


def write_frame(stream, value) -> None:
    blobs: list[bytes] = []
    text = json.dumps(encode(value, blobs), separators=(",", ":")).encode()
    parts = [_HEAD.pack(len(text), len(blobs)), text]
    for blob in blobs:
        parts += [_BLOB.pack(len(blob)), blob]
    stream.write(b"".join(parts))
    stream.flush()


def _exactly(stream, n: int) -> bytes:
    if n > MAX_FRAME:
        raise WireError(f"frame part of {n} bytes")
    chunks, left = [], n
    while left:
        chunk = stream.read(left)
        if not chunk:
            raise EOFError("the stream ended inside a frame")
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_frame(stream):
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
        tree = json.loads(text)
    except ValueError as e:
        raise WireError(f"bad JSON: {e}") from e
    return decode(tree, blobs)
