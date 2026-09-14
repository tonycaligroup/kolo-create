from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import sync_playwright

from .browser_extract import _browser_executable


def _clean(value: str) -> str:
    return (
        re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`", lambda match: next(part for part in match.groups() if part), value)
        .strip()
    )


def _split_feature(value: str) -> tuple[str, str]:
    value = re.sub(r"^\d{1,2}[.)]\s*", "", _clean(value))
    index = value.find(":")
    return (value[:index], value[index + 1 :].strip()) if 0 < index < 58 else (value, "")


def _measure_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not requests:
        return []
    browser_path = _browser_executable()
    if not browser_path:
        raise RuntimeError("Chromium is required for deterministic presentation text measurement")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=browser_path)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.set_content("<!doctype html><html><body></body></html>")
            return page.evaluate(
                """requests => requests.map(request => {
                    const node = document.createElement('div');
                    Object.assign(node.style, {
                      position: 'absolute', visibility: 'hidden', left: '0', top: '0',
                      width: `${request.width}px`, height: 'auto', margin: '0', padding: '0',
                      fontFamily: request.fontFamily, fontSize: `${request.fontPx}px`,
                      fontWeight: String(request.weight), lineHeight: String(request.lineHeight),
                      whiteSpace: 'normal', overflowWrap: 'normal', wordBreak: 'normal'
                    });
                    node.textContent = request.text;
                    document.body.appendChild(node);
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    const lineTops = [...range.getClientRects()].map(rect => Math.round(rect.top * 10) / 10);
                    const result = {
                      ...request,
                      height: Math.ceil(node.getBoundingClientRect().height),
                      lineCount: new Set(lineTops).size || 1,
                    };
                    node.remove();
                    return result;
                })""",
                requests,
            )
        finally:
            browser.close()


def measure_presentation_layout(
    system: dict[str, Any], plan: dict[str, Any], blocks: list[dict[str, str]]
) -> dict[str, Any]:
    """Measure adaptive presentation components with the actual browser text engine."""
    profile = str((plan.get("art_direction") or {}).get("profile", "precision"))
    display_font = "Georgia" if system["tokens"]["typography"].get("display_fallback") == "serif" else "Arial"
    body_font = "Georgia" if system["tokens"]["typography"].get("body_fallback") == "serif" else "Arial"
    block_map = {block["id"]: block for block in blocks}
    requests: list[dict[str, Any]] = []
    card_refs: list[tuple[str, int, str, str]] = []
    if profile == "kinetic":
        for slide in plan.get("slides", []):
            if slide.get("archetype") != "feature-list":
                continue
            bullets = [
                block_map[block_id]
                for block_id in slide.get("block_ids", [])
                if block_id in block_map and block_map[block_id]["kind"] == "bullet"
            ][:6]
            for index, block in enumerate(bullets):
                label, detail = _split_feature(block["text"])
                lead = index == 0
                width = (470 if lead else 590) - 64
                label_key = f"{slide['id']}:card:{index}:label"
                detail_key = f"{slide['id']}:card:{index}:detail"
                requests.extend([
                    {
                        "key": label_key, "text": label, "width": width,
                        "fontFamily": display_font, "fontPx": (23 if lead else 19) * 96 / 72,
                        "weight": 700, "lineHeight": 1.18,
                    },
                    {
                        "key": detail_key, "text": detail, "width": width,
                        "fontFamily": body_font, "fontPx": (16 if lead else 12) * 96 / 72,
                        "weight": 400, "lineHeight": 1.18,
                    },
                ])
                card_refs.append((slide["id"], index, label_key, detail_key))
    measured = {item["key"]: item for item in _measure_requests(requests)}
    slides: dict[str, Any] = {}
    for slide_id, index, label_key, detail_key in card_refs:
        slides.setdefault(slide_id, {"cards": []})["cards"].append({
            "index": index,
            "label_height": measured[label_key]["height"],
            "label_lines": measured[label_key]["lineCount"],
            "detail_height": measured[detail_key]["height"],
            "detail_lines": measured[detail_key]["lineCount"],
        })
    return {
        "schema_version": 1,
        "engine": "chromium-dom/1",
        "profile": profile,
        "fonts": {"display": display_font, "body": body_font},
        "request_count": len(requests),
        "slides": slides,
    }
