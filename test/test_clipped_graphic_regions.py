import fitz

from paper_agent.paper_summary import _page_graphic_regions, _visual_rect_for_caption_direction


class ClippedPage:
    rect = fitz.Rect(0, 0, 612, 792)

    def get_text(self, *args, **kwargs):
        return {"blocks": []}

    def get_drawings(self, extended=False):
        assert extended
        return [
            {"type": "group", "level": 0, "rect": self.rect},
            {"type": "clip", "level": 1, "scissor": fitz.Rect(283, 288, 481, 384)},
            {"type": "group", "level": 2, "rect": fitz.Rect(222, -44, 605, 498)},
            {"type": "f", "level": 3, "rect": fitz.Rect(222, -44, 605, 498)},
            {"type": "clip", "level": 3, "scissor": fitz.Rect(290, 10, 310, 30)},
            {"type": "f", "level": 4, "rect": fitz.Rect(290, 10, 310, 30)},
            {"type": "f", "level": 2, "rect": fitz.Rect(300, 310, 340, 355)},
            {"type": "f", "level": 1, "rect": fitz.Rect(100, 600, 160, 650)},
        ]


def test_graphic_bounds_respect_nested_clips_and_restore_siblings():
    regions = _page_graphic_regions(ClippedPage())
    assert regions == [fitz.Rect(283, 288, 481, 384), fitz.Rect(300, 310, 340, 355),
                       fitz.Rect(100, 600, 160, 650)]


def test_figure_above_caption_ignores_invisible_out_of_page_geometry():
    region = _visual_rect_for_caption_direction(
        ClippedPage(), fitz.Rect(283, 397, 481, 430), [], "above",
    )
    assert region is not None
    assert region.y0 == 288
    assert region.y1 == 384
    assert region.x0 == 283


def test_legacy_page_drawing_api_remains_supported():
    class LegacyPage:
        rect = fitz.Rect(0, 0, 612, 792)

        def get_text(self, *args):
            return {"blocks": []}

        def get_drawings(self):
            return [{"rect": fitz.Rect(50, 50, 250, 150)}]

    assert _page_graphic_regions(LegacyPage()) == [fitz.Rect(50, 50, 250, 150)]
