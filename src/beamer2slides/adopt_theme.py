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
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field

# title, subtitle and slide number: which placeholder, the frame command that carries the words, and
# what the template writes in their place
SLOTS = {
    "title": (("TITLE", "CENTERED_TITLE"), "\\frametitle", "\\layouttitle", "\\withtitle"),
    "subtitle": (("SUBTITLE",), "\\framesubtitle", "\\layoutsubtitle", "\\withsubtitle"),
    "number": (("SLIDE_NUMBER",), None, "\\insertframenumber", "\\withnumber"),
}
OVERLAP_MARGIN = 1.0        # page pt around a box: what a moved title must keep clear of what is under it
SENTINEL = ("Qqzxbeginslotq", "Qqzxendslotq")
RAW_HASH = re.compile(r"(?<!\\)#")

# beamer's own themes: a deck titled like one gets a name that does not shadow it
BEAMER_THEMES = {n.lower() for n in """AnnArbor Antibes Bergen Berkeley Berlin Boadilla CambridgeUS Copenhagen
Darmstadt Dresden EastLansing Frankfurt Goettingen Hannover Ilmenau JuanLesPins Luebeck Madrid Malmoe
Marburg Montpellier PaloAlto Pittsburgh Rochester Singapore Szeged Warsaw boxes default focus metropolis
moloch""".split()}


@dataclass
class FramePlan:
    """What the theme draws for one frame, and so what the frame leaves out."""
    layout: str | None = None                  # the layout's template name
    drawn: set = field(default_factory=set)    # indices of the slide's elements the layout draws
    words: dict = field(default_factory=dict)  # slot -> the words the frame hands its layout
    background: str | None = None              # a canvas colour of the slide's own (a colour name)
    backdrop: str | None = None                # a canvas picture of the slide's own (a tree path)
    nonumber: bool = False                     # the layout numbers its slides, this one it does not

    def options(self) -> str:
        opts = ["plain"]
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
        return [f"  {SLOTS[k][1]}{{{self.words[k]}}}" for k in ("title", "subtitle") if k in self.words]


@dataclass
class Layout:
    slug: str
    name: str
    master: str | None
    deco: tuple = ((), ())                       # (master pieces, layout pieces)
    canvas: tuple | None = None                  # ("colour", name) | ("picture", path) | None
    slots: dict = field(default_factory=dict)    # slot -> (prefix, suffix)
    order: list = field(default_factory=list)    # slots in the order the template draws them
    slides: int = 0


def slug(name: str, taken: set[str]) -> str:
    """A layout's display name as a template name: lower case, words joined by '-'; letters of any
    script stay (LuaTeX reads them in a control sequence name), TeX's special characters go."""
    s = re.sub(r"[^\w]+|_", "-", name.strip().lower()).strip("-") or "layout"
    out, k = s, 2
    while out in taken:
        out, k = f"{s}-{k}", k + 1
    taken.add(out)
    return out


def theme_name(target: dict) -> str:
    title = (target.get("source") or {}).get("title") or ""
    words = re.findall(r"[A-Za-z0-9]+", title)
    name = "".join(w[:1].upper() + w[1:] for w in words)[:40]
    if not name or not name[0].isalpha():
        name = "Deck" + name
    if name.lower() in BEAMER_THEMES:
        name += "Deck"
    return name


def _boxes_touch(a: list, b: list, margin: float = OVERLAP_MARGIN) -> bool:
    return not (a[2] + margin <= b[0] or b[2] + margin <= a[0] or a[3] + margin <= b[1] or b[3] + margin <= a[1])


def slot_parts(el: dict, real: str, render) -> tuple[str, str, str] | None:
    """(prefix, suffix, words) of a placeholder's LaTeX: `real` is prefix + words + suffix, where the
    prefix and suffix are what `render` writes around any other words (found by writing the element
    again with markers at both ends of its text). None when the words are not one plain stretch."""
    paras = [p for p in el.get("paragraphs") or [] if p.get("runs")]
    if not paras:
        return None
    marked = copy.deepcopy(el)
    ps = [p for p in marked["paragraphs"] if p.get("runs")]
    ps[0]["runs"][0]["text"] = SENTINEL[0] + ps[0]["runs"][0]["text"]
    ps[-1]["runs"][-1]["text"] = ps[-1]["runs"][-1]["text"] + SENTINEL[1]
    got = render(marked)
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


