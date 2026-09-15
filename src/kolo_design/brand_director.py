from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from PIL import Image

from .brand_components import build_brand_components
from .contracts import validate_design_system
from .design_grammar import compile_design_grammar
from .util import read_json, write_json


DEFAULT_BRAND_MODEL = "openai/gpt-5.6-sol"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
VISUAL_MODES = {
    "typography-led", "product-led", "media-led", "illustration-led", "editorial-led", "utility-led",
}
ACCENT_ROLES = {"dominant", "supporting", "minor-functional", "neutral", "avoid"}
PAGE_MARKERS = {"cover-only", "all-pages", "none"}
SECTION_MARKERS = {"selective", "frequent", "none"}


PRODUCTION_USE_VALUES = {"allow", "reference-only", "uncertain"}


def _deterministic_production_eligible(asset: dict[str, Any]) -> bool:
    """Read the deterministic media verdict, ignoring any earlier director run.

    ``asset_class`` is written by the deterministic media policy and never by the
    director, so it remains authoritative when the director is applied again to a
    system that already carries a previous ``production_eligible`` verdict.
    """
    asset_class = asset.get("asset_class")
    if asset_class is not None:
        return asset_class != "reference-evidence"
    return asset.get("production_eligible") is not False


class BrandDirectionError(RuntimeError):
    pass


def _evidence(system: dict[str, Any]) -> dict[str, Any]:
    colors = (system.get("evidence") or {}).get("colors") or []
    assets = [
        {
            key: asset.get(key)
            for key in (
                "id", "kind", "source", "alt", "text_sample", "keywords", "pixel_width", "pixel_height",
                "score", "asset_class", "production_eligible", "reuse_reasons", "embedded_content",
            )
            if asset.get(key) not in (None, "", [])
        }
        for asset in system.get("assets") or []
        if asset.get("kind") in {"logo", "hero-image"}
    ][:18]
    return {
        "brand": {"id": system["id"], "name": system["name"], "source": system.get("source")},
        "current_tokens": system["tokens"],
        "current_visual_language": system.get("visual_language") or {},
        "observed_colors": [
            {key: item.get(key) for key in ("value", "occurrences", "reference_status", "viewport_share") if item.get(key) is not None}
            for item in colors[:24]
        ],
        "logo_colors": (system.get("evidence") or {}).get("logo_colors") or [],
        "candidate_assets": assets,
    }


def _prompt(system: dict[str, Any], attachments: list[dict[str, str]], rejection: str | None = None) -> str:
    schema = {
        "schema_version": 1,
        "brand_name": "string",
        "identity_confidence": "number 0..1",
        "dominant_character": ["up to 6 short strings"],
        "accent_role": "dominant|supporting|minor-functional|neutral|avoid",
        "primary_visual_mode": "typography-led|product-led|media-led|illustration-led|editorial-led|utility-led",
        "palette": {"background": "#RRGGBB", "surface": "#RRGGBB", "text": "#RRGGBB", "accent": "#RRGGBB"},
        "logo_decisions": [{"asset_id": "existing ID", "decision": "accept|reject|uncertain", "reason": "short string"}],
        "hero_decisions": [{
            "asset_id": "existing ID", "decision": "accept|reject|uncertain",
            "production_use": "allow|reference-only|uncertain", "reason": "short string",
        }],
        "composition": {"prefer": ["up to 6 short strings"], "avoid": ["up to 6 short strings"]},
        "motif_strategy": {
            "page_marker": "cover-only|all-pages|none",
            "section_markers": "selective|frequent|none",
            "max_repeated_motif_per_page": "integer 0..2",
        },
        "summary": "short string",
    }
    repair = f"\nYour previous response was rejected: {rejection}. Return a corrected object." if rejection else ""
    return (
        "You are Kolo Create's brand director. Review the bounded deterministic evidence and attached visual captures. "
        "Resolve brand identity, reject unrelated logos/media, and describe a restrained visual direction. "
        "For every hero decision, distinguish a clean production image from a webpage screenshot that is useful only as reference. "
        "Do not invent assets, colors, copy, dimensions, coordinates, or layout geometry. Use only asset IDs and colors in evidence. "
        "Prefer selective motifs; choose frequent/all-pages only when repetition is visibly signature to the source. "
        "Return JSON only, exactly matching this schema:\n"
        f"{json.dumps(schema, separators=(',', ':'))}\n"
        f"ATTACHMENTS_IN_ORDER:\n{json.dumps(attachments, separators=(',', ':'))}\n"
        f"EVIDENCE:\n{json.dumps(_evidence(system), separators=(',', ':'))}{repair}"
    )


