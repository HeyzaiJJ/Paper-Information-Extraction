from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.preprocess.pdf_figures import _figure_regions


def _page3_payload(*children):
    return {
        "id": "/document",
        "block_type": "Document",
        "children": [{
            "id": "/page/3/Page/485",
            "block_type": "Page",
            "page_id": 3,
            "bbox": [0, 0, 596, 794],
            "children": list(children),
        }],
    }


def _caption(number, bbox, block_id=None):
    block_id = block_id or f"/page/3/Caption/{number}"
    return {
        "id": block_id,
        "block_type": "Caption",
        "bbox": bbox,
        "html": f"<p>Fig. {number}. Caption {number}.</p>",
    }


def _figure(block_id, bbox):
    return {
        "id": block_id,
        "block_type": "Figure",
        "bbox": bbox,
        "images": {block_id: "/9j/fake-image"},
    }


def test_ambiguous_figure_group_uses_distinct_nearest_visuals():
    # This mirrors the affected article: Marker put Fig. 3 and Fig. 4
    # captions in one group that explicitly references only the Fig. 4 image.
    payload = _page3_payload(
        _figure("/page/3/Figure/2", [32, 62, 279, 271]),
        {
            "id": "/page/3/FigureGroup/484",
            "block_type": "FigureGroup",
            "bbox": [29, 277, 284, 537],
            "html": (
                "<content-ref src='/page/3/Caption/3'></content-ref>"
                "<content-ref src='/page/3/Figure/4'></content-ref>"
                "<content-ref src='/page/3/Caption/5'></content-ref>"
            ),
            "children": [
                _caption(3, [29, 277, 284, 296]),
                _figure("/page/3/Figure/4", [32, 314, 279, 511]),
                _caption(4, [29, 518, 284, 537], "/page/3/Caption/5"),
            ],
        },
    )

    regions = _figure_regions(json.dumps(payload))
    by_id = {item["id"]: item for item in regions}

    assert set(by_id) == {"fig3", "fig4"}
    assert by_id["fig3"]["bbox"] == (32.0, 62.0, 279.0, 271.0)
    assert by_id["fig4"]["bbox"] == (32.0, 314.0, 279.0, 511.0)
    assert by_id["fig3"]["bbox"] != by_id["fig4"]["bbox"]


def test_one_to_one_figure_group_keeps_group_horizontal_bounds():
    payload = _page3_payload(
        {
            "id": "/page/3/FigureGroup/1",
            "block_type": "FigureGroup",
            "bbox": [20, 200, 280, 430],
            "html": (
                "<content-ref src='/page/3/Caption/1'></content-ref>"
                "<content-ref src='/page/3/Figure/1'></content-ref>"
            ),
            "children": [
                _caption(1, [25, 410, 275, 430]),
                _figure("/page/3/Figure/1", [32, 220, 250, 400]),
            ],
        },
    )

    regions = _figure_regions(json.dumps(payload))

    assert regions == [{
        "id": "fig1",
        "page": 3,
        "bbox": (20.0, 220.0, 280.0, 400.0),
    }]


def test_visual_is_not_reused_when_multiple_captions_compete():
    payload = _page3_payload(
        _caption(1, [30, 200, 280, 220]),
        _caption(2, [30, 300, 280, 320]),
        _figure("/page/3/Figure/1", [32, 100, 279, 190]),
    )

    regions = _figure_regions(json.dumps(payload))

    assert [item["id"] for item in regions] == ["fig1"]


def test_equal_distance_candidates_are_left_unassigned():
    payload = _page3_payload(
        _caption(1, [30, 200, 280, 220]),
        _figure("/page/3/Figure/1", [32, 100, 279, 190]),
        _figure("/page/3/Figure/2", [32, 230, 279, 320]),
    )

    assert _figure_regions(json.dumps(payload)) == []
