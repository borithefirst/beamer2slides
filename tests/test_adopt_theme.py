"""A foreign deck's theme, recovered as a beamer theme (adopt_theme.py).

Offline, no TeX and no Google. `plan` is fed a hand-built IR and the LaTeX each element was written
as, standing in for `element_latex`, so what it decides - which slides take their layout, which keep
their elements, which titles move into the template - can be read off directly. The last tests go
through `deck_ir` and `adopt.bootstrap` on the fixture of test_adopt.py.
"""

from beamer2slides import adopt, adopt_theme
from beamer2slides.adopt_theme import plan, slot_parts, slug, theme_name
from beamer2slides.deck_ir import deck_ir

from .test_adopt import presentation, text_shape


def words_of(el: dict) -> str:
    return "".join(r["text"] for p in el.get("paragraphs") or [] for r in p["runs"])


def render(el: dict) -> str:
    """What `element_latex` would write: the words between a box's opening and closing lines."""
    return f"  \\begin{{textblock*}}{{{el['bbox'][0]}}}\n    {words_of(el)}\n  \\end{{textblock*}}"


def title(words: str, box=(10, 10, 200, 40), kind="TITLE") -> dict:
    return {"kind": "text", "placeholder": kind, "bbox": list(box), "paragraphs": [{"runs": [{"text": words}]}]}


def deco(owner: str, name: str, box=(0, 0, 450, 20)) -> dict:
    return {"kind": "shape", "inherited": owner, "bbox": list(box), "name": name}


def own(name: str, box=(300, 150, 400, 200)) -> dict:
    return {"kind": "shape", "bbox": list(box), "name": name}


def target(slides: list[dict]) -> dict:
    return {"source": {"title": "My talk"}, "slides": slides, "layouts": {
        "L": {"name": "Title and body", "master": "M", "background_color": None, "background_picture": None},
        "M": {"name": "Simple", "master": None, "background_color": None, "background_picture": None}}}


def latex(el: dict) -> str:
    return render(el) if el["kind"] == "text" else f"  % {el['name']}"


def planned(slides: list[dict], deck_bg=None):
    t = target(slides)
    pieces = [[latex(e) for e in s["elements"]] for s in slides]
    return plan(t, pieces, render, lambda c: "c" + c.strip("#"), lambda f: f"figures/{f}", deck_bg)


def slide(*elements, layout="L", **extra) -> dict:
    return {"layout": layout, "elements": list(elements), **extra}


# ---------------------------------------------------------------- names

def test_a_layout_is_named_by_its_display_name():
    taken: set = set()
    assert slug("Title and body", taken) == "title-and-body"
    assert slug("Title and body", taken) == "title-and-body-2", "two layouts may share a display name"
    assert slug("Caption - Optional", taken) == "caption-optional"
    assert slug("  ", taken) == "layout"
    assert slug("Titre_et contenu", taken) == "titre-et-contenu"


def test_the_theme_is_named_after_the_deck_and_shadows_no_beamer_theme():
    assert theme_name({"source": {"title": "CS 161 SP25 - Lecture 17"}}) == "CS161SP25Lecture17"
    assert theme_name({"source": {"title": "2024 talk"}}) == "Deck2024Talk"
    assert theme_name({"source": {"title": "Madrid"}}) == "MadridDeck"
    assert theme_name({"source": {}}) == "Deck"


# ---------------------------------------------------------------- the layout's decoration

def test_slides_on_one_layout_name_it_and_leave_its_decoration_to_the_theme():
    slides = [slide(deco("M", "bar"), deco("L", "logo"), own(f"card {n}")) for n in range(3)]
    sty, plans = planned(slides)
    assert [p.layout for p in plans] == ["title-and-body"] * 3
    assert all(p.drawn == {0, 1} for p in plans), "the frame keeps only its own element"
    assert "\\defbeamertemplate{background}{title-and-body}{\\layoutdecoration{%\n  % bar\n  % logo" in sty
    assert sty.count("% bar") == 1 and "% card" not in sty
    assert plans[0].options() == "[plain,layout=title-and-body]"


