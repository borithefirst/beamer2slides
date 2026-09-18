# The playground

A small web app that shows the whole pipeline on a talk someone types, picks or uploads.

```
python -m beamer2slides playground [--host 127.0.0.1] [--port 7860]
```

It is the standard library's HTTP server and a static page (`src/beamer2slides/playground/`):
no new dependency, nothing to build.

## What it shows

- **Try it**: an editor with the example talks (the demo talk and four test decks), *Compile &
  convert*, or a PDF upload. The job then runs for real, one stage after another, each with its
  time shown: pdflatex (lualatex when the source loads `fontspec`), extract + classify, render.
  Every slide of the result can be seen five ways, next to its PDF page:
  - *Editable preview*: the slide rebuilt from `deck.json` over its background picture: text in
    the calibrated substitutes (Lato, PT Serif, Roboto Mono, from Google Fonts), shapes, tables,
    diagrams and pictures. Every text is `contenteditable`, and a formula picture follows the gap
    its text keeps for it as one types, as it does in the deck (where `emit.measure_places` measures
    the gap on Google's renderer). It is an approximation made in a browser; the real rendering is
    Google's and `fidelity` measures that one.
  - *What becomes native*: every element's box on the PDF page, coloured by kind, with its words
    in a tooltip.
  - *Background*: what stays a picture.
  - *Classify debug*: classify's own debug image.
  - *IR*: the slide's `deck.json` entry.
- **Sync & adopt (recorded)**: the Google side, from the front page's recorded runs
  (`docs/media`, made by `tools/readme_demos.py` and `tools/readme_images.py`).

A link to a job (`?job=<id>&slide=3&mode=native`) opens that job, slide and view, with its source
in the editor, for as long as the server keeps the job.

## The Google deck

A public playground must never convert into its owner's Drive, so the *Create the Google Slides
deck* button only exists where both hold:

- `B2S_PLAYGROUND_GOOGLE=1` is set, and
- the server finds a `token.json` (`google_auth.credential_file`, as for the command line).

It then runs `emit` on the job (a new deck each time) and links it. That is for running the
playground on one's own machine; everywhere else the recorded runs show that part.

## Limits

| | |
|---|---|
| jobs at a time | one (a worker thread; the stages print, and pdflatex is heavy) |
| waiting | at most 8 (`MAX_QUEUE`), then 503 |
| pages | 40 (`MAX_PAGES`) |
| upload | 25 MB |
| each TeX run | 90 s, twice (navigation needs the second) |
| jobs kept | the last 40 finished ones; folders an earlier run of the server left are swept at start |

Jobs live in `$B2S_PLAYGROUND_JOBS/<port>` (default: the temp folder's `b2s-playground`); one
folder per port, so a second server on the same machine does not sweep the first one's jobs.
Examples and pictures are read from the checkout, or from `$B2S_PLAYGROUND_ROOT`.

## Hosting it

The `Dockerfile` at the root is the whole deployment: Python 3.12, TeX Live from Debian (the
packages the examples need, plus luatex/xetex), beamer2slides, the example sources and `docs/media`,
a non-root user (uid 1000) and port 7860. That is what a Hugging Face Docker Space expects (a Space
whose README front matter says `sdk: docker` and `app_port: 7860`), and any container host (Cloud
Run, Fly.io, Render) runs it as is:

```
docker build -t beamer2slides-playground .
docker run --rm -p 7860:7860 beamer2slides-playground
```

A TeX source from strangers is code. What keeps it contained:

- shell escape off (`-no-shell-escape`), so `\write18` runs nothing;
- `openin_any=p` / `openout_any=p` (TeX Live's paranoid mode, set in the image and for every run):
  no reading or writing absolute paths or `..`, so `\input{/etc/passwd}` is refused;
- a time limit per run and a page limit per job;
- the container: a non-root user, no credentials in the image (`.dockerignore` lets only the
  package, the example sources and `docs/media` in), and no Google side.

MiKTeX ignores `openin_any`, so on Windows the playground is for one's own machine.
