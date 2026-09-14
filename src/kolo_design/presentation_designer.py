from __future__ import annotations

import json
import io
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from copy import deepcopy
from html import unescape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from PIL import Image as PILImage
from playwright.sync_api import sync_playwright

from .assets import raster_dimensions, select_logo_asset
from .browser_extract import _browser_executable

from .contracts import validate_design_system, validate_document_request
from .planner import source_blocks
from .presentation_planner import DeterministicPresentationPlanner, PresentationPlanner, validate_presentation_plan
from .presentation_art_direction import apply_presentation_art_direction
from .util import read_json, sha256_bytes, write_json


_PRESENTATION_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_PRESENTATION_LOGO_SUFFIXES = _PRESENTATION_IMAGE_SUFFIXES | {".svg"}


def _safe_svg_logo(path: Path) -> bool:
    """Accept self-contained SVG marks while rejecting active or remote content."""
    try:
        payload = path.read_bytes()
        if len(payload) > 2_000_000 or re.search(br"<!DOCTYPE|<!ENTITY", payload, re.IGNORECASE):
            return False
        root = ElementTree.fromstring(payload)
    except (OSError, ElementTree.ParseError):
        return False
    forbidden = {"script", "foreignobject", "iframe", "object", "embed"}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() in forbidden:
            return False
        for key, value in element.attrib.items():
            local_key = key.rsplit("}", 1)[-1].casefold()
            normalized = str(value).strip().casefold()
            if local_key.startswith("on"):
                return False
            if local_key in {"href", "src"} and normalized and not normalized.startswith("#"):
                return False
            if "url(" in normalized and "url(#" not in normalized:
                return False
    return True


