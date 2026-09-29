"""Many ways to say how a compiled page differs from Google's picture of its slide, one per kind of defect.

`page_score` gives the adopt bench its three numbers: ink overlap within a pixel (`boxes`, `page`) and
the mean RGB difference (`pixels`). Overlap has a cliff - ink two pixels off counts as ink that is
gone, so a paragraph wrapped one line differently loses every line after it - and it cannot see
colour. This measures each slide many ways, each meant for a kind of defect a person would name, so a
run can be searched by kind: the slides a metric likes least are where to look.

  numpy only
    overlap, boxes, pixels   as `page_score`
    graded      1 - mean distance from each side's ink to the other's, capped at REACH, / REACH
    drift       mean distance (thumbnail px) of the ink that has a counterpart within REACH
    missing     share of the deck's ink with none of ours within REACH: words, pictures gone
    extra       share of our ink with none of the deck's within REACH
    local_*     graded, drift, missing, extra of local ink: a pixel off its own surroundings rather
                than off the page's one colour, so words on a panel, a photo or a gradient count as
                words and the panel as its edge only (`local_ink`)
    ink_de      mean colour difference (CIE76) where both sides have ink: text and fill colours
    local_ink_de  the same where both sides have local ink: text colour, panels left out
    ground_de   mean colour difference where neither side has ink: the page's own colour
    tile_*      the worst TILE x TILE tile of overlap, graded, local graded, local ink_de (local ink),
                pixels: one wrong word
  torch (a GPU; `out/metrics-venv`, see docs/adopt-bench.md "Metrics")
    ot_shift    RMS distance (px) unbalanced optimal transport moves the deck's local ink to ours
    ot_missing, ot_extra   mass it would rather destroy or create than move that far
    ssim, tile_ssim        structural similarity of luminance: blur, texture, resampled pictures
    lpips, tile_lpips      learned perceptual distance (AlexNet)
    dino, dino_worst       DINOv2 embeddings: the whole page, and its least alike patch
    clip        CLIP image embeddings: the whole page, as a caption would see it

  run TAG [DECK...]        a bench run's slides -> runs/TAG/metrics.json (compiles the run's tree when
                           the bench kept no PDF); --gpu adds the torch metrics
  calibrate TAG VERDICTS   how well each metric tells slides the blind judges called identical from
                           slides with each category of defect (ROC AUC; 0.5 = no better than chance)
  flag TAG CALIBRATION     each slide's metrics past the value only FLAG_SHARE of identical slides
                           reach (thresholds from `calibrate --json`): which ones trip says what to look for
  compare BEFORE AFTER     two runs metric by metric: means, how many slides each moved, which most
  worst TAG METRIC [-n N]  the slides a metric likes least, with their sheets
  show DECK:N TAG [--gpu]  one slide's maps: where the ink went (missing red, extra blue, moved orange),
                           and each metric's own map -> out/metrics/

Every metric is written so that larger is worse (similarities as 1 - x), and `calibrate` reads them so.
The corpus is $B2S_ADOPT_CORPUS, as the bench's.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from beamer2slides.arrays import RGB, Floats32, Int16, Mask, SignedRGB
from beamer2slides.page_score import covered_mask, ink_masks, overlap

REACH = 16          # px on the thumbnail grid (1600 wide): about a line of body text
TILE = 100          # px: the tile the tile_* metrics take the worst of
TILE_INK = 200      # a tile needs this much ink (either side) for its overlap to count
LOCAL = 15          # px: the window local ink is measured against
LOCAL_CONTRAST = 48 # a channel this far off the window's mean is ink
OT_CELL = 8         # px per cell of the transport grid
OT_SIGMA = 1.0      # cells: the entropic blur of the transport plan
OT_REACH = 6.0      # cells: beyond about this, mass is destroyed and created rather than moved
OT_ITERS = 300
FLAG_SHARE = 0.05   # a metric's flag threshold: exceeded by this share of the slides judged identical

# larger is worse for all of them; `calibrate` and `worst` rely on it
NUMPY_METRICS = ("overlap", "boxes", "pixels", "graded", "drift", "missing", "extra",
                 "local_graded", "local_drift", "local_missing", "local_extra",
                 "ink_de", "local_ink_de", "ground_de",
                 "tile_overlap", "tile_graded", "tile_local_graded", "tile_ink_de", "tile_pixels")
TORCH_METRICS = ("ot_shift", "ot_missing", "ot_extra", "ssim", "tile_ssim", "lpips", "tile_lpips",
                 "dino", "dino_worst", "clip")


# ------------------------------------------------------------------------------------------ numpy

def distance_to(mask: Mask, reach: int = REACH) -> Int16:
    """Every pixel's distance to the nearest set pixel of `mask`, up to `reach` (reach + 1 beyond):
    dilations alternating 4- and 8-neighbour, an octagon within 8% of the Euclidean distance."""
    d = np.full(mask.shape, reach + 1, dtype=np.int16)
    grown = mask.copy()
    d[grown] = 0
    for k in range(1, reach + 1):
        g = grown.copy()
        g[1:] |= grown[:-1]
        g[:-1] |= grown[1:]
        g[:, 1:] |= grown[:, :-1]
        g[:, :-1] |= grown[:, 1:]
        if k % 2 == 0:
            g[1:, 1:] |= grown[:-1, :-1]
            g[1:, :-1] |= grown[:-1, 1:]
            g[:-1, 1:] |= grown[1:, :-1]
            g[:-1, :-1] |= grown[1:, 1:]
        d[g & ~grown] = k
        grown = g
    return d


def lab(a: SignedRGB) -> Floats32:
    """sRGB (0-255, any integer or float array) -> CIE L*a*b* (D65)."""
    c = a.astype(np.float32) / 255
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]], np.float32)
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883], np.float32)
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def tiles(h: int, w: int, size: int = TILE):
    for y in range(0, h, size):
        for x in range(0, w, size):
            yield slice(y, min(h, y + size)), slice(x, min(w, x + size))


def box_mean(a: RGB | Mask, k: int) -> Floats32:
    """The mean over a k x k window (edges clamped), from an integral image."""
    r = k // 2
    p = np.pad(a.astype(np.float32), ((r + 1, r),) + ((r + 1, r),) + ((0, 0),) * (a.ndim - 2), mode="edge")
    c = p.cumsum(0).cumsum(1)
    h, w = a.shape[:2]
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)


def rank_filter(a: RGB, k: int, fn) -> RGB:
    """A k x k min or max filter (fn = np.minimum / np.maximum), separable, edges clamped: windows
    doubled 1, 2, 4 ... then two overlapping ones make k."""
    r = k // 2
    for axis in (0, 1):
        b = np.pad(np.moveaxis(a, axis, 0), [(r, r)] + [(0, 0)] * (a.ndim - 1), mode="edge")
        n = a.shape[axis]
        span = 1
        while span * 2 <= k:
            b = fn(b[:len(b) - span], b[span:])
            span *= 2
        a = np.moveaxis(fn(b[:n], b[k - span:k - span + n]), 0, axis)
    return a


def local_ink(a: SignedRGB) -> Mask:
    """Ink against its own surroundings rather than the page's one colour: what a LOCAL px opening or
    closing takes away (a top-hat, either polarity), by more than LOCAL_CONTRAST in some channel.
    Strokes thinner than LOCAL are ink on any ground - a panel, a photo, a gradient - while a panel
    and its straight edges survive both and are not."""
    px = a.astype(np.uint8)
    opened = rank_filter(rank_filter(px, LOCAL, np.minimum), LOCAL, np.maximum)
    closed = rank_filter(rank_filter(px, LOCAL, np.maximum), LOCAL, np.minimum)
    # one polarity per place: dark words on a light ground are what the closing takes away, and the
    # light gaps between their letters (which the opening takes) are ground. The ground is the one of
    # the two the neighbourhood's mean is nearer (decided on a grid 4 px apart).
    h, w = px.shape[:2]
    s = (slice(None, None, 4), slice(None, None, 4))
    mean = box_mean(px[s], (2 * LOCAL + 1) // 4 | 1)
    light = np.abs(mean - closed[s]).max(-1) <= np.abs(mean - opened[s]).max(-1)
    light_ground = light.repeat(4, 0).repeat(4, 1)[:h, :w]
    m = np.where(light_ground, (closed.astype(np.int16) - px).max(-1), (px.astype(np.int16) - opened).max(-1)) \
        > LOCAL_CONTRAST
    return m & (box_mean(m[..., None], 3)[..., 0] * 9 >= 4.5)     # itself and 4 of its 8 neighbours


def ink_pair(m_ref: Mask, m_got: Mask) -> dict:
    """Where each side's ink has the other's: capped distances both ways."""
    d_ref, d_got = distance_to(m_got), distance_to(m_ref)
    cap = np.float32(REACH)
    near_ref, near_got = m_ref & (d_ref <= REACH), m_got & (d_got <= REACH)
    dist = np.minimum(np.where(m_ref, d_ref, 0), cap).astype(np.float32) + \
        np.minimum(np.where(m_got, d_got, 0), cap).astype(np.float32)   # each ink pixel's capped distance
    n_ref, n_got = int(m_ref.sum()), int(m_got.sum())
    ink = n_ref + n_got
    return {"d_ref": d_ref, "d_got": d_got, "dist": dist / cap,
            "graded": float(dist.sum() / cap / ink) if ink else 0.0,
            "drift": float((d_ref[near_ref].sum() + d_got[near_got].sum()) / max(1, near_ref.sum() + near_got.sum())),
            "missing": float((m_ref & ~near_ref).sum() / n_ref) if n_ref else 0.0,
            "extra": float((m_got & ~near_got).sum() / n_got) if n_got else 0.0}


def worst_tile(dist: Floats32, m_ref: Mask, m_got: Mask) -> float:
    h, w = dist.shape
    out = 0.0
    for ys, xs in tiles(h, w):
        n = int(m_ref[ys, xs].sum() + m_got[ys, xs].sum())
        if n >= TILE_INK:
            out = max(out, float(dist[ys, xs].sum() / n))
    return out


def numpy_metrics(ref: SignedRGB, got: SignedRGB, slide: dict) -> tuple[dict, dict]:
    """The numpy metrics of `got` against `ref` (int16 RGB arrays of one size), and the maps they were
    read from (for heat maps)."""
    h, w = ref.shape[:2]
    m_ref, m_got = ink_masks(ref, got)
    l_ref, l_got = local_ink(ref), local_ink(got)
    covered = covered_mask(slide, w, h)
    page, local = ink_pair(m_ref, m_got), ink_pair(l_ref, l_got)
    both, l_both = m_ref & m_got, l_ref & l_got
    neither = ~m_ref & ~m_got
    de = np.sqrt(((lab(ref) - lab(got)) ** 2).sum(-1))
    diff = np.abs(ref - got).mean(-1) / 255
    out = {
        "overlap": 1 - overlap(m_ref, m_got),
        "boxes": 1 - overlap(m_ref & covered, m_got & covered),
        "pixels": float(diff.mean()),
        **{k: page[k] for k in ("graded", "drift", "missing", "extra")},
        **{"local_" + k: local[k] for k in ("graded", "drift", "missing", "extra")},
        "ink_de": float(de[both].mean()) if both.any() else 0.0,
        "local_ink_de": float(de[l_both].mean()) if l_both.any() else 0.0,
        "ground_de": float(de[neither].mean()) if neither.any() else 0.0,
        "tile_graded": worst_tile(page["dist"], m_ref, m_got),
        "tile_local_graded": worst_tile(local["dist"], l_ref, l_got),
    }
    worst = {"tile_overlap": 0.0, "tile_ink_de": 0.0, "tile_pixels": 0.0}
    for ys, xs in tiles(h, w):
        a, b = m_ref[ys, xs], m_got[ys, xs]
        worst["tile_pixels"] = max(worst["tile_pixels"], float(diff[ys, xs].mean()))
        if int(a.sum() + b.sum()) >= TILE_INK:
            worst["tile_overlap"] = max(worst["tile_overlap"], 1 - overlap(a, b))
        bt = l_both[ys, xs]
        if bt.sum() >= TILE_INK / 4:
            worst["tile_ink_de"] = max(worst["tile_ink_de"], float(de[ys, xs][bt].mean()))
    out.update(worst)
    maps = {"graded": page["dist"], "local_graded": local["dist"], "ink_de": np.where(l_both, de, 0),
            "ground_de": np.where(neither, de, 0),
            "m_ref": m_ref, "m_got": m_got, "d_ref": page["d_ref"], "d_got": page["d_got"],
            "l_ref": l_ref, "l_got": l_got, "ld_ref": local["d_ref"], "ld_got": local["d_got"]}
    return {k: round(v, 5) for k, v in out.items()}, maps


# ------------------------------------------------------------------------------------------ torch

class Gpu:
    """The torch metrics, their models loaded once. Needs torch (and, for lpips/dino/clip, the
    `lpips` and `transformers` packages): see docs/adopt-bench.md "Metrics"."""

    def __init__(self, device: str | None = None):
        import torch
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._lpips = self._dino = self._clip = None

    # -- unbalanced optimal transport of ink mass, log-domain Sinkhorn on a separable grid
    def ot(self, m_ref: Mask, m_got: Mask) -> dict:
        """Unbalanced entropic optimal transport (squared distance, KL marginals) of the deck's ink onto
        ours, on a grid of OT_CELL px cells. Mass moves when that is cheaper than destroying it and
        creating it anew, which it is up to about OT_REACH cells: `ot_shift` is how far the moved mass
        went (RMS, px), `ot_missing`/`ot_extra` the shares of each side's ink left unmatched. The
        entropy smears every plan over about a cell (a page against itself costs ~10 px), so the shift
        is debiased as a Sinkhorn divergence: the mean of the two self-transport costs taken off."""
        t = self.torch
        dev, dt = self.device, t.float64

        def pool(m):
            x = t.as_tensor(m, dtype=dt, device=dev)[None, None]
            return t.nn.functional.avg_pool2d(x, OT_CELL, ceil_mode=True)[0, 0]
        a, b = pool(m_ref), pool(m_got)
        sa, sb = float(a.sum()), float(b.sum())
        if sa == 0 or sb == 0:
            return {"ot_shift": 0.0, "ot_missing": 1.0 if sa else 0.0, "ot_extra": 1.0 if sb else 0.0}
        scale = max(sa, sb)
        a, b = a / scale, b / scale
        h, w = a.shape
        eps = 2 * OT_SIGMA ** 2
        rho = OT_REACH ** 2                    # KL weight: a move of about OT_REACH costs what dropping it does
        fi = rho / (rho + eps)
        iy, ix = t.arange(h, device=dev, dtype=dt), t.arange(w, device=dev, dtype=dt)
        cy, cx = (iy[:, None] - iy[None]) ** 2, (ix[:, None] - ix[None]) ** 2
        ly, lx = -cy / eps, -cx / eps          # log kernels, separable

        def apply(log_v, ky=ly, kx=lx):
            # log sum_j K_ij v_j with K = ky (x) kx, as two log-sum-exps
            s = t.logsumexp(log_v[:, None, :] + kx[None], dim=2)            # over x: (h, w)
            return t.logsumexp(s[None, :, :] + ky[:, :, None], dim=1)        # over y: (h, w)
        wy = t.where(cy > 0, ly + t.log(cy.clamp_min(1e-300)), t.full_like(cy, -float("inf")))
        wx = t.where(cx > 0, lx + t.log(cx.clamp_min(1e-300)), t.full_like(cx, -float("inf")))

        def plan(a, b):
            """The plan's two marginals and its cost per unit of mass moved (cells^2)."""
            la, lb = t.log(a), t.log(b)
            f = t.zeros_like(a)                 # log u
            g = t.zeros_like(b)                 # log v
            for _ in range(OT_ITERS):
                f = fi * (la - apply(g))
                g = fi * (lb - apply(f))
            f = t.where(a > 0, f, t.full_like(f, -float("inf")))
            g = t.where(b > 0, g, t.full_like(g, -float("inf")))
            moved_a, moved_b = t.exp(f + apply(g)), t.exp(g + apply(f))
            # sum_ij pi_ij C_ij, C = dy^2 + dx^2: the kernels weighted by each term apart
            cost = t.exp(f + apply(g, ky=wy)).sum() + t.exp(f + apply(g, kx=wx)).sum()
            return moved_a, moved_b, float(cost) / max(float(moved_a.sum()), 1e-12)
        with t.no_grad():
            moved_ref, moved_got, c_ab = plan(a, b)
            c_aa, c_bb = plan(a, a)[2], plan(b, b)[2]
        shift = max(c_ab - (c_aa + c_bb) / 2, 0.0) ** 0.5
        return {"ot_shift": round(shift * OT_CELL, 3),
                "ot_missing": round(float((a - moved_ref).clamp_min(0).sum()) / (sa / scale), 5),
                "ot_extra": round(float((b - moved_got).clamp_min(0).sum()) / (sb / scale), 5)}

    # -- structural similarity
    def ssim(self, ref: SignedRGB, got: SignedRGB) -> dict:
        t = self.torch
        y = lambda a: t.as_tensor(a[..., :3] @ np.array([0.299, 0.587, 0.114]), dtype=t.float32,  # noqa: E731
                                  device=self.device)[None, None] / 255
        x1, x2 = y(ref), y(got)
        k = t.exp(-(t.arange(11, device=self.device, dtype=t.float32) - 5) ** 2 / (2 * 1.5 ** 2))
        k = (k / k.sum())
        win = (k[:, None] * k[None])[None, None]
        blur = lambda z: t.nn.functional.conv2d(z, win, padding=5)                            # noqa: E731
        mu1, mu2 = blur(x1), blur(x2)
        s11, s22, s12 = blur(x1 * x1) - mu1 ** 2, blur(x2 * x2) - mu2 ** 2, blur(x1 * x2) - mu1 * mu2
        c1, c2 = 0.01 ** 2, 0.03 ** 2
        m = ((2 * mu1 * mu2 + c1) * (2 * s12 + c2)) / ((mu1 ** 2 + mu2 ** 2 + c1) * (s11 + s22 + c2))
        loss = (1 - m)[0, 0]
        tiled = t.nn.functional.avg_pool2d(loss[None, None], TILE, ceil_mode=True)
        return {"ssim": round(float(loss.mean()), 5), "tile_ssim": round(float(tiled.max()), 5)}, loss.cpu().numpy()

    # -- learned perceptual distance
    def lpips(self, ref: SignedRGB, got: SignedRGB) -> dict:
        t = self.torch
        if self._lpips is None:
            import lpips
            self._lpips = lpips.LPIPS(net="alex", spatial=True, verbose=False).to(self.device).eval()
        im = lambda a: (t.as_tensor(a[..., :3], dtype=t.float32, device=self.device).permute(2, 0, 1)[None]  # noqa: E731
                        / 127.5 - 1)
        with t.no_grad():
            d = self._lpips(im(ref), im(got))[0, 0]
        tiled = t.nn.functional.avg_pool2d(d[None, None], TILE, ceil_mode=True)
        return {"lpips": round(float(d.mean()), 5), "tile_lpips": round(float(tiled.max()), 5)}, d.cpu().numpy()

    # -- embeddings
    def dino(self, ref: SignedRGB, got: SignedRGB) -> dict:
        """DINOv2 (small) at 37 x 21 patches: 1 - cosine of the pooled embeddings, and of the least
        alike patch."""
        t = self.torch
        if self._dino is None:
            from transformers import AutoModel
            self._dino = AutoModel.from_pretrained("facebook/dinov2-small").to(self.device).eval()
        mean = t.tensor([0.485, 0.456, 0.406], device=self.device)[:, None, None]
        std = t.tensor([0.229, 0.224, 0.225], device=self.device)[:, None, None]

        def feats(a):
            x = t.as_tensor(a[..., :3], dtype=t.float32, device=self.device).permute(2, 0, 1) / 255
            x = t.nn.functional.interpolate(x[None], size=(294, 518), mode="bilinear", antialias=True, align_corners=False)
            with t.no_grad():
                o = self._dino(pixel_values=(x - mean) / std).last_hidden_state[0]
            return o[0], o[1:]
        c1, p1 = feats(ref)
        c2, p2 = feats(got)
        cos = t.nn.functional.cosine_similarity
        patch = 1 - cos(p1, p2, dim=-1)
        return {"dino": round(float(1 - cos(c1, c2, dim=0)), 5), "dino_worst": round(float(patch.max()), 5)}, \
            patch.reshape(21, 37).cpu().numpy()

    def clip(self, ref: SignedRGB, got: SignedRGB) -> dict:
        t = self.torch
        if self._clip is None:
            from transformers import CLIPModel, CLIPProcessor
            self._clip = (CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(self.device).eval(),
                          CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32"))
        model, proc = self._clip
        with t.no_grad():
            x = proc(images=[Image.fromarray(ref.astype(np.uint8)), Image.fromarray(got.astype(np.uint8))],
                     return_tensors="pt")["pixel_values"].to(self.device)
            e = model.get_image_features(pixel_values=x)
        if not isinstance(e, t.Tensor):              # transformers 5 answers with a model output
            e = e.pooler_output
        return {"clip": round(float(1 - t.nn.functional.cosine_similarity(e[0], e[1], dim=0)), 5)}

    def metrics(self, ref: SignedRGB, got: SignedRGB, m_ref: Mask, m_got: Mask) -> dict:
        out = dict(self.ot(m_ref, m_got))
        for fn in (self.ssim, self.lpips, self.dino):
            out.update(fn(ref, got)[0])
        out.update(self.clip(ref, got))
        return out


# ------------------------------------------------------------------------------------------ pages

def corpus_dir() -> Path:
    from beamer2slides.devtools.adopt_bench import CORPUS
    return CORPUS


def run_pdf(run: Path) -> Path | None:
    """The PDF of a bench run: the one it compiled, or its tree compiled now (a cached run keeps none)."""
    pdf = run / "work" / "build" / "main.pdf"
    if pdf.exists():
        return pdf
    from beamer2slides.inverse import Workspace
    pdf, _ = Workspace(run / "tree" / "main.tex", run / "metrics-work").compile()
    return pdf


def pages(deck: str, tag: str):
    """(slide number, deck thumbnail, our page on its grid, the slide's IR) for a bench run's slides."""
    from beamer2slides.fidelity import rgb_array
    from beamer2slides.pdf import Document
    folder = corpus_dir() / deck
    run = folder / "runs" / tag
    target = json.loads(((run / "target.json") if (run / "target.json").exists() else folder / "target.json")
                        .read_text(encoding="utf-8"))
    pdf = run_pdf(run)
    if pdf is None:
        return
    doc = Document(pdf)
    try:
        for i, slide in enumerate(target["slides"]):
            ref_path = folder / "slides" / f"{i + 1:03}.png"
            if i >= len(doc) or not ref_path.exists():
                continue
            ref_img = Image.open(ref_path).convert("RGB")
            w, h = ref_img.size
            got_img = Image.fromarray(doc[i].render(w / doc[i].width)).convert("RGB").resize((w, h))
            yield i + 1, rgb_array(ref_img), rgb_array(got_img), slide
    finally:
        doc.close()


def measure_deck(deck: str, tag: str, gpu: Gpu | None) -> list[dict]:
    rows = []
    for n, ref, got, slide in pages(deck, tag):
        row, maps = numpy_metrics(ref, got, slide)
        if gpu is not None:
            row.update(gpu.metrics(ref, got, maps["l_ref"], maps["l_got"]))
        rows.append({"slide": n, **row})
    return rows


def decks_of(tag: str, names: list[str]) -> list[str]:
    root = corpus_dir()
    return names or sorted(p.name for p in root.iterdir() if (p / "runs" / tag / "tree" / "main.tex").exists())


def run(tag: str, names: list[str], gpu: bool, jobs: int) -> None:
    decks = decks_of(tag, names)
    # PDFs first, in parallel (a compile is a subprocess); the measuring after, on this thread (PDFium
    # is not thread-safe, and the GPU is one)
    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(lambda d: run_pdf(corpus_dir() / d / "runs" / tag), decks))
    def save(deck, rows):
        out = corpus_dir() / deck / "runs" / tag / "metrics.json"
        out.write_text(json.dumps({"deck": deck, "tag": tag, "reach": REACH, "slides": rows}, indent=0),
                       encoding="utf-8")
        print(f"{deck:<24} {len(rows):>4} slides", flush=True)
    if gpu:
        g = Gpu()
        for deck in decks:
            save(deck, measure_deck(deck, tag, g))
        return
    # without the GPU a deck per process: each has its own PDFium (one thread took 21 minutes over
    # the 1,937 slides of round off1)
    from concurrent.futures import ProcessPoolExecutor, as_completed
    with ProcessPoolExecutor(max(1, jobs)) as pool:
        futures = {pool.submit(measure_deck, deck, tag, None): deck for deck in decks}
        for f in as_completed(futures):
            save(futures[f], f.result())


