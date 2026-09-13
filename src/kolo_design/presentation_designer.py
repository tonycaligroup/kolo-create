from __future__ import annotations

import json
import os
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

from playwright.sync_api import sync_playwright

from .browser_extract import _browser_executable

from .contracts import validate_design_system, validate_document_request
from .planner import source_blocks
from .presentation_planner import DeterministicPresentationPlanner, PresentationPlanner, validate_presentation_plan
from .util import read_json, sha256_bytes, write_json


_PRESENTATION_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


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
        if asset.get("kind") == "hero-image" and Path(asset_path).suffix.lower() not in _PRESENTATION_IMAGE_SUFFIXES:
            rejected.append(asset_path)
            continue
        safe_assets.append(asset)
    safe_system["assets"] = safe_assets
    return safe_system, rejected


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
        }
        all_text: list[str] = []
        shape_count = 0
        image_count = 0
        for index in range(1, slide_count + 1):
            root = ElementTree.fromstring(archive.read(f"ppt/slides/slide{index}.xml"))
            all_text.extend(node.text or "" for node in root.findall(".//a:t", namespaces))
            shape_count += len(root.findall(".//p:sp", namespaces))
            image_count += len(root.findall(".//p:pic", namespaces))
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
        return {"editable_shapes": shape_count, "embedded_images": image_count}


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

    output_path = output_path.resolve()
    if output_path.suffix.lower() != ".pptx":
        raise ValueError("PowerPoint output must use the .pptx extension")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    preview_dir = output_path.parent / f"{output_path.stem}-preview"
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
            "off_canvas_geometry_absent": True,
            "stale_content_type_targets_repaired": len(repaired_content_types),
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
        "design_system": {"id": system["id"], "version": system["version"]},
    }
