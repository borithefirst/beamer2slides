You are handed the LaTeX source of a slide deck and asked to make three edits to one slide:

1. change the wording of one line of its text;
2. move one box 20 pt to the right;
3. restyle a phrase in it (bold, or another colour).

Below are pairs of sources. In each pair you see two sources, "A" and "B". For each pair, say which
of the two you would rather be handed to do that job, and - this is the part that matters - what
stands in the way in each of them. Be concrete: name the line or the construct that would slow you
down, or the one that would make it quick.

There is no right answer and no trick: some pairs show the same slide written in two ways, some show
two different slides from two different talks. Judge the source you would have to edit, not how
pretty the slide would look. If they are genuinely equal, say so.

Each source is shown as the body of one `frame`. The macros a source uses are defined once, at the
top, under "Vocabulary": read that first, as you would read a package's documentation once.

Answer with JSON and nothing else: a list with one object per pair, in the order the pairs are given:

[{"pair": "<the pair's id>",
  "prefer": "A" | "B" | "tie",
  "confidence": 1 | 2 | 3,
  "reason": "<one or two sentences: why that one>",
  "in_the_way_A": "<what would slow you down in A>",
  "in_the_way_B": "<what would slow you down in B>"}]


## Vocabulary

Each source below names the vocabulary it is written in. Read that section once. Only each
macro's first line and the comment written above it are shown - the bodies are left out, as
you would not read a package's implementation to use it.

### Vocabulary V1

