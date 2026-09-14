from __future__ import annotations

import hashlib
import json
from typing import Any


_COMPONENTS = {
    "opening": ("type-poster", "split-media", "product-window", "product-stage", "indexed-open", "framed-cover", "monochrome-media-frame"),
    "process": ("step-rail", "numbered-columns", "modular-steps", "product-sequence", "scoreboard", "sequence-bands", "monochrome-media-sequence"),
    "features": ("open-feature-list", "measured-card-grid", "asymmetric-mosaic", "product-shelf", "product-ledger", "contrast-bands", "monochrome-media-bands"),
    "statement": ("editorial-pullquote", "split-statement", "manifesto-field", "product-callout", "framed-statement", "monochrome-media-statement"),
    "section": ("open-section", "section-band", "media-copy-split"),
    "closing": ("open-signature", "framed-signoff", "split-finish", "product-stamp", "product-signoff", "monochrome-media-signoff"),
}

_AFFINITY = {
    "type-poster": "editorial", "split-media": "kinetic", "product-window": "product", "product-stage": "product", "indexed-open": "precision", "framed-cover": "monochrome",
    "step-rail": "editorial", "numbered-columns": "precision", "modular-steps": "product", "product-sequence": "product", "scoreboard": "kinetic", "sequence-bands": "monochrome",
    "open-feature-list": "editorial", "measured-card-grid": "precision", "asymmetric-mosaic": "kinetic", "product-shelf": "product", "product-ledger": "product", "contrast-bands": "monochrome",
    "editorial-pullquote": "editorial", "split-statement": "precision", "manifesto-field": "kinetic", "product-callout": "product", "framed-statement": "monochrome",
    "open-section": "precision", "section-band": "monochrome", "media-copy-split": "kinetic",
    "open-signature": "precision", "framed-signoff": "monochrome", "split-finish": "kinetic", "product-stamp": "product", "product-signoff": "product",
    "monochrome-media-frame": "monochrome", "monochrome-media-sequence": "monochrome",
    "monochrome-media-bands": "monochrome", "monochrome-media-statement": "monochrome",
    "monochrome-media-signoff": "monochrome",
}


def _component_score(component: str, grammar: dict[str, Any], unit: dict[str, Any], repeat: int) -> float:
    traits = grammar["traits"]
    direction = _AFFINITY[component]
    score = float(grammar["direction_weights"].get(direction, 0)) * 180
    if "media" in component:
        score += traits["media_intensity"] * 18
        score += 5 if unit.get("has_media_request") else 0
    elif component in {"product-window", "product-shelf"}:
        score += traits["media_intensity"] * 8
    if "card" in component or "modular" in component or "product" in component:
        score += traits["surface_layers"] * 14 + traits["curvature"] * 8
    if "product" in component:
        score += traits["product_focus"] * 12
    if direction == "kinetic":
        score += traits["media_intensity"] * 12 + traits["asymmetry"] * 6
    if component.startswith("monochrome-media"):
        score += traits["media_intensity"] * 30
    if component in {"framed-cover", "sequence-bands", "contrast-bands", "framed-statement", "framed-signoff"}:
        score += (1 - traits["media_intensity"]) * 30 + traits["whitespace"] * 5
    if component in {"product-stage", "product-sequence", "product-ledger", "product-signoff"}:
        score += traits["whitespace"] * 14 + (1 - traits["surface_layers"]) * 9
    if component in {"product-window", "modular-steps", "product-shelf", "product-callout", "product-stamp"}:
        score += traits["surface_layers"] * 13 + traits["curvature"] * 7
    if "open" in component or "editorial" in component:
        score += traits["whitespace"] * 12
    if "asymmetric" in component or "split" in component or "manifesto" in component:
        score += traits["asymmetry"] * 10
    if unit.get("density") == "dense" and component in {"open-feature-list", "step-rail", "split-statement"}:
        score += 8
    return score - repeat * 24


def build_scene_plan(
    grammar: dict[str, Any], content_map: dict[str, Any], *, format_name: str, brand_id: str
) -> dict[str, Any]:
    """Select inspectable semantic components before a renderer assigns coordinates."""
    used: dict[str, int] = {}
    scenes: list[dict[str, Any]] = []
    previous_component = ""
    for index, unit in enumerate(content_map["units"]):
        role = unit["role"]
        primary_direction = str(grammar["ranked_directions"][0])
        secondary_direction = str(grammar["ranked_directions"][1])
        candidates = []
        primary_weight = float(grammar["direction_weights"][primary_direction])
        secondary_weight = float(grammar["direction_weights"][secondary_direction])
        close_direction_pair = primary_weight - secondary_weight <= 0.02
        for component in _COMPONENTS[role]:
            score = _component_score(component, grammar, unit, used.get(component, 0))
            component_direction = _AFFINITY[component]
            score -= grammar["ranked_directions"].index(component_direction) * 14
            score -= max(0.0, primary_weight - float(grammar["direction_weights"][component_direction])) * 200
            if close_direction_pair and index % 2 == 1 and component_direction == secondary_direction:
                score += 70
            if component == previous_component:
                score -= 50
            if role in {"opening", "closing"} and _AFFINITY[component] != primary_direction:
                score -= 18
            candidates.append({
                "component": component,
                "direction": _AFFINITY[component],
                "score": round(score, 3),
            })
        candidates.sort(key=lambda item: (-item["score"], item["component"]))
        selected = candidates[0]
        used[selected["component"]] = used.get(selected["component"], 0) + 1
        previous_component = selected["component"]
        scenes.append({
            "id": f"scene-{index + 1:02d}",
            "role": role,
            "block_ids": unit["block_ids"],
            "component": selected["component"],
            "direction": selected["direction"],
            "density": unit["density"],
            "alternates": candidates[1:3],
            "selection_score": selected["score"],
        })
    payload = {
        "schema_version": 1,
        "format": format_name,
        "brand_id": brand_id,
        "grammar_signature": grammar["signature"],
        "scenes": scenes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["signature"] = hashlib.sha256(canonical).hexdigest()
    return payload


def validate_scene_plan(scene_plan: dict[str, Any], blocks: list[dict[str, str]]) -> None:
    if scene_plan.get("schema_version") != 1 or scene_plan.get("format") not in {"document", "presentation"}:
        raise ValueError("Invalid scene plan")
    planned = [block_id for scene in scene_plan.get("scenes", []) for block_id in scene.get("block_ids", [])]
    expected = [block["id"] for block in blocks]
    if len(planned) != len(set(planned)) or set(planned) != set(expected):
        raise ValueError("Scene plan must preserve every source block exactly once")