# ------------------------------------------------------------------------------------------ judged

def auc(pos: list[float], neg: list[float]) -> float | None:
    """P(a positive scores above a negative), ties half: the ROC AUC (Mann-Whitney)."""
    if not pos or not neg:
        return None
    allv = np.array(neg + pos)
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv))
    sv = allv[order]
    i = 0
    while i < len(sv):                         # average ranks over ties
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = ranks[len(neg):].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def auc_within(pos: list[tuple[str, int]], neg: list[tuple[str, int]], score) -> tuple[float | None, int]:
    """The AUC counted only over pairs from one deck, and how many pairs there were. A category's
    slides are often most of one or two decks (jruby-ja's gradients, drawing-workshop's colours), and
    across decks a metric can rank a deck's style rather than the defect."""
    by_deck: dict[str, list[float]] = {}
    for k in neg:
        by_deck.setdefault(k[0], []).append(score(k))
    wins = pairs = 0
    for k in pos:
        s = score(k)
        for v in by_deck.get(k[0], ()):
            wins += 1.0 if s > v else 0.5 if s == v else 0.0
            pairs += 1
    return (wins / pairs if pairs else None), pairs


def judged(verdicts: Path) -> dict[tuple[str, int], set[str]]:
    """(deck, slide) -> the categories the blind judges found there ({} = judged identical), from a
    judging folder: judging/<deck>/verdict-*.json, findings.json ("trusted": the findings that held up)
    and judging-key.json (the planted control sheets, left out)."""
    planted = set()
    key = verdicts / "judging-key.json"
    if key.exists():
        for deck, items in json.loads(key.read_text(encoding="utf-8")).items():
            planted |= {(deck, int(it["sheet"].split(".")[0])) for it in items}
    trusted: dict[tuple[str, int], set[str]] = {}
    for f in json.loads((verdicts / "findings.json").read_text(encoding="utf-8"))["trusted"]:
        trusted.setdefault((f["deck"], int(f["sheet"])), set()).add(f["category"])
    out = {}
    for path in (verdicts / "judging").glob("*/verdict-*.json"):
        deck = path.parent.name
        for s in json.loads(path.read_text(encoding="utf-8"))["sheets"]:
            k = (deck, int(s["sheet"]))
            if k in planted:
                continue
            if s["same"]:
                out[k] = set()
            elif k in trusted:
                out[k] = trusted[k]
    return out


