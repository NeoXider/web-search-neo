"""A located tile must land on the widget, whatever resolution the picture came in at."""

from __future__ import annotations

from web_search_neo import captcha_actions

# The widget occupies 330x236 CSS pixels at (10, 20) on the page.
RECT = (10.0, 20.0, 330.0, 236.0)


def test_a_one_to_one_picture_puts_the_click_on_its_tile():
    box, click = captcha_actions._to_page([220, 118, 110, 118], RECT, [330, 236])
    assert box == [230.0, 138.0, 110.0, 118.0]
    assert click == {"x": 285.0, "y": 197.0}


def test_a_double_resolution_picture_still_lands_inside_the_widget():
    # The same widget captured at DPR 2 ships a 660x472 picture: one image pixel is half a
    # CSS pixel, and the answer must not fly off the page.
    box, click = captcha_actions._to_page([448, 240, 224, 240], RECT, [660, 472])
    assert box == [234.0, 140.0, 112.0, 120.0]
    assert RECT[0] <= click["x"] <= RECT[0] + RECT[2]
    assert RECT[1] <= click["y"] <= RECT[1] + RECT[3]


def test_a_missing_size_does_not_invent_a_scale():
    box, click = captcha_actions._to_page([8, 8, 16, 16], RECT, [])
    assert box == [18.0, 28.0, 16.0, 16.0]
    assert click == {"x": 26.0, "y": 36.0}
