"""Filesystem-backed temporary documents and export archives.

Only unarchived work lives here. Knowledge-base persistence is handled by
``knowledge_db.py`` and copies selected assets into SQLite in one transaction.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import mimetypes
from pathlib import Path
import re
import shutil
import time
import uuid
import zipfile
from typing import Any, Iterable

from PIL import Image

from backend.runtime_config import RUNTIME_CONFIG
from backend.paths import DATA_DIR

STAGING_DIR = DATA_DIR / "staging"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR = DATA_DIR / "uploads"
for _path in (STAGING_DIR, EXPORT_DIR, UPLOAD_DIR):
    _path.mkdir(parents=True, exist_ok=True)


_SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_DATA_URI_RE = re.compile(
    r"^data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$",
    re.DOTALL,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str, fallback: str = "document") -> str:
    value = _SAFE_NAME_RE.sub("_", str(value or "")).strip(" ._")
    return (value or fallback)[:160]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_document_dir(task_id: str, document_id: str | None = None) -> tuple[str, Path]:
    document_id = document_id or uuid.uuid4().hex
    path = STAGING_DIR / safe_name(task_id) / safe_name(document_id)
    path.mkdir(parents=True, exist_ok=False)
    (path / "images").mkdir()
    return document_id, path


def document_dir(document_id: str) -> Path:
    matches = list(STAGING_DIR.glob(f"*/{safe_name(document_id)}"))
    if not matches:
        raise FileNotFoundError(f"临时文档不存在或已过期：{document_id}")
    return matches[0]


def delete_document_dir(document_id: str) -> bool:
    """Delete every staging directory for one document id.

    The lookup is constrained to ``STAGING_DIR/<task>/<document>`` and is
    intentionally idempotent so cancellation/deletion retries are safe.
    """
    if not str(document_id or "").strip():
        return False
    normalized = safe_name(document_id)
    staging_root = STAGING_DIR.resolve()
    removed = False
    for candidate in list(STAGING_DIR.glob(f"*/{normalized}")):
        resolved = candidate.resolve()
        if staging_root not in resolved.parents:
            raise ValueError("非法临时文档路径")
        if not resolved.is_dir():
            continue
        parent = resolved.parent
        shutil.rmtree(resolved)
        removed = True
        try:
            parent.rmdir()
        except OSError:
            pass
    return removed


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _image_extension(mime_type: str, preferred: str = "") -> str:
    preferred = preferred.lower().lstrip(".")
    if preferred in {"jpg", "jpeg", "png", "webp", "gif", "tif", "tiff"}:
        return "jpg" if preferred == "jpeg" else preferred
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/tiff": "tiff",
    }.get(mime_type.lower(), "png")


def save_image_bytes(
    doc_dir: Path,
    content: bytes,
    *,
    marker_name: str = "",
    mime_type: str = "",
    source: str = "marker",
    asset_id: str | None = None,
) -> dict[str, Any]:
    if not content:
        raise ValueError("图片内容为空")
    asset_id = asset_id or uuid.uuid4().hex
    try:
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format or "", "")
            preferred_ext = Path(marker_name).suffix
    except Exception:
        width = height = 0
        detected = ""
        preferred_ext = Path(marker_name).suffix
    mime_type = mime_type or detected or mimetypes.guess_type(marker_name)[0] or "image/png"
    extension = _image_extension(mime_type, preferred_ext)
    filename = f"{asset_id}.{extension}"
    relative_path = f"images/{filename}"
    target = doc_dir / relative_path
    target.write_bytes(content)
    return {
        "asset_id": asset_id,
        "marker_name": marker_name or filename,
        "relative_path": relative_path,
        "mime_type": mime_type,
        "extension": extension,
        "width": width,
        "height": height,
        "byte_size": len(content),
        "sha256": sha256_bytes(content),
        "source": source,
    }


def save_pil_images(doc_dir: Path, images: dict[str, Any], source: str = "marker_markdown") -> tuple[list[dict], dict[str, str]]:
    assets: list[dict] = []
    replacements: dict[str, str] = {}
    for marker_name, image in (images or {}).items():
        suffix = Path(marker_name).suffix.lower()
        fmt = "JPEG" if suffix in {".jpg", ".jpeg"} else "PNG"
        mime = "image/jpeg" if fmt == "JPEG" else "image/png"
        if fmt == "JPEG" and image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        stream = io.BytesIO()
        image.save(stream, format=fmt)
        asset = save_image_bytes(
            doc_dir,
            stream.getvalue(),
            marker_name=marker_name,
            mime_type=mime,
            source=source,
        )
        assets.append(asset)
        replacements[str(marker_name)] = asset["relative_path"].replace("\\", "/")
    return assets, replacements


def decode_data_uri(value: str) -> tuple[str, bytes] | None:
    match = _DATA_URI_RE.match(str(value or "").strip())
    if not match:
        return None
    return match.group(1).lower(), base64.b64decode(re.sub(r"\s+", "", match.group(2)))


def persist_figure_index_images(
    doc_dir: Path,
    figure_index: list[dict[str, Any]],
    existing_assets: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Decode FigureIndex data URIs once, then replace them with asset IDs/URLs."""
    assets = list(existing_assets or [])
    by_sha = {item.get("sha256"): item for item in assets if item.get("sha256")}
    clean_index: list[dict[str, Any]] = []
    for source in figure_index or []:
        item = dict(source)
        image = item.pop("image", "")
        decoded = decode_data_uri(image) if image else None
        if decoded:
            mime, content = decoded
            digest = sha256_bytes(content)
            asset = by_sha.get(digest)
            if asset is None:
                marker_name = f"{item.get('id') or 'figure'}.{_image_extension(mime)}"
                asset = save_image_bytes(
                    doc_dir,
                    content,
                    marker_name=marker_name,
                    mime_type=mime,
                    source="figure_index",
                )
                assets.append(asset)
                by_sha[digest] = asset
            item["temp_asset_id"] = asset["asset_id"]
            item["image_url"] = f"/api/temp-assets/{{document_id}}/{asset['asset_id']}"
        clean_index.append(item)
    return clean_index, assets


