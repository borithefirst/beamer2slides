"""Slides' measures as emit uses them: calibrated font substitutes and their advances (FontMapper),
a text box's line geometry, and bullets.
"""

import json
import re
import unicodedata
from collections.abc import Mapping
from importlib import resources
from typing import Literal, TypedDict

from .emit_model import BulletFace, JsonMap, SetBullet, SetRun, bullet_of, run_of
from .fonts import METRICS_FAMILIES, MetricsFamily, font_info, google_font, metrics_family, written_face
from .google_types import SlidesTextStyle
from .gslides import pt_json as pt
from .json_types import Json, JsonObject, JsonShapeError, as_object
from .mono_edges import EDGE_SPACE, EDGE_SPACE_EM


# Found through the package, never through the checkout: an installed wheel, a zip import and
# a build that stages sources elsewhere all keep the data beside the module, not beside __file__.
CALIBRATION_DIR = resources.files("beamer2slides") / "calibration"
CALIBRATION = CALIBRATION_DIR / "fonts.json"
SLIDE_W = 720.0
# Slides text box model, measured by tools/calibrate.py and the spacing probes
# (docs/calibration.md).
BASELINE_A = 6.48    # box top -> first baseline = A + ASCENT_EM * size
ASCENT_EM = 0.968
LINE_EM = 1.2        # baseline pitch at lineSpacing 100 (before pixel snapping)
PX_PT = 0.75         # Slides snaps line pitches to whole CSS pixels
DESCENT_EM = LINE_EM - ASCENT_EM
PAD_X = 6.7          # box edge -> text start
BULLET_GAP = 1.9     # bullet glyph's right edge sits this far before indentFirstLine
PPTX_TITLE_DY = 3.9  # title placeholders of pptx-imported decks have a smaller top inset
MIDDLE_BASELINE_EM = ASCENT_EM - LINE_EM / 2  # contentAlignment MIDDLE: baseline below the box middle (tools/probe_middle.py: 0.362)
SOFT_BREAK = chr(11)  # vertical tab: a line break inside a paragraph

FONT_FOR_FAMILY = {"sans": "Lato", "serif": "PT Serif", "mono": "Roboto Mono"}
# Advance widths in Slides text (Lato and its fallback fonts), measured by tools/probe_symbols.py.
SYMBOL_ADVANCE_EM = {
    "=": 0.58, "+": 0.58, "−": 0.58, "<": 0.58, ">": 0.58, "≤": 0.58, "≥": 0.58, "×": 0.58, "·": 0.271,
    "/": 0.313, "∑": 0.682, "∏": 0.682, "∫": 0.397, "∈": 0.984, "∉": 0.492, "⊂": 0.981, "⊆": 0.981,
    "∪": 0.981, "∩": 0.717, "→": 0.998, "←": 0.998, "⇒": 0.981, "⇔": 0.981, "≈": 0.577, "≠": 0.577,
    "±": 0.577, "∞": 0.682, "ℝ": 0.633, "ℕ": 0.65, "ℤ": 0.534, "ℚ": 0.65, "ℂ": 0.65, "α": 0.573,
    "β": 0.57, "γ": 0.496, "δ": 0.552, "ε": 0.443, "θ": 0.552, "λ": 0.496, "μ": 0.573, "π": 0.615,
    "σ": 0.612, "φ": 0.643, "ω": 0.777, "Δ": 0.664, "Σ": 0.615, "Ω": 0.742, "∂": 0.577, "∇": 0.981,
    "′": 0.186, "∀": 0.981, "∃": 0.981, "∧": 0.981, "∨": 0.981, "⊥": 0.981, "∥": 0.981, "∘": 0.489,
    "…": 0.724, " ": 0.19,
    "\u2008": 0.278,  # (the punctuation space a relation's thick space is written as: classify_text.THICK_SPACE)
    EDGE_SPACE: EDGE_SPACE_EM,  # (the six-per-em space at inline code's edges: mono_edges, tools/probe_mono_edges.py)
}
MATH_SPACE_EM = 0.278  # TeX's \thickmuskip (5 mu) around relations
CMTT_ADVANCE_EM, ROBOTO_MONO_ADVANCE_EM = 0.525, 0.6

BULLET_PRESETS = {
    "number": "NUMBERED_DIGIT_ALPHA_ROMAN",
    "number_parens": "NUMBERED_DIGIT_ALPHA_ROMAN_PARENS",
}
# A preset's three glyphs are those of nesting levels 0, 1, 2; deeper levels repeat ● ○ ■
# whatever the preset. A bullet shape is a preset plus the level showing it (bullet_level).
# Ink height and the gap between ink and indentFirstLine are per em of the bullet's size
# (tools/probe_bullets.py).
BULLET_SHAPES: dict[str, tuple[str, int, float, float]] = {  # shape: (preset, level, ink height em, gap em)
    "disc": ("BULLET_DISC_CIRCLE_SQUARE", 0, 0.413, 0.08),
    "circle": ("BULLET_DISC_CIRCLE_SQUARE", 1, 0.43, 0.08),
    "square": ("BULLET_DISC_CIRCLE_SQUARE", 2, 0.45, 0.07),
    "open_square": ("BULLET_CHECKBOX", 0, 0.69, 0.155),  # ❏
    "triangle": ("BULLET_ARROW3D_CIRCLE_SQUARE", 0, 0.525, 0.07),  # ➢: presets have no ▶
    "star": ("BULLET_STAR_CIRCLE_SQUARE", 0, 0.81, 0.07),
    "diamond": ("BULLET_DIAMOND_CIRCLE_SQUARE", 0, 0.81, 0.08),
    "open_diamond": ("BULLET_DIAMONDX_HOLLOWDIAMOND_SQUARE", 1, 0.87, 0.06),
}
# A bullet no preset draws, written as its own character instead (`a:buChar` in a text shell the
# .pptx carries, emit_pptx._add_text_shell; docs/project-notes.md "Triangle bullets through the
# .pptx"): (character, ink height em, gap em) as BULLET_SHAPES has them. Beamer's filled ▶ at
# every level, sized by the PDF's own ink as the vector bullets are (beamer draws its item and
# subitem with one \blacktriangleright at two sizes; ▸ is another shape). It is written ►, the
# same shape: Slides draws ▶ in a fallback face only 0.45 em tall, so beamer's (0.58 em) would
# need a bullet larger than its text, which pushes the line down (+0.9 pt at 133%); ► is 0.68 em
# in every face tried (Lato, Arial; Noto Sans Symbols 2's ▶ is 0.56), its bottom 0.02 em under
# the baseline (beamer's 0.04), its right edge 0.28 em before indentFirstLine (0.21 on a probe
# box, 0.075 em more on all 97 bullets of a converted deck, whose words started where the PDF's
# do; docs/project-notes.md "Triangle bullets").
CHAR_BULLETS: dict[BulletFace, tuple[str, float, float]] = {"triangle": ("►", 0.68, 0.28)}
# The glyph each preset draws a face as (BULLET_SHAPES, tools/probe_bullets.py). A text shell that
# holds a bullet no preset draws (CHAR_BULLETS) writes its other bullets as these characters
# (`a:buChar` in the run's face, as createParagraphBullets' bullet is drawn in its paragraph's), so
# a box mixing beamer's ▶ with discs or balls (its subitems', or its items' under ▶ subitems)
# keeps its ▶ and draws the rest as the preset would, at the preset's measures
# (emit_text.shell_route; tools/probe_shell_bullets.py compares the two live).
PRESET_CHARS: dict[BulletFace, str] = {"disc": "●", "circle": "○", "square": "■", "open_square": "❏", "triangle": "➢",
                                       "star": "★", "diamond": "◆", "open_diamond": "◇"}
