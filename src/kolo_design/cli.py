from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .browser_evidence import load_browser_evidence
from .brand_director import DEFAULT_BRAND_MODEL, apply_brand_direction
from .extractor import extract_brand
from .html_designer import compare_pdf_renderers, create_html_pdf
from .network import FetchError
from .pdf_designer import create_pdf
from .planner import DeterministicPlanner, OpenAICompatiblePlanner
from .presentation_designer import create_presentation
from .presentation_planner import DeterministicPresentationPlanner, OpenAICompatiblePresentationPlanner
from .source_extract import extract_source_brand
from .source_fidelity import SourceFidelityError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kolo-create")
    commands = root.add_subparsers(dest="command", required=True)
    create_system = commands.add_parser("create", help="Create a reusable design system from a website")
    create_commands = create_system.add_subparsers(dest="create_command", required=True)
    design_system = create_commands.add_parser("design-system", help="Create a design system from a website or frontend source")
    source = design_system.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Public website URL")
    source.add_argument("--repo-url", help="Public HTTPS GitHub repository URL")
    source.add_argument("--source-dir", type=Path, help="Local frontend source directory")
    source.add_argument("--source-archive", type=Path, help="Local ZIP containing frontend source")
    source.add_argument(
        "--browser-evidence", type=Path,
        help="Kolo visible-browser evidence directory or bounded ZIP",
    )
    design_system.add_argument("--workspace", type=Path, required=True)
    design_system.add_argument("--name")
    design_system.add_argument(
        "--brand-director", choices=("auto", "llm", "deterministic"), default="auto",
        help="Use one bounded brand-judgment call when available, require it, or disable it",
    )
    design_system.add_argument(
        "--brand-model", default=DEFAULT_BRAND_MODEL,
        help=f"Workspace-entitled brand-director model (default: {DEFAULT_BRAND_MODEL})",
    )
    design_system.add_argument(
        "--example-output",
        type=Path,
        help="Override the automatic <workspace>/examples/<brand-id>-kolo-create.pdf path",
    )
    design_system.add_argument(
        "--example-presentation-output",
        type=Path,
        help="Override the automatic <workspace>/examples/<brand-id>-kolo-create.pptx path",
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

    powerpoint = commands.add_parser("powerpoint", help="Design an editable PowerPoint with a saved design system")
    powerpoint_commands = powerpoint.add_subparsers(dest="powerpoint_command", required=True)
    powerpoint_create = powerpoint_commands.add_parser("create")
    powerpoint_create.add_argument("--system", type=Path, required=True)
    powerpoint_create.add_argument("--content", type=Path, required=True)
    powerpoint_create.add_argument("--prompt", required=True)
    powerpoint_create.add_argument("--output", type=Path, required=True)
    powerpoint_create.add_argument("--planner", choices=("deterministic", "llm"), default="deterministic")
    powerpoint_create.add_argument("--model", help="Workspace-entitled model ID; required with --planner llm")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "create":
            if args.url:
                result = extract_brand(args.url, args.workspace, args.name)
            elif args.browser_evidence:
                rendered, metadata = load_browser_evidence(args.browser_evidence)
                result = extract_brand(
                    rendered["url"], args.workspace, args.name,
                    rendered_override=rendered, source_metadata=metadata,
                )
            else:
                result = extract_source_brand(
                    args.workspace, args.name, source_dir=args.source_dir,
                    source_archive=args.source_archive, repo_url=args.repo_url,
                )
            design_system_path = Path(result["design_system"])
            latest_path = Path(result.get("latest") or design_system_path.parent.parent / "latest.json")
            result["brand_direction"] = (
                apply_brand_direction(
                    design_system_path, latest_path,
                    mode=args.brand_director, model=args.brand_model,
                )
                if design_system_path.exists()
                else {"status": "skipped", "reason": "design-system artifact unavailable", "model": args.brand_model}
            )
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
                brand_demonstration=True,
            )
            result["example_pdf"] = example
            presentation_output = args.example_presentation_output or (
                args.workspace.resolve() / "examples" / f"{result['brand_id']}-kolo-create.pptx"
            )
            result["example_powerpoint"] = create_presentation(
                Path(result["design_system"]),
                example_source,
                "Explain Kolo Create as a concise, brand-led presentation",
                presentation_output,
                DeterministicPresentationPlanner(),
                brand_demonstration=True,
            )
            result["artifacts"] = [
                {
                    "kind": "pdf", "path": result["example_pdf"]["pdf"],
                    "delivery": ["attach-to-current-chat", "open-in-visible-browser"],
                },
                {
                    "kind": "powerpoint", "path": result["example_powerpoint"]["pptx"],
                    "delivery": ["attach-to-current-chat"],
                    "previews": result["example_powerpoint"].get("preview_html", []),
                },
            ]
            result["delivery"] = {
                "required": True,
                "status": "pending-agent-delivery",
                "completion_rule": "Do not report completion until both chat attachments have receipts.",
            }
        elif args.command == "powerpoint":
            if args.planner == "llm":
                if not args.model:
                    raise ValueError("--model is required with --planner llm")
                presentation_planner = OpenAICompatiblePresentationPlanner.from_environment(args.model)
            else:
                presentation_planner = DeterministicPresentationPlanner()
            result = create_presentation(args.system, args.content, args.prompt, args.output, presentation_planner)
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
    except SourceFidelityError as exc:
        print(json.dumps({
            "status": "error",
            "code": "browser_evidence_required",
            "message": str(exc),
            "source_fidelity": exc.report,
            "question": "This page did not render as trustworthy brand evidence. Open it in Kolo's visible browser and capture it?",
            "recommended_next_step": "Use the visible-browser evidence workflow, then rerun with --browser-evidence.",
        }, sort_keys=True))
        return 2
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
