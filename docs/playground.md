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
- **Workbench**: a folder on the server and every journey run inside it - see below.
- **Sync & adopt (recorded)**: the Google side, from the front page's recorded runs
  (`docs/media`, made by `tools/readme_demos.py` and `tools/readme_images.py`).

A link to a job (`?job=<id>&slide=3&mode=native`) opens that job, slide and view, with its source
in the editor, for as long as the server keeps the job.

## The workbench

*Try it* is one road - a talk in, a deck out - and everything else this library does (sync,
pull, adopt, the whole Google Docs side) is a road it does not have. The workbench is the rest:
a folder per visitor, a file editor, and the eleven journeys of the agent layer
(`src/beamer2slides/agent/`, docs/agent-tools.md) run in it, one form each.

Nothing is reimplemented. The tools come from `agent.tools.TOOLS`, their forms are built in the
browser from `agent.schema.all_schemas()` - so what a visitor fills in and the signature that
runs are the same text - and what comes back is the `Result` every tool answers in: the summary,
the diagnostics, the files it wrote (clickable, they open in the editor) and what to consider
next. `INSTRUCTIONS.md`, the rules these tools are used by, is on the page under the log.

`tex_compile` is the twelfth entry and the only one that is not a journey: it is how a source in
the workspace becomes the PDF the deck journeys start from.

A round trip is therefore: `tex_compile` on `talk.tex`, `deck_inspect` on `talk.pdf`,
`deck_convert` into your own Drive, edit the deck in Slides and `talk.tex` here, then
`deck_sync` with `dry_run` on to see what the merge would do. The Docs side starts at `doc_push`
on `doc.html`. A new workspace is seeded with a talk, a canonical document and a README saying
exactly that.

**There is no shell.** The only two things the workbench executes are a TeX engine and its own
journeys, and both are fenced:

- a journey runs in a **subprocess** (`runner.py`). `@tool` serialises one journey per process,
  so two visitors would otherwise wait on each other's conversion; the library's own LaTeX
  compiles (`inverse.Compiler`, which `pull` and `converge` loop over) carry no time limit of
  their own, and a process can be killed where a thread cannot; and a journey that dies takes
  nothing of the server with it. The job goes in on **stdin**, so a visitor's access token is
  never in a command line, and progress lines and the `Result` come back as JSON lines.
- a run is stopped after `RUN_TIMEOUT` (420 s), and the kill takes the whole process group - a
  journey's TeX run is not its last breath.
- every path a visitor names goes through `LocalWorkspace.resolve`, the agent layer's own
  boundary: one that climbs out is refused by the same code a journey's would be.
- `shell_escape=f`, `openin_any=p` and `openout_any=p` are set for everything a run starts, so
  the compiles the library does for itself are fenced as the playground's own are.
- the workspace holds 80 MB and 3000 files; one upload is 25 MB; the last 12 workspaces stay.

Google works as it does everywhere else on this page: in `local` mode with the host's own token,
in `signin` mode with the visitor's, asked for at the click and handed to that one child process.
The server asks for it only where the journey needs it (`effects.google`), and a local journey
carries nobody's credentials.

### Reaching a deck this app did not make

`drive.file` reaches only the files the app itself created, which is what keeps it a
non-sensitive scope - and what makes a deck somebody else built invisible to `deck_adopt` and
`deck_pull`. The [Google Picker](https://developers.google.com/drive/picker) is Google's own
answer: the visitor chooses the file in Google's own window, and that choice grants this app
`drive.file` on that one file. Where `B2S_PLAYGROUND_GOOGLE_API_KEY` names a browser API key
(Picker API enabled, restricted to the service's referrer), the `deck` and `doc` arguments grow
a *Pick from Drive…* button. An API key is not a secret; like the client id it belongs in the
deployment's environment. Without one the buttons are simply not there.

They are a `signin` thing only. The Picker wants an OAuth token from the *browser*, and the
browser has one exactly where visitors sign in; in `local` mode the token is the host's own, it
carries `presentations` as well, and it already reaches whatever its owner can open - so there is
nothing for a Picker to grant there, and the button would be a promise the page cannot keep.

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
| workbench runs | one at a time, stopped after 420 s (`B2S_WORKBENCH_TIMEOUT`) |
| a workspace | 80 MB, 3000 files; the last 12 stay |

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

- shell escape off (`-no-shell-escape` on the command line, `shell_escape=f` in the environment
  for the compiles the library starts for itself), so `\write18` runs nothing;
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
  --set-env-vars B2S_PLAYGROUND_GOOGLE_CLIENT_ID=<the web client id>,B2S_PLAYGROUND_GOOGLE_API_KEY=<the browser key>
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
