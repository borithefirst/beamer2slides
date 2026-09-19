"""The macro layer adopt writes its frames in (`slides.sty` beside main.tex, `adopt.SLIDES_TEXT` and
`adopt_shapes.SHAPE_MACRO`), and the names it gives the deck's text styles and colours.

The layer is only worth having if it draws what the spelled-out TeX drew: the bench shows every page of
three decks pixel-identical (docs/adopt-bench.md); here the short forms are compiled beside their long
forms and compared page against page, where lualatex is at hand.
"""

import re
import shutil
import subprocess

import numpy as np
import pytest

from beamer2slides import adopt, adopt_shapes
from beamer2slides.deck_ir import deck_ir
from beamer2slides.inverse import tex_env
from beamer2slides.texmap import build_visible

from . import test_adopt_text as T
from .test_adopt_shapes import shape, transform


@pytest.fixture(autouse=True)
def no_machine_fonts(monkeypatch, tmp_path):
    monkeypatch.setenv("B2S_FONTS", str(tmp_path / "no-fonts-here"))


def sample_deck() -> dict:
    """A one-paragraph middle-aligned box, a bulleted two-paragraph one, a rectangle and an ellipse."""
    return T.deck(
        T.box("s_one", T.para("One middle paragraph", runs=[("One middle paragraph", {"fontSize": T.pt(24)})]),
              x=60, y=40, w=300, h=90, contentAlignment="MIDDLE"),
        T.box("s_two", T.para("First item", level=0) + T.para("Second, deeper", level=1, glyph="○"),
              x=60, y=160, w=300, h=150, placeholder={"type": "BODY", "parentObjectId": "m_body"}),
        shape("r", "RECTANGLE", 120, 60, transform(420, 60), outline="000000"),
        shape("e", "ELLIPSE", 120, 80, transform(420, 180), fill="EA4335"))


def written(tmp_path) -> tuple[str, str]:
    text = adopt.bootstrap(deck_ir(sample_deck(), foreign=True), tmp_path / "tree" / "main.tex")
    return text, (tmp_path / "tree" / "slides.sty").read_text(encoding="utf-8")


def test_the_macros_are_a_package_beside_main_tex(tmp_path):
    text, sty = written(tmp_path)
    assert sty.startswith("%% slides.sty") and "\\ProvidesPackage{slides}" in sty
    assert "\\makeatletter" not in sty and "\\makeatother" not in sty, "@ is a letter in a .sty"
    for macro in ("\\newenvironment{slidebox}", "\\newcommand\\slidepar", "\\newcommand\\slidetext",
                  "\\newcommand\\slidestyle", "\\newcommand\\slideshape", "\\newcommand\\sliderect",
                  "\\newcommand\\slideellipse"):
        assert macro in sty and macro not in text, macro
    assert "\\usepackage" not in sty, "packages are loaded by main.tex, before slides.sty"
    preamble = text[:text.index("\\begin{document}")]
    assert preamble.index("\\usepackage{tikz}") < preamble.index("\\usepackage{slides}")
    assert preamble.index("\\usepackage{slides}") < preamble.index("\\slidestyle{")
    assert not re.search(r"^\\slidestyle\{", sty, re.M), "the deck's own styles are in main.tex"


def test_a_frame_reads_as_boxes_styles_and_words(tmp_path):
    text, _ = written(tmp_path)
    frame = T.frame_of(text)
    assert "\\slidetext[middle]{" in frame and "{One middle paragraph}" in frame
    assert "\\begin{slidebox}" in frame and "\\begin{itemize}" in frame and "\\item " in frame
    assert "\\sliderect[" in frame and "\\slideellipse[" in frame
    for plumbing in ("\\vbox", "\\vskip", "\\prevdepth", "\\baselineskip", "\\leftskip", "\\llap", "textblock",
                     "tikzpicture", "b2s", "\\slidebullet"):
        assert plumbing not in frame, plumbing
    # every style the frames and the deck's defaults name is defined once in the preamble
    used = set(re.findall(r"\\slidetext(?:\[[^\]]*\])?\{[^}]*\}\{([\w-]+)\}\{", frame))
    used |= set(re.findall(r"(?<![\w])(?:label)?style=([\w-]+)", text[text.index("\\usepackage{slides}"):]))
    defined = re.findall(r"^\\slidestyle\{([\w-]+)\}", text, re.M)
    assert used and used <= set(defined) and len(defined) == len(set(defined))
    marks = set(re.findall(r"mark=([\w-]+)", text))
    assert marks and marks <= set(re.findall(r"^\\slidemark\{([\w-]+)\}", text, re.M))


