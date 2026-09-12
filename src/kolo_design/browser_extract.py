from __future__ import annotations

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
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
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
                  return { title: document.title, html: document.documentElement.outerHTML, computed_css: rows.join('\\n') };
                }"""
            )
            snapshot["url"] = page.url
            snapshot["screenshot"] = page.screenshot(full_page=False, type="png")
            return snapshot
        finally:
            context.close()
            browser.close()
