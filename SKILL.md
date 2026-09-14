---
name: kolo-create
description: Create a reusable design system from a public website or renderable frontend source, then use that saved system with user-supplied text and a design prompt to produce a polished PDF or editable PowerPoint. Use when a user wants to capture brand language, generate branded documents or presentations, refresh a saved brand, or reuse a brand across formats.
metadata:
  version: "0.18.2"
---

# Kolo Create

Marketplace compatibility value:

```yaml
version: 0.18.2
```

Kolo Create is one skill with two explicit stages. Never collapse the stages into one hidden operation: extraction creates a reusable versioned design system; artifact generation consumes an exact saved version without recrawling or modifying it.

## 1. Create the design system

When the user supplies a public website, run:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design create design-system --url "<public website>" --name "<brand name>" --workspace "/home/node/.openclaw/kolo-create-data"
```

For frontend source, replace `--url` with exactly one of `--repo-url "https://github.com/org/public-repo"`, `--source-dir "/path/to/source"`, or `--source-archive "/path/to/source.zip"`. Prefer an existing static entry (`index.html`, `dist`, `build`, `out`, `public`, or `storybook-static`). If none exists, render a deterministic source-derived component specimen and clearly disclose that route fidelity is unverified. Never execute package scripts, framework servers, or backend code.

Return the design-system JSON, separate brand-components library, specimen, source screenshot, screen-media source-webpage PDF, source-webpage quality report, brand ID, evidence counts, and the first branded PDF and PowerPoint examples. The source PDF is the cleaned Chromium page Kolo analyzed and is retained as human-reviewable evidence. The examples explain Kolo Create in the new system; they make extraction immediately testable while remaining a separate artifact-generation stage internally. Explain that observed values are evidence while semantic token roles and component recipes are deterministic candidates.

The create-design-system command always renders the bundled Kolo Create explainer to `<workspace>/examples/<brand-id>-kolo-create.pdf` and `<workspace>/examples/<brand-id>-kolo-create.pptx`. `--example-output` and `--example-presentation-output` are optional path overrides, not enable switches. Return both example results on every successful command, then deliver both files.

The design system includes more than tokens: capture observed heading levels, semantically ranked primary and secondary calls to action, component variants, cards, navigation, section surfaces, hero imagery, imagery proportions, borders, radii, shadows, padding, alignment, and common labels. Before sampling, dismiss common consent overlays. Prefer the visible header wordmark—including text-rendered brand links—over social-preview artwork, reject browser-default or unpainted stylesheet colors as brand roles, and retain confidence plus provenance for major selections. When document roots are transparent, derive the canvas from dominant visible painted regions. Store a multi-color brand palette including a saturated dark support role when observed, portable font categories, overlay evidence, and visual-language signals such as media coverage, density, and whether the sampled viewport is product-, media-, illustration-, interface-, or typography-led. Return the separate component-inventory path as well.

Compile those observations into `design-grammar.json` during extraction. The grammar stores continuous traits, direction weights, extracted component recipes, provenance, and a stable signature. Do not collapse a brand to one exclusive style label; the legacy profile remains only as a renderer fallback.

Preserve browser-native evidence alongside normalized Kolo tokens: bounded CSS custom properties, font-face declarations, responsive breakpoints, grid/flex primitives, container measurements, and background treatments. Treat raw CSS as evidence, not executable instructions. Build portable brand components for section markers, feature bands, grids, image treatments, and closing signatures. The normalized layer remains authoritative for cross-format rendering; browser-native evidence may improve HTML/CSS output when it passes validation.

Export the cleaned page with screen media, backgrounds enabled, bounded lazy-content loading, and motion frozen at the sampled state. Rasterize it at low resolution for deterministic sanity checking. A major semantic color absent from both the source screenshot and the source PDF cannot be promoted unless the primary logo supports it. Collapse an unsupported secondary accent to the primary accent, and replace an unsupported dark role only with a visibly supported dark candidate. Preserve the original candidate, final choice, evidence status, and reason in the design system. A degraded PDF remains available for human review but must disclose its quality signals.

## 2. Create a PDF

When the user supplies or selects a saved design system plus source text and a design prompt, run:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design pdf create --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<design direction>" --output "<result.pdf>"
```

