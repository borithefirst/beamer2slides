"""Pictures for the showcase decks, drawn here so every pixel of the gallery is ours to publish.

Deterministic: the same seed gives the same PNG, so rebuilding a deck does not change its pictures.
"""

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HEX = lambda h: tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))  # noqa: E731


def truchet(path: Path, seed: int, size=(900, 600), cell=60, fg="#f2c14e", bg="#1d2d44", width=9) -> Path:
    """Quarter-circle Truchet tiles."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, HEX(bg))
    d = ImageDraw.Draw(im)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if rng.random() < 0.5:
                d.arc((x - cell // 2, y - cell // 2, x + cell // 2, y + cell // 2), 0, 90, fill=HEX(fg), width=width)
                d.arc((x + cell // 2, y + cell // 2, x + 3 * cell // 2, y + 3 * cell // 2), 180, 270, fill=HEX(fg), width=width)
            else:
                d.arc((x + cell // 2, y - cell // 2, x + 3 * cell // 2, y + cell // 2), 90, 180, fill=HEX(fg), width=width)
                d.arc((x - cell // 2, y + cell // 2, x + cell // 2, y + 3 * cell // 2), 270, 360, fill=HEX(fg), width=width)
    im.save(path)
    return path


def flow(path: Path, seed: int, size=(900, 600), bg="#0f1a20", colours=("#e76f51", "#f4a261", "#e9c46a", "#2a9d8f")) -> Path:
    """Lines following a smooth angle field."""
    rng = np.random.default_rng(seed)
    w, h = size
    im = Image.new("RGB", size, HEX(bg))
    d = ImageDraw.Draw(im)
    a, b, c = rng.uniform(1.5, 3.5, 3)
    for k in range(700):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        pts = [(x, y)]
        for _ in range(60):
            ang = math.sin(x / w * a * math.pi) * 2.2 + math.cos(y / h * b * math.pi) * 1.6 + c
            x, y = x + 4 * math.cos(ang), y + 4 * math.sin(ang)
            pts.append((x, y))
        d.line(pts, fill=HEX(colours[k % len(colours)]), width=2)
    im.save(path)
    return path


def rings(path: Path, seed: int, size=(900, 600), bg="#f6efe6", colours=("#264653", "#2a9d8f", "#e9c46a", "#e76f51")) -> Path:
    """Concentric rings from a few centres."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, HEX(bg))
    d = ImageDraw.Draw(im)
    for _ in range(7):
        cx, cy = rng.uniform(0, size[0]), rng.uniform(0, size[1])
        r = rng.uniform(80, 260)
        k = int(rng.integers(0, len(colours)))
        while r > 6:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=HEX(colours[k % len(colours)]), width=7)
            r -= 16
            k += 1
    im.save(path)
    return path


def landscape(path: Path, seed: int, size=(1600, 900), sky=("#1b263b", "#e07a5f"), layers=("#3d405b", "#2b2d42", "#1d1e2c", "#11121a")) -> Path:
    """Layered hills under a gradient sky, with a low sun."""
    rng = np.random.default_rng(seed)
    w, h = size
    t = np.linspace(0, 1, h)[:, None, None]
    top, bottom = np.array(HEX(sky[0])), np.array(HEX(sky[1]))
    arr = np.broadcast_to(top * (1 - t) + bottom * t, (h, w, 3)).astype(np.uint8).copy()
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)
    sx, sy, sr = w * rng.uniform(0.55, 0.8), h * 0.52, h * 0.09
    d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=HEX("#f2cc8f"))
    xs = np.arange(0, w + 8, 8)
    for i, col in enumerate(layers):
        base = h * (0.55 + 0.12 * i)
        ph = rng.uniform(0, 6.28, 3)
        ys = base + sum(h * amp * np.sin(xs / w * f * math.pi + p)
                        for amp, f, p in zip((0.05, 0.025, 0.012), (2.3, 5.1, 11.7), ph))
        d.polygon([(0, h)] + list(zip(xs.tolist(), ys.tolist())) + [(w, h)], fill=HEX(col))
    im.filter(ImageFilter.GaussianBlur(0.6)).save(path)
    return path


def marble(path: Path, seed: int, size=(900, 600), a="#a8dadc", b="#457b9d", c="#f1faee") -> Path:
    """Banded 'marble' from summed sines."""
    rng = np.random.default_rng(seed)
    w, h = size
    y, x = np.mgrid[0:h, 0:w] / max(w, h)
    field = x * 6 + y * 3
    for _ in range(5):
        fx, fy, p = rng.uniform(2, 9), rng.uniform(2, 9), rng.uniform(0, 6.28)
        field = field + 0.6 * np.sin(x * fx * math.pi + y * fy * math.pi + p)
    t = (np.sin(field * math.pi) + 1) / 2
    ca, cb, cc = (np.array(HEX(v), float) for v in (a, b, c))
    arr = np.where(t[..., None] < 0.5, ca + (cb - ca) * (t[..., None] * 2), cb + (cc - cb) * ((t[..., None] - 0.5) * 2))
    Image.fromarray(arr.astype(np.uint8)).save(path)
    return path


def ring_chart(path: Path, share: float, colour="#e76f51", track="#3a3f4b", size=400, width=40) -> Path:
    """A donut showing `share` of a full turn, on a transparent ground."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    box = (width // 2, width // 2, size - width // 2, size - width // 2)
    d.arc(box, 0, 360, fill=HEX(track) + (255,), width=width)
    d.arc(box, -90, -90 + 360 * share, fill=HEX(colour) + (255,), width=width)
    im.save(path)
    return path


def pie(path: Path, parts: list[tuple[str, float, str]], size=(900, 600), bg="#ffffff", ink="#2f3640") -> Path:
    """A labelled pie chart, as a chart exported from a spreadsheet would be."""
    from PIL import ImageFont
    im = Image.new("RGB", size, HEX(bg))
    d = ImageDraw.Draw(im)
    cx, cy, r = size[0] * 0.33, size[1] * 0.5, size[1] * 0.4
    total = sum(v for _, v, _ in parts)
    start = -90.0
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    for i, (label, v, col) in enumerate(parts):
        sweep = 360 * v / total
        d.pieslice((cx - r, cy - r, cx + r, cy + r), start, start + sweep, fill=HEX(col), outline=HEX(bg), width=4)
        start += sweep
        ly = size[1] * 0.2 + i * 64
        d.rectangle((size[0] * 0.66, ly, size[0] * 0.66 + 30, ly + 30), fill=HEX(col))
        d.text((size[0] * 0.66 + 44, ly + 2), f"{label}  {v:.0f}%", fill=HEX(ink), font=font)
    im.save(path)
    return path


def portrait(path: Path, seed: int, size=(600, 600), bg="#e9edc9", colours=("#ccd5ae", "#d4a373", "#faedcd", "#a3b18a")) -> Path:
    """An abstract 'portrait': overlapping soft discs, for a team slide."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, HEX(bg))
    d = ImageDraw.Draw(im)
    for _ in range(9):
        r = rng.uniform(60, 200)
        cx, cy = rng.uniform(0, size[0]), rng.uniform(0, size[1])
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=HEX(colours[int(rng.integers(0, len(colours)))]))
    im.filter(ImageFilter.GaussianBlur(3)).save(path)
    return path
