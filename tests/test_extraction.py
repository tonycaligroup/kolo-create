from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from kolo_design.browser_extract import _browser_executable, _dismiss_overlays, _freeze_motion, _reference_pdf

from kolo_design.extractor import (
    _browser_native_evidence,
    _choose_colors,
    _choose_fonts,
    _color_to_hex,
    _component_inventory,
    _logo_candidates,
    _logo_palette,
    _refine_rendered_colors,
    _refine_rendered_fonts,
    _spacing,
    _visual_language,
)
from kolo_design.reference_evidence import analyze_reference_pdf, reconcile_reference_colors


def test_browser_native_evidence_preserves_css_for_future_renderers() -> None:
    css = """
    :root { --brand-blue: #123456; --space-lg: 32px; --private-key: do-not-store; }
    @font-face { font-family: 'Brand Sans'; font-weight: 700; src: url('/brand.woff2'); }
    @media (min-width: 768px) { .grid { display: grid; } }
    @media (max-width: 1200px) { .grid { gap: 24px; } }
    """
    rendered = {
        "viewport": {"width": 1440, "height": 1100},
        "elements": [{
            "tag": "section", "rect": {"width": 1120, "height": 500},
            "viewport": {"visible": True, "area_ratio": 0.35},
            "semantic": {"region": "main", "overlay": False},
            "style": {
                "display": "grid", "gap": "24px", "grid_template_columns": "1fr 1fr",
                "grid_template_rows": "auto", "max_width": "1120px", "padding": "32px",
                "background_image": "linear-gradient(#fff, #eee)",
            },
        }],
    }
    evidence = _browser_native_evidence(css, rendered)
    assert evidence["breakpoints_px"] == [768, 1200]
    assert {item["name"] for item in evidence["css_custom_properties"]} == {"--brand-blue", "--space-lg"}
    assert evidence["font_faces"][0]["family"] == "Brand Sans"
    assert evidence["layout_primitives"][0]["columns"] == "1fr 1fr"
    assert evidence["background_treatments"] == ["linear-gradient(#fff, #eee)"]


def test_consent_cleanup_removes_orphaned_fullscreen_backdrop() -> None:
    executable = _browser_executable()
    if not executable:
        pytest.skip("Chromium is not installed")
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.set_content(
            """
            <main>Visible brand page</main>
            <div class="privacy-backdrop" style="position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.5)"></div>
            <div role="dialog" class="cookie-dialog"><p>Cookie privacy preferences</p><button>Accept All</button></div>
            """
        )
        result = _dismiss_overlays(page)
        assert result == {"clicked": "Accept All", "hidden": 1, "backdrops_hidden": 1}
        assert page.locator(".privacy-backdrop").evaluate("el => getComputedStyle(el).display") == "none"
        browser.close()


def test_reference_pdf_validates_rendered_colors_and_rejects_unpainted_css(tmp_path) -> None:
    executable = _browser_executable()
    if not executable:
        pytest.skip("Chromium is not installed")
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.set_content(
            """
            <style>
              html,body { margin:0; background:#f9f8f5; color:#111827; }
              main { min-height:1800px; padding:80px; }
              button { background:#007aff; color:white; padding:18px 30px; }
              .unused { color:#16a34a; }
            </style>
            <main><h1>Visible brand page</h1><button>Book a Demo</button></main>
            """
        )
        _freeze_motion(page)
        screenshot = page.screenshot(type="png")
        payload, metadata = _reference_pdf(page)
        browser.close()
    assert payload is not None
    assert metadata["status"] == "captured"
    pdf_path = tmp_path / "source-webpage.pdf"
    pdf_path.write_bytes(payload)
    assert len(PdfReader(str(pdf_path)).pages) >= 1
    report = analyze_reference_pdf(
        pdf_path,
        screenshot,
        ["#F9F8F5", "#111827", "#007AFF", "#16A34A"],
        [],
    )
    support = {item["value"]: item["status"] for item in report["candidates"]}
    assert report["usable_for_color_validation"] is True
    assert support["#007AFF"] == "supported"
    assert support["#16A34A"] == "contradicted"
    colors, decisions = reconcile_reference_colors(
        {
            "background": "#F9F8F5", "surface": "#FFFFFF", "text": "#111827",
            "accent": "#007AFF", "accent_secondary": "#16A34A",
        },
        [
            {"value": "#007AFF", "occurrences": 20},
            {"value": "#16A34A", "occurrences": 40},
        ],
        report,
    )
    assert colors["accent_secondary"] == "#007AFF"
    assert next(item for item in decisions if item["role"] == "accent_secondary")["status"] == "contradicted"


