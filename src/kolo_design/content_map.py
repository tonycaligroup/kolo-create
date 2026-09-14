from __future__ import annotations

import re
from typing import Any


def _role(blocks: list[dict[str, str]], index: int, last: bool) -> str:
    kinds = [block["kind"] for block in blocks]
    text = " ".join(block["text"] for block in blocks).lower()
    if index == 0:
        return "opening"
    if last and "action" in kinds:
        return "closing"
    if sum(kind == "bullet" for kind in kinds) >= 3 and re.search(r"\b(step|process|workflow|start|then|next)\b", text):
        return "process"
    if sum(kind == "bullet" for kind in kinds) >= 2:
        return "features"
    if "callout" in kinds or sum(kind == "paragraph" for kind in kinds) >= 2:
        return "statement"
    return "section"


def build_content_map(blocks: list[dict[str, str]], prompt: str = "") -> dict[str, Any]:
    """Describe source meaning without rewriting or assigning geometry."""
    groups: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for block in blocks:
        if block["kind"] in {"heading1", "heading2"} and current:
            groups.append(current)
            current = []
        current.append(block)
    if current:
        groups.append(current)

    units = []
    for index, group in enumerate(groups):
        characters = sum(len(block["text"]) for block in group)
        role = _role(group, index, index == len(groups) - 1)
        units.append({
            "id": f"unit-{index + 1:02d}",
            "role": role,
            "block_ids": [block["id"] for block in group],
            "characters": characters,
            "density": "dense" if characters > 700 else "balanced" if characters > 280 else "sparse",
            "importance": 1.0 if role in {"opening", "closing"} else 0.86 if role in {"statement", "process"} else 0.72,
            "has_media_request": bool(re.search(r"\b(image|photo|visual|showcase|product)\b", prompt, re.I)),
        })
    return {"schema_version": 1, "source_policy": "preserve", "units": units}


def validate_content_map(content_map: dict[str, Any], blocks: list[dict[str, str]]) -> None:
    if content_map.get("schema_version") != 1 or content_map.get("source_policy") != "preserve":
        raise ValueError("Invalid content map")
    planned = [block_id for unit in content_map.get("units", []) for block_id in unit.get("block_ids", [])]
    expected = [block["id"] for block in blocks]
    if len(planned) != len(set(planned)) or set(planned) != set(expected):
        raise ValueError("Content map must own every source block exactly once")
