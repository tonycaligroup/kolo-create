from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class DocumentPlanner(Protocol):
    version: str

    def plan(self, content: str, prompt: str) -> dict[str, Any]: ...


def _source_title(content: str) -> str:
    for line in content.splitlines():
        clean = re.sub(r"^#+\s*", "", line).strip()
        if clean:
            return clean[:100]
    return "Designed Document"


def _source_subtitle(content: str) -> str:
    paragraph: list[str] = []
    for line in content.splitlines():
        clean = line.strip()
        if clean.startswith("#") or re.match(r"^[-*•]\s+", clean):
            if paragraph:
                break
            continue
        if not clean:
            if paragraph:
                break
            continue
        paragraph.append(clean)
    value = " ".join(paragraph) or "A designed document"
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
    if chosen:
        return " ".join(chosen)
    return value[:190].rsplit(" ", 1)[0]


@dataclass(frozen=True)
class DeterministicPlanner:
    version: str = "deterministic-planner/1"

    def plan(self, content: str, prompt: str) -> dict[str, Any]:
        lowered = prompt.lower()
        style = "editorial"
        for candidate in ("minimal", "bold", "executive", "editorial"):
            if candidate in lowered:
                style = candidate
                break
        title_match = re.search(r"(?:title|called)\s+[\"']?([^\n\"']{3,100})", prompt, re.I)
        title = title_match.group(1).strip(" .") if title_match else _source_title(content)
        return {
            "schema_version": 1,
            "title": title,
            "subtitle": _source_subtitle(content),
            "style": style,
            "page_size": "A4" if "a4" in lowered else "LETTER",
            "orientation": "landscape" if "landscape" in lowered else "portrait",
            "source_policy": "preserve",
        }


@dataclass(frozen=True)
class OpenAICompatiblePlanner:
    base_url: str
    model: str
    token: str
    version: str = "openai-compatible-planner/1"

    @classmethod
    def from_environment(cls, model: str) -> "OpenAICompatiblePlanner":
        base_url = os.environ.get("KOLO_LLM_BASE_URL", "").rstrip("/")
        token = os.environ.get("KOLO_LLM_TOKEN", "")
        if not base_url or not token:
            raise ValueError("KOLO_LLM_BASE_URL and KOLO_LLM_TOKEN are required for --planner llm")
        return cls(base_url=base_url, model=model, token=token)

    def plan(self, content: str, prompt: str) -> dict[str, Any]:
        system = (
            "Return JSON only. Plan a polished PDF without inventing facts. Required keys: "
            "schema_version=1, title, subtitle, style (minimal|bold|executive|editorial), "
            "page_size (LETTER|A4), orientation (portrait|landscape), source_policy='preserve'."
        )
        response = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "model": self.model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"DESIGN REQUEST:\n{prompt}\n\nSOURCE CONTENT:\n{content[:60000]}"},
                ],
            },
            timeout=45,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        plan = json.loads(raw)
        required = {"schema_version", "title", "subtitle", "style", "page_size", "orientation", "source_policy"}
        if required - plan.keys() or plan["schema_version"] != 1 or plan["source_policy"] != "preserve":
            raise ValueError("LLM planner returned an invalid document plan")
        return plan