def test_words_ending_in_a_tie_keep_it_from_par():
    """\\par takes the last glue off a paragraph. Spelled out, a line end followed the words and \\par
    took that; in `\\slidepar{words}` nothing follows, so a closing space stands in for it
    (arabic-training's "Meeting~~~~~~" moved its line, comps-analysis' underlined "Pros ~ ~")."""
    def words_of(text):
        el = {"kind": "text", "bbox": [0, 0, 100, 40], "box": {"scale": 1.0, "valign": "top"},
              "paragraphs": [{"runs": [{"text": text, "size": 10.0}], "slides": {}}]}
        return re.search(r"\{body\}\{(.*)\}$", adopt.text_box_latex(el, adopt.Context(), ""))[1]
    assert words_of("Meeting  ") == "Meeting~~ "
    assert words_of("Plain words") == "Plain words", "nothing to take: nothing added"


def test_text_styles_are_named_by_size_and_what_sets_them_apart():
    ctx = adopt.Context()
    ctx.body_size, ctx.main_colour = 10.0, "#202124"
    m = ("9.68", "12", "2.32")
    assert adopt.text_style(ctx, 10.0, colour="#202124", metrics=m) == "body"
    assert adopt.text_style(ctx, 10.0, colour="#202124", metrics=m) == "body", "one name per style"
    assert adopt.text_style(ctx, 24.0, weight="bold", colour="#202124", metrics=m) == "title-bold"
    assert adopt.text_style(ctx, 15.0, colour="#4285f4", metrics=m) == "heading-blue"
    assert adopt.text_style(ctx, 8.0, family="mono", italic=True, metrics=m) == "small-mono-italic"
    assert adopt.text_style(ctx, 10.0, weight="w600", colour="#202124", metrics=m) == "body-semibold"
    assert adopt.text_style(ctx, 10.0, colour="#202124") == "label", "a bullet's style has no line box"
    # the same name for another style is told apart by its size, then by a number
    assert adopt.text_style(ctx, 10.0, colour="#202124", metrics=("9.68", "14", "4.32")) == "body-10"
    assert adopt.text_style(ctx, 10.0, colour="#202124", metrics=("9.68", "15", "5.32")) == "body-10-2"
    lines = adopt.style_definitions(ctx)
    assert lines[0] == "\\slidestyle{body}{size=10, color=b2s202124, ascent=9.68, pitch=12, depth=2.32}"
    assert "\\slidestyle{small-mono-italic}{size=8, family=mono, italic, ascent=9.68, pitch=12, depth=2.32}" in lines


def test_colours_are_named_by_what_they_look_like():
    assert [adopt.colour_word(c) for c in ("#4285F4", "#EA4335", "#202124", "#F1F3F4", "#34A853", "#7F7F7F")] == \
        ["Blue", "Red", "NearBlack", "OffWhite", "Green", "Grey"]
    text = ("\\definecolor{b2s4285F4}{HTML}{4285F4}\\definecolor{b2s1A73E8}{HTML}{1A73E8}\n"
            "\\color{b2s1A73E8} \\color{b2s1A73E8} \\color{b2s4285F4} b2s4285F4x")
    renamed = adopt.rename_colours(text, {})
    assert "\\definecolor{Blue}{HTML}{1A73E8}" in renamed, "the most used blue is Blue"
    assert "\\definecolor{Blue2}{HTML}{4285F4}" in renamed
    assert renamed.endswith("b2s4285F4x"), "only whole names"


def test_a_rectangle_or_an_ellipse_is_one_line_only_when_the_macro_draws_the_same_path():
    ctx = adopt.Context()
    rect, turned, rounded, oval = (adopt_shapes.shape_block(e, ctx, "") for e in shapes_ir())
    assert rect.startswith("\\sliderect[") and "cycle" not in rect
    assert turned.startswith("\\slideshape{") and "cm={" in turned
    assert rounded.startswith("\\slideshape{") and "controls" in rounded
    assert oval.startswith("\\slideellipse[")
    x, y, rx, ry = (float(v) for v in re.search(r"\{([^}]*)\}\s*$", oval)[1].split(","))
    assert abs(rx - 120 / 720 * 453.54 / 2) < 0.01 and abs(ry - 80 / 720 * 453.54 / 2) < 0.01


def shapes_ir() -> list[dict]:
    from .test_adopt_shapes import elements
    return elements(shape("r", "RECTANGLE", 120, 60, transform(100, 60)),
                    shape("t", "RECTANGLE", 120, 60, transform(100, 160, deg=30)),
                    shape("q", "ROUND_RECTANGLE", 120, 60, transform(300, 60)),
                    shape("e", "ELLIPSE", 120, 80, transform(300, 160)))


