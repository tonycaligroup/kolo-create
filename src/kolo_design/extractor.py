from __future__ import annotations

import colorsys
import asyncio
import base64
import html as html_module
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from PIL import ImageColor

from .browser_extract import browser_snapshot
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
                channels = [round(float(part)) for part in parts[:3]]
                if all(0 <= channel <= 255 for channel in channels):
                    return "#" + "".join(f"{channel:02X}" for channel in channels)
        if value.startswith("hsl"):
            parts = re.findall(r"[\d.]+", value)
            if len(parts) >= 3:
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


def _choose_colors(css: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    variables: dict[str, str] = {}
    for name, raw in CSS_VAR_PATTERN.findall(css):
        normalized = _color_to_hex(raw.split()[0].rstrip(","))
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


def _logo_candidates(soup: BeautifulSoup, base_url: str) -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    selectors = [
        ('meta[property="og:image"]', "content", 55, "og-image"),
        ('link[rel~="apple-touch-icon"]', "href", 80, "apple-touch-icon"),
        ('link[rel~="icon"]', "href", 45, "icon"),
        ('img[src]', "src", 10, "image"),
    ]
    for selector, attribute, score, source in selectors:
        for element in soup.select(selector):
            raw = element.get(attribute)
            if not raw or str(raw).startswith("data:"):
                continue
            hint = " ".join(str(element.get(key, "")) for key in ("class", "id", "alt", "aria-label")).lower()
            adjusted = score + (70 if "logo" in hint or "brand" in hint else 0)
            candidates.append((adjusted, urljoin(base_url, str(raw)), source))
    return sorted(set(candidates), reverse=True)


def _save_best_logo(soup: BeautifulSoup, base_url: str, asset_dir: Path) -> list[dict[str, Any]]:
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
            return [{
                "id": "primary-logo",
                "kind": "logo",
                "path": str(target),
                "source_url": base_url,
                "source": "logo-scraper",
                "score": 200,
                "sha256": sha256_bytes(payload),
                "media_type": media_type,
            }]
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
            return [{
                "id": "primary-logo",
                "kind": "logo",
                "path": str(target),
                "source_url": final_url,
                "source": source,
                "score": score,
                "sha256": sha256_bytes(payload),
                "media_type": content_type.split(";")[0],
            }]
        except Exception:
            continue
    return []


def _specimen(system: dict[str, Any]) -> bytes:
    colors = system["tokens"]["colors"]
    typo = system["tokens"]["typography"]
    spacing = system["tokens"]["spacing"]["base"]
    swatches = "".join(
        f'<div><span style="background:{value}"></span><b>{html_module.escape(role)}</b><code>{value}</code></div>'
        for role, value in colors.items()
    )
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{html_module.escape(system['name'])} design system</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:{colors['background']};color:{colors['text']};font-family:{typo['body_family']},Arial,sans-serif}}main{{max-width:1100px;margin:auto;padding:{spacing*8}px 32px}}.eyebrow{{color:{colors['accent']};text-transform:uppercase;letter-spacing:.14em;font-size:12px}}h1{{font-family:{typo['display_family']},Arial,sans-serif;font-size:clamp(48px,8vw,92px);line-height:.94;max-width:850px;margin:20px 0 64px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:20px}}.grid div{{background:{colors['surface']};padding:20px;border-radius:{system['tokens']['shape']['radius']}px}}.grid span{{height:120px;display:block;margin-bottom:16px;border:1px solid color-mix(in srgb,currentColor 20%,transparent)}}b,code{{display:block;margin-top:7px}}code{{opacity:.7}}</style></head>
<body><main><p class=\"eyebrow\">Extracted design language · v{system['version']}</p><h1>{html_module.escape(system['name'])}</h1><div class=\"grid\">{swatches}</div></main></body></html>""".encode()


def extract_brand(url: str, workspace: Path, name: str | None = None) -> dict[str, Any]:
    final_url, html_bytes, content_type = fetch_limited(url, MAX_HTML_BYTES, accept="text/html,application/xhtml+xml")
    if "html" not in content_type and b"<html" not in html_bytes[:2000].lower():
        raise FetchError("The supplied URL did not return an HTML page")
    soup = BeautifulSoup(html_bytes, "html.parser")
    rendered = None
    try:
        rendered = browser_snapshot(final_url)
    except Exception:
        rendered = None
    if rendered:
        final_url = rendered["url"]
        soup = BeautifulSoup(rendered["html"], "html.parser")
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
    colors, color_evidence = _choose_colors(css)
    display_font, body_font, font_evidence = _choose_fonts(css, soup)
    base_spacing, spacing_scale = _spacing(css)
    radii = [round(float(value)) for value in RADIUS_PATTERN.findall(css) if 2 <= float(value) <= 100]
    widths = [int(value) for value in WIDTH_PATTERN.findall(css) if 480 <= int(value) <= 1920]
    radius = Counter(radii).most_common(1)[0][0] if radii else 12
    content_width = Counter(widths).most_common(1)[0][0] if widths else 1120
    assets = _save_best_logo(soup, final_url, asset_dir)
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
            "typography": {"display_family": display_font, "body_family": body_font, "scale": [11, 14, 18, 26, 40, 64]},
            "spacing": {"base": base_spacing, "scale": spacing_scale},
            "shape": {"radius": radius},
            "layout": {"content_width": content_width, "columns": 12},
        },
        "recipes": {
            "cover": {"accent_rule": True, "headline_scale": "display", "whitespace": "generous"},
            "section": {"max_columns": 2, "heading_alignment": "left"},
            "callout": {"background": "surface", "accent_edge": True},
        },
        "assets": assets,
        "evidence": {
            "colors": color_evidence,
            "fonts": font_evidence,
            "counts": {"css_bytes": len(css.encode()), "stylesheets_fetched": len(stylesheet_urls), "logo_assets": len(assets), "browser_rendered": bool(rendered)},
            "limitations": [
                "v1 reads server-rendered HTML and linked CSS; client-only computed styles may be missed",
                "semantic token roles are deterministic candidates and should be reviewed before high-stakes publication",
            ],
        },
    }
    validate_design_system(system)
    write_json(brand_dir / "design-system.json", system)
    write_json(workspace.resolve() / "brands" / brand_id / "latest.json", system)
    atomic_write(brand_dir / "specimen.html", _specimen(system))
    return {
        "status": "succeeded",
        "design_system": str(brand_dir / "design-system.json"),
        "latest": str(workspace.resolve() / "brands" / brand_id / "latest.json"),
        "specimen": str(brand_dir / "specimen.html"),
        "brand_id": brand_id,
        "evidence": system["evidence"]["counts"],
        "source_screenshot": str(screenshot_path) if screenshot_path else None,
    }
