"""Marker 2.0 one-pass conversion engine.

The engine writes large artifacts to staging and returns compact manifests. It
never places Markdown, JSON, image bytes, or base64 strings on a multiprocessing
queue.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import time
from typing import Any, Callable

from runtime_config import RUNTIME_CONFIG, apply_runtime_environment, marker_config_values

# Windows spawn imports this module afresh. Reapply before importing Marker/Surya.
apply_runtime_environment(RUNTIME_CONFIG)

from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.output import text_from_rendered
from marker.renderers.json import JSONRenderer
from marker.renderers.markdown import MarkdownRenderer

from temp_assets import (
    create_manifest,
    rewrite_markdown_image_paths,
    save_pil_images,
    sha256_file,
    write_json,
)


ProgressCallback = Callable[[str, float], None]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    return str(value)


def effective_marker_config(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    config = marker_config_values(RUNTIME_CONFIG)
    config.update({k: v for k, v in (extra or {}).items() if v is not None})
    # These decisions are fixed by runtime.yaml for this Web application.
    config["mode"] = "balanced"
    config["pdftext_workers"] = 1
    config["extract_images"] = True
    config["use_llm"] = False
    config["output_format"] = "markdown"
    return config


def marker_config_hash(config: dict[str, Any]) -> str:
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MarkerEngine:
    def __init__(self, extra_config: dict[str, Any] | None = None):
        self.config = effective_marker_config(extra_config)
        self.config_hash = marker_config_hash(self.config)
        self.marker_version = importlib.metadata.version("marker-pdf")
        self.artifact_dict = create_model_dict(inference_backend="vllm")
        parser = ConfigParser(self.config)
        self.converter = PdfConverter(
            config=parser.generate_config_dict(),
            artifact_dict=self.artifact_dict,
            processor_list=parser.get_processors(),
            renderer=parser.get_renderer(),
            llm_service=parser.get_llm_service(),
        )

    def build_document(self, pdf_path: str, progress: ProgressCallback | None = None):
        if progress:
            progress("正在连接远程 Surya/vLLM 并构建文档", 15.0)
        document = self.converter.build_document(pdf_path)
        self.converter.page_count = len(document.pages)
        if progress:
            progress("文档结构识别完成", 72.0)
        return document

    def render_markdown(self, document):
        rendered = self.converter.resolve_dependencies(MarkdownRenderer)(document)
        markdown, _ext, images = text_from_rendered(rendered)
        metadata = _jsonable(getattr(rendered, "metadata", {}) or {})
        return markdown, images, metadata

    def render_json(self, document) -> tuple[str, dict[str, Any]]:
        rendered = self.converter.resolve_dependencies(JSONRenderer)(document)
        payload, _ext, _images = text_from_rendered(rendered)
        metadata = _jsonable(getattr(rendered, "metadata", {}) or {})
        return payload, metadata

    def render_analysis_once(
        self,
        pdf_path: str,
        doc_dir: Path,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        document = self.build_document(pdf_path, progress)
        if progress:
            progress("正在从同一 Document 渲染 Markdown", 75.0)
        markdown, images, markdown_meta = self.render_markdown(document)
        assets, replacements = save_pil_images(doc_dir, images)
        markdown = rewrite_markdown_image_paths(markdown, replacements)
        if progress:
            progress("正在从同一 Document 渲染 JSON", 81.0)
        internal_json, json_meta = self.render_json(document)
        return {
            "markdown": markdown,
            "internal_json": internal_json,
            "assets": assets,
            "page_count": len(document.pages),
            "elapsed": time.time() - started,
            "metadata": {"markdown": markdown_meta, "json": json_meta},
        }


def convert_pdf_to_staging(
    *,
    pdf_path: str,
    source_name: str,
    task_id: str,
    document_id: str,
    doc_dir: str,
    extra_config: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Build one Marker Document and write Markdown/images/metadata to staging."""
    path = Path(doc_dir)
    started = time.time()
    engine = MarkerEngine(extra_config)
    document = engine.build_document(pdf_path, progress)
    if progress:
        progress("正在渲染 Markdown 与独立图片", 78.0)
    markdown, images, marker_metadata = engine.render_markdown(document)
    assets, replacements = save_pil_images(path, images)
    markdown = rewrite_markdown_image_paths(markdown, replacements)
    (path / "document.md").write_text(markdown, encoding="utf-8")
    marker_meta = {
        "marker_version": engine.marker_version,
        "marker_config": engine.config,
        "marker_config_hash": engine.config_hash,
        "marker_metadata": marker_metadata,
        "page_count": len(document.pages),
    }
    write_json(path / "marker_meta.json", marker_meta)
    manifest = create_manifest(
        path,
        task_id=task_id,
        document_id=document_id,
        source_name=source_name,
        pdf_sha256=sha256_file(Path(pdf_path)),
        mode="conversion",
        marker_version=engine.marker_version,
        marker_config_hash=engine.config_hash,
        assets=assets,
        page_count=len(document.pages),
        elapsed=time.time() - started,
        extra={"markdown_path": "document.md", "marker_metadata_path": "marker_meta.json"},
    )
    if progress:
        progress("转换完成，正在生成下载包", 100.0)
    return manifest