def test_a_slide_that_draws_its_layout_differently_keeps_its_elements():
    """The template is the LaTeX the elements were written as, so a slide whose inherited pieces came
    out otherwise (a fill read off its own thumbnail) is not given a template that would change it."""
    slides = [slide(deco("M", "bar"), deco("L", "logo")) for _ in range(3)]
    slides.append(slide(deco("M", "bar"), deco("L", "logo, read differently")))
    _sty, plans = planned(slides)
    assert [p.layout for p in plans] == ["title-and-body"] * 3 + [None]
    assert plans[3].drawn == set() and plans[3].options() == "[plain]"


def test_a_master_several_layouts_draw_is_said_once():
    slides = [slide(deco("M", "bar"), deco("L", "logo")), slide(deco("M", "bar"), deco("L2", "stripe"), layout="L2")]
    t = target(slides)
    t["layouts"]["L2"] = {"name": "Section header", "master": "M", "background_color": None,
                          "background_picture": None}
    pieces = [[latex(e) for e in s["elements"]] for s in slides]
    sty, plans = plan(t, pieces, render, str, str, None)
    assert [p.layout for p in plans] == ["title-and-body", "section-header"]
    assert sty.count("% bar") == 1 and sty.count("\\drawmaster{simple}") == 2
    # a second master drawing the same (a deck pasted into another brings its master along)
    for s in slides[1:]:
        s["elements"][0]["inherited"] = "M2"
    t["layouts"]["L2"]["master"] = "M2"
    t["layouts"]["M2"] = {**t["layouts"]["M"], "name": "Simple copy"}
    sty, _plans = plan(t, pieces, render, str, str, None)
    assert sty.count("% bar") == 1 and sty.count("\\drawmaster{simple}") == 2


def test_a_piece_with_a_parameter_sign_is_never_put_in_a_template():
    """Inside `\\defbeamertemplate` a `#` would be read as the template's parameter."""
    slides = [slide(deco("L", "fill #1")) for _ in range(2)]
    sty, plans = planned(slides)
    assert [p.layout for p in plans] == [None, None] and "fill #1" not in sty


def test_a_slide_with_no_layout_is_left_alone():
    assert plan({"slides": [slide(own("x"))]}, [["x"]], render, str, str, None) is None, \
        "an IR written before deck_ir recorded layouts"
    _sty, plans = planned([slide(own("x"), layout=None)])
    assert plans[0].layout is None and plans[0].options() == "[plain]"


# ---------------------------------------------------------------- the page under it

def test_the_layouts_page_comes_with_it_and_a_slide_of_its_own_says_so():
    slides = [slide(deco("L", "logo"), background_color="#112233") for _ in range(2)]
    slides.append(slide(deco("L", "logo"), background_color="#ffffff"))
    slides.append(slide(deco("L", "logo"), background_file="bars.png"))
    t = target(slides)
    t["layouts"]["L"]["background_color"] = "#112233"
    pieces = [[latex(e) for e in s["elements"]] for s in slides]
    sty, plans = plan(t, pieces, render, lambda c: "c" + c.strip("#"), lambda f: f"figures/{f}", "#ffffff")
    assert "\\layoutcanvas{title-and-body}{\\setbeamercolor{background canvas}{bg=c112233}}" in sty
    assert plans[0].background is None and plans[0].backdrop is None
    assert plans[2].background == "deckbg", "the deck's own page, under a layout that has another"
    assert plans[3].backdrop == "figures/bars.png"
    assert plans[3].options() == "[plain,layout=title-and-body,backdrop=figures/bars.png]"


# ---------------------------------------------------------------- title, subtitle, number

def test_slot_parts_split_a_placeholder_into_its_frame_and_its_words():
    el = title("Last time: CAPTCHAs")
    prefix, suffix, words = slot_parts(el, render(el), render)
    assert words == "Last time: CAPTCHAs" and prefix + words + suffix == render(el)
    assert slot_parts(title("50% off"), render(title("50% off")), render) is None, "a comment sign"
    two = {**el, "paragraphs": [{"runs": [{"text": "a"}]}, {"runs": [{"text": "b"}]}]}
    assert slot_parts(two, render(two).replace("ab", "a\nb"), lambda e: render(e).replace(
        "Qqzxbeginslotqa", "Qqzxbeginslotqa\n")) is None, "words over several lines stay in the frame"


