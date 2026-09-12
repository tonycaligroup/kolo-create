from __future__ import annotations

from bs4 import BeautifulSoup

from kolo_design.extractor import _choose_colors, _choose_fonts, _spacing


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
