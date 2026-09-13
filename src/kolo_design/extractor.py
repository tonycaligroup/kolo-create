from __future__ import annotations

import colorsys
import asyncio
import base64
import html as html_module
import math
import re
from collections import Counter
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from PIL import Image, ImageColor

from .browser_extract import browser_snapshot, rasterize_svg
from .contracts import validate_design_system
from .network import MAX_ASSET_BYTES, MAX_HTML_BYTES, FetchError, fetch_limited
from .util import atomic_write, sha256_bytes, slugify, write_json

COLOR_PATTERN = re.compile(
    r"(?<![-\w])(?:#[0-9a-fA-F]{3,8}\b|rgba?\([^)]{3,60}\)|hsla?\([^)]{3,60}\))",
    re.I,
)
FONT_PATTERN = re.compile(r"font-family\s*:\s*([^;}{]+)", re.I)
SPACE_PATTERN = re.compile(r"(?:margin|padding|gap|row-gap|column-gap)(?:-[a-z]+)?\s*:[^;}{]*?(-?\d+(?:\.\d+)?)px", re.I)
RADIUS_PATTERN = re.compile(r"border-radius\s*:\s*(\d+(?:\.\d+)?)px", re.I)
WIDTH_PATTERN = re.compile(r"(?:max-width|width)\s*:\s*(\d{3,4})px", re.I)
CSS_VAR_PATTERN = re.compile(r"--([\w-]+)\s*:\s*([^;}{]+)")
BROWSER_DEFAULT_COLORS = {"#0000EE", "#551A8B", "#0000FF"}


def _color_to_hex(value: str) -> str | None:
    value = value.strip().lower()
    try:
        if value.startswith("#"):
            raw = value[1:]
            if len(raw) in {3, 4}:
                raw = "".join(char * 2 for char in raw)
            if len(raw) == 8:
                raw = raw[:6]
            if len(raw) == 6:
                return "#" + raw.upper()
        if value.startswith("rgb"):
            parts = re.findall(r"[\d.]+", value)
            if len(parts) >= 3:
                if value.startswith("rgba") and len(parts) >= 4 and float(parts[3]) <= 0.01:
                    return None
                channels = [round(float(part)) for part in parts[:3]]
                if all(0 <= channel <= 255 for channel in channels):
                    return "#" + "".join(f"{channel:02X}" for channel in channels)
        if value.startswith("hsl"):
            parts = re.findall(r"[\d.]+", value)
            if len(parts) >= 3:
                if value.startswith("hsla") and len(parts) >= 4 and float(parts[3]) <= 0.01:
                    return None
                h, s, light = float(parts[0]) % 360 / 360, float(parts[1]) / 100, float(parts[2]) / 100
                red, green, blue = colorsys.hls_to_rgb(h, light, s)
                return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"
        rgb = ImageColor.getrgb(value)
        return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
    except (ValueError, TypeError):
        return None
    return None


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


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


def _hue(value: str) -> float:
    red, green, blue = (channel / 255 for channel in _rgb(value))
    return colorsys.rgb_to_hsv(red, green, blue)[0]


def _hue_distance(left: str, right: str) -> float:
    distance = abs(_hue(left) - _hue(right))
    return min(distance, 1 - distance)


def _area_ratio(element: dict[str, Any], rendered: dict[str, Any]) -> float:
    viewport = element.get("viewport") or {}
    if isinstance(viewport.get("area_ratio"), (int, float)):
        return float(viewport["area_ratio"])
    bounds = element.get("rect") or {}
    page = rendered.get("viewport") or {"width": 1440, "height": 1000}
    area = max(0, float(bounds.get("width", 0))) * max(0, float(bounds.get("height", 0)))
    return min(1.0, area / max(1, float(page.get("width", 1440)) * float(page.get("height", 1000))))


def _is_overlay(element: dict[str, Any]) -> bool:
    return bool((element.get("semantic") or {}).get("overlay"))


def _in_primary_view(element: dict[str, Any]) -> bool:
    viewport = element.get("viewport") or {}
    visible = viewport.get("visible", True)
    region = str((element.get("semantic") or {}).get("region", "")).lower()
    return bool(visible) and not _is_overlay(element) and region not in {"dialog", "footer"}