def load_metrics(tag: str) -> dict[tuple[str, int], dict]:
    rows = {}
    for p in corpus_dir().glob(f"*/runs/{tag}/metrics.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        for r in d["slides"]:
            rows[(d["deck"], r["slide"])] = r
    return rows


def calibrate(tag: str, verdicts: Path) -> dict:
    labels = judged(verdicts)
    rows = load_metrics(tag)
    keys = [k for k in labels if k in rows]
    names = [m for m in (*NUMPY_METRICS, *TORCH_METRICS) if all(m in rows[k] for k in keys)]
    cats = sorted({c for k in keys for c in labels[k]}, key=lambda c: -sum(c in labels[k] for k in keys))
    neg = [k for k in keys if not labels[k]]
    thresholds = {m: float(np.quantile([rows[k][m] for k in neg], 1 - FLAG_SHARE)) for m in names}
    # how much more MISSING_METRICS should count than the rest of SEVERITY: fit once, against whether
    # severity ranks a CONTENT_LOST slide (words or a picture gone) above one with some other judged
    # defect - the ordering the bug is actually about, not merely "past a threshold at all".
    weight, weight_auc, weight_pairs = fit_missing_weight(rows, keys, labels, thresholds)
    severity_weights = {m: weight for m in MISSING_METRICS}
    content_pos, content_neg = content_lost_split(keys, labels)
    before_auc, before_pairs = content_lost_auc(rows, content_pos, content_neg, thresholds, None)
    table = {"n": len(keys), "identical": len(neg), "categories": {},
             # what only FLAG_SHARE of the slides the judges called identical exceed
             "thresholds": thresholds,
             "severity_weights": severity_weights,
             # does severity rank a slide with words or a picture gone over one with some other
             # judged defect - the ordering this weight exists for, before and after fitting it
             "severity_weight_calibration": {
                 "note": "CONTENT_LOST slides ranked over other-defect slides (never vs identical)",
                 "n_content_lost": len(content_pos), "n_other_defect": len(content_neg),
                 "before": {"weight": 1.0, "auc": before_auc, "pairs": before_pairs},
                 "after": {"weight": weight, "auc": weight_auc, "pairs": weight_pairs}}}
    for cat in ["any", *cats]:
        pos = [k for k in keys if labels[k] and (cat == "any" or cat in labels[k])]
        within = {m: auc_within(pos, neg, lambda k, m=m: rows[k][m]) for m in names}
        sev_before = lambda k: severity(rows[k], thresholds)[0]                        # noqa: E731
        sev_after = lambda k: severity(rows[k], thresholds, severity_weights)[0]       # noqa: E731
        table["categories"][cat] = {"n": len(pos), "decks": len({k[0] for k in pos}),
                                    "pairs": next(iter(within.values()))[1] if within else 0,
                                    "auc": {m: auc([rows[k][m] for k in pos], [rows[k][m] for k in neg])
                                            for m in names},
                                    "within": {m: w[0] for m, w in within.items()},
                                    "severity": {
                                        "before": {"auc": auc([sev_before(k) for k in pos],
                                                             [sev_before(k) for k in neg]),
                                                   "within": auc_within(pos, neg, sev_before)[0]},
                                        "after": {"auc": auc([sev_after(k) for k in pos],
                                                            [sev_after(k) for k in neg]),
                                                  "within": auc_within(pos, neg, sev_after)[0]}}}
    return table


def print_calibration(table: dict) -> None:
    cats = table["categories"]
    names = list(next(iter(cats.values()))["auc"])
    print(f"{table['n']} judged slides, {table['identical']} identical. ROC AUC per defect category "
          f"(1 = the metric ranks every such slide above every identical one, 0.5 = chance)")
    for key, title, count in (("auc", "all pairs", "n"),
                              ("within", "pairs from one deck only (a deck's style can't score)", "pairs")):
        print(f"\n{title}\n{'category':<16}{count:>6}  " + " ".join(f"{m[:10]:>10}" for m in names))
        for cat, row in cats.items():
            cells = []
            for m in names:
                v = row[key][m]
                cells.append(f"{v:>10.2f}" if v is not None else f"{'-':>10}")
            print(f"{cat:<16}{row[count]:>6}  " + " ".join(cells))
    def sev_cell(v: dict) -> str:
        return f"{v['auc']:.2f}/{v['within']:.2f}" if v["auc"] is not None else "-/-"

    w = table.get("severity_weights", {})
    one = next(iter(w.values()), 1.0) if w else 1.0
    calib = table.get("severity_weight_calibration", {})
    print(f"\nseverity, judged-bad vs judged-identical (the coarse check: does the sum still find a "
          f"defect at all): all pairs / within-deck, before -> after {', '.join(MISSING_METRICS)} x{one:g}")
    for cat, row in cats.items():
        s = row["severity"]
        print(f"{cat:<16}{row['n']:>6}  {sev_cell(s['before']):>12} -> {sev_cell(s['after']):<12}")
    if calib.get("before", {}).get("auc") is not None:
        b, a = calib["before"], calib["after"]
        print(f"\nseverity, CONTENT_LOST slides (words/a picture gone) vs other-defect slides (the fix's "
              f"own target - never vs identical): {calib['n_content_lost']} content-lost, "
              f"{calib['n_other_defect']} other-defect")
        print(f"  before (1x):   AUC {b['auc']:.3f} over {b['pairs']} pairs")
        print(f"  after  ({a['weight']:g}x): AUC {a['auc']:.3f} over {a['pairs']} pairs")
    else:
        print("\nseverity, CONTENT_LOST vs other-defect: not enough judged CONTENT_LOST slides to calibrate against")


def flag(tag: str, thresholds: dict, n: int) -> None:
    """Each slide's metrics past their thresholds (from `calibrate --json`), the slides with the most
    first: which metrics a slide trips says what kind of defect to look for."""
    rows = load_metrics(tag)
    hits = []
    for key, r in rows.items():
        over = {m: r[m] / t for m, t in thresholds.items() if m in r and t > 0 and r[m] > t}
        if over:
            hits.append((len(over), max(over.values()), key, over))
    hits.sort(key=lambda h: (-h[0], -h[1]))
    print(f"{len(hits)} of {len(rows)} slides trip a metric\n")
    for count, _, (deck, slide), over in hits[:n]:
        worst_first = sorted(over.items(), key=lambda kv: -kv[1])
        print(f"{deck}:{slide:<4} {count:>2}  " + " ".join(f"{m} x{v:.1f}" for m, v in worst_first[:8]))


def compare(before: str, after: str, n: int) -> None:
    """Two runs of the same decks, metric by metric: the mean of each and the slides it moved most."""
    a, b = load_metrics(before), load_metrics(after)
    keys = sorted(set(a) & set(b))
    names = [m for m in (*NUMPY_METRICS, *TORCH_METRICS) if all(m in a[k] and m in b[k] for k in keys)]
    print(f"{len(keys)} slides in both. Larger is worse; worse/better = slides that moved more than a tenth "
          f"of the metric's spread\n")
    print(f"{'metric':<18}{before:>10}{after:>10}{'worse':>7}{'better':>7}   most worse")
    for m in names:
        va, vb = np.array([a[k][m] for k in keys]), np.array([b[k][m] for k in keys])
        step = 0.1 * max(float(np.std(np.concatenate([va, vb]))), 1e-9)
        d = vb - va
        top = [keys[i] for i in np.argsort(-d)[:n] if d[i] > step]
        print(f"{m:<18}{va.mean():>10.4f}{vb.mean():>10.4f}{int((d > step).sum()):>7}{int((d < -step).sum()):>7}   "
              + " ".join(f"{deck}:{s}" for deck, s in top))


def worst(tag: str, metric: str, n: int) -> None:
    rows = load_metrics(tag)
    for (deck, slide), r in sorted(rows.items(), key=lambda kv: -kv[1].get(metric, -1))[:n]:
        sheet = corpus_dir() / deck / "runs" / tag / "sheets" / f"{slide:03}.png"
        print(f"{r.get(metric, float('nan')):>9.4f}  {deck}:{slide}  {sheet}")


#: The metrics a slide's severity adds up, each with the kind of defect it names when it dominates:
#: the ones that held up within decks on the judges' verdicts (docs/adopt-bench.md "Metrics").
SEVERITY = {"tile_pixels": "a region wrong", "local_missing": "words or pictures missing",
            "missing": "ink missing", "extra": "extra ink", "graded": "moved or broken lines",
            "local_graded": "moved on a panel", "tile_ink_de": "text colour", "ground_de": "background or fill",
            "dino": "looks different", "tile_lpips": "a region looks different"}
SEVERITY_CAP = 10.0     # one metric counts at most ten thresholds: a gradient's ground_de x60 is one defect

#: local_missing / missing name the one defect a person always ranks worst - Google's ink with none
#: of ours within REACH, words or whole pictures gone - so `calibrate` may count them for more than
#: one threshold each: ink two pixels off (graded) or a stray glyph (extra) is never as bad as ink
#: that just is not there. `fit_missing_weight` picks how much more from the judges' verdicts.
MISSING_METRICS = ("missing", "local_missing")
#: candidates for that multiplier: a grid, not a search, because AUC moves in ties on this few a
#: point, not smoothly - gradient descent would chase noise.
MISSING_WEIGHT_GRID = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0)
#: the judged categories that *are* the defect MISSING_METRICS name (docs/adopt-grind.md "Judge,
#: trace, fix"): a slide labelled one of these is words or a picture gone, not drift or a wrong
#: colour, so this is the ordering severity is actually for - not "bad" vs "identical" (that one a
#: sum of thresholds already separates decently; see `calibrate`'s "any" AUC) but *whose* bad ranks
#: worse. `fit_missing_weight` is calibrated against it, never against a number picked by fiat.
CONTENT_LOST = frozenset({"text_missing", "picture_missing"})


