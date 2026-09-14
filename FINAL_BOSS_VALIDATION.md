# Kolo Create final-boss validation

Validated on 2026-09-13 against Kolo Create v0.18.2. All runs used the deterministic planner and made zero paid model or image-generation calls.

## Result

- 17/17 live websites produced versioned design systems.
- 17/17 source-webpage PDFs remained usable for color validation.
- 17/17 three-page Kolo Create example PDFs passed content and render checks.
- 17/17 six-slide editable PowerPoints passed content, geometry, preview, and OOXML package checks.
- Readiness passed and the repository test suite passed: 100 tests.

| Brand | Source PDF | Pages | PDF | PowerPoint | PPTX package |
|---|---:|---:|---:|---:|---:|
| Adobe | usable | 1 | pass | pass | valid |
| Anvil | usable | 7 | pass | pass | valid |
| Apple | usable | 11 | pass | pass | valid |
| Dropbox | usable | 6 | pass | pass | valid |
| Duolingo | usable | 9 | pass | pass | valid |
| IKEA | usable | 12 | pass | pass | valid |
| Kolo | degraded, usable for color | 13 | pass | pass | valid |
| Mailchimp | usable | 7 | pass | pass | valid |
| Medicube | degraded, usable for color | 5 | pass | pass | valid |
| Nike | usable | 15 | pass | pass | valid |
| Notion | usable | 5 | pass | pass | valid |
| Red Bull | usable | 6 | pass | pass | valid |
| Samsung | degraded, usable for color | 7 | pass | pass | valid |
| Slack | usable | 8 | pass | pass | valid |
| Stripe | usable | 10 | pass | pass | valid |
| Uber | usable | 7 | pass | pass | valid |
| Whataburger | usable | 1 | pass | pass | valid |

## Regressions found and fixed

- Reduced decorative line density in both formats: short page markers replace full-width PDF rules, repeated deck row rules became quiet surfaces or local markers, and paired cover/closing rules were removed.
- Reconciled palette roles against the rendered screenshot and source PDF so incidental colors cannot become brand-dark roles.
- Rejected generic page imagery, invalid raster payloads, sparse browser placeholders, and near-solid broken-image captures before they can become logos or heroes.
- Preferred visible wordmarks over small metadata icons and capped raster marks at native resolution.
- Tightened logo contrast fields to the actual mark instead of rendering wide badge bars.
- Preserved image aspect ratio and rejected placements that would require raster enlargement.
- Separated PDF and PowerPoint quality sidecars so one format cannot overwrite the other's validation report.
- Repaired PDF callout foreground selection using the actual callout background.

## Known limits for Kolo validation

- Kolo, Medicube, and Samsung source exports are marked degraded because some captured pages are visually low-information. They still pass the color-validation threshold and generation succeeds.
- Kolo's current pod can create PPTX files but cannot render them through PowerPoint or LibreOffice. The skill therefore uses a same-plan Chromium composition preview plus direct OOXML validation; PDF remains the browser-preview format.
- Image sourcing still comes only from the captured brand site. Stock-photo or generated-image diversification is intentionally deferred.
