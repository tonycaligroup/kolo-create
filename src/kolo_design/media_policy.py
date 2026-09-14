from __future__ import annotations

from typing import Any


def classify_rendered_media(item: dict[str, Any]) -> dict[str, Any]:
    """Separate a brand-reference capture from media safe to reuse in artifacts."""
    tag = str(item.get("capture_tag") or item.get("tag") or "").casefold()
    text = str(item.get("text_sample") or "").strip()
    text_characters = int(item.get("embedded_text_characters", len(text)) or 0)
    text_elements = int(item.get("embedded_text_elements", 1 if text else 0) or 0)
    interactive = int(item.get("embedded_interactive_elements", 0) or 0)
    navigation = int(item.get("embedded_navigation_elements", 0) or 0)
    reasons: list[str] = []
    if navigation:
        reasons.append("contains navigation chrome")
    if interactive:
        reasons.append("contains interactive controls")
    if text_elements >= 2 or text_characters >= 32:
        reasons.append("contains substantial embedded webpage text")
    if tag not in {"img", "picture", "video"} and text:
        reasons.append("rendered container combines media and content")
    production_eligible = not reasons
    return {
        "asset_class": "production-media" if production_eligible else "reference-evidence",
        "production_eligible": production_eligible,
        "reuse_reasons": reasons or ["clean rendered media element"],
        "embedded_content": {
            "text_characters": text_characters,
            "text_elements": text_elements,
            "interactive_elements": interactive,
            "navigation_elements": navigation,
        },
    }


def production_media(asset: dict[str, Any]) -> bool:
    return asset.get("kind") == "hero-image" and asset.get("production_eligible") is not False


def crop_visible_fraction(source_aspect: float, target_aspect: float) -> float:
    """Return the fraction of source pixels visible under a centered cover crop."""
    if source_aspect <= 0 or target_aspect <= 0:
        return 0.0
    return min(source_aspect, target_aspect) / max(source_aspect, target_aspect)