def _image_paths(system: dict[str, Any], system_path: Path) -> list[Path]:
    paths: list[Path] = []
    screenshot = system_path.parent / "source-screenshot.png"
    if screenshot.exists():
        paths.append(screenshot)
    for asset in system.get("assets") or []:
        if asset.get("kind") not in {"logo", "hero-image"}:
            continue
        path = Path(str(asset.get("path", "")))
        if path.exists() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            if path not in paths:
                paths.append(path)
        if len(paths) >= 5:
            break
    return paths


def _image_manifest(system: dict[str, Any], paths: list[Path]) -> list[dict[str, str]]:
    ids_by_path = {
        str(Path(str(asset.get("path", ""))).resolve()): str(asset.get("id"))
        for asset in system.get("assets") or []
        if asset.get("path")
    }
    return [
        {
            "attachment": str(index + 1),
            "asset_id": ids_by_path.get(str(path.resolve()), "source-screenshot"),
            "kind": "source-screenshot" if path.name == "source-screenshot.png" else "candidate-asset",
        }
        for index, path in enumerate(paths)
    ]


def _prepare_images(paths: list[Path], directory: Path) -> list[Path]:
    """Bound visual context so one judgment call cannot inherit full-resolution media cost."""
    prepared: list[Path] = []
    for index, path in enumerate(paths):
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
                maximum = (1280, 1280) if index == 0 and path.name == "source-screenshot.png" else (720, 720)
                image.thumbnail(maximum)
                target = directory / f"evidence-{index + 1}.png"
                image.save(target, "PNG", optimize=True)
                prepared.append(target)
        except (OSError, ValueError):
            continue
    return prepared


def _direct_configuration() -> tuple[str, str] | None:
    base = os.environ.get("KOLO_LLM_BASE_URL") or os.environ.get("LITELLM_BASE_URL")
    token = os.environ.get("KOLO_LLM_TOKEN") or os.environ.get("LITELLM_API_KEY")
    return (base.rstrip("/"), token) if base and token else None


def available_transport() -> str | None:
    if _direct_configuration():
        return "openai-compatible"
    if shutil.which("openclaw"):
        return "openclaw-cli"
    return None


def _invoke_direct(model: str, prompt: str, images: list[Path]) -> str:
    configuration = _direct_configuration()
    if not configuration:
        raise BrandDirectionError("OpenAI-compatible proxy credentials are unavailable")
    base, token = configuration
    endpoint = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in images:
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}", "detail": "low"}})
    response = httpx.post(
        endpoint,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
        },
        timeout=75,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload["choices"][0]["message"]["content"])


