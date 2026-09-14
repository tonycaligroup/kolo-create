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
const profile = spec.plan.art_direction?.profile || "precision";
const secondary = palette.accent_secondary || accent;
const surface = palette.surface || background;
const dark = palette.brand_dark || palette.text;

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
  const cleaned = clean(text);
  const value = options.body && cleaned.length > 100 ? balancedCopy(cleaned, width, size) : cleaned;
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
function addRect(slide, x, y, width, height, fill, radius = 0, line = null, options = {}) {
  slide.addShape(radius ? pptx.ShapeType.roundRect : pptx.ShapeType.rect, {
    x: inch(x), y: inch(y), w: inch(width), h: inch(height),
    fill: { color: noHash(fill) },
    line: line ? { color: noHash(line), width: 1 } : { color: noHash(fill), transparency: 100 },
    objectName: options.name,
    shadow: options.shadow ? {
      type: "outer", color: noHash(dark), opacity: .12, blur: 4, angle: 45, distance: 2,
    } : undefined,
  });
  const shadow = options.shadow ? `;box-shadow:0 6px 18px rgba(0,15,30,.12)` : "";
  preview(slide, `<div style="position:absolute;left:${x}px;top:${y}px;width:${width}px;height:${height}px;background:${fill};border-radius:${radius}px${shadow}"></div>`);
}
function addOutline(slide, x, y, width, height, color, stroke = 2, radius = 0) {
  slide.addShape(radius ? pptx.ShapeType.roundRect : pptx.ShapeType.rect, {
    x: inch(x), y: inch(y), w: inch(width), h: inch(height),
    fill: { color: noHash(background), transparency: 100 }, line: { color: noHash(color), width: stroke },
  });
  preview(slide, `<div style="position:absolute;left:${x}px;top:${y}px;width:${width}px;height:${height}px;border:${stroke}px solid ${color};border-radius:${radius}px"></div>`);
}
function addRule(slide, x, y, width, color = accent, height = 4, options = {}) {
  addRect(slide, x, y, width, height, color, 0, null, options);
}

