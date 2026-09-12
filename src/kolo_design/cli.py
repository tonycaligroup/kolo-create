from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extractor import extract_brand
from .pdf_designer import create_pdf
from .planner import DeterministicPlanner, OpenAICompatiblePlanner


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kolo-design")
    commands = root.add_subparsers(dest="command", required=True)
    brand = commands.add_parser("brand", help="Create a reusable design system from a website")
    brand_commands = brand.add_subparsers(dest="brand_command", required=True)
    extract = brand_commands.add_parser("extract")
    extract.add_argument("--url", required=True)
    extract.add_argument("--workspace", type=Path, required=True)
    extract.add_argument("--name")

    pdf = commands.add_parser("pdf", help="Design a PDF with a saved design system")
    pdf_commands = pdf.add_subparsers(dest="pdf_command", required=True)
    create = pdf_commands.add_parser("create")
    create.add_argument("--system", type=Path, required=True)
    create.add_argument("--content", type=Path, required=True)
    create.add_argument("--prompt", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--planner", choices=("deterministic", "llm"), default="deterministic")
    create.add_argument("--model", help="Workspace-entitled model ID; required with --planner llm")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "brand":
            result = extract_brand(args.url, args.workspace, args.name)
        else:
            if args.planner == "llm":
                if not args.model:
                    raise ValueError("--model is required with --planner llm")
                planner_impl = OpenAICompatiblePlanner.from_environment(args.model)
            else:
                planner_impl = DeterministicPlanner()
            result = create_pdf(args.system, args.content, args.prompt, args.output, planner_impl)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "code": "design_studio_error", "message": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
