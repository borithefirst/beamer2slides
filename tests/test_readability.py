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


def test_lines_every_frame_repeats_are_what_a_theme_should_say():
    frame = "\\begin{frame}\n  \\includegraphics[width=453.5bp]{figures/master-logo.png}\n  Words %d here\n\\end{frame}\n"
    tex = "\\begin{document}\n" + "".join(frame % k for k in range(4)) + "\\end{document}"
    m = readability.measure(tex)
    assert m["repeat"] < 0.7 and m["top_repeated"][0][0] == 4
