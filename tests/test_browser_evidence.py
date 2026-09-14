from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from kolo_design.browser_evidence import load_browser_evidence
from kolo_design.source_fidelity import SourceFidelityError, ensure_source_fidelity, evaluate_source_fidelity


def _element(tag: str, text: str = "", *, src: str = "") -> dict:
    return {
        "tag": tag, "text_sample": text, "src": src, "alt": "",
        "rect": {"x": 0, "y": 0, "width": 800, "height": 200},
        "viewport": {"visible": True, "area_ratio": 0.12},
        "semantic": {"region": "main", "overlay": False},
        "style": {"color": "rgb(17, 17, 17)", "background": "rgb(255, 255, 255)"},
    }


def test_tesla_style_access_denied_is_rejected_even_with_http_200() -> None:
    snapshot = {
        "title": "Access Denied", "response_status": 200,
        "html": "<html><title>Access Denied</title><body>Access Denied. You don't have permission to access this server.</body></html>",
        "elements": [_element("h1", "Access Denied")],
    }
    report = evaluate_source_fidelity(snapshot)
    assert report["status"] == "blocked"
    assert "access_denied" in report["reasons"]
    with pytest.raises(SourceFidelityError):
        ensure_source_fidelity(snapshot)


def test_real_product_page_evidence_passes() -> None:
    snapshot = {
        "title": "Electric Cars, Solar & Clean Energy | Tesla", "response_status": 200,
        "html": "<html><body><main><h1>Model Y</h1><p>Built for safety, range, and everyday driving.</p><a>Order Now</a></main></body></html>",
        "elements": [
            _element("main", "Model Y Built for safety, range, and everyday driving. Order Now"),
            _element("h1", "Model Y"), _element("p", "Built for safety, range, and everyday driving."),
            _element("img", src="https://www.tesla.com/model-y.jpg"),
        ],
    }
    assert evaluate_source_fidelity(snapshot)["status"] == "pass"


def test_visible_browser_bundle_loads_and_is_gated(tmp_path: Path) -> None:
    (tmp_path / "page.html").write_text(
        "<html><body><main><h1>Model Y</h1><p>Explore electric vehicles, energy products, and charging.</p></main></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "computed-css.txt").write_text(
        "color:rgb(23,26,32);background-color:rgb(255,255,255);font-family:Arial;font-size:48px",
        encoding="utf-8",
    )
    elements = [
        _element("main", "Model Y Explore electric vehicles, energy products, and charging."),
        _element("h1", "Model Y"), _element("p", "Explore electric vehicles, energy products, and charging."),
        _element("img", src="https://www.tesla.com/model-y.jpg"),
    ]
    (tmp_path / "elements.json").write_text(json.dumps(elements), encoding="utf-8")
    Image.new("RGB", (1440, 1100), "white").save(tmp_path / "viewport.png")
    manifest = {
        "schema": "kolo.browser-evidence/v1", "url": "https://www.tesla.com/", "title": "Tesla",
        "response_status": 200, "viewport": {"width": 1440, "height": 1100},
        "root_styles": {"body": {"color": "rgb(23,26,32)", "background": "rgb(255,255,255)"}},
        "files": {
            "html": "page.html", "computed_css": "computed-css.txt",
            "elements": "elements.json", "screenshot": "viewport.png",
        },
    }
    (tmp_path / "browser-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    snapshot, metadata = load_browser_evidence(tmp_path)
    assert snapshot["source_fidelity"]["status"] == "pass"
    assert snapshot["navigation_fallback"] == "kolo_visible_browser_evidence"
    assert metadata["kind"] == "kolo-visible-browser-evidence"


def test_browser_bundle_rejects_unsafe_file_reference(tmp_path: Path) -> None:
    manifest = {
        "schema": "kolo.browser-evidence/v1", "url": "https://example.com", "title": "Example",
        "files": {"html": "../secret", "computed_css": "x", "elements": "y", "screenshot": "z"},
    }
    (tmp_path / "browser-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe path"):
        load_browser_evidence(tmp_path)