def test_reference_reconciliation_replaces_unsupported_mailchimp_teal_with_supported_dark() -> None:
    reference = {
        "usable_for_color_validation": True,
        "candidates": [
            {"value": "#FFE01B", "status": "supported", "pdf_share_within_rgb_12": 0.003, "screenshot_share_within_rgb_12": 0.01, "logo_supported": True},
            {"value": "#004E56", "status": "contradicted", "pdf_share_within_rgb_12": 0.0, "screenshot_share_within_rgb_12": 0.0, "logo_supported": False},
            {"value": "#231E15", "status": "supported", "pdf_share_within_rgb_12": 0.12, "screenshot_share_within_rgb_12": 0.04, "logo_supported": False},
        ],
    }
    colors, _ = reconcile_reference_colors(
        {
            "background": "#FFFFFF", "surface": "#F5F5F5", "text": "#000000",
            "accent": "#FFE01B", "accent_secondary": "#004E56", "brand_dark": "#004E56",
        },
        [
            {"value": "#FFE01B", "occurrences": 120},
            {"value": "#004E56", "occurrences": 83},
            {"value": "#231E15", "occurrences": 752},
        ],
        reference,
    )
    assert colors["accent_secondary"] == "#FFE01B"
    assert colors["brand_dark"] == "#231E15"


def test_reference_reconciliation_preserves_supported_redbull_navy() -> None:
    reference = {
        "usable_for_color_validation": True,
        "candidates": [
            {"value": "#D2003C", "status": "supported", "pdf_share_within_rgb_12": 0.0001, "screenshot_share_within_rgb_12": 0.0003, "logo_supported": True},
            {"value": "#FFCC00", "status": "supported", "pdf_share_within_rgb_12": 0.0001, "screenshot_share_within_rgb_12": 0.0001, "logo_supported": True},
            {"value": "#00162B", "status": "supported", "pdf_share_within_rgb_12": 0.18, "screenshot_share_within_rgb_12": 0.01, "logo_supported": False},
        ],
    }
    colors, _ = reconcile_reference_colors(
        {
            "background": "#FFFFFF", "surface": "#F8F8F8", "text": "#000000",
            "accent": "#D2003C", "accent_secondary": "#FFCC00", "brand_dark": "#00162B",
        },
        [
            {"value": "#D2003C", "occurrences": 61},
            {"value": "#FFCC00", "occurrences": 20},
            {"value": "#00162B", "occurrences": 110},
        ],
        reference,
    )
    assert colors["accent_secondary"] == "#FFCC00"
    assert colors["brand_dark"] == "#00162B"


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
    assert colors == {
        "background": "#090909", "surface": "#12100F", "text": "#FFFAF3",
        "accent": "#FF8557", "accent_secondary": "#FF8557",
    }


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


