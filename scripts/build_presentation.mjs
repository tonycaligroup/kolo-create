import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import PptxGenJS from "pptxgenjs";

const [specPath, outputPath, previewDir] = process.argv.slice(2);
if (!specPath || !outputPath || !previewDir) throw new Error("usage: build_presentation.mjs SPEC OUTPUT PREVIEW_DIR");
const spec = JSON.parse(await fs.readFile(specPath, "utf8"));
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const PX = 96;
const W = 1280, H = 720;
const inch = (value) => value / PX;
const noHash = (value) => value.replace(/^#/, "").toUpperCase();
const escapeHtml = (value) => value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const system = spec.system;
const palette = system.tokens.colors;
const blocks = new Map(spec.blocks.map((block) => [block.id, block]));
const clean = (value = "") => value
  .replace(/\*\*([^*]+)\*\*/g, "$1").replace(/__([^_]+)__/g, "$1")
  .replace(/`([^`]+)`/g, "$1").replace(/\[([^\]]+)\]\([^\)]+\)/g, "$1").trim();
const rgb = (value) => [1, 3, 5].map((index) => parseInt(value.slice(index, index + 2), 16) / 255);
const lum = (value) => rgb(value).map((v) => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
  .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
const contrast = (a, b) => { const [high, low] = [lum(a), lum(b)].sort((x, y) => y - x); return (high + .05) / (low + .05); };
const readable = (background, preferred = palette.text) => contrast(background, preferred) >= 4.5
  ? preferred : (contrast(background, "#FFFFFF") > contrast(background, "#111111") ? "#FFFFFF" : "#111111");
const displayFont = system.tokens.typography.display_fallback === "serif" ? "Georgia" : "Arial";
const bodyFont = system.tokens.typography.body_fallback === "serif" ? "Georgia" : "Arial";
const accent = palette.accent;
const background = palette.background;
const ink = readable(background, palette.text);

const pptx = new PptxGenJS();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "Kolo Create";
pptx.company = "Kolo";
pptx.subject = `${system.name} presentation`;
pptx.title = spec.plan.title;
pptx.lang = "en-US";
pptx.theme = { headFontFace: displayFont, bodyFontFace: bodyFont, lang: "en-US" };

const previews = new Map();
function prepareSlide(slide, color) {
  slide.background = { color: noHash(color) };
  previews.set(slide, { background: color, elements: [] });
}
function preview(slide, html) { previews.get(slide).elements.push(html); }
function addText(slide, text, x, y, width, height, size, color = ink, options = {}) {
  const value = clean(text);
  slide.addText(value, {
    x: inch(x), y: inch(y), w: inch(width), h: inch(height),
    fontFace: options.body ? bodyFont : displayFont,
    fontSize: size, bold: options.bold ?? !options.body,
    color: noHash(color), margin: 0, fit: "shrink",
    align: options.align || "left", valign: options.vertical === "top" ? "top" : "mid",
    breakLine: false, isTextBox: true, objectName: options.name,
  });
  const weight = (options.bold ?? !options.body) ? 700 : 400;
  preview(slide, `<div class="text" style="left:${x}px;top:${y}px;width:${width}px;height:${height}px;font-family:${escapeHtml(options.body ? bodyFont : displayFont)};font-size:${size * 96 / 72}px;font-weight:${weight};color:${color};text-align:${options.align || "left"};align-items:${options.vertical === "top" ? "flex-start" : "center"}">${escapeHtml(value).replace(/\n/g, "<br>")}</div>`);
}
function addRect(slide, x, y, width, height, fill, radius = 0, line = null) {
  slide.addShape(radius ? pptx.ShapeType.roundRect : pptx.ShapeType.rect, {
    x: inch(x), y: inch(y), w: inch(width), h: inch(height),
    fill: { color: noHash(fill) },
    line: line ? { color: noHash(line), width: 1 } : { color: noHash(fill), transparency: 100 },
  });
  preview(slide, `<div style="position:absolute;left:${x}px;top:${y}px;width:${width}px;height:${height}px;background:${fill};border-radius:${radius}px"></div>`);
}
function addRule(slide, x, y, width, color = accent, height = 4) { addRect(slide, x, y, width, height, color); }
function addFooter(slide, index) {
  addText(slide, system.name.toUpperCase(), 72, 668, 500, 24, 10, ink, { body: true, bold: true, name: "brand-footer" });
  addText(slide, String(index).padStart(2, "0"), 1160, 668, 48, 24, 10, ink, { body: true, align: "right", name: "slide-number" });
}
async function addImage(slide, asset, position, alt) {
  if (!asset?.path) return false;
  const suffix = path.extname(asset.path).toLowerCase();
  if (![".png", ".jpg", ".jpeg", ".webp"].includes(suffix)) return false;
  try {
    await fs.access(asset.path);
    slide.addImage({
      path: asset.path, altText: alt,
      x: inch(position.left), y: inch(position.top), w: inch(position.width), h: inch(position.height),
      sizing: { type: "cover", w: inch(position.width), h: inch(position.height) },
    });
    preview(slide, `<img alt="${escapeHtml(alt)}" src="${pathToFileURL(asset.path).href}" style="position:absolute;left:${position.left}px;top:${position.top}px;width:${position.width}px;height:${position.height}px;object-fit:cover">`);
    return true;
  } catch { return false; }
}
function slideBlocks(planSlide) { return planSlide.block_ids.map((id) => blocks.get(id)).filter(Boolean); }
function contentOnly(items) { return items.filter((block) => !block.kind.startsWith("heading")); }
function splitFeature(text) {
  const value = clean(text).replace(/^\d{1,2}[.)]\s*/, "");
  const index = value.indexOf(":");
  return index > 0 && index < 58 ? [value.slice(0, index), value.slice(index + 1).trim()] : [value, ""];
}

const media = (system.assets || []).filter((asset) => asset.kind === "hero-image" && asset.path);
let mediaIndex = 0;
for (let index = 0; index < spec.plan.slides.length; index++) {
  const planSlide = spec.plan.slides[index];
  const body = contentOnly(slideBlocks(planSlide));
  const slide = pptx.addSlide();
  prepareSlide(slide, background);

  if (planSlide.archetype === "cover") {
    const useMedia = media.length > 0 && (system.visual_language?.media_coverage || 0) >= .15;
    if (useMedia) {
      await addImage(slide, media[mediaIndex++], { left: 700, top: 0, width: 580, height: 720 }, `${system.name} brand imagery`);
      addText(slide, system.name.toUpperCase(), 72, 54, 520, 28, 11, accent, { body: true, bold: true, name: "brand-label" });
      addText(slide, planSlide.title, 72, 140, 560, 210, 48, ink, { bold: true, name: "title" });
      addText(slide, planSlide.subtitle, 72, 385, 540, 145, 22, ink, { body: true, bold: false, name: "subtitle" });
      addRule(slide, 72, 610, 190, accent, 8);
    } else {
      addText(slide, system.name.toUpperCase(), 72, 58, 600, 28, 11, accent, { body: true, bold: true, name: "brand-label" });
      addText(slide, planSlide.title, 72, 168, 1020, 165, 56, ink, { bold: true, name: "title" });
      addText(slide, planSlide.subtitle, 72, 380, 850, 120, 23, ink, { body: true, bold: false, name: "subtitle" });
      addRule(slide, 72, 594, 330, accent, 10);
      addRule(slide, 420, 594, 110, palette.accent_secondary || accent, 10);
    }
  } else if (planSlide.archetype === "process") {
    addText(slide, planSlide.title, 72, 54, 1120, 72, 34, ink, { bold: true, name: "title" });
    addRule(slide, 72, 143, 1120, accent, 3);
    const bullets = body.filter((block) => block.kind === "bullet").slice(0, 4);
    const intro = body.find((block) => block.kind === "paragraph");
    if (intro) addText(slide, intro.text, 72, 166, 1050, 74, 18, ink, { body: true, bold: false });
    bullets.forEach((block, itemIndex) => {
      const [label, detail] = splitFeature(block.text);
      const x = 72 + itemIndex * (1100 / bullets.length);
      addText(slide, String(itemIndex + 1).padStart(2, "0"), x, 268, 70, 44, 19, accent, { body: true, bold: true });
      addText(slide, label, x, 322, 225, 78, 23, ink, { bold: true, vertical: "top" });
      addText(slide, detail, x, 416, 225, 130, 16, ink, { body: true, bold: false, vertical: "top" });
    });
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "feature-list") {
    addText(slide, planSlide.title, 72, 54, 1120, 72, 34, ink, { bold: true, name: "title" });
    const intro = body.find((block) => block.kind === "paragraph");
    if (intro) addText(slide, intro.text, 72, 145, 1080, 76, 18, ink, { body: true, bold: false });
    const bullets = body.filter((block) => block.kind === "bullet").slice(0, 6);
    bullets.forEach((block, itemIndex) => {
      const [label, detail] = splitFeature(block.text);
      const column = itemIndex % 2, row = Math.floor(itemIndex / 2);
      const x = 72 + column * 570, y = 248 + row * 124;
      addRule(slide, x, y, 500, column ? palette.accent_secondary || accent : accent, 3);
      addText(slide, label, x, y + 16, 500, 34, 20, ink, { bold: true, vertical: "top" });
      addText(slide, detail, x, y + 53, 500, 55, 15, ink, { body: true, bold: false, vertical: "top" });
    });
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "statement") {
    addText(slide, planSlide.title, 72, 54, 1120, 72, 34, ink, { bold: true, name: "title" });
    const statement = body.find((block) => block.kind === "callout") || body[0];
    addText(slide, statement?.text || "", 72, 176, 1040, 240, 38, ink, { bold: true, vertical: "top" });
    addRule(slide, 72, 470, 300, accent, 8);
    const remainder = body.filter((block) => block !== statement).map((block) => clean(block.text)).join("\n\n");
    addText(slide, remainder, 670, 470, 500, 130, 17, ink, { body: true, bold: false, vertical: "top" });
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "closing") {
    const dark = palette.brand_dark || palette.text;
    prepareSlide(slide, dark);
    const onDark = readable(dark, palette.background);
    addText(slide, system.name.toUpperCase(), 72, 58, 600, 28, 11, accent, { body: true, bold: true });
    addText(slide, planSlide.title, 72, 162, 1040, 150, 48, onDark, { bold: true, name: "title" });
    const copy = body.filter((block) => block.kind !== "action").map((block) => clean(block.text)).join("\n\n");
    addText(slide, copy, 72, 348, 850, 128, 21, onDark, { body: true, bold: false, vertical: "top" });
    const action = body.find((block) => block.kind === "action");
    if (action) {
      addRule(slide, 72, 552, 420, accent, 5);
      addText(slide, action.text, 72, 574, 600, 44, 18, onDark, { body: true, bold: true });
    }
  } else {
    const useMedia = mediaIndex < media.length;
    addText(slide, planSlide.title, 72, 54, useMedia ? 650 : 1120, 88, 34, ink, { bold: true, name: "title" });
    const copy = body.map((block) => clean(block.text)).join("\n\n");
    addText(slide, copy, 72, 188, useMedia ? 520 : 940, 360, 20, ink, { body: true, bold: false, vertical: "top" });
    if (useMedia) await addImage(slide, media[mediaIndex++], { left: 690, top: 0, width: 590, height: 635 }, `${system.name} brand imagery`);
    else addRule(slide, 72, 590, 500, accent, 7);
    addFooter(slide, index + 1);
  }
  slide.addNotes(`Generated from ${system.name} design system ${system.version || "1.0.0"}. Source block IDs: ${planSlide.block_ids.join(", ")}.`);
}

await pptx.writeFile({ fileName: outputPath, compression: true });
let previewIndex = 0;
for (const state of previews.values()) {
  previewIndex += 1;
  const name = `slide-${String(previewIndex).padStart(2, "0")}`;
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>*{box-sizing:border-box}html,body{margin:0;width:${W}px;height:${H}px;overflow:hidden}.slide{position:relative;width:${W}px;height:${H}px;background:${state.background};overflow:hidden}.text{position:absolute;display:flex;line-height:1.18;white-space:pre-wrap;overflow:hidden}</style></head><body><main class="slide">${state.elements.join("")}</main><script>for(const el of document.querySelectorAll('.text')){let size=parseFloat(getComputedStyle(el).fontSize);while((el.scrollHeight>el.clientHeight||el.scrollWidth>el.clientWidth)&&size>10){size-=.5;el.style.fontSize=size+'px'}}</script></body></html>`;
  await fs.writeFile(path.join(previewDir, `${name}.html`), html);
  await fs.writeFile(path.join(previewDir, `${name}.layout.json`), JSON.stringify({
    schema: "kolo.presentation.layout/v1", unit: "px", slide: previewIndex,
    frame: { left: 0, top: 0, width: W, height: H }, elementCount: state.elements.length,
    previewKind: "same-plan-html-composition-preview",
  }, null, 2));
}
console.log(JSON.stringify({ output: outputPath, slides: spec.plan.slides.length, previews: previewDir }));