def test_pull_reads_the_words_of_a_slidepar_and_nothing_else():
    latex = ("\\setslidepar{style=body}\n"
             "\\setslidelist{itemize}{1}{style=large,indent=22.68,mark=dot-red,gap=12.25}\n"
             "\\begin{slidebox}[middle,style=body-bold]{29.4,50.4,369.57,157.5}\n"
             "  \\slidepar[indent=22.68]{Lead}\n"
             "  \\begin{itemize}[labelstyle=label-red]\n"
             "    \\item[label={\\arabic*.},space=1.04] Goals\n"
             "    \\item Confidentiality\\textmd{: read}\\slidebreak more\n"
             "  \\end{itemize}\n"
             "\\end{slidebox}\n"
             "\\sliderect[fill=Blue]{10,10,20,20}\n"
             "\\slidetext[center]{67.2,63,243.57,37.8}{heading-blue}{Slide 2}\n")
    vis = build_visible(latex, 0, len(latex))
    words = vis.text.split()
    assert words == ["Lead", "Goals", "Confidentiality:", "read", "more", "Slide", "2"], words


# ---------------------------------------------------------------- compiled: short form = long form

def long_form(frame: str) -> str:
    """`frame` with every `\\slidetext` spelled out as a slidebox of one `\\slidepar` and every
    `\\sliderect` / `\\slideellipse` as the `\\slideshape` path they stand for."""
    box_keys = ("top", "middle", "bottom", "inset", "tail")

    def group(s, i):
        depth = 0
        for j in range(i, len(s)):
            depth += {"{": 1, "}": -1}.get(s[j], 0)
            if depth == 0:
                return s[i + 1:j], j + 1
        raise ValueError(s[i:])

    out, i = [], 0
    while True:
        m = re.compile(r"\\slide(text|rect|ellipse)(?:\[([^\]]*)\])?\{").search(frame, i)
        if not m:
            return "".join(out) + frame[i:]
        out.append(frame[i:m.start()])
        opts = [o for o in (m[2] or "").split(",") if o]
        geometry, j = group(frame, m.end() - 1)
        if m[1] == "text":
            style, j = group(frame, j)
            words, j = group(frame, j)
            bo = [o for o in opts if o.split("=")[0] in box_keys]
            po = [o for o in opts if o.split("=")[0] not in box_keys]
            out.append(f"\\begin{{slidebox}}[{','.join(bo)}]{{{geometry}}}\\slidepar[{','.join([f'style={style}'] + po)}]"
                       f"{{{words}}}\\end{{slidebox}}")
        elif m[1] == "rect":
            x, y, w, h = geometry.split(",")
            out.append(f"\\slideshape{{{geometry}}}{{\\path[{','.join(opts)}] (0bp,0bp) -- ({w}bp,0bp) -- "
                       f"({w}bp,-{h}bp) -- (0bp,-{h}bp) -- cycle;}}")
        else:
            x, y, rx, ry = geometry.split(",")
            out.append(f"\\slideshape{{{x},{y},{2 * float(rx):g},{2 * float(ry):g}}}{{\\path[{','.join(opts)}] "
                       f"({rx}bp,-{ry}bp) ellipse [x radius={rx}bp, y radius={ry}bp];}}")
        i = j


def lualatex():
    return shutil.which("lualatex", path=tex_env()["PATH"])


@pytest.mark.skipif(not lualatex(), reason="lualatex not found")
def test_the_short_forms_draw_what_their_long_forms_draw(tmp_path):
    text, _ = written(tmp_path)
    start, end = text.index("\\begin{frame}"), text.index("\\end{frame}") + len("\\end{frame}")
    frame = text[start:end]
    longer = long_form(frame)
    assert "\\slidetext" not in longer and "\\sliderect" not in longer and "\\slideellipse" not in longer
    main = tmp_path / "tree" / "main.tex"
    main.write_text(text[:end] + "\n" + longer + text[end:], encoding="utf-8")
    r = subprocess.run([lualatex(), "-interaction=nonstopmode", "-halt-on-error", "main.tex"], cwd=main.parent,
                       capture_output=True, text=True, errors="replace", env=tex_env(), timeout=300)
    assert r.returncode == 0, r.stdout[-3000:]
    from beamer2slides import pdf
    doc = pdf.Document(main.with_suffix(".pdf"))
    assert len(doc) == 2
    short, spelled = (np.asarray(doc[k].render(3.0)) for k in (0, 1))
    assert (short < 250).any(), "the page has ink"
    assert short.shape == spelled.shape and (short == spelled).all()


