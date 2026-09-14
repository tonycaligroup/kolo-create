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


def _usable_logo_raster(path: Path, width: int, height: int, asset: dict[str, Any]) -> bool:
    """Reject favicon-like and visually empty rasters before they reach a cover."""
    source_hint = " ".join(str(asset.get(key, "")) for key in ("source", "source_url", "provenance")).lower()
    if asset.get("source") == "image" and not any(
        marker in source_hint for marker in ("logo", "wordmark", "brandmark", "brand-mark")
    ):
        return False
    if 0.82 <= width / max(1, height) <= 1.22 and any(
        marker in source_hint for marker in ("favicon", "apple-touch", "app-icon", "manifest")
    ):
        return False
    try:
        with PILImage.open(path) as source:
            rgba = source.convert("RGBA")
            alpha = rgba.getchannel("A")
            bbox = alpha.getbbox()
            if bbox is None:
                return False
            visible_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            if visible_area / max(1, width * height) < 0.04:
                return False
            alpha_sample = alpha.copy()
            alpha_sample.thumbnail((96, 96))
            painted_pixels = sum(1 for value in alpha_sample.get_flattened_data() if value >= 16)
            if (
                asset.get("source") == "visible-header-logo"
                and 0.82 <= width / max(1, height) <= 1.22
                and painted_pixels / max(1, alpha_sample.width * alpha_sample.height) < 0.08
            ):
                return False
            # Chromium paints a failed transparent image as a nearly solid dark
            # square with a tiny broken-image glyph in one corner. It is fully
            # opaque, so alpha-only checks mistake it for a legitimate mark.
            # Reject near-square header captures whose dominant painted color
            # consumes almost the entire crop; real app tiles retain materially
            # more foreground detail.
            if asset.get("source") == "visible-header-logo" and 0.82 <= width / max(1, height) <= 1.22:
                sample = rgba.copy()
                sample.thumbnail((96, 96))
                opaque_pixels = [pixel[:3] for pixel in sample.get_flattened_data() if pixel[3] >= 240]
                if opaque_pixels:
                    color_counts: dict[tuple[int, int, int], int] = {}
                    for pixel in opaque_pixels:
                        color_counts[pixel] = color_counts.get(pixel, 0) + 1
                    dominant_count = max(color_counts.values(), default=0)
                    if dominant_count / len(opaque_pixels) >= 0.9:
                        return False
    except (OSError, ValueError):
        return False
    return True


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
        if not _usable_logo_raster(path, width, height, asset):
            continue
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
