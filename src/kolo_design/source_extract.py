from __future__ import annotations

import json
import html
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from PIL import Image

from .brand_components import build_brand_components
from .browser_extract import browser_snapshot
from .extractor import _choose_colors, extract_brand
from .util import sha256_bytes, write_json

MAX_ARCHIVE_FILES = 20_000
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_SOURCE_ASSET_BYTES = 12 * 1024 * 1024
STATIC_ENTRIES = (
    "storybook-static/index.html", "dist/index.html", "build/index.html", "out/index.html", "public/index.html", "index.html",
)
IGNORED_PARTS = {".git", "node_modules", ".next", ".cache", "coverage", "vendor"}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def _safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_ARCHIVE_FILES or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("Source archive exceeds the bounded extraction limits")
        destination_root = destination.resolve()
        for item in entries:
            target = (destination / item.filename).resolve()
            if destination_root not in target.parents and target != destination_root:
                raise ValueError("Source archive contains an unsafe path")
            mode = item.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError("Source archive contains a symbolic link")
        bundle.extractall(destination)


def _clone_public_repo(repo_url: str, destination: Path) -> None:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"} or parsed.username or parsed.password:
        raise ValueError("--repo-url must be a public HTTPS GitHub repository URL")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--", repo_url, str(destination)],
        check=True, capture_output=True, text=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _source_root(source_dir: Path | None, source_archive: Path | None, repo_url: str | None, scratch: Path) -> tuple[Path, dict[str, Any]]:
    if source_dir:
        root = source_dir.resolve()
        if not root.is_dir():
            raise ValueError("--source-dir must be an existing directory")
        return root, {"kind": "source-directory", "source_path": str(root)}
    if source_archive:
        archive = source_archive.resolve()
        if not archive.is_file() or archive.suffix.lower() != ".zip":
            raise ValueError("--source-archive must be an existing ZIP file")
        extracted = scratch / "archive"
        extracted.mkdir()
        _safe_extract(archive, extracted)
        children = [item for item in extracted.iterdir() if item.name != "__MACOSX"]
        root = children[0] if len(children) == 1 and children[0].is_dir() else extracted
        return root, {"kind": "source-archive", "source_path": str(archive), "source_sha256": sha256_bytes(archive.read_bytes())}
    if repo_url:
        root = scratch / "repository"
        _clone_public_repo(repo_url, root)
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        return root, {"kind": "github-repository", "repository_url": repo_url, "revision": revision}
    raise ValueError("One source input is required")


def _static_entry(root: Path) -> Path | None:
    for relative in STATIC_ENTRIES:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    matches = [path for path in root.glob("*/index.html") if not (set(path.parts) & IGNORED_PARTS)]
    if matches:
        return sorted(matches, key=lambda item: len(item.parts))[0]
    return None


