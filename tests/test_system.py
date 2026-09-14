from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from PIL import Image as PILImage
from pypdf import PdfReader
from reportlab.lib.enums import TA_CENTER, TA_LEFT

import kolo_design.cli as cli_module
from kolo_design.assets import select_logo_asset
from kolo_design.brand_components import build_brand_components, select_component_plan, validate_component_plan
from kolo_design.composition import select_composition, validate_composition
from kolo_design.contracts import validate_design_system
from kolo_design.cli import parser
from kolo_design.browser_extract import _browser_executable
from kolo_design.html_designer import _document_html, _inline_html, _page_groups, create_html_pdf
from kolo_design.network import FetchError, assert_public_url
from kolo_design.pdf_designer import (
    _bounded_radius,
    _brand_dark,
    _cover_alignment,
    _eyebrow_color,
    _feature_parts,
    _legible_foreground,
    _paired_grid_rows,
    _preferred_foreground,
    create_pdf,
)
from kolo_design.planner import DeterministicPlanner, source_blocks, validate_plan
from kolo_design.presentation_planner import DeterministicPresentationPlanner, validate_presentation_plan
from kolo_design.presentation_art_direction import apply_presentation_art_direction, presentation_profile
from kolo_design.presentation_layout import measure_presentation_layout
from kolo_design.presentation_designer import (
    _normalize_presentation_images,
    _safe_presentation_system,
    create_presentation,
)
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


def test_html_cover_uses_hero_image_once(tmp_path: Path) -> None:
    hero = tmp_path / "hero.jpg"
    PILImage.new("RGB", (1200, 700), "navy").save(hero)
    system = read_json(FIXTURES / "design-system.json")
    system["assets"] = [{"id": "launch-hero", "kind": "hero-image", "path": str(hero), "alt": "Launch product showcase"}]
    content = "# Launch\n\nA concise introduction."
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create a product showcase", blocks)
    plan["composition"] = select_composition(system, plan, blocks, "Create a product showcase")
    plan["component_plan"] = select_component_plan(system, plan, blocks, "Create a product showcase")
    markup, _ = _document_html(system, plan, blocks)
    assert markup.count(hero.resolve().as_uri()) == 1
    assert 'class="hero-frame"' in markup
    assert 'class="page cover hero-landscape"' in markup


def test_cover_rejects_unrelated_brand_media(tmp_path: Path) -> None:
    hero = tmp_path / "football.jpg"
    PILImage.new("RGB", (1600, 900), "navy").save(hero)
    system = read_json(FIXTURES / "design-system.json")
    system["assets"] = [{
        "id": "football", "kind": "hero-image", "path": str(hero),
        "alt": "football athlete on the field", "keywords": ["football", "athlete"],
    }]
    content = "# Seller Hub announcement\n\nManage marketplace inventory and orders."
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create an operational announcement", blocks)
    selected = select_component_plan(system, plan, blocks, "Create an operational announcement")
    assert selected["cover"]["component"] == "type-led-cover"
    assert selected["cover"]["asset_id"] is None


def test_cover_ignores_generic_metadata_overlap(tmp_path: Path) -> None:
    hero = tmp_path / "travel.jpg"
    PILImage.new("RGB", (1400, 900), "black").save(hero)
    system = read_json(FIXTURES / "design-system.json")
    system["assets"] = [{
        "id": "travel", "kind": "hero-image", "path": str(hero),
        "alt": "Travel collage with taxis and luggage", "role": "main",
        "source_url": "https://cdn.example.com/media.jpg?format=webp",
    }]
    content = "# Kolo Create\n\nDesign documents and formats from one brand system."
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create a visual brand document", blocks)
    selected = select_component_plan(system, plan, blocks, "Create a visual brand document")
    assert selected["cover"]["component"] == "type-led-cover"


def test_monochrome_sections_use_open_editorial_features() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system["tokens"]["colors"] = {
        "background": "#FFFFFF", "surface": "#E5E5E5", "text": "#000000",
        "accent": "#000000", "accent_secondary": "#000000",
    }
    content = "# Title\n\n## Capabilities\n\n- Source: Pull from a website or repository\n- System: Save the reusable language"
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Explain the product", blocks)
    selected = select_component_plan(system, plan, blocks, "Explain the product")
    section = next(item for item in selected["sections"] if item["treatment"] == "editorial-feature-list")
    assert "feature-band" not in section["components"]


def test_editorial_feature_parser_preserves_bold_label_boundary() -> None:
    assert _feature_parts("**01. Clear priorities:** See the work that matters.", 1) == (
        "Clear priorities", "See the work that matters.",
    )


