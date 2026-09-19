# Adopt benchmark

How `beamer2slides adopt` is measured, and what each change measured. Newest results go at the
end, as a section of their own (tags before -> after, corpus boxes/page, decks that moved, what was
tried and dropped, what is left). CLAUDE.md keeps only the commands and the current score.

## Corpus and commands

**Benchmark** (`tools/adopt_bench.py`, corpus of 29 public decks in `tests/decks/foreign/corpus.json`,
cached in `out/adopt-corpus` or `$B2S_ADOPT_CORPUS`): `capture` reads a deck and its LARGE thumbnails
(read-only, 429 back-off), `run [decks] --jobs N --tag T` bootstraps, compiles (a failing deck is
split frame by frame to name the broken ones), scores every slide (`boxes` / `page` / `pixels`) and
writes deck|source|diff sheets to `<corpus>/<deck>/runs/<tag>/sheets`; `report --tag T`.

## Workflow

`run` caches each deck's scores by its source tree, IR and scorer (`<corpus>/<deck>/cache`,
`--no-cache` to compile anyway), so a change recompiles only the decks whose source it changed, and
starts the slowest decks first; `losses --tag T` charges every pixel the `boxes` score counts against
a slide to the smallest element box holding it and ranks elements, decks, kinds and fonts in
thousandths of the corpus score (`element_losses`, `tests/test_adopt_bench.py`). A study: run the
bench at the base (`--tag x-base`), change one cause, rerun (`--tag x-after`), and compare per deck
and per slide; a result belongs in a new section at the end of this file.

## Bootstrap, units, text (`abs` -> `combined`)