def severity(r: dict, thresholds: dict, weights: dict | None = None) -> tuple[float, dict]:
    """How far past what identical slides reach a slide is: each SEVERITY metric over its threshold,
    scaled by its calibrated `weights` (default 1x, i.e. unweighted), capped, summed. Returns the sum
    and {metric: weighted ratio} of those past their threshold."""
    over = {}
    for m in SEVERITY:
        t = thresholds.get(m, 0)
        if m not in r or t <= 0 or r[m] <= t:
            continue
        w = weights.get(m, 1.0) if weights else 1.0
        over[m] = min(w * r[m] / t, SEVERITY_CAP)
    return sum(over.values()), over


def content_lost_split(keys: list, labels: dict) -> tuple[list, list]:
    """CONTENT_LOST slides (words or a picture gone) and slides with some other judged defect (drift,
    colour, a wrong shape...) - never the judged-identical ones, which a threshold already separates."""
    pos = [k for k in keys if labels[k] & CONTENT_LOST]
    neg = [k for k in keys if labels[k] and not (labels[k] & CONTENT_LOST)]
    return pos, neg


def content_lost_auc(rows: dict, pos: list, neg: list, thresholds: dict,
                     weights: dict | None) -> tuple[float | None, int]:
    """How well `severity(..., weights)` ranks CONTENT_LOST slides over other-defect ones: by
    within-deck AUC where any such pair exists (a deck's own style cannot win it), else over every
    pair (too few decks carry a CONTENT_LOST verdict to keep to one deck at a time)."""
    if not pos or not neg:
        return None, 0
    score = lambda k: severity(rows[k], thresholds, weights)[0]  # noqa: E731
    a, pairs = auc_within(pos, neg, score)
    if not pairs:                                      # no same-deck pair: fall back to every pair
        a, pairs = auc(list(map(score, pos)), list(map(score, neg))), len(pos) * len(neg)
    return a, pairs


