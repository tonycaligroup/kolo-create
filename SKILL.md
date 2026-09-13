---
name: kolo-create
description: Create a reusable design system from a public website, then use that saved system with user-supplied text and a design prompt to produce a polished, verified PDF. Use when a user wants to capture brand language, generate branded documents, refresh a saved brand, or reuse a brand across new PDFs.
metadata:
  version: "0.3.0"
---

# Kolo Create

Marketplace compatibility value:

```yaml
version: 0.3.0
```

Kolo Create is one skill with two explicit stages. Never collapse the stages into one hidden operation: website extraction creates a reusable versioned design system; PDF generation consumes an exact saved version without recrawling or modifying it.

## 1. Create the design system

When the user supplies a public website, run:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design create design-system --url "<public website>" --name "<brand name>" --workspace "/home/node/.openclaw/kolo-create-data"
```

Return the design-system JSON, specimen, source screenshot, brand ID, and evidence counts. Explain that observed values are evidence while semantic token roles are deterministic candidates.

The design system includes more than tokens: capture observed heading levels, primary and secondary buttons, cards, navigation, section surfaces, imagery proportions, borders, radii, shadows, padding, alignment, and common labels. Return the separate component-inventory path as well.

## 2. Create a PDF

When the user supplies or selects a saved design system plus source text and a design prompt, run:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design pdf create --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<design direction>" --output "<result.pdf>"
```

Return the PDF, inspectable layout plan, previews, quality report, page count, component-usage counts, and exact design-system version. The deterministic planner preserves Markdown structure, assigns stable IDs to every source block, and recognizes explicit style, title, page-size, and orientation direction. It turns suitable feature lists into card grids and renders callouts and standalone links with the saved design system's component recipes.

Use the modular LLM planner only when semantic restructuring is needed:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design pdf create --system "<design-system.json>" --content "<source.txt-or-md>" --prompt "<design direction>" --output "<result.pdf>" --planner llm --model "<workspace-entitled model>"
```

The LLM route requires `KOLO_LLM_BASE_URL` and `KOLO_LLM_TOKEN`. Choose the model from the current workspace catalog, entitlement, evaluated capability, and budget. The model may arrange stable source-block IDs but code owns the copy and rejects omitted, repeated, or invented blocks. Never assume another workspace's model list, silently upgrade models, or silently fall back after a failed paid call.

## Safety and quality boundaries

- Accept public HTTP(S) websites only; validate every redirect and cap every response while streaming.
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
