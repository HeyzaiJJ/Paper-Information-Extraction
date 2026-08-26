"""Repair Marker figure crops by rendering FigureGroup geometry from the PDF.

Marker can detect a complete FigureGroup while its nested Chart/Picture box is
too narrow.  The group provides reliable horizontal bounds, while the nested
visual block provides vertical bounds that exclude the figure caption.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path


_FIGURE_LABEL_RE = re.compile(r"\bfig(?:ure)?\.?\s*(\d+[a-z]?)\b", re.IGNORECASE)
_CONTENT_REF_RE = re.compile(r"<content-ref\b[^>]*\bsrc=[\"']([^\"']+)", re.IGNORECASE)
_VISUAL_TYPES = {"figure", "picture", "chart", "image", "11", "20"}
_CAPTION_TYPES = {"caption", "9"}
_FIGURE_GROUP_TYPES = {"figuregroup", "figure_group", "4"}


def _normalized_ref(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().replace("\\", "/").split("#", 1)[0].split("?", 1)[0]
    return "/" + value.lstrip("/").casefold() if value else ""


def _node_ref(node: dict) -> str:
    for key in ("id", "ref", "path", "source_ref", "block_id"):
        value = node.get(key)
        ref = _normalized_ref(str(value) if isinstance(value, (int, float)) else value)
        if ref:
            return ref
    return ""


def _node_type(node: dict) -> str:
    return str(
        node.get("block_type")
        or node.get("type")
        or node.get("block_type_name")
        or node.get("name")
        or ""
    ).strip().casefold().replace(" ", "")


def _node_text(node: dict) -> str:
    values: list[str] = []
    for key in ("caption", "text", "html", "markdown", "content", "raw", "value"):
        value = node.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(part for part in value if isinstance(part, str))
    return "\n".join(values)


def _bbox(value) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        for key in ("bbox", "polygon", "coordinates"):
            found = _bbox(value.get(key))
            if found:
                return found
        values = [value.get(key) for key in ("x0", "y0", "x1", "y1")]
        if all(isinstance(item, (int, float)) for item in values):
            return tuple(float(item) for item in values)
        return None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x0, y0, x1, y1 = (float(item) for item in value)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    points = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            if isinstance(item[0], (int, float)) and isinstance(item[1], (int, float)):
                points.append((float(item[0]), float(item[1])))
            else:
                found = _bbox(item)
                if found:
                    points.extend(((found[0], found[1]), (found[2], found[3])))
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _contains(outer, inner, tolerance: float = 6.0) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and outer[2] + tolerance >= inner[2]
        and outer[3] + tolerance >= inner[3]
    )


def _page_number(node: dict, fallback) -> int | None:
    for key in ("page_id", "page", "page_number"):
        value = node.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    match = re.search(r"(?:page|p)[^0-9]*(\d+)", _node_ref(node), re.IGNORECASE)
    return int(match.group(1)) if match else fallback


def _walk_nodes(value, page=None):
    if isinstance(value, list):
        for item in value:
            yield from _walk_nodes(item, page)
        return
    if not isinstance(value, dict):
        return
    block_type = _node_type(value)
    current_page = _page_number(value, page) if block_type in {"page", "8"} else page
    yield value, current_page
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _walk_nodes(child, current_page)


def _page_bounds(payload) -> dict[int, tuple[float, float, float, float]]:
    bounds: dict[int, tuple[float, float, float, float]] = {}
    for node, page in _walk_nodes(payload):
        if _node_type(node) not in {"page", "8"} or page is None:
            continue
        if box := _bbox(node.get("bbox") or node.get("polygon") or node.get("coordinates")):
            bounds[page] = box
    return bounds


def _figure_regions(internal_json: str) -> list[dict]:
    """Return a figure number and its complete crop rectangle in PDF points."""
    try:
        payload = json.loads(internal_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return []

    nodes = []
    seen_nodes: set[tuple[int | None, str, str]] = set()
    refs: dict[str, dict] = {}
    for node, page in _walk_nodes(payload):
        box = _bbox(node.get("bbox") or node.get("polygon") or node.get("coordinates"))
        record = {
            "node": node,
            "page": page,
            "type": _node_type(node),
            "box": box,
            "text": _node_text(node),
            "ref": _node_ref(node),
        }
        node_key = (page, record["type"], str(node.get("block_id") or record["ref"]))
        if not box:
            continue
        if node_key in seen_nodes and node_key[2]:
            continue
        seen_nodes.add(node_key)
        nodes.append(record)
        if record["ref"]:
            refs[record["ref"]] = record

    visuals = [item for item in nodes if item["type"] in _VISUAL_TYPES and item["box"]]
    captions = [item for item in nodes if item["type"] in _CAPTION_TYPES and item["box"]]
    groups = [item for item in nodes if item["type"] in _FIGURE_GROUP_TYPES and item["box"]]
    page_bounds = _page_bounds(payload)
    regions: dict[str, dict] = {}

    for caption in captions:
        match = _FIGURE_LABEL_RE.search(caption["text"])
        if not match or caption["page"] is None:
            continue
        figure_id = f"fig{match.group(1).lower()}"
        same_page_visuals = [item for item in visuals if item["page"] == caption["page"]]
        same_page_groups = [item for item in groups if item["page"] == caption["page"]]

        group = next((item for item in same_page_groups if _contains(item["box"], caption["box"])), None)
        if group:
            group_refs = {
                _normalized_ref(value)
                for value in _CONTENT_REF_RE.findall(group["text"])
            }
            # Current Marker JSON stores FigureGroup membership as
            # structure=[{page_id, block_id, block_type}, ...], rather than
            # HTML content-ref nodes.
            for member in group["node"].get("structure") or []:
                if isinstance(member, dict):
                    ref = _normalized_ref(
                        f"/page/{member.get('page_id')}/{member.get('block_id')}"
                    )
                    if ref:
                        group_refs.add(ref)
            group_visuals = [item for item in same_page_visuals if item["ref"] in group_refs]
            if not group_visuals:
                group_visuals = [item for item in same_page_visuals if _contains(group["box"], item["box"])]
        else:
            group_visuals = []

        candidates = group_visuals or same_page_visuals
        if not candidates:
            continue
        visual = min(
            candidates,
            key=lambda item: abs((item["box"][1] + item["box"][3]) / 2 - (caption["box"][1] + caption["box"][3]) / 2),
        )
        visual_box = visual["box"]
        horizontal_box = group["box"] if group else visual_box
        page_box = page_bounds.get(caption["page"])
        x0, x1 = horizontal_box[0], horizontal_box[2]
        if page_box:
            x0, x1 = max(page_box[0], x0), min(page_box[2], x1)
        regions[figure_id] = {
            "id": figure_id,
            "page": caption["page"],
            # Leave a small amount of vertical breathing room for axis labels
            # and tick marks, but stop above the caption itself.
            "bbox": (x0, visual_box[1], x1, visual_box[3]),
        }
    if regions:
        return list(regions.values())

    # Layout debug JSON does not carry extracted caption text. In that form,
    # FigureGroup structure still gives an unambiguous ordered list of visual
    # regions. The caller can map this ordered list to FigureIndex IDs.
    fallback = []
    seen_groups: set[tuple[int | None, str]] = set()
    for group in groups:
        group_key = (group["page"], str(group["node"].get("block_id") or group["ref"]))
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        members = group["node"].get("structure") or []
        member_ids = {
            str(item.get("block_id")) for item in members if isinstance(item, dict)
        }
        group_visuals = [
            item for item in visuals
            if item["page"] == group["page"] and str(item["node"].get("block_id")) in member_ids
        ]
        if not group_visuals:
            group_visuals = [item for item in visuals if item["page"] == group["page"] and _contains(group["box"], item["box"])]
        if not group_visuals or group["page"] is None:
            continue
        visual = min(group_visuals, key=lambda item: item["box"][1])
        fallback.append({
            "id": "",
            "page": group["page"],
            "bbox": (group["box"][0], visual["box"][1], group["box"][2], visual["box"][3]),
        })
    return fallback


def _pdftoppm() -> str | None:
    bundled = Path(
        r"C:\Users\chenzhijia\.cache\codex-runtimes\codex-primary-runtime"
        r"\dependencies\native\poppler\Library\bin\pdftoppm.exe"
    )
    if bundled.exists():
        return str(bundled)
    path = shutil.which("pdftoppm")
    return path or None


def render_complete_figure_images(
    pdf_path: str | Path,
    internal_json: str,
    figure_ids: list[str] | None = None,
    dpi: int = 220,
) -> dict[str, str]:
    """Render complete figure images keyed by ``figN`` from a PDF and Marker JSON."""
    try:
        from PIL import Image
    except ImportError:
        return {}
    renderer = _pdftoppm()
    if not renderer:
        return {}
    regions = _figure_regions(internal_json)
    if not regions:
        return {}
    page_images: dict[int, Image.Image] = {}
    results: dict[str, str] = {}
    figure_ids = [str(item).strip().lower() for item in (figure_ids or []) if str(item).strip()]
    with tempfile.TemporaryDirectory(prefix="marker-figure-repair-") as temp_dir:
        temp_dir_path = Path(temp_dir)
        for region in regions:
            page = int(region["page"]) + 1
            image_path = temp_dir_path / f"page-{page}.png"
            if page not in page_images:
                try:
                    subprocess.run(
                        [renderer, "-f", str(page), "-l", str(page), "-png", "-r", str(dpi), "-singlefile", str(pdf_path), str(image_path.with_suffix(""))],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    page_images[page] = Image.open(image_path).convert("RGB")
                except (OSError, subprocess.CalledProcessError):
                    continue
            image = page_images[page]
            # JSON page bounds are normally A4 points. Rendered dimensions let
            # the crop remain correct for arbitrary DPI.
            scale = image.width / 596.0
            x0, y0, x1, y1 = region["bbox"]
            crop = image.crop((
                max(0, round((x0 - 6) * scale)),
                max(0, round((y0 - 4) * scale)),
                min(image.width, round((x1 + 6) * scale)),
                min(image.height, round((y1 + 4) * scale)),
            ))
            if crop.width < 20 or crop.height < 20:
                continue
            stream = io.BytesIO()
            crop.save(stream, format="PNG", optimize=True)
            key = region["id"] or (figure_ids[len(results)] if len(results) < len(figure_ids) else "")
            if key:
                results[key] = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
    return results
