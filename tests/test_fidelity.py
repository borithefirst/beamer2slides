import numpy as np

from beamer2slides.fidelity import ink_box


def test_rows_of_specks_thinner_than_a_column_have_no_ink_box():
    # Four rows of 4 px each, every pixel in a column of its own: rows count, columns do not
    # (found by the visual hunt, r2_code_v1: fidelity died with an IndexError).
    mask = np.zeros((10, 40), dtype=bool)
    for r in range(4):
        mask[2 + r, [r * 8 + k * 2 for k in range(4)]] = True
    assert ink_box(mask) is None


def test_ink_box_of_a_block():
    mask = np.zeros((10, 20), dtype=bool)
    mask[2:6, 3:9] = True
    assert tuple(int(v) for v in ink_box(mask)) == (3, 2, 9, 6)
