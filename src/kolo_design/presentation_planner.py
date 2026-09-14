from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


ALLOWED_ARCHETYPES = {
    "cover",
    "statement",
    "process",
    "feature-list",
    "image-led",
    "section",
    "closing",
}


class PresentationPlanner(Protocol):
    version: str

    def plan(self, content: str, prompt: str, source_blocks: list[dict[str, str]]) -> dict[str, Any]: ...


def _sections(blocks: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    groups: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for block in blocks:
        if block["kind"] == "heading2" and current:
            groups.append(current)
            current = []
        current.append(block)
    if current:
        groups.append(current)
    return groups


def _archetype(group: list[dict[str, str]], is_last: bool) -> str:
    if is_last:
        return "closing"
    kinds = [block["kind"] for block in group]
    bullets = [block for block in group if block["kind"] == "bullet"]
    if len(bullets) >= 3 and all(block["text"].lstrip("* ").split(":", 1)[0].strip("* ").startswith(("01.", "02.", "03.", "1.", "2.", "3.")) for block in bullets[:3]):
        return "process"
    if len(bullets) >= 2:
        return "feature-list"
    if "callout" in kinds:
        return "statement"
    if kinds.count("paragraph") >= 2:
        return "statement"
    return "section"


def validate_presentation_plan(plan: dict[str, Any], blocks: list[dict[str, str]]) -> None:
    if plan.get("schema_version") != 1 or plan.get("source_policy") != "preserve":
        raise ValueError("Planner returned an invalid presentation-plan envelope")
    slides = plan.get("slides")
    if not isinstance(slides, list) or len(slides) < 2:
        raise ValueError("Planner returned too few slides")
    if slides[0].get("archetype") != "cover":
        raise ValueError("The first slide must be a cover")
    planned: list[str] = []
    for slide in slides:
        if slide.get("archetype") not in ALLOWED_ARCHETYPES:
            raise ValueError("Planner returned an unsupported slide archetype")
        ids = slide.get("block_ids")
        if not isinstance(ids, list):
            raise ValueError("Planner returned invalid slide block ownership")
        planned.extend(ids)
    expected = [block["id"] for block in blocks]
    if len(planned) != len(set(planned)) or set(planned) != set(expected):
        raise ValueError("Planner must use every source block exactly once")


@dataclass(frozen=True)
class DeterministicPresentationPlanner:
    version: str = "deterministic-presentation-planner/1"

    def plan(self, content: str, prompt: str, source_blocks: list[dict[str, str]]) -> dict[str, Any]:
        groups = _sections(source_blocks)
        lead = groups[0]
        title = next((b["text"] for b in lead if b["kind"] == "heading1"), source_blocks[0]["text"])
        subtitle = next((b["text"] for b in lead if b["kind"] in {"callout", "paragraph"}), "")
        slides: list[dict[str, Any]] = [{
            "id": "slide-01",
            "archetype": "cover",
            "title": title,
            "subtitle": subtitle,
            "block_ids": [b["id"] for b in lead],
        }]
        remaining = groups[1:]
        for index, group in enumerate(remaining, 2):
            heading = next((b["text"] for b in group if b["kind"].startswith("heading")), f"Section {index - 1}")
            slides.append({
                "id": f"slide-{index:02d}",
                "archetype": _archetype(group, index == len(remaining) + 1),
                "title": heading,
                "block_ids": [b["id"] for b in group],
            })
        plan = {
            "schema_version": 1,
            "title": title,
            "subtitle": subtitle,
            "aspect_ratio": "16:9",
            "source_policy": "preserve",
            "slides": slides,
        }
        validate_presentation_plan(plan, source_blocks)
        return plan


@dataclass(frozen=True)
class OpenAICompatiblePresentationPlanner:
    base_url: str
    model: str
    token: str
    version: str = "openai-compatible-presentation-planner/1"

    @classmethod
    def from_environment(cls, model: str) -> "OpenAICompatiblePresentationPlanner":
        base_url = os.environ.get("KOLO_LLM_BASE_URL", "").rstrip("/")
        token = os.environ.get("KOLO_LLM_TOKEN", "")
        if not base_url or not token:
            raise ValueError("KOLO_LLM_BASE_URL and KOLO_LLM_TOKEN are required for --planner llm")
        return cls(base_url, model, token)

    def plan(self, content: str, prompt: str, source_blocks: list[dict[str, str]]) -> dict[str, Any]:
        system = (
            "Return JSON only. Arrange every source block ID exactly once into a 16:9 presentation. "
            "Do not write or rewrite copy. Required: schema_version=1, title, subtitle, aspect_ratio='16:9', "
            "source_policy='preserve', and slides. Each slide needs id, title, archetype and block_ids. "
            f"Allowed archetypes: {sorted(ALLOWED_ARCHETYPES)}. The first slide must be cover. "
            "Prefer six to eight slides, one clear subject per slide, and no repeated block IDs."
        )
        response = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"model": self.model, "response_format": {"type": "json_object"}, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"design_request": prompt, "source_blocks": source_blocks})},
            ]},
            timeout=45,
        )
        response.raise_for_status()
        plan = response.json()["choices"][0]["message"]["content"]
        plan = json.loads(plan)
        validate_presentation_plan(plan, source_blocks)
        return plan
