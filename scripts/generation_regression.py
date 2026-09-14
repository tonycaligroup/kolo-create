from __future__ import annotations

import argparse
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kolo_design.content_map import build_content_map, validate_content_map
from kolo_design.design_grammar import compile_design_grammar, validate_design_grammar
from kolo_design.design_quality import evaluate_design_plan
from kolo_design.planner import source_blocks
from kolo_design.scene_graph import build_scene_plan, validate_scene_plan
from kolo_design.util import write_json


def _named_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Expected NAME=/path/to/file")
    name, raw_path = value.split("=", 1)
    path = Path(raw_path).resolve()
    if not name.strip() or not path.is_file():
        raise argparse.ArgumentTypeError(f"Missing named file: {value}")
    return name.strip(), path


def _similarity(left: list[str], right: list[str]) -> float:
    slots = max(len(left), len(right), 1)
    return round(sum(a == b for a, b in zip(left, right)) / slots, 4)


def run(systems: list[tuple[str, Path]], contents: list[tuple[str, Path]], output: Path) -> dict[str, Any]:
    cases = []
    for (brand_name, system_path), (content_name, content_path), format_name in itertools.product(
        systems, contents, ("document", "presentation")
    ):
        system = json.loads(system_path.read_text(encoding="utf-8"))
        blocks = source_blocks(content_path.read_text(encoding="utf-8"))
        grammar = system.get("design_grammar") or compile_design_grammar(system)
        validate_design_grammar(grammar)
        content_map = build_content_map(blocks, f"Regression case: {content_name}")
        validate_content_map(content_map, blocks)
        scene_plan = build_scene_plan(
            grammar, content_map, format_name=format_name, brand_id=system["id"]
        )
        validate_scene_plan(scene_plan, blocks)
        quality = evaluate_design_plan(grammar, scene_plan)
        cases.append({
            "id": f"{brand_name}:{content_name}:{format_name}",
            "brand": brand_name,
            "content": content_name,
            "format": format_name,
            "system": str(system_path),
            "grammar_signature": grammar["signature"],
            "ranked_directions": grammar["ranked_directions"],
            "scene_signature": scene_plan["signature"],
            "components": [scene["component"] for scene in scene_plan["scenes"]],
            "directions": [scene["direction"] for scene in scene_plan["scenes"]],
            "quality": quality,
            "model_calls": 0,
        })

    comparisons = []
    for content_name, format_name in itertools.product(
        [name for name, _ in contents], ("document", "presentation")
    ):
        selected = [case for case in cases if case["content"] == content_name and case["format"] == format_name]
        for left, right in itertools.combinations(selected, 2):
            comparisons.append({
                "content": content_name,
                "format": format_name,
                "left": left["brand"],
                "right": right["brand"],
                "component_similarity": _similarity(left["components"], right["components"]),
                "direction_similarity": _similarity(left["directions"], right["directions"]),
            })
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "planner": "generation-v2-scene-candidate/1",
        "model_calls": 0,
        "case_count": len(cases),
        "cases": cases,
        "comparisons": comparisons,
    }
    write_json(output.resolve(), report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a no-model Kolo Create generation regression manifest")
    parser.add_argument("--system", action="append", required=True, type=_named_path)
    parser.add_argument("--content", action="append", required=True, type=_named_path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.system, args.content, args.output)
    print(json.dumps({
        "status": "succeeded", "output": str(args.output.resolve()),
        "cases": report["case_count"], "model_calls": report["model_calls"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
