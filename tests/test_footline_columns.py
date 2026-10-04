"""A footline the deck does not repeat whole (a page read alone, or a deck of two frames) is still
no paragraph of the column above it: synthetic pages modelled on real_africa-remote-sens-30 (a
public Russian talk, never republished), whose \\tiny body sits over a 5 pt custom footline and
whose overfull blocks run down over it. `classify.furniture` finds that footline only on a deck
where it repeats; here no furniture is known, as on a page classified alone."""

from beamer2slides.classify import PageClassifier
from beamer2slides.classify_lines import LinesMixin

from .test_tiny_body_deck import FOOTLINE, SANS, Page, deck, elements, texts, words_of

AUTHOR = "Леменкова П. А."


def column_beside(page: Page, x0: float, first: float, lines: int) -> None:
    """A right-hand column of 6 pt prose (the deck's body size)."""
    for k in range(lines):
        page.words("Излучина Кена особенность геоморфологии южного", x0, first + 7.0 * k, 6.0)


def footline(page: Page, number: str) -> None:
    page.words(FOOTLINE, 28.35, 268.54, 4.98)
    page.words(AUTHOR, 223.3, 268.54, 4.98)
    page.span(number, 328.0, 268.54, 4.98, SANS)


def paragraphs(page: Page) -> list[list[str]]:
    return [words_of(e) for e in texts(elements(deck([page]), 0))]


def test_a_footline_well_below_a_list_of_larger_items_is_its_own_box():
    # (5 pt footline words 2.1 em of the 8 pt items under the list's last line: the 2.6 em boxes
    # allow at the list's size took it as the list's last paragraph, which rode down with the
    # list's reflow and was cut by the page's edge - slide 3)
    page = Page(2)
    page.words("Введение", 8.5, 23.7, 10.91)
    for k, item in enumerate(["Опустынивание на севере", "Изменение климата и засухи", "Вырубка тропических лесов",
                              "Утрата биоразнообразия", "Загрязнения воздуха и почвы"]):
        baseline = 213.6 + 9.48 * k
        page.span("•", 20.5, baseline, 7.97, SANS)
        page.words(item, 28.35, baseline, 7.97)
    column_beside(page, 180.0, 60.0, 12)
    footline(page, "3")
    found = paragraphs(page)
    items = next(p for p in found if p[0].startswith("Опустынивание"))
    assert len(items) == 5 and not any("Институт" in w for w in items), found
    assert [FOOTLINE] in found


def test_an_overfull_list_over_the_footline_keeps_its_words_apart():
    # (the left column's last item runs down over the footline a point lower, the right
    # column's last line a point higher over the author and the frame number: the rows were
    # read as one line each, 'Институтскриптыдля...', and the frame number '24' took the 'С'
    # it covers into a picture - slides 24, 28, 36, 39, 42)
    page = Page(23)
    page.words("Египет", 8.5, 23.7, 10.91)
    for k, line in enumerate(["Скрипты GMT для моделирования", "изменения покрова в зависимости",
                              "от геологии региона Кена"]):
        page.words(line, 35.8, 230.0 + 7.0 * k, 5.98)
    page.span("•", 28.4, 255.6, 5.98, SANS)
    page.words("Методы сопоставления карт на", 35.8, 255.6, 5.98)
    page.words("основе данных и скрипты для", 35.8, 262.58, 5.98)
    page.words("скрипты для автоматизированного", 35.8, 269.55, 5.98)
    column_beside(page, 177.2, 225.0, 5)
    words = page.words("структурном отношении регионов долины Нила.", 210.0, 267.54, 5.98)
    page.span("С", words[-1]["bbox"][2] + 0.33 * 5.98, 267.54, 5.98, SANS)
    footline(page, "24")  # (the frame number at 328 pt, under the 'С')
    found = paragraphs(page)
    assert [FOOTLINE] in found and [AUTHOR] in found and ["24"] in found, found
    left = next(p for p in found if p[0].startswith("Методы"))
    assert left[-1].endswith("основе данных и скрипты для скрипты для автоматизированного"), left
    right = next(p for p in found if any("Нила." in w for w in p))
    assert right[-1].endswith("структурном отношении регионов долины Нила. С"), right


def test_words_of_two_sizes_side_by_side_are_one_line():
    # (overprinted_rows asks for a word covering a word: a \small aside on the line's baseline,
    # or a script of one size stacked over another, is no second row)
    page = Page(0)
    page.words("Слово крупно", 40.0, 120.0, 6.0)
    page.words("и мелкий текст рядом", 80.0, 120.0, 4.98)
    page.span("ab", 140.0, 117.5, 4.2, SANS)
    page.span("cd", 140.0, 121.6, 4.2, SANS)
    spans = PageClassifier(page.raw(), 6.0).spans()
    assert LinesMixin.overprinted_rows(spans) == [None] * len(spans)
    # (and the same aside printed a point under the words it covers is a row of its own)
    page.words("и мелкий текст", 42.0, 121.0, 4.98)
    spans = PageClassifier(page.raw(), 6.0).spans()
    rows = LinesMixin.overprinted_rows(spans)
    assert rows[0] is not None and rows[0] == rows[1] and rows[-1] is not None and rows[-1] != rows[0]
