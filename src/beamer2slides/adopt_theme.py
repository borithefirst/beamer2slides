"""A foreign deck's theme, recovered as a beamer theme for the source `adopt` writes.

A deck a person built in Slides keeps its look on its masters and layouts: the backdrop, the bars and
logos, where the title goes and in which font. `deck_ir(foreign=True)` hands each slide what its layout
and master draw (`inherited_chain`), and written into every frame that is the same dozen textblocks on
every slide. Here they are said once, in a `beamertheme<Deck>.sty` beside main.tex:

- each layout the slides use is a `background` template named after the layout's own display name,
  drawing its master's and its own decoration, and a frame picks it with `[layout=<name>]`;
- the page under it (the layout's background colour or picture) comes with the layout; a slide with a
  page of its own says `background=<colour>` or `backdrop=<file>`;
- the layout's title, subtitle and slide number placeholders become part of its template, so the frame
  says `\\frametitle{...}` / `\\framesubtitle{...}` and the number is `\\insertframenumber`.

The rule is that the PDF does not change. A template is the LaTeX those elements were already written
as, so a slide takes its layout only where its own pieces are exactly the template's, character for
character; a slide whose layout decoration came out differently (a fill read off its own thumbnail, a
slide that overrides the layout) keeps its elements in the frame. The template is drawn by beamer's
`background`, which is under everything a frame's textblocks draw (textpos ships those in the page's
foreground): for decoration that is the deck's own order, and a title, subtitle or number goes there only
when nothing the slide draws before it touches it. Placed with textpos's relative mode inside a zero
box at the page's corner, a textblock lands exactly where the absolute one did.

The deck comes in as `deck_ir_types` records; the plan goes out as frozen records too, worked out in
steps over plain local tables and built once at the end.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from .deck_ir_types import (Absent, Box, TargetDeck, TargetElement, TargetParagraph, TargetSlide,
                            TargetText)

Slot = Literal["title", "subtitle", "number"]
# the master's pieces, then the layout's own
Deco = tuple[tuple[str, ...], tuple[str, ...]]
Canvas = tuple[Literal["colour", "picture"], str]
# what `render` writes before and after a placeholder's words
Frame = tuple[str, str]
# (slide, element, (prefix, suffix, words)) for each slide whose slot could move
Found = tuple[int, int, tuple[str, str, str]]


@dataclass(frozen=True, kw_only=True)
class SlotSpec:
    """A slot's placeholder types, the frame command that carries its words (none for the number),
    what the template writes in their place, and the guard that draws it only when there are words."""
    types: tuple[str, ...]
    command: str | None
    insert: str
    guard: str


SLOTS: dict[Slot, SlotSpec] = {
    "title": SlotSpec(types=("TITLE", "CENTERED_TITLE"), command="\\frametitle", insert="\\layouttitle",
                      guard="\\withtitle"),
    "subtitle": SlotSpec(types=("SUBTITLE",), command="\\framesubtitle", insert="\\layoutsubtitle",
                         guard="\\withsubtitle"),
    "number": SlotSpec(types=("SLIDE_NUMBER",), command=None, insert="\\insertframenumber",
                       guard="\\withnumber"),
}
OVERLAP_MARGIN = 1.0        # page pt around a box: what a moved title must keep clear of what is under it
SENTINEL = ("Qqzxbeginslotq", "Qqzxendslotq")
RAW_HASH = re.compile(r"(?<!\\)#")

# beamer's own themes: a deck titled like one gets a name that does not shadow it
BEAMER_THEMES = {n.lower() for n in """AnnArbor Antibes Bergen Berkeley Berlin Boadilla CambridgeUS Copenhagen
Darmstadt Dresden EastLansing Frankfurt Goettingen Hannover Ilmenau JuanLesPins Luebeck Madrid Malmoe
Marburg Montpellier PaloAlto Pittsburgh Rochester Singapore Szeged Warsaw boxes default focus metropolis
moloch""".split()}


@dataclass(frozen=True, kw_only=True)
class FramePlan:
    """What the theme draws for one frame, and so what the frame leaves out."""
    layout: str | None                  # the layout's template name
    drawn: frozenset[int]               # indices of the slide's elements the layout draws
    words: Mapping[Slot, str]           # slot -> the words the frame hands its layout
    background: str | None              # a canvas colour of the slide's own (a colour name)
    backdrop: str | None                # a canvas picture of the slide's own (a tree path)
    nonumber: bool                      # the layout numbers its slides, this one it does not

    def options(self, label: str | None) -> str:
        opts = ["plain"]
        if label:
            # ahead of the theme's own keys, which set templates and colours as they are read
            # (`\deck@keys`): beamer's `label` is then the plain option it looks like
            opts.append(f"label={label}")
        if self.layout:
            opts.append(f"layout={self.layout}")
        if self.background is not None:
            opts.append(f"background={self.background}")
        if self.backdrop:
            opts.append(f"backdrop={self.backdrop}")
        if self.nonumber:
            opts.append("nonumber")
        return "[" + ",".join(opts) + "]"

    def header(self) -> list[str]:
        out: list[str] = []
        for k in ("title", "subtitle"):
            command = SLOTS[k].command
            if k in self.words and command is not None:
                out.append(f"  {command}{{{self.words[k]}}}")
        return out


@dataclass(frozen=True, kw_only=True)
class ThemeLayout:
    """One layout as its template: named apart from `deck_ir_types.Layout`, the deck's own record."""
    slug: str
    name: str
    master: str | None
    deco: Deco
    canvas: Canvas | None
    slots: Mapping[Slot, Frame]         # slot -> (prefix, suffix)
    order: tuple[Slot, ...]             # slots in the order the template draws them
    slides: int


