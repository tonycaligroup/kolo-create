from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


ALLOWED_STYLES = {"minimal", "bold", "executive", "editorial"}
ALLOWED_SECTION_VARIANTS = {"standard", "card_grid", "callout", "actions"}


class DocumentPlanner(Protocol):
    version: str

    def plan(self, content: str, prompt: str, source_blocks: list[dict[str, str]]) -> dict[str, Any]: ...


def source_blocks(content: str) -> list[dict[str, str]]:
    """Parse source once so planners can arrange, but never rewrite, its content."""
    parsed: list[tuple[str, str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            parsed.append(("paragraph", " ".join(paragraph).strip()))
            paragraph.clear()

    for raw in content.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("### "):
            flush(); parsed.append(("heading3", line[4:].strip()))
        elif line.startswith("## "):
            flush(); parsed.append(("heading2", line[3:].strip()))
        elif line.startswith("# "):
            flush(); parsed.append(("heading1", line[2:].strip()))
        elif line.startswith("> "):
            flush(); parsed.append(("callout", line[2:].strip()))
        elif re.match(r"^[-*•]\s+", line):
            flush(); parsed.append(("bullet", re.sub(r"^[-*•]\s+", "", line)))
        elif re.fullmatch(r"\[[^]\n]+\]\(https?://[^)\s]+\)", line):
            flush(); parsed.append(("action", line))
        else:
            paragraph.append(line)
    flush()
    return [{"id": f"b{index:03d}", "kind": kind, "text": text} for index, (kind, text) in enumerate(parsed, 1)]


def _source_title(blocks: list[dict[str, str]]) -> str:
    return (blocks[0]["text"][:100] if blocks else "Designed Document")


def _source_subtitle(blocks: list[dict[str, str]]) -> str:
    value = next((block["text"] for block in blocks if block["kind"] == "paragraph"), "A designed document")
    if len(value) <= 190:
        return value
    sentences = re.split(r"(?<=[.!?])\s+", value)
    chosen: list[str] = []
    for sentence in sentences:
        if chosen and len(" ".join(chosen + [sentence])) > 190:
            break
        if len(sentence) > 190:
            break
        chosen.append(sentence)
    return " ".join(chosen) if chosen else value[:190].rsplit(" ", 1)[0]


def _requested_title(prompt: str) -> str | None:
    quoted = re.search(r"(?:title|called)\s+([\"'])([^\n\"']{3,100})\1", prompt, re.I)
    if quoted:
        return quoted.group(2).strip(" .")
    unquoted = re.search(r"(?:title|called)\s+([^\n]{3,100}?)(?=\s+(?:with|using|in|as)\b|$)", prompt, re.I)
    return unquoted.group(1).strip(" .") if unquoted else None


def _group_sections(blocks: list[dict[str, str]], style: str, prompt: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: list[str] = []
    section_index = 1
    by_id = {block["id"]: block for block in blocks}

    def flush() -> None:
        nonlocal current, section_index
        if not current:
            return
        kinds = [by_id[block_id]["kind"] for block_id in current]
        content_kinds = [kind for kind in kinds if not kind.startswith("heading")]
        variant = "standard"
        if content_kinds and all(kind == "callout" for kind in content_kinds):
            variant = "callout"
        elif content_kinds and all(kind == "action" for kind in content_kinds):
            variant = "actions"
        elif content_kinds.count("bullet") >= 2 and (
            style in {"bold", "executive"} or "card" in prompt.lower() or "feature" in prompt.lower()
        ):
            variant = "card_grid"
        sections.append({"id": f"s{section_index:02d}", "variant": variant, "block_ids": current})
        section_index += 1
        current = []

    for block in blocks:
        if block["kind"] in {"heading1", "heading2"} and current:
            flush()
        current.append(block["id"])
    flush()
    return sections


def validate_plan(plan: dict[str, Any], blocks: list[dict[str, str]]) -> None:
    required = {"schema_version", "title", "subtitle", "style", "page_size", "orientation", "source_policy", "layout"}
    if required - plan.keys() or plan.get("schema_version") != 2:
        raise ValueError("Planner returned an invalid document-plan envelope")
    if plan.get("source_policy") != "preserve":
        raise ValueError("Planner must preserve source content")
    if plan.get("style") not in ALLOWED_STYLES:
        raise ValueError("Planner returned an unsupported style")
    if plan.get("page_size") not in {"LETTER", "A4"} or plan.get("orientation") not in {"portrait", "landscape"}:
        raise ValueError("Planner returned an unsupported page format")
    sections = plan.get("layout", {}).get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("Planner returned no layout sections")
    planned_ids: list[str] = []
    for section in sections:
        if section.get("variant") not in ALLOWED_SECTION_VARIANTS or not isinstance(section.get("block_ids"), list):
            raise ValueError("Planner returned an invalid section")
        planned_ids.extend(section["block_ids"])
    source_ids = [block["id"] for block in blocks]
    if len(planned_ids) != len(set(planned_ids)):
        raise ValueError("Planner repeated source blocks")
    if set(planned_ids) != set(source_ids):
        raise ValueError("Planner omitted or invented source blocks")


@dataclass(frozen=True)
class DeterministicPlanner:
    version: str = "deterministic-planner/2"

    def plan(self, content: str, prompt: str, source_blocks: list[dict[str, str]]) -> dict[str, Any]:
        lowered = prompt.lower()
        style = next((candidate for candidate in ("minimal", "bold", "executive", "editorial") if candidate in lowered), "editorial")
        requested_title = _requested_title(prompt)
        plan = {
            "schema_version": 2,
            "title": requested_title or _source_title(source_blocks),
            "subtitle": _source_subtitle(source_blocks),
            "style": style,
            "page_size": "A4" if "a4" in lowered else "LETTER",
            "orientation": "landscape" if "landscape" in lowered else "portrait",
            "source_policy": "preserve",
            "layout": {"template": "brand-editorial", "sections": _group_sections(source_blocks, style, prompt)},
        }
        validate_plan(plan, source_blocks)
        return plan


@dataclass(frozen=True)
class OpenAICompatiblePlanner:
    base_url: str
    model: str
    token: str
    version: str = "openai-compatible-planner/2"

    @classmethod
    def from_environment(cls, model: str) -> "OpenAICompatiblePlanner":
        base_url = os.environ.get("KOLO_LLM_BASE_URL", "").rstrip("/")
        token = os.environ.get("KOLO_LLM_TOKEN", "")
        if not base_url or not token:
            raise ValueError("KOLO_LLM_BASE_URL and KOLO_LLM_TOKEN are required for --planner llm")
        return cls(base_url=base_url, model=model, token=token)

    def plan(self, content: str, prompt: str, source_blocks: list[dict[str, str]]) -> dict[str, Any]:
        system = (
            "Return JSON only. Create a layout plan, not new copy. Use every supplied block ID exactly once and never invent IDs. "
            "Required schema_version=2, title, subtitle, style (minimal|bold|executive|editorial), page_size "
            "(LETTER|A4), orientation (portrait|landscape), source_policy='preserve', and layout with template and sections. "
            "Each section requires id, variant (standard|card_grid|callout|actions), and block_ids. "
            "Prefer card_grid for compact feature/benefit lists, callout for quoted emphasis, and actions for link-only blocks."
        )
        response = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "model": self.model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps({"design_request": prompt, "source_blocks": source_blocks})},
                ],
            },
            timeout=45,
        )
        response.raise_for_status()
        plan = json.loads(response.json()["choices"][0]["message"]["content"])
        requested_title = _requested_title(prompt)
        # Copy remains code-owned. The model may arrange block IDs, but it may not author cover copy.
        plan["title"] = requested_title or _source_title(source_blocks)
        plan["subtitle"] = _source_subtitle(source_blocks)
        validate_plan(plan, source_blocks)
        return plan
