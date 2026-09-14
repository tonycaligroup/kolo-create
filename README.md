# Kolo Create

Kolo Create is one Kolo skill with two reusable stages:

1. Turn a public website or renderable frontend source into a versioned design system.
2. Turn source text plus a design prompt into a verified PDF or editable PowerPoint using that exact design-system version.

The second stage never recrawls or mutates the brand. This keeps artifacts reproducible while presenting one coherent skill to the user.

## Quick start

```sh
uv sync --frozen
npm install --ignore-scripts

uv run kolo-design create design-system \
  --url "https://kolo.ai" \
  --name "Kolo" \
  --workspace "./data"

# Or ingest a frontend without executing its build or backend:
uv run kolo-design create design-system \
  --source-dir "../my-site" \
  --name "My Site" \
  --workspace "./data"

uv run kolo-design pdf create \
  --system "./data/brands/kolo/latest.json" \
  --content "./tests/fixtures/content.md" \
  --prompt "Create a bold executive brief called Kolo Create" \
  --output "./output/kolo-create.pdf"

uv run kolo-design pdf compare \
  --system "./data/brands/kolo/latest.json" \
  --content "./tests/fixtures/content.md" \
  --prompt "Create a bold executive brief called Kolo Create" \
  --output-dir "./output/kolo-create-comparison"

uv run kolo-design powerpoint create \
  --system "./data/brands/kolo/latest.json" \
  --content "./tests/fixtures/content.md" \
  --prompt "Create a concise brand presentation" \
  --output "./output/kolo-create.pptx"
```

PowerPoint generation uses PptxGenJS 4.0.1 installed locally in the skill directory. It creates native editable text, shapes, and images. Extracted brand marks replace generic name labels on covers, signoffs, and page furniture when they pass vector-safety or raster-density checks; text is the fallback only when no usable logo exists. Because Kolo's current pod does not include LibreOffice, Chromium renders a same-plan HTML composition preview while code separately checks the real PPTX package for source-text coverage, editable shapes, slide count, and off-canvas geometry. The preview is not represented as a literal PowerPoint render. `KOLO_PRESENTATION_NODE` is optional when `node` is not on `PATH`.

PDF quality evidence is written beside the document as `<stem>.quality.json`; PowerPoint evidence uses `<stem>.presentation-quality.json`, so generating both formats with the same stem cannot overwrite either report.

PptxGenJS currently brings an `image-size` advisory affecting ICNS, JXL, and HEIF parsing. Kolo Create never passes those formats to the renderer: the presentation boundary accepts only PNG, JPEG, and WebP assets.

Presentation typography is copy-aware: long paragraphs cannot become oversized display walls, long headlines receive balanced breaks, and conservative line-length checks reject layouts likely to create one-word widows when Office reflows the text.

Generation v2 compiles the extracted evidence into a signed, format-neutral design grammar. Instead of assigning a brand to one exclusive style bucket, it records continuous traits for color energy, media intensity, whitespace, curvature, surface layering, typographic contrast, asymmetry, product focus, and monochrome restraint. Precision, kinetic, editorial, product, and monochrome remain reusable component vocabularies whose weights can be blended within one coherent document.

Every generation builds the same content map and candidate scene plan before PDF, HTML, or PowerPoint rendering. Each source group receives a semantic role; several compatible components are scored for content fit, brand fit, and repetition; and the selected component plus alternates is stored in the layout artifact. PowerPoint uses the scene's selected direction per slide while the strongest overall direction anchors the opening and closing. This preserves the existing reliable renderers while removing their independent, coarse composition decisions.

Each deck quality report also includes a content-independent layout identity. Comparing those identities flags near-identical archetype and geometry selections across brands before look-alike decks are accepted. Media-led decks close with the same light-copy/bold-field language used on their covers, avoiding isolated logo patches on the final slide.

Website extraction prefers Chromium-rendered computed styles, dismisses common consent overlays, captures a clean source screenshot and screen-media source-webpage PDF, and falls back to bounded static HTML/CSS extraction. Frontend source ingestion accepts a local directory, ZIP, or public HTTPS GitHub repository and renders an existing `index.html`, `dist`, `build`, `out`, `public`, or Storybook static output on loopback. If no static route exists, it renders a deterministic source-derived component specimen and records route fidelity as unverified. It never runs package scripts or backend code, and blocks external requests during the source render. The source PDF uses backgrounds, bounded lazy-content loading, and a frozen visual state. A low-resolution raster sanity check grades the export and tests whether proposed semantic colors actually appear in either rendered artifact. Unsupported secondary accents collapse to the primary accent, while unsupported dark roles can be replaced only by a visibly supported dark candidate. The browser path can recover sites that reject the lightweight HTTP client while still rejecting error pages. Palette scoring distinguishes page, surface, text, primary accent, secondary accent, and dark brand-support roles; derives transparent-root canvases from dominant painted regions; rejects browser-default and unpainted stylesheet colors; and excludes detected consent or modal overlays. Logo discovery prefers the visible header wordmark—including text-rendered brand links—over social-preview artwork, calls to action are ranked by semantic purpose rather than frequency alone, and large visible media is stored with semantic labels, dimensions, role, and provenance. Major selections retain confidence and provenance. It rejects private-network website targets and validates every HTTP redirect.

