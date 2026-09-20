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

A public playground must never convert into its owner's Drive, so whose Drive a deck goes into
is what decides whether the *Create the deck* button exists at all (`server.google_mode`):

| mode | asked for by | the deck lands in |
|---|---|---|
| `local` | `B2S_PLAYGROUND_GOOGLE=1` and a `token.json` (`google_auth.credential_file`) | the server owner's Drive |
| `signin` | `B2S_PLAYGROUND_GOOGLE_CLIENT_ID=<an OAuth **web** client>` | the visitor's own Drive |
| off | neither | nowhere: the recorded runs show that part |

`local` is for one's own machine. `signin` is the only one a public host may use: the visitor
clicks, Google's sign-in script (`accounts.google.com/gsi/client`, loaded only in this mode)
hands the page an access token, the page sends it with that one request, and the server holds it
for the one `emit` call through `google_auth.use_provider`. Nothing is stored, and no refresh
token exists - so the host keeps no credentials of anyone's, and a visitor who closes the tab has
left nothing behind. `use_provider` is process-wide, so `to_slides` takes a lock: two visitors
converting at once must never build with each other's credentials. A client id is not a secret;
it identifies the app to Google and belongs in the deployment's environment, not in git.

The browser asks for **`drive.file` alone** (`server.WEB_SCOPES`), which reaches only the files
the app itself creates. That is enough for the whole pipeline - the deck is imported as a .pptx
and then edited, so every file it touches is its own - and **measured**: a token carrying only
`drive.file` built the demo deck end to end. It matters beyond tidiness, because `presentations`
(what the command line asks for) is a *sensitive* scope, and a published app asking for one must
pass Google's review - a privacy policy, a verified domain, a demo video, weeks of waiting -
while an app asking only for `drive.file` needs none of it.

Until the consent screen is switched from *Testing* to *In production*, only the test users it
lists can sign in; everyone else is refused by Google before the playground sees anything.

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
a non-root user (uid 1000) and port 7860 (or `$PORT`, which is how Cloud Run says where to listen).
That is what a Hugging Face Docker Space expects (a Space whose README front matter says
`sdk: docker` and `app_port: 7860`; hosting one there now needs a PRO account), and any container
host (Cloud Run, Fly.io, Render) runs it as is:

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

### On Cloud Run

No Docker and no image registry of one's own are needed: Cloud Run builds the Dockerfile from the
source it is handed. From a checkout, with the project's billing account linked:

```
gcloud run deploy beamer2slides-playground --source . --region europe-west1 \
  --allow-unauthenticated --memory 2Gi --cpu 2 --timeout 900 --max-instances 1 \
  --set-env-vars B2S_PLAYGROUND_GOOGLE_CLIENT_ID=<the web client id>
```

What the flags are for, beyond taste:

- **`--max-instances 1`** is not only about money. A job lives in the server's memory and on its
  disk, so a visitor's next request must reach the same container; a second instance would answer
  "no such job". One instance with the queue in front of it is the design (`MAX_QUEUE`), and it is
  also the spending cap: Cloud Run bills per instance-second, and there can only ever be one.
- **`--memory 2Gi`**: jobs live in `/tmp`, which on Cloud Run is memory, and TeX Live plus a
  render of 40 pages wants room. `KEEP_JOBS` (40 finished jobs) is what that has to hold.
- **`--timeout 900`**: a compile and a conversion take seconds, but the deck build waits on
  Google's API; the default 5 minutes is close enough to be worth raising.
- **`--allow-unauthenticated`** is what makes it a public playground rather than a private one.

The service's URL (`https://<service>-<hash>.<region>.run.app`) must then be added to the OAuth
web client's **authorized JavaScript origins**, or the browser's sign-in is refused before it
starts. Cloud Run scales to zero, so an idle playground costs only storage of the built image.

What `--source .` sends to Cloud Build is `.gcloudignore` - the same set as `.dockerignore`, but
read with **git's** rules rather than Docker's: a file only comes back if every folder above it
came back first, and re-including a folder brings its whole subtree, so each one has to be cut
down again. `gcloud meta list-files-for-upload` prints exactly what a deploy would send, which is
how one checks that no credential is among it.

The running deployment is
[beamer2slides-playground](https://beamer2slides-playground-702466108736.europe-west1.run.app),
with a CHF 50/month budget whose **spend cap** (Preview; Cloud Run is one of the services it can
enforce on) pauses the service rather than only mailing about it. `--max-instances 1` is the other
half of that: there is never a second instance to bill for.

### The consent screen

The playground serves its privacy policy at `/privacy` (`static/privacy.html`), because Google
will not let an External app leave *Testing* until the Branding page has an application home page
and a privacy policy link, and every link's domain must be an authorized domain - which is why
both live on the service's own `run.app` URL rather than on GitHub. What the policy says is what
the code does: `drive.file` only, the token used for one conversion and kept nowhere, jobs deleted
as they roll over.