```latex
\renewcommand{\familydefault}{\sfdefault}
% A layout's decoration sits in beamer's background, under the frame's own textblocks: textpos
% places them there in its relative mode, from the page's top left corner.
\newcommand{\layoutdecoration}[1]{\vbox to 0pt{\TP@absposfalse#1\vss}}
\newcommand{\layouttitle}{\beamer@frametitle}
\newcommand{\layoutsubtitle}{\expandafter\deck@unbrace\insertframesubtitle}
\newcommand{\withtitle}[1]{\ifx\beamer@frametitle\@empty\else#1\fi}
\newcommand{\withsubtitle}[1]{\ifx\insertframesubtitle\@empty\else#1\fi}
\newcommand{\withnumber}[1]{\ifdeck@nonumber\else#1\fi}
\newcommand{\layoutcanvas}[2]{\@namedef{deck@canvas@#1}{#2}}
\newcommand{\defmaster}[2]{\expandafter\long\expandafter\def\csname deck@master@#1\endcsname{#2}}
\newcommand{\drawmaster}[1]{\@nameuse{deck@master@#1}}
% (\setbeamercolor reads keys of its own, which would end the frame's option list: `\deck@keys`)
\define@key{beamerframe}{layout}{\deck@keys{\setbeamertemplate{background}[#1]\@nameuse{deck@canvas@#1}}}
\define@key{beamerframe}{background}{\deck@keys{\setbeamertemplate{background canvas}[default]%
\define@key{beamerframe}{backdrop}{\deck@keys{\setbeamertemplate{background canvas}{%
\define@key{beamerframe}{nonumber}[true]{\deck@nonumbertrue}
% frame options last until the next frame, which starts from the deck's page again
\AddToHook{env/frame/before}{\setbeamertemplate{background}{}\deck@nonumberfalse
%% The vocabulary main.tex's frames are written in: the deck's text model (slidebox, \slidepar,
%% \slidestyle), its tab stops, and the helpers of its shapes and tables. Nothing here is specific to
%% this deck; the deck's styles and colours are named in main.tex's preamble.
% --- Pictures ---------------------------------------------------------------------------------
% \slidepicture[options]{x,y,w,h}{file}: a picture w by h bp whose top left corner is x bp from the
%   page's left edge and y bp from its top. Options, the picture's edits in the deck:
%   trim=l b r t   bp of the file cut off its left, bottom, right and top (graphicx's trim, clipped);
%   angle=a        turned a degrees counter-clockwise; x,y is then the corner of the turned picture's bounds;
%   flip           mirrored left to right;
%   opacity=o      see-through, 0..1;
%   outline=colour, outline width=bp, dash=dotted|dashed: a line drawn on the picture's edge; x,y is then
%                  the line's outer corner, half its width up and left of the picture's.
\newcommand\slidepicture[3][]{\slides@xywh#2\@nil
  % turned by graphicx itself unless a node or a mirror wraps the picture: then by \rotatebox
% --- Shapes -----------------------------------------------------------------------------------
% \slideshape[turn]{x,y,w,h}{paths}: TikZ paths in a box x bp from the page's left edge and y bp from
%   its top, w by h bp; the origin is the box's top left corner, y pointing up. Arrow heads and strokes
%   may reach out of the box: they overlap, the box stays. `turn` is TikZ's rotate= (degrees,
%   counter-clockwise) and/or flip (mirrored left to right), both about the box's centre.
\newcommand\slideshape[3][]{\slides@xywh#2\@nil\slides@centre{\slides@w bp}{\slides@h bp}%
% the centre of a w by h box, from its top left corner: (\slides@cx,\slides@cy), and minus that
% path options with a rotate= or flip in them act about the shape's centre
% \sliderect[options]{x,y,w,h}: a rectangle filling that box, drawn with TikZ's path options (fill=,
%   draw=, line width=...), rounded=r for corners rounded with radius r bp, and rotate=/flip.
%   A rounded one starts halfway up the left edge, where the top left corner's arc begins, so a dash
%   pattern runs from there: TikZ's own cycle would start one arc later and shift every dash.
\newcommand\sliderect[2][]{\slides@xywh#2\@nil\slides@centre{\slides@w bp}{\slides@h bp}\slides@about{#1}%
% \slides@r := the rounded= value in a list of path options, empty when there is none
% \slideellipse[options]{x,y,rx,ry}: an ellipse of radii rx and ry bp whose box's top left corner is
%   x bp from the page's left edge and y bp from its top.
\newcommand\slideellipse[2][]{\slides@xywh#2\@nil
% \slideline[options]{x1,y1}{x2,y2}: a straight line between two points of the page (bp from its left
%   and top edges), with TikZ's path options; arrow heads as the deck draws them: -> or <-> (a filled
%   triangle), or -SlideStealth, -SlideOpen, -SlideCircle, -SlideOpenCircle, -SlideSquare,
%   -SlideOpenSquare, -SlideDiamond, -SlideOpenDiamond.
\newcommand\slideline[3][]{\slides@xy#2\@nil\let\slides@ax\slides@x\let\slides@ay\slides@y\slides@xy#3\@nil
% --- Text boxes laid out as the deck lays them out ------------------------------------
% \slidesize{z}: z bp type on z bp lines, with the font's own interword space and no shrink.
\newif\ifslidesspace
\AddToHook{selectfont}{\ifslidesspace\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax\fi}
\newcommand{\slidesize}[1]{\fontsize{#1bp}{#1bp}\selectfont\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax}
\newcommand{\slidesbox}{\slidesspacetrue\parindent=0pt\parskip=0pt\lineskip=0pt\lineskiplimit=-\maxdimen\hyphenpenalty=10000\exhyphenpenalty=50\tolerance=9999\emergencystretch=0pt\frenchspacing\hbadness=10000\hfuzz=\maxdimen\vbadness=10000\vfuzz=\maxdimen}
%
% \slidestyle{name}{size=, family=mono|serif, face=\fontswitch, weight=bold|w<NNN>, italic, color=,
%   ascent=, pitch=, depth=}: a text style. size in bp; ascent, pitch and depth are the the deck line
%   box of a paragraph in it (its first line's height above the baseline, the distance between
%   baselines, and what the box ends under its last line). A bullet's style needs no line box.
\newcommand\slidestyle[2]{%
%
% \begin{slidebox}[options]{x,y,w,h} ... \end{slidebox}: a text box whose text starts x bp from the
%   page's left edge, y bp from its top, lines broken at w bp, in a box h bp tall. Options: middle,
%   bottom (where the text stands in the box; top by default), inset= (bp above a top-aligned box's
%   first line or under a bottom-aligned one's last: \setslideinset gives the default), tail= (bp
%   stacked under a middle- or bottom-aligned box's last line: its last paragraph's space below), and
%   any of \slidepar's style=, left, center, right, justify, indent=, rindent=, first=, lang=, space=, which
%   its paragraphs and list items then take unless they say otherwise.
\def\slidesinset{0}
\newcommand\setslideinset[1]{\def\slidesinset{#1}}
% \setslidepar{options}: what every paragraph of the deck takes unless its box or itself says otherwise
\newcommand\setslidepar[1]{\def\slides@deckpar{#1}}
\newenvironment{slidebox}[2][]{%
%
% \slidepar[options]{words}: a paragraph of a slidebox. Options: style= (a \slidestyle), left, center,
%   right, justify; indent=, rindent= (bp from the box's left and right edges), first= (bp the first
%   line starts past indent), space= (bp between this paragraph and the one before beyond their line
%   boxes, which the two styles' line boxes give: the deck's space above or below), lang= (the babel
%   language of a right-to-left paragraph), mixed (words of several sizes, each carrying its line
%   box: \slidestrut), prevdepth= (bp: where the paragraph after a mixed one takes its line from).
% a list item's bullet: mark= (a \slidemark), or label= (typed, in labelstyle=; in an enumerate
%   \arabic*, \alph*, \Alph*, \roman*, \Roman* stand for the item's number), gap= (bp between its
%   right edge and the text); start= (an enumerate's first number)
\newcommand\slidepar[1][]{%
% (the words are a group, not an argument: they are read with the catcodes they are set in)
% what the next paragraph and the box's end take from this one: its line box under the baseline
%
% itemize and enumerate in a slidebox: \item[options] words, where each item is a \slidepar with a
%   bullet. What an item of a list level looks like is said once for the deck,
%   \setslidelist{itemize|enumerate}{level}{options}, a list may say what its own items differ in
%   (\begin{itemize}[options]), and an item what it alone differs in.
\newcommand\setslidelist[3]{\@namedef{slides@L@#1@#2}{#3}}
% (a list nested in an item starts from the box's own font and colour, not its item's)
% an item's paragraph ends at the next \item, at \end of its list, or where a list nested in it begins
%
% \slidetext[options]{x,y,w,h}{style}{words}: a slidebox holding one \slidepar, the options of both
%   in one list.
\newcommand\slidetext[3][]{\let\slides@bo\@empty\def\slides@po{style=#3}\setkeys{slidetext}{#1}%
%
% \slidelabel{style}{text}{gap}: a typed bullet (or number) in a style, ending gap bp before the
%   text (negative: into it). \slidebullet{name}{gap}: a drawn one, named by \slidemark.
%   \slidestrut{height}{depth}: a word's own line box in a paragraph of several sizes.
\newcommand\slidelabel[3]{\expandafter\let\expandafter\slides@llead\csname slides@s@#1\endcsname
\newcommand\slidemark[2]{\expandafter\def\csname slides@m@#1\endcsname{#2}}
\newcommand\slidebullet[2]{\llap{\csname slides@m@#1\endcsname\hskip#2bp}}
\newcommand\slidestrut[2]{\vrule width0bp height#1bp depth#2bp\relax}
% \slidebreak: a soft line break (Shift+Enter); \slidefillbreak: the same in a justified paragraph,
%   whose broken line stays unjustified.
\newcommand\slidebreak{\unskip\break}
\newcommand\slidefillbreak{\unskip\hfil\break}
% the deck's arrow heads, sized in line widths: > (FILL_ARROW), and SlideStealth, SlideOpen, ...
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
%% The vocabulary main.tex's frames are written in: the deck's text model (slidebox, \slidepar,
%% \slidestyle), its tab stops, and the helpers of its shapes and tables. Nothing here is specific to
%% this deck; the deck's styles and colours are named in main.tex's preamble.
% --- Freeforms ---------------------------------------------------------------------------------
% \slidefreeform[options]{x,y,w,h}{file}: a freeform shape in the box x,y,w,h (bp, as \slideshape),
%   its outline read from `file`: TikZ path rings relative to the box's top left corner, y pointing up,
%   filled even-odd. Options: fill=colour, opacity=o (of the fill), outline=colour and outline width=w
%   (bp; the outline is drawn inside the edge, where the deck's picture shows it).
\newcommand\slidefreeform[3][]{\def\slides@f@fill{black}\let\slides@f@op\@empty
% --- Pictures ---------------------------------------------------------------------------------
% \slidepicture[options]{x,y,w,h}{file}: a picture w by h bp whose top left corner is x bp from the
%   page's left edge and y bp from its top. Options, the picture's edits in the deck:
%   trim=l b r t   bp of the file cut off its left, bottom, right and top (graphicx's trim, clipped);
%   angle=a        turned a degrees counter-clockwise; x,y is then the corner of the turned picture's bounds;
%   flip           mirrored left to right;
%   opacity=o      see-through, 0..1;
%   outline=colour, outline width=bp, dash=dotted|dashed: a line drawn on the picture's edge; x,y is then
%                  the line's outer corner, half its width up and left of the picture's.
\newcommand\slidepicture[3][]{\slides@xywh#2\@nil
% the deck's arrow heads, sized in line widths: > (FILL_ARROW), and SlideStealth, SlideOpen, ...
% the fourth argument is boxed at its own width and set turned about the centre, where it would be if
% the element were upright - the way the deck turns a text box with everything in it.
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
% --- Shapes -----------------------------------------------------------------------------------
% \slideshape[turn]{x,y,w,h}{paths}: TikZ paths in a box x bp from the page's left edge and y bp from
%   its top, w by h bp; the origin is the box's top left corner, y pointing up. Arrow heads and strokes
%   may reach out of the box: they overlap, the box stays. `turn` is TikZ's rotate= (degrees,
%   counter-clockwise) and/or flip (mirrored left to right), both about the box's centre.
\newcommand\slideshape[3][]{\slides@xywh#2\@nil\slides@centre{\slides@w bp}{\slides@h bp}%
% --- Tables ---------------------------------------------------------------------------------
% \begin{slidetable}[options]{x,y}{w1,...,wn} rows \end{slidetable}: a table whose top left corner is
%   x bp from the page's left edge and y bp from its top, its columns w1...wn bp wide. A row is written
%   as in a tabular, `a & b & c \\`; a cell is its text, or \cell[options]{text} (text holding \\ or
%   several paragraphs), or \multicell{k}[options]{text} over k columns (rows=r: and r rows; the rows
%   under it then leave its columns out). \row[options] opens a row. After the last row,
%   \hborder{i}[a-b]{style} restyles the line above row i (\vborder{j}...: left of column j), from
%   cell a to cell b of it or all of it; `none` takes it away.
%   A row is as tall as it says unless its cells need more (the deck's rule), or `fixed`.
%   Options, of the table, a \row or a cell (the nearest one says):
%     inset=d, inset x=d, inset y=d  the text's distance from the cell's edges, bp (table only)
%     border=style                   TikZ options of every border line (table only); none
%     h=d, fixed | grow              the row's height, bp; fixed: kept whatever its text needs
%     fill=colour, fill opacity=o    the cell's fill; none
%     valign=top|middle|bottom, align=left|center|right
%     aligns={a1,...,an}             each column's align (table only; a cell's own still wins)
%     style=switches, pitch=d        the text's font and colour; its lines d bp apart
%     baseline=d                     the first baseline of a top-aligned cell d bp under its top (the
%                                    last of a bottom-aligned one d bp over its bottom); empty: the
%                                    inset places the text
%     lang=language                  the text is right to left, in that babel language
%     word | wrap                    one word too wide for the insets is set without them (word, the
%                                    default), or sticks out like any line (wrap)
% one paragraph set in one line that does not fit its room (TeX is asked to box the line to it: a
% line that fits by shrinking its spaces fits): a word to set again without the insets
% cell #1, from row #2 to row #3, its text #4 wide with the vertical inset #5; #6 wide with no insets,
% shifted by #7: its box, the rows it ends grown to hold it
  % a measured row is what the deck laid out, to the pixel: text that fills it exactly is no sign of
  % text set without insets
  % colour's whatsit on either end keeps \lastbox from counting lines): pieces split off the top until
  % one holds a line
% markers, told apart by meaning
    % the insets to the hundredth where they make lengths, as the tikz grid this replaces wrote them
        % where lines and fills go: to a tenth of a point, as the tikz grid this replaces had them
    % rows holding one cell grow first, so a merged cell only adds what they left it short of
      % the column's alignment, then what the cell says
% the border segments a merged cell covers
% where cell #1 ends: \l__slides_t_r_one_int = its last row + 1, \l__slides_t_c_one_int = last column + 1
% segment #3 of line #2 (h: above row #2, v: left of column #2): its style in \l__slides_t_seg_tl
% a line's styles in the order they first appear along it, each drawn in runs of touching segments
% --- Text boxes laid out as the deck lays them out ------------------------------------
% \slidesize{z}: z bp type on z bp lines, with the font's own interword space and no shrink.
\newif\ifslidesspace
%% The vocabulary main.tex's frames are written in: the deck's text model (slidebox, \slidepar,
%% \slidestyle), its tab stops, and the helpers of its shapes and tables. Nothing here is specific to
%% this deck; the deck's styles and colours are named in main.tex's preamble.
% --- Tables ---------------------------------------------------------------------------------
% \begin{slidetable}[options]{x,y}{w1,...,wn} rows \end{slidetable}: a table whose top left corner is
%   x bp from the page's left edge and y bp from its top, its columns w1...wn bp wide. A row is written
%   as in a tabular, `a & b & c \\`; a cell is its text, or \cell[options]{text} (text holding \\ or
%   several paragraphs), or \multicell{k}[options]{text} over k columns (rows=r: and r rows; the rows
%   under it then leave its columns out). \row[options] opens a row. After the last row,
%   \hborder{i}[a-b]{style} restyles the line above row i (\vborder{j}...: left of column j), from
%   cell a to cell b of it or all of it; `none` takes it away.
%   A row is as tall as it says unless its cells need more (the deck's rule), or `fixed`.
%   Options, of the table, a \row or a cell (the nearest one says):
%     inset=d, inset x=d, inset y=d  the text's distance from the cell's edges, bp (table only)
%     border=style                   TikZ options of every border line (table only); none
%     h=d, fixed | grow              the row's height, bp; fixed: kept whatever its text needs
%     fill=colour, fill opacity=o    the cell's fill; none
%     valign=top|middle|bottom, align=left|center|right
%     aligns={a1,...,an}             each column's align (table only; a cell's own still wins)
%     style=switches, pitch=d        the text's font and colour; its lines d bp apart
%     baseline=d                     the first baseline of a top-aligned cell d bp under its top (the
%                                    last of a bottom-aligned one d bp over its bottom); empty: the
%                                    inset places the text
%     lang=language                  the text is right to left, in that babel language
%     word | wrap                    one word too wide for the insets is set without them (word, the
%                                    default), or sticks out like any line (wrap)
% one paragraph set in one line that does not fit its room (TeX is asked to box the line to it: a
% line that fits by shrinking its spaces fits): a word to set again without the insets
% cell #1, from row #2 to row #3, its text #4 wide with the vertical inset #5; #6 wide with no insets,
% shifted by #7: its box, the rows it ends grown to hold it
  % a measured row is what the deck laid out, to the pixel: text that fills it exactly is no sign of
  % text set without insets
  % colour's whatsit on either end keeps \lastbox from counting lines): pieces split off the top until
  % one holds a line
% markers, told apart by meaning
    % the insets to the hundredth where they make lengths, as the tikz grid this replaces wrote them
        % where lines and fills go: to a tenth of a point, as the tikz grid this replaces had them
    % rows holding one cell grow first, so a merged cell only adds what they left it short of
      % the column's alignment, then what the cell says
% the border segments a merged cell covers
% where cell #1 ends: \l__slides_t_r_one_int = its last row + 1, \l__slides_t_c_one_int = last column + 1
% segment #3 of line #2 (h: above row #2, v: left of column #2): its style in \l__slides_t_seg_tl
% a line's styles in the order they first appear along it, each drawn in runs of touching segments
% --- Text boxes laid out as the deck lays them out ------------------------------------
% \slidesize{z}: z bp type on z bp lines, with the font's own interword space and no shrink.
\newif\ifslidesspace
```

### Vocabulary V2

```latex
Plain beamer and LaTeX: the `frame`, `itemize`, `tabular`, `tikzpicture` and
`block` environments, `\includegraphics`, `\textbf`, `\alert`, and so on.
No macros of its own.
```


## The pairs


### Pair h06

The two sources are different slides from different talks.