def test_brand_component_plan_is_restrained_and_shared() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system["tokens"]["colors"].update({"accent_secondary": "#F9CB10", "brand_dark": "#000F1E"})
    system["evidence"]["colors"] = [
        {"value": system["tokens"]["colors"]["accent"], "occurrences": 100},
        {"value": "#F9CB10", "occurrences": 50},
    ]
    system["brand_components"] = build_brand_components(system)
    content = "# Title\n\n## First\n\n- One\n- Two\n\n## Second\n\n- Three\n- Four\n\n## Finish\n\n[Continue](https://example.com)"
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create a product showcase", blocks)
    component_plan = select_component_plan(system, plan, blocks)
    validate_component_plan(component_plan)
    assert component_plan["preferred_renderer"] == "reportlab"
    assert component_plan["library"]["components"]["section-marker"]["style"] == "dual-tone"
    assert sum(section["treatment"] == "feature-band" for section in component_plan["sections"]) == 1


def test_brand_components_reject_weak_semantic_secondary_color() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system["tokens"]["colors"].update({"accent": "#007AFF", "accent_secondary": "#16A34A"})
    system["evidence"]["colors"] = [
        {"value": "#007AFF", "occurrences": 251},
        {"value": "#16A34A", "occurrences": 44},
    ]
    library = build_brand_components(system)
    marker = library["components"]["section-marker"]
    assert marker["style"] == "solid"
    assert marker["secondary"] == "#007AFF"


def test_monochrome_brand_uses_open_grid_and_dark_surface() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system["tokens"]["colors"] = {
        "background": "#000000", "surface": "#292929", "text": "#FFFFFF",
        "accent": "#FFFFFF", "accent_secondary": "#FFFFFF",
    }
    library = build_brand_components(system)
    assert library["components"]["feature-band"]["background"] == "#292929"
    assert library["components"]["numbered-feature-grid"]["cell_style"] == "open"


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


def test_create_design_system_source_contract() -> None:
    args = parser().parse_args([
        "create", "design-system", "--source-archive", "./site.zip", "--workspace", "./data",
    ])
    assert args.source_archive == Path("./site.zip")
    assert args.url is None


def test_compare_renderers_command_contract() -> None:
    args = parser().parse_args([
        "pdf", "compare", "--system", "./system.json", "--content", "./content.md",
        "--prompt", "Create a brief", "--output-dir", "./comparison",
    ])
    assert args.pdf_command == "compare"
    assert args.output_dir == Path("./comparison")
    assert args.planner == "deterministic"


def test_powerpoint_create_command_contract() -> None:
    args = parser().parse_args([
        "powerpoint", "create", "--system", "./system.json", "--content", "./content.md",
        "--prompt", "Create a concise deck", "--output", "./deck.pptx",
    ])
    assert args.command == "powerpoint"
    assert args.powerpoint_command == "create"
    assert args.output == Path("./deck.pptx")
    assert args.planner == "deterministic"


def test_presentation_plan_preserves_blocks_and_uses_slide_archetypes() -> None:
    content = (Path(__file__).parents[1] / "assets" / "kolo-create-explainer.md").read_text(encoding="utf-8")
    blocks = source_blocks(content)
    plan = DeterministicPresentationPlanner().plan(content, "Create a concise presentation", blocks)
    validate_presentation_plan(plan, blocks)
    planned = [block_id for slide in plan["slides"] for block_id in slide["block_ids"]]
    assert planned == [block["id"] for block in blocks]
    assert plan["aspect_ratio"] == "16:9"
    assert plan["slides"][0]["archetype"] == "cover"
    assert {slide["archetype"] for slide in plan["slides"]} >= {"cover", "process", "feature-list", "closing"}


@pytest.mark.parametrize(("system", "expected"), [
    ({"tokens": {"typography": {"display_fallback": "serif"}, "colors": {"accent": "#FFE01B"}}}, "editorial"),
    ({"tokens": {"typography": {"display_fallback": "sans-serif"}, "colors": {"accent": "#111111"}}}, "monochrome"),
    ({"tokens": {"typography": {"display_fallback": "sans-serif"}, "colors": {"accent": "#FFDB00"}}, "visual_language": {"primary_mode": "product-led"}}, "product"),
    ({"tokens": {"typography": {"display_fallback": "sans-serif"}, "colors": {"accent": "#D2003C"}}, "visual_language": {"media_coverage": 0.9}}, "kinetic"),
    ({"tokens": {"typography": {"display_fallback": "sans-serif"}, "colors": {"accent": "#007AFF"}}, "visual_language": {"media_coverage": 0.1}}, "precision"),
])
def test_presentation_profile(system: dict[str, object], expected: str) -> None:
    assert presentation_profile(system) == expected


