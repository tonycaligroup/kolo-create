from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .network import assert_public_url


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
        context = browser.new_context(viewport={"width": 1000, "height": 600}, device_scale_factor=1)
        page = context.new_page()
        try:
            page.set_content(
                f'<style>html,body{{margin:0;background:transparent}}img{{display:block;max-width:800px;max-height:500px}}</style>'
                f'<img id="logo" src="data:image/svg+xml;base64,{encoded}">',
                wait_until="load",
            )
            logo = page.locator("#logo")
            logo.wait_for(state="visible", timeout=5_000)
            return logo.screenshot(type="png", omit_background=True)
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
              const parent = el.closest('[role="dialog"],dialog,[id*="cookie" i],[class*="cookie" i],[id*="consent" i],[class*="consent" i]');
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
    hidden = page.evaluate(
        """() => {
          const words = /(cookie|consent|privacy|tracking|preference)/i;
          const candidates = [...document.querySelectorAll('[role="dialog"],dialog,[id*="cookie" i],[class*="cookie" i],[id*="consent" i],[class*="consent" i]')];
          let hidden = 0;
          for (const el of candidates) {
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            const text = el.innerText || el.getAttribute('aria-label') || '';
            if (r.width > 1 && r.height > 1 && s.display !== 'none' && words.test(text)) {
              el.style.setProperty('display', 'none', 'important');
              hidden += 1;
            }
          }
          if (hidden) {
            document.documentElement.style.setProperty('overflow', 'auto', 'important');
            document.body.style.setProperty('overflow', 'auto', 'important');
          }
          return hidden;
        }"""
    )
    result["hidden"] = hidden
    return result


def _visible_logo(page: Any, base_url: str) -> dict[str, Any] | None:
    """Capture the strongest visible header/nav wordmark before metadata images."""
    brand = (urlparse(base_url).hostname or "").lower().removeprefix("www.").split(".")[0]
    locator = page.locator(
        'header img,header svg,nav img,nav svg,a[href="/"] img,a[href="/"] svg,'
        '[class*="logo" i] img,[class*="logo" i] svg,img[alt*="logo" i]'
    )
    ranked: list[tuple[float, Any, dict[str, Any]]] = []
    for index in range(min(locator.count(), 80)):
        candidate = locator.nth(index)
        try:
            info = candidate.evaluate(
                """(el, brand) => {
                  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                  const hint = [el.id, el.className, el.getAttribute('alt'), el.getAttribute('aria-label'), el.getAttribute('src')]
                    .filter((value) => typeof value === 'string').join(' ').toLowerCase();
                  const inHeader = Boolean(el.closest('header,nav'));
                  const homeLink = Boolean(el.closest('a[href="/"],a[href$=".com/"],a[href$=".ai/"]'));
                  return {
                    visible: r.width >= 35 && r.height >= 12 && r.width <= 500 && r.height <= 180 &&
                      s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0 && r.bottom > 0 && r.top < innerHeight,
                    width: r.width, height: r.height, y: r.y, hint, inHeader, homeLink,
                    brandMatch: Boolean(brand && hint.includes(brand))
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
                + (20 if float(info.get("y", 9999)) < 180 else 0)
                + (15 if 1.5 <= aspect <= 10 else 0)
            )
            ranked.append((score, candidate, info))
        except Exception:
            continue
    for score, candidate, info in sorted(ranked, key=lambda item: item[0], reverse=True):
        try:
            return {
                "png": candidate.screenshot(type="png", omit_background=True),
                "score": score,
                "width": round(float(info["width"])),
                "height": round(float(info["height"])),
                "hint": str(info.get("hint", ""))[:240],
            }
        except Exception:
            continue
    return None


def browser_snapshot(url: str) -> dict[str, Any] | None:
    executable = _browser_executable()
    if not executable:
        return None
    assert_public_url(url)
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)

        def route_request(route: Any) -> None:
            if _literal_private_host(route.request.url):
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
                        display: s.display, object_fit: s.objectFit, position: s.position, z_index: s.zIndex
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
            return snapshot
        finally:
            context.close()
            browser.close()