**h06 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{NearBlack}{HTML}{212121}
\setslideinset{3.27}
\slidestyle{body-mono}{size=8.06, family=mono, color=NearBlack, ascent=7.8, pitch=11.12, depth=3.32}
\slidestyle{label-mono}{size=8.06, family=mono, color=NearBlack}
\setslidepar{style=body-mono}
\setslidelist{itemize}{1}{style=body-mono,indent=18.14,labelstyle=label-mono,label={•},gap=10.03}
\setslidelist{itemize}{2}{style=body-mono,indent=36.28,labelstyle=label-mono,label={•},gap=10.03}
\setslidelist{enumerate}{1}{style=body-mono,indent=18.14,labelstyle=label-mono,label={\arabic*.},gap=10.03}
```


The frame:

```latex
\begin{frame}[plain,layout=section]
  \frametitle{Expressions}
\end{frame}
```


**h06 source B** (vocabulary V2)


The frame:

```latex
\begin{frame}{A photo with a caption}
  \centering
  \includegraphics[width=0.7\textwidth]{photo.jpg}

  \small Figure 1: a generated photo-like JPEG.
\end{frame}
```


### Pair h07

The two sources are different slides from different talks.


**h07 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}{Buttons and small print}
  \hyperlink{last}{\beamergotobutton{Jump to the end}}

  \vspace{1em}
  {\tiny This tiny print is a disclaimer that nobody reads, but it is still text.}

  \begin{verse}
    Roses are red,\\
    violets are blue.
  \end{verse}
\end{frame}
```


**h07 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{large-mspgothic}{size=17.64, face=\adoptfontB, color=black, ascent=17.08, pitch=24.34, depth=7.27}
\slidestyle{body}{size=15.12, color=black, ascent=14.64, pitch=20.87, depth=6.23}
\slidestyle{label}{size=22.68, color=black}
\slidestyle{heading-microsoftjhenghe-22.7}{size=22.68, face=\adoptfontA, color=black, ascent=21.95, pitch=40.82, depth=5.26}
\setslidepar{style=body}
\setslidelist{itemize}{1}{style=heading-microsoftjhenghe-22.7,indent=22.68,labelstyle=label,label={★},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=標題與內文-2]
  \frametitle{第一題}
  \begin{slidebox}[first=22.68]{19.7,72.8,414.24,172.3}
    \slidepar[style=large-mspgothic,first=0]{- Get Ready to Use Technology in the Classroom.}
    \slidepar[first=0,space=0.01]{{\adoptfontB -\ }When trying to select the right tool to integrate into your class, you should always start with:}
    \slidepar{A The learning goal}
    \slidepar{B The functionality of the tool}
    \slidepar{C A help site}
    \slidepar{D An idea from a trusted colleague}
  \end{slidebox}
\end{frame}
```


### Pair h08

The two sources are different slides from different talks.


**h08 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{deckbg}{HTML}{EEEEEE}
\definecolor{Green}{HTML}{34A853}
\setslideinset{4.08}
\slidestyle{tiny-googlesans-medium-green}{size=6.61, face=\adoptfontA, weight=w500, color=Green, ascent=6.4, pitch=7.93, depth=1.53}
\slidestyle{title-googlesans-24.6}{size=24.57, face=\adoptfontA, color=black, ascent=23.78, pitch=29.29, depth=5.7}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=blank-1,background=deckbg]
  \slidepicture[trim=482.22 50.93 481.14 50.93]{202.1,18.8,240.2,232.6}{figures/picture-170dada5.jpg}
  \slidepicture{0,0,453.5,255.1}{figures/picture-6bb9dee1.png}
  \slidepicture[trim=0 2.08 0 2.08]{118.5,182.6,83.7,52.1}{figures/picture-38208a9f.png}
  \slidepicture{31.1,21.6,136.7,17.7}{figures/picture-5bdc791d.png}
  \slidetext{30.2,78.9,167.84,68.7}{title-googlesans-24.6}{Chapter \textbf{Title\ }Goes Here.}
  \slidetext[inset=0]{65.5,31.6,119.48,8}{tiny-googlesans-medium-green}{Editable Location}
\end{frame}
```


**h08 source B** (vocabulary V2)


The frame:

```latex
\begin{frame}{CMYK and greyscale}
  \centering
  \includegraphics[width=0.38\textwidth]{photo_cmyk.jpg}
  \hspace{0.06\textwidth}
  \includegraphics[width=0.38\textwidth]{plot_gray.png}
\end{frame}
```


### Pair h09

The two sources are different slides from different talks.


**h09 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}[label=end]{Summary}
  \begin{itemize}
    \item Label every frame
    \item Sync after every change of the source
  \end{itemize}
\end{frame}
```


**h09 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{body}{size=15.12, color=black, ascent=14.64, pitch=20.87, depth=6.23}
\slidestyle{label}{size=22.68, color=black}
\slidestyle{heading-microsoftjhenghe-22.7}{size=22.68, face=\adoptfontA, color=black, ascent=21.95, pitch=40.82, depth=5.26}
\slidestyle{small-microsoftjhenghe}{size=11.34, face=\adoptfontA, color=black, ascent=10.98, pitch=13.7, depth=2.63}
\setslidepar{style=body}
\setslidelist{itemize}{1}{style=heading-microsoftjhenghe-22.7,indent=22.68,labelstyle=label,label={★},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=區段標題]
  \frametitle{Google Groups}
  \begin{slidebox}{13.4,79.6,424.8,159.9}
    \begin{itemize}
      \item \uline{建立群組}
      \item \uline{邀請人加入群組}
      \item 群組討論
    \end{itemize}
  \end{slidebox}
  \slidepicture[trim=10.66 0 0 0]{7.4,4.7,50,63.8}{figures/picture-de54c7a2.png}
  \slidetext[middle]{129.8,102.5,435.89,19.9}{small-microsoftjhenghe}{\href{https://drive.google.com/file/d/0B6VPO_ULtz0RdWI0MWJOSU5raDg/view}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RdWI0MWJOSU5raDg/view}}}
  \slidetext[middle]{137.3,148.6,428.37,18.1}{small-microsoftjhenghe}{\href{https://drive.google.com/file/d/0B6VPO_ULtz0RUWdsNVdzNzN5elk/view}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RUWdsNVdzNzN5elk/view}}}
\end{frame}
```


### Pair h10

The two sources are different slides from different talks.


**h10 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{DarkBlue}{HTML}{20124D}
\setslideinset{4.08}
\slidestyle{small}{size=11.34, color=black, ascent=10.98, pitch=13.7, depth=2.63}
\slidestyle{body-calibri}{size=15.12, face=\adoptfontA, color=black, ascent=11.91, pitch=14.52, depth=2.6}
\slidestyle{label-bold-darkblue}{size=15.12, weight=bold, color=DarkBlue}
\slidemark{dot}{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];}
\setslidepar{style=small}
\setslidelist{itemize}{1}{style=body-calibri,indent=22.68,labelstyle=label-bold-darkblue,mark=dot,gap=12.55}
```


The frame:

```latex
\begin{frame}[plain,layout=title-and-body]
  \slidepicture[trim=8.07 17.41 0 0]{26.7,3.8,418.8,247.6}{figures/picture-0c48b18b.png}
\end{frame}
```


**h10 source B** (vocabulary V2)


The frame:

```latex
\begin{frame}{Turned and mirrored}
  \centering
  \includegraphics[angle=30, width=0.28\textwidth]{plot.png}
  \hspace{0.12\textwidth}
  \reflectbox{\includegraphics[width=0.3\textwidth]{plot.png}}
\end{frame}
```


### Pair h11

The two sources are different slides from different talks.


**h11 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}{Footnote marks}
  A circled mark\textsuperscript{\circled{1}} after a word, and another one\textsuperscript{\circled{2}} here.

  A footnote in a coloured box\textsuperscript{\colorbox{orange}{\color{white}1}} in the middle of a line.

  A real footnote\footnote{The footnote text.} and a boxed mark{\fboxsep=1pt\fbox{\tiny 2}} after words.
\end{frame}
```


**h11 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{deckbg}{HTML}{FFF8EC}
\definecolor{NearBlack2}{HTML}{242424}
\definecolor{Yellow2}{HTML}{F3AB15}
\slidestyle{title-pacifico-yellow}{size=28.35, face=\adoptfontB, color=Yellow2, ascent=27.44, pitch=40.82, depth=13.38}
\slidestyle{small}{size=6.93, color=black, ascent=6.71, pitch=11.64, depth=1.61}
\slidestyle{small-arial-nearblack}{size=6.93, face=\adoptfontA, color=NearBlack2, ascent=6.71, pitch=11.64, depth=1.61}
\slidestyle{body}{size=7.56, color=black, ascent=7.32, pitch=12.7, depth=1.75}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=blank,background=deckbg]
  \slidepicture[trim=0 0 0 106.11]{250.8,0,132.9,72.7}{figures/picture-8c058ab9.png}
  \slidetext{25.5,25.3,232.82,68.3}{title-pacifico-yellow}{How to Use This Presentation}
  \slidetext[center]{37,160.8,98.74,55.4}{small}{Click on the \textbf{\symbol{34}Canva\symbol{34}} button under this presentation preview. Start editing your presentation. You need to sign in to your Canva account.}
  \slidetext[center]{175.4,160.8,102.1,78.9}{small}{Export this design from Canva as a \textbf{PowerPoint template}. Open the design in \textbf{Canva}. This will provide you with all the fonts used and elements used in this presentation as listed on page 21. Learn more on slide 3.}
  \slidetext[center]{317.3,160.8,101.04,67.2}{small}{Export this design from Canva as a \textbf{GoogleSlide template}. This will provide you with all the fonts used and elements used in this presentation as listed on page 21. Learn more on slide 4.}
  \slidetext[center]{11.9,140.7,148.83,17.6}{small-arial-nearblack}{CANVA}
  \slidetext[center]{152.4,140.7,148.83,17.6}{small-arial-nearblack}{POWERPOINT}
  \slidetext[center]{291.7,140.7,148.83,17.6}{small-arial-nearblack}{GOOGLE SLIDES}
