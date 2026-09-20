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
% the fourth argument is boxed at its own width and set turned about the centre, where it would be if
% the element were upright - the way the deck turns a text box with everything in it.
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
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
% the deck's arrow heads, sized in line widths: > (FILL_ARROW), and SlideStealth, SlideOpen, ...
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
```

### Vocabulary V2

```latex
\renewcommand{\familydefault}{\sfdefault}
\newsavebox\adopt@box
% the fourth argument is boxed at its own width and set turned about the centre, where it would be if
% the element were upright - the way the deck turns a text box with everything in it.
\newcommand\adoptturned[4]{\begingroup
  \def\adopt@angle{#1}\def\adopt@cx{#2}\def\adopt@cy{#3}%
\newdimen\slidesx
\newcount\slidestabn
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
\newif\ifslidesspace
\AddToHook{selectfont}{\ifslidesspace\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax\fi}
\newcommand{\slidesize}[1]{\fontsize{#1bp}{#1bp}\selectfont\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax}
\newcommand{\slidesbox}{\slidesspacetrue\parindent=0pt\parskip=0pt\lineskip=0pt\lineskiplimit=-\maxdimen\hyphenpenalty=10000\exhyphenpenalty=50\tolerance=9999\emergencystretch=0pt\frenchspacing\hbadness=10000\hfuzz=\maxdimen\vbadness=10000\vfuzz=\maxdimen}
\newcommand\adoptrow[2]{\expandafter\edef\csname adopt@row@#1\endcsname{\the\dimexpr#2\relax}%
\newcommand\adoptfix[1]{\expandafter\def\csname adopt@fix@#1\endcsname{1}}% a row the thumbnail measured
\newcommand\adoptsetcell[2]{\vbox{\hsize=#1\relax\linewidth\hsize\parindent\z@
\newcommand\adoptcell[8]{% box, first row, last row, text width, vertical inset (both),
  % width with no insets, its shift, content
  % a measured row is what the deck laid out, to the pixel: text that fills it exactly is no sign of
  % text set without insets
\newcommand\adopt@first[1]{% from a vbox's top to its first baseline (its height reaches its last,
  % and a colour's whatsit on either end keeps \lastbox from counting lines): pieces split off the
  % top until one holds a line
\newcommand\adopttops[1]{...}
\newcommand\adopty[1]{\csname adopt@y@#1\endcsname}
\newcommand\adoptbox[1]{\copy\csname adopt@box@#1\endcsname}
\newcommand\adoptdrop[1]{\csname adopt@drop@#1\endcsname}
\newcommand\adoptht[1]{\csname adopt@ht@#1\endcsname}% the height of a cell's first line
\newcommand\adoptdp[1]{\csname adopt@dp@#1\endcsname}% the depth of its last
```


## The pairs


### Pair f01

The two sources are the same slide written two ways.


**f01 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Red}{HTML}{EA4335}
\setslideinset{4.08}
\slidestyle{title-bold-white}{size=50.39, weight=bold, color=white, ascent=48.78, pitch=60.47, depth=11.69}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=title-slide,background=Red]
  \slidetext{30.2,92.4,401.39,70.2}{title-bold-white}{Quotes}
\end{frame}
```


**f01 source B** (vocabulary V2)


The frame:

```latex
{\setbeamercolor{background canvas}{bg=b2sEA4335}
\begin{frame}[plain]
  \begin{textblock*}{401.39bp}(30.2bp,92.4bp)
    \vbox to 70.2bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{50.39}\bfseries \color{white}\vrule width0bp height48.78bp depth0bp\relax%
      Quotes
    \baselineskip=60.47bp\par}
    \vskip\dimexpr11.69bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
}
```


### Pair f02

The two sources are the same slide written two ways.


**f02 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s873624}{HTML}{873624}
\definecolor{b2s895D1D}{HTML}{895D1D}
\definecolor{b2sD9CCCB}{HTML}{D9CCCB}
\definecolor{b2sDBA253}{HTML}{DBA253}
\definecolor{b2sE8E8E8}{HTML}{E8E8E8}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{362.8bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (362.83bp,-272.12bp);
      \path[fill=b2sE8E8E8,shift={(0bp,0bp)}] (0bp,0bp) -- (362.83bp,0bp) -- (362.83bp,-272.12bp) -- (0bp,-272.12bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{28.07bp}(167.9bp,55.2bp)
    \vbox to 36.6bp{\slidesbox
    \vskip1.45bp
    \begin{otherlanguage}{hebrew}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{27.21}\color{b2sDBA253}\vrule width0bp height26.34bp depth0bp\relax%
      ❧
    \baselineskip=32.50bp\par}\end{otherlanguage}
    \vskip\dimexpr6.31bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{123.8bp}(46.5bp,76.8bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (123.79bp,-0.07bp);
      \path[draw=b2sDBA253,line width=0.50bp] (123.79bp,-0.07bp) -- (0bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{123.8bp}(191.7bp,76.7bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (123.79bp,-0.06bp);
      \path[draw=b2sDBA253,line width=0.50bp] (123.79bp,-0.06bp) -- (0bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{300.99bp}(30.7bp,22.6bp)
    \vbox to 41.8bp{\slidesbox
    \vss
    \begin{otherlanguage}{hebrew}{\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{27.21}\rmfamily\color{b2s895D1D}\vrule width0bp height26.34bp depth0bp\relax%
      מה מייצג כל דבר?
    \baselineskip=32.50bp\par}\end{otherlanguage}
    \vskip\dimexpr6.31bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{310.8bp}(24.3bp,89.2bp)
    \adoptrow{0}{28.86bp}
    \adoptrow{1}{28.86bp}
    \adoptrow{2}{28.86bp}
    \adoptfix{0}
    \adoptfix{1}
    \adoptfix{2}
    \adoptcell{1}{0}{0}{149.58bp}{1.81bp}{155.42bp}{-2.92bp}{%
      \raggedright\large\color{black}\baselineskip=14.52bp\relax%
      \begin{otherlanguage}{hebrew}
      \centering בסיפור\par
      \end{otherlanguage}}
    \adoptcell{2}{0}{0}{149.58bp}{1.81bp}{155.42bp}{-2.92bp}{%
      \raggedright\large\color{black}\baselineskip=14.52bp\relax%
      \begin{otherlanguage}{hebrew}
      \centering בחיים\par
      \end{otherlanguage}}
    \adoptcell{3}{1}{1}{149.58bp}{1.81bp}{155.42bp}{-2.92bp}{%
      \raggedright\large\color{black}\baselineskip=14.52bp\relax%
      \begin{otherlanguage}{hebrew}
      \centering עצים סביב\par
      \end{otherlanguage}}
    \adoptcell{4}{2}{2}{149.58bp}{1.81bp}{155.42bp}{-2.92bp}{%
      \raggedright\large\color{black}\baselineskip=14.52bp\relax%
      \begin{otherlanguage}{hebrew}
      \centering סערה\par
      \end{otherlanguage}}
    \adopttops{3}
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \path[use as bounding box] (0bp,0bp) rectangle (310.8bp,{-\adopty{3}});
      \fill[b2s873624] (0.0bp,{-\adopty{0}}) rectangle (155.4bp,{-\adopty{1}});
      \fill[b2s873624] (155.4bp,{-\adopty{0}}) rectangle (310.8bp,{-\adopty{1}});
      \fill[b2sD9CCCB] (0.0bp,{-\adopty{1}}) rectangle (155.4bp,{-\adopty{2}});
      \fill[b2sD9CCCB] (155.4bp,{-\adopty{1}}) rectangle (310.8bp,{-\adopty{2}});
      \draw[white,line width=0.50bp,line cap=rect] (0.0bp,{-\adopty{0}}) -- (310.8bp,{-\adopty{0}});
      \draw[white,line width=1.51bp,line cap=rect] (0.0bp,{-\adopty{1}}) -- (310.8bp,{-\adopty{1}});
      \draw[white,line width=0.50bp,line cap=rect] (0.0bp,{-\adopty{2}}) -- (310.8bp,{-\adopty{2}});
      \draw[white,line width=0.50bp,line cap=rect] (0.0bp,{-\adopty{3}}) -- (310.8bp,{-\adopty{3}});
      \draw[white,line width=0.50bp,line cap=rect] (0.0bp,{-\adopty{0}}) -- (0.0bp,{-\adopty{3}});
      \draw[white,line width=0.50bp,line cap=rect] (155.4bp,{-\adopty{0}}) -- (155.4bp,{-\adopty{3}});
      \draw[white,line width=0.50bp,line cap=rect] (310.8bp,{-\adopty{0}}) -- (310.8bp,{-\adopty{3}});
      \node[anchor=north west] at (2.9bp,{-\adopty{0}-13.13bp+\adoptht{1}}) {\adoptbox{1}};
      \node[anchor=north west] at (158.3bp,{-\adopty{0}-13.13bp+\adoptht{2}}) {\adoptbox{2}};
      \node[anchor=north west] at (2.9bp,{-\adopty{1}-13.13bp+\adoptht{3}}) {\adoptbox{3}};
      \node[anchor=north west] at (2.9bp,{-\adopty{2}-13.13bp+\adoptht{4}}) {\adoptbox{4}};
    \end{tikzpicture}
  \end{textblock*}
\end{frame}
```


**f02 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{NearBlack}{HTML}{262626}
\definecolor{DarkRed}{HTML}{873624}
\definecolor{LightGrey}{HTML}{D9CCCB}
\setslideinset{1.45}
\slidestyle{body-serif}{size=12.09, family=serif, color=black, ascent=11.7, pitch=14.36, depth=2.8}
\slidestyle{body-serif-nearblack}{size=12.09, family=serif, color=NearBlack, ascent=11.7, pitch=14.36, depth=2.8}
\slidestyle{label-darkred-12.1}{size=12.09, color=DarkRed}
\setslidepar{style=body-serif}
\setslidelist{itemize}{1}{style=body-serif-nearblack,indent=14.51,labelstyle=label-darkred-12.1,label={❧},gap=9.42}
```


The frame:

```latex
\begin{frame}[plain,layout=כותרת-ותוכן]
  \frametitle{מה מייצג כל דבר?}
  \begin{slidetable}[inset x=2.923, inset y=1.814, border={white,line width=0.5bp}, h=28.86, fixed, align=center, style={\large\color{black}}, pitch=14.52, baseline=13.13]{24.3,89.2}{155.424,155.424}
    \row[fill=DarkRed] \cell[lang=hebrew]{בסיפור} & \cell[lang=hebrew]{בחיים} \\
    \row[fill=LightGrey] \cell[lang=hebrew]{עצים סביב} & \\
    \cell[lang=hebrew]{סערה} & \\
    \hborder{1}{white,line width=1.51bp}
  \end{slidetable}
\end{frame}
```


### Pair f03

The two sources are the same slide written two ways.


**f03 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{3.27}
\slidestyle{body}{size=15.12, color=black, ascent=14.64, pitch=18.14, depth=3.51}
\slidestyle{label}{size=15.12, color=black}
\slidestyle{body-bold}{size=15.12, weight=bold, color=black, ascent=14.64, pitch=18.14, depth=3.51}
\slidemark{dot}{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];}
\setslidepar{style=body}
\setslidelist{itemize}{1}{style=body,indent=18.14,mark=dot,gap=10.28}
\setslidelist{enumerate}{1}{style=body,indent=18.14,labelstyle=label,label={\arabic*.},gap=10.03}
```


The frame:

```latex
\begin{frame}[plain,layout=title-and-body]
  \frametitle{CS206: Overview}
  \begin{slidebox}{21.5,63.5,319.85,197.1}
    \slidepar[style=body-bold,space=3.02]{Douglas Blank}
    \begin{itemize}
      \item[space=3.02] dblank@cs.brynmawr.edu
      \item cs.brynmawr.edu/\textasciitilde{}dblank
      \item Park Science 248, (610) 526-6501
      \item Office hours: MW 1-2pm, \& by appointment
    \end{itemize}
    \slidepar[style=body-bold,space=3.02]{Course}
    \begin{itemize}
      \item[space=3.02] cs.brynmawr.edu/Courses/cs206/spring2013
      \item Park Science 349
      \item Tue Thur 2:15 - 3:45
      \item Open Lab: Friday, 1-2pm
    \end{itemize}
  \end{slidebox}
\end{frame}
```


**f03 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2sCFD4D4}{HTML}{CFD4D4}
\definecolor{b2sDA0002}{HTML}{DA0002}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{326.6bp}(18.1bp,265.8bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (326.55bp,-0.01bp);
      \path[draw=b2sCFD4D4,line width=2.02bp] (0bp,0bp) -- (326.55bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{326.6bp}(18.1bp,60.5bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (326.55bp,-0.01bp);
      \path[draw=b2sDA0002,line width=2.02bp] (0bp,0bp) -- (326.55bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{319.78bp}(21.5bp,10.9bp)
    \vbox to 45.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{18.14}\bfseries \color{b2sDA0002}\vrule width0bp height17.56bp depth0bp\relax%
      CS206: Overview
    \baselineskip=21.92bp\par}
    \vskip\dimexpr4.21bp-\prevdepth\relax
    \vskip3.27bp}
  \end{textblock*}
  \begin{textblock*}{319.85bp}(21.5bp,63.5bp)
    \vbox to 197.1bp{\slidesbox
    \vskip3.27bp
    \vskip3.02bp{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\bfseries \color{black}\vrule width0bp height14.64bp depth0bp\relax%
      Douglas Blank
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth-3.02bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      dblank@cs.brynmawr.edu
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      cs.brynmawr.edu/\textasciitilde{}dblank
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Park Science 248, (610) 526-6501
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Office hours: MW 1-2pm, \& by appointment
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth-3.02bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\bfseries \color{black}\vrule width0bp height14.64bp depth0bp\relax%
      Course
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth-3.02bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      cs.brynmawr.edu/Courses/cs206/spring2013
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Park Science 349
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Tue Thur 2:15 - 3:45
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Open Lab: Friday, 1-2pm
    \baselineskip=18.14bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f04

The two sources are the same slide written two ways.


**f04 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s185DA2}{HTML}{185DA2}
\definecolor{b2s2388DB}{HTML}{2388DB}
```


The frame:

```latex
}
\begin{frame}[plain]
  \begin{textblock*}{362.8bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (362.83bp,-60.83bp);
      \path[fill=b2s2388DB,shift={(0bp,0bp)}] (0bp,0bp) -- (362.83bp,0bp) -- (362.83bp,-60.83bp) -- (0bp,-60.83bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{362.8bp}(0.0bp,59.7bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (362.83bp,-0.01bp);
      \path[draw=black,line width=2.27bp,draw opacity=0.149] (0bp,0bp) -- (362.83bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{319.78bp}(21.5bp,10.9bp)
    \vbox to 45.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{18.14}\bfseries \color{white}\vrule width0bp height17.56bp depth0bp\relax%
      Characters and Objects
    \baselineskip=21.92bp\par}
    \vskip\dimexpr4.21bp-\prevdepth\relax
    \vskip3.27bp}
  \end{textblock*}
  \begin{textblock*}{319.85bp}(21.5bp,63.5bp)
    \vbox to 197.1bp{\slidesbox
    \vskip3.27bp
    \vskip3.02bp{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      You can insert any image for your characters and objects.
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      However, I recommend using images in PNG format because of their transparency.
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Good sites for PNG images include:
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=36.28bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s185DA2}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.85bp]\path[draw=black,line width=0.73bp] (2.60bp,2.60bp) circle[radius=2.24bp];\hskip10.04bp}%
      \href{http://openclipart.org}{\uline{http://openclipart.org}}
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=36.28bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s185DA2}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.85bp]\path[draw=black,line width=0.73bp] (2.60bp,2.60bp) circle[radius=2.24bp];\hskip10.04bp}%
      \href{http://www.wpclipart.com}{\uline{http://www.wpclipart.com}}
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=36.28bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s185DA2}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.85bp]\path[draw=black,line width=0.73bp] (2.60bp,2.60bp) circle[radius=2.24bp];\hskip10.04bp}%
      \href{http://www.clker.com}{\uline{http://www.clker.com}}
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=36.28bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s185DA2}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.85bp]\path[draw=black,line width=0.73bp] (2.60bp,2.60bp) circle[radius=2.24bp];\hskip10.04bp}%
      \href{http://images.google.com/advanced_image_search?hl=en}{\uline{http://images.google.com/advanced\_image\_search?hl=en}}
    \baselineskip=18.14bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{61.4bp}(291.4bp,123.0bp)
    \includegraphics[width=61.4bp,height=78.0bp]{figures/picture-2dba4100.png}
  \end{textblock*}
\end{frame}
```


**f04 source B** (vocabulary V1)


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
  \frametitle{Characters and Objects}
  \begin{slidebox}{21.5,63.5,319.85,197.1}
    \begin{itemize}
      \item[space=3.02] You can insert any image for your characters and objects.
      \item However, I recommend using images in PNG format because of their transparency.
      \item Good sites for PNG images include:
      \begin{itemize}
        \item \href{http://openclipart.org}{\uline{http://openclipart.org}}
        \item \href{http://www.wpclipart.com}{\uline{http://www.wpclipart.com}}
        \item \href{http://www.clker.com}{\uline{http://www.clker.com}}
        \item \href{http://images.google.com/advanced_image_search?hl=en}{\uline{http://images.google.com/advanced\_image\_search?hl=en}}
      \end{itemize}
    \end{itemize}
  \end{slidebox}
  \slidepicture{291.4,123,61.4,78}{figures/picture-2dba4100.png}
\end{frame}
```


### Pair f05

The two sources are the same slide written two ways.


**f05 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{deckbg}{HTML}{FFFFFF}
\setslideinset{4.08}
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
\begin{frame}[plain,layout=section-header-optional,background=deckbg]
  \frametitle{TLS Trust Issues: Certificate Authorities}
  \note{1 hr 5 min - 1:45}
\end{frame}
```


**f05 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s080302}{HTML}{080302}
\definecolor{b2s595959}{HTML}{595959}
\definecolor{b2s742E2A}{HTML}{742E2A}
\definecolor{b2sE25952}{HTML}{E25952}
```


The frame:

```latex
}
\begin{frame}[plain]
  \begin{textblock*}{453.5bp}(0.0bp,50.5bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (453.54bp,-5.53bp);
      \path[left color=b2s080302,right color=b2sE25952,middle color=b2s742E2A,shift={(0bp,0bp)}] (0bp,0bp) -- (453.54bp,0bp) -- (453.54bp,-5.53bp) -- (0bp,-5.53bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{445.17bp}(4.2bp,50.5bp)
    \vbox to 5.5bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{3.78}\bfseries \color{white}\vrule width0bp height3.66bp depth0bp\relax%
      Computer Science 161
    \baselineskip=4.54bp\par}
    \vskip\dimexpr0.88bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(19.7bp,106.7bp)
    \vbox to 41.8bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\color{black}\vrule width0bp height21.95bp depth0bp\relax%
      TLS Trust Issues: Certificate Authorities
    \baselineskip=27.40bp\par}
    \vskip\dimexpr5.26bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{18.79bp}(424.5bp,231.3bp)
    \vbox to 19.5bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\color{b2s595959}\vrule width0bp height6.10bp depth0bp\relax%
      33
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \note{1 hr 5 min - 1:45}
\end{frame}
```


### Pair f06

The two sources are the same slide written two ways.


**f06 source A** (vocabulary V2)


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{198.6bp}(134.5bp,46.4bp)
    \includegraphics[width=198.6bp,height=153.5bp]{figures/picture-30abff92.png}
  \end{textblock*}
\end{frame}
```


**f06 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{PaleBlue}{HTML}{C9DAF8}
\setslideinset{4.08}
\slidestyle{body}{size=8.82, color=PaleBlue, ascent=8.54, pitch=10.58, depth=2.05}
\slidestyle{label}{size=8.82, color=PaleBlue}
\setslidepar{style=body}
\setslidelist{itemize}{1}{style=body,indent=22.68,labelstyle=label,label={-},gap=12.54}
\setslidelist{enumerate}{1}{style=body,indent=22.68,labelstyle=label,label={\arabic*.},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=title-slide]
  \slidepicture{134.5,46.4,198.6,153.5}{figures/picture-30abff92.png}
\end{frame}
```


### Pair f07

The two sources are the same slide written two ways.


**f07 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s1C4587}{HTML}{1C4587}
```


The frame:

```latex
}
{\setbeamertemplate{background canvas}{\includegraphics[width=\paperwidth,height=\paperheight]{figures/background-6b8af706.jpg}}
\begin{frame}[plain]
  \begin{textblock*}{362.8bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (362.83bp,-68.58bp);
      \path[fill=b2s1C4587,fill opacity=0.354,shift={(0bp,0bp)}] (0bp,0bp) -- (362.83bp,0bp) -- (362.83bp,-68.58bp) -- (0bp,-68.58bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{296.41bp}(3.0bp,9.1bp)
    \vbox to 37.4bp{\slidesbox
    \vskip3.27bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.17}\adoptfontB\color{black}\vrule width0bp height21.46bp depth0bp\relax%
      Individual Research
    \baselineskip=26.46bp\par}
    \vskip\dimexpr5.14bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{272.4bp}(17.2bp,77.7bp)
    \includegraphics[width=272.4bp,height=127.0bp]{figures/picture-94b0d61d.png}
  \end{textblock*}
  \begin{textblock*}{120.1bp}(225.6bp,49.0bp)
    \noindent\rotatebox[origin=c]{-15.25}{\textcolor{black}{\resizebox*{118.5bp}{21.8bp}{\bfseries Example}}}
  \end{textblock*}
  \begin{textblock*}{68.8bp}(121.0bp,244.1bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (68.78bp,-0.01bp);
      \path[draw=black,line width=3.02bp,{Triangle[length=0bp 5.0, width=0bp 4.5]}-{Circle[length=0bp 3.6]}] (0bp,0bp) -- (68.78bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{132.5bp}(196.3bp,231.2bp)
    \noindent\textcolor{black}{\resizebox*{132.5bp}{21.8bp}{\bfseries Template}}
  \end{textblock*}
  \begin{textblock*}{60.0bp}(298.6bp,2.3bp)
    \noindent\textcolor{black}{\resizebox*{60.0bp}{26.0bp}{\bfseries Grade 3}}
  \end{textblock*}
  \begin{textblock*}{57.4bp}(300.3bp,28.8bp)
    \noindent\textcolor{black}{\resizebox*{57.4bp}{25.9bp}{\bfseries Science}}
  \end{textblock*}
  \begin{textblock*}{296.41bp}(4.3bp,10.0bp)
    \vbox to 37.4bp{\slidesbox
    \vskip3.27bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.17}\adoptfontB\color{white}\vrule width0bp height21.46bp depth0bp\relax%
      Individual Research
    \baselineskip=26.46bp\par}
    \vskip\dimexpr5.14bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{81.1bp}(38.1bp,225.4bp)
    \includegraphics[width=81.1bp,height=37.4bp]{figures/picture-1643bd0e.png}
  \end{textblock*}
\end{frame}
```


**f07 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{3.27}
\slidestyle{label}{size=12.09, color=white}
\slidestyle{small}{size=11.09, color=white, ascent=10.74, pitch=13.23, depth=2.57}
\slidestyle{body-12.1}{size=12.09, color=white, ascent=11.7, pitch=14.36, depth=2.8}
\slidestyle{label-11.1}{size=11.09, color=white}
\slidestyle{heading-deliusunicase-22.2}{size=22.17, face=\adoptfontB, color=white, ascent=21.46, pitch=26.46, depth=5.14}
\slidestyle{tiny-calibri-7.1}{size=7.06, face=\adoptfontA, color=white, ascent=6.83, pitch=9.74, depth=2.91}
\slidemark{ring-7.1}{\tikz[baseline=-0.49bp]\path[draw=white,line width=0.42bp] (1.52bp,1.52bp) circle[radius=1.31bp];}
\setslidepar{style=small}
\setslidelist{itemize}{1}{style=body-12.1,indent=18.14,labelstyle=label,label={★},gap=10.03}
\setslidelist{itemize}{2}{style=tiny-calibri-7.1,indent=36.28,mark=ring-7.1,gap=9.64}
\setslidelist{enumerate}{1}{style=small,indent=18.14,labelstyle=label-11.1,label={\arabic*.},gap=10.03}
```


The frame:

```latex
\begin{frame}[plain,layout=subtitle-cold]
  \frametitle{Individual Research}
  \slidepicture{17.2,77.7,272.4,127}{figures/picture-94b0d61d.png}
  \begin{textblock*}{120.1bp}(225.6bp,49.0bp)
    \noindent\rotatebox[origin=c]{-15.25}{\textcolor{black}{\resizebox*{118.5bp}{21.8bp}{\bfseries Example}}}
  \end{textblock*}
  \slideline[draw=black,line width=3.02bp,<-SlideCircle]{121,244.1}{189.78,244.1}
  \begin{textblock*}{132.5bp}(196.3bp,231.2bp)
    \noindent\textcolor{black}{\resizebox*{132.5bp}{21.8bp}{\bfseries Template}}
  \end{textblock*}
  \begin{textblock*}{60.0bp}(298.6bp,2.3bp)
    \noindent\textcolor{black}{\resizebox*{60.0bp}{26.0bp}{\bfseries Grade 3}}
  \end{textblock*}
  \begin{textblock*}{57.4bp}(300.3bp,28.8bp)
    \noindent\textcolor{black}{\resizebox*{57.4bp}{25.9bp}{\bfseries Science}}
  \end{textblock*}
  \slidetext{4.3,10,296.41,37.4}{heading-deliusunicase-22.2}{Individual Research}
  \slidepicture{38.1,225.4,81.1,37.4}{figures/picture-1643bd0e.png}
\end{frame}
```


### Pair f08

The two sources are the same slide written two ways.


**f08 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{DarkGrey}{HTML}{555555}
\definecolor{Grey}{HTML}{898989}
\definecolor{Orange3}{HTML}{E36C0A}
\definecolor{Red}{HTML}{E84E4E}
\definecolor{Orange}{HTML}{FFA500}
\definecolor{Yellow}{HTML}{FFC000}
\setslideinset{1.45}
\slidestyle{tiny-grey}{size=6.05, color=Grey, ascent=5.86, pitch=7.26, depth=1.4}
\slidestyle{tiny}{size=9.07, color=black, ascent=8.78, pitch=10.96, depth=2.1}
\slidestyle{body-serif-bold-orange}{size=16.13, family=serif, weight=bold, color=Orange, ascent=15.61, pitch=19.28, depth=3.74}
\slidestyle{label-yellow}{size=16.12, color=Yellow}
\slidestyle{body-serif-darkgrey}{size=16.13, family=serif, color=DarkGrey, ascent=15.61, pitch=19.28, depth=3.74}
\slidestyle{label}{size=16.13, color=black}
\slidestyle{small}{size=14.11, color=black, ascent=13.66, pitch=17.01, depth=3.27}
\slidestyle{label-14.1}{size=14.11, color=black}
\setslidepar{style=body-serif-darkgrey}
\setslidelist{itemize}{1}{style=tiny,indent=13.61,labelstyle=label,label={•},gap=10.03}
\setslidelist{itemize}{2}{style=small,indent=13.61,labelstyle=label-14.1,label={•},gap=7.51}
```


The frame:

```latex
\begin{frame}[plain,layout=عنوان-ومحتوى]
  \begin{slidebox}[lang=arabic,space=3.23]{3.4,54.4,348.97,217.7}
    \begin{itemize}
      \item[style=body-serif-bold-orange,labelstyle=label-yellow,gap=2.97,space=0] {\slidesize{4.03}\textcolor{Yellow}{\textsf{~}}}\textcolor{Yellow}{{\adoptfontA •}}{\slidesize{4.03}\textcolor{Yellow}{\textsf{~~}}}~التطبيقات المشتركة~Application Sharing:
    \end{itemize}
    \slidepar[indent=13.61,first=-10.08]{\textcolor{Red}{~ ~ ~}تمكين الطلاب أو المتدربين من المشاركة سوياً في العمل على أحد البرامج (تحرير النصوص ، عروض ... إلخ )أو استخدام السبورة الإلكترونية على الشبكة.\slidebreak \textcolor{Orange}{\textbf{~}}{\slidesize{4.03}\textcolor{Yellow}{\textbf{\textsf{~~}}}}\textcolor{Yellow}{\textbf{{\adoptfontA •}}}{\slidesize{4.03}\textcolor{Yellow}{\textbf{\textsf{~}}}}\textcolor{Orange}{\textbf{~ مؤتمرات الفيديو~Video conferencing:}}}
    \slidepar[indent=13.61,first=-10.08]{\textcolor{Red}{~ ~ ~~}\symbol{34}التواصل بالصوت والصورة والنص بين المعلم وطلابه والطلاب بعضهم البعض.\symbol{34} (المبارك، 1425: 60)}
    \begin{itemize}
      \item[style=body-serif-bold-orange,labelstyle=label-yellow,gap=2.97] {\slidesize{4.03}\textcolor{Yellow}{\textsf{~}}}\textcolor{Yellow}{{\adoptfontA •}}{\slidesize{4.03}\textcolor{Yellow}{\textsf{~ ~}}}~مؤتمرات الصوت~Audio conferencing:
    \end{itemize}
    \slidepar[indent=13.61,first=-10.08]{\textcolor{Orange3}{~ ~ ~}\symbol{34}التواصل~بالصوت والنص بين المعلم وطلابه وبين الطلاب بعضهم البعض.\symbol{34} (المبارك، 1425: 60)\slidebreak \slidebreak }
  \end{slidebox}
  \slidetext[middle,lang=arabic]{127.3,252.2,108.2,14.5}{tiny-grey}{التعليم الإلكتروني : الفصول الافتراضية}
  \slidetext[middle,lang=arabic]{127.3,252.2,108.14,14.5}{tiny}{التعليم الإلكتروني : الفصول الافتراضية}
  \slidetext[middle,lang=hebrew]{263.4,252.2,77.95,14.5}{tiny-grey}{}
\end{frame}
```


**f08 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s555555}{HTML}{555555}
\definecolor{b2s898989}{HTML}{898989}
\definecolor{b2sE36C0A}{HTML}{E36C0A}
\definecolor{b2sE84E4E}{HTML}{E84E4E}
\definecolor{b2sFFA500}{HTML}{FFA500}
\definecolor{b2sFFC000}{HTML}{FFC000}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{363.8bp}(-0.5bp,0.0bp)
    \includegraphics[width=363.8bp,height=103.0bp]{figures/picture-60d2551b.png}
  \end{textblock*}
  \begin{textblock*}{348.97bp}(3.4bp,54.4bp)
    \vbox to 217.7bp{\slidesbox
    \vskip1.45bp
    \begin{otherlanguage}{arabic}{\leftskip=13.61bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{16.13}\rmfamily\bfseries \color{b2sFFA500}\vrule width0bp height15.61bp depth0bp\relax\llap{{\slidesize{16.12}\color{b2sFFC000}•}\hskip2.97bp}%
      {\slidesize{4.03}\textcolor{b2sFFC000}{\textsf{~}}}\textcolor{b2sFFC000}{{\adoptfontA •}}{\slidesize{4.03}\textcolor{b2sFFC000}{\textsf{~~}}}~التطبيقات المشتركة~Application Sharing:
    \baselineskip=19.28bp\par}\end{otherlanguage}
    \prevdepth=\dimexpr\prevdepth-3.23bp\relax\begin{otherlanguage}{arabic}{\leftskip=13.61bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{16.13}\rmfamily\color{b2s555555}\vrule width0bp height15.61bp depth0bp\relax\hskip-10.08bp\relax%
      \textcolor{b2sE84E4E}{~ ~ ~}تمكين الطلاب أو المتدربين من المشاركة سوياً في العمل على أحد البرامج (تحرير النصوص ، عروض ... إلخ )أو استخدام السبورة الإلكترونية على الشبكة.\unskip\break \textcolor{b2sFFA500}{\textbf{~}}{\slidesize{4.03}\textcolor{b2sFFC000}{\textbf{\textsf{~~}}}}\textcolor{b2sFFC000}{\textbf{{\adoptfontA •}}}{\slidesize{4.03}\textcolor{b2sFFC000}{\textbf{\textsf{~}}}}\textcolor{b2sFFA500}{\textbf{~ مؤتمرات الفيديو~Video conferencing:}}
    \baselineskip=19.28bp\par}\end{otherlanguage}
    \prevdepth=\dimexpr\prevdepth-3.23bp\relax\begin{otherlanguage}{arabic}{\leftskip=13.61bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{16.13}\rmfamily\color{b2s555555}\vrule width0bp height15.61bp depth0bp\relax\hskip-10.08bp\relax%
      \textcolor{b2sE84E4E}{~ ~ ~~}\symbol{34}التواصل بالصوت والصورة والنص بين المعلم وطلابه والطلاب بعضهم البعض.\symbol{34} (المبارك، 1425: 60)
    \baselineskip=19.28bp\par}\end{otherlanguage}
    \prevdepth=\dimexpr\prevdepth-3.23bp\relax\begin{otherlanguage}{arabic}{\leftskip=13.61bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{16.13}\rmfamily\bfseries \color{b2sFFA500}\vrule width0bp height15.61bp depth0bp\relax\llap{{\slidesize{16.12}\color{b2sFFC000}•}\hskip2.97bp}%
      {\slidesize{4.03}\textcolor{b2sFFC000}{\textsf{~}}}\textcolor{b2sFFC000}{{\adoptfontA •}}{\slidesize{4.03}\textcolor{b2sFFC000}{\textsf{~ ~}}}~مؤتمرات الصوت~Audio conferencing:
    \baselineskip=19.28bp\par}\end{otherlanguage}
    \prevdepth=\dimexpr\prevdepth-3.23bp\relax\begin{otherlanguage}{arabic}{\leftskip=13.61bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{16.13}\rmfamily\color{b2s555555}\vrule width0bp height15.61bp depth0bp\relax\hskip-10.08bp\relax%
      \textcolor{b2sE36C0A}{~ ~ ~}\symbol{34}التواصل~بالصوت والنص بين المعلم وطلابه وبين الطلاب بعضهم البعض.\symbol{34} (المبارك، 1425: 60)\unskip\break \unskip\break
    \baselineskip=19.28bp\par}\end{otherlanguage}
    \vskip\dimexpr3.74bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{108.20bp}(127.3bp,252.2bp)
    \vbox to 14.5bp{\slidesbox
    \vss
    \begin{otherlanguage}{arabic}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.05}\color{b2s898989}\vrule width0bp height5.86bp depth0bp\relax%
      التعليم الإلكتروني : الفصول الافتراضية
    \baselineskip=7.26bp\par}\end{otherlanguage}
    \vskip\dimexpr1.40bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{108.14bp}(127.3bp,252.2bp)
    \vbox to 14.5bp{\slidesbox
    \vss
    \begin{otherlanguage}{arabic}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{9.07}\color{black}\vrule width0bp height8.78bp depth0bp\relax%
      التعليم الإلكتروني : الفصول الافتراضية
    \baselineskip=10.96bp\par}\end{otherlanguage}
    \vskip\dimexpr2.10bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{77.95bp}(263.4bp,252.2bp)
    \vbox to 14.5bp{\slidesbox
    \vss
    \begin{otherlanguage}{hebrew}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.05}\color{b2s898989}\vrule width0bp height5.86bp depth0bp\relax% blank line
    \baselineskip=7.26bp\par}\end{otherlanguage}
    \vskip\dimexpr1.40bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f09

The two sources are the same slide written two ways.


**f09 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{deckbg}{HTML}{EEEEEE}
\definecolor{Red}{HTML}{EA4335}
\setslideinset{4.08}
\slidestyle{large-googlesans-semibold}{size=11.34, face=\adoptfontA, weight=w600, color=black, ascent=10.98, pitch=13.7, depth=2.63}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\slidestyle{title-googlesans-bold}{size=113.39, face=\adoptfontA, weight=bold, color=black, ascent=109.76, pitch=136.06, depth=26.31}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=blank-1,background=deckbg]
  \slidetext[center]{81.9,18.3,289.02,146.6}{title-googlesans-bold}{97\%}
  \sliderect[fill=Red,draw=black,line width=1.42bp,rounded=9.49]{103.5,155.9,246.58,56.94}
  \slidetext[middle,center]{107.7,155.9,238.18,56.9}{large-googlesans-semibold}{Statistic caption this is body copy and it goes a little like this}
\end{frame}
```


**f09 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2sEA4335}{HTML}{EA4335}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{289.02bp}(81.9bp,18.3bp)
    \vbox to 146.6bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{113.39}\adoptfontA\bfseries \color{black}\vrule width0bp height109.76bp depth0bp\relax%
      97\%
    \baselineskip=136.06bp\par}
    \vskip\dimexpr26.31bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{246.6bp}(103.5bp,155.9bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (246.58bp,-56.94bp);
      \path[fill=b2sEA4335,draw=black,line width=1.42bp,shift={(0bp,0bp)}] (0bp,-9.49bp) .. controls (0bp,-4.25bp) and (4.25bp,0bp) .. (9.49bp,0bp) -- (237.09bp,0bp) .. controls (242.33bp,0bp) and (246.58bp,-4.25bp) .. (246.58bp,-9.49bp) -- (246.58bp,-47.45bp) .. controls (246.58bp,-52.69bp) and (242.33bp,-56.94bp) .. (237.09bp,-56.94bp) -- (9.49bp,-56.94bp) .. controls (4.25bp,-56.94bp) and (0bp,-52.69bp) .. (0bp,-47.45bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{238.18bp}(107.7bp,155.9bp)
    \vbox to 56.9bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\adoptfontA\fontseries{w600}\selectfont \color{black}\vrule width0bp height10.98bp depth0bp\relax%
      Statistic caption this is body copy and it goes a little like this
    \baselineskip=13.70bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f10

The two sources are the same slide written two ways.


**f10 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{deckbg}{HTML}{FFF8EC}
\slidestyle{body}{size=7.56, color=black, ascent=7.32, pitch=12.7, depth=1.75}
\slidestyle{large-arial}{size=10.08, face=\adoptfontA, color=black, ascent=9.76, pitch=14.52, depth=4.76}
\slidestyle{title-pacifico}{size=25.2, face=\adoptfontB, color=black, ascent=24.39, pitch=36.29, depth=11.89}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=blank,background=deckbg]
  \slidepicture{63.5,115.7,37.8,33.9}{figures/picture-ef3729a7.png}
  \slidepicture{194,112.6,65.5,40}{figures/picture-2d56df9c.png}
  \slidepicture{350.3,108.1,41.9,49.1}{figures/picture-435039fd.png}
  \slidepicture[trim=0 0 207.46 193.3]{368.3,0.6,86,81}{figures/picture-e3002213.png}
  \slidetext[center]{63,25.5,327.63,30.2}{title-pacifico}{Write your topic or idea}
  \slidetext[center]{25.5,160.1,113.72,12}{large-arial}{Add a main point}
  \slidetext[center]{169.9,160.1,113.71,12}{large-arial}{Add a main point}
  \slidetext[center]{314.3,160.1,113.72,12}{large-arial}{Add a main point}
  \slidetext[center]{25.5,179.6,113.72,20.7}{body}{Briefly elaborate on what you want to discuss.}
  \slidetext[center]{169.9,179.6,113.71,20.7}{body}{Briefly elaborate on what you want to discuss.}
  \slidetext[center]{314.3,179.6,113.72,20.7}{body}{Briefly elaborate on what you want to discuss.}
\end{frame}
```


**f10 source B** (vocabulary V2)


The frame:

```latex
}
\begin{frame}[plain]
  \begin{textblock*}{37.8bp}(63.5bp,115.7bp)
    \includegraphics[width=37.8bp,height=33.9bp]{figures/picture-ef3729a7.png}
  \end{textblock*}
  \begin{textblock*}{65.5bp}(194.0bp,112.6bp)
    \includegraphics[width=65.5bp,height=40.0bp]{figures/picture-2d56df9c.png}
  \end{textblock*}
  \begin{textblock*}{41.9bp}(350.3bp,108.1bp)
    \includegraphics[width=41.9bp,height=49.1bp]{figures/picture-435039fd.png}
  \end{textblock*}
  \begin{textblock*}{86.0bp}(368.3bp,0.6bp)
    \includegraphics[trim=0.00 0.00 207.46 193.30,clip,width=86.0bp,height=81.0bp]{figures/picture-e3002213.png}
  \end{textblock*}
  \begin{textblock*}{327.63bp}(63.0bp,25.5bp)
    \vbox to 30.2bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{25.20}\adoptfontB\color{black}\vrule width0bp height24.39bp depth0bp\relax%
      Write your topic or idea
    \baselineskip=36.29bp\par}
    \vskip\dimexpr11.89bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{113.72bp}(25.5bp,160.1bp)
    \vbox to 12.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{10.08}\adoptfontA\color{black}\vrule width0bp height9.76bp depth0bp\relax%
      Add a main point
    \baselineskip=14.52bp\par}
    \vskip\dimexpr4.76bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{113.71bp}(169.9bp,160.1bp)
    \vbox to 12.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{10.08}\adoptfontA\color{black}\vrule width0bp height9.76bp depth0bp\relax%
      Add a main point
    \baselineskip=14.52bp\par}
    \vskip\dimexpr4.76bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{113.72bp}(314.3bp,160.1bp)
    \vbox to 12.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{10.08}\adoptfontA\color{black}\vrule width0bp height9.76bp depth0bp\relax%
      Add a main point
    \baselineskip=14.52bp\par}
    \vskip\dimexpr4.76bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{113.72bp}(25.5bp,179.6bp)
    \vbox to 20.7bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{7.56}\color{black}\vrule width0bp height7.32bp depth0bp\relax%
      Briefly elaborate on what you want to discuss.
    \baselineskip=12.70bp\par}
    \vskip\dimexpr1.75bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{113.71bp}(169.9bp,179.6bp)
    \vbox to 20.7bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{7.56}\color{black}\vrule width0bp height7.32bp depth0bp\relax%
      Briefly elaborate on what you want to discuss.
    \baselineskip=12.70bp\par}
    \vskip\dimexpr1.75bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{113.72bp}(314.3bp,179.6bp)
    \vbox to 20.7bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{7.56}\color{black}\vrule width0bp height7.32bp depth0bp\relax%
      Briefly elaborate on what you want to discuss.
    \baselineskip=12.70bp\par}
    \vskip\dimexpr1.75bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f11

The two sources are the same slide written two ways.


**f11 source A** (vocabulary V2)


The frame:

```latex
{\setbeamercolor{background canvas}{bg=b2s4A86E8}
\begin{frame}[plain]
  \begin{textblock*}{331.39bp}(15.7bp,113.8bp)
    \vbox to 44.5bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{white}\vrule width0bp height14.64bp depth0bp\relax%
      Expressions
    \baselineskip=18.14bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
}
```


**f11 source B** (vocabulary V1)


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


### Pair f12

The two sources are the same slide written two ways.


**f12 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Red}{HTML}{EA4335}
\setslideinset{4.08}
\slidestyle{title-bold-white}{size=50.39, weight=bold, color=white, ascent=48.78, pitch=60.47, depth=11.69}
\slidestyle{body}{size=8.82, color=black, ascent=8.54, pitch=12.17, depth=3.63}
\setslidepar{style=body}
```


The frame:

```latex
\begin{frame}[plain,layout=title-slide,background=Red]
  \slidetext{30.2,92.4,401.39,70.2}{title-bold-white}{Code}
\end{frame}
```


**f12 source B** (vocabulary V2)


The frame:

```latex
{\setbeamercolor{background canvas}{bg=b2sEA4335}
\begin{frame}[plain]
  \begin{textblock*}{401.39bp}(30.2bp,92.4bp)
    \vbox to 70.2bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{50.39}\bfseries \color{white}\vrule width0bp height48.78bp depth0bp\relax%
      Code
    \baselineskip=60.47bp\par}
    \vskip\dimexpr11.69bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
}
```


### Pair f13

The two sources are the same slide written two ways.


**f13 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s424242}{HTML}{424242}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{30.5bp}(21.3bp,63.3bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (30.46bp,-0.01bp);
      \path[draw=b2s424242,line width=0.94bp,dash pattern=on 7.56bp off 2.83bp] (0bp,0bp) -- (30.46bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{38.55bp}(402.3bp,3.4bp)
    \vbox to 21.3bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\adoptfontA\bfseries \color{black}\vrule width0bp height14.64bp depth0bp\relax%
      學科
    \baselineskip=17.95bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(19.7bp,18.5bp)
    \vbox to 36.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{27.72}\adoptfontA\color{red}\vrule width0bp height26.83bp depth0bp\relax%
      第一題
    \baselineskip=33.07bp\par}
    \vskip\dimexpr6.43bp-\prevdepth\relax
    \vskip4.08bp}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(19.7bp,72.8bp)
    \vbox to 172.3bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{17.64}\adoptfontB\color{black}\vrule width0bp height17.08bp depth0bp\relax%
      - Get Ready to Use Technology in the Classroom.
    \baselineskip=24.34bp\par}
    \prevdepth=\dimexpr\prevdepth-1.04bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax%
      {\adoptfontB -\ }When trying to select the right tool to integrate into your class, you should always start with:
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip22.68bp\relax%
      A The learning goal
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip22.68bp\relax%
      B The functionality of the tool
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip22.68bp\relax%
      C A help site
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip22.68bp\relax%
      D An idea from a trusted colleague
    \baselineskip=20.87bp\par}
    \vskip\dimexpr6.23bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f13 source B** (vocabulary V1)


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


### Pair f14

The two sources are the same slide written two ways.


**f14 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s3C4043}{HTML}{3C4043}
\definecolor{b2s999999}{HTML}{999999}
\definecolor{b2sB7B7B7}{HTML}{B7B7B7}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{453.5bp}(-0.0bp,0.0bp)
    \includegraphics[width=453.5bp,height=255.1bp]{figures/image-8439d22e.png}
  \end{textblock*}
  \begin{textblock*}{34.13bp}(42.2bp,28.9bp)
    \vbox to 18.0bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{14.17}\color{black}\vrule width0bp height13.72bp depth0bp\relax%
      Icons
    \baselineskip=17.00bp\par}
    \vskip\dimexpr3.29bp-\prevdepth\relax
    \vskip0.00bp}
  \end{textblock*}
  \begin{textblock*}{31.00bp}(40.6bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Alarm
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{30.99bp}(82.4bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Assessment
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{30.99bp}(124.6bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Sync
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{30.99bp}(169.3bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Exit App
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{30.99bp}(210.4bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Movie
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{30.99bp}(253.0bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Visibility
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{30.99bp}(295.2bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Trolley
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{30.99bp}(339.9bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{2.13}\color{b2s999999}\vrule width0bp height2.06bp depth0bp\relax%
      Open
    \baselineskip=2.56bp\par}
    \vskip\dimexpr0.49bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{31.00bp}(382.0bp,79.4bp)
    \vbox to 9.0bp{\slidesbox
    \vskip1.53bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
    [... 620 further lines of the same forms as above ...]
    \vskip0.00bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.61}\color{b2s3C4043}\vrule width0bp height6.40bp depth0bp\relax%
      All icons are vector objects and can \unskip\break be recolored using the fill menu.
    \baselineskip=7.93bp\par}
    \vskip\dimexpr1.53bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{70.5bp}(340.2bp,22.6bp)
    \includegraphics[width=70.5bp,height=26.5bp]{figures/image-9302583d.png}
  \end{textblock*}
\end{frame}
```


**f14 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Blue}{HTML}{0005DF}
\definecolor{DarkGrey}{HTML}{3C4043}
\definecolor{Grey4}{HTML}{999999}
\definecolor{LightGrey}{HTML}{B7B7B7}
\setslideinset{1.53}
\slidestyle{tiny-black}{size=3.78, color=black, ascent=3.66, pitch=4.54, depth=0.88}
\slidestyle{body}{size=6.61, color=DarkGrey, ascent=6.4, pitch=7.93, depth=1.53}
\slidestyle{title-mono-black}{size=13.7, family=mono, color=black, ascent=13.26, pitch=23.02, depth=3.18}
\slidestyle{label-mono-blue}{size=13.7, family=mono, color=Blue}
\slidestyle{title-black-14.2}{size=14.17, color=black, ascent=13.72, pitch=17, depth=3.29}
\slidestyle{tiny-grey}{size=2.13, color=Grey4, ascent=2.06, pitch=2.56, depth=0.49}
\setslidepar{style=tiny-black}
\setslidelist{enumerate}{1}{style=title-mono-black,indent=20.18,labelstyle=label-mono-blue,label={\arabic*.},gap=13.78}
```


The frame:

```latex
\begin{frame}[plain,layout=dot-grid-30bp]
  \slidetext[bottom,inset=0]{42.2,28.9,34.13,18}{title-black-14.2}{Icons}
  \slidetext[center]{40.6,79.4,31,9}{tiny-grey}{Alarm}
  \slidetext[center]{82.4,79.4,30.99,9}{tiny-grey}{Assessment}
  \slidetext[center]{124.6,79.4,30.99,9}{tiny-grey}{Sync}
  \slidetext[center]{169.3,79.4,30.99,9}{tiny-grey}{Exit App}
  \slidetext[center]{210.4,79.4,30.99,9}{tiny-grey}{Movie}
  \slidetext[center]{253,79.4,30.99,9}{tiny-grey}{Visibility}
  \slidetext[center]{295.2,79.4,30.99,9}{tiny-grey}{Trolley}
  \slidetext[center]{339.9,79.4,30.99,9}{tiny-grey}{Open}
  \slidetext[center]{382,79.4,31,9}{tiny-grey}{Location}
  \slidetext[center]{40.6,112.1,31,9}{tiny-grey}{Settings}
  \slidetext[center]{82.4,112.1,30.99,9}{tiny-grey}{Assignment}
  \slidetext[center]{124.6,112.1,30.99,9}{tiny-grey}{Check}
  \slidetext[center]{169.3,112.1,30.99,9}{tiny-grey}{Explore}
  \slidetext[center]{206.6,112.1,38.07,9}{tiny-grey}{Thumb Down}
  \slidetext[center]{253,112.1,30.99,9}{tiny-grey}{Today}
  \slidetext[center]{295.2,112.1,30.99,9}{tiny-grey}{Perm Media}
  \slidetext[center]{339.9,112.1,30.99,9}{tiny-grey}{People}
  \slidetext[center]{382,112.1,31,9}{tiny-grey}{search}
  \slidetext[center]{40.6,138.2,31,9.1}{tiny-grey}{Airplane}
  \slidetext[center]{82.4,138.2,30.99,9.1}{tiny-grey}{Signal}
  \slidetext[center]{124.6,138.2,30.99,9.1}{tiny-grey}{Photo}
  \slidetext[center]{169.3,138.2,30.99,9.1}{tiny-grey}{Play 1}
  \slidetext[center]{210.4,138.2,30.99,9.1}{tiny-grey}{Block}
  \slidetext[center]{247.7,138.2,40.89,9.1}{tiny-grey}{Send}
  \slidetext[center]{295.2,138.2,30.99,9.1}{tiny-grey}{Smartphone}
  \slidetext[center]{339.9,138.2,30.99,9.1}{tiny-grey}{Style}
  \slidetext[center]{382,138.2,31,9.1}{tiny-grey}{Walk}
  \slidetext[center]{40.6,168.8,31,9.1}{tiny-grey}{Bluetooth}
  \slidetext[center]{82.4,168.8,30.99,9.1}{tiny-grey}{WiFi}
  \slidetext[center]{124.6,168.8,30.99,9.1}{tiny-grey}{Upload}
  \slidetext[center]{169.3,168.8,30.99,9.1}{tiny-grey}{Play 2}
  \slidetext[center]{210.4,168.8,30.99,9.1}{tiny-grey}{Email}
  \slidetext[center]{253,168.8,30.99,9.1}{tiny-grey}{Laptop}
  \slidetext[center]{295.2,168.8,30.99,9.1}{tiny-grey}{iPhone}
  \slidetext[center]{339.9,168.8,30.99,9.1}{tiny-grey}{Controls}
  \slidetext[center]{382,168.8,31,9.1}{tiny-grey}{Bike}
  \slidetext[center]{40.6,201.9,31,9}{tiny-grey}{Pie Chart}
  \slidetext[center]{79.2,201.9,37.73,9}{tiny-grey}{Money}
  \slidetext[center]{124.6,201.9,30.99,9}{tiny-grey}{Attachment}
  \slidetext[center]{169.3,201.9,30.99,9}{tiny-grey}{Video}
  \slidetext[center]{210.4,201.9,30.99,9}{tiny-grey}{Business}
  \slidetext[center]{253,201.9,30.99,9}{tiny-grey}{Chromebook}
  \slidetext[center]{295.2,201.9,30.99,9}{tiny-grey}{Security}
  \slidetext[center]{339.9,201.9,30.99,9}{tiny-grey}{Notification}
  \slidetext[center]{382,201.9,31,9}{tiny-grey}{Bus}
  \slidefreeform[fill=LightGrey]{132.6,94.2,15.4,15.4}{shapes/freeform-9524d76f.tex}
  \slidefreeform[fill=LightGrey]{176.9,94.2,15.4,15.4}{shapes/freeform-7f75285e.tex}
  \slidefreeform[fill=LightGrey]{217.5,94.2,17.04,15.47}{shapes/freeform-d2ee32ea.tex}
  \slidefreeform[fill=LightGrey]{262.4,94.2,13.87,15.4}{shapes/freeform-5dbff63b.tex}
  \slidefreeform[fill=LightGrey]{302.6,94.2,18.49,15.47}{shapes/freeform-3ed64619.tex}
  \slidefreeform[fill=LightGrey]{48.1,94.2,14.89,15.4}{shapes/freeform-f372f2dc.tex}
  \slidefreeform[fill=LightGrey]{91,94.2,13.84,15.4}{shapes/freeform-f27e6e2f.tex}
  \slidefreeform[fill=LightGrey]{346.4,98.8,15.47,10.85}{shapes/freeform-931c9c8f.tex}
  \slidefreeform[fill=LightGrey]{390.5,96.1,13.52,13.45}{shapes/freeform-a3f8dff9.tex}
  \slidefreeform[fill=LightGrey]{49.9,123.2,13.26,14.03}{shapes/freeform-bcedb4a0.tex}
  \slidefreeform[fill=LightGrey]{51.9,153.3,8.93,14.08}{shapes/freeform-74695b2d.tex}
  \slidefreeform[fill=LightGrey]{49.1,181.3,14.03,14.03}{shapes/freeform-51731cb5.tex}
  \slidefreeform[fill=LightGrey]{89.8,154.2,16.3,12.94}{shapes/freeform-8df9f5e7.tex}
  \slidefreeform[fill=LightGrey]{93.5,181.6,8.41,14.8}{shapes/freeform-f9c67f78.tex}
  \slidefreeform[fill=LightGrey]{136.3,180.4,7.81,15.58}{shapes/freeform-2ab7b0ff.tex}
  \slidefreeform[fill=LightGrey]{133.4,121.8,14.88,14.88}{shapes/freeform-6e4b3abc.tex}
  \slidefreeform[fill=LightGrey]{134,153.3,11.63,13.25}{shapes/freeform-fb37d3ae.tex}
  \slidefreeform[fill=LightGrey]{176,151,15.98,15.98}{shapes/freeform-5b81136b.tex}
  \slidefreeform[fill=LightGrey]{176.8,184.6,14.34,9.56}{shapes/freeform-3f79508c.tex}
  \slidefreeform[fill=LightGrey]{217.6,181.1,16.04,14.49}{shapes/freeform-1d502a33.tex}
  \slidefreeform[fill=LightGrey]{217.6,121.1,16.04,16.09}{shapes/freeform-75b98a1a.tex}
  \slidefreeform[fill=LightGrey]{217.6,153.2,16.04,12.81}{shapes/freeform-92fbad4b.tex}
  \slidefreeform[fill=LightGrey]{257.6,152.2,19.99,13.32}{shapes/freeform-74952eff.tex}
  \slidefreeform[fill=LightGrey]{257.6,181.2,19.99,14.16}{shapes/freeform-8473bd2e.tex}
  \slidefreeform[fill=LightGrey]{306.3,119.5,10.81,17.01}{shapes/freeform-8103ffe1.tex}
  \slidefreeform[fill=LightGrey]{306.6,149.8,10.08,17.01}{shapes/freeform-0632097d.tex}
  \slidefreeform[fill=LightGrey]{304.7,180.6,12.81,15.65}{shapes/freeform-6bab7687.tex}
  \slidefreeform[fill=LightGrey]{348,121.2,16.01,14.61}{shapes/freeform-b4bb6222.tex}
  \slidefreeform[fill=LightGrey]{349.1,152.7,13.77,13.77}{shapes/freeform-5043e570.tex}
  \slidefreeform[fill=LightGrey]{349.4,180.2,13.19,15.58}{shapes/freeform-ada047e1.tex}
  \slidefreeform[fill=LightGrey]{387.8,150.9,19.65,17.07}{shapes/freeform-7dbafe51.tex}
  \slidefreeform[fill=LightGrey]{390.6,180.5,13.19,15.51}{shapes/freeform-b14ce0f6.tex}
  \slidefreeform[fill=LightGrey]{393.6,120.7,10.08,16.88}{shapes/freeform-b9323537.tex}
  \slidefreeform[fill=LightGrey]{131.8,65.8,17.04,12.34}{shapes/freeform-909bcce2.tex}
  \slidefreeform[fill=LightGrey]{177.5,64.7,13.84,13.84}{shapes/freeform-bd45cb44.tex}
  \slidefreeform[fill=LightGrey]{219.9,65.1,12.34,13.84}{shapes/freeform-62145e00.tex}
  \slidefreeform[fill=LightGrey]{303.4,64.2,15.34,15.47}{shapes/freeform-3b72f7b6.tex}
  \slidefreeform[fill=LightGrey]{48.2,64.1,15.4,15.54}{shapes/freeform-cded2ed4.tex}
  \slidefreeform[fill=LightGrey]{91,63.8,13.84,15.4}{shapes/freeform-933f57de.tex}
  \slidefreeform[fill=LightGrey]{347.3,67,13.84,13.91}{shapes/freeform-b5e621be.tex}
  \slidefreeform[fill=LightGrey]{392.1,65,10.79,15.47}{shapes/freeform-8d9a3d3f.tex}
  \slidefreeform[fill=LightGrey]{259.8,65.8,18.49,12.58}{shapes/freeform-de57cd5f.tex}
  \slidetext[inset=0]{226.4,27.4,222.35,17}{body}{All icons are vector objects and can \slidebreak be recolored using the fill menu.}
  \slidepicture{340.2,22.6,70.5,26.5}{figures/image-9302583d.png}
\end{frame}
```


### Pair f15

The two sources are the same slide written two ways.


**f15 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s34A853}{HTML}{34A853}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{240.2bp}(202.1bp,18.8bp)
    \includegraphics[trim=482.22 50.93 481.14 50.93,clip,width=240.2bp,height=232.6bp]{figures/picture-170dada5.jpg}
  \end{textblock*}
  \begin{textblock*}{453.5bp}(0.0bp,0.0bp)
    \includegraphics[width=453.5bp,height=255.1bp]{figures/picture-6bb9dee1.png}
  \end{textblock*}
  \begin{textblock*}{83.7bp}(118.5bp,182.6bp)
    \includegraphics[trim=0.00 2.08 0.00 2.08,clip,width=83.7bp,height=52.1bp]{figures/picture-38208a9f.png}
  \end{textblock*}
  \begin{textblock*}{136.7bp}(31.1bp,21.6bp)
    \includegraphics[width=136.7bp,height=17.7bp]{figures/picture-5bdc791d.png}
  \end{textblock*}
  \begin{textblock*}{167.84bp}(30.2bp,78.9bp)
    \vbox to 68.7bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{24.57}\adoptfontA\color{black}\vrule width0bp height23.78bp depth0bp\relax%
      Chapter \textbf{Title\ }Goes Here.
    \baselineskip=29.29bp\par}
    \vskip\dimexpr5.70bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{119.48bp}(65.5bp,31.6bp)
    \vbox to 8.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.61}\adoptfontA\fontseries{w500}\selectfont \color{b2s34A853}\vrule width0bp height6.40bp depth0bp\relax%
      Editable Location
    \baselineskip=7.93bp\par}
    \vskip\dimexpr1.53bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f15 source B** (vocabulary V1)


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


Answer with the JSON list described above, one object per pair, nothing else.