def _choose_colors(css: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    variables: dict[str, str] = {}
    for name, raw in CSS_VAR_PATTERN.findall(css):
        parts = raw.split()
        if not parts:
            continue
        normalized = _color_to_hex(parts[0].rstrip(","))
        if normalized:
            variables[name.lower()] = normalized
    observed = [color for raw in COLOR_PATTERN.findall(css) if (color := _color_to_hex(raw))]
    counts = Counter(observed)
    ordered = [color for color, _ in counts.most_common(30) if color not in {"#00000000"}]
    if not ordered:
        ordered = ["#FFFFFF", "#F4F4F2", "#161616", "#5B5BD6"]

    def named(words: tuple[str, ...]) -> str | None:
        for name, color in variables.items():
            if any(word in name for word in words):
                return color
        return None

    background = named(("background", "page-bg", "canvas"))
    if not background:
        background = max(ordered, key=_luminance)
    text = named(("foreground", "text-primary", "text-color", "body-text"))
    if not text or _contrast(background, text) < 3:
        text = max(ordered + ["#111111", "#FFFFFF"], key=lambda candidate: _contrast(background, candidate))
    surface = named(("surface", "card", "panel"))
    if not surface or surface in {background, text}:
        surface = next((color for color in ordered if color not in {background, text} and _contrast(background, color) < 2.5), background)
    accent = named(("accent", "brand", "primary", "link"))
    if not accent or accent in {background, surface, text}:
        candidates = [color for color in ordered if color not in {background, surface, text}]
        accent = max(candidates, key=lambda color: abs(_luminance(color) - _luminance(background)), default="#5B5BD6")

    evidence = [
        {"value": color, "occurrences": count, "kind": "observed-css-color"}
        for color, count in counts.most_common(20)
    ]
    return {"background": background, "surface": surface, "text": text, "accent": accent}, evidence


def _refine_rendered_colors(
    colors: dict[str, str], rendered: dict[str, Any] | None, logo_colors: list[str] | None = None
) -> dict[str, str]:
    """Prefer what the browser actually paints over noisy stylesheet frequency."""
    if not rendered:
        return colors
    result = dict(colors)
    roots = rendered.get("root_styles") or {}
    background, text = result["background"], result["text"]
    root_accepted = False
    for node in ("body", "html"):
        root = roots.get(node) or {}
        candidate_background = _color_to_hex(root.get("background", ""))
        candidate_text = _color_to_hex(root.get("color", ""))
        # Consent overlays often dim the root without changing its inherited text.
        # Accept a rendered root palette only when it remains a readable pair.
        if candidate_background and candidate_text and _contrast(candidate_background, candidate_text) >= 3:
            background, text = candidate_background, candidate_text
            root_accepted = True
            break

    elements = rendered.get("elements") or []
    primary_elements = [element for element in elements if _in_primary_view(element)]
    large_backgrounds: Counter[str] = Counter()
    color_counts: Counter[str] = Counter()
    text_counts: Counter[str] = Counter()
    for element in primary_elements:
        style = element.get("style") or {}
        for key in ("color", "background", "border_color"):
            if value := _color_to_hex(style.get(key, "")):
                color_counts[value] += 1
        value = _color_to_hex(style.get("background", ""))
        area = _area_ratio(element, rendered)
        if value and element.get("tag") in {"article", "aside", "div", "main", "section"} and area >= 0.18:
            large_backgrounds[value] += area
        if element.get("text_sample") and (value := _color_to_hex(style.get("color", ""))):
            text_counts[value] += max(1, min(8, len(str(element["text_sample"])) // 12))

    painted_background = large_backgrounds.most_common(1)[0][0] if large_backgrounds else None
    if painted_background:
        root_is_dark = _luminance(background) < 0.2
        painted_is_light = _luminance(painted_background) > 0.7
        current_pair_is_unreadable = _contrast(background, text) < 3
        painted_is_dominant = large_backgrounds[painted_background] >= 0.4
        if (
            (root_accepted and root_is_dark and painted_is_light)
            or (not root_accepted and current_pair_is_unreadable)
            or (not root_accepted and painted_is_light and painted_is_dominant)
        ):
            background = painted_background
    readable_text = [value for value in text_counts if _contrast(background, value) >= 3]
    if readable_text:
        text = max(readable_text, key=lambda value: text_counts[value])
    elif _contrast(background, text) < 3:
        text = max([result["text"], "#111111", "#FFFFFF"], key=lambda value: _contrast(background, value))

    surface_counts: Counter[str] = Counter()
    for element in primary_elements:
        style = element.get("style") or {}
        value = _color_to_hex(style.get("background", ""))
        area = _area_ratio(element, rendered)
        if value and value != background and 0.008 <= area <= 0.45 and element.get("tag") in {"article", "aside", "div", "section"}:
            surface_counts[value] += max(0.1, math.sqrt(area))
    if painted_background and painted_background != background and _contrast(background, painted_background) < 1.35:
        surface = painted_background
    else:
        surface = surface_counts.most_common(1)[0][0] if surface_counts else result["surface"]
    if _contrast(surface, text) < 2.5:
        surface = result["surface"]

    excluded = {background, surface, text}

    accent_scores: Counter[str] = Counter()
    for value, count in color_counts.items():
        if value not in excluded and value not in BROWSER_DEFAULT_COLORS and _chroma(value) >= 0.25:
            accent_scores[value] += (_chroma(value) * 2.5) + math.log1p(count) * 0.3
    for element in primary_elements:
        if not (element.get("tag") == "button" or element.get("role") == "button" or element.get("href")):
            continue
        bounds = element.get("rect") or {}
        if not (26 <= bounds.get("height", 0) <= 90 and 38 <= bounds.get("width", 0) <= 520):
            continue
        value = _color_to_hex((element.get("style") or {}).get("background", ""))
        if value and value not in {background, text} and value not in BROWSER_DEFAULT_COLORS and _chroma(value) >= 0.2:
            accent_scores[value] += 8 + _chroma(value) * 3
    for index, value in enumerate(logo_colors or []):
        if value not in {background, text} and value not in BROWSER_DEFAULT_COLORS and _chroma(value) >= 0.25:
            accent_scores[value] += max(4, 10 - index * 2)
    fallback_accent = result["accent"]
    if fallback_accent in BROWSER_DEFAULT_COLORS:
        fallback_accent = text
    accent = accent_scores.most_common(1)[0][0] if accent_scores else fallback_accent
    secondary = next(
        (
            value for value, _ in accent_scores.most_common()
            if value != accent and _chroma(value) >= 0.45 and _hue_distance(value, accent) >= 0.08
            and _contrast(value, accent) >= 1.2
        ),
        accent,
    )
    refined = {
        "background": background, "surface": surface, "text": text,
        "accent": accent, "accent_secondary": secondary,
    }
    dark_candidates = [
        value for value in color_counts
        if value not in {background, surface, text}
        and _luminance(value) <= 0.12
        and _chroma(value) >= 0.45
        and _contrast(background, value) >= 3
    ]
    if dark_candidates:
        refined["brand_dark"] = max(dark_candidates, key=lambda value: color_counts[value])
    return refined


def _choose_fonts(css: str, soup: BeautifulSoup) -> tuple[str, str, list[dict[str, Any]]]:
    families: Counter[str] = Counter()
    for match in FONT_PATTERN.findall(css):
        family = match.split(",")[0].strip().strip("'\"")
        if family and not family.startswith("var(") and family.lower() not in {"inherit", "initial", "sans-serif", "serif"}:
            families[family] += 1
    for link in soup.select('link[href*="fonts.googleapis.com"]'):
        href = link.get("href", "")
        for name in re.findall(r"family=([^:&]+)", href):
            families[name.replace("+", " ")] += 3
    ranked = families.most_common()
    body = ranked[0][0] if ranked else "Helvetica"
    display = ranked[1][0] if len(ranked) > 1 and ranked[1][1] >= ranked[0][1] * 0.25 else body
    evidence = [{"value": family, "occurrences": count, "kind": "observed-font-family"} for family, count in families.most_common(10)]
    return display, body, evidence


def _font_category(stack: str) -> str:
    lowered = stack.lower()
    if any(marker in lowered for marker in ("times", "georgia", "garamond", "baskerville")):
        return "serif"
    if re.search(r"(?:^|[,\s])serif(?:$|[,\s])", lowered) and "sans-serif" not in lowered:
        return "serif"
    return "sans-serif"


def _refine_rendered_fonts(
    display: str, body: str, rendered: dict[str, Any] | None
) -> tuple[str, str, str, str]:
    if not rendered:
        return display, body, _font_category(display), _font_category(body)
    elements = [element for element in rendered.get("elements", []) if _in_primary_view(element)]
    headings = [element for element in elements if element.get("tag") in {"h1", "h2", "h3"} and element.get("text_sample")]
    body_elements = [
        element for element in elements
        if element.get("tag") in {"p", "li", "a", "button", "span"} and element.get("text_sample")
        and 10 <= _px((element.get("style") or {}).get("font_size")) <= 26
    ]

    def best(items: list[dict[str, Any]], fallback: str, *, size_weight: bool) -> tuple[str, str]:
        scores: Counter[str] = Counter()
        stacks: dict[str, str] = {}
        for element in items:
            stack = str((element.get("style") or {}).get("font_family", "")).strip()
            family = stack.split(",")[0].strip(" '\"")
            if not family or family.lower() in {"inherit", "initial", "sans-serif", "serif"}:
                continue
            stacks[family] = stack
            score = max(1.0, len(str(element.get("text_sample", ""))) / 24)
            if size_weight:
                score *= max(1.0, _px((element.get("style") or {}).get("font_size")) / 16)
            scores[family] += score
        family = scores.most_common(1)[0][0] if scores else fallback
        return family, _font_category(stacks.get(family, family))

    display_family, display_category = best(headings, display, size_weight=True)
    body_family, body_category = best(body_elements, body, size_weight=False)
    return display_family, body_family, display_category, body_category


def _spacing(css: str) -> tuple[int, list[int]]:
    values = [abs(round(float(item))) for item in SPACE_PATTERN.findall(css) if 0 < abs(float(item)) <= 160]
    if not values:
        return 8, [4, 8, 16, 24, 32, 48, 64]
    candidate_scores = {
        candidate: sum(1 for value in values if min(value % candidate, candidate - value % candidate) <= 1)
        for candidate in (4, 5, 6, 8, 10, 12)
    }
    base = max(candidate_scores, key=lambda item: (candidate_scores[item], item == 8, -item))
    scale = sorted(set([base, base * 2, base * 3, base * 4, base * 6, base * 8]))
    return base, scale


def _px(value: str | None) -> float:
    if not value:
        return 0
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else 0


def _dominant(values: list[Any], default: Any = None) -> Any:
    present = [value for value in values if value not in {None, "", "none", "normal", "rgba(0, 0, 0, 0)"}]
    return Counter(present).most_common(1)[0][0] if present else default


def _style_recipe(elements: list[dict[str, Any]], default_colors: dict[str, str]) -> dict[str, Any]:
    if not elements:
        return {}
    styles = [item["style"] for item in elements]
    foreground = _color_to_hex(_dominant([style.get("color") for style in styles], default_colors["text"])) or default_colors["text"]
    background = _color_to_hex(_dominant([style.get("background") for style in styles], default_colors["surface"])) or default_colors["surface"]
    border_color = _color_to_hex(_dominant([style.get("border_color") for style in styles], default_colors["text"])) or default_colors["text"]
    return {
        "observations": len(elements),
        "foreground": foreground,
        "background": background,
        "border_color": border_color,
        "border_width": round(_px(_dominant([style.get("border_width") for style in styles], "0px")), 2),
        "radius": round(_px(_dominant([style.get("border_radius") for style in styles], "0px")), 2),
        "shadow": _dominant([style.get("box_shadow") for style in styles], "none"),
        "font_family": str(_dominant([style.get("font_family") for style in styles], "Helvetica")).split(",")[0].strip(" '\""),
        "font_size": round(_px(_dominant([style.get("font_size") for style in styles], "16px")), 2),
        "font_weight": str(_dominant([style.get("font_weight") for style in styles], "400")),
        "line_height": _dominant([style.get("line_height") for style in styles], "normal"),
        "letter_spacing": _dominant([style.get("letter_spacing") for style in styles], "normal"),
        "text_align": _dominant([style.get("text_align") for style in styles], "left"),
        "padding": _dominant([style.get("padding") for style in styles], "0px"),
        "typical_width": round(float(_dominant([item["rect"].get("width") for item in elements], 0)), 2),
        "typical_height": round(float(_dominant([item["rect"].get("height") for item in elements], 0)), 2),
    }


def _style_variants(
    elements: list[dict[str, Any]], default_colors: dict[str, str], limit: int = 4
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for element in elements:
        style = element.get("style") or {}
        fingerprint = (
            _color_to_hex(style.get("background", "")),
            _color_to_hex(style.get("color", "")),
            round(_px(style.get("border_radius"))),
            round(_px(style.get("border_width"))),
            style.get("box_shadow") not in {None, "", "none"},
            str(style.get("font_family", "")).split(",")[0].strip(" '\""),
        )
        groups.setdefault(fingerprint, []).append(element)
    ranked = sorted(groups.values(), key=lambda group: len(group), reverse=True)[:limit]
    return [_style_recipe(group, default_colors) for group in ranked]


def _component_inventory(elements: list[dict[str, Any]], colors: dict[str, str]) -> dict[str, Any]:
    elements = [item for item in elements if not _is_overlay(item)]
    headings = {
        level: _style_recipe([item for item in elements if item.get("tag") == level], colors)
        for level in ("h1", "h2", "h3")
    }
    button_candidates = [
        item for item in elements
        if (item.get("tag") == "button" or item.get("role") == "button" or (item.get("tag") == "a" and item.get("href")))
        and 26 <= item.get("rect", {}).get("height", 0) <= 90
        and 38 <= item.get("rect", {}).get("width", 0) <= 520
    ]
    colored_buttons = [
        item for item in button_candidates
        if (_color_to_hex(item["style"].get("background", "")) or colors["background"]) not in {colors["background"], colors["surface"]}
    ]
    secondary_buttons = [item for item in button_candidates if item not in colored_buttons]
    card_candidates = [
        item for item in elements
        if item.get("tag") in {"article", "aside", "div", "section"}
        and 120 <= item.get("rect", {}).get("width", 0) <= 1000
        and 60 <= item.get("rect", {}).get("height", 0) <= 800
        and (
            _px(item["style"].get("border_radius")) >= 4
            or _px(item["style"].get("border_width")) > 0
            or item["style"].get("box_shadow") not in {"none", "", None}
        )
    ]
    nav = [item for item in elements if item.get("tag") in {"nav", "header"} or item.get("role") == "navigation"]
    sections = [
        item for item in elements
        if item.get("tag") in {"section", "main", "footer"}
        and 80 <= item.get("rect", {}).get("height", 0) <= 2500
    ]
    images = [item for item in elements if item.get("tag") in {"img", "picture", "video"} and item.get("rect", {}).get("height", 0) > 20]
    ratios = [round(item["rect"]["width"] / item["rect"]["height"], 2) for item in images if item["rect"]["height"]]
    return {
        "typography": headings,
        "buttons": {
            "primary": _style_recipe(colored_buttons, colors),
            "secondary": _style_recipe(secondary_buttons, colors),
            "variants": _style_variants(button_candidates, colors),
            "labels": [label for label, _ in Counter(item.get("text_sample", "") for item in button_candidates if item.get("text_sample")).most_common(8)],
        },
        "cards": _style_recipe(card_candidates, colors),
        "card_variants": _style_variants(card_candidates, colors),
        "navigation": _style_recipe(nav, colors),
        "sections": {
            "recipe": _style_recipe(sections, colors),
            "variants": _style_variants(sections, colors),
            "backgrounds": [value for value, _ in Counter(filter(None, (_color_to_hex(item["style"].get("background", "")) for item in sections))).most_common(8)],
        },
        "imagery": {
            "observations": len(images),
            "common_aspect_ratios": [value for value, _ in Counter(ratios).most_common(6)],
            "object_fit": _dominant([item["style"].get("object_fit") for item in images], "fill"),
            "radius": round(_px(_dominant([item["style"].get("border_radius") for item in images], "0px")), 2),
        },
    }


def _visual_language(rendered: dict[str, Any] | None) -> dict[str, Any]:
    if not rendered:
        return {
            "primary_mode": "unknown", "media_coverage": 0, "large_media_count": 0,
            "overlay_count": 0, "density": "unknown", "dominant_alignment": "left",
        }
    elements = rendered.get("elements") or []
    primary = [element for element in elements if _in_primary_view(element)]
    media = [element for element in primary if element.get("tag") in {"img", "picture", "video"}]
    media_coverage = min(1.0, sum(_area_ratio(element, rendered) for element in media))
    large_media = [element for element in media if _area_ratio(element, rendered) >= 0.12]
    illustration_area = sum(
        _area_ratio(item, rendered) for item in media
        if any(word in f"{item.get('src', '')} {item.get('alt', '')}".lower() for word in ("illustr", "character", "mascot", "artwork", ".svg"))
    )
    interface_area = sum(
        _area_ratio(item, rendered) for item in media
        if any(word in f"{item.get('src', '')} {item.get('alt', '')}".lower() for word in ("dashboard", "interface", "product-ui", "screenshot", "app-ui"))
    )
    if interface_area >= 0.12:
        primary_mode = "interface-led"
    elif illustration_area >= 0.08 or (media_coverage < 0.2 and illustration_area >= 0.008):
        primary_mode = "illustration-led"
    elif media_coverage >= 0.35 or large_media:
        primary_mode = "media-led"
    elif len(primary) >= 700:
        primary_mode = "interface-dense"
    else:
        primary_mode = "typography-led"
    alignments = Counter(
        str((element.get("style") or {}).get("text_align", "left"))
        for element in primary if element.get("text_sample")
    )
    density = "dense" if len(primary) >= 650 else "sparse" if len(primary) < 180 else "balanced"
    return {
        "primary_mode": primary_mode,
        "media_coverage": round(media_coverage, 3),
        "large_media_count": len(large_media),
        "overlay_count": sum(1 for element in elements if _is_overlay(element)),
        "density": density,
        "dominant_alignment": alignments.most_common(1)[0][0] if alignments else "left",
        "viewport_elements": len(primary),
    }


def _logo_candidates(soup: BeautifulSoup, base_url: str) -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    host_stem = (urlparse(base_url).hostname or "").lower().removeprefix("www.").split(".")[0]
    brand_hint = re.sub(r"(?:app|ai|inc)$", "", host_stem) or host_stem
    selectors = [
        ('meta[property="og:image"]', "content", 5, "og-image"),
        ('link[rel~="apple-touch-icon"]', "href", 120, "apple-touch-icon"),
        ('link[rel~="icon"]', "href", 90, "icon"),
        ('img[src]', "src", 10, "image"),
    ]
    for selector, attribute, score, source in selectors:
        for element in soup.select(selector):
            raw = element.get(attribute)
            if not raw or str(raw).startswith("data:"):
                continue
            resolved = urljoin(base_url, str(raw))
            hint = " ".join(str(element.get(key, "")) for key in ("alt", "aria-label")).lower()
            path_hint = urlparse(resolved).path.lower()
            filename_hint = Path(path_hint).name
            logo_match = "logo" in hint or "brand" in hint or "logo" in filename_hint or "brand" in filename_hint
            brand_match = bool(brand_hint and (brand_hint in hint or brand_hint in filename_hint))
            brand_logo_match = bool(
                brand_hint and (
                    (brand_hint in hint and ("logo" in hint or "brand" in hint))
                    or (brand_hint in filename_hint and ("logo" in filename_hint or "brand" in filename_hint))
                )
            )
            direct_brand_logo = bool(
                brand_hint and re.search(rf"{re.escape(brand_hint)}[-_ ]*logo", filename_hint)
            )
            semantic_score = (
                170 if direct_brand_logo
                else 160 if brand_logo_match
                else 130 if source == "og-image" and logo_match
                else 100 if brand_match
                else 80 if logo_match
                else score
            )
            adjusted = max(score, semantic_score)
            candidates.append((adjusted, resolved, source))
    return sorted(set(candidates), reverse=True)


def _save_best_logo(soup: BeautifulSoup, base_url: str, asset_dir: Path) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    try:
        from logo_scraper import discover_logo_candidates

        discovered = asyncio.run(discover_logo_candidates(base_url))
        if discovered:
            candidate = discovered[0]
            header, encoded = candidate.data_url.split(",", 1)
            payload = base64.b64decode(encoded)
            media_type = header.removeprefix("data:").split(";")[0]
            suffix = ".svg" if "svg" in media_type else ".png"
            target = asset_dir / f"logo{suffix}"
            atomic_write(target, payload)
            saved.append({
                "id": "primary-logo",
                "kind": "logo",
                "path": str(target),
                "source_url": base_url,
                "source": "logo-scraper",
                "score": 200,
                "sha256": sha256_bytes(payload),
                "media_type": media_type,
            })
            if suffix == ".svg" and (raster := rasterize_svg(payload)):
                raster_target = asset_dir / "logo.png"
                atomic_write(raster_target, raster)
                saved.append({
                    "id": "primary-logo-raster", "kind": "logo", "path": str(raster_target),
                    "source_url": base_url, "source": "svg-raster-fallback", "score": 199,
                    "sha256": sha256_bytes(raster), "media_type": "image/png",
                })
                return saved
            if suffix != ".svg":
                return saved
    except (ImportError, RuntimeError, ValueError):
        pass
    for score, url, source in _logo_candidates(soup, base_url)[:12]:
        try:
            final_url, payload, content_type = fetch_limited(url, MAX_ASSET_BYTES, accept="image/*")
            if not payload or not (content_type.startswith("image/") or urlparse(final_url).path.lower().endswith(".svg")):
                continue
            suffix = Path(urlparse(final_url).path).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
                suffix = ".svg" if "svg" in content_type else ".png"
            target = asset_dir / f"logo{suffix}"
            atomic_write(target, payload)
            asset = {
                "id": "primary-logo-raster" if saved else "primary-logo",
                "kind": "logo",
                "path": str(target),
                "source_url": final_url,
                "source": source,
                "score": score,
                "sha256": sha256_bytes(payload),
                "media_type": content_type.split(";")[0],
            }
            if suffix == ".svg":
                if not saved:
                    saved.append(asset)
                if raster := rasterize_svg(payload):
                    raster_target = asset_dir / "logo.png"
                    atomic_write(raster_target, raster)
                    saved.append({
                        "id": "primary-logo-raster", "kind": "logo", "path": str(raster_target),
                        "source_url": final_url, "source": "svg-raster-fallback", "score": max(0, score - 1),
                        "sha256": sha256_bytes(raster), "media_type": "image/png",
                    })
                    return saved
                continue
            saved.append(asset)
            return saved
        except Exception:
            continue
    return saved


def _logo_palette(assets: list[dict[str, Any]], limit: int = 5) -> list[str]:
    if not assets:
        return []
    path = next(
        (
            candidate for asset in assets
            if (candidate := Path(str(asset.get("path", "")))).exists() and candidate.suffix.lower() != ".svg"
        ),
        None,
    )
    if path is None:
        return []
    try:
        with Image.open(BytesIO(path.read_bytes())) as source:
            image = source.convert("RGBA")
            image.thumbnail((180, 180))
            pixel_data = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
            pixels = [
                (red, green, blue)
                for red, green, blue, alpha in pixel_data
                if alpha >= 96 and not (red >= 245 and green >= 245 and blue >= 245)
            ]
            if not pixels:
                return []
            quantized = Image.new("RGB", (len(pixels), 1))
            quantized.putdata(pixels)
            reduced = quantized.quantize(colors=12, method=Image.Quantize.MEDIANCUT).convert("RGB")
            reduced_data = reduced.get_flattened_data() if hasattr(reduced, "get_flattened_data") else reduced.getdata()
            counts = Counter(reduced_data)
            colors = [f"#{red:02X}{green:02X}{blue:02X}" for (red, green, blue), _ in counts.most_common()]
            chromatic = [value for value in colors if _chroma(value) >= 0.25 and 0.04 <= _luminance(value) <= 0.92]
            return chromatic[:limit]
    except (OSError, ValueError):
        return []


def _specimen(system: dict[str, Any]) -> bytes:
    colors = system["tokens"]["colors"]
    typo = system["tokens"]["typography"]
    spacing = system["tokens"]["spacing"]["base"]
    swatches = "".join(
        f'<div><span style="background:{value}"></span><b>{html_module.escape(role)}</b><code>{value}</code></div>'
        for role, value in colors.items()
    )
    primary = system.get("components", {}).get("buttons", {}).get("primary", {})
    radius = primary.get("radius", system["tokens"]["shape"]["radius"])
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{html_module.escape(system['name'])} design system</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:{colors['background']};color:{colors['text']};font-family:{typo['body_family']},Arial,sans-serif}}main{{max-width:1100px;margin:auto;padding:{spacing*8}px 32px}}.eyebrow{{color:{colors['accent']};text-transform:uppercase;letter-spacing:.14em;font-size:12px}}h1{{font-family:{typo['display_family']},Arial,sans-serif;font-size:clamp(48px,8vw,92px);line-height:.94;max-width:850px;margin:20px 0 64px}}h2{{font-size:32px;margin:72px 0 24px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:20px}}.grid>div,.card{{background:{colors['surface']};padding:20px;border-radius:{system['tokens']['shape']['radius']}px}}.grid span{{height:120px;display:block;margin-bottom:16px;border:1px solid color-mix(in srgb,currentColor 20%,transparent)}}b,code{{display:block;margin-top:7px}}code{{opacity:.7}}.components{{display:grid;grid-template-columns:1.2fr .8fr;gap:24px}}button{{border:0;border-radius:{radius}px;padding:14px 22px;font:600 15px inherit;margin:0 10px 12px 0}}.primary{{background:{colors['accent']};color:#fff}}.secondary{{background:{colors['surface']};color:{colors['text']};border:1px solid color-mix(in srgb,{colors['text']} 18%,transparent)}}.card{{min-height:180px}}.card p{{max-width:46ch;line-height:1.55}}</style></head>
<body><main><p class=\"eyebrow\">Extracted design language · v{system['version']}</p><h1>{html_module.escape(system['name'])}</h1><div class=\"grid\">{swatches}</div><h2>Component language</h2><div class=\"components\"><div class=\"card\"><p class=\"eyebrow\">Reusable card</p><h3>Structure from observed evidence</h3><p>Typography, surface, spacing, radius, border, and shadow treatments are stored as reusable recipes.</p></div><div><button class=\"primary\">Primary action</button><button class=\"secondary\">Secondary</button></div></div></main></body></html>""".encode()


def extract_brand(url: str, workspace: Path, name: str | None = None) -> dict[str, Any]:
    rendered = None
    try:
        candidate = browser_snapshot(url)
        if candidate and (candidate.get("response_status") is None or int(candidate["response_status"]) < 400):
            rendered = candidate
    except Exception:
        rendered = None
    if rendered:
        final_url = rendered["url"]
        html_bytes = rendered["html"].encode("utf-8")
        content_type = "text/html; source=browser"
    else:
        final_url, html_bytes, content_type = fetch_limited(url, MAX_HTML_BYTES, accept="text/html,application/xhtml+xml")
    if "html" not in content_type and b"<html" not in html_bytes[:2000].lower():
        raise FetchError("The supplied URL did not return an HTML page")
    soup = BeautifulSoup(html_bytes, "html.parser")
    title = (rendered.get("title", "").strip() if rendered else "") or (soup.title.string.strip() if soup.title and soup.title.string else urlparse(final_url).hostname or "Brand")
    brand_name = name or re.split(r"[|–—-]", title)[0].strip() or title
    brand_id = slugify(brand_name)
    brand_dir = workspace.resolve() / "brands" / brand_id / "1.0.0"
    asset_dir = brand_dir / "assets"

    css_parts = [tag.get_text(" ", strip=False) for tag in soup.find_all("style")]
    css_parts.extend(str(tag.get("style")) for tag in soup.select("[style]"))
    if rendered:
        css_parts.append(rendered["computed_css"])
    stylesheet_urls: list[str] = []
    for link in soup.select('link[rel~="stylesheet"][href]')[:10]:
        stylesheet_url = urljoin(final_url, str(link.get("href")))
        try:
            resolved, payload, _ = fetch_limited(stylesheet_url, 2 * 1024 * 1024, accept="text/css,*/*;q=.1")
            css_parts.append(payload.decode("utf-8", errors="replace"))
            stylesheet_urls.append(resolved)
        except Exception:
            continue
    css = "\n".join(css_parts)
    assets = _save_best_logo(soup, final_url, asset_dir)
    logo_colors = _logo_palette(assets)
    colors, color_evidence = _choose_colors(css)
    colors = _refine_rendered_colors(colors, rendered, logo_colors)
    display_font, body_font, font_evidence = _choose_fonts(css, soup)
    display_font, body_font, display_fallback, body_fallback = _refine_rendered_fonts(
        display_font, body_font, rendered
    )
    base_spacing, spacing_scale = _spacing(css)
    radii = [round(float(value)) for value in RADIUS_PATTERN.findall(css) if 2 <= float(value) <= 100]
    widths = [int(value) for value in WIDTH_PATTERN.findall(css) if 480 <= int(value) <= 1920]
    radius = Counter(radii).most_common(1)[0][0] if radii else 12
    content_width = Counter(widths).most_common(1)[0][0] if widths else 1120
    components = _component_inventory(rendered.get("elements", []) if rendered else [], colors)
    visual_language = _visual_language(rendered)
    screenshot_path = None
    if rendered and rendered.get("screenshot"):
        screenshot_path = brand_dir / "source-screenshot.png"
        atomic_write(screenshot_path, rendered["screenshot"])

    system = {
        "schema_version": 1,
        "id": brand_id,
        "version": "1.0.0",
        "name": brand_name,
        "created_at": datetime.now(UTC).isoformat(),
        "source": {"url": final_url, "html_sha256": sha256_bytes(html_bytes), "stylesheets": stylesheet_urls},
        "tokens": {
            "colors": colors,
            "typography": {
                "display_family": display_font, "body_family": body_font,
                "display_fallback": display_fallback, "body_fallback": body_fallback,
                "scale": [11, 14, 18, 26, 40, 64],
            },
            "spacing": {"base": base_spacing, "scale": spacing_scale},
            "shape": {"radius": radius},
            "layout": {"content_width": content_width, "columns": 12},
        },
        "recipes": {
            "cover": {"accent_rule": True, "headline_scale": "display", "whitespace": "generous"},
            "section": {"max_columns": 2, "heading_alignment": "left"},
            "callout": {"background": "surface", "accent_edge": True},
        },
        "components": components,
        "visual_language": visual_language,
        "assets": assets,
        "evidence": {
            "colors": color_evidence,
            "logo_colors": logo_colors,
            "fonts": font_evidence,
            "counts": {
                "css_bytes": len(css.encode()), "stylesheets_fetched": len(stylesheet_urls),
                "logo_assets": len(assets), "browser_rendered": bool(rendered),
                "visible_elements_sampled": len(rendered.get("elements", [])) if rendered else 0,
                "overlays_excluded": visual_language["overlay_count"],
            },
            "limitations": [
                "Browser-rendered evidence is viewport-bounded; states below the sampled page region may be missed",
                "Observed web fonts are classified for portable PDF fallback but are not embedded automatically",
                "semantic token roles are deterministic candidates and should be reviewed before high-stakes publication",
            ],
        },
    }
    validate_design_system(system)
    write_json(brand_dir / "design-system.json", system)
    write_json(workspace.resolve() / "brands" / brand_id / "latest.json", system)
    component_inventory_path = write_json(brand_dir / "component-inventory.json", components)
    atomic_write(brand_dir / "specimen.html", _specimen(system))
    return {
        "status": "succeeded",
        "design_system": str(brand_dir / "design-system.json"),
        "latest": str(workspace.resolve() / "brands" / brand_id / "latest.json"),
        "specimen": str(brand_dir / "specimen.html"),
        "component_inventory": str(component_inventory_path),
        "brand_id": brand_id,
        "evidence": system["evidence"]["counts"],
        "source_screenshot": str(screenshot_path) if screenshot_path else None,
    }
