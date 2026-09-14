from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image as PILImage
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .network import MAX_HTML_BYTES, assert_public_url, fetch_limited


MAX_REFERENCE_PDF_BYTES = 45 * 1024 * 1024
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


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


def _has_visible_logo_pixels(payload: bytes) -> bool:
    """Reject blank SVG-use rasterizations before accepting them as logos."""
    try:
        with PILImage.open(io.BytesIO(payload)) as source:
            rgba = source.convert("RGBA")
            rgba.thumbnail((300, 120))
            pixels = rgba.get_flattened_data() if hasattr(rgba, "get_flattened_data") else rgba.getdata()
            visible = [pixel for pixel in pixels if pixel[3] >= 24]
            if len(visible) < 12:
                return False
            colors = {(red // 16, green // 16, blue // 16, alpha // 32) for red, green, blue, alpha in visible}
            return len(colors) >= 2 or len(visible) >= 80
    except (OSError, ValueError):
        return False


def _remove_flat_logo_background(payload: bytes, tolerance: int = 18) -> bytes:
    """Make a flat header field transparent while preserving its visible mark."""
    try:
        with PILImage.open(io.BytesIO(payload)) as source:
            rgba = source.convert("RGBA")
            corners = [
                rgba.getpixel((0, 0)), rgba.getpixel((rgba.width - 1, 0)),
                rgba.getpixel((0, rgba.height - 1)), rgba.getpixel((rgba.width - 1, rgba.height - 1)),
            ]
            background = max(set(corners), key=corners.count)
            if corners.count(background) < 3:
                return payload
            pixels = []
            for red, green, blue, alpha in rgba.get_flattened_data():
                distance = max(abs(red - background[0]), abs(green - background[1]), abs(blue - background[2]))
                pixels.append((red, green, blue, 0 if distance <= tolerance else alpha))
            rgba.putdata(pixels)
            target = io.BytesIO()
            rgba.save(target, "PNG", optimize=True)
            return _trim_transparent_png(target.getvalue())
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
    result: dict[str, Any] = {"clicked": "", "hidden": 0, "backdrops_hidden": 0}
    click_script = """() => {
          const deepElements = () => {
            const elements = [], roots = [document];
            for (const root of roots) {
              for (const el of root.querySelectorAll('*')) {
                elements.push(el);
                if (el.shadowRoot) roots.push(el.shadowRoot);
              }
            }
            return elements;
          };
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
          const controls = deepElements()
            .filter((el) => el.matches('button,[role="button"],input[type="button"],input[type="submit"]'))
            .filter((el) => {
              const r = el.getBoundingClientRect(), s = getComputedStyle(el);
              return r.width > 1 && r.height > 1 && s.display !== 'none' && s.visibility !== 'hidden';
            });
          for (const pattern of choices) {
            const control = controls.find((el) => {
              const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
              if (!pattern.test(label)) return false;
              const parent = el.closest(overlaySelector);
              if (parent && overlayWords.test(parent.innerText || parent.getAttribute('aria-label') || '')) {
                return true;
              }
              let ancestor = el.parentElement || el.getRootNode()?.host;
              for (let depth = 0; ancestor && depth < 10; depth += 1) {
                const text = (ancestor.innerText || ancestor.getAttribute('aria-label') || '').trim();
                const position = getComputedStyle(ancestor).position;
                if (text.length <= 2500 && overlayWords.test(text) && ['fixed', 'sticky'].includes(position)) {
                  return true;
                }
                ancestor = ancestor.parentElement || ancestor.getRootNode()?.host;
              }
              return false;
            });
            if (control) {
              const label = (control.innerText || control.value || control.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
              control.click();
              return { clicked: label, hidden: 0 };
            }
          }
          return { clicked: '', hidden: 0 };
        }"""
    frames = list(page.frames)
    for frame in frames:
        try:
            frame_result = frame.evaluate(click_script)
            if frame_result.get("clicked") and not result["clicked"]:
                result["clicked"] = frame_result["clicked"]
        except PlaywrightError:
            continue
    page.wait_for_timeout(650)
    cleanup_script = """() => {
          const deepElements = () => {
            const elements = [], roots = [document];
            for (const root of roots) {
              for (const el of root.querySelectorAll('*')) {
                elements.push(el);
                if (el.shadowRoot) roots.push(el.shadowRoot);
              }
            }
            return elements;
          };
          const words = /(cookie|consent|privacy|tracking|preference)/i;
          const overlaySelector = [
            '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]', 'dialog',
            '[id*="cookie" i]', '[class*="cookie" i]',
            '[id*="consent" i]', '[class*="consent" i]'
          ].join(',');
          const elements = deepElements();
          const candidates = elements.filter((el) => el.matches(overlaySelector));
          let hidden = 0;
          for (const el of candidates) {
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            const text = el.innerText || el.getAttribute('aria-label') || '';
            if (r.width > 1 && r.height > 1 && s.display !== 'none' && words.test(text)) {
              el.style.setProperty('display', 'none', 'important');
              hidden += 1;
            }
          }
          for (const el of elements) {
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
            if (
              r.width > 1 && r.height > 1 && ['fixed', 'sticky'].includes(s.position)
              && text.length <= 2500 && words.test(text)
              && el.querySelector('button,[role="button"],input[type="button"],input[type="submit"]')
            ) {
              el.style.setProperty('display', 'none', 'important');
              hidden += 1;
            }
          }
          let backdrops = 0;
          for (const el of elements) {
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
    for frame in frames:
        try:
            hidden_result = frame.evaluate(cleanup_script)
            result["hidden"] += hidden_result["dialogs"]
            result["backdrops_hidden"] += hidden_result["backdrops"]
        except PlaywrightError:
            continue
    try:
        consent_iframes = page.evaluate(
            """() => {
              let hidden = 0;
              for (const frame of document.querySelectorAll('iframe')) {
                const hint = `${frame.src || ''} ${frame.id || ''} ${frame.className || ''} ${frame.title || ''}`;
                const r = frame.getBoundingClientRect(), s = getComputedStyle(frame);
                if (r.width > 1 && r.height > 1 && s.display !== 'none' && /(cookie|consent|privacy|ccpa|gdpr)/i.test(hint)) {
                  frame.style.setProperty('display', 'none', 'important');
                  hidden += 1;
                }
              }
              return hidden;
            }"""
        )
        result["hidden"] += consent_iframes
    except PlaywrightError:
        pass
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


def _reference_pdf(
    page: Any, *, viewport_screenshot: bytes | None = None
) -> tuple[bytes | None, dict[str, Any]]:
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
        metadata = {
            "status": "captured",
            "document": document,
            "media": "screen",
            "page_css_pixels": {"width": 1440, "height": 1100},
            "print_background": True,
        }
        options = {
            "width": "1440px",
            "height": "1100px",
            "print_background": True,
            "margin": {"top": "0", "right": "0", "bottom": "0", "left": "0"},
        }
        payload = None
        try:
            payload = page.pdf(**options)
            metadata.update({"bytes": len(payload), "full_capture_bytes": len(payload)})
        except Exception as exc:
            metadata.update({
                "rich_page": document.get("height", 0) > 1100 * 5,
                "full_capture_error": type(exc).__name__,
            })
        if payload is None and viewport_screenshot:
            try:
                with PILImage.open(io.BytesIO(viewport_screenshot)) as source:
                    source = source.convert("RGB")
                    target = io.BytesIO()
                    source.save(target, "PDF", resolution=96, quality=78)
                    bounded = target.getvalue()
                if len(bounded) <= MAX_REFERENCE_PDF_BYTES:
                    metadata.update({
                        "status": "captured_bounded",
                        "bytes": len(bounded),
                        "page_ranges": "1-1",
                        "delivery_bounded": True,
                        "capture_mode": "rasterized_viewport_screenshot",
                    })
                    return bounded, metadata
            except Exception as exc:
                metadata["raster_fallback_error"] = type(exc).__name__
        if payload is None or len(payload) > MAX_REFERENCE_PDF_BYTES:
            metadata.update({
                "rich_page": metadata.get("rich_page", True),
                "maximum_bytes": MAX_REFERENCE_PDF_BYTES,
            })
            for final_page in (8, 6, 4, 2, 1):
                try:
                    bounded = page.pdf(**options, page_ranges=f"1-{final_page}")
                except Exception:
                    continue
                if len(bounded) <= MAX_REFERENCE_PDF_BYTES:
                    metadata.update({
                        "status": "captured_bounded",
                        "bytes": len(bounded),
                        "page_ranges": f"1-{final_page}",
                        "delivery_bounded": True,
                    })
                    return bounded, metadata
            try:
                raster_payload = viewport_screenshot or page.screenshot(
                    full_page=False, type="jpeg", quality=78, scale="css"
                )
                with PILImage.open(io.BytesIO(raster_payload)) as source:
                    source = source.convert("RGB")
                    pages = [
                        source.crop((0, top, source.width, min(top + 1100, source.height)))
                        for top in range(0, source.height, 1100)
                    ]
                    for final_page in (8, 6, 4, 2, 1):
                        selected = pages[:final_page]
                        if not selected:
                            continue
                        target = io.BytesIO()
                        selected[0].save(
                            target, "PDF", save_all=True, append_images=selected[1:],
                            resolution=96, quality=78,
                        )
                        bounded = target.getvalue()
                        if len(bounded) <= MAX_REFERENCE_PDF_BYTES:
                            metadata.update({
                                "status": "captured_bounded",
                                "bytes": len(bounded),
                                "page_ranges": f"1-{len(selected)}",
                                "delivery_bounded": True,
                                "capture_mode": "rasterized_viewport_screenshot",
                            })
                            return bounded, metadata
            except Exception as exc:
                metadata["raster_fallback_error"] = type(exc).__name__
            metadata.update({"status": "omitted_too_large", "delivery_bounded": False})
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


def _high_density_element_screenshot(page: Any, candidate: Any, scale: float = 4) -> bytes | None:
    """Capture a small rendered wordmark sharply enough for document output."""
    original_style = candidate.get_attribute("style")
    try:
        candidate.evaluate(
            "(el, scale) => { el.style.zoom = String(scale); el.style.transformOrigin = 'top left'; }",
            scale,
        )
        page.wait_for_timeout(30)
        return _remove_flat_logo_background(
            candidate.screenshot(type="png", omit_background=True, timeout=5_000)
        )
    except Exception:
        return None
    finally:
        try:
            candidate.evaluate(
                "(el, style) => style === null ? el.removeAttribute('style') : el.setAttribute('style', style)",
                original_style,
            )
        except Exception:
            pass


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
                  const utilityLink = /(shop|store|careers|privacy|supplier|updates|support|page|go to)/i.test(aria);
                  const exactBrandText = Boolean(
                    brand && !utilityLink && (aria === brand || aria === `${brand} logo` || (text === brand && homeLink))
                  );
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
            payload = _rasterize_svg_on_page(page, str(info["svg"])) if info.get("svg") else None
            if not payload or not _has_visible_logo_pixels(payload):
                descendants = candidate.locator("svg,img")
                capture_candidate = descendants.first if descendants.count() else candidate
                payload = _high_density_element_screenshot(page, capture_candidate)
            if not payload or not _has_visible_logo_pixels(payload):
                payload = _trim_transparent_png(capture_candidate.screenshot(type="png", omit_background=True))
            if not _has_visible_logo_pixels(payload):
                continue
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
        browser = runtime.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100},
            device_scale_factor=1,
            user_agent=BROWSER_USER_AGENT,
            locale="en-US",
        )

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
            navigation_fallback = ""
            response = None
            navigation_error = None
            for _ in range(2):
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                    navigation_error = None
                    break
                except PlaywrightError as exc:
                    navigation_error = exc
                    page.wait_for_timeout(250)
            if navigation_error is None:
                snapshot_url = page.url
                response_status = response.status if response else None
            else:
                if allow_local:
                    raise navigation_error
                snapshot_url, fallback_html, content_type = fetch_limited(
                    url, MAX_HTML_BYTES, accept="text/html,application/xhtml+xml"
                )
                if "html" not in content_type and b"<html" not in fallback_html[:2000].lower():
                    raise navigation_error
                page.close()

                def fulfill_document(route: Any) -> None:
                    route.fulfill(status=200, body=fallback_html, content_type="text/html")

                context.route(snapshot_url, fulfill_document)
                context.route(snapshot_url.rstrip("/") + "/", fulfill_document)
                page = context.new_page()
                response = page.goto(snapshot_url, wait_until="domcontentloaded", timeout=20_000)
                response_status = response.status if response else 200
                navigation_fallback = "bounded_html_fulfilled_at_source_origin"
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1200)
            _freeze_motion(page)
            if not allow_local:
                assert_public_url(snapshot_url)
            overlay_actions = _dismiss_overlays(page)
            visible_logo = _visible_logo(page, snapshot_url)
            snapshot = page.evaluate(
                """() => {
                  const visible = [...document.querySelectorAll('body *')].filter((el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 1 && r.height > 1 && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
                  }).slice(0, 5000);
                  const rows = visible.map((el) => {
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    const before = getComputedStyle(el, '::before'), after = getComputedStyle(el, '::after');
                    return [
                      `color:${s.color}`, `background-color:${s.backgroundColor}`, `border-color:${s.borderColor}`,
                      `font-family:${s.fontFamily}`, `font-size:${s.fontSize}`, `font-weight:${s.fontWeight}`,
                      `line-height:${s.lineHeight}`, `letter-spacing:${s.letterSpacing}`,
                      `padding-top:${s.paddingTop}`, `padding-right:${s.paddingRight}`, `padding-bottom:${s.paddingBottom}`, `padding-left:${s.paddingLeft}`,
                      `margin-top:${s.marginTop}`, `margin-right:${s.marginRight}`, `margin-bottom:${s.marginBottom}`, `margin-left:${s.marginLeft}`,
                      `gap:${s.gap}`, `border-radius:${s.borderRadius}`, `box-shadow:${s.boxShadow}`,
                      `width:${Math.round(r.width)}px`, `max-width:${s.maxWidth}`,
                      `before-background-image:${before.backgroundImage}`, `after-background-image:${after.backgroundImage}`
                    ].join(';');
                  });
                  const viewportArea = Math.max(1, innerWidth * innerHeight);
                  const elements = visible.slice(0, 1800).map((el, index) => {
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    const before = getComputedStyle(el, '::before'), after = getComputedStyle(el, '::after');
                    const clippedWidth = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
                    const clippedHeight = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
                    const areaRatio = Math.round((clippedWidth * clippedHeight / viewportArea) * 10000) / 10000;
                    const tag = el.tagName.toLowerCase();
                    const sourceCandidates = [...el.querySelectorAll('source')].flatMap((source) => {
                      const values = [source.src, source.getAttribute('src') || '', source.getAttribute('srcset') || ''];
                      return values.flatMap((value) => value.split(',').map((part) => part.trim().split(/\\s+/)[0])).filter(Boolean);
                    }).slice(0, 12);
                    const pseudoBackgrounds = [before.backgroundImage, after.backgroundImage]
                      .filter((value) => value && value !== 'none');
                    const captureable = ['img', 'picture', 'video'].includes(tag)
                      || (s.backgroundImage && s.backgroundImage !== 'none') || pseudoBackgrounds.length > 0;
                    const captureKey = captureable && areaRatio >= .08 ? `asset-${index}` : '';
                    if (captureKey) el.setAttribute('data-kolo-capture-key', captureKey);
                    const embeddedTextElements = [...el.querySelectorAll('h1,h2,h3,h4,p,a,button,[role="button"]')]
                      .filter((child) => (child.innerText || child.getAttribute('aria-label') || '').trim()).length;
                    const embeddedInteractiveElements = el.querySelectorAll('a,button,input,select,textarea,[role="button"]').length;
                    const embeddedNavigationElements = el.querySelectorAll('nav,header,[role="navigation"]').length;
                    const embeddedTextCharacters = (el.innerText || '').trim().replace(/\\s+/g, ' ').length;
                    const region = el.closest('[role="dialog"],dialog,header,nav,main,footer,section,article,aside');
                    const hint = [el.id, el.className, el.getAttribute('role'), el.getAttribute('aria-label')]
                      .filter((value) => typeof value === 'string').join(' ').toLowerCase();
                    const overlayHint = /(cookie|consent|privacy|modal|dialog|overlay|tracking|preference)/.test(hint);
                    const overlayPosition = ['fixed', 'sticky'].includes(s.position) && clippedWidth * clippedHeight > viewportArea * 0.08;
                    return {
                      tag,
                      role: el.getAttribute('role') || '',
                      href: Boolean(el.getAttribute('href')),
                      src: el.currentSrc || el.getAttribute('src') || '',
                      poster: el.poster || el.getAttribute('poster') || '',
                      source_candidates: sourceCandidates,
                      pseudo_background_images: pseudoBackgrounds,
                      capture_key: captureKey,
                      embedded_text_characters: embeddedTextCharacters,
                      embedded_text_elements: embeddedTextElements,
                      embedded_interactive_elements: embeddedInteractiveElements,
                      embedded_navigation_elements: embeddedNavigationElements,
                      alt: el.getAttribute('alt') || '',
                      text_sample: (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
                      rect: { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) },
                      viewport: {
                        visible: clippedWidth > 0 && clippedHeight > 0,
                        area_ratio: areaRatio
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
            snapshot["url"] = snapshot_url
            snapshot["response_status"] = response_status
            snapshot["navigation_fallback"] = navigation_fallback
            snapshot["overlay_actions"] = overlay_actions
            snapshot["visible_logo"] = visible_logo
            snapshot["screenshot"] = page.screenshot(full_page=False, type="png")
            captured_assets: list[dict[str, Any]] = []
            seen_captures: set[str] = set()
            capture_candidates = sorted(
                (item for item in snapshot["elements"] if item.get("capture_key")),
                key=lambda item: float((item.get("viewport") or {}).get("area_ratio", 0)),
                reverse=True,
            )
            for item in capture_candidates:
                try:
                    locator = page.locator(f'[data-kolo-capture-key="{item["capture_key"]}"]').first
                    payload = locator.screenshot(type="png", omit_background=False, timeout=5_000)
                    digest = hashlib.sha256(payload).hexdigest()
                    if digest in seen_captures:
                        continue
                    seen_captures.add(digest)
                    captured_assets.append({
                        "kind": "hero-image", "path": f"browser-capture-{len(captured_assets) + 1}.png",
                        "payload": payload, "media_type": "image/png",
                        "source_url": item.get("poster") or item.get("src") or snapshot_url,
                        "alt": item.get("alt", ""), "text_sample": item.get("text_sample", ""),
                        "role": str((item.get("semantic") or {}).get("region", "main")),
                        "score": round(float((item.get("viewport") or {}).get("area_ratio", 0)) * 100, 2),
                        "capture_kind": "rendered-element",
                        "capture_tag": item.get("tag"),
                        "embedded_text_characters": item.get("embedded_text_characters", 0),
                        "embedded_text_elements": item.get("embedded_text_elements", 0),
                        "embedded_interactive_elements": item.get("embedded_interactive_elements", 0),
                        "embedded_navigation_elements": item.get("embedded_navigation_elements", 0),
                    })
                    if len(captured_assets) >= 6:
                        break
                except (PlaywrightError, PlaywrightTimeoutError):
                    continue
            snapshot["captured_assets"] = captured_assets
            reference_pdf, reference_pdf_metadata = _reference_pdf(
                page, viewport_screenshot=snapshot["screenshot"]
            )
            snapshot["reference_pdf"] = reference_pdf
            snapshot["reference_pdf_metadata"] = reference_pdf_metadata
            return snapshot
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass
            try:
                browser.close()
            except PlaywrightError:
                pass