def test_presentation_art_direction_assigns_inspectable_variants() -> None:
    plan = {"slides": [{"archetype": "cover"}, {"archetype": "closing"}]}
    system = {
        "tokens": {"typography": {"display_fallback": "serif"}, "colors": {"accent": "#FFE01B"}},
    }
    directed = apply_presentation_art_direction(system, plan)
    assert directed["art_direction"]["profile"] == "editorial"
    assert [slide["variant"] for slide in directed["slides"]] == ["editorial-poster", "editorial-signoff"]


@pytest.mark.skipif(not _browser_executable(), reason="Chromium is required for text measurement")
def test_presentation_layout_measures_wrapped_card_copy() -> None:
    system = read_json(FIXTURES / "design-system.json")
    plan = {
        "art_direction": {"profile": "kinetic"},
        "slides": [{
            "id": "slide-01", "archetype": "feature-list", "block_ids": ["b001"],
        }],
    }
    blocks = [{
        "id": "b001", "kind": "bullet",
        "text": "Measured card: This deliberately long description must wrap across multiple rendered lines so the card can hug its actual contents.",
    }]
    measured = measure_presentation_layout(system, plan, blocks)
    card = measured["slides"]["slide-01"]["cards"][0]

    assert measured["engine"] == "chromium-dom/1"
    assert measured["request_count"] == 2
    assert card["detail_lines"] >= 2
    assert card["detail_height"] > 19


@pytest.mark.skipif(not (Path(__file__).parents[1] / "node_modules" / "pptxgenjs").is_dir(), reason="run npm install for presentation tests")
def test_powerpoint_vertical_slice(tmp_path: Path) -> None:
    output = tmp_path / "designed.pptx"
    result = create_presentation(
        FIXTURES / "design-system.json",
        FIXTURES / "content.md",
        "Create a concise brand presentation",
        output,
    )
    assert result["status"] == "succeeded"
    assert result["renderer"] == "pptxgenjs/1"
    assert result["slides"] == 4
    assert zipfile.is_zipfile(output)
    assert len(result["previews"]) == result["slides"]
    assert len(result["preview_html"]) == result["slides"]
    assert Path(result["quality"]).exists()
    quality = read_json(Path(result["quality"]))
    assert quality["checks"]["stale_content_type_targets_repaired"] == result["slides"] - 1
    assert quality["checks"]["oversized_text_walls"] == 0
    assert quality["checks"]["unbalanced_headlines"] == 0
    assert quality["checks"]["long_copy_orphans"] == 0
    assert quality["checks"]["unsafe_controlled_lines"] == 0
    assert quality["checks"]["distorted_images"] == 0
    assert quality["checks"]["misaligned_supporting_copy"] == 0
    assert quality["checks"]["footer_encroachments"] == 0
    assert quality["checks"]["feature_card_overflows"] == 0
    assert quality["checks"]["misaligned_feature_copy"] == 0
    assert quality["checks"]["distinct_layout_variants"] >= 3


def test_powerpoint_rejects_unsupported_image_formats_before_node(tmp_path: Path) -> None:
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg viewBox="0 0 200 50"><path d="M0 0h200v50H0z"/></svg>', encoding="utf-8")
    safe, rejected = _safe_presentation_system({
        "assets": [
            {"kind": "hero-image", "path": "/tmp/unsafe.heif"},
            {"kind": "hero-image", "path": "/tmp/safe.webp"},
            {"id": "brand-mark", "kind": "logo", "path": str(logo), "score": 100},
        ]
    })
    assert rejected == ["/tmp/unsafe.heif"]
    assert [asset["path"] for asset in safe["assets"]] == ["/tmp/safe.webp", str(logo)]
    assert safe["presentation_logo"]["id"] == "brand-mark"


def test_powerpoint_rejects_active_svg_logo(tmp_path: Path) -> None:
    logo = tmp_path / "unsafe.svg"
    logo.write_text('<svg viewBox="0 0 100 20"><script>alert(1)</script></svg>', encoding="utf-8")
    safe, rejected = _safe_presentation_system({"assets": [{"kind": "logo", "path": str(logo)}]})
    assert rejected == [str(logo)]
    assert safe["assets"] == []
    assert safe["presentation_logo"] is None