_GLYPH_FACES: tuple[tuple[str, BulletFace], ...] = (
    ("▶►▸‣", "triangle"), ("•●", "disc"), ("◦○", "circle"), ("■▪", "square"), ("□", "open_square"), ("★⋆", "star"),
    ("◆♦", "diamond"), ("◇⋄", "open_diamond"))
GLYPH_SHAPES: dict[str, BulletFace] = {ch: face for chars, face in _GLYPH_FACES for ch in chars}


# Width per em of Computer Modern's optical sizes relative to the 10 pt cut, from the glyph
# advances of the Type 1 fonts (cmss8.pfb ... cmss17.pfb) over a sample sentence. EC and
# Latin Modern share these metrics. CM Sans has no cut below 8 pt, EC does (cm-super's
# sfss0500-sfss0700, beamer's \tiny footlines): without them a 6 pt footline was taken for the
# 8 pt cut and came out ~10% narrower than the PDF's.
DESIGN_WIDTH = {
    "sans": {5: 1.2814, 6: 1.1733, 7: 1.1073, 8: 1.0623, 9: 1.0273, 10: 1.0, 12: 0.9753, 17: 0.9377},
    "serif": {5: 1.3758, 6: 1.2291, 7: 1.1424, 8: 1.0629, 9: 1.0277, 10: 1.0, 12: 0.9786, 17: 0.9136},
    "mono": {8: 1.0114, 9: 1.0, 10: 1.0, 12: 0.979},
}
# A small optical cut is wider per em than the 10 pt one, not taller (cmr5's x-height is cmr10's
# per em), so a substitute sized to its width grows its letters as much: a 5 pt LMRoman5 footline
# (1.38x) came out 1.4 times as tall as the PDF's and filled its bar. The width is matched up to
# the small-caps compromise (SMALL_CAPS_WIDTH) and no further: at 5 pt the line comes out ~18%
# narrower than the PDF's and ~13% taller. 8 and 9 pt cuts (1.03-1.06) are matched in full.
OPTICAL_WIDTH_MAX = 1.13
# A small optical cut is heavier per em as well: the stem of CM Sans's l and I, per em, relative to
# its 10.95 pt cut (cm-super's sfss*.pfb) is 1.99 at 5 pt, 1.38-1.52 at 6 pt, 1.17 at 7 pt, 1.10 at
# 8 pt, 1.06 at 9 pt and 0.80 at 14.4 pt; Lato Bold's is 1.38-1.39 times its Regular's. A sans cut
# nearer the Bold than the Regular (6 pt and less: beamer's \tiny footlines, frame counters)
# was set heavier than regular (wave 4: Lato 800, which Slides draws as its Bold). Measured on
# the r8 (Lato 600, drawn Regular) and r9 (800) renders against the PDF's, stroke width as 2 x ink
# area / ink perimeter at 1600 px over eight footline boxes of control_a2, control_c1 and sci_v1:
# PDF 1.85 px, Lato Regular 1.75 (-6%), Lato Bold 2.35 (+27%); body text (CMSS 10.95 against
# Lato Regular) 0.92 of Lato's. Bold was too heavy on every slide, and a \tiny reference block
# lost the contrast with its bold volume numbers (sci_v1 s8). No served medium lands between:
# Slides draws Lato 500/600 as Regular, and Source Sans 3 600, width-matched, is +31% on Lato
# Regular's stroke (probe_font_weights: 4.52 px at 24 pt against Lato's 3.58, 4% narrower) - as
# heavy as the Bold. So the cut is drawn Regular, as the nearest face. It is written at 600 all
# the same: Slides draws that weight as Regular and reads it back as written (`bold: false`,
# weight 600), which the converter writes nowhere else - deck_ir takes it for a 6 pt sans cut,
# whose width it inverts as such (OPTICAL_WIDTH_MAX; as CM's 8 pt cut it came back 6% large).
# Written without a `bold` field: the API applies `bold` after the weight, and a `bold: false`
# could take it back to 400.
OPTICAL_WEIGHT_DESIGN = {"sans": 6.0}
OPTICAL_WEIGHT = 600
# Weights read back as a small sans cut, not bold: 600 (drawn Regular), and 800 (drawn Bold),
# which the decks converted between wave 4 and wave 5 wrote.
OPTICAL_WEIGHTS_READ = (OPTICAL_WEIGHT, 800)
# The weights Slides draws with Lato's Bold face (probe_font_weights: 500 and 600 are Regular).
DRAWN_BOLD_WEIGHT = 700


# EC's bold extended sans (cm-super's SFSX, SFSO: beamer's bold sans under T1) is the other way
# round: wider per em above 10 pt, not narrower, and only a little wider below it. Width of the
# calibration sentences per em relative to the 10 pt cut, from the ecsx*.tfm advances (10.95,
# 14.4, 17.28 and 20.74 pt keyed 11, 14, 17, 21). Taken for CM Sans's regular widths, a 12 pt
# SFSX1200 frame title came out 8% narrower than the PDF's, a 14.4 pt title page 11% (lecture-
# phylogenetics, esi-dev1). CM has its bold extended sans at 10 pt only (CMSSBX10), as Latin Modern.
BOLD_SANS_DESIGN_WIDTH = {5: 1.0883, 6: 1.042, 7: 1.0226, 8: 1.0321, 9: 0.9577, 10: 1.0, 11: 1.0017, 12: 1.0239,
                          14: 1.046, 17: 1.0547, 21: 1.067}


def optical_width(family: str, design: float) -> float:
    """How much wider per em than its 10 pt cut the size factor takes a run's optical size to be."""
    return min(OPTICAL_WIDTH_MAX, design_width(DESIGN_WIDTH.get(family, DESIGN_WIDTH["sans"]), design))


