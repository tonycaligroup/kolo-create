from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PILImage
from pypdf import PdfReader
from reportlab.lib.enums import TA_CENTER, TA_LEFT

import kolo_design.cli as cli_module
from kolo_design.assets import select_logo_asset
from kolo_design.composition import select_composition, validate_composition
from kolo_design.contracts import validate_design_system
from kolo_design.cli import parser
from kolo_design.browser_extract import _browser_executable
from kolo_design.html_designer import _inline_html, _page_groups, create_html_pdf
from kolo_design.network import FetchError, assert_public_url
from kolo_design.pdf_designer import (
    _bounded_radius,
    _brand_dark,
    _cover_alignment,
    _eyebrow_color,
    _legible_foreground,
    _paired_grid_rows,
    _preferred_foreground,
    create_pdf,
)
from kolo_design.planner import DeterministicPlanner, source_blocks, validate_plan
from kolo_design.util import read_json

FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_system_is_valid() -> None:
    validate_design_system(read_json(FIXTURES / "design-system.json"))


def test_logo_selection_prefers_vector_for_html(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.png"
    vector = tmp_path / "logo.svg"
    PILImage.new("RGB", (40, 20), "white").save(tiny)
    vector.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40"/>')
    system = {"assets": [
        {"kind": "logo", "path": str(tiny)},
        {"kind": "logo", "path": str(vector)},
    ]}
    assert select_logo_asset(system, allow_svg=True, max_width=132, max_height=56)["path"] == str(vector)


def test_logo_selection_rejects_raster_that_would_be_enlarged(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.png"
    PILImage.new("RGB", (51, 40), "white").save(tiny)
    system = {"assets": [{"kind": "logo", "path": str(tiny)}]}
    assert select_logo_asset(system, allow_svg=False, max_width=108, max_height=46.8) is None


def test_component_foreground_falls_back_to_readable_contrast() -> None:
    assert _legible_foreground("#000000", "#1264A3", "#000000") == "#FFFFFF"


def test_accessible_observed_button_foreground_is_preserved() -> None:
    assert _preferred_foreground("#007AFF", "#FFFFFF", "#1E293B") == "#FFFFFF"


def test_button_radius_is_bounded_inside_its_rendered_height() -> None:
    assert _bounded_radius(20, 168, 31) == 15


def test_product_and_asymmetric_covers_use_one_left_aligned_grid() -> None:
    assert _cover_alignment("product_showcase", "center") == TA_LEFT
    assert _cover_alignment("asymmetric_feature_grid", "right") == TA_LEFT
    assert _cover_alignment("editorial_narrative", "center") == TA_CENTER


def test_paired_grid_uses_an_explicit_middle_gutter() -> None:
    assert _paired_grid_rows(["one", "two", "three"]) == [
        ["one", "", "two"],
        ["three", "", ""],
    ]


def test_brand_dark_can_be_recovered_from_saved_color_evidence() -> None:
    system = {"evidence": {"colors": [
        {"value": "#000000", "occurrences": 1000},
        {"value": "#00162B", "occurrences": 110},
        {"value": "#1B6AEE", "occurrences": 35},
    ]}}
    palette = {
        "background": "#FFFFFF", "surface": "#F8F8F8", "text": "#000000",
        "accent": "#FFCC00", "accent_secondary": "#D2003C",
    }
    assert _brand_dark(system, palette) == "#00162B"


@pytest.mark.parametrize(
    ("palette", "expected"),
    [
        ({"background": "#FFFFFF", "text": "#000000", "accent": "#FFCC00", "accent_secondary": "#D2003C"}, "#D2003C"),
        ({"background": "#FFFFFF", "text": "#000000", "accent": "#FFE01B", "accent_secondary": "#004E56"}, "#004E56"),
    ],
)
def test_cover_eyebrow_avoids_low_contrast_signature_yellow(palette: dict[str, str], expected: str) -> None:
    assert _eyebrow_color(palette) == expected


def test_create_design_system_command_contract() -> None:
    args = parser().parse_args([
        "create", "design-system", "--url", "https://example.com", "--workspace", "./data",
        "--example-output", "./example.pdf",
    ])
    assert args.command == "create"
    assert args.create_command == "design-system"
    assert args.example_output == Path("./example.pdf")


def test_compare_renderers_command_contract() -> None:
    args = parser().parse_args([
        "pdf", "compare", "--system", "./system.json", "--content", "./content.md",
        "--prompt", "Create a brief", "--output-dir", "./comparison",
    ])
    assert args.pdf_command == "compare"
    assert args.output_dir == Path("./comparison")
    assert args.planner == "deterministic"


def test_html_renderer_helpers_preserve_markup_and_page_ownership() -> None:
    assert _inline_html("A **strong** [link](https://example.com)") == (
        'A <strong>strong</strong> <a href="https://example.com">link</a>'
    )
    sections = [{"id": f"s{index}"} for index in range(1, 7)]
    assert _page_groups(sections) == [sections[:3], sections[3:]]


def test_create_design_system_can_render_bundled_first_example(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    system_path = tmp_path / "design-system.json"
    example_path = tmp_path / "first-example.pdf"
    monkeypatch.setattr(
        cli_module,
        "extract_brand",
        lambda url, workspace, name: {"status": "succeeded", "design_system": str(system_path)},
    )
    captured: dict[str, Path] = {}

    def fake_create_pdf(system: Path, content: Path, prompt: str, output: Path, planner: object) -> dict[str, str]:
        captured.update(system=system, content=content, output=output)
        return {"status": "succeeded", "pdf": str(output)}

    monkeypatch.setattr(cli_module, "create_pdf", fake_create_pdf)
    status = cli_module.main([
        "create", "design-system", "--url", "https://example.com", "--workspace", str(tmp_path),
        "--example-output", str(example_path),
    ])
    assert status == 0
    assert captured["system"] == system_path
    assert captured["output"] == example_path
    assert captured["content"].name == "kolo-create-explainer.md"
    assert captured["content"].exists()


def test_composition_uses_brand_and_content_signals() -> None:
    content = "# Launch\n\n## How it works\n\n- Observe\n- Interpret\n- Save"
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create an overview", blocks)
    illustration_system = {"visual_language": {"primary_mode": "illustration-led", "density": "balanced"}}
    dense_media_system = {"visual_language": {"primary_mode": "media-led", "density": "dense"}}
    product_system = {"visual_language": {"primary_mode": "product-led", "density": "balanced"}}
    assert select_composition(illustration_system, plan, blocks, "Create an overview")["family"] == "modular_announcement"
    assert select_composition(dense_media_system, plan, blocks, "Create an overview")["family"] == "asymmetric_feature_grid"
    assert select_composition(product_system, plan, blocks, "Create an overview")["family"] == "product_showcase"
    assert select_composition({}, plan, blocks, "Create an overview")["family"] == "numbered_process"


def test_explicit_composition_overrides_brand_signals() -> None:
    content = "# Story\n\nA narrative paragraph."
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create a brief", blocks)
    composition = select_composition(
        {"visual_language": {"primary_mode": "illustration-led", "density": "balanced"}},
        plan,
        blocks,
        "Use an editorial narrative",
    )
    validate_composition(composition)
    assert composition["family"] == "editorial_narrative"
    assert composition["reason"] == "explicit_prompt"


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://localhost", "http://169.254.169.254/latest/meta-data"])
def test_private_fetch_targets_are_rejected(url: str) -> None:
    with pytest.raises(FetchError):
        assert_public_url(url)


def test_pdf_vertical_slice(tmp_path: Path) -> None:
    output = tmp_path / "designed.pdf"
    result = create_pdf(
        FIXTURES / "design-system.json",
        FIXTURES / "content.md",
        "Create a bold executive brief",
        output,
    )
    assert result["status"] == "succeeded"
    assert result["pages"] >= 2
    assert len(PdfReader(str(output)).pages) == result["pages"]
    assert Path(result["quality_report"]).exists()
    assert Path(result["layout_plan"]).exists()
    layout = read_json(Path(result["layout_plan"]))
    assert layout["composition"]["family"] in {
        "editorial_narrative", "asymmetric_feature_grid", "numbered_process", "modular_announcement", "product_showcase"
    }


@pytest.mark.skipif(_browser_executable() is None, reason="Chromium is required")
def test_html_pdf_vertical_slice(tmp_path: Path) -> None:
    output = tmp_path / "designed-html.pdf"
    result = create_html_pdf(
        FIXTURES / "design-system.json",
        FIXTURES / "content.md",
        "Create a bold executive brief",
        output,
    )
    assert result["status"] == "succeeded"
    assert result["renderer"] == "html-css/1"
    assert result["pages"] == 3
    assert Path(result["html"]).exists()
    assert Path(result["quality_report"]).exists()
    assert len(PdfReader(str(output)).pages) == 3


def test_deterministic_layout_uses_cards_and_preserves_block_ids() -> None:
    content = "# Launch\n\n## Features\n\n- One\n- Two\n- Three\n\n> Built for people.\n\n[Learn more](https://example.com)"
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create a bold launch brief called Product One with cards", blocks)
    validate_plan(plan, blocks)
    planned_ids = [block_id for section in plan["layout"]["sections"] for block_id in section["block_ids"]]
    assert planned_ids == [block["id"] for block in blocks]
    assert plan["title"] == "Product One"
    assert any(section["variant"] == "card_grid" for section in plan["layout"]["sections"])


def test_layout_validation_rejects_dropped_source_blocks() -> None:
    blocks = source_blocks("# Title\n\nBody")
    plan = DeterministicPlanner().plan("# Title\n\nBody", "Editorial brief", blocks)
    plan["layout"]["sections"][0]["block_ids"].pop()
    with pytest.raises(ValueError, match="omitted or invented"):
        validate_plan(plan, blocks)


def test_pdf_resolves_inline_markdown_and_removes_unsupported_emoji(tmp_path: Path) -> None:
    source = tmp_path / "announcement.md"
    source.write_text(
        "# ANNOUNCEMENT!!! 🚀\n\nThe **Kolo Seller Hub** is live. 🤝\n\n- **Deal Builder:** live pricing 💰\n- **Skill Building ⭐️:** quote a reusable skill\n\n> Built for people, not paperwork.\n\n[Learn more](https://example.com)\n",
        encoding="utf-8",
    )
    output = tmp_path / "announcement.pdf"
    result = create_pdf(
        FIXTURES / "design-system.json",
        source,
        "Create a bold launch announcement called Kolo Seller Hub",
        output,
    )
    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(str(output)).pages)
    report = read_json(Path(result["quality_report"]))
    layout = read_json(Path(result["layout_plan"]))
    assert "**" not in extracted
    assert not ({"■", "□", "�"} & set(extracted))
    assert "Kolo Seller Hub" in extracted
    assert report["checks"]["inline_markdown_resolved"] is True
    assert report["checks"]["tofu_glyphs_absent"] is True
    assert report["normalization"]["unsupported_emoji_removed"] >= 3
    assert layout["schema_version"] == 2
    assert layout["component_usage"]["cards"] == 2
    assert layout["component_usage"]["callouts"] == 1
    assert layout["component_usage"]["actions"] == 1