def slug(name: str, taken: set[str]) -> str:
    """A layout's display name as a template name: lower case, words joined by '-'; letters of any
    script stay (LuaTeX reads them in a control sequence name), TeX's special characters go."""
    s = re.sub(r"[^\w]+|_", "-", name.strip().lower()).strip("-") or "layout"
    out, k = s, 2
    while out in taken:
        out, k = f"{s}-{k}", k + 1
    taken.add(out)
    return out


def theme_name(title: str | None) -> str:
    """The theme's name, from the deck's title (`TargetDeck.source.title`)."""
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    name = "".join(w[:1].upper() + w[1:] for w in words)[:40]
    if not name or not name[0].isalpha():
        name = "Deck" + name
    if name.lower() in BEAMER_THEMES:
        name += "Deck"
    return name


def _boxes_touch(a: Box, b: Box, margin: float) -> bool:
    return not (a[2] + margin <= b[0] or b[2] + margin <= a[0] or a[3] + margin <= b[1] or b[3] + margin <= a[1])


def _given(v: str | None | Absent) -> str | None:
    """A slide key deck_ir sometimes leaves out, read as the old `.get` did."""
    return None if isinstance(v, Absent) else v


def slot_parts(el: TargetText, real: str, render: Callable[[TargetElement], str]) -> tuple[str, str, str] | None:
    """(prefix, suffix, words) of a placeholder's LaTeX: `real` is prefix + words + suffix, where the
    prefix and suffix are what `render` writes around any other words (found by writing the element
    again with markers at both ends of its text). None when the words are not one plain stretch."""
    with_runs = [i for i, p in enumerate(el.paragraphs) if p.runs]
    if not with_runs:
        return None
    first, last = with_runs[0], with_runs[-1]

    def marked(i: int, p: TargetParagraph) -> TargetParagraph:
        if i not in (first, last):
            return p
        runs = list(p.runs)
        # one paragraph may be both first and last, one run both ends: both markers go on
        if i == first:
            runs[0] = replace(runs[0], text=SENTINEL[0] + runs[0].text)
        if i == last:
            runs[-1] = replace(runs[-1], text=runs[-1].text + SENTINEL[1])
        return replace(p, runs=tuple(runs))

    got = render(replace(el, paragraphs=tuple(marked(i, p) for i, p in enumerate(el.paragraphs))))
    if got.count(SENTINEL[0]) != 1 or got.count(SENTINEL[1]) != 1:
        return None
    a, b = got.index(SENTINEL[0]), got.index(SENTINEL[1])
    prefix, suffix = got[:a], got[b + len(SENTINEL[1]):]
    if not (real.startswith(prefix) and real.endswith(suffix)) or len(prefix) + len(suffix) > len(real):
        return None
    words = real[len(prefix):len(real) - len(suffix)]
    if not words.strip() or "\n" in words or re.search(r"(?<!\\)%", words) or RAW_HASH.search(words) \
            or words.count("{") != words.count("}"):
        return None
    return prefix, suffix, words


