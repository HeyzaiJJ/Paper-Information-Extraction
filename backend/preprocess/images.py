"""Image helpers for visual analysis inputs.

Figure-to-image association is intentionally not implemented here. It is built
from Marker JSON Figure/Caption geometry in marker_platform.py.
"""

import base64
import mimetypes
import re
from dataclasses import dataclass
import io
from pathlib import Path


IMG_MD_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
IMG_HTML_RE = re.compile(r'<img[^>]*\bsrc=["\']([^"\'>]+?)["\']', re.IGNORECASE)


@dataclass
class ImageItem:
    index: int
    kind: str
    data: bytes
    mime_type: str = "image/png"
    pos: int = 0
    end_pos: int = 0
    caption: str = ""
    priority: int = 1
    figure_id: str = ""


def _collect_image_hits(raw_md: str):
    """Return Markdown and HTML image references in document order."""
    hits = []
    for match in IMG_MD_RE.finditer(raw_md):
        hits.append((match.start(), match.end(), match.group(1) or "", match.group(2)))
    for match in IMG_HTML_RE.finditer(raw_md):
        url = match.group(1) or ""
        if url.startswith("data:image/"):
            hits.append((match.start(), match.end(), "", url))
    hits.sort(key=lambda hit: hit[0])
    return hits


def extract_images(raw_md: str, base_dir=None, max_images: int = 40) -> list[ImageItem]:
    """Extract embedded or locally resolvable images without assigning figure IDs."""
    found: list[ImageItem] = []
    for start, end, alt, url in _collect_image_hits(raw_md):
        if url.startswith("data:image/"):
            header, encoded = url.split(",", 1)
            mime_type = header[5:].split(";", 1)[0]
            data = base64.b64decode(encoded)
        else:
            if not base_dir:
                continue
            path = Path(base_dir) / url.lstrip("./")
            if not path.exists():
                continue
            mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
            data = path.read_bytes()

        found.append(ImageItem(
            index=len(found),
            kind="bytes",
            data=data,
            mime_type=mime_type,
            pos=start,
            end_pos=end,
            caption=(alt or "").strip(),
            priority=1,
        ))

    seen, unique = set(), []
    for item in found:
        fingerprint = item.data[:200] + item.data[-80:] if len(item.data) > 200 else item.data
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(item)
    unique.sort(key=lambda item: (item.priority, item.index))
    return unique[:max_images]


def figure_index_to_image_items(
    figure_index: list[dict],
    document_id: str = "",
) -> list[ImageItem]:
    """Resolve temporary/database asset IDs to bytes for one transient VLM call."""
    from backend.temp_assets import resolve_asset
    from backend.knowledge_db import knowledge_asset

    items: list[ImageItem] = []
    seen: set[str] = set()
    for figure in figure_index or []:
        fig_id = str(figure.get("id") or "").strip().lower()
        if not fig_id or fig_id in seen:
            continue
        data = b""
        mime_type = "image/png"
        temp_asset_id = str(figure.get("temp_asset_id") or "")
        image_id = str(figure.get("image_id") or "")
        try:
            asset_document_id = str(figure.get("document_id") or document_id or "")
            if temp_asset_id and asset_document_id:
                path, asset = resolve_asset(asset_document_id, temp_asset_id)
                data = path.read_bytes()
                mime_type = str(asset.get("mime_type") or mime_type)
            elif image_id:
                data, mime_type, _extension = knowledge_asset(image_id)
            else:
                # Legacy input is decoded only in memory and is never persisted.
                image = str(figure.get("image") or "").strip()
                if image.startswith("data:image/"):
                    header, encoded = image.split(",", 1)
                    mime_type = header[5:].split(";", 1)[0]
                    data = base64.b64decode(encoded)
        except (FileNotFoundError, KeyError, ValueError, OSError):
            data = b""
        if not data:
            continue
        seen.add(fig_id)
        position = int(figure.get("position") or 0)
        items.append(ImageItem(
            index=len(items),
            kind="bytes",
            data=data,
            mime_type=mime_type,
            pos=position,
            end_pos=position,
            caption=str(figure.get("caption") or ""),
            priority=0,
            figure_id=fig_id,
        ))
    return items