def fit_missing_weight(rows: dict, keys: list, labels: dict, thresholds: dict) -> tuple[float, float | None, int]:
    """The MISSING_METRICS weight (>=1x) whose severity ranks CONTENT_LOST slides over other-defect
    ones best (`content_lost_auc`) - the ordering the flagged-content-missing bug is actually about,
    not the coarser "past a threshold at all" one `calibrate`'s per-metric AUCs already answer. Ties
    go to the smallest weight that reaches the best AUC: a bigger one buys nothing more and only makes
    one slide's missing ink count for an unearned multiple of another's drift. Returns (weight, its
    AUC, pairs it was judged on); (1.0, None, 0) with nothing to calibrate against."""
    pos, neg = content_lost_split(keys, labels)
    if not pos or not neg:
        return 1.0, None, 0
    best_w, best_auc, best_pairs = 1.0, -1.0, 0
    for w in MISSING_WEIGHT_GRID:
        a, pairs = content_lost_auc(rows, pos, neg, thresholds, {m: w for m in MISSING_METRICS})
        if a is not None and a > best_auc + 1e-9:
            best_w, best_auc, best_pairs = w, a, pairs
    return best_w, (best_auc if best_auc >= 0 else None), best_pairs


def rank(tag: str, thresholds: dict, corpora: list[Path], weights: dict | None = None) -> list[dict]:
    """Every slide of run `tag` in `corpora`, worst first."""
    out = []
    for corpus in corpora:
        for p in corpus.glob(f"*/runs/{tag}/metrics.json"):
            d = json.loads(p.read_text(encoding="utf-8"))
            for r in d["slides"]:
                sev, over = severity(r, thresholds, weights)
                out.append({"corpus": corpus.name, "deck": d["deck"], "slide": r["slide"], "severity": round(sev, 2),
                            "over": {m: round(v, 1) for m, v in sorted(over.items(), key=lambda kv: -kv[1])},
                            "sheet": str(p.parent / "sheets" / f"{r['slide']:03}.png")})
    out.sort(key=lambda s: -s["severity"])
    return out


