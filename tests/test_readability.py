"""The readability proxy for sources adopt writes (offline, no corpus)."""

from beamer2slides.devtools import readability

HAND = r"""\documentclass{beamer}
\begin{document}
\begin{frame}{Results}
  \begin{itemize}
    \item Latency fell by a third after the cache was added
    \item Throughput held steady under the heavier load
  \end{itemize}
\end{frame}
\end{document}
"""

MACHINE = r"""\documentclass{beamer}
\begin{document}
\begin{frame}[plain]
  \begin{textblock*}{340.76bp}(74.7bp,79.1bp)
    \vbox to 25.7bp{\slidesbox
    {\leftskip=8.50bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{14.65}\ttfamily\vrule width0bp height14.18bp depth0bp\relax%
      Latency fell by a third
    \baselineskip=26.37bp\par}}
  \end{textblock*}
\end{frame}
\end{document}
"""


def test_a_hand_written_frame_reads_as_one_and_a_typeset_one_does_not():
    hand, machine = readability.measure(HAND), readability.measure(MACHINE)
    assert readability.score(hand) > 0.9
    assert readability.score(machine) < 0.3
    assert machine["numbers"] > 10 * hand["numbers"]


def test_each_line_is_charged_to_what_wrote_it():
    assert readability.construct(r"    \noindent\slidesize{14.65}\vrule width0bp") == "text plumbing"
    assert readability.construct(r"  \begin{textblock*}{340.76bp}(74.7bp,79.1bp)") == "placement"
    assert readability.construct(r"      \path[fill=white] (0bp,0bp) -- cycle;") == "shape"
    assert readability.construct("      Latency fell by a third") == "text"


def test_a_style_name_handed_to_a_macro_is_not_words():
    assert readability.visible(r"\slidetext[center]{67.2,63,243.57,37.8}{body-serif-white}{own words}") == "own words"
    assert readability.visible(r"\slidepar[space=2.42]{title-lightyellow}{Item One}") == "Item One"


def test_what_the_decks_own_sty_defines_reads_as_vocabulary_and_not_as_plumbing():
    sty = (r"\newcommand\slidepicture[3][]{...}" "\n" r"\newenvironment{slidetable}{}{}" "\n"
           r"\newcommand{\slidestrut}[2]{...}" "\n" r"\def\slides@k@color{black}")
    v = readability.vocabulary(sty)
    assert v == {"slidepicture", "slidetable"}, "a strut is plumbing whoever named it, and @ names are internal"
    frame = "\\begin{document}\n\\begin{frame}\n  \\slidepicture{1,2,3,4}{a.png} words here\n\\end{frame}\n"
    assert readability.measure(frame, v)["author"] > readability.measure(frame)["author"]


def test_lines_every_frame_repeats_are_what_a_theme_should_say():
    frame = "\\begin{frame}\n  \\includegraphics[width=453.5bp]{figures/master-logo.png}\n  Words %d here\n\\end{frame}\n"
    tex = "\\begin{document}\n" + "".join(frame % k for k in range(4)) + "\\end{document}"
    m = readability.measure(tex)
    assert m["repeat"] < 0.7 and m["top_repeated"][0][0] == 4


def test_machinery_said_three_times_inside_one_frame_counts_as_said_three_times():
    """The judges' commonest complaint: a style dumped onto every row of one table is free while the
    same line on three frames is charged. A list of items is not machinery: its key is too short."""
    words = ("alpha", "beta", "gamma", "delta")
    rows = "".join("  \\slidepar[style=body-mono-blue,space=0.01,indent=22.68]{Row %s}\n" % w for w in words)
    table = "\\begin{document}\n\\begin{frame}\n" + rows + "\\end{frame}\n\\end{document}"
    items = "\\begin{document}\n\\begin{frame}\n" + "".join(f"  \\item Item {w}\n" for w in words) + \
            "\\end{frame}\n\\end{document}"
    assert readability.measure(table)["repeat"] < 0.3, "one style, said four times"
    assert readability.measure(items)["repeat"] == 1.0, "four items a person wrote"


def test_a_frame_emptied_into_the_theme_is_not_free():
    """Every measure is a ratio per word, so a frame holding only its title scored near 1.0 while the
    box a person wants to move now lives in the layout (the judges refused that source)."""
    tex = "\\begin{document}\n" + "".join(
        "\\begin{frame}[plain,layout=section]\n  \\frametitle{Section %d}\n\\end{frame}\n" % k for k in range(3)) + \
        "\\end{document}"
    theme = "\\defbeamertemplate{background}{section}{%\n" + "".join(
        "  \\slidetext{%d.5,63,243.57,37.8}{body}{Decoration %d}\n" % (k, k) for k in range(20)) + "}\n"
    assert readability.score(readability.measure(tex)) > readability.score(readability.measure(tex, shared=theme))


def test_a_literal_a_frame_says_twice_is_reported():
    twice = ("\\begin{document}\n\\begin{frame}\n  \\frametitle{Quarterly revenue}\n"
             "  \\slidetext{1,2,3,4}{body}{Quarterly revenue}\n\\end{frame}\n\\end{document}")
    once = twice.replace("{body}{Quarterly revenue}", "{body}{Something else entirely}")
    assert readability.measure(twice)["twins"] == 1 and readability.measure(once)["twins"] == 0
