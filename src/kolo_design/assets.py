from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image as PILImage


RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def raster_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with PILImage.open(path) as image:
            return image.width, image.height
    except (OSError, ValueError):
        return None


def select_logo_asset(
    system: dict[str, Any],
    *,
    allow_svg: bool,
    max_width: float,
    max_height: float,
    minimum_density: float = 1.5,
) -> dict[str, Any] | None:
    """Choose a logo that can be rendered without visibly enlarging its pixels."""
    candidates: list[tuple[float, dict[str, Any]]] = []
    for asset in system.get("assets") or []:
        if asset.get("kind") != "logo":
            continue
        path = Path(str(asset.get("path", "")))
        if not path.exists():
            continue
        suffix = path.suffix.lower()
        if suffix == ".svg" and allow_svg:
            return asset
        if suffix not in RASTER_SUFFIXES:
            continue
        dimensions = raster_dimensions(path)
        if not dimensions:
            continue
        width, height = dimensions
        scale = min(max_width / width, max_height / height)
        rendered_width, rendered_height = width * scale, height * scale
        density = min(width / rendered_width, height / rendered_height)
        if density < minimum_density:
            continue
        score = density * 1000 + width * height / 1000 + float(asset.get("score", 0))
        enriched = dict(asset)
        enriched["pixel_width"] = width
        enriched["pixel_height"] = height
        candidates.append((score, enriched))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None