def plan(target: dict, pieces: list[list[str]], render, colour, picture, deck_bg: str | None):
    """(theme .sty text, FramePlan per slide), or None when the IR does not say which layouts the
    slides use (a target read before `deck_ir` recorded them).

    `pieces[n][k]`: element k of slide n as `element_latex` wrote it. `render(el)` writes an element
    again (for `slot_parts`), `colour(hex)` names a colour, `picture(file)` copies a picture into the
    tree and gives its path there."""
    layouts_ir = target.get("layouts")
    if not layouts_ir:
        return None
    slides = target["slides"]
    taken: set[str] = set()
    layouts: dict[str, Layout] = {}
    plans = [FramePlan() for _ in slides]

    def deco_of(n: int) -> tuple:
        s = slides[n]
        lid = s.get("layout")
        master = [pieces[n][k] for k, e in enumerate(s["elements"]) if e.get("inherited") and e["inherited"] != lid and pieces[n][k]]
        own = [pieces[n][k] for k, e in enumerate(s["elements"]) if e.get("inherited") == lid and pieces[n][k]]
        return tuple(master), tuple(own)

    # 1. which slides take their layout's decoration: those whose inherited pieces are the layout's
    #    most common variant, exactly
    variants: dict[str, Counter] = {}
    decos = []
    for n, s in enumerate(slides):
        lid = s.get("layout")
        d = deco_of(n) if lid in layouts_ir else None
        decos.append(d)
        if d is not None and not any(RAW_HASH.search(p) for part in d for p in part):
            variants.setdefault(lid, Counter())[d] += 1
    for n, s in enumerate(slides):
        lid = s.get("layout")
        if lid not in variants:
            continue
        chosen = variants[lid].most_common(1)[0][0]
        if decos[n] != chosen:
            continue                                 # the slide draws its layout differently: explicit
        if lid not in layouts:
            info = layouts_ir[lid]
            layouts[lid] = Layout(slug(info.get("name") or "", taken), info.get("name") or "",
                                  info.get("master"), chosen)
        layouts[lid].slides += 1
        plans[n].layout = layouts[lid].slug
        plans[n].drawn = {k for k, e in enumerate(s["elements"]) if e.get("inherited")}

    # 2. the page under each layout, and each slide's own page where it differs
    def wanted(s: dict):
        if s.get("background_file"):
            rel = picture(s["background_file"])
            if rel:
                return ("picture", rel)
        c = s.get("background_color")
        if c and c != deck_bg:
            return ("colour", colour(c))
        return None

    for lid, lay in layouts.items():
        info = layouts_ir[lid]
        pic, col = info.get("background_picture"), info.get("background_color")
        canvas = None
        if pic:
            for s in slides:
                if s.get("layout") == lid and s.get("background_picture") == pic and s.get("background_file"):
                    rel = picture(s["background_file"])
                    canvas = ("picture", rel) if rel else None
                    break
        elif col and col != deck_bg:
            canvas = ("colour", colour(col))
        lay.canvas = canvas
    for n, s in enumerate(slides):
        lay = next((v for v in layouts.values() if v.slug == plans[n].layout), None)
        want, base = wanted(s), (lay.canvas if lay else None)
        if want == base:
            continue
        if want and want[0] == "picture":
            plans[n].backdrop = want[1]
        else:
            plans[n].background = want[1] if want else ("deckbg" if deck_bg else "")

    # 3. title, subtitle, number: into the layout's template where the words are all that differs
    found: dict[str, dict[str, list]] = {}
    for n, s in enumerate(slides):
        lid = s.get("layout")
        if lid not in layouts or plans[n].layout is None:
            continue
        els = s["elements"]
        own = [k for k, e in enumerate(els) if not e.get("inherited") and pieces[n][k]]
        for slot, (types, *_rest) in SLOTS.items():
            k = next((k for k in own if els[k].get("placeholder") in types and els[k]["kind"] == "text"), None)
            if k is None or RAW_HASH.search(pieces[n][k]):
                continue
            parts = slot_parts(els[k], pieces[n][k], render)
            if parts is None or (slot == "number" and parts[2] != str(n + 1)):
                continue
            found.setdefault(lid, {}).setdefault(slot, []).append((n, k, parts))
    for lid, slots in found.items():
        lay = layouts[lid]
        for slot in SLOTS:
            if slot not in slots:
                continue
            common = Counter(p[:2] for _, _, p in slots[slot]).most_common(1)[0][0]
            lay.slots[slot] = common
        lay.order = [s for s in SLOTS if s in lay.slots]
    # which slides hand their slots over: every slot the template has, the slide's pieces equal to it
    # and nothing drawn before them in the slide on top of them
    for n, s in enumerate(slides):
        lid = s.get("layout")
        if lid not in found or plans[n].layout is None:
            continue
        lay, els = layouts[lid], s["elements"]
        moved = {}
        for slot, items in found[lid].items():
            for m, k, parts in items:
                if m == n and parts[:2] == lay.slots.get(slot):
                    moved[slot] = (k, parts[2])
        # the template draws its slots over the decoration and under everything the frame draws, in
        # `lay.order`: drop a slot while anything the slide drew before it touches it
        changed = True
        while changed:
            changed = False
            for slot, (k, _) in list(moved.items()):
                ks = {kk for kk, _ in moved.values()}
                below = [j for j in range(k) if not els[j].get("inherited") and pieces[n][j] and j not in ks]
                # moved slots keep the template's order among themselves, or must not touch
                crossed = [kk for sl, (kk, _) in moved.items() if sl != slot and
                           (lay.order.index(sl) < lay.order.index(slot)) != (kk < k)]
                if any(_boxes_touch(els[j]["bbox"], els[k]["bbox"]) for j in below + crossed):
                    del moved[slot]
                    changed = True
                    break
        for slot, (k, words) in moved.items():
            plans[n].drawn.add(k)
            if slot != "number":
                plans[n].words[slot] = words
        if "number" in lay.slots and "number" not in moved:
            plans[n].nonumber = True
    return sty(target, layouts, deck_bg), plans


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