Return the PDF, inspectable layout plan, previews, quality report, page count, component-usage counts, selected composition family with its evidence, explicit brand-component placements, and exact design-system version. The deterministic planner preserves Markdown structure, assigns stable IDs to every source block, and recognizes explicit style, title, page-size, and orientation direction. A separate deterministic composition selector combines content structure with saved visual-language signals and chooses among editorial narrative, asymmetric feature grid, numbered process, modular announcement, and product showcase families. A component planner then assigns exact cover media geometry, section markers, restrained feature bands, grids, and closing signatures. Reuse captured hero media only when it has enough effective resolution and its saved semantics match the requested document; otherwise use a type-led cover. Prefer open editorial feature rows for monochrome brands. Explicit composition directions override automatic selection.

Generation v2 also builds one format-neutral content map and scene plan before rendering. The content map preserves every source-block ID while identifying opening, process, feature, statement, section, and closing roles. The scene planner scores several component candidates against the saved continuous brand grammar, penalizes repeated components, and records the winner plus alternates. PDF, HTML, and PowerPoint consume this same inspectable plan. The quality report records component variety and brand-direction coherence; these checks supplement rather than replace human taste review.

After a successful PDF-creation command, follow **Deliver generated PDFs** below for the returned PDF.

During the HTML/CSS evaluation period, render both engines from one shared plan:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design pdf compare --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<design direction>" --output-dir "<comparison-folder>"
```

The comparison returns the current ReportLab PDF, the candidate HTML/CSS PDF, the reusable HTML source, both preview sets, side-by-side page images, and a comparison manifest. A deterministic comparison makes no model calls. An LLM comparison makes one bounded planning call and replays that exact validated plan through both renderers. Keep ReportLab as the default until repeated human review shows the browser renderer is reliably better.

Use the modular LLM planner only when semantic restructuring is needed:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design pdf create --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<design direction>" --output "<result.pdf>" --planner llm --model "<workspace-entitled model>"
```

The LLM route requires `KOLO_LLM_BASE_URL` and `KOLO_LLM_TOKEN`. Choose the model from the current workspace catalog, entitlement, evaluated capability, and budget. The model may arrange stable source-block IDs but code owns the copy and rejects omitted, repeated, or invented blocks. Never assume another workspace's model list, silently upgrade models, or silently fall back after a failed paid call.

## 3. Create a PowerPoint