def _source_specimen(root: Path, scratch: Path, requested_name: str | None) -> Path:
    css_chunks: list[str] = []
    labels: list[str] = []
    total = 0
    for path in root.rglob("*"):
        if not path.is_file() or set(path.parts) & IGNORED_PARTS:
            continue
        if path.suffix.lower() in {".css", ".scss", ".sass", ".less"} and total < 4 * 1024 * 1024:
            payload = path.read_text(encoding="utf-8", errors="replace")[:512_000]
            css_chunks.append(payload)
            total += len(payload.encode("utf-8"))
        elif path.suffix.lower() in {".html", ".jsx", ".tsx", ".vue", ".svelte"} and len(labels) < 12:
            payload = path.read_text(encoding="utf-8", errors="replace")[:200_000]
            labels.extend(
                value.strip() for value in re.findall(r">\s*([A-Za-z][^<>{}\n]{2,70})\s*<", payload)
                if not re.search(r"[=;{}]", value)
            )
    package_name = None
    package_path = root / "package.json"
    if package_path.is_file():
        try:
            package_name = json.loads(package_path.read_text(encoding="utf-8")).get("name")
        except (json.JSONDecodeError, OSError):
            pass
    brand_name = requested_name or str(package_name or root.name).replace("-", " ").title()
    colors, _ = _choose_colors("\n".join(css_chunks))
    sample_labels = list(dict.fromkeys(labels))[:4] or ["Primary action", "A reusable surface", "Supporting information"]
    preview = scratch / "source-specimen"
    preview.mkdir()
    entry = preview / "index.html"
    cards = "".join(f"<article><b>{index:02d}</b><p>{html.escape(label)}</p></article>" for index, label in enumerate(sample_labels, 1))
    entry.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(brand_name)} source specimen</title>
    <style>*{{box-sizing:border-box}}body{{margin:0;background:{colors['background']};color:{colors['text']};font-family:Arial,sans-serif}}main{{max-width:1120px;margin:auto;padding:88px 56px}}.eyebrow{{color:{colors['accent']};font-size:12px;text-transform:uppercase;letter-spacing:.14em}}h1{{font-size:72px;line-height:.96;max-width:850px;margin:22px 0 64px}}section{{display:grid;grid-template-columns:repeat(2,1fr);gap:20px}}article{{background:{colors['surface']};padding:28px;min-height:150px;border-radius:12px;border-top:5px solid {colors['accent']};}}article b{{font-size:24px}}article p{{font-size:18px;line-height:1.45}}button{{margin-top:40px;padding:15px 24px;border:0;border-radius:999px;background:{colors['accent']};color:{colors['background']};font-weight:700}}</style></head>
    <body><main><div class="eyebrow">Source-derived component specimen</div><h1>{html.escape(brand_name)}</h1><section>{cards}</section><button>Primary action</button></main></body></html>""", encoding="utf-8")
    return entry


@contextmanager
def _serve(root: Path) -> Iterator[tuple[ThreadingHTTPServer, str]]:
    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _asset_candidates(root: Path) -> list[Path]:
    ranked: list[tuple[int, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file() or set(path.parts) & IGNORED_PARTS or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        if path.stat().st_size > MAX_SOURCE_ASSET_BYTES:
            continue
        hint = path.stem.lower()
        score = (100 if "logo" in hint or "wordmark" in hint else 0) + (70 if any(term in hint for term in ("hero", "banner", "masthead")) else 0)
        try:
            with Image.open(path) as image:
                width, height = image.size
            score += min(50, width * height // 100_000)
            if score:
                ranked.append((score, path))
        except (OSError, ValueError):
            continue
    return [path for _, path in sorted(ranked, key=lambda item: (-item[0], str(item[1])))[:12]]


def _copy_source_assets(root: Path, system_path: Path) -> int:
    system = json.loads(system_path.read_text(encoding="utf-8"))
    asset_dir = system_path.parent / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    existing_hashes = {asset.get("sha256") for asset in system.get("assets", [])}
    added = 0
    for candidate in _asset_candidates(root):
        payload = candidate.read_bytes()
        digest = sha256_bytes(payload)
        if digest in existing_hashes:
            continue
        hint = candidate.stem.lower()
        kind = "logo" if "logo" in hint or "wordmark" in hint else "hero-image"
        target = asset_dir / f"source-{kind}-{added + 1}{candidate.suffix.lower()}"
        target.write_bytes(payload)
        with Image.open(candidate) as image:
            width, height = image.size
        words = sorted({word for word in re.findall(r"[a-z0-9]+", hint) if len(word) >= 3})
        system.setdefault("assets", []).append({
            "id": f"source-{kind}-{added + 1}", "kind": kind, "path": str(target),
            "source": "frontend-source-file", "source_path": str(candidate.relative_to(root)),
            "provenance": "original frontend repository asset", "sha256": digest,
            "media_type": f"image/{'jpeg' if candidate.suffix.lower() in {'.jpg', '.jpeg'} else candidate.suffix.lower()[1:]}",
            "pixel_width": width, "pixel_height": height, "aspect_ratio": round(width / max(1, height), 3),
            "orientation": "landscape" if width >= height * 1.2 else "portrait" if height >= width * 1.2 else "square",
            "keywords": words,
        })
        existing_hashes.add(digest)
        added += 1
    system["brand_components"] = build_brand_components(system)
    write_json(system_path, system)
    write_json(system_path.parent / "brand-components.json", system["brand_components"])
    write_json(system_path.parent.parent / "latest.json", system)
    return added


def extract_source_brand(
    workspace: Path,
    name: str | None = None,
    *,
    source_dir: Path | None = None,
    source_archive: Path | None = None,
    repo_url: str | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="kolo-create-source-") as temporary:
        scratch = Path(temporary)
        root, metadata = _source_root(source_dir, source_archive, repo_url, scratch)
        entry = _static_entry(root)
        actual_route = entry is not None
        if entry is None:
            entry = _source_specimen(root, scratch, name)
        serve_root = entry.parent
        with _serve(serve_root) as (server, base_url):
            del server
            rendered = browser_snapshot(f"{base_url}/{entry.name}", allow_local=True)
            if not rendered:
                raise RuntimeError("Chromium could not render the supplied frontend source")
        metadata.update({
            "render_mode": "existing-static-frontend" if actual_route else "source-derived-component-specimen",
            "render_entry": str(entry.relative_to(root)) if actual_route else None,
            "route_fidelity": "rendered-static-route" if actual_route else "unverified-source-specimen",
            "backend_executed": False, "external_requests_blocked": True,
        })
        result = extract_brand(rendered["url"], workspace, name, rendered_override=rendered, source_metadata=metadata)
        result["source_assets_added"] = _copy_source_assets(root, Path(result["design_system"]))
        return result