\end{frame}
```


### Pair h12

The two sources are different slides from different talks.


**h12 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}{Flowchart}
  \centering
  \begin{tikzpicture}[node distance=1.2cm and 2cm, every node/.style={font=\small}]
    \node[draw, rounded corners, fill=blue!10] (start) {Start};
    \node[draw, diamond, aspect=2, fill=yellow!20, below=of start] (dec) {Valid?};
    \node[draw, fill=green!15, right=of dec] (ok) {Convert};
    \node[draw, fill=red!15, below=of dec] (no) {Reject};
    \draw[->] (start) -- (dec);
    \draw[->] (dec) -- node[above] {yes} (ok);
    \draw[->] (dec) -- node[right] {no} (no);
    \draw[->] (ok) |- (start);
  \end{tikzpicture}
\end{frame}
```


**h12 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{deckbg}{HTML}{EEEEEE}
\definecolor{Blue}{HTML}{4285F4}
\definecolor{PaleCyan}{HTML}{C3ECF6}
\setslideinset{4.08}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\slidestyle{heading-googlesans-semibold-14.5}{size=14.49, face=\adoptfontA, weight=w600, color=black, ascent=14.03, pitch=17.48, depth=3.36}
\slidestyle{tiny-mono-medium}{size=5.04, family=mono, weight=w500, color=black, ascent=4.88, pitch=6.05, depth=1.17}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=blank-1,background=deckbg]
  \slideline[draw=black,line width=0.94bp]{226.8,172.21}{226.8,148.6}
  \sliderect[fill=PaleCyan,draw=black,line width=0.94bp,rounded=8.8]{42.7,71,52.81,56.55}
  \slidetext[inset=0,center]{51,110.8,36.19,9}{tiny-mono-medium}{Short label}
  \sliderect[fill=PaleCyan,draw=black,line width=0.94bp,rounded=8.8]{105.8,71,52.81,56.55}
  \slidetext[inset=0,center]{114.1,110.8,36.18,9}{tiny-mono-medium}{Short label}
  \sliderect[fill=PaleCyan,draw=black,line width=0.94bp,rounded=8.8]{168.8,71,52.81,56.55}
  \slidetext[inset=0,center]{177.2,110.8,36.18,9}{tiny-mono-medium}{Short label}
  \sliderect[fill=PaleCyan,draw=black,line width=0.94bp,rounded=8.8]{295,71,52.81,56.55}
  \slidetext[inset=0,center]{303.3,110.8,36.18,9}{tiny-mono-medium}{Short label}
  \sliderect[fill=PaleCyan,draw=black,line width=0.94bp,rounded=8.8]{358.1,71,52.81,56.55}
  \slidetext[inset=0,center]{366.4,110.8,36.19,9}{tiny-mono-medium}{Short label}
  \sliderect[fill=PaleCyan,draw=black,line width=0.94bp,rounded=8.8]{231.9,71,52.8,56.55}
  \slidetext[inset=0,center]{240.2,110.8,36.19,9}{tiny-mono-medium}{Short label}
  \slideellipse[fill=Blue,draw=black,line width=0.94bp]{58,82.3,11.13,11.13}
  \slideellipse[fill=Blue,draw=black,line width=0.94bp]{121.1,82.3,11.13,11.13}
  \slideellipse[fill=Blue,draw=black,line width=0.94bp]{184.2,82.3,11.13,11.13}
  \slideellipse[fill=Blue,draw=black,line width=0.94bp]{247.3,82.3,11.13,11.13}
  \slideellipse[fill=Blue,draw=black,line width=0.94bp]{310.4,82.3,11.13,11.13}
  \slideellipse[fill=Blue,draw=black,line width=0.94bp]{373.5,82.3,11.13,11.13}
  \sliderect[fill=PaleCyan,draw=black,line width=0.94bp,rounded=9.42]{198.4,170.2,56.68,56.54}
  \slidetext[inset=0,center]{208.7,209.9,36.19,9}{tiny-mono-medium}{Short label}
  \slideellipse[fill=Blue,draw=black,line width=0.94bp]{215.7,181.4,11.13,11.13}
  \slidefreeform[fill=white]{63.8,87.9,10.55,11.16}{shapes/freeform-6a70e6a8.tex}
  \slidefreeform[fill=white]{126.9,87.9,10.55,11.16}{shapes/freeform-4076e224.tex}
  \slidefreeform[fill=white]{190,87.9,10.55,11.16}{shapes/freeform-063929a5.tex}
  \slidefreeform[fill=white,outline=black,outline width=0.47]{253.1,87.9,10.55,11.16}{shapes/freeform-a03dc58f.tex}
  \slidefreeform[fill=white]{316.2,87.9,10.55,11.16}{shapes/freeform-788b3c45.tex}
  \slidefreeform[fill=white]{379.3,87.9,10.55,11.16}{shapes/freeform-ea89a320.tex}
  \slidefreeform[fill=white]{221.3,187.3,10.55,11.16}{shapes/freeform-073a870c.tex}
  \slideline[draw=black,line width=0.94bp]{409.05,148.6}{44.6,148.6}
  \slideline[draw=black,line width=0.94bp]{44.6,148.57}{44.6,138.2}
  \slideline[draw=black,line width=0.94bp]{409.1,148.57}{409.1,138.2}
  \slidetext[bottom,inset=0]{42.2,28.5,188.74,17.9}{heading-googlesans-semibold-14.5}{Group chart}
\end{frame}
```


### Pair h13

The two sources are different slides from different talks.


**h13 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}{Helvetica via helvet}
  \begin{itemize}
    \item Running text in a Helvetica clone
    \item \textbf{Bold} and \textit{italic} variants
    \item A longer item that wraps onto a second line so we can check the line breaks in Arial
  \end{itemize}
\end{frame}
```


**h13 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Blue}{HTML}{185DA2}
\setslideinset{3.27}
\slidestyle{body-blue-15.1}{size=15.12, color=Blue, ascent=14.64, pitch=18.14, depth=3.51}
\slidestyle{body}{size=15.12, color=black, ascent=14.64, pitch=18.14, depth=3.51}
\slidestyle{tiny}{size=7.06, color=black, ascent=6.83, pitch=8.47, depth=1.64}
\slidemark{dot}{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];}
\slidemark{ring-12.1}{\tikz[baseline=-0.85bp]\path[draw=black,line width=0.73bp] (2.60bp,2.60bp) circle[radius=2.24bp];}
\setslidepar{style=tiny}
\setslidelist{itemize}{1}{style=body,indent=18.14,mark=dot,gap=10.28}
\setslidelist{itemize}{2}{style=body-blue-15.1,indent=36.28,mark=ring-12.1,gap=10.04}
```


The frame:

```latex
\begin{frame}[plain,layout=title-and-body]
  \frametitle{Backgrounds}
  \begin{slidebox}{21.5,63.5,319.85,197.1}
    \begin{itemize}
      \item[space=3.02] Otherwise, if you are using images with transparency (such as PNG files) you can change the slide background to any color or any image.
      \item Click \symbol{34}Slide\symbol{34}, then \symbol{34}Background\symbol{34}, then \symbol{34}Color\symbol{34} or \symbol{34}Image\symbol{34}.
    \end{itemize}
  \end{slidebox}
