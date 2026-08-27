"""Repair Marker figure crops by rendering FigureGroup geometry from the PDF.

Marker can detect a complete FigureGroup while its nested Chart/Picture box is
too narrow.  The group provides reliable horizontal bounds, while the nested
visual block provides vertical bounds that exclude the figure caption.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path


logger = logging.getLogger("paper.figures")


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


def _ref_candidates(value, page=None) -> list[str]:
    """Return Marker node-reference aliases from most to least specific."""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return []
    raw = value.strip().replace("\\", "/").split("#", 1)[0].split("?", 1)[0].strip()
    normalized = _normalized_ref(raw)
    if not normalized:
        return []
    parts = normalized.strip("/").split("/")
    aliases = [normalized]
    if len(parts) >= 2:
        aliases.append("/" + "/".join(parts[-2:]))
    if page is not None:
        aliases.append(_normalized_ref(f"/page/{page}/{raw.lstrip('/')}"))
        aliases.append(_normalized_ref(f"/page/{page}/{parts[-1]}"))
    aliases.append("/" + parts[-1])
    return list(dict.fromkeys(item for item in aliases if item))


def _resolve_ref_records(ref_records: dict[str, list[dict]], value, page=None) -> list[dict]:
    for alias in _ref_candidates(value, page):
        records = ref_records.get(alias) or []
        if len(records) == 1:
            return records
    return []


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
    ref_records: dict[str, list[dict]] = defaultdict(list)
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
        node_values = [
            node.get(key)
            for key in ("id", "ref", "path", "source_ref", "block_id")
            if node.get(key) is not None
        ]
        for value in node_values:
            for alias in _ref_candidates(value, page):
                if record not in ref_records[alias]:
                    ref_records[alias].append(record)

    visuals = [item for item in nodes if item["type"] in _VISUAL_TYPES and item["box"]]
    captions = [item for item in nodes if item["type"] in _CAPTION_TYPES and item["box"]]
    groups = [item for item in nodes if item["type"] in _FIGURE_GROUP_TYPES and item["box"]]
    page_bounds = _page_bounds(payload)
    regions: dict[str, dict] = {}

    # Build the group membership once. A FigureGroup can be geometrically
    # broad, and some Marker versions put captions from adjacent figures in
    # the same group. Such a group is useful only when it is unambiguously a
    # single caption-to-visual pair.
    def group_refs(group: dict) -> list:
        refs = list(_CONTENT_REF_RE.findall(group["text"]))
        # Current Marker JSON stores FigureGroup membership as
        # structure=[{page_id, block_id, block_type}, ...], rather than
        # HTML content-ref nodes.
        for member in group["node"].get("structure") or []:
            if not isinstance(member, dict):
                continue
            block_id = member.get("block_id")
            if block_id is None:
                continue
            refs.append(block_id)
            block_type = str(
                member.get("block_type")
                or member.get("type")
                or member.get("block_type_name")
                or ""
            ).strip()
            if block_type:
                refs.append(f"{block_type}/{block_id}")
            member_page = member.get("page_id", group.get("page"))
            if member_page is not None:
                block_path = str(block_id).lstrip("/")
                typed_id = (
                    block_path
                    if "/" in block_path or not block_type
                    else f"{block_type}/{block_path}"
                )
                refs.append(f"/page/{member_page}/{typed_id.lstrip('/')}")
        return refs

    def unique_records(records: list[dict]) -> list[dict]:
        found: list[dict] = []
        for record in records:
            if record not in found:
                found.append(record)
        return found

    group_infos = []
    for group in groups:
        same_page_captions = [item for item in captions if item["page"] == group["page"]]
        same_page_visuals = [item for item in visuals if item["page"] == group["page"]]
        resolved = []
        for ref in group_refs(group):
            resolved.extend(_resolve_ref_records(ref_records, ref, group.get("page")))
        # Union explicit refs with containment. This prevents a broad group
        # from appearing one-to-one merely because one of its nested refs was
        # not resolvable in a particular Marker release.
        group_captions = unique_records(
            [item for item in resolved if item["type"] in _CAPTION_TYPES]
            + [item for item in same_page_captions if _contains(group["box"], item["box"])],
        )
        group_visuals = unique_records(
            [item for item in resolved if item["type"] in _VISUAL_TYPES]
            + [item for item in same_page_visuals if _contains(group["box"], item["box"])],
        )
        group_infos.append({
            "group": group,
            "captions": group_captions,
            "visuals": group_visuals,
        })

    caption_entries = []
    caption_entry_by_record = {}
    seen_figure_ids: set[str] = set()
    for caption in captions:
        match = _FIGURE_LABEL_RE.search(caption["text"])
        if not match or caption["page"] is None:
            continue
        figure_id = f"fig{match.group(1).lower()}"
        # Keep one geometry record per figure ID. Duplicate caption nodes are
        # common in reconstructed JSON and must not overwrite a good match.
        if figure_id in seen_figure_ids:
            continue
        entry = {"id": figure_id, "record": caption}
        caption_entries.append(entry)
        caption_entry_by_record[id(caption)] = entry
        seen_figure_ids.add(figure_id)

    visual_index_by_record = {id(item): index for index, item in enumerate(visuals)}
    claimed_caption_ids: set[str] = set()
    claimed_visual_indexes: set[int] = set()
    direct_matches = []
    for info in group_infos:
        # Do not infer a pair from document order. The group must contain one
        # caption and one visual, otherwise it is handled by global geometry.
        if len(info["captions"]) != 1 or len(info["visuals"]) != 1:
            continue
        entry = caption_entry_by_record.get(id(info["captions"][0]))
        visual_index = visual_index_by_record.get(id(info["visuals"][0]))
        if entry is None or visual_index is None:
            continue
        if entry["id"] in claimed_caption_ids or visual_index in claimed_visual_indexes:
            continue
        claimed_caption_ids.add(entry["id"])
        claimed_visual_indexes.add(visual_index)
        direct_matches.append((entry, visual_index, info["group"]["box"]))

    def add_region(entry: dict, visual_index: int, horizontal_box) -> None:
        visual_box = visuals[visual_index]["box"]
        page = entry["record"]["page"]
        page_box = page_bounds.get(page)
        x0, x1 = horizontal_box[0], horizontal_box[2]
        if page_box:
            x0, x1 = max(page_box[0], x0), min(page_box[2], x1)
        regions[entry["id"]] = {
            "id": entry["id"],
            "page": page,
            # Leave a small amount of vertical breathing room for axis labels
            # and tick marks, but stop above the figure caption itself.
            "bbox": (x0, visual_box[1], x1, visual_box[3]),
        }

    for entry, visual_index, horizontal_box in direct_matches:
        add_region(entry, visual_index, horizontal_box)

    # Match all remaining captions in one pass. Mutual unique-nearest
    # matching prevents two captions from reusing one visual and refuses ties
    # instead of silently choosing by traversal order.
    candidates: list[tuple[float, int, int]] = []
    for caption_index, entry in enumerate(caption_entries):
        if entry["id"] in claimed_caption_ids:
            continue
        caption_box = entry["record"]["box"]
        if caption_box is None:
            continue
        caption_center = (
            (caption_box[0] + caption_box[2]) / 2,
            (caption_box[1] + caption_box[3]) / 2,
        )
        for visual_index, visual in enumerate(visuals):
            if visual_index in claimed_visual_indexes or visual["page"] != entry["record"]["page"]:
                continue
            visual_box = visual["box"]
            visual_center = (
                (visual_box[0] + visual_box[2]) / 2,
                (visual_box[1] + visual_box[3]) / 2,
            )
            distance = (
                (caption_center[0] - visual_center[0]) ** 2
                + (caption_center[1] - visual_center[1]) ** 2
            )
            candidates.append((distance, caption_index, visual_index))

    by_caption: dict[int, list[tuple[float, int]]] = {}
    by_visual: dict[int, list[tuple[float, int]]] = {}
    for distance, caption_index, visual_index in candidates:
        by_caption.setdefault(caption_index, []).append((distance, visual_index))
        by_visual.setdefault(visual_index, []).append((distance, caption_index))

    nearest_caption: dict[int, int] = {}
    for caption_index, matches in by_caption.items():
        matches.sort()
        if len(matches) == 1 or matches[0][0] < matches[1][0]:
            nearest_caption[caption_index] = matches[0][1]

    nearest_visual: dict[int, int] = {}
    for visual_index, matches in by_visual.items():
        matches.sort()
        if len(matches) == 1 or matches[0][0] < matches[1][0]:
            nearest_visual[visual_index] = matches[0][1]

    for caption_index, visual_index in nearest_caption.items():
        if nearest_visual.get(visual_index) != caption_index:
            continue
        entry = caption_entries[caption_index]
        add_region(entry, visual_index, visuals[visual_index]["box"])
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
        group_refs = []
        for member in members:
            if not isinstance(member, dict) or member.get("block_id") is None:
                continue
            block_id = member["block_id"]
            group_refs.append(block_id)
            block_type = str(
                member.get("block_type")
                or member.get("type")
                or member.get("block_type_name")
                or ""
            ).strip()
            if block_type:
                group_refs.append(f"{block_type}/{block_id}")
        group_records = []
        for ref in group_refs:
            for record in _resolve_ref_records(ref_records, ref, group["page"]):
                if record not in group_records:
                    group_records.append(record)
        group_visuals = [item for item in visuals if item in group_records]
        if not group_visuals:
            group_visuals = [item for item in visuals if item["page"] == group["page"] and _contains(group["box"], item["box"])]
        # Layout-only JSON has no caption text to provide a label. It is safe to
        # return a crop only when the group contains exactly one visual.
        if len(group_visuals) != 1 or group["page"] is None:
            continue
        visual = group_visuals[0]
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
                    logger.warning(
                        "pdftoppm 图像渲染失败 page=%s renderer=%s",
                        page, Path(renderer).name, exc_info=True,
                    )
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
