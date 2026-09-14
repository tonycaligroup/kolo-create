from __future__ import annotations

import json
import tempfile
import zipfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

from PIL import Image

from .network import assert_public_url
from .source_fidelity import ensure_source_fidelity
from .util import sha256_bytes


MAX_EVIDENCE_BYTES = 100 * 1024 * 1024
MAX_EVIDENCE_FILES = 250
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 12 * 1024 * 1024
MANIFEST_NAMES = ("browser-evidence.json", "capture.json")


def _confined(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved = root.resolve()
    if candidate != resolved and resolved not in candidate.parents:
        raise ValueError("Browser evidence contains an unsafe path")
    return candidate


def _read_bounded(path: Path, maximum: int) -> bytes:
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"Missing or oversized browser evidence file: {path.name}")
    return path.read_bytes()


def _safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_EVIDENCE_FILES or sum(item.file_size for item in entries) > MAX_EVIDENCE_BYTES:
            raise ValueError("Browser evidence ZIP exceeds the 100 MB / 250 file limit")
        for item in entries:
            target = _confined(destination, item.filename)
            mode = item.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError("Browser evidence ZIP contains a symbolic link")
            if target.suffix.lower() in {".zip", ".tar", ".gz", ".tgz"}:
                raise ValueError("Nested archives are not accepted as browser evidence")
        bundle.extractall(destination)


def _validate_tree(root: Path) -> None:
    files = [item for item in root.rglob("*") if item.is_file()]
    if len(files) > MAX_EVIDENCE_FILES or sum(item.stat().st_size for item in files) > MAX_EVIDENCE_BYTES:
        raise ValueError("Browser evidence exceeds the 100 MB / 250 file limit")


@contextmanager
def _evidence_root(source: Path) -> Iterator[Path]:
    source = source.resolve()
    if source.is_dir():
        _validate_tree(source)
        yield source
        return
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise ValueError("--browser-evidence must be a directory or ZIP")
    with tempfile.TemporaryDirectory(prefix="kolo-browser-evidence-") as temporary:
        root = Path(temporary)
        _safe_extract(source, root)
        children = [item for item in root.iterdir() if item.name != "__MACOSX"]
        selected = children[0] if len(children) == 1 and children[0].is_dir() else root
        _validate_tree(selected)
        yield selected


def _file(root: Path, files: dict[str, Any], key: str, maximum: int, *, required: bool = True) -> bytes | None:
    relative = files.get(key)
    if not relative:
        if required:
            raise ValueError(f"Browser evidence manifest is missing files.{key}")
        return None
    return _read_bounded(_confined(root, str(relative)), maximum)


def load_browser_evidence(source: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a bounded Kolo visible-browser capture into browser_snapshot shape."""
    with _evidence_root(source) as root:
        manifest_path = next((root / name for name in MANIFEST_NAMES if (root / name).is_file()), None)
        if manifest_path is None:
            raise ValueError("Browser evidence requires browser-evidence.json or capture.json")
        manifest = json.loads(_read_bounded(manifest_path, 256 * 1024).decode("utf-8"))
        if manifest.get("schema") != "kolo.browser-evidence/v1":
            raise ValueError("Unsupported browser evidence schema")
        url = str(manifest.get("url") or "")
        assert_public_url(url)
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise ValueError("Browser evidence manifest requires a files object")
        html_payload = _file(root, files, "html", MAX_HTML_BYTES)
        css_payload = _file(root, files, "computed_css", MAX_JSON_BYTES)
        elements_payload = _file(root, files, "elements", MAX_JSON_BYTES)
        screenshot = _file(root, files, "screenshot", 35 * 1024 * 1024)
        reference_pdf = _file(root, files, "reference_pdf", 45 * 1024 * 1024, required=False)
        assert html_payload is not None and css_payload is not None and elements_payload is not None and screenshot is not None
        elements = json.loads(elements_payload.decode("utf-8"))
        if not isinstance(elements, list) or len(elements) > 5000:
            raise ValueError("Browser evidence elements must be a list of at most 5000 entries")
        with Image.open(BytesIO(screenshot)) as image:
            image.verify()
        visible_logo = None
        logo_payload = _file(root, files, "visible_logo", 8 * 1024 * 1024, required=False)
        if logo_payload:
            with Image.open(BytesIO(logo_payload)) as image:
                image.verify()
            visible_logo = {"png": logo_payload, **(manifest.get("visible_logo") or {})}
        captured_assets: list[dict[str, Any]] = []
        for index, item in enumerate(manifest.get("assets") or []):
            if not isinstance(item, dict) or item.get("kind") not in {"hero-image", "logo"}:
                continue
            payload = _read_bounded(_confined(root, str(item.get("path") or "")), 12 * 1024 * 1024)
            captured_assets.append({**item, "payload": payload, "capture_index": index})
        snapshot: dict[str, Any] = {
            "url": url,
            "title": str(manifest.get("title") or ""),
            "response_status": manifest.get("response_status", 200),
            "html": html_payload.decode("utf-8", errors="replace"),
            "computed_css": css_payload.decode("utf-8", errors="replace"),
            "elements": elements,
            "root_styles": manifest.get("root_styles") or {},
            "viewport": manifest.get("viewport") or {"width": 1440, "height": 1100},
            "overlay_actions": manifest.get("overlay_actions") or {},
            "navigation_fallback": "kolo_visible_browser_evidence",
            "visible_logo": visible_logo,
            "screenshot": screenshot,
            "reference_pdf": reference_pdf,
            "reference_pdf_metadata": manifest.get("reference_pdf_metadata") or {
                "status": "captured" if reference_pdf else "unavailable",
                "capture_mode": "kolo_visible_browser",
            },
            "captured_assets": captured_assets,
        }
        report = ensure_source_fidelity(snapshot)
        metadata = {
            "kind": "kolo-visible-browser-evidence",
            "browser_evidence_source": str(source.resolve()),
            "browser_evidence_manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
            "source_fidelity": report,
        }
        return snapshot, metadata
