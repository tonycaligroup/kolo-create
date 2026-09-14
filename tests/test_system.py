from __future__ import annotations

import io
import json
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
from kolo_design.brand_director import DEFAULT_BRAND_MODEL
from kolo_design.brand_demonstration import require_demonstration_assets
from kolo_design.composition import select_composition, validate_composition
from kolo_design.content_map import build_content_map, validate_content_map
from kolo_design.contracts import validate_design_system
from kolo_design.design_grammar import compile_design_grammar, validate_design_grammar
from kolo_design.cli import parser
from kolo_design.browser_extract import _browser_executable, _trim_transparent_png, _visible_logo
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
from kolo_design.media_policy import crop_visible_fraction
from kolo_design.presentation_planner import DeterministicPresentationPlanner, validate_presentation_plan
from kolo_design.presentation_art_direction import apply_presentation_art_direction, presentation_profile
from kolo_design.presentation_similarity import compare_presentation_layouts, presentation_layout_identity
from kolo_design.presentation_layout import measure_presentation_layout
from kolo_design.scene_graph import build_scene_plan, validate_scene_plan
from kolo_design.presentation_designer import (
    _normalize_presentation_images,
    _safe_presentation_system,
    _select_presentation_logo,
    create_presentation,
)
from kolo_design.util import read_json

FIXTURES = Path(__file__).parent / "fixtures"


def test_design_grammar_compiles_continuous_brand_traits() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system.setdefault("components", {})["cards"] = {
        "radius": 18, "shadow": "0 8px 20px rgba(0,0,0,.12)", "observations": 6,
    }
    grammar = compile_design_grammar(system)
    validate_design_grammar(grammar)

    assert grammar["traits"]["curvature"] > 0.5
    assert grammar["traits"]["surface_layers"] > 0.5
    assert abs(sum(grammar["direction_weights"].values()) - 1) < 0.001
    assert grammar["recipes"]["card"]["observations"] == 6


def test_scene_plan_preserves_content_and_varies_by_brand_grammar() -> None:
    content = "# Title\n\nIntro copy.\n\n## Features\n\n- First benefit\n- Second benefit\n\n## Finish\n\n[Continue](https://example.com)"
    blocks = source_blocks(content)
    content_map = build_content_map(blocks, "Create a concise presentation")
    validate_content_map(content_map, blocks)

    product_system = read_json(FIXTURES / "design-system.json")
    product_system["visual_language"] = {"primary_mode": "product-led", "density": "sparse", "media_coverage": 0.4}
    product_system.setdefault("components", {})["cards"] = {"radius": 20, "shadow": "0 8px 20px #0003", "observations": 8}
    monochrome_system = read_json(FIXTURES / "design-system.json")
    monochrome_system["tokens"]["colors"].update({"accent": "#111111", "accent_secondary": "#222222"})
    monochrome_system["visual_language"] = {"primary_mode": "typography-led", "density": "sparse", "media_coverage": 0.0}

    product = build_scene_plan(
        compile_design_grammar(product_system), content_map,
        format_name="presentation", brand_id="product",
    )
    monochrome = build_scene_plan(
        compile_design_grammar(monochrome_system), content_map,
        format_name="presentation", brand_id="monochrome",
    )
    validate_scene_plan(product, blocks)
    validate_scene_plan(monochrome, blocks)

    assert product["signature"] != monochrome["signature"]
    assert [scene["component"] for scene in product["scenes"]] != [scene["component"] for scene in monochrome["scenes"]]
    assert all(len(scene["alternates"]) == 2 for scene in product["scenes"])


def test_fixture_system_is_valid() -> None:
    validate_design_system(read_json(FIXTURES / "design-system.json"))


def test_trim_transparent_logo_viewport(tmp_path: Path) -> None:
    source = PILImage.new("RGBA", (80, 80), (0, 0, 0, 0))
    for x in range(20, 60):
        for y in range(30, 50):
            source.putpixel((x, y), (0, 0, 0, 255))
    path = tmp_path / "logo.png"
    source.save(path)

    with PILImage.open(io.BytesIO(_trim_transparent_png(path.read_bytes()))) as trimmed:
        assert trimmed.size == (40, 20)


