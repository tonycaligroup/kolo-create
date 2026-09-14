from __future__ import annotations

import colorsys
import math
import shutil
import subprocess
import tempfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat
from pypdf import PdfReader

from .util import sha256_bytes


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


def _distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return math.sqrt(sum((left[index] - right[index]) ** 2 for index in range(3)))


def _share_near(pixels: list[tuple[int, int, int]], value: str, threshold: float = 12) -> float:
    target = _rgb(value)
    hits = sum(1 for pixel in pixels if _distance(pixel, target) <= threshold)
    return round(hits / max(1, len(pixels)), 7)


def _sample(image: Image.Image, size: int = 360) -> tuple[Image.Image, list[tuple[int, int, int]]]:
    sampled = image.convert("RGB")
    sampled.thumbnail((size, size))
    pixel_reader = getattr(sampled, "get_flattened_data", sampled.getdata)
    return sampled, list(pixel_reader())


def _information_share(pixels: list[tuple[int, int, int]]) -> float:
    if not pixels:
        return 0
    dominant = Counter(pixels).most_common(1)[0][0]
    similar = sum(1 for pixel in pixels if _distance(pixel, dominant) <= 16)
    return round(1 - similar / len(pixels), 4)


def _image_similarity(left: Image.Image, right: Image.Image) -> float:
    size = (320, 244)
    first = left.convert("RGB").resize(size)
    second = right.convert("RGB").resize(size)
    mean = sum(ImageStat.Stat(ImageChops.difference(first, second)).mean) / 3
    return round(max(0, 1 - mean / 255), 4)


def _logo_support(value: str, logo_colors: list[str]) -> bool:
    target = _rgb(value)
    return any(_distance(target, _rgb(candidate)) <= 24 for candidate in logo_colors)


def _status(screenshot_share: float, pdf_share: float, logo_supported: bool) -> str:
    if logo_supported or screenshot_share >= 0.00002 or pdf_share >= 0.00001:
        return "supported"
    if screenshot_share >= 0.000002 or pdf_share >= 0.000002:
        return "weakly_supported"
    return "contradicted"


def analyze_reference_pdf(
    pdf_path: Path,
    screenshot_payload: bytes,
    candidates: list[str],
    logo_colors: list[str],
) -> dict[str, Any]:
    """Rasterize a browser PDF and measure whether candidate colors truly render."""
    payload = pdf_path.read_bytes()
    reader = PdfReader(BytesIO(payload))
    page_count = len(reader.pages)
    renderer = shutil.which("pdftoppm")
    if not renderer:
        return {
            "status": "unverified",
            "usable_for_color_validation": False,
            "reason": "pdftoppm_unavailable",
            "pages": page_count,
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
            "candidates": [],
        }

    with tempfile.TemporaryDirectory(prefix="kolo-reference-pdf-") as temporary:
        prefix = Path(temporary) / "page"
        subprocess.run(
            [renderer, "-png", "-r", "36", str(pdf_path), str(prefix)],
            check=True,
            timeout=120,
            capture_output=True,
        )
        page_paths = sorted(Path(temporary).glob("page-*.png"))
        sampled_pages: list[Image.Image] = []
        pdf_pixels: list[tuple[int, int, int]] = []
        page_information: list[float] = []
        for page_path in page_paths:
            sampled, pixels = _sample(Image.open(page_path))
            sampled_pages.append(sampled)
            pdf_pixels.extend(pixels)
            page_information.append(_information_share(pixels))

    screenshot, screenshot_pixels = _sample(Image.open(BytesIO(screenshot_payload)))
    similarity = _image_similarity(screenshot, sampled_pages[0]) if sampled_pages else 0
    low_information_pages = sum(value < 0.04 for value in page_information)
    low_information_ratio = round(low_information_pages / max(1, len(page_information)), 4)
    if not sampled_pages:
        grade = "rejected"
    elif similarity < 0.55 or low_information_ratio >= 0.35:
        grade = "degraded"
    elif similarity < 0.7 or low_information_ratio >= 0.12:
        grade = "degraded"
    else:
        grade = "usable"

    color_results = []
    for value in dict.fromkeys(candidate.upper() for candidate in candidates):
        screenshot_share = _share_near(screenshot_pixels, value)
        pdf_share = _share_near(pdf_pixels, value)
        logo_supported = _logo_support(value, logo_colors)
        color_results.append({
            "value": value,
            "status": _status(screenshot_share, pdf_share, logo_supported),
            "screenshot_share_within_rgb_12": screenshot_share,
            "pdf_share_within_rgb_12": pdf_share,
            "logo_supported": logo_supported,
        })
    return {
        "status": grade,
        "usable_for_color_validation": grade in {"usable", "degraded"},
        "pages": page_count,
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
        "screen_media": True,
        "first_page_screenshot_similarity": similarity,
        "low_information_pages": low_information_pages,
        "low_information_page_ratio": low_information_ratio,
        "page_information_share": page_information,
        "candidates": color_results,
        "limitations": [
            "Photographic pixels corroborate color presence but do not establish semantic UI roles alone.",
            "Footer and repeated fixed-element weighting requires DOM evidence in addition to PDF pixels.",
            "Dynamic pages are frozen at one observed state for deterministic capture.",
        ],
    }


