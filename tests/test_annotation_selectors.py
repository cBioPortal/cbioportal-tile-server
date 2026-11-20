"""Write-time selector checks (app.annotation_selectors)."""

import pytest
from pydantic import ValidationError

from app import annotation_selectors
from app.annotation_selectors import SelectorError, check_selector
from app.annotations import (
    AnnotationBatchIn,
    AnnotationBatchUpdate,
    AnnotationBody,
    AnnotationIn,
    AnnotationOut,
    AnnotationTarget,
    AnnotationUpdate,
)


def svg(value: str) -> dict:
    return {"type": "SvgSelector", "value": value}


# Every form the viewer (and the assistant) writes, and what Annotorious
# serializes when a shape is edited in place.
VIEWER_SELECTORS = [
    # points: a circle of radius 0
    '<svg><circle cx="10.5" cy="20.25" r="0" /></svg>',
    '<svg><circle cx="10" cy="20" r="5"></circle></svg>',
    '<svg><ellipse cx="10" cy="20" rx="3" ry="4" /></svg>',
    '<svg><ellipse cx="10" cy="20" rx="3" ry="4"></ellipse></svg>',
    '<svg><line x1="0" y1="0" x2="10.5" y2="-3" /></svg>',
    '<svg><rect x="1" y="2" width="30" height="40" /></svg>',
    '<svg><polygon points="1,1 4,1 4,4" /></svg>',
    "<svg><polygon points='1,1 4,1 4,4'/></svg>",
    '<svg><polygon points="1,1 4,1 4,4"></polygon></svg>',
    '<svg><polyline points="1,1 4,1 4,4" /></svg>',
    '<svg><path d="M1,1 L4,1 L4,4 Z"></path></svg>',
    # holes / several parts
    '<svg><g><path fill-rule="evenodd" d="M 0,0 L 10,0 L 10,10 L 0,10 Z M 2,2 L 4,2 L 4,4 Z" />'
    '<path fill-rule="evenodd" d="M 20,20 L 30,20 L 30,30 Z" /></g></svg>',
    # the assistant's ``{x:g}`` numbers
    '<svg><rect x="1e+06" y="2.5e-05" width="3" height="4" /></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><polygon points="1,1 4,1 4,4"></polygon></svg>',
    '  <svg>\n  <polygon points="1 1, 4 1, 4 4" />\n</svg>  ',
]


@pytest.mark.parametrize("value", VIEWER_SELECTORS)
def test_the_viewers_shapes_are_accepted(value):
    assert check_selector(svg(value))


@pytest.mark.parametrize(
    "value",
    ["xywh=pixel:1,2,30,40", "xywh=1,2,30,40", "xywh=pixel:-1.5,2e3,30.25,.5"],
)
def test_fragment_rectangles_are_accepted(value):
    selector = {"type": "FragmentSelector", "conformsTo": "http://www.w3.org/TR/media-frags/", "value": value}
    assert check_selector(selector)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ('<svg><script>alert(1)</script></svg>', "element"),
        ('<svg onload="alert(1)"><polygon points="1,1 2,2 3,3" /></svg>', "attribute"),
        ('<svg><polygon onclick="alert(1)" points="1,1 2,2 3,3" /></svg>', "attribute"),
        ('<svg><polygon onmouseover="x" points="1,1 2,2 3,3" /></svg>', "attribute"),
        ('<svg><polygon style="fill:url(javascript:x)" points="1,1 2,2 3,3" /></svg>', "attribute"),
        ('<svg><polygon class="x" points="1,1 2,2 3,3" /></svg>', "attribute"),
        ('<svg><a href="javascript:alert(1)"><circle cx="1" cy="1" r="1" /></a></svg>', "element"),
        ('<svg><polygon href="javascript:x" points="1,1 2,2 3,3" /></svg>', "attribute"),
        (
            '<svg xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<polygon xlink:href="javascript:x" points="1,1 2,2 3,3" /></svg>',
            "namespaced_attribute",
        ),
        ('<svg><foreignObject><div>x</div></foreignObject></svg>', "element"),
        ('<svg><image href="x" /></svg>', "element"),
        ('<svg><use href="#x" /></svg>', "element"),
        ('<svg><style>*{}</style></svg>', "element"),
        ('<svg><animate attributeName="x" /></svg>', "element"),
        ('<svg><polygon points="1,1 2,2 3,3" /><!-- x --></svg>', "markup"),
        ('<svg><![CDATA[<script>x</script>]]><polygon points="1,1 2,2 3,3" /></svg>', "markup"),
        ('<!DOCTYPE svg [<!ENTITY x "y">]><svg><polygon points="1,1 2,2 3,3" /></svg>', "markup"),
        ('<?xml-stylesheet href="x"?><svg><polygon points="1,1 2,2 3,3" /></svg>', "markup"),
        ('<svg><polygon points="1,1 2,2 &#51;,3" /></svg>', "markup"),
        ('<svg><polygon points="1,1 2,2 3,3" />text</svg>', "text"),
        ('<svg>text<polygon points="1,1 2,2 3,3" /></svg>', "text"),
        ('<svg><polygon points="1,1 2,2 x,3" /></svg>', "value"),
        ('<svg><polygon points="" /></svg>', "value"),
        ('<svg><circle cx="NaN" cy="1" r="0" /></svg>', "value"),
        ('<svg><circle cx="Infinity" cy="1" r="0" /></svg>', "value"),
        ('<svg><circle cx="1px" cy="1" r="0" /></svg>', "value"),
        ('<svg><path d="M1,1 L2,2 url(x)" /></svg>', "value"),
        ('<svg><path d="" /></svg>', "value"),
        ('<svg><path fill-rule="javascript" d="M1,1 L2,2" /></svg>', "fill_rule"),
        ('<svg><g><g><polygon points="1,1 2,2 3,3" /></g></g></svg>', "element"),
        ('<svg><g transform="rotate(1)"><polygon points="1,1 2,2 3,3" /></g></svg>', "attribute"),
        ('<svg><polygon points="1,1 2,2 3,3"><title>x</title></polygon></svg>', "nesting"),
        ('<svg/>', "empty"),
        ('<svg><g></g></svg>', "empty"),
        ('<div><polygon points="1,1 2,2 3,3" /></div>', "root"),
        ('<svg xmlns="http://www.w3.org/1999/xhtml"><polygon points="1,1 2,2 3,3" /></svg>', "namespaced_element"),
        ('<svg><polygon points="1,1 2,2 3,3" />', "parse"),
        ('<svg width="10"><polygon points="1,1 2,2 3,3" /></svg>', "attribute"),
        ("", "parse"),
    ],
)
def test_anything_else_in_an_svg_is_refused(value, reason):
    with pytest.raises(SelectorError) as error:
        check_selector(svg(value))
    assert error.value.reason == reason


