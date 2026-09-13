# Kolo Create

Kolo Create is one Kolo skill with two reusable stages:

1. Turn a public website into a versioned design system.
2. Turn source text plus a design prompt into a verified PDF using that exact design-system version.

The second stage never recrawls or mutates the brand. This keeps artifacts reproducible while presenting one coherent skill to the user.

## Quick start

```sh
uv sync --frozen

uv run kolo-design create design-system \
  --url "https://kolo.ai" \
  --name "Kolo" \
  --workspace "./data" \
  --example-output "./output/kolo-create-example.pdf"

uv run kolo-design pdf create \
  --system "./data/brands/kolo/latest.json" \
  --content "./tests/fixtures/content.md" \
  --prompt "Create a bold executive brief called Kolo Create" \
  --output "./output/kolo-create.pdf"
```

Website extraction prefers Chromium-rendered computed styles, captures a source screenshot, and falls back to bounded static HTML/CSS extraction. The browser path can recover sites that reject the lightweight HTTP client while still rejecting error pages. Palette scoring distinguishes page, surface, text, primary accent, and secondary accent roles; weights logo and CTA colors; rejects browser-default links; and excludes detected consent or modal overlays. Logo discovery favors site icons and brand-specific assets, and component text falls back to a readable foreground when observed styles conflict. It rejects private-network targets and validates every HTTP redirect.

The resulting design system includes component variants for heading hierarchy, buttons, cards, navigation, and section surfaces, plus imagery proportions, borders, radii, shadows, padding, and alignment. It also records portable font categories and visual-language signals including media coverage, viewport density, dominant alignment, overlay count, and an observed presentation mode. A separate component inventory and expanded specimen make the extracted language inspectable before reuse.

PDF creation compiles source Markdown into stable content-block IDs and an inspectable, format-independent layout plan. A code-owned composition selector combines source structure with saved visual-language evidence, then chooses an editorial narrative, asymmetric feature grid, numbered process, or modular announcement. The renderer maps the blocks into that family with extracted heading, card, callout, and action recipes, then reopens the final document, measures source-content coverage, renders PNG previews with Poppler, and writes a quality report.

`--example-output` keeps the two stages separate but runs them back-to-back: after extraction it renders the bundled Kolo Create explainer with the new system. This provides an immediate, comparable first artifact for every brand without a model call.

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
```

The model receives bounded source blocks and may only arrange their IDs into supported presentation roles; code rejects any plan that drops, repeats, or invents a block. The repository deliberately contains no universal model allowlist and no image-provider selector. Image generation can later use the workspace's single configured route without changing the design-system contract.

## Validate

```sh
uv run python scripts/readiness.py
uv run --extra dev pytest -q
```

See [INSTALL-KOLO.md](INSTALL-KOLO.md) for the expected Kolo workspace layout.
