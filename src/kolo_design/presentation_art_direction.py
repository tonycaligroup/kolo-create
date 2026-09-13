from __future__ import annotations

from copy import deepcopy
from typing import Any


_VARIANTS = {
    "precision": {
        "cover": "precision-index", "process": "precision-columns", "feature-list": "precision-grid",
        "statement": "precision-duet", "section": "precision-section", "image-led": "precision-image",
        "closing": "precision-signature",
    },
    "kinetic": {
        "cover": "kinetic-split", "process": "kinetic-scoreboard", "feature-list": "kinetic-mosaic",
        "statement": "kinetic-manifesto", "section": "kinetic-section", "image-led": "kinetic-image",
        "closing": "kinetic-finish",
    },
    "editorial": {
        "cover": "editorial-poster", "process": "editorial-rail", "feature-list": "editorial-list",
        "statement": "editorial-pullquote", "section": "editorial-section", "image-led": "editorial-image",
        "closing": "editorial-signoff",
    },
    "product": {
        "cover": "product-window", "process": "product-cards", "feature-list": "product-shelf",
        "statement": "product-callout", "section": "product-section", "image-led": "product-image",
        "closing": "product-stamp",
    },
    "monochrome": {
        "cover": "monochrome-frame", "process": "monochrome-sequence", "feature-list": "monochrome-bands",
        "statement": "monochrome-poster", "section": "monochrome-section", "image-led": "monochrome-image",
        "closing": "monochrome-minimal",
    },
}


def _chroma(value: str) -> float:
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    return max(channels) - min(channels)


def presentation_profile(system: dict[str, Any]) -> str:
    typography = system["tokens"]["typography"]
    colors = system["tokens"]["colors"]
    visual = system.get("visual_language") or {}
    if typography.get("display_fallback") == "serif":
        return "editorial"
    accent = colors["accent"]
    secondary = colors.get("accent_secondary", accent)
    if max(_chroma(accent), _chroma(secondary)) < 0.08:
        return "monochrome"
    if float(visual.get("media_coverage", 0)) >= 0.65:
        return "kinetic"
    if visual.get("primary_mode") == "product-led":
        return "product"
    return "precision"


def apply_presentation_art_direction(system: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    directed = deepcopy(plan)
    profile = presentation_profile(system)
    colors = system["tokens"]["colors"]
    directed["art_direction"] = {
        "schema_version": 1,
        "profile": profile,
        "deck_motif": {
            "precision": "measured rules and indexed whitespace",
            "kinetic": "cropped media, score lines, and high-contrast pacing",
            "editorial": "serif scale, margin rhythm, and print-like fields",
            "product": "modular surfaces, display windows, and labeled shelves",
            "monochrome": "framed fields, hard contrast, and quiet geometry",
        }[profile],
        "accent_strategy": "dual" if colors.get("accent_secondary", colors["accent"]) != colors["accent"] else "single",
    }
    variants = _VARIANTS[profile]
    seen: dict[str, int] = {}
    for slide in directed["slides"]:
        archetype = slide["archetype"]
        occurrence = seen.get(archetype, 0)
        slide["variant"] = variants[archetype] + ("-alternate" if occurrence else "")
        seen[archetype] = occurrence + 1
    return directed
