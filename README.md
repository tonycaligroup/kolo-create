# Kolo Create

Kolo Create is one Kolo skill with two reusable stages:

1. Turn a public website into a versioned design system.
2. Turn source text plus a design prompt into a verified PDF using that exact design-system version.

The second stage never recrawls or mutates the brand. This keeps artifacts reproducible while presenting one coherent skill to the user.

## Quick start

```sh
uv sync --frozen

uv run kolo-design brand extract \
  --url "https://kolo.ai" \
  --name "Kolo" \
  --workspace "./data"

uv run kolo-design pdf create \
  --system "./data/brands/kolo/latest.json" \
  --content "./tests/fixtures/content.md" \
  --prompt "Create a bold executive brief called Kolo Create" \
  --output "./output/kolo-create.pdf"
```

Website extraction prefers Chromium-rendered computed styles, captures a source screenshot, and falls back to bounded static HTML/CSS extraction. It rejects private-network targets and validates every HTTP redirect.

PDF creation reopens the final document, measures source-content coverage, renders PNG previews with Poppler, and writes a quality report.

## Modular planning

The deterministic planner makes the default workflow inexpensive and reproducible. For semantic restructuring, provide an explicit workspace-entitled model:

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

The repository deliberately contains no universal model allowlist and no image-provider selector. Image generation can later use the workspace's single configured route without changing the design-system contract.

## Validate

```sh
uv run python scripts/readiness.py
uv run --extra dev pytest -q
```

See [INSTALL-KOLO.md](INSTALL-KOLO.md) for the expected Kolo workspace layout.