def sty(target: dict, layouts: dict[str, Layout], deck_bg: str | None) -> str:
    title = (target.get("source") or {}).get("title") or "the deck"
    out = [f"% The theme of {title!s}, as `beamer2slides adopt` read it from its masters and layouts.",
           PRELUDE.replace("DECKBG", "deckbg" if deck_bg else "")]
    # what a master draws, when more than one layout draws it, is said once (two masters that draw
    # the same are one: a deck copied from another keeps both)
    uses = Counter(lay.deco[0] for lay in layouts.values() if lay.deco[0])
    masters: dict = {}
    taken: set[str] = set()
    names = {lid: info.get("name") or "" for lid, info in (target.get("layouts") or {}).items()}
    for part, count in uses.items():
        if count > 1:
            mid = next(lay.master for lay in layouts.values() if lay.deco[0] == part)
            masters[part] = slug(names.get(mid) or "master", taken)
            out.append(f"% master \"{names.get(mid) or mid}\"")
            out.append(f"\\defmaster{{{masters[part]}}}{{%\n" + "\n".join(part) + "\n}")
    for lay in layouts.values():
        out.append(f"\n% layout \"{lay.name.strip()}\"" + (f", {lay.slides} slide" + "s" * (lay.slides != 1)))
        body = []
        if lay.deco[0]:
            m = masters.get(lay.deco[0])
            body += [f"  \\drawmaster{{{m}}}"] if m else list(lay.deco[0])
        body += list(lay.deco[1])
        for slot in lay.order:
            prefix, suffix = lay.slots[slot]
            _types, _cmd, insert, guard = SLOTS[slot]
            # `{}` ends the command's name and keeps a space after it, as the words did
            body.append(f"  {guard}{{%\n{prefix}{insert}{{}}{suffix}}}")
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
