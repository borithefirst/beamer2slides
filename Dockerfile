# The playground (docs/playground.md): TeX Live + beamer2slides, serving on port 7860.
#   docker build -t beamer2slides-playground .
#   docker run --rm -p 7860:7860 beamer2slides-playground
# Works as is as a Hugging Face Docker Space (port 7860, user 1000) and on any container host.
# Google Cloud Run sets $PORT instead, which the command below follows.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra \
        texlive-pictures texlive-fonts-recommended texlive-science texlive-plain-generic \
        texlive-luatex texlive-xetex lmodern cm-super \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 player
WORKDIR /app
COPY --chown=player pyproject.toml README.md ./
COPY --chown=player src ./src
RUN pip install --no-cache-dir ".[google]"
# What the page shows besides the talks it is given: the examples and the recorded runs.
COPY --chown=player examples/demo/demo.tex ./examples/demo/
COPY --chown=player tests/decks/11_research_talk.tex tests/decks/13_inline_math.tex \
     tests/decks/18_blocks_resize.tex tests/decks/19_labels_on_graphics.tex ./tests/decks/
COPY --chown=player docs/media ./docs/media

USER player
# `shell_escape=f` is the workbench's doing: its journeys compile LaTeX through the library
# (`inverse.Compiler`), which passes no `-no-shell-escape` of its own.
ENV B2S_PLAYGROUND_ROOT=/app B2S_PLAYGROUND_JOBS=/tmp/b2s-playground \
    openin_any=p openout_any=p shell_escape=f PYTHONUNBUFFERED=1
EXPOSE 7860
CMD exec python -m beamer2slides playground --host 0.0.0.0 --port "${PORT:-7860}"
