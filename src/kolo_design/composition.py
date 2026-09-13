from __future__ import annotations

import re
from typing import Any


COMPOSITION_FAMILIES = {
    "editorial_narrative",
    "asymmetric_feature_grid",
    "numbered_process",
    "modular_announcement",
}

_EXPLICIT_ALIASES = (
    ("asymmetric_feature_grid", ("asymmetric feature grid", "asymmetric grid")),
    ("numbered_process", ("numbered process", "step-by-step", "step by step")),
    ("modular_announcement", ("modular announcement", "announcement layout")),
    ("editorial_narrative", ("editorial narrative", "magazine layout")),
)


def _visual_language(system: dict[str, Any]) -> dict[str, Any]:
    value = system.get("visual_language")
    return value if isinstance(value, dict) else {}


def select_composition(
    system: dict[str, Any],
    plan: dict[str, Any],
    blocks: list[dict[str, str]],
    prompt: str,
) -> dict[str, Any]:
    """Choose a layout family from inspectable content and brand signals.

    The planner still owns block arrangement. This selector is deterministic,
    makes no model call, and never changes source text or block ownership.
    """
    lowered = prompt.lower()
    for family, aliases in _EXPLICIT_ALIASES:
        if any(alias in lowered for alias in aliases):
            return {
                "family": family,
                "reason": "explicit_prompt",
                "scores": {candidate: int(candidate == family) * 100 for candidate in sorted(COMPOSITION_FAMILIES)},
                "signals": {"prompt_override": next(alias for alias in aliases if alias in lowered)},
            }

    scores = {family: 0 for family in COMPOSITION_FAMILIES}
    reasons: dict[str, list[str]] = {family: [] for family in COMPOSITION_FAMILIES}

    def add(family: str, points: int, reason: str) -> None:
        scores[family] += points
        reasons[family].append(reason)

    bullet_count = sum(block["kind"] == "bullet" for block in blocks)
    heading_text = " ".join(block["text"] for block in blocks if block["kind"].startswith("heading")).lower()
    process_cues = len(re.findall(r"\b(?:how|process|workflow|steps?|from|build|create|start)\b", heading_text))
    if bullet_count >= 3:
        add("asymmetric_feature_grid", 2, "feature-rich source")
        add("modular_announcement", 1, "source supports modules")
    if bullet_count >= 3 and process_cues:
        add("numbered_process", 3, "ordered/process content")
    if sum(block["kind"] == "paragraph" for block in blocks) >= 4:
        add("editorial_narrative", 2, "narrative source")

    style = str(plan.get("style", "editorial"))
    if style == "editorial":
        add("editorial_narrative", 1, "editorial direction")
    elif style in {"bold", "executive"}:
        add("asymmetric_feature_grid", 1, f"{style} direction")
    if "announcement" in lowered or "launch" in lowered:
        add("modular_announcement", 4, "announcement direction")
    if "process" in lowered or "steps" in lowered:
        add("numbered_process", 4, "process direction")

    visual = _visual_language(system)
    mode = str(visual.get("primary_mode", "unknown"))
    density = str(visual.get("density", "unknown"))
    if mode == "illustration-led":
        add("modular_announcement", 5, "illustration-led brand")
    elif mode == "typography-led":
        add("editorial_narrative", 5, "typography-led brand")
    elif mode == "interface-led":
        add("numbered_process", 4, "interface-led brand")
    elif mode == "media-led":
        if density == "sparse":
            add("editorial_narrative", 5, "sparse media-led brand")
        else:
            add("asymmetric_feature_grid", 5, f"{density} media-led brand")

    # Fixed precedence makes ties reproducible and favors a usable baseline when
    # older design systems do not yet contain visual-language evidence.
    precedence = (
        "numbered_process",
        "modular_announcement",
        "asymmetric_feature_grid",
        "editorial_narrative",
    )
    family = max(precedence, key=lambda candidate: (scores[candidate], -precedence.index(candidate)))
    return {
        "family": family,
        "reason": "; ".join(reasons[family]) or "default composition",
        "scores": {candidate: scores[candidate] for candidate in sorted(scores)},
        "signals": {
            "brand_mode": mode,
            "brand_density": density,
            "bullet_count": bullet_count,
            "process_cues": process_cues,
            "planner_style": style,
        },
    }


def validate_composition(composition: dict[str, Any]) -> None:
    if composition.get("family") not in COMPOSITION_FAMILIES:
        raise ValueError("Unsupported composition family")
    if not isinstance(composition.get("scores"), dict) or not isinstance(composition.get("signals"), dict):
        raise ValueError("Invalid composition evidence")