def _deco_of(s: TargetSlide, pieces: list[str], lid: str) -> Deco:
    master = tuple(pieces[k] for k, e in enumerate(s.elements)
                   if e.inherited and e.inherited != lid and pieces[k])
    own = tuple(pieces[k] for k, e in enumerate(s.elements) if e.inherited == lid and pieces[k])
    return master, own


def plan(deck: TargetDeck, pieces: list[list[str]], render: Callable[[TargetElement], str],
         colour: Callable[[str], str], picture: Callable[[str], str | None],
         deck_bg: str | None) -> tuple[str, list[FramePlan]] | None:
    """(theme .sty text, FramePlan per slide), or None when the IR does not say which layouts the
    slides use (a target read before `deck_ir` recorded them).

    `pieces[n][k]`: element k of slide n as `element_latex` wrote it. `render(el)` writes an element
    again (for `slot_parts`), `colour(hex)` names a colour, `picture(file)` copies a picture into the
    tree and gives its path there."""
    if not deck.layouts:
        return None
    layouts_ir = dict(deck.layouts)
    slides = deck.slides
    lids = [_given(s.layout) for s in slides]
    taken: set[str] = set()

    # 1. which slides take their layout's decoration: those whose inherited pieces are the layout's
    #    most common variant, exactly
    variants: dict[str, Counter[Deco]] = {}
    decos: list[Deco | None] = []
    for n, s in enumerate(slides):
        lid = lids[n]
        d = _deco_of(s, pieces[n], lid) if lid is not None and lid in layouts_ir else None
        decos.append(d)
        if lid is not None and d is not None and not any(RAW_HASH.search(p) for part in d for p in part):
            variants.setdefault(lid, Counter())[d] += 1
    # each layout taken, in the order a slide first takes it: (slug, its decoration) and how many take it
    taken_by: dict[str, tuple[str, Deco]] = {}
    count: Counter[str] = Counter()
    layout_of: list[str | None] = [None] * len(slides)
    drawn: list[set[int]] = [set() for _ in slides]
    for n, s in enumerate(slides):
        lid = lids[n]
        if lid is None or lid not in variants:
            continue
        chosen = variants[lid].most_common(1)[0][0]
        if decos[n] != chosen:
            continue                                 # the slide draws its layout differently: explicit
        if lid not in taken_by:
            taken_by[lid] = (slug(layouts_ir[lid].name, taken), chosen)
        count[lid] += 1
        layout_of[n] = lid
        drawn[n] = {k for k, e in enumerate(s.elements) if e.inherited}

    # 2. the page under each layout, and each slide's own page where it differs
    def wanted(s: TargetSlide) -> Canvas | None:
        file = _given(s.background_file)
        if file:
            rel = picture(file)
            if rel:
                return ("picture", rel)
        c = s.background_color
        if c and c != deck_bg:
            return ("colour", colour(c))
        return None

    canvases: dict[str, Canvas | None] = {}
    for lid in taken_by:
        info = layouts_ir[lid]
        pic, col = info.background_picture, info.background_color
        canvas: Canvas | None = None
        if pic:
            for m, s in enumerate(slides):
                file = _given(s.background_file)
                if lids[m] == lid and s.background_picture == pic and file:
                    rel = picture(file)
                    canvas = ("picture", rel) if rel else None
                    break
        elif col and col != deck_bg:
            canvas = ("colour", colour(col))
        canvases[lid] = canvas
    background: list[str | None] = [None] * len(slides)
    backdrop: list[str | None] = [None] * len(slides)
    for n, s in enumerate(slides):
        lid = layout_of[n]
        want, base = wanted(s), (canvases[lid] if lid is not None else None)
        if want == base:
            continue
        if want and want[0] == "picture":
            backdrop[n] = want[1]
        else:
            background[n] = want[1] if want else ("deckbg" if deck_bg else "")

    # 3. title, subtitle, number: into the layout's template where the words are all that differs
    found: dict[str, dict[Slot, list[Found]]] = {}
    for n, s in enumerate(slides):
        lid = layout_of[n]
        if lid is None:
            continue
        els = s.elements
        own = [k for k, e in enumerate(els) if not e.inherited and pieces[n][k]]
        for slot, spec in SLOTS.items():
            k = next((k for k in own if _placeholder(els[k]) in spec.types), None)
            if k is None or RAW_HASH.search(pieces[n][k]):
                continue
            el = els[k]
            parts = slot_parts(el, pieces[n][k], render) if isinstance(el, TargetText) else None
            if parts is None or (slot == "number" and parts[2] != str(n + 1)):
                continue
            found.setdefault(lid, {}).setdefault(slot, []).append((n, k, parts))
    slot_frames: dict[str, dict[Slot, Frame]] = {}
    for lid, slots in found.items():
        frames: dict[Slot, Frame] = {}
        for slot in SLOTS:
            if slot in slots:
                frames[slot] = Counter((p[0], p[1]) for _, _, p in slots[slot]).most_common(1)[0][0]
        slot_frames[lid] = frames
    orders = {lid: tuple(s for s in SLOTS if s in frames) for lid, frames in slot_frames.items()}
    # which slides hand their slots over: every slot the template has, the slide's pieces equal to it
    # and nothing drawn before them in the slide on top of them
    words: list[dict[Slot, str]] = [{} for _ in slides]
    nonumber = [False] * len(slides)
    for n, s in enumerate(slides):
        lid = layout_of[n]
        if lid is None or lid not in found:
            continue
        frames, order, els = slot_frames[lid], orders[lid], s.elements
        moved: dict[Slot, tuple[int, str]] = {}
        for slot, items in found[lid].items():
            for m, k, parts in items:
                if m == n and (parts[0], parts[1]) == frames.get(slot):
                    moved[slot] = (k, parts[2])
        # the template draws its slots over the decoration and under everything the frame draws, in
        # `order`: drop a slot while anything the slide drew before it touches it
        changed = True
        while changed:
            changed = False
            for slot, (k, _) in list(moved.items()):
                ks = {kk for kk, _ in moved.values()}
                below = [j for j in range(k) if not els[j].inherited and pieces[n][j] and j not in ks]
                # moved slots keep the template's order among themselves, or must not touch
                crossed = [kk for sl, (kk, _) in moved.items() if sl != slot and
                           (order.index(sl) < order.index(slot)) != (kk < k)]
                if any(_boxes_touch(els[j].bbox, els[k].bbox, OVERLAP_MARGIN) for j in below + crossed):
                    del moved[slot]
                    changed = True
                    break
        for slot, (k, said) in moved.items():
            drawn[n].add(k)
            if slot != "number":
                words[n][slot] = said
        if "number" in frames and "number" not in moved:
            nonumber[n] = True

    layouts = [ThemeLayout(slug=name, name=layouts_ir[lid].name, master=layouts_ir[lid].master, deco=deco,
                           canvas=canvases[lid], slots=slot_frames.get(lid, {}), order=orders.get(lid, ()),
                           slides=count[lid])
               for lid, (name, deco) in taken_by.items()]
    plans = [FramePlan(layout=taken_by[lid][0] if lid is not None else None, drawn=frozenset(drawn[n]),
                       words=words[n], background=background[n], backdrop=backdrop[n], nonumber=nonumber[n])
             for n, lid in enumerate(layout_of)]
    return sty(deck, layouts, deck_bg), plans


