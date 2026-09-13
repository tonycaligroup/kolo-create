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


def _refine_rendered_colors(colors: dict[str, str], rendered: dict[str, Any] | None) -> dict[str, str]:
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
    container_backgrounds: list[str] = []
    color_counts: Counter[str] = Counter()
    for element in elements:
        style = element.get("style") or {}
        for key in ("color", "background", "border_color"):
            if value := _color_to_hex(style.get(key, "")):
                color_counts[value] += 1
        if element.get("tag") in {"article", "aside", "div", "section", "header", "footer"}:
            value = _color_to_hex(style.get("background", ""))
            rect = element.get("rect") or {}
            if value and value != background and rect.get("width", 0) >= 120 and rect.get("height", 0) >= 50:
                container_backgrounds.append(value)
    surface = Counter(container_backgrounds).most_common(1)[0][0] if container_backgrounds else result["surface"]
    if _contrast(surface, text) < 2.5:
        surface = result["surface"]
    if not root_accepted and _contrast(surface, text) >= 3:
        background = surface

    excluded = {background, surface, text}

    def chroma(value: str) -> float:
        red, green, blue = (channel / 255 for channel in _rgb(value))
        return colorsys.rgb_to_hsv(red, green, blue)[1]

    accent_candidates = [value for value in color_counts if value not in excluded and chroma(value) >= 0.25]
    accent = max(
        accent_candidates,
        key=lambda value: (chroma(value) * 3) + math.log1p(color_counts[value]) * 0.35,
        default=result["accent"],
    )
    return {"background": background, "surface": surface, "text": text, "accent": accent}


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


def _component_inventory(elements: list[dict[str, Any]], colors: dict[str, str]) -> dict[str, Any]:
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
            "labels": [label for label, _ in Counter(item.get("text_sample", "") for item in button_candidates if item.get("text_sample")).most_common(8)],
        },
        "cards": _style_recipe(card_candidates, colors),
        "navigation": _style_recipe(nav, colors),
        "sections": {
            "recipe": _style_recipe(sections, colors),
            "backgrounds": [value for value, _ in Counter(filter(None, (_color_to_hex(item["style"].get("background", "")) for item in sections))).most_common(8)],
        },
        "imagery": {
            "observations": len(images),
            "common_aspect_ratios": [value for value, _ in Counter(ratios).most_common(6)],
            "object_fit": _dominant([item["style"].get("object_fit") for item in images], "fill"),
            "radius": round(_px(_dominant([item["style"].get("border_radius") for item in images], "0px")), 2),
        },
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
            logo_match = "logo" in hint or "brand" in hint or "logo" in path_hint or "brand" in path_hint
            brand_match = bool(brand_hint and (brand_hint in hint or brand_hint in path_hint))
            semantic_score = 110 if source == "og-image" and logo_match else 100 if logo_match and brand_match else 80 if brand_match else 60 if logo_match else score
            adjusted = max(score, semantic_score)
            candidates.append((adjusted, resolved, source))
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
    primary = system.get("components", {}).get("buttons", {}).get("primary", {})
    radius = primary.get("radius", system["tokens"]["shape"]["radius"])
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{html_module.escape(system['name'])} design system</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:{colors['background']};color:{colors['text']};font-family:{typo['body_family']},Arial,sans-serif}}main{{max-width:1100px;margin:auto;padding:{spacing*8}px 32px}}.eyebrow{{color:{colors['accent']};text-transform:uppercase;letter-spacing:.14em;font-size:12px}}h1{{font-family:{typo['display_family']},Arial,sans-serif;font-size:clamp(48px,8vw,92px);line-height:.94;max-width:850px;margin:20px 0 64px}}h2{{font-size:32px;margin:72px 0 24px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:20px}}.grid>div,.card{{background:{colors['surface']};padding:20px;border-radius:{system['tokens']['shape']['radius']}px}}.grid span{{height:120px;display:block;margin-bottom:16px;border:1px solid color-mix(in srgb,currentColor 20%,transparent)}}b,code{{display:block;margin-top:7px}}code{{opacity:.7}}.components{{display:grid;grid-template-columns:1.2fr .8fr;gap:24px}}button{{border:0;border-radius:{radius}px;padding:14px 22px;font:600 15px inherit;margin:0 10px 12px 0}}.primary{{background:{colors['accent']};color:#fff}}.secondary{{background:{colors['surface']};color:{colors['text']};border:1px solid color-mix(in srgb,{colors['text']} 18%,transparent)}}.card{{min-height:180px}}.card p{{max-width:46ch;line-height:1.55}}</style></head>
<body><main><p class=\"eyebrow\">Extracted design language · v{system['version']}</p><h1>{html_module.escape(system['name'])}</h1><div class=\"grid\">{swatches}</div><h2>Component language</h2><div class=\"components\"><div class=\"card\"><p class=\"eyebrow\">Reusable card</p><h3>Structure from observed evidence</h3><p>Typography, surface, spacing, radius, border, and shadow treatments are stored as reusable recipes.</p></div><div><button class=\"primary\">Primary action</button><button class=\"secondary\">Secondary</button></div></div></main></body></html>""".encode()


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
    colors = _refine_rendered_colors(colors, rendered)
    display_font, body_font, font_evidence = _choose_fonts(css, soup)
    base_spacing, spacing_scale = _spacing(css)
    radii = [round(float(value)) for value in RADIUS_PATTERN.findall(css) if 2 <= float(value) <= 100]
    widths = [int(value) for value in WIDTH_PATTERN.findall(css) if 480 <= int(value) <= 1920]
    radius = Counter(radii).most_common(1)[0][0] if radii else 12
    content_width = Counter(widths).most_common(1)[0][0] if widths else 1120
    assets = _save_best_logo(soup, final_url, asset_dir)
    components = _component_inventory(rendered.get("elements", []) if rendered else [], colors)
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
        "components": components,
        "assets": assets,
        "evidence": {
            "colors": color_evidence,
            "fonts": font_evidence,
            "counts": {"css_bytes": len(css.encode()), "stylesheets_fetched": len(stylesheet_urls), "logo_assets": len(assets), "browser_rendered": bool(rendered), "visible_elements_sampled": len(rendered.get("elements", [])) if rendered else 0},
            "limitations": [
                "v1 reads server-rendered HTML and linked CSS; client-only computed styles may be missed",
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
