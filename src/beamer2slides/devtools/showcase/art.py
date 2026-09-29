"""Pictures for the showcase decks, drawn here so every pixel of the gallery is ours to publish.

Deterministic: the same seed gives the same PNG, so rebuilding a deck does not change its pictures.
Every look (size, colours, strokes) is its caller's to say: `decks.pictures` names each picture's.
"""

import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

Size = tuple[int, int]                  # px: width, height
Rgb = tuple[int, int, int]


def hex_rgb(h: str) -> Rgb:
    """"#rrggbb" as its three channels."""
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def truchet(path: Path, seed: int, *, size: Size, cell: int, fg: str, bg: str, width: int) -> Path:
    """Quarter-circle Truchet tiles."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, hex_rgb(bg))
    d = ImageDraw.Draw(im)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if rng.random() < 0.5:
                d.arc((x - cell // 2, y - cell // 2, x + cell // 2, y + cell // 2), 0, 90, fill=hex_rgb(fg), width=width)
                d.arc((x + cell // 2, y + cell // 2, x + 3 * cell // 2, y + 3 * cell // 2), 180, 270, fill=hex_rgb(fg),
                      width=width)
            else:
                d.arc((x + cell // 2, y - cell // 2, x + 3 * cell // 2, y + cell // 2), 90, 180, fill=hex_rgb(fg),
                      width=width)
                d.arc((x - cell // 2, y + cell // 2, x + cell // 2, y + 3 * cell // 2), 270, 360, fill=hex_rgb(fg),
                      width=width)
    im.save(path)
    return path


def flow(path: Path, seed: int, *, size: Size, bg: str, colours: Sequence[str]) -> Path:
    """Lines following a smooth angle field."""
    rng = np.random.default_rng(seed)
    w, h = size
    im = Image.new("RGB", size, hex_rgb(bg))
    d = ImageDraw.Draw(im)
    a, b, c = (float(v) for v in rng.uniform(1.5, 3.5, 3))
    for k in range(700):
        x, y = float(rng.uniform(0, w)), float(rng.uniform(0, h))
        pts = [(x, y)]
        for _ in range(60):
            ang = math.sin(x / w * a * math.pi) * 2.2 + math.cos(y / h * b * math.pi) * 1.6 + c
            x, y = x + 4 * math.cos(ang), y + 4 * math.sin(ang)
            pts.append((x, y))
        d.line(pts, fill=hex_rgb(colours[k % len(colours)]), width=2)
    im.save(path)
    return path


def rings(path: Path, seed: int, *, size: Size, bg: str, colours: Sequence[str]) -> Path:
    """Concentric rings from a few centres."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, hex_rgb(bg))
    d = ImageDraw.Draw(im)
    for _ in range(7):
        cx, cy = float(rng.uniform(0, size[0])), float(rng.uniform(0, size[1]))
        r = float(rng.uniform(80, 260))
        k = int(rng.integers(0, len(colours)))
        while r > 6:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=hex_rgb(colours[k % len(colours)]), width=7)
            r -= 16
            k += 1
    im.save(path)
    return path


def landscape(path: Path, seed: int, *, size: Size, sky: tuple[str, str], layers: Sequence[str]) -> Path:
    """Layered hills under a gradient sky, with a low sun."""
    rng = np.random.default_rng(seed)
    w, h = size
    t = np.linspace(0, 1, h)[:, None, None]
    top, bottom = np.array(hex_rgb(sky[0])), np.array(hex_rgb(sky[1]))
    arr = np.broadcast_to(top * (1 - t) + bottom * t, (h, w, 3)).astype(np.uint8).copy()
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)
    sx, sy, sr = w * float(rng.uniform(0.55, 0.8)), h * 0.52, h * 0.09
    d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=hex_rgb("#f2cc8f"))
    xs = np.arange(0, w + 8, 8)
    for i, col in enumerate(layers):
        base = h * (0.55 + 0.12 * i)
        ph = rng.uniform(0, 6.28, 3)
        # the three waves summed from 0 in this order, then raised to `base`: as sum() added them
        wave = np.zeros(xs.shape)
        for amp, f, p in zip((0.05, 0.025, 0.012), (2.3, 5.1, 11.7), ph):
            wave = wave + h * amp * np.sin(xs / w * f * math.pi + p)
        ys = base + wave
        outline = [(float(x), float(y)) for x, y in zip(xs, ys)]
        d.polygon([(0.0, float(h))] + outline + [(float(w), float(h))], fill=hex_rgb(col))
    im.filter(ImageFilter.GaussianBlur(0.6)).save(path)
    return path