@pytest.mark.parametrize(
    ("selector", "reason"),
    [
        ({"type": "FragmentSelector", "value": "xywh=pixel:1,2,3"}, "fragment"),
        ({"type": "FragmentSelector", "value": "xywh=percent:1,2,3,4"}, "fragment"),
        ({"type": "FragmentSelector", "value": "xywh=1,2,3,<script>"}, "fragment"),
        ({"type": "FragmentSelector", "value": "xywh=1,2,3,4", "conformsTo": "javascript:x"}, "conforms_to"),
        ({"type": "FragmentSelector", "value": "xywh=1,2,3,4", "conformsTo": 'http://x"><b>'}, "conforms_to"),
        ({"type": "Point", "x": 1, "value": "1"}, "keys"),
        ({"type": "Point", "value": "1"}, "type"),
        ({"type": "SvgSelector"}, "value_type"),
        ({"type": "SvgSelector", "value": ["<svg/>"]}, "value_type"),
        ({"type": "SvgSelector", "value": "<svg/>", "refinedBy": {}}, "keys"),
        ("<svg/>", "shape"),
    ],
)
def test_other_selectors_are_refused(selector, reason):
    with pytest.raises(SelectorError) as error:
        check_selector(selector)
    assert error.value.reason == reason


def test_a_selector_value_is_capped(monkeypatch):
    monkeypatch.setattr(annotation_selectors, "SELECTOR_VALUE_MAX_CHARS", 40)
    with pytest.raises(SelectorError) as error:
        check_selector(svg('<svg><polygon points="1,1 2,2 3,3 4,4 5,5" /></svg>'))
    assert error.value.reason == "too_long"


def test_a_long_bad_path_is_refused_quickly():
    value = '<svg><path d="M' + " 1" * 200_000 + ' !" /></svg>'
    with pytest.raises(SelectorError):
        check_selector(svg(value))


BAD = {"selector": svg("<svg><script>alert(1)</script></svg>")}


def test_every_write_model_checks_the_target():
    base = {"slide_id": "0123456789ab", "study_id": "s", "body": AnnotationBody()}
    with pytest.raises(ValidationError):
        AnnotationIn(**base, target=BAD)
    with pytest.raises(ValidationError):
        AnnotationIn(**base, target=AnnotationTarget(**BAD))
    with pytest.raises(ValidationError):
        AnnotationUpdate(target=BAD, version=1)
    with pytest.raises(ValidationError):
        AnnotationBatchIn(slide_id="0123456789ab", study_id="s", annotations=[{"body": {}, "target": BAD}])
    with pytest.raises(ValidationError):
        AnnotationBatchUpdate(items=[{"id": "a", "version": 1, "target": BAD}])
    assert AnnotationUpdate(version=1).target is None


def test_stored_targets_are_read_without_the_check():
    row = AnnotationOut(
        id="a",
        slide_id="0123456789ab",
        study_id="s",
        body=AnnotationBody(),
        target=AnnotationTarget(**BAD),
        created_by="u",
        visible_to=None,
        version=1,
        created_at="",
        updated_at="",
    )
    assert row.target.selector == BAD["selector"]
