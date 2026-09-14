from __future__ import annotations

import hashlib
import json
from typing import Any


def presentation_layout_identity(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a content-independent identity for a deck's chosen geometry family."""
    art_direction = plan.get("art_direction") or {}
    sequence = [
        {
            "archetype": str(slide.get("archetype", "")),
            "variant": str(slide.get("variant", "")),
            "design_profile": str(slide.get("design_profile", "")),
            "scene_component": str(slide.get("scene_component", "")),
        }
        for slide in plan.get("slides", [])
    ]
    payload = {
        "profile": str(art_direction.get("profile", "precision")),
        "sequence": sequence,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        **payload,
        "signature": hashlib.sha256(canonical).hexdigest(),
    }


def compare_presentation_layouts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Flag near-identical layout selections before two brand decks are accepted."""
    left_identity = presentation_layout_identity(left)
    right_identity = presentation_layout_identity(right)
    left_sequence = left_identity["sequence"]
    right_sequence = right_identity["sequence"]
    slots = max(len(left_sequence), len(right_sequence), 1)
    matching = sum(
        left_item == right_item
        for left_item, right_item in zip(left_sequence, right_sequence)
    )
    similarity = matching / slots
    return {
        "schema_version": 1,
        "similarity": round(similarity, 4),
        "nearly_identical": similarity >= 0.8,
        "left_signature": left_identity["signature"],
        "right_signature": right_identity["signature"],
        "threshold": 0.8,
    }