def persist_json_image_data(
    doc_dir: Path,
    payload: Any,
    existing_assets: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Replace every JSON data URI with a compact temporary asset reference."""
    assets = list(existing_assets or [])
    by_sha = {item.get("sha256"): item for item in assets if item.get("sha256")}

    def visit(value: Any) -> Any:
        if isinstance(value, str):
            decoded = decode_data_uri(value)
            if not decoded:
                return value
            mime, content = decoded
            digest = sha256_bytes(content)
            asset = by_sha.get(digest)
            if asset is None:
                asset = save_image_bytes(
                    doc_dir,
                    content,
                    marker_name=f"json-image.{_image_extension(mime)}",
                    mime_type=mime,
                    source="marker_json",
                )
                assets.append(asset)
                by_sha[digest] = asset
            return {
                "temp_asset_id": asset["asset_id"],
                "image_url": f"/api/temp-assets/{{document_id}}/{asset['asset_id']}",
                "mime_type": asset["mime_type"],
            }
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {str(key): visit(item) for key, item in value.items()}
        return value

    return visit(payload), assets


def rewrite_markdown_image_paths(markdown: str, replacements: dict[str, str]) -> str:
    result = markdown or ""
    for old, new in replacements.items():
        result = result.replace(f"]({old})", f"]({new})")
        result = result.replace(f'src="{old}"', f'src="{new}"')
        result = result.replace(f"src='{old}'", f"src='{new}'")
    return result


def create_manifest(
    doc_dir: Path,
    *,
    task_id: str,
    document_id: str,
    source_name: str,
    pdf_sha256: str,
    mode: str,
    marker_version: str,
    marker_config_hash: str,
    assets: list[dict[str, Any]],
    page_count: int = 0,
    elapsed: float = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "document_id": document_id,
        "source_name": source_name,
        "pdf_sha256": pdf_sha256,
        "mode": mode,
        "marker_version": marker_version,
        "marker_config_hash": marker_config_hash,
        "page_count": page_count,
        "elapsed": elapsed,
        "created_at": utc_now_iso(),
        "assets": assets,
    }
    if extra:
        payload.update(extra)
    write_json(doc_dir / "manifest.json", payload)
    return payload


def resolve_asset(document_id: str, asset_id: str) -> tuple[Path, dict[str, Any]]:
    doc_dir = document_dir(document_id)
    manifest = read_json(doc_dir / "manifest.json")
    asset = next(
        (item for item in manifest.get("assets", []) if item.get("asset_id") == asset_id),
        None,
    )
    if asset is None:
        raise FileNotFoundError("临时图片不存在")
    target = (doc_dir / asset["relative_path"]).resolve()
    if doc_dir.resolve() not in target.parents:
        raise ValueError("非法临时图片路径")
    if not target.exists():
        raise FileNotFoundError("临时图片文件已过期")
    return target, asset


def build_export_zip(task_id: str, manifests: Iterable[dict[str, Any]]) -> Path:
    manifests = list(manifests)
    export_path = EXPORT_DIR / f"{safe_name(task_id)}.zip"
    with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, manifest in enumerate(manifests, start=1):
            doc_dir = document_dir(manifest["document_id"])
            # Windows Explorer still applies the legacy MAX_PATH limit when it
            # extracts a ZIP.  Keep the archive path short even when the source
            # PDF has a long (or non-ASCII) filename.  The original name remains
            # available in manifest.json inside the package.
            source_stem = safe_name(Path(manifest.get("source_name", "")).stem, "document")
            folder = f"paper-{index:03d}_{source_stem[:48].rstrip(' ._')}"
            folder = folder.rstrip(" ._") or f"paper-{index:03d}"

            # Keep every generated artifact that exists in staging.  The normal
            # conversion path has document.md + marker_meta.json; analysis
            # preparation additionally creates structure/figure index JSON.
            for name in (
                "document.md",
                "marker_meta.json",
                "structure.json",
                "figure_index.json",
                "manifest.json",
            ):
                path = doc_dir / name
                if path.exists():
                    archive.write(path, f"{folder}/{name}")
            for asset in manifest.get("assets", []):
                path = doc_dir / asset["relative_path"]
                if path.exists():
                    archive.write(path, f"{folder}/images/{Path(path).name}")
    return export_path


def cleanup_expired() -> dict[str, int]:
    now = time.time()
    staging_ttl = float(RUNTIME_CONFIG.conversion.get("staging_ttl_hours", 24)) * 3600
    export_ttl = float(RUNTIME_CONFIG.conversion.get("export_ttl_hours", 24)) * 3600
    removed = {"staging": 0, "exports": 0, "uploads": 0}
    for task_dir in STAGING_DIR.iterdir():
        if task_dir.is_dir() and now - task_dir.stat().st_mtime > staging_ttl:
            shutil.rmtree(task_dir, ignore_errors=True)
            removed["staging"] += 1
    for path in EXPORT_DIR.iterdir():
        if path.is_file() and now - path.stat().st_mtime > export_ttl:
            path.unlink(missing_ok=True)
            removed["exports"] += 1
    for path in UPLOAD_DIR.iterdir():
        if path.is_file() and now - path.stat().st_mtime > staging_ttl:
            path.unlink(missing_ok=True)
            removed["uploads"] += 1
    return removed
