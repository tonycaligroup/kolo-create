from __future__ import annotations

from bs4 import BeautifulSoup

from kolo_design.extractor import _choose_colors, _choose_fonts, _color_to_hex, _component_inventory, _spacing


def test_style_evidence_compiles_to_semantic_tokens() -> None:
    css = """
    :root { --background: #ffffff; --text-primary: #172033; --brand-primary: #1677ff; }
    body { color: #172033; background-color: #ffffff; font-family: Inter, sans-serif; padding: 24px; }
    button { color: #ffffff; background: #1677ff; border-radius: 14px; padding: 8px 16px; }
    """
    colors, evidence = _choose_colors(css)
    display, body, fonts = _choose_fonts(css, BeautifulSoup("<html></html>", "html.parser"))
    base, scale = _spacing(css)
    assert colors == {"background": "#FFFFFF", "surface": "#FFFFFF", "text": "#172033", "accent": "#1677FF"}
    assert evidence
    assert display == body == "Inter"
    assert fonts
    assert base in scale


def test_transparent_computed_colors_are_not_treated_as_black() -> None:
    assert _color_to_hex("rgba(0, 0, 0, 0)") is None
    assert _color_to_hex("rgba(0, 122, 255, 1)") == "#007AFF"


def test_component_inventory_separates_primary_and_secondary_buttons() -> None:
    def element(tag: str, background: str, foreground: str, *, role: str = "", href: bool = False) -> dict:
        return {
            "tag": tag, "role": role, "href": href, "text_sample": "Action",
            "rect": {"width": 160, "height": 40},
            "style": {
                "color": foreground, "background": background, "border_color": foreground,
                "border_width": "1px", "border_radius": "20px", "box_shadow": "none",
                "font_family": "Inter", "font_size": "14px", "font_weight": "600",
                "line_height": "20px", "letter_spacing": "normal", "text_align": "center",
                "padding": "10px 16px 10px 16px", "object_fit": "fill",
            },
        }

    colors = {"background": "#FFFFFF", "surface": "#F1F5F9", "text": "#1E293B", "accent": "#007AFF"}
    inventory = _component_inventory([
        element("button", "rgb(0, 122, 255)", "rgb(255, 255, 255)"),
        element("button", "rgb(241, 245, 249)", "rgb(30, 41, 59)"),
    ], colors)
    assert inventory["buttons"]["primary"]["background"] == "#007AFF"
    assert inventory["buttons"]["primary"]["foreground"] == "#FFFFFF"
    assert inventory["buttons"]["secondary"]["background"] == "#F1F5F9"