def test_powerpoint_normalizes_mislabeled_webp_without_changing_geometry(tmp_path: Path) -> None:
    source = tmp_path / "hero.jpg"
    PILImage.new("RGB", (320, 120), "#CC1E2C").save(source, "WEBP", quality=95)
    system = {
        "assets": [{
            "id": "hero-1", "kind": "hero-image", "path": str(source),
            "media_type": "image/webp", "sha256": "abc123",
        }]
    }
    normalized = _normalize_presentation_images(system, tmp_path / "preview")
    target = Path(system["assets"][0]["path"])

    assert len(normalized) == 1
    assert target.suffix == ".jpg"
    assert target.read_bytes().startswith(b"\xff\xd8\xff")
    with PILImage.open(target) as image:
        assert image.size == (320, 120)
        assert image.format == "JPEG"
    assert system["assets"][0]["aspect_ratio"] == pytest.approx(320 / 120, rel=0.001)


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
        lambda url, workspace, name: {"status": "succeeded", "design_system": str(system_path), "brand_id": "sample-brand"},
    )
    captured: dict[str, Path] = {}

    def fake_create_pdf(system: Path, content: Path, prompt: str, output: Path, planner: object) -> dict[str, str]:
        captured.update(system=system, content=content, output=output)
        return {"status": "succeeded", "pdf": str(output)}

    monkeypatch.setattr(cli_module, "create_pdf", fake_create_pdf)
    monkeypatch.setattr(cli_module, "create_presentation", lambda *args, **kwargs: {"status": "succeeded", "pptx": str(tmp_path / "first-example.pptx")})
    status = cli_module.main([
        "create", "design-system", "--url", "https://example.com", "--workspace", str(tmp_path),
        "--example-output", str(example_path),
    ])
    assert status == 0
    assert captured["system"] == system_path
    assert captured["output"] == example_path
    assert captured["content"].name == "kolo-create-explainer.md"
    assert captured["content"].exists()


def test_create_design_system_automatically_renders_default_first_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system_path = tmp_path / "design-system.json"
    monkeypatch.setattr(
        cli_module,
        "extract_brand",
        lambda url, workspace, name: {
            "status": "succeeded", "design_system": str(system_path), "brand_id": "sample-brand",
        },
    )
    captured: dict[str, Path] = {}

    def fake_create_pdf(system: Path, content: Path, prompt: str, output: Path, planner: object) -> dict[str, str]:
        captured.update(system=system, content=content, output=output)
        return {"status": "succeeded", "pdf": str(output)}

    monkeypatch.setattr(cli_module, "create_pdf", fake_create_pdf)
    powerpoint: dict[str, Path] = {}
    monkeypatch.setattr(
        cli_module,
        "create_presentation",
        lambda system, content, prompt, output, planner: powerpoint.update(system=system, content=content, output=output) or {"status": "succeeded", "pptx": str(output)},
    )
    status = cli_module.main([
        "create", "design-system", "--url", "https://example.com", "--workspace", str(tmp_path),
    ])
    assert status == 0
    assert captured["system"] == system_path
    assert captured["output"] == tmp_path / "examples" / "sample-brand-kolo-create.pdf"
    assert captured["content"].name == "kolo-create-explainer.md"
    assert powerpoint["output"] == tmp_path / "examples" / "sample-brand-kolo-create.pptx"


def test_source_design_system_also_renders_default_first_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "frontend"
    system_path = tmp_path / "design-system.json"
    monkeypatch.setattr(
        cli_module,
        "extract_source_brand",
        lambda workspace, name, **inputs: {
            "status": "succeeded", "design_system": str(system_path), "brand_id": "source-brand",
        },
    )
    captured: dict[str, Path] = {}

    def fake_create_pdf(system: Path, content: Path, prompt: str, output: Path, planner: object) -> dict[str, str]:
        captured.update(system=system, output=output)
        return {"status": "succeeded", "pdf": str(output)}

    monkeypatch.setattr(cli_module, "create_pdf", fake_create_pdf)
    powerpoint: dict[str, Path] = {}
    monkeypatch.setattr(
        cli_module,
        "create_presentation",
        lambda system, content, prompt, output, planner: powerpoint.update(system=system, output=output) or {"status": "succeeded", "pptx": str(output)},
    )
    status = cli_module.main([
        "create", "design-system", "--source-dir", str(source), "--workspace", str(tmp_path),
    ])
    assert status == 0
    assert captured["system"] == system_path
    assert captured["output"] == tmp_path / "examples" / "source-brand-kolo-create.pdf"
    assert powerpoint["output"] == tmp_path / "examples" / "source-brand-kolo-create.pptx"


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


def test_html_cover_without_hero_uses_open_composition() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system["assets"] = [asset for asset in system["assets"] if asset.get("kind") != "hero"]
    content = (FIXTURES / "content.md").read_text(encoding="utf-8")
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create an editorial brief", blocks)
    plan["composition"] = select_composition(system, plan, blocks, "Create an editorial brief")
    markup, _ = _document_html(system, plan, blocks)
    assert 'class="page cover hero-none"' in markup
    assert "hero-abstract" not in markup


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
