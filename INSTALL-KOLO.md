# Install Kolo Create

Kolo's portal **Create a Skill** screen drafts a skill from prose; it does not currently expose source-archive upload. Install this repository through the workspace/pod source deployment path.

## Workspace layout

Place the complete repository at:

```text
/home/node/.openclaw/workspace-main/skills/kolo-create
```

Keep generated design systems and documents outside the replaceable skill directory:

```text
/home/node/.openclaw/kolo-create-data
```

## Install and validate

```sh
cd /home/node/.openclaw/workspace-main/skills/kolo-create
uv sync --frozen
npm install --ignore-scripts
uv run python scripts/readiness.py
uv run --extra dev pytest -q
```

If Chromium is not `/usr/local/bin/chromium`, set `KOLO_CHROMIUM_PATH` to the actual executable.

PowerPoint generation requires Node.js and the repository's pinned PptxGenJS dependency. Install it locally with `npm install --ignore-scripts`; do not install it globally. The local `node_modules` persists with the skill under `/home/node`. If Node.js is not on `PATH`, set `KOLO_PRESENTATION_NODE` to its executable. The readiness command verifies the runtime before the first extraction because every new design system receives an automatic PowerPoint example.

The current Kolo pod does not contain LibreOffice. Kolo Create therefore produces same-plan HTML/PNG slide previews with Chromium and validates the actual PPTX through its OOXML package. The quality report clearly marks that these are composition previews, not literal PowerPoint renders.

## First Kolo trial

Create the design system:

```sh
uv run kolo-design create design-system \
  --url "https://your-company.example" \
  --name "Your Company" \
  --workspace "/home/node/.openclaw/kolo-create-data"
```

Then create a PDF from the saved system:

```sh
uv run kolo-design pdf create \
  --system "/home/node/.openclaw/kolo-create-data/brands/your-company/latest.json" \
  --content "/path/to/source.md" \
  --prompt "Create a polished executive brief" \
  --output "/home/node/.openclaw/kolo-create-data/output/your-company-brief.pdf"
```

Or create an editable PowerPoint:

```sh
uv run kolo-design powerpoint create \
  --system "/home/node/.openclaw/kolo-create-data/brands/your-company/latest.json" \
  --content "/path/to/source.md" \
  --prompt "Create a concise leadership presentation" \
  --output "/home/node/.openclaw/kolo-create-data/output/your-company-deck.pptx"
```

The extraction output contains an immutable `1.0.0` design system, `latest.json`, source screenshot, logo asset, evidence, component inventory, specimen, and automatic Kolo Create examples in PDF and PowerPoint. Both renderers return previews, an inspectable plan, and a quality report.

Optional LLM planning requires `KOLO_LLM_BASE_URL`, `KOLO_LLM_TOKEN`, `--planner llm`, and an explicit model ID selected from that workspace's catalog and entitlement.