def _luminance(value: str) -> float:
    channels = []
    for channel in _rgb(value):
        normalized = channel / 255
        channels.append(normalized / 12.92 if normalized <= 0.04045 else ((normalized + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(left: str, right: str) -> float:
    high, low = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _chroma(value: str) -> float:
    red, green, blue = (channel / 255 for channel in _rgb(value))
    return colorsys.rgb_to_hsv(red, green, blue)[1]


def reconcile_reference_colors(
    colors: dict[str, str],
    color_evidence: list[dict[str, Any]],
    reference: dict[str, Any],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Reject major semantic colors contradicted by both rendered artifacts."""
    result = dict(colors)
    if not reference.get("usable_for_color_validation"):
        return result, []
    support = {item["value"]: item for item in reference.get("candidates", [])}

    def status(value: str) -> str:
        return str((support.get(value.upper()) or {}).get("status", "unverified"))

    decisions: list[dict[str, Any]] = []
    initial_accent = result["accent"]
    if status(initial_accent) == "contradicted":
        alternatives = [
            item for item in color_evidence
            if status(str(item.get("value", ""))) == "supported"
            and _chroma(str(item["value"])) >= 0.25
            and str(item["value"]).upper() not in {result["background"], result["surface"], result["text"]}
        ]
        if alternatives:
            result["accent"] = str(max(alternatives, key=lambda item: int(item.get("occurrences", 0)))["value"]).upper()
    decisions.append({
        "role": "accent", "initial": initial_accent, "final": result["accent"],
        "status": status(initial_accent),
        "reason": "kept_or_replaced_using_rendered_reference_evidence",
    })

    initial_secondary = result.get("accent_secondary", result["accent"])
    secondary_status = status(initial_secondary)
    if secondary_status != "supported":
        result["accent_secondary"] = result["accent"]
    decisions.append({
        "role": "accent_secondary", "initial": initial_secondary, "final": result["accent_secondary"],
        "status": secondary_status,
        "reason": "collapsed_to_primary_when_not_visibly_supported",
    })

    initial_dark = result.get("brand_dark")
    dark_status = status(initial_dark) if initial_dark else "missing"
    initial_dark_support = support.get(str(initial_dark).upper()) if initial_dark else None
    initial_dark_is_material = bool(
        initial_dark_support
        and (
            initial_dark_support.get("logo_supported")
            or float(initial_dark_support.get("pdf_share_within_rgb_12", 0)) >= 0.002
            or float(initial_dark_support.get("screenshot_share_within_rgb_12", 0)) >= 0.002
        )
    )
    if initial_dark and (dark_status != "supported" or not initial_dark_is_material):
        result.pop("brand_dark", None)
    if "brand_dark" not in result:
        excluded = {result[role].upper() for role in ("background", "surface", "accent", "accent_secondary")}
        dark_candidates = []
        for item in color_evidence:
            value = str(item.get("value", "")).upper()
            candidate = support.get(value) or {}
            if (
                candidate.get("status") == "supported"
                and (
                    candidate.get("logo_supported")
                    or float(candidate.get("pdf_share_within_rgb_12", 0)) >= 0.002
                    or float(candidate.get("screenshot_share_within_rgb_12", 0)) >= 0.002
                )
                and value not in excluded
                and _luminance(value) <= 0.12
                and _chroma(value) >= 0.08
                and _contrast(result["background"], value) >= 3
            ):
                score = (
                    float(candidate.get("pdf_share_within_rgb_12", 0)) * 1000
                    + float(candidate.get("screenshot_share_within_rgb_12", 0)) * 1500
                    + math.log1p(int(item.get("occurrences", 0)))
                    + (20 if candidate.get("logo_supported") else 0)
                )
                dark_candidates.append((score, value))
        if dark_candidates:
            result["brand_dark"] = max(dark_candidates)[1]
    decisions.append({
        "role": "brand_dark", "initial": initial_dark, "final": result.get("brand_dark"),
        "status": dark_status,
        "reason": "kept_or_replaced_with_supported_dark_rendered_candidate",
    })
    return result, decisions
