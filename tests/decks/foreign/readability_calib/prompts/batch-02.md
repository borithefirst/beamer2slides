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

### Vocabulary V2

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
% the fourth argument is boxed at its own width and set turned about the centre, where it would be if
% the element were upright - the way the deck turns a text box with everything in it.
\newcommand\slidestab[2]{\setbox0\hbox{#2}\global\advance\slidesx\wd0 \unhbox0 \slidestabn=\numexpr\slidesx/\dimexpr#1\relax\relax\ifdim\slidestabn\dimexpr#1\relax>\slidesx \advance\slidestabn-1 \fi\advance\slidestabn1 \hskip\dimexpr\slidestabn\dimexpr#1\relax-\slidesx\relax\global\slidesx=\slidestabn\dimexpr#1\relax}
% --- Shapes -----------------------------------------------------------------------------------
% \slideshape[turn]{x,y,w,h}{paths}: TikZ paths in a box x bp from the page's left edge and y bp from
%   its top, w by h bp; the origin is the box's top left corner, y pointing up. Arrow heads and strokes
%   may reach out of the box: they overlap, the box stays. `turn` is TikZ's rotate= (degrees,
%   counter-clockwise) and/or flip (mirrored left to right), both about the box's centre.
\newcommand\slideshape[3][]{\slides@xywh#2\@nil\slides@centre{\slides@w bp}{\slides@h bp}%
```


## The pairs


### Pair f16

The two sources are the same slide written two ways.


**f16 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s873624}{HTML}{873624}
\definecolor{b2sD9CCCB}{HTML}{D9CCCB}
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
  \begin{textblock*}{241.9bp}(61.9bp,13.5bp)
    \adoptrow{0}{15.41bp}
    \adoptrow{1}{78.57bp}
    \adoptfix{0}
    \adoptfix{1}
    \adoptcell{1}{0}{0}{115.10bp}{1.81bp}{120.94bp}{-2.92bp}{%
      \raggedright\small\color{black}\baselineskip=12.12bp\relax%
      \begin{otherlanguage}{hebrew}
      \centering בסיפור\par
      \end{otherlanguage}}
    \adoptcell{2}{0}{0}{115.10bp}{1.81bp}{120.94bp}{-2.92bp}{%
      \raggedright\small\color{black}\baselineskip=12.12bp\relax%
      \begin{otherlanguage}{hebrew}
      \centering בחיים\par
      \end{otherlanguage}}
    \adoptcell{3}{1}{1}{115.10bp}{1.81bp}{120.94bp}{-5.85bp}{%
      \raggedright\large\color{black}\baselineskip=14.52bp\relax%
      \begin{otherlanguage}{hebrew}
      \raggedright נער שובב קרא קרא \textbf{"זאב! זאב!"} והבהיל את האיכרים בכפרו לשווא. \textbf{כשבאמת הגיע זאב}, אף אחד לא האמין לו.\par
      \end{otherlanguage}}
    \adoptcell{4}{1}{1}{115.10bp}{1.81bp}{120.94bp}{-5.85bp}{%
      \raggedright\large\color{black}\baselineskip=14.52bp\relax%
      \begin{otherlanguage}{hebrew}
      \raggedright אם \textbf{תקרא לעזרה} כשאינך באמת זקוק לה, \textbf{כשבאמת תהיה במצב מסוכן} לא יאמינו לך.\par
      \end{otherlanguage}}
    \adopttops{2}
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \path[use as bounding box] (0bp,0bp) rectangle (241.9bp,{-\adopty{2}});
      \fill[b2s873624] (0.0bp,{-\adopty{0}}) rectangle (120.9bp,{-\adopty{1}});
      \fill[b2s873624] (120.9bp,{-\adopty{0}}) rectangle (241.9bp,{-\adopty{1}});
      \fill[b2sD9CCCB] (0.0bp,{-\adopty{1}}) rectangle (120.9bp,{-\adopty{2}});
      \fill[b2sD9CCCB] (120.9bp,{-\adopty{1}}) rectangle (241.9bp,{-\adopty{2}});
      \draw[white,line width=0.50bp,line cap=rect] (0.0bp,{-\adopty{0}}) -- (241.9bp,{-\adopty{0}});
      \draw[white,line width=1.51bp,line cap=rect] (0.0bp,{-\adopty{1}}) -- (241.9bp,{-\adopty{1}});
      \draw[white,line width=0.50bp,line cap=rect] (0.0bp,{-\adopty{2}}) -- (241.9bp,{-\adopty{2}});
      \draw[white,line width=0.50bp,line cap=rect] (0.0bp,{-\adopty{0}}) -- (0.0bp,{-\adopty{2}});
      \draw[white,line width=0.50bp,line cap=rect] (120.9bp,{-\adopty{0}}) -- (120.9bp,{-\adopty{2}});
      \draw[white,line width=0.50bp,line cap=rect] (241.9bp,{-\adopty{0}}) -- (241.9bp,{-\adopty{2}});
      \node[anchor=north west] at (2.9bp,{-\adopty{0}-11.38bp+\adoptht{1}}) {\adoptbox{1}};
      \node[anchor=north west] at (123.9bp,{-\adopty{0}-11.38bp+\adoptht{2}}) {\adoptbox{2}};
      \node[anchor=north west] at (2.9bp,{-\adopty{1}-13.33bp+\adoptht{3}}) {\adoptbox{3}};
      \node[anchor=north west] at (123.9bp,{-\adopty{1}-13.33bp+\adoptht{4}}) {\adoptbox{4}};
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{310.45bp}(27.6bp,144.6bp)
    \vbox to 122.1bp{\slidesbox
    \vskip1.45bp
    \begin{otherlanguage}{hebrew}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{10.08}\rmfamily\bfseries \color{black}\vrule width0bp height9.76bp depth0bp\relax%
      \uline{כתבו מהו מוסר ההשכל שניתן ללמוד מהסיפור, נמקו את תשובתכם לפי הסיפור.}
    \baselineskip=12.09bp\par}\end{otherlanguage}
    \prevdepth=\dimexpr\prevdepth-0.16bp\relax\begin{otherlanguage}{hebrew}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{9.07}\rmfamily\color{black}\vrule width0bp height8.78bp depth0bp\relax% blank line
    \baselineskip=10.96bp\par}\end{otherlanguage}
    \prevdepth=\dimexpr\prevdepth+0.16bp\relax\begin{otherlanguage}{hebrew}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{10.08}\rmfamily\color{black}\vrule width0bp height9.76bp depth0bp\relax%
      אם תקרא לעזרה כשאינך באמת זקוק לה, כשבאמת תהיה במצב מסוכן לא יאמינו לך. בסיפור, נער שובב קרא קרא \symbol{34}זאב! זאב!\symbol{34} והבהיל את האיכרים בכפרו לשווא. כשבאמת הגיע זאב, אף אחד לא האמין לו.
    \baselineskip=12.09bp\par}\end{otherlanguage}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax\begin{otherlanguage}{hebrew}{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{10.08}\rmfamily\color{black}\vrule width0bp height9.76bp depth0bp\relax% blank line
    \baselineskip=12.09bp\par}\end{otherlanguage}
    \vskip\dimexpr2.34bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f16 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{NearBlack}{HTML}{262626}
\definecolor{DarkRed}{HTML}{873624}
\definecolor{LightGrey}{HTML}{D9CCCB}
\setslideinset{1.45}
\slidestyle{body-serif}{size=12.09, family=serif, color=black, ascent=11.7, pitch=14.36, depth=2.8}
\slidestyle{small-serif-bold}{size=10.08, family=serif, weight=bold, color=black, ascent=9.76, pitch=12.09, depth=2.34}
\slidestyle{small-serif}{size=9.07, family=serif, color=black, ascent=8.78, pitch=10.96, depth=2.1}
\slidestyle{small-serif-10.1}{size=10.08, family=serif, color=black, ascent=9.76, pitch=12.09, depth=2.34}
\slidestyle{body-serif-nearblack}{size=12.09, family=serif, color=NearBlack, ascent=11.7, pitch=14.36, depth=2.8}
\slidestyle{label-darkred-12.1}{size=12.09, color=DarkRed}
\setslidepar{style=body-serif}
\setslidelist{itemize}{1}{style=body-serif-nearblack,indent=14.51,labelstyle=label-darkred-12.1,label={❧},gap=9.42}
```


The frame:

```latex
\begin{frame}[plain,layout=ריק]
  \begin{slidetable}[inset x=2.923, inset y=1.814, border={white,line width=0.5bp}, h=15.41, fixed, align=center, fill=DarkRed, style={\small\color{black}}, pitch=12.12, baseline=11.38]{61.9,13.5}{120.943,120.943}
    \cell[lang=hebrew]{בסיפור} & \cell[lang=hebrew]{בחיים} \\
    \row[h=78.57, fill=LightGrey, style={\large\color{black}}, pitch=14.52, baseline=13.33] \cell[align=right, lang=hebrew]{נער שובב קרא קרא \textbf{"זאב! זאב!"} והבהיל את האיכרים בכפרו לשווא. \textbf{כשבאמת הגיע זאב}, אף אחד לא האמין לו.} & \cell[align=right, lang=hebrew]{אם \textbf{תקרא לעזרה} כשאינך באמת זקוק לה, \textbf{כשבאמת תהיה במצב מסוכן} לא יאמינו לך.} \\
    \hborder{1}{white,line width=1.51bp}
  \end{slidetable}
  \begin{slidebox}[style=small-serif-10.1,lang=hebrew]{27.6,144.6,310.45,122.1}
    \slidepar[style=small-serif-bold]{\uline{כתבו מהו מוסר ההשכל שניתן ללמוד מהסיפור, נמקו את תשובתכם לפי הסיפור.}}
    \slidepar[style=small-serif,space=0.01]{}
    \slidepar[space=-0.01]{אם תקרא לעזרה כשאינך באמת זקוק לה, כשבאמת תהיה במצב מסוכן לא יאמינו לך. בסיפור, נער שובב קרא קרא \symbol{34}זאב! זאב!\symbol{34} והבהיל את האיכרים בכפרו לשווא. כשבאמת הגיע זאב, אף אחד לא האמין לו.}
    \slidepar{}
  \end{slidebox}
\end{frame}
```


### Pair f17

The two sources are the same slide written two ways.


**f17 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2sE91D63}{HTML}{E91D63}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{453.5bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (453.54bp,-73.82bp);
      \path[fill=b2sE91D63,shift={(0bp,0bp)}] (0bp,0bp) -- (453.54bp,0bp) -- (453.54bp,-73.82bp) -- (0bp,-73.82bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{402.42bp}(27.2bp,12.2bp)
    \vbox to 44.8bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontC\color{white}\vrule width0bp height21.95bp depth0bp\relax%
      Google Groups
    \baselineskip=27.40bp\par}
    \vskip\dimexpr5.26bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{424.80bp}(13.4bp,79.6bp)
    \vbox to 159.9bp{\slidesbox
    \vskip4.08bp
    {\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      \uline{建立群組}
    \baselineskip=40.82bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      \uline{邀請人加入群組}
    \baselineskip=40.82bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      群組討論
    \baselineskip=40.82bp\par}
    \vskip\dimexpr5.26bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{50.0bp}(7.4bp,4.7bp)
    \includegraphics[trim=10.66 0.00 0.00 0.00,clip,width=50.0bp,height=63.8bp]{figures/picture-de54c7a2.png}
  \end{textblock*}
  \begin{textblock*}{435.89bp}(129.8bp,102.5bp)
    \vbox to 19.9bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\adoptfontA\color{black}\vrule width0bp height10.98bp depth0bp\relax%
      \href{https://drive.google.com/file/d/0B6VPO_ULtz0RdWI0MWJOSU5raDg/view}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RdWI0MWJOSU5raDg/view}}
    \baselineskip=13.70bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{428.37bp}(137.3bp,148.6bp)
    \vbox to 18.1bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\adoptfontA\color{black}\vrule width0bp height10.98bp depth0bp\relax%
      \href{https://drive.google.com/file/d/0B6VPO_ULtz0RUWdsNVdzNzN5elk/view}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RUWdsNVdzNzN5elk/view}}
    \baselineskip=13.70bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f17 source B** (vocabulary V2)


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


### Pair f18

The two sources are the same slide written two ways.


**f18 source A** (vocabulary V1)


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
  \begin{textblock*}{348.28bp}(7.2bp,47.1bp)
    \vbox to 37.4bp{\slidesbox
    \vskip3.27bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{12.09}\color{white}\vrule width0bp height11.70bp depth0bp\relax%
      Sight Words
    \baselineskip=14.36bp\par}
    \vskip\dimexpr2.80bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{353.05bp}(5.6bp,237.1bp)
    \vbox to 41.1bp{\slidesbox
    \vskip3.27bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{9.07}\color{white}\vrule width0bp height8.78bp depth0bp\relax%
      Tip: This doesn’t work well on iPads unfortunately-{}-I’d recommend using Chromebooks or Computers with Google Classroom. If you have Clever or QR sign on, you’re golden!
    \baselineskip=10.96bp\par}
    \vskip\dimexpr2.10bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{348.4bp}(9.7bp,6.5bp)
    \noindent\textcolor{black}{\resizebox*{348.4bp}{37.4bp}{\bfseries Drag and Drop Activity}}
  \end{textblock*}
  \begin{textblock*}{348.4bp}(7.2bp,6.5bp)
    \noindent\textcolor{black}{\resizebox*{348.4bp}{37.4bp}{\bfseries Drag and Drop Activity}}
  \end{textblock*}
  \begin{textblock*}{258.2bp}(14.7bp,74.8bp)
    \includegraphics[trim=88.76 0.00 282.87 201.49,clip,width=258.2bp,height=156.5bp]{figures/picture-4709b982.png}
  \end{textblock*}
  \begin{textblock*}{74.1bp}(278.1bp,70.8bp)
    \noindent\textcolor{black}{\resizebox*{74.1bp}{26.0bp}{\bfseries Grade K-1}}
  \end{textblock*}
  \begin{textblock*}{79.6bp}(275.4bp,113.2bp)
    \includegraphics[width=79.6bp,height=79.6bp]{figures/picture-de3cd391.png}
  \end{textblock*}
\end{frame}
```


**f18 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{3.27}
\slidestyle{label}{size=12.09, color=white}
\slidestyle{small}{size=11.09, color=white, ascent=10.74, pitch=13.23, depth=2.57}
\slidestyle{body-12.1}{size=12.09, color=white, ascent=11.7, pitch=14.36, depth=2.8}
\slidestyle{label-11.1}{size=11.09, color=white}
\slidestyle{small-9.1}{size=9.07, color=white, ascent=8.78, pitch=10.96, depth=2.1}
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
  \framesubtitle{Sight Words}
  \slidetext{5.6,237.1,353.05,41.1}{small-9.1}{Tip: This doesn’t work well on iPads unfortunately-{}-I’d recommend using Chromebooks or Computers with Google Classroom. If you have Clever or QR sign on, you’re golden!}
  \begin{textblock*}{348.4bp}(9.7bp,6.5bp)
    \noindent\textcolor{black}{\resizebox*{348.4bp}{37.4bp}{\bfseries Drag and Drop Activity}}
  \end{textblock*}
  \begin{textblock*}{348.4bp}(7.2bp,6.5bp)
    \noindent\textcolor{black}{\resizebox*{348.4bp}{37.4bp}{\bfseries Drag and Drop Activity}}
  \end{textblock*}
  \slidepicture[trim=88.76 0 282.87 201.49]{14.7,74.8,258.2,156.5}{figures/picture-4709b982.png}
  \begin{textblock*}{74.1bp}(278.1bp,70.8bp)
    \noindent\textcolor{black}{\resizebox*{74.1bp}{26.0bp}{\bfseries Grade K-1}}
  \end{textblock*}
  \slidepicture{275.4,113.2,79.6,79.6}{figures/picture-de3cd391.png}
\end{frame}
```


### Pair f19

The two sources are the same slide written two ways.


**f19 source A** (vocabulary V2)


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


**f19 source B** (vocabulary V1)


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{418.8bp}(26.7bp,3.8bp)
    \includegraphics[trim=8.07 17.41 0.00 0.00,clip,width=418.8bp,height=247.6bp]{figures/picture-0c48b18b.png}
  \end{textblock*}
\end{frame}
```


### Pair f20

The two sources are the same slide written two ways.


**f20 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s0097A7}{HTML}{0097A7}
\definecolor{b2s434343}{HTML}{434343}
\definecolor{b2s595959}{HTML}{595959}
\definecolor{b2sFF9900}{HTML}{FF9900}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{406.9bp}(35.8bp,3.9bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (406.91bp,-58.16bp);
      \path[fill=b2s434343,shift={(0bp,0bp)}] (0bp,-9.69bp) .. controls (0bp,-4.34bp) and (4.34bp,0bp) .. (9.69bp,0bp) -- (397.22bp,0bp) .. controls (402.57bp,0bp) and (406.91bp,-4.34bp) .. (406.91bp,-9.69bp) -- (406.91bp,-48.47bp) .. controls (406.91bp,-53.82bp) and (402.57bp,-58.16bp) .. (397.22bp,-58.16bp) -- (9.69bp,-58.16bp) .. controls (4.34bp,-58.16bp) and (0bp,-53.82bp) .. (0bp,-48.47bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{66.2bp}(10.8bp,-0.0bp)
    \includegraphics[width=66.2bp,height=65.9bp]{figures/picture-8370d7d1.png}
  \end{textblock*}
  \begin{textblock*}{315.07bp}(82.9bp,15.3bp)
    \vbox to 28.4bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\color{white}\vrule width0bp height21.95bp depth0bp\relax%
      Changing Slide Dimensions
    \baselineskip=27.40bp\par}
    \vskip\dimexpr5.26bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{189.54bp}(14.4bp,86.3bp)
    \vbox to 127.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s434343}\vrule width0bp height14.64bp depth0bp\relax\llap{{\slidesize{15.12}\color{b2s434343}➔}\hskip12.54bp}%
      Click “File>Page Setup
    \baselineskip=17.95bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=45.35bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{b2s434343}\vrule width0bp height14.64bp depth0bp\relax\llap{{\slidesize{15.12}\color{b2s434343}◆}\hskip12.54bp}%
      Click the dropdown carrots and choose “Custom” to set your preferred slide dimension
    \baselineskip=17.95bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{89.89bp}(105.8bp,238.4bp)
    \vbox to 11.5bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{7.56}\adoptfontA\color{b2s0097A7}\vrule width0bp height7.32bp depth0bp\relax%
      \uline{Back to Table of Contents}
    \baselineskip=9.07bp\par}
    \vskip\dimexpr1.75bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{124.7bp}(266.4bp,164.3bp)
    \tikz\node[inner sep=0bp,draw=b2s595959,line width=0.47bp]{\includegraphics[width=124.2bp,height=81.5bp]{figures/picture-cc434f98.jpg}};
  \end{textblock*}
  \begin{textblock*}{124.7bp}(266.4bp,71.7bp)
    \tikz\node[inner sep=0bp,draw=b2s595959,line width=0.47bp]{\includegraphics[width=124.2bp,height=74.4bp]{figures/picture-c331c286.jpg}};
  \end{textblock*}
  \begin{textblock*}{57.9bp}(192.5bp,87.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (57.93bp,-29.25bp);
      \path[draw=b2sFF9900,line width=0.94bp] (0bp,-29.25bp) -- (57.93bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{76.1bp}(250.4bp,87.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (76.06bp,-16.96bp);
      \path[draw=b2sFF9900,line width=0.94bp,-{Triangle[length=0bp 5.0, width=0bp 4.5]}] (0bp,0bp) -- (76.06bp,-16.96bp);
    \end{tikzpicture}
  \end{textblock*}
\end{frame}
```


**f20 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{Cyan}{HTML}{0097A7}
\definecolor{NearBlack}{HTML}{222222}
\definecolor{DarkGrey}{HTML}{434343}
\definecolor{DarkGrey2}{HTML}{595959}
\definecolor{Orange}{HTML}{FF9900}
\setslideinset{4.08}
\slidestyle{heading-white}{size=22.68, color=white, ascent=21.95, pitch=27.4, depth=5.26}
\slidestyle{body}{size=15.12, color=DarkGrey, ascent=14.64, pitch=20.87, depth=6.23}
\slidestyle{label}{size=11.34, color=DarkGrey}
\slidestyle{small-cyan}{size=11.34, color=Cyan, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{small-cyan-11.3}{size=11.34, color=Cyan, ascent=10.98, pitch=13.7, depth=2.63}
\slidestyle{tiny-arial-nearblack}{size=10.71, face=\adoptfontA, color=NearBlack, ascent=10.37, pitch=14.78, depth=4.41}
\slidestyle{label-nearblack}{size=10.71, color=NearBlack}
\slidestyle{tiny-arial-cyan}{size=7.56, face=\adoptfontA, color=Cyan, ascent=7.32, pitch=9.07, depth=1.75}
\slidestyle{label-15.1}{size=15.12, color=DarkGrey}
\slidestyle{small-11.3-2}{size=11.34, color=DarkGrey, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{body-15.1}{size=15.12, color=DarkGrey, ascent=14.64, pitch=17.95, depth=3.51}
\setslidepar{style=small-cyan-11.3}
\setslidelist{itemize}{1}{style=body,indent=22.68,labelstyle=label-15.1,label={➔},gap=12.54}
\setslidelist{itemize}{2}{style=small-11.3-2,indent=45.35,labelstyle=label,label={◆},gap=12.54}
\setslidelist{itemize}{3}{style=small-cyan,indent=68.03,labelstyle=label,label={A.},gap=12.54}
\setslidelist{enumerate}{1}{style=tiny-arial-nearblack,indent=15.75,labelstyle=label-nearblack,label={\arabic*.},gap=8.76}
```


The frame:

```latex
\begin{frame}[plain,layout=title-and-body]
  \sliderect[fill=DarkGrey,rounded=9.69]{35.8,3.9,406.91,58.16}
  \slidepicture{10.8,0,66.2,65.9}{figures/picture-8370d7d1.png}
  \slidetext[center]{82.9,15.3,315.07,28.4}{heading-white}{Changing Slide Dimensions}
  \begin{slidebox}{14.4,86.3,189.54,127.1}
    \begin{itemize}
      \item[style=body-15.1] Click “File>Page Setup
      \begin{itemize}
        \item[style=body-15.1,labelstyle=label-15.1] Click the dropdown carrots and choose “Custom” to set your preferred slide dimension
      \end{itemize}
    \end{itemize}
  \end{slidebox}
  \slidetext{105.8,238.4,89.89,11.5}{tiny-arial-cyan}{\uline{Back to Table of Contents}}
  \slidepicture[outline=DarkGrey2,outline width=0.47]{266.4,164.3,124.2,81.5}{figures/picture-cc434f98.jpg}
  \slidepicture[outline=DarkGrey2,outline width=0.47]{266.4,71.7,124.2,74.4}{figures/picture-c331c286.jpg}
  \slideline[draw=Orange,line width=0.94bp]{192.5,116.25}{250.43,87}
  \slideline[draw=Orange,line width=0.94bp,->]{250.4,87}{326.46,103.96}
\end{frame}
```


### Pair f21

The two sources are the same slide written two ways.


**f21 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s242424}{HTML}{242424}
\definecolor{b2sF3AB15}{HTML}{F3AB15}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{132.9bp}(250.8bp,0.0bp)
    \includegraphics[trim=0.00 0.00 0.00 106.11,clip,width=132.9bp,height=72.7bp]{figures/picture-8c058ab9.png}
  \end{textblock*}
  \begin{textblock*}{232.82bp}(25.5bp,25.3bp)
    \vbox to 68.3bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{28.35}\adoptfontB\color{b2sF3AB15}\vrule width0bp height27.44bp depth0bp\relax%
      How to Use This Presentation
    \baselineskip=40.82bp\par}
    \vskip\dimexpr13.38bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{98.74bp}(37.0bp,160.8bp)
    \vbox to 55.4bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.93}\color{black}\vrule width0bp height6.71bp depth0bp\relax%
      Click on the \textbf{\symbol{34}Canva\symbol{34}} button under this presentation preview. Start editing your presentation. You need to sign in to your Canva account.
    \baselineskip=11.64bp\par}
    \vskip\dimexpr1.61bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{102.10bp}(175.4bp,160.8bp)
    \vbox to 78.9bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.93}\color{black}\vrule width0bp height6.71bp depth0bp\relax%
      Export this design from Canva as a \textbf{PowerPoint template}. Open the design in \textbf{Canva}. This will provide you with all the fonts used and elements used in this presentation as listed on page 21. Learn more on slide 3.
    \baselineskip=11.64bp\par}
    \vskip\dimexpr1.61bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{101.04bp}(317.3bp,160.8bp)
    \vbox to 67.2bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.93}\color{black}\vrule width0bp height6.71bp depth0bp\relax%
      Export this design from Canva as a \textbf{GoogleSlide template}. This will provide you with all the fonts used and elements used in this presentation as listed on page 21. Learn more on slide 4.
    \baselineskip=11.64bp\par}
    \vskip\dimexpr1.61bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{148.83bp}(11.9bp,140.7bp)
    \vbox to 17.6bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.93}\adoptfontA\color{b2s242424}\vrule width0bp height6.71bp depth0bp\relax%
      CANVA
    \baselineskip=11.64bp\par}
    \vskip\dimexpr1.61bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{148.83bp}(152.4bp,140.7bp)
    \vbox to 17.6bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.93}\adoptfontA\color{b2s242424}\vrule width0bp height6.71bp depth0bp\relax%
      POWERPOINT
    \baselineskip=11.64bp\par}
    \vskip\dimexpr1.61bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{148.83bp}(291.7bp,140.7bp)
    \vbox to 17.6bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.93}\adoptfontA\color{b2s242424}\vrule width0bp height6.71bp depth0bp\relax%
      GOOGLE SLIDES
    \baselineskip=11.64bp\par}
    \vskip\dimexpr1.61bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f21 source B** (vocabulary V2)


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


### Pair f22

The two sources are the same slide written two ways.


**f22 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{DarkGrey}{HTML}{595959}
\definecolor{Lime}{HTML}{9CAE4B}
\definecolor{Grey}{HTML}{9E9E9E}
\setslideinset{4.08}
\slidestyle{large-extrabold-white}{size=11.34, weight=w800, color=white, ascent=10.98, pitch=13.7, depth=2.63}
\slidestyle{small-darkgrey-9.4}{size=9.45, color=DarkGrey, ascent=9.15, pitch=13.04, depth=3.89}
\slidestyle{tiny-medium-5}{size=5.04, weight=w500, color=black, ascent=4.88, pitch=6.05, depth=1.17}
\slidestyle{large-darkgrey}{size=11.34, color=DarkGrey, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{body-light}{size=10.08, weight=w300, color=black, ascent=9.76, pitch=12.28, depth=2.34}
\slidestyle{label}{size=10.08, color=black}
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
  \sliderect[fill=Lime,draw=DarkGrey,line width=0.47bp,rounded=4.74]{0,0,453.54,28.41}
  \slidetext[middle]{4.2,0,445.17,28.4}{large-extrabold-white}{Product \& Market: track customer journeys}
  \slidetext{4.2,28.4,445.17,26.8}{large-darkgrey}{Pipeline: LOIs -> Pilot projects -> Sales \ \ \ [(X) - in negotiation)]}
  \begin{slidetable}[inset x=4.241, inset y=4.535, border={Grey,line width=0.47bp}, h=27.21, fixed, aligns={left,left,center,center,center,center,center,center}, style={\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}}, pitch=9.12, baseline=11.63]{2,59.5}{61.933,61.933,44.34,43.67,55.584,61.422,58.373,62.352}
    \row[h=35.87] Customer type & Product type & \cell{
        Identify\\ LOIs} & \cell{
        Explore\\ Pilots} & \cell{
        Develop\\ PMF \& integration} & Now value & Future value & \$ and time to sell \\
    Early adopter & Bespoke projects & 2 (2) & 3 (2) & 3 & \$50 - \$100k & \$100k / yr & \$10k, 5 ± 3 months \\
    \row[h=17.86] Enterprise & Supply chain & & & & \$80k & \$10M / yr & \$1M, 1-2 years \\
    Specialty business & \cell{
        Subscriptions

        Unit license} & 0 & 0 & 0 & & & \\
    \row[h=26.93] Service bureau & \cell{
        Subscriptions

        Unit license} & 1 & 0 & 0 & & & \\
    \row[h=26.93] Buyers \& sellers & Marketplace & 0 & 0 & 0 & & & \\
    \row[h=18.14] & & & & & & & \\
  \end{slidetable}
\end{frame}
```


**f22 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s595959}{HTML}{595959}
\definecolor{b2s9CAE4B}{HTML}{9CAE4B}
\definecolor{b2s9E9E9E}{HTML}{9E9E9E}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{453.5bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (453.54bp,-28.41bp);
      \path[fill=b2s9CAE4B,draw=b2s595959,line width=0.47bp,shift={(0bp,0bp)}] (0bp,-4.74bp) .. controls (0bp,-2.12bp) and (2.12bp,0bp) .. (4.74bp,0bp) -- (448.8bp,0bp) .. controls (451.42bp,0bp) and (453.54bp,-2.12bp) .. (453.54bp,-4.74bp) -- (453.54bp,-23.67bp) .. controls (453.54bp,-26.29bp) and (451.42bp,-28.41bp) .. (448.8bp,-28.41bp) -- (4.74bp,-28.41bp) .. controls (2.12bp,-28.41bp) and (0bp,-26.29bp) .. (0bp,-23.67bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{445.17bp}(4.2bp,0.0bp)
    \vbox to 28.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\fontseries{w800}\selectfont \color{white}\vrule width0bp height10.98bp depth0bp\relax%
      Product \& Market: track customer journeys
    \baselineskip=13.70bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{445.17bp}(4.2bp,28.4bp)
    \vbox to 26.8bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      Pipeline: LOIs -> Pilot projects -> Sales \ \ \ [(X) - in negotiation)]
    \baselineskip=15.65bp\par}
    \vskip\dimexpr4.67bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{449.6bp}(2.0bp,59.5bp)
    \adoptrow{0}{35.87bp}
    \adoptrow{1}{27.21bp}
    \adoptrow{2}{17.86bp}
    \adoptrow{3}{27.21bp}
    \adoptrow{4}{26.93bp}
    \adoptrow{5}{26.93bp}
    \adoptrow{6}{18.14bp}
    \adoptfix{0}
    \adoptfix{1}
    \adoptfix{2}
    \adoptfix{3}
    \adoptfix{4}
    \adoptfix{5}
    \adoptfix{6}
    \adoptcell{1}{0}{0}{53.45bp}{4.54bp}{61.93bp}{0.00bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      Customer type}
    \adoptcell{2}{0}{0}{53.45bp}{4.54bp}{61.93bp}{0.00bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      Product type}
    \adoptcell{3}{0}{0}{35.86bp}{4.54bp}{44.34bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering Identify\\ LOIs}
    \adoptcell{4}{0}{0}{35.19bp}{4.54bp}{43.67bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering Explore\\ Pilots}
    \adoptcell{5}{0}{0}{47.10bp}{4.54bp}{55.58bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering Develop\\ PMF \& integration}
    \adoptcell{6}{0}{0}{52.94bp}{4.54bp}{61.42bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering Now value}
    \adoptcell{7}{0}{0}{49.89bp}{4.54bp}{58.37bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering Future value}
    \adoptcell{8}{0}{0}{53.87bp}{4.54bp}{62.35bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering \$ and time to sell}
    \adoptcell{9}{1}{1}{53.45bp}{4.54bp}{61.93bp}{0.00bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      Early adopter}
    \adoptcell{10}{1}{1}{53.45bp}{4.54bp}{61.93bp}{0.00bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      Bespoke projects}
    \adoptcell{11}{1}{1}{35.86bp}{4.54bp}{44.34bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering 2 (2)}
    \adoptcell{12}{1}{1}{35.19bp}{4.54bp}{43.67bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering 3 (2)}
    \adoptcell{13}{1}{1}{47.10bp}{4.54bp}{55.58bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{3}\ifdim\wd0>\linewidth\kern-4.24bp\hbox to\dimexpr\linewidth+8.48bp{\hss\box0\hss}\kern-4.24bp\else\hss\box0\hss\fi}}
    \adoptcell{14}{1}{1}{52.94bp}{4.54bp}{61.42bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering \$50 - \$100k}
    \adoptcell{15}{1}{1}{49.89bp}{4.54bp}{58.37bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering \$100k / yr}
    \adoptcell{16}{1}{1}{53.87bp}{4.54bp}{62.35bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \centering \$10k, 5 ± 3 months}
    \adoptcell{17}{2}{2}{53.45bp}{4.54bp}{61.93bp}{0.00bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      \noindent\hbox to\linewidth{\setbox0\hbox{Enterprise}\ifdim\wd0>\linewidth\kern-4.24bp\hbox to\dimexpr\linewidth+8.48bp{\box0\hss}\kern-4.24bp\else\box0\hss\fi}}
    \adoptcell{18}{2}{2}{53.45bp}{4.54bp}{61.93bp}{0.00bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
      Supply chain}
    \adoptcell{19}{2}{2}{52.94bp}{4.54bp}{61.42bp}{-4.24bp}{%
      \raggedright\fontsize{7.6bp}{9.1bp}\selectfont\adoptfontA\color{black}\baselineskip=9.12bp\relax%
    [... 113 further lines of the same forms as above ...]
  \end{textblock*}
  \begin{textblock*}{75.10bp}(374.3bp,240.1bp)
    \vbox to 15.1bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\fontseries{w300}\selectfont \color{b2s595959}\vrule width0bp height6.10bp depth0bp\relax%
      17
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f23

The two sources are the same slide written two ways.


**f23 source A** (vocabulary V2)


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


**f23 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s4285F4}{HTML}{4285F4}
\definecolor{b2sC3ECF6}{HTML}{C3ECF6}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{0.1bp}(226.8bp,148.6bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (0.01bp,-23.61bp);
      \path[draw=black,line width=0.94bp] (0bp,-23.61bp) -- (0bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{52.8bp}(42.7bp,71.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (52.81bp,-56.55bp);
      \path[fill=b2sC3ECF6,draw=black,line width=0.94bp,shift={(0bp,0bp)}] (0bp,-8.8bp) .. controls (0bp,-3.94bp) and (3.94bp,0bp) .. (8.8bp,0bp) -- (44.01bp,0bp) .. controls (48.87bp,0bp) and (52.81bp,-3.94bp) .. (52.81bp,-8.8bp) -- (52.81bp,-47.75bp) .. controls (52.81bp,-52.61bp) and (48.87bp,-56.55bp) .. (44.01bp,-56.55bp) -- (8.8bp,-56.55bp) .. controls (3.94bp,-56.55bp) and (0bp,-52.61bp) .. (0bp,-47.75bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{36.19bp}(51.0bp,110.8bp)
    \vbox to 9.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\ttfamily\fontseries{w500}\selectfont \color{black}\vrule width0bp height4.88bp depth0bp\relax%
      Short label
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{52.8bp}(105.8bp,71.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (52.81bp,-56.55bp);
      \path[fill=b2sC3ECF6,draw=black,line width=0.94bp,shift={(0bp,0bp)}] (0bp,-8.8bp) .. controls (0bp,-3.94bp) and (3.94bp,0bp) .. (8.8bp,0bp) -- (44.01bp,0bp) .. controls (48.87bp,0bp) and (52.81bp,-3.94bp) .. (52.81bp,-8.8bp) -- (52.81bp,-47.75bp) .. controls (52.81bp,-52.61bp) and (48.87bp,-56.55bp) .. (44.01bp,-56.55bp) -- (8.8bp,-56.55bp) .. controls (3.94bp,-56.55bp) and (0bp,-52.61bp) .. (0bp,-47.75bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{36.18bp}(114.1bp,110.8bp)
    \vbox to 9.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\ttfamily\fontseries{w500}\selectfont \color{black}\vrule width0bp height4.88bp depth0bp\relax%
      Short label
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{52.8bp}(168.8bp,71.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (52.81bp,-56.55bp);
      \path[fill=b2sC3ECF6,draw=black,line width=0.94bp,shift={(0bp,0bp)}] (0bp,-8.8bp) .. controls (0bp,-3.94bp) and (3.94bp,0bp) .. (8.8bp,0bp) -- (44.01bp,0bp) .. controls (48.87bp,0bp) and (52.81bp,-3.94bp) .. (52.81bp,-8.8bp) -- (52.81bp,-47.75bp) .. controls (52.81bp,-52.61bp) and (48.87bp,-56.55bp) .. (44.01bp,-56.55bp) -- (8.8bp,-56.55bp) .. controls (3.94bp,-56.55bp) and (0bp,-52.61bp) .. (0bp,-47.75bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{36.18bp}(177.2bp,110.8bp)
    \vbox to 9.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\ttfamily\fontseries{w500}\selectfont \color{black}\vrule width0bp height4.88bp depth0bp\relax%
      Short label
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{52.8bp}(295.0bp,71.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (52.81bp,-56.55bp);
      \path[fill=b2sC3ECF6,draw=black,line width=0.94bp,shift={(0bp,0bp)}] (0bp,-8.8bp) .. controls (0bp,-3.94bp) and (3.94bp,0bp) .. (8.8bp,0bp) -- (44.01bp,0bp) .. controls (48.87bp,0bp) and (52.81bp,-3.94bp) .. (52.81bp,-8.8bp) -- (52.81bp,-47.75bp) .. controls (52.81bp,-52.61bp) and (48.87bp,-56.55bp) .. (44.01bp,-56.55bp) -- (8.8bp,-56.55bp) .. controls (3.94bp,-56.55bp) and (0bp,-52.61bp) .. (0bp,-47.75bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{36.18bp}(303.3bp,110.8bp)
    \vbox to 9.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\ttfamily\fontseries{w500}\selectfont \color{black}\vrule width0bp height4.88bp depth0bp\relax%
      Short label
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{52.8bp}(358.1bp,71.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (52.81bp,-56.55bp);
      \path[fill=b2sC3ECF6,draw=black,line width=0.94bp,shift={(0bp,0bp)}] (0bp,-8.8bp) .. controls (0bp,-3.94bp) and (3.94bp,0bp) .. (8.8bp,0bp) -- (44.01bp,0bp) .. controls (48.87bp,0bp) and (52.81bp,-3.94bp) .. (52.81bp,-8.8bp) -- (52.81bp,-47.75bp) .. controls (52.81bp,-52.61bp) and (48.87bp,-56.55bp) .. (44.01bp,-56.55bp) -- (8.8bp,-56.55bp) .. controls (3.94bp,-56.55bp) and (0bp,-52.61bp) .. (0bp,-47.75bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{36.19bp}(366.4bp,110.8bp)
    \vbox to 9.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\ttfamily\fontseries{w500}\selectfont \color{black}\vrule width0bp height4.88bp depth0bp\relax%
      Short label
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{52.8bp}(231.9bp,71.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (52.80bp,-56.55bp);
      \path[fill=b2sC3ECF6,draw=black,line width=0.94bp,shift={(0bp,0bp)}] (0bp,-8.8bp) .. controls (0bp,-3.94bp) and (3.94bp,0bp) .. (8.8bp,0bp) -- (44bp,0bp) .. controls (48.86bp,0bp) and (52.8bp,-3.94bp) .. (52.8bp,-8.8bp) -- (52.8bp,-47.75bp) .. controls (52.8bp,-52.61bp) and (48.86bp,-56.55bp) .. (44bp,-56.55bp) -- (8.8bp,-56.55bp) .. controls (3.94bp,-56.55bp) and (0bp,-52.61bp) .. (0bp,-47.75bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{36.19bp}(240.2bp,110.8bp)
    \vbox to 9.0bp{\slidesbox
    \vskip0.00bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\ttfamily\fontseries{w500}\selectfont \color{black}\vrule width0bp height4.88bp depth0bp\relax%
    [... 125 further lines of the same forms as above ...]
  \end{textblock*}
  \begin{textblock*}{188.74bp}(42.2bp,28.5bp)
    \vbox to 17.9bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{14.49}\adoptfontA\fontseries{w600}\selectfont \color{black}\vrule width0bp height14.03bp depth0bp\relax%
      Group chart
    \baselineskip=17.48bp\par}
    \vskip\dimexpr3.36bp-\prevdepth\relax
    \vskip0.00bp}
  \end{textblock*}
\end{frame}
```


### Pair f24

The two sources are the same slide written two ways.


**f24 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{body}{size=15.12, color=black, ascent=14.64, pitch=20.87, depth=6.23}
\slidestyle{heading-microsoftjhenghe}{size=22.68, face=\adoptfontA, color=black, ascent=21.95, pitch=31.3, depth=9.34}
\slidestyle{label}{size=22.68, color=black}
\slidestyle{heading-microsoftjhenghe-22.7}{size=22.68, face=\adoptfontA, color=black, ascent=21.95, pitch=40.82, depth=5.26}
\slidestyle{small-microsoftjhenghe}{size=11.34, face=\adoptfontA, color=black, ascent=10.98, pitch=13.7, depth=2.63}
\setslidepar{style=body}
\setslidelist{itemize}{1}{style=heading-microsoftjhenghe-22.7,indent=22.68,labelstyle=label,label={★},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=區段標題]
  \frametitle{Google Slides}
  \begin{slidebox}{13.4,79.6,419.16,159.9}
    \begin{itemize}[style=heading-microsoftjhenghe]
      \item \uline{研究(Research)功能}
      \item \uline{通知與註解}
      \item \uline{文字方塊與投影片編輯}
      \item \uline{分享簡報檔案}
      \item \uline{嵌入Youtube影片}
    \end{itemize}
  \end{slidebox}
  \slidepicture[trim=185.74 0 4.33 0]{4.6,5.2,53.8,63.6}{figures/picture-8edd8859.png}
  \slidetext[middle]{245.1,86.6,404.49,23}{small-microsoftjhenghe}{\href{https://drive.google.com/file/d/0B6VPO_ULtz0RSkRYYl9sd1VkRDQ/view}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RSkRYYl9sd1VkRDQ/view}}}
  \slidetext[middle]{230.4,211.3,466.97,20.8}{small-microsoftjhenghe}{\href{https://drive.google.com/file/d/0B6VPO_ULtz0RNlVRWDBOTlJWdkk/view?usp=sharing}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RNlVRWDBOTlJWdkk/view?usp=sharing}}}
\end{frame}
```


**f24 source B** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2sE91D63}{HTML}{E91D63}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{453.5bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (453.54bp,-73.82bp);
      \path[fill=b2sE91D63,shift={(0bp,0bp)}] (0bp,0bp) -- (453.54bp,0bp) -- (453.54bp,-73.82bp) -- (0bp,-73.82bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{402.42bp}(27.2bp,12.2bp)
    \vbox to 44.8bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontC\color{white}\vrule width0bp height21.95bp depth0bp\relax%
      Google Slides
    \baselineskip=27.40bp\par}
    \vskip\dimexpr5.26bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{419.16bp}(13.4bp,79.6bp)
    \vbox to 159.9bp{\slidesbox
    \vskip4.08bp
    {\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      \uline{研究(Research)功能}
    \baselineskip=31.30bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      \uline{通知與註解}
    \baselineskip=31.30bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      \uline{文字方塊與投影片編輯}
    \baselineskip=31.30bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      \uline{分享簡報檔案}
    \baselineskip=31.30bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=22.68bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\adoptfontA\color{black}\vrule width0bp height21.95bp depth0bp\relax\llap{{\slidesize{22.68}\color{black}★}\hskip12.54bp}%
      \uline{嵌入Youtube影片}
    \baselineskip=31.30bp\par}
    \vskip\dimexpr9.34bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{53.8bp}(4.6bp,5.2bp)
    \includegraphics[trim=185.74 0.00 4.33 0.00,clip,width=53.8bp,height=63.6bp]{figures/picture-8edd8859.png}
  \end{textblock*}
  \begin{textblock*}{404.49bp}(245.1bp,86.6bp)
    \vbox to 23.0bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\adoptfontA\color{black}\vrule width0bp height10.98bp depth0bp\relax%
      \href{https://drive.google.com/file/d/0B6VPO_ULtz0RSkRYYl9sd1VkRDQ/view}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RSkRYYl9sd1VkRDQ/view}}
    \baselineskip=13.70bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{466.97bp}(230.4bp,211.3bp)
    \vbox to 20.8bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\adoptfontA\color{black}\vrule width0bp height10.98bp depth0bp\relax%
      \href{https://drive.google.com/file/d/0B6VPO_ULtz0RNlVRWDBOTlJWdkk/view?usp=sharing}{\uline{https://drive.google.com/file/d/0B6VPO\_ULtz0RNlVRWDBOTlJWdkk/view?usp=sharing}}
    \baselineskip=13.70bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


### Pair f25

The two sources are the same slide written two ways.


**f25 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s2388DB}{HTML}{2388DB}
```


The frame:

```latex
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
      Backgrounds
    \baselineskip=21.92bp\par}
    \vskip\dimexpr4.21bp-\prevdepth\relax
    \vskip3.27bp}
  \end{textblock*}
  \begin{textblock*}{319.85bp}(21.5bp,63.5bp)
    \vbox to 197.1bp{\slidesbox
    \vskip3.27bp
    \vskip3.02bp{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Otherwise, if you are using images with transparency (such as PNG files) you can change the slide background to any color or any image.
    \baselineskip=18.14bp\par}
    \prevdepth=\dimexpr\prevdepth+0.00bp\relax{\leftskip=18.14bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{15.12}\color{black}\vrule width0bp height14.64bp depth0bp\relax\llap{\tikz[baseline=-0.91bp]\path[fill=black] (3.12bp,3.12bp) circle[radius=3.12bp];\hskip10.28bp}%
      Click \symbol{34}Slide\symbol{34}, then \symbol{34}Background\symbol{34}, then \symbol{34}Color\symbol{34} or \symbol{34}Image\symbol{34}.
    \baselineskip=18.14bp\par}
    \vskip\dimexpr3.51bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f25 source B** (vocabulary V2)


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


### Pair f26

The two sources are the same slide written two ways.


**f26 source A** (vocabulary V1)


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{26.2bp}(420.8bp,221.6bp)
    \includegraphics[width=26.2bp,height=26.2bp]{figures/picture-2c99a5c9.png}
  \end{textblock*}
  \begin{textblock*}{414.13bp}(19.7bp,12.5bp)
    \vbox to 28.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{10.77}\bfseries \color{black}\vrule width0bp height10.43bp depth0bp\relax%
      Would you prefer reading the Solidity documentation in your native language or English?
    \baselineskip=12.76bp\par}
    \vskip\dimexpr2.50bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{410.89bp}(19.7bp,228.2bp)
    \vbox to 15.3bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{5.04}\color{black}\vrule width0bp height4.88bp depth0bp\relax%
      n = 510
    \baselineskip=6.05bp\par}
    \vskip\dimexpr1.17bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{315.3bp}(69.1bp,40.9bp)
    \includegraphics[width=315.3bp,height=195.0bp]{figures/chart-014c5378.png}
  \end{textblock*}
\end{frame}
```


**f26 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{tiny-5}{size=5.04, color=black, ascent=4.88, pitch=6.05, depth=1.17}
\slidestyle{small-bold}{size=10.77, weight=bold, color=black, ascent=10.43, pitch=12.76, depth=2.5}
\setslidepar{style=tiny-5}
```


The frame:

```latex
\begin{frame}[plain,layout=title-only]
  \slidetext[middle]{19.7,12.5,414.13,28.4}{small-bold}{Would you prefer reading the Solidity documentation in your native language or English?}
  \slidetext{19.7,228.2,410.89,15.3}{tiny-5}{n = 510}
  \slidepicture{69.1,40.9,315.3,195}{figures/chart-014c5378.png}
\end{frame}
```


### Pair f27

The two sources are the same slide written two ways.


**f27 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s0079AB}{HTML}{0079AB}
\definecolor{b2s595959}{HTML}{595959}
\definecolor{b2s9CAE4B}{HTML}{9CAE4B}
\definecolor{b2sC98D4B}{HTML}{C98D4B}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{8.4bp}(260.8bp,127.1bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (8.38bp,-8.38bp);
      \path[fill=b2s9CAE4B,draw=b2s9CAE4B,line width=1.89bp,shift={(0bp,0bp)}] (4.19bp,-4.19bp) ellipse [x radius=4.19bp, y radius=4.19bp];
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{75.10bp}(374.3bp,240.1bp)
    \vbox to 15.1bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\fontseries{w300}\selectfont \color{b2s595959}\vrule width0bp height6.10bp depth0bp\relax%
      11
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{453.5bp}(0.0bp,0.0bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (453.54bp,-28.41bp);
      \path[fill=b2s9CAE4B,draw=b2s595959,line width=0.47bp,shift={(0bp,0bp)}] (0bp,-4.74bp) .. controls (0bp,-2.12bp) and (2.12bp,0bp) .. (4.74bp,0bp) -- (448.8bp,0bp) .. controls (451.42bp,0bp) and (453.54bp,-2.12bp) .. (453.54bp,-4.74bp) -- (453.54bp,-23.67bp) .. controls (453.54bp,-26.29bp) and (451.42bp,-28.41bp) .. (448.8bp,-28.41bp) -- (4.74bp,-28.41bp) .. controls (2.12bp,-28.41bp) and (0bp,-26.29bp) .. (0bp,-23.67bp) -- cycle;
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{445.17bp}(4.2bp,0.0bp)
    \vbox to 28.4bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\fontseries{w800}\selectfont \color{white}\vrule width0bp height10.98bp depth0bp\relax%
      Pipeline
    \baselineskip=13.70bp\par}
    \vskip\dimexpr2.63bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{35.59bp}(111.4bp,117.0bp)
    \vbox to 12.2bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Feasibility
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{35.59bp}(193.4bp,117.0bp)
    \vbox to 12.2bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Prototype
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{35.58bp}(256.9bp,117.0bp)
    \vbox to 12.2bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Pilot
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{35.58bp}(34.6bp,117.0bp)
    \vbox to 12.2bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\bfseries \color{black}\vrule width0bp height6.10bp depth0bp\relax%
      Discovery
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{68.4bp}(38.8bp,131.3bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (68.43bp,-0.01bp);
      \path[draw=b2s0079AB,line width=1.89bp] (68.43bp,0bp) -- (0bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{68.4bp}(115.6bp,131.3bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (68.44bp,-0.01bp);
      \path[draw=b2s9CAE4B,line width=1.89bp] (68.44bp,0bp) -- (0bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{68.4bp}(269.2bp,131.3bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (68.43bp,-0.01bp);
      \path[draw=b2sC98D4B,line width=1.89bp] (0bp,0bp) -- (68.43bp,0bp);
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{8.4bp}(30.4bp,127.1bp)
    \begin{tikzpicture}[baseline=(current bounding box.north),inner sep=0bp,outer sep=0bp]
      \useasboundingbox (0bp,0bp) rectangle (8.38bp,-8.38bp);
      \path[fill=b2s0079AB,draw=b2s0079AB,line width=1.89bp,shift={(0bp,0bp)}] (4.19bp,-4.19bp) ellipse [x radius=4.19bp, y radius=4.19bp];
    \end{tikzpicture}
  \end{textblock*}
  \begin{textblock*}{1.01bp}(34.6bp,127.1bp)
    [... 140 further lines of the same forms as above ...]
  \end{textblock*}
  \begin{textblock*}{311.74bp}(32.4bp,219.1bp)
    \vbox to 19.8bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{8.82}\fontseries{w500}\selectfont \color{black}\vrule width0bp height8.54bp depth0bp\relax%
      Go ahead and make a funnel, too. If you like that sort of thing.
    \baselineskip=10.58bp\par}
    \vskip\dimexpr2.05bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f27 source B** (vocabulary V2)


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


### Pair f28

The two sources are the same slide written two ways.


**f28 source A** (vocabulary V1)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{b2s595959}{HTML}{595959}
```


The frame:

```latex
\begin{frame}[plain]
  \begin{textblock*}{445.17bp}(4.2bp,28.4bp)
    \vbox to 226.7bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax% blank line
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-10.08bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\bfseries \color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      Product companies: customer segments
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-10.08bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\fontseries{w500}\selectfont \color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      Do it yourself
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-10.08bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\fontseries{w500}\selectfont \color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      Make the supply chain do it
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-10.08bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\fontseries{w500}\selectfont \color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      X-as-a-service
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-10.08bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\bfseries \color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      Information companies: customer segments
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-10.08bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\fontseries{w500}\selectfont \color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      Monitoring - nowcasting
    \baselineskip=15.65bp\par}
    \prevdepth=\dimexpr\prevdepth-10.08bp\relax{\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\fontseries{w500}\selectfont \color{b2s595959}\vrule width0bp height10.98bp depth0bp\relax%
      Change prediction - forecasting
    \baselineskip=15.65bp\par}
    \vskip\dimexpr4.67bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{75.10bp}(374.3bp,240.1bp)
    \vbox to 15.1bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\fontseries{w300}\selectfont \color{b2s595959}\vrule width0bp height6.10bp depth0bp\relax%
      23
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
\end{frame}
```


**f28 source B** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\definecolor{DarkGrey}{HTML}{595959}
\setslideinset{4.08}
\slidestyle{tiny-light-darkgrey}{size=6.3, weight=w300, color=DarkGrey, ascent=6.1, pitch=7.56, depth=1.46}
\slidestyle{large-medium-darkgrey}{size=11.34, weight=w500, color=DarkGrey, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{small-darkgrey-9.4}{size=9.45, color=DarkGrey, ascent=9.15, pitch=13.04, depth=3.89}
\slidestyle{tiny-medium-5}{size=5.04, weight=w500, color=black, ascent=4.88, pitch=6.05, depth=1.17}
\slidestyle{large-darkgrey}{size=11.34, color=DarkGrey, ascent=10.98, pitch=15.65, depth=4.67}
\slidestyle{body-light}{size=10.08, weight=w300, color=black, ascent=9.76, pitch=12.28, depth=2.34}
\slidestyle{label}{size=10.08, color=black}
\slidestyle{large-bold-darkgrey}{size=11.34, weight=bold, color=DarkGrey, ascent=10.98, pitch=15.65, depth=4.67}
\slidemark{dot-darkgrey}{\tikz[baseline=-0.68bp]\path[fill=DarkGrey] (2.34bp,2.34bp) circle[radius=2.34bp];}
\slidemark{ring-darkgrey}{\tikz[baseline=-0.66bp]\path[draw=DarkGrey,line width=0.57bp] (2.03bp,2.03bp) circle[radius=1.75bp];}
\setslidepar{style=tiny-medium-5}
\setslidelist{itemize}{1}{style=large-darkgrey,indent=22.68,mark=dot-darkgrey,gap=12.25}
\setslidelist{itemize}{2}{style=small-darkgrey-9.4,indent=45.35,mark=ring-darkgrey,gap=12.09}
\setslidelist{enumerate}{1}{style=body-light,indent=22.68,labelstyle=label,label={\arabic*.},gap=12.54}
```


The frame:

```latex
\begin{frame}[plain,layout=blank,nonumber]
  \begin{slidebox}[style=large-medium-darkgrey,space=10.08]{4.2,28.4,445.17,226.7}
    \slidepar[style=large-darkgrey,space=0]{}
    \slidepar[style=large-bold-darkgrey]{Product companies: customer segments}
    \slidepar{Do it yourself}
    \slidepar{Make the supply chain do it}
    \slidepar{X-as-a-service}
    \slidepar[style=large-bold-darkgrey]{Information companies: customer segments}
    \slidepar{Monitoring - nowcasting}
    \slidepar{Change prediction - forecasting}
  \end{slidebox}
  \slidetext[middle,right]{374.3,240.1,75.1,15.1}{tiny-light-darkgrey}{23}
\end{frame}
```


### Pair f29

The two sources are the same slide written two ways.


**f29 source A** (vocabulary V2)


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


**f29 source B** (vocabulary V1)


The frame:

```latex
}
{\setbeamercolor{background canvas}{bg=black}
\begin{frame}[plain]
  \begin{textblock*}{25.68bp}(43.2bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Developer
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.67bp}(85.1bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Write
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.67bp}(127.3bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Cloud
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.67bp}(172.0bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Audio
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.67bp}(213.0bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Key
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.67bp}(255.7bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Desktop Mac
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.67bp}(297.9bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Watch
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.67bp}(342.5bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Person
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.68bp}(384.7bp,93.5bp)
    \vbox to 9.1bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
      Car
    \baselineskip=2.27bp\par}
    \vskip\dimexpr0.44bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{25.68bp}(43.2bp,124.5bp)
    \vbox to 9.0bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{1.89}\adoptfontA\color{white}\vrule width0bp height1.83bp depth0bp\relax%
    [... 631 further lines of the same forms as above ...]
  \end{textblock*}
  \begin{textblock*}{188.74bp}(42.2bp,32.8bp)
    \vbox to 17.9bp{\slidesbox
    \vss
    {\leftskip=0.00bp\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{14.49}\adoptfontA\color{white}\vrule width0bp height14.03bp depth0bp\relax%
      Icons
    \baselineskip=17.48bp\par}
    \vskip\dimexpr3.36bp-\prevdepth\relax
    \vskip0.00bp}
  \end{textblock*}
\end{frame}
```


### Pair f30

The two sources are the same slide written two ways.


**f30 source A** (vocabulary V2)


Its deck's preamble, the part this frame leans on:

```latex
\setslideinset{4.08}
\slidestyle{title-22.7}{size=22.68, color=black, ascent=21.95, pitch=27.4, depth=5.26}
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
\begin{frame}[plain,layout=section-header]
  \slidetext[middle,center]{19.7,106.7,414.24,41.8}{title-22.7}{Forward Secrecy}
  \slidetext[center]{29.6,224.2,394.36,28.4}{large}{Textbook Chapter 31.1}
  \note{45 min - 1:25

That’s the full TLS handshake in all its gory detail. Qs? This is definitely one of our densest topics–it’s going to take a couple run throughs to fully digest.}
\end{frame}
```


**f30 source B** (vocabulary V1)


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
  \begin{textblock*}{414.24bp}(19.7bp,106.7bp)
    \vbox to 41.8bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{22.68}\color{black}\vrule width0bp height21.95bp depth0bp\relax%
      Forward Secrecy
    \baselineskip=27.40bp\par}
    \vskip\dimexpr5.26bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{18.79bp}(424.5bp,231.3bp)
    \vbox to 19.5bp{\slidesbox
    \vss
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp\relax\parfillskip=0bp\relax
      \noindent\slidesize{6.30}\color{b2s595959}\vrule width0bp height6.10bp depth0bp\relax%
      17
    \baselineskip=7.56bp\par}
    \vskip\dimexpr1.46bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \begin{textblock*}{394.36bp}(29.6bp,224.2bp)
    \vbox to 28.4bp{\slidesbox
    \vskip4.08bp
    {\leftskip=0.00bp plus 1fil\relax\rightskip=0.00bp plus 1fil\relax\parfillskip=0bp\relax
      \noindent\slidesize{11.34}\color{black}\vrule width0bp height10.98bp depth0bp\relax%
      Textbook Chapter 31.1
    \baselineskip=15.65bp\par}
    \vskip\dimexpr4.67bp-\prevdepth\relax
    \vss}
  \end{textblock*}
  \note{45 min - 1:25

That’s the full TLS handshake in all its gory detail. Qs? This is definitely one of our densest topics–it’s going to take a couple run throughs to fully digest.}
\end{frame}
```


Answer with the JSON list described above, one object per pair, nothing else.