Measured (bootstrap only, 912 slides): boxes 0.593 -> 0.754 (mean per deck 0.565 -> 0.725, every
deck up; pixels 0.913 -> 0.939), 11 frames that did not compile -> 0 (tags `abs` -> `merged`).
Lengths in bp (`adopt.to_bp`: the IR is PDF points, TeX's pt is 72.27 to the inch) and see-through page
backgrounds blended over white: 0.754 -> 0.818 (`units`). Text (tags `units` -> `text-b` -> `text-ins2`):
boxes 0.818 -> 0.843 -> 0.852, page 0.814 -> 0.839 -> 0.848. With the fills below (`combined`): boxes
0.885, page 0.881, pixels 0.970, no deck down.

## Bullets, empty paragraphs, real fonts, weights (`combined` -> `g24-final`)

Then (`combined` -> `weak-e6`: boxes 0.885 -> 0.915, page 0.881 -> 0.911, pixels 0.970 -> 0.975):
a bullet's `\llap{}` line ended in a word space that pushed every bulleted line right by one space
(`%` after it: cs161-tls 0.867 -> 0.989, ds-lecture 0.802 -> 0.964, comic-strips 0.908 -> 0.980);
an empty paragraph is as tall as its own newline's style (creandum-board 0.760 -> 0.867) and a line
spacing >= `WIDE_SPACING` adds nothing under the last line; foreign decks keep their fonts' real
names and sizes (`text_paragraphs(foreign=True)`, not FontMapper's CM stand-ins); Windows fonts are
found by the family their name table gives (`font_candidates`: `ariblk.ttf` = Arial Black), and a
missing one takes a metric-compatible fetched stand-in (`adopt.SUBSTITUTES`: Arimo, Tinos, Cousine,
Carlito, Archivo Black, Libre Bodoni...); CJK falls back to the Noto Sans JP/KR/SC/TC Slides draws
with (`scripts.RENDERER_CJK`); a fixed left-aligned box whose thumbnail ink starts at its edge (and
whose first ink row is above the inset line) has no insets (`deck_ir.thumbnail_insets`, wherever thumbnails are read:
gdg24 0.896 -> 0.900). Then (`g24-base` -> `g24-final`, boxes 0.924 -> 0.938, page 0.920 -> 0.934,
pixels 0.976 -> 0.978, no deck down): "Google Sans Text" is Google Sans' variable font at opsz 17,
not the opsz 18 display cut that stood in 3% narrower (`fontfetch.OPTICAL`, renamed instances);
`thumbnail_insets` leaves out only the rows another element crosses instead of the whole box
(gdg24's overlapping heading/caption boxes, code under a highlight bar) and takes ink on the box's
first pixel column unless it goes on outside; and a run's `weightedFontFamily.weight` other than
400/700 (`run["weight"]`) is set in an instance `fontfetch.weight_file` cuts from the fetched
variable font, declared as `FontFace={w600}{n}{...}` and selected by `\fontseries{w600}`
(`adopt.weight_faces`, `series`). gdg24 0.906 -> 0.985, creandum-board 0.867 -> 0.923,
sc-dark-minimal 0.900 -> 0.955, journey-maps 0.864 -> 0.909, intro-lecture 0.937 -> 0.948,
devfest2020 0.897 -> 0.903. Left in gdg24: code overflowing a middle-aligned box (slides 78-79), Google
Sans Mono vs Code glyph offsets (77), centred boxes whose insets no ink edge shows (73), and captions
whose first spaceAbove the deck does not apply when their text overflows (52-56; the rule is unsettled).
Left: comps-analysis (0.336; its text sits ~4 pt high - insets the API does
not report - and its Bodoni is narrower than any fetchable one), devfest2020's numbered lists (Slides
places big numbers differently), jruby-ja (gradient backdrop; Japanese still sets wider), hebrew-lesson.
Known gaps, by what they cost: text insets the API does not report where no autofit height gives
them away, the freeforms tracing refuses (below; Google's .pptx export has their geometry, and
none is cached in the corpus), and dragged shape adjustments.

## Fills read off the thumbnail (`units` -> `fills-d`)

Fills the API cannot say (`deck_fills.py`, `tests/test_adopt_fills.py`): a gradient, picture or texture
fill reads `shapeBackgroundFill: {}`, a .pptx table style's cell colour NOT_RENDERED, and every
placeholder INHERIT chain in the corpus ends NOT_RENDERED too - so `deck_ir(foreign=True,
thumbnails=n -> image)` reads them from the slide's own thumbnail (the bench passes the cached LARGE
ones; live `adopt` reads one per slide, `deck_ir.slide_thumbnails`, 3 at a time into
`<work>/thumbnails`, `$B2S_ADOPT_THUMBNAILS=0` skips it; without thumbnails nothing changes). Conservative, since a false fill paints over what lies
under it: the box less a rim and less opaque elements above must be one flat colour (ink allowed only
in boxes of texts above), not the page's or the colour all around it (`edges_show`, table cells vs
the page outside the table), settled top down; a rectangle may read as a three-stop axis gradient
(TikZ `left/middle/right color`: cs161's header bar). Measured (`units` -> `fills-d`): cs161-net
page 0.734 -> 0.917, cs161-tls 0.647 -> 0.864, hebrew-lesson 0.310 -> 0.668 (paper backdrops and
style-coloured cells). SlidesCarnival's 2,068 `{}` freeforms are squiggles whose box is not flat
and stay unfilled: that is the freeform gap, not a fill one.

## Freeforms traced (`ff-base` -> `ff-d`)

Freeforms traced from the thumbnail (`deck_freeforms.py`, `tests/test_adopt_freeforms.py`): the API gives
a freeform (shapeType CUSTOM or none, and lines with no line type) as a box only, so `deck_fills.settle`
traces it in the same picture, top down: how much of its paint (fill, outline, or the one colour a
`{}` fill shows over its ground) each pixel holds, cut at one half by marching squares, simplified
(Douglas-Peucker 0.4 px) into rings in page pt that `adopt_shapes.traced_block` draws as one even-odd
TikZ path - a vector outline, since the shapes are flat colour and a path stays editable where a
cropped PNG would be a picture of a shape. Pixels under opaque elements above are the shape's where
they continue it; holes are filled when letters above, a shape above nobody could read (`unsaid`),
or ink no colour under the shape explains made them. Refused (the box stays as before) when the paint
is ambiguous (a colour an element under it or text over it has too), translucent over an unknown
ground, runs on outside the box (a squiggle tile over a wave), misses a side of the box, or fills the
box (the preset rectangle says that better). Corpus slides: 1,573 of 3,322 freeforms traced (142 of
them `{}` fills); most refusals are off-page (2,358) or `{}` pictures/textures (621). Measured against
the same code without it, rerun the same day (`ff-base` -> `ff-d`, 912 slides): boxes 0.885 -> 0.890,
page 0.880 -> 0.886, pixels 0.970 -> 0.971, no deck down (sc-dark-modern 0.739 -> 0.810, devfest2020
0.827 -> 0.857, sc-dark-minimal 0.864 -> 0.884, sc-memphis 0.831 -> 0.846). `adopt_shapes.pt` wrote
every length between -1 and 0 as positive until then (`"-0.67".replace("-0", "0")`).

## PowerPoint insets, mixed sizes, stand-in widths (`ffmain` -> `px-f`)

More from the thumbnails (`ffmain` -> `px-f`, 912 slides: boxes 0.923 -> 0.928, page 0.919 -> 0.924, no
deck down): a box measured 3.6 pt high has PowerPoint's top/bottom insets (`deck_ir.top_drift`,
`pptx_insets`, `KNOWN_CAPS` only: other faces' cap heights move the first ink as much), and a deck most
of whose measured boxes have them is a .pptx import whose boxes all do, sides too (`box.inset_x` 3.6:
comps-analysis 0.43 -> 0.66; ap-bio-stats' two lone ones keep Slides' sides); a paragraph of several
sizes is spaced line by line (per-word struts, the next paragraph from the depth TeX recorded); a
full-slide template picture under a box no longer hides its insets (`deck_ir.crossed`: devfest2020
0.857 -> 0.897); and a stand-in font is condensed to the widths the thumbnails show
(`deck_ir.ink_widths` measures each box's first line, `adopt.font_widths` holds it against the
stand-in's advances less the end bearings, median of >= 2 within 4%, applied as fontspec
`FakeStretch` when off by > 2%): comps-analysis's Libre Bodoni at 0.94, 0.66 -> 0.675. A deck's own
font is never stretched: Arial measures 0.987-1.002, and Pacifico's 0.967 was its kerning.

## Picture fills (`g24-final` -> `tp-e`)

Picture fills (`g24-final` -> `tp-e`, 912 slides: boxes 0.938 -> 0.949, no deck down): a `{}` fill that
is neither one colour nor a ramp - a photo cut to a freeform, a texture - carries no URL in the API and
was dropped with its shape; `deck_fills.thumbnail_picture` now writes the thumbnail's pixels in its box
as a picture, with the letters of texts above painted out (a pixel nearer the run's colour than the
box's ground, dilated, filled in from around; a looser colour test flattened sc-memphis' whole pink
band under its yellow words) and the page colour transparent when nothing else lies under it. A second
face now also gets its switch when its letters cover the page (`adopt.AREA_SIZE`: sc-memphis' 166 pt
section numbers were set in the body face). sc-memphis 0.847 -> 0.994, sc-dark-modern 0.827 -> 0.984,
sc-functions 0.960 -> 0.985. What that costs: the picture is at thumbnail resolution (1600 px across
the slide) and bakes in whatever lies under the shape.

## Hebrew and Japanese (`hj-head3` -> `hj-final2`)

Hebrew and Japanese (`hj-head3` -> `hj-final2`, same day, 912 slides: boxes 0.930 -> 0.935, page
0.926 -> 0.931, no deck down): `top_drift` reads a first line with no capitals by its baseline
(`deck_ir.baseline_drift`: Hebrew/Arabic only, the lowest row inked a quarter as densely as the
line's densest, underlines cleared), so hebrew-lesson's boxes join `pptx_insets` (its text stood
3.6 pt low); table cells keep `direction: rtl`; a cell's lines are Slides' pitch apart
(`adopt.cell_lead`: `\baselineskip` only - a full-pitch strut grew comps-analysis's rows). A run that
only names its font reads back `bold: false`, weight 400, under a bold parent, and Slides draws some
of those bold and some not with identical API data (jruby-ja's titles bold, drawings-basics' slides
9 and 11 regular): `weight_unsure`, settled by the thumbnail's stroke width
(`deck_ir.thumbnail_weights`, `stroke_em` > `BOLD_STROKE_EM` 0.10 em). hebrew-lesson 0.734 -> 0.879,
jruby-ja 0.756 -> 0.796, sc-dark-modern 0.827 -> 0.831. jruby-ja's rest: mixed kana/Latin lines set
1-2% wider, Tahoma Bold ~2.5% wider than Slides'.

## Pictures, pies, text spacing (`tp-e` -> `cp-a`)

Pictures, pies and text spacing (`tp-e` -> `cp-a`, 912 slides: boxes 0.949 -> 0.956, no deck down).
A picture's brightness, contrast and recolour are baked into the file adopt writes
(`compare.adjusted_picture`, recolour ramp first, then contrast, then Slides' measured brightness:
x(1+b) below 0, x1/(1-b) above): drawings-basics 0.942 -> 0.971. A PIE shape's dragged angles, which
the API does not give (the preset's 270 degrees drew), are read along rays from its centre in the
thumbnail (`deck_fills.pie_angles`: the circle less its largest gap, colours of pies above count as
hidden): intro-lecture 0.948 -> 0.974. And, from a study agent (`ca-base` = bc373b9 -> `ca-final2`;
creandum-board 0.923 -> 0.946, ap-bio-stats 0.925 -> 0.951): the gap between two paragraphs is the
bigger of spaceBelow and the next spaceAbove, not their sum (ap-bio-stats slide 52 0.47 -> 0.90), per
side for list items; spaces at a run's edges are kept however many and set in the run's own font
inside its style (ap-bio's literal "•  " Arial bullets in Calibri text, slide 36 0.56 -> 0.94); a
superscript's strut stands outside the script, or it raised its line box; a middle-aligned table cell
drops by its own line box (`\adoptdrop`) and a one-word cell wider than its insets stays on its line;
tabs in bulleted paragraphs. Tried and dropped: making the space where two faces meet the wider of
the two (it read jruby-ja's bold title, which the IR calls regular, as a Courier space, and wrapped
cs161-tls' `google.com` line). Line breaking was studied and needs nothing: Slides draws text kerned
and breaks on the kerned width of a line at exactly the box's width, as TeX does here (of 579 boxes
whose thumbnail lines can be counted, the rules tried - unkerned, a tolerance, pixel-rounded widths -
gain at most one box); the breaks that still differ are fonts unlike Slides' own.

## Drive video posters, glyph bullets (`cache-a` -> `vb-a`)

Then (`cache-a` -> `vb-a`, boxes 0.956 -> 0.958): a Drive video's poster frame, which no API gives,
is read off the slide's thumbnail (`settle`, `poster: thumbnail`) instead of a play panel, and a glyph
bullet counts as text for the fallback chain (`scripts.deck_text`: supercharge-slides' ➔, which Alegreya
lacks, came out as its .notdef cross). supercharge-slides 0.958 -> 0.986, drawing-workshop 0.982 -> 0.990.

## Tables from their thumbnails (`cache-a` -> `tb-final`)

Tables from their thumbnails (`cache-a` -> `tb-final`, 912 slides: boxes 0.956 -> 0.958, no deck down):
a stored row height is a minimum Slides grows by what the API does not say (an empty cell's line in
a size nobody reads back - solidity-survey's 15.75 pt rows draw 19.4, creandum-board's do not grow -
a .pptx's insets, a word broken in a narrow column), so `deck_ir.thumbnail_rows` reads each row
boundary whose borders cover half the table where the thumbnail draws it (the first pixel row
where 85% of the sampled columns turn towards the border colour *and* come back within its
thickness: a step between two fills is no line), top down until one is not found, and those rows
get the measured height and no growth in TeX (`rows_fixed` -> `\adoptfix`, 1 pt of slack before a
cell is set again without insets). `deck_ir.thumbnail_cell_pad` takes the side inset from where
single-aligned cells' ink begins (median of >= 3, less 0.06 em bearing: comps-analysis 3 pt, not
the 5.8 guessed, so "Implied Equity Value" stops wrapping), and `cell_pad` keeps the cap when the
rows are less than a point short of it (creandum-board's 22.0 pt rows of 7 pt text draw 22.8 =
8.4 + 2 x 7.2). comps-analysis 0.677 -> 0.722 (slide 10 0.335 -> 0.584, 7 0.458 -> 0.639),
creandum-board 0.946 -> 0.958 (13: 0.599 -> 0.956), solidity-survey 29 0.596 -> 0.910, hebrew-lesson
0.773 -> 0.779, journey-maps 15 0.826 -> 0.924. Tried and dropped: a 14 pt strut in empty cells
(fixed solidity, broke creandum 13 and comps 9). Left: comps-10's "Adjusted" + nbsp header wraps
the nbsp onto a line of its own in Slides; cells' vertical insets (comps' bottom-aligned text 2 pt
low, hebrew's 5.4); tables whose borders are not visible (creandum 13, cs161-tls) stay guessed.

## Right-to-left sides, line pitch snapping (`cache-a` -> `rtl-c`)

Right-to-left text and line pitch (`cache-a` -> `rtl-c`, 912 slides: boxes 0.956 -> 0.963, mean per deck
0.936 -> 0.944, no deck down by more than 0.0003). hebrew-lesson is a .pptx import like comps-analysis
(`pptx_insets` gave it 3.6 pt sides), but its right-aligned Hebrew starts 3.2 pt further from the box's
right edge than that put it: its boxes kept Slides' own sides. `deck_ir.side_gap` reads where a box's words
start from its start side in the thumbnail (the right edge for right-to-left text; no bullets, rows other
elements reach into left out), `side_inset` takes a 0.04 em bearing off, and a deck imported whole gets
3.6 pt sides only when the median is below `SIDE_SPLIT` 5.15 (comps-analysis 3.4, every deck with Slides'
sides 5.3-7.6): hebrew-lesson 0.773 -> 0.879. And single-spaced lines are a whole number of CSS pixels
apart (`adopt.snapped_line_box`, `emit.snap`): 24 pt lines 28.5 pt, not 28.8, 18 pt 21.75, 16 pt 19.5
(34 boxes in 7 decks), but not below `SNAP_FROM` 16 pt (gdg24's 14 pt Google Sans Text, journey-maps'
14 pt Montserrat, cs161-net's 8 pt Arial stand 1.2 em apart - Arial at 14 pt snaps, so the line is where
the data is), not at other spacings, and not on pages over `deck_ir.SNAP_PAGE` 960 pt (box `snap`: the
1440 pt SlidesCarnival decks' Inter and NTR lines are 1.2 em apart; snapping them cost sc-dark-minimal
0.002, gdg24 0.002 at 14 pt). hebrew-lesson -> 0.903, comps-analysis 0.677 -> 0.722, ap-bio-stats
0.951 -> 0.976, journey-maps 0.913 -> 0.938, ds-lecture +0.004, arabic-training 0.907 -> 0.908. Why
Slides snaps some lines and not others is not known.

## CJK faces (`cache-a` -> `cjk-final`)

CJK text (`cache-a` -> `cjk-final`, 912 slides: boxes 0.956 -> 0.958, mean per deck 0.9356 -> 0.9372, no
deck down; jruby-ja 0.796 -> 0.806, apps-edu-zh 0.913 -> 0.949). CJK letters no longer count against
the coverage of the font they are typed in (`adopt.chain_letter`: they come from the fallback chain
whatever the run says), so jruby-ja's Arial is the main font again instead of Tahoma, 4% wider. Every
face of a .ttc is found (`scripts.faces` closed the shared file after face 0; `font_candidates` names
each face, fontspec `FontIndex=`): apps-edu-zh's MS PGothic is face 2 of msgothic.ttc. A CJK font Slides
does not have (not on google/fonts, not `SLIDES_CJK`) is drawn by Slides as Times New Roman for its
Latin and the renderer's Noto for its ideographs (`adopt.slides_lacks_cjk`: apps-edu-zh's Microsoft
JhengHei, lines 0.995-1.021 of that model's widths against 0.885-0.944 of JhengHei's own), and Chinese
gets `palt` like Japanese. Only CJK faces: intro-lecture's CMTT9, which Slides lacks too, is drawn in a
monospace. Left: jruby-ja's kana are narrower than Noto Sans JP 2.004's palt and its ideographs ~4%
smaller (another Noto/Source Han version, none on google/fonts matched), its gradient backdrop.

## Tables, right-to-left, CJK and video posters together (`vb-a` -> `m3-a`)

The four branches above merged (`vb-a` -> `m3-a`, 912 slides): boxes 0.958 -> 0.967, page 0.966, pixels
0.984, mean per deck 0.937 -> 0.949. Their gains add up almost exactly: hebrew-lesson 0.773 -> 0.908,
comps-analysis 0.677 -> 0.767 (tables and sides together, more than either alone), apps-edu-zh 0.913 ->
0.949, journey-maps 0.913 -> 0.942, ap-bio-stats 0.951 -> 0.976, creandum-board 0.946 -> 0.958. No deck
down by more than 0.0003 (gdg24 slide 57 -0.014).

## Insets read by glyph tops and bearings (`mv-a` -> `df-c2`)

Text boxes with no insets, read from more of the thumbnails (`mv-a` -> `df-c2`, 912 slides: boxes 0.9674 ->
0.9704, page 0.9688, pixels 0.9841, mean per deck 0.9488 -> 0.9527, no deck down). devfest2020's
PowerPoint-made template sets most text boxes' insets to 0, which the API does not report, and
`thumbnail_insets` found almost none of them, for four reasons. (1) A big first glyph's side bearing was
more than half the inset: the deck's own font, where it is at hand under its own name
(`deck_thumbs.face_glyphs`: fontTools bounds from the files `adopt.font_family` finds, never a stand-in's),
now gives the first glyph's bearing (`starting_bearing`) to add to the gap. (2) Centred, right-aligned and
bulleted boxes were skipped, having no side edge to read: `inset_rows` reads their rows instead. It sets the
first line (the last, bottom-aligned) where `adopt.text_box_latex` would set it, as `snapped_line_box`,
spaceAbove and the bottom depth give it, takes the ink top (bottom) of that line's glyphs from the font,
and reads the first inked row of the band where the words stand. Within `INSET_TELL` 0.35 of the inset of
the no-inset position it answers 0, near Slides' own 1, else nothing. (3) The test that lets a panel under
a box count as ground wanted the panel to hold the box's whole width: devfest2020's "50%" runs 800 pt off
the slide, its "Slide Formats" and "Icons & Assets" are full width, and its lists run 2 pt past their
panel. It now wants the panel to hold only the strip being read, with a 1 pt margin (`crossing`). (4) The
anchor-based prediction of the first baseline disagreed with what adopt draws for lists at lineSpacing
1.4-1.5, where the thumbnail's rows fell between the two predictions. A box whose rows say 0 loses its
top (bottom) inset as well as its side ones, and its anchor moves by BASELINE_A.

devfest2020 0.903 -> 0.968: slide 2 "Colors" 0.81 -> 1.0, 8 "Slide Formats" 0.81 -> 1.0, 9 "Full screen
slide" 0.72 -> 1.0, 11 "Item One" 0.52 -> 0.99, 13 "50%" 0.71 -> 0.94, 23 "Charts" 0.83 -> 1.0, 34 "Icons &
Assets" 0.80 -> 1.0, 30 0.86 -> 1.0, 32 0.88 -> 1.0, 24 0.88 -> 0.98. poster-48x36 0.701 -> 0.749 (a .pptx
import: its centred Arial headings are read by their rows now), gdg24 +0.0008, firebase-jam +0.0002.

The first full run (`df-c1`) cost ap-bio-stats 0.003 and arabic-training 0.002, both fixed. ap-bio-stats'
slide 27 lost 0.19 because its full-width box starts 1.9 pt left of the slide: the crop starts at the
slide's edge, but the gap was counted from the box's, so Slides' own inset read as 1.9 pt too small (the
old enclosure test had hidden this). arabic-training's right-to-left lists (8, 9) read as inset-free
because Arabic is shaped: a joined letter is not the glyph its code point maps to, so the cmap's heights
say nothing of the line's tops. Rows are not read for Arabic or Hebrew letters.

Tried and dropped: taking every text box of devfest2020 as inset-free (0.964: slides 1, 3 and 33 went down,
since some of its boxes keep Slides' insets); predicting the first baseline from the IR's anchor (the lists
fell between the two predictions, so it was replaced by adopt's own line model).

Left:
- devfest2020 slide 10, numbered list, 0.51. The rows are right now, but the numbers and the words both
  stand 7 IR pt (about 29 Slides pt, about indentFirstLine) left of the deck's. Deck: number at 55.2 and
  words at 85.25, in a box at 57.4 with indentStart 85.4 and indentFirstLine 29 (Slides pt). Ours: 48.1
  and 78.45. Slide 11's bulleted list, indentStart 36 and indentFirstLine 18, is right.
- Slide 33's ROUND_RECTANGLE captions (0.78) have no outlines or shadows, and the text sits wrong in the
  shape's text rectangle.
- Slide 1's subtitle is kept at the default insets: its rows fall between the two predictions, 0.57 pt
  high.
- Slide 26's "Short Label" boxes stand on diagram shapes that cross the band, so they are not read.
## Cell baselines, a deck's vote on PowerPoint insets (`mv-a` -> `tc-all`)

Cells' vertical placement and right-to-left boxes (`mv-a` -> `tc-all`, 912 slides: boxes 0.9674 -> 0.9689,
mean per deck 0.9488 -> 0.9516, no deck down; arabic-training 0.908 -> 0.941, comps-analysis 0.767 ->
0.790, poster-48x36 0.701 -> 0.712, hebrew-lesson 0.908 -> 0.917, journey-maps 0.942 -> 0.946).
Measured on the thumbnails, a top-aligned cell's first baseline stands T + 0.968 em under its row's
top and a bottom-aligned one's last baseline T + 0.232 em over its row's bottom, T one number per
table: hebrew-lesson 1.45 pt (1.8 guessed), comps-analysis 1.0 in both of its kinds of table (0.7 and
1.6 guessed). TeX anchored the cell's box by its strut, 0.84 em over the baseline, at row + `pady`,
hence 5.4 pt and 2 pt off. `deck_thumbs.thumbnail_cell_text` reads T (`cell_text_y`) from cells
with one paragraph in rows `thumbnail_rows` placed: the first (last) band of inked rows, and its
baseline read as `baseline_drift` reads it, median of >= 3. `adopt.table_block` then places the box
by that baseline: `\adoptht` is from the box's top to its first baseline and `\adoptdp` its depth.
A vbox's height reaches its *last* baseline, and the colour whatsit at either end of a cell's box
stops both a single `\vsplit to 0pt` (it splits off only the whatsit, and text sat 10 pt low: comps
0.767 -> 0.636) and a `\lastbox` count, so `\adopt@first` splits pieces off the top until one holds
a line. comps-analysis 6 +0.121, 7 and 12 +0.074; hebrew-lesson 11, 17 and 21 +0.038; residual cell
error 0-0.45 pt. arabic-training's boxes, Latin ones included, all stood 3.6 pt low: PowerPoint
insets that `pptx_insets` never gave it. Two of its four readings were Calibri Arabic at -2.3, just
outside the -3.6 +- 1.2 window: the densest row of the fallback font's letters stands a little under the
baseline. Its bulleted boxes, most of the deck, were not measured at all. Now a bulleted first
line is measured by its baseline (Arial -3.7, Times -4.0; no other deck's readings change), and
whether a deck came whole is a vote: of the readings within 6 pt (an 8 pt one misread the line), >= 60%
must be nearer -3.6 than 0. The window still decides a measured box alone. That makes
arabic-training whole (4 of 4), and poster-48x36 too (7 of 8, -3.1 to -5.1), which gains as well;
no other deck changes (cs161-net's 96 readings are near -9, ap-bio-stats 3 of 51). Tried and dropped: a vbox's `\ht` as the
first line's height (multi-line cells drawn above the table, hebrew 7 -0.036); one `\vsplit`; a
`\lastbox` line count (see above). Left: hebrew-lesson 7's body cell wraps one line more than Slides
(a width, not a placement); arabic-training's two Calibri Arabic boxes measured at -2.3 keep Slides'
insets, and their last lines do match the deck, so what Slides does with them is not known; the
Arabic first lines stand 0.5-1.7 pt apart from the deck, fallback glyphs taller than the line.

## Both merged (`m4-a` -> `m5-b`)

The two studies above together: boxes 0.9704 -> 0.9719, page 0.9703, pixels 0.9844, mean per deck
0.9551. They met in one place: the devfest study's side readings added a -3.1 pt one for
arabic-training (a box whose words cannot start outside it), which took the deck's median side from
5.5 to 2.3 and gave it PowerPoint's 3.6 pt sides (0.941 -> 0.917). `pptx_insets` now drops side
readings below 0; nothing else moved.
