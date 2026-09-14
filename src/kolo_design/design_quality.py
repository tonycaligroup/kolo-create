from __future__ import annotations

from collections import Counter
from typing import Any


def evaluate_design_plan(grammar: dict[str, Any], scene_plan: dict[str, Any]) -> dict[str, Any]:
    """Report design-system behavior that mechanical file checks cannot see."""
    scenes = scene_plan.get("scenes") or []
    components = [str(scene.get("component", "")) for scene in scenes]
    directions = [str(scene.get("direction", "")) for scene in scenes]
    adjacent_repeats = sum(left == right for left, right in zip(components, components[1:]))
    component_counts = Counter(components)
    direction_counts = Counter(directions)
    scene_count = len(scenes)
    variety = len(component_counts) / max(1, scene_count)
    dominant_share = max(component_counts.values(), default=0) / max(1, scene_count)
    primary_direction = str((grammar.get("ranked_directions") or [""])[0])
    primary_share = direction_counts.get(primary_direction, 0) / max(1, scene_count)
    return {
        "schema_version": 1,
        "status": "pass" if adjacent_repeats == 0 and variety >= 0.66 and dominant_share <= 0.5 else "review",
        "checks": {
            "scene_count": scene_count,
            "distinct_components": len(component_counts),
            "component_variety": round(variety, 4),
            "dominant_component_share": round(dominant_share, 4),
            "adjacent_component_repeats": adjacent_repeats,
            "directions_used": len(direction_counts),
            "primary_direction_share": round(primary_share, 4),
        },
        "note": "This gate measures variety and brand-direction coherence; human review still owns taste.",
    }