\end{frame}
```


### Pair h14

The two sources are different slides from different talks.


**h14 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Cyan}{HTML}{0079AB}
\definecolor{DarkGrey}{HTML}{595959}
\definecolor{Lime}{HTML}{9CAE4B}
\definecolor{Orange}{HTML}{C98D4B}
\setslideinset{4.08}
\slidestyle{small-medium}{size=8.82, weight=w500, color=black, ascent=8.54, pitch=10.58, depth=2.05}
\slidestyle{large-extrabold-white}{size=11.34, weight=w800, color=white, ascent=10.98, pitch=13.7, depth=2.63}
\slidestyle{small-darkgrey-9.4}{size=9.45, color=DarkGrey, ascent=9.15, pitch=13.04, depth=3.89}
\slidestyle{tiny-medium-5}{size=5.04, weight=w500, color=black, ascent=4.88, pitch=6.05, depth=1.17}
\slidestyle{large-darkgrey}{size=11.34, color=DarkGrey, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{body-light}{size=10.08, weight=w300, color=black, ascent=9.76, pitch=12.28, depth=2.34}
\slidestyle{label}{size=10.08, color=black}
\slidestyle{tiny-medium-5.7}{size=5.67, weight=w500, color=black, ascent=5.49, pitch=6.8, depth=1.32}
\slidestyle{tiny-bold}{size=6.3, weight=bold, color=black, ascent=6.1, pitch=7.56, depth=1.46}
\slidestyle{small-arial-white}{size=8.82, face=\adoptfontA, color=white, ascent=8.54, pitch=10.58, depth=2.05}
\slidestyle{tiny-medium-5.7-2}{size=5.67, weight=w500, color=black, ascent=8.54, pitch=6.8, depth=2.05}
\slidemark{dot-darkgrey}{\tikz[baseline=-0.68bp]\path[fill=DarkGrey] (2.34bp,2.34bp) circle[radius=2.34bp];}
\slidemark{ring-darkgrey}{\tikz[baseline=-0.66bp]\path[draw=DarkGrey,line width=0.57bp] (2.03bp,2.03bp) circle[radius=1.75bp];}
\setslidepar{style=tiny-medium-5}
\setslidelist{itemize}{1}{style=large-darkgrey,indent=22.68,mark=dot-darkgrey,gap=12.25}
\setslidelist{itemize}{2}{style=small-darkgrey-9.4,indent=45.35,mark=ring-darkgrey,gap=12.09}
\setslidelist{enumerate}{1}{style=body-light,indent=22.68,labelstyle=label,label={\arabic*.},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=blank]
  \slideellipse[fill=Lime,draw=Lime,line width=1.89bp]{260.8,127.1,4.19,4.19}
  \sliderect[fill=Lime,draw=DarkGrey,line width=0.47bp,rounded=4.74]{0,0,453.54,28.41}
  \slidetext[middle]{4.2,0,445.17,28.4}{large-extrabold-white}{Pipeline}
  \slidetext[center]{111.4,117,35.59,12.2}{tiny-bold}{Feasibility}
  \slidetext[center]{193.4,117,35.59,12.2}{tiny-bold}{Prototype}
  \slidetext[center]{256.9,117,35.58,12.2}{tiny-bold}{Pilot}
  \slidetext[center]{34.6,117,35.58,12.2}{tiny-bold}{Discovery}
  \slideline[draw=Cyan,line width=1.89bp]{107.23,131.3}{38.8,131.3}
  \slideline[draw=Lime,line width=1.89bp]{184.04,131.3}{115.6,131.3}
  \slideline[draw=Orange,line width=1.89bp]{269.2,131.3}{337.63,131.3}
  \slideellipse[fill=Cyan,draw=Cyan,line width=1.89bp]{30.4,127.1,4.19,4.19}
  \slidetext[middle,center]{34.6,127.1,1.01,8.4}{small-arial-white}{1}
  \slideellipse[fill=Lime,draw=Lime,line width=1.89bp]{184,127.1,4.18,4.19}
  \slidetext[middle]{188.2,127.1,1.01,8.4}{small-arial-white}{2}
  \slideellipse[fill=Orange,draw=Orange,line width=1.89bp]{337.6,127.1,4.19,4.19}
  \slideellipse[fill=Orange,draw=Orange,line width=1.89bp]{414.5,127.1,4.19,4.19}
  \slideline[draw=Lime,line width=1.89bp]{260.83,131.3}{192.4,131.3}
  \slideline[draw=Orange,line width=1.89bp]{346,131.3}{414.44,131.3}
  \slideellipse[fill=Cyan,draw=Cyan,line width=1.89bp]{107.2,127.1,4.19,4.19}
  \slidetext[middle]{111.4,127.1,1.01,8.4}{small-arial-white}{1}
  \slidetext[inset=0]{324.4,117.2,63.1,12.2}{tiny-bold}{Product-Market Fit}
  \slidetext[inset=0]{409.5,117.2,44.04,12.2}{tiny-bold}{Integration}
  \slidetext[mixed]{87.4,56,67.94,40.5}{tiny-medium-5.7-2}{{\slidesize{8.82}\slidestrut{8.54}{2.05}A \slidestrut{8.54}{2.05}Inc.}\slidebreak \slidestrut{5.49}{1.32}Opportunity \slidestrut{5.49}{1.32}\$10M \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}yr\slidebreak \slidestrut{5.49}{1.32}Pilot \slidestrut{5.49}{1.32}\$100k \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}6 \slidestrut{5.49}{1.32}mo}
  \slidetext[mixed]{192.4,36.5,65.55,40.5}{tiny-medium-5.7-2}{{\slidesize{8.82}\slidestrut{8.54}{2.05}B \slidestrut{8.54}{2.05}Ltd.}\slidebreak \slidestrut{5.49}{1.32}Opportunity \slidestrut{5.49}{1.32}\$10M \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}yr\slidebreak \slidestrut{5.49}{1.32}Pilot \slidestrut{5.49}{1.32}\$100k \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}6 \slidestrut{5.49}{1.32}mo}
  \slidetext[mixed]{11.4,37.5,65.55,40.5}{tiny-medium-5.7-2}{{\slidesize{8.82}\slidestrut{8.54}{2.05}C \slidestrut{8.54}{2.05}Corp.}\slidebreak \slidestrut{5.49}{1.32}Opportunity \slidestrut{5.49}{1.32}\$10M \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}yr\slidebreak \slidestrut{5.49}{1.32}Pilot \slidestrut{5.49}{1.32}\$100k \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}6 \slidestrut{5.49}{1.32}mo}
  \slidetext{87.4,160.3,67.94,29.8}{tiny-medium-5.7}{Changes so A can get to the next step}
  \slidetext{188.2,160.3,67.94,29.8}{tiny-medium-5.7}{Changes so B can get to the next step}
  \slidetext[mixed]{189.4,72.6,65.54,40.5}{tiny-medium-5.7-2}{{\slidesize{8.82}\slidestrut{8.54}{2.05}D \slidestrut{8.54}{2.05}Ltd.}\slidebreak \slidestrut{5.49}{1.32}Opportunity \slidestrut{5.49}{1.32}\$10M \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}yr\slidebreak \slidestrut{5.49}{1.32}Pilot \slidestrut{5.49}{1.32}\$100k \slidestrut{5.49}{1.32}/ \slidestrut{5.49}{1.32}6 \slidestrut{5.49}{1.32}mo}
  \slidetext{32.4,219.1,311.74,19.8}{small-medium}{Go ahead and make a funnel, too. If you like that sort of thing.}
\end{frame}
```


**h14 source B** (vocabulary V2)


The frame:

```latex
\begin{frame}[label=versions]{Three versions}
  \centering
  \begin{tikzpicture}[node distance=0.8cm and 1.8cm, every node/.style={font=\small}]
    \node[draw, fill=blue!10, minimum width=2.2cm, minimum height=0.8cm] (base) {Base};
    \node[draw, fill=green!15, minimum width=2.2cm, minimum height=0.8cm, below left=of base] (ours) {Source};
    \node[draw, fill=orange!20, minimum width=2.2cm, minimum height=0.8cm, below right=of base] (theirs) {Deck};
    \node[draw, fill=gray!15, minimum width=2.2cm, minimum height=0.8cm, below=2.4cm of base] (merged) {Merged};
    \draw[-{Stealth}] (base) -- (ours);
    \draw[-{Stealth}] (base) -- (theirs);
    \draw[-{Stealth}] (ours) -- (merged);
    \draw[-{Stealth}] (theirs) -- (merged);
  \end{tikzpicture}
\end{frame}
```


### Pair h15

The two sources are different slides from different talks.


**h15 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}{Emphasis ellipses}
  Stochastic methods estimate the gradient from a small batch, which makes each step cheap
  but \tikzmarknode{noisy}{noisy}; averaging over many steps recovers the true descent
  direction in expectation.
  \begin{tikzpicture}[remember picture, overlay]
    \draw[red, thick] (noisy.center) ellipse [x radius=0.75cm, y radius=0.3cm];
  \end{tikzpicture}

  \vspace{1em}
  Only the \tikzmarknode{last}{last layer} is fine-tuned, the rest stays frozen.
  \begin{tikzpicture}[remember picture, overlay]
    \node[draw=blue, thick, rounded corners=4pt, inner sep=2pt, fit=(last)] {};
  \end{tikzpicture}