def _select_presentation_logo(system: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer a real wordmark with enough pixels, while retaining vector marks."""
    vectors = [
        asset for asset in system.get("assets", [])
        if asset.get("kind") == "logo" and Path(str(asset.get("path", ""))).suffix.lower() == ".svg"
    ]
    if vectors:
        selected = dict(max(vectors, key=lambda asset: float(asset.get("score", 0))))
        selected["visual_luminance"] = _logo_visual_luminance(system, selected)
        return selected

    ranked: list[tuple[float, dict[str, Any]]] = []
    for asset in system.get("assets", []):
        if asset.get("kind") != "logo":
            continue
        enriched = select_logo_asset(
            {"assets": [asset]}, allow_svg=False, max_width=180, max_height=52, minimum_density=1.25
        )
        if not enriched:
            continue
        width, height = raster_dimensions(Path(str(enriched["path"]))) or (1, 1)
        ratio = width / max(1, height)
        source = " ".join(str(asset.get(key, "")) for key in ("source", "provenance")).casefold()
        semantic = float(asset.get("score", 0)) * 10
        semantic += 350 if ratio >= 1.8 else 0
        semantic += 250 if "visible-header-logo" in source else 0
        ranked.append((semantic + min(width * height / 1000, 300), enriched))
    if not ranked:
        return None
    selected = dict(max(ranked, key=lambda item: item[0])[1])
    selected["visual_luminance"] = _logo_visual_luminance(system, selected)
    return selected


def _logo_visual_luminance(system: dict[str, Any], selected: dict[str, Any]) -> float | None:
    """Estimate mark luminance from transparent pixels or an SVG's raster sibling."""
    selected_path = Path(str(selected.get("path", "")))
    candidates = [selected]
    if selected_path.suffix.lower() == ".svg":
        source_url = selected.get("source_url")
        candidates.extend(
            asset for asset in system.get("assets", [])
            if asset.get("kind") == "logo"
            and Path(str(asset.get("path", ""))).suffix.lower() in _PRESENTATION_IMAGE_SUFFIXES
            and (asset.get("source_url") == source_url or asset.get("id") == "primary-logo-raster")
        )
    for asset in candidates:
        path = Path(str(asset.get("path", "")))
        if path.suffix.lower() not in _PRESENTATION_IMAGE_SUFFIXES:
            continue
        try:
            with PILImage.open(path) as image:
                rgba = image.convert("RGBA")
                rgba.thumbnail((128, 128))
                weighted = total_alpha = 0.0
                for red, green, blue, alpha in rgba.getdata():
                    if alpha < 16:
                        continue
                    channels = []
                    for channel in (red, green, blue):
                        value = channel / 255
                        channels.append(value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4)
                    pixel_luminance = channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722
                    weight = alpha / 255
                    weighted += pixel_luminance * weight
                    total_alpha += weight
                if total_alpha:
                    return round(weighted / total_alpha, 4)
        except (OSError, ValueError):
            continue
    return None


def _node_runtime() -> Path:
    configured = os.environ.get("KOLO_PRESENTATION_NODE")
    candidate = Path(configured).expanduser() if configured else None
    if candidate and candidate.exists():
        return candidate.resolve()
    discovered = shutil.which("node")
    if not discovered:
        raise RuntimeError("PowerPoint generation requires Node.js")
    return Path(discovered).resolve()


def _presentation_runtime() -> Path:
    candidate = Path(__file__).resolve().parents[2] / "node_modules" / "pptxgenjs"
    if candidate.is_dir():
        return candidate
    raise RuntimeError(
        "PowerPoint generation requires the local JavaScript dependencies. Run `npm install --ignore-scripts` "
        "inside the Kolo Create skill directory."
    )


def _plain_source(value: str) -> str:
    value = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`", lambda match: next(part for part in match.groups() if part), value)
    value = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", value)
    return unescape(value).strip()


def _coverage_text(value: str) -> str:
    return re.sub(r"[\W_]+", " ", value.casefold(), flags=re.UNICODE).strip()


def _safe_presentation_system(system: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Keep unsupported image parsers outside the PptxGenJS process entirely."""
    safe_system = deepcopy(system)
    rejected: list[str] = []
    safe_assets: list[dict[str, Any]] = []
    for asset in safe_system.get("assets", []):
        asset_path = str(asset.get("path", ""))
        suffix = Path(asset_path).suffix.lower()
        if asset.get("kind") == "hero-image" and suffix not in _PRESENTATION_IMAGE_SUFFIXES:
            rejected.append(asset_path)
            continue
        if asset.get("kind") == "logo" and (
            suffix not in _PRESENTATION_LOGO_SUFFIXES
            or (suffix == ".svg" and not _safe_svg_logo(Path(asset_path)))
        ):
            rejected.append(asset_path)
            continue
        safe_assets.append(asset)
    safe_system["assets"] = safe_assets
    safe_system["presentation_logo"] = _select_presentation_logo(safe_system)
    return safe_system, rejected


def _normalize_presentation_images(system: dict[str, Any], preview_dir: Path) -> list[dict[str, Any]]:
    """Decode hero imagery once and give PowerPoint honest JPEG/PNG files.

    Browser image endpoints sometimes return WebP bytes behind a .jpg URL. PptxGenJS
    then cannot discover the intrinsic dimensions and stretches the image to its
    frame. Normalizing removes that ambiguity while retaining the source resolution.
    """
    normalized: list[dict[str, Any]] = []
    render_assets = preview_dir / "render-assets"
    for asset in system.get("assets", []):
        if asset.get("kind") != "hero-image":
            continue
        source = Path(str(asset.get("path", "")))
        try:
            with PILImage.open(source) as image:
                image.load()
                width, height = image.size
                has_alpha = image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                )
                render_assets.mkdir(parents=True, exist_ok=True)
                fingerprint = str(asset.get("sha256") or sha256_bytes(source.read_bytes()))[:12]
                suffix = ".png" if has_alpha else ".jpg"
                target = render_assets / f"{asset.get('id', 'image')}-{fingerprint}{suffix}"
                if has_alpha:
                    image.convert("RGBA").save(target, "PNG", optimize=True)
                    media_type = "image/png"
                else:
                    image.convert("RGB").save(
                        target, "JPEG", quality=95, subsampling=0, optimize=True
                    )
                    media_type = "image/jpeg"
                asset["original_path"] = str(source)
                asset["path"] = str(target)
                asset["media_type"] = media_type
                asset["pixel_width"] = width
                asset["pixel_height"] = height
                asset["aspect_ratio"] = round(width / max(1, height), 4)
                normalized.append({
                    "asset_id": str(asset.get("id", "image")),
                    "source": str(source),
                    "render_path": str(target),
                    "width": width,
                    "height": height,
                })
        except (OSError, ValueError):
            # The earlier allowlist is intentionally independent from decoding.
            # A broken asset remains available for the renderer's normal fallback.
            continue
    return normalized


def _repair_content_type_targets(path: Path) -> list[str]:
    """Remove PptxGenJS overrides that point at slide masters it did not write."""
    namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
    with zipfile.ZipFile(path) as source:
        names = set(source.namelist())
        root = ElementTree.fromstring(source.read("[Content_Types].xml"))
        removed: list[str] = []
        for override in list(root.findall(f"{{{namespace}}}Override")):
            part_name = override.get("PartName", "")
            if part_name.lstrip("/") not in names:
                root.remove(override)
                removed.append(part_name)
        if not removed:
            return []
        ElementTree.register_namespace("", namespace)
        content_types = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".pptx", delete=False) as temporary:
            repaired_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(repaired_path, "w", compression=zipfile.ZIP_DEFLATED) as repaired:
                for entry in source.infolist():
                    payload = content_types if entry.filename == "[Content_Types].xml" else source.read(entry.filename)
                    repaired.writestr(entry, payload)
            os.replace(repaired_path, path)
        finally:
            repaired_path.unlink(missing_ok=True)
    return removed


def _validate_package(path: Path, slide_count: int, blocks: list[dict[str, str]]) -> dict[str, int]:
    if not zipfile.is_zipfile(path):
        raise RuntimeError("PowerPoint renderer did not produce a valid OOXML package")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        required = {"[Content_Types].xml", "ppt/presentation.xml"}
        required.update(f"ppt/slides/slide{index}.xml" for index in range(1, slide_count + 1))
        if missing := required - names:
            raise RuntimeError(f"PowerPoint package is incomplete: {sorted(missing)}")
        content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
        content_namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
        missing_targets = [
            node.get("PartName", "")
            for node in content_types.findall(f"{{{content_namespace}}}Override")
            if node.get("PartName", "").lstrip("/") not in names
        ]
        if missing_targets:
            raise RuntimeError(f"PowerPoint content types reference missing package parts: {missing_targets[:3]}")
        namespaces = {
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        all_text: list[str] = []
        shape_count = 0
        image_count = 0
        oversized_text_walls = 0
        unbalanced_headlines = 0
        long_copy_orphans = 0
        unsafe_controlled_lines = 0
        distorted_images = 0
        misaligned_supporting_copy = 0
        footer_encroachments = 0
        feature_card_overflows = 0
        misaligned_feature_copy = 0
        for index in range(1, slide_count + 1):
            root = ElementTree.fromstring(archive.read(f"ppt/slides/slide{index}.xml"))
            all_text.extend(node.text or "" for node in root.findall(".//a:t", namespaces))
            shape_count += len(root.findall(".//p:sp", namespaces))
            image_count += len(root.findall(".//p:pic", namespaces))
            relationship_path = f"ppt/slides/_rels/slide{index}.xml.rels"
            relationships: dict[str, str] = {}
            if relationship_path in names:
                relationship_root = ElementTree.fromstring(archive.read(relationship_path))
                relationships = {
                    node.get("Id", ""): node.get("Target", "")
                    for node in relationship_root
                }
            for picture in root.findall(".//p:pic", namespaces):
                name_node = picture.find("p:nvPicPr/p:cNvPr", namespaces)
                if name_node is None or name_node.get("name") != "brand-image":
                    continue
                blip = picture.find("p:blipFill/a:blip", namespaces)
                extent = picture.find("p:spPr/a:xfrm/a:ext", namespaces)
                source_rect = picture.find("p:blipFill/a:srcRect", namespaces)
                if blip is None or extent is None:
                    continue
                relation_id = blip.get(f"{{{namespaces['r']}}}embed", "")
                target = relationships.get(relation_id, "")
                media_path = posixpath.normpath(posixpath.join("ppt/slides", target))
                if media_path not in names:
                    continue
                try:
                    with PILImage.open(io.BytesIO(archive.read(media_path))) as image:
                        source_ratio = image.width / max(1, image.height)
                except (OSError, ValueError):
                    continue
                frame_ratio = int(extent.get("cx", "0")) / max(1, int(extent.get("cy", "0")))
                crop_values = [
                    int(source_rect.get(edge, "0")) if source_rect is not None else 0
                    for edge in ("l", "r", "t", "b")
                ]
                if abs(source_ratio / max(frame_ratio, 0.001) - 1) > 0.01 and not any(crop_values):
                    distorted_images += 1
            copy_tops: dict[str, int] = {}
            feature_geometry: dict[str, tuple[int, int, int, int]] = {}
            for shape in root.findall(".//p:sp", namespaces):
                shape_text = " ".join(node.text or "" for node in shape.findall(".//a:t", namespaces)).strip()
                sizes = [int(node.get("sz", "0")) for node in shape.findall(".//a:rPr", namespaces)]
                max_size = max(sizes, default=0)
                if len(shape_text) > 150 and max_size > 2_600:
                    oversized_text_walls += 1
                paragraphs = [
                    " ".join(node.text or "" for node in paragraph.findall(".//a:t", namespaces)).strip()
                    for paragraph in shape.findall(".//a:p", namespaces)
                ]
                paragraphs = [paragraph for paragraph in paragraphs if paragraph]
                if len(shape_text) > 100 and max_size >= 1_400 and (
                    len(paragraphs) < 2 or any(len(paragraph.split()) < 2 for paragraph in paragraphs)
                ):
                    long_copy_orphans += 1
                name_node = shape.find("p:nvSpPr/p:cNvPr", namespaces)
                shape_name = name_node.get("name") if name_node is not None else ""
                if shape_name.startswith("feature-card-"):
                    offset = shape.find("p:spPr/a:xfrm/a:off", namespaces)
                    extent = shape.find("p:spPr/a:xfrm/a:ext", namespaces)
                    if offset is not None and extent is not None:
                        feature_geometry[shape_name] = (
                            int(offset.get("x", "0")), int(offset.get("y", "0")),
                            int(extent.get("cx", "0")), int(extent.get("cy", "0")),
                        )
                if shape_name in {"primary-copy", "supporting-copy"}:
                    offset = shape.find("p:spPr/a:xfrm/a:off", namespaces)
                    if offset is not None:
                        copy_tops[shape_name] = int(offset.get("y", "0"))
                if shape_name.startswith("feature-card-"):
                    offset = shape.find("p:spPr/a:xfrm/a:off", namespaces)
                    extent = shape.find("p:spPr/a:xfrm/a:ext", namespaces)
                    if offset is not None and extent is not None:
                        lower_edge = int(offset.get("y", "0")) + int(extent.get("cy", "0"))
                        if lower_edge > 640 * 9_525:
                            footer_encroachments += 1
                if shape_name in {"primary-copy", "supporting-copy", "closing-copy"} and max_size:
                    extent = shape.find("p:spPr/a:xfrm/a:ext", namespaces)
                    if extent is not None:
                        width_px = int(extent.get("cx", "0")) / 9_525
                        font_px = (max_size / 100) * 96 / 72
                        conservative_capacity = max(18, int(width_px / (font_px * 0.65)))
                        if any(len(paragraph) > conservative_capacity for paragraph in paragraphs):
                            unsafe_controlled_lines += 1
                if (
                    name_node is not None
                    and shape_name == "title"
                    and len(shape_text) >= 32
                    and max_size >= 4_000
                    and shape.find(".//a:br", namespaces) is None
                    and len(shape.findall(".//a:p", namespaces)) < 2
                ):
                    unbalanced_headlines += 1
            if {"primary-copy", "supporting-copy"} <= copy_tops.keys():
                if abs(copy_tops["primary-copy"] - copy_tops["supporting-copy"]) > 9_525:
                    misaligned_supporting_copy += 1
            card_ids = {
                name.removeprefix("feature-card-")
                for name in feature_geometry
                if name.removeprefix("feature-card-").isdigit()
            }
            for card_id in card_ids:
                card = feature_geometry.get(f"feature-card-{card_id}")
                title = feature_geometry.get(f"feature-card-title-{card_id}")
                copy = feature_geometry.get(f"feature-card-copy-{card_id}")
                if not card:
                    continue
                card_x, card_y, card_width, card_height = card
                horizontal_padding = 24 * 9_525
                vertical_padding = 8 * 9_525
                for child in (title, copy):
                    if child is None:
                        continue
                    x, y, width, height = child
                    if not (
                        x >= card_x + horizontal_padding
                        and y >= card_y + vertical_padding
                        and x + width <= card_x + card_width - horizontal_padding
                        and y + height <= card_y + card_height - vertical_padding
                    ):
                        feature_card_overflows += 1
                if title and copy and abs(title[0] - copy[0]) > 9_525:
                    misaligned_feature_copy += 1
            for transform in root.findall(".//a:xfrm", namespaces):
                offset = transform.find("a:off", namespaces)
                extent = transform.find("a:ext", namespaces)
                if offset is None or extent is None:
                    continue
                x, y = int(offset.get("x", "0")), int(offset.get("y", "0"))
                width, height = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
                if x < 0 or y < 0 or x + width > 12_192_000 or y + height > 6_858_000:
                    raise RuntimeError(f"PowerPoint slide {index} contains off-canvas geometry")
        rendered_text = _coverage_text(" ".join(all_text))
        omitted = [
            _plain_source(block["text"])
            for block in blocks
            if _coverage_text(_plain_source(block["text"])) not in rendered_text
        ]
        if omitted:
            raise RuntimeError(f"PowerPoint package omitted source text: {omitted[:3]}")
        if shape_count < slide_count:
            raise RuntimeError("PowerPoint package did not retain editable text and shapes")
        if oversized_text_walls:
            raise RuntimeError("PowerPoint package contains oversized walls of body copy")
        if unbalanced_headlines:
            raise RuntimeError("PowerPoint package contains a long display headline without a controlled line break")
        if long_copy_orphans:
            raise RuntimeError("PowerPoint package contains long copy without controlled, widow-safe line breaks")
        if unsafe_controlled_lines:
            raise RuntimeError("PowerPoint package contains a controlled text line that can reflow inside its box")
        if distorted_images:
            raise RuntimeError("PowerPoint package contains brand imagery stretched without an aspect-preserving crop")
        if misaligned_supporting_copy:
            raise RuntimeError("PowerPoint package contains supporting copy that is not top-aligned to its primary copy")
        if footer_encroachments:
            raise RuntimeError("PowerPoint package contains feature-card copy inside the protected footer zone")
        if feature_card_overflows:
            raise RuntimeError("PowerPoint package contains feature-card text inside its protected padding area")
        if misaligned_feature_copy:
            raise RuntimeError("PowerPoint package contains feature-card title and copy on different left edges")
        return {
            "editable_shapes": shape_count,
            "embedded_images": image_count,
            "oversized_text_walls": oversized_text_walls,
            "unbalanced_headlines": unbalanced_headlines,
            "long_copy_orphans": long_copy_orphans,
            "unsafe_controlled_lines": unsafe_controlled_lines,
            "distorted_images": distorted_images,
            "misaligned_supporting_copy": misaligned_supporting_copy,
            "footer_encroachments": footer_encroachments,
            "feature_card_overflows": feature_card_overflows,
            "misaligned_feature_copy": misaligned_feature_copy,
        }


def _render_previews(preview_dir: Path, slide_count: int) -> list[str]:
    browser_path = _browser_executable()
    if not browser_path:
        raise RuntimeError("Chromium is required for PowerPoint composition previews")
    previews: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_path)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
            for index in range(1, slide_count + 1):
                html_path = preview_dir / f"slide-{index:02d}.html"
                if not html_path.exists():
                    raise RuntimeError(f"PowerPoint composition preview is missing: {html_path.name}")
                page.goto(html_path.as_uri(), wait_until="load", timeout=20_000)
                output = preview_dir / f"slide-{index:02d}.png"
                page.screenshot(path=str(output), full_page=False)
                previews.append(str(output))
        finally:
            browser.close()
    return previews


def create_presentation(
    design_system_path: Path,
    content_path: Path,
    prompt: str,
    output_path: Path,
    planner: PresentationPlanner | None = None,
) -> dict[str, Any]:
    system = read_json(design_system_path)
    validate_design_system(system)
    render_system, rejected_images = _safe_presentation_system(system)
    content = content_path.read_text(encoding="utf-8")
    validate_document_request(content, prompt)
    blocks = source_blocks(content)
    planner = planner or DeterministicPresentationPlanner()
    plan = planner.plan(content, prompt, blocks)
    validate_presentation_plan(plan, blocks)
    plan = apply_presentation_art_direction(system, plan)

    output_path = output_path.resolve()
    if output_path.suffix.lower() != ".pptx":
        raise ValueError("PowerPoint output must use the .pptx extension")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    preview_dir = output_path.parent / f"{output_path.stem}-preview"
    normalized_images = _normalize_presentation_images(render_system, preview_dir)
    plan_path = output_path.with_suffix(".presentation-plan.json")
    quality_path = output_path.with_suffix(".quality.json")
    write_json(plan_path, plan)

    script_source = Path(__file__).resolve().parents[2] / "scripts" / "build_presentation.mjs"
    _presentation_runtime()
    with tempfile.TemporaryDirectory(prefix="kolo-create-pptx-") as temporary:
        build_dir = Path(temporary)
        spec_path = build_dir / "spec.json"
        spec_path.write_text(json.dumps({"system": render_system, "blocks": blocks, "plan": plan}, ensure_ascii=False), encoding="utf-8")
        process = subprocess.run(
            [str(_node_runtime()), str(script_source), str(spec_path), str(output_path), str(preview_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if process.returncode:
            message = (process.stderr or process.stdout).strip()
            output_path.with_suffix(".presentation-error.log").write_text(message, encoding="utf-8")
            lines = message.splitlines()
            excerpt = "\n".join((lines[:1] + [line[:1200] for line in lines[-8:]]))
            raise RuntimeError(f"PowerPoint renderer failed (exit {process.returncode}): {excerpt}")

    slide_count = len(plan["slides"])
    repaired_content_types = _repair_content_type_targets(output_path)
    package_counts = _validate_package(output_path, slide_count, blocks)
    previews = _render_previews(preview_dir, slide_count)
    layouts = sorted(str(path) for path in preview_dir.glob("slide-*.layout.json"))
    quality = {
        "status": "pass" if len(previews) == slide_count and len(layouts) == slide_count else "fail",
        "checks": {
            "valid_ooxml": True,
            "source_block_coverage": 1.0,
            "preview_count": len(previews),
            "layout_count": len(layouts),
            "expected_slides": slide_count,
            "editable_text_and_shapes": True,
            "editable_shape_count": package_counts["editable_shapes"],
            "embedded_image_count": package_counts["embedded_images"],
            "unsupported_images_rejected": len(rejected_images),
            "normalized_presentation_images": len(normalized_images),
            "off_canvas_geometry_absent": True,
            "stale_content_type_targets_repaired": len(repaired_content_types),
            "oversized_text_walls": package_counts["oversized_text_walls"],
            "unbalanced_headlines": package_counts["unbalanced_headlines"],
            "long_copy_orphans": package_counts["long_copy_orphans"],
            "unsafe_controlled_lines": package_counts["unsafe_controlled_lines"],
            "distorted_images": package_counts["distorted_images"],
            "misaligned_supporting_copy": package_counts["misaligned_supporting_copy"],
            "footer_encroachments": package_counts["footer_encroachments"],
            "feature_card_overflows": package_counts["feature_card_overflows"],
            "misaligned_feature_copy": package_counts["misaligned_feature_copy"],
            "distinct_layout_variants": len({slide["variant"] for slide in plan["slides"]}),
        },
        "art_direction": plan["art_direction"],
        "brand_mark": {
            "asset_id": (render_system.get("presentation_logo") or {}).get("id"),
            "format": Path(str((render_system.get("presentation_logo") or {}).get("path", ""))).suffix.lower() or None,
            "source": (render_system.get("presentation_logo") or {}).get("source"),
            "source_score": (render_system.get("presentation_logo") or {}).get("score"),
            "source_confidence": (render_system.get("presentation_logo") or {}).get("confidence"),
            "visual_luminance": (render_system.get("presentation_logo") or {}).get("visual_luminance"),
            "text_fallback": render_system.get("presentation_logo") is None,
            "policy": "native vector or density-checked raster; proportions preserved",
        },
        "preview": {
            "kind": "same-plan HTML composition preview",
            "literal_powerpoint_render": False,
            "note": "The Kolo pod has no LibreOffice. Actual PPTX structure and geometry are checked in OOXML; Chromium renders a parallel preview from the same layout calls.",
        },
        "font_portability": {
            "observed_display": system["tokens"]["typography"]["display_family"],
            "observed_body": system["tokens"]["typography"]["body_family"],
            "rendered_display": "Georgia" if system["tokens"]["typography"].get("display_fallback") == "serif" else "Arial",
            "rendered_body": "Georgia" if system["tokens"]["typography"].get("body_fallback") == "serif" else "Arial",
            "note": "Office-safe fonts preserve the extracted serif or sans-serif character without relying on a web font.",
        },
    }
    write_json(quality_path, quality)
    if quality["status"] != "pass":
        raise RuntimeError("PowerPoint visual QA artifacts are incomplete")
    output_path.with_suffix(".presentation-error.log").unlink(missing_ok=True)
    return {
        "status": "succeeded",
        "renderer": "pptxgenjs/1",
        "pptx": str(output_path),
        "sha256": sha256_bytes(output_path.read_bytes()),
        "slides": slide_count,
        "plan": str(plan_path),
        "previews": previews,
        "preview_html": sorted(str(path) for path in preview_dir.glob("slide-*.html")),
        "layouts": layouts,
        "quality": str(quality_path),
        "planner": planner.version,
        "art_direction": plan["art_direction"],
        "design_system": {"id": system["id"], "version": system["version"]},
    }
