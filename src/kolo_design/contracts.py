from __future__ import annotations

import re
from typing import Any


HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_design_system(value: dict[str, Any]) -> None:
    required = {"schema_version", "id", "version", "name", "source", "tokens", "recipes", "assets", "evidence"}
    missing = required - value.keys()
    if missing:
        raise ValueError(f"Design system missing fields: {sorted(missing)}")
    if value["schema_version"] != 1:
        raise ValueError("Unsupported design-system schema version")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value["id"]):
        raise ValueError("Invalid design-system ID")
    colors = value["tokens"].get("colors", {})
    for role in ("background", "surface", "text", "accent"):
        if role not in colors or not HEX.fullmatch(colors[role]):
            raise ValueError(f"Missing or invalid color token: {role}")
    typography = value["tokens"].get("typography", {})
    if not typography.get("display_family") or not typography.get("body_family"):
        raise ValueError("Typography families are required")
    spacing = value["tokens"].get("spacing", {})
    if not isinstance(spacing.get("base"), (int, float)) or spacing["base"] <= 0:
        raise ValueError("Positive base spacing token is required")


def validate_document_request(content: str, prompt: str) -> None:
    if not content.strip():
        raise ValueError("Document content is empty")
    if len(content.encode("utf-8")) > 2_000_000:
        raise ValueError("Document content exceeds the 2 MB v1 limit")
    if not prompt.strip():
        raise ValueError("A design prompt is required")
