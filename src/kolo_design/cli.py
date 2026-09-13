from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extractor import extract_brand
from .html_designer import compare_pdf_renderers, create_html_pdf
from .network import FetchError
from .pdf_designer import create_pdf
from .planner import DeterministicPlanner, OpenAICompatiblePlanner


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kolo-design")
    commands = root.add_subparsers(dest="command", required=True)
    create_system = commands.add_parser("create", help="Create a reusable design system from a website")
    create_commands = create_system.add_subparsers(dest="create_command", required=True)
    design_system = create_commands.add_parser("design-system", help="Create a design system from a public website")
    design_system.add_argument("--url", required=True)
    design_system.add_argument("--workspace", type=Path, required=True)
    design_system.add_argument("--name")
    design_system.add_argument(
        "--example-output",
        type=Path,
        help="Override the automatic <workspace>/examples/<brand-id>-kolo-create.pdf path",
    )

    pdf = commands.add_parser("pdf", help="Design a PDF with a saved design system")
    pdf_commands = pdf.add_subparsers(dest="pdf_command", required=True)
    create = pdf_commands.add_parser("create")
    create.add_argument("--system", type=Path, required=True)
    create.add_argument("--content", type=Path, required=True)
    create.add_argument("--prompt", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--planner", choices=("deterministic", "llm"), default="deterministic")
    create.add_argument("--model", help="Workspace-entitled model ID; required with --planner llm")
    create.add_argument("--renderer", choices=("reportlab", "html"), default="reportlab")

    compare = pdf_commands.add_parser("compare", help="Render the same plan with ReportLab and HTML/CSS")
    compare.add_argument("--system", type=Path, required=True)
    compare.add_argument("--content", type=Path, required=True)
    compare.add_argument("--prompt", required=True)
    compare.add_argument("--output-dir", type=Path, required=True)
    compare.add_argument("--planner", choices=("deterministic", "llm"), default="deterministic")
    compare.add_argument("--model", help="Workspace-entitled model ID; required with --planner llm")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "create":
            result = extract_brand(args.url, args.workspace, args.name)
            example_output = args.example_output or (
                args.workspace.resolve() / "examples" / f"{result['brand_id']}-kolo-create.pdf"
            )
            example_source = Path(__file__).resolve().parents[2] / "assets" / "kolo-create-explainer.md"
            example = create_pdf(
                Path(result["design_system"]),
                example_source,
                "Explain Kolo Create using the composition best suited to this brand and content",
                example_output,
                DeterministicPlanner(),
            )
            result["example_pdf"] = example
        else:
            if args.planner == "llm":
                if not args.model:
                    raise ValueError("--model is required with --planner llm")
                planner_impl = OpenAICompatiblePlanner.from_environment(args.model)
            else:
                planner_impl = DeterministicPlanner()
            if args.pdf_command == "compare":
                result = compare_pdf_renderers(args.system, args.content, args.prompt, args.output_dir, planner_impl)
            elif args.renderer == "html":
                result = create_html_pdf(args.system, args.content, args.prompt, args.output, planner_impl)
            else:
                result = create_pdf(args.system, args.content, args.prompt, args.output, planner_impl)
        print(json.dumps(result, sort_keys=True))
        return 0
    except FetchError as exc:
        print(json.dumps({
            "status": "error",
            "code": "website_access_blocked",
            "message": str(exc),
            "question": "Can you provide another public landing-page URL for this brand?",
            "recommended_next_step": "Try a public regional or campaign page on the same domain.",
        }, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "error", "code": "design_studio_error", "message": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
