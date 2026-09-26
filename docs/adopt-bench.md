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
## The single text boxes that lose most (`mv-a` -> `sb-final`)

The worst text boxes outside tables (`mv-a` -> `sb-final`, 912 slides): boxes 0.9674 -> 0.9684, page
0.9663 -> 0.9669, mean per deck 0.9488 -> 0.9494. Three mechanisms:
- A middle- or bottom-aligned box stacks its last paragraph's spaceBelow under its last line
  (`adopt.trailing_space`). intro-lecture's title "add(add(6, ...))" / "???" (10 pt below each) stood
  5 pt low without it: intro-lecture 0.9736 -> 0.9837 (slide 30 +0.32), jeb-arch 0.9496 -> 0.9509.
  gdg24's turned ELLIPSE stickers (10 pt below too) do not show it: their words stood within 0.8 pt of
  the thumbnail's without it and 1.2-1.9 pt high with it, so ELLIPSE is left out.
- `deck_thumbs.thumbnail_insets` reads a box whose words overflow it where they stand
  (`deck_thumbs.text_rows`: 1.19 em x line spacing per paragraph, spilling as the alignment lets them).
  gdg24's code listings (15 lines of 9.45 pt in a 63 pt middle-aligned box) show only indented lines
  inside the box, so the ink started 23 pt in and the box kept its insets.
- The measure is scaled as the words are (`adopt.measure`). IR sizes are page pt to 0.01, so gdg24's
  15 pt code is set at 9.45 instead of 9.44875. Its 69-character line is 621.0 slide pt in a 621.0 pt
  box, one line in Slides and two in TeX. With the box's insets both, gdg24 79 went +0.17 and 78
  +0.09 (deck 0.9845 -> 0.9871), and cs161-tls 0.9892 -> 0.9924. A flat 0.06 pt slack instead cost
  sc-dark-minimal 7 0.27: "About Us." (27.714 pt, set at 27.71) is 0.04 pt wider than its box in
  Slides and fitted in TeX.

Down: comic-strips 0.9805 -> 0.9790 (slides 11 -0.018, 8 -0.008). "However, I recommend using images in
PNG" in Arial is 598.58 slide pt kerned (600.23 unkerned) against 598.6 of room. Slides wraps it; the
measure now 0.05 pt wider fits it. That is 0.02 slide pt, below what the IR's 0.01 page pt edges can
tell apart.

Tried and dropped:
- sc-dark-minimal 11's "Our Projects." (the worst box, 0.67): Slides wraps it and TeX does not. Its
  first line's ink matches Inter at opsz 14 (Slides' Inter 4), and Inter 3.19 did not change the
  break. Newline-as-space and the other break rules tested over the corpus's measured lines did not
  wrap it either. Breaking on unkerned widths is the only rule that wraps it (and comic-strips 11), but
  the corpus as a whole does not support it: other lines that Slides keeps would wrap.
- Bodoni Moda as comps-analysis' stand-in is no closer than Libre Bodoni at 0.94. The deck's face has
  a smaller cap height and is lighter, so the headings lose on glyph shape, not width.

Left:
- comps-analysis 10's sub-bullet wraps because its italic is condensed with the upright's measured
  width; italic width is not measured on its own.
- creandum-board 23's justified line is ~1 pt overfull in TeX where Slides wraps.
- The Google Sans Mono vs Google Sans Code glyph offsets on gdg24 77-79.

## All three merged (`m5-b` -> `m6-a`)

With the single-box study on top: boxes 0.9719 -> 0.9728, page 0.9714, pixels 0.9845, mean per deck
0.9557. The gains add up (intro-lecture +0.010, cs161-tls +0.003, gdg24 +0.003, jeb-arch +0.001);
comic-strips -0.0015 is the study's known cost (a line 0.02 pt short of its box that Slides wraps).
Left, by the loss report: jruby-ja (kana widths, gradient), comps-analysis' Bodoni glyph shapes,
devfest2020's numbered lists, sc-dark-minimal 11's unexplained wrap.

## Readability: the second score (`m6-a` baseline)

Fidelity says the PDF looks like the deck; it says nothing of whether the source is one a person would
keep. `devtools/readability.py` scores the frame bodies of any .tex against sources people wrote
(`REFERENCE`: the test decks and the sync talk, medians from `readability ref`) - lines per frame,
numbers and non-author commands per word, characters per visible character, the share of author
commands, and the lines repeated on 3+ frames (what a theme or a macro should say once) - each as
min(1, human/ours), geometric mean. It reads the trees a run leaves, so it needs no compile:
`python -m beamer2slides.devtools.readability report --tag T [-v]`. Hand-written sources score
0.6-1.0 (the test decks that are torture tests lowest: 16_colored_table 0.36); adopt at `m6-a`
scores 0.134 (lines 0.09, numbers 0.05, plumbing 0.03, bloat 0.34, author 0.33, repeat 0.61).
Frame body lines by construct: text plumbing 36%, `textblock` placement 25%, shapes 23% (traced
freeforms: sc-memphis 922 lines a frame), words 6%. Plan: first what leaves the PDF unchanged (a
macro layer and a recovered beamer theme for masters and layouts, gated on identical renders), then
structure (itemize, frametitle, tabular), gated on fidelity per deck.

## The deck's theme as a beamer theme (`m6-a` -> `th-final2`)

`adopt_theme.py`: what the masters and layouts draw goes into a `beamertheme<Deck>.sty` beside main.tex
(`\usetheme{<Deck>}` after the packages). `deck_ir(foreign=True)` now also records each slide's
`layout` and the IR's `layouts` (display name, master, page colour or picture: `deck_ir.layouts_of`).
- Each layout the slides use is a `background` template named after its display name
  (`title-and-body`, a second one with the same name `title-and-body-2`). A frame picks it with
  `\begin{frame}[plain,layout=title-and-body]`. What a master draws for several layouts is written
  once, as `\defmaster` / `\drawmaster`.
- The layout's page colour or picture comes with it. A slide with a page of its own says
  `background=<colour>` or `backdrop=<file>`. That replaces the old `{\setbeamercolor ...` group
  around the frame.
- TITLE/CENTERED_TITLE, SUBTITLE and SLIDE_NUMBER placeholders become part of the template. The frame
  says `\frametitle{...}` / `\framesubtitle{...}`, and the number is `\insertframenumber`.
  - `nonumber` marks a slide of a numbered layout that shows no number.
  - `\frametitle` works because beamer builds the `background` template in the output routine, where
    `\beamer@frametitle` is still set.

The rule is that the PDF does not change, so nothing is guessed:
- A template is the exact LaTeX the elements were already written as. For each layout, the
  most common variant of its inherited pieces wins. A slide whose pieces differ in any character keeps
  them in its frame (one cs161-net "Title only" slide, whose gradient came out differently).
- A placeholder's prefix and suffix are found by writing it again with marker words at both ends
  (`slot_parts`). The slot is templated only when the real output is prefix + words + suffix, and the
  words are one line with no `%`, `#` or unbalanced brace.
- A number is templated only when it equals the frame's number.
- The template draws under everything the frame draws (textpos ships frame blocks in the page's
  foreground). So a title moves only when none of the slide's elements drawn before it touch its
  box (1 pt margin).
- Pieces with a raw `#` stay out (inside `\defbeamertemplate` it would be a parameter).
- Placement: textpos in relative mode inside a `\vbox to 0pt` at the page corner lands exactly where
  the absolute block did.
- Frame options last until something resets them, and `env/frame/after` never fires, so
  `env/frame/before` resets them.
- `\setbeamercolor` inside a frame key runs a `\setkeys` of its own, which ended the frame's option
  list (`keyval Error: nonumber undefined`). `\deck@keys` saves and restores `\KV@prefix` around it.

Fidelity: all 912 slides score exactly as at `m6-a`, `boxes`, `page` and `pixels` alike (0 differ).

Readability, `m6-a` -> `th-final2`:

| | score | lines | numbers | plumbing | bloat | author | repeat |
|---|---|---|---|---|---|---|---|
| `m6-a` | 0.134 | 0.09 | 0.05 | 0.03 | 0.34 | 0.33 | 0.61 |
| `th-final2` | 0.139 | 0.11 | 0.06 | 0.03 | 0.34 | 0.31 | 0.62 |

- Frame body lines went from 104,736 to 96,133 (-8.2%). The theme files hold 3,689 lines in all.
- 894 of 912 frames name a layout. 316 titles and 13 subtitles are `\frametitle` / `\framesubtitle`.
- Largest cuts: cs161-net -1,881 lines (168 -> 137 a frame), cs161-tls -1,431, solidity-survey -711,
  creandum-board -595, hebrew-lesson -566, devfest2020 -549, apps-edu-zh -497.
- Per-deck scores: ds-lecture 0.140 -> 0.155, cs161-tls 0.142 -> 0.158, apps-edu-zh 0.153 -> 0.174,
  comic-strips 0.156 -> 0.178, hebrew-lesson 0.159 -> 0.176.
- gdg24, sc-memphis, sc-aesthetic-school, sc-dark-minimal, jeb-arch and poster-48x36 do not move.
  Their slides inherit nothing and have no placeholders (.pptx and Canva imports): their theme is
  pasted onto every slide as the slide's own elements.

Where the proxy mis-scores this:
- `author` drops (0.33 -> 0.31; devfest2020 0.33 -> 0.31, instagram 0.47 -> 0.28). The decoration
  that left the frames was tikz `\path` and `\includegraphics`, which count as author vocabulary, so
  the text plumbing that stays is a larger share.
- `repeat` hardly moves (0.61 -> 0.62). Removing the repeated decoration also shrinks the denominator,
  and the lines still repeated on 3+ frames are text-box plumbing (`\vskip4.08bp`, the `\leftskip`
  lines), which is the text layer's job.
- The theme file is not scored at all. That is intended: it is written once, not per frame.

Left:
- BODY placeholders stay in the frame (several paragraphs, not one stretch of words): they need the
  text layer's named styles first.
- Slide-owned elements repeated across slides. Surveyed: in cs161-net 44% of own lines repeat on 3+
  slides, but they are build-up slides that copy the previous one's content, not decoration (beamer
  overlays would be the fix). In gdg24/sc-memphis they are text boxes.
- Two layouts that draw the same (two "Title and body" in cs161-net) still get a template each.
## A macro layer, style and colour names (`m6-a` -> `ma-final`)

The first half of that plan, for text boxes and shapes only, with no change to structure. adopt now
writes `slides.sty` beside main.tex (`\usepackage{slides}`), the vocabulary its frames are written in:

- `slidebox` `[middle|bottom, inset=, tail=]{x,y,w,h}` holding `\slidepar[center|right|justify, indent=,
  rindent=, first=, space=, prevdepth=, mixed, lang=]{style}{words}`, and `\slidetext[...]{x,y,w,h}{style}{words}`
  for a box of one paragraph, which covers most boxes. Everything the spelled-out paragraph had (the
  `\vbox to`, `\vss`, `\leftskip`/`\rightskip`/`\parfillskip`, the strut, `\baselineskip`, the
  `\prevdepth` arithmetic, `otherlanguage`) is in the macros.
- `\slidestyle{name}{size=, family=, face=, weight=, italic, color=, ascent=, pitch=, depth=}` in
  main.tex's preamble. The name comes from what sets the style apart: its size next to the deck's
  body size (title, heading, large, body, small, tiny), then typeface, weight, slant and colour
  (`title-serif-white`, `body-mono-italic`, `label-darkred` for a bullet), with a size or number added
  when two styles would share a name.
- Colours get names a person would give them (`Blue`, `DarkGrey`, `NearBlack`, `LightYellow`, `Blue2`
  for a second blue; the most used one takes the bare name), not `b2s4285F4`. `\setslideinset` sets the
  deck's usual box inset once, so boxes that use it do not repeat it.
- Drawn bullets are `\slidemark{dot-red}{<tikz>}` once and `\slidebullet{dot-red}{gap}` per line;
  typed ones are `\slidelabel{style}{text}{gap}`; soft breaks are `\slidebreak`.
- Shapes: `\slideshape{x,y,w,h}{paths}` on one line in place of textblock + tikzpicture + bounding box.
  `\sliderect[opts]{x,y,w,h}` and `\slideellipse[opts]{x,y,rx,ry}` are used only when the path the
  writer built is exactly the one the macro builds from those numbers. A zero `shift` is dropped.
- Numbers lose padded zeros (`12.5`, not `12.50`), which are the same length to TeX. Nothing is rounded
  beyond what was written before.

texmap reads the words in a `\slidepar` / `\slidetext` and skips `\slideshape`, so pull still finds
them. The readability constructs count the new macros as what they replace.

**Fidelity unchanged, to the pixel.** `ma-final` has boxes 0.9728, page 0.9714, pixels 0.9845, with every
slide's three scores equal to `m6-a`'s. All 912 compiled pages are pixel-identical to `m6-a`'s at 3x zoom.
It took one fix to get there. `\par` removes the last glue of a paragraph. Spelled out, the line end
after the words gave it a space to remove. `\slidepar{..}{words}` has nothing after the words, so a
trailing tie was removed instead. That moved arabic-training 18's "Meeting~~~~~~" line and
comps-analysis 13's underlined "Pros ~ ~" heading (-0.001). Words that end in glue now end in a space.
Other equivalences the macros depend on:
- the words are read as a group, not an argument, so their catcodes stay live;
- a paragraph ending on a tab gets a trailing space, for the same reason;
- a bounding box of a slightly different size moves no ink, because the picture hangs from its
  top-left corner.

`tests/test_adopt_macros.py` compiles the short forms beside their spelled-out forms and compares the
pages.

**Readability 0.134 -> 0.258** (29 decks, `readability report --tag ma-final`):

| component | `m6-a` | `ma-final` |
|---|---|---|
| lines | 0.09 | 0.33 |
| numbers | 0.05 | 0.07 |
| plumbing | 0.03 | 0.14 |
| bloat | 0.34 | 0.42 |
| author | 0.33 | 0.48 |
| repeat | 0.61 | 0.85 |

