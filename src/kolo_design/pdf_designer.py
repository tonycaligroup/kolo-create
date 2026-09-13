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
    display = system["tokens"]["typography"]["display_family"].lower()
    serif_markers = ("serif", "times", "georgia", "garamond", "baskerville")
    display_font = "Times-Bold" if any(marker in display for marker in serif_markers) else "Helvetica-Bold"
    body = system["tokens"]["typography"]["body_family"].lower()
    body_font = "Times-Roman" if any(marker in body for marker in serif_markers) else "Helvetica"
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

    page_size = A4 if plan["page_size"] == "A4" else LETTER
    if plan["orientation"] == "landscape":
        page_size = landscape(page_size)
    width, height = page_size
    palette = system["tokens"]["colors"]
    background = _reportlab_color(palette["background"])
    surface = _reportlab_color(palette["surface"])
    text = _reportlab_color(palette["text"])
    accent = _reportlab_color(palette["accent"])
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
        "bullet": ParagraphStyle("bullet", parent=None, fontName=body_font, fontSize=10.5, leading=16, textColor=text, leftIndent=16, firstLineIndent=-10, bulletIndent=0, spaceAfter=base),
        "card": ParagraphStyle("card", fontName=body_font, fontSize=_number(card_recipe.get("font_size"), 10.5, 9, 12), leading=15, textColor=_reportlab_color(card_foreground_hex), spaceAfter=0),
        "callout": ParagraphStyle("callout", fontName=body_font, fontSize=12, leading=18, textColor=text, spaceAfter=0),
        "action": ParagraphStyle("action", fontName=label_font, fontSize=_number(primary_button.get("font_size"), 10, 9, 12), leading=14, textColor=_recipe_color(primary_button.get("foreground"), "#FFFFFF"), alignment=TA_CENTER, spaceAfter=0),
    }

    def decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(background)
        canvas.rect(0, 0, width, height, stroke=0, fill=1)
        canvas.setFillColor(accent)
        canvas.rect(0, height - 7, width, 7, stroke=0, fill=1)
        if doc.page == 1:
            canvas.setFillColor(surface)
            canvas.circle(width - 0.3 * inch, 0.55 * inch, 1.7 * inch, stroke=0, fill=1)
            canvas.setFillColor(accent)
            canvas.circle(width - 0.05 * inch, 0.3 * inch, 0.85 * inch, stroke=0, fill=1)
        if doc.page > 1:
            canvas.setFont(label_font, 7.5)
            canvas.setFillColor(text)
            canvas.drawString(0.72 * inch, 0.35 * inch, system["name"].upper())
            canvas.drawRightString(width - 0.72 * inch, 0.35 * inch, f"{doc.page - 1:02d}")
        canvas.restoreState()

    story: list[Any] = [Spacer(1, height * 0.13)]
    story.append(Paragraph(html.escape(system["name"].upper()), styles["eyebrow"]))
    story.append(Paragraph(_inline_markdown(plan["title"]), styles["cover"]))
    story.append(Table([[""]], colWidths=[1.2 * inch], rowHeights=[5], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent)])))
    story.append(Spacer(1, base * 3))
    story.append(Paragraph(_inline_markdown(plan["subtitle"]), styles["subtitle"]))
    logo = next((asset for asset in system["assets"] if asset.get("kind") == "logo" and Path(asset.get("path", "")).suffix.lower() in {".png", ".jpg", ".jpeg"}), None)
    if logo and Path(logo["path"]).exists():
        image = Image(logo["path"], width=1.5 * inch, height=0.65 * inch, kind="proportional")
        image.hAlign = "LEFT"
        story.extend([Spacer(1, base * 5), image])
    story.append(PageBreak())

    by_id = {block["id"]: block for block in blocks}
    component_usage = {"headings": 0, "cards": 0, "callouts": 0, "actions": 0, "standard_blocks": 0}
    skipped_cover_heading = False

    def add_standard(block: dict[str, str]) -> None:
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
            story.extend([
                CondPageBreak(120),
                HRFlowable(width="10%", thickness=3, color=accent, spaceBefore=base * 2, spaceAfter=base * 1.4, hAlign="LEFT"),
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
                safe, styles["action"], background=_recipe_color(primary_button.get("background"), palette["accent"]),
                border=_recipe_color(primary_button.get("border_color"), palette["accent"]),
                border_width=_number(primary_button.get("border_width"), 0, 0, 2),
                radius=_number(primary_button.get("radius"), 16, 0, 20), padding=10, min_height=36,
                shadow=bool(primary_button.get("shadow") and primary_button.get("shadow") != "none"),
            )
            story.extend([Table([[action]], colWidths=[button_width], style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)])), Spacer(1, base * 2)])
            component_usage["actions"] += 1
        else:
            story.append(Paragraph(safe, styles["body"]))
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

    for section in plan["layout"]["sections"]:
        section_blocks = [by_id[block_id] for block_id in section["block_ids"]]
        variant = section["variant"]
        if variant == "card_grid":
            pending_cards: list[dict[str, str]] = []
            for block in section_blocks:
                if block["kind"] == "bullet":
                    pending_cards.append(block)
                else:
                    add_card_grid(pending_cards)
                    pending_cards = []
                    add_standard(block)
            add_card_grid(pending_cards)
        else:
            for block in section_blocks:
                add_standard(block)

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
                "background": primary_button.get("background") or palette["accent"],
                "foreground": primary_button.get("foreground") or "#FFFFFF",
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
        "component_usage": component_usage,
    }
