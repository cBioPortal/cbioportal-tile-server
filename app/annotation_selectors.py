"""Write-time checks on annotation selectors.

The viewer hands ``target.selector.value`` to Annotorious, which puts an
``SvgSelector`` into the page as SVG markup, so a stored selector must hold
nothing but the shapes the viewer itself writes.  Every create and update
(single or batch) is checked here; stored rows are never re-checked, so
existing annotations stay readable.

A selector is an object with ``type``, ``value`` and, optionally,
``conformsTo`` (an ``http(s)`` URL); nothing else.

``FragmentSelector``: ``xywh=x,y,w,h`` or ``xywh=pixel:x,y,w,h``, four
numbers.

``SvgSelector``: one ``<svg>`` element (no attributes but the SVG
``xmlns``) holding at least one shape, directly or inside ``<g>`` elements
(one level, no attributes but ``fill-rule``):

- ``<polygon points>`` and ``<polyline points>``: numbers separated by
  commas or whitespace;
- ``<path d>``: path commands (``MLHVCSQTAZ``, either case) and numbers;
- ``<circle cx cy r>`` (a point is ``r="0"``), ``<ellipse cx cy rx ry>``,
  ``<line x1 y1 x2 y2>``, ``<rect x y width height rx ry>``: each a number;
- ``fill-rule`` (``nonzero`` or ``evenodd``) on any shape or ``<g>``.

Anything else is refused: other elements (``script``, ``foreignObject``,
``image``, ``use``, ...), other attributes (``on*``, ``style``, ``href``,
``xlink:href``, ``class``, ...), text, entities and character references,
comments, CDATA, processing instructions and DOCTYPEs.  A number is a
decimal with an optional sign and exponent; ``NaN`` and ``Infinity`` are
not numbers.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

# Longest selector value, in characters (a 5000-vertex import is ~100k).
SELECTOR_VALUE_MAX_CHARS = 1_000_000

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SELECTOR_KEYS = frozenset({"type", "value", "conformsTo"})

_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_NUMBER_RE = re.compile(_NUMBER)
_PATH_TOKEN_RE = re.compile(rf"[MmLlHhVvCcSsQqTtAaZz]|{_NUMBER}")
_LIST_SEPARATOR_RE = re.compile(r"[\s,]+")
_FRAGMENT_RE = re.compile(rf"xywh=(?:pixel:)?({_NUMBER}),({_NUMBER}),({_NUMBER}),({_NUMBER})")
_CONFORMS_TO_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$'()*+,;=%-]{1,200}")
_FILL_RULES = frozenset({"nonzero", "evenodd"})

_NUMERIC_ATTRIBUTES: dict[str, frozenset[str]] = {
    "circle": frozenset({"cx", "cy", "r"}),
    "ellipse": frozenset({"cx", "cy", "rx", "ry"}),
    "line": frozenset({"x1", "y1", "x2", "y2"}),
    "rect": frozenset({"x", "y", "width", "height", "rx", "ry"}),
}
_POINT_LIST_SHAPES = frozenset({"polygon", "polyline"})
SHAPES = frozenset({*_NUMERIC_ATTRIBUTES, *_POINT_LIST_SHAPES, "path"})


class SelectorError(ValueError):
    """A refused selector; ``reason`` is a short fixed code (no input text)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _is_number(value: str) -> bool:
    return bool(_NUMBER_RE.fullmatch(value.strip()))


def _is_point_list(value: str) -> bool:
    tokens = [token for token in _LIST_SEPARATOR_RE.split(value.strip()) if token]
    return bool(tokens) and all(_NUMBER_RE.fullmatch(token) for token in tokens)


def _is_path_data(value: str) -> bool:
    if not _PATH_TOKEN_RE.search(value):
        return False
    return not _LIST_SEPARATOR_RE.sub("", _PATH_TOKEN_RE.sub(" ", value))


def _local_name(name: str, what: str) -> str:
    """``name`` without the SVG namespace; refuses any other namespace."""
    if name.startswith("{"):
        namespace, _, local = name[1:].partition("}")
        if namespace != SVG_NAMESPACE or what == "attribute":
            raise SelectorError(
                f"namespaced_{what}", f"An SVG selector may not use namespaced {what}s"
            )
        return local
    return name


def _no_text(element: ET.Element) -> None:
    if (element.text or "").strip() or (element.tail or "").strip():
        raise SelectorError("text", "An SVG selector may not contain text")