@pytest.mark.skipif(not _browser_executable(), reason="Chromium is required for logo extraction")
def test_visible_logo_prefers_small_exact_brand_svg() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=_browser_executable(), headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 500})
        page.set_content('''
          <header>
            <a href="/" aria-label="Example"><svg width="14" height="44" viewBox="0 0 14 44"><path d="M1 15h12v14H1z"/></svg></a>
            <a href="/store" aria-label="Store"><svg width="40" height="44" viewBox="0 0 40 44"><path d="M1 15h38v14H1z"/></svg></a>
          </header>
        ''')
        result = _visible_logo(page, "https://example.com/")
        browser.close()

    assert result is not None
    assert result["width"] == 14
    assert result["score"] >= 250
    with PILImage.open(io.BytesIO(result["png"])) as logo:
        assert logo.width > 100
        assert logo.height > 100


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


def test_cover_rejects_reference_only_webpage_capture(tmp_path: Path) -> None:
    hero = tmp_path / "page-capture.jpg"
    PILImage.new("RGB", (1440, 768), "navy").save(hero)
    system = read_json(FIXTURES / "design-system.json")
    system["assets"] = [{
        "id": "page-capture", "kind": "hero-image", "path": str(hero),
        "production_eligible": False, "asset_class": "reference-evidence",
        "alt": "Red Bull surfer",
    }]
    content = "# Kolo Create\n\nCreate a reusable design system."
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Explain Kolo Create", blocks)
    selected = select_component_plan(system, plan, blocks, "Explain Kolo Create", brand_demonstration=True)
    assert selected["cover"]["asset_id"] is None


def test_crop_visible_fraction_detects_severe_landscape_to_portrait_crop() -> None:
    assert crop_visible_fraction(1.875, 0.46) < 0.25
    assert crop_visible_fraction(1.875, 1.875) == pytest.approx(1.0)


def test_brand_demonstration_can_use_signature_media_without_topic_overlap(tmp_path: Path) -> None:
    hero = tmp_path / "rocket.jpg"
    PILImage.new("RGB", (1600, 900), "black").save(hero)
    system = read_json(FIXTURES / "design-system.json")
    system["assets"] = [{
        "id": "rocket", "kind": "hero-image", "path": str(hero),
        "alt": "Falcon rocket launch", "keywords": ["falcon", "rocket", "launch"],
    }]
    content = "# Kolo Create\n\nCreate a reusable design system."
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Explain Kolo Create", blocks)
    selected = select_component_plan(
        system, plan, blocks, "Explain Kolo Create", brand_demonstration=True
    )
    assert selected["cover"]["asset_id"] == "rocket"
    assert selected["cover"]["reason"] == "signature brand media selected for automatic brand demonstration"


def test_media_led_brand_demonstration_refuses_assetless_output() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system["visual_language"] = {"primary_mode": "media-led", "media_coverage": 0.72}
    with pytest.raises(RuntimeError, match="requires imagery"):
        require_demonstration_assets(
            system, hero_selected=False, logo_selected=False, format_name="PDF"
        )


def test_media_led_demonstration_uses_type_fallback_for_reference_only_capture() -> None:
    system = read_json(FIXTURES / "design-system.json")
    system["visual_language"] = {"primary_mode": "media-led", "media_coverage": 0.72}
    system["assets"] = [{
        "id": "composite", "kind": "hero-image", "path": "/tmp/composite.png",
        "production_eligible": False,
    }]
    result = require_demonstration_assets(
        system, hero_selected=False, logo_selected=False, format_name="PDF"
    )
    assert result["media_fallback"] == "type-led"


