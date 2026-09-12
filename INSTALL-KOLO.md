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
uv run python scripts/readiness.py
uv run --extra dev pytest -q
```

If Chromium is not `/usr/local/bin/chromium`, set `KOLO_CHROMIUM_PATH` to the actual executable.

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

The extraction output contains an immutable `1.0.0` design system, `latest.json`, source screenshot, logo asset, evidence, and specimen. PDF output includes previews and a quality report.

Optional LLM planning requires `KOLO_LLM_BASE_URL`, `KOLO_LLM_TOKEN`, `--planner llm`, and an explicit model ID selected from that workspace's catalog and entitlement.
