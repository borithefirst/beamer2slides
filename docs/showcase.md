# The public adopt gallery

https://borithefirst.github.io/beamer2slides/ shows `adopt` on six Google Slides decks, slide by slide:
Google's thumbnail, the compiled adopted PDF and the frame that draws it, with a view-only link to
every original and each deck's adopted source.

## Why our own decks

The adopt benchmark corpus (`tests/decks/foreign/corpus.json`) is other people's work. We checked on
2026-09-26 and found no licence that lets us republish those slides: SlidesCarnival's terms (CC BY
4.0 with attribution) forbid redistributing the templates as they are, CS161's decks carry no
licence, the GDG/DevFest templates are Google brand assets, and the rest are individual authors'
decks. So the gallery shows decks we wrote for it, `devtools/showcase/decks.py`:

| deck | how it is made | what it exercises |
|---|---|---|
| `hashing` | Slides API only | Google's layouts and placeholders, nested and numbered bullets, a diagram, code, a table |
| `talk` | .pptx | a master and layouts with decoration (shapes, a picture), stat cards, a timeline, a link |
| `review` | .pptx | KPI cards with ring pictures, a heatmap table, a chart picture, check-box bullets |
| `bees` | .pptx | preset hexagons, freeforms (star, clouds, arrow), a callout, rotated cards |
| `water` | .pptx | Hebrew and Arabic right to left, Japanese, Chinese, a multilingual table |
| `portfolio` | .pptx | full-bleed and cropped pictures, Dancing Script and Playfair Display |

Every picture is drawn from a seed by `devtools/showcase/art.py`. The decks live in the owner's
beamer2slides Drive folder, shared "anyone with the link can view" (the owner's display name shows
to visitors). `decks.json` records their ids and page ids; rebuilding a deck replaces its content in
place, so links stay valid.

## Steps

```
python tools/showcase.py decks [NAME...]    # build or rebuild in Drive, share, update decks.json
python tools/showcase.py capture [NAME...]  # adopt_bench capture into out/showcase-corpus
python tools/showcase.py run                # adopt_bench run, tag `showcase` (MiKTeX on PATH)
python tools/showcase.py gallery out/showcase/site
python tools/showcase.py swipe out/showcase/site  # the README's docs/media/adopt-swipe.gif
python tools/showcase.py fixture            # the targets into tests/decks/foreign/showcase
```

`fixture` copies each captured target, with its pictures shrunk to 64 px, into the repository.
`tests/test_adopt_compiles.py` adopts and compiles every one of them. CI runs that test on TeX Live
2022 and on the current TeX Live (`.github/workflows/texlive.yml`), with no fonts and with the decks'
own fetched fonts. Run `fixture` again after a `capture`.

The page opens in Swipe view: Google's render left of a divider, the adopted PDF right of it, and
each divider sweeps across once when its slide scrolls into view (not with reduced motion), because
a divider standing still over two near-identical renders looks like one picture.
`gallery.SWIPES` picks the README animation's slides: flat colours keep the GIF under 1 MB (one
palette per slide, so a frame stores only the strip around the divider).

`gallery.PICKS` chooses the slides and holds each caption. The captions say what adopt wrote and
where it falls short, so re-read them against the new frames after an adopt change. The site is
`index.html` (data inlined into `template.html`), `img/`, and `src/<deck>.zip`. A zip holds the
adopted tree without `fonts/`: those are static instances adopt makes of the deck's Google Fonts,
and a tree may hold a face copied from the machine that ran adopt (the first water run had Windows'
Arial). `FONTS.txt` lists what `fonts/` should contain.

## Publishing

The site is the orphan branch `gh-pages` (Pages source: that branch, `/`). To update it, build the
site, then copy it over a worktree of `gh-pages` and commit only that:

```
git worktree add ../b2s-pages gh-pages
python tools/showcase.py gallery ../b2s-pages
git -C ../b2s-pages add -A; git -C ../b2s-pages commit -m "Gallery: ..."; git -C ../b2s-pages push
```

## What the decks found in adopt (2026-09-26)

- Fixed: a script font on google/fonts only (Noto Sans Hebrew, Noto Sans Arabic) was never fetched
  before `scripts.plan` picked a face, so a first run set those letters in Arial
  (`test_the_deck_s_own_hebrew_font_is_fetched_before_one_is_picked`). The water deck went from
  0.949 to 0.965.
- Open: the API reports no adjustment values, so rounded-rectangle radii and callout tails take the
  preset's defaults (talk 3, review 2, bees 2). Shape text insets are not reported either (review 5).
- Open: a freeform with an outline (water's cloud) comes back as a rectangle, and a freeform over a
  shape of its own colour (the wavy sea edge) as a flat strip.
