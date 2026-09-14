from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


_DIRECTIONS = ("precision", "kinetic", "editorial", "product", "monochrome")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(value)))
    except (TypeError, ValueError):
        return default


def _chroma(value: str) -> float:
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return 0.0
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    return max(channels) - min(channels)


def _style_recipe(value: dict[str, Any]) -> dict[str, Any]:
    shadow = str(value.get("shadow", "none"))
    return {
        "radius": round(_clamp(_number(value.get("radius"), 0), 0, 48), 2),
        "border_width": round(_clamp(_number(value.get("border_width"), 0), 0, 4), 2),
        "shadowed": shadow not in {"", "none", "0", "0px"},
        "padding": str(value.get("padding", "")),
        "alignment": str(value.get("text_align", "start")),
        "observations": int(_number(value.get("observations"), 0)),
    }


def compile_design_grammar(system: dict[str, Any]) -> dict[str, Any]:
    """Compile extracted evidence into continuous, format-neutral design traits."""
    tokens = system["tokens"]
    colors = tokens["colors"]
    typography = tokens["typography"]
    visual = system.get("visual_language") or {}
    components = system.get("components") or {}
    cards = components.get("cards") or {}
    buttons = (components.get("buttons") or {}).get("primary") or {}
    sections = (components.get("sections") or {}).get("recipe") or {}
    imagery = components.get("imagery") or {}

    accent = colors["accent"]
    secondary = colors.get("accent_secondary", accent)
    color_energy = max(_chroma(accent), _chroma(secondary))
    media_intensity = _clamp(_number(visual.get("media_coverage"), 0))
    density = str(visual.get("density", "balanced"))
    whitespace = {"sparse": 0.86, "balanced": 0.58, "dense": 0.3}.get(density, 0.55)
    radius = max(
        _number(cards.get("radius"), 0),
        _number(buttons.get("radius"), 0),
        _number(tokens.get("shape", {}).get("radius"), 0),
    )
    curvature = _clamp(radius / 28)
    elevated = any(_style_recipe(item)["shadowed"] for item in (cards, buttons, sections))
    surface_layers = _clamp(
        (0.42 if cards else 0.0)
        + (0.2 if elevated else 0.0)
        + (0.18 if colors.get("surface") != colors.get("background") else 0.0)
    )
    serif = typography.get("display_fallback") == "serif"
    observed_scale = typography.get("scale") or [11, 14, 18, 26, 40, 64]
    scale_ratio = _clamp((_number(observed_scale[-1], 64) / max(1, _number(observed_scale[1], 14)) - 2) / 4)
    alignment = str(visual.get("dominant_alignment", "start"))
    asymmetry = _clamp(
        0.22
        + media_intensity * 0.34
        + (0.18 if alignment == "start" else 0.04)
        + (0.12 if len(set((components.get("imagery") or {}).get("common_aspect_ratios") or [])) >= 3 else 0)
    )
    product_focus = 1.0 if visual.get("primary_mode") == "product-led" else 0.0
    monochrome = 1 - color_energy

    traits = {
        "color_energy": round(color_energy, 4),
        "media_intensity": round(media_intensity, 4),
        "whitespace": round(whitespace, 4),
        "curvature": round(curvature, 4),
        "surface_layers": round(surface_layers, 4),
        "typographic_contrast": round(scale_ratio, 4),
        "asymmetry": round(asymmetry, 4),
        "product_focus": round(product_focus, 4),
        "monochrome": round(monochrome, 4),
        "serif_display": serif,
    }
    scores = {
        "precision": whitespace * 0.34 + (1 - surface_layers) * 0.2 + (1 - curvature) * 0.16 + (1 - media_intensity) * 0.12 + 0.18,
        "kinetic": media_intensity * 0.38 + color_energy * 0.25 + asymmetry * 0.22 + (1 - whitespace) * 0.08 + 0.07,
        "editorial": (0.55 if serif else 0.08) + whitespace * 0.2 + scale_ratio * 0.15 + (1 - surface_layers) * 0.1,
        "product": product_focus * 0.45 + surface_layers * 0.22 + curvature * 0.14 + media_intensity * 0.12 + whitespace * 0.1 + 0.07,
        "monochrome": monochrome * 0.56 + (1 - surface_layers) * 0.14 + whitespace * 0.16 + scale_ratio * 0.08 + 0.06,
    }
    total = sum(scores.values()) or 1
    normalized = {key: round(value / total, 4) for key, value in scores.items()}
    ranked = sorted(_DIRECTIONS, key=lambda key: (-normalized[key], _DIRECTIONS.index(key)))
    recipes = {
        "card": _style_recipe(cards),
        "button": _style_recipe(buttons),
        "section": _style_recipe(sections),
        "image": {
            "radius": round(_clamp(_number(imagery.get("radius"), 0), 0, 48), 2),
            "fit": str(imagery.get("object_fit", "cover")),
            "aspect_ratios": [round(_number(value), 3) for value in (imagery.get("common_aspect_ratios") or [])[:8]],
            "observations": int(_number(imagery.get("observations"), 0)),
        },
    }
    payload = {
        "schema_version": 1,
        "brand": {"id": system["id"], "version": system["version"]},
        "traits": traits,
        "direction_weights": normalized,
        "ranked_directions": ranked,
        "recipes": recipes,
        "evidence": {
            "component_observations": sum(recipe.get("observations", 0) for recipe in recipes.values()),
            "visual_mode": visual.get("primary_mode", "unknown"),
            "density": density,
        },
    }
    signature_source = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["signature"] = hashlib.sha256(signature_source).hexdigest()
    return payload


def validate_design_grammar(grammar: dict[str, Any]) -> None:
    if grammar.get("schema_version") != 1:
        raise ValueError("Invalid design grammar schema")
    weights = grammar.get("direction_weights")
    if not isinstance(weights, dict) or set(weights) != set(_DIRECTIONS):
        raise ValueError("Design grammar is missing direction weights")
    if not isinstance(grammar.get("traits"), dict) or not grammar.get("signature"):
        raise ValueError("Design grammar is incomplete")