def marble(path: Path, seed: int, *, size: Size, a: str, b: str, c: str) -> Path:
    """Banded 'marble' from summed sines."""
    rng = np.random.default_rng(seed)
    w, h = size
    y, x = np.mgrid[0:h, 0:w] / max(w, h)
    field = x * 6 + y * 3
    for _ in range(5):
        fx, fy, p = rng.uniform(2, 9), rng.uniform(2, 9), rng.uniform(0, 6.28)
        field = field + 0.6 * np.sin(x * fx * math.pi + y * fy * math.pi + p)
    t = (np.sin(field * math.pi) + 1) / 2
    ca, cb, cc = (np.array(hex_rgb(v), float) for v in (a, b, c))
    arr = np.where(t[..., None] < 0.5, ca + (cb - ca) * (t[..., None] * 2), cb + (cc - cb) * ((t[..., None] - 0.5) * 2))
    Image.fromarray(arr.astype(np.uint8)).save(path)
    return path


def ring_chart(path: Path, share: float, *, colour: str, track: str, size: int, width: int) -> Path:
    """A donut showing `share` of a full turn, on a transparent ground (`size` px square)."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    box = (width // 2, width // 2, size - width // 2, size - width // 2)
    d.arc(box, 0, 360, fill=hex_rgb(track) + (255,), width=width)
    d.arc(box, -90, -90 + 360 * share, fill=hex_rgb(colour) + (255,), width=width)
    im.save(path)
    return path


def pie(path: Path, parts: Sequence[tuple[str, float, str]], *, size: Size, bg: str, ink: str) -> Path:
    """A labelled pie chart, as a chart exported from a spreadsheet would be."""
    im = Image.new("RGB", size, hex_rgb(bg))
    d = ImageDraw.Draw(im)
    cx, cy, r = size[0] * 0.33, size[1] * 0.5, size[1] * 0.4
    total = sum(v for _, v, _ in parts)
    start = -90.0
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    for i, (label, v, col) in enumerate(parts):
        sweep = 360 * v / total
        d.pieslice((cx - r, cy - r, cx + r, cy + r), start, start + sweep, fill=hex_rgb(col), outline=hex_rgb(bg),
                   width=4)
        start += sweep
        ly = size[1] * 0.2 + i * 64
        d.rectangle((size[0] * 0.66, ly, size[0] * 0.66 + 30, ly + 30), fill=hex_rgb(col))
        d.text((size[0] * 0.66 + 44, ly + 2), f"{label}  {v:.0f}%", fill=hex_rgb(ink), font=font)
    im.save(path)
    return path


def portrait(path: Path, seed: int, *, size: Size, bg: str, colours: Sequence[str]) -> Path:
    """An abstract 'portrait': overlapping soft discs, for a team slide."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, hex_rgb(bg))
    d = ImageDraw.Draw(im)
    for _ in range(9):
        r = float(rng.uniform(60, 200))
        cx, cy = float(rng.uniform(0, size[0])), float(rng.uniform(0, size[1]))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=hex_rgb(colours[int(rng.integers(0, len(colours)))]))
    im.filter(ImageFilter.GaussianBlur(3)).save(path)
    return path