def _placeholder(el: TargetElement) -> str | None:
    """A text element's placeholder type; nothing else is a slot."""
    return el.placeholder if isinstance(el, TargetText) else None


PRELUDE = r"""% What the deck's masters and layouts draw, said once. A frame names its layout:
%   \begin{frame}[plain,layout=<name>]  - the layout's decoration, page colour or picture,
%                                         and its title, subtitle and number placeholders
%   background=<colour>, backdrop=<file> - a page of the slide's own
%   \frametitle{..} \framesubtitle{..}  - the words the layout's placeholders show
\mode<presentation>
\RequirePackage[absolute,overlay]{textpos}
\RequirePackage{graphicx}

% A layout's decoration sits in beamer's background, under the frame's own textblocks: textpos
% places them there in its relative mode, from the page's top left corner.
\newcommand{\layoutdecoration}[1]{\vbox to 0pt{\TP@absposfalse#1\vss}}
\newcommand{\layouttitle}{\beamer@frametitle}
\def\deck@unbrace#1{\@firstofone#1}
\newcommand{\layoutsubtitle}{\expandafter\deck@unbrace\insertframesubtitle}
\newcommand{\withtitle}[1]{\ifx\beamer@frametitle\@empty\else#1\fi}
\newcommand{\withsubtitle}[1]{\ifx\insertframesubtitle\@empty\else#1\fi}
\newif\ifdeck@nonumber
\newcommand{\withnumber}[1]{\ifdeck@nonumber\else#1\fi}
\newcommand{\layoutcanvas}[2]{\@namedef{deck@canvas@#1}{#2}}
\newcommand{\defmaster}[2]{\expandafter\long\expandafter\def\csname deck@master@#1\endcsname{#2}}
\newcommand{\drawmaster}[1]{\@nameuse{deck@master@#1}}
% (\setbeamercolor reads keys of its own, which would end the frame's option list: `\deck@keys`)
\newcommand{\deck@keys}[1]{\let\deck@kv\KV@prefix#1\let\KV@prefix\deck@kv}
\define@key{beamerframe}{layout}{\deck@keys{\setbeamertemplate{background}[#1]\@nameuse{deck@canvas@#1}}}
\define@key{beamerframe}{background}{\deck@keys{\setbeamertemplate{background canvas}[default]%
  \setbeamercolor{background canvas}{bg=#1}}}
\define@key{beamerframe}{backdrop}{\deck@keys{\setbeamertemplate{background canvas}{%
  \includegraphics[width=\paperwidth,height=\paperheight]{#1}}}}
\define@key{beamerframe}{nonumber}[true]{\deck@nonumbertrue}
% frame options last until the next frame, which starts from the deck's page again
\AddToHook{env/frame/before}{\setbeamertemplate{background}{}\deck@nonumberfalse
  \setbeamertemplate{background canvas}[default]\setbeamercolor{background canvas}{bg=DECKBG}}
"""


