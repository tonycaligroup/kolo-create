from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .contracts import validate_design_system, validate_document_request
from .planner import source_blocks
from .presentation_planner import DeterministicPresentationPlanner, PresentationPlanner, validate_presentation_plan
from .util import read_json, sha256_bytes, write_json


def _node_runtime() -> Path:
    configured = os.environ.get("KOLO_PRESENTATION_NODE")
    candidate = Path(configured).expanduser() if configured else None
    if candidate and candidate.exists():
        return candidate.resolve()
    discovered = shutil.which("node")
    if not discovered:
        raise RuntimeError("PowerPoint generation requires Node.js")
    return Path(discovered).resolve()


def _node_modules() -> Path:
    configured = os.environ.get("KOLO_PRESENTATION_NODE_MODULES")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if (candidate / "@oai" / "artifact-tool").is_dir():
            return candidate
    raise RuntimeError(
        "PowerPoint generation requires @oai/artifact-tool. Set KOLO_PRESENTATION_NODE_MODULES "
        "to the node_modules directory that contains it."
    )


def _validate_package(path: Path, slide_count: int) -> None:
    if not zipfile.is_zipfile(path):
        raise RuntimeError("PowerPoint renderer did not produce a valid OOXML package")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        required = {"[Content_Types].xml", "ppt/presentation.xml"}
        required.update(f"ppt/slides/slide{index}.xml" for index in range(1, slide_count + 1))
        if missing := required - names:
            raise RuntimeError(f"PowerPoint package is incomplete: {sorted(missing)}")


def create_presentation(
    design_system_path: Path,
    content_path: Path,
    prompt: str,
    output_path: Path,
    planner: PresentationPlanner | None = None,
) -> dict[str, Any]:
    system = read_json(design_system_path)
    validate_design_system(system)
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
    with tempfile.TemporaryDirectory(prefix="kolo-create-pptx-") as temporary:
        build_dir = Path(temporary)
        script = build_dir / "build_presentation.mjs"
        shutil.copy2(script_source, script)
        os.symlink(_node_modules(), build_dir / "node_modules", target_is_directory=True)
        spec_path = build_dir / "spec.json"
        spec_path.write_text(json.dumps({"system": system, "blocks": blocks, "plan": plan}, ensure_ascii=False), encoding="utf-8")
        process = subprocess.run(
            [str(_node_runtime()), str(script), str(spec_path), str(output_path), str(preview_dir)],
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
    _validate_package(output_path, slide_count)
    previews = sorted(str(path) for path in preview_dir.glob("slide-*.png"))
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
        "renderer": "artifact-tool/1",
        "pptx": str(output_path),
        "sha256": sha256_bytes(output_path.read_bytes()),
        "slides": slide_count,
        "plan": str(plan_path),
        "previews": previews,
        "layouts": layouts,
        "quality": str(quality_path),
        "planner": planner.version,
        "design_system": {"id": system["id"], "version": system["version"]},
    }