Per frame, without the proxy's denominators:
- lines 114.8 -> 31.9;
- characters 8380 -> 4417;
- numeric literals 434 -> 307;
- commands 256 -> 65.

main.tex totals 7.80 MB -> 4.24 MB, plus 14 kB of slides.sty per deck. Frame body lines by construct:
- text plumbing 37,400 -> 1,727;
- placement 25,974 -> 6,532;
- shapes 23,936 -> 6,130;
- text went from 6.1% to 25.8% of all lines. The words are unchanged; `\slidepar` lines count as text.

**Where the proxy misleads.** It counts the words of the macro vocabulary as visible text: environment
and style names (`body-serif-white` is three words), `hebrew`, `center`, `ellipse`, `radius`. That
inflates the denominator of numbers, plumbing and bloat. Taking plumbing out shrank the "words" per
frame from 160 to 93 while the deck's words stayed the same. So numbers per word barely moved (0.05 ->
0.07) although there are 29% fewer numbers. `\begin`/`\end` count as author vocabulary, so a slidebox
still scores as an author command while `\slidetext` does not. No numbers were tuned to the proxy.
Numbers per frame and lines per frame are the honest measures.

How it got there, on devfest2020, gdg24 and hebrew-lesson, each step pixel-identical to the base:

| tag | readability | what it added |
|---|---|---|
| `ma-base` | 0.131 | none |
| `ma-1` | 0.218 | the slidebox environment, styles and colour names |
| `ma-2` | 0.213 | `\slidetext`, `\sliderect`, `\slidebreak` |
| `ma-3` | 0.212 | `\slideellipse` |

The last two made the source shorter and still lowered the score, for the proxy reasons above. They
are kept.
Numbers are not rounded beyond what the writers already printed. Only padded zeros go.

Left, outside this change:
- numbers, now 307 a frame: shape paths (sc-memphis' traced freeforms make it the worst deck, 0.091),
  rounded rectangles, whose `.. controls` corners are not yet a macro, and table cells;
- pictures, still `textblock*` + `\includegraphics` (8% of lines);
- tables (`\adopt...`, 11%);
- structure (itemize, frametitle, masters and layouts as a theme), which is another change's work.

## Theme and macro layer together (m6-a -> mt-a)

Both merged: 912 slides score as at m6-a on every slide (boxes 0.9728, page 0.9714, pixels 0.9845).
The theme file and main.tex share one colour naming (`adopt.rename_colours` runs over both).
The readability proxy no longer counts a style name handed to `\slidetext`/`\slidepar` as words, nor
`\begin`/`\end` as author vocabulary (an environment swapped for a macro says no less); HUMAN refreshed
from `ref`. On that scorer: m6-a 0.130 -> mt-a 0.253 (lines 0.09 -> 0.38, numbers 0.05 -> 0.07,
plumbing 0.04 -> 0.17, bloat 0.33 -> 0.38, author 0.19 -> 0.36, repeat 0.61 -> 0.84).
Frame body lines now: text 23%, placement 22%, shape 21%, table 12%, picture 7%, plumbing 6%.


## Lists and paragraph defaults (mt-a -> li-full)

Slides lists are written as lists: a slidebox's bulleted paragraphs are `itemize` / `enumerate`
(nested by list level), which `slides.sty` redefines inside a slidebox so every item is still a
`\slidepar` with its bullet hung where Slides hangs it. What an item of a level looks like - style,
indent, first-line shift, bullet (`mark=` a drawn dot/ring/square, or `label=` typed text in
`labelstyle=`) and gap - is said once per deck, `\setslidelist{itemize}{2}{...}`, from the majority of
the deck's items at that level (`adopt.deck_text_survey`); a list says what its own items share
(`\begin{itemize}[gap=0.91]`), an item only what it alone differs in (`\item[style=tiny] words`).
Numbered lists count: a glyph `3.`, `b)`, `iv.` becomes `label={\arabic*.}` etc. with `start=`, and a
number the counter would not print stays a typed `label=`. The deck's usual paragraph style is
`\setslidepar{style=...}`; a slidebox takes paragraph keys as defaults for its paragraphs
(`\begin{slidebox}[style=body-bold,space=2.42]`), chosen where saying them once costs fewer keys.
`\slidepar[options]{words}` takes its style as `style=`, and `space=` is now only what Slides adds
beyond the two styles' line boxes (the macro works the step out from the previous style's pitch and
ascent), so the same space between items of one list is said once or not at all. One-paragraph boxes
keep `\slidetext` unchanged. Nothing went back to the old form: no deck or level needed it.