def test_site_benchmark_matrix_covers_ten_distinct_brands() -> None:
    matrix = read_json(Path(__file__).parents[1] / "benchmarks" / "site-matrix.json")
    sites = matrix["sites"]
    assert len(sites) == 10
    assert len({site["id"] for site in sites}) == 10
    hazards = {hazard for site in sites for hazard in site["hazards"]}
    assert {"video", "gradients", "illustration", "dense-commerce", "pale-contrast"} <= hazards


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
    assert args.brand_director == "auto"
    assert args.brand_model == DEFAULT_BRAND_MODEL
    assert parser().prog == "kolo-create"


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


def test_presentation_similarity_flags_reused_geometry_across_brands() -> None:
    left = {
        "art_direction": {"profile": "precision"},
        "slides": [
            {"archetype": "cover", "variant": "precision-index"},
            {"archetype": "closing", "variant": "precision-signature"},
        ],
    }
    same_geometry = {
        "art_direction": {"profile": "precision"},
        "slides": [
            {"archetype": "cover", "variant": "precision-index"},
            {"archetype": "closing", "variant": "precision-signature"},
        ],
    }
    kinetic = {
        "art_direction": {"profile": "kinetic"},
        "slides": [
            {"archetype": "cover", "variant": "kinetic-split"},
            {"archetype": "closing", "variant": "kinetic-finish"},
        ],
    }

    assert presentation_layout_identity(left)["signature"]
    assert compare_presentation_layouts(left, same_geometry)["nearly_identical"] is True
    assert compare_presentation_layouts(left, kinetic) == {
        "schema_version": 1,
        "similarity": 0.0,
        "nearly_identical": False,
        "left_signature": presentation_layout_identity(left)["signature"],
        "right_signature": presentation_layout_identity(kinetic)["signature"],
        "threshold": 0.8,
    }


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


@pytest.mark.skipif(not _browser_executable(), reason="Chromium is required for text measurement")
def test_product_presentation_layout_measures_multiline_card_titles() -> None:
    system = read_json(FIXTURES / "design-system.json")
    plan = {
        "art_direction": {"profile": "product"},
        "slides": [{
            "id": "slide-01", "archetype": "feature-list", "variant": "product-feature",
            "block_ids": ["b001"],
        }],
    }
    blocks = [{
        "id": "b001", "kind": "bullet",
        "text": "The brand stays consistent: Every new piece starts from the visual language you already approved.",
    }]
    measured = measure_presentation_layout(system, plan, blocks)
    card = measured["slides"]["slide-01"]["cards"][0]

    assert measured["request_count"] == 2
    assert card["label_lines"] >= 2
    assert card["label_height"] > 30


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
    assert quality["checks"]["supporting_rule_overlaps"] == 0
    assert quality["checks"]["distinct_layout_variants"] >= 3


def test_powerpoint_rejects_unsupported_image_formats_before_node(tmp_path: Path) -> None:
    logo = tmp_path / "logo.svg"
    safe_webp = tmp_path / "safe.webp"
    logo.write_text('<svg viewBox="0 0 200 50"><path d="M0 0h200v50H0z"/></svg>', encoding="utf-8")
    PILImage.new("RGB", (320, 180), "#222222").save(safe_webp, "WEBP")
    safe, rejected = _safe_presentation_system({
        "assets": [
            {"kind": "hero-image", "path": "/tmp/unsafe.heif"},
            {"kind": "hero-image", "path": str(safe_webp)},
            {"id": "brand-mark", "kind": "logo", "path": str(logo), "score": 100},
        ]
    })
    assert rejected == ["/tmp/unsafe.heif"]
    assert [asset["path"] for asset in safe["assets"]] == [str(safe_webp), str(logo)]
    assert safe["presentation_logo"]["id"] == "brand-mark"


def test_powerpoint_rejects_svg_bytes_disguised_as_jpeg(tmp_path: Path) -> None:
    fake_jpeg = tmp_path / "hero.jpg"
    fake_jpeg.write_text('<svg viewBox="0 0 400 200"><path d="M0 0h400v200H0z"/></svg>', encoding="utf-8")

    safe, rejected = _safe_presentation_system({
        "assets": [{"id": "hero-1", "kind": "hero-image", "path": str(fake_jpeg)}]
    })

    assert rejected == [str(fake_jpeg)]
    assert safe["assets"] == []


