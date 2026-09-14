from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image as PILImage
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .network import assert_public_url


MAX_REFERENCE_PDF_BYTES = 45 * 1024 * 1024


def _trim_transparent_png(payload: bytes) -> bytes:
    """Remove SVG viewport whitespace without changing the rendered mark."""
    try:
        with PILImage.open(io.BytesIO(payload)) as source:
            rgba = source.convert("RGBA")
            bbox = rgba.getchannel("A").getbbox()
            if not bbox:
                return payload
            cropped = rgba.crop(bbox)
            target = io.BytesIO()
            cropped.save(target, "PNG", optimize=True)
            return target.getvalue()
    except (OSError, ValueError):
        return payload


def _browser_executable() -> str | None:
    candidates = [
        os.environ.get("KOLO_CHROMIUM_PATH"),
        "/usr/local/bin/chromium",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    return next((value for value in candidates if value and Path(value).exists()), None)


def _literal_private_host(url: str) -> bool:
    import ipaddress

    host = urlparse(url).hostname
    if not host or host.lower() == "localhost":
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def rasterize_svg(payload: bytes) -> bytes | None:
    """Create a transparent PNG fallback for an SVG logo without network access."""
    executable = _browser_executable()
    if not executable:
        return None
    encoded = base64.b64encode(payload).decode("ascii")
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1000, "height": 600}, device_scale_factor=3)
        page = context.new_page()
        try:
            page.set_content(
                f'<style>html,body{{margin:0;background:transparent}}img{{display:block;width:auto;height:160px;max-width:800px}}</style>'
                f'<img id="logo" src="data:image/svg+xml;base64,{encoded}">',
                wait_until="load",
            )
            logo = page.locator("#logo")
            logo.wait_for(state="visible", timeout=5_000)
            return _trim_transparent_png(logo.screenshot(type="png", omit_background=True))
        except PlaywrightTimeoutError:
            return None
        finally:
            context.close()
            browser.close()


def _dismiss_overlays(page: Any) -> dict[str, Any]:
    """Dismiss common consent UI before sampling or taking the evidence screenshot."""
    result = page.evaluate(
        """() => {
          const overlayWords = /(cookie|consent|privacy|tracking|preference)/i;
          const overlaySelector = [
            '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]', 'dialog',
            '[id*="cookie" i]', '[class*="cookie" i]',
            '[id*="consent" i]', '[class*="consent" i]'
          ].join(',');
          const choices = [
            /^(decline|reject)( all)?$/i,
            /^(only |use )?(necessary|essential)( cookies)?$/i,
            /^(continue without accepting|do not sell)$/i,
            /^(accept|allow|agree)( all)?$/i
          ];
          const controls = [...document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')]
            .filter((el) => {
              const r = el.getBoundingClientRect(), s = getComputedStyle(el);
              return r.width > 1 && r.height > 1 && s.display !== 'none' && s.visibility !== 'hidden';
            });
          for (const pattern of choices) {
            const control = controls.find((el) => {
              const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
              if (!pattern.test(label)) return false;
              const parent = el.closest(overlaySelector);
              return Boolean(parent && overlayWords.test(parent.innerText || parent.getAttribute('aria-label') || ''));
            });
            if (control) {
              const label = (control.innerText || control.value || control.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
              control.click();
              return { clicked: label, hidden: 0 };
            }
          }
          return { clicked: '', hidden: 0 };
        }"""
    )
    page.wait_for_timeout(650)
    hidden_result = page.evaluate(
        """() => {
          const words = /(cookie|consent|privacy|tracking|preference)/i;
          const overlaySelector = [
            '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]', 'dialog',
            '[id*="cookie" i]', '[class*="cookie" i]',
            '[id*="consent" i]', '[class*="consent" i]'
          ].join(',');
          const candidates = [...document.querySelectorAll(overlaySelector)];
          let hidden = 0;
          for (const el of candidates) {
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            const text = el.innerText || el.getAttribute('aria-label') || '';
            if (r.width > 1 && r.height > 1 && s.display !== 'none' && words.test(text)) {
              el.style.setProperty('display', 'none', 'important');
              hidden += 1;
            }
          }
          let backdrops = 0;
          for (const el of document.querySelectorAll('body *')) {
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            const area = Math.max(0, r.width) * Math.max(0, r.height);
            const hint = `${el.id || ''} ${typeof el.className === 'string' ? el.className : ''}`.toLowerCase();
            const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
            const z = Number.parseInt(s.zIndex, 10) || 0;
            const backdropLike = /(backdrop|scrim|overlay|veil|modal|consent|privacy|cookie)/.test(hint) || z >= 1000;
            if (['fixed', 'sticky'].includes(s.position) && area >= innerWidth * innerHeight * 0.55 && text.length <= 2 && backdropLike) {
              el.style.setProperty('display', 'none', 'important');
              backdrops += 1;
            }
          }
          if (hidden || backdrops) {
            document.documentElement.style.setProperty('overflow', 'auto', 'important');
            document.body.style.setProperty('overflow', 'auto', 'important');
          }
          return {dialogs: hidden, backdrops};
        }"""
    )
    result["hidden"] = hidden_result["dialogs"]
    result["backdrops_hidden"] = hidden_result["backdrops"]
    return result