\end{frame}
```


**h15 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\slidestyle{tiny-googlesans-white-1.9}{size=1.89, face=\adoptfontA, color=white, ascent=1.83, pitch=2.27, depth=0.44}
\slidestyle{tiny-googlesans-white-5.7}{size=5.67, face=\adoptfontA, color=white, ascent=5.49, pitch=6.8, depth=1.32}
\slidestyle{heading-googlesans-white-14.5}{size=14.49, face=\adoptfontA, color=white, ascent=14.03, pitch=17.48, depth=3.36}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=blank-2,background=black]
  \slidetext[center]{43.2,93.5,25.68,9.1}{tiny-googlesans-white-1.9}{Developer}
  \slidetext[center]{85.1,93.5,25.67,9.1}{tiny-googlesans-white-1.9}{Write}
  \slidetext[center]{127.3,93.5,25.67,9.1}{tiny-googlesans-white-1.9}{Cloud}
  \slidetext[center]{172,93.5,25.67,9.1}{tiny-googlesans-white-1.9}{Audio}
  \slidetext[center]{213,93.5,25.67,9.1}{tiny-googlesans-white-1.9}{Key}
  \slidetext[center]{255.7,93.5,25.67,9.1}{tiny-googlesans-white-1.9}{Desktop Mac}
  \slidetext[center]{297.9,93.5,25.67,9.1}{tiny-googlesans-white-1.9}{Watch}
  \slidetext[center]{342.5,93.5,25.67,9.1}{tiny-googlesans-white-1.9}{Person}
  \slidetext[center]{384.7,93.5,25.68,9.1}{tiny-googlesans-white-1.9}{Car}
  \slidetext[center]{43.2,124.5,25.68,9}{tiny-googlesans-white-1.9}{Devices}
  \slidetext[center]{85.1,124.5,25.67,9}{tiny-googlesans-white-1.9}{Quote}
  \slidetext[inset=0,center]{123.1,124.5,34.11,9}{tiny-googlesans-white-1.9}{Folder}
  \slidetext[inset=0,center]{167.7,124.5,34.11,9}{tiny-googlesans-white-1.9}{Web Page}
  \slidetext[center]{213,124.5,25.67,9}{tiny-googlesans-white-1.9}{Archive}
  \slidetext[center]{255.7,124.5,25.67,9}{tiny-googlesans-white-1.9}{Desktop PC}
  \slidetext[inset=0,center]{293.6,124.5,34.11,9}{tiny-googlesans-white-1.9}{Flag}
  \slidetext[inset=0,center]{338.3,124.5,34.11,9}{tiny-googlesans-white-1.9}{World}
  \slidetext[inset=0,center]{380.4,124.5,34.12,9}{tiny-googlesans-white-1.9}{Boat}
  \slidetext[inset=0,center]{39,153.7,34.12,9.1}{tiny-googlesans-white-1.9}{Software}
  \slidetext[inset=0,center]{80.8,153.7,34.11,9.1}{tiny-googlesans-white-1.9}{Emotion}
  \slidetext[inset=0,center]{123.1,153.7,34.11,9.1}{tiny-googlesans-white-1.9}{Mic}
  \slidetext[inset=0,center]{167.7,153.7,34.11,9.1}{tiny-googlesans-white-1.9}{Call}
  \slidetext[inset=0,center]{208.8,153.7,34.11,9.1}{tiny-googlesans-white-1.9}{Cut}
  \slidetext[inset=0,center]{251.4,153.7,34.11,9.1}{tiny-googlesans-white-1.9}{headphones}
  \slidetext[inset=0,center]{293.6,153.7,34.11,9.1}{tiny-googlesans-white-1.9}{Camera}
  \slidetext[inset=0,center]{338.3,153.7,34.11,9.1}{tiny-googlesans-white-1.9}{Education}
  \slidetext[inset=0,center]{380.4,153.7,34.12,9.1}{tiny-googlesans-white-1.9}{Train}
  \slidetext[inset=0,center]{39,184.3,34.12,9.1}{tiny-googlesans-white-1.9}{Weather}
  \slidetext[inset=0,center]{80.8,184.3,34.11,9.1}{tiny-googlesans-white-1.9}{Link}
  \slidetext[inset=0,center]{123.1,184.3,34.11,9.1}{tiny-googlesans-white-1.9}{Movie}
  \slidetext[inset=0,center]{167.7,184.3,34.11,9.1}{tiny-googlesans-white-1.9}{Chart}
  \slidetext[inset=0,center]{205,184.3,41.16,9.1}{tiny-googlesans-white-1.9}{Paste}
  \slidetext[inset=0,center]{251.4,184.3,34.11,9.1}{tiny-googlesans-white-1.9}{Keyboard}
  \slidetext[inset=0,center]{293.6,184.3,34.11,9.1}{tiny-googlesans-white-1.9}{TV}
  \slidetext[inset=0,center]{338.3,184.3,34.11,9.1}{tiny-googlesans-white-1.9}{MMS}
  \slidetext[inset=0,center]{380.4,184.3,34.12,9.1}{tiny-googlesans-white-1.9}{Subway}
  \slidetext[inset=0,center]{39,215.8,34.12,9.1}{tiny-googlesans-white-1.9}{Hotel}
  \slidetext[inset=0,center]{80.8,215.8,34.11,9.1}{tiny-googlesans-white-1.9}{Laundry}
  \slidetext[center]{120.5,215.8,38.13,9.1}{tiny-googlesans-white-1.9}{Location History}
  \slidetext[inset=0,center]{168.3,215.8,34.11,9.1}{tiny-googlesans-white-1.9}{Layers}
  \slidetext[center]{214.4,215.8,25.68,9.1}{tiny-googlesans-white-1.9}{Offer}
  \slidetext[inset=0,center]{252.4,215.8,34.12,9.1}{tiny-googlesans-white-1.9}{Map}
  \slidetext[inset=0,center]{295.9,215.8,34.11,9.1}{tiny-googlesans-white-1.9}{Bar}
  \slidetext[inset=0,center]{334.5,215.8,40.82,9.1}{tiny-googlesans-white-1.9}{Pizza}
  \slidetext[inset=0,center]{379.9,215.8,34.11,9.1}{tiny-googlesans-white-1.9}{Web}
  \slidefreeform[fill=white]{50.8,76.5,11.25,15.46}{shapes/freeform-1f959c1a.tex}
  \slidefreeform[fill=white]{48.1,111.7,16.88,11.18}{shapes/freeform-a22fc80e.tex}
  \slidefreeform[fill=white]{48.8,139.4,15.46,12.6}{shapes/freeform-91ea67c3.tex}
  \slidefreeform[fill=white]{48.8,170,15.46,12.6}{shapes/freeform-076d9fe8.tex}
  \slidefreeform[fill=white]{90.9,78,14.08,14.01}{shapes/freeform-01f4e7c5.tex}
  \slidefreeform[fill=white]{92.2,114.6,11.58,8.27}{shapes/freeform-7333043e.tex}
  \slidefreeform[fill=white]{89.7,135.5,16.55,16.49}{shapes/freeform-f237a10c.tex}
  \slidefreeform[fill=white]{89.7,174.2,16.55,8.33}{shapes/freeform-a37a9333.tex}
  \slidefreeform[fill=white]{131.4,80.8,16.88,11.18}{shapes/freeform-1934b47c.tex}
  \slidefreeform[fill=white]{132.8,111.6,14.03,11.25}{shapes/freeform-e00dc0e9.tex}
  \slidefreeform[fill=white]{135.5,137.4,10.73,14.62}{shapes/freeform-ad8bb0ba.tex}
  \slidefreeform[fill=white]{133.1,170.3,15.33,12.28}{shapes/freeform-50630d90.tex}
  \slidefreeform[fill=white]{176,110,16.04,12.87}{shapes/freeform-b7ce9177.tex}
  \slidefreeform[fill=white]{176.8,137.6,14.42,14.43}{shapes/freeform-bda02ff0.tex}
  \slidefreeform[fill=white]{176.9,166.5,16.07,16.09}{shapes/freeform-927147cd.tex}
  \slidefreeform[fill=white]{216.9,82.3,17.6,9.62}{shapes/freeform-f0c5114b.tex}
  \slidefreeform[fill=white]{218.6,109,13.91,13.91}{shapes/freeform-7e2c4e76.tex}
  \slidefreeform[fill=white]{217.8,136.5,15.46,15.45}{shapes/freeform-0cdce3a6.tex}
  \slidefreeform[fill=white]{218.7,165.6,13.86,17.01}{shapes/freeform-e2e4eb7e.tex}
  \slidefreeform[fill=white]{258.4,106.2,18.36,16.68}{shapes/freeform-be324bd7.tex}
  \slidefreeform[fill=white]{260.2,136.1,14.93,15.84}{shapes/freeform-0b7f0fcc.tex}
  \slidefreeform[fill=white]{259.3,170.9,16.68,11.63}{shapes/freeform-ef8f67c0.tex}
  \slidefreeform[fill=white]{305.5,74.2,11.83,17.72}{shapes/freeform-595ca85e.tex}
  \sliderect[fill=white]{309.2,146,4.92,4.92}
  \slidefreeform[fill=white]{304,138.2,15.39,13.76}{shapes/freeform-b5b00fdd.tex}
  \slidefreeform[fill=white]{349.8,79.6,12.48,12.41}{shapes/freeform-8b94efc0.tex}
  \slidefreeform[fill=white]{348.2,107.3,15.58,15.59}{shapes/freeform-2eb53043.tex}
  \slidefreeform[fill=white]{347.4,137.9,17.14,14.04}{shapes/freeform-7006e5ff.tex}
  \slidefreeform[fill=white]{349.7,167.8,14.8,14.8}{shapes/freeform-c8ab9af8.tex}
  \slidefreeform[fill=white]{390.2,78.9,14.8,13.06}{shapes/freeform-4ff46731.tex}
  \slidefreeform[fill=white]{389.4,105,16.32,17.91}{shapes/freeform-740306ae.tex}
  \slidefreeform[fill=white]{391,136.5,13.09,15.52}{shapes/freeform-93ac6f9d.tex}
  \slidefreeform[fill=white]{391,166.2,13.13,16.36}{shapes/freeform-b380ee2c.tex}
  \slidefreeform[fill=white]{301,166.2,19.8,16.36}{shapes/freeform-d833ea4f.tex}
  \slidefreeform[fill=white]{258,74.6,19.15,17.38}{shapes/freeform-59f09160.tex}
  \slidefreeform[fill=white]{48.8,200,15.82,10.86}{shapes/freeform-95d9cd58.tex}
  \slidefreeform[fill=white]{91.9,198.2,12.26,15.32}{shapes/freeform-6d92401c.tex}
  \slidefreeform[fill=white]{133.4,198.1,13.91,16.19}{shapes/freeform-d3946151.tex}
  \slidefreeform[fill=white]{179.2,199.2,13.04,13.79}{shapes/freeform-0507e186.tex}
  \slidefreeform[fill=white]{220.3,199,15.22,15.31}{shapes/freeform-6d8788b1.tex}
  \slidefreeform[fill=white]{262.6,199.7,13.87,13.86}{shapes/freeform-2d71e7c9.tex}
  \slidefreeform[fill=white]{306.8,200,12.96,12.96}{shapes/freeform-ad574e53.tex}
  \slidefreeform[fill=white]{347.8,197.8,13.79,15.24}{shapes/freeform-bf5ad474.tex}
  \slidefreeform[fill=white]{389.1,196.6,16.1,16.14}{shapes/freeform-d9fd1be1.tex}
  \slidetext[inset=0]{239.3,34.9,101.38,17}{tiny-googlesans-white-5.7}{All icons are vector objects and can \slidebreak be recolored using the fill menu.}
  \slidepicture{340.6,30.1,70.5,26.5}{figures/image-9302583d.png}
  \slidetext[bottom,inset=0]{42.2,32.8,188.74,17.9}{heading-googlesans-white-14.5}{Icons}
\end{frame}
```


### Pair h16

The two sources are different slides from different talks.


**h16 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Green}{HTML}{93C47D}
\definecolor{LightBlue}{HTML}{9FC5E8}
\definecolor{LightPink}{HTML}{D5A6BD}
\definecolor{Orange}{HTML}{E69138}
\definecolor{Yellow}{HTML}{F1C232}
\setslideinset{1.31}
\slidestyle{heading-serif-orange}{size=15.72, family=serif, color=Orange, ascent=15.22, pitch=18.71, depth=3.65}
\slidestyle{body-italic-green}{size=9.68, italic, color=Green, ascent=9.37, pitch=11.57, depth=2.25}
\slidestyle{small}{size=8.47, color=white, ascent=8.2, pitch=10.2, depth=1.97}
\slidemark{dot-green}{\tikz[baseline=-0.58bp]\path[fill=Green] (2.00bp,2.00bp) circle[radius=2.00bp];}
\slidemark{square-green}{\tikz[baseline=-0.00bp]\path[fill=Green] (0bp,0bp) rectangle (3.81bp,3.81bp);}
\slidemark{ring}{\tikz[baseline=-0.59bp]\path[draw=white,line width=0.51bp] (1.82bp,1.82bp) circle[radius=1.57bp];}
\setslidepar{style=heading-serif-orange}
\setslidelist{itemize}{1}{style=body-italic-green,indent=13.61,mark=dot-green,gap=2.59}
\setslidelist{itemize}{2}{style=small,indent=27.21,mark=ring,gap=2.49}
\setslidelist{itemize}{3}{style=small,indent=40.82,mark=square-green,gap=2.41}
```


The frame:

```latex
\begin{frame}[plain,layout=title-and-body]
  \slidetext{12,10.4,340.25,39.7}{heading-serif-orange}{Key Statistics, Ratios, Trading Multiples Continued....}
  \begin{slidetable}[inset x=1.303, inset y=1.633, border={black,line width=0.34bp}, h=9.76, fixed, aligns={center,center,center,left}, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightBlue}}, pitch=6.48, baseline=6.25]{39.9,29}{78.846,79.155,79.158,78.226}
    \row[h=13.67, style={\fontsize{8.5bp}{10.2bp}\selectfont\bfseries\color{white}}, pitch=10.2, baseline=9.19] \underline{\emph{Moodys}} & \underline{\emph{S\&P}} & \underline{\emph{Fitch}} & \cell[align=center]{\underline{\emph{Definition}}} \\
    \row[style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Green}}] Aaa & AAA & AAA & Highest Quality \\
    Aa1 & AA+ & AA+ & \\
    Aa2 & AA & AA & Very High Quality \\
    \row[h=10.42] Aa3 & AA- & AA- & \\
    \row[h=11.07, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightPink}}] A1 & A+ & A+ & \\
    \row[style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightPink}}] A2 & A & A & High Quality \\
    \row[h=10.42, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightPink}}] A3 & A- & A- & \\
    \row[h=11.07, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Yellow}}] Baa1 & BBB+ & BBB+ & \\
    \row[h=10.42, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Yellow}}] Baa2 & BBB & BBB & Medium Grade \\
    \row[style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Yellow}}] Baa3 & BBB- & BBB- & \\
  \end{slidetable}
  \begin{slidetable}[inset x=1.2, inset y=1.633, border={black,line width=0.34bp}, h=9.78, fixed, aligns={center,center,center,left}, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Green}}, pitch=6.48, baseline=6.2]{39.9,145.1}{79.012,79.012,79.012,79.012}
    Ba1 & BB+ & BB+ & \\
    Ba2 & BB & BB & Speculative \\
    Ba3 & BB- & BB- & \\
    \row[style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightBlue}}] B1 & B+ & B+ & \\
    \row[style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightBlue}}] B2 & B & B & Highly Speculative \\
    \row[style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightBlue}}] B3 & B- & B- & \\
    \row[h=10.43, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightPink}}] Caa1 & CCC+ & CCC+ & \\
    \row[h=10.43, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightPink}}] Caa2 & CCC & CCC & Substantial Risk \\
    \row[h=10.43, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{LightPink}}] Caa3 & CCC- & CCC- & \\
    \row[h=11.09, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Yellow}}] Ca & CC & CC & \\
    \row[h=13.04, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Yellow}}] C & C & C & Very Speculative / Default \\
    \row[h=11.09, style={\fontsize{5.4bp}{6.5bp}\selectfont\color{Yellow}}] - & D & D & \\
  \end{slidetable}
