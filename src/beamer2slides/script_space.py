"""The room after a sub- or superscript, as Slides is made to leave it.

TeX ends every script with \\scriptspace (0.5 pt) and sets it at 0.7-0.73 of its line in CMMI's
wide italic; Slides draws a script run at 0.665 of its size (classify_text.SLIDES_SCRIPT) in Lato
or PT Serif. So what follows a script directly - a comma, a bracket, a period, the next letter -
comes in Slides 0.12 em of the line nearer than TeX set it (median over 110 subscripts of five
real decks, p10 0.06, p90 0.20; superscripts 0.13 over 92): 'D_{k,i},' read k,i' with the comma
on the i (real_lecture-phylogenet s15), 'K_Y.' had the period under the Y's arm
(real_beamer-derived-cat s19), v_jk_j ran together (real_linear-attention s12) and inline
formulas came out ~14% short.

So the script run gets SCRIPT_SPACE characters at its end (`script_fill`), as many as bring the
script's Slides advance (`emit_widths.slides_width`) nearest the PDF's distance from the script's
first glyph to the glyph after it - only where that glyph follows on the same line with no space
between (a word space after a script is a space, which `classify_text.widened` may widen). A hair
space draws nothing and Slides breaks no line at one (as at a thin space, tools/probe_hole_break.py
r10; tools/probe_script_space.py checks both), but it judges whether a word fits only up to the
hair spaces, the rest going along (`emit_widths.first_break`, `text_layout.wrap`). Readers take it for nothing (`inverse.latex_escape`,
`compare.NORMALISE`, `texmap.NORMALISE`, `merge.collapse_holes` through emit_widths.ADDED_SPACE);
emit measures it by SYMBOL_ADVANCE_EM and writes it only on a line it leaves no wider than the PDF's
(`emit_text.within_budget`).

Not in a code block (its columns are spaces), a table cell or a node label (`span_runs`: their
widths are fitted as a whole), nor before a formula's picture (a hole is placed by measurement).
"""

SCRIPT_SPACE = "\u200a"  # HAIR SPACE, written at the end of a script run before a glyph
SCRIPT_SPACE_EM = 0.085  # Slides' advance of a hair space in a Lato run, em of its drawn size (tools/probe_mono_edges.py)
SCRIPT_SPACE_MAX = 4     # at most this many after one script (0.23 em of its line)


def script_fill(short: float, drawn: float) -> str:
    """The SCRIPT_SPACE characters written at the end of a script run that Slides sets `short` pt
    narrower than the PDF's distance to the glyph after it, its letters drawn at `drawn` pt: as
    many as bring that distance nearest the PDF's."""
    if drawn <= 0 or short <= 0:
        return ""
    return SCRIPT_SPACE * max(0, min(SCRIPT_SPACE_MAX, round(short / (SCRIPT_SPACE_EM * drawn))))
