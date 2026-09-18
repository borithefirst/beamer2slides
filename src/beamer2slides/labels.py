r"""Frame labels: the one piece of identity that survives compiling (docs/labels.md).

`\begin{frame}[label=intro]` writes a PDF named destination `intro` on the frame's first page,
and `intro<n>` on each overlay step. `extract.frame_labels` reads those back, `identity` keys the
slide by them, and `sync` pairs the deck's slide with the source's frame by that key wherever the
frame moved. A frame with no label falls back to its title, its occurrence among frames of that
title, and its position - which is what breaks when titles repeat or frames are reordered.

So a label per frame, stable over the life of the deck, is what makes a sync reliable. This module
is the two halves of keeping that true:

- `survey` reads a converted deck's slide infos and says which slides have no label and which
  labels name more than one frame. `convert --check-labels` prints it (`error` refuses).
- `plan` reads the .tex itself (`texmap.Source`, no compilation) and writes a label into every
  frame that has none, leaving every existing label exactly as it is. `python -m beamer2slides
  label main.tex --apply` is that, with the same backups `pull --apply` keeps.

What this module will not do is rename or move an existing label. A label is a promise to the deck
that was converted from it: changing one tells sync that a frame it knows is a different frame.
Duplicates are reported, never resolved, for the same reason - we cannot know which of the two the
deck's slide came from.
"""

import re
import unicodedata

from . import texmap

MAX_SLUG = 28
# Everything a beamer option list, a PDF destination name and a hyperref \hyperlink can carry
# without quoting. Deliberately narrow: a label is not prose.
SAFE = re.compile(r"[^a-z0-9-]+")


def slug(title: str | None, taken: set[str], fallback: str = "frame") -> str:
    """A readable label from a frame title, unique among `taken`. Accents are folded (a PDF
    destination name is not the place to find out how a viewer encodes them) and the result is
    kept short enough to read in a diff."""
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    base = re.sub(r"-{2,}", "-", SAFE.sub("-", text)).strip("-")[:MAX_SLUG].strip("-") or fallback
    if base[0].isdigit():
        base = f"f-{base}"[:MAX_SLUG]
    if base not in taken:
        return base
    for n in range(2, 1000):
        cand = f"{base[:MAX_SLUG - len(str(n)) - 1]}-{n}"
        if cand not in taken:
            return cand
    raise ValueError(f"cannot make a label out of {title!r}")


def survey(infos: list[dict]) -> dict:
    """What a converted deck's slides say about labels: the ones without, and the labels that name
    more than one frame. Overlay steps of one frame share its label and are not duplicates, so
    slides are counted by frame (`page` is the frame's first page for every step of it).

    A label written on two frames is *not* one of the duplicates found here, because it never
    reaches the PDF twice: hyperref keeps the first destination of a name and drops the second, so
    the second frame arrives with no label at all and shows up under `unlabelled`
    (`tests/test_stress_live.py::test_a_label_written_twice_reaches_the_pdf_as_no_label_at_all`).
    Only `plan` below, which reads the `.tex`, can see that case. What is left for `duplicates` is
    a label whose slides are not one run, or whose steps do not agree on a title - which the PDF
    can show and which nothing downstream expects."""
    frames: dict[object, dict] = {}
    for i, info in enumerate(infos):
        key = info.get("label") or ("#", i)
        frames.setdefault(key, {"label": info.get("label"), "titles": [], "slides": []})
        frames[key]["slides"].append(i)
        if info.get("title"):
            frames[key]["titles"].append(info["title"])
    unlabelled = [{"slide": f["slides"][0], "title": (f["titles"] or [None])[0]}
                  for f in frames.values() if not f["label"]]
    duplicates = []
    for f in frames.values():
        if not f["label"]:
            continue
        titles = sorted({t for t in f["titles"]})
        spread = f["slides"] != list(range(f["slides"][0], f["slides"][0] + len(f["slides"])))
        # Steps of one frame are consecutive and say the same thing. Two frames that share a label,
        # sit next to each other and have the same title cannot be told apart from steps here: that
        # is a distinction only the .tex has, and `label` (this module's other half) makes it there.
        if len(titles) > 1 or spread:
            duplicates.append({"label": f["label"], "titles": titles, "slides": f["slides"]})
    return {"slides": len(infos), "frames": len(frames), "unlabelled": unlabelled, "duplicates": duplicates}


def problems(found: dict) -> list[str]:
    """Human lines for a survey, empty when every frame carries a label of its own."""
    out = []
    if found["unlabelled"]:
        which = ", ".join(f"{u['title'] or 'untitled'} (slide {u['slide'] + 1})" for u in found["unlabelled"][:3])
        out.append(f"{len(found['unlabelled'])} of {found['frames']} frames have no label: {which}"
                   f"{', ...' if len(found['unlabelled']) > 3 else ''}. Their identity falls back to the title and "
                   f"the position, so a sync can lose track of them when frames are reordered or titles repeat. "
                   f"`python -m beamer2slides label <main.tex> --apply` writes one into each - and says so if "
                   f"one of them does carry a `label=` that another frame already uses, which the PDF keeps "
                   f"only once, so from here the frame looks unlabelled.")
    for d in found["duplicates"]:
        out.append(f"label `{d['label']}` is on more than one frame ({'; '.join(d['titles'][:3])}): a sync cannot "
                   f"tell which of them a slide came from. Give each frame a label of its own.")
    return out


def plan(source: texmap.Source) -> list[dict]:
    """One edit per frame that has no label: where to write it, and what. Frames that already have
    one are left alone, and their labels are what the new ones are kept distinct from."""
    taken = {f.label for f in source.frames if f.label}
    edits = []
    for frame in source.frames:
        if frame.label:
            continue
        name = slug(frame.title, taken)
        taken.add(name)
        if frame.opts_end >= 0:
            at, text = frame.opts_end, (f",label={name}" if frame.options.strip() else f"label={name}")
        else:
            at, text = frame.opts_at, f"[label={name}]"
        edits.append({"file": frame.file, "at": at, "text": text, "label": name,
                      "title": frame.title, "line": frame.begin_line, "index": frame.index})
    return edits


def apply(source: texmap.Source, edits: list[dict]) -> dict:
    """The new text of each file the edits touch. Offsets are into the file as it was read, so the
    edits of one file are written back to front and never move one another."""
    out: dict = {}
    for path in {e["file"] for e in edits}:
        text = source.text(path)
        for e in sorted((e for e in edits if e["file"] == path), key=lambda e: e["at"], reverse=True):
            text = text[:e["at"]] + e["text"] + text[e["at"]:]
        out[path] = text
    return out