def gallery(tag: str, thresholds: dict, corpora: list[Path], n: int, out: Path,
            previous: Path | None = None, per_deck: int = 2, weights: dict | None = None,
            decks: set[str] | None = None) -> Path:
    """The `n` worst slides of run `tag` as one self-contained HTML page (their sheets inlined:
    the deck | our page | the ink diff), with what each trips, the kind of defect that names, and
    against `previous` (an earlier gallery's .json) which slides are new to the list. The corpora
    are other people's decks: the page is for looking at here, never for publishing. `weights`
    scales SEVERITY metrics (a `calibrate --json`'s "severity_weights"; omitted, every metric counts
    1x as before). `decks` keeps only those decks."""
    import base64
    import html
    import io
    ranked = [s for s in rank(tag, thresholds, corpora, weights) if decks is None or s["deck"] in decks]
    before = set()
    if previous and previous.exists():
        before = {(s["deck"], s["slide"]) for s in json.loads(previous.read_text(encoding="utf-8"))["worst"]}
    top, shown = [], {}
    for s in ranked:                       # a deck's family of defect once or twice, not the whole list
        if len(top) < n and shown.get(s["deck"], 0) < per_deck:
            top.append(s)
            shown[s["deck"]] = shown.get(s["deck"], 0) + 1
    stamp = time.strftime("%Y-%m-%d %H:%M")
    rows = []
    for i, s in enumerate(top, 1):
        img = ""
        if Path(s["sheet"]).exists():
            im = Image.open(s["sheet"]).convert("RGB")
            im.thumbnail((1500, 1500))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            img = f'<img src="data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}">'
        kinds = sorted({SEVERITY[m] for m in list(s["over"])[:3]}, key=list(SEVERITY.values()).index)
        new = before and (s["deck"], s["slide"]) not in before
        trips = " ".join(f"<code>{m}</code>&nbsp;x{v}" for m, v in s["over"].items())
        rows.append(f'<section><h2>{i}. {html.escape(s["deck"])}:{s["slide"]}'
                    f'{" <span class=new>new</span>" if new else ""}<small>{s["corpus"]} · severity {s["severity"]}'
                    f'</small></h2><p class=kind>{html.escape(", ".join(kinds))}</p><p>{trips}</p>{img}</section>')
    total = len(ranked)
    flagged = sum(s["severity"] > 0 for s in ranked)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Worst adopted slides</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ --bg:#fff; --fg:#1b1b1b; --muted:#666; --line:#e3e3e3; --accent:#b3261e; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#161616; --fg:#eee; --muted:#aaa; --line:#333; --accent:#ff8a80; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui,sans-serif; margin:0 auto; max-width:1500px; padding:16px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} header p {{ color:var(--muted); margin:0 0 16px; }}
