"""The numpy arrays the package passes around, named by what they hold.

A bare `np.ndarray` says nothing to the reader and, on a numpy whose stubs give ndarray's
type parameters no defaults (2.2, the newest with a Python 3.10 wheel), nothing to the checker
either. These aliases say the element type; the name says the shape and the meaning. Aliases of
one dtype are the same type to the checker: the name is for the reader."""

from typing import TypeAlias

import numpy as np
import numpy.typing as npt

Pixels: TypeAlias = npt.NDArray[np.uint8]
"""An image's bytes, h x w x channels: RGB (3) or RGBA (4) as a render or a loaded file gives it,
or a decoded image's rows in PDFium's order (BGR, BGRA)."""

RGB: TypeAlias = npt.NDArray[np.uint8]
"""An RGB image, h x w x 3 bytes (a page render on white, a thumbnail, a background PNG)."""

RGBA: TypeAlias = npt.NDArray[np.uint8]
"""An RGBA image, h x w x 4 bytes (a render on a transparent ground, a picture with its alpha)."""

BGRA: TypeAlias = npt.NDArray[np.uint8]
"""A bitmap as PDFium lays it out, h x w x 4 bytes: blue, green, red and alpha ("bgra"), the
fourth byte unused ("bgrx"), or a k8bppMask value repeated in bytes 0-2 ("mask")."""

SignedRGB: TypeAlias = npt.NDArray[np.int16]
"""An RGB image widened to int16, so a difference of two goes negative instead of wrapping (a
thumbnail as deck_fills and deck_freeforms read it, a colour to compare with one)."""

Gray: TypeAlias = npt.NDArray[np.uint8]
"""One byte per pixel, h x w: a grey image, an alpha channel, or a coverage mask 0-255."""

Bytes: TypeAlias = npt.NDArray[np.uint8]
"""Raw bytes in rows, rows x pitch: a stream's samples before they are decoded into pixels."""

Mask: TypeAlias = npt.NDArray[np.bool_]
"""True where a pixel belongs (ink, a region, what a shape covers), h x w."""

Floats: TypeAlias = npt.NDArray[np.float64]
"""Float64 measures: a distance field, a profile, colours in Lab or 0-1, a matrix, a point list."""

Floats32: TypeAlias = npt.NDArray[np.float32]
"""Float32 values, where the pure renderer computes as PDFium does in single precision."""

Ints: TypeAlias = npt.NDArray[np.int64]
"""Int64 values: counts, sums of a mask along an axis, pixel indices."""

Int16: TypeAlias = npt.NDArray[np.int16]
"""Int16 values that are no image: pixel distances capped at a small reach."""

Int32: TypeAlias = npt.NDArray[np.int32]
"""Int32 values: pixel arithmetic widened from bytes, component labels."""

UInt32: TypeAlias = npt.NDArray[np.uint32]
"""Uint32 words: a pixel packed 0xAARRGGBB, or sums that wrap as C's unsigned arithmetic does."""
