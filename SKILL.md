---
name: kolo-create
description: Create a reusable design system from a public website, then use that saved system with user-supplied text and a design prompt to produce a polished, verified PDF. Use when a user wants to capture brand language, generate branded documents, refresh a saved brand, or reuse a brand across new PDFs.
metadata:
  version: "0.6.6"
---

# Kolo Create

Marketplace compatibility value:

```yaml
version: 0.6.6
```

Kolo Create is one skill with two explicit stages. Never collapse the stages into one hidden operation: website extraction creates a reusable versioned design system; PDF generation consumes an exact saved version without recrawling or modifying it.

## 1. Create the design system

When the user supplies a public website, run:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design create design-system --url "<public website>" --name "<brand name>" --workspace "/home/node/.openclaw/kolo-create-data" --example-output "/home/node/.openclaw/kolo-create-data/examples/<brand-id>-kolo-create.pdf"
```

Return the design-system JSON, specimen, source screenshot, brand ID, evidence counts, and the first branded example PDF. The example is a canonical explanation of Kolo Create rendered in the new system; it makes the extraction immediately testable while remaining a separate PDF-generation stage internally. Explain that observed values are evidence while semantic token roles are deterministic candidates.

The design system includes more than tokens: capture observed heading levels, semantically ranked primary and secondary calls to action, component variants, cards, navigation, section surfaces, hero imagery, imagery proportions, borders, radii, shadows, padding, alignment, and common labels. Before sampling, dismiss common consent overlays. Prefer the visible header wordmark—including text-rendered brand links—over social-preview artwork, reject browser-default or unpainted stylesheet colors as brand roles, and retain confidence plus provenance for major selections. When document roots are transparent, derive the canvas from dominant visible painted regions. Store a multi-color brand palette including a saturated dark support role when observed, portable font categories, overlay evidence, and visual-language signals such as media coverage, density, and whether the sampled viewport is product-, media-, illustration-, interface-, or typography-led. Return the separate component-inventory path as well.

## 2. Create a PDF

When the user supplies or selects a saved design system plus source text and a design prompt, run:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design pdf create --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<design direction>" --output "<result.pdf>"
```

Return the PDF, inspectable layout plan, previews, quality report, page count, component-usage counts, selected composition family with its evidence, and exact design-system version. The deterministic planner preserves Markdown structure, assigns stable IDs to every source block, and recognizes explicit style, title, page-size, and orientation direction. A separate deterministic composition selector combines content structure with saved visual-language signals and chooses among editorial narrative, asymmetric feature grid, numbered process, modular announcement, and product showcase families. Product showcase and media-led asymmetric covers reuse captured hero media; product showcase also uses deliberately balanced section breaks. Explicit composition directions override automatic selection.

Use the modular LLM planner only when semantic restructuring is needed:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design pdf create --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<design direction>" --output "<result.pdf>" --planner llm --model "<workspace-entitled model>"
```

The LLM route requires `KOLO_LLM_BASE_URL` and `KOLO_LLM_TOKEN`. Choose the model from the current workspace catalog, entitlement, evaluated capability, and budget. The model may arrange stable source-block IDs but code owns the copy and rejects omitted, repeated, or invented blocks. Never assume another workspace's model list, silently upgrade models, or silently fall back after a failed paid call.

## Safety and quality boundaries

- Accept public HTTP(S) websites only; validate every redirect and cap every response while streaming.
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
