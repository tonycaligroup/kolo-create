from __future__ import annotations

import json
from pathlib import Path

import pytest

import kolo_design.brand_director as brand_director_module
from kolo_design.brand_components import select_component_plan
from kolo_design.brand_director import DEFAULT_BRAND_MODEL, apply_brand_direction
from kolo_design.planner import DeterministicPlanner, source_blocks
from kolo_design.util import read_json, write_json


FIXTURES = Path(__file__).parent / "fixtures"


def _judgment(asset_id: str = "candidate-logo") -> dict[str, object]:
    return {
        "schema_version": 1,
        "brand_name": "Fixture Brand",
        "identity_confidence": 0.94,
        "dominant_character": ["restrained", "editorial"],
        "accent_role": "minor-functional",
        "primary_visual_mode": "typography-led",
        "palette": {"background": "#FFFFFF", "surface": "#F4F4F2", "text": "#111111", "accent": "#FF00FF"},
        "logo_decisions": [{"asset_id": asset_id, "decision": "reject", "reason": "unrelated navigation mark"}],
        "hero_decisions": [],
        "composition": {"prefer": ["open editorial space"], "avoid": ["generic repeated rules"]},
        "motif_strategy": {"page_marker": "cover-only", "section_markers": "selective", "max_repeated_motif_per_page": 1},
        "summary": "A quiet typography-led system.",
    }


def _judgment_with_hero(hero_id: str, production_use: str) -> dict[str, object]:
    value = _judgment()
    value["hero_decisions"] = [{
        "asset_id": hero_id, "decision": "accept",
        "production_use": production_use, "reason": "webpage chrome is embedded",
    }]
    return value


def _system(tmp_path: Path) -> tuple[Path, Path]:
    system = read_json(FIXTURES / "design-system.json")
    system["assets"] = [{"id": "candidate-logo", "kind": "logo", "path": str(tmp_path / "logo.png")}]
    system["evidence"]["colors"] = [
        {"value": "#FFFFFF", "occurrences": 100, "reference_status": "supported"},
        {"value": "#F4F4F2", "occurrences": 80, "reference_status": "supported"},
        {"value": "#111111", "occurrences": 70, "reference_status": "supported"},
    ]
    system_path = write_json(tmp_path / "v1" / "design-system.json", system)
    latest_path = tmp_path / "latest.json"
    return system_path, latest_path


def test_brand_director_applies_only_supported_colors_and_rejects_assets(tmp_path: Path) -> None:
    system_path, latest_path = _system(tmp_path)
    calls: list[str] = []

    def invoke(model: str, prompt: str, images: list[Path]) -> str:
        calls.append(model)
        assert "Do not invent assets, colors" in prompt
        return json.dumps(_judgment())

    result = apply_brand_direction(system_path, latest_path, mode="llm", invoke=invoke)
    system = read_json(system_path)
    assert result["status"] == "succeeded"
    assert calls == [DEFAULT_BRAND_MODEL]
    assert system["tokens"]["colors"]["background"] == "#FFFFFF"
    assert system["tokens"]["colors"]["accent"] != "#FF00FF"
    assert system["assets"][0]["director_eligible"] is False
    assert system["brand_direction"]["motif_strategy"]["section_markers"] == "selective"
    assert latest_path.exists()
    assert (system_path.parent / "brand-direction.json").exists()


def test_brand_director_repairs_one_invalid_structured_response(tmp_path: Path) -> None:
    system_path, latest_path = _system(tmp_path)
    responses = iter(["not json", json.dumps(_judgment())])
    result = apply_brand_direction(
        system_path, latest_path, mode="llm",
        invoke=lambda model, prompt, images: next(responses),
    )
    assert result["attempts"] == 2


def test_brand_director_rejects_unknown_asset_ids(tmp_path: Path) -> None:
    system_path, latest_path = _system(tmp_path)
    with pytest.raises(Exception, match="invalid structured output"):
        apply_brand_direction(
            system_path, latest_path, mode="llm",
            invoke=lambda model, prompt, images: json.dumps(_judgment("invented-logo")),
        )


def test_brand_director_cannot_override_deterministic_reference_only_media(tmp_path: Path) -> None:
    system_path, latest_path = _system(tmp_path)
    system = read_json(system_path)
    system["assets"].append({
        "id": "composite-hero", "kind": "hero-image", "path": str(tmp_path / "hero.png"),
        "asset_class": "reference-evidence", "production_eligible": False,
    })
    write_json(system_path, system)
    result = apply_brand_direction(
        system_path, latest_path, mode="llm",
        invoke=lambda model, prompt, images: json.dumps(_judgment_with_hero("composite-hero", "allow")),
    )
    assert result["status"] == "succeeded"
    updated = read_json(system_path)
    hero = next(asset for asset in updated["assets"] if asset["id"] == "composite-hero")
    assert hero["production_eligible"] is False


def test_direct_brand_call_omits_model_specific_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "{}"}}]}

    def fake_post(*args: object, **kwargs: object) -> Response:
        captured.update(kwargs)
        return Response()

    monkeypatch.setenv("LITELLM_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-token")
    monkeypatch.setattr(brand_director_module.httpx, "post", fake_post)
    brand_director_module._invoke_direct(DEFAULT_BRAND_MODEL, "Review", [])
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert "temperature" not in payload


def test_selective_section_markers_are_not_repeated_on_every_section(tmp_path: Path) -> None:
    system_path, _ = _system(tmp_path)
    system = read_json(system_path)
    system["brand_direction"] = _judgment()
    content = "# Title\n\n## One\n\nCopy.\n\n## Two\n\nCopy.\n\n## Three\n\nCopy.\n\n## Four\n\nCopy."
    blocks = source_blocks(content)
    plan = DeterministicPlanner().plan(content, "Create an editorial brief", blocks)
    component_plan = select_component_plan(system, plan, blocks)
    marked = [section for section in component_plan["sections"] if "section-marker" in section["components"]]
    assert 0 < len(marked) <= 2
    assert len(marked) < len(component_plan["sections"])