section {{ border-top:1px solid var(--line); padding:14px 0; }}
h2 {{ font-size:17px; margin:0; }} h2 small {{ color:var(--muted); font-weight:400; margin-left:10px; }}
.kind {{ color:var(--accent); margin:4px 0; }} code {{ font-size:13px; }}
.new {{ background:var(--accent); color:var(--bg); border-radius:4px; font-size:12px; padding:1px 6px; }}
img {{ max-width:100%; height:auto; border:1px solid var(--line); margin-top:6px; }}
</style></head><body><header><h1>Worst adopted slides - run {html.escape(tag)}</h1>
<p>{stamp} · {flagged} of {total} slides past what judged-identical slides reach · each sheet: the deck |
the adopted source | ink (red only in the deck, blue only in ours) · x = times the threshold, capped at
{SEVERITY_CAP:g}</p></header>{"".join(rows)}</body></html>"""
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"worst-{tag}-{time.strftime('%Y%m%d-%H%M')}.html"
    path.write_text(page, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({"tag": tag, "when": stamp, "slides": total, "flagged": flagged,
                                                     "worst": top}, indent=1), encoding="utf-8")
    return path


def heat(values: Floats32, size: tuple[int, int], top: float) -> Image.Image:
    """A map as white (0) to dark red (`top` and over), resized to `size`."""
    v = np.clip(np.nan_to_num(values.astype(np.float32)) / top, 0, 1)
    rgb = np.stack([255 - 115 * v, 255 - 255 * v, 255 - 255 * v], -1).astype(np.uint8)
    return Image.fromarray(rgb).resize(size, Image.NEAREST)


def ink_map(maps: dict, local: bool = False) -> Image.Image:
    """Where the ink went: the deck's ink with none of ours near it red, ours with none of the deck's
    blue, ink that has a counterpart grey (on the spot) to orange (REACH away)."""
    m_ref, m_got, d_ref, d_got = (maps[k] for k in (("l_ref", "l_got", "ld_ref", "ld_got") if local else
                                                     ("m_ref", "m_got", "d_ref", "d_got")))
    out = np.full(m_ref.shape + (3,), 255, np.uint8)
    for m, d in ((m_got, d_got), (m_ref, d_ref)):
        near = m & (d <= REACH)
        k = (d[near] / REACH)[:, None]
        out[near] = (np.array([170, 170, 170]) * (1 - k) + np.array([245, 140, 0]) * k).astype(np.uint8)
    out[m_got & (d_got > REACH)] = (30, 90, 230)
    out[m_ref & (d_ref > REACH)] = (220, 20, 20)
    return Image.fromarray(out)


def show(deck: str, slide: int, tag: str, out: Path, gpu: bool) -> Path:
    """One slide's sheet: the deck | our page | where the ink went, then a map per metric family."""
    from PIL import ImageDraw
    for n, ref, got, ir in pages(deck, tag):
        if n != slide:
            continue
        h, w = ref.shape[:2]
        scores, maps = numpy_metrics(ref, got, ir)
        panels = [("deck", Image.fromarray(ref.astype(np.uint8))), ("adopted", Image.fromarray(got.astype(np.uint8))),
                  ("ink: red missing, blue extra, orange moved", ink_map(maps)),
                  ("local ink: red missing, blue extra, orange moved", ink_map(maps, local=True)),
                  ("local_ink_de", heat(maps["ink_de"], (w, h), 40.0)),
                  ("ground_de", heat(maps["ground_de"], (w, h), 20.0))]
        if gpu:
            g = Gpu()
            (s, m) = g.ssim(ref, got)
            scores.update(s)
            panels.append(("ssim", heat(m, (w, h), 0.5)))
            (s, m) = g.lpips(ref, got)
            scores.update(s)
            panels.append(("lpips", heat(m, (w, h), 0.5)))
            (s, m) = g.dino(ref, got)
            scores.update(s)
            panels.append(("dino patches", heat(m, (w, h), 0.5)))
            scores.update(g.ot(maps["l_ref"], maps["l_got"]))
        cols, pw, ph = 3, w // 2, h // 2
        rows = -(-len(panels) // cols)
        sheet = Image.new("RGB", (cols * (pw + 8), rows * (ph + 26) + 60), "white")
        draw = ImageDraw.Draw(sheet)
        for k, (name, im) in enumerate(panels):
            x, y = (k % cols) * (pw + 8), (k // cols) * (ph + 26)
            draw.text((x + 4, y + 4), name, fill="black")
            sheet.paste(im.resize((pw, ph)), (x, y + 22))
        text = "  ".join(f"{k} {v:.3g}" for k, v in scores.items())
        draw.text((4, rows * (ph + 26) + 8), f"{deck}:{slide} {tag}", fill="black")
        draw.text((4, rows * (ph + 26) + 30), text, fill="black")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{deck}-{slide:03}-{tag}.png"
        sheet.save(path)
        return path
    raise SystemExit(f"{deck} has no slide {slide} in run {tag}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("tag")
    r.add_argument("decks", nargs="*")
    r.add_argument("--gpu", action="store_true", help="the torch metrics too")
    r.add_argument("--jobs", type=int, default=6)
    c = sub.add_parser("calibrate")
    c.add_argument("tag")
    c.add_argument("verdicts", type=Path)
    c.add_argument("--json", type=Path)
    w = sub.add_parser("worst")
    w.add_argument("tag")
    w.add_argument("metric")
    w.add_argument("-n", type=int, default=20)
    f = sub.add_parser("flag")
    f.add_argument("tag")
    f.add_argument("calibration", type=Path, help="a calibrate --json file")
    f.add_argument("-n", type=int, default=40)
    d = sub.add_parser("compare")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("-n", type=int, default=4)
    s = sub.add_parser("show")
    s.add_argument("slide", help="DECK:N")
    s.add_argument("tag")
    s.add_argument("--out", type=Path, default=Path("out") / "metrics")
    s.add_argument("--gpu", action="store_true")
    g = sub.add_parser("gallery", help="the worst slides of a run as one HTML page")
    g.add_argument("tag")
    g.add_argument("calibration", type=Path, help="a calibrate --json file")
    g.add_argument("--corpus", type=Path, action="append", help="a corpus folder (repeat); default the bench's")
    g.add_argument("-n", type=int, default=20)
    g.add_argument("--out", type=Path, default=Path("out") / "grind")
    g.add_argument("--previous", type=Path, help="an earlier gallery's .json: mark slides new to the list")
    g.add_argument("--per-deck", type=int, default=2, help="at most this many slides of one deck")
    a = ap.parse_args(argv)
    if a.cmd == "gallery":
        cal = json.loads(a.calibration.read_text(encoding="utf-8"))
        print(gallery(a.tag, cal["thresholds"], a.corpus or [corpus_dir()], a.n, a.out, a.previous, a.per_deck,
                      weights=cal.get("severity_weights")))
    elif a.cmd == "compare":
        compare(a.before, a.after, a.n)
    elif a.cmd == "flag":
        flag(a.tag, json.loads(a.calibration.read_text(encoding="utf-8"))["thresholds"], a.n)
    elif a.cmd == "show":
        deck, _, n = a.slide.rpartition(":")
        print(show(deck, int(n), a.tag, a.out, a.gpu))
    elif a.cmd == "run":
        run(a.tag, a.decks, a.gpu, a.jobs)
    elif a.cmd == "calibrate":
        table = calibrate(a.tag, a.verdicts)
        print_calibration(table)
        if a.json:
            a.json.write_text(json.dumps(table, indent=1), encoding="utf-8")
    else:
        worst(a.tag, a.metric, a.n)


if __name__ == "__main__":
    sys.exit(main())