def test_cta_and_logo_colors_outrank_browser_default_links() -> None:
    rendered = {
        "root_styles": {"body": {"background": "#FFFFFF", "color": "#3C3C3C"}},
        "viewport": {"width": 1440, "height": 1000},
        "elements": [
            {
                "tag": "main", "rect": {"width": 1440, "height": 900},
                "viewport": {"visible": True, "area_ratio": 0.9}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "#FFFFFF", "color": "#3C3C3C", "border_color": "#FFFFFF"},
            },
            {
                "tag": "a", "href": True, "text_sample": "Get started", "rect": {"width": 240, "height": 48},
                "viewport": {"visible": True, "area_ratio": 0.008}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "#58CC02", "color": "#FFFFFF", "border_color": "#58CC02"},
            },
            {
                "tag": "a", "href": True, "text_sample": "Legal", "rect": {"width": 80, "height": 30},
                "viewport": {"visible": True, "area_ratio": 0.002}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "transparent", "color": "#0000EE", "border_color": "#0000EE"},
            },
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#FFFFFF", "surface": "#F7F7F7", "text": "#3C3C3C", "accent": "#0000EE"},
        rendered,
        ["#58CC02"],
    )
    assert colors["accent"] == "#58CC02"
    assert colors["accent_secondary"] == "#58CC02"


def test_frequent_saturated_dark_color_is_preserved_as_brand_support() -> None:
    rendered = {
        "root_styles": {"body": {"background": "#FFFFFF", "color": "#000000"}},
        "viewport": {"width": 1000, "height": 1000},
        "elements": [
            {
                "tag": "main", "rect": {"width": 1000, "height": 900},
                "viewport": {"visible": True, "area_ratio": 0.9}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "#FFFFFF", "color": "#000000", "border_color": "#00162B"},
            },
            *[
                {
                    "tag": "nav", "rect": {"width": 500, "height": 48},
                    "viewport": {"visible": True, "area_ratio": 0.024}, "semantic": {"region": "nav", "overlay": False},
                    "style": {"background": "#00162B", "color": "#FFFFFF", "border_color": "#00162B"},
                }
                for _ in range(4)
            ],
            {
                "tag": "a", "href": True, "rect": {"width": 160, "height": 44},
                "viewport": {"visible": True, "area_ratio": 0.007}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "#D2003C", "color": "#FFFFFF", "border_color": "#D2003C"},
            },
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#FFFFFF", "surface": "#F8F8F8", "text": "#000000", "accent": "#FFCC00"},
        rendered,
        ["#FFCC00", "#D2003C"],
    )
    assert colors["brand_dark"] == "#00162B"


def test_browser_default_blue_cannot_become_brand_dark() -> None:
    rendered = {
        "root_styles": {"body": {"background": "#FFFFFF", "color": "#000000"}},
        "viewport": {"width": 1000, "height": 1000},
        "elements": [
            {
                "tag": "main", "rect": {"width": 1000, "height": 900},
                "viewport": {"visible": True, "area_ratio": 0.9}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "#FFFFFF", "color": "#000000", "border_color": "#0000EE"},
            },
            *[
                {
                    "tag": "a", "href": True, "rect": {"width": 120, "height": 32},
                    "viewport": {"visible": True, "area_ratio": 0.004}, "semantic": {"region": "nav", "overlay": False},
                    "style": {"background": "transparent", "color": "#0000EE", "border_color": "#0000EE"},
                }
                for _ in range(12)
            ],
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#FFFFFF", "surface": "#F8F8F8", "text": "#000000", "accent": "#1428A0"}, rendered
    )
    assert colors.get("brand_dark") != "#0000EE"


def test_overlay_colors_are_excluded_from_palette_and_visual_language() -> None:
    rendered = {
        "root_styles": {"body": {"background": "#FFFFFF", "color": "#111111"}},
        "viewport": {"width": 1000, "height": 1000},
        "elements": [
            {
                "tag": "main", "rect": {"width": 1000, "height": 900},
                "viewport": {"visible": True, "area_ratio": 0.9}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "#FFFFFF", "color": "#111111", "border_color": "#FFFFFF", "text_align": "left"},
            },
            {
                "tag": "div", "rect": {"width": 1000, "height": 700},
                "viewport": {"visible": True, "area_ratio": 0.7}, "semantic": {"region": "dialog", "overlay": True},
                "style": {"background": "#765432", "color": "#FFFFFF", "border_color": "#765432", "text_align": "center"},
            },
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#FFFFFF", "surface": "#F8F8F8", "text": "#111111", "accent": "#2244CC"}, rendered
    )
    assert colors["background"] == "#FFFFFF"
    assert colors["accent"] != "#765432"
    assert _visual_language(rendered)["overlay_count"] == 1