# the same box twice: its lists and defaults as adopt writes them, then every paragraph spelled out as
# a `\slidepar` saying all its keys, with its bullet drawn in its words as the older form did
LISTS_SHORT = r"""
\begin{frame}[plain]
  \begin{slidebox}[style=small,space=2]{42,20,180,300}
    \slidepar{Lead paragraph}
    \begin{itemize}[gap=0.91]
      \item First item, long enough to wrap onto a second line of the box it is set in
      \begin{itemize}
        \item Deeper one
        \item[mark=dot-red] Deeper two
      \end{itemize}
      \item[style=tiny,space=1] {}[Back] out
    \end{itemize}
    \slidepar[style=body,center]{Between}
    \begin{enumerate}[start=3]
      \item Third
      \item[label={x)}] Odd
      \item Fifth
    \end{enumerate}
  \end{slidebox}
\end{frame}
"""
LISTS_LONG = r"""
\begin{frame}[plain]
  \begin{slidebox}{42,20,180,300}
    \slidepar[style=small,space=2]{Lead paragraph}
    \slidepar[style=body,indent=20,space=2]{\slidebullet{dot-red}{0.91}First item, long enough to wrap onto a second line of the box it is set in}
    \slidepar[style=tiny,indent=40,first=3,space=2]{\slidebullet{ring-red}{0.7}Deeper one}
    \slidepar[style=tiny,indent=40,first=3,space=2]{\slidebullet{dot-red}{0.7}Deeper two}
    \slidepar[style=tiny,indent=20,space=1]{\slidebullet{dot-red}{0.91}[Back] out}
    \slidepar[style=body,center,space=2]{Between}
    \slidepar[style=small,indent=20,space=2]{\slidelabel{tiny}{3.}{2}Third}
    \slidepar[style=small,indent=20,space=2]{\slidelabel{tiny}{x)}{2}Odd}
    \slidepar[style=small,indent=20,space=2]{\slidelabel{tiny}{5.}{2}Fifth}
  \end{slidebox}
\end{frame}
"""
LEVELS = r"""
\setslidepar{style=tiny}
\setslidelist{itemize}{1}{style=body,indent=20,mark=dot-red,gap=1}
\setslidelist{itemize}{2}{style=tiny,indent=40,first=3,mark=ring-red,gap=0.7}
\setslidelist{enumerate}{1}{style=small,indent=20,labelstyle=tiny,label={\arabic*.},gap=2}
"""


@pytest.mark.skipif(not lualatex(), reason="lualatex not found")
def test_lists_and_defaults_draw_what_each_paragraph_spelled_out_draws(tmp_path):
    """Nested itemize, an enumerate starting at 3 with one typed label, a paragraph between lists,
    box defaults (style, space), list options, level defaults (`\\setslidelist`) and the deck's
    (`\\setslidepar`) against the same paragraphs each saying everything: pixel for pixel."""
    text, _ = written(tmp_path)
    head = text[:text.index("\\begin{document}")]
    main = tmp_path / "tree" / "main.tex"
    main.write_text(head + LEVELS + "\\begin{document}\n" + LISTS_SHORT + LISTS_LONG + "\\end{document}\n",
                    encoding="utf-8")
    r = subprocess.run([lualatex(), "-interaction=nonstopmode", "-halt-on-error", "main.tex"], cwd=main.parent,
                       capture_output=True, text=True, errors="replace", env=tex_env(), timeout=300)
    assert r.returncode == 0, r.stdout[-3000:]
    from beamer2slides import pdf
    doc = pdf.Document(main.with_suffix(".pdf"))
    assert len(doc) == 2
    short, spelled = (np.asarray(doc[k].render(3.0)) for k in (0, 1))
    assert (short < 250).any(), "the page has ink"
    assert short.shape == spelled.shape and (short == spelled).all()


def test_an_enumerate_counts_and_types_only_the_numbers_it_cannot_count():
    assert adopt.number_format("3.") == ("\\arabic*.", 3)
    assert adopt.number_format("B)") == ("\\Alph*)", 2)
    assert adopt.number_format("iv.") == ("\\roman*.", 4)
    assert adopt.number_format("01.") is None and adopt.number_format("•") is None
    bullet = lambda n: {"text": n, "size": 10.0, "kind": "number"}
    el = T.prose(*({"runs": [T.words(w)], "bullet": bullet(n), "slides": {"indent_start": 18}}
                   for n, w in (("2.", "Two"), ("3.", "Three"), ("7.", "Seven"))))
    ctx = adopt.Context()
    tex = adopt.text_box_latex(el, ctx, "")
    assert "\\begin{enumerate}[start=2]" in tex, tex
    assert "\\item Two" in tex and "\\item Three" in tex and "\\item[label={7.}] Seven" in tex, tex
    level, = (ln for ln in adopt.level_definitions(ctx) if ln.startswith("\\setslidelist{enumerate}{1}"))
    assert "label={\\arabic*.}" in level, "the level counts"