async function prepareLogo(asset) {
  if (!asset?.path) return null;
  try {
    await fs.access(asset.path);
    const suffix = path.extname(asset.path).toLowerCase();
    let width = Number(asset.pixel_width || asset.css_width || 0);
    let height = Number(asset.pixel_height || asset.css_height || 0);
    if (suffix === ".svg") {
      const source = await fs.readFile(asset.path, "utf8");
      const viewBox = source.match(/viewBox=["']\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)\s*["']/i);
      const explicitWidth = source.match(/<svg[^>]*\bwidth=["']([\d.]+)/i);
      const explicitHeight = source.match(/<svg[^>]*\bheight=["']([\d.]+)/i);
      width = Number(viewBox?.[1] || explicitWidth?.[1] || width || 3);
      height = Number(viewBox?.[2] || explicitHeight?.[1] || height || 1);
    }
    if (!width || !height) { width = 3; height = 1; }
    return { asset, suffix, width, height, aspect: width / height };
  } catch { return null; }
}

const preparedLogo = await prepareLogo(system.presentation_logo);
function addBrandMark(slide, position, fieldColor, options = {}) {
  if (!preparedLogo) {
    addText(slide, system.name.toUpperCase(), position.left, position.top, position.width, position.height, options.size || 11,
      options.color || readable(fieldColor, palette.text), { body: true, bold: true, name: options.name || "brand-label" });
    return false;
  }
  const markLuminance = Number(preparedLogo.asset.visual_luminance);
  const fieldLuminance = lum(fieldColor);
  const markContrast = Number.isFinite(markLuminance)
    ? (Math.max(markLuminance, fieldLuminance) + .05) / (Math.min(markLuminance, fieldLuminance) + .05)
    : 0;
  const needsContrastField = options.contrastField ?? markContrast < 2.2;
  const paddingX = needsContrastField ? 12 : 0;
  const paddingY = needsContrastField ? 7 : 0;
  if (needsContrastField) addRect(slide, position.left, position.top, position.width, position.height, background, 0);
  const availableWidth = position.width - paddingX * 2;
  const availableHeight = position.height - paddingY * 2;
  let width = availableWidth;
  let height = width / preparedLogo.aspect;
  if (height > availableHeight) { height = availableHeight; width = height * preparedLogo.aspect; }
  const left = position.left + paddingX;
  const top = position.top + (position.height - height) / 2;
  slide.addImage({
    path: preparedLogo.asset.path, altText: `${system.name} logo`, objectName: options.name || "brand-logo",
    x: inch(left), y: inch(top), w: inch(width), h: inch(height), sizing: { type: "contain", w: inch(width), h: inch(height) },
  });
  preview(slide, `<img alt="${escapeHtml(system.name)} logo" src="${pathToFileURL(preparedLogo.asset.path).href}" style="position:absolute;left:${left}px;top:${top}px;width:${width}px;height:${height}px;object-fit:contain;object-position:left center">`);
  return true;
}
function addFooter(slide, index, color = ink) {
  addBrandMark(slide, { left: 72, top: 660, width: 128, height: 32 }, previews.get(slide).background,
    { color, size: 10, name: "brand-footer" });
  addText(slide, String(index).padStart(2, "0"), 1160, 668, 48, 24, 10, color, { body: true, align: "right", name: "slide-number" });
}
async function addImage(slide, asset, position, alt) {
  if (!asset?.path) return false;
  const suffix = path.extname(asset.path).toLowerCase();
  if (![".png", ".jpg", ".jpeg", ".webp"].includes(suffix)) return false;
  try {
    await fs.access(asset.path);
    const sourceWidth = Number(asset.pixel_width);
    const sourceHeight = Number(asset.pixel_height);
    const hasIntrinsicSize = sourceWidth > 0 && sourceHeight > 0;
    const naturalWidth = hasIntrinsicSize ? position.width : position.width;
    const naturalHeight = hasIntrinsicSize ? naturalWidth * sourceHeight / sourceWidth : position.height;
    slide.addImage({
      path: asset.path, altText: alt, objectName: "brand-image",
      x: inch(position.left), y: inch(position.top), w: inch(naturalWidth), h: inch(naturalHeight),
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
function balancedHeadline(text) {
  const value = clean(text);
  if (value.includes("\n") || value.length < 32) return value;
  const words = value.split(/\s+/);
  let best = 1, bestScore = Number.POSITIVE_INFINITY;
  for (let index = 2; index <= words.length - 2; index++) {
    const left = words.slice(0, index).join(" ");
    const right = words.slice(index).join(" ");
    const score = Math.abs(left.length - right.length);
    if (score < bestScore) { best = index; bestScore = score; }
  }
  return `${words.slice(0, best).join(" ")}\n${words.slice(best).join(" ")}`;
}
function balancedCopy(text, width, size) {
  const value = clean(text);
  const words = value.split(/\s+/);
  const averageCharacterWidth = size * 96 / 72 * .65;
  const capacity = Math.max(18, Math.floor(width / averageCharacterWidth));
  const lines = [];
  let line = [];
  for (const word of words) {
    const candidate = [...line, word].join(" ");
    if (line.length && candidate.length > capacity) {
      lines.push(line);
      line = [word];
    } else {
      line.push(word);
    }
  }
  if (line.length) lines.push(line);
  while (
    lines.length > 1
    && (lines.at(-1).length < 2 || lines.at(-1).join(" ").length < capacity * .35)
    && lines.at(-2).length > 2
  ) {
    lines.at(-1).unshift(lines.at(-2).pop());
  }
  if (lines.length > 2 && lines.at(-1).length === 1) {
    for (let source = lines.length - 2; source >= 0; source--) {
      if (lines[source].length <= 2) continue;
      for (let target = source; target < lines.length - 1; target++) {
        lines[target + 1].unshift(lines[target].pop());
      }
      break;
    }
  }
  return lines.map((parts) => parts.join(" ")).join("\n");
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
    if (profile === "editorial") {
      const field = contrast(accent, palette.text) >= 4.5 ? accent : background;
      const onField = readable(field, palette.text);
      prepareSlide(slide, field);
      addBrandMark(slide, { left: 72, top: 44, width: 190, height: 54 }, field, { color: onField, name: "brand-label" });
      addText(slide, "01", 1090, 40, 120, 70, 38, secondary, { body: true, bold: true, align: "right" });
      addText(slide, balancedHeadline(planSlide.title), 72, 150, 940, 210, 60, onField, { bold: true, name: "title" });
      addRule(slide, 72, 405, 1080, onField, 2);
      addText(slide, planSlide.subtitle, 420, 456, 730, 140, 21, onField, { body: true, bold: false, name: "subtitle", vertical: "top" });
    } else if (profile === "product") {
      prepareSlide(slide, surface);
      addRect(slide, 0, 0, 34, 720, accent);
      addBrandMark(slide, { left: 76, top: 42, width: 190, height: 54 }, surface, { color: secondary, name: "brand-label" });
      addText(slide, balancedHeadline(planSlide.title), 76, 142, 590, 205, 52, ink, { bold: true, name: "title" });
      addText(slide, planSlide.subtitle, 76, 405, 540, 150, 20, ink, { body: true, name: "subtitle", vertical: "top" });
      addRect(slide, 720, 74, 470, 550, background, 18);
      if (useMedia) await addImage(slide, media[mediaIndex++], { left: 744, top: 98, width: 422, height: 502 }, `${system.name} brand imagery`);
      else {
        addText(slide, "DESIGN\nSYSTEM", 770, 200, 360, 160, 34, secondary, { bold: true });
        addRule(slide, 770, 410, 260, accent, 14);
      }
    } else if (profile === "monochrome") {
      const field = dark;
      const onField = readable(field, "#FFFFFF");
      prepareSlide(slide, field);
      addOutline(slide, 48, 42, 1184, 636, onField, 2);
      addBrandMark(slide, { left: 82, top: 54, width: 190, height: 52 }, field, { color: onField, name: "brand-label" });
      addText(slide, balancedHeadline(planSlide.title), 82, 150, useMedia ? 580 : 1000, 210, 54, onField, { bold: true, name: "title" });
      addText(slide, planSlide.subtitle, 82, 420, useMedia ? 520 : 760, 135, 20, onField, { body: true, name: "subtitle", vertical: "top" });
      if (useMedia) {
        addOutline(slide, 742, 102, 424, 474, onField, 2);
        await addImage(slide, media[mediaIndex++], { left: 758, top: 118, width: 392, height: 442 }, `${system.name} brand imagery`);
      }
    } else if (profile === "kinetic" && useMedia) {
      await addImage(slide, media[mediaIndex++], { left: 700, top: 0, width: 580, height: 720 }, `${system.name} brand imagery`);
      addBrandMark(slide, { left: 72, top: 44, width: 200, height: 56 }, background, { color: accent, name: "brand-label", contrastField: false });
      addText(slide, planSlide.title, 72, 140, 560, 210, 48, ink, { bold: true, name: "title" });
      addText(slide, planSlide.subtitle, 72, 385, 540, 145, 22, ink, { body: true, bold: false, name: "subtitle" });
      addRule(slide, 72, 610, 190, accent, 8);
      addRule(slide, 280, 610, 96, secondary, 8);
    } else {
      addText(slide, "01", 1010, 106, 180, 120, 72, surface, { body: true, bold: true, align: "right" });
      addBrandMark(slide, { left: 72, top: 48, width: 200, height: 56 }, background, { color: accent, name: "brand-label", contrastField: false });
      addText(slide, planSlide.title, 72, 182, 880, 165, 56, ink, { bold: true, name: "title" });
      addText(slide, planSlide.subtitle, 330, 404, 790, 120, 23, ink, { body: true, bold: false, name: "subtitle" });
      addRule(slide, 72, 594, 330, accent, 10);
      addRule(slide, 420, 594, 110, secondary, 10);
    }
  } else if (planSlide.archetype === "process") {
    const bullets = body.filter((block) => block.kind === "bullet").slice(0, 4);
    const intro = body.find((block) => block.kind === "paragraph");
    addText(slide, planSlide.title, 72, 54, 1120, 72, profile === "editorial" ? 38 : 34, ink, { bold: true, name: "title" });
    if (profile === "editorial") {
      if (intro) addText(slide, intro.text, 72, 150, 420, 180, 18, ink, { body: true, vertical: "top" });
      addRule(slide, 540, 150, 3, accent, 430);
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const y = 150 + itemIndex * 142;
        addText(slide, String(itemIndex + 1).padStart(2, "0"), 585, y, 64, 40, 18, secondary, { body: true, bold: true });
        addText(slide, label, 670, y, 470, 42, 23, ink, { bold: true, vertical: "top" });
        addText(slide, detail, 670, y + 48, 470, 68, 15, ink, { body: true, vertical: "top" });
      });
    } else if (profile === "product") {
      if (intro) addText(slide, intro.text, 72, 142, 1000, 78, 17, ink, { body: true, vertical: "top" });
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const x = 72 + itemIndex * 365;
        addRect(slide, x, 250, 330, 310, surface, 16);
        addRect(slide, x, 250, 330, 18, itemIndex === 1 ? secondary : accent, 8);
        addText(slide, String(itemIndex + 1).padStart(2, "0"), x + 24, 292, 70, 38, 18, secondary, { body: true, bold: true });
        addText(slide, label, x + 24, 350, 280, 80, 22, ink, { bold: true, vertical: "top" });
        addText(slide, detail, x + 24, 446, 280, 88, 15, ink, { body: true, vertical: "top" });
      });
    } else if (profile === "kinetic") {
      addRect(slide, 0, 142, 1280, 112, dark);
      if (intro) addText(slide, intro.text, 72, 162, 1080, 72, 18, readable(dark, "#FFFFFF"), { body: true, vertical: "top" });
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const x = 72 + itemIndex * 370;
        addText(slide, String(itemIndex + 1).padStart(2, "0"), x, 282, 100, 75, 38, itemIndex === 1 ? secondary : accent, { body: true, bold: true });
        addText(slide, label, x, 370, 315, 72, 22, ink, { bold: true, vertical: "top" });
        addText(slide, detail, x, 466, 315, 90, 15, ink, { body: true, vertical: "top" });
      });
    } else if (profile === "monochrome") {
      if (intro) addText(slide, intro.text, 72, 142, 1030, 80, 17, ink, { body: true, vertical: "top" });
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const x = 72 + itemIndex * 372;
        const filled = itemIndex === 1; const fill = filled ? dark : background; const color = readable(fill, ink);
        if (filled) addRect(slide, x, 256, 332, 300, fill); else addOutline(slide, x, 256, 332, 300, ink, 2);
        addText(slide, `0${itemIndex + 1}`, x + 24, 280, 70, 42, 18, color, { body: true, bold: true });
        addText(slide, label, x + 24, 350, 280, 72, 22, color, { bold: true, vertical: "top" });
        addText(slide, detail, x + 24, 448, 280, 84, 15, color, { body: true, vertical: "top" });
      });
    } else {
      addRule(slide, 72, 143, 1120, accent, 3);
      if (intro) addText(slide, intro.text, 72, 166, 1050, 74, 18, ink, { body: true, bold: false });
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text);
        const x = 72 + itemIndex * (1100 / bullets.length);
        addText(slide, String(itemIndex + 1).padStart(2, "0"), x, 268, 70, 44, 19, accent, { body: true, bold: true });
        addText(slide, label, x, 322, 225, 78, 23, ink, { bold: true, vertical: "top" });
        addText(slide, detail, x, 416, 225, 130, 16, ink, { body: true, bold: false, vertical: "top" });
      });
    }
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "feature-list") {
    if (profile === "kinetic") prepareSlide(slide, surface);
    addText(slide, planSlide.title, 72, 54, 1120, 72, 34, ink, { bold: true, name: "title" });
    const intro = body.find((block) => block.kind === "paragraph");
    if (intro) addText(slide, intro.text, 72, 145, 1080, 76, 18, ink, { body: true, bold: false });
    const bullets = body.filter((block) => block.kind === "bullet").slice(0, 6);
    const alternate = planSlide.variant?.endsWith("-alternate");
    if (alternate && profile === "precision") {
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const y = 238 + itemIndex * 86;
        addText(slide, `0${itemIndex + 1}`, 72, y, 52, 30, 13, accent, { body: true, bold: true });
        addText(slide, label, 160, y, 330, 34, 19, ink, { bold: true, vertical: "top" });
        addText(slide, detail, 540, y, 590, 48, 14, ink, { body: true, vertical: "top" });
        addRule(slide, 160, y + 58, 970, accent, 2);
      });
    } else if (alternate && ["editorial", "product"].includes(profile)) {
      const measuredCards = new Map(
        (spec.plan.layout_measurements?.slides?.[planSlide.id]?.cards || [])
          .map(card => [Number(card.index), card])
      );
      const productLayouts = bullets.map((block, itemIndex) => {
        const measured = measuredCards.get(itemIndex) || {};
        const labelHeight = Math.max(30, Number(measured.label_height || 0) + 4);
        const detailHeight = Math.max(22, Number(measured.detail_height || 0) + 4);
        const detailOffset = 18 + labelHeight + 10;
        return { labelHeight, detailHeight, detailOffset, height: Math.max(132, detailOffset + detailHeight + 18) };
      });
      const rowHeights = [0, 1, 2].map(row => Math.max(0, ...productLayouts
        .filter((_, itemIndex) => Math.floor(itemIndex / 2) === row).map(layout => layout.height)));
      const rowTops = [242];
      for (let row = 1; row < rowHeights.length; row++) rowTops[row] = rowTops[row - 1] + rowHeights[row - 1] + 18;
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const column = itemIndex % 2, row = Math.floor(itemIndex / 2);
        const layout = productLayouts[itemIndex];
        const x = 72 + column * 570, y = profile === "product" ? rowTops[row] : 242 + row * 158;
        const height = profile === "product" ? layout.height : 132;
        if (profile === "product") addRect(slide, x, y, 520, height, surface, 14, null, { name: `feature-card-${itemIndex + 1}` });
        else addOutline(slide, x, y, 520, height, accent, 2);
        addText(slide, `0${itemIndex + 1}`, x + 20, y + 18, 48, 28, 12, secondary, { body: true, bold: true });
        addText(slide, label, x + 82, y + 18, 400, profile === "product" ? layout.labelHeight : 34, 19, ink,
          { bold: true, vertical: "top", name: profile === "product" ? `feature-card-title-${itemIndex + 1}` : undefined });
        addText(slide, detail, x + 82, y + (profile === "product" ? layout.detailOffset : 62), 400,
          profile === "product" ? layout.detailHeight : 50, 14, ink,
          { body: true, vertical: "top", name: profile === "product" ? `feature-card-copy-${itemIndex + 1}` : undefined });
      });
    } else if (profile === "editorial") {
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const y = 246 + itemIndex * 94;
        addText(slide, `0${itemIndex + 1}`, 72, y, 52, 32, 14, secondary, { body: true, bold: true });
        addText(slide, label, 160, y, 330, 34, 20, ink, { bold: true, vertical: "top" });
        addText(slide, detail, 520, y, 610, 52, 15, ink, { body: true, vertical: "top" });
        addRule(slide, 160, y + 66, 970, itemIndex % 2 ? secondary : accent, 2);
      });
    } else if (profile === "product") {
      const measuredCards = new Map(
        (spec.plan.layout_measurements?.slides?.[planSlide.id]?.cards || [])
          .map(card => [Number(card.index), card])
      );
      const cardLayouts = bullets.map((block, itemIndex) => {
        const measured = measuredCards.get(itemIndex) || {};
        const labelHeight = Math.max(30, Number(measured.label_height || 0) + 4);
        const detailHeight = Math.max(22, Number(measured.detail_height || 0) + 4);
        const detailOffset = 20 + labelHeight + 10;
        return { labelHeight, detailHeight, detailOffset, height: Math.max(140, detailOffset + detailHeight + 20) };
      });
      const rowHeights = [0, 1].map(row => Math.max(0, ...cardLayouts
        .filter((_, itemIndex) => Math.floor(itemIndex / 3) === row).map(layout => layout.height)));
      const rowTops = [246, 246 + rowHeights[0] + 18];
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const column = itemIndex % 3, row = Math.floor(itemIndex / 3);
        const layout = cardLayouts[itemIndex];
        const x = 72 + column * 365, y = rowTops[row];
        addRect(slide, x, y, 330, layout.height, surface, 14, null, { name: `feature-card-${itemIndex + 1}` });
        addRect(slide, x, y, 18, layout.height, column === 1 ? secondary : accent, 7);
        addText(slide, label, x + 40, y + 20, 260, layout.labelHeight, 19, ink,
          { bold: true, vertical: "top", name: `feature-card-title-${itemIndex + 1}` });
        addText(slide, detail, x + 40, y + layout.detailOffset, 260, layout.detailHeight, 14, ink,
          { body: true, vertical: "top", name: `feature-card-copy-${itemIndex + 1}` });
      });
    } else if (profile === "kinetic" && bullets.length >= 3) {
      const measuredCards = new Map(
        (planSlide && spec.plan.layout_measurements?.slides?.[planSlide.id]?.cards || [])
          .map(card => [Number(card.index), card])
      );
      const cardLayouts = bullets.map((block, itemIndex) => {
        const lead = itemIndex === 0;
        const measured = measuredCards.get(itemIndex) || {};
        const labelHeight = Math.max(lead ? 36 : 24, Number(measured.label_height || 0) + 4);
        const detailHeight = Math.max(lead ? 25 : 19, Number(measured.detail_height || 0) + 4);
        const labelOffset = lead ? 58 : alternate ? 45 : 58;
        const detailOffset = lead
          ? Math.max(152, labelOffset + labelHeight + 16)
          : labelOffset + labelHeight + (alternate ? 8 : 10);
        const minimumHeight = lead ? (alternate ? 424 : 310) : (alternate ? 120 : 148);
        const bottomPadding = lead ? 24 : alternate ? 16 : 24;
        return {
          lead, labelHeight, detailHeight, labelOffset, detailOffset,
          height: Math.max(minimumHeight, detailOffset + detailHeight + bottomPadding),
        };
      });
      const smallCards = cardLayouts.slice(1);
      const regionTop = alternate ? 190 : 246;
      const regionHeight = alternate ? 424 : 360;
      const smallHeight = smallCards.reduce((total, card) => total + card.height, 0);
      const smallGap = smallCards.length > 1
        ? Math.max(12, Math.min(20, Math.floor((regionHeight - smallHeight) / (smallCards.length - 1))))
        : 0;
      let smallTop = regionTop;
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text);
        const layout = cardLayouts[itemIndex];
        const lead = layout.lead;
        const x = lead ? (alternate ? 738 : 72) : (alternate ? 72 : 600);
        const y = lead ? regionTop : smallTop;
        const width = lead ? 470 : 590;
        const height = layout.height;
        const fill = lead ? dark : background; const color = readable(fill, ink);
        const inset = 32;
        const ruleTop = y + (alternate && !lead ? 20 : 24);
        const labelTop = y + layout.labelOffset;
        const detailTop = y + layout.detailOffset;
        addRect(slide, x, y, width, height, fill, 8, null,
          { shadow: true, name: `feature-card-${itemIndex + 1}` });
        addRule(slide, x + inset, ruleTop, lead ? 150 : 110, itemIndex % 2 ? secondary : accent, 5);
        addText(slide, label, x + inset, labelTop, width - inset * 2, layout.labelHeight, lead ? 23 : 19, color,
          { bold: true, vertical: "top", name: `feature-card-title-${itemIndex + 1}` });
        addText(slide, detail, x + inset, detailTop, width - inset * 2, layout.detailHeight, lead ? 16 : 12, color,
          { body: true, vertical: "top", name: `feature-card-copy-${itemIndex + 1}` });
        if (!lead) smallTop += height + smallGap;
      });
    } else if (profile === "monochrome") {
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text); const column = itemIndex % 2, row = Math.floor(itemIndex / 2);
        const x = 72 + column * 570, y = 246 + row * 138; const fill = (itemIndex + row + (alternate ? 1 : 0)) % 2 ? dark : background; const color = readable(fill, ink);
        if (fill === background) addOutline(slide, x, y, 520, 112, ink, 2); else addRect(slide, x, y, 520, 112, fill);
        addText(slide, label, x + 22, y + 18, 230, 36, 19, color, { bold: true, vertical: "top" });
        addText(slide, detail, x + 250, y + 18, 245, 70, 14, color, { body: true, vertical: "top" });
      });
    } else {
      bullets.forEach((block, itemIndex) => {
        const [label, detail] = splitFeature(block.text);
        const column = itemIndex % 2, row = Math.floor(itemIndex / 2);
        const x = 72 + column * 570, y = 248 + row * 124;
        addRule(slide, x, y, 500, column ? secondary : accent, 3);
        addText(slide, label, x, y + 16, 500, 34, 20, ink, { bold: true, vertical: "top" });
        addText(slide, detail, x, y + 53, 500, 55, 15, ink, { body: true, bold: false, vertical: "top" });
      });
    }
    addFooter(slide, index + 1);
  } else if (planSlide.archetype === "statement") {
    const statement = body.find((block) => block.kind === "callout") || body[0];
    const remainder = body.filter((block) => block !== statement).map((block) => clean(block.text)).join("\n\n");
    if (profile === "editorial") {
      const field = contrast(accent, palette.text) >= 4.5 ? accent : background; const onField = readable(field, palette.text);
      prepareSlide(slide, field);
      addBrandMark(slide, { left: 72, top: 44, width: 190, height: 52 }, field, { color: onField, name: "brand-label" });
      addText(slide, planSlide.title, 72, 118, 1060, 90, 40, onField, { bold: true, name: "title" });
      addText(slide, statement?.text || "", 72, 250, 760, 260, 23, onField, { body: true, bold: true, vertical: "top", name: "primary-copy" });
      addRule(slide, 900, 220, 250, onField, 2, { name: "supporting-rule" });
      addText(slide, remainder, 900, 250, 250, 246, 15, onField, { body: true, vertical: "top", name: "supporting-copy" });
      addFooter(slide, index + 1, onField);
    } else if (profile === "product") {
      addText(slide, planSlide.title, 72, 58, 1060, 90, 40, ink, { bold: true, name: "title" });
      addRect(slide, 72, 188, 1110, 386, surface, 18);
      addRect(slide, 72, 188, 26, 386, accent, 12);
      addText(slide, statement?.text || "", 136, 238, 650, 250, 21, ink, { body: true, bold: true, vertical: "top", name: "primary-copy" });
      addRule(slide, 830, 210, 290, secondary, 4, { name: "supporting-rule" });
      addText(slide, remainder, 830, 238, 290, 246, 15, ink, { body: true, vertical: "top", name: "supporting-copy" });
      addFooter(slide, index + 1);
    } else if (profile === "monochrome") {
      const field = dark; const onField = readable(field, "#FFFFFF"); prepareSlide(slide, field);
      addText(slide, planSlide.title, 72, 62, 1040, 96, 42, onField, { bold: true, name: "title" });
      addOutline(slide, 72, 190, 1110, 390, onField, 2);
      addText(slide, statement?.text || "", 112, 232, 650, 270, 21, onField, { body: true, bold: true, vertical: "top", name: "primary-copy" });
      addText(slide, remainder, 840, 232, 290, 268, 15, onField, { body: true, vertical: "top", name: "supporting-copy" });
      addFooter(slide, index + 1, onField);
    } else if (profile === "kinetic") {
      addRect(slide, 0, 0, 330, 720, dark);
      addText(slide, "05", 68, 80, 190, 110, 62, accent, { body: true, bold: true });
      addText(slide, planSlide.title, 390, 58, 800, 96, 42, ink, { bold: true, name: "title" });
      addText(slide, statement?.text || "", 390, 198, 430, 390, 20, ink, { body: true, bold: true, vertical: "top", name: "primary-copy" });
      addRule(slide, 875, 198, 4, secondary, 390);
      addText(slide, remainder, 920, 198, 260, 330, 15, ink, { body: true, vertical: "top", name: "supporting-copy" });
      addFooter(slide, index + 1);
    } else {
      addText(slide, planSlide.title, 72, 62, 1060, 92, 42, ink, { bold: true, name: "title" });
      addRule(slide, 72, 184, 210, accent, 7);
      addText(slide, statement?.text || "", 72, 234, 650, 292, 21, ink, { body: true, bold: true, vertical: "top", name: "primary-copy" });
      addRule(slide, 800, 204, 380, secondary, 3, { name: "supporting-rule" });
      addText(slide, remainder, 800, 234, 380, 296, 16, ink, { body: true, bold: false, vertical: "top", name: "supporting-copy" });
      addFooter(slide, index + 1);
    }
  } else if (planSlide.archetype === "closing") {
    const copy = body.filter((block) => block.kind !== "action").map((block) => clean(block.text)).join("\n\n");
    const action = body.find((block) => block.kind === "action");
    const field = profile === "editorial" && contrast(accent, palette.text) >= 4.5 ? accent
      : profile === "product" && contrast(secondary, "#FFFFFF") >= 4.5 ? secondary : dark;
    const onField = readable(field, palette.background);
    prepareSlide(slide, field);
    if (profile === "product") {
      addRect(slide, 0, 0, 28, 720, accent);
      addRect(slide, 1000, 64, 200, 112, accent, 12);
      addText(slide, "READY", 1020, 84, 160, 70, 24, readable(accent, palette.text), { body: true, bold: true, align: "center" });
    } else if (profile === "editorial") {
      addText(slide, "FIN", 1050, 50, 140, 52, 28, secondary, { body: true, bold: true, align: "right" });
      addRule(slide, 72, 112, 1070, onField, 2);
    } else if (profile === "monochrome") {
      addOutline(slide, 48, 42, 1184, 636, onField, 2);
    } else if (profile === "kinetic") {
      addRect(slide, 0, 0, 22, 720, accent);
      addRect(slide, 22, 0, 12, 720, secondary);
    }
    const left = profile === "monochrome" ? 82 : 72;
    addBrandMark(slide, { left, top: 48, width: 210, height: 58 }, field,
      { color: profile === "editorial" ? onField : accent, name: "brand-label" });
    addText(slide, balancedHeadline(planSlide.title), left, 150, profile === "product" ? 850 : 1040, 190, 44, onField, { bold: true, name: "title" });
    addText(slide, copy, left, 348, 850, 128, 21, onField, { body: true, bold: false, vertical: "top", name: "closing-copy" });
    if (action) {
      addRule(slide, left, 552, 420, profile === "editorial" ? onField : accent, 5);
      addText(slide, action.text, left, 574, 600, 44, 18, onField, { body: true, bold: true });
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
    artDirection: spec.plan.art_direction,
    variant: spec.plan.slides[previewIndex - 1].variant,
    previewKind: "same-plan-html-composition-preview",
  }, null, 2));
}
console.log(JSON.stringify({ output: outputPath, slides: spec.plan.slides.length, previews: previewDir }));