def test_presentation_logo_rejects_promotional_lockup_and_ambiguous_nav_crop(tmp_path: Path) -> None:
    promo = tmp_path / "promo.png"
    nav_crop = tmp_path / "nav.png"
    PILImage.new("RGBA", (400, 80), (255, 255, 255, 255)).save(promo)
    PILImage.new("RGB", (94, 88), "white").save(nav_crop)
    selected = _select_presentation_logo({
        "assets": [
            {"id": "product-lockup", "kind": "logo", "path": str(promo), "source_url": "https://example.com/images/promo_logo_product.png", "score": 500},
            {"id": "ambiguous-nav", "kind": "logo", "path": str(nav_crop), "source": "visible-header-logo", "source_url": "https://example.com/", "css_width": 46, "css_height": 44, "score": 200},
        ]
    })

    assert selected is None


def test_presentation_logo_prefers_large_visible_wordmark_over_square_vector_icon(tmp_path: Path) -> None:
    icon = tmp_path / "icon.svg"
    wordmark = tmp_path / "wordmark.png"
    icon.write_text('<svg viewBox="0 0 34 34"><path d="M0 0h34v34H0z"/></svg>', encoding="utf-8")
    PILImage.new("RGBA", (1200, 240), (0, 0, 0, 255)).save(wordmark)

    selected = _select_presentation_logo({
        "assets": [
            {"id": "icon", "kind": "logo", "path": str(icon), "score": 200},
            {
                "id": "wordmark", "kind": "logo", "path": str(wordmark),
                "source": "visible-header-logo", "score": 260,
            },
        ]
    })

    assert selected is not None
    assert selected["id"] == "wordmark"


def test_presentation_logo_accepts_visible_wordmark_at_native_size(tmp_path: Path) -> None:
    wordmark = tmp_path / "wordmark.png"
    PILImage.new("RGBA", (179, 42), (88, 204, 2, 255)).save(wordmark)

    selected = _select_presentation_logo({
        "assets": [{
            "id": "visible-wordmark", "kind": "logo", "path": str(wordmark),
            "source": "visible-header-logo", "score": 185,
        }]
    })

    assert selected is not None
    assert selected["id"] == "visible-wordmark"


def test_generic_page_image_cannot_masquerade_as_logo(tmp_path: Path) -> None:
    product = tmp_path / "product.jpg"
    PILImage.new("RGB", (1344, 135), "white").save(product)

    selected = _select_presentation_logo({
        "assets": [{
            "id": "not-a-logo", "kind": "logo", "path": str(product),
            "source": "image", "source_url": "https://example.com/campaign.jpg", "score": 500,
        }]
    })

    assert selected is None


def test_sparse_broken_image_placeholder_cannot_masquerade_as_logo(tmp_path: Path) -> None:
    placeholder = tmp_path / "broken.png"
    image = PILImage.new("RGBA", (480, 480), (0, 0, 0, 0))
    for x in range(45):
        for y in range(45):
            image.putpixel((x, y), (192, 192, 192, 255))
    image.save(placeholder)

    selected = _select_presentation_logo({
        "assets": [{
            "id": "broken", "kind": "logo", "path": str(placeholder),
            "source": "visible-header-logo", "css_width": 78, "css_height": 78, "score": 300,
        }]
    })

    assert selected is None


def test_near_solid_broken_image_capture_cannot_masquerade_as_logo(tmp_path: Path) -> None:
    placeholder = tmp_path / "opaque-broken.png"
    image = PILImage.new("RGBA", (480, 480), (0, 0, 0, 255))
    for x in range(42):
        for y in range(42):
            image.putpixel((x, y), (190, 210, 225, 255))
    image.save(placeholder)

    selected = _select_presentation_logo({
        "assets": [{
            "id": "opaque-broken", "kind": "logo", "path": str(placeholder),
            "source": "visible-header-logo", "css_width": 78, "css_height": 78, "score": 300,
        }]
    })

    assert selected is None


