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
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
% the deck's arrow heads, sized in line widths: > (FILL_ARROW), and SlideStealth, SlideOpen, ...
% the fourth argument is boxed at its own width and set turned about the centre, where it would be if
% the element were upright - the way the deck turns a text box with everything in it.
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
```

### Vocabulary V2

```latex
\renewcommand{\familydefault}{\sfdefault}
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
\newif\ifslidesspace
\AddToHook{selectfont}{\ifslidesspace\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax\fi}
\newcommand{\slidesize}[1]{\fontsize{#1bp}{#1bp}\selectfont\spaceskip=\fontdimen2\font plus\fontdimen3\font\relax}
\newcommand{\slidesbox}{\slidesspacetrue\parindent=0pt\parskip=0pt\lineskip=0pt\lineskiplimit=-\maxdimen\hyphenpenalty=10000\exhyphenpenalty=50\tolerance=9999\emergencystretch=0pt\frenchspacing\hbadness=10000\hfuzz=\maxdimen\vbadness=10000\vfuzz=\maxdimen}
\newsavebox\adopt@box
% the fourth argument is boxed at its own width and set turned about the centre, where it would be if
% the element were upright - the way the deck turns a text box with everything in it.
\newcommand\adoptturned[4]{\begingroup
  \def\adopt@angle{#1}\def\adopt@cx{#2}\def\adopt@cy{#3}%
\newdimen\slidesx
\newcount\slidestabn
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
```

### Vocabulary V3

```latex
Plain beamer and LaTeX: the `frame`, `itemize`, `tabular`, `tikzpicture` and
`block` environments, `\includegraphics`, `\textbf`, `\alert`, and so on.
No macros of its own.
```


## The pairs


### Pair f31

The two sources are the same slide written two ways.


**f31 source A** (vocabulary V1)


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


**f31 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s93C47D}{HTML}{93C47D}
\definecolor{b2s9FC5E8}{HTML}{9FC5E8}
\definecolor{b2sD5A6BD}{HTML}{D5A6BD}
\definecolor{b2sE69138}{HTML}{E69138}
\definecolor{b2sF1C232}{HTML}{F1C232}
```


The frame:

```latex
}
{\setbeamertemplate{background canvas}{\includegraphics[width=\paperwidth,height=\paperheight]{figures/background-0322b612.png}}
\begin{frame}[plain]
  \begin{textblock*}{340.25bp}(12.0bp,10.4bp)
    \vbox to 39.7bp{\slidesbox
    \vskip1.31bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.72}\rmfamily\color{b2sE69138}\vrule width0bp height15.22bp depth0bp\relax%
      Key Statistics, Ratios, Trading Multiples Continued....
    \baselineskip=18.71bp\par}
    \vskip\dimexpr3.65bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{315.4bp}(39.9bp,29.0bp)
    \adoptrow{0}{13.67bp}
    \adoptrow{1}{9.76bp}
    \adoptrow{2}{9.76bp}
    \adoptrow{3}{9.76bp}
    \adoptrow{4}{10.42bp}
    \adoptrow{5}{11.07bp}
    \adoptrow{6}{9.76bp}
    \adoptrow{7}{10.42bp}
    \adoptrow{8}{11.07bp}
    \adoptrow{9}{10.42bp}
    \adoptrow{10}{9.76bp}
    \adoptfix{0}
    \adoptfix{1}
    \adoptfix{2}
    \adoptfix{3}
    \adoptfix{4}
    \adoptfix{5}
    \adoptfix{6}
    \adoptfix{7}
    \adoptfix{8}
    \adoptfix{9}
    \adoptfix{10}
    \adoptcell{1}{0}{0}{76.24bp}{1.63bp}{78.85bp}{-1.30bp}{%
      \raggedright\fontsize{8.5bp}{10.2bp}\selectfont\bfseries\color{white}\baselineskip=10.20bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{\underline{\emph{Moodys}}}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{2}{0}{0}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{8.5bp}{10.2bp}\selectfont\bfseries\color{white}\baselineskip=10.20bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{\underline{\emph{S\&P}}}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{3}{0}{0}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{8.5bp}{10.2bp}\selectfont\bfseries\color{white}\baselineskip=10.20bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{\underline{\emph{Fitch}}}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{4}{0}{0}{75.62bp}{1.63bp}{78.23bp}{-1.30bp}{%
      \raggedright\fontsize{8.5bp}{10.2bp}\selectfont\bfseries\color{white}\baselineskip=10.20bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{\underline{\emph{Definition}}}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{5}{1}{1}{76.24bp}{1.63bp}{78.85bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s93C47D}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Aaa}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{6}{1}{1}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s93C47D}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AAA}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{7}{1}{1}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s93C47D}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AAA}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{8}{1}{1}{75.62bp}{1.63bp}{78.23bp}{0.00bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s93C47D}\baselineskip=6.48bp\relax%
      Highest Quality}
    \adoptcell{9}{2}{2}{76.24bp}{1.63bp}{78.85bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Aa1}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{10}{2}{2}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AA+}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{11}{2}{2}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AA+}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{12}{3}{3}{76.24bp}{1.63bp}{78.85bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Aa2}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{13}{3}{3}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AA}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{14}{3}{3}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AA}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{15}{3}{3}{75.62bp}{1.63bp}{78.23bp}{0.00bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      Very High Quality}
    \adoptcell{16}{4}{4}{76.24bp}{1.63bp}{78.85bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Aa3}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{17}{4}{4}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AA-}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{18}{4}{4}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2s9FC5E8}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{AA-}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{19}{5}{5}{76.24bp}{1.63bp}{78.85bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2sD5A6BD}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{A1}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{20}{5}{5}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2sD5A6BD}\baselineskip=6.48bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{A+}\ifdim\wd0>\linewidth\kern-1.30bp\hbox to\dimexpr\linewidth+2.61bp{\hss\box0\hss}\kern-1.30bp\else\hss\box0\hss\fi}}
    \adoptcell{21}{5}{5}{76.55bp}{1.63bp}{79.16bp}{-1.30bp}{%
      \raggedright\fontsize{5.4bp}{6.5bp}\selectfont\color{b2sD5A6BD}\baselineskip=6.48bp\relax%
    [... 309 further lines of the same forms as above ...]
      \node[anchor=north west] at (80.2bp,{-\adopty{9}-6.20bp+\adoptht{32}}) {\adoptbox{32}};
      \node[anchor=north west] at (159.2bp,{-\adopty{9}-6.20bp+\adoptht{33}}) {\adoptbox{33}};
      \node[anchor=north west] at (1.2bp,{-\adopty{10}-6.20bp+\adoptht{34}}) {\adoptbox{34}};
      \node[anchor=north west] at (80.2bp,{-\adopty{10}-6.20bp+\adoptht{35}}) {\adoptbox{35}};
      \node[anchor=north west] at (159.2bp,{-\adopty{10}-6.20bp+\adoptht{36}}) {\adoptbox{36}};
      \node[anchor=north west] at (238.2bp,{-\adopty{10}-6.20bp+\adoptht{37}}) {\adoptbox{37}};
      \node[anchor=north west] at (1.2bp,{-\adopty{11}-6.20bp+\adoptht{38}}) {\adoptbox{38}};
      \node[anchor=north west] at (80.2bp,{-\adopty{11}-6.20bp+\adoptht{39}}) {\adoptbox{39}};
      \node[anchor=north west] at (159.2bp,{-\adopty{11}-6.20bp+\adoptht{40}}) {\adoptbox{40}};
    \end{tikzpicture}
  \end{textblock*}
\end{frame}
```


### Pair f32

The two sources are the same slide written two ways.


**f32 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s185DA2}{HTML}{185DA2}
\definecolor{b2s2388DB}{HTML}{2388DB}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{362.8bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (362.83bp,-186.15bp);
      \path[fill=b2s2388DB,shift={(0bp,0bp)}] (0bp,0bp) -- (362.83bp,0bp) -- (362.83bp,-186.15bp) -- (0bp,-186.15bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{362.8bp}(0.0bp,185.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (362.83bp,-0.01bp);
      \path[draw=black,line width=2.27bp,draw opacity=0.149] (0bp,0bp) -- (362.83bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{312.78bp}(19.5bp,74.1bp)
    \vbox to 112.0bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{30.24}\bfseries \color{white}\vrule width0bp height29.27bp depth0bp\relax%
      Creating Comic Strips with Google Presentations
    \baselineskip=36.28bp\par}
    \vskip\dimexpr7.02bp-\prevdepth\relax
    \vskip3.27bp}
  \end{textblock*}
  \begin{textblock*}{327.24bp}(18.2bp,191.9bp)
    \vbox to 58.7bp{\slidesbox
    \vskip3.27bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s2388DB}\vrule width0bp height14.64bp depth0bp\relax%
      Eric Curts - North Canton City Schools
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s2388DB}\vrule width0bp height14.64bp depth0bp\relax%
      ericcurts.com - ericcurts@gmail.com
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s185DA2}\vrule width0bp height14.64bp depth0bp\relax%
      \href{http://twitter.com/ericcurts}{\uline{@ericcurts}} \textcolor{b2s2388DB}{-} \href{http://gplus.to/ericcurts}{\uline{gplus.to/ericcurts}}
    \baselineskip=18.14bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{60.1bp}(294.3bp,95.1bp)
    \includegraphics[width=60.1bp,height=70.0bp]{figures/picture-82c640c4.png}
  \end{textblock*}
  \begin{textblock*}{333.6bp}(19.5bp,6.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (333.56bp,-61.79bp);
      \path[fill=white,draw=black,line width=0.38bp,shift={(0bp,0bp)}] (0bp,0bp) -- (333.56bp,0bp) -- (333.56bp,-61.79bp) -- (0bp,-61.79bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{326.79bp}(22.9bp,6.0bp)
    \vbox to 61.8bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{18.14}\color{black}\vrule width0bp height17.56bp depth0bp\relax%
      Please go to \href{http://tinyurl.com/curts19}{\textcolor{b2s185DA2}{\uline{tinyurl.com/curts19}}} to find other helpful resources for this session.
    \baselineskip=21.92bp\par}
    \vskip\dimexpr4.21bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f32 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Blue}{HTML}{185DA2}
\definecolor{Blue2}{HTML}{2388DB}
\setslideinset{3.27}
\slidestyle{body-blue}{size=15.12, color=Blue2, ascent=14.64, pitch=18.14, depth=3.51}
\slidestyle{body-blue-15.1}{size=15.12, color=Blue, ascent=14.64, pitch=18.14, depth=3.51}
\slidestyle{large}{size=18.14, color=black, ascent=17.56, pitch=21.92, depth=4.21}
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
\begin{frame}[plain,layout=title-slide]
  \frametitle{Creating Comic Strips with Google Presentations}
  \begin{slidebox}[style=body-blue]{18.2,191.9,327.24,58.7}
    \slidepar{Eric Curts - North Canton City Schools}
    \slidepar{ericcurts.com - ericcurts@gmail.com}
    \slidepar[style=body-blue-15.1]{\href{http://twitter.com/ericcurts}{\uline{@ericcurts}} \textcolor{Blue2}{-} \href{http://gplus.to/ericcurts}{\uline{gplus.to/ericcurts}}}
  \end{slidebox}
  \slidepicture{294.3,95.1,60.1,70}{figures/picture-82c640c4.png}
  \sliderect[fill=white,draw=black,line width=0.38bp]{19.5,6,333.56,61.79}
  \slidetext[middle]{22.9,6,326.79,61.8}{large}{Please go to \href{http://tinyurl.com/curts19}{\textcolor{Blue}{\uline{tinyurl.com/curts19}}} to find other helpful resources for this session.}
\end{frame}
```


### Pair f33

The two sources are the same slide written two ways.


**f33 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s080302}{HTML}{080302}
\definecolor{b2s595959}{HTML}{595959}
\definecolor{b2s742E2A}{HTML}{742E2A}
\definecolor{b2sE25952}{HTML}{E25952}
```


The frame:

```latex
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
  \begin{textblock*}{414.09bp}(9.3bp,13.4bp)
    \vbox to 28.4bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.87}\color{black}\vrule width0bp height15.36bp depth0bp\relax%
      TLS: Efficiency
    \baselineskip=18.90bp\par}
    \vskip\dimexpr3.68bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(14.1bp,61.8bp)
    \vbox to 186.8bp{\slidesbox
    \vskip4.08bp
    {\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\color{black}\vrule width0bp height10.98bp depth0bp\relax\llap{\tikz[baseline=-0.68bp]\path[fill=black] (2.34bp,2.34bp) circle[radius=2.34bp];\hskip12.25bp}%
      Public-key cryptography: Minor costs
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-1.04bp\relax{\leftskip=45.35bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax\llap{\tikz[baseline=-0.62bp]\path[draw=black,line width=0.53bp] (1.90bp,1.90bp) circle[radius=1.63bp];\hskip12.04bp}%
      Client and server must perform Diffie-Hellman key exchange
    \baselineskip=12.17bp\par}
    \prevdepth=\dimexpr\prevdepth+1.04bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\color{black}\vrule width0bp height10.98bp depth0bp\relax\llap{\tikz[baseline=-0.68bp]\path[fill=black] (2.34bp,2.34bp) circle[radius=2.34bp];\hskip12.25bp}%
      Symmetric-key cryptography: Effectively free
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-1.04bp\relax{\leftskip=45.35bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax\llap{\tikz[baseline=-0.62bp]\path[draw=black,line width=0.53bp] (1.90bp,1.90bp) circle[radius=1.63bp];\hskip12.04bp}%
      Modern hardware has dedicated support for symmetric-key cryptography
    \baselineskip=12.17bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=45.35bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax\llap{\tikz[baseline=-0.62bp]\path[draw=black,line width=0.53bp] (1.90bp,1.90bp) circle[radius=1.63bp];\hskip12.04bp}%
      Performance impact is negligible
    \baselineskip=12.17bp\par}
    \prevdepth=\dimexpr\prevdepth+1.04bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\color{black}\vrule width0bp height10.98bp depth0bp\relax\llap{\tikz[baseline=-0.68bp]\path[fill=black] (2.34bp,2.34bp) circle[radius=2.34bp];\hskip12.25bp}%
      Latency: Extra waiting time before the first message
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-1.04bp\relax{\leftskip=45.35bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax\llap{\tikz[baseline=-0.62bp]\path[draw=black,line width=0.53bp] (1.90bp,1.90bp) circle[radius=1.63bp];\hskip12.04bp}%
      Must perform the entire TLS handshake before sending the first message
    \baselineskip=12.17bp\par}
    \vskip\dimexpr3.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{18.79bp}(424.5bp,231.3bp)
    \vbox to 19.5bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\color{b2s595959}\vrule width0bp height6.10bp depth0bp\relax%
      21
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \note{Handshake has overhead but it’s pretty minor

Every message now has to be encrypted instead of just sending plaintext over TCP

Symmetric-key is so fast: it’s just bit flips!

TLS is such a common protocol, AES, etc. everyone’s using it -> modern hardware is dedicated for that!

Technically slower than TCP, but you’re probably never going to notice it

Latency: in vanilla TCP, need the 3-way handshake (syn, syn-ack, ack), but now in TLS, need to set up entire TCP handshake + everything else}
\end{frame}
```


**f33 source B** (vocabulary V1)


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


### Pair f34

The two sources are the same slide written two ways.


**f34 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{LightGreen}{HTML}{59FF82}
\definecolor{Red}{HTML}{EE6C4D}
\definecolor{LightOrange}{HTML}{FCB295}
\slidestyle{body-11.3}{size=11.34, color=white, ascent=10.98, pitch=13.61, depth=2.63}
\slidestyle{label}{size=11.34, color=white}
\slidestyle{title-changa-lightgreen-31.5}{size=31.5, face=\adoptfontA, color=LightGreen, ascent=27.66, pitch=34.02, depth=6.36}
\slidestyle{small-bold-black}{size=9.45, weight=bold, color=black, ascent=9.15, pitch=11.34, depth=2.19}
\slidestyle{small-black}{size=9.45, color=black, ascent=9.15, pitch=11.34, depth=2.19}
\slidestyle{tiny-7.6}{size=7.56, color=white, ascent=7.32, pitch=11.79, depth=1.75}
\setslidepar{style=tiny-7.6}
\setslidelist{itemize}{1}{style=body-11.3,indent=19.28,labelstyle=label,label={•},gap=4.57}
```


The frame:

```latex
\begin{frame}[plain]
  \slidepicture{1.1,0,452.4,255.1}{figures/picture-fill-dc0f3714.png}
  \slidepicture{1.1,0,452.4,255.1}{figures/picture-fill-a260d2b6.png}
  \sliderect[fill=LightOrange]{25.1,111.3,147.39,78.4}
  \begin{slidetable}[inset x=1.827, inset y=1.134, border={white,line width=0.71bp}, h=22.8, fixed, aligns={right,center,center,center,center}, valign=middle, style={\footnotesize\color{white}}, pitch=14.79]{211.7,164}{37.687,47.639,47.64,47.64,47.64}
    \cell[style={\footnotesize\bfseries\color{LightOrange}}]{Name} & Alex & Jude & Arlo & Casey \\
    \cell[style={\footnotesize\bfseries\color{LightOrange}}]{Age} & 9 & 12 & 11 & 11 \\
  \end{slidetable}
  \slidepicture{379.8,105.2,38.8,43.4}{figures/picture-fill-8337bae5.png}
  \slidepicture{328.2,108.6,36.9,41.4}{figures/picture-fill-cbfb7de1.png}
  \slidepicture{274.1,108,39.1,42.2}{figures/picture-fill-3c86c260.png}
  \slidepicture{228.8,110,30.3,38.6}{figures/picture-fill-7b606e52.png}
  \slidetext{25.5,32.6,237.29,59.1}{title-changa-lightgreen-31.5}{WHAT ARE RELATIONS?}
  \slidepicture{201.8,13.6,17.9,19.3}{figures/picture-fill-51d60eb9.png}
  \slidepicture{428.3,66.9,11.3,13}{figures/picture-fill-acd5d7c7.png}
  \slidepicture{14.7,198.1,21.5,21.3}{figures/picture-fill-88b02842.png}
  \slidefreeform[fill=Red]{313.4,-18.2,153.25,52.87}{shapes/freeform-9d97167c.tex}
  \begin{slidebox}{42.5,127.8,113.16,45.8}
    \slidepar[style=small-bold-black]{Example:}
    \slidepar[style=small-black]{Names and their age are a set of ordered pairs that we could put into a table.}
  \end{slidebox}
  \slidetext{226.8,51,188.12,27.5}{body-11.3}{A relation is a set of \textcolor{LightOrange}{\textbf{ordered pairs}} that represent a relationship.}
  \slidepicture{-20.5,168.5,195.2,97.4}{figures/picture-c6b63208.png}
\end{frame}
```


**f34 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s59FF82}{HTML}{59FF82}
\definecolor{b2sEE6C4D}{HTML}{EE6C4D}
\definecolor{b2sFCB295}{HTML}{FCB295}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{452.4bp}(1.1bp,0.0bp)
    \includegraphics[width=452.4bp,height=255.1bp]{figures/picture-fill-dc0f3714.png}
  \end{textblock*}
  \begin{textblock*}{452.4bp}(1.1bp,0.0bp)
    \includegraphics[width=452.4bp,height=255.1bp]{figures/picture-fill-a260d2b6.png}
  \end{textblock*}
  \begin{textblock*}{147.4bp}(25.1bp,111.3bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (147.39bp,-78.40bp);
      \path[fill=b2sFCB295,shift={(0bp,0bp)}] (0bp,0bp) -- (147.39bp,0bp) -- (147.39bp,-78.4bp) -- (0bp,-78.4bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{228.2bp}(211.7bp,164.0bp)
    \adoptrow{0}{22.80bp}
    \adoptrow{1}{22.80bp}
    \adoptfix{0}
    \adoptfix{1}
    \adoptcell{1}{0}{0}{34.03bp}{1.13bp}{37.69bp}{-3.65bp}{%
      \raggedright\footnotesize\bfseries\color{b2sFCB295}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Name}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0}\kern-1.83bp\else\hss\box0\fi}}
    \adoptcell{2}{0}{0}{43.98bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Alex}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adoptcell{3}{0}{0}{43.99bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Jude}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adoptcell{4}{0}{0}{43.99bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Arlo}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adoptcell{5}{0}{0}{43.99bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Casey}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adoptcell{6}{1}{1}{34.03bp}{1.13bp}{37.69bp}{-3.65bp}{%
      \raggedright\footnotesize\bfseries\color{b2sFCB295}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Age}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0}\kern-1.83bp\else\hss\box0\fi}}
    \adoptcell{7}{1}{1}{43.98bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{9}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adoptcell{8}{1}{1}{43.99bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{12}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adoptcell{9}{1}{1}{43.99bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{11}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adoptcell{10}{1}{1}{43.99bp}{1.13bp}{47.64bp}{-1.83bp}{%
      \raggedright\footnotesize\color{white}\baselineskip=14.79bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{11}\ifdim\wd0>\linewidth\kern-1.83bp\hbox to\dimexpr\linewidth+3.65bp{\hss\box0\hss}\kern-1.83bp\else\hss\box0\hss\fi}}
    \adopttops{2}
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \path[use as bounding box] (0bp,0bp) rectangle (228.2bp,{-\adopty{2}});
      \draw[white,line width=0.71bp,line cap=rect] (0.0bp,{-\adopty{0}}) -- (228.2bp,{-\adopty{0}});
      \draw[white,line width=0.71bp,line cap=rect] (0.0bp,{-\adopty{1}}) -- (228.2bp,{-\adopty{1}});
      \draw[white,line width=0.71bp,line cap=rect] (0.0bp,{-\adopty{2}}) -- (228.2bp,{-\adopty{2}});
      \draw[white,line width=0.71bp,line cap=rect] (0.0bp,{-\adopty{0}}) -- (0.0bp,{-\adopty{2}});
      \draw[white,line width=0.71bp,line cap=rect] (37.7bp,{-\adopty{0}}) -- (37.7bp,{-\adopty{2}});
      \draw[white,line width=0.71bp,line cap=rect] (85.3bp,{-\adopty{0}}) -- (85.3bp,{-\adopty{2}});
      \draw[white,line width=0.71bp,line cap=rect] (133.0bp,{-\adopty{0}}) -- (133.0bp,{-\adopty{2}});
      \draw[white,line width=0.71bp,line cap=rect] (180.6bp,{-\adopty{0}}) -- (180.6bp,{-\adopty{2}});
      \draw[white,line width=0.71bp,line cap=rect] (228.2bp,{-\adopty{0}}) -- (228.2bp,{-\adopty{2}});
      \node[anchor=west,yshift=-\adoptdrop{1}] at (1.8bp,{-(\adopty{0}+\adopty{1})/2}) {\adoptbox{1}};
      \node[anchor=west,yshift=-\adoptdrop{2}] at (39.5bp,{-(\adopty{0}+\adopty{1})/2}) {\adoptbox{2}};
      \node[anchor=west,yshift=-\adoptdrop{3}] at (87.2bp,{-(\adopty{0}+\adopty{1})/2}) {\adoptbox{3}};
      \node[anchor=west,yshift=-\adoptdrop{4}] at (134.8bp,{-(\adopty{0}+\adopty{1})/2}) {\adoptbox{4}};
      \node[anchor=west,yshift=-\adoptdrop{5}] at (182.4bp,{-(\adopty{0}+\adopty{1})/2}) {\adoptbox{5}};
      \node[anchor=west,yshift=-\adoptdrop{6}] at (1.8bp,{-(\adopty{1}+\adopty{2})/2}) {\adoptbox{6}};
      \node[anchor=west,yshift=-\adoptdrop{7}] at (39.5bp,{-(\adopty{1}+\adopty{2})/2}) {\adoptbox{7}};
      \node[anchor=west,yshift=-\adoptdrop{8}] at (87.2bp,{-(\adopty{1}+\adopty{2})/2}) {\adoptbox{8}};
      \node[anchor=west,yshift=-\adoptdrop{9}] at (134.8bp,{-(\adopty{1}+\adopty{2})/2}) {\adoptbox{9}};
      \node[anchor=west,yshift=-\adoptdrop{10}] at (182.4bp,{-(\adopty{1}+\adopty{2})/2}) {\adoptbox{10}};
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{38.8bp}(379.8bp,105.2bp)
    \includegraphics[width=38.8bp,height=43.4bp]{figures/picture-fill-8337bae5.png}
  \end{textblock*}
  \begin{textblock*}{36.9bp}(328.2bp,108.6bp)
    \includegraphics[width=36.9bp,height=41.4bp]{figures/picture-fill-cbfb7de1.png}
  \end{textblock*}
  \begin{textblock*}{39.1bp}(274.1bp,108.0bp)
    \includegraphics[width=39.1bp,height=42.2bp]{figures/picture-fill-3c86c260.png}
  \end{textblock*}
  \begin{textblock*}{30.3bp}(228.8bp,110.0bp)
    \includegraphics[width=30.3bp,height=38.6bp]{figures/picture-fill-7b606e52.png}
  \end{textblock*}
  \begin{textblock*}{237.29bp}(25.5bp,32.6bp)
    \vbox to 59.1bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{31.50}\adoptfontA\color{b2s59FF82}\vrule width0bp height27.66bp depth0bp\relax%
      WHAT ARE RELATIONS?
    \baselineskip=34.02bp\par}
    \vskip\dimexpr6.36bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{17.9bp}(201.8bp,13.6bp)
    \includegraphics[width=17.9bp,height=19.3bp]{figures/picture-fill-51d60eb9.png}
  \end{textblock*}
  \begin{textblock*}{11.3bp}(428.3bp,66.9bp)
    [... 27 further lines of the same forms as above ...]
    \vskip0.00bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\color{white}\vrule width0bp height10.98bp depth0bp\relax%
      A relation is a set of \textcolor{b2sFCB295}{\textbf{ordered pairs}} that represent a relationship.
    \baselineskip=13.61bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{195.2bp}(-20.5bp,168.5bp)
    \includegraphics[width=195.2bp,height=97.4bp]{figures/picture-c6b63208.png}
  \end{textblock*}
\end{frame}
```


### Pair f35

The two sources are the same slide written two ways.


**f35 source A** (vocabulary V1)


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


**f35 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s1BA94E}{HTML}{1BA94E}
\definecolor{b2s595959}{HTML}{595959}
\definecolor{b2s666666}{HTML}{666666}
\definecolor{b2sEEEEEE}{HTML}{EEEEEE}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{8.8bp}(15.5bp,263.6bp)
    \includegraphics[trim=647.95 405.58 646.00 0.00,clip,width=8.8bp,height=11.6bp]{figures/picture-603307e4.png}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(19.7bp,16.0bp)
    \vbox to 26.9bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\bfseries \color{black}\vrule width0bp height14.64bp depth0bp\relax%
      Product Roadmap
    \baselineskip=17.95bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{18.79bp}(424.5bp,263.6bp)
    \vbox to 15.1bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\adoptfontB\fontseries{w300}\selectfont \color{b2s666666}\vrule width0bp height4.88bp depth0bp\relax%
      26
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{420.1bp}(20.6bp,255.3bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (420.15bp,-0.01bp);
      \path[draw=b2s595959,line width=0.47bp,-{Triangle[length=0bp 5.0, width=0bp 4.5]}] (0bp,0bp) -- (420.15bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{57.42bp}(23.5bp,95.5bp)
    \vbox to 15.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Product \#1
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{57.42bp}(23.5bp,135.4bp)
    \vbox to 15.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Product \#2
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{57.42bp}(23.5bp,175.4bp)
    \vbox to 15.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Product \#3
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{57.42bp}(23.5bp,215.3bp)
    \vbox to 15.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Product \#4
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{49.7bp}(391.0bp,24.8bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (49.73bp,-12.89bp);
      \path[fill=b2sEEEEEE,draw=white,line width=0.47bp,shift={(0bp,0bp)}] (0bp,0bp) -- (49.73bp,0bp) -- (49.73bp,-12.89bp) -- (0bp,-12.89bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{41.30bp}(395.3bp,24.8bp)
    \vbox to 12.9bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\adoptfontA\color{b2s666666}\vrule width0bp height6.10bp depth0bp\relax%
      Not Started
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{49.7bp}(336.1bp,24.8bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (49.73bp,-12.89bp);
      \path[fill=b2s1BA94E,draw=white,line width=0.47bp,shift={(0bp,0bp)}] (0bp,0bp) -- (49.73bp,0bp) -- (49.73bp,-12.89bp) -- (0bp,-12.89bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{41.30bp}(340.4bp,24.8bp)
    \vbox to 12.9bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\adoptfontA\color{white}\vrule width0bp height6.10bp depth0bp\relax%
      In Progress
    [... 201 further lines of the same forms as above ...]
  \end{textblock*}
  \begin{textblock*}{28.22bp}(270.4bp,216.4bp)
    \vbox to 12.9bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\adoptfontA\color{b2s666666}\vrule width0bp height6.10bp depth0bp\relax%
      Feature
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f36

The two sources are the same slide written two ways.


**f36 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{body-white}{size=7.56, color=white, ascent=7.32, pitch=10.43, depth=3.11}
\slidestyle{body}{size=7.56, color=black, ascent=7.32, pitch=10.43, depth=3.11}
\slidestyle{label-white}{size=7.56, color=white}
\slidestyle{body-7.6}{size=7.56, color=black, ascent=7.32, pitch=9.07, depth=1.75}
\slidemark{dot}{\tikz[baseline=-0.45bp]\path[fill=black] (1.56bp,1.56bp) circle[radius=1.56bp];}
\setslidepar{style=body-white}
\setslidelist{itemize}{1}{style=body,indent=22.68,mark=dot,gap=11.94}
\setslidelist{enumerate}{1}{style=body-white,indent=22.68,labelstyle=label-white,label={\arabic*.},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=content-slide]
  \frametitle{Main KPI Development \#1}
  \slidepicture{7.6,50.4,431,178.2}{figures/chart-079fc98f.png}
  \slidetext{19.7,230.3,414.24,26.9}{body-7.6}{Description of factors that impacted the KPI since last board meeting / description of activities that will have an impact in the near future,}
\end{frame}
```


**f36 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s666666}{HTML}{666666}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{8.8bp}(15.5bp,263.6bp)
    \includegraphics[trim=647.95 405.58 646.00 0.00,clip,width=8.8bp,height=11.6bp]{figures/picture-603307e4.png}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(19.7bp,16.0bp)
    \vbox to 26.9bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\bfseries \color{black}\vrule width0bp height14.64bp depth0bp\relax%
      Main KPI Development \#1
    \baselineskip=17.95bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{18.79bp}(424.5bp,263.6bp)
    \vbox to 15.1bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\adoptfontB\fontseries{w300}\selectfont \color{b2s666666}\vrule width0bp height4.88bp depth0bp\relax%
      14
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{431.0bp}(7.6bp,50.4bp)
    \includegraphics[width=431.0bp,height=178.2bp]{figures/chart-079fc98f.png}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(19.7bp,230.3bp)
    \vbox to 26.9bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{7.56}\color{black}\vrule width0bp height7.32bp depth0bp\relax%
      Description of factors that impacted the KPI since last board meeting / description of activities that will have an impact in the near future,
    \baselineskip=9.07bp\par}
    \vskip\dimexpr1.75bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f37

The two sources are the same slide written two ways.


**f37 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s080302}{HTML}{080302}
\definecolor{b2s595959}{HTML}{595959}
\definecolor{b2s742E2A}{HTML}{742E2A}
\definecolor{b2sA4C2F4}{HTML}{A4C2F4}
\definecolor{b2sB6D7A8}{HTML}{B6D7A8}
\definecolor{b2sE25952}{HTML}{E25952}
\definecolor{b2sEA9999}{HTML}{EA9999}
\definecolor{b2sF9CB9C}{HTML}{F9CB9C}
\definecolor{b2sFFAB40}{HTML}{FFAB40}
\definecolor{b2sFFF2CC}{HTML}{FFF2CC}
```


The frame:

```latex
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
  \begin{textblock*}{414.09bp}(9.3bp,13.4bp)
    \vbox to 28.4bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.87}\color{black}\vrule width0bp height15.36bp depth0bp\relax%
      Example: HTTP Request
    \baselineskip=18.90bp\par}
    \vskip\dimexpr3.68bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{71.2bp}(56.5bp,63.2bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (71.23bp,-28.41bp);
      \path[fill=b2sA4C2F4,draw=b2s595959,line width=0.47bp,shift={(0bp,0bp)}] (0bp,0bp) -- (71.23bp,0bp) -- (71.23bp,-28.41bp) -- (0bp,-28.41bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{62.81bp}(60.8bp,63.2bp)
    \vbox to 28.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax%
      HTTP
    \baselineskip=10.58bp\par}
    \vskip\dimexpr2.05bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{71.2bp}(56.5bp,102.4bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (71.23bp,-28.41bp);
      \path[fill=b2sB6D7A8,draw=b2s595959,line width=0.47bp,shift={(0bp,0bp)}] (0bp,0bp) -- (71.23bp,0bp) -- (71.23bp,-28.41bp) -- (0bp,-28.41bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{62.81bp}(60.8bp,102.4bp)
    \vbox to 28.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax%
      TCP
    \baselineskip=10.58bp\par}
    \vskip\dimexpr2.05bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{71.2bp}(56.5bp,141.6bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (71.23bp,-28.41bp);
      \path[fill=b2sFFF2CC,draw=b2s595959,line width=0.47bp,shift={(0bp,0bp)}] (0bp,0bp) -- (71.23bp,0bp) -- (71.23bp,-28.41bp) -- (0bp,-28.41bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{62.81bp}(60.8bp,141.6bp)
    \vbox to 28.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax%
      IP
    \baselineskip=10.58bp\par}
    \vskip\dimexpr2.05bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{71.2bp}(56.5bp,180.8bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (71.23bp,-28.40bp);
      \path[fill=b2sF9CB9C,draw=b2s595959,line width=0.47bp,shift={(0bp,0bp)}] (0bp,0bp) -- (71.23bp,0bp) -- (71.23bp,-28.4bp) -- (0bp,-28.4bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{62.81bp}(60.8bp,180.8bp)
    \vbox to 28.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\color{black}\vrule width0bp height8.54bp depth0bp\relax%
      Ethernet
    \baselineskip=10.58bp\par}
    \vskip\dimexpr2.05bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{71.2bp}(56.5bp,220.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (71.23bp,-28.41bp);
      \path[fill=b2sEA9999,draw=b2s595959,line width=0.47bp,shift={(0bp,0bp)}] (0bp,0bp) -- (71.23bp,0bp) -- (71.23bp,-28.41bp) -- (0bp,-28.41bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{62.81bp}(60.8bp,220.0bp)
    [... 206 further lines of the same forms as above ...]
  \end{textblock*}
  \begin{textblock*}{18.79bp}(424.5bp,231.3bp)
    \vbox to 19.5bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\color{b2s595959}\vrule width0bp height6.10bp depth0bp\relax%
      55
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f37 source B** (vocabulary V1)


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


### Pair f38

The two sources are the same slide written two ways.


**f38 source A** (vocabulary V2)


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
      第八題
    \baselineskip=33.07bp\par}
    \vskip\dimexpr6.43bp-\prevdepth\relax
    \vskip4.08bp}
  \end{textblock*}
  \begin{textblock*}{414.24bp}(19.7bp,72.8bp)
    \vbox to 172.3bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax%
      Why would you want to hyperlink objects into your own Google Slides presentation? (Select all that apply.)
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth-3.15bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip27.72bp\relax%
      A So students can use the Slides for review and can go
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth-3.15bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip27.72bp\relax%
      \ \ \ \ to additional resources outside of the presentation.
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth-3.15bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip27.72bp\relax%
      B To make the presentation more graphically appealing
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth-3.15bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip27.72bp\relax%
      C To make sure the presentation is linear
    \baselineskip=20.87bp\par}
    \prevdepth=\dimexpr\prevdepth-3.15bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\hskip27.72bp\relax%
      D For extending the learning experience for students.
    \baselineskip=20.87bp\par}
    \vskip\dimexpr6.23bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f38 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{body}{size=15.12, color=black, ascent=14.64, pitch=20.87, depth=6.23}
\slidestyle{label}{size=22.68, color=black}
\slidestyle{heading-microsoftjhenghe-22.7}{size=22.68, face=\adoptfontA, color=black, ascent=21.95, pitch=40.82, depth=5.26}
\setslidepar{style=body}
\setslidelist{itemize}{1}{style=heading-microsoftjhenghe-22.7,indent=22.68,labelstyle=label,label={★},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=標題與內文-2]
  \frametitle{第八題}
  \begin{slidebox}[first=27.72,space=3.15]{19.7,72.8,414.24,172.3}
    \slidepar[first=0,space=0]{Why would you want to hyperlink objects into your own Google Slides presentation? (Select all that apply.)}
    \slidepar{A So students can use the Slides for review and can go}
    \slidepar{\ \ \ \ to additional resources outside of the presentation.}
    \slidepar{B To make the presentation more graphically appealing}
    \slidepar{C To make sure the presentation is linear}
    \slidepar{D For extending the learning experience for students.}
  \end{slidebox}
\end{frame}
```


### Pair f39

The two sources are the same slide written two ways.


**f39 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s4D4F51}{HTML}{4D4F51}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{421.84bp}(18.2bp,5.6bp)
    \vbox to 27.5bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\bfseries \color{blue}\vrule width0bp height14.64bp depth0bp\relax%
      \textcolor{black}{\uline{Determining Significance}}\textcolor{black}{-} Null Hypothesis and P- value
    \baselineskip=17.95bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{434.96bp}(8.8bp,37.9bp)
    \vbox to 208.6bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\bfseries \color{b2s4D4F51}\vrule width0bp height14.64bp depth0bp\relax%
      In every experiment, there is an effect or difference between groups that the researchers are testing. It could be the effectiveness of a new drug, new fertilizer, or other variables that has benefits. Unfortunately for the researchers, there is always the possibility that there is no effect, that is, that there is no difference between the groups. This lack of a difference is called the \href{http://support.minitab.com/en-us/minitab/17/topic-library/basic-statistics-and-graphs/hypothesis-tests/basics/null-and-alternative-hypotheses/}{\textcolor{blue}{null hypothesis}}, which is essentially the position a devil’s advocate would take when evaluating the results of an experiment.
    \baselineskip=17.95bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f39 source B** (vocabulary V1)


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


### Pair f40

The two sources are the same slide written two ways.


**f40 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Blue}{HTML}{0005DF}
\definecolor{Green}{HTML}{0CAD4D}
\definecolor{NearBlack2}{HTML}{1A1A1A}
\definecolor{Grey2}{HTML}{5F6368}
\definecolor{OffWhite}{HTML}{DCE5EF}
\definecolor{Yellow2}{HTML}{FFBC04}
\setslideinset{1.53}
\slidestyle{tiny-black}{size=3.78, color=black, ascent=3.66, pitch=4.54, depth=0.88}
\slidestyle{title-mono-black}{size=13.7, family=mono, color=black, ascent=13.26, pitch=23.02, depth=3.18}
\slidestyle{label-mono-blue}{size=13.7, family=mono, color=Blue}
\slidestyle{title-black-14.2}{size=14.17, color=black, ascent=13.72, pitch=17, depth=3.29}
\slidestyle{large-medium-nearblack}{size=7.09, weight=w500, color=NearBlack2, ascent=6.86, pitch=8.51, depth=1.64}
\slidestyle{tiny-medium-grey}{size=4.25, weight=w500, color=Grey2, ascent=4.11, pitch=5.1, depth=0.99}
\slidestyle{heading-medium-white}{size=9.45, weight=w500, color=white, ascent=9.15, pitch=11.34, depth=2.19}
\setslidepar{style=tiny-black}
\setslidelist{enumerate}{1}{style=title-mono-black,indent=20.18,labelstyle=label-mono-blue,label={\arabic*.},gap=13.78}
```


The frame:

```latex
\begin{frame}[plain,layout=dot-grid-30bp]
  \slideline[draw=Green,line width=0.94bp,->]{56.2,61.4}{56.2,83.34}
  \slidetext[bottom,inset=0]{42.2,28.9,90.23,18}{title-black-14.2}{Process Chart}
  \sliderect[fill=OffWhite,rounded=9.48]{42.8,156,184.03,56.88}
  \sliderect[fill=white,rounded=5.58]{58.1,167.4,33.46,34.04}
  \slidetext[inset=0,center]{63.4,190.8,23,5.7}{tiny-medium-grey}{Short Label}
  \sliderect[fill=white,rounded=5.58]{98.1,167.4,33.45,34.04}
  \slidetext[inset=0,center]{103.3,190.8,23.01,5.7}{tiny-medium-grey}{Short Label}
  \sliderect[fill=white,rounded=5.58]{138.1,167.4,33.46,34.04}
  \slidetext[inset=0,center]{143.3,190.8,23.01,5.7}{tiny-medium-grey}{Short Label}
  \sliderect[fill=white,rounded=5.58]{178,167.4,33.46,34.04}
  \slidetext[inset=0,center]{183.2,190.8,23.01,5.7}{tiny-medium-grey}{Short Label}
  \sliderect[fill=white,draw=Green,line width=1.42bp,rounded=3.42]{42.9,120.9,55.01,20.52}
  \slidetext[middle,center]{44.5,120.9,51.88,20.5}{large-medium-nearblack}{Item A}
  \sliderect[fill=white,draw=Green,line width=1.42bp,rounded=3.42]{107,120.9,55.01,20.52}
  \slidetext[middle,center]{108.6,120.9,51.88,20.5}{large-medium-nearblack}{Item B}
  \sliderect[fill=white,draw=Green,line width=1.42bp,rounded=3.42]{171,120.9,55,20.52}
  \slidetext[middle,center]{172.6,120.9,51.87,20.5}{large-medium-nearblack}{Item C}
  \slideline[draw=Green,line width=0.94bp,<-]{194.8,167.37}{198.58,120.9}
  \slideline[draw=Green,line width=0.94bp,->]{198.5,120.9}{114.8,167.38}
  \slideline[draw=Green,line width=0.94bp,<-]{194.78,167.37}{134.5,120.9}
  \slideline[draw=Green,line width=0.94bp,->]{134.51,120.9}{74.9,167.38}
  \slideline[draw=Green,line width=0.94bp,->]{70.4,120.9}{154.77,167.38}
  \slideline[draw=Green,line width=0.94bp,->]{70.4,120.9}{114.81,167.38}
  \sliderect[fill=Green,draw=Green,line width=1.42bp,rounded=3.21]{42.6,86,183.79,19.24}
  \slidetext[middle,center]{44.2,86,180.66,19.2}{heading-medium-white}{Layer 1}
  \slideline[draw=Green,line width=0.94bp,->]{134.45,86}{70.4,120.93}
  \slideline[draw=Green,line width=0.94bp,->]{134.5,86}{134.5,120.93}
  \slideline[draw=Green,line width=0.94bp,->]{134.5,86}{198.56,120.93}
  \sliderect[fill=white,draw=Yellow2,line width=1.42bp,rounded=2.16]{42.9,57.3,27.15,12.98}
  \slidetext[middle,center]{44.5,57.3,24.01,13}{large-medium-nearblack}{A}
  \slideline[draw=Green,line width=0.94bp,->]{95.3,61.4}{95.3,83.34}
  \sliderect[fill=white,draw=Yellow2,line width=1.42bp,rounded=2.16]{81.9,57.3,27.15,12.98}
  \slidetext[middle,center]{83.5,57.3,24.01,13}{large-medium-nearblack}{B}
  \slideline[draw=Green,line width=0.94bp,->]{134.4,61.4}{134.4,83.34}
  \sliderect[fill=white,draw=Yellow2,line width=1.42bp,rounded=2.16]{120.9,57.3,27.14,12.98}
  \slidetext[middle,center]{122.5,57.3,24,13}{large-medium-nearblack}{C}
  \slideline[draw=Green,line width=0.94bp,->]{173.4,61.4}{173.4,83.34}
  \sliderect[fill=white,draw=Yellow2,line width=1.42bp,rounded=2.16]{159.9,57.3,27.14,12.98}
  \slidetext[middle,center]{161.5,57.3,24,13}{large-medium-nearblack}{D}
  \slideline[draw=Green,line width=0.94bp,->]{212.5,61.4}{212.5,83.34}
  \sliderect[fill=white,draw=Yellow2,line width=1.42bp,rounded=2.16]{198.9,57.3,27.14,12.98}
  \slidetext[middle,center]{200.5,57.3,24,13}{large-medium-nearblack}{E}
  \slideellipse[fill=Blue]{67.5,173.2,7.35,7.36}
  \slideellipse[fill=Blue]{107.5,173.2,7.36,7.36}
  \slideellipse[fill=Blue]{147.4,173.2,7.35,7.36}
  \slideellipse[fill=Blue]{187.8,173.2,7.35,7.36}
  \slidefreeform[fill=white]{191.2,176.2,8.01,8.47}{shapes/freeform-47faf52c.tex}
  \slidefreeform[fill=white]{151,176.2,8.01,8.47}{shapes/freeform-1e9d0459.tex}
  \slidefreeform[fill=white]{110.8,176.2,8.01,8.47}{shapes/freeform-0dbacc68.tex}
  \slidefreeform[fill=white]{70.9,176.2,8,8.47}{shapes/freeform-45d99793.tex}
\end{frame}
```


**f40 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s0005DF}{HTML}{0005DF}
\definecolor{b2s0CAD4D}{HTML}{0CAD4D}
\definecolor{b2s1A1A1A}{HTML}{1A1A1A}
\definecolor{b2s5F6368}{HTML}{5F6368}
\definecolor{b2sDCE5EF}{HTML}{DCE5EF}
\definecolor{b2sFFBC04}{HTML}{FFBC04}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{453.5bp}(-0.0bp,0.0bp)
    \includegraphics[width=453.5bp,height=255.1bp]{figures/image-8439d22e.png}
  \end{textblock*}
  \begin{textblock*}{0.1bp}(56.2bp,61.4bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (0.01bp,-21.94bp);
      \path[draw=b2s0CAD4D,line width=0.94bp,-{Triangle[length=0bp 5.0, width=0bp 4.5]}] (0bp,0bp) -- (0bp,-21.94bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{90.23bp}(42.2bp,28.9bp)
    \vbox to 18.0bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{14.17}\color{black}\vrule width0bp height13.72bp depth0bp\relax%
      Process Chart
    \baselineskip=17.00bp\par}
    \vskip\dimexpr3.29bp-\prevdepth\relax
    \vskip0.00bp}
  \end{textblock*}
  \begin{textblock*}{184.0bp}(42.8bp,156.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (184.03bp,-56.88bp);
      \path[fill=b2sDCE5EF,shift={(0bp,0bp)}] (0bp,-9.48bp) .. controls (0bp,-4.24bp) and (4.24bp,0bp) .. (9.48bp,0bp) -- (174.55bp,0bp) .. controls (179.79bp,0bp) and (184.03bp,-4.24bp) .. (184.03bp,-9.48bp) -- (184.03bp,-47.4bp) .. controls (184.03bp,-52.64bp) and (179.79bp,-56.88bp) .. (174.55bp,-56.88bp) -- (9.48bp,-56.88bp) .. controls (4.24bp,-56.88bp) and (0bp,-52.64bp) .. (0bp,-47.4bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{33.5bp}(58.1bp,167.4bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (33.46bp,-34.04bp);
      \path[fill=white,shift={(0bp,0bp)}] (0bp,-5.58bp) .. controls (0bp,-2.5bp) and (2.5bp,0bp) .. (5.58bp,0bp) -- (27.88bp,0bp) .. controls (30.96bp,0bp) and (33.46bp,-2.5bp) .. (33.46bp,-5.58bp) -- (33.46bp,-28.46bp) .. controls (33.46bp,-31.54bp) and (30.96bp,-34.04bp) .. (27.88bp,-34.04bp) -- (5.58bp,-34.04bp) .. controls (2.5bp,-34.04bp) and (0bp,-31.54bp) .. (0bp,-28.46bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{23.00bp}(63.4bp,190.8bp)
    \vbox to 5.7bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{4.25}\fontseries{w500}\selectfont \color{b2s5F6368}\vrule width0bp height4.11bp depth0bp\relax%
      Short Label
    \baselineskip=5.10bp\par}
    \vskip\dimexpr0.99bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{33.5bp}(98.1bp,167.4bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (33.45bp,-34.04bp);
      \path[fill=white,shift={(0bp,0bp)}] (0bp,-5.58bp) .. controls (0bp,-2.5bp) and (2.5bp,0bp) .. (5.58bp,0bp) -- (27.87bp,0bp) .. controls (30.95bp,0bp) and (33.45bp,-2.5bp) .. (33.45bp,-5.58bp) -- (33.45bp,-28.46bp) .. controls (33.45bp,-31.54bp) and (30.95bp,-34.04bp) .. (27.87bp,-34.04bp) -- (5.58bp,-34.04bp) .. controls (2.5bp,-34.04bp) and (0bp,-31.54bp) .. (0bp,-28.46bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{23.01bp}(103.3bp,190.8bp)
    \vbox to 5.7bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{4.25}\fontseries{w500}\selectfont \color{b2s5F6368}\vrule width0bp height4.11bp depth0bp\relax%
      Short Label
    \baselineskip=5.10bp\par}
    \vskip\dimexpr0.99bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{33.5bp}(138.1bp,167.4bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (33.46bp,-34.04bp);
      \path[fill=white,shift={(0bp,0bp)}] (0bp,-5.58bp) .. controls (0bp,-2.5bp) and (2.5bp,0bp) .. (5.58bp,0bp) -- (27.88bp,0bp) .. controls (30.96bp,0bp) and (33.46bp,-2.5bp) .. (33.46bp,-5.58bp) -- (33.46bp,-28.46bp) .. controls (33.46bp,-31.54bp) and (30.96bp,-34.04bp) .. (27.88bp,-34.04bp) -- (5.58bp,-34.04bp) .. controls (2.5bp,-34.04bp) and (0bp,-31.54bp) .. (0bp,-28.46bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{23.01bp}(143.3bp,190.8bp)
    \vbox to 5.7bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{4.25}\fontseries{w500}\selectfont \color{b2s5F6368}\vrule width0bp height4.11bp depth0bp\relax%
      Short Label
    \baselineskip=5.10bp\par}
    \vskip\dimexpr0.99bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{33.5bp}(178.0bp,167.4bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (33.46bp,-34.04bp);
      \path[fill=white,shift={(0bp,0bp)}] (0bp,-5.58bp) .. controls (0bp,-2.5bp) and (2.5bp,0bp) .. (5.58bp,0bp) -- (27.88bp,0bp) .. controls (30.96bp,0bp) and (33.46bp,-2.5bp) .. (33.46bp,-5.58bp) -- (33.46bp,-28.46bp) .. controls (33.46bp,-31.54bp) and (30.96bp,-34.04bp) .. (27.88bp,-34.04bp) -- (5.58bp,-34.04bp) .. controls (2.5bp,-34.04bp) and (0bp,-31.54bp) .. (0bp,-28.46bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{23.01bp}(183.2bp,190.8bp)
    \vbox to 5.7bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{4.25}\fontseries{w500}\selectfont \color{b2s5F6368}\vrule width0bp height4.11bp depth0bp\relax%
      Short Label
    \baselineskip=5.10bp\par}
    \vskip\dimexpr0.99bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{55.0bp}(42.9bp,120.9bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (55.01bp,-20.52bp);
      \path[fill=white,draw=b2s0CAD4D,line width=1.42bp,shift={(0bp,0bp)}] (0bp,-3.42bp) .. controls (0bp,-1.53bp) and (1.53bp,0bp) .. (3.42bp,0bp) -- (51.59bp,0bp) .. controls (53.48bp,0bp) and (55.01bp,-1.53bp) .. (55.01bp,-3.42bp) -- (55.01bp,-17.1bp) .. controls (55.01bp,-18.99bp) and (53.48bp,-20.52bp) .. (51.59bp,-20.52bp) -- (3.42bp,-20.52bp) .. controls (1.53bp,-20.52bp) and (0bp,-18.99bp) .. (0bp,-17.1bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{51.88bp}(44.5bp,120.9bp)
    \vbox to 20.5bp{\slidesbox
    [... 251 further lines of the same forms as above ...]
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (8.01bp,-8.47bp);
      \path[fill=white,even odd rule] (3.7bp,-0.26bp) -- (4.12bp,-0.09bp) -- (7.93bp,-3.09bp) -- (4.12bp,-6.14bp) -- (2.99bp,-5.45bp) -- (0.14bp,-3.09bp) -- cycle (0.37bp,-5.07bp) -- (0.72bp,-4.89bp) -- (1.01bp,-5.01bp) -- (3.84bp,-7.22bp) -- (4.12bp,-7.25bp) -- (6.96bp,-4.99bp) -- (7.24bp,-4.87bp) -- (7.89bp,-5.36bp) -- (4.12bp,-8.35bp) -- (3.84bp,-8.35bp) -- (0.35bp,-5.64bp) -- (0.07bp,-5.36bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{8.0bp}(70.9bp,176.2bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (8.00bp,-8.47bp);
      \path[fill=white,even odd rule] (3.7bp,-0.26bp) -- (4.12bp,-0.09bp) -- (7.92bp,-3.09bp) -- (4.12bp,-6.14bp) -- (2.98bp,-5.44bp) -- (0.14bp,-3.09bp) -- cycle (0.37bp,-5.07bp) -- (0.71bp,-4.89bp) -- (1bp,-5bp) -- (3.83bp,-7.22bp) -- (4.12bp,-7.26bp) -- (6.95bp,-4.99bp) -- (7.23bp,-4.87bp) -- (7.88bp,-5.36bp) -- (4.12bp,-8.36bp) -- (3.83bp,-8.35bp) -- (0.43bp,-5.72bp) -- (0.07bp,-5.36bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
\end{frame}
```


### Pair h01

The two sources are different slides from different talks.


**h01 source A** (vocabulary V1)


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


**h01 source B** (vocabulary V3)


The frame:

```latex
\begin{frame}{Two-digit balls}
  \begin{enumerate}
    \setcounter{enumi}{8}
    \item Ninth item
    \item Tenth item
    \item Eleventh item
  \end{enumerate}
\end{frame}
```


### Pair h02

The two sources are different slides from different talks.


**h02 source A** (vocabulary V3)


The frame:

```latex
\begin{frame}{Orange balls}
  \begin{itemize}
    \item First level
      \begin{itemize}
        \item Second level
          \begin{itemize}
            \item Third level
          \end{itemize}
      \end{itemize}
    \item Back to the first
  \end{itemize}
\end{frame}
```


**h02 source B** (vocabulary V1)


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


### Pair h03

The two sources are different slides from different talks.


**h03 source A** (vocabulary V3)


The frame:

```latex
\begin{frame}{Three digits and large items}
  \begin{enumerate}
    \setcounter{enumi}{98}
    \item Ninety-nine
    \item One hundred
    \item One hundred and one
  \end{enumerate}
  \begin{columns}[T]
    \column{0.45\textwidth}
    \large
    \begin{enumerate}
      \item Large one
      \item Large two
    \end{enumerate}
    \column{0.45\textwidth}
    \begin{enumerate}
      \item Right one
      \item Right two
    \end{enumerate}
  \end{columns}
\end{frame}
```


**h03 source B** (vocabulary V1)


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


### Pair h04

The two sources are different slides from different talks.


**h04 source A** (vocabulary V1)


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


**h04 source B** (vocabulary V3)


The frame:

```latex
\begin{frame}{Cut out and faded}
  \centering
  \includegraphics[trim=120 90 120 90, clip, width=0.3\textwidth]{plot.png}
  \hspace{0.1\textwidth}
  \begin{tikzpicture}
    \node[opacity=0.4, inner sep=0] {\includegraphics[width=0.3\textwidth]{plot.png}};
  \end{tikzpicture}
\end{frame}
```


### Pair h05

The two sources are different slides from different talks.


**h05 source A** (vocabulary V1)


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


**h05 source B** (vocabulary V3)


The frame:

```latex
\begin{frame}{Coloured boxes}
  \begin{beamercolorbox}[rounded=true,shadow=true,center,wd=6cm,sep=6pt]{block title}
    A centred callout box
  \end{beamercolorbox}
  \vspace{1em}
  \begin{beamercolorbox}[wd=\textwidth,sep=4pt]{title}
    A full-width banner with text on it
  \end{beamercolorbox}
  \vspace{1em}
  \centering
  \tikz\node[draw=blue,fill=blue!10,rounded corners,minimum width=4cm,minimum height=1cm]{Text in a TikZ node};
\end{frame}
```


Answer with the JSON list described above, one object per pair, nothing else.
