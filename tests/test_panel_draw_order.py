"""Two panels painted on one box are stacked as the PDF draws them (real_slide-20250221, never
republished: a #fafafa frame background painted over a white one 0.02 pt smaller came out under
it, and the white one covered the page ground)."""

from .test_tiny_body_deck import Page, deck, elements, shapes


def test_panels_on_one_box_keep_the_pdfs_draw_order():
    page = Page(0)
    page.words("A frame title", 8.5, 24.0, 10.91)
    page.fill((20.01, 60.01, 339.99, 239.99), "#ffffff", 1.0)
    page.fill((20.0, 60.0, 340.0, 240.0), "#fafafa", 1.0)  # (drawn after, 0.02 pt larger)
    page.words("Words on the frame's background panel", 40.0, 120.0, 10.0)
    panels = [s["fill"] for s in shapes(elements(deck([page]), 0)) if s["role"] == "panel"]
    assert panels == ["#ffffff", "#fafafa"], panels
