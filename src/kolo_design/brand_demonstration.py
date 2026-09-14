from __future__ import annotations

from pathlib import Path
from typing import Any

from .assets import raster_dimensions


MEDIA_LED_MODES = {"media-led", "product-led", "illustration-led"}


def media_expected(system: dict[str, Any]) -> bool:
    visual = system.get("visual_language") or {}
    return (
        str(visual.get("primary_mode", "")) in MEDIA_LED_MODES
        or float(visual.get("media_coverage", 0) or 0) >= 0.15
    )


def usable_hero_assets(system: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        asset for asset in system.get("assets", [])
        if asset.get("kind") == "hero-image"
        and Path(str(asset.get("path", ""))).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        and raster_dimensions(Path(str(asset.get("path", ""))))
    ]


def extracted_logo_count(system: dict[str, Any]) -> int:
    counts = (system.get("evidence") or {}).get("counts") or {}
    return int(counts.get("logo_assets", 0) or 0)


def require_demonstration_assets(
    system: dict[str, Any], *, hero_selected: bool, logo_selected: bool, format_name: str,
) -> dict[str, Any]:
    """Fail an automatic sample that contradicts the extracted visual mode."""
    expected = media_expected(system)
    logo_count = extracted_logo_count(system)
    if expected and not hero_selected:
        raise RuntimeError(
            f"{format_name} brand demonstration requires imagery because the source is media-led, "
            "but no usable hero asset was selected"
        )
    if logo_count > 0 and not logo_selected:
        raise RuntimeError(
            f"{format_name} brand demonstration extracted {logo_count} logo asset(s), but none was usable"
        )
    return {
        "enabled": True,
        "media_expected": expected,
        "hero_selected": hero_selected,
        "extracted_logo_count": logo_count,
        "logo_selected": logo_selected,
        "logo_text_fallback_disclosed": not logo_selected,
    }