def _invoke_cli(model: str, prompt: str, images: list[Path]) -> str:
    command = [
        "openclaw", "infer", "model", "run", "--model", model,
        "--thinking", "off", "--json", "--prompt", prompt,
    ]
    for path in images:
        command.extend(["--file", str(path)])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    if completed.returncode:
        raise BrandDirectionError(f"Brand director model call failed: {completed.stderr.strip() or completed.stdout.strip()}")
    try:
        envelope = json.loads(completed.stdout)
        return str(envelope["outputs"][0]["text"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise BrandDirectionError("Brand director returned an invalid OpenClaw response envelope") from exc


def _validate(value: dict[str, Any], system: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    confidence = value.get("identity_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("identity_confidence must be 0..1")
    if not isinstance(value.get("brand_name"), str) or not value["brand_name"].strip():
        raise ValueError("brand_name must be a string")
    if value.get("accent_role") not in ACCENT_ROLES or value.get("primary_visual_mode") not in VISUAL_MODES:
        raise ValueError("invalid brand role or visual mode")
    if (
        not isinstance(value.get("dominant_character"), list)
        or len(value["dominant_character"]) > 6
        or any(not isinstance(item, str) or len(item) > 80 for item in value["dominant_character"])
    ):
        raise ValueError("dominant_character must contain at most 6 items")
    palette = value.get("palette") or {}
    if set(palette) != {"background", "surface", "text", "accent"} or not all(HEX.fullmatch(str(item)) for item in palette.values()):
        raise ValueError("palette must contain four #RRGGBB values")
    assets = {str(asset.get("id")): asset for asset in system.get("assets") or []}
    seen: set[str] = set()
    for field in ("logo_decisions", "hero_decisions"):
        decisions = value.get(field)
        if not isinstance(decisions, list):
            raise ValueError(f"{field} must be a list")
        for decision in decisions:
            asset_id = decision.get("asset_id")
            expected_kind = "logo" if field == "logo_decisions" else "hero-image"
            if (
                asset_id not in assets
                or assets[asset_id].get("kind") != expected_kind
                or asset_id in seen
                or decision.get("decision") not in {"accept", "reject", "uncertain"}
                or not isinstance(decision.get("reason"), str)
            ):
                raise ValueError(f"{field} contains an unknown asset or decision")
            if field == "hero_decisions" and decision.get("production_use") not in PRODUCTION_USE_VALUES:
                # A missing or unrecognised production_use must not abort
                # extraction. "uncertain" is the fail-closed value: the asset
                # stays reference-only until a director explicitly allows it.
                decision["production_use"] = "uncertain"
            seen.add(asset_id)
    composition = value.get("composition") or {}
    if any(not isinstance(composition.get(key), list) or len(composition[key]) > 6 for key in ("prefer", "avoid")):
        raise ValueError("composition lists may contain at most 6 items")
    motifs = value.get("motif_strategy") or {}
    if motifs.get("page_marker") not in PAGE_MARKERS or motifs.get("section_markers") not in SECTION_MARKERS:
        raise ValueError("invalid motif strategy")
    maximum = motifs.get("max_repeated_motif_per_page")
    if not isinstance(maximum, int) or not 0 <= maximum <= 2:
        raise ValueError("max_repeated_motif_per_page must be 0..2")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("summary must be a string")
    return value


def _parse(raw: str, system: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    if not isinstance(value, dict):
        raise ValueError("response must be an object")
    return _validate(value, system)


def _supported_colors(system: dict[str, Any]) -> set[str]:
    evidence = system.get("evidence") or {}
    return {
        str(item.get("value", "")).upper()
        for item in evidence.get("colors") or []
        if HEX.fullmatch(str(item.get("value", ""))) and item.get("reference_status") != "contradicted"
    } | {str(value).upper() for value in evidence.get("logo_colors") or [] if HEX.fullmatch(str(value))}


def apply_brand_direction(
    design_system_path: Path,
    latest_path: Path,
    *,
    mode: str = "auto",
    model: str = DEFAULT_BRAND_MODEL,
    invoke: Callable[[str, str, list[Path]], str] | None = None,
) -> dict[str, Any]:
    system_path = design_system_path.resolve()
    system = read_json(system_path)
    transport = "injected" if invoke else available_transport()
    if mode == "deterministic" or (mode == "auto" and not transport):
        return {"status": "skipped", "reason": "disabled" if mode == "deterministic" else "no model transport available", "model": model}
    if not transport:
        raise BrandDirectionError("No brand-director model transport is available")
    call = invoke or (_invoke_direct if transport == "openai-compatible" else _invoke_cli)
    source_images = _image_paths(system, system_path)
    attachments = _image_manifest(system, source_images)
    started = time.monotonic()
    attempts = 0
    rejection: str | None = None
    judgment: dict[str, Any] | None = None
    with tempfile.TemporaryDirectory(prefix="kolo-brand-director-") as temporary:
        images = _prepare_images(source_images, Path(temporary))
        while attempts < 2:
            attempts += 1
            raw = call(model, _prompt(system, attachments, rejection), images)
            try:
                judgment = _parse(raw, system)
                break
            except (json.JSONDecodeError, ValueError) as exc:
                rejection = str(exc)
    if judgment is None:
        raise BrandDirectionError(f"Brand director returned invalid structured output after {attempts} attempts: {rejection}")

    decisions = {
        item["asset_id"]: item
        for field in ("logo_decisions", "hero_decisions")
        for item in judgment[field]
    }
    for asset in system.get("assets") or []:
        decision = decisions.get(asset.get("id"))
        if decision:
            asset["director_decision"] = decision["decision"]
            asset["director_reason"] = str(decision.get("reason", ""))[:240]
            asset["director_eligible"] = decision["decision"] != "reject"
            if asset.get("kind") == "hero-image":
                deterministic_eligible = _deterministic_production_eligible(asset)
                asset["director_production_use"] = decision["production_use"]
                asset["production_eligible"] = bool(
                    deterministic_eligible
                    and decision["decision"] == "accept"
                    and decision["production_use"] == "allow"
                )

    supported = _supported_colors(system)
    applied_palette: dict[str, str] = {}
    for role, value in judgment["palette"].items():
        normalized = value.upper()
        if normalized in supported:
            system["tokens"]["colors"][role] = normalized
            applied_palette[role] = normalized
    system.setdefault("visual_language", {})["primary_mode"] = judgment["primary_visual_mode"]
    direction = {
        **judgment,
        "model": model,
        "transport": transport,
        "attempts": attempts,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "images_reviewed": [str(path) for path in source_images],
        "applied_palette": applied_palette,
    }
    system["brand_direction"] = direction
    system["brand_components"] = build_brand_components(system)
    system["design_grammar"] = compile_design_grammar(system)
    validate_design_system(system)
    write_json(system_path, system)
    write_json(latest_path.resolve(), system)
    write_json(system_path.parent / "brand-components.json", system["brand_components"])
    write_json(system_path.parent / "design-grammar.json", system["design_grammar"])
    direction_path = write_json(system_path.parent / "brand-direction.json", direction)
    return {
        "status": "succeeded", "model": model, "transport": transport, "attempts": attempts,
        "brand_direction": str(direction_path), "applied_palette": applied_palette,
    }