def _check_fill_rule(name: str, value: str) -> bool:
    if name != "fill-rule":
        return False
    if value.strip() not in _FILL_RULES:
        raise SelectorError("fill_rule", "fill-rule must be nonzero or evenodd")
    return True


def _check_shape(element: ET.Element, tag: str) -> None:
    for raw_name, value in element.attrib.items():
        name = _local_name(raw_name, "attribute")
        if _check_fill_rule(name, value):
            continue
        if tag in _NUMERIC_ATTRIBUTES and name in _NUMERIC_ATTRIBUTES[tag]:
            ok = _is_number(value)
        elif tag in _POINT_LIST_SHAPES and name == "points":
            ok = _is_point_list(value)
        elif tag == "path" and name == "d":
            ok = _is_path_data(value)
        else:
            raise SelectorError(
                "attribute", f"<{tag}> may not carry the attribute {name!r}"
            )
        if not ok:
            raise SelectorError("value", f"<{tag}> {name} must be numeric")
    if len(element):
        raise SelectorError("nesting", f"<{tag}> may not contain other elements")


def _check_container(element: ET.Element, tag: str, depth: int) -> int:
    """Check ``<svg>``/``<g>`` and its children; returns the shapes found."""
    for raw_name, value in element.attrib.items():
        name = _local_name(raw_name, "attribute")
        if tag == "g" and _check_fill_rule(name, value):
            continue
        raise SelectorError("attribute", f"<{tag}> may not carry the attribute {name!r}")
    shapes = 0
    for child in element:
        if not isinstance(child.tag, str):
            raise SelectorError("markup", "An SVG selector may hold only elements")
        child_tag = _local_name(child.tag, "element")
        if child_tag not in SHAPES and not (child_tag == "g" and depth == 0):
            raise SelectorError(
                "element", f"An SVG selector may not contain <{child_tag}>"
            )
        _no_text(child)
        if child_tag == "g":
            shapes += _check_container(child, "g", depth + 1)
        else:
            _check_shape(child, child_tag)
            shapes += 1
    return shapes


def check_svg(value: str) -> None:
    """Raise ``SelectorError`` unless ``value`` is an SVG the viewer writes."""
    if "<!" in value or "<?" in value or "&" in value:
        raise SelectorError(
            "markup",
            "An SVG selector may not contain comments, CDATA, declarations,"
            " processing instructions or entities",
        )
    try:
        root = ET.fromstring(value.strip())
    except ET.ParseError:
        raise SelectorError("parse", "An SVG selector must be well-formed SVG") from None
    if _local_name(root.tag, "element") != "svg":
        raise SelectorError("root", "An SVG selector must be one <svg> element")
    if (root.text or "").strip():
        raise SelectorError("text", "An SVG selector may not contain text")
    if _check_container(root, "svg", 0) == 0:
        raise SelectorError("empty", "An SVG selector must hold at least one shape")


def check_fragment(value: str) -> None:
    """Raise ``SelectorError`` unless ``value`` is ``xywh=[pixel:]x,y,w,h``."""
    if not _FRAGMENT_RE.fullmatch(value.strip()):
        raise SelectorError(
            "fragment", "A FragmentSelector value must be xywh=[pixel:]x,y,w,h with numbers"
        )


def check_selector(selector: Any) -> dict[str, Any]:
    """``selector`` if the viewer may be handed it; raises ``SelectorError``."""
    if not isinstance(selector, dict):
        raise SelectorError("shape", "A selector must be an object")
    extra = set(selector) - SELECTOR_KEYS
    if extra:
        raise SelectorError(
            "keys", "A selector may carry only type, value and conformsTo"
        )
    value = selector.get("value")
    if not isinstance(value, str):
        raise SelectorError("value_type", "A selector's value must be a string")
    if len(value) > SELECTOR_VALUE_MAX_CHARS:
        raise SelectorError(
            "too_long", f"A selector's value is at most {SELECTOR_VALUE_MAX_CHARS} characters"
        )
    conforms_to = selector.get("conformsTo")
    if conforms_to is not None and not (
        isinstance(conforms_to, str) and _CONFORMS_TO_RE.fullmatch(conforms_to)
    ):
        raise SelectorError("conforms_to", "conformsTo must be an http(s) URL")
    kind = selector.get("type")
    if kind == "SvgSelector":
        check_svg(value)
    elif kind == "FragmentSelector":
        check_fragment(value)
    else:
        raise SelectorError(
            "type", "A selector's type must be SvgSelector or FragmentSelector"
        )
    return selector
