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
from reportlab.platypus import CondPageBreak, Flowable, HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
        if self.shadow:
            canvas.setFillColor(colors.Color(0, 0, 0, alpha=0.07))
            canvas.roundRect(2, -2, self.width - 2, self.height, self.radius, stroke=0, fill=1)
        canvas.setFillColor(self.background)
        canvas.setStrokeColor(self.border)
        canvas.setLineWidth(self.border_width)
        canvas.roundRect(0, 0, self.width, self.height, self.radius, stroke=int(self.border_width > 0), fill=1)
        if self.accent is not None:
            canvas.setFillColor(self.accent)
            canvas.roundRect(0, 0, 5, self.height, min(3, self.radius), stroke=0, fill=1)
        paragraph_width, paragraph_height = self._paragraph_size
        self.paragraph.drawOn(canvas, self.padding, self.height - self.padding - paragraph_height)


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
    family = plan["composition"]["family"]

    page_size = A4 if plan["page_size"] == "A4" else LETTER
    if plan["orientation"] == "landscape":
        page_size = landscape(page_size)
    width, height = page_size
    palette = system["tokens"]["colors"]
    background = _reportlab_color(palette["background"])
    surface = _reportlab_color(palette["surface"])
    text = _reportlab_color(palette["text"])
    accent = _reportlab_color(palette["accent"])
    accent_secondary = _reportlab_color(palette.get("accent_secondary", palette["accent"]))
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
    button_foreground_hex = _legible_foreground(button_background_hex, primary_button.get("foreground"), palette["text"])
    card_padding = _number(base * 3, 12, 9, 20)
    section_padding = _number(base * 4, 16, 12, 26)
    section_background = _recipe_color(section_recipe.get("background"), palette["surface"])

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output_path), pagesize=page_size, leftMargin=0.72 * inch, rightMargin=0.72 * inch,
        topMargin=0.78 * inch, bottomMargin=0.7 * inch,
        title=plan["title"], author=f"Kolo Design Studio · {system['name']}",
    )
    styles = {
        "eyebrow": ParagraphStyle("eyebrow", fontName=label_font, fontSize=8.5, leading=11, textColor=accent, spaceAfter=base * 2, tracking=1.1),
        "cover": ParagraphStyle("cover", fontName=display_font, fontSize=h1_size, leading=h1_size * 1.08, textColor=text, spaceAfter=base * 3, alignment=_alignment(heading1_recipe.get("text_align"))),
        "subtitle": ParagraphStyle("subtitle", fontName=body_font, fontSize=13, leading=19, textColor=text, spaceAfter=base * 3),
        "h1": ParagraphStyle("h1", fontName=display_font, fontSize=min(30, h1_size * 0.7), leading=min(34, h1_size * 0.78), textColor=text, spaceBefore=base * 3, spaceAfter=base * 2, alignment=_alignment(heading1_recipe.get("text_align"))),
        "h2": ParagraphStyle("h2", fontName=display_font, fontSize=h2_size, leading=h2_size * 1.12, textColor=text, spaceBefore=base * 2.5, spaceAfter=base * 1.5, alignment=_alignment(heading2_recipe.get("text_align"))),
        "h3": ParagraphStyle("h3", fontName=label_font, fontSize=h3_size, leading=h3_size * 1.3, textColor=accent, spaceBefore=base * 2, spaceAfter=base, alignment=_alignment(heading3_recipe.get("text_align"))),
        "body": ParagraphStyle("body", fontName=body_font, fontSize=10.5, leading=16, textColor=text, spaceAfter=base * 1.6, alignment=TA_LEFT),
        "lead": ParagraphStyle("lead", fontName=body_font, fontSize=13.5, leading=20, textColor=text, spaceAfter=base * 2.2, alignment=TA_LEFT),
        "bullet": ParagraphStyle("bullet", parent=None, fontName=body_font, fontSize=10.5, leading=16, textColor=text, leftIndent=16, firstLineIndent=-10, bulletIndent=0, spaceAfter=base),
        "card": ParagraphStyle("card", fontName=body_font, fontSize=_number(card_recipe.get("font_size"), 10.5, 9, 12), leading=15, textColor=_reportlab_color(card_foreground_hex), spaceAfter=0),
        "module": ParagraphStyle("module", fontName=body_font, fontSize=10, leading=14, textColor=text, spaceAfter=0),
        "number": ParagraphStyle("number", fontName=display_font, fontSize=18, leading=20, textColor=accent, alignment=TA_CENTER, spaceAfter=0),
        "callout": ParagraphStyle("callout", fontName=body_font, fontSize=12, leading=18, textColor=text, spaceAfter=0),
        "action": ParagraphStyle("action", fontName=label_font, fontSize=_number(primary_button.get("font_size"), 10, 9, 12), leading=14, textColor=_reportlab_color(button_foreground_hex), alignment=TA_CENTER, spaceAfter=0),
    }

    def decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(background)
        canvas.rect(0, 0, width, height, stroke=0, fill=1)
        canvas.setFillColor(accent)
        if family == "editorial_narrative":
            canvas.rect(0, 0, 9, height, stroke=0, fill=1)
        elif family == "asymmetric_feature_grid":
            canvas.rect(0, height - 12, width * 0.64, 12, stroke=0, fill=1)
            canvas.setFillColor(accent_secondary)
            canvas.rect(width * 0.64, height - 12, width * 0.36, 12, stroke=0, fill=1)
        elif family == "numbered_process":
            canvas.rect(0, height - 7, width, 7, stroke=0, fill=1)
            if doc.page > 1:
                canvas.setStrokeColor(surface)
                canvas.setLineWidth(2)
                canvas.line(0.95 * inch, 0.75 * inch, 0.95 * inch, height - 0.85 * inch)
        else:
            canvas.rect(0, height - 7, width, 7, stroke=0, fill=1)
        if doc.page == 1:
            if family == "editorial_narrative":
                canvas.setFillColor(surface)
                canvas.rect(width * 0.68, 0, width * 0.32, height, stroke=0, fill=1)
            elif family == "asymmetric_feature_grid":
                canvas.setFillColor(surface)
                canvas.rect(width * 0.57, 0, width * 0.43, height * 0.72, stroke=0, fill=1)
                canvas.setFillColor(accent_secondary)
                canvas.rect(width * 0.77, 0, width * 0.23, height * 0.31, stroke=0, fill=1)
            elif family == "numbered_process":
                canvas.setFillColor(surface)
                canvas.setFont(display_font, min(170, width * 0.28))
                canvas.drawRightString(width - 0.45 * inch, 0.8 * inch, "01")
            else:
                canvas.setFillColor(surface)
                canvas.rect(0, 0, width, height * 0.27, stroke=0, fill=1)
                canvas.setFillColor(accent_secondary)
                canvas.rect(width * 0.72, 0, width * 0.28, height * 0.27, stroke=0, fill=1)
        if doc.page > 1:
            canvas.setFont(label_font, 7.5)
            canvas.setFillColor(text)
            canvas.drawString(0.72 * inch, 0.35 * inch, system["name"].upper())
            canvas.drawRightString(width - 0.72 * inch, 0.35 * inch, f"{doc.page - 1:02d}")
        canvas.restoreState()

    story: list[Any] = [Spacer(1, height * (0.09 if family == "modular_announcement" else 0.13))]
    story.append(Paragraph(html.escape(system["name"].upper()), styles["eyebrow"]))
    cover_title = Paragraph(_inline_markdown(plan["title"]), styles["cover"])
    cover_subtitle = Paragraph(_inline_markdown(plan["subtitle"]), styles["subtitle"])
    if family == "modular_announcement":
        cover_foreground = _legible_foreground(palette["accent"], palette["text"])
        modular_cover = ParagraphStyle("modular-cover", parent=styles["cover"], textColor=_reportlab_color(cover_foreground), fontSize=min(44, h1_size), leading=min(48, h1_size * 1.08))
        modular_subtitle = ParagraphStyle("modular-subtitle", parent=styles["subtitle"], textColor=_reportlab_color(cover_foreground))
        panel = Table(
            [[Paragraph(_inline_markdown(plan["title"]), modular_cover)], [Paragraph(_inline_markdown(plan["subtitle"]), modular_subtitle)]],
            colWidths=[document.width * 0.82],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), accent),
                ("LEFTPADDING", (0, 0), (-1, -1), section_padding),
                ("RIGHTPADDING", (0, 0), (-1, -1), section_padding),
                ("TOPPADDING", (0, 0), (-1, 0), section_padding),
                ("BOTTOMPADDING", (0, -1), (-1, -1), section_padding),
            ]),
            hAlign="LEFT",
        )
        story.extend([panel, Spacer(1, base * 3)])
    elif family == "asymmetric_feature_grid":
        story.extend([
            Table([[cover_title, ""]], colWidths=[document.width * 0.68, document.width * 0.32], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")])),
            Spacer(1, base * 2),
            Table([["", cover_subtitle]], colWidths=[document.width * 0.23, document.width * 0.77], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])),
        ])
    else:
        story.extend([cover_title, Table([[""]], colWidths=[1.2 * inch], rowHeights=[5], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent)])), Spacer(1, base * 3), cover_subtitle])
    logo = next((asset for asset in system["assets"] if asset.get("kind") == "logo" and Path(asset.get("path", "")).suffix.lower() in {".png", ".jpg", ".jpeg"}), None)
    if logo and Path(logo["path"]).exists():
        image = Image(logo["path"], width=1.5 * inch, height=0.65 * inch, kind="proportional")
        image.hAlign = "LEFT"
        story.extend([Spacer(1, base * 5), image])
    story.append(PageBreak())

    by_id = {block["id"]: block for block in blocks}
    component_usage = {"headings": 0, "cards": 0, "callouts": 0, "actions": 0, "standard_blocks": 0}
    skipped_cover_heading = False

    def add_standard(block: dict[str, str], *, lead: bool = False) -> None:
        nonlocal skipped_cover_heading
        kind, value = block["kind"], block["text"]
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
            elif family == "modular_announcement":
                label_foreground = _legible_foreground(palette["accent"], palette["text"])
                label_style = ParagraphStyle("module-heading", parent=styles["h2"], textColor=_reportlab_color(label_foreground), spaceBefore=0, spaceAfter=0)
                story.extend([
                    CondPageBreak(145),
                    Table([[Paragraph(safe, label_style)]], colWidths=[document.width], style=TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), accent),
                        ("LEFTPADDING", (0, 0), (-1, -1), section_padding),
                        ("RIGHTPADDING", (0, 0), (-1, -1), section_padding),
                        ("TOPPADDING", (0, 0), (-1, -1), base * 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), base * 2),
                    ])),
                    Spacer(1, base * 1.5),
                ])
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
            story.extend([
                BrandedBox(safe, styles["callout"], background=section_background, border=surface, border_width=0,
                           radius=card_radius, padding=section_padding, accent=accent),
                Spacer(1, base * 2),
            ])
            component_usage["callouts"] += 1
        elif kind == "action":
            button_width = _number(primary_button.get("typical_width"), 170, 120, min(250, document.width))
            action = BrandedBox(
                safe, styles["action"], background=_reportlab_color(button_background_hex),
                border=_recipe_color(primary_button.get("border_color"), palette["accent"]),
                border_width=_number(primary_button.get("border_width"), 0, 0, 2),
                radius=_number(primary_button.get("radius"), 16, 0, 20), padding=10, min_height=36,
                shadow=bool(primary_button.get("shadow") and primary_button.get("shadow") != "none"),
            )
            story.extend([Table([[action]], colWidths=[button_width], style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)])), Spacer(1, base * 2)])
            component_usage["actions"] += 1
        else:
            story.append(Paragraph(safe, styles["lead"] if lead else styles["body"]))
            component_usage["standard_blocks"] += 1

    def add_card_grid(card_blocks: list[dict[str, str]]) -> None:
        if not card_blocks:
            return
        gap = max(8, base * 2)
        columns = 1 if document.width < 430 else 2
        cell_width = (document.width - gap * (columns - 1)) / columns
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
            rows.append(row)
        grid = Table(rows, colWidths=[cell_width] * columns, hAlign="LEFT", spaceBefore=base, spaceAfter=base * 2)
        grid.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), gap),
            ("TOPPADDING", (0, 0), (-1, -1), gap / 2), ("BOTTOMPADDING", (0, 0), (-1, -1), gap / 2),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(grid)
        component_usage["cards"] += len(card_blocks)

    def add_asymmetric_grid(card_blocks: list[dict[str, str]]) -> None:
        if not card_blocks:
            return
        gap = max(8, base * 2)
        first = BrandedBox(
            _inline_markdown(card_blocks[0]["text"]), styles["lead"], background=section_background,
            border=card_border, border_width=card_border_width, radius=card_radius, padding=section_padding,
            accent=accent_secondary, min_height=76, shadow=bool(card_recipe.get("shadow") and card_recipe.get("shadow") != "none"),
        )
        story.extend([Table([[first]], colWidths=[document.width], style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)])), Spacer(1, gap)])
        if len(card_blocks) > 1:
            add_card_grid(card_blocks[1:])
        component_usage["cards"] += 1

    def add_modular_grid(card_blocks: list[dict[str, str]]) -> None:
        if not card_blocks:
            return
        gap = max(7, base * 1.5)
        columns = 1 if document.width < 430 else (3 if len(card_blocks) >= 3 else 2)
        cell_width = (document.width - gap * (columns - 1)) / columns
        module_backgrounds = [surface, section_background, accent_secondary]
        cells: list[Any] = []
        for index, block in enumerate(card_blocks):
            chosen = module_backgrounds[index % len(module_backgrounds)]
            chosen_hex = palette["surface"] if index % len(module_backgrounds) < 2 else palette.get("accent_secondary", palette["accent"])
            module_style = ParagraphStyle(
                f"module-{index}", parent=styles["module"],
                textColor=_reportlab_color(_legible_foreground(chosen_hex, palette["text"])),
            )
            cells.append(BrandedBox(
                _inline_markdown(block["text"]), module_style, background=chosen, border=card_border,
                border_width=card_border_width, radius=card_radius, padding=card_padding, min_height=92,
                shadow=bool(card_recipe.get("shadow") and card_recipe.get("shadow") != "none"),
            ))
        rows: list[list[Any]] = []
        for index in range(0, len(cells), columns):
            row = cells[index:index + columns]
            row.extend([""] * (columns - len(row)))
            rows.append(row)
        grid = Table(rows, colWidths=[cell_width] * columns, hAlign="LEFT", spaceAfter=base * 2)
        grid.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), gap),
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
                border=card_border, border_width=card_border_width, radius=card_radius, padding=min(card_padding, 10),
                min_height=44,
            )
            row = Table([[number, step]], colWidths=[0.55 * inch, document.width - 0.55 * inch], style=TableStyle([
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
        """Keep a short final promise and action together as a compact visual ending."""
        panel_hex = palette["accent"] if family != "modular_announcement" else palette.get("accent_secondary", palette["accent"])
        panel_background = _reportlab_color(panel_hex)
        panel_foreground_hex = _legible_foreground(panel_hex, palette["text"])
        panel_foreground = _reportlab_color(panel_foreground_hex)
        close_h = ParagraphStyle("close-h", parent=styles["h2"], fontSize=min(22, h2_size), leading=min(25, h2_size * 1.1), textColor=panel_foreground, spaceBefore=0, spaceAfter=0)
        close_body = ParagraphStyle("close-body", parent=styles["body"], fontSize=9.5, leading=13, textColor=panel_foreground, spaceAfter=0)
        rows: list[list[Any]] = []
        for block in section_blocks:
            safe = _inline_markdown(block["text"])
            if block["kind"].startswith("heading"):
                rows.append([Paragraph(safe, close_h)])
                component_usage["headings"] += 1
            elif block["kind"] == "action":
                close_button_hex = palette["surface"]
                close_action_style = ParagraphStyle(
                    "close-action", parent=styles["action"],
                    textColor=_reportlab_color(_legible_foreground(close_button_hex, palette["text"])),
                )
                action = BrandedBox(
                    safe, close_action_style, background=_reportlab_color(close_button_hex),
                    border=_reportlab_color(close_button_hex), border_width=0,
                    radius=_number(primary_button.get("radius"), 16, 0, 20), padding=8, min_height=31,
                )
                rows.append([Table([[action]], colWidths=[min(190, document.width * 0.42)], hAlign="LEFT", style=TableStyle([
                    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]))])
                component_usage["actions"] += 1
            else:
                rows.append([Paragraph(safe, close_body)])
                component_usage["standard_blocks"] += 1
        story.extend([
            CondPageBreak(102),
            Table(rows, colWidths=[document.width], style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), panel_background),
                ("LEFTPADDING", (0, 0), (-1, -1), section_padding),
                ("RIGHTPADDING", (0, 0), (-1, -1), section_padding),
                ("TOPPADDING", (0, 0), (-1, 0), base * 1.8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), base * 1.8),
                ("TOPPADDING", (0, 1), (-1, -1), base),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ])),
            Spacer(1, base * 1.5),
        ])

    sections = plan["layout"]["sections"]
    for section_index, section in enumerate(sections):
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
                "radius": _number(primary_button.get("radius"), 16, 0, 20),
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
