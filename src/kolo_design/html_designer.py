from __future__ import annotations

import copy
import html
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from .browser_extract import _browser_executable
from .assets import select_logo_asset
from .brand_components import select_component_plan, validate_component_plan
from .composition import select_composition, validate_composition
from .content_map import build_content_map, validate_content_map
from .contracts import validate_design_system, validate_document_request
from .design_grammar import compile_design_grammar, validate_design_grammar
from .design_quality import evaluate_design_plan
from .pdf_designer import (
    TOFU_MARKERS,
    _brand_dark,
    _contrast_ratio,
    _eyebrow_color,
    _legible_foreground,
    _number,
    _preferred_foreground,
    _print_safe_text,
    create_pdf,
)
from .planner import DeterministicPlanner, DocumentPlanner, source_blocks, validate_plan
from .scene_graph import build_scene_plan, validate_scene_plan
from .util import atomic_write, confined, read_json, sha256_bytes, write_json


def _inline_html(value: str) -> str:
    safe = html.escape(value)
    safe = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"__([^_\n]+)__", r"<strong>\1</strong>", safe)
    safe = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", safe)
    safe = re.sub(r"\[([^]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', safe)
    return safe


def _font_stack(tokens: dict[str, Any], role: str) -> str:
    typography = tokens.get("typography") or {}
    family = str(typography.get(f"{role}_family") or ("Georgia" if role == "display" else "Arial"))
    fallback = str(typography.get(f"{role}_fallback") or "sans-serif")
    generic = "Georgia, 'Times New Roman', serif" if fallback == "serif" else "Arial, Helvetica, sans-serif"
    return f"{family!r}, {generic}"


def _asset_uri(system: dict[str, Any], kind: str) -> str | None:
    if kind == "logo":
        asset = select_logo_asset(system, allow_svg=True, max_width=132, max_height=56)
        if asset:
            return Path(str(asset["path"])).resolve().as_uri()
        return None
    suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
    for asset in system.get("assets") or []:
        candidate = Path(str(asset.get("path", "")))
        if asset.get("kind") == kind and candidate.suffix.lower() in suffixes and candidate.exists():
            return candidate.resolve().as_uri()
    return None


def _page_groups(sections: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep comparison documents at three explicit pages with stable ownership."""
    split = min(3, max(1, len(sections) - 1))
    return [sections[:split], sections[split:]]


def _render_section(
    section: dict[str, Any],
    by_id: dict[str, dict[str, str]],
    family: str,
    *,
    first: bool = False,
    closing: bool = False,
    treatment: str = "standard",
    skip_ids: set[str] | None = None,
) -> tuple[str, dict[str, int]]:
    skip_ids = skip_ids or set()
    blocks = [by_id[block_id] for block_id in section["block_ids"] if block_id not in skip_ids]
    counts = {"headings": 0, "cards": 0, "callouts": 0, "actions": 0, "standard_blocks": 0, "brand_rules": 0, "feature_bands": 0, "editorial_features": 0}
    if closing:
        heading = next((block for block in blocks if block["kind"].startswith("heading")), None)
        action = next((block for block in blocks if block["kind"] == "action"), None)
        body = [block for block in blocks if block["kind"] == "paragraph"]
        counts["headings"] += int(heading is not None)
        counts["brand_rules"] += int(heading is not None)
        counts["actions"] += int(action is not None)
        counts["standard_blocks"] += len(body)
        body_html = "".join(f'<p>{_inline_html(block["text"])}</p>' for block in body)
        action_html = ""
        if action:
            action_html = f'<div class="closing-action"><span>Continue</span>{_inline_html(action["text"])}</div>'
        return (
            '<section class="section closing">'
            '<div class="section-rule"></div>'
            f'<h2>{_inline_html(heading["text"]) if heading else ""}</h2>'
            f'<div class="closing-grid"><div>{body_html}</div>{action_html}</div>'
            '</section>',
            counts,
        )

    rendered: list[str] = []
    pending_cards: list[dict[str, str]] = []

    def flush_cards() -> None:
        if not pending_cards:
            return
        cards = []
        for index, block in enumerate(pending_cards):
            extra = " feature-card brand-feature-band" if treatment == "feature-band" and index == 0 else ""
            if treatment == "editorial-feature-list":
                extra += " editorial-feature"
            cards.append(f'<div class="card{extra}"><div>{_inline_html(block["text"])}</div></div>')
        grid_class = "card-grid editorial-feature-list" if treatment == "editorial-feature-list" else "card-grid"
        rendered.append(f'<div class="{grid_class}">{"".join(cards)}</div>')
        counts["cards"] += len(pending_cards)
        counts["feature_bands"] += int(treatment == "feature-band")
        counts["editorial_features"] += len(pending_cards) if treatment == "editorial-feature-list" else 0
        pending_cards.clear()

    bullet_count = sum(block["kind"] == "bullet" for block in blocks)
    for block in blocks:
        kind, value = block["kind"], _inline_html(block["text"])
        if kind == "heading1":
            continue
        if kind == "bullet" and bullet_count >= 2:
            pending_cards.append(block)
            continue
        flush_cards()
        if kind.startswith("heading"):
            rendered.append(f'<div class="section-rule"></div><h2>{value}</h2>')
            counts["headings"] += 1
            counts["brand_rules"] += 1
        elif kind == "callout":
            rendered.append(f'<div class="callout">{value}</div>')
            counts["callouts"] += 1
        elif kind == "action":
            rendered.append(f'<div class="text-action"><span>Continue</span>{value}</div>')
            counts["actions"] += 1
        else:
            rendered.append(f'<p class="{"lead" if first else "body"}">{value}</p>')
            counts["standard_blocks"] += 1
            first = False
    flush_cards()
    return f'<section class="section">{"".join(rendered)}</section>', counts


def _document_html(system: dict[str, Any], plan: dict[str, Any], blocks: list[dict[str, str]]) -> tuple[str, dict[str, int]]:
    palette = system["tokens"]["colors"]
    components = system.get("components") or {}
    cards = components.get("cards") or {}
    buttons = (components.get("buttons") or {}).get("primary") or {}
    typography = components.get("typography") or {}
    base = float(system["tokens"]["spacing"]["base"])
    background = palette["background"]
    text = palette["text"]
    accent = palette["accent"]
    accent_secondary = palette.get("accent_secondary", accent)
    surface = palette["surface"]
    brand_dark = _brand_dark(system, palette)
    eyebrow = _eyebrow_color(palette)
    card_background = cards.get("background") if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(cards.get("background", ""))) else surface
    card_text = _legible_foreground(card_background, cards.get("foreground"), text)
    button_background = buttons.get("background") if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(buttons.get("background", ""))) else accent
    button_text = _preferred_foreground(button_background, buttons.get("foreground"), text)
    h1 = _number((typography.get("h1") or {}).get("font_size"), 42, 32, 58)
    h2 = _number((typography.get("h2") or {}).get("font_size"), 30, 22, 38)
    radius = _number(cards.get("radius"), system["tokens"].get("shape", {}).get("radius", 8), 0, 22)
    card_padding = _number(base * 3, 14, 10, 22)
    family = plan["composition"]["family"]
    component_plan = plan.get("component_plan") or select_component_plan(system, plan, blocks)
    validate_component_plan(component_plan)
    section_treatments = {item["section_id"]: item["treatment"] for item in component_plan["sections"]}
    component_library = component_plan["library"]["components"]
    marker_recipe = component_library["section-marker"]
    marker_style = marker_recipe["style"]
    accent = marker_recipe.get("primary", accent)
    accent_secondary = marker_recipe.get("secondary", accent)
    feature_recipe = component_library["feature-band"]
    grid_cell_style = component_library["numbered-feature-grid"].get("cell_style", "card")
    logo = _asset_uri(system, "logo")
    cover_component = component_plan["cover"]
    cover_asset_id = cover_component.get("asset_id")
    hero_asset = next(
        (asset for asset in system.get("assets") or [] if cover_asset_id and asset.get("id") == cover_asset_id), None
    )
    hero = Path(str(hero_asset["path"])).resolve().as_uri() if hero_asset and Path(str(hero_asset["path"])).exists() else None
    hero_layout = {"bottom-band": "landscape", "side-panel": "portrait", "none": "none"}[cover_component["placement"]]
    hero_markup = (
        f'<figure class="hero-frame"><img src="{hero}" alt=""></figure>'
        if hero else ""
    )
    logo_markup = f'<img class="logo" src="{logo}" alt="{html.escape(system["name"])} logo">' if logo else f'<div class="wordmark">{html.escape(system["name"])}</div>'
    page_size = "A4" if plan["page_size"] == "A4" else "Letter"
    portrait_dimensions = ("8.27in", "11.69in") if page_size == "A4" else ("8.5in", "11in")
    page_dimensions = tuple(reversed(portrait_dimensions)) if plan["orientation"] == "landscape" else portrait_dimensions
    page_width, page_height = page_dimensions

    by_id = {block["id"]: block for block in blocks}
    cover_subtitle = plan["subtitle"]
    cover_source_id: str | None = None
    if family == "product_showcase":
        opening_callout = next((block for block in blocks[:3] if block["kind"] == "callout"), None)
        if opening_callout:
            cover_subtitle = opening_callout["text"]
            cover_source_id = opening_callout["id"]
    skip_ids = {cover_source_id} if cover_source_id else set()
    groups = _page_groups(plan["layout"]["sections"])
    usage = {"headings": 0, "cards": 0, "callouts": 0, "actions": 0, "standard_blocks": 0, "brand_rules": 1, "feature_bands": 0, "editorial_features": 0}
    body_pages = []
    for page_index, group in enumerate(groups, 1):
        section_markup = []
        for section_index, section in enumerate(group):
            effective_blocks = [
                by_id[block_id] for block_id in section["block_ids"]
                if block_id not in skip_ids and by_id[block_id]["kind"] != "heading1"
            ]
            if not effective_blocks:
                continue
            is_closing = page_index == len(groups) and section is group[-1] and any(
                by_id[block_id]["kind"] == "action" for block_id in section["block_ids"]
            )
            markup, counts = _render_section(
                section, by_id, family,
                first=family == "editorial_narrative" and section_index == 0,
                closing=is_closing,
                treatment=section_treatments.get(section["id"], "standard"),
                skip_ids=skip_ids,
            )
            section_markup.append(markup)
            for key, value in counts.items():
                usage[key] += value
        body_pages.append(
            f'<article class="page content-page" data-page="{page_index + 1}">'
            f'<main class="content-grid">{"".join(section_markup)}</main>'
            f'<footer><span>{html.escape(system["name"].upper())}</span><span>{page_index:02d}</span></footer>'
            '</article>'
        )

    css = f"""
      :root {{
        --background:{background}; --surface:{surface}; --text:{text}; --accent:{accent};
        --accent-2:{accent_secondary}; --brand-dark:{brand_dark}; --eyebrow:{eyebrow};
        --feature-bg:{feature_recipe['background']}; --feature-text:{feature_recipe['foreground']};
        --feature-accent:{feature_recipe['accent']};
        --card-bg:{card_background}; --card-text:{card_text}; --button-bg:{button_background};
        --button-text:{button_text}; --display:{_font_stack(system['tokens'], 'display')};
        --body:{_font_stack(system['tokens'], 'body')}; --radius:{radius}px; --card-pad:{card_padding}px;
        --page-width:{page_width}; --page-height:{page_height}; --h1:{h1}px; --h2:{h2}px;
      }}
      @page {{ size:{page_size} {plan['orientation']}; margin:0; }}
      * {{ box-sizing:border-box; }}
      html,body {{ margin:0; padding:0; background:#d9d9d9; }}
      body {{ font-family:var(--body); color:var(--text); }}
      .page {{ width:var(--page-width); height:var(--page-height); position:relative; overflow:hidden;
        background:var(--background); break-after:page; page-break-after:always; }}
      .page:last-child {{ break-after:auto; page-break-after:auto; }}
      .cover {{ padding:.82in .72in .65in; display:grid; grid-template-columns:50% 50%; gap:0; }}
      .cover-copy {{ position:relative; z-index:2; align-self:center; padding-right:.32in; }}
      .cover.hero-landscape {{ grid-template-columns:1fr; grid-template-rows:56% 44%; }}
      .cover.hero-landscape .cover-copy {{ max-width:6.35in; padding-right:0; }}
      .cover.hero-none {{ grid-template-columns:1fr; }}
      .cover.hero-none .cover-copy {{ width:100%; max-width:6.35in; padding-right:0; justify-self:center; }}
      .eyebrow {{ color:var(--eyebrow); font-size:10px; text-transform:uppercase; letter-spacing:1.2px; font-weight:700; margin-bottom:28px; }}
      h1,h2 {{ font-family:var(--display); font-weight:500; letter-spacing:-.025em; margin:0; }}
      h1 {{ font-size:var(--h1); line-height:1.02; margin-bottom:20px; }}
      .subtitle {{ font-size:15px; line-height:1.52; max-width:340px; margin:0 0 36px; }}
      .logo {{ display:block; max-width:132px; max-height:56px; object-fit:contain; object-position:left center; }}
      .wordmark {{ font-family:var(--display); font-size:28px; font-weight:700; }}
      .hero-frame {{ align-self:stretch; margin:-.82in -.72in -.65in 0; overflow:hidden; background:var(--brand-dark); }}
      .hero-frame img {{ display:block; width:100%; height:100%; object-fit:cover; object-position:54% center; }}
      .hero-landscape .hero-frame {{ margin:0 -.72in -.65in; }}
      .hero-landscape .hero-frame img {{ object-position:center center; }}
      .content-page {{ padding:.55in .72in .58in; }}
      .content-grid {{ width:100%; height:calc(100% - .28in); display:flex; flex-direction:column; justify-content:flex-start; }}
      .section {{ width:100%; margin-bottom:22px; break-inside:avoid; }}
      .section:last-child {{ margin-bottom:0; }}
      .section-rule {{ width:94px; height:5px; border-radius:3px; background:var(--accent); margin:0 0 17px; }}
      .marker-dual-tone .section-rule {{ background:linear-gradient(90deg,var(--accent) 0 72%,var(--accent-2) 72% 100%); }}
      h2 {{ font-size:var(--h2); line-height:1.04; margin-bottom:9px; max-width:92%; }}
      p {{ margin:0 0 11px; font-size:12.5px; line-height:1.48; }}
      .lead {{ font-size:15px; line-height:1.5; }}
      .callout {{ border-left:6px solid var(--accent); padding:17px 20px; margin:0 0 23px; font-size:15px; line-height:1.48; background:color-mix(in srgb, var(--surface), transparent 55%); }}
      .card-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:11px; margin-top:14px; }}
      .card {{ min-height:67px; padding:var(--card-pad); border-radius:var(--radius); background:var(--card-bg); color:var(--card-text); font-size:13px; line-height:1.35; display:flex; align-items:center; border:1px solid color-mix(in srgb, var(--card-text), transparent 86%); }}
      .card strong {{ font-weight:700; }}
      .family-asymmetric_feature_grid .feature-card,.family-product_showcase .feature-card {{ grid-column:1/-1; min-height:76px; font-size:15px; }}
      .brand-feature-band {{ background:var(--feature-bg); color:var(--feature-text); border:0; border-top:5px solid var(--feature-accent); padding:18px 20px; }}
      .editorial-feature-list {{ grid-template-columns:1fr; gap:0; counter-reset:features; }}
      .editorial-feature {{ counter-increment:features; display:grid; grid-template-columns:52px 1fr; align-items:start; padding:16px 0; border:0; border-top:1px solid var(--surface); border-radius:0; background:transparent; color:var(--text); }}
      .editorial-feature::before {{ content:counter(features, decimal-leading-zero); color:var(--accent); font:500 20px/1 var(--display); }}
      .grid-open .card:not(.brand-feature-band) {{ min-height:64px; align-items:flex-start; padding:15px 0; border:0; border-top:1px solid var(--surface); border-radius:0; background:transparent; color:var(--text); }}
      .family-editorial_narrative .card-grid {{ gap:0 24px; }}
      .family-editorial_narrative .card {{ background:transparent; color:var(--text); border:0; border-top:1px solid var(--surface); border-radius:0; padding:13px 0; min-height:58px; }}
      .text-action,.closing-action {{ text-align:right; }}
      .text-action span,.closing-action span {{ display:block; color:var(--eyebrow); text-transform:uppercase; letter-spacing:1px; font-size:8px; margin-bottom:7px; }}
      a {{ color:inherit; text-decoration:none; }}
      .closing {{ border-top:1px solid var(--surface); padding-top:10px; margin-top:auto; }}
      .closing-grid {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(190px,38%); gap:28px; align-items:center; }}
      .closing-action {{ font-size:15px; line-height:1.3; }}
      footer {{ position:absolute; left:.72in; right:.72in; bottom:.28in; display:flex; justify-content:space-between; font-size:8px; letter-spacing:.04em; text-transform:uppercase; }}
      .family-product_showcase .section-rule {{ background:var(--accent-2); }}
      @media screen {{ .page {{ margin:24px auto; box-shadow:0 18px 55px rgba(0,0,0,.18); }} }}
      @media print {{ html,body {{ background:transparent; }} .page {{ margin:0; box-shadow:none; }} }}
    """
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="generator" content="Kolo Create HTML renderer"><title>{html.escape(plan["title"])}</title><style>{css}</style></head>'
        f'<body class="family-{family} marker-{marker_style} grid-{grid_cell_style}">'
        f'<article class="page cover hero-{hero_layout}" data-page="1">'
        f'<div class="cover-copy"><div class="eyebrow">{html.escape(system["name"])} design language</div>'
        f'<h1>{_inline_html(plan["title"])}</h1><p class="subtitle">{_inline_html(cover_subtitle)}</p>{logo_markup}</div>'
        f'{hero_markup}</article>{"".join(body_pages)}</body></html>'
    )
    return document, usage


def _render_previews(pdf_path: Path) -> list[str]:
    preview_dir = confined(pdf_path.parent, pdf_path.stem + "-preview")
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    renderer = shutil.which("pdftoppm")
    if not renderer:
        return []
    subprocess.run([renderer, "-png", "-r", "120", str(pdf_path), str(preview_dir / "page")], check=True, timeout=120, capture_output=True)
    return [str(path) for path in sorted(preview_dir.glob("page-*.png"))]


def create_html_pdf(
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
    plan["title"], title_removed = _print_safe_text(str(plan["title"]))
    plan["subtitle"], subtitle_removed = _print_safe_text(str(plan["subtitle"]))
    removed_emoji_count += title_removed + subtitle_removed
    plan["composition"] = select_composition(system, plan, blocks, prompt)
    validate_composition(plan["composition"])
    plan["design_grammar"] = system.get("design_grammar") or compile_design_grammar(system)
    validate_design_grammar(plan["design_grammar"])
    plan["content_map"] = build_content_map(blocks, prompt)
    validate_content_map(plan["content_map"], blocks)
    plan["scene_plan"] = build_scene_plan(
        plan["design_grammar"], plan["content_map"], format_name="document", brand_id=system["id"]
    )
    validate_scene_plan(plan["scene_plan"], blocks)
    plan["design_quality"] = evaluate_design_plan(plan["design_grammar"], plan["scene_plan"])
    plan["component_plan"] = select_component_plan(system, plan, blocks, prompt)
    validate_component_plan(plan["component_plan"])
    markup, component_usage = _document_html(system, plan, blocks)

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = output_path.with_suffix(".html")
    atomic_write(html_path, markup.encode("utf-8"))
    executable = _browser_executable()
    if not executable:
        raise RuntimeError("Chromium is required for the HTML/CSS renderer")
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context()

        def route_request(route: Any) -> None:
            if urlparse(route.request.url).scheme in {"file", "data", "about", "blob"}:
                route.continue_()
            else:
                route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        try:
            page.goto(html_path.as_uri(), wait_until="load", timeout=20_000)
            page.emulate_media(media="print")
            metrics = page.evaluate(
                """() => {
                  const pages = [...document.querySelectorAll('.page')];
                  const overflow = pages.map((el, index) => ({
                    page: index + 1,
                    horizontal: Math.max(0, el.scrollWidth - el.clientWidth),
                    vertical: Math.max(0, el.scrollHeight - el.clientHeight)
                  })).filter((item) => item.horizontal > 1 || item.vertical > 1);
                  const bad = [...document.querySelectorAll('.page *')].filter((el) => {
                    const r = el.getBoundingClientRect(), p = el.closest('.page').getBoundingClientRect();
                    return r.width > 0 && (r.left < p.left - 1 || r.right > p.right + 1 || r.top < p.top - 1 || r.bottom > p.bottom + 1);
                  }).slice(0, 20).map((el) => el.className || el.tagName);
                  const alignment = [];
                  for (const page of document.querySelectorAll('.content-page')) {
                    const grid = page.querySelector('.content-grid').getBoundingClientRect();
                    for (const el of page.querySelectorAll('.section,.card-grid,.closing-grid')) {
                      const r = el.getBoundingClientRect();
                      if (Math.abs(r.left - grid.left) > 1 || Math.abs(r.right - grid.right) > 1) {
                        alignment.push({page: page.dataset.page, target: el.className, left: r.left - grid.left, right: r.right - grid.right});
                      }
                    }
                    const action = page.querySelector('.closing-action');
                    if (action && Math.abs(action.getBoundingClientRect().right - grid.right) > 1) {
                      alignment.push({page: page.dataset.page, target: 'closing-action-right'});
                    }
                  }
                  return {pageCount: pages.length, overflow, outOfBounds: bad, alignment};
                }"""
            )
            if metrics["overflow"] or metrics["outOfBounds"] or metrics["alignment"]:
                raise RuntimeError(f"HTML layout QA failed: {metrics}")
            page.pdf(path=str(output_path), print_background=True, prefer_css_page_size=True, margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        finally:
            context.close()
            browser.close()

    payload = output_path.read_bytes()
    reader = PdfReader(str(output_path))
    extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
    source_words = set(re.findall(r"[A-Za-z0-9]{4,}", content.lower()))
    output_words = set(re.findall(r"[A-Za-z0-9]{4,}", extracted.lower()))
    coverage = len(source_words & output_words) / max(1, len(source_words))
    tofu_found = sorted(marker for marker in TOFU_MARKERS if marker in extracted)
    unresolved_markdown = "**" in extracted or "__" in extracted
    if coverage < 0.75 or tofu_found or unresolved_markdown:
        raise RuntimeError(f"HTML PDF content QA failed: coverage={coverage:.1%}, tofu={tofu_found}, markdown={unresolved_markdown}")
    previews = _render_previews(output_path)
    layout_path = output_path.with_suffix(".layout.json")
    write_json(layout_path, {
        **plan,
        "planner": planner.version,
        "renderer": "html-css/1",
        "design_system": {"id": system["id"], "version": system["version"]},
        "source_blocks": blocks,
        "component_usage": component_usage,
    })
    quality_path = output_path.with_suffix(".quality.json")
    write_json(quality_path, {
        "schema_version": 1,
        "status": "pass",
        "created_at": datetime.now(UTC).isoformat(),
        "renderer": "html-css/1",
        "planner": planner.version,
        "design_system": {"id": system["id"], "version": system["version"], "path": str(design_system_path.resolve())},
        "design_grammar": {
            "signature": plan["design_grammar"]["signature"],
            "ranked_directions": plan["design_grammar"]["ranked_directions"],
        },
        "scene_plan": {
            "signature": plan["scene_plan"]["signature"],
            "components": [scene["component"] for scene in plan["scene_plan"]["scenes"]],
        },
        "design_quality": plan["design_quality"],
        "prompt": prompt,
        "pdf": {"path": str(output_path), "sha256": sha256_bytes(payload), "bytes": len(payload), "pages": len(reader.pages)},
        "html": {"path": str(html_path), "sha256": sha256_bytes(markup.encode("utf-8"))},
        "checks": {"dom_overflow_absent": True, "content_coverage": round(coverage, 4), "inline_markdown_resolved": not unresolved_markdown, "tofu_glyphs_absent": not tofu_found, "previews_rendered": bool(previews)},
        "normalization": {"unsupported_emoji_removed": removed_emoji_count},
        "previews": previews,
    })
    return {
        "status": "succeeded", "renderer": "html-css/1", "pdf": str(output_path), "html": str(html_path),
        "layout_plan": str(layout_path), "quality_report": str(quality_path), "previews": previews,
        "pages": len(reader.pages), "component_usage": component_usage, "composition": plan["composition"],
        "scene_plan": plan["scene_plan"]["signature"],
    }


@dataclass(frozen=True)
class _ReplayPlanner:
    plan_value: dict[str, Any]
    version: str

    def plan(self, content: str, prompt: str, source_blocks: list[dict[str, str]]) -> dict[str, Any]:
        return copy.deepcopy(self.plan_value)


def _comparison_previews(left_paths: list[str], right_paths: list[str], output_dir: Path) -> list[str]:
    preview_dir = confined(output_dir, "comparison-preview")
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    results: list[str] = []
    for index, (left_path, right_path) in enumerate(zip(left_paths, right_paths), 1):
        left, right = Image.open(left_path).convert("RGB"), Image.open(right_path).convert("RGB")
        height = max(left.height, right.height)
        header = 42
        canvas = Image.new("RGB", (left.width + right.width + 18, height + header), "#E9E9E9")
        canvas.paste(left, (0, header))
        canvas.paste(right, (left.width + 18, header))
        draw = ImageDraw.Draw(canvas)
        draw.text((16, 14), "REPORTLAB / CURRENT", fill="#111111")
        draw.text((left.width + 34, 14), "HTML + CSS / CANDIDATE", fill="#111111")
        path = preview_dir / f"page-{index}.png"
        canvas.save(path)
        results.append(str(path))
    return results


def compare_pdf_renderers(
    design_system_path: Path,
    content_path: Path,
    prompt: str,
    output_dir: Path,
    planner: DocumentPlanner | None = None,
) -> dict[str, Any]:
    planner = planner or DeterministicPlanner()
    content = content_path.read_text(encoding="utf-8")
    blocks = source_blocks(content)
    shared_plan = planner.plan(content, prompt, blocks)
    validate_plan(shared_plan, blocks)
    replay = _ReplayPlanner(shared_plan, f"{planner.version}+shared-plan")
    system = read_json(design_system_path)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = system["id"]
    reportlab = create_pdf(design_system_path, content_path, prompt, output_dir / f"{prefix}-reportlab.pdf", replay)
    html_result = create_html_pdf(design_system_path, content_path, prompt, output_dir / f"{prefix}-html.pdf", replay)
    comparisons = _comparison_previews(reportlab["previews"], html_result["previews"], output_dir)
    manifest_path = output_dir / "comparison.json"
    write_json(manifest_path, {
        "schema_version": 1,
        "status": "ready_for_human_review",
        "design_system": {"id": system["id"], "version": system["version"], "path": str(design_system_path.resolve())},
        "prompt": prompt,
        "planner": replay.version,
        "model_calls": 0 if isinstance(planner, DeterministicPlanner) else 1,
        "reportlab": reportlab,
        "html_css": html_result,
        "comparison_previews": comparisons,
        "review_dimensions": ["brand fidelity", "composition", "typography", "spacing", "alignment", "readability", "overall preference"],
    })
    return {"status": "succeeded", "manifest": str(manifest_path), "reportlab": reportlab, "html_css": html_result, "comparison_previews": comparisons}