def face_optical_width(family: str, bold: bool, design: float) -> float:
    """`optical_width` of a face: a bold sans cut's are its own (BOLD_SANS_DESIGN_WIDTH)."""
    if family == "sans" and bold:
        return min(OPTICAL_WIDTH_MAX, design_width(BOLD_SANS_DESIGN_WIDTH, design))
    return optical_width(family, design)


def u16(text: str) -> int:
    """Length in UTF-16 code units, which is what every Slides text index counts: an astral
    character (𝔼 U+1D53C from amssymb's \\mathbb, 𝛽 U+1D6FD) is two. Counting code points put
    every style range after one a unit early and split the next one's surrogate pair (two tofu)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def letter_faces(text: str, font: str, start: int) -> list[tuple[int, int, str]]:
    """(start, end, face) of each stretch of a run's `text` written in a face of its own (script
    capitals, fonts.letter_face of the run's PDF `font`; a math font's operators Slides would draw
    from its fallback, fonts.OPERATOR_FACES: fonts.written_face), in UTF-16 units from `start`
    (where the run's text starts)."""
    out: list[tuple[int, int, str]] = []
    at = start
    for c in text:
        n = u16(c)
        face = written_face(c, font)
        if face is not None:
            if out and out[-1][1] == at and out[-1][2] == face:
                out[-1] = (out[-1][0], at + n, face)
            else:
                out.append((at, at + n, face))
        at += n
    return out


def letter_face_style(face: str, bold: bool) -> SlidesTextStyle:
    """The style written over a run's `letter_faces`, `fields` "weightedFontFamily" (its weight
    the run's: a fontFamily alone would set it regular)."""
    return {"weightedFontFamily": {"fontFamily": face, "weight": 700 if bold else 400}}


# XML 1.0 refuses C0 controls other than tab, newline and return, lone surrogates and U+FFFE/F:
# a Type 3 T1 font's raw glyph codes (quotes, dashes, ligatures at 0x10-0x1d) reach alt texts.
XML_REFUSED = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_text(text: str) -> str:
    """A string lxml accepts as an attribute value (build_pptx): refused characters dropped."""
    return XML_REFUSED.sub("", text)


# Small caps (tools/probe_text_fit_fonts.py): Slides draws a smallCaps lowercase letter as its
# capital at 0.70 of the size (PT Serif and Lato alike, advance and ink height), where TeX's
# CMCSC10 draws it at 0.755 - and CMCSC is an extended face, its capitals 8% wider than CMR's,
# while PT Serif's are 11% narrower. A CMCSC line came out 0.777 of the PDF's width; at the
# serif size these substitute small caps are 1.27-1.30 times too narrow over any sentence
# (probe advances + CMCSC10.afm), so the size makes up the width, as the size factors do for
# every other face. (Only for Computer Modern / EC / Latin Modern small caps, the ones measured.)
# The full 1.28 made the capitals ~30% taller than CMCSC's; 1.13 (about the square root) is the
# chosen compromise: the line comes out ~12% narrower than the PDF's and ~13% taller.
SMALL_CAPS_WIDTH = {"serif": 1.13}

# Computer Modern's own advance widths, kerning pairs and interword spaces (em; the AFM and TFM
# files, tools/cm_advances.py; EC and Latin Modern share them), per 10 pt face. With Slides'
# advances (ADVANCES) they predict how wide a run comes out in Slides against the PDF - within
# 0.5% of Google's renderer on the calibration rows and the text-fit torture lines.
CM_ADVANCES = json.loads((CALIBRATION_DIR / "cm_advances.json").read_text(encoding="utf-8"))["fonts"]
CM_FACE = {("sans", False, False): "cmss10", ("sans", True, False): "cmssbx10", ("sans", False, True): "cmssi10",
           ("sans", True, True): "cmssbx10",  # beamer's bold italic sans is CMSSBX
           ("serif", False, False): "cmr10", ("serif", True, False): "cmbx10", ("serif", False, True): "cmti10",
           ("serif", True, True): "cmbxti10"}
CM_LIGATURES = (("ffi", "ﬃ"), ("ffl", "ﬄ"), ("ff", "ﬀ"), ("fi", "ﬁ"), ("fl", "ﬂ"))
# TeX's space factor codes (plain/LaTeX \nonfrenchspacing): a space after a factor of 2000 or
# more gets the font's extra space; a capital (999) keeps "A." from ending a sentence.
SPACE_FACTOR = {".": 3000, "?": 3000, "!": 3000, ":": 2000, ";": 1500, ",": 1250}
KEEPS_SPACE_FACTOR = ")]'’”"
# Numbers: Computer Modern's digits are 0.5 em where Slides' Lato draws tabular digits at
# 0.577 em, 13% wider after the size correction, which is calibrated on sentences (and they
# stand at cap height, 7% taller than CM's). A run that is only a number - a table cell, a
# number column, a frame counter - is set at the size that gives it the PDF's width
# (FontMapper.shape_ratio); a number inside a sentence keeps the sentence's size.
DIGITS = "0123456789"
NUMBER_CHARS = DIGITS + ",.:%/-()+"
# Any other run: the calibrated factor makes an average sentence as wide as the PDF's, and a
# text whose letters are unlike a sentence's is off by what its own advances say (serif capitals
# 0.87-0.92, a line of w 1.10). Short texts vary the most - across the 84 test PDFs, a word or a
# label of under 10 letters spreads over 0.92-1.14 - so only a run of at least SHAPE_MIN_CHARS
# counted characters whose width is predicted off by more than SHAPE_TOL is resized: no run of
# 15 characters or more in the test decks is (the widest ordinary one is 6.3% off, "Somewhere
# University"), and text_fit calls a line off at 8%. Ordinary prose keeps the deck-wide size.
SHAPE_MIN_CHARS = 15
SHAPE_TOL = 0.07
# The sentences the size factors were calibrated on (tools/calibrate.py WIDTH_ROWS): a run is
# judged against them in its own face, so bold and italic keep their half correction.
SHAPE_REFERENCE = ("The quick brown fox jumps over the lazy dog",
                   "Another first level item that is long enough to wrap",
                   "Lorem ipsum dolor sit amet, consectetur adipiscing elit")
SHAPE_TITLE_REFERENCE = ("Itemize, nested and frame titles",)  # the title factor's row (CMSS12)
# Dots set apart from words: \dotfill and \dots leaders, \ldots (". . ." in the text layer) and
# "...". TeX sets them as fixed boxes or thin spaces, not as sentence ends; read as periods with
# interword and sentence spaces they made a leader line look 30-40% too wide for Slides, and the
# run was set 1.3-1.7 times too large. Their width is neither the PDF's letters' nor a sentence's,
# so they count on neither side, with the spaces around them (advance_widths).
LEADER = re.compile(r"[   ]*\.(?:[   ]*\.)+[   ]*")
LEADER_ONLY = re.compile(r"\.(?:\s*\.){3,}")  # a run that is a leader alone (classify_lines.LEADER_RE)
# Slides' distance from one dot to the next of ". . ." (em), measured on Google's renderer
# (tools/probe_leaders.py, 25 dots at 20 pt; the hunt archives' thumbnails agreed:
# real_defense-defense 8 and 41, 0.442; r3_textfx_v2 2, 0.405). ADVANCES' "." and " " add up to
# 0.525-0.581 in PT Serif, so a leader sized by them came out 16% short: PT Serif kerns a period
# against a space by -0.033 to -0.042 em on either side (". " and " .", every face;
# tools/probe_period_kerning.py), twice per spaced dot, and its "." is 0.27-0.30 em, which rows of
# one character (probe_advances) never see. Lato kerns none: its sums hold within 1%. In a sentence
# the one kern after its period is 0.04 em, left out. Dots set otherwise apart are ADVANCES' sum.
LEADER_PITCH_EM: dict[tuple[str, str], float] = {
    ("PT Serif", "regular"): 0.442, ("PT Serif", "bold"): 0.4959,
    ("PT Serif", "italic"): 0.4334, ("PT Serif", "bold_italic"): 0.4891,
    ("Lato", "regular"): 0.405, ("Lato", "bold"): 0.4177,
    ("Lato", "italic"): 0.4059, ("Lato", "bold_italic"): 0.4181,
}
SPACED_DOTS = re.compile(r"(?:\. )+")
FaceStyle = Literal["regular", "bold", "italic", "bold_italic"]
FACE_STYLES: tuple[FaceStyle, ...] = ("regular", "bold", "italic", "bold_italic")
STYLE_KEY: dict[tuple[bool, bool], FaceStyle] = {(False, False): "regular", (True, False): "bold",
                                                 (False, True): "italic", (True, True): "bold_italic"}
SLANTED = re.compile(r"CMB?X?SL\d|SFSL\d|SFBL\d|LMROMANSLANT")  # slanted roman: upright widths


def cm_face(run: JsonMap) -> str | None:
    """`cm_face_of` a run dict."""
    return cm_face_of(run_of(run))


def cm_face_of(run: SetRun) -> str | None:
    """The CM_ADVANCES face a Computer Modern (EC, Latin Modern) run is set in, or None."""
    if font_info(run.font).design_size is None:
        return None
    slanted = bool(SLANTED.match(re.sub(r"[^A-Z0-9]", "", run.font.split("+", 1)[-1].upper())))
    return CM_FACE.get((run.family, run.bold, run.italic and not slanted))


class CmFace(TypedDict):
    """A CM_ADVANCES entry: one Computer Modern face's advances and kerning pairs (character or
    pair -> em) and its interword and extra (sentence) space."""
    advances: dict[str, float]
    kerns: dict[str, float]
    space: float
    extra_space: float


def _em(value: Json, where: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise JsonShapeError(f"{where}: a number was expected")


def _ems(value: Json, where: str) -> dict[str, float]:
    return {k: _em(v, f"{where}.{k}") for k, v in as_object(value, where).items()}


def parse_face(value: Json, where: str) -> CmFace:
    """A CM_ADVANCES-shaped face record read from JSON."""
    o = as_object(value, where)
    return CmFace(advances=_ems(o["advances"], f"{where}.advances"), kerns=_ems(o["kerns"], f"{where}.kerns"),
                  space=_em(o["space"], f"{where}.space"), extra_space=_em(o["extra_space"], f"{where}.extra_space"))


def parse_text_advances(value: Json, where: str) -> dict[MetricsFamily, dict[FaceStyle, CmFace]]:
    """calibration/text_advances.json: every family `fonts.metrics_family` names, with the styles
    the TeX tree has for it (Bera Serif has no italic)."""
    fonts = as_object(as_object(value, where)["fonts"], f"{where}.fonts")
    out: dict[MetricsFamily, dict[FaceStyle, CmFace]] = {}
    for family in METRICS_FAMILIES:
        if family not in fonts:
            raise JsonShapeError(f"{where}.fonts: no {family}")
        faces = as_object(fonts[family], f"{where}.fonts.{family}")
        out[family] = {style: parse_face(faces[style], f"{where}.fonts.{family}.{style}")
                       for style in FACE_STYLES if style in faces}
    return out


_TEXT_ADVANCES_JSON: Json = json.loads((CALIBRATION_DIR / "text_advances.json").read_text(encoding="utf-8"))
# The advances, kerns and spaces of TeX text faces other than Computer Modern (Linux Libertine,
# Biolinum, Bera, DejaVu, Palatino, Utopia, Inconsolata), from the TFM files TeX set them with
# (tools/text_font_advances.py). With Slides' advances (ADVANCES) they give such a face its own
# size factor (FontMapper.size_of), as CM_ADVANCES predict CM's within 0.5% of the measured one.
TEXT_ADVANCES = parse_text_advances(_TEXT_ADVANCES_JSON, "text_advances.json")


def text_face(font: str) -> tuple[str, CmFace] | None:
    """(key, TEXT_ADVANCES face) of a TeX text font that is not Computer Modern: the font's own
    style, else its upright one, else its family's regular face; None for any other font."""
    family = metrics_family(font)
    if family is None:
        return None
    faces = TEXT_ADVANCES[family]
    info = font_info(font)
    for style in (STYLE_KEY[(info.bold, info.italic)], STYLE_KEY[(info.bold, False)], "regular"):
        if style in faces:
            return f"{family}/{style}", faces[style]
    return None


def text_face_of(run: SetRun) -> tuple[str, CmFace] | None:
    """`text_face` of the font a run is set in."""
    return text_face(run.font)


def text_regular(font: str) -> tuple[str, CmFace] | None:
    """(key, face) of the regular face of a font's TEXT_ADVANCES family, which its size factor is
    made on (`FontMapper.text_ratios`)."""
    family = metrics_family(font)
    if family is None or "regular" not in TEXT_ADVANCES[family]:
        return None
    return f"{family}/regular", TEXT_ADVANCES[family]["regular"]


def mono_pitch(run: SetRun, design: float) -> float:
    """The advance (em of the run's size) a monospaced run's columns have in the PDF, which its
    Roboto Mono (ROBOTO_MONO_ADVANCE_EM) is sized to: a listing's grid pitch when the PDF set code
    on a column grid in a proportional face (`pitch`, classify's `span.grid`), a monospaced
    TEXT_ADVANCES face's one advance (Inconsolata's 0.5 em), else CMTT's at its optical size."""
    if run.pitch is not None and run.pitch > 0:
        return run.pitch
    face = text_face_of(run)
    pitch = None if face is None else monospaced_pitch(face[1])
    return pitch if pitch is not None else CMTT_ADVANCE_EM * design_width(DESIGN_WIDTH["mono"], design)


def monospaced_pitch(face: CmFace) -> float | None:
    """A monospaced face's one advance (em: Inconsolata's 0.5), None for a proportional one."""
    widths = {face["advances"].get(ch) for ch in "imMW0"}
    return None if len(widths) != 1 or None in widths else face["advances"]["m"]


def _advance(table: Mapping[str, float], ch: str) -> float | None:
    """A character's advance in a table, or its base letter's (é -> e: an accent adds no width)."""
    w = table.get(ch)
    if w is None and ch.isalpha():
        w = table.get(unicodedata.normalize("NFD", ch)[0])
    return w


def unspaced(text: str) -> str:
    """A text's letters alone: its spaces, which TeX stretches after a sentence, taken out."""
    return text.replace(" ", "").replace("\xa0", "")


def advance_widths(text: str, cm: CmFace, slides: Mapping[str, float]) -> tuple[float, float, int, int]:
    """(Slides em, PDF em, characters counted, characters skipped) of a text set in a Computer
    Modern face (`cm`, a CM_ADVANCES entry) and in its Slides substitute (`slides`, an ADVANCES
    entry). The PDF side is what TeX set: ligatures, kerning pairs, interword space and the extra
    space after a sentence. A character either table lacks counts on neither side, and so do dot
    leaders and ellipses (LEADER): each of their dots is a skipped character."""
    for seq, lig in CM_LIGATURES:
        if lig in cm["advances"]:
            text = text.replace(seq, lig)
    text = LEADER.sub(lambda m: "\0" * m.group().count("."), text)
    s_em = p_em = 0.0
    counted = skipped = 0
    factor = 1000
    prev: str | None = None
    for ch in text:
        if ch == "\0":  # a leader's dot
            skipped += 1
            factor, prev = 1000, None
            continue
        if ch in "  ":
            s_em += slides.get(" ", 0.0)
            p_em += cm["space"] + (cm["extra_space"] if factor >= 2000 else 0.0)
            factor, prev = 1000, None
            continue
        parts = next((seq for seq, lig in CM_LIGATURES if lig == ch), ch)  # Slides sets no ligature
        p = _advance(cm["advances"], ch)
        ws = [w for c in parts if (w := _advance(slides, c)) is not None]
        if p is None or len(ws) < len(parts):
            skipped += len(parts)
            prev = None
            continue
        p_em += p + (cm["kerns"].get(prev + ch, 0.0) if prev else 0.0)
        s_em += sum(ws)
        counted += len(parts)
        prev = ch
        if ch.isupper():
            factor = 999
        elif ch in SPACE_FACTOR:
            factor = SPACE_FACTOR[ch] if factor >= 1000 else 1000
        elif ch not in KEEPS_SPACE_FACTOR:
            factor = 1000
    return s_em, p_em, counted, skipped


def design_width(table: dict[int, float], design: float) -> float:
    keys = sorted(table)
    if design <= keys[0]:
        return table[keys[0]]
    if design >= keys[-1]:
        return table[keys[-1]]
    hi = next(k for k in keys if k >= design)
    lo = max(k for k in keys if k <= design)
    if hi == lo:
        return table[lo]
    t = (design - lo) / (hi - lo)
    return table[lo] + t * (table[hi] - table[lo])


class FontMapper:
    """TeX font + size -> Slides font family + size with calibrated width correction."""

    def __init__(self):
        self.factors = {}  # family -> (running text factor, title factor)
        self.style = {}    # family -> {"bold": ratio, "italic": ratio} relative to running text
        self._reference = {}  # reference_ratio's answers
        for family, path in (("sans", CALIBRATION), ("serif", CALIBRATION_DIR / "fonts_serif.json")):
            cal = json.loads(path.read_text(encoding="utf-8"))["fonts"]
            ratios = cal[FONT_FOR_FAMILY[family]]["width_ratio"]
            self.factors[family] = (ratios["text_mean"], ratios["by_row"]["title"])
            self.style[family] = {k: ratios["relative_to_text"][k] for k in ("bold", "italic")}

    # A run dict (classify's lines, deck_ir's read of a deck, the tests) reaches each method through
    # its twin on a `SetRun` (`emit_model.run_of`), which emit's planners call.

    def text_style(self, run: JsonMap, scale: float) -> tuple[JsonObject, list[str]]:
        return self.style_of(run_of(run), scale)

    def style_of(self, run: SetRun, scale: float) -> tuple[JsonObject, list[str]]:
        """Font part of a Slides TextStyle. Google fonts used by the PDF itself keep their
        family and weight (e.g. Fira Sans Light); TeX fonts get a calibrated substitute."""
        family, size = self.size_of(run, scale)
        google = google_font(run.font)
        if google:
            return ({"weightedFontFamily": {"fontFamily": google[0], "weight": google[1]},
                     "fontSize": pt(size), "italic": google[2] or run.italic},
                    ["weightedFontFamily", "fontSize", "italic"])
        if self.optical_weight_of(run):
            return ({"weightedFontFamily": {"fontFamily": family, "weight": OPTICAL_WEIGHT}, "fontSize": pt(size),
                     "italic": run.italic},
                    ["weightedFontFamily", "fontSize", "italic"])
        return ({"fontFamily": family, "fontSize": pt(size), "bold": run.bold, "italic": run.italic},
                ["fontFamily", "fontSize", "bold", "italic"])

    @staticmethod
    def optical_weight(run: JsonMap) -> bool:
        return FontMapper.optical_weight_of(run_of(run))

    @staticmethod
    def optical_weight_of(run: SetRun) -> bool:
        """Whether a regular run is written at OPTICAL_WEIGHT for its small optical cut
        (OPTICAL_WEIGHT_DESIGN)."""
        if run.bold or google_font(run.font) or run.family not in OPTICAL_WEIGHT_DESIGN:
            return False
        design = font_info(run.font).design_size
        return design is not None and design <= OPTICAL_WEIGHT_DESIGN[run.family]

    def face(self, run: JsonMap) -> str:
        return self.face_of(run_of(run))

    def face_of(self, run: SetRun) -> str:
        """The ADVANCES style Slides draws a run in: a run written at OPTICAL_WEIGHT takes the bold
        face only where Slides draws that weight bold (DRAWN_BOLD_WEIGHT)."""
        heavy = OPTICAL_WEIGHT >= DRAWN_BOLD_WEIGHT and self.optical_weight_of(run)
        return STYLE_KEY[(run.bold or heavy, run.italic)]

    def width_ratio(self, font: str, family: str, bold: bool, italic: bool) -> float:
        """Expected Slides width / PDF width of a run after the size correction: bold and
        italic are only half corrected (see __call__)."""
        if google_font(font) or family not in self.style:
            return 1.0
        text = self.text_ratios(font, family, bold, italic)
        if text is not None:  # a TeX text face of TEXT_ADVANCES: its own style against its regular
            base, own = text
            rel = own / base
            return rel / (1 + (rel - 1) / 2)
        ratio = 1.0
        for key, on in (("bold", bold), ("italic", italic)):
            if on:
                rel = self.style[family][key]
                ratio *= rel / (1 + (rel - 1) / 2)
        return ratio

    def __call__(self, run: JsonMap, scale: float) -> tuple[str, float]:
        return self.size_of(run_of(run), scale)

    def size_of(self, run: SetRun, scale: float) -> tuple[str, float]:
        """The Slides family and size (pt) a run is set in."""
        google = google_font(run.font)
        if google:  # same font in Slides: no width correction
            return google[0], round(run.size * scale, 1)
        info = font_info(run.font)
        family = FONT_FOR_FAMILY.get(run.family, "Lato")
        leader = self.leader_factor_of(run, family)
        if leader is not None:
            return family, round(run.size * scale / leader, 1)
        design = info.design_size or 10
        text_ratios = None if run.family == "mono" or info.design_size is not None else \
            self.text_ratios(run.font, run.family, run.bold, run.italic)
        if run.family == "mono":
            factor = ROBOTO_MONO_ADVANCE_EM / mono_pitch(run, design)
        elif text_ratios is not None:
            # A TeX text face other than Computer Modern (Libertine, Bera, DejaVu, Palatino...): its
            # factor is what its own advances predict for the calibration sentences in the substitute,
            # the prediction that is within 0.5% of the measured factors for CM. Its bold and italic
            # are half corrected against its regular, as CM's are.
            base, own = text_ratios
            factor = base * (1 + (own / base - 1) / 2)
            if not run.smallcaps:
                factor *= self.shape_ratio_of(run, family, factor, design)
        else:
            text, title = self.factors.get(run.family, self.factors["sans"])
            # A bold sans cut has widths of its own per optical size (BOLD_SANS_DESIGN_WIDTH: EC's
            # SFSX); 1.0 at 10 pt, CM's and Latin Modern's only bold sans.
            cut = face_optical_width(run.family, info.bold, design) / optical_width(run.family, design)
            if run.family != "serif" and 11.5 <= design < 14:
                factor = title  # calibrated directly on CMSS12 titles
                if cut != 1.0:
                    factor /= cut
            else:
                # Other optical sizes: CM's small cuts are wider per em (up to OPTICAL_WIDTH_MAX),
                # its large ones narrower.
                factor = text / face_optical_width(run.family, info.bold, design)
            # Bold and italic substitutes run 4-8% narrower than CM's; correct half of that, so
            # widths come closer without emphasised words looking visibly larger.
            style = self.style.get(run.family, self.style["sans"])
            for key, on in (("bold", run.bold), ("italic", run.italic)):
                if on:
                    factor *= 1 + (style[key] - 1) / 2
            if info.design_size is not None:  # Computer Modern metrics (CM, EC, Latin Modern)
                if run.smallcaps:
                    factor /= SMALL_CAPS_WIDTH.get(run.family, 1.0)
                else:
                    factor *= self.shape_ratio_of(run, family, factor, design)
        return family, round(run.size * scale / factor, 1)

    def leader_factor_of(self, run: SetRun, family: str) -> float | None:
        """The size factor of a run that is nothing but a leader's dots, with the distance the PDF
        set them apart (classify's `pitch` on such a run): Slides' distance from one dot to the next
        in `family` over the PDF's, so that its dots stand as far apart as the PDF's and the row is
        as long. Sized like words, a \\dotline of PT Serif dots came out 12% short
        (real_defense-defense 41). None for any other run, a monospaced one (`pitch` is its column
        grid's), or one among other words (`in_sentence`: sized like them)."""
        steps = self.leader_steps_of(run, family)
        return None if steps is None or run.pitch is None else steps[0] / steps[1] / run.pitch

    def leader_steps_of(self, run: SetRun, family: str) -> tuple[float, int, float] | None:
        """(Slides' em from a sized leader's first dot to its last, the steps between them, and
        what ADVANCES add up to over the same characters) for a run `leader_factor_of` sizes;
        None for any other."""
        text = run.text.strip()
        if run.pitch is None or run.pitch <= 0 or run.family == "mono" or run.in_sentence or run.script \
                or run.hole is not None or run.cell or LEADER_ONLY.fullmatch(text) is None or family not in ADVANCES:
            return None
        face = self.face_of(run)
        steps = text.count(".") - 1
        between = text[:text.rindex(".")]  # first dot to last
        slides = ADVANCES[family][face]
        summed = sum(slides.get(c, UNMEASURED_ADVANCE_EM) for c in between)
        measured = LEADER_PITCH_EM.get((family, face))
        if measured is not None and SPACED_DOTS.fullmatch(between):
            return measured * steps, steps, summed
        return summed, steps, summed

    def leader_correction_of(self, run: SetRun, family: str) -> float:
        """How much narrower (em, negative) Slides sets a sized leader than its characters' ADVANCES
        add up to (LEADER_PITCH_EM): a box measured by ADVANCES alone ran 19% past a PT Serif row,
        off the slide (real_defense-defense 8). 0.0 for any other run."""
        steps = self.leader_steps_of(run, family)
        return 0.0 if steps is None else steps[0] - steps[2]

    def shape_ratio(self, run: JsonMap, family: str, factor: float, design: float) -> float:
        return self.shape_ratio_of(run_of(run), family, factor, design)

    def shape_ratio_of(self, run: SetRun, family: str, factor: float, design: float) -> float:
        """How much wider than the PDF's Slides sets this run for its letters, beyond what the
        size factor corrects; 1.0 when that is within tolerance (ordinary prose, a word), not
        known (a character neither table has) or not this run's to fix (scripts, holes).

        A run that is only a number (a digit, nothing but digits and a number's punctuation)
        gets the whole ratio at the size `factor` gives it, when it comes out wider. Any other
        run of SHAPE_MIN_CHARS counted characters or more is judged against the calibration
        sentences in its own face and gets the ratio when it is off by more than SHAPE_TOL, unless
        it shares its paragraph with other runs (`in_sentence`: sized like them).

        A Computer Modern run is judged in its CM_ADVANCES face at its optical size, any other TeX
        text face of TEXT_ADVANCES in its own (`text_face_of`; no optical sizes)."""
        text = run.text
        info = font_info(run.font)
        name = cm_face_of(run)
        known = (name, CM_ADVANCES[name]) if name is not None and name in CM_ADVANCES else \
            None if info.design_size is not None else text_face_of(run)
        # (a table cell's run keeps the table's size: its column is made as wide as Slides sets
        # it (fit_columns), and a number set smaller rode high in its top-anchored cell)
        if known is None or not text.strip() or run.script or run.hole or run.cell or family not in ADVANCES:
            return 1.0
        key, face = known
        slides = ADVANCES[family][self.face_of(run)]
        number = "".join(text.split())
        if any(c in DIGITS for c in number) and all(c in NUMBER_CHARS for c in number):
            widths = [face["advances"].get(c) for c in number]
            if None in widths:
                return 1.0
            s_em = sum(slides.get(c, UNMEASURED_ADVANCE_EM) for c in number)
            p_em = sum(w for w in widths if w is not None)
            cut = 1.0 if info.design_size is None else face_optical_width(run.family, info.bold, design)
            return max(1.0, s_em / factor / (p_em * cut))
        if run.in_sentence:  # (a run among others keeps their size: in_sentence)
            return 1.0
        s_em, p_em, counted, skipped = advance_widths(text, face, slides)
        if counted < SHAPE_MIN_CHARS or skipped > 0.1 * counted or p_em <= 0:
            return 1.0
        # sized by the title factor (CM's sans titles only)
        title = info.design_size is not None and run.family != "serif" and 11.5 <= design < 14
        ratio = s_em / p_em / self.reference_ratio_in(key, face, slides, title, False)
        if abs(ratio - 1) <= SHAPE_TOL:
            return 1.0
        # Its letters are what is sized, never its spaces: 'Query: “A ? C ? F ? E ? B ?”' is half
        # spaces, TeX's wide ones after each '?', which made it 9% larger than its letters asked
        # (real_linear-attention-a s39). Letters alike keep the deck's size.
        ls_em, lp_em, _, _ = advance_widths(unspaced(text), face, slides)
        letters = ls_em / lp_em / self.reference_ratio_in(key, face, slides, title, True) if lp_em > 0 else 1.0
        return letters if abs(letters - 1) > SHAPE_TOL and (letters - 1) * (ratio - 1) > 0 else 1.0

    def reference_ratio(self, face: str, slides: Mapping[str, float], title: bool) -> float:
        """`reference_ratio_in` a CM_ADVANCES face, by its name."""
        return self.reference_ratio_in(face, CM_ADVANCES[face], slides, title, False)

    def reference_ratio_in(self, key: str, face: CmFace, slides: Mapping[str, float], title: bool,
                           letters: bool) -> float:
        """Slides em / PDF em of the calibration sentences in a face (`key` names it: a CM_ADVANCES
        name, or 'family/style' of TEXT_ADVANCES): where its size factor puts the widths of ordinary
        text; with `letters`, of their letters alone (`unspaced`)."""
        cached = (key, id(slides), title, letters)  # `slides` is one of ADVANCES' tables, which live as long
        if cached not in self._reference:
            s_em = p_em = 0.0
            for sentence in SHAPE_TITLE_REFERENCE if title else SHAPE_REFERENCE:
                s, p, _, _ = advance_widths(unspaced(sentence) if letters else sentence, face, slides)
                s_em, p_em = s_em + s, p_em + p
            self._reference[cached] = s_em / p_em
        return self._reference[cached]

    def text_ratios(self, font: str, family: str, bold: bool, italic: bool) -> tuple[float, float] | None:
        """(regular, own) `reference_ratio_in` of a TeX text face other than Computer Modern
        (TEXT_ADVANCES) set in `family`'s substitute: its regular face in the substitute's regular,
        and its own face in the substitute's face Slides draws it in. None for a font of no such
        family, or a substitute ADVANCES has not measured."""
        substitute = FONT_FOR_FAMILY.get(family)
        if substitute is None or substitute not in ADVANCES or family == "mono":
            return None
        own, regular = text_face(font), text_regular(font)
        if own is None or regular is None:
            return None
        slides = ADVANCES[substitute]
        return (self.reference_ratio_in(regular[0], regular[1], slides["regular"], False, False),
                self.reference_ratio_in(own[0], own[1], slides[STYLE_KEY[(bold, italic)]], False, False))


# A bullet dict (the tests, devtools.alignment, classify's reading of a page) reaches each bullet
# function through its twin on a `SetBullet` (`emit_model.bullet_of`), which emit's planners call.

def bullet_shape(bullet: JsonMap) -> BulletFace | None:
    return bullet_shape_of(bullet_of(bullet))


def bullet_shape_of(bullet: SetBullet) -> BulletFace | None:
    """The BULLET_SHAPES entry for a bullet; None for numbers. A glyph's is its character's,
    except a bullet character the PDF draws as a filled square (LM Sans's \\textbullet)."""
    text = bullet.text
    if bullet.kind == "number" or (bullet.kind == "image" and text.isdigit()):
        return None
    if bullet.kind == "glyph":
        shape = GLYPH_SHAPES.get(text, "disc")
        return "square" if shape == "disc" and inked_square(bullet) else shape
    return "disc" if bullet.shape is None else bullet.shape


def inked_square(bullet: SetBullet) -> bool:
    ink = bullet.ink
    if ink is None or ink.fill < 0.9:  # (a disc fills 0.79 of its box)
        return False
    w, h = ink.box[2] - ink.box[0], ink.box[3] - ink.box[1]
    return 0.8 <= w / h <= 1.25 if h > 0 else False


# A glyph bullet whose ink (render.glyph_ink) would come out this much smaller than a bullet at
# the label's size is sized by its ink, as vector bullets are. Beamer's own glyphs (MSAM's ▶
# 0.58 em, CMSY's • 0.39 em against the disc's 0.41) keep the label's size.
INK_SIZED = 0.75


def _label_size(bullet: SetBullet, size: float, scale: float) -> float:
    """The size (Slides pt) of the font the bullet's label is drawn in, no larger than `size`."""
    return min(size, (size / scale if bullet.label_size is None else bullet.label_size) * scale)


def bullet_char_of(bullet: SetBullet) -> str | None:
    """The character a bullet is written as when no preset draws it (CHAR_BULLETS); None else."""
    shape = bullet_shape_of(bullet)
    return None if shape is None or shape not in CHAR_BULLETS else CHAR_BULLETS[shape][0]


def shell_char_of(bullet: SetBullet) -> str | None:
    """The character a bullet is written as in a text shell: its own (CHAR_BULLETS) where no preset
    draws it, else its preset's glyph (PRESET_CHARS); None for a number (Slides numbers a list
    itself, never probed through a .pptx)."""
    shape = bullet_shape_of(bullet)
    if shape is None:
        return None
    return CHAR_BULLETS[shape][0] if shape in CHAR_BULLETS else PRESET_CHARS[shape]


def face_ems(shape: BulletFace, char: bool) -> tuple[float, float]:
    """(ink height em, gap em) of a bullet face as Slides draws it: its own character's
    (CHAR_BULLETS) when `char` and it has one, else its preset glyph's."""
    if char and shape in CHAR_BULLETS:
        return CHAR_BULLETS[shape][1], CHAR_BULLETS[shape][2]
    return BULLET_SHAPES[shape][2], BULLET_SHAPES[shape][3]


def ink_sized(bullet: SetBullet, size: float, scale: float, char: bool) -> float | None:
    """The size that gives a glyph bullet its PDF ink height, when it is to be used. A bullet
    written as its own character (`char`), or one whose label is an icon font's glyph
    (`label_icon`: a dingbat drawn as the preset nearest it), is always sized by its ink: Slides
    draws another face's glyph than the PDF's, so the label's size says nothing of its height.
    (Zapf Dingbats' eight-pointed ✴ at 5.98 pt, 0.72 em of ink, was a ★ of 0.81 em at the
    label's size: 12% taller than the PDF's, real_africa-remote-sens-30 41.)"""
    shape = bullet_shape_of(bullet)
    if bullet.kind != "glyph" or bullet.ink is None or shape is None:  # (a glyph always has a shape)
        return None
    full = _label_size(bullet, size, scale)
    height = (bullet.ink.box[3] - bullet.ink.box[1]) * scale
    inked = max(0.3 * size, min(size, height / face_ems(shape, char)[0]))
    if (char and shape in CHAR_BULLETS) or bullet.label_icon:
        return inked
    return inked if inked < INK_SIZED * full or shape != GLYPH_SHAPES.get(bullet.text, "disc") else None


def bullet_preset(bullet: JsonMap) -> str:
    return bullet_preset_of(bullet_of(bullet))


def bullet_preset_of(bullet: SetBullet) -> str:
    shape = bullet_shape_of(bullet)
    if shape is None:
        return BULLET_PRESETS["number_parens" if ")" in bullet.text else "number"]
    return BULLET_SHAPES[shape][0]


def bullet_level(bullet: JsonMap, level: int) -> int:
    return bullet_level_of(bullet_of(bullet), level)


def bullet_level_of(bullet: SetBullet, level: int) -> int:
    """Slides nesting level: numbers count by depth (1., a., i.), glyphs pick their shape.
    ● ○ ■ keep the depth (they repeat every 3 levels); other glyphs exist at one level only.
    (Indents are set explicitly, so the level only decides the glyph and what Tab does.)"""
    shape = bullet_shape_of(bullet)
    if shape is None:
        return level
    preset, first = BULLET_SHAPES[shape][:2]
    return 3 * min(level, 2) + first if preset == "BULLET_DISC_CIRCLE_SQUARE" else first


def bullet_size(bullet: JsonMap, size: float, scale: float) -> float:
    return bullet_size_of(bullet_of(bullet), size, scale, False)


def bullet_size_of(bullet: SetBullet, size: float, scale: float, char: bool) -> float:
    """Font size giving the bullet its PDF height (at most the text's: a larger bullet would
    push the line down). Glyph and number boxes are font boxes: their size is the font's, or
    their ink's where that is much smaller (`ink_sized`). `char`: the bullet is written as its
    own character where it has one (CHAR_BULLETS), not as a preset's glyph."""
    shape = bullet_shape_of(bullet)
    inked = ink_sized(bullet, size, scale, char)
    if inked is not None:
        return round(inked, 1)
    if bullet.kind in ("glyph", "number") or shape is None:
        return round(_label_size(bullet, size, scale), 1)
    # A ball's image box is rounded out to whole points around a transparent margin (a 5 pt ball
    # in a 6 pt box, real_ansible-meetup-201-beamer 23): its ink is the height the preset's
    # (BULLET_SHAPES, Slides' own ink per em) is to match.
    box = bullet.bbox if bullet.ink is None else bullet.ink.box
    height = (box[3] - box[1]) * scale
    return round(max(0.3 * size, min(size, height / face_ems(shape, char)[0])), 1)


def bullet_gap(bullet: SetBullet, size: float, char: bool) -> float:
    """Distance from the bullet box's right edge to indentFirstLine."""
    shape = bullet_shape_of(bullet)
    if bullet.kind in ("glyph", "number") or shape is None:
        return BULLET_GAP
    return face_ems(shape, char)[1] * size


def bullet_extent(bullet: JsonMap, size: float, scale: float) -> tuple[float, float, float]:
    return bullet_extent_of(bullet_of(bullet), size, scale, False)


def bullet_extent_of(bullet: SetBullet, size: float, scale: float, char: bool) -> tuple[float, float, float]:
    """(PDF x0, PDF x1, gap after it in Slides pt) of what the Slides bullet stands for: the
    ink of a glyph sized by its ink (then placed as a vector bullet is), else the bullet's box."""
    z = bullet_size_of(bullet, size, scale, char)
    shape = bullet_shape_of(bullet)
    if bullet.ink is not None and shape is not None and (
            bullet.kind == "image" or ink_sized(bullet, size, scale, char) is not None):
        return bullet.ink.box[0], bullet.ink.box[2], face_ems(shape, char)[1] * z
    return bullet.bbox[0], bullet.bbox[2], bullet_gap(bullet, z, char)


def rgb(hex_color: str) -> JsonObject:
    h = hex_color.lstrip("#")
    return {"opaqueColor": {"rgbColor": {k: int(h[i:i + 2], 16) / 255 for k, i in
                                         (("red", 0), ("green", 2), ("blue", 4))}}}


# Advance widths (em) of text characters on Slides' own renderer, per substitute font and
# style (tools/probe_advances.py). Slides' Lato is not the Lato on google/fonts (its space is
# 0.19 em, its slash 0.31), so these are measured, not read out of a font file.
ADVANCES = json.loads((CALIBRATION_DIR / "advances.json").read_text(encoding="utf-8"))["fonts"]
UNMEASURED_ADVANCE_EM = 0.6  # a character the probe did not measure: as wide as the widest digits
