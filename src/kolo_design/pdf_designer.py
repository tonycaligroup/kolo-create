from __future__ import annotations

import html
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.utils import ImageReader
from reportlab.platypus import CondPageBreak, Flowable, HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .assets import select_logo_asset
from .brand_components import select_component_plan, validate_component_plan
from .composition import select_composition, validate_composition
from .contracts import validate_design_system, validate_document_request
from .planner import DeterministicPlanner, DocumentPlanner, source_blocks, validate_plan
from .util import confined, read_json, sha256_bytes, write_json

EMOJI_PATTERN = re.compile(
    "["
    "\\U0001F1E6-\\U0001F1FF"
    "\\U0001F300-\\U0001FAFF"
    "\\U00002700-\\U000027BF"
    "\\U00002600-\\U000026FF"
    "\\U00002B00-\\U00002BFF"
    "\\U00002300-\\U000023FF"
    "\\U0000FE0F\\U0000200D"
    "]+"
)
TOFU_MARKERS = {"■", "□", "�"}


def _reportlab_color(value: str) -> colors.Color:
    return colors.HexColor(value)


def _recipe_color(value: Any, fallback: str) -> colors.Color:
    return colors.HexColor(value if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else fallback)


def _contrast_ratio(left: str, right: str) -> float:
    def luminance(value: str) -> float:
        channels = []
        for index in (1, 3, 5):
            channel = int(value[index:index + 2], 16) / 255
            channels.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    high, low = sorted((luminance(left), luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _legible_foreground(background: str, *candidates: Any) -> str:
    valid = [value.upper() for value in candidates if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value)]
    valid.extend(["#FFFFFF", "#111111"])
    return max(dict.fromkeys(valid), key=lambda value: _contrast_ratio(background, value))


def _preferred_foreground(background: str, preferred: Any, *fallbacks: Any) -> str:
    """Keep an observed foreground when it is readable; otherwise repair contrast."""
    if isinstance(preferred, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", preferred):
        preferred = preferred.upper()
        if _contrast_ratio(background, preferred) >= 3:
            return preferred
    return _legible_foreground(background, *fallbacks)


def _brand_dark(system: dict[str, Any], palette: dict[str, str]) -> str:
    explicit = palette.get("brand_dark")
    if isinstance(explicit, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", explicit):
        return explicit.upper()
    surface = palette.get("surface")
    if (
        isinstance(surface, str)
        and re.fullmatch(r"#[0-9A-Fa-f]{6}", surface)
        and _contrast_ratio(palette["background"], surface) >= 1.15
        and _contrast_ratio(surface, palette["text"]) >= 3
    ):
        return surface.upper()
    excluded = {palette.get(role, "").upper() for role in ("background", "surface", "text", "accent", "accent_secondary")}
    candidates: list[tuple[int, str]] = []
    for item in (system.get("evidence") or {}).get("colors", []):
        value = str(item.get("value", "")).upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", value) or value in excluded:
            continue
        channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        saturation = (max(channels) - min(channels)) / max(max(channels), 0.001)
        if max(channels) <= 0.24 and saturation >= 0.45 and _contrast_ratio(palette["background"], value) >= 3:
            candidates.append((int(item.get("occurrences", 0)), value))
    return max(candidates, default=(0, palette["text"]), key=lambda item: item[0])[1]


def _eyebrow_color(palette: dict[str, str]) -> str:
    return next(
        (
            candidate for candidate in (palette["accent"], palette.get("accent_secondary"), palette["text"])
            if isinstance(candidate, str) and _contrast_ratio(palette["background"], candidate) >= 3
        ),
        palette["text"],
    )


def _font_roles(system: dict[str, Any]) -> tuple[str, str, str]:
    typography = system["tokens"]["typography"]
    display = typography["display_family"].lower()
    serif_markers = ("serif", "times", "georgia", "garamond", "baskerville")
    display_is_serif = typography.get("display_fallback") == "serif" or any(marker in display for marker in serif_markers)
    display_font = "Times-Bold" if display_is_serif else "Helvetica-Bold"
    body = typography["body_family"].lower()
    body_is_serif = typography.get("body_fallback") == "serif" or any(marker in body for marker in serif_markers)
    body_font = "Times-Roman" if body_is_serif else "Helvetica"
    return display_font, body_font, "Helvetica-Bold"


def _print_safe_text(value: str) -> tuple[str, int]:
    matches = EMOJI_PATTERN.findall(value)
    cleaned = EMOJI_PATTERN.sub("", value)
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned, sum(len(match) for match in matches)


def _inline_markdown(value: str) -> str:
    safe = html.escape(value)
    safe = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", safe)
    safe = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", safe)
    safe = re.sub(r"`([^`\n]+)`", r"<font name=\"Courier\">\1</font>", safe)
    safe = re.sub(r"\[([^]\n]+)\]\((https?://[^)\s]+)\)", r'<link href="\2">\1</link>', safe)
    return safe


def _number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


def _alignment(value: str | None) -> int:
    return {"center": TA_CENTER, "right": TA_RIGHT, "end": TA_RIGHT}.get(str(value).lower(), TA_LEFT)


def _cover_alignment(family: str, observed: str | None) -> int:
    """Keep each cover on one grid instead of mixing sampled alignments."""
    return TA_LEFT if family in {"product_showcase", "asymmetric_feature_grid"} else _alignment(observed)


def _paired_grid_rows(cells: list[Any]) -> list[list[Any]]:
    """Place paired cells around an explicit gutter so both outer edges align."""
    rows: list[list[Any]] = []
    for index in range(0, len(cells), 2):
        pair = cells[index:index + 2]
        pair.extend([""] * (2 - len(pair)))
        rows.append([pair[0], "", pair[1]])
    return rows


def _bounded_radius(radius: float, width: float, height: float) -> float:
    """Keep rounded rectangles inside ReportLab's stable geometric range."""
    return max(0.0, min(radius, max(0.0, width / 2 - 0.5), max(0.0, height / 2 - 0.5)))


class BrandedBox(Flowable):
    """A splittable-safe branded card, callout, or action built from one paragraph."""

    def __init__(
        self,
        text: str,
        style: ParagraphStyle,
        *,
        background: colors.Color,
        border: colors.Color,
        border_width: float,
        radius: float,
        padding: float,
        accent: colors.Color | None = None,
        accent_position: str = "left",
        min_height: float = 0,
        shadow: bool = False,
    ) -> None:
        super().__init__()
        self.paragraph = Paragraph(text, style)
        self.background = background
        self.border = border
        self.border_width = border_width
        self.radius = radius
        self.padding = padding
        self.accent = accent
        self.accent_position = accent_position
        self.min_height = min_height
        self.shadow = shadow
        self._paragraph_size = (0.0, 0.0)

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        paragraph_width, paragraph_height = self.paragraph.wrap(max(1, available_width - self.padding * 2), available_height)
        self._paragraph_size = (paragraph_width, paragraph_height)
        self.width = available_width
        self.height = max(self.min_height, paragraph_height + self.padding * 2)
        return self.width, self.height

    def draw(self) -> None:
        canvas = self.canv
        radius = _bounded_radius(self.radius, self.width, self.height)
        if self.shadow:
            canvas.setFillColor(colors.Color(0, 0, 0, alpha=0.07))
            shadow_radius = _bounded_radius(radius, self.width - 2, self.height)
            canvas.roundRect(2, -2, self.width - 2, self.height, shadow_radius, stroke=0, fill=1)
        canvas.setFillColor(self.background)
        canvas.setStrokeColor(self.border)
        canvas.setLineWidth(self.border_width)
        canvas.roundRect(0, 0, self.width, self.height, radius, stroke=int(self.border_width > 0), fill=1)
        if self.accent is not None:
            clip = canvas.beginPath()
            clip.roundRect(0, 0, self.width, self.height, radius)
            canvas.saveState()
            canvas.clipPath(clip, stroke=0, fill=0)
            canvas.setFillColor(self.accent)
            if self.accent_position == "top":
                canvas.rect(0, self.height - 5, self.width, 5, stroke=0, fill=1)
            else:
                canvas.rect(0, 0, 5, self.height, stroke=0, fill=1)
            canvas.restoreState()
        paragraph_width, paragraph_height = self._paragraph_size
        self.paragraph.drawOn(canvas, self.padding, (self.height - paragraph_height) / 2)


def create_pdf(
    design_system_path: Path,
    content_path: Path,
    prompt: str,
    output_path: Path,
    planner: DocumentPlanner | None = None,
) -> dict[str, Any]:
    system = read_json(design_system_path)
    validate_design_system(system)
    content = content_path.read_text(encoding="utf-8")
    validate_document_request(content, prompt)
    print_content, removed_emoji_count = _print_safe_text(content)
    blocks = source_blocks(print_content)
    planner = planner or DeterministicPlanner()
    plan = planner.plan(content, prompt, source_blocks(content))
    validate_plan(plan, source_blocks(content))
    plan["title"], title_emoji_count = _print_safe_text(str(plan["title"]))
    plan["subtitle"], subtitle_emoji_count = _print_safe_text(str(plan["subtitle"]))
    removed_emoji_count += title_emoji_count + subtitle_emoji_count
    plan["composition"] = select_composition(system, plan, blocks, prompt)
    validate_composition(plan["composition"])
    plan["component_plan"] = select_component_plan(system, plan, blocks)
    validate_component_plan(plan["component_plan"])
    family = plan["composition"]["family"]
    component_plan = plan["component_plan"]
    component_library = component_plan["library"]["components"]
    marker_recipe = component_library["section-marker"]
    dual_marker = marker_recipe["style"] == "dual-tone"
    section_treatments = {item["section_id"]: item["treatment"] for item in component_plan["sections"]}

    page_size = A4 if plan["page_size"] == "A4" else LETTER
    if plan["orientation"] == "landscape":
        page_size = landscape(page_size)
    width, height = page_size
    palette = system["tokens"]["colors"]
    background = _reportlab_color(palette["background"])
    surface = _reportlab_color(palette["surface"])
    text = _reportlab_color(palette["text"])
    accent = _reportlab_color(marker_recipe.get("primary", palette["accent"]))
    accent_secondary = _reportlab_color(marker_recipe.get("secondary", palette["accent"]))
    brand_dark_hex = _brand_dark(system, palette)
    brand_dark = _reportlab_color(brand_dark_hex)
    display_font, body_font, label_font = _font_roles(system)
    base = float(system["tokens"]["spacing"]["base"])
    components = system.get("components") or {}
    typography = components.get("typography") or {}
    heading1_recipe = typography.get("h1") or {}
    heading2_recipe = typography.get("h2") or {}
    heading3_recipe = typography.get("h3") or {}
    card_recipe = components.get("cards") or {}
    primary_button = (components.get("buttons") or {}).get("primary") or {}
    section_recipe = (components.get("sections") or {}).get("recipe") or {}

    h1_size = _number(heading1_recipe.get("font_size"), 40, 30, 46)
    h2_size = _number(heading2_recipe.get("font_size"), 28, 19, 32)
    h3_size = _number(heading3_recipe.get("font_size"), 13, 11, 16)
    card_radius = _number(card_recipe.get("radius"), system["tokens"].get("shape", {}).get("radius", 8), 0, 18)
    card_border_width = _number(card_recipe.get("border_width"), 0.75, 0, 2)
    card_background_hex = card_recipe.get("background") if isinstance(card_recipe.get("background"), str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", card_recipe["background"]) else palette["surface"]
    card_foreground_hex = _legible_foreground(card_background_hex, card_recipe.get("foreground"), palette["text"])
    card_background = _reportlab_color(card_background_hex)
    card_border = _recipe_color(card_recipe.get("border_color"), palette["surface"])
    button_background_hex = primary_button.get("background") if isinstance(primary_button.get("background"), str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", primary_button["background"]) else palette["accent"]
    button_foreground_hex = _preferred_foreground(
        button_background_hex, primary_button.get("foreground"), palette["text"]
    )
    button_height = _number(primary_button.get("typical_height"), 38, 32, 52)
    button_radius = _number(primary_button.get("radius"), 16, 0, button_height / 2)
    button_font_size = _number(primary_button.get("font_size"), 10, 9, 16)
    button_border_hex = (
        primary_button.get("border_color")
        if isinstance(primary_button.get("border_color"), str)
        and re.fullmatch(r"#[0-9A-Fa-f]{6}", primary_button["border_color"])
        else button_background_hex
    )
    button_border = _reportlab_color(button_border_hex)
    button_border_width = _number(primary_button.get("border_width"), 0, 0, 2)
    # A sampled navigation treatment can be visually transparent on the page
    # (for example, white text on a white header). Keep its geometry, but give
    # document actions a visible brand-colored surface instead of orphaned text.
    if _contrast_ratio(button_background_hex, palette["background"]) <= 1.05 and button_border_width < 0.5:
        button_background_hex = palette["accent"]
        button_foreground_hex = _preferred_foreground(button_background_hex, primary_button.get("foreground"), palette["text"])
        button_border_hex = button_background_hex
        button_border = _reportlab_color(button_border_hex)
    eyebrow_hex = _eyebrow_color(palette)
    cover_alignment = _cover_alignment(family, heading1_recipe.get("text_align"))
    section_alignment = TA_LEFT if family == "editorial_narrative" else _alignment(heading2_recipe.get("text_align"))
    card_padding = _number(base * 3, 12, 9, 20)
    section_padding = _number(base * 4, 16, 12, 26)
    section_background = _recipe_color(section_recipe.get("background"), palette["surface"])
    logo_asset = select_logo_asset(
        system, allow_svg=False, max_width=1.5 * inch, max_height=0.65 * inch,
    )
    cover_asset_id = component_plan["cover"].get("asset_id")
    hero_asset = next((asset for asset in system["assets"] if asset.get("id") == cover_asset_id), None)

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output_path), pagesize=page_size, leftMargin=0.72 * inch, rightMargin=0.72 * inch,
        topMargin=0.78 * inch, bottomMargin=0.7 * inch,
        title=plan["title"], author=f"Kolo Design Studio · {system['name']}",
    )
    # BaseDocTemplate's frame reserves six points on each side. Paragraphs already
    # honor that inset, so explicit table widths must use the same content grid.
    content_width = document.width - 12
    button_width = _number(primary_button.get("typical_width"), 170, 120, min(250, content_width))
    styles = {
        "eyebrow": ParagraphStyle("eyebrow", fontName=label_font, fontSize=8.5, leading=11, textColor=_reportlab_color(eyebrow_hex), spaceAfter=base * 2, tracking=1.1),
        "cover-eyebrow": ParagraphStyle("cover-eyebrow", fontName=label_font, fontSize=8.5, leading=11, textColor=_reportlab_color(eyebrow_hex), spaceAfter=base * 2, tracking=1.1, alignment=cover_alignment),
        "cover": ParagraphStyle("cover", fontName=display_font, fontSize=h1_size, leading=h1_size * 1.08, textColor=text, spaceAfter=base * 3, alignment=cover_alignment),
        "subtitle": ParagraphStyle("subtitle", fontName=body_font, fontSize=13, leading=19, textColor=text, spaceAfter=base * 3, alignment=cover_alignment),
        "h1": ParagraphStyle("h1", fontName=display_font, fontSize=min(30, h1_size * 0.7), leading=min(34, h1_size * 0.78), textColor=text, spaceBefore=base * 3, spaceAfter=base * 2, alignment=_alignment(heading1_recipe.get("text_align"))),
        "h2": ParagraphStyle("h2", fontName=display_font, fontSize=h2_size, leading=h2_size * 1.12, textColor=text, spaceBefore=base * 2.5, spaceAfter=base * 1.5, alignment=section_alignment),
        "h3": ParagraphStyle("h3", fontName=label_font, fontSize=h3_size, leading=h3_size * 1.3, textColor=accent, spaceBefore=base * 2, spaceAfter=base, alignment=_alignment(heading3_recipe.get("text_align"))),
        "body": ParagraphStyle("body", fontName=body_font, fontSize=10.5, leading=16, textColor=text, spaceAfter=base * 1.6, alignment=TA_LEFT),
        "lead": ParagraphStyle("lead", fontName=body_font, fontSize=13.5, leading=20, textColor=text, spaceAfter=base * 2.2, alignment=TA_LEFT),
        "bullet": ParagraphStyle("bullet", parent=None, fontName=body_font, fontSize=10.5, leading=16, textColor=text, leftIndent=16, firstLineIndent=-10, bulletIndent=0, spaceAfter=base),
        "card": ParagraphStyle("card", fontName=body_font, fontSize=_number(card_recipe.get("font_size"), 10.5, 9, 12), leading=15, textColor=_reportlab_color(card_foreground_hex), spaceAfter=0),
        "module": ParagraphStyle("module", fontName=body_font, fontSize=10, leading=14, textColor=text, spaceAfter=0),
        "number": ParagraphStyle("number", fontName=display_font, fontSize=18, leading=20, textColor=accent, alignment=TA_CENTER, spaceAfter=0),
        "callout": ParagraphStyle("callout", fontName=body_font, fontSize=12, leading=18, textColor=text, spaceAfter=0),
        "action": ParagraphStyle("action", fontName=label_font, fontSize=button_font_size, leading=max(14, button_font_size * 1.15), textColor=_reportlab_color(button_foreground_hex), alignment=TA_CENTER, spaceAfter=0),
    }

    def fitted_button_width(markup: str, maximum: float) -> float:
        plain_label = html.unescape(re.sub(r"<[^>]+>", "", markup))
        label_width = pdfmetrics.stringWidth(plain_label, label_font, button_font_size)
        return min(max(button_width, label_width + 28), maximum)

    def brand_rule(rule_width: float, thickness: float = 4, h_align: str = "LEFT") -> Table:
        if dual_marker:
            primary_width = rule_width * float(marker_recipe.get("primary_share", 0.72))
            rule = Table([["", ""]], colWidths=[primary_width, rule_width - primary_width], rowHeights=[thickness], hAlign=h_align)
            rule.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), accent),
                ("BACKGROUND", (1, 0), (1, 0), accent_secondary),
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            return rule
        rule = Table([[""]], colWidths=[rule_width], rowHeights=[thickness], hAlign=h_align)
        rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        return rule

    def draw_cover_image(canvas: Any, image_path: str, x: float, y: float, box_width: float, box_height: float) -> None:
        source = ImageReader(image_path)
        image_width, image_height = source.getSize()
        scale = max(box_width / max(1, image_width), box_height / max(1, image_height))
        draw_width, draw_height = image_width * scale, image_height * scale
        clip = canvas.beginPath()
        clip.rect(x, y, box_width, box_height)
        canvas.saveState()
        canvas.clipPath(clip, stroke=0, fill=0)
        canvas.drawImage(
            source, x + (box_width - draw_width) / 2, y + (box_height - draw_height) / 2,
            width=draw_width, height=draw_height, preserveAspectRatio=True, mask="auto",
        )
        canvas.restoreState()

    def decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(background)
        canvas.rect(0, 0, width, height, stroke=0, fill=1)
        canvas.setFillColor(accent)
        if dual_marker:
            share = float(marker_recipe.get("primary_share", 0.72))
            canvas.rect(0, height - 7, width * share, 7, stroke=0, fill=1)
            canvas.setFillColor(accent_secondary)
            canvas.rect(width * share, height - 7, width * (1 - share), 7, stroke=0, fill=1)
        elif family == "editorial_narrative":
            canvas.rect(0, height - 7, width, 7, stroke=0, fill=1)
        elif family == "asymmetric_feature_grid":
            canvas.rect(0, height - 12, width * 0.64, 12, stroke=0, fill=1)
            canvas.setFillColor(accent_secondary)
            canvas.rect(width * 0.64, height - 12, width * 0.36, 12, stroke=0, fill=1)
        elif family == "numbered_process":
            canvas.rect(0, height - 7, width, 7, stroke=0, fill=1)
        elif family == "product_showcase":
            canvas.rect(0, height - 7, width, 7, stroke=0, fill=1)
        else:
            canvas.rect(0, height - 7, width, 7, stroke=0, fill=1)
        if doc.page == 1:
            if family == "editorial_narrative":
                pass
            elif family == "asymmetric_feature_grid":
                canvas.setFillColor(brand_dark)
                canvas.rect(width * 0.57, 0, width * 0.43, height * 0.72, stroke=0, fill=1)
                if hero_asset and Path(hero_asset["path"]).exists():
                    draw_cover_image(canvas, hero_asset["path"], width * 0.57, 0, width * 0.43, height * 0.72)
                else:
                    canvas.setFillColor(accent_secondary)
                    canvas.rect(width * 0.77, 0, width * 0.23, height * 0.31, stroke=0, fill=1)
            elif family == "numbered_process":
                canvas.setFillColor(surface)
                canvas.setFont(display_font, min(170, width * 0.28))
                canvas.drawRightString(width - 0.45 * inch, 0.8 * inch, "01")
            elif component_plan["cover"]["placement"] == "bottom-band" and hero_asset and Path(hero_asset["path"]).exists():
                draw_cover_image(canvas, hero_asset["path"], 0, 0, width, height * 0.46)
            elif component_plan["cover"]["placement"] == "side-panel" and hero_asset and Path(hero_asset["path"]).exists():
                draw_cover_image(canvas, hero_asset["path"], width * 0.57, 0, width * 0.43, height * 0.72)
            elif component_library["numbered-feature-grid"].get("cell_style") == "open":
                canvas.setFillColor(text)
                canvas.rect(width - 1.45 * inch, 0.72 * inch, 1.05 * inch, 5, stroke=0, fill=1)
                canvas.setFillColor(surface)
                canvas.rect(width - 1.02 * inch, 0.52 * inch, 0.62 * inch, 5, stroke=0, fill=1)
            else:
                canvas.setFillColor(accent)
                canvas.circle(width - 0.82 * inch, 0.82 * inch, 0.48 * inch, stroke=0, fill=1)
                canvas.setFillColor(accent_secondary)
                canvas.circle(width - 0.42 * inch, 0.43 * inch, 0.2 * inch, stroke=0, fill=1)
        if doc.page > 1:
            canvas.setFont(label_font, 7.5)
            canvas.setFillColor(text)
            canvas.drawString(0.72 * inch, 0.35 * inch, system["name"].upper())
            canvas.drawRightString(width - 0.72 * inch, 0.35 * inch, f"{doc.page - 1:02d}")
        canvas.restoreState()

    story: list[Any] = [Spacer(1, height * (0.08 if family in {"modular_announcement", "product_showcase"} else 0.13))]
    story.append(Paragraph(html.escape(f"{system['name']} DESIGN LANGUAGE".upper()), styles["cover-eyebrow"]))
    cover_title = Paragraph(_inline_markdown(plan["title"]), styles["cover"])
    cover_source_id: str | None = None
    cover_subtitle_text = plan["subtitle"]
    if family == "product_showcase":
        opening_callout = next((block for block in blocks[:3] if block["kind"] == "callout"), None)
        if opening_callout:
            cover_subtitle_text = opening_callout["text"]
            cover_source_id = opening_callout["id"]
    cover_subtitle = Paragraph(_inline_markdown(cover_subtitle_text), styles["subtitle"])
    if family == "product_showcase":
        product_cover = ParagraphStyle(
            "product-cover", parent=styles["cover"], fontSize=min(46, max(40, h1_size)),
            leading=min(50, max(44, h1_size * 1.05)), alignment=TA_LEFT,
        )
        story.extend([
            Paragraph(_inline_markdown(plan["title"]), product_cover),
            brand_rule(content_width * 0.24, 5), Spacer(1, base * 3),
            cover_subtitle,
        ])
    elif family == "modular_announcement":
        story.extend([
            cover_title,
            HRFlowable(width="22%", thickness=4, color=accent, spaceBefore=0, spaceAfter=base * 3, hAlign="LEFT"),
            cover_subtitle,
        ])
    elif family == "asymmetric_feature_grid":
        story.extend([
            Table([[cover_title, ""]], colWidths=[document.width * 0.54, document.width * 0.46], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")])),
            Spacer(1, base * 2),
            Table([[cover_subtitle, ""]], colWidths=[document.width * 0.54, document.width * 0.46], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])),
        ])
    else:
        story.extend([
            cover_title,
            HRFlowable(
                width="18%", thickness=5, color=accent, spaceBefore=0, spaceAfter=base * 3,
                hAlign="CENTER" if cover_alignment == TA_CENTER else "RIGHT" if cover_alignment == TA_RIGHT else "LEFT",
            ),
            cover_subtitle,
        ])
    if logo_asset and Path(logo_asset["path"]).exists():
        pixel_width = float(logo_asset["pixel_width"])
        pixel_height = float(logo_asset["pixel_height"])
        scale = min((1.5 * inch) / pixel_width, (0.65 * inch) / pixel_height)
        image = Image(logo_asset["path"], width=pixel_width * scale, height=pixel_height * scale)
        image.hAlign = "CENTER" if cover_alignment == TA_CENTER else "RIGHT" if cover_alignment == TA_RIGHT else "LEFT"
        story.extend([Spacer(1, base * 5), image])
    else:
        wordmark = Paragraph(html.escape(_print_safe_text(system["name"])[0]), ParagraphStyle(
            "cover-wordmark", fontName=display_font, fontSize=20, leading=24,
            textColor=text, alignment=cover_alignment,
        ))
        story.extend([Spacer(1, base * 5), wordmark])
    story.append(PageBreak())

    by_id = {block["id"]: block for block in blocks}
    component_usage = {"headings": 0, "cards": 0, "callouts": 0, "actions": 0, "standard_blocks": 0, "brand_rules": 1, "feature_bands": 0}
    skipped_cover_heading = False

    def add_standard(block: dict[str, str], *, lead: bool = False) -> None:
        nonlocal skipped_cover_heading
        kind, value = block["kind"], block["text"]
        if block["id"] == cover_source_id:
            return
        safe = _inline_markdown(value)
        if kind == "heading1" and not skipped_cover_heading and value.strip().lower() == plan["title"].strip().lower():
            skipped_cover_heading = True
            return
        if kind == "heading1":
            story.extend([CondPageBreak(120), Paragraph(safe, styles["h1"])])
            component_usage["headings"] += 1
        elif kind == "heading2":
            if family == "editorial_narrative":
                story.extend([
                    CondPageBreak(145),
                    HRFlowable(width="100%", thickness=0.8, color=accent, spaceBefore=base * 3, spaceAfter=base * 1.4, hAlign="LEFT"),
                    Paragraph(safe, styles["h2"]),
                ])
            elif family in {"modular_announcement", "product_showcase"}:
                story.extend([
                    CondPageBreak(145),
                    Spacer(1, base * 2.5), brand_rule(content_width * 0.18, 4), Spacer(1, base * 1.5),
                    Paragraph(safe, styles["h2"]),
                ])
                component_usage["brand_rules"] += 1
            else:
                story.extend([
                    CondPageBreak(130),
                    HRFlowable(width="14%", thickness=4, color=accent, spaceBefore=base * 2, spaceAfter=base * 1.4, hAlign="LEFT"),
                    Paragraph(safe, styles["h2"]),
                ])
            component_usage["headings"] += 1
        elif kind == "heading3":
            story.append(Paragraph(safe.upper(), styles["h3"]))
            component_usage["headings"] += 1
        elif kind == "bullet":
            story.append(Paragraph(f"<bullet>•</bullet>{safe}", styles["bullet"]))
            component_usage["standard_blocks"] += 1
        elif kind == "callout":
            callout_radius = 0 if family == "numbered_process" else card_radius
            callout_background = background if family == "modular_announcement" else section_background
            callout_border_width = 0.8 if family == "modular_announcement" else 0
            story.extend([
                BrandedBox(safe, styles["callout"], background=callout_background, border=surface, border_width=callout_border_width,
                           radius=callout_radius, padding=section_padding, accent=accent),
                Spacer(1, base * 2),
            ])
            component_usage["callouts"] += 1
        elif kind == "action":
            action_width = fitted_button_width(safe, min(250, content_width))
            action = BrandedBox(
                safe, styles["action"], background=_reportlab_color(button_background_hex),
                border=button_border, border_width=button_border_width,
                radius=button_radius, padding=10, min_height=button_height,
                shadow=bool(primary_button.get("shadow") and primary_button.get("shadow") != "none"),
            )
            story.extend([Table([[action]], colWidths=[action_width], style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)])), Spacer(1, base * 2)])
            component_usage["actions"] += 1
        else:
            story.append(Paragraph(safe, styles["lead"] if lead else styles["body"]))
            component_usage["standard_blocks"] += 1

    def add_card_grid(card_blocks: list[dict[str, str]]) -> None:
        if not card_blocks:
            return
        gap = max(8, base * 2)
        columns = 1 if content_width < 430 else 2
        cell_width = (content_width - gap * (columns - 1)) / columns
        cell_style = component_library["numbered-feature-grid"].get("cell_style", "card")
        if cell_style == "open":
            open_style = ParagraphStyle("open-card", parent=styles["card"], textColor=text, leading=15)
            cells = [
                Table([[Paragraph(_inline_markdown(block["text"]), open_style)]], colWidths=[cell_width], style=TableStyle([
                    ("LINEABOVE", (0, 0), (-1, 0), 0.8, surface),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), card_padding),
                    ("TOPPADDING", (0, 0), (-1, -1), card_padding), ("BOTTOMPADDING", (0, 0), (-1, -1), card_padding),
                ]))
                for block in card_blocks
            ]
        else:
            cells = [
                BrandedBox(
                    _inline_markdown(block["text"]), styles["card"], background=card_background, border=card_border,
                    border_width=card_border_width, radius=card_radius, padding=card_padding, min_height=58,
                    shadow=bool(card_recipe.get("shadow") and card_recipe.get("shadow") != "none"),
                )
                for block in card_blocks
            ]
        rows: list[list[Any]] = []
        for index in range(0, len(cells), columns):
            row = cells[index:index + columns]
            row.extend([""] * (columns - len(row)))
            rows.append(row if columns == 1 else _paired_grid_rows(row)[0])
        column_widths = [cell_width] if columns == 1 else [cell_width, gap, cell_width]
        grid = Table(rows, colWidths=column_widths, hAlign="LEFT", spaceBefore=base, spaceAfter=base * 2)
        grid.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), gap / 2), ("BOTTOMPADDING", (0, 0), (-1, -1), gap / 2),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(grid)
        component_usage["cards"] += len(card_blocks)

    def add_asymmetric_grid(card_blocks: list[dict[str, str]]) -> None:
        if not card_blocks:
            return
        gap = max(8, base * 2)
        feature_recipe = component_library["feature-band"]
        feature_background_hex = feature_recipe["background"]
        asym_lead_style = ParagraphStyle(
            "asym-lead", parent=styles["lead"],
            textColor=_reportlab_color(feature_recipe["foreground"]),
        )
        first = BrandedBox(
            _inline_markdown(card_blocks[0]["text"]), asym_lead_style, background=_reportlab_color(feature_background_hex),
            border=_reportlab_color(feature_background_hex), border_width=0, radius=min(8, card_radius), padding=card_padding,
            accent=_reportlab_color(feature_recipe["accent"]), accent_position="top", min_height=76, shadow=False,
        )
        story.extend([Table([[first]], colWidths=[content_width], style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)])), Spacer(1, gap)])
        if len(card_blocks) > 1:
            add_card_grid(card_blocks[1:])
        component_usage["cards"] += 1

    def add_modular_grid(card_blocks: list[dict[str, str]]) -> None:
        if not card_blocks:
            return
        gap = max(7, base * 1.5)
        columns = 1 if content_width < 430 else 2
        cell_width = (content_width - gap * (columns - 1)) / columns
        cells: list[Any] = []
        cell_style = component_library["numbered-feature-grid"].get("cell_style", "card")
        for index, block in enumerate(card_blocks):
            module_style = ParagraphStyle(f"module-{index}", parent=styles["module"], textColor=text)
            if cell_style == "open":
                cells.append(Table([[Paragraph(_inline_markdown(block["text"]), module_style)]], colWidths=[cell_width], style=TableStyle([
                    ("LINEABOVE", (0, 0), (-1, 0), 1.2, text),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), card_padding),
                    ("TOPPADDING", (0, 0), (-1, -1), card_padding), ("BOTTOMPADDING", (0, 0), (-1, -1), card_padding),
                ])))
            else:
                cells.append(BrandedBox(
                    _inline_markdown(block["text"]), module_style, background=background, border=surface,
                    border_width=0.8, radius=min(8, card_radius), padding=card_padding,
                    accent=accent if index % 2 == 0 else accent_secondary, min_height=72, shadow=False,
                ))
        rows: list[list[Any]] = []
        for index in range(0, len(cells), columns):
            row = cells[index:index + columns]
            row.extend([""] * (columns - len(row)))
            rows.append(row if columns == 1 else _paired_grid_rows(row)[0])
        column_widths = [cell_width] if columns == 1 else [cell_width, gap, cell_width]
        grid = Table(rows, colWidths=column_widths, hAlign="LEFT", spaceAfter=base * 2)
        grid.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), gap / 2), ("BOTTOMPADDING", (0, 0), (-1, -1), gap / 2),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(grid)
        component_usage["cards"] += len(card_blocks)

    def add_product_grid(card_blocks: list[dict[str, str]], treatment: str) -> None:
        if not card_blocks:
            return
        gap = max(10, base * 2.5)
        cell_width = (content_width - gap) / 2
        remaining = card_blocks
        if treatment == "feature-band":
            feature_recipe = component_library["feature-band"]
            feature_style = ParagraphStyle(
                "brand-feature", parent=styles["module"], fontSize=11.5, leading=16,
                textColor=_reportlab_color(feature_recipe["foreground"]),
            )
            feature = BrandedBox(
                _inline_markdown(card_blocks[0]["text"]), feature_style,
                background=_reportlab_color(feature_recipe["background"]), border=_reportlab_color(feature_recipe["background"]),
                border_width=0, radius=min(10, card_radius), padding=max(15, card_padding),
                accent=_reportlab_color(feature_recipe["accent"]), accent_position="top", min_height=76, shadow=False,
            )
            story.extend([Table([[feature]], colWidths=[content_width], style=TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ])), Spacer(1, gap)])
            remaining = card_blocks[1:]
            component_usage["feature_bands"] += 1
        cells = [
            BrandedBox(
                _inline_markdown(block["text"]), styles["module"], background=card_background,
                border=card_background, border_width=0, radius=min(10, card_radius),
                padding=max(12, card_padding), min_height=74, shadow=False,
            )
            for block in remaining
        ]
        if cells:
            rows = _paired_grid_rows(cells)
            grid = Table(rows, colWidths=[cell_width, gap, cell_width], hAlign="LEFT", spaceAfter=base * 3)
            grid.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), gap / 2), ("BOTTOMPADDING", (0, 0), (-1, -1), gap / 2),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(grid)
        component_usage["cards"] += len(card_blocks)

    step_number = 0

    def add_numbered_steps(step_blocks: list[dict[str, str]]) -> None:
        nonlocal step_number
        for block in step_blocks:
            step_number += 1
            number = Paragraph(f"{step_number:02d}", styles["number"])
            step = BrandedBox(
                _inline_markdown(block["text"]), styles["module"], background=section_background,
                border=section_background, border_width=0, radius=0, padding=min(card_padding, 10),
                min_height=44,
            )
            row = Table([[number, step]], colWidths=[0.55 * inch, content_width - 0.55 * inch], style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, -1), base * 2),
                ("RIGHTPADDING", (1, 0), (1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.extend([row, Spacer(1, max(3, base))])
        component_usage["cards"] += len(step_blocks)

    def add_closing_section(section_blocks: list[dict[str, str]]) -> None:
        """Resolve a short final promise as an open, spacious closing signature."""
        close_h = ParagraphStyle(
            "close-h", parent=styles["h2"], fontSize=min(25, h2_size), leading=min(29, h2_size * 1.12),
            textColor=text, spaceBefore=0, spaceAfter=0, alignment=TA_LEFT,
        )
        close_body = ParagraphStyle("close-body", parent=styles["body"], fontSize=10.5, leading=16, textColor=text, spaceAfter=0)
        heading_flowable: Any = ""
        body_values: list[str] = []
        action_flowable: Any = ""
        close_action_width = min(max(160, content_width * 0.34), content_width * 0.42)
        for block in section_blocks:
            safe = _inline_markdown(block["text"])
            if block["kind"].startswith("heading"):
                heading_flowable = Paragraph(safe, close_h)
                component_usage["headings"] += 1
            elif block["kind"] == "action":
                close_action_label = ParagraphStyle(
                    "close-action-label", parent=styles["eyebrow"], fontSize=7.5, leading=10,
                    textColor=_reportlab_color(eyebrow_hex), alignment=TA_RIGHT, spaceAfter=0,
                )
                close_action_style = ParagraphStyle(
                    "close-action", parent=styles["action"], fontSize=12.5,
                    leading=15, textColor=text, alignment=TA_RIGHT,
                )
                action_flowable = [
                    Paragraph("CONTINUE", close_action_label),
                    Spacer(1, max(3, base * 0.7)),
                    Paragraph(safe, close_action_style),
                ]
                component_usage["actions"] += 1
            else:
                body_values.append(safe)
                component_usage["standard_blocks"] += 1
        close_gap = max(12, base * 3)
        body_width = content_width - close_action_width - close_gap
        body_and_action = Table(
            [[Paragraph("<br/><br/>".join(body_values), close_body), "", action_flowable]],
            colWidths=[body_width, close_gap, close_action_width],
            style=TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]),
        )
        story.extend([
            CondPageBreak(130),
            HRFlowable(width="100%", thickness=0.8, color=surface, spaceBefore=base * 2.5, spaceAfter=base * 1.2, hAlign="LEFT"),
            brand_rule(content_width * 0.16, 4), Spacer(1, base * 2),
            heading_flowable,
            Spacer(1, base * 2),
            body_and_action,
            Spacer(1, base * 2),
        ])
        component_usage["brand_rules"] += 1

    sections = plan["layout"]["sections"]
    for section_index, section in enumerate(sections):
        if family == "editorial_narrative" and section_index == 3:
            story.append(PageBreak())
        if family == "product_showcase" and section_index == 3:
            story.append(PageBreak())
        section_blocks = [by_id[block_id] for block_id in section["block_ids"]]
        if (
            section_index == len(sections) - 1
            and len(section_blocks) <= 4
            and section_blocks[0]["kind"] in {"heading2", "heading3"}
            and any(block["kind"] == "action" for block in section_blocks)
        ):
            add_closing_section(section_blocks)
            continue
        bullet_blocks = [block for block in section_blocks if block["kind"] == "bullet"]
        first_paragraph = True
        pending_cards: list[dict[str, str]] = []

        def flush_cards() -> None:
            nonlocal pending_cards
            if not pending_cards:
                return
            if family == "numbered_process":
                add_numbered_steps(pending_cards)
            elif family == "modular_announcement":
                add_modular_grid(pending_cards)
            elif family == "asymmetric_feature_grid":
                add_asymmetric_grid(pending_cards)
            elif family == "product_showcase":
                add_product_grid(pending_cards, section_treatments.get(section["id"], "standard"))
            elif section["variant"] == "card_grid":
                add_card_grid(pending_cards)
            else:
                for pending in pending_cards:
                    add_standard(pending)
            pending_cards = []

        for block in section_blocks:
            if block["kind"] == "bullet" and (len(bullet_blocks) >= 2 or section["variant"] == "card_grid"):
                pending_cards.append(block)
                continue
            flush_cards()
            is_lead = family == "editorial_narrative" and block["kind"] == "paragraph" and first_paragraph
            add_standard(block, lead=is_lead)
            if block["kind"] == "paragraph":
                first_paragraph = False
        flush_cards()

    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    payload = output_path.read_bytes()
    reader = PdfReader(str(output_path))
    extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
    source_words = set(re.findall(r"[A-Za-z0-9]{4,}", content.lower()))
    output_words = set(re.findall(r"[A-Za-z0-9]{4,}", extracted.lower()))
    coverage = len(source_words & output_words) / max(1, len(source_words))
    if coverage < 0.75:
        raise RuntimeError(f"PDF content coverage check failed: {coverage:.1%}")
    unresolved_markdown = "**" in extracted or "__" in extracted
    tofu_found = sorted(marker for marker in TOFU_MARKERS if marker in extracted)
    if unresolved_markdown:
        raise RuntimeError("PDF contains unresolved inline Markdown markers")
    if tofu_found:
        raise RuntimeError(f"PDF contains unsupported replacement glyphs: {tofu_found}")

    layout_path = output_path.with_suffix(".layout.json")
    layout_artifact = {
        **plan,
        "planner": planner.version,
        "design_system": {"id": system["id"], "version": system["version"]},
        "source_blocks": blocks,
        "component_resolution": {
            "typography": {"h1_points": round(h1_size, 2), "h2_points": round(h2_size, 2), "h3_points": round(h3_size, 2)},
            "cards": {
                "background": card_recipe.get("background") or palette["surface"],
                "foreground": card_foreground_hex,
                "text_contrast": round(_contrast_ratio(card_background_hex, card_foreground_hex), 2),
                "border_color": card_recipe.get("border_color") or palette["surface"],
                "border_width": card_border_width,
                "radius": card_radius,
                "padding": card_padding,
            },
            "primary_action": {
                "background": button_background_hex,
                "foreground": button_foreground_hex,
                "text_contrast": round(_contrast_ratio(button_background_hex, button_foreground_hex), 2),
                "border_color": button_border_hex,
                "border_width": button_border_width,
                "radius": button_radius,
            },
            "section": {
                "background": section_recipe.get("background") or palette["background"],
                "padding": section_padding,
            },
        },
        "component_usage": component_usage,
    }
    write_json(layout_path, layout_artifact)

    preview_dir = confined(output_path.parent, output_path.stem + "-preview")
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    previews: list[str] = []
    renderer = shutil.which("pdftoppm")
    if renderer:
        prefix = preview_dir / "page"
        subprocess.run([renderer, "-png", "-r", "120", str(output_path), str(prefix)], check=True, timeout=120, capture_output=True)
        previews = [str(path) for path in sorted(preview_dir.glob("page-*.png"))]

    report = {
        "schema_version": 2,
        "status": "pass",
        "created_at": datetime.now(UTC).isoformat(),
        "design_system": {"id": system["id"], "version": system["version"], "path": str(design_system_path.resolve())},
        "planner": planner.version,
        "composition": plan["composition"],
        "layout_plan": str(layout_path),
        "component_usage": component_usage,
        "prompt": prompt,
        "pdf": {"path": str(output_path), "sha256": sha256_bytes(payload), "bytes": len(payload), "pages": len(reader.pages)},
        "checks": {
            "pdf_reopened": True,
            "content_coverage": round(coverage, 4),
            "inline_markdown_resolved": not unresolved_markdown,
            "tofu_glyphs_absent": not tofu_found,
            "previews_rendered": bool(previews),
        },
        "normalization": {"unsupported_emoji_removed": removed_emoji_count},
        "previews": previews,
    }
    quality_path = output_path.with_suffix(".quality.json")
    write_json(quality_path, report)
    return {
        "status": "succeeded", "pdf": str(output_path), "layout_plan": str(layout_path),
        "quality_report": str(quality_path), "previews": previews, "pages": len(reader.pages),
        "component_usage": component_usage, "composition": plan["composition"],
    }