Fidelity: all 912 slides score exactly as at mt-a - boxes, page, pixels and every element's loss equal
to the scorer's precision on every slide of all 29 decks (boxes 0.9728, page 0.9714, pixels 0.9845).
Found on the way: a list nested in a bold item came out bold (its items' styles only say what differs
from the box's font), so a nested list first goes back to the box's font and colour.
`tests/test_adopt_macros.py` compiles lists and defaults beside every paragraph spelled out and
compares the pages pixel for pixel.

Readability 0.253 -> 0.279 (lines 0.38 -> 0.36, numbers 0.07 -> 0.08, plumbing 0.17 -> 0.22, bloat
0.38 -> 0.40, author 0.36 -> 0.46, repeat 0.84 -> 0.84). Every one of the corpus's 1,270 bullets
(806 `\slidebullet`, 464 `\slidelabel` in frames at mt-a) is now an `\item`, 1,003 of them with no
options; 574 lists, 46 `\setslidelist`, 29 `\setslidepar`, 286 slideboxes with defaults. The proxy
counts the `\begin{itemize}` / `\end{itemize}` lines a list adds as lines per frame, which is why
lines drops while the text reads more like beamer (cs161-tls 0.291 -> 0.327, supercharge-slides 0.334
-> 0.397, ds-lecture 0.364 -> 0.545). The readability scorer's `STYLE_ARG` no longer reads the words
of `\slidepar{words}` as a style name (it still drops the name in the older `\slidepar[..]{style}{words}`,
so older runs score as before).

Left: `lang=` on every RTL `\slidetext` (hebrew-lesson, arabic-training; a deck default for
one-paragraph boxes would need `\slidetext` to take one); `prevdepth=` after a paragraph of mixed sizes;
the per-deck gap and indent numbers themselves (Slides' measures, not beamer's).
## Tables, pictures and shapes in the same vocabulary (`mt-a` -> `tsp-c`)

What `mt-a` left: tables were a tikz grid of `\adoptcell`s (12% of all frame lines), pictures a
`textblock*` around an `\includegraphics` (7%), and a shape's path was spelled out in full, rounded
corners as `.. controls` and a turn as a `cm` matrix. All three now read as what they are:

- `\begin{slidetable}[...]{x,y}{w1,...,wn}` with rows written as in a tabular, `a & b \\`. The column
  widths are said once; a row is `\row[h=, style=, ...]` only where it differs from the table's own
  options, a cell `\cell[fill=, valign=, ...]{...}` only where it differs from its row's, and
  `\multicell{k}[rows=r]{...}` spans. Borders are the table's `border=` plus `\hborder{i}[a-b]{style}`
  / `\vborder{j}...` after the last row for the few lines that differ, `none` for one Slides does not
  draw. `aligns={...}` gives each column its alignment. What was 10 `\adoptrow` + 10 `\adoptfix` + 40
  `\adoptcell` blocks in a `textblock*` (comps-analysis' TSM table, 146 lines) is 12 lines, one a row.
- `\slidepicture[trim=, angle=, flip, opacity=, outline=, outline width=, dash=]{x,y,w,h}{file}`:
  one line per picture, its Slides edits as named options, expanding to exactly the
  `\includegraphics` / tikz node / `\reflectbox` / `\rotatebox` order `inverse.picture_block` wrote.
- `\sliderect[rounded=r]`, `rotate=`/`flip` on any shape (about its own centre, so the numbers stay
  the box a person reads), `\slideline[options]{x1,y1}{x2,y2}` for the 0.01 bp boxes that were lines,
  and `\slidefreeform[...]{x,y,w,h}{file}` for a traced outline, whose hundreds of points live in a
  file of their own (`shapes/freeform-<sha8>.tex`), as a picture's pixels do.

texmap reads a cell's words like a paragraph's (`"cell": "oM"`, `ENV_ARGS["slidetable"]`), so `pull`
still finds them; the readability tool's CONSTRUCTS learned the new names (line classification only,
no score component touched).

**Fidelity: every one of the 912 slides scores exactly as at `mt-a`** - boxes 0.9728, page 0.9714,
pixels 0.9845, +0.0000 on all 29 decks and on every slide. 768 of the 912 compiled pages are
pixel-identical at 2x zoom; of the 144 that differ, 141 differ by at most 20 levels of 255 and the
whole corpus holds 2,968 pixels that differ by more than 8. Every one was tracked down:

- 2,924 of them are on three pages (sc-memphis 1 and 6, solidity-survey 10) and are not this
  converter's. Nothing in their geometry moved (chars, paths and image matrices equal): lualatex
  writes unscoped cumulative `cm` translations, so a picture's page x is the float32 composition of
  every translation before it, and moving a shape earlier on the page changes it by 1.5e-4 bp. That
  shifts which source pixel PDFium samples where the picture is scaled down, at every zoom.
- The rest are antialiasing: a rounded rectangle's corner arcs. TikZ's `rounded corners` uses the
  exact 0.5523 kappa where the old writer printed its control points to 2 decimals, so a control
  point moves by up to 0.017 bp (bounds by 0.004) - 44 pixels over 8 levels in the corpus.
- The same rectangle written from another corner, its coordinates agreeing to 0.006 bp: the rasteriser
  covers its edges a level differently (journey-maps, supercharge-slides, jeb-arch, comic-strips,
  firebase-jam - 20 to 200 pixels a page, none of them over 2 levels).

Two things the pixel comparison found, which the score could not (both fixed, both real):
- A **dashed** rounded rectangle's dash pattern runs from where the path starts. TikZ's `cycle` starts
  it one arc later than the spelled-out path did, which moved every dash on intro-lecture 29 and 33 by
  a third of a period (138 levels along the whole border). `\sliderect[rounded=]` now writes the path
  from halfway up the left edge, where the top left corner's arc begins, as the old writer did.
- `\slides@t@check` (a cell of one paragraph that came out one line too wide is set again without its
  insets, for creandum-board's numbers) measured the line unset. journey-maps 15's `10^6 * X` is 0.2 bp
  over its column and has 0.1 bp of shrink in each of its two spaces, so TeX had fitted it and the check
  widened the cell anyway. It now asks TeX to box the line to the room and looks at the badness.

**Readability 0.253 -> 0.268** (29 decks): lines 0.38 -> 0.52, numbers 0.07 -> 0.11, plumbing 0.17,
bloat 0.38 -> 0.58, author 0.36 -> 0.16, repeat 0.84. Per frame, without the proxy's denominators:

| | `mt-a` | `tsp-c` |
|---|---|---|
| lines a frame | 30.4 | 20.0 |
| numeric literals a frame | 299 | 79 |
| characters a frame | 4,321 | 1,609 |
| main.tex, all 29 decks | 4.19 MB | 1.71 MB |

Frame body lines by construct: table 12.0% -> 2.3%, placement 21.9% -> 9.1%, text plumbing 6.2% ->
1.0%, shape 20.7% -> 30.2%, text 22.9% -> 32.4%, picture 7.4% -> 11.9% (the shares of what is left;
in lines, shapes went 5,727 -> 5,515 and tables 3,322 -> 420). Biggest per-deck gains: hebrew-lesson
0.214 -> 0.451, comps-analysis 0.098 -> 0.282, creandum-board 0.097 -> 0.231, sc-functions 0.109 ->
0.209, journey-maps 0.120 -> 0.178, solidity-survey 0.254 -> 0.350.

**Where the proxy misleads**, again in the direction of punishing the change:
- `author` (the share of commands that are author vocabulary) falls 0.36 -> 0.16, because `\path`,
  `\includegraphics` and `\begin{textblock*}` are author vocabulary to it and `\sliderect`,
  `\slidepicture` and `\slidetable` are not. It is the one component that *drops*, and it drags six
  decks' scores down although their sources are half as long: sc-memphis 0.091 -> 0.063 (235 -> 168
  lines a frame, 12.9 -> 2.0 numeric literals a word), sc-river-a4 0.188 -> 0.110, sc-dark-minimal 0.269 -> 0.158,
  sc-aesthetic-school 0.321 -> 0.207, firebase-jam 0.530 -> 0.431, instagram 0.443 -> 0.394. Those are
  the decks that are almost entirely pictures and traced freeforms. Nothing was tuned to this: the
  scorer is unchanged apart from which construct a line is counted under.
- `numbers` moves 0.07 -> 0.11 for a 74% cut in numeric literals, because the words it divides by
  shrank too (option and column names are not words).

Left:
- shapes are now 30% of the lines: a diagram's own path (`\slideshape{...}{\path ... -- ...}`) is still
  a list of coordinates, and only a shape whose path *is* its box, its rounded box, its ellipse or a
  line has a name;
- `other` is 10%: `\begin{frame}` lines, notes, `\href`, `\resizebox` for WordArt;
- a freeform's traced outline is a file, not fewer numbers;
- structure (itemize, columns) is the text layer's, not this change's.

## Lists, tables, pictures and shapes together (mt-a -> ls-a)

Both agents merged: 912 slides score as at mt-a on every slide (boxes 0.9728, page 0.9714,
pixels 0.9845). texmap knows one vocabulary (slidepar takes its style as an option, shapes take one,
the slidetable rows and cells are named).

The proxy now counts what the tree own .sty files define as author vocabulary (`vocabulary`,
`tree_vocabulary`): a person reads `\slidepicture{x,y,w,h}{file}` as they read `\includegraphics`.
Without it, a source scored *lower* for saying the same thing in one word (six picture decks did).
A name the plumbing or style-switch patterns match (`\slidestrut`, `\slidesize`) stays plumbing, and
`slides@` internals never count.

On that scorer: m6-a 0.130 -> mt-a 0.366 -> ls-a 0.490
(lines 0.09 -> 0.48, numbers 0.05 -> 0.12, plumbing 0.04 -> 0.76, bloat 0.33 -> 0.61,
author 0.19 -> 0.92, repeat 0.61 -> 0.84).
Frame body lines now: text 36%, shape 28%, picture 11%, other 10%, placement 9%, table 2%, plumbing 1%.
Numbers per word (0.12) is the weakest component: shape paths hold most of them.

## Is the proxy measuring what a person means? (blind judging)

The readability number now steers the work, and it has already been wrong twice (it read style names
handed to a macro as visible words, and it read the deck's own `.sty` vocabulary as plumbing, so a
source scored *lower* for saying the same thing in one word). So it was checked against readers who
know nothing about it: `devtools/readability_calib.py` builds the sample and the prompts, the judging
is done outside the module by fresh agents that never see this repo, and the module takes the verdicts
back as JSON and prints the agreement. **The module calls no model**, and the scorer was not touched:
the point was to find where it is wrong, not to make it agree.

**The sample.** 40 slides of the 909 that both tags have, drawn by sha256 of (seed, deck, index) -
no RNG stream, so the draw replays exactly - and stratified by what the frame is made of
(`frame_kind`, which reads both vocabularies): 5 title, 5 table, 10 picture, 6 shape, 7 list,
7 prose. Five of every kind so each is represented at all, the rest shared out towards what the corpus
is (424 of its 909 slides are picture-led, 187 prose, 174 list, 43 title, 43 shape, 38 table), and at
most 2 slides per deck, so no deck's habits carry the result. Seed `readability-calibration-1`,
sample and pairs in `tests/decks/foreign/readability_calib/sample.json`.

**The pairs.** 40 *form* pairs - the same slide at `m6-a` (`textblock*` + `\vbox to` + `\leftskip`)
and at `ls-a` (the vocabulary) - and 20 *human* pairs, an `ls-a` frame against a frame from
`tests/decks/*.tex` or `tests/decks/sync/talk.tex` matched by kind. Sides are shuffled per pair by
hash and shown as A and B; a frame is shown with the wrapper lines around it (`m6-a` sets the slide
colour *outside* the frame, and hiding that would have been a tell); the frame's deck preamble is
filtered to the names it actually uses; the vocabulary of each form is given once per batch as
signatures plus their doc comments, never the implementation. `scrub` removes the comments that name
the writer. Nothing in a prompt says `beamer2slides`, `adopt`, `readability`, `m6-a` or `ls-a`.

**The question is an editing task, not taste**: change the wording of a line, move a box 20 pt to the
right, restyle a phrase. Which source makes that easier, how sure are you, *and what stands in the
way on each side* - the free text is the evidence, the vote is only the index into it.
**Three independent judges per pair**, 60 pairs in 4 batches of 15, 12 judge runs, each a fresh agent
with no memory of the others. Their agreement with each other is the ceiling any proxy can reach.

### The numbers

```
form   pairs  39  proxy agrees with the judges' majority 0.97   (no majority: 1)
       title 5/5  table 5/5  picture 8/9  shape 6/6  list 7/7  prose 7/7
human  pairs  20  proxy agrees with the judges' majority 0.65   (no majority: 0)
       title 1/3  table 0/1  picture 2/3  shape 4/4  list 2/4  prose 4/5

inter-judge  all three agree 0.88   pairwise 0.92
   j1 with the other two 0.96 | j2 0.93 | j3 0.98

Spearman (proxy margin vs judge margin): all 0.67, form pairs 0.85
Spearman over 100 frames (score vs share of votes won): 0.59
   m6-a: score 0.18 wins 0.02 | ls-a: score 0.54 wins 0.88 | human: score 0.72 wins 0.43
```

**On the question the proxy was built to answer - did this change make the source better to keep? -
it is right 0.97 of the time against a ceiling of 0.92-0.96**, on every kind of slide, and the size of
its margin tracks the judges' confidence (Spearman 0.85). The one form pair with no majority (f19) is a
slide holding a single picture: all three judges answered "tie" and said so in the same words - "the
move is the same single number in both". 113 of the 180 votes were at the top confidence, 10 at the
bottom, 5 were ties.

Against the hand-written anchors it is right 0.65 of the time, and that is the interesting half.

### The eight disagreements

| pair | slide | judges | proxy | |
|---|---|---|---|---|
| h13 | list, comic-strips | ls-a, 3-0 | human by 0.511 | |
| h01 | title, gdg24 | ls-a, 3-0 | human by 0.500 | |
| h02 | list, ds-lecture | ls-a, 3-0 | human by 0.313 | |
| h05 | prose, gdg24 | ls-a, 3-0 | human by 0.179 | |
| h06 | title, intro-lecture | human, 3-0 | **ls-a by 0.150** | |
| h04 | picture, drawing-workshop | ls-a, 3-0 | human by 0.107 | |
| h16 | table, comps-analysis | ls-a, 2-1 | human by 0.094 | |
| f06 | picture, drawings-basics | ls-a, 2-0-1 | m6-a by 0.033 | |

Six of the seven human pairs go the same way: the judges prefer the adopt frame where the proxy
prefers the hand-written one. Their reasons say why, and it is **mostly the task, not the proxy**.
One of the three edits is "move a box 20 pt right", and flowed beamer has no box:

> "A's nested itemize is flowed and has no coordinate at all." (h02, j3)
> "Plain beamer flows the list in the text area, so a 20 pt shift needs `\hspace*{20pt}`, an
> adjustwidth or a `\leftskip` change - none of them an exact move, and any of them re-wraps the
> items." (h01, j3)
> "`\centering` means nothing has a position: a 20 pt move of the `\includegraphics` needs
> `\hspace*{...}` inside a centred line, which shifts the box by only half the skip." (h06, j3)

That is a real property of absolute geometry and it is worth knowing that readers feel it, but it is
not evidence that an adopt source is nicer to keep than a talk somebody wrote. **The adopt-vs-human
comparison measures this task as much as it measures the sources**, and the 0.65 should be read that
way; the `m6-a`-vs-`ls-a` half, where both sides are absolute, is the clean measurement. The same
caveat explains the win shares: `ls-a` frames win 0.88 of their votes and the anchors 0.43, while the
proxy scores them 0.54 and 0.72.

Two disagreements are the proxy being wrong, and both are worth fixing.

**h06 - a frame with nothing in it scores 0.86.** The `ls-a` side is a section slide whose whole body
is `\frametitle{Expressions}` under `layout=section`; the anchor is a centred figure with a caption.
The proxy prefers the empty one, because every one of its components is a ratio per word or per line
and a frame with almost nothing in it has almost nothing to charge.

> "A's frame is a title and nothing else, so two of the three edits have no target at all." (j1)
> "`layout=section` draws the whole slide: the only editable token is `\frametitle{Expressions}`.
> There is no phrase to restyle, and moving a box means editing the shared layout (hitting every
> section slide) or inventing a textblock that isn't there." (j1)
> "I would have to invent a slidebox with coordinates and a style from the preamble just to have
> something to edit." (j3)

This is the theme layer's shadow: moving content out of the frame and into a recovered layout raised
the score *and* took the content out of reach. The proxy scores frame bodies only - the theme file is
not scored at all - so an edit at a distance is free.

**h16's dissent - a thing said twenty times in one frame is invisible.** Two judges preferred the
`ls-a` table; the third preferred the anchor for a reason the proxy cannot see, because `repeat` only
charges lines that appear in three or more *different* frames:

> "A's table is split into two `\slidetable` blocks with a per-row `\fontsize`/`\color` dump repeated
> twenty times." (j3)
> "`\row[h=10.42, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightPink}}]` is repeated on nearly
> every row, so a restyle has to be written in that idiom rather than as `\textbf`." (j2)

f06 is the last one and it is noise: a slide holding one picture, margin 0.033, judges B-tie-B.

### What the judges say stands in the way, that the proxy does not charge for

Every judge wrote what stood in the way on both sides of all 60 pairs. Sorted by how often it comes
back, on the `ls-a` side:

- **Saying the same thing twice inside one frame.** The URL in `\href{...}{\uline{...}}` written raw
  in the target and `\_`-escaped in the text ("so any change there is two edits", f17 j3); a title
  written both as `\frametitle` and as a `\slidetext` drop shadow ("a reword done once leaves the
  other copy stale", f07 j3); "Drag and Drop Activity is written twice, at (9.7bp,6.5bp) and
  (7.2bp,6.5bp)" (f18 j2); a footer "duplicated at the same coordinates (127.3,252.2) in two styles"
  (f08 j2). `repeat` sees none of it.
- **One thing that is several elements.** "a node is four independent elements at four coordinates
  (rect, label, ellipse, freeform), so 'move the box 20 pt right' is four edits that must agree"
  (f23 j3); "every chip is two elements ... so a 20 pt move has to be made twice or the label slides
  off its box" (f35 j3); `\slideline[...]{192.5,116.25}{250.43,87}` "gives two absolute endpoints, so
  moving that arrow 20 pt right costs two numbers instead of one" (f20 j3).
- **Rows of near-identical lines told apart only by a coordinate.** "Fifty `\slidetext` lines whose
  only distinguishing content is the word and the coordinates" (f29 j2); "44 near-identical
  `\slidetext[inset=0,center]` lines at 1.89 bp type, so the label you want is found only by its
  coordinates" (f29 j3); "All seven labels read 'Short label'" (f23 j1). The proxy counts those as
  short clean lines and rewards them.
- **Positional quads.** "{37,160.8,98.74,55.4} is positional, so you must count to the first number to
  move the box" (f21 j3); "{26.7,3.8,418.8,247.6} is positional, so without the vocabulary note you
  cannot tell x from w" (f19 j3). Named keys and a bare list of four numbers score the same.
- **Numbers that are not geometry.** "`space=0.01` / `space=-0.01` on the two blank `\slidepar` lines
  are unexplained hundredth-of-a-point fudges" (f16 j3); "the column widths 78.846/79.155/79.158/78.226
  are magic numbers I must not disturb" (f31 j2); `\slidestrut{5.49}{1.32}` "in front of every single
  word" (f27 j2). `numbers` counts a coordinate somebody needs and a fudge nobody understands alike.
- **Content in a file named by a hash.** "the freeform outlines live in external files
  (shapes/freeform-*.tex)", "which I cannot inspect from here" (f14 j2, j1); "named only by hash
  (shapes/freeform-8103ffe1.tex)" (f29 j3). Hundreds of numbers leave the frame and the frame's score
  improves.
- **Edits at a distance.** "the gradient bar, the 'Computer Science 161' strip and the page number have
  gone into `layout=section-header`, so if the box you are asked to move is one of those it is not in
  the frame at all" (f30 j3); "editing the shared style title-bold-white in the preamble would repaint
  every title in the deck" (f01 j1). Both are improvements the score should not get for free.

Two things the judges *did* credit, which the scorer already counts the way they read it: named
vocabulary reads as vocabulary ("a person reads `\slidepicture{x,y,w,h}{file}` as they read
`\includegraphics`" was the fix before this; no judge ever called `\slidetext` plumbing), and
`\slidestrut`/`\slidesize` are plumbing to them too.

### What to change in the proxy - named, not done

Nothing below was applied: a scorer tuned to its own calibration measures nothing. Each is worth a
separate round with the fidelity numbers held flat.

1. **Charge within-frame repetition.** `repeat` counts only lines shared by `REPEAT_FRAMES` (3) or
   more frames. Add a per-frame term: distinct lines / lines, or distinct option lists / option lists.
   *Gain*: it sees the per-row `\fontsize` walls, the doubled `\href` text, the twenty identical
   `\row` styles - the single most common complaint. *Cost*: a table of ten honest rows or a grid of
   labels is legitimately repetitive; the term has to charge repeated *plumbing*, not repeated
   content, so it should count option lists and macro arguments, not whole lines, or it will punish
   real tables.
2. **Refuse to reward an empty frame.** A frame under some floor of visible words (the corpus median
   is far above it) should not contribute a near-perfect score to the mean; either exclude it, or
   score the layout it names along with it. *Gain*: h06, and the whole class of "the content moved to
   the theme so the frame got easier". *Cost*: some slides really are one title, and the honest fix -
   scoring the recovered theme file as part of the deck - is a bigger change than a floor.
3. **Count a value written twice in one frame as a defect of its own.** A literal string or number
   appearing in two places in one frame body (a URL, a title, a size in both `textblock*` and
   `\includegraphics`) is exactly "one edit is two edits". *Gain*: it is the judges' own test, and it
   also catches the `m6-a` duplication they flagged (f06, f19). *Cost*: coordinates repeat innocently
   (a shared left edge), so it must compare only strings above some length and numbers that are not
   geometry.
4. **Tell a named argument from a positional quad.** `numbers` treats `{37,160.8,98.74,55.4}` and
   `x=37, y=160.8` alike. Weighting a literal by whether it sits under a name would say what the
   judges say. *Gain*: honest. *Cost*: it points the work at `key=value` everywhere, which lengthens
   every line and would fight the `lines` component; and no judge ever preferred the *other* source
   because of a quad - it is a tie-breaker, not a decider. Lowest value of the six.
5. **Separate a coordinate from a fudge.** A number under `space=`, `first=`, `ascent=`, `prevdepth=`,
   `h=` or inside a `\slidestrut` is the text model's residue, not the deck's geometry, and readers
   single those out every time. Charging them more than a position would aim the work where the
   judges point. *Gain*: it names the real remaining problem (0.12 on `numbers`). *Cost*: it is the
   easiest of the six to turn into tuning; the weights would need to come from the judges' *reasons*,
   which is exactly the thing this calibration must not be used for twice.
6. **Do not let a hash-named include hide its content.** A `\slidefreeform{...}{shapes/freeform-<sha8>.tex}`
   takes hundreds of numbers out of the frame. Count the included file's numbers at a discount, or
   charge the frame for naming a file whose name says nothing. *Gain*: the freeform decks (sc-memphis
   and friends) stop scoring as if their outlines were gone. *Cost*: it would undo a real improvement
   on paper - the frame genuinely is easier to read - so the right shape may be a separate reported
   number ("numbers in files this frame names") rather than a component.

Ranked by what the judges actually complained about: 1, 2, 3 first; 5 next and carefully; 6 as a
reported number; 4 last.

### Replaying it

```
python -m beamer2slides.devtools.readability_calib build     # sample.json + prompts/batch-NN.md
python -m beamer2slides.devtools.readability_calib report -v # agreement, Spearman, every disagreement
```
`build` needs the corpus (`$B2S_ADOPT_CORPUS`); the verdicts are committed under
`tests/decks/foreign/readability_calib/verdicts/jN-batch-NN.json`, so `report` replays the numbers
above from the tree. `tests/test_readability_calib.py` pins the draw (the committed sample is what
the corpus gives back), the prompt hygiene and the agreement maths on a made-up verdict set.

## Does an edited source still stand up? (m6-a vs ls-a, by compiling)

Readability is a proxy for one thing: a person opens the source, changes a line, and the slide still
holds. `devtools/edit_robustness.py` measures that directly - it makes the change and compiles. The
blind judges above were given exactly this task in words ("change the wording of a line, move a box
20 pt to the right, restyle a phrase"); this is the objective half of the same question, and the
answer is not the one the readability number would predict.

**Both forms behave identically. On all 143 edits the two forms reach the same verdict - not one
disagreement - and their unedited pages are pixel-identical on all 24 slides.** `ls-a` is a
refactoring of what `m6-a` said, not a change of what it does. What `ls-a` buys is the *cost* of the
edit: 1.2 source lines per edit against 2.1, and 1 line against 19.8 for a table row.

### The design

**The sample.** 24 slides drawn from the slides both tags have, by `random.Random(seed)` over each
category's pool sorted by (deck, slide) - so the draw replays exactly - taken round-robin over what
the slide is made of (`slide_category`: prose, list, table, shape, picture, title) and with every
deck used once before any deck is used twice. Seed 7 gives 4 slides of each category over 16 decks.
`sample` prints it, and `tests/test_edit_robustness.py` pins that the draw replays and that the
categories come out even.

**The nine edits**, each applied to the *same element of the same slide in both forms*. The element
is found by the words it prints (`Anchor.text`, a phrase that appears on exactly one clean line of
each form), so no edit can land on a different thing in one form than in the other:

| edit | what it does | slides it applied to |
|---|---|---|
| reword-longer | the line's words, half as many again | 24 |
| reword-shorter | half the words | 24 |
| restyle-run | two words into `\textbf` | 24 |
| change-title | another title of about the same length | 16 |
| move-box | the element's box 20 pt right | 24 |
| add-item | one more list item | 5 |
| delete-item | one list item less | 5 |
| add-paragraph | one more paragraph in a text box | 17 |
| add-table-row | one more row at the end of a table | 4 |

Not every edit fits every slide, and only the ones that applied are counted: 143 pairs.
An edit is offered only where **both** forms can take it (`both_forms`): a box inside `\adoptturned`
is never moved (the turn places it as much as its x does), a title is changed only where both forms
have one, and an `ls-a` `\item` that has a list under it is neither added after nor deleted.

**The judgement.** Each (edit, form) is compiled **as a one-frame document** with the deck's own
preamble and `.sty` files (as `adopt_bench.broken_frames` does; one `Builder` per deck and form, so
the tree is copied once), the page is rendered at 1000 px wide and three things are asked:

- **compiles** - lualatex produced a PDF, of one page.
- **confined** - every pixel that differs from the unedited page (≥ 16 in any channel) lies in the
  room the edit is allowed. That room is the element's own box out of the IR, plus 3 bp, **together
  with the pixels that element already covers on the unedited page** - measured by compiling the
  frame a second time with that element's lines taken out (`element_span`, `ink_hull`). 40 px
  outside is the threshold, and the sides breached are reported.
- **visible** - the page really changed (≥ 40 px), the words the edit writes are on it and the words
  it takes away are not. An edit that compiles because LaTeX quietly swallowed it is a failure, and
  this is the criterion that caught the biggest finding below.

**Why `confined` is fair.** It is one criterion, measured the same way on both forms, against the
same element box out of the same IR, with no reference to how either form is written. TeX's own
overfull-box warnings cannot serve: both forms set `\hfuzz=\maxdimen` and `\vfuzz=\maxdimen` inside
`\slidesbox`, so neither *ever* reports a box it overflows - the judgement has to be pixels.
Three allowances keep it from charging an edit for something it did not do, and all three are read
from facts about the element, not from the form:

- the element's existing ink, because Slides lets a text box's last line hang below it and both
  forms reproduce that. Without it every edit to such a box inherits the box's own overflow - which
  is how the first run came to fail `move-box` and `change-title`, edits that reflow no text at all
  and that now pass 24/24 and 16/16;
- `move-box` gets 20 pt more room to the right, and nothing else;
- an edit that *adds* content gets the box's column as far as the edge its flow grows towards -
  `box_align`, read out of each form's own syntax (`m6-a` puts `\vss` where the slack goes, `ls-a`
  names `bottom`/`middle` as a box key; **the two forms agree on the alignment of all 24 anchors**).
  A bottom-aligned box given one more line grows *upward*, as it does in Slides, and judging it
  downward charged it for the one direction it can never use (drawings-basics slide 11).
  A reword gets no such room: whether the words still fit is the question being asked.

A slide whose IR size is no paper size beamer knows is written at the paper's scale (poster-48x36:
362.8 × 272.1 in the IR, 1728 × 1296 bp on paper), so IR boxes are put into the page's units first
(`on_page`) - without it that deck failed all 7 of its edits in both forms, against the wrong part of
the page. A table's box is the one the source *draws* (`table_of`, `x .. x + Σ widths`), not the IR
element's: a Slides table sizes itself to its rows, so the box the API reports is not the table on
the slide (cs161-net slide 48: the IR says 148.8 wide, the table is 269.3).

**The fourth number is the cost of the form**: `lines`, how many source lines a person must add,
delete or change to make that edit - the edit as written here, which is the edit a person types.

### The numbers

```
edit             slides |         m6-a         |         ls-a
reword-longer        24 | 13/24 pass    1.0 lines | 13/24 pass    1.0 lines
reword-shorter       24 | 24/24 pass    1.0 lines | 24/24 pass    1.0 lines
restyle-run          24 | 15/24 pass    1.0 lines | 15/24 pass    1.0 lines
change-title         16 | 16/16 pass    1.0 lines | 16/16 pass    1.0 lines
move-box             24 | 24/24 pass    1.0 lines | 24/24 pass    1.0 lines
add-item              5 |  5/5  pass    4.0 lines |  5/5  pass    1.0 lines
delete-item           5 |  5/5  pass    4.0 lines |  5/5  pass    1.0 lines
add-paragraph        17 | 17/17 pass    4.0 lines | 17/17 pass    2.9 lines
add-table-row         4 |  4/4  pass   19.8 lines |  4/4  pass    1.0 lines

m6-a: 123/143 pass (86%), 299 lines over 143 edits (2.1 each)
ls-a: 123/143 pass (86%), 176 lines over 143 edits (1.2 each)

143 pairs, 0 where the two forms disagree on the verdict
24 slides, 24 whose unedited page is pixel-identical in the two forms
286 rows in 1335 s (seed 7, --n 24, --jobs 5; about 430 compiles - 48 baselines, 96 for the
element ink, 286 edits)
```

**Nothing failed to compile**, in either form, on any of the 286 edits. All 20 failures are the same
two kinds, and both forms fail on exactly the same slides.

`lines` is where the forms part, and it is the whole of what the rewrite bought. A list item is one
`\item` against a four-line `{\leftskip=...}` ... `\par}` group; a paragraph the same; a table row is
one `Newly & added \\` line against 11 to 31 lines of `\adoptrow` / `\adoptfix` / `\adoptcell` /
`\adopttops` / bounding box / foot rule / vertical-rule ends / one `\node` per cell, every piece
named by number. The ls-a row also comes out *right*: the `slidetable` environment gives it the
table's own default height (`h=19.56` on cs161-net 48), its fill and its verticals, while the m6-a
row can only inherit the previous row's geometry - on that slide the last row is 74.87 bp tall, so
the added row is nearly four times the height of a real one. Both "pass": the row is visible and
stays in the table's column. Only the source says which one a person would want to have typed.

### What fails

**reword-longer, 11 of 24, both forms.** A line made half as long again wraps, and the extra line
leaves the box: the box has a fixed height and no form reflows anything around it. Ten breach
downward, one upward (drawings-basics 11, whose box is bottom-aligned, so its flow grows the other
way). This is not a defect of either writer - it is the price of absolute geometry, which `adopt`
chooses deliberately, and the blind judges felt the same thing from the other side when they said
flowed beamer "has no coordinate at all". It is worth knowing how big it is: **a text box in an
adopted source has no slack at all in 46% of cases**, and the ink lands on whatever is below.

```
reword-longer  drawings-basics         11 title   changed= 47901 outside= 20716 top
reword-longer  sc-aesthetic-school      3 prose   changed= 10615 outside= 10615 bottom
reword-longer  comps-analysis          10 table   changed= 19871 outside=  4604 bottom
reword-longer  ap-bio-stats            56 picture changed= 32177 outside=  4585 bottom
reword-longer  sc-river-a4              2 shape   changed=  9997 outside=  1991 bottom
reword-longer  sc-functions             5 table   changed=  2400 outside=  1792 bottom
reword-longer  gdg24                   37 shape   changed=  3193 outside=  1377 bottom
reword-longer  sc-functions             2 picture changed=  1337 outside=  1337 bottom
reword-longer  sc-functions             3 table   changed=  1263 outside=  1263 bottom
reword-longer  gdg24                   34 shape   changed=  3194 outside=   730 bottom
reword-longer  supercharge-slides       7 picture changed=    57 outside=    57 bottom
```

The last one is marginal: 57 changed pixels of a 5 bp footer, 17 over the 40 px threshold. The other
ten are not close.

**restyle-run, 9 of 24, both forms: the page did not change at all.** Zero pixels, and the compile
was clean. Six of these are a real defect (below); three (ap-bio-stats 21, 22, 56) are a limit of the
measurement - their style is already `weight=bold`, so `\textbf` asks for what is already there.

### The one real defect this found: `\textbf` is a silent no-op

Six of the nine invisible restyles are slides whose style resolves to a family `adopt` declares with
an upright file and nothing else:

```
\setsansfont{NTR}[Path=fonts/,Extension=.ttf,UprightFont=*-Regular]              sc-functions 2, 3, 5
\setsansfont{Delius}[Path=fonts/,Extension=.ttf,UprightFont=*-Regular]           drawing-workshop 52
\newfontfamily\adoptfontB{Pacifico}[...,UprightFont=*-Regular]                   sc-aesthetic-school 3
\newfontfamily\adoptfontB{msgothic}[...,Extension=.ttc,UprightFont=*,FontIndex=2] apps-edu-zh 10
```

With `Path`/`Extension`/`UprightFont` given, fontspec looks for no other file, so the bold series is
the upright one. `\textbf` then draws nothing different - no error, no warning the reader would see,
and the edit a person just made is gone. That is worse than a failure to compile: the source
compiled, the slide looks finished, and the emphasis is missing.

It is in **both** forms, because it is in the preamble `adopt` writes, not in how the frame is
written; `adopt.py`/`fontfetch.py` know whether google/fonts had a bold instance, and a family with
none should get `AutoFakeBold` (luaotfload draws it) or a named substitute. Worth fixing next; it was
not fixed here, because this task measures.

### What the measurement cannot say

- **Three of the nine invisible restyles are the measurement's own fault**: the anchor's style is
  already bold, so the edit is a no-op by construction. Picking the run to embolden by what the style
  says would fix it and would also stop the edit being the same edit in both forms.
- **`confined` is a proxy for "the layout holds"**, not the thing itself: ink that stays inside the
  element's own room can still be ugly (a line cramped onto two), and ink that leaves it may land on
  empty paper. It does say, exactly, whether the edit took room it did not have.
- **One frame is not the deck.** Each compile is the frame alone, which is what makes 286 compiles
  affordable; a frame whose neighbours matter (a continued list) is not measured as such.
- **These are nine edits, not editing.** Nothing here measures the edit a person most often makes to
  an adopted source, which is to throw away the geometry and let beamer lay the slide out.

### Replaying it

```
python -m beamer2slides.devtools.edit_robustness sample --seed 7 --n 24   # the slides and their edits
python -m beamer2slides.devtools.edit_robustness run --seed 7 --n 24 --jobs 5 --tag a
python -m beamer2slides.devtools.edit_robustness report --tag a
```
`run` needs the corpus (`$B2S_ADOPT_CORPUS`) and lualatex, and writes `sample.json` / `results.json`
to `out/edit-robustness/<tag>`; re-run it after the writer changes and the table above is what should
move. `tests/test_edit_robustness.py` is the offline half (34 tests, no corpus and no compile): the
sample replays, each edit lands where it is meant to in both forms, and the judging is right on
made-up compile output and made-up pages.

## What the judges changed in the proxy (ls-a rescored)

Three of the six changes the calibration named, made:
- `_key` is now a line without its words, and a key must carry at least HEAVY (20) characters of
  machinery. A line counts as repeated when its key is said REPEAT_FRAMES times anywhere, within one
  frame or across frames: a style dumped onto twenty rows of one table is no longer free (the judges
  commonest complaint), while a list of items is not machinery and is not charged.
- A recovered beamer theme is measured with the frames it serves (`measure(..., shared=...)`, its
  lines spread over them). Every measure is a ratio per word, so a frame whose body is
  `\frametitle{Expressions}` scored 0.86 while the box a person wants to move had gone to the layout
  (judges h06). Moving content out of the frames is still worth it, but not free.
- `twins` (reported, not scored): long literals a frame says twice - a link raw and escaped, a title
  both in `\frametitle` and in a text box. Two edits, one of them easy to forget.

Rescored on that scorer: m6-a 0.128, mt-a 0.329, ls-a 0.438 (lines 0.41, numbers 0.12, plumbing 0.53,
bloat 0.57, author 0.86, repeat 0.87). The ranking the judges agreed with at 0.97 is unchanged.

Not made, and why: naming positional quads (no judge decided a pair on it), separating a coordinate
from a fudge (`space=-0.01`; the easiest of the six to turn into tuning), and charging a hash-named
include (better as a reported number). A wall of 50 `\slidetext` lines told apart only by their
coordinates is still uncharged: their machinery really does differ.
## Can the deck be synced back? Identity, and the merge (`ls-a` -> `lbl-a`)

Everything above asks how well the source *draws* the deck. This asks the other question: a person
adopts a deck, edits the source, and meanwhile somebody edits the deck in Slides. Does `sync` put the
two together without losing their work? Two things had to be true, and one of them was not.

### Every frame `adopt` writes now carries a label

**Before: none did.** `adopt.bootstrap` wrote `\begin{frame}[plain,layout=...]`, and a compiled
adopted source read back **0 labelled pages**. A frame label is the only piece of a slide's identity
that survives compiling (docs/labels.md): without one, `identity` falls back to the slide's title, its
occurrence among slides of that title, and the order-keeping alignment. That is the weakest identity
the system has, and an adopted deck is the worst deck to hand it:

| the corpus (29 decks, 912 slides) | |
| --- | --- |
| slides with a title | 483 (53%) |
| slides with no words at all | 17 |
| slides whose `objectId` repeats inside its deck | 0 |

Nearly half the slides have nothing to be identified by, and `pull` then rewrites the words of the
ones that do.

**Now** (`adopt.frame_labels`, `FramePlan.options`, `inverse.frame_latex`) every frame carries
`label=<slug of the slide's objectId>` — the deck's own name for the slide. It is unique by
construction, the same on every read, present for a slide with no words, and unrelated to anything the
source may later change. A title-derived label would have to move the day the slide is retitled, and a
label may not move: that is what `beamer2slides label` refuses to do to an existing one, and what
docs/sync.md "When a label moved" is about. `labels.slug` makes the id legal for `\label`/hyperref,
and because slugging is lossy (`Same_Id` and `same-id` slug alike; the API's own
`SLIDES_API1234567890_0` ids differ late) uniqueness is enforced against the labels already given out.
That guard matters more here than anywhere else: **a label written twice never reaches the PDF twice**
— hyperref keeps the first destination and drops the second — so a duplicate does not show up as a
clash, it shows up as *no label*, on the slides that have nothing else. Over the corpus the guard
never had to fire (0 slug collisions in 912 slides), and the labels are the same on a second run.

The option goes in ahead of the theme's own keys, where beamer reads `label` as the plain option it
looks like (`\deck@keys` sets templates and colours as they are read).

Measured: a compiled adopted `comic-strips` reads back **17 of 17 pages labelled**
(`p`, `g11460474-0-27`, …) where it read back 0.

**Fidelity is untouched** — `adopt_bench run --jobs 5 --tag lbl-a`, 29/29 decks, 912 slides:

| | boxes | page | pixels | deck mean |
| --- | --- | --- | --- | --- |
| `ls-a` | 0.9728 | 0.9714 | 0.9845 | 0.9557 |
| `lbl-a` | 0.9728 | 0.9714 | 0.9845 | 0.9557 |

`cmp.py ls-a lbl-a` reports `+0.0000` on every deck and every slide it lists. A frame option changes
no ink, which is the point of checking.

### The merge, against adopt-shaped decks

The offline loss campaign (docs/sync.md, "Proving nothing is lost") drew decks `convert` would write.
`fuzz_sync offline --shape adopt` draws what `adopt` writes instead: 4-7 slides of 6-16 small boxes,
fewer than half with a title, two columns of near-identical one-line phrases from the same dozen
("Agenda", "Next steps", "Questions?"), a footer and a shared logo on every slide, grouped clusters, a
table on a third of them, and every third slide a twin of the one before it with one line of its own.
Over 120 seeds: 649 slides, 10.56 elements a slide, 274 titled, 237 with a grouped cluster, 196 with a
table (5358 texts, 825 pictures, 473 shapes, 196 tables). It runs at about the same speed as the
converted shape and shrinks the same way.

It found three things, and one of them was a real defect:

1. **`sync.Sync.restack` could stack a new panel over kept text** (a real defect, fixed). A recreated
   element keeps the deck's place in the z-order and a created one goes where the source puts it —
   after the element before it. Where those two orders disagree, a created opaque panel landed above
   text the source draws *above* it, and nothing was deleted, so every other check passed. An adopted
   deck reaches that at once (a label that moved writes a whole other frame's elements onto a slide);
   a converted one rarely does. A created element now also stays below the first element the source
   draws above it. `tests/test_sync.py::test_a_created_shape_stays_under_the_text_the_source_draws_
   above_it` fails without it, and so do 2 of 400 rounds.
2. **The oracle could not name an element the source added** (a harness bug, fixed):
   `loss_oracle._element_of` read the base's elements only, so an object created for a *new* element
   had no key, and `_source_stacks_above` — the only excuse `text_hidden` has — can excuse nothing it
   cannot name. 3 of 400 rounds.
3. **The reference applier left created objects on top** (a harness bug, fixed):
   `fuzz_world._update_slide` gave a recreated object its old slot and a created one whatever place
   Slides gives a new object, which is the front. `_restack` now models the outcome sync writes.

Campaigns after the fixes, all clean: 5000 adopt-shaped rounds, 600 at chain 4 (edit → sync → edit →
sync, four times), and — the regression check, since the world and the shrinker both changed — 2000
converted rounds and 500 converted rounds at chain 4. `tests/test_sync_fuzz.py` runs 25 adopt-shaped
seeds plus the five that failed (82, 328, 441, 568, 629) in the default suite.

### What the label is worth, measured

The loss oracle cannot answer this: following a label onto the wrong frame deletes nothing, so the
sync campaign is clean with labels and clean without them (400 rounds each way). `fuzz_labels`
can, because its synthetic source tags every frame and so knows the right pairing. It takes the
shape now too (`--shape adopt`), and 1500 rounds over 8239 frames say:

| misidentified frames | `order` | `before` | `now` (what sync does) |
|---|---|---|---|
| adopt-shaped, labels sound | 0.00% | 0.00% | 0.00% |
| adopt-shaped, labels broken | 12.34% | 12.34% | **2.94%** |
| a converted talk, labels sound, a frame moved | 6.93% | **0.00%** | 0.00% |
| a converted talk, labels broken | 15.48% | 15.38% | **1.29%** |

Two things to read there. With the labels kept, **not one frame of 4068 went to the wrong slide** —
that is the deck a person adopts from now on. And `before` equals `order` frame for frame on this
shape: `identity.cross_pairs` and `identity.gap_pairs`, the two passes that rescue a converted
talk's moved or retitled frames, **recover nothing at all** here. They need one leftover that
explains one slide unmistakably, or a lone gap whose slide and frame share words nobody else shares;
a deck of repeated one-line phrases offers neither. `identity.label_moves` still earns its place
(12.34% -> 2.94%) but saves less than on a talk and says less — 39 of the 60 rounds left wrong
passed in silence, against 1 of 22.

So on an adopted deck the label is not the best identity available. It is the only one.

### What a person should still not expect to survive

- **A label that moved.** If the source's frames are reordered or rewritten so that a label ends up on
  another frame, sync follows the label and writes that frame onto that slide. `identity.label_moves`
  reports it when the content disagrees; it cannot undo it. On an adopted deck the labels are written
  once and never touched, so this only happens if someone moves them by hand.
- **A slide with no label and no words.** 17 corpus slides have no text at all. Those are kept by the
  alignment and by nothing else: insert a frame in front of one and it can be created afresh while
  the old slide keeps the person's edits.
- **Stacking a person chose.** Sync keeps the deck's z-order for what it recreates and the source's
  for what it adds, and where the two contradict each other the new element goes to the bottom. That
  is safe, not faithful: a panel the person deliberately brought to the front can end up behind.
- **Everything docs/sync.md "Not supported yet" already lists** — crossing reorders of unlabelled
  frames, diff3 inside diagrams, a group the person drew being rebuilt.

### The rebuild guard, checked by reading

Asked as part of this: does `guard.py` hold for an adopted deck, whose base `convert` never wrote?
It does, and for two reasons that need no new code.

- `adopt` writes a source tree and **no output folder**. The only way `convert` rebuilds a deck in
  place is `guard.previous_deck`, which reads `<out>/emit.json`, and only `convert` writes one — so
  the foreign deck a person adopted can never be the deck a rebuild replaces. Adopt, then convert,
  and the convert makes a *new* deck in `out/<stem>/` with a base of its own; rebuilding that folder
  is the ordinary guarded path, and the foreign deck is not touched.
- If a base is missing anyway (the folder's copy deleted and Drive's `appProperties.b2sBase` gone),
  `guard.check_rebuild` sets `reason = "no-base"` and refuses — already pinned by
  `tests/test_guard.py::test_a_deck_without_a_base_is_never_silently_rebuilt`.
## Making `\textbf` draw bold (fb-a vs ls-a)

The defect the section above found, fixed and fenced. A source `adopt` writes now names **every one
of the four faces** of every family it declares, and says in the source when one of them is
synthesised; a new torture test compiles the real preamble and measures the ink, so the hole cannot
open again quietly.

**Before, 63 of 267 (family, style) pairs across the corpus drew no emphasis at all; after, 6 - and
none of those 6 is a style the decks' own text asks for.** Fidelity is unchanged (slides mean
0.9728 -> 0.9727, deck mean 0.9557 -> 0.9557) and readability is unchanged to every decimal the
report prints (0.490, and every component equal).

### What was wrong

`font_files_latex` wrote only the faces the family had files for:

```
\setsansfont{NTR}[Path=fonts/,Extension=.ttf,UprightFont=*-Regular]
```

With `Path`, `Extension` and `UprightFont` given and no `BoldFont`, fontspec looks for no second
file, so the bold series *is* the upright. `\textbf` draws the same glyphs, lualatex says nothing,
and the slide looks finished with its emphasis gone - which is worse than an error, because nothing
asks the reader to look. The same hole was open for `\textit` / `\emph`, and for bold italic
wherever a family has a bold and no italic (Oswald, Noto Sans JP, every google/fonts family whose
variable font has no `ital` axis - `fontfetch` cuts wght 400 and 700 and nothing else).

### What it writes now

`adopt.fake_faces` fills the missing styles from the nearest face the family does have - a bold
italic is slanted off the real bold where there is one, emboldened off the real italic otherwise,
and both off the upright when that is all there is:

```
\setsansfont{NTR}[Path=fonts/,Extension=.ttf,UprightFont=*-Regular,
  BoldFont=*-Regular,BoldFeatures={FakeBold=2.5},
  ItalicFont=*-Regular,ItalicFeatures={FakeSlant=0.2},
  BoldItalicFont=*-Regular,BoldItalicFeatures={FakeBold=2.5,FakeSlant=0.2}]
\newfontfamily\adoptfontC{Oswald}[...,UprightFont=*-Regular,BoldFont=*-Bold,
  ItalicFont=*-Regular,ItalicFeatures={FakeSlant=0.2},
  BoldItalicFont=*-Bold,BoldItalicFeatures={FakeSlant=0.2}]
\newfontfamily\adoptfontB{msgothic}[FontIndex=2,Path=fonts/,Extension=.ttc,UprightFont=*,
  BoldFont=*,BoldFeatures={FakeBold=2.5},...]
```

Three decisions, each of them measured, not assumed:

- **Synthesise rather than leave it unsaid, because that is what the deck shows.** Slides has no
  second face either and draws a synthetic one; matching Slides is the goal, so the source should
  say out loud what Slides does silently.
- **Per face, never `AutoFakeBold`/`AutoFakeSlant`.** `AutoFakeBold` *overrides* a real `BoldFont`
  (Oswald's drawn bold came out as its emboldened regular), and `AutoFakeSlant` on its own leaves
  bold italic upright - it makes the `it` shape from the upright and never touches `bx/it`. A
  family-level `BoldFeatures` does not clobber the one `scripts.script_preamble` puts into
  `\defaultfontfeatures` (measured: a CJK fallback still draws under `BoldFeatures={FakeBold=..}`).
- **`FontIndex` first.** For the one face of a collection (MS PGothic, face 2 of `msgothic.ttc`) the
  index is a family-wide key, so it holds for the faked faces too and the synthetic bold keeps face
  2's proportional widths instead of face 0's monospaced ones.

`scripts.babelfont_line` had the identical hole in a second writer, and the torture found it:
`onchar=ids fonts` sends every Hebrew or Arabic letter to the babel font whatever the surrounding
text is set in, so it is *that* line, not `adopt.font_preamble`'s, that decides what `\textit` draws
for those letters - and it named only the upright. hebrew-lesson's Hebrew serif drew `\textit`
upright; it now goes through the same `fake_faces`.

### Calibrating the two numbers

`FakeBold` is linear in the stroke it adds: over NTR, Delius, Pacifico and ArchitectsDaughter set at
40 pt it adds 0.011-0.015 em of stroke per unit (mean stroke = 2 x ink area / ink outline, the
measure `deck_thumbs.stroke_em` uses).

The judge has to be the deck, and the deck can only be read where the text is big: one thumbnail
pixel is 1/(2.222 x size) em, which at the 5-12 pt most bold runs are set in is 0.03-0.05 em and
swamps any emboldening difference. **The corpus has exactly one clean sample** -
sc-aesthetic-school's Pacifico at 28.35 pt, where a pixel is 0.016 em:

```
deck thumbnails   Pacifico upright 0.128 em    Slides' own bold 0.166 em
compiled source   Pacifico upright 0.126 em    FakeBold=2.5     0.161 em
```

1.5 gives 0.147 (a pixel and a half short) and 3.0 gives 0.170 (also within the pixel); 2.5 sits
nearest. The other families then read 1.33-1.38x their upright, which is about what a drawn bold
weighs (Arial's real bold reads 1.49x, Changa's 1.70x).

`FakeSlant=0.2` is fontspec's own documented value, atan(0.2) = 11.3 deg. It is not calibrated
against Slides, because there is nothing to calibrate against: the corpus sets 260 italic runs in 13
decks and nearly all of them are Arial, which has a real italic file. The one italic the thumbnails
do show - ap-bio-stats' Arial Italic, drawn and not synthesised - leans 0.23 against its upright's
-0.02 by the shear the torture measures, and a `FakeSlant=0.2` face reads 0.15-0.22 the same way.

**Offline, the fix costs nothing**: a machine with no font and no network (`$B2S_FONT_FETCH=0`) is
set in TeX Gyre, which every TeX distribution carries with all four faces, so nothing is synthesised
and the source still shows emphasis. `tests/test_adopt.py` asserts exactly that.

### The torture test: `devtools/bold_torture.py`

Nothing already in the tree could see this. The fidelity bench cannot: a word drawn upright where it
should be bold costs a handful of pixels on one slide out of 912. `edit_robustness` found it, but
only by accident, on 6 of 24 restyle edits, and it takes 22 minutes.

So the emphasis gets its own judge, and the judge is the ink. Per deck: build the source's real font
preamble (`scripts.script_preamble` + `adopt.font_preamble`, the writers under test - no bootstrap,
no slides, no Google), then compile **one page per declared family** with the deck's own most used
letters for that family set four ways (upright, `\bfseries`, `\itshape`, both) at 40 pt, and measure
each band:

```
stroke   2 x ink area / ink outline: the mean stroke width in em, the same measure
         `deck_thumbs.stroke_em` reads the deck's thumbnails with
slant    the shear that lines the ink into columns (projection-profile deskew, argmax over
         the sum of squares of the column histogram): ~0 upright, > 0.1 leaning right
```

A family fails `bold` when its bold weighs no more than 1.10x its upright, `italic` when it leans no
further than 0.08 past it, `bolditalic` when either holds. The defect gives exactly 1.00 and 0.00, a
fake gives 1.3-1.5 and ~0.20, a real face as much or more - so the threshold never has to be a
judgement call.

**Every family the preamble declares is probed, not only the styles the deck's text asks for**: a
person restyling an adopted slide in Slides reaches the rest, which is how `edit_robustness` found
this in the first place. `asked` records which is which, so the report can separate "the deck is
missing an emphasis it prints" from "the deck would be, if someone typed one".

It is cheap and re-runnable: one compile per deck, 698 s over the 29 decks (about 3 minutes wall at
`--jobs 5`, and gdg24's 115 s is the floor), and it exits non-zero on a finding.

```
python -m beamer2slides.devtools.bold_torture run --jobs 5 --tag after
python -m beamer2slides.devtools.bold_torture report --tag after
```

**The PDF's own font names are deliberately not used as evidence.** A static instance cut from a
variable font reports the name it was cut from - every weight of Open Sans says `OpenSans-Regular` -
and the four styles of a `.ttc` face share one font id, so the names agree exactly where the ink
does not.

### The numbers

```
                       before                            after
29 decks, 267 (family, style) pairs
  drawn                204                               261
  not drawn             63  (8 the deck's text asks for)   6  (0 the deck's text asks for)
  decks with a finding  13                                 2
```

The four decks the defect was reported on, by the torture's own reading (x = heavier than its
upright, lean = past its upright's slant):

```
                                             before                    after
sc-functions         NTR      bold        x1.01  not bold          x1.37  drawn   (asked)
                     NTR      italic      x0.99  not slanted       lean +0.21  drawn
                     Changa   bolditalic  lean +0.04  not slanted  lean +0.22  drawn
drawing-workshop     Delius   bold        x0.97  not bold          x1.33  drawn   (asked)
                     Delius   italic      lean +0.00  not slanted  lean +0.20  drawn   (asked)
                     Delius   bolditalic  x1.00  neither           x1.36 lean +0.21  (asked)
                     11 families, 33 pairs   23 not drawn     0 not drawn
sc-aesthetic-school  Pacifico bold        x1.00  not bold          x1.28  drawn   (asked)
                     Pacifico italic      lean +0.00  not slanted  lean +0.21  drawn
apps-edu-zh          MSPGothic bold       x1.00  not bold          x1.33  drawn
                     Oswald   bolditalic  lean +0.00  not slanted  lean +0.21  drawn
```

### Fidelity and readability

```
python -m beamer2slides.devtools.adopt_bench run --jobs 5 --tag fb-a
29/29 decks, 912 slides: boxes 0.9728 page 0.9714 pixels 0.9844
slides mean 0.9728 -> 0.9727      deck mean 0.9557 -> 0.9557      (vs ls-a)
readability 0.490 -> 0.490        (every component equal: the fix adds options to preamble lines
                                   that were already there, and writes no new line)
```

Only three decks move at all.

**sc-aesthetic-school +0.0001, all of it slide 11 (+0.003).** That slide has the one Pacifico bold
run in the corpus set big enough to read - 28.35 pt - and the deck's own thumbnail shows it at
0.166 em against the upright's 0.128. The gain is the compiled page finally drawing a heavier face
there. This is the calibration sample, so it is also the one slide where a gain was expected.

**sc-functions +0.0001, slides 7 and 11 (+0.001 each), slide 9 -0.001.** Both gaining slides carry
NTR bold runs; the deck's thumbnails read NTR bold at 0.119 em against 0.075 upright, so Slides
synthesises one there too. (Those runs are set at 5-9 pt, where a thumbnail pixel is 0.05-0.09 em, so
that reading says "the deck draws something heavier" and not how much heavier - which is why the
calibration used Pacifico and not this.)

**drawing-workshop -0.0005: slides 3, 23 and 24, -0.005 to -0.007 each. This one is a loss, and it
is not the deck's own emphasis.** Those slides have no bold or italic runs in the IR at all. The
whole difference between the two pages is WordArt: `adopt.wordart_block` writes `\bfseries` on
purpose, to stand in for the heavy outline Slides draws WordArt with, and drawing-workshop's WordArt
is set in Delius - which had no bold, so that `\bfseries` drew nothing and the score was measured
against an approximation that was never applied. Diffing the two compiled pages charges every
differing pixel to a WordArt element (32,074 px on slide 3: 24,435 to `Joshua Pomeroy`, 7,527 to
`#GOALS`; 22,788 and 22,714 on 23 and 24 to the two stacked `Drag and Drop Activity` boxes).

So the loss is real and its cause is exact: **WordArt's own weight has never been calibrated**, and
now that `\bfseries` really draws, it is slightly too heavy for a family with no bold of its own.
That is a question about `wordart_block`, not about the face declarations, and it is left where it
is - fixing it means measuring Slides' WordArt outline against the corpus, which is its own exercise.

### What the torture still cannot see

- **Whether the weight is the right one.** A faked bold is not the family's real bold, only heavier
  than its own upright; and a family whose real bold this machine cannot fetch (`$B2S_FONT_FETCH=0`)
  is still a pass, because TeX Gyre has all four faces.
- **Whether a run the deck sets at weight 600 got 600** rather than 700. `weight_faces` is not
  probed; only the four shapes are.
- **Whether the face a style names has the deck's own glyphs in it.** This is the one finding left
  that is not a limitation: arabic-training's `\textit` picks the real `ariali.ttf`, which has no
  Arabic in it, so the probe reads half the ink (x0.50) and no slant. The torture calls it "not
  slanted", which is true but not the reason; the real defect is that a family's italic file can
  lack the script the deck is written in. Left open - it is a different fix (fall back to a slanted
  upright when the named face has none of the run's characters), and no deck in the corpus prints an
  italic Arabic run.
- **The styles a fallback chain draws.** `scripts.script_preamble` names one
  `luaotfload.add_fallback` chain for upright and one for bold, so `\textit` over Japanese draws the
  upright chain (jruby-ja, 2 pairs). Giving the chain an italic means a third and fourth chain plus
  per-shape `RawFeature`s inside a `\defaultfontfeatures` every family reads - where a `FakeSlant`
  would slant the real italic of every family that has one, twice. Left open, and now measured.
- **Whether the emphasis is in the right place.** The probe sets words the writer chose, not the
  deck's own runs on the deck's own slides; it proves the *face* draws, not that the run reached it.
  `devtools/edit_robustness`'s restyle edit is the check that covers that, and it agrees.

### The offline tests

`tests/test_adopt_text.py`, under "every style is named (adopt.fake_faces)": a family with all four
faces fakes nothing; a family of one face still names bold, italic and both; a bold and no italic
slants the bold for bold italic; an italic and no bold emboldens the italic for bold italic; a lone
face of a collection keeps its `FontIndex` in every style; a bold run in a family with no bold comes
out of `bootstrap` with a source that says bold; and a babel language font names every style too.
`tests/test_adopt.py`'s two font tests and `test_adopt_media.py` / `test_adopt_scripts.py` pin the
exact declarations the writers now produce.

## The combined tree, verified (fl-a)

Three lanes landed together - labels on every adopted frame, the scorer charging repetition inside a
frame, and every font face named - so the tree was benched once more from cold (29 decks, 912
slides, 0 cached): **boxes 0.9728, page 0.9714, pixels 0.9844, deck mean 0.9557**. Against `ls-a`
exactly three decks move, all of them fonts, and no others by a pixel:

| deck | before | after | why |
|---|---|---|---|
| sc-aesthetic-school | 0.9960 | 0.9961 | a style the deck calls bold now draws bold |
| sc-functions | 0.9853 | 0.9854 | the same |
| drawing-workshop | 0.9898 | 0.9893 | `wordart_block`s `\bfseries` stand-in, which had never drawn; an honest loss |

Readability is unmoved at **0.438** (lines 0.41, numbers 0.11, plumbing 0.53, bloat 0.56, author
0.86, repeat 0.91), which is the point: labels and font declarations go in the preamble, not into
the frames. `numbers` is now the weakest component by a factor of four, and shapes are where the
numbers are - 5,515 shape lines over 26 decks carrying 30,574 of them, 28% of all frame body lines,
with `\sliderect[fill=white,fill opacity=#]{#,#,#,#}` said 785 times and `\sliderect[fill=black]`
756 times. That is the next lane.

## A shape met once (sh-a)

Based on `d838120`, the tree `fl-a` was measured from (main had moved on to `98fb536`; not merged
again mid-flight). Two moves, both of them about **repetition and coordinates**, neither of them
about the drawing:

1. **A look the deck draws again and again becomes a name.** `survey_styles` writes every shape of
   the deck onto a scratch context (the way `adopt.recovered_theme` surveys the theme), counts the
   option lists the frames really produce, and gives a name to each one said `REPEAT_STYLE` = 3
   times or more. main.tex's preamble says it once - `\slideshapestyle{fill-white-outline-darkgrey}
   {fill=white,draw=DarkGrey,line width=0.47bp}` - and the frame says
   `\sliderect[fill-white-outline-darkgrey]{67.2,63,243.6,37.8}`. **192 names over 27 decks, 3,534
   uses.**
2. **A path of more numbers than anyone reads keeps its points in a file.** Past
   `OUTLINE_NUMBERS` = 12 numbers, a preset's outline goes to `shapes/<kind>-<sha8>.tex` and the
   frame says `\slideshape{151.9,174,19.64,17.74}{\slidepath[fill-yellow-outline-paleblue]
   {shapes/star5-9b3c2dfe.tex}}` - the box, and a name for the look. A five-pointed star is twenty
   numbers; jruby-ja's frames were 2,346 numbers over 102 lines. **132 such lines, 85 new files**
   (identical geometry is written once and shared, whatever style each shape draws it in), on top of
   the 1,462 files `\slidefreeform` already wrote.

### The names

Named after what they are, from the keys themselves: `fill-white`, `outline-darkgrey`,
`fill-lightblue-outline-darkgrey`, `gradient-nearblack`, `outline-red-dashed`, `fill-red-faded`. The
colour word is the deck's own colour name where it has one (`colour_word` turns `b2sFFCC00` into
`yellow`), and the name says `fill` or `outline` **in front of** the colour on purpose: TikZ reads a
bare `red` in that very place as the colour red, so a style called `red` would take the word away
from anyone editing the frame. Two styles alike but for the weight get the weight
(`outline-black-0.47` beside `outline-black-0.94`); anything still colliding gets `-2`, `-3`
(`fill-green-2`, where the deck fills in two different greens).

A name is a `\tikzset` style, not a macro, so it sits where the options go and **any key after it
still wins**: `\sliderect[card,fill=Red]{...}` changes that one card and nothing else. That is the
answer to "a named shape must make an edit cheaper, not dearer".

`\slidepath` is nested inside `\slideshape`, not a macro of its own, for two reasons: the expansion
is then byte-identical to the tokens the frame held (turned, transformed or plain), and
`readability.construct()` would count a brand-new top-level macro name as "text", which would move
the ruler rather than the source.

### Fidelity: nothing moved, in either direction

`adopt_bench run --jobs 5 --tag sh-a`, 29 decks, 912 slides: **boxes 0.9728, page 0.9714, pixels
0.9844, deck mean 0.9557** - equal to `fl-a` in every digit. `cmp.py fl-a sh-a` compares **per
slide**: every one of the 912 moves by +0.0000, and no deck moves by 0.0001 in either direction
(slides mean 0.9727 -> 0.9727, deck mean 0.9557 -> 0.9557). That is what the
design was for - `\slideshapestyle` expands to exactly the keys it replaced (written through `to_bp`
like the frames' own lines, or a named `line width` would be 0.4% off the inline one), and
`\CatchFileDef` pulls the path back byte for byte.

### Readability: 0.438 -> 0.448

| component | fl-a | sh-a |
|---|---|---|
| **score** | **0.4382** | **0.4477** |
| lines | 0.4054 | 0.4054 |
| numbers | 0.1147 | 0.1231 |
| plumbing | 0.5333 | 0.5350 |
| bloat | 0.5596 | 0.5825 |
| author | 0.8639 | 0.8639 |
| repeat | 0.9061 | 0.9061 |

Every deck moves up or stands still; none moves down. The biggest: jruby-ja 0.235 -> 0.333,
instagram 0.296 -> 0.336, comic-strips 0.510 -> 0.544, drawing-workshop 0.444 -> 0.458,
intro-lecture 0.492 -> 0.504, jeb-arch 0.267 -> 0.276, journey-maps 0.340 -> 0.349. On the shape
lines themselves: **30,574 numbers -> 26,300** (-14%) and **437,832 characters -> 382,552** (-13%);
jruby-ja 2,346 -> 1,061 numbers, cs161-net 2,002 -> 1,637, sc-memphis 11,640 -> 11,246.

What did **not** move says as much:

- `lines` is untouched, and the construct table is identical (5,515 shape lines, 28.4%). Neither
  move removes a line: a shape is still one line, it is shorter and says fewer numbers.
- `repeat` is untouched because `readability._key` keeps a line's numbers and only takes its words
  out, so two rects at different coordinates never shared a key and still do not. What the naming
  removes is repetition a reader sees and the ruler does not charge.
- `author` is untouched: `\slidepath` replaced `\path` one for one, and a style *name* is neither a
  command nor vocabulary (`vocabulary()` reads `\newcommand|\def|\newenvironment`, so a
  `\tikzset{name/.style=...}` name is neutral - it costs nothing and earns nothing).

**Disclosure, for the ruler's sake.** The preamble is not a frame body, so the 192 `\slideshapestyle`
lines and the 85 files are not charged at all, and a file path inside braces counts as a *visible
word* (and a sha8 beginning with a digit as one spurious number), which flatters the per-word ratios
by at most 132 words. This is the arrangement `\slidefreeform` has had since `ff-*`; it is the same
bargain - the source a person reads is smaller, and the bytes are still in the tree - but it is a
bargain the score cannot see, which is why the honest numbers above are the per-line counts.

### What was left inline, on purpose

- **A style used twice.** Three is the threshold (`REPEAT_STYLE`, the ruler's own `REPEAT_FRAMES`):
  a name a person meets once costs them a lookup and saves nothing. cs161-net's
  `\sliderect[fill=Purple,draw=black,line width=0.94bp]` stays as it is, two doors down from
  `\sliderect[fill-yellow-outline-black]`.
- **A path of 12 numbers or fewer.** An elbow connector (`\slideline{a,b}{c,d}` plus one bend) and
  gdg24's arcs read perfectly well; putting them in a file would cost a reader a second file to
  open for less than they already see.
- **Multi-path preset bodies** (CUBE, DONUT, an ARC drawn filled and then stroked): the body is
  several `\path`s that differ in their keys, and the shape is the *relation* between them. One file
  per path would scatter it; one name for a body that appears twice would be a macro nobody reads.
- **Arrow tips and the geometry keys** - `->`, `rotate=`, `flip`, `rounded=` - stay where they are.
  They are this shape's own facts, not the deck's look; a `rounded=29.4` in a style would be a
  number hidden in a name.
- **`\slidefreeform`'s options**, which are keyval and already carry their points in a file.

### What it costs a person editing the source

`edit_robustness`'s two forms are the frozen corpus tags `m6-a` and `ls-a`, and the tool is not this
lane's to change, so the measurement was made **against a shadow corpus**: a scratch directory whose
`<deck>/runs/m6-a` and `<deck>/runs/ls-a` are NTFS junctions onto the real corpus's `m6-a` and onto
`fl-a` (the before run) or `sh-a` (the after run), with `target.json` copied. `$B2S_ADOPT_CORPUS`
points the tool at it; the real corpus, and `ls-a` in it, are untouched. Both runs draw the **same
sample** (seed 7, 24 slides; `sample.json` identical byte for byte).

**The two runs agree in every row**: 143 judged edits each, `m6-a` 123/143 pass (86%), 2.1 lines per
edit; the adopted form 129/143 (90%), 1.2 lines per edit - and the whole `results.json`, pixel
counts included, is the same multiset of rows before and after. Naming a shape's look and moving a
long outline into a file costs a person editing the *text* of a slide exactly nothing, which is what
one would want and is worth having measured rather than assumed.

What the tool cannot say is the other half: none of its nine edits touches a shape, so it can
neither confirm nor deny that a named shape is cheaper to change. That half is by construction and
is pinned offline instead (`tests/test_adopt_shapes.py`):

- a name is a `\tikzset` style in the options slot, so one shape changes on the spot -
  `\sliderect[card,fill=Red]{...}` - with no macro to break and no optional argument to design
  (`test_a_named_look_can_still_be_changed_on_one_shape`);
- changing what *all* of them look like is now one line in the preamble instead of one line per
  shape: sc-memphis's nine names cover 1,629 frame lines, cs161-net's eighteen cover 332;
- and a name is never a word TikZ reads as a colour, so it cannot quietly redefine `red` under an
  editing hand (`test_a_style_name_is_never_a_word_tikz_reads_as_a_colour`).

### What was tried and rejected

- **Decoration every slide repeats hoisted into the theme** (candidate 2). Measured over the corpus:
  whole groups of shapes repeated slide to slide account for ~340 of 5,515 shape elements, and
  hoisting only the blocks that are identical *and* consecutive saves 1.7% of shape lines. Worse,
  the repeats are not identical: sc-memphis's tile differs in the last hundredth of a coordinate
  from slide to slide and carries a per-slide `fill opacity`, so a layout drawing it once would move
  pixels. Fidelity is the hard constraint; rejected.
- **A lattice for a column or row of cards** (the arithmetic-run form of candidate 3). Only 22 of
  the 5,515 shape lines sit in an exact arithmetic run of three or more. Rejected: a `\foreach`
  wrapper for 0.4% of the lines is machinery, which is what this lane is removing.
- **Rounding coordinates.** It cannot help at all: `readability.NUM` counts a literal whether it is
  `63` or `63.174`, so precision is free to the ruler and costs only pixels. Rejected on both sides.
- **Dropping a shape that draws nothing** (a white rect at `fill opacity` on white, a shape another
  shape covers completely). Not attempted, and deliberately: the brief allows it only with a proof
  by construction, and "nothing is on top of it" is a fact about the whole paint order - layout
  decoration, pictures, the deck's own background - which the writer does not model per pixel. A
  line saved against a proof that holds for one deck's thumbnail is not a line saved.

## The loop, measured at last (2026-09-26: `vb-before` -> `vb-after` -> `vb-pair`, `base` replay)

Three decks people brought from inside Google (a one-word title slide, the saudi-cats deck, a third)
failed to converge, and the bench explained why nobody had seen it: `run` scored the bootstrap only
(`iters 0`), so the loop had never run on a corpus deck. With `--iter 2` (now reported per deck:
`CONVERGED` or `open a -> b -> c`, and a summary of how many decks ended worse):

- **0 of 30 decks converged; 16 ended worse than their first draft** (open residuals 6907 -> 8387;
  sc-memphis boxes 0.994 -> 0.118, sc-dark-modern 0.993 -> 0.030). Cause: a foreign deck's slides
  have no keys, so `compare.match_slides` paired adopt's frames by title and text only; untitled or
  look-alike slides found no mate (jeb-arch 6/7, sc-memphis 19/20) and the loop deleted their frames
  and wrote a bare flow frame for each (sc-memphis lost 3382 of its source lines). Fixed:
  `compare.target_keys` gives such a slide the label `adopt.frame_labels` wrote. Ten small decks
  after it (`vb-pair`): no collapse (sc-memphis 0.988, jeb-arch 0.928), still 0/10 converged, open
  5314 -> 5242, and sc-functions 0.985 -> 0.788, plain-fonts 0.957 -> 0.831.
- Two more before it: a BOTTOM/MIDDLE-aligned box's first baseline was read as if top-aligned (the
  default theme's title slide, 57 pt off; `deck_ir.stacked_baseline`, `compare.stacked_y`), and every
  face `fontfetch` cut from a variable font called itself by the default instance (Montserrat-Thin:
  bold headings read back as not bold; `fontfetch.rename`, `repair_names`).

**Replay** (`devtools/adopt_replay.py`, tag `base`): round 0 alone, over all 32 decks, with the
compile and the target cached (4 min cold, 17 s warm). Of 16,569 open residuals, **12,743 are on
slides whose ink already scores >= 0.97**: `element_missing` 7,000, `paragraph_missing` 1,929,
`paragraph_extra` 769, `geometry` 756. They are the read-back's, not the source's: classify was made
for beamer PDFs, and on an adopted page a full-page picture takes every box drawn over it into a
figure (drawing-workshop slide 1: 2 of 23 elements read back), shapes are background, and stacked
text boxes merge. Seven decks read back with another page count (notes pages kept as slides:
saudi-cats 14 pages for 7 slides, cs161-net 88 for 61). What the loop does with those residuals is
the damage above. Next: tag every element `slides.sty` draws in the PDF (marked content, which PDFium
reads per page object) so the read-back of an adopted page lists what adopt wrote, not a guess.

**Micro-corpus** (`adopt_bench.MICRO`, `run --micro` in both tools): 36 single slides, one per
family the replay found, each a slide the first draft already inks well that the read-back still
finds tens to hundreds of residuals on (shapes over pictures, a full-page picture, stacked boxes,
notes, right-to-left, tables, CJK, an A4 page, Slides' layouts, variable-font weights). Replay tag
`micro-base`: 2023 open, 1903 suspect, three slides read back as two pages (cs161-net:15,
cs161-tls:9, instagram:2: the notes page); 133 s cold (a one-frame compile of sc-functions or
devfest2020 still takes ~100 s, all preamble and pictures), **3.7 s warm**. `adopt_bench run
--micro --iter N --tag T` runs the loop on the same slides (`NAME:a-b` per deck, tagged `T-sa-b`).

The loop on the micro-corpus (`adopt_bench run --micro --iter 2 --tag micro`, 6.4 min at 12 jobs, the
~350 s devfest2020/gdg24 slides setting the wall clock): 1/36 converged, open 2023 -> 1985, 7 slides
worse. The one that converged was the worst: plain-fonts:5 (a big "7" over a line) went 0.979 ->
0.267, because `compare.counts_as_text` took any lone number for a frame counter, so the target's
title "7" was never compared; the read-back merged it into the line's box, the loop deleted the
"extra" 7 and nothing missed it. A title is no longer a counter (0.979 kept, converged). Still
collapsing on the planner's side, from read-back misreadings: plain-layouts:6 0.993 -> 0.501 (a
wrapped paragraph read as several; the box emptied and a fragment written in a textblock),
ds-lecture:13 0.988 -> 0.439 (`\slidebreak` rewritten as `\\`, an item copied out of its list),
sc-functions:8 0.984 -> 0.372, ap-bio-stats:40 0.989 -> 0.627, drawing-workshop:6 0.999 -> 0.574.

## The frame guard (2026-09-26: `noguard` -> `guard3`, `blind3`)

Pull's loop now never leaves a frame worse than it found it (`frame_guard.py`, called by
`inverse.converge`). Every round it remembers each frame's text and scores the page it made; when the
loop stops, each frame ends at its **best round**, and the rest of the source (the preamble's added
colours and packages) stays as the loop left it. A frame is one unit: its target slides are the ones
compare paired with its pages, and it is known across rounds by its label. Best means:

- **ink** when the target carries Google's own picture of the slide (a foreign read keeps its
  thumbnail's path, `deck_ir`; the bench hands the corpus's `slides/NNN.png`): the page's `boxes`
  score against it, moved from the bench into the package (`page_score.py`; the bench imports it).
  Rounds within 0.0005 of the best tie. It needs nothing from the read-back, which is the point.
- then the **fewest weighted residuals** (`RESIDUAL_WEIGHT`: words 3, elements 2, places 0.5) plus one
  per word the page has wrong (the page's words as extract reads them). Pull has no picture, so this
  is its whole score.
- then the **earliest round**: a round has to earn its edits. The first cut only put back frames
  worse than their best by a margin, and that missed the commonest case: sc-functions:8 read the
  same 66 residuals after round 1 while its ink fell from 0.984 to 0.912, arabic-training:6 read 29
  and 29 from 0.960 to 0.805. A 0.005 ink margin also let five slides keep rounds that cost a
  thousandth or two of ink and bought nothing (apps-edu-zh:9, creandum-board:22).

What was put back and why is in the result (`restored`, with the source lines) and in `edits.md`
("Frames put back"); the agent tools add one note per frame. `adopt_bench run --no-guard` runs the
loop without it, `--blind` hides the thumbnails so the residual score is measured alone.

The micro-corpus, `--iter 2`: first draft, the loop without the guard, with it, and blind. The 25
slides the loop changed:

| slide | first draft | no guard | guard | blind | open, no guard | blind ends at |
|---|---|---|---|---|---|---|
| plain-layouts:6 | 0.993 | 0.501 | 0.993 | 0.993 | 5 -> 2 -> 5 | 5 |
| ds-lecture:13 | 0.988 | 0.439 | 0.988 | 0.988 | 15 -> 12 -> 14 | 15 |
| sc-functions:8 | 0.984 | 0.372 | 0.984 | 0.984 | 66 -> 66 -> 67 | 66 |
| sc-functions:11 | 0.973 | 0.530 | 0.973 | 0.973 | 78 -> 78 -> 80 | 78 |
| ap-bio-stats:40 | 0.989 | 0.627 | 0.989 | 0.989 | 44 -> 53 -> 46 | 44 |
| drawing-workshop:6 | 0.999 | 0.574 | 0.999 | 0.999 | 62 -> 64 -> 64 | 62 |
| drawing-workshop:1 | 0.948 | 0.886 | 0.948 | 0.948 | 36 -> 34 -> 34 | 36 |
| arabic-training:6 | 0.960 | 0.805 | 0.960 | 0.960 | 29 -> 29 | 29 |
| sc-dark-modern:17 | 0.971 | 0.904 | 0.971 | 0.904 | 25 -> 21 | 21 |
| sc-dark-modern:18 | 0.993 | 0.980 | 0.993 | 0.980 | 46 -> 44 | 44 |
| sc-memphis:16 | 0.999 | 0.974 | 0.999 | 0.999 | 297 -> 297 -> 298 | 297 |
| sc-memphis:9 | 1.000 | 0.992 | 1.000 | 0.992 | 82 -> 82 -> 82 | 82 |
| sc-memphis:2 | 0.987 | 0.978 | 0.987 | 0.987 | 210 -> 210 -> 210 | 210 |
| comic-strips:7 | 0.974 | 0.960 | 0.974 | 0.974 | 26 -> 28 -> 28 | 26 |
| drawings-basics:13 | 0.983 | 0.969 | 0.983 | 0.969 | 30 -> 30 -> 29 | 29 |
| journey-maps:15 | 0.953 | 0.939 | 0.953 | 0.939 | 55 -> 10 -> 6 | 6 |
| journey-maps:2 | 0.982 | 0.976 | 0.982 | 0.976 | 34 -> 30 | 30 |
| sc-aesthetic-school:21 | 1.000 | 0.993 | 1.000 | 0.993 | 44 -> 44 | 44 |
| sc-river-a4:2 | 0.991 | 0.986 | 0.991 | 0.991 | 55 -> 55 -> 55 | 55 |
| devfest2020:35 | 0.984 | 0.970 | 0.984 | 0.984 | 126 -> 126 -> 145 | 126 |
| gdg24:4 | 0.999 | 0.997 | 0.999 | 0.997 | 67 -> 64 -> 64 | 64 |
| supercharge-slides:31 | 0.982 | 0.980 | 0.982 | 0.982 | 27 -> 27 | 27 |
| apps-edu-zh:9 | 0.987 | 0.986 | 0.987 | 0.987 | 13 -> 13 -> 13 | 13 |
| creandum-board:22 | 0.971 | 0.970 | 0.971 | 0.971 | 22 -> 22 | 22 |
| devfest2020:39 | 0.998 | 0.997 | 0.998 | 0.998 | 86 -> 86 -> 86 | 86 |

- no guard: `loop ink: boxes 0.9847 -> 0.8930 over 36 slides; 19 slide(s) below their first draft`
  (by more than 0.005; all 25 are below), open 2022 -> 1985.
- guard: `loop ink: boxes 0.9847 -> 0.9847 over 36 slides; 0 slide(s) below their first draft, 0
  above; guard put back 25 frame(s)`, open 2022 -> 2022. The five named slides alone (`guard3`,
  `--jobs 5`): 0.9906 -> 0.9906, 0 below, 5 put back.
- blind: `loop ink: boxes 0.9847 -> 0.9811 over 36 slides; 8 slide(s) below their first draft, 0
  above; guard put back 17 frame(s)`, open 2022 -> 1959 (before the earliest-round rule: 0.9758 and 15
  below).

**No round of the loop raised any slide's ink.** The two gains it was credited with are losses by the
deck's own picture: journey-maps:15 open 55 -> 6 went 0.953 -> 0.939, sc-dark-modern:17 25 -> 21 went
0.971 -> 0.904. The loop cleared `paragraph_extra`/`element_extra` residuals by deleting or merging what
the read-back called extra. So with a picture the guard puts all 25 back, and the open count after it
is the first draft's to the residual (2022), which also says a put-back rebuilds the first draft
exactly. Blind, the 8 still below are rounds the residuals really do score better, most often by an
`element_missing` turning into a `geometry` (sc-memphis:9, sc-aesthetic-school:21: 2 weight for 0.5):
the read-back found the element after the edit, in the wrong place. Nothing without a picture can
tell those apart; the read-back's misreadings above are what to fix, and the guard is what keeps them
from costing a slide meanwhile.

## Read-back from marks (2026-09-26: `micro-base` -> `marks`, `base` -> `marks-full`, bench `marks`)

Round 0 no longer guesses what is on an adopted page. Before this, it classified the compiled PDF like any
beamer deck. Now it reads what adopt wrote. `slides.sty` (lualatex, `\pdfextension literal page`) wraps
everything it draws in marked content, and the read-back takes those marks as the page's structure.

**The marks.** `slides.sty` writes these:

- `/B2S <</k key /n N /t kind ...>> BDC ... EMC` around each element.
  - `k` is the target's key (`adopt.mark_key`).
  - `n` is its drawing order, with the layout template last.
  - `t` is the kind: text, shape, image, table, line.
  - A text box and a shape also say `/box (x y w h)`: the box adopt set them in.
  - A line says `/line (x1 y1 x2 y2)`: its two points.
  - A table says its `/rows` and `/cols`.
- `/B2Sp <</i /a /l>>` around each paragraph: its index, align and level.
- `/B2Sb` around each bullet.
- `/B2Sc <</r /c>>` around each table cell.
- `/B2Su` and `/B2Ss` around the words `\uline` and `\sout` decorate. They wrap ulem's own commands at
  `\AtBeginDocument`, only when those commands exist.

**Reading the marks.** The PDF layer gives each page object its marked-content stack (both backends,
`pdf/api.py`). `extract` puts `marks` on spans, drawings and images, and `extract.page_marks(page)`
maps object id to marks.

`marked.py` splits a page by element mark, then reads each group:

- A text group is classified alone. Its paragraphs come from `/B2Sp`, not from line gaps, and its
  bullets from `/B2Sb`.
- A shape takes its `/box` when the drawn bbox fits inside that box, grown by half the widest stroke
  plus 1.5 pt. Otherwise a traced outline would be read as a smaller box.
- A line takes its two `/line` points.
- A table takes its grid from `/rows`, `/cols` and the cell marks.
- Underline and strike come from their marks, not from rules found under the words.

An unmarked page, meaning any deck adopt did not write, takes the old path unchanged.

A text box's `/box` is kept only as data (`mark_box`). `\slidetext` places its box at the target's text
anchor, not at the target's bbox. Using `mark_box` as the element's edge made geometry worse
(ap-bio-stats:40 dx -4.25), so that change was reverted.

**Words a later picture hides.** extract drops text under a later opaque image, which is right for a
beamer deck. On an adopted page, though, those words are the person's own: in Slides they sit under a
picture too. On a page with marks, `extract` now keeps such runs as `hidden_spans` (ids `p{n}h{i}`).
`marked.split` gives them to their element's group, and `text_elements` moves them from `spans` into
`hidden_spans`, because render erases whatever `spans` names.

**Compare.**

- A candidate element pairs with a target element by key (`compare.target_key`, `keyed_elements`,
  `keyed_paragraphs`) before any text or geometry pairing.
- A target placed off the page is `parked`: it is not reported as missing.
- Paragraph order is checked within each marked box. The boxes themselves are not reordered against
  each other: two stacked boxes are two elements, not one element's paragraphs out of order.

**Note pages.** `notes.prepare` took some adopted slides for beamer note pages: a band across the top
with small words at its right end (drawing-workshop 28 and 50). That changed the page count, 61 != 63.
Now:

- a page that carries marks is always a frame;
- a note page whose thumbnail is an empty quarter-size canvas still counts as a note page
  (`_thumbnail_canvas`). textpos blocks never reach `\insertslideintonotes`, so the canvas is all
  such a note page shows.

**Replay, micro-corpus** (`--micro --against micro-base`, open / suspect):

| step | open | suspect |
|---|---|---|
| `micro-base` | 2023 | 1903 |
| elements, paragraphs, bullets, cells from marks | 648 | 601 |
| key pairing, off-page | 206 | 175 |
| shape `/box` | 191 | 160 |
| `/line`, underline and strike marks | 150 | 119 |
| hidden words, per-box order (`marks`) | 123 | 95 |

0 slides lost ink against `micro-base`. The 95 suspects by kind: geometry 24, text 22, style 21,
shape 19, element_missing 7, paragraph_missing 1, align 1.

**Replay, whole corpus** (`--against base`, saved `marks-full`).

| | open | suspect |
|---|---|---|
| `base` | 16569 | 12743 |
| `marks-full` | 1449 | 779 |

- 31 decks were replayed. firebase-jam still fails to compile, as it did in `base` (main.tex:617,
  "There's no line here to end").
- Ink is 0.962.
- No deck's page count differs any more. `base` had 7 such decks; drawing-workshop 61 != 63 is still
  in it.
- The 779 suspects by kind: geometry 251, text 140, style 100, element_missing 84, shape 73,
  paragraph_missing 41, element_extra 34, paragraph_extra 22, align 15, notes 8, table 6, bullet 4,
  image 1.

The diff against `base` shows three gdg24 slides losing ink: 52 went 0.910 -> 0.904; 53 and 56 went
0.982 -> 0.981. These drops are not caused by the marks. A worktree at 65383aa, before the marks,
compiled today gives the same ink as the new code on all 99 gdg24 slides. The difference is `base`'s
older compile.

**The loop** (`adopt_bench run --micro --iter 2 --tag marks --jobs 12`):

| | before (`guard3`) | `marks` |
|---|---|---|
| boxes | 0.9847 | 0.9847 |
| converged | 1/36 | 11/36 |
| open residuals | 2022 -> 2022 | 123 -> 123 |
| loop ink | 0.9847 -> 0.9847 | 0.9847 -> 0.9847 |
| slides below / above their first draft | 0 / 0 | 0 / 0 |
| frames the guard put back | 25 | 11 |

First-draft ink did not drop on any slide. The loop now stops on its own where the page is already
right, where before it had to be stopped. Still, no round raised any slide's ink.

**What the read-back still reports wrongly, by family:**

- **Bold in a face with no bold cut.** NTR in sc-functions:8 and :11, 30 style residuals. The PDF is
  regular; Slides fakes the bold. This is a typesetting limit, not a misreading.
- **Style on drawing-workshop**, 57 residuals. Examples: a bold "Create a"; "Curved" coloured #00aeef
  where the target has #00ffff.
- **Geometry.**
  - gdg24 57, cs161-tls 34, cs161-net 27, supercharge-slides 26.
  - Arc shapes, and rotated or flipped shapes. The marks give the unrotated box, not the transform.
- **Text.**
  - cs161-tls 35.
  - sc-functions reads "- 1" for "-1".
  - sc-dark-modern:17 reads "AdditionalResources": mono spacing laid out by code pitch.
- **element_missing.** devfest2020 35.
- **Shape.** cs161-net 32. Its targets are outline-only, and adopt draws them as filled traced rings:
  fill #00882b where the target has none.
- **arabic-training.** RTL text and paragraph residuals.

## saudi-cats, one defect family at a time (2026-09-26)

saudi-cats is a private deck someone reported. It is Montserrat and Roboto, with Arabic, curly
quotes, speaker notes and photos, and it was adopted with `--no-downloads`. It is kept out of the
public manifest and out of MICRO. Each defect was isolated on a one-slide replay
(`adopt_replay run saudi-cats:N-N`, seconds), fixed, and then checked on the whole deck and the
corpus.

| defect | cause | fix |
|---|---|---|
| `"` and `--` became curly quotes and an en dash | fontspec's default `Ligatures=TeX` | `adopt.TEX_LIGATURES_OFF` (25ab88f) |
| Arabic drawn as tofu | the deck's second typeface (a `\newfontfamily` switch) lacks Arabic, and babel's `onchar=ids fonts` only swaps families it knows | `scripts.switch_font_lines`: a `\babelfont{<switch>}` with a per-language face, only for languages the face lacks (b499a72) |
| notes pages read as slides | the note template's header band and thumbnail canvas | marked pages are frames (`notes._thumbnail_canvas`, the marks merge) |
| a note hyphenated at a line end was a residual | LuaTeX breaks after an explicit `-` in `\note` whatever the penalties | `compare.norm_notes` (5f97834) |
| paragraphs merged in the read-back | the classifier joined boxes | marks (`/B2Sp`) |
| Roboto table cells set in Montserrat | the font census in `font_preamble` looked at text boxes only, not cells or group children | the census walks `text_elements` |
| 11 photos missing, and nothing said so | `--no-downloads` refused both the fetch and the Drive export; `element_latex` wrote nothing | `adopt.pictures_missing`: logged, agent `data["pictures_missing"]`, a `% picture left out` comment where each one went |

The whole-deck replay after the marks merge: open residuals 136 -> 0, suspects 105 -> 0, ink 0.989.

The Arabic fix itself broke drawing-workshop: in `font_preamble` the switch branch's set of
languages took the name `lacking`, which is the recorder of missing fonts, and the recorder was
called again for the next font ('set' object is not callable). The corpus baseline at 193e6e0
showed it as an ERROR row. With the rename, drawing-workshop replays at ink 0.989, open 363, 63 of
63 pages (`base`: 0.853, 962, 61 pages). The census and pictures changes left every other corpus
source byte for byte as it was (`census` vs `head-193e6e0`: every compile cached, open 1076 ->
1076).

**The one-slide trap.** A one-slide replay makes font decisions from that slide's letters alone.
The main font and the switch fonts are chosen by letter counts over the deck. So a slide replayed
alone can be set in a different main face than it gets in the whole deck. This hid the Arabic bug
(on its own the slide's Arabic went to the main font, which has Arabic). It also misled a table
diagnosis. Before believing a one-slide result about fonts, check it on the whole deck.

**Model calibration for this work.** Each model was tested on a known answer before it was trusted.
- Haiku is good for triage only: which slides, which residual kinds. It misdiagnosed a "7" bug that
  had a known cause.
- Sonnet passed a scoped diagnosis: one defect, the files named, a written hypothesis to check.

**Open findings, not fixed:**
- Slides breaks a line after "°" ("70°" / "C"). TeX does not. That line then breaks elsewhere in
  the source.
- A Montserrat Bold header is set slightly wider than Slides sets it. The cause is not isolated.
- The first Arabic fix, which also sent Arabic from faces that have it to babel's font, raised ink
  a little on arabic-training slides 1-3, 11 and 21. That suggests a font there draws Arabic
  differently from Slides. It was not pursued.