def sty(deck: TargetDeck, layouts: list[ThemeLayout], deck_bg: str | None) -> str:
    title = deck.source.title or "the deck"
    out = [f"% The theme of {title!s}, as `beamer2slides adopt` read it from its masters and layouts.",
           PRELUDE.replace("DECKBG", "deckbg" if deck_bg else "")]
    # what a master draws, when more than one layout draws it, is said once (two masters that draw
    # the same are one: a deck copied from another keeps both)
    uses = Counter(lay.deco[0] for lay in layouts if lay.deco[0])
    masters: dict[tuple[str, ...], str] = {}
    taken: set[str] = set()
    names = {lid: info.name for lid, info in deck.layouts or ()}
    for part, n in uses.items():
        if n > 1:
            mid = next(lay.master for lay in layouts if lay.deco[0] == part)
            said = names.get(mid) if mid is not None else None
            masters[part] = slug(said or "master", taken)
            out.append(f"% master \"{said or mid}\"")
            out.append(f"\\defmaster{{{masters[part]}}}{{%\n" + "\n".join(part) + "\n}")
    for lay in layouts:
        out.append(f"\n% layout \"{lay.name.strip()}\"" + (f", {lay.slides} slide" + "s" * (lay.slides != 1)))
        body: list[str] = []
        if lay.deco[0]:
            m = masters.get(lay.deco[0])
            body += [f"  \\drawmaster{{{m}}}"] if m else list(lay.deco[0])
        body += list(lay.deco[1])
        for slot in lay.order:
            prefix, suffix = lay.slots[slot]
            spec = SLOTS[slot]
            # `{}` ends the command's name and keeps a space after it, as the words did
            body.append(f"  {spec.guard}{{%\n{prefix}{spec.insert}{{}}{suffix}}}")
        if body:
            out.append(f"\\defbeamertemplate{{background}}{{{lay.slug}}}{{\\layoutdecoration{{%\n"
                       + "\n".join(body) + "\n}}")
        else:
            out.append(f"\\defbeamertemplate{{background}}{{{lay.slug}}}{{}}")
        if lay.canvas:
            kind, value = lay.canvas
            what = (f"\\setbeamercolor{{background canvas}}{{bg={value}}}" if kind == "colour" else
                    "\\setbeamertemplate{background canvas}{\\includegraphics[width=\\paperwidth,"
                    f"height=\\paperheight]{{{value}}}}}")
            out.append(f"\\layoutcanvas{{{lay.slug}}}{{{what}}}")
    out.append("\n\\mode<all>\n")
    return "\n".join(out)
