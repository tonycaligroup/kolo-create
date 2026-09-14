from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from .network import FetchError


BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("access_denied", re.compile(r"\baccess denied\b|you (?:do not|don't) have permission to access", re.I)),
    ("request_blocked", re.compile(r"\brequest (?:was )?blocked\b|\bthe requested url was rejected\b", re.I)),
    ("forbidden", re.compile(r"(?:^|\W)403\s+forbidden\b|\bforbidden\s+access\b", re.I)),
    ("human_verification", re.compile(r"\bverify (?:that )?you are human\b|\bcomplete the captcha\b|\bunusual traffic\b", re.I)),
    ("security_challenge", re.compile(r"\bchecking your browser\b|\battention required[!\s|]*cloudflare\b|\bsecurity check\b", re.I)),
    ("service_error", re.compile(r"\bservice unavailable\b|\btemporarily unavailable\b|\bgateway timeout\b", re.I)),
)


class SourceFidelityError(FetchError):
    """Raised when a capture is an error/interstitial rather than the intended site."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def evaluate_source_fidelity(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Deterministically reject blocked/error captures before brand inference."""
    html = str(snapshot.get("html") or "")
    title = str(snapshot.get("title") or "")
    elements = snapshot.get("elements") if isinstance(snapshot.get("elements"), list) else []
    samples = {
        str(item.get("text_sample") or "").strip()
        for item in elements
        if isinstance(item, dict) and str(item.get("text_sample") or "").strip()
    }
    soup_text = BeautifulSoup(html[:8_000_000], "html.parser").get_text(" ", strip=True)
    visible_text = " ".join(sorted(samples))
    diagnostic_text = re.sub(r"\s+", " ", f"{title} {visible_text} {soup_text[:12000]}").strip()
    status = snapshot.get("response_status")
    reasons: list[str] = []
    if isinstance(status, int) and status >= 400:
        reasons.append(f"http_status_{status}")
    for code, pattern in BLOCK_PATTERNS:
        if pattern.search(diagnostic_text):
            reasons.append(code)

    image_count = sum(
        1 for item in elements
        if isinstance(item, dict) and item.get("tag") in {"img", "picture", "video"}
    )
    signals = {
        "response_status": status,
        "html_bytes": len(html.encode("utf-8", errors="replace")),
        "visible_elements": len(elements),
        "unique_text_characters": len(visible_text),
        "document_text_characters": len(soup_text),
        "visible_media_elements": image_count,
        "title": title[:240],
    }
    if reasons:
        state = "blocked"
    elif len(soup_text) < 40 and len(elements) < 3 and image_count == 0:
        state = "insufficient"
        reasons.append("too_little_rendered_evidence")
    else:
        state = "pass"
    return {
        "schema": "kolo.source-fidelity/v1",
        "status": state,
        "usable": state == "pass",
        "reasons": sorted(set(reasons)),
        "signals": signals,
    }


def ensure_source_fidelity(snapshot: dict[str, Any]) -> dict[str, Any]:
    report = evaluate_source_fidelity(snapshot)
    snapshot["source_fidelity"] = report
    if not report["usable"]:
        reason = ", ".join(report["reasons"])
        raise SourceFidelityError(
            f"The captured page is not reliable brand evidence ({reason}).",
            report,
        )
    return report
