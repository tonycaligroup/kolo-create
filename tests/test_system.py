from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from kolo_design.contracts import validate_design_system
from kolo_design.cli import parser
from kolo_design.network import FetchError, assert_public_url
from kolo_design.pdf_designer import create_pdf
from kolo_design.util import read_json

FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_system_is_valid() -> None:
    validate_design_system(read_json(FIXTURES / "design-system.json"))


def test_create_design_system_command_contract() -> None:
    args = parser().parse_args([
        "create", "design-system", "--url", "https://example.com", "--workspace", "./data"
    ])
    assert args.command == "create"
    assert args.create_command == "design-system"


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://localhost", "http://169.254.169.254/latest/meta-data"])
def test_private_fetch_targets_are_rejected(url: str) -> None:
    with pytest.raises(FetchError):
        assert_public_url(url)


def test_pdf_vertical_slice(tmp_path: Path) -> None:
    output = tmp_path / "designed.pdf"
    result = create_pdf(
        FIXTURES / "design-system.json",
        FIXTURES / "content.md",
        "Create a bold executive brief",
        output,
    )
    assert result["status"] == "succeeded"
    assert result["pages"] >= 2
    assert len(PdfReader(str(output)).pages) == result["pages"]
    assert Path(result["quality_report"]).exists()


def test_pdf_resolves_inline_markdown_and_removes_unsupported_emoji(tmp_path: Path) -> None:
    source = tmp_path / "announcement.md"
    source.write_text(
        "# ANNOUNCEMENT!!! 🚀\n\nThe **Kolo Seller Hub** is live. 🤝\n\n- **Deal Builder:** live pricing 💰\n- **Skill Building ⭐️:** quote a reusable skill\n",
        encoding="utf-8",
    )
    output = tmp_path / "announcement.pdf"
    result = create_pdf(
        FIXTURES / "design-system.json",
        source,
        "Create a bold launch announcement called Kolo Seller Hub",
        output,
    )
    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(str(output)).pages)
    report = read_json(Path(result["quality_report"]))
    assert "**" not in extracted
    assert not ({"■", "□", "�"} & set(extracted))
    assert "Kolo Seller Hub" in extracted
    assert report["checks"]["inline_markdown_resolved"] is True
    assert report["checks"]["tofu_glyphs_absent"] is True
    assert report["normalization"]["unsupported_emoji_removed"] >= 3
