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
