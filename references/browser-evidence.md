# Visible-browser evidence fallback

Use this only when `create design-system --url` returns `browser_evidence_required`. The normal extractor remains the first path.

## Capture contract

1. Open the exact requested URL in Kolo's shared visible Chromium profile. Reuse the user's existing session; do not launch a separate browser profile.
2. Wait for the intended page, dismiss ordinary consent UI, freeze animation, and scroll through at most 20 viewports to load lazy media. Return to the top.
3. Confirm the visible page is the requested brand, not an access-denied page, CAPTCHA, login wall, security challenge, or service error.
4. Create a persistent directory under `/home/node/.openclaw/kolo-create-data/browser-evidence/<host>-<timestamp>/`.
5. Save these files from the same stable page state:
   - `page.html`: `document.documentElement.outerHTML`
   - `computed-css.txt`: one bounded computed-style row per visible element, using the fields sampled by `browser_extract.py`
   - `elements.json`: at most 1,800 visible-element records in the same shape returned by `browser_extract.py`
   - `viewport.png`: the visible 1440×1100 viewport
   - `page.pdf`: browser PDF using screen media and backgrounds, when available
   - `logo.png` and `assets/hero-*.png|jpg|webp`: direct element screenshots or original browser-response assets when access requires the browser session
6. Write `browser-evidence.json` using this schema:

```json
{
  "schema": "kolo.browser-evidence/v1",
  "url": "https://example.com/",
  "title": "Example",
  "response_status": 200,
  "viewport": {"width": 1440, "height": 1100},
  "root_styles": {
    "html": {"color": "rgb(0, 0, 0)", "background": "rgb(255, 255, 255)", "font_family": "Arial"},
    "body": {"color": "rgb(0, 0, 0)", "background": "rgb(255, 255, 255)", "font_family": "Arial"}
  },
  "overlay_actions": {"clicked": "", "hidden": 0, "backdrops_hidden": 0},
  "files": {
    "html": "page.html",
    "computed_css": "computed-css.txt",
    "elements": "elements.json",
    "screenshot": "viewport.png",
    "reference_pdf": "page.pdf",
    "visible_logo": "logo.png"
  },
  "visible_logo": {"score": 250, "width": 180, "height": 32},
  "assets": [
    {
      "kind": "hero-image",
      "path": "assets/hero-1.jpg",
      "source_url": "https://example.com/media/hero.jpg",
      "alt": "Product hero",
      "role": "main"
    }
  ]
}
```

Omit optional file keys when unavailable. `html`, `computed_css`, `elements`, and `screenshot` are required. Keep the bundle below 100 MB. Do not include cookies, authorization headers, local storage, browser history, or unrelated tabs.

Then run:

```sh
uv run --project /home/node/.openclaw/workspace-main/skills/kolo-create kolo-design create design-system \
  --browser-evidence "<capture-directory-or-zip>" \
  --name "<brand name>" \
  --workspace "/home/node/.openclaw/kolo-create-data"
```

The CLI reruns the same deterministic fidelity gate on imported evidence. If that still fails, stop and ask for a Chrome capture ZIP or a different public page. Never generate a design system from failed evidence.
