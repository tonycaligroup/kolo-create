from __future__ import annotations

from pathlib import Path
from typing import Any

from .assets import raster_dimensions


def build_brand_components(system: dict[str, Any]) -> dict[str, Any]:
    """Normalize observed evidence into portable document component recipes."""
    colors = system["tokens"]["colors"]
    accent = colors["accent"]
    accent_secondary = colors.get("accent_secondary", accent)
    brand_dark = colors.get("brand_dark", colors["text"])
    dual_tone = accent_secondary.upper() != accent.upper()
    return {
        "schema_version": 1,
        "brand": {"id": system["id"], "version": system["version"]},
        "preferred_renderer": "reportlab",
        "components": {
            "section-marker": {
                "kind": "rule",
                "style": "dual-tone" if dual_tone else "solid",
                "primary": accent,
                "secondary": accent_secondary,
                "primary_share": 0.72,
            },
            "feature-band": {
                "kind": "panel",
                "background": brand_dark,
                "foreground": "#FFFFFF",
                "accent": accent_secondary,
                "usage": "one lead feature group per document",
            },
            "numbered-feature-grid": {
                "kind": "grid",
                "columns": 2,
                "lead_spans_columns": True,
                "number_style": "two-digit",
            },
            "media-band": {
                "kind": "image",
                "fit": "cover",
                "minimum_density": 1.25,
                "preferred_aspect": "landscape",
            },
            "closing-signature": {
                "kind": "closing",
                "style": "open",
                "marker": "section-marker",
            },
        },
        "constraints": {
            "max_feature_bands_per_document": 1,
            "max_decorative_components_per_page": 2,
            "never_upscale_raster_assets": True,
        },
    }


def _component_library(system: dict[str, Any]) -> dict[str, Any]:
    library = system.get("brand_components")
    if isinstance(library, dict) and library.get("schema_version") == 1:
        return library
    return build_brand_components(system)


def _cover_media(system: dict[str, Any], minimum_density: float) -> dict[str, Any]:
    for asset in system.get("assets") or []:
        path = Path(str(asset.get("path", "")))
        if asset.get("kind") != "hero-image" or not path.exists():
            continue
        dimensions = raster_dimensions(path)
        if not dimensions:
            continue
        width, height = dimensions
        aspect = width / max(1, height)
        if aspect >= 1.25:
            target_width, target_height = 816, 430
            placement = "bottom-band"
        else:
            target_width, target_height = 408, 960
            placement = "side-panel"
        density = min(width / target_width, height / target_height)
        if density >= minimum_density:
            return {
                "component": "media-band",
                "asset_id": asset.get("id"),
                "placement": placement,
                "fit": "cover",
                "focal_point": [0.54, 0.5],
                "effective_density": round(density, 2),
            }
    return {"component": "type-led-cover", "asset_id": None, "placement": "none"}


def select_component_plan(
    system: dict[str, Any], plan: dict[str, Any], blocks: list[dict[str, str]]
) -> dict[str, Any]:
    """Select a restrained set of brand components for exact document regions."""
    library = _component_library(system)
    recipes = library["components"]
    by_id = {block["id"]: block for block in blocks}
    feature_band_used = False
    sections: list[dict[str, Any]] = []
    layout_sections = plan["layout"]["sections"]
    for index, section in enumerate(layout_sections):
        section_blocks = [by_id[block_id] for block_id in section["block_ids"]]
        bullet_count = sum(block["kind"] == "bullet" for block in section_blocks)
        closing = index == len(layout_sections) - 1 and any(block["kind"] == "action" for block in section_blocks)
        components = ["section-marker"] if any(block["kind"].startswith("heading") for block in section_blocks) else []
        treatment = "standard"
        if closing:
            components.append("closing-signature")
            treatment = "closing-signature"
        elif bullet_count >= 2:
            components.append("numbered-feature-grid")
            treatment = "feature-grid"
            if not feature_band_used:
                components.append("feature-band")
                treatment = "feature-band"
                feature_band_used = True
        sections.append({"section_id": section["id"], "components": components, "treatment": treatment})
    minimum_density = float(recipes["media-band"]["minimum_density"])
    return {
        "schema_version": 1,
        "preferred_renderer": library.get("preferred_renderer", "reportlab"),
        "library": library,
        "cover": _cover_media(system, minimum_density),
        "sections": sections,
    }


def validate_component_plan(value: dict[str, Any]) -> None:
    if value.get("schema_version") != 1 or value.get("preferred_renderer") not in {"reportlab", "html-css"}:
        raise ValueError("Invalid brand component plan")
    cover = value.get("cover") or {}
    if cover.get("placement") not in {"bottom-band", "side-panel", "none"}:
        raise ValueError("Invalid cover component placement")
    if not isinstance(value.get("sections"), list):
        raise ValueError("Invalid section component plan")
