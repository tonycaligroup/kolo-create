import fs from "node:fs/promises";
import path from "node:path";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

process.on("uncaughtException", (error) => {
  console.error("KOLO_PRESENTATION_ERROR", error?.stack || String(error));
  process.exit(1);
});
process.on("unhandledRejection", (error) => {
  console.error("KOLO_PRESENTATION_ERROR", error?.stack || String(error));
  process.exit(1);
});

const [specPath, outputPath, previewDir] = process.argv.slice(2);
if (!specPath || !outputPath || !previewDir) throw new Error("usage: build_presentation.mjs SPEC OUTPUT PREVIEW_DIR");
const spec = JSON.parse(await fs.readFile(specPath, "utf8"));
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const W = 1280, H = 720;
const system = spec.system;
const palette = system.tokens.colors;
const blocks = new Map(spec.blocks.map((block) => [block.id, block]));
const clean = (value = "") => value
  .replace(/\*\*([^*]+)\*\*/g, "$1")
  .replace(/__([^_]+)__/g, "$1")
  .replace(/`([^`]+)`/g, "$1")
  .replace(/\[([^\]]+)\]\([^\)]+\)/g, "$1")
  .trim();
const rgb = (value) => [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16) / 255);
const lum = (value) => rgb(value).map((v) => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
  .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
const contrast = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + .05) / (lo + .05); };
const readable = (bg, preferred = palette.text) => contrast(bg, preferred) >= 4.5 ? preferred : (contrast(bg, "#FFFFFF") > contrast(bg, "#111111") ? "#FFFFFF" : "#111111");
// PowerPoint does not reliably embed web fonts. Preserve the extracted serif/sans
// character with Office-safe families so the deck travels cleanly.
const font = system.tokens.typography.display_fallback === "serif" ? "Georgia" : "Arial";
const bodyFont = system.tokens.typography.body_fallback === "serif" ? "Georgia" : "Arial";
const accent = palette.accent;
const bg = palette.background;
const ink = readable(bg, palette.text);
const surface = palette.surface;

function addText(slide, text, x, y, w, h, size, color = ink, options = {}) {
  const shape = slide.shapes.add({ geometry: "textbox", name: options.name, position: { left: x, top: y, width: w, height: h }, fill: "none", line: { fill: "none", width: 0 } });
  shape.text = clean(text);
  shape.text.style = {
    typeface: options.body ? bodyFont : font,
    fontSizePt: size,
    bold: options.bold ?? !options.body,
    color,
    autoFit: "shrinkText",
    alignment: options.align || "left",
    verticalAlignment: options.vertical || "middle",
  };
  return shape;
}
function addRect(slide, x, y, w, h, fill, radius = 0, line = "none") {
  return slide.shapes.add({ geometry: radius ? "roundRect" : "rect", position: { left: x, top: y, width: w, height: h }, fill, line: { fill: line, width: line === "none" ? 0 : 1 }, borderRadius: radius });
}
function addRule(slide, x, y, w, color = accent, height = 4) { addRect(slide, x, y, w, height, color); }
function addFooter(slide, index) {
  addText(slide, system.name.toUpperCase(), 72, 668, 500, 24, 10, ink, { body: true, bold: true, name: "brand-footer" });
  addText(slide, String(index).padStart(2, "0"), 1160, 668, 48, 24, 10, ink, { body: true, align: "right", name: "slide-number" });
}
async function addImage(slide, asset, position, alt) {
  if (!asset?.path) return false;
  try {
    const blob = new Uint8Array(await fs.readFile(asset.path));
    const ext = path.extname(asset.path).toLowerCase();
    const contentType = ext === ".png" ? "image/png" : ext === ".webp" ? "image/webp" : "image/jpeg";
    slide.images.add({ blob, contentType, alt, fit: "cover", position });
    return true;
  } catch { return false; }
}
function slideBlocks(planSlide) { return planSlide.block_ids.map((id) => blocks.get(id)).filter(Boolean); }
function contentOnly(items) { return items.filter((b) => !b.kind.startsWith("heading")); }
function splitFeature(text) {
  const value = clean(text).replace(/^\d{1,2}[.)]\s*/, "");
  const idx = value.indexOf(":");
  return idx > 0 && idx < 58 ? [value.slice(0, idx), value.slice(idx + 1).trim()] : [value, ""];
}

const presentation = Presentation.create({ slideSize: { width: W, height: H } });
const media = (system.assets || []).filter((a) => a.kind === "hero-image" && a.path);
let mediaIndex = 0;

for (let index = 0; index < spec.plan.slides.length; index++) {
  const planSlide = spec.plan.slides[index];
  const items = slideBlocks(planSlide);
  const body = contentOnly(items);
  const slide = presentation.slides.add();
  slide.background.fill = bg;

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
    const bullets = body.filter((b) => b.kind === "bullet").slice(0, 4);
    const intro = body.find((b) => b.kind === "paragraph");
    if (intro) addText(slide, intro.text, 72, 166, 1050, 74, 18, ink, { body: true, bold: false });
    bullets.forEach((block, i) => {
      const [label, detail] = splitFeature(block.text);
      const x = 72 + i * (1100 / bullets.length);
      addText(slide, String(i + 1).padStart(2, "0"), x, 268, 70, 44, 19, accent, { body: true, bold: true });
      addText(slide, label, x, 322, 225, 78, 23, ink, { bold: true, vertical: "top" });
      addText(slide, detail, x, 416, 225, 130, 16, ink, { body: true, bold: false, vertical: "top" });
    });
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "feature-list") {
    addText(slide, planSlide.title, 72, 54, 1120, 72, 34, ink, { bold: true, name: "title" });
    const intro = body.find((b) => b.kind === "paragraph");
    if (intro) addText(slide, intro.text, 72, 145, 1080, 76, 18, ink, { body: true, bold: false });
    const bullets = body.filter((b) => b.kind === "bullet").slice(0, 6);
    bullets.forEach((block, i) => {
      const [label, detail] = splitFeature(block.text);
      const col = i % 2, row = Math.floor(i / 2);
      const x = 72 + col * 570, y = 248 + row * 124;
      addRule(slide, x, y, 500, col ? palette.accent_secondary || accent : accent, 3);
      addText(slide, label, x, y + 16, 500, 34, 20, ink, { bold: true, vertical: "top" });
      addText(slide, detail, x, y + 53, 500, 55, 15, ink, { body: true, bold: false, vertical: "top" });
    });
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "statement") {
    addText(slide, planSlide.title, 72, 54, 1120, 72, 34, ink, { bold: true, name: "title" });
    const statement = body.find((b) => b.kind === "callout") || body[0];
    addText(slide, statement?.text || "", 72, 176, 1040, 240, 38, ink, { bold: true, vertical: "top" });
    addRule(slide, 72, 470, 300, accent, 8);
    const remainder = body.filter((b) => b !== statement).map((b) => clean(b.text)).join("\n\n");
    addText(slide, remainder, 670, 470, 500, 130, 17, ink, { body: true, bold: false, vertical: "top" });
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "closing") {
    const dark = palette.brand_dark || palette.text;
    slide.background.fill = dark;
    const onDark = readable(dark, palette.background);
    addText(slide, system.name.toUpperCase(), 72, 58, 600, 28, 11, accent, { body: true, bold: true });
    addText(slide, planSlide.title, 72, 162, 1040, 150, 48, onDark, { bold: true, name: "title" });
    const copy = body.filter((b) => b.kind !== "action").map((b) => clean(b.text)).join("\n\n");
    addText(slide, copy, 72, 348, 850, 128, 21, onDark, { body: true, bold: false, vertical: "top" });
    const action = body.find((b) => b.kind === "action");
    if (action) {
      addRule(slide, 72, 552, 420, accent, 5);
      addText(slide, action.text, 72, 574, 600, 44, 18, onDark, { body: true, bold: true });
    }
  } else {
    const useMedia = mediaIndex < media.length;
    addText(slide, planSlide.title, 72, 54, useMedia ? 650 : 1120, 88, 34, ink, { bold: true, name: "title" });
    const copy = body.map((b) => clean(b.text)).join("\n\n");
    addText(slide, copy, 72, 188, useMedia ? 520 : 940, 360, 20, ink, { body: true, bold: false, vertical: "top" });
    if (useMedia) await addImage(slide, media[mediaIndex++], { left: 690, top: 0, width: 590, height: 635 }, `${system.name} brand imagery`);
    else addRule(slide, 72, 590, 500, accent, 7);
    addFooter(slide, index + 1);
  }
  slide.speakerNotes.textFrame.setText(`Generated from ${system.name} design system ${system.version || "1.0.0"}. Source block IDs: ${planSlide.block_ids.join(", ")}.`);
}

await (await PresentationFile.exportPptx(presentation)).save(outputPath);
for (let index = 0; index < presentation.slides.items.length; index++) {
  const slide = presentation.slides.items[index];
  const png = await slide.export({ format: "png", scale: 1.5 });
  await fs.writeFile(path.join(previewDir, `slide-${String(index + 1).padStart(2, "0")}.png`), new Uint8Array(await png.arrayBuffer()));
  const layout = await slide.export({ format: "layout" });
  await fs.writeFile(path.join(previewDir, `slide-${String(index + 1).padStart(2, "0")}.layout.json`), await layout.text());
}
console.log(JSON.stringify({ output: outputPath, slides: presentation.slides.items.length, previews: previewDir }));
