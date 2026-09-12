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
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import CondPageBreak, HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .contracts import validate_design_system, validate_document_request
from .planner import DeterministicPlanner, DocumentPlanner
from .util import confined, read_json, sha256_bytes, slugify, write_json

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


def _blocks(content: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            result.append(("paragraph", " ".join(paragraph).strip()))
            paragraph.clear()

    for raw in content.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("### "):
            flush(); result.append(("heading3", line[4:].strip()))
        elif line.startswith("## "):
            flush(); result.append(("heading2", line[3:].strip()))
        elif line.startswith("# "):
            flush(); result.append(("heading1", line[2:].strip()))
        elif re.match(r"^[-*•]\s+", line):
            flush(); result.append(("bullet", re.sub(r"^[-*•]\s+", "", line)))
        else:
            paragraph.append(line)
    flush()
    return result


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
    planner = planner or DeterministicPlanner()
    plan = planner.plan(content, prompt)
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

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(output_path), pagesize=page_size, leftMargin=0.72 * inch, rightMargin=0.72 * inch,
        topMargin=0.78 * inch, bottomMargin=0.7 * inch,
        title=plan["title"], author=f"Kolo Design Studio · {system['name']}",
    )
    styles = {
        "eyebrow": ParagraphStyle("eyebrow", fontName=label_font, fontSize=8.5, leading=11, textColor=accent, spaceAfter=base * 2, tracking=1.1),
        "cover": ParagraphStyle("cover", fontName=display_font, fontSize=38 if width < 700 else 46, leading=42 if width < 700 else 50, textColor=text, spaceAfter=base * 3),
        "subtitle": ParagraphStyle("subtitle", fontName=body_font, fontSize=13, leading=19, textColor=text, spaceAfter=base * 3),
        "h1": ParagraphStyle("h1", fontName=display_font, fontSize=25, leading=29, textColor=text, spaceBefore=base * 3, spaceAfter=base * 2),
        "h2": ParagraphStyle("h2", fontName=display_font, fontSize=18, leading=22, textColor=text, spaceBefore=base * 2.5, spaceAfter=base),
        "h3": ParagraphStyle("h3", fontName=label_font, fontSize=11, leading=15, textColor=accent, spaceBefore=base * 2, spaceAfter=base),
        "body": ParagraphStyle("body", fontName=body_font, fontSize=10.5, leading=16, textColor=text, spaceAfter=base * 1.6, alignment=TA_LEFT),
        "bullet": ParagraphStyle("bullet", parent=None, fontName=body_font, fontSize=10.5, leading=16, textColor=text, leftIndent=16, firstLineIndent=-10, bulletIndent=0, spaceAfter=base),
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

    parsed = _blocks(print_content)
    if parsed and parsed[0][0] == "heading1" and parsed[0][1].strip().lower() == plan["title"].strip().lower():
        parsed = parsed[1:]
    previous_kind: str | None = None
    for kind, value in parsed:
        safe = _inline_markdown(value)
        if kind == "paragraph" and previous_kind == "bullet":
            story.append(Spacer(1, base * 1.5))
        if kind == "heading1":
            story.append(CondPageBreak(110))
            story.append(Paragraph(safe, styles["h1"]))
        elif kind == "heading2":
            story.append(CondPageBreak(105))
            story.append(HRFlowable(width="10%", thickness=3, color=accent, spaceBefore=base * 2, spaceAfter=base * 1.4, hAlign="LEFT"))
            story.append(Paragraph(safe, styles["h2"]))
        elif kind == "heading3":
            story.append(Paragraph(safe.upper(), styles["h3"]))
        elif kind == "bullet":
            story.append(Paragraph(f"<bullet>•</bullet>{safe}", styles["bullet"]))
        else:
            story.append(Paragraph(safe, styles["body"]))
        previous_kind = kind

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

    preview_dir = confined(output_path.parent, output_path.stem + "-preview")
    preview_dir.mkdir(parents=True, exist_ok=True)
    previews: list[str] = []
    renderer = shutil.which("pdftoppm")
    if renderer:
        prefix = preview_dir / "page"
        subprocess.run([renderer, "-png", "-r", "120", str(output_path), str(prefix)], check=True, timeout=120, capture_output=True)
        previews = [str(path) for path in sorted(preview_dir.glob("page-*.png"))]

    report = {
        "schema_version": 1,
        "status": "pass",
        "created_at": datetime.now(UTC).isoformat(),
        "design_system": {"id": system["id"], "version": system["version"], "path": str(design_system_path.resolve())},
        "planner": planner.version,
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
    return {"status": "succeeded", "pdf": str(output_path), "quality_report": str(quality_path), "previews": previews, "pages": len(reader.pages)}
