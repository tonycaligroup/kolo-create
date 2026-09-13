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
            snapshot["screenshot"] = page.screenshot(full_page=False, type="png")
            return snapshot
        finally:
            context.close()
            browser.close()