def test_dominant_light_canvas_replaces_noisy_static_dark_background() -> None:
    rendered = {
        "root_styles": {
            "body": {"background": "transparent", "color": "#000000"},
            "html": {"background": "transparent", "color": "#000000"},
        },
        "viewport": {"width": 1000, "height": 1000},
        "elements": [
            {
                "tag": "main", "text_sample": "Main content", "rect": {"width": 1000, "height": 900},
                "viewport": {"visible": True, "area_ratio": 0.9}, "semantic": {"region": "main", "overlay": False},
                "style": {"background": "#F7F5F2", "color": "#000000", "border_color": "#F7F5F2"},
            }
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#161313", "surface": "#002969", "text": "#F7F5F2", "accent": "#0061FE"}, rendered
    )
    assert colors["background"] == "#F7F5F2"
    assert colors["text"] == "#000000"


def test_dominant_dark_canvas_replaces_unseen_static_campaign_color() -> None:
    rendered = {
        "root_styles": {
            "body": {"background": "transparent", "color": "#000000"},
            "html": {"background": "transparent", "color": "#000000"},
        },
        "viewport": {"width": 1000, "height": 1000},
        "elements": [
            {
                "tag": "div", "text_sample": "Skip to main content", "rect": {"width": 1000, "height": 1000},
                "viewport": {"visible": True, "area_ratio": 1.0}, "semantic": {"region": "body", "overlay": False},
                "style": {"background": "#FFFFFF", "color": "#000000", "border_color": "#FFFFFF"},
            },
            *[
                {
                    "tag": "section", "text_sample": "Go anywhere with Uber", "rect": {"width": 1000, "height": 500},
                    "viewport": {"visible": True, "area_ratio": 0.5}, "semantic": {"region": "section", "overlay": False},
                    "style": {"background": "#000000", "color": "#FFFFFF", "border_color": "#000000"},
                }
                for _ in range(3)
            ],
            *[
                {
                    "tag": "div", "text_sample": "Ride details", "rect": {"width": 280, "height": 100},
                    "viewport": {"visible": True, "area_ratio": 0.028}, "semantic": {"region": "section", "overlay": False},
                    "style": {"background": "#383838", "color": "#FFFFFF", "border_color": "#383838"},
                }
                for _ in range(6)
            ],
        ],
    }
    colors = _refine_rendered_colors(
        {"background": "#F43B00", "surface": "#000000", "text": "#FFFFFF", "accent": "#F43B00"}, rendered
    )
    assert colors["background"] == "#000000"
    assert colors["surface"] == "#383838"
    assert colors["text"] == "#FFFFFF"
    assert colors["accent"] == "#FFFFFF"


def test_rendered_heading_and_body_fonts_define_portable_categories() -> None:
    rendered = {
        "elements": [
            {
                "tag": "h1", "text_sample": "Big headline", "semantic": {"overlay": False},
                "style": {"font_family": "Means Web, Georgia, serif", "font_size": "64px"},
            },
            {
                "tag": "p", "text_sample": "Readable supporting copy for the page", "semantic": {"overlay": False},
                "style": {"font_family": "Graphik Web, Arial, sans-serif", "font_size": "17px"},
            },
        ]
    }
    display, body, display_fallback, body_fallback = _refine_rendered_fonts("Arial", "Arial", rendered)
    assert (display, body) == ("Means Web", "Graphik Web")
    assert (display_fallback, body_fallback) == ("serif", "sans-serif")


def test_site_icon_outranks_unrelated_customer_logo() -> None:
    soup = BeautifulSoup(
        '<link rel="icon" href="/favicon.png"><img alt="Customer logo" src="/customers/acme.png">',
        "html.parser",
    )
    candidates = _logo_candidates(soup, "https://stripe.com")
    assert candidates[0][1] == "https://stripe.com/favicon.png"


def test_logo_palette_uses_raster_fallback_after_svg(tmp_path) -> None:
    svg = tmp_path / "logo.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    png = tmp_path / "logo.png"
    Image.new("RGB", (20, 20), "#0061FE").save(png)
    palette = _logo_palette([{"path": str(svg)}, {"path": str(png)}])
    assert palette[0] == "#0061FE"


def test_structured_brand_logo_outranks_product_logo() -> None:
    soup = BeautifulSoup(
        '<meta property="og:image" content="/structured/open_graph_logo.png"><img alt="Product logo" src="/images/logos/apple-watch.png">',
        "html.parser",
    )
    candidates = _logo_candidates(soup, "https://apple.com")
    assert candidates[0][1] == "https://apple.com/structured/open_graph_logo.png"


def test_exact_brand_logo_outranks_product_logos_in_brand_asset_namespace() -> None:
    soup = BeautifulSoup(
        '<img alt="" src="/assets/dropbox/replay-logo-nav.svg">'
        '<img alt="" src="/assets/dropbox/dropbox-sign-logo.svg">'
        '<img alt="" src="/assets/dropbox/Dropbox-logo-nav.svg">',
        "html.parser",
    )
    candidates = _logo_candidates(soup, "https://dropbox.com")
    assert candidates[0][1].endswith("/Dropbox-logo-nav.svg")


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


def test_semantic_product_cta_outranks_repeated_navigation_links() -> None:
    def element(text: str, region: str, background: str, border: str, *, tag: str = "a") -> dict:
        return {
            "tag": tag, "role": "button" if tag == "button" else "", "href": tag == "a", "text_sample": text,
            "rect": {"width": 150, "height": 42},
            "viewport": {"visible": True, "area_ratio": 0.005}, "semantic": {"region": region, "overlay": False},
            "style": {
                "color": "#000000", "background": background, "border_color": border,
                "border_width": "1px", "border_radius": "22px", "box_shadow": "none",
                "font_family": "Arial", "font_size": "14px", "font_weight": "700",
                "line_height": "20px", "letter_spacing": "normal", "text_align": "center",
                "padding": "10px 16px 10px 16px", "object_fit": "fill",
            },
        }

    inventory = _component_inventory(
        [*[element("Support", "nav", "transparent", "transparent") for _ in range(8)],
         element("Buy now", "main", "transparent", "#000000", tag="button")],
        {"background": "#FFFFFF", "surface": "#F7F7F7", "text": "#000000", "accent": "#1428A0"},
    )
    assert inventory["buttons"]["selection"]["primary_label"] == "Buy now"
    assert inventory["buttons"]["primary"]["background"] == "#FFFFFF"
    assert inventory["buttons"]["primary"]["border_color"] == "#000000"


def test_large_product_media_selects_product_led_visual_language() -> None:
    rendered = {
        "viewport": {"width": 1000, "height": 1000},
        "elements": [
            {
                "tag": "main", "text_sample": "Buy the new Galaxy phone", "rect": {"width": 1000, "height": 900},
                "viewport": {"visible": True, "area_ratio": 0.9}, "semantic": {"region": "main", "overlay": False},
                "style": {"text_align": "left"},
            },
            {
                "tag": "img", "src": "https://example.com/phone.jpg", "alt": "Galaxy phone",
                "text_sample": "", "rect": {"width": 900, "height": 500},
                "viewport": {"visible": True, "area_ratio": 0.45}, "semantic": {"region": "main", "overlay": False},
                "style": {"text_align": "left"},
            },
        ],
    }
    visual = _visual_language(rendered)
    assert visual["primary_mode"] == "product-led"
    assert visual["product_language"] is True