The resulting design system includes component variants for heading hierarchy, buttons, cards, navigation, and section surfaces, plus captured hero imagery, imagery proportions, borders, radii, shadows, padding, and alignment. It also records portable font categories and visual-language signals including media coverage, viewport density, dominant alignment, overlay count, and an observed presentation mode—including product-led sites. Browser-native evidence preserves bounded CSS custom properties, font-face declarations, breakpoints, grid/flex primitives, and background treatments alongside the normalized cross-renderer tokens. A separate `brand-components.json` turns that evidence into portable recipes for section markers, feature bands, grids, media treatments, and closing signatures. Secondary brand colors must clear an evidence threshold before they enter multi-color components; monochrome brands receive an open, rule-led grid instead of generic filled cards.

PDF creation compiles source Markdown into stable content-block IDs and an inspectable, format-independent layout plan. A code-owned composition selector combines source structure with saved visual-language evidence, then chooses an editorial narrative, asymmetric feature grid, numbered process, modular announcement, or product showcase. A second deterministic pass assigns exact brand-component treatments to the cover and each section, limits prominent motifs to avoid repetition, and rejects raster image placements that would require enlargement. Cover media must also match the document topic; unrelated site photography falls back to a type-led cover. Monochrome systems use numbered editorial feature rows instead of a generic dark slab. Both renderers execute the shared plan, with ReportLab remaining preferred while the HTML/CSS implementation matures.

Source inputs are mutually exclusive:

```sh
kolo-design create design-system --url "https://example.com" --workspace ./data
kolo-design create design-system --repo-url "https://github.com/org/site" --workspace ./data
kolo-design create design-system --source-dir "../site" --workspace ./data
kolo-design create design-system --source-archive "../site.zip" --workspace ./data
kolo-design create design-system --browser-evidence "../tesla-browser-evidence" --workspace ./data
```

Every website source passes a deterministic fidelity gate before extraction. Error pages, access blocks, bot challenges, and materially empty captures stop with `browser_evidence_required`; Kolo can then capture the real page from its shared visible Chromium session and import a bounded evidence directory or ZIP. See [`references/browser-evidence.md`](references/browser-evidence.md). The evidence bundle intentionally excludes cookies, headers, local storage, and browser history.

The browser also preserves rendered frames from large video, picture, CSS-background, and pseudo-element regions. Automatic examples run in a dedicated brand-demonstration mode, allowing one signature brand image even when the Kolo Create explainer does not share its subject. Normal documents still require semantic media relevance. Media-led automatic examples fail QA when they contain no usable brand imagery.

The maintained ten-site coverage matrix is [`benchmarks/site-matrix.json`](benchmarks/site-matrix.json). Freeze approved evidence bundles for deterministic regressions and run the listed URLs separately as changing live canaries.

The comparison command evaluates two renderers without paying for two planning calls. It produces the current ReportLab PDF and a candidate HTML/CSS document and PDF from one validated plan, runs DOM overflow and grid-alignment checks, and generates side-by-side page previews plus a comparison manifest. ReportLab remains the default while repeated review determines which browser patterns deserve promotion.

Every successful design-system command automatically renders the bundled Kolo Create explainer to `<workspace>/examples/<brand-id>-kolo-create.pdf` and `<workspace>/examples/<brand-id>-kolo-create.pptx`. `--example-output` and `--example-presentation-output` optionally override those destinations; they do not enable the behavior. The two stages remain separate internally, but these automatic first artifacts make every extraction immediately comparable without a model call.

When the skill runs inside Kolo, its delivery contract attaches that validated example PDF to the current chat with Kolo's `message` tool and opens the same local file in the visible Kolo desktop browser. User-requested PDFs follow the same contract. The command-line program itself remains UI-independent: it returns the artifact path, while the skill performs chat and browser delivery so the renderer stays reusable in other environments.

Inline bold, links, and code spans are converted for ReportLab. Emoji unsupported by the PDF fonts are removed automatically and counted in the quality report, so the agent does not need to rewrite the source into a separate print copy.

## Modular planning

The deterministic planner makes the default workflow inexpensive and reproducible. It creates card grids for suitable feature lists without making a model call. For semantic layout choices, provide an explicit workspace-entitled model:

```sh
export KOLO_LLM_BASE_URL="https://your-openai-compatible-gateway"
export KOLO_LLM_TOKEN="..."

uv run kolo-design pdf create \
  --system "./data/brands/kolo/latest.json" \
  --content "./content.md" \
  --prompt "Restructure this into a concise board update" \
  --output "./output/board-update.pdf" \
  --planner llm \
  --model "workspace-entitled-model-id"

uv run kolo-design powerpoint create \
  --system "./data/brands/kolo/latest.json" \
  --content "./content.md" \
  --prompt "Shape this into a concise leadership presentation" \
  --output "./output/leadership-update.pptx" \
  --planner llm \
  --model "workspace-entitled-model-id"
```

The model receives bounded source blocks and may only arrange their IDs into supported presentation roles; code rejects any plan that drops, repeats, or invents a block. The repository deliberately contains no universal model allowlist and no image-provider selector. Image generation can later use the workspace's single configured route without changing the design-system contract.

## Validate

```sh
uv run python scripts/readiness.py
uv run --extra dev pytest -q
```

Build a no-model, multi-brand and multi-document planning regression manifest with `scripts/generation_regression.py`. Supply repeated `--system NAME=PATH` and `--content NAME=PATH` arguments; the output records grammar signatures, component sequences, direction sequences, quality checks, and pairwise similarity for both document and presentation plans.

See [INSTALL-KOLO.md](INSTALL-KOLO.md) for the expected Kolo workspace layout.
