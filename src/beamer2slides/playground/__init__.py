"""The playground: a small web app that shows the whole pipeline on a talk you type or upload.

    python -m beamer2slides playground [--port 7860] [--host 127.0.0.1]

Everything up to the Google side runs here, for real: pdflatex (when a TeX distribution is on the
PATH), extract, classify and render, with what each stage decided drawn on the slide. The last step,
the Google Slides deck, needs Google credentials: where the server has them (`google_auth`) and
`B2S_PLAYGROUND_GOOGLE=1` is set, a button converts the job into a real deck; elsewhere (a public
host) the recorded runs on the front page show that part. See docs/playground.md.
"""