def _freeze_motion(page: Any) -> None:
    """Keep the screenshot, evidence sample, and PDF on one stable visual state."""
    page.add_style_tag(content="""
      *, *::before, *::after {
        animation-play-state: paused !important;
        transition-duration: 0s !important;
        transition-delay: 0s !important;
        caret-color: transparent !important;
      }
      html { scroll-behavior: auto !important; }
    """)


def _warm_lazy_content(page: Any) -> None:
    """Bounded scrolling loads ordinary lazy media before the reference export."""
    page.evaluate(
        """async () => {
          const viewport = Math.max(600, innerHeight);
          const maximum = Math.min(document.documentElement.scrollHeight, viewport * 20);
          const step = Math.max(600, Math.floor(viewport * 0.8));
          for (let y = 0; y < maximum; y += step) {
            scrollTo(0, y);
            await new Promise((resolve) => setTimeout(resolve, 80));
          }
          scrollTo(0, 0);
          await new Promise((resolve) => setTimeout(resolve, 300));
        }"""
    )


def _reference_pdf(page: Any) -> tuple[bytes | None, dict[str, Any]]:
    """Export the cleaned screen presentation as paginated browser evidence."""
    try:
        _warm_lazy_content(page)
        page.emulate_media(media="screen")
        document = page.evaluate(
            """() => ({
              width: document.documentElement.scrollWidth,
              height: document.documentElement.scrollHeight
            })"""
        )
        payload = page.pdf(
            width="1440px",
            height="1100px",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        metadata = {
            "status": "captured",
            "bytes": len(payload),
            "document": document,
            "media": "screen",
            "page_css_pixels": {"width": 1440, "height": 1100},
            "print_background": True,
        }
        if len(payload) > MAX_REFERENCE_PDF_BYTES:
            metadata.update({"status": "omitted_too_large", "maximum_bytes": MAX_REFERENCE_PDF_BYTES})
            return None, metadata
        return payload, metadata
    except Exception as exc:
        return None, {"status": "failed", "reason": type(exc).__name__}


def _rasterize_svg_on_page(page: Any, payload: str) -> bytes | None:
    """Rasterize an inline mark at delivery resolution without starting another browser."""
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    browser = page.context.browser
    if browser is None:
        return None
    render_context = browser.new_context(
        viewport={"width": 1000, "height": 600}, device_scale_factor=3
    )
    render_page = render_context.new_page()
    try:
        render_page.set_content(
            '<style>html,body{margin:0;background:transparent}'
            'img{display:block;width:auto;height:160px}</style>'
            f'<img id="logo" src="data:image/svg+xml;base64,{encoded}">',
            wait_until="load",
        )
        logo = render_page.locator("#logo")
        logo.wait_for(state="visible", timeout=5_000)
        return _trim_transparent_png(logo.screenshot(type="png", omit_background=True))
    except Exception:
        return None
    finally:
        render_context.close()


def _visible_logo(page: Any, base_url: str) -> dict[str, Any] | None:
    """Capture the strongest visible header/nav wordmark before metadata images."""
    brand = (urlparse(base_url).hostname or "").lower().removeprefix("www.").split(".")[0]
    locator = page.locator(
        'header img,header svg,nav img,nav svg,a[href="/"] img,a[href="/"] svg,'
        '[class*="logo" i] img,[class*="logo" i] svg,img[alt*="logo" i],'
        'header a,nav a,[role="banner"] a'
    )
    ranked: list[tuple[float, Any, dict[str, Any]]] = []
    for index in range(min(locator.count(), 80)):
        candidate = locator.nth(index)
        try:
            info = candidate.evaluate(
                """(el, brand) => {
                  const r = el.getBoundingClientRect(), s = getComputedStyle(el), owner = el.closest('a');
                  const hint = [el.id, el.className, el.getAttribute('alt'), el.getAttribute('aria-label'), el.getAttribute('src'), owner?.className, owner?.getAttribute('aria-label')]
                    .filter((value) => typeof value === 'string').join(' ').toLowerCase();
                  const text = (el.innerText || owner?.innerText || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                  const aria = (el.getAttribute('aria-label') || owner?.getAttribute('aria-label') || '').trim().toLowerCase();
                  const inHeader = Boolean(el.closest('header,nav'));
                  const homeLink = Boolean(el.closest('a[href="/"],a[href$=".com/"],a[href$=".ai/"]'));
                  const exactBrandText = Boolean(brand && (text === brand || aria === brand || aria === `${brand} logo`));
                  return {
                    visible: r.width >= (exactBrandText ? 10 : 35) && r.height >= 12 && r.width <= 500 && r.height <= 180 &&
                      s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0 && r.bottom > 0 && r.top < innerHeight,
                    width: r.width, height: r.height, y: r.y, hint, text, inHeader, homeLink,
                    svg: el.tagName.toLowerCase() === 'svg' ? el.outerHTML : null,
                    brandMatch: Boolean(brand && hint.includes(brand)),
                    exactBrandText
                  };
                }""",
                brand,
            )
            if not info.get("visible"):
                continue
            aspect = float(info["width"]) / max(1.0, float(info["height"]))
            score = (
                (70 if info.get("inHeader") else 0)
                + (45 if info.get("homeLink") else 0)
                + (55 if "logo" in str(info.get("hint", "")) else 0)
                + (35 if info.get("brandMatch") else 0)
                + (90 if info.get("exactBrandText") else 0)
                + (25 if info.get("svg") else 0)
                + (20 if float(info.get("y", 9999)) < 180 else 0)
                + (15 if 1.5 <= aspect <= 10 else 0)
            )
            ranked.append((score, candidate, info))
        except Exception:
            continue
    for score, candidate, info in sorted(ranked, key=lambda item: item[0], reverse=True):
        try:
            payload = (
                _rasterize_svg_on_page(page, str(info["svg"]))
                if info.get("svg") else None
            ) or _trim_transparent_png(candidate.screenshot(type="png", omit_background=True))
            return {
                "png": payload,
                "score": score,
                "width": round(float(info["width"])),
                "height": round(float(info["height"])),
                "hint": str(info.get("hint", ""))[:240],
            }
        except Exception:
            continue
    return None


def browser_snapshot(url: str, *, allow_local: bool = False) -> dict[str, Any] | None:
    executable = _browser_executable()
    if not executable:
        return None
    if not allow_local:
        assert_public_url(url)
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=2)

        def route_request(route: Any) -> None:
            request_url = route.request.url
            if allow_local:
                host = (urlparse(request_url).hostname or "").lower()
                if urlparse(request_url).scheme in {"data", "blob", "about"} or host in {"127.0.0.1", "localhost", "::1"}:
                    route.continue_()
                else:
                    route.abort()
            elif _literal_private_host(request_url):
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_request)
        page = context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1200)
            _freeze_motion(page)
            if not allow_local:
                assert_public_url(page.url)
            overlay_actions = _dismiss_overlays(page)
            visible_logo = _visible_logo(page, page.url)
            snapshot = page.evaluate(
                """() => {
                  const visible = [...document.querySelectorAll('body *')].filter((el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 1 && r.height > 1 && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
                  }).slice(0, 5000);
                  const rows = visible.map((el) => {
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return [
                      `color:${s.color}`, `background-color:${s.backgroundColor}`, `border-color:${s.borderColor}`,
                      `font-family:${s.fontFamily}`, `font-size:${s.fontSize}`, `font-weight:${s.fontWeight}`,
                      `line-height:${s.lineHeight}`, `letter-spacing:${s.letterSpacing}`,
                      `padding-top:${s.paddingTop}`, `padding-right:${s.paddingRight}`, `padding-bottom:${s.paddingBottom}`, `padding-left:${s.paddingLeft}`,
                      `margin-top:${s.marginTop}`, `margin-right:${s.marginRight}`, `margin-bottom:${s.marginBottom}`, `margin-left:${s.marginLeft}`,
                      `gap:${s.gap}`, `border-radius:${s.borderRadius}`, `box-shadow:${s.boxShadow}`,
                      `width:${Math.round(r.width)}px`, `max-width:${s.maxWidth}`
                    ].join(';');
                  });
                  const viewportArea = Math.max(1, innerWidth * innerHeight);
                  const elements = visible.slice(0, 1800).map((el) => {
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    const clippedWidth = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
                    const clippedHeight = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
                    const region = el.closest('[role="dialog"],dialog,header,nav,main,footer,section,article,aside');
                    const hint = [el.id, el.className, el.getAttribute('role'), el.getAttribute('aria-label')]
                      .filter((value) => typeof value === 'string').join(' ').toLowerCase();
                    const overlayHint = /(cookie|consent|privacy|modal|dialog|overlay|tracking|preference)/.test(hint);
                    const overlayPosition = ['fixed', 'sticky'].includes(s.position) && clippedWidth * clippedHeight > viewportArea * 0.08;
                    return {
                      tag: el.tagName.toLowerCase(),
                      role: el.getAttribute('role') || '',
                      href: Boolean(el.getAttribute('href')),
                      src: el.currentSrc || el.getAttribute('src') || '',
                      alt: el.getAttribute('alt') || '',
                      text_sample: (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
                      rect: { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) },
                      viewport: {
                        visible: clippedWidth > 0 && clippedHeight > 0,
                        area_ratio: Math.round((clippedWidth * clippedHeight / viewportArea) * 10000) / 10000
                      },
                      semantic: {
                        region: region ? (region.getAttribute('role') || region.tagName.toLowerCase()) : 'body',
                        overlay: overlayHint || overlayPosition || el.getAttribute('aria-modal') === 'true'
                      },
                      style: {
                        color: s.color, background: s.backgroundColor, border_color: s.borderColor,
                        background_image: s.backgroundImage,
                        border_width: s.borderWidth, border_radius: s.borderRadius, box_shadow: s.boxShadow,
                        font_family: s.fontFamily, font_size: s.fontSize, font_weight: s.fontWeight,
                        line_height: s.lineHeight, letter_spacing: s.letterSpacing, text_align: s.textAlign,
                        padding: `${s.paddingTop} ${s.paddingRight} ${s.paddingBottom} ${s.paddingLeft}`,
                        display: s.display, gap: s.gap, grid_template_columns: s.gridTemplateColumns,
                        grid_template_rows: s.gridTemplateRows, max_width: s.maxWidth, min_width: s.minWidth,
                        overflow: s.overflow, object_fit: s.objectFit, object_position: s.objectPosition,
                        position: s.position, z_index: s.zIndex
                      }
                    };
                  });
                  const rootStyle = (el) => {
                    const s = getComputedStyle(el);
                    return { color: s.color, background: s.backgroundColor, font_family: s.fontFamily };
                  };
                  return {
                    title: document.title, html: document.documentElement.outerHTML, computed_css: rows.join('\\n'), elements,
                    root_styles: { html: rootStyle(document.documentElement), body: rootStyle(document.body) },
                    viewport: { width: innerWidth, height: innerHeight }
                  };
                }"""
            )
            snapshot["url"] = page.url
            snapshot["response_status"] = response.status if response else None
            snapshot["overlay_actions"] = overlay_actions
            snapshot["visible_logo"] = visible_logo
            snapshot["screenshot"] = page.screenshot(full_page=False, type="png")
            reference_pdf, reference_pdf_metadata = _reference_pdf(page)
            snapshot["reference_pdf"] = reference_pdf
            snapshot["reference_pdf_metadata"] = reference_pdf_metadata
            return snapshot
        finally:
            context.close()
            browser.close()