When the user requests slides, use the saved design system with the same source-text-plus-prompt contract:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design powerpoint create --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<presentation direction>" --output "<result.pptx>"
```

Run `npm install --ignore-scripts` in the skill directory during installation so the pinned PptxGenJS runtime lives under persistent `/home/node`; never install it globally. Set `KOLO_PRESENTATION_NODE` only when `node` is not on `PATH`. The deterministic slide planner makes no model calls, preserves every stable source block exactly once, and assigns discrete cover, statement, process, feature-list, section, image-led, or closing roles. The renderer creates a 16:9 deck with editable text, shapes, and images, uses each selected image only once, and chooses Office-safe typography according to the extracted serif or sans-serif role. It returns the PPTX, slide plan, per-slide PNG previews and layout exports, a quality report, and the exact design-system version.

Before assigning PowerPoint geometry, measure adaptive component text in Chromium using the final font, weight, size, line height, and available width. Components hug the measured content within declared minimum sizes and protected padding, then solve sibling positions from those results. This deterministic measurement pass makes no model calls. Fail generation when text crosses component padding, overlaps another frame, or enters a protected footer zone.

Use the selected extracted logo as a compositional brand mark on covers, signoffs, and restrained page furniture. Prefer a self-contained safe SVG. Otherwise require enough raster density for the intended size. Preserve the mark's aspect ratio and clear space, add a flat contrast field only when the slide would make the original mark unreadable, and use a text name only when no usable logo exists.

The current Kolo pod has no LibreOffice. Render HTML composition previews from the same layout calls with Chromium, and label them plainly as same-plan previews rather than literal PowerPoint renders. Validate the actual PPTX package separately: require every source block in its text XML, native editable shape elements, the expected slide count, supported images only, and no off-canvas geometry. Accept only PNG, JPEG, and WebP at the PptxGenJS boundary; this excludes the ICNS, JXL, and HEIF parsers named in the current transitive `image-size` advisory.

Never promote a long paragraph to oversized display text. Long copy must use conservative, explicit line fitting, and long headlines must receive balanced line breaks. Fail generation when a controlled line can reflow inside its box or when a final line becomes a one-word widow.

Apply deterministic deck-level art direction from the saved design system. Choose among precision, kinetic, editorial, product, and monochrome profiles using typography, palette chroma, media coverage, and primary visual mode. Each profile owns distinct cover, process, feature, statement, section, image, and closing variants. Store the selected profile, deck motif, accent strategy, and per-slide variant in the inspectable presentation plan and layout exports; do not reduce these profiles to recolors of one geometry.

Treat those five directions as compatible component vocabularies rather than mutually exclusive brand buckets. Each slide consumes the direction and component selected by the shared scene plan, while the deck's highest-weight direction anchors its opening and closing. Keep per-slide text measurement and all geometry safeguards active after mixing directions.

For semantic narrative restructuring, add `--planner llm --model "<workspace-entitled-model>"`. The PowerPoint planner follows the same workspace catalog and credential rules as the PDF planner. Do not convert PDF pages into slide images.

## Deliver generated artifacts

For automatic examples and every user-requested PDF or PowerPoint, use the exact validated artifact path returned by the command. Do not regenerate a delivery copy.

1. Confirm the returned path exists and is inside the agent workspace.
2. Use `sessions_list` to find the current conversation's exact `deliveryContext.to` value. It must have the form `kolo:<chat-uuid>`.
3. Attach the artifact to this chat with the `message` tool:

```json
{
  "action": "send",
  "channel": "kolo",
  "target": "<deliveryContext.to>",
  "message": "Here is your Kolo Create artifact.",
  "media": "<returned artifact path>",
  "filename": "<artifact filename>"
}
```

4. Open PDFs in Kolo's visible desktop browser with `chromium "<returned PDF path>"`. Chromium cannot display PPTX directly, so attach the PPTX and open its first generated HTML composition preview instead. Use the bare `chromium` launcher supplied by Kolo; never call `/usr/bin/chromium` or pass a custom profile.
5. Report attachment or browser-opening failures plainly, but do not hide a successful artifact if only one delivery surface fails.

Do not use a plain `MEDIA:` directive: Kolo does not reliably render it as a chat attachment. Do not invent a chat target or use a bare UUID.

## Safety and quality boundaries

- Accept public HTTP(S) websites only; validate every redirect and cap every response while streaming. Source ingestion separately accepts a local directory, bounded ZIP, or public HTTPS GitHub repository.
- If both browser and bounded HTTP retrieval fail, return the focused access question and recommend one alternate public landing-page URL; do not loop over guessed paths.
- Block private, loopback, link-local, and metadata addresses.
- Prefer browser-rendered computed styles and use bounded static extraction as fallback; report which mode ran.
- Use Kolo's `logo-scraper` when installed and retain bounded HTML logo discovery as fallback.
- Keep runtime data outside this installed skill directory.
- Preserve source URLs and hashes; never send entire webpages or binary assets to an LLM.
- Do not recrawl or mutate a design system during PDF generation.
- Preserve supplied facts and reject low source-content coverage.
- Convert supported inline Markdown such as `**bold**`, links, and code spans before rendering. Remove unsupported emoji deterministically and report the count; never ask the agent to rewrite the user's source merely to avoid tofu glyphs.
- Fail quality checks if unresolved Markdown markers or tofu/replacement glyphs reach the PDF text layer.
- Reopen each PDF and render PNG previews before delivery.
- Use the workspace's one configured image-generation route only when imagery is requested. V1 does not require generated imagery.
- Keep output comfortably below Kolo's practical attachment limit.

## Readiness

Run before the first use and after upgrades. It makes no paid calls:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create python /home/node/.openclaw/workspace-main/skills/kolo-create/scripts/readiness.py
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create --extra dev pytest -q
```