def test_a_title_moves_into_the_template_and_the_frame_says_frametitle():
    slides = [slide(deco("L", "logo"), title(f"Point {n}"), own("card")) for n in range(3)]
    sty, plans = planned(slides)
    assert [p.header() for p in plans] == [[f"  \\frametitle{{Point {n}}}"] for n in range(3)]
    assert all(p.drawn == {0, 1} for p in plans)
    assert "\\withtitle{%\n  \\begin{textblock*}{10}\n    \\layouttitle{}\n  \\end{textblock*}}" in sty


def test_a_title_stays_in_the_frame_when_something_under_it_touches_it():
    """The template draws under everything the frame draws: a title the slide drew over its own
    picture would end up under that picture."""
    slides = [slide(deco("L", "logo"), own("photo", box=(0, 0, 450, 250)), title("Over a photo")),
              slide(deco("L", "logo"), own("aside", box=(300, 150, 400, 200)), title("Beside a card"))]
    _sty, plans = planned(slides)
    assert plans[0].header() == [] and 2 not in plans[0].drawn
    assert plans[1].header() == ["  \\frametitle{Beside a card}"] and 2 in plans[1].drawn


def test_a_title_written_differently_stays_in_the_frame():
    slides = [slide(title(f"T{n}")) for n in range(3)]
    t = target(slides)
    pieces = [[render(e) for e in s["elements"]] for s in slides]
    pieces[2][0] = pieces[2][0].replace("textblock*}{10}", "textblock*}{11}")    # moved by a point
    _sty, plans = plan(t, pieces, render, str, str, None)
    assert [bool(p.header()) for p in plans] == [True, True, False]


def test_the_slide_number_is_the_frame_number():
    number = lambda n: title(str(n), box=(420, 240, 440, 250), kind="SLIDE_NUMBER")
    slides = [slide(deco("L", "logo"), number(1)), slide(deco("L", "logo"), number(2)),
              slide(deco("L", "logo"), number(7)), slide(deco("L", "logo"))]
    sty, plans = planned(slides)
    assert "\\withnumber{%\n  \\begin{textblock*}{420}\n    \\insertframenumber{}\n" in sty
    assert [1 in p.drawn for p in plans[:3]] == [True, True, False], "a number that is not the page's"
    assert [p.nonumber for p in plans] == [False, False, True, True]
    assert plans[3].options() == "[plain,layout=title-and-body,nonumber]"


# ---------------------------------------------------------------- through deck_ir and bootstrap

def test_deck_ir_records_each_slides_layout_and_the_layouts_names():
    ir = deck_ir(presentation(), foreign=True)
    assert [s["layout"] for s in ir["slides"]] == ["L1"] * 3
    assert ir["layouts"]["L1"]["name"] == "Section" and ir["layouts"]["L1"]["master"] == "m1"
    assert "m1" in ir["layouts"]
    assert "layouts" not in deck_ir(presentation()), "pull reads no theme"


def test_bootstrap_writes_the_theme_beside_the_source_and_uses_it(tmp_path):
    pres = presentation()
    pres["slides"][2]["pageElements"].append(text_shape("s2_x", "own words", 60, 300, 300, 40))
    text = adopt.bootstrap(deck_ir(pres, foreign=True), tmp_path / "tree" / "main.tex")
    sty = tmp_path / "tree" / "beamerthemeTemplate.sty"
    assert sty.exists() and "\\usetheme{Template}" in text
    assert text.index("\\usetheme{Template}") < text.index("\\begin{document}")
    theme = sty.read_text(encoding="utf-8")
    assert "\\defbeamertemplate{background}{section}" in theme and theme.rstrip().endswith("\\mode<all>")
    frame = text[text.index("% slide 3"):]
    assert "own words" in frame and frame.count("\\begin{textblock*}") == 1


def test_a_flow_source_has_no_theme(tmp_path):
    text = adopt.bootstrap(deck_ir(presentation(), foreign=True), tmp_path / "tree" / "main.tex", flow=True)
    assert "\\usetheme{Template}" not in text and not list((tmp_path / "tree").glob("*.sty"))


def test_the_theme_resets_what_a_frame_asked_for():
    """beamer's frame options are set globally and last until something sets them again; the next
    frame must start from the deck's own page, with no layout and its number shown."""
    assert "\\AddToHook{env/frame/before}{\\setbeamertemplate{background}{}\\deck@nonumberfalse" \
        in adopt_theme.PRELUDE