\end{frame}
```


**h16 source B** (vocabulary V2)


The frame:

```latex
\begin{frame}{Math in cells}
  \centering
  \begin{tabular}{lcc}
    \toprule
    Parameter & Value & Error \\
    \midrule
    $\alpha$ & $0.5$ & $\pm 0.01$ \\
    $\lambda_{\max}$ & $10^{-3}$ & $\pm 2\times 10^{-4}$ \\
    \bottomrule
  \end{tabular}
\end{frame}
```


### Pair h17

The two sources are different slides from different talks.


**h17 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}{Columns}
  \begin{columns}[T]
    \column{0.48\textwidth}
    \textbf{Left column}

    Some text on the left side that wraps over a couple of lines.
    \column{0.48\textwidth}
    \textbf{Right column}
    \begin{enumerate}
      \item Alpha
      \item Beta
    \end{enumerate}
  \end{columns}
\end{frame}
```


**h17 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{DarkGrey}{HTML}{595959}
\setslideinset{4.08}
\slidestyle{tiny-darkgrey}{size=6.3, color=DarkGrey, ascent=6.1, pitch=7.56, depth=1.46}
\slidestyle{large}{size=11.34, color=black, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\slidestyle{tiny-6.3}{size=6.3, color=black, ascent=6.1, pitch=7.56, depth=1.46}
\slidemark{dot}{\tikz[baseline=-0.68bp]\path[fill=black] (2.34bp,2.34bp) circle[radius=2.34bp];}
\slidemark{ring}{\tikz[baseline=-0.62bp]\path[draw=black,line width=0.53bp] (1.90bp,1.90bp) circle[radius=1.63bp];}
\slidemark{square}{\tikz[baseline=-0.00bp]\path[fill=black] (0bp,0bp) rectangle (3.97bp,3.97bp);}
\slidemark{dot-8.8}{\tikz[baseline=-0.53bp]\path[fill=black] (1.82bp,1.82bp) circle[radius=1.82bp];}
\setslidepar{style=tiny-6.3}
\setslidelist{itemize}{1}{style=large,indent=22.68,mark=dot,gap=12.25}
\setslidelist{itemize}{2}{style=body,indent=45.35,mark=ring,gap=12.04}
\setslidelist{itemize}{3}{style=body,indent=68.03,mark=square,gap=11.96}
\setslidelist{itemize}{4}{style=body,indent=90.71,mark=dot-8.8,gap=12.04}
```


The frame:

```latex
\begin{frame}[plain,layout=title-and-body,nonumber]
  \frametitle{TLS: Efficiency}
  \begin{slidebox}{14.1,61.8,414.24,186.8}
    \begin{itemize}
      \item Public-key cryptography: Minor costs
      \begin{itemize}
        \item Client and server must perform Diffie-Hellman key exchange
      \end{itemize}
      \item Symmetric-key cryptography: Effectively free
      \begin{itemize}
        \item Modern hardware has dedicated support for symmetric-key cryptography
        \item Performance impact is negligible
      \end{itemize}
      \item Latency: Extra waiting time before the first message
      \begin{itemize}
        \item Must perform the entire TLS handshake before sending the first message
      \end{itemize}
    \end{itemize}
  \end{slidebox}
  \slidetext[middle,right]{424.5,231.3,18.79,19.5}{tiny-darkgrey}{21}
  \note{Handshake has overhead but it’s pretty minor

Every message now has to be encrypted instead of just sending plaintext over TCP

Symmetric-key is so fast: it’s just bit flips!

TLS is such a common protocol, AES, etc. everyone’s using it -> modern hardware is dedicated for that!

Technically slower than TCP, but you’re probably never going to notice it

Latency: in vanilla TCP, need the 3-way handshake (syn, syn-ack, ack), but now in TLS, need to set up entire TCP handshake + everything else}
\end{frame}
```


### Pair h18

The two sources are different slides from different talks.


**h18 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}{Marks on alerted words}
  The loss is \tikzmarknode{al}{\alert{not convex}}, so the start point matters.
  \begin{tikzpicture}[remember picture, overlay]
    \draw[red, thick] (al.south west) -- (al.south east);
  \end{tikzpicture}

  \vspace{1em}
  We \tikzmarknode{st}{\alert{always}} normalise the inputs first.
  \begin{tikzpicture}[remember picture, overlay]
    \draw[black, thick] (st.west) -- (st.east);
  \end{tikzpicture}

  \vspace{1em}
  Use a \tikzmarknode{sq}{\alert{validation split}} to pick the schedule.
  \begin{tikzpicture}[remember picture, overlay]
    \draw[red, thick, decorate, decoration={snake, amplitude=0.6pt, segment length=4pt}]
      ([yshift=-1pt]sq.south west) -- ([yshift=-1pt]sq.south east);
  \end{tikzpicture}
\end{frame}
```


**h18 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Green}{HTML}{1BA94E}
\definecolor{DarkGrey}{HTML}{595959}
\definecolor{Grey2}{HTML}{666666}
\definecolor{OffWhite}{HTML}{EEEEEE}
\setslideinset{4.08}
\slidestyle{body-white}{size=7.56, color=white, ascent=7.32, pitch=10.43, depth=3.11}
\slidestyle{body}{size=7.56, color=black, ascent=7.32, pitch=10.43, depth=3.11}
\slidestyle{label-white}{size=7.56, color=white}
\slidestyle{small-bold}{size=6.3, weight=bold, color=black, ascent=6.1, pitch=7.56, depth=1.46}
\slidestyle{small-arial-grey}{size=6.3, face=\adoptfontA, color=Grey2, ascent=6.1, pitch=7.56, depth=1.46}
\slidestyle{small-arial-white}{size=6.3, face=\adoptfontA, color=white, ascent=6.1, pitch=7.56, depth=1.46}
\slidestyle{small}{size=6.3, color=black, ascent=6.1, pitch=7.56, depth=1.46}
\slidestyle{small-bold-green}{size=6.3, weight=bold, color=Green, ascent=6.1, pitch=7.56, depth=1.46}
\slidemark{dot}{\tikz[baseline=-0.45bp]\path[fill=black] (1.56bp,1.56bp) circle[radius=1.56bp];}
\setslidepar{style=body-white}
\setslidelist{itemize}{1}{style=body,indent=22.68,mark=dot,gap=11.94}
\setslidelist{enumerate}{1}{style=body-white,indent=22.68,labelstyle=label-white,label={\arabic*.},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=content-slide]
  \frametitle{Product Roadmap}
  \slideline[draw=DarkGrey,line width=0.47bp,->]{20.6,255.3}{440.75,255.3}
  \slidetext{23.5,95.5,57.42,15.1}{small-bold}{Product \#1}
  \slidetext{23.5,135.4,57.42,15.1}{small-bold}{Product \#2}
  \slidetext{23.5,175.4,57.42,15.1}{small-bold}{Product \#3}
  \slidetext{23.5,215.3,57.42,15.1}{small-bold}{Product \#4}
  \sliderect[fill=OffWhite,draw=white,line width=0.47bp]{391,24.8,49.73,12.89}
  \slidetext[middle,center]{395.3,24.8,41.3,12.9}{small-arial-grey}{Not Started}
  \sliderect[fill=Green,draw=white,line width=0.47bp]{336.1,24.8,49.73,12.89}
  \slidetext[middle,center]{340.4,24.8,41.3,12.9}{small-arial-white}{In Progress}
  \slideline[draw=DarkGrey,line width=0.47bp]{72.8,255.25}{72.8,70.8}
  \slideline[draw=DarkGrey,line width=0.47bp]{162.2,255.25}{162.2,70.8}
  \slideline[draw=Green,line width=0.47bp]{251.6,255.25}{251.6,70.8}
  \slideline[draw=DarkGrey,line width=0.47bp]{341,255.25}{341,70.8}
  \slideline[draw=DarkGrey,line width=0.47bp]{430.4,255.25}{430.4,70.8}
  \slidetext[center]{44.1,51.7,57.43,15.1}{small}{-6 months}
  \slidetext[center]{133.5,51.7,57.42,15.1}{small}{-3 months}
  \slidetext[center]{223.9,51.7,57.42,15.1}{small-bold-green}{Today}
  \slidetext[center]{312.3,51.7,57.43,15.1}{small}{3 months}
  \slidetext[center]{400.8,51.7,57.42,15.1}{small}{6 months}
  \sliderect[fill=Green,draw=white,line width=0.47bp]{148.2,96.5,151.09,12.89}
  \slidetext[middle,center]{152.4,96.5,142.68,12.9}{small-arial-white}{Feature}
  \sliderect[fill=OffWhite,draw=white,line width=0.47bp]{305.4,96.5,49.73,12.89}
  \slidetext[middle,center]{309.6,96.5,41.3,12.9}{small-arial-grey}{Feature}
  \sliderect[fill=OffWhite]{266.2,136.5,164.24,12.88}
  \slidetext[middle,center]{270.4,136.5,155.83,12.9}{small-arial-grey}{Feature}
  \sliderect[fill=Green,draw=white,line width=0.47bp]{112.5,176.5,151.09,12.88}
  \slidetext[middle,center]{116.7,176.5,142.68,12.9}{small-arial-white}{Feature}
  \sliderect[fill=OffWhite,draw=white,line width=0.47bp]{271.5,176.5,36.65,12.88}
  \slidetext[middle,center]{275.7,176.5,28.22,12.9}{small-arial-grey}{Feature}
  \sliderect[fill=OffWhite,draw=white,line width=0.47bp]{316.1,176.5,69.8,12.88}
  \slidetext[middle,center]{320.3,176.5,61.38,12.9}{small-arial-grey}{Feature}
  \sliderect[fill=OffWhite,draw=white,line width=0.47bp]{308.7,216.4,36.65,12.89}
  \slidetext[middle,center]{312.9,216.4,28.22,12.9}{small-arial-grey}{Feature}
  \sliderect[fill=OffWhite,draw=white,line width=0.47bp]{266.2,216.4,36.65,12.89}
  \slidetext[middle,center]{270.4,216.4,28.22,12.9}{small-arial-grey}{Feature}
\end{frame}
```


### Pair h19

The two sources are different slides from different talks.


**h19 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{DarkGrey}{HTML}{595959}
\definecolor{LightBlue}{HTML}{A4C2F4}
\definecolor{LightGreen}{HTML}{B6D7A8}
\definecolor{Pink}{HTML}{EA9999}
\definecolor{LightOrange}{HTML}{F9CB9C}
\definecolor{Orange}{HTML}{FFAB40}
\definecolor{PaleYellow}{HTML}{FFF2CC}
\setslideinset{4.08}
\slidestyle{large}{size=11.34, color=black, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\slidestyle{label}{size=8.82, color=black}
\slidestyle{body-8.8}{size=8.82, color=black, ascent=8.54, pitch=10.58, depth=2.05}
\slidestyle{tiny}{size=6.3, color=black, ascent=6.1, pitch=7.56, depth=1.46}
\slidemark{dot}{\tikz[baseline=-0.68bp]\path[fill=black] (2.34bp,2.34bp) circle[radius=2.34bp];}
\slidemark{ring}{\tikz[baseline=-0.62bp]\path[draw=black,line width=0.53bp] (1.90bp,1.90bp) circle[radius=1.63bp];}
\slidemark{square}{\tikz[baseline=-0.00bp]\path[fill=black] (0bp,0bp) rectangle (3.97bp,3.97bp);}
\setslidepar{style=tiny}
\setslidelist{itemize}{1}{style=large,indent=22.68,mark=dot,gap=12.25}
\setslidelist{itemize}{2}{style=body,indent=45.35,mark=ring,gap=12.04}
\setslidelist{itemize}{3}{style=body,indent=68.03,mark=square,gap=11.96}
\setslidelist{enumerate}{2}{style=body,indent=45.35,labelstyle=label,label={\arabic*.},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=title-only]
  \frametitle{Example: HTTP Request}
  \sliderect[fill=LightBlue,draw=DarkGrey,line width=0.47bp]{56.5,63.2,71.23,28.41}
  \slidetext[middle,center]{60.8,63.2,62.81,28.4}{body-8.8}{HTTP}
  \sliderect[fill=LightGreen,draw=DarkGrey,line width=0.47bp]{56.5,102.4,71.23,28.41}
  \slidetext[middle,center]{60.8,102.4,62.81,28.4}{body-8.8}{TCP}
  \sliderect[fill=PaleYellow,draw=DarkGrey,line width=0.47bp]{56.5,141.6,71.23,28.41}
  \slidetext[middle,center]{60.8,141.6,62.81,28.4}{body-8.8}{IP}
  \sliderect[fill=LightOrange,draw=DarkGrey,line width=0.47bp]{56.5,180.8,71.23,28.4}
  \slidetext[middle,center]{60.8,180.8,62.81,28.4}{body-8.8}{Ethernet}
  \sliderect[fill=Pink,draw=DarkGrey,line width=0.47bp]{56.5,220,71.23,28.41}
  \slidetext[middle,center]{60.8,220,62.81,28.4}{body-8.8}{Wires}
  \sliderect[fill=LightBlue,draw=DarkGrey,line width=0.47bp]{325.8,63.2,71.23,28.41}
  \slidetext[middle,center]{330,63.2,62.81,28.4}{body-8.8}{HTTP}
  \sliderect[fill=LightGreen,draw=DarkGrey,line width=0.47bp]{325.8,102.4,71.23,28.41}
  \slidetext[middle,center]{330,102.4,62.81,28.4}{body-8.8}{TCP}
  \sliderect[fill=PaleYellow,draw=DarkGrey,line width=0.47bp]{325.8,141.6,71.23,28.41}
  \slidetext[middle,center]{330,141.6,62.81,28.4}{body-8.8}{IP}
  \sliderect[fill=LightOrange,draw=DarkGrey,line width=0.47bp]{325.8,180.8,71.23,28.4}
  \slidetext[middle,center]{330,180.8,62.81,28.4}{body-8.8}{Ethernet}
  \sliderect[fill=Pink,draw=DarkGrey,line width=0.47bp]{325.8,220,71.23,28.41}
  \slidetext[middle,center]{330,220,62.81,28.4}{body-8.8}{Wires}
  \sliderect[fill=LightOrange,draw=DarkGrey,line width=0.47bp]{211.7,140.1,101.29,107.94}
  \begin{slidebox}{216,140.1,92.87,107.9}
    \slidepar{From: 89:8d:33:25:47:24}
    \slidepar{To: d5:a9:20:68:e0:80}
  \end{slidebox}
  \sliderect[fill=PaleYellow,draw=DarkGrey,line width=0.47bp]{220.1,164.3,85.58,80.3}
  \begin{slidebox}{224.3,164.3,77.16,80.3}
    \slidepar{From: 1.2.3.4}
    \slidepar{To: 5.6.7.8}
  \end{slidebox}
  \sliderect[fill=LightGreen,draw=DarkGrey,line width=0.47bp]{226.8,188.8,71.23,53.06}
  \begin{slidebox}{231,188.8,62.81,53.1}
    \slidepar{From: Port 1234}
    \slidepar{To: Port 80}
  \end{slidebox}
  \sliderect[fill=LightBlue,draw=DarkGrey,line width=0.47bp]{233.5,213.3,57.78,25.8}
  \begin{slidebox}{237.7,213.3,49.36,25.8}
    \slidepar{GET / HTTP/1.1}
    \slidepar{...}
  \end{slidebox}
  \sliderect[fill=Pink,draw=DarkGrey,line width=0.47bp]{204.5,113.4,115.78,28.41}
  \slidetext[middle,center]{208.7,113.4,107.36,28.4}{body-8.8}{Received over the physical medium}
  \sliderect[fill=Orange,draw=black,line width=0.94bp]{3.3,160.6,179.44,41.23}
  \slidetext[center]{7.5,160.6,171.03,41.2}{body-8.8}{Notice: The MAC addresses changed because the recipient is on a different network}
  \slideline[draw=DarkGrey,line width=0.47bp,->]{182.7,181.19}{214.03,154.1}
\end{frame}
```


**h19 source B** (vocabulary V2)


The frame:

```latex
\begin{frame}{Centred and right-aligned}
  \begin{center}
    The centred line has $\sqrt{a^2 + b^2}$ in the middle of it.
  \end{center}
  \begin{flushright}
    A right-aligned line with $\frac{p}{q}\sum_k c_k$ near its end.
  \end{flushright}
  \begin{block}{In a block}
    The block body holds $\sum_{j=1}^{m} w_j^2$ between words, and a second line
    with $\bar{X}_n^{(k)}$ too.
  \end{block}
\end{frame}
```


### Pair h20

The two sources are different slides from different talks.


**h20 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}[label=results]{Results}
  \begin{block}{Finding}
    Edits made in the deck survive a new conversion.
  \end{block}
  \vspace{1em}
  Conflicts are reported, never silently overwritten.
\end{frame}
```


**h20 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{DarkBlue}{HTML}{20124D}
\definecolor{DarkGrey2}{HTML}{4D4F51}
\setslideinset{4.08}
\slidestyle{small}{size=11.34, color=black, ascent=10.98, pitch=13.7, depth=2.63}
\slidestyle{body-calibri}{size=15.12, face=\adoptfontA, color=black, ascent=11.91, pitch=14.52, depth=2.6}
\slidestyle{label-bold-darkblue}{size=15.12, weight=bold, color=DarkBlue}
\slidestyle{body-bold-blue}{size=15.12, weight=bold, color=blue, ascent=14.64, pitch=17.95, depth=3.51}
\slidestyle{body-bold-darkgrey-15.1}{size=15.12, weight=bold, color=DarkGrey2, ascent=14.64, pitch=17.95, depth=3.51}
\slidemark{dot}{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];}
\setslidepar{style=small}
\setslidelist{itemize}{1}{style=body-calibri,indent=22.68,labelstyle=label-bold-darkblue,mark=dot,gap=12.55}
```


The frame:

```latex
\begin{frame}[plain,layout=title-and-body]
  \slidetext{18.2,5.6,421.84,27.5}{body-bold-blue}{\textcolor{black}{\uline{Determining Significance}}\textcolor{black}{-} Null Hypothesis and P- value}
  \slidetext{8.8,37.9,434.96,208.6}{body-bold-darkgrey-15.1}{In every experiment, there is an effect or difference between groups that the researchers are testing. It could be the effectiveness of a new drug, new fertilizer, or other variables that has benefits. Unfortunately for the researchers, there is always the possibility that there is no effect, that is, that there is no difference between the groups. This lack of a difference is called the \href{http://support.minitab.com/en-us/minitab/17/topic-library/basic-statistics-and-graphs/hypothesis-tests/basics/null-and-alternative-hypotheses/}{\textcolor{blue}{null hypothesis}}, which is essentially the position a devil’s advocate would take when evaluating the results of an experiment.}
\end{frame}
```


Answer with the JSON list described above, one object per pair, nothing else.
