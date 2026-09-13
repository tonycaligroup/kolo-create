from __future__ import annotations

from bs4 import BeautifulSoup

from kolo_design.extractor import _choose_colors, _choose_fonts, _color_to_hex, _component_inventory, _logo_candidates, _refine_rendered_colors, _spacing


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


def test_empty_css_variable_does_not_break_palette_extraction() -> None:
    colors, _ = _choose_colors(":root { --empty: ; --accent: #635bff; } body { color: #101010; background: #ffffff; }")
    assert colors["accent"] == "#635BFF"


def test_transparent_computed_colors_are_not_treated_as_black() -> None:
    assert _color_to_hex("rgba(0, 0, 0, 0)") is None
    assert _color_to_hex("rgba(0, 122, 255, 1)") == "#007AFF"


def test_rendered_dark_brand_overrides_noisy_static_palette() -> None:
    rendered = {
        "root_styles": {
            "body": {"background": "rgb(9, 9, 9)", "color": "rgb(255, 250, 243)"},
            "html": {"background": "rgb(9, 9, 9)", "color": "rgb(255, 250, 243)"},
        },
        "elements": [
            {"tag": "section", "rect": {"width": 1440, "height": 800}, "style": {"background": "rgb(18, 16, 15)", "color": "rgb(255, 250, 243)", "border_color": "rgb(48, 37, 31)"}},
            {"tag": "strong", "rect": {"width": 500, "height": 100}, "style": {"background": "rgba(0, 0, 0, 0)", "color": "rgb(255, 133, 87)", "border_color": "rgb(255, 133, 87)"}},
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#FFFFFF", "surface": "#F5F5F5", "text": "#000000", "accent": "#090909"}, rendered
    )
    assert colors == {"background": "#090909", "surface": "#12100F", "text": "#FFFAF3", "accent": "#FF8557"}


def test_low_contrast_cookie_overlay_does_not_replace_brand_background() -> None:
    rendered = {
        "root_styles": {"body": {"background": "#525252", "color": "#333333"}},
        "elements": [
            {"tag": "div", "rect": {"width": 900, "height": 500}, "style": {"background": "#FFFFFF", "color": "#333333", "border_color": "#FFFFFF"}},
            {"tag": "strong", "rect": {"width": 300, "height": 60}, "style": {"background": "transparent", "color": "#FE7800", "border_color": "#FE7800"}},
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#FFFFFF", "surface": "#FFFFFF", "text": "#333333", "accent": "#FE7800"}, rendered
    )
    assert colors["background"] == "#FFFFFF"
    assert colors["text"] == "#333333"


def test_site_icon_outranks_unrelated_customer_logo() -> None:
    soup = BeautifulSoup(
        '<link rel="icon" href="/favicon.png"><img alt="Customer logo" src="/customers/acme.png">',
        "html.parser",
    )
    candidates = _logo_candidates(soup, "https://stripe.com")
    assert candidates[0][1] == "https://stripe.com/favicon.png"


def test_structured_brand_logo_outranks_product_logo() -> None:
    soup = BeautifulSoup(
        '<meta property="og:image" content="/structured/open_graph_logo.png"><img alt="Product logo" src="/images/logos/apple-watch.png">',
        "html.parser",
    )
    candidates = _logo_candidates(soup, "https://apple.com")
    assert candidates[0][1] == "https://apple.com/structured/open_graph_logo.png"


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