def test_sparse_full_bbox_browser_placeholder_cannot_masquerade_as_logo(tmp_path: Path) -> None:
    placeholder = tmp_path / "scattered-broken.png"
    image = PILImage.new("RGBA", (480, 480), (0, 0, 0, 0))
    for point in ((0, 0), (479, 0), (0, 479), (479, 479)):
        image.putpixel(point, (180, 200, 220, 255))
    for x in range(30):
        for y in range(30):
            image.putpixel((x + 4, y + 4), (180, 200, 220, 255))
    image.save(placeholder)

    selected = _select_presentation_logo({
        "assets": [{
            "id": "scattered-broken", "kind": "logo", "path": str(placeholder),
            "source": "visible-header-logo", "css_width": 78, "css_height": 78, "score": 300,
        }]
    })

    assert selected is None


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

    def fake_create_pdf(system: Path, content: Path, prompt: str, output: Path, planner: object, **options: object) -> dict[str, str]:
        captured.update(system=system, content=content, output=output, **options)
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
    assert captured["brand_demonstration"] is True


def test_create_design_system_automatically_renders_default_first_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
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

    def fake_create_pdf(system: Path, content: Path, prompt: str, output: Path, planner: object, **options: object) -> dict[str, str]:
        captured.update(system=system, content=content, output=output, **options)
        return {"status": "succeeded", "pdf": str(output)}

    monkeypatch.setattr(cli_module, "create_pdf", fake_create_pdf)
    powerpoint: dict[str, Path] = {}
    monkeypatch.setattr(
        cli_module,
        "create_presentation",
        lambda system, content, prompt, output, planner, **options: powerpoint.update(system=system, content=content, output=output, **options) or {"status": "succeeded", "pptx": str(output)},
    )
    status = cli_module.main([
        "create", "design-system", "--url", "https://example.com", "--workspace", str(tmp_path),
    ])
    assert status == 0
    assert captured["system"] == system_path
    assert captured["output"] == tmp_path / "examples" / "sample-brand-kolo-create.pdf"
    assert captured["content"].name == "kolo-create-explainer.md"
    assert powerpoint["output"] == tmp_path / "examples" / "sample-brand-kolo-create.pptx"
    assert captured["brand_demonstration"] is True
    assert powerpoint["brand_demonstration"] is True
    result = json.loads(capsys.readouterr().out)
    assert [artifact["kind"] for artifact in result["artifacts"]] == ["pdf", "powerpoint"]
    assert result["delivery"]["status"] == "pending-agent-delivery"
    assert "receipts" in result["delivery"]["completion_rule"]


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

    def fake_create_pdf(system: Path, content: Path, prompt: str, output: Path, planner: object, **options: object) -> dict[str, str]:
        captured.update(system=system, output=output, **options)
        return {"status": "succeeded", "pdf": str(output)}

    monkeypatch.setattr(cli_module, "create_pdf", fake_create_pdf)
    powerpoint: dict[str, Path] = {}
    monkeypatch.setattr(
        cli_module,
        "create_presentation",
        lambda system, content, prompt, output, planner, **options: powerpoint.update(system=system, output=output, **options) or {"status": "succeeded", "pptx": str(output)},
    )
    status = cli_module.main([
        "create", "design-system", "--source-dir", str(source), "--workspace", str(tmp_path),
    ])
    assert status == 0
    assert captured["system"] == system_path
    assert captured["output"] == tmp_path / "examples" / "source-brand-kolo-create.pdf"
    assert powerpoint["output"] == tmp_path / "examples" / "source-brand-kolo-create.pptx"
    assert captured["brand_demonstration"] is True
    assert powerpoint["brand_demonstration"] is True


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
