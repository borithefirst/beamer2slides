"""The word space at an edge of inline code, as Slides is made to leave it.

TeX sets the space beside a \\texttt word in the surrounding text's font: on 366 such edges of the
real decks and the built ones it is CM Sans's word space (0.333 em at 10 pt, 0.326 at 12, 0.313 at
17; Latin Modern Sans alike), before the code as after it - twice that where a macro adds one
(real_talksx's \\code, 0.627 em). classify writes that space in the prose run
(`classify_text.prose_spaces`: in Roboto Mono it would be 0.525 em), where Slides draws Lato's
space: 0.193 em of Lato, which is set at LATO_SIZE of the PDF's size, so 0.189 em - 0.144 em short
on each side of the code. In a sentence Lato's wider letters make its narrow spaces up (the size
factor is calibrated on whole sentences), but inline code is sized by its columns alone (Roboto
Mono to CMTT's 0.525 em advance) and makes up nothing: the words beside it ran into it
('cvsimport,git', real_gittalk-gittalk s13; 'LambdaCase,ViewPatterns' and lines 3-6% short,
real_talksx s13-s38).

So the prose run gets EDGE_SPACE characters (`edge_fill`) before that space, as many as bring
Slides' gap nearest the PDF's. Before it, as a sentence's thick space is (`classify_text.widened`):
a line Slides breaks at the space leaves them at the end of the line before, not in front of the
next line's first word. Readers take EDGE_SPACE and the space after it for one space
(`inverse.latex_escape`, `compare.NORMALISE`, `merge.collapse_holes`); emit measures it by
SYMBOL_ADVANCE_EM and writes it only on a line it leaves no wider than the PDF's
(`emit_text.within_budget`: Lato's letters may already have made the gap up).

Measured live (tools/probe_mono_edges.py, 2026-10-04, 11/18/28 pt both ways): in a Lato run a
plain space is 0.193 em, U+2009 0.201, U+200A 0.085, U+2006 0.169, U+2008 0.280, U+2004 0.333;
in the Roboto Mono run every one of them is 0.537 em. TeX's 0.333 em is 0.340 of Lato's size,
and a SIX-PER-EM SPACE before the space comes nearest it (0.362, +0.022; a thin space 0.394,
two hair spaces 0.363 in two characters). The readers and emit's measure take EDGE_SPACE from
here.
"""

EDGE_SPACE = "\u2006"  # SIX-PER-EM SPACE, written before the word space at an edge of inline code
EDGE_SPACE_EM = 0.169   # Slides' advance of EDGE_SPACE in a Lato run, em of the run's size (probe)
PLAIN_SPACE_EM = 0.193  # Slides' advance of a plain space in a Lato run beside Roboto Mono (probe)
LATO_SIZE = 0.98       # Lato's size per the PDF's of a CM Sans run: FontMapper's running-text factor, 1/1.0203
EDGE_MAX = 3           # at most this many EDGE_SPACEs at one edge (a gap wider is a \quad's: em spaces)


def edge_fill(gap: float) -> str:
    """The EDGE_SPACE characters written beside the plain space at an edge of inline code, in a
    Lato run, for a gap of `gap` em (of the prose's size) in the PDF: as many as bring Slides' gap
    nearest it, none where a plain space alone is as near."""
    short = gap / LATO_SIZE - PLAIN_SPACE_EM  # (em of Lato's size)
    return EDGE_SPACE * max(0, min(EDGE_MAX, round(short / EDGE_SPACE_EM)))


def edge_width(fill: str) -> float:
    """What `fill` adds to its line in Slides, em of the prose's size."""
    return len(fill) * EDGE_SPACE_EM * LATO_SIZE
