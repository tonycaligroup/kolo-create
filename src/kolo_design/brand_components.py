from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any

from .assets import raster_dimensions


def _luminance(value: str) -> float:
    channels = []
    for index in (1, 3, 5):
        channel = int(value[index:index + 2], 16) / 255
        channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(left: str, right: str) -> float:
    low, high = sorted((_luminance(left), _luminance(right)))
    return (high + 0.05) / (low + 0.05)


def _chroma(value: str) -> float:
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    return max(channels) - min(channels)


def _color_occurrences(system: dict[str, Any], value: str) -> int:
    return next(
        (int(item.get("occurrences", 0)) for item in (system.get("evidence") or {}).get("colors", [])
         if str(item.get("value", "")).upper() == value.upper()),
        0,
    )


def _supported_secondary(system: dict[str, Any], primary: str, secondary: str) -> bool:
    if secondary.upper() == primary.upper():
        return False
    primary_count = _color_occurrences(system, primary)
    secondary_count = _color_occurrences(system, secondary)
    logo_colors = {str(value).upper() for value in (system.get("evidence") or {}).get("logo_colors", [])}
    return secondary.upper() in logo_colors or secondary_count >= max(12, round(primary_count * 0.3))


def _foreground(background: str) -> str:
    return max(("#111111", "#FFFFFF"), key=lambda value: _contrast(background, value))


def build_brand_components(system: dict[str, Any]) -> dict[str, Any]:
    """Normalize observed evidence into portable document component recipes."""
    colors = system["tokens"]["colors"]
    accent = colors["accent"]
    candidate_secondary = colors.get("accent_secondary", accent)
    dual_tone = _supported_secondary(system, accent, candidate_secondary)
    accent_secondary = candidate_secondary if dual_tone else accent
    brand_dark = colors.get("brand_dark")
    if not brand_dark:
        surface = colors["surface"]
        brand_dark = surface if _contrast(colors["background"], surface) >= 1.15 and _contrast(surface, colors["text"]) >= 3 else colors["text"]
    feature_accent = next(
        (value for value in (accent, accent_secondary, colors["text"]) if _contrast(brand_dark, value) >= 1.5),
        _foreground(brand_dark),
    )
    monochrome = max(_chroma(accent), _chroma(accent_secondary)) < 0.08
    return {
        "schema_version": 3,
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
                "foreground": _foreground(brand_dark),
                "accent": feature_accent,
                "accent_position": "top",
                "usage": "one lead feature group per document",
            },
            "numbered-feature-grid": {
                "kind": "grid",
                "columns": 2,
                "lead_spans_columns": True,
                "number_style": "two-digit",
                "cell_style": "open" if monochrome else "card",
            },
            "editorial-feature-list": {
                "kind": "list",
                "style": "open",
                "number_style": "two-digit",
                "separator": "rule",
                "usage": "structured features without a filled panel",
            },
            "statement": {
                "kind": "statement",
                "style": "oversized-type",
                "maximum_per_document": 1,
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
    if isinstance(library, dict) and library.get("schema_version") == 3:
        return library
    return build_brand_components(system)


_STOPWORDS = {
    "about", "after", "and", "asset", "body", "brand", "create", "design", "document", "format",
    "from", "image", "into", "kolo", "main", "media", "more", "photo", "source", "that", "the",
    "their", "this", "using", "visual", "website", "with", "your",
}


def _terms(value: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9]+", value.lower()) if len(term) >= 3 and term not in _STOPWORDS}


def _asset_semantics(asset: dict[str, Any]) -> set[str]:
    values = [asset.get("alt"), asset.get("text_sample")]
    values.extend(asset.get("keywords") or [])
    return _terms(" ".join(str(value or "") for value in values))


def _cover_media(
    system: dict[str, Any], minimum_density: float, context: str, *, allow_unlabeled: bool = False,
) -> dict[str, Any]:
    context_terms = _terms(context)
    candidates: list[tuple[float, dict[str, Any]]] = []
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
            semantic_terms = _asset_semantics(asset)
            overlap = context_terms & semantic_terms
            relevance = len(overlap) / max(1, min(5, len(context_terms)))
            if semantic_terms and not overlap:
                continue
            if not semantic_terms and not allow_unlabeled:
                continue
            candidate = {
                "component": "media-band",
                "asset_id": asset.get("id"),
                "placement": placement,
                "fit": "cover",
                "focal_point": [0.54, 0.5],
                "effective_density": round(density, 2),
                "semantic_relevance": round(relevance, 2),
                "matched_terms": sorted(overlap),
                "reason": "content-matched brand media" if overlap else "unlabeled media allowed by explicit image-led prompt",
            }
            candidates.append((relevance * 100 + float(asset.get("score", 0)) / 10 + density, candidate))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    variants = ("index", "rule-stack", "wordmark-scale")
    variant = variants[int(hashlib.sha256(context.encode("utf-8")).hexdigest()[:8], 16) % len(variants)]
    return {
        "component": "type-led-cover", "asset_id": None, "placement": "none",
        "variant": variant,
        "reason": "no sufficiently dense, content-relevant brand image",
    }


def select_component_plan(
    system: dict[str, Any], plan: dict[str, Any], blocks: list[dict[str, str]], prompt: str = "",
) -> dict[str, Any]:
    """Select a restrained set of brand components for exact document regions."""
    library = _component_library(system)
    recipes = library["components"]
    by_id = {block["id"]: block for block in blocks}
    feature_band_used = False
    sections: list[dict[str, Any]] = []
    layout_sections = plan["layout"]["sections"]
    composition_family = (plan.get("composition") or {}).get("family")
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
            cell_style = recipes["numbered-feature-grid"].get("cell_style", "card")
            if cell_style == "open" or composition_family == "editorial_narrative":
                components.append("editorial-feature-list")
                treatment = "editorial-feature-list"
            else:
                components.append("numbered-feature-grid")
                treatment = "feature-grid"
            if not feature_band_used and cell_style != "open" and composition_family != "editorial_narrative":
                components.append("feature-band")
                treatment = "feature-band"
                feature_band_used = True
        sections.append({"section_id": section["id"], "components": components, "treatment": treatment})
    minimum_density = float(recipes["media-band"]["minimum_density"])
    context = " ".join([prompt, str(plan.get("title", "")), str(plan.get("subtitle", "")), *(block["text"] for block in blocks[:12])])
    explicit_image_prompt = bool(re.search(r"\b(image|photo|photographic|showcase|visual)\b", prompt, re.I))
    return {
        "schema_version": 1,
        "preferred_renderer": library.get("preferred_renderer", "reportlab"),
        "library": library,
        "cover": _cover_media(system, minimum_density, context, allow_unlabeled=explicit_image_prompt),
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
