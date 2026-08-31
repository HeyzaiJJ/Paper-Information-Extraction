"""
基于 marker_demo.py 的 FastAPI 平台版。

与 marker_demo.py 的区别：
  - 模型在启动时只加载一次（lifespan），常驻显存，所有请求复用。
  - 提供网页上传界面（GET /）和上传转换接口（POST /convert）。
  - 网页端：上传后可逐个删除文件、多选输出格式，转换后在页面内预览、逐卡片右上角下载。
  - 接口端（不带 return_json）：单文件直接下载，多文件打包 zip（兼容 curl / 直接调用）。

运行（PowerShell，项目根目录）：
    $env:HF_ENDPOINT = "https://hf-mirror.com"
    $env:TORCH_DEVICE = "cuda"
    .\\.venv\\Scripts\\python.exe -m backend.main --port 8000
然后浏览器打开 http://127.0.0.1:8000  （接口文档在 /docs）
"""

import asyncio
import base64
import copy
from html import unescape as html_unescape
from html.parser import HTMLParser
import io
import json
import logging
import multiprocessing
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
import re
from typing import List
from urllib.parse import quote

import psutil

# 与 marker_demo.py / marker_single 一致：仅影响第三方库日志/回退，不改变输出
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

from backend.runtime_config import (
    RUNTIME_CONFIG,
    apply_runtime_environment,
    check_local_ocr_error,
    check_remote_inference,
    marker_config_values,
    prewarm_local_ocr_error,
    validate_marker_version,
)
from backend.paths import BACKEND_ROOT, FRONTEND_ROOT, REPO_ROOT
from backend.logging_setup import (
    bind_log_context,
    configure_main_logging,
    configure_worker_logging,
    get_log_queue,
    reset_log_context,
    shutdown_logging,
)

# Surya 在 import 时缓存 settings；必须先由项目配置写环境变量。
apply_runtime_environment(RUNTIME_CONFIG)

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import Response, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from marker.converters.pdf import PdfConverter
from marker.converters.table import TableConverter
from marker.models import create_model_dict
from marker.config.parser import ConfigParser
from marker.output import text_from_rendered
from marker.renderers.json import JSONRenderer
from marker.renderers.markdown import MarkdownRenderer
from backend.preprocess.pdf_figures import render_complete_figure_images
from backend.marker_engine import MarkerEngine, convert_pdf_to_staging
from backend.temp_assets import (
    EXPORT_DIR,
    UPLOAD_DIR,
    build_export_zip,
    cleanup_expired,
    create_document_dir,
    create_manifest,
    delete_document_dir,
    document_dir,
    persist_figure_index_images,
    persist_json_image_data,
    read_json,
    resolve_asset,
    safe_name,
    sha256_file,
    write_json,
)
from backend.knowledge_db import (
    ArchiveConflict,
    archive_document,
    create_folder,
    delete_folder,
    get_report,
    init_database,
    knowledge_asset,
    manage_reports_in_folder,
    knowledge_snapshot,
    remove_report_from_folder,
    update_folder,
)

logger = logging.getLogger("paper")
access_logger = logging.getLogger("paper.access")

# 调大 Starlette multipart 单 part 上限（默认 1MB，论文 5MB+ 越界导致 request.form() 解析空）
# 注意：Request._get_form() 和 MultiPartParser.__init__ 各有独立的默认值，两处都要改。
from starlette.formparsers import MultiPartParser
MultiPartParser.max_part_size = 10 * 1024 * 1024  # 10MB
MultiPartParser.spool_max_size = 10 * 1024 * 1024  # 内存缓冲也一并调大

from starlette.requests import Request as _StarletteRequest
_form = _StarletteRequest._get_form  # 保留原始方法引用

async def _get_form_patched(self, *, max_files=1000, max_fields=1000, max_part_size=None):
    if max_part_size is None:
        max_part_size = 10 * 1024 * 1024
    return await _form(self, max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)

_StarletteRequest._get_form = _get_form_patched

# ============ 全局状态 ============
MODELS = {}                       # 转换模型字典：主进程不再常驻，由转换子进程首次调用 _convert 时加载
TASKS: dict = {}                  # 异步转换任务表：task_id -> 进度/结果（含 proc/q 供取消）
_MP_CTX = None                    # spawn 多进程上下文（惰性创建）
_CONVERSION_SLOTS_PROC = None     # 仅供旧同步转换调用；可取消任务不得在子进程中持有
_ASYNC_CONVERSION_SLOTS = None    # 主进程有界 worker 池：强杀 worker 后仍能可靠释放


def _mp():
    """惰性获取 spawn 多进程上下文（Windows 默认即 spawn，显式指定更稳）。"""
    global _MP_CTX
    if _MP_CTX is None:
        _MP_CTX = multiprocessing.get_context("spawn")
    return _MP_CTX


def _get_conversion_slots():
    """旧同步转换接口使用的跨进程槽位。

    可取消的转换/论文预处理任务使用主进程 asyncio.Semaphore，不能让可能被
    terminate/kill 的子进程持有信号量，否则强杀会永久泄漏槽位。
    """
    global _CONVERSION_SLOTS_PROC
    if _CONVERSION_SLOTS_PROC is None:
        limit = int(RUNTIME_CONFIG.conversion.get("max_concurrent_pdfs", 3))
        _CONVERSION_SLOTS_PROC = _mp().BoundedSemaphore(max(1, limit))
    return _CONVERSION_SLOTS_PROC


def _get_async_conversion_slots():
    global _ASYNC_CONVERSION_SLOTS
    if _ASYNC_CONVERSION_SLOTS is None:
        limit = int(RUNTIME_CONFIG.conversion.get("max_concurrent_pdfs", 3))
        _ASYNC_CONVERSION_SLOTS = asyncio.Semaphore(max(1, limit))
    return _ASYNC_CONVERSION_SLOTS


# ============ marker 配置（backend/config/marker_config.json）============
# 集中管理 marker 可调参数（batch_size、是否提图、语言、页数等）。
# JSON 里值为 null 的键表示“用 marker 库默认”，加载时自动跳过，不会覆盖库默认。
MARKER_CONFIG_PATH = BACKEND_ROOT / "config" / "marker_config.json"


def _load_marker_config(override_path: str | None = None) -> dict:
    """加载 marker 配置 JSON，返回“非 null 键”的字典。

    - 默认读 backend/config/marker_config.json（若存在）。
    - override_path 非空时优先读该路径（单次请求覆盖全局默认）。
    - 值为 null 的键被丢弃，避免把 marker 的库默认覆盖成 None。
    返回的字典可直接合并进 _convert 的 kwargs（接口显式参数再覆盖它）。
    """
    merged: dict = {}
    candidates = []
    if override_path:
        candidates.append(Path(override_path))
    candidates.append(MARKER_CONFIG_PATH)
    for p in candidates:
        p = Path(p)
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if v is not None:
                    merged[k] = v
        except Exception:
            # 配置读取失败不应阻断转换，降级为“不用该配置文件”
            pass
        if override_path:
            break  # 指定 override 时只读它，不再回退默认
    # runtime.yaml 是 Web 项目的最终控制面；固定 balanced、单 pdftext worker，
    # 防止旧 marker_config.json 或请求参数重新启用本地主模型/fast layout。
    merged.update(marker_config_values(RUNTIME_CONFIG))
    merged.update({
        "mode": "balanced",
        "pdftext_workers": 1,
        "extract_images": True,
        "use_llm": False,
        "disable_tqdm": True,
        "debug_pdf_images": False,
        "debug_layout_images": False,
        "debug_json": False,
    })
    return merged


# ============ 启动 / 关闭 ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_main_logging(RUNTIME_CONFIG)
    logger.info("服务启动检查开始")
    try:
        version = validate_marker_version(RUNTIME_CONFIG)
        remote = await asyncio.to_thread(check_remote_inference, RUNTIME_CONFIG)
        await asyncio.to_thread(prewarm_local_ocr_error, RUNTIME_CONFIG)
        local_ocr = await asyncio.to_thread(check_local_ocr_error, RUNTIME_CONFIG)
        await asyncio.to_thread(init_database)
        await asyncio.to_thread(cleanup_expired)
    except Exception:
        logger.exception("服务启动检查失败")
        shutdown_logging()
        raise
    logger.info(
        "服务启动检查完成 marker_version=%s remote_models=%d ocr_status=%s max_pdf_concurrency=%s",
        version,
        len(remote.get("models", [])),
        local_ocr.get("status", "ok"),
        RUNTIME_CONFIG.conversion.get("max_concurrent_pdfs", 3),
    )

    stop_cleanup = asyncio.Event()

    async def cleanup_loop():
        while not stop_cleanup.is_set():
            try:
                await asyncio.wait_for(stop_cleanup.wait(), timeout=3600)
            except asyncio.TimeoutError:
                await asyncio.to_thread(cleanup_expired)

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        stop_cleanup.set()
        cleanup_task.cancel()
        MODELS.clear()
        # 关闭服务时强杀所有存活的 worker 子进程，防止留下孤儿转换。
        all_tasks = list(TASKS.values()) + list(PAPER_PREP_TASKS.values()) + list(MATERIAL_TASKS.values())
        for task in all_tasks:
            processes = list((task.get("processes") or {}).values())
            if task.get("proc") is not None:
                processes.append(task["proc"])
            for process in processes:
                if process is not None and process.is_alive():
                    try:
                        process.terminate()
                    except Exception:
                        logger.exception("关闭服务时终止 worker 失败")
                if process is not None:
                    try:
                        await asyncio.to_thread(process.join, 2)
                    except Exception:
                        logger.exception("关闭服务时回收 worker 失败")
        logger.info("服务关闭")
        shutdown_logging()


app = FastAPI(title="论文.pdf转换与信息提取", lifespan=lifespan)


def _request_id(value: str | None) -> str:
    """Accept only log-safe client IDs; otherwise generate a fresh UUID."""
    value = str(value or "").strip()
    if value and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return uuid.uuid4().hex


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = _request_id(request.headers.get("x-request-id"))
    request.state.request_id = request_id
    token = bind_log_context(request_id=request_id)
    started = time.perf_counter()
    client = request.client.host if request.client else "-"
    content_length = request.headers.get("content-length", "-")
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "request failed method=%s path=%s duration_ms=%.1f client=%s content_length=%s",
            request.method, request.url.path,
            duration_ms,
            client, content_length,
        )
        # Starlette's ServerErrorMiddleware sits outside user middleware, so
        # re-raising would lose the correlation header. Return the same 500
        # status with a safe body and request ID instead of exposing a trace.
        response = JSONResponse(
            {"detail": "内部服务器错误", "request_id": request_id},
            status_code=500,
            headers={"X-Request-ID": request_id},
        )
        access_logger.error(
            "request complete method=%s path=%s status=500 duration_ms=%.1f client=%s content_length=%s",
            request.method, request.url.path, duration_ms, client, content_length,
        )
        return response
    else:
        response.headers["X-Request-ID"] = request_id
        access_logger.info(
            "request complete method=%s path=%s status=%s duration_ms=%.1f client=%s content_length=%s",
            request.method, request.url.path, response.status_code,
            (time.perf_counter() - started) * 1000,
            client, content_length,
        )
        return response
    finally:
        reset_log_context(token)

# 前端资源：模板与静态文件（路径基于仓库根目录，避免依赖启动 cwd）
BASE_DIR = BACKEND_ROOT
FRONTEND_DIR = FRONTEND_ROOT
templates = Jinja2Templates(directory=str(FRONTEND_DIR))
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ============ 图片内联：把 images 字典编码成 base64 塞进文本 ============
def _inline_images(text: str, ext: str, images: dict) -> str:
    """把 marker 返回的图片（{文件名: PIL.Image}）内联进 md/html。

    marker 默认把图片存成独立文件，并在文本里用 ![](文件名) / <img src="文件名">
    引用。此函数把每张图编码成 data URI 直接替换引用，使单个 md/html
    自带全部图片，下载或预览都能看到原图。json 格式无图片，原样返回。
    """
    if not images or ext not in ("md", "html"):
        return text
    for img_name, img in images.items():
        suffix = Path(img_name).suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            fmt, mime = "JPEG", "image/jpeg"
            if img.mode != "RGB":  # RGBA 不能存 JPEG
                img = img.convert("RGB")
        else:
            fmt, mime = "PNG", "image/png"
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        data_uri = f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"
        # markdown: ![alt](文件名)  html: src="文件名" / src='文件名'
        text = text.replace(f"]({img_name})", f"]({data_uri})")
        text = text.replace(f'src="{img_name}"', f'src="{data_uri}"')
        text = text.replace(f"src='{img_name}'", f"src='{data_uri}'")
    return text


_MARKDOWN_IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()([^)]*)(\))")
_FIGURE_LABEL_RE = re.compile(r"\b(?:fig(?:ure)?)[. ]*([0-9]+[a-z]?)\b", re.IGNORECASE)
_FIGURE_CAPTION_ANCHOR_RE = re.compile(
    r"(?im)^\s*(?:<[^>]+>\s*)*\*{0,2}(?:fig(?:ure)?)[. ]*([0-9]+[a-z]?)\b"
)


def _replace_repaired_figure_images(text: str, repaired: dict[str, str]) -> str:
    """Replace only Marker image URLs whose following caption identifies a figure.

    The Markdown text, including all prose and caption text, is preserved byte
    for byte. Only the URL inside the existing image reference is changed.
    """
    if not text or not repaired:
        return text
    matches = list(_MARKDOWN_IMAGE_RE.finditer(text))
    replacements = []
    for index, match in enumerate(matches):
        window_end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 1200)
        following = text[match.end():window_end]
        label = _FIGURE_LABEL_RE.search(following)
        if not label:
            continue
        figure_id = "fig" + label.group(1).lower()
        image = repaired.get(figure_id)
        if image:
            replacements.append((match.start(2), match.end(2), image))
    for start, end, value in reversed(replacements):
        text = text[:start] + value + text[end:]
    return text


def _figure_ids_in_text(text: str) -> list[str]:
    found = []
    for match in _FIGURE_CAPTION_ANCHOR_RE.finditer(text or ""):
        figure_id = "fig" + match.group(1).lower()
        if figure_id not in found:
            found.append(figure_id)
    return found


# ============ 核心转换（同步，跑在线程池里）============
def _convert(
    pdf_path: str,
    output_format: str = "markdown",
    use_llm: bool = False,
    table_mode: bool = False,
    google_api_key: str | None = None,
    openai_api_key: str | None = None,
    marker_config: str | None = None,
):
    """复刻 marker_demo.py 的转换逻辑。

    返回 (text, ext)：text 是转换文本，ext 是扩展名（md/json/html）。
    google_api_key / openai_api_key 会在启用 use_llm 时注入 converter 配置，
    使网页填写的 key 生效（marker 的 settings 在导入时缓存，环境变量方式不生效）。

    本函数只在转换子进程内被调用：模型字典由子进程首次调用时加载并缓存在
    本进程的 MODELS 中（主进程不再常驻模型，避免显存翻倍）。
    """
    artifact_dict = MODELS.get("artifact_dict")
    if artifact_dict is None:
        artifact_dict = create_model_dict()
        MODELS["artifact_dict"] = artifact_dict
    cfg = _load_marker_config(marker_config)

    if table_mode:
        # 表格专属提取（marker_demo.demo_table）
        converter = TableConverter(artifact_dict=artifact_dict, config=cfg)
        rendered = converter(pdf_path)
        text, ext, images = text_from_rendered(rendered)
        text = _inline_images(text, ext, images)
        return text, ext

    # 基础转换 / LLM 润色（marker_demo.demo_basic / demo_with_llm）
    kwargs = dict(cfg)                        # 先合并 marker 配置（batch_size 等）
    kwargs["output_format"] = output_format  # 接口显式格式优先
    if use_llm:
        kwargs["use_llm"] = True
        # 用哪个 key 决定用哪个 LLM 服务
        if openai_api_key:
            kwargs["llm_service"] = "marker.services.openai.OpenAIService"
        elif google_api_key:
            kwargs["llm_service"] = "marker.services.gemini.GoogleGeminiService"
    config_parser = ConfigParser(kwargs)
    config_dict = config_parser.generate_config_dict()
    if google_api_key:
        config_dict["gemini_api_key"] = google_api_key
    if openai_api_key:
        config_dict["openai_api_key"] = openai_api_key
    converter = PdfConverter(
        config=config_dict,
        artifact_dict=artifact_dict,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )
    rendered = converter(pdf_path)
    text, ext, images = text_from_rendered(rendered)
    text = _inline_images(text, ext, images)
    if output_format in {"markdown", "html"}:
        try:
            internal_json, _ = _convert(
                pdf_path, "json", False, False, None, None, marker_config,
            )
            repaired = render_complete_figure_images(
                pdf_path, internal_json, _figure_ids_in_text(text),
            )
            text = _replace_repaired_figure_images(text, repaired)
        except Exception:
            # Image repair is supplementary. A missing Poppler/runtime must
            # never prevent the original Marker text from being returned.
            logger.exception("Marker 图像修复失败 format=%s", output_format)
    return text, ext


def _start_estimated_progress(progress, start: float, ceiling: float, interval: float = 0.7):
    """在 Marker 未提供细粒度回调时，平滑上报不超过 ceiling 的估算进度。

    这个进度只用于避免长时间停留在单一里程碑；调用方必须在阶段文案中明确标注
    “进度为估算”。Marker 完成后仍由真实阶段里程碑覆盖最终百分比。
    """
    if progress is None or ceiling <= start:
        return None, None

    stop_event = threading.Event()
    state = {"percent": float(start)}

    def creep():
        while not stop_event.wait(interval):
            state["percent"] = min(
                ceiling,
                state["percent"] + max(0.15, (ceiling - state["percent"]) * 0.025),
            )
            progress(round(state["percent"], 1))

    thread = threading.Thread(target=creep, daemon=True)
    thread.start()
    return stop_event, thread


def _stop_estimated_progress(stop_event, thread) -> None:
    if stop_event is not None:
        stop_event.set()
    if thread is not None:
        thread.join(timeout=1)


def _convert_markdown_and_json_once(
    pdf_path: str,
    marker_config: str | None = None,
    progress=None,
) -> tuple[str, str]:
    """一次构建 Marker Document，同时渲染分析用 Markdown 和内部 JSON。

    PdfConverter(pdf_path) 每调用一次都会重新执行 PDF 读取、版面分析、OCR、表格/公式
    识别和 processors。材料工作区需要的 Markdown、FigureIndex 和图片修复都来自同一份
    文档结构，因此这里复用 build_document 的结果，再分别调用 Markdown/JSON renderer。
    """
    def report(stage: str, percent: float) -> None:
        if progress is not None:
            progress(stage, percent)

    report("正在初始化 Marker 模型", 10.0)
    artifact_dict = MODELS.get("artifact_dict")
    if artifact_dict is None:
        artifact_dict = create_model_dict()
        MODELS["artifact_dict"] = artifact_dict
    report("Marker 模型初始化完成，正在准备解析器", 22.0)

    # 解析配置沿用完整 Markdown 路径；output_format 只决定 ConfigParser 的默认 renderer，
    # 实际的 Markdown 和 JSON 输出在同一个 Document 上分别完成。
    kwargs = dict(_load_marker_config(marker_config))
    kwargs["output_format"] = "markdown"
    config_parser = ConfigParser(kwargs)
    converter = PdfConverter(
        config=config_parser.generate_config_dict(),
        artifact_dict=artifact_dict,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )

    report("正在进行版面分析、OCR 与结构识别（进度为估算）", 28.0)
    stop_event, creep_thread = _start_estimated_progress(
        lambda percent: report("正在进行版面分析、OCR 与结构识别（进度为估算）", percent),
        start=28.0,
        ceiling=70.0,
    )
    try:
        document = converter.build_document(pdf_path)
    finally:
        _stop_estimated_progress(stop_event, creep_thread)

    converter.page_count = len(document.pages)
    report("文档识别完成，正在渲染 Markdown", 72.0)
    markdown_rendered = converter.resolve_dependencies(MarkdownRenderer)(document)
    markdown, ext, images = text_from_rendered(markdown_rendered)
    markdown = _inline_images(markdown, ext, images)

    report("正在渲染内部 JSON", 78.0)
    json_rendered = converter.resolve_dependencies(JSONRenderer)(document)
    internal_json, _, _ = text_from_rendered(json_rendered)
    report("Markdown 与内部 JSON 已生成", 80.0)
    return markdown, internal_json


# ============ 网页上传界面（资源均在 frontend/）============
@app.get("/")
async def index(request: Request):
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "title": "论文.pdf转换与信息提取",
            "formats": ["markdown", "json", "html"],
        },
    )
    # The task-lifecycle client and backend must stay in lockstep. Always
    # revalidate the HTML so a normal reload picks up the latest cache-buster.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# ============ 上传转换接口 ============
MIME_BY_EXT = {"md": "text/markdown", "json": "application/json", "html": "text/html"}


@app.post("/convert")
async def convert(
    request: Request,
    files: List[UploadFile] = File(..., description="PDF 文件，可多选"),
    output_format: List[str] = Form(["markdown"]),
    use_llm: bool = Form(False),
    table_mode: bool = Form(False),
    use_google: bool = Form(False),
    google_key: str = Form(""),
    use_openai: bool = Form(False),
    openai_key: str = Form(""),
    return_json: bool = Form(False),
    marker_config: str = Form("", description="可选：marker 配置 JSON 路径，覆盖 backend/config/marker_config.json"),
):
    return JSONResponse(
        {"error": "旧转换接口已停用；请使用 /api/conversion/batch 获取 Markdown+图片 ZIP。"},
        status_code=410,
    )
    allowed = {"markdown", "json", "html", "chunks"}
    fmts = [f for f in output_format if f in allowed]
    if not fmts:
        return Response(content="未选择有效的输出格式", status_code=400)
    if table_mode:
        # 表格模式只产出 markdown，忽略多选的格式
        fmts = ["markdown"]

    # 启用大模型润色时，至少得选并填一个 key
    gk = google_key.strip() if (use_google and google_key.strip()) else None
    ok = openai_key.strip() if (use_openai and openai_key.strip()) else None
    if use_llm and not gk and not ok:
        return Response(
            content="启用大模型润色需勾选并填写至少一个 API Key（Google 或 OpenAI）",
            status_code=400,
        )

    tmp_paths: List[str] = []
    items: list[tuple[str, str]] = []
    for uf in files:
        suffix = Path(uf.filename).suffix or ".pdf"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=".")
        content = await uf.read()
        tmp.write(content)
        tmp.close()
        tmp_paths.append(tmp.name)
        items.append((tmp.name, uf.filename))

    # 同步接口：起子进程转换并阻塞等待结果（无取消语义；模型在子进程内加载）
    ctx = _mp()
    q = ctx.Queue()
    slots = _get_conversion_slots()
    proc = ctx.Process(
        target=_convert_task_worker,
        args=(items, fmts, use_llm, table_mode, gk, ok, marker_config, q, slots,
              get_log_queue(), getattr(request.state, "request_id", ""), ""),
        daemon=True,
    )
    proc.start()
    results: list[dict] = []       # {source, format, filename, mime, content}
    any_failed = False
    try:
        while True:
            try:
                msg = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                if not proc.is_alive():
                    any_failed = True
                    break
                continue
            except (EOFError, OSError, ValueError):
                any_failed = True
                break
            kind = msg[0]
            if kind == "result":
                results.append(msg[1])
            elif kind == "done":
                any_failed = not bool(msg[1].get("success", True))
                break
            elif kind == "error":
                any_failed = True
                break
    finally:
        try:
            await asyncio.to_thread(proc.join, 5)
        except Exception:
            pass
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            q.close()
        except Exception:
            pass

    # —— 网页预览模式：返回 JSON，前端逐卡片渲染 + 右上角下载 ——
    if return_json:
        return JSONResponse({"success": not any_failed, "results": results})

    # —— 兼容旧行为（curl / 直接调用）：单文件直接下载，多文件打包 zip ——
    if len(results) == 1:
        r = results[0]
        disposition = f"attachment; filename*=UTF-8''{quote(r['filename'])}"
        return Response(
            content=r["content"],
            media_type=MIME_BY_EXT.get(r["ext"], "application/octet-stream") if "ext" in r else r["mime"],
            headers={"Content-Disposition": disposition},
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in results:
            z.writestr(r["filename"], r["content"])
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote("marker_results.zip")},
    )


# ============ 异步转换任务：提交后轮询进度（网页端实时进度条用） ============
def _prune_tasks():
    """清理 1 小时前已完成的任务，防止 TASKS 无限增长。"""
    now = time.time()
    stale = [tid for tid, t in TASKS.items() if t.get("done") and now - t.get("created", now) > 3600]
    for tid in stale:
        TASKS.pop(tid, None)


def _cleanup_convert_task_files(task: dict):
    """清理转换任务临时文件；取消接口和消费协程都可能调用，因此必须幂等。"""
    for path, _ in task.get("_items", []):
        try:
            os.remove(path)
        except OSError:
            pass


# ============ 转换 worker（子进程入口）============
def _convert_task_worker(
    items, fmts, use_llm, table_mode, gk, ok, marker_config, q,
    conversion_slots, log_queue=None, request_id="", task_id="",
):
    """子进程入口：加载模型并串行转换全部文件；每文件/格式结果与进度经 q 回传主进程。
    主进程可随时 terminate 本进程实现立即取消（当前正在转的文件会丢弃）。"""
    configure_worker_logging(log_queue)
    worker_token = bind_log_context(request_id=request_id, task_id=task_id)
    worker_logger = logging.getLogger("paper.worker.convert")
    worker_logger.info("转换 worker 启动 files=%d formats=%s", len(items), ",".join(fmts))
    state = {"p": 0.0, "cap": 0.0}
    stop_evt = threading.Event()

    def _creep():
        # 单个文件转换期间让进度平滑爬升（模拟原 asyncio 版 _creep）
        while not stop_evt.wait(0.5):
            if state["cap"] > state["p"]:
                state["p"] = min(state["cap"], state["p"] + max(0.1, (state["cap"] - state["p"]) * 0.04))
                q.put(("percent", round(state["p"], 1)))

    t = threading.Thread(target=_creep, daemon=True)
    t.start()
    try:
        with (conversion_slots if conversion_slots is not None else nullcontext()):
            total = max(1, len(items) * len(fmts))
            unit = 0
            any_failed = False
            for path, orig_name in items:
                for fmt in fmts:
                    seg_start = unit / total * 100
                    seg_end = (unit + 1) / total * 100
                    state["p"] = seg_start
                    state["cap"] = seg_start + (seg_end - seg_start) * 0.95
                    q.put(("stage", f"正在转换 {orig_name}（{fmt}）"))
                    q.put(("percent", seg_start))
                    try:
                        text, ext = _convert(path, fmt, use_llm, table_mode, gk, ok, marker_config)
                    except Exception as e:  # 单个格式失败不影响其它
                        worker_logger.exception(
                            "单个格式转换失败 source=%s format=%s",
                            Path(orig_name).name, fmt,
                        )
                        text, ext = f"[转换失败] {orig_name} ({fmt}): {e}", "md"
                        any_failed = True
                    unit += 1
                    state["p"] = state["cap"] = unit / total * 100
                    q.put(("percent", unit / total * 100))
                    q.put(("result", {
                        "source": orig_name,
                        "format": fmt,
                        "filename": f"{Path(orig_name).stem}.{ext}",
                        "mime": MIME_BY_EXT.get(ext, "text/plain"),
                        "content": text,
                    }))
            q.put(("done", {"success": not any_failed}))
    except Exception as e:
        worker_logger.exception("转换 worker 异常退出")
        try:
            q.put(("error", str(e)))
        except Exception:
            pass
    finally:
        stop_evt.set()
        try:
            t.join(timeout=1)
        except Exception:
            pass
        reset_log_context(worker_token)


async def _consume_convert_task(task_id: str):
    """消费转换子进程回传的消息，更新 TASKS；子进程被终止时收尾标记取消/失败并清理临时文件。
    注意：multiprocessing.Queue 的写端句柄主进程也持有，子进程死亡不会产生 EOF，
    因此用带超时的 get + is_alive() 轮询判断收尾时机。"""
    task = TASKS[task_id]
    proc = task.get("proc")
    q = task.get("q")
    try:
        while True:
            try:
                msg = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                if proc is None or not proc.is_alive():
                    break   # 子进程已死（正常结束或被强杀），队列已清空
                continue
            except (EOFError, OSError, ValueError):
                logger.warning(
                    "转换 worker 队列异常或已关闭",
                    extra={"task_id": task_id}, exc_info=True,
                )
                break
            kind = msg[0]
            if kind == "stage":
                task["stage"] = msg[1]
            elif kind == "percent":
                task["percent"] = msg[1]
            elif kind == "result":
                task["results"].append(msg[1])
            elif kind == "done":
                if task.get("cancel"):
                    task["cancelled"] = True
                    task["success"] = False
                    task["stage"] = "已取消"
                else:
                    task["success"] = bool(msg[1].get("success", False))
                    task["stage"] = "完成" if task["success"] else "部分失败"
                task["percent"] = 100.0
                task["done"] = True
                return
            elif kind == "error":
                task["error"] = msg[1]
                task["stage"] = "失败"
                task["done"] = True
                return
    finally:
        if proc is not None:
            try:
                await asyncio.to_thread(proc.join, 2)
            except Exception:
                pass
        if task.get("cancel") and not task.get("done"):
            # 用户点了停止：子进程已被 terminate，保留已回传的已完成结果
            task["cancelled"] = True
            task["stage"] = "已取消"
            task["done"] = True
        elif not task.get("done"):
            task["error"] = task.get("error") or "转换进程意外退出"
            task["stage"] = "失败"
            task["done"] = True
            logger.error(
                "转换 worker 意外退出 exitcode=%s",
                proc.exitcode if proc is not None else None,
                extra={"task_id": task_id},
            )
        # 无论正常/取消/异常，临时上传文件统一由主进程清理（子进程被强杀时其 finally 不执行）
        _cleanup_convert_task_files(task)
        try:
            q.close()
        except Exception:
            pass


@app.post("/convert_async")
async def convert_async(
    files: List[UploadFile] = File(..., description="PDF 文件，可多选"),
    output_format: List[str] = Form(["markdown"]),
    use_llm: bool = Form(False),
    table_mode: bool = Form(False),
    use_google: bool = Form(False),
    google_key: str = Form(""),
    use_openai: bool = Form(False),
    openai_key: str = Form(""),
    marker_config: str = Form("", description="可选：marker 配置 JSON 路径，覆盖 backend/config/marker_config.json"),
):
    """提交转换任务，立即返回 task_id；用 GET /progress/{task_id} 轮询进度和结果。"""
    return JSONResponse(
        {"error": "旧预览转换接口已停用；请使用 /api/conversion/batch。"},
        status_code=410,
    )
    allowed = {"markdown", "json", "html", "chunks"}
    fmts = [f for f in output_format if f in allowed]
    if not fmts:
        return JSONResponse({"error": "未选择有效的输出格式"}, status_code=400)
    if table_mode:
        fmts = ["markdown"]

    gk = google_key.strip() if (use_google and google_key.strip()) else None
    ok = openai_key.strip() if (use_openai and openai_key.strip()) else None
    if use_llm and not gk and not ok:
        return JSONResponse(
            {"error": "启用大模型润色需勾选并填写至少一个 API Key（Google 或 OpenAI）"},
            status_code=400,
        )

    items: list[tuple[str, str]] = []
    for uf in files:
        suffix = Path(uf.filename).suffix or ".pdf"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=".")
        tmp.write(await uf.read())
        tmp.close()
        items.append((tmp.name, uf.filename))

    _prune_tasks()
    task_id = uuid.uuid4().hex
    ctx = _mp()
    q = ctx.Queue()
    slots = _get_conversion_slots()
    proc = ctx.Process(
        target=_convert_task_worker,
        args=(items, fmts, use_llm, table_mode, gk, ok, marker_config, q, slots,
              get_log_queue(), "", task_id),
        daemon=True,
    )
    TASKS[task_id] = {
        "created": time.time(),
        "percent": 0.0,
        "stage": "排队中",
        "done": False,
        "success": False,
        "results": [],
        "error": "",
        "proc": proc,
        "q": q,
        "_items": items,           # 供消费协程收尾统一清理临时文件
    }
    proc.start()
    asyncio.create_task(_consume_convert_task(task_id))
    return JSONResponse({"task_id": task_id})


@app.get("/progress/{task_id}")
async def progress(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    return JSONResponse({
        "percent": round(task["percent"], 1),
        "stage": task["stage"],
        "done": task["done"],
        "success": task["success"],
        "error": task["error"],
        "cancelled": bool(task.get("cancel", False)),
        # 仅在完成后返回结果，避免轮询期间传输大内容
        "results": task["results"] if task["done"] else [],
    })


@app.post("/api/stop_task/{task_id}")
async def stop_task(task_id: str):
    """请求终止正在进行的 PDF 转换任务：terminate 子进程实现立即取消
    （当前正在转的文件丢弃，已完成的文件结果保留）。"""
    task = TASKS.get(task_id)
    if task is None:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    if task.get("done"):
        return JSONResponse({"ok": True, "already_done": True})
    task["cancel"] = True
    task["stage"] = "正在取消…"
    task["status"] = "cancelling"
    proc = task.get("proc")
    if proc is not None and proc.is_alive():
        proc.terminate()   # 立即强杀转换子进程
        try:
            await asyncio.to_thread(proc.join, 5)
        except Exception:
            pass
        if proc.is_alive():
            try:
                proc.kill()
                await asyncio.to_thread(proc.join, 2)
            except Exception:
                pass
    # 不等待前端轮询，主进程直接清理上传文件；消费协程后续会再次幂等清理。
    _cleanup_convert_task_files(task)
    task["cancelled"] = True
    return JSONResponse({"ok": True, "stopped": True})


# ============ Marker 2 普通转换：有界批队列 + 每篇独立子进程 + ZIP ============
def _conversion_document_worker(
    pdf_path: str,
    source_name: str,
    task_id: str,
    document_id: str,
    doc_dir_path: str,
    marker_options: dict,
    q,
    conversion_slots,
    log_queue=None,
    request_id="",
):
    """One PDF per process. Large outputs are written to staging, never to Queue."""
    configure_worker_logging(log_queue)
    worker_token = bind_log_context(
        request_id=request_id, task_id=task_id, document_id=document_id,
    )
    worker_logger = logging.getLogger("paper.worker.conversion_document")
    worker_logger.info("文档转换 worker 启动")
    try:
        q.put(("queued", "等待转换槽位"))
        with (conversion_slots if conversion_slots is not None else nullcontext()):
            q.put(("stage", "正在启动 Marker 2.0", 3.0))

            def progress(stage: str, percent: float):
                q.put(("stage", stage, float(percent)))

            manifest = convert_pdf_to_staging(
                pdf_path=pdf_path,
                source_name=source_name,
                task_id=task_id,
                document_id=document_id,
                doc_dir=doc_dir_path,
                extra_config=marker_options,
                progress=progress,
            )
            q.put(("result", {
                "manifest_path": str(Path(doc_dir_path) / "manifest.json"),
                "document_id": document_id,
                "page_count": manifest.get("page_count", 0),
                "elapsed": manifest.get("elapsed", 0),
            }))
            q.put(("done", True))
    except Exception as exc:
        worker_logger.exception("文档转换 worker 异常退出")
        try:
            q.put(("error", str(exc)))
        except Exception:
            pass
    finally:
        reset_log_context(worker_token)


def _active_conversion_document_count() -> int:
    return sum(
        1
        for task in TASKS.values()
        if not task.get("done")
        for item in task.get("files", [])
        if item.get("status") in {"queued", "running"}
    )


async def _run_conversion_document(task_id: str, item: dict):
    task = TASKS[task_id]
    async_slots = _get_async_conversion_slots()
    item["status"] = "queued"
    item["stage"] = "等待三并发 worker 槽位"
    await async_slots.acquire()
    if task.get("cancel") or item.get("status") == "cancelled":
        item["status"] = "cancelled"
        item["stage"] = "已取消"
        Path(item["upload_path"]).unlink(missing_ok=True)
        async_slots.release()
        return
    ctx = _mp()
    q = ctx.Queue()
    process = ctx.Process(
        target=_conversion_document_worker,
        args=(
            item["upload_path"],
            item["source_name"],
            task_id,
            item["document_id"],
            item["doc_dir"],
            task.get("marker_options") or {},
            q,
            None,  # 主进程已持有 async_slots；子进程不得持有可泄漏的信号量
            get_log_queue(),
            task.get("request_id", ""),
        ),
        daemon=True,
    )
    task.setdefault("processes", {})[item["document_id"]] = process
    task.setdefault("queues", {})[item["document_id"]] = q
    try:
        logger.info(
            "文档转换子进程启动",
            extra={"request_id": task.get("request_id", ""), "task_id": task_id,
                   "document_id": item.get("document_id", "")},
        )
        await asyncio.to_thread(process.start)
        while True:
            if task.get("cancel"):
                break
            try:
                message = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                if not process.is_alive():
                    break
                continue
            except (EOFError, OSError, ValueError):
                logger.warning(
                    "文档转换 worker 队列异常或已关闭",
                    extra={"request_id": task.get("request_id", ""), "task_id": task_id,
                           "document_id": item.get("document_id", "")},
                    exc_info=True,
                )
                break
            kind = message[0]
            if kind == "queued":
                item["status"] = "queued"
                item["stage"] = message[1]
            elif kind == "stage":
                item["status"] = "running"
                item["stage"] = message[1]
                item["percent"] = max(item.get("percent", 0), float(message[2]))
            elif kind == "result":
                item.update(message[1])
            elif kind == "done":
                item["status"] = "completed"
                item["stage"] = "完成"
                item["percent"] = 100.0
                logger.info(
                    "文档转换完成",
                    extra={"request_id": task.get("request_id", ""), "task_id": task_id,
                           "document_id": item.get("document_id", "")},
                )
                return
            elif kind == "error":
                item["status"] = "failed"
                item["stage"] = "失败"
                item["error"] = message[1]
                logger.error(
                    "文档转换失败 error=%s", str(message[1])[:300],
                    extra={"request_id": task.get("request_id", ""), "task_id": task_id,
                           "document_id": item.get("document_id", "")},
                )
                return
    finally:
        if task.get("cancel") and process.is_alive():
            process.terminate()
        try:
            await asyncio.to_thread(process.join, 3)
        except Exception:
            pass
        if item.get("status") not in {"completed", "failed", "cancelled"}:
            if task.get("cancel"):
                item["status"] = "cancelled"
                item["stage"] = "已取消"
            else:
                item["status"] = "failed"
                item["stage"] = "失败"
                item["error"] = item.get("error") or "转换子进程意外退出"
                logger.error(
                    "文档转换 worker 意外退出 exitcode=%s",
                    process.exitcode,
                    extra={"request_id": task.get("request_id", ""), "task_id": task_id,
                           "document_id": item.get("document_id", "")},
                )
        Path(item["upload_path"]).unlink(missing_ok=True)
        try:
            q.close()
        except Exception:
            pass
        async_slots.release()


async def _run_conversion_batch(task_id: str):
    task = TASKS[task_id]
    try:
        workers = [
            asyncio.create_task(_run_conversion_document(task_id, item))
            for item in task["files"]
        ]
        await asyncio.gather(*workers)
        if task.get("cancel"):
            task["stage"] = "已取消"
            task["success"] = False
            task["cancelled"] = True
            logger.info(
                "转换批次已取消",
                extra={"request_id": task.get("request_id", ""), "task_id": task_id},
            )
        else:
            completed = [item for item in task["files"] if item["status"] == "completed"]
            failed = [item for item in task["files"] if item["status"] == "failed"]
            if completed:
                manifests = [read_json(Path(item["manifest_path"])) for item in completed]
                export_path = await asyncio.to_thread(build_export_zip, task_id, manifests)
                task["export_path"] = str(export_path)
                task["download_url"] = f"/api/conversion/download/{task_id}"
            task["success"] = bool(completed) and not failed
            task["stage"] = "完成" if task["success"] else ("部分失败" if completed else "失败")
            logger.info(
                "转换批次完成 success=%s completed=%d failed=%d",
                task["success"], len(completed), len(failed),
                extra={"request_id": task.get("request_id", ""), "task_id": task_id},
            )
    except Exception as exc:
        logger.exception(
            "转换批次失败",
            extra={"request_id": task.get("request_id", ""), "task_id": task_id},
        )
        task["error"] = str(exc)
        task["stage"] = "失败"
        task["success"] = False
    finally:
        task["done"] = True
        task["percent"] = (
            sum(float(item.get("percent", 0)) for item in task.get("files", []))
            / max(1, len(task.get("files", [])))
        )


@app.post("/api/conversion/batch")
async def conversion_batch(
    request: Request,
    files: List[UploadFile] = File(..., description="PDF 文件，可多选"),
):
    max_files = int(RUNTIME_CONFIG.conversion.get("max_files_per_batch", 50))
    queue_size = int(RUNTIME_CONFIG.conversion.get("queue_size", 100))
    if not files:
        return JSONResponse({"error": "请至少选择一个 PDF"}, status_code=400)
    if len(files) > max_files:
        return JSONResponse({"error": f"单批最多 {max_files} 篇 PDF"}, status_code=400)
    if _active_conversion_document_count() + len(files) > queue_size:
        return JSONResponse({"error": f"转换队列已满（上限 {queue_size} 篇）"}, status_code=429)
    if any(not str(file.filename or "").lower().endswith(".pdf") for file in files):
        return JSONResponse({"error": "普通转换区仅接受 PDF 文件"}, status_code=400)

    _prune_tasks()
    task_id = uuid.uuid4().hex
    task_files = []
    try:
        for upload in files:
            document_id, doc_dir_path = create_document_dir(task_id)
            upload_path = UPLOAD_DIR / f"{task_id}_{document_id}.pdf"
            upload_path.write_bytes(await upload.read())
            task_files.append({
                "document_id": document_id,
                "source_name": upload.filename,
                "upload_path": str(upload_path),
                "doc_dir": str(doc_dir_path),
                "status": "queued",
                "stage": "排队中",
                "percent": 0.0,
                "error": "",
            })
    except Exception:
        logger.exception(
            "保存转换上传文件失败",
            extra={"request_id": getattr(request.state, "request_id", "")},
        )
        for item in task_files:
            Path(item["upload_path"]).unlink(missing_ok=True)
        raise

    TASKS[task_id] = {
        "created": time.time(),
        "request_id": getattr(request.state, "request_id", ""),
        "files": task_files,
        "marker_options": _load_marker_config(),
        "processes": {},
        "queues": {},
        "stage": "排队中",
        "percent": 0.0,
        "done": False,
        "success": False,
        "cancel": False,
        "cancelled": False,
        "error": "",
        "download_url": "",
    }
    logger.info(
        "提交转换批次 files=%d",
        len(task_files),
        extra={"request_id": getattr(request.state, "request_id", ""), "task_id": task_id},
    )
    asyncio.create_task(_run_conversion_batch(task_id))
    return JSONResponse({
        "task_id": task_id,
        "max_concurrent_pdfs": int(RUNTIME_CONFIG.conversion.get("max_concurrent_pdfs", 3)),
        "files": [{"document_id": item["document_id"], "name": item["source_name"]} for item in task_files],
    })


@app.get("/api/conversion/progress/{task_id}")
async def conversion_progress(task_id: str):
    task = TASKS.get(task_id)
    if task is None or "files" not in task:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    files_payload = [{
        "document_id": item["document_id"],
        "name": item["source_name"],
        "status": item["status"],
        "stage": item["stage"],
        "percent": round(float(item.get("percent", 0)), 1),
        "error": item.get("error", ""),
        "page_count": item.get("page_count", 0),
        "elapsed": round(float(item.get("elapsed", 0)), 2),
    } for item in task["files"]]
    percent = sum(item["percent"] for item in files_payload) / max(1, len(files_payload))
    task["percent"] = percent
    return JSONResponse({
        "task_id": task_id,
        "percent": round(percent, 1),
        "stage": task.get("stage", ""),
        "done": task.get("done", False),
        "success": task.get("success", False),
        "cancelled": task.get("cancelled", False),
        "error": task.get("error", ""),
        "download_url": task.get("download_url", ""),
        "files": files_payload,
    })


@app.post("/api/conversion/stop/{task_id}")
async def conversion_stop(task_id: str):
    task = TASKS.get(task_id)
    if task is None or "files" not in task:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    task["cancel"] = True
    task["stage"] = "正在取消"
    logger.info(
        "请求取消转换批次",
        extra={"request_id": task.get("request_id", ""), "task_id": task_id},
    )
    for process in list((task.get("processes") or {}).values()):
        if process.is_alive():
            process.terminate()
    return JSONResponse({"ok": True})


@app.post("/api/conversion/stop/{task_id}/{document_id}")
async def conversion_stop_one(task_id: str, document_id: str):
    task = TASKS.get(task_id)
    if task is None or "files" not in task:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    item = next((row for row in task["files"] if row["document_id"] == document_id), None)
    if item is None:
        return JSONResponse({"error": "PDF 子任务不存在"}, status_code=404)
    process = (task.get("processes") or {}).get(document_id)
    logger.info(
        "请求取消单篇转换",
        extra={"request_id": task.get("request_id", ""), "task_id": task_id,
               "document_id": document_id},
    )
    if process is not None and process.is_alive():
        process.terminate()
    item["status"] = "cancelled"
    item["stage"] = "已取消"
    return JSONResponse({"ok": True})


@app.get("/api/conversion/download/{task_id}")
async def conversion_download(task_id: str):
    task = TASKS.get(task_id)
    path = Path(task.get("export_path", "")) if task else None
    if path is None or not path.exists():
        return JSONResponse({"error": "下载包不存在或已过期"}, status_code=404)
    completed_count = sum(1 for item in task["files"] if item["status"] == "completed")
    if completed_count == 1:
        source = next(item["source_name"] for item in task["files"] if item["status"] == "completed")
        filename = f"{safe_name(Path(source).stem)}.zip"
    else:
        filename = f"marker_batch_{task_id[:8]}.zip"
    return FileResponse(path, media_type="application/zip", filename=filename)


# ============ 本地文件转 MD（供材料工作区「＋」上传 pdf 时使用）============
@app.post("/api/convert_md")
async def convert_md(
    files: List[UploadFile] = File(..., description="PDF 文件，可多选"),
    marker_config: str = Form("", description="可选：marker 配置 JSON 路径，覆盖 backend/config/marker_config.json"),
    prepare_for_analysis: bool = Form(False),
):
    """把上传的 PDF 转成 Markdown。

    prepare_for_analysis=True 时同时建立内部 JSON 和 FigureIndex，供材料工作区的本地
    PDF 入口使用；默认行为仍兼容旧调用方，只返回 Markdown。
    """
    return JSONResponse(
        {"error": "旧 Base64 Markdown 接口已停用；普通转换请下载 ZIP，信息提取请使用 /api/prepare_paper。"},
        status_code=410,
    )
    cfg_path = marker_config or None
    items: list[tuple[str, str]] = []
    for uf in files:
        suffix = Path(uf.filename).suffix or ".pdf"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=".")
        content = await uf.read()
        tmp.write(content)
        tmp.close()
        items.append((tmp.name, uf.filename))
    if not items:
        return JSONResponse({"files": []})

    if prepare_for_analysis:
        out, errors = [], []
        try:
            for path, source_name in items:
                prepared, error = await _prepare_one_paper_sync(
                    path, source_name, "", cfg_path,
                )
                if error:
                    errors.append({"name": source_name, "error": error})
                    continue
                out.append({
                    "name": source_name,
                    "md": prepared.get("markdown", ""),
                    "figure_index": prepared.get("figure_index", []),
                })
        finally:
            for path, _ in items:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return JSONResponse({"files": out, "errors": errors})

    ctx = _mp()
    q = ctx.Queue()
    slots = _get_conversion_slots()
    proc = ctx.Process(
        target=_convert_task_worker,
        args=(items, ["markdown"], False, False, None, None, cfg_path, q, slots,
              get_log_queue(), "", ""),
        daemon=True,
    )
    proc.start()
    out = []
    try:
        while True:
            try:
                msg = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                if not proc.is_alive():
                    break
                continue
            except (EOFError, OSError, ValueError):
                logger.warning(
                    "同步转换 worker 队列异常或已关闭",
                    extra={},
                    exc_info=True,
                )
                break
            kind = msg[0]
            if kind == "result":
                out.append({"name": msg[1]["source"], "md": msg[1]["content"]})
            elif kind in ("done", "error"):
                break
    finally:
        try:
            await asyncio.to_thread(proc.join, 5)
        except Exception:
            pass
        for p, _ in items:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            q.close()
        except Exception:
            pass
    return JSONResponse({"files": out})



# ============ 论文预处理：原始 PDF -> Markdown + 内部 JSON -> FigureIndex ============
_FIGURE_LABEL_RE = re.compile(
    r"(?:\b(?:fig(?:ure)?|fig\.)\s*(\d+[a-zA-Z]?)|图\s*(\d+[a-zA-Z]?))",
    re.IGNORECASE,
)
def _figure_key(value: str) -> str:
    """把 Fig. 3 / Figure 3a / 图3 统一成 FigureIndex 的 fig3 / fig3a 键。"""
    value = str(value or "").strip()
    value = re.sub(r"\s+", "", value).lower()
    return f"fig{value}" if re.fullmatch(r"\d+[a-z]?", value) else ""


def _first_figure_label(value: str) -> tuple[str, str]:
    match = _FIGURE_LABEL_RE.search(value or "")
    if not match:
        return "", ""
    number = match.group(1) or match.group(2) or ""
    return _figure_key(number), match.group(0).strip()


def _json_text(value) -> str:
    """从一个 Marker JSON 节点自身的常见内容字段提取文本。

    不向子节点递归。否则文档/页面等父节点会包含整棵树的所有图注，导致第一张图
    错误吸收整篇论文内容；子节点由调用方的 visit() 单独遍历。
    """
    if not isinstance(value, dict):
        return str(value) if isinstance(value, str) else ""
    chunks = []
    for key in ("caption", "text", "html", "markdown", "content", "raw", "value"):
        item = value.get(key)
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, list):
            chunks.extend(part for part in item if isinstance(part, str))
    return "\n".join(chunk for chunk in chunks if chunk)


def _bbox_center(value) -> tuple[float, float] | None:
    """Return a center point from Marker bbox/polygon variants."""
    if isinstance(value, dict):
        for key in ("bbox", "polygon", "coordinates"):
            center = _bbox_center(value.get(key))
            if center is not None:
                return center
        values = [value.get(key) for key in ("x0", "y0", "x1", "y1")]
        if all(isinstance(item, (int, float)) for item in values):
            return ((values[0] + values[2]) / 2, (values[1] + values[3]) / 2)
        return None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if all(isinstance(item, (int, float)) for item in value):
        if len(value) == 4:
            return ((value[0] + value[2]) / 2, (value[1] + value[3]) / 2)
        return (value[0], value[1]) if len(value) >= 2 else None
    points = [_bbox_center(item) for item in value]
    points = [point for point in points if point is not None]
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _json_image_data_uris(value) -> list[str]:
    """Read base64 image bytes stored directly in a Marker JSON Figure node."""
    found: list[str] = []

    def append_image(item):
        if not isinstance(item, str) or not item:
            return
        if item.startswith("data:image/"):
            data_uri = item
        elif item.startswith("/9j/"):
            data_uri = f"data:image/jpeg;base64,{item}"
        elif item.startswith("iVBORw0KGgo"):
            data_uri = f"data:image/png;base64,{item}"
        elif item.startswith("R0lGOD"):
            data_uri = f"data:image/gif;base64,{item}"
        elif item.startswith("UklGR"):
            data_uri = f"data:image/webp;base64,{item}"
        else:
            return
        if data_uri not in found:
            found.append(data_uri)

    if isinstance(value, dict):
        for item in value.values():
            append_image(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            append_image(item)
    else:
        append_image(value)
    return found


class _ContentRefParser(HTMLParser):
    """Collect Marker content-ref src values from a JSON HTML fragment."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []

    def _collect(self, tag, attrs):
        if str(tag).lower() != "content-ref":
            return
        for name, value in attrs:
            if str(name).lower() == "src" and isinstance(value, str):
                self.refs.append(value)
                return

    def handle_starttag(self, tag, attrs):
        self._collect(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._collect(tag, attrs)


def _normalized_json_ref(value) -> str:
    """Normalize a Marker JSON node id/content-ref src for exact comparison."""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    ref = html_unescape(value).strip().replace("\\", "/")
    if not ref:
        return ""
    ref = ref.split("#", 1)[0].split("?", 1)[0].strip()
    if not ref:
        return ""
    return "/" + ref.lstrip("/").casefold()


def _node_json_ref(node) -> str:
    """Return the stable JSON path assigned to a Marker block, if present."""
    if not isinstance(node, dict):
        return ""
    for key in ("id", "ref", "path", "source_ref"):
        ref = _normalized_json_ref(node.get(key))
        if ref:
            return ref
    return ""


def _json_ref_candidates(value, page=None) -> list[str]:
    """Return ordered aliases for Marker node/content-ref identifiers.

    Marker JSON releases have used full paths (``/page/0/figure/2``), typed
    suffixes (``figure/2``), and bare block ids (``2``).  Resolve the most
    specific spelling first and only use a page-qualified/bare alias as a
    fallback so a repeated block id on another page cannot steal a figure.
    """
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return []
    raw = html_unescape(value).strip().replace("\\", "/")
    if not raw:
        return []
    raw = raw.split("#", 1)[0].split("?", 1)[0].strip()
    normalized = _normalized_json_ref(raw)
    if not normalized:
        return []
    parts = normalized.strip("/").split("/")
    candidates = [normalized]
    if len(parts) >= 2:
        candidates.append("/" + "/".join(parts[-2:]))
    if page is not None:
        # A bare block id in FigureGroup.structure is scoped to its page.
        candidates.append(_normalized_json_ref(f"/page/{page}/{raw.lstrip('/')}"))
        if parts:
            candidates.append(_normalized_json_ref(f"/page/{page}/{parts[-1]}"))
    if parts:
        candidates.append("/" + parts[-1])
    return list(dict.fromkeys(item for item in candidates if item))


def _add_json_ref_aliases(target: dict[str, set], values, page=None, item=None) -> None:
    """Add a node id under all safe aliases used by FigureGroup matching."""
    for value in values:
        for alias in _json_ref_candidates(value, page):
            target.setdefault(alias, set()).add(item)


def _resolve_json_ref(alias_map: dict[str, set], value, page=None) -> set:
    """Resolve one ref, preferring exact/specific aliases over bare ids."""
    for alias in _json_ref_candidates(value, page):
        matches = alias_map.get(alias) or set()
        if len(matches) == 1:
            return set(matches)
    return set()


def _figure_group_content_refs(value) -> list[str]:
    """Read structured FigureGroup content-ref values without text-order inference."""
    parser = _ContentRefParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:
        return []
    return list(dict.fromkeys(
        ref for value in parser.refs
        if (ref := _normalized_json_ref(value))
    ))


def _ref_block_type(ref: str) -> str:
    """Identify the referenced Marker block type from its stable JSON path."""
    match = re.search(r"/(figure|picture|chart|image|caption)(?:/|$)", str(ref), re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return "visual" if re.search(r"(?:^|/)\d+$", str(ref)) else ""


def _build_figure_index(internal_json: str) -> list[dict]:
    """从 Marker JSON 的 Figure/Caption 节点建立图号白名单。

    Marker 不同版本的 JSON 字段名略有差异，因此这里按节点类型和 caption 文本双重
    识别真实 Fig. N，并保留 JSON 中可用的 page_id / polygon / bbox。Caption 与
    Figure/Picture 仅在同页按坐标一对一配对；图片只读取 JSON Figure 节点引用的
    Marker 图片资源。完全不使用 Markdown 图注、文档顺序或距离推断。
    """
    try:
        payload = json.loads(internal_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}

    figures: dict[str, dict] = {}

    figure_nodes: list[dict] = []
    figure_nodes_by_ref: dict[str, set[int]] = {}
    caption_figures_by_ref: dict[str, set[str]] = {}
    figure_group_refs: list[dict] = []
    labelled_figure_nodes: set[int] = set()
    visit_order = 0

    def page_number_from_node(node, fallback):
        for key in ("page_id", "page", "page_number"):
            if node.get(key) is not None:
                return node[key]
        match = re.search(r"(?:page|p)\D*(\d+)", str(node.get("id") or ""), re.IGNORECASE)
        return int(match.group(1)) if match else fallback

    def visit(node, page=None):
        nonlocal visit_order
        if isinstance(node, list):
            for child in node:
                visit(child, page)
            return
        if not isinstance(node, dict):
            return
        block_type = str(
            node.get("block_type")
            or node.get("type")
            or node.get("block_type_name")
            or node.get("name")
            or ""
        ).strip().lower()
        current_page = page
        if block_type == "page":
            current_page = page_number_from_node(node, page)
        text = _json_text(node)
        fig_id, label = _first_figure_label(text)
        is_visual_node = block_type in {"figure", "picture", "chart", "image", "11", "20"}
        is_caption_node = block_type in {"caption", "9"}
        is_figure_group = block_type in {"figuregroup", "figure_group", "4"}
        bbox = node.get("bbox") or node.get("polygon") or node.get("coordinates")
        images = _json_image_data_uris(node.get("images")) if is_visual_node else []
        node_ref = _node_json_ref(node)
        node_ref_values = [
            node.get(key)
            for key in ("id", "ref", "path", "source_ref", "block_id")
            if node.get(key) is not None
        ]
        node_index = None
        if is_visual_node and images:
            node_index = len(figure_nodes)
            figure_nodes.append({
                "page": current_page,
                "bbox": bbox,
                "order": visit_order,
                "images": images,
            })
            _add_json_ref_aliases(
                figure_nodes_by_ref,
                node_ref_values,
                current_page,
                node_index,
            )
        if is_figure_group:
            refs = _figure_group_content_refs(text)
            for member in node.get("structure") or []:
                if isinstance(member, dict) and member.get("block_id") is not None:
                    block_id = member["block_id"]
                    refs.append(block_id)
                    block_type = str(
                        member.get("block_type")
                        or member.get("type")
                        or member.get("block_type_name")
                        or ""
                    ).strip()
                    if block_type:
                        refs.append(f"{block_type}/{block_id}")
                    member_page = member.get("page_id", current_page)
                    if member_page is not None:
                        block_path = str(block_id).lstrip("/")
                        typed_id = (
                            block_path
                            if "/" in block_path or not block_type
                            else f"{block_type}/{block_path}"
                        )
                        refs.append(f"/page/{member_page}/{typed_id.lstrip('/')}")
            if refs:
                figure_group_refs.append({"refs": refs, "page": current_page})
        if fig_id and (is_caption_node or is_visual_node or node.get("caption") is not None):
            entry = figures.setdefault(fig_id, {
                "id": fig_id,
                "label": label or f"Fig. {fig_id[3:]}",
                "caption": "",
                "page": current_page,
                "bbox": bbox,
                "position": visit_order,
                "image": "",
            })
            if entry["page"] is None:
                entry["page"] = current_page
            # Marker can encode scientific typesetting as literal HTML entities
            # (for example Fe&lt;sub&gt;75&lt;/sub&gt;). Decode it once so the
            # browser preview can render the sub/sup tags as real typography.
            caption = html_unescape(text).strip()
            if len(caption) > len(entry["caption"]):
                entry["caption"] = caption[:2000]
            if bbox is not None:
                entry["bbox"] = bbox
            if is_visual_node:
                entry["images"] = images
                if node_index is not None:
                    labelled_figure_nodes.add(node_index)
            if is_caption_node:
                _add_json_ref_aliases(
                    caption_figures_by_ref,
                    node_ref_values,
                    current_page,
                    fig_id,
                )
        visit_order += 1
        for child in node.values():
            if isinstance(child, (dict, list)):
                visit(child, current_page)

    visit(payload)

    # A FigureGroup records Marker JSON's explicit Figure/Caption membership. Use
    # only unambiguous one-to-one groups before considering geometric fallback.
    direct_pairs: set[tuple[str, int]] = set()
    for group in figure_group_refs:
        caption_ids: set[str] = set()
        visual_node_indexes: set[int] = set()
        group_page = group.get("page")
        for ref in group.get("refs") or []:
            caption_ids.update(
                _resolve_json_ref(caption_figures_by_ref, ref, group_page)
            )
            visual_node_indexes.update(
                _resolve_json_ref(figure_nodes_by_ref, ref, group_page)
            )
        if len(caption_ids) == 1 and len(visual_node_indexes) == 1:
            direct_pairs.add((next(iter(caption_ids)), next(iter(visual_node_indexes))))

    direct_by_figure: dict[str, set[int]] = {}
    direct_by_node: dict[int, set[str]] = {}
    for fig_id, node_index in direct_pairs:
        direct_by_figure.setdefault(fig_id, set()).add(node_index)
        direct_by_node.setdefault(node_index, set()).add(fig_id)

    claimed_figures = {
        fig_id for fig_id, entry in figures.items() if entry.get("images")
    }
    claimed_nodes = set(labelled_figure_nodes)
    for fig_id, node_indexes in direct_by_figure.items():
        if len(node_indexes) != 1:
            continue
        node_index = next(iter(node_indexes))
        if len(direct_by_node.get(node_index, set())) != 1 or fig_id not in figures:
            continue
        node = figure_nodes[node_index]
        figures[fig_id]["bbox"] = node.get("bbox")
        figures[fig_id]["images"] = node.get("images") or []
        claimed_figures.add(fig_id)
        claimed_nodes.add(node_index)

    # For JSON documents without an explicit FigureGroup link, fall back to
    # same-page coordinates. Each remaining visual block may bind to one caption.
    candidates: list[tuple[float, str, int]] = []
    for fig_id, entry in figures.items():
        if fig_id in claimed_figures or entry.get("images"):
            continue
        caption_center = _bbox_center(entry.get("bbox"))
        if caption_center is None or entry.get("page") is None:
            continue
        for node_index, node in enumerate(figure_nodes):
            if node_index in claimed_nodes:
                continue
            if node.get("page") != entry.get("page"):
                continue
            figure_center = _bbox_center(node.get("bbox"))
            if figure_center is None:
                continue
            distance = (
                (caption_center[0] - figure_center[0]) ** 2
                + (caption_center[1] - figure_center[1]) ** 2
            )
            candidates.append((distance, fig_id, node_index))

    # Do not break equal-distance ties by document order. A match is accepted
    # only when the caption and Figure/Picture are each other's unique closest
    # JSON-coordinate candidate on the same page.
    by_figure: dict[str, list[tuple[float, int]]] = {}
    by_node: dict[int, list[tuple[float, str]]] = {}
    for distance, fig_id, node_index in candidates:
        by_figure.setdefault(fig_id, []).append((distance, node_index))
        by_node.setdefault(node_index, []).append((distance, fig_id))

    unique_figure_match: dict[str, int] = {}
    for fig_id, matches in by_figure.items():
        matches.sort()
        if len(matches) == 1 or matches[0][0] < matches[1][0]:
            unique_figure_match[fig_id] = matches[0][1]

    unique_node_match: dict[int, str] = {}
    for node_index, matches in by_node.items():
        matches.sort()
        if len(matches) == 1 or matches[0][0] < matches[1][0]:
            unique_node_match[node_index] = matches[0][1]

    for fig_id, node_index in unique_figure_match.items():
        if unique_node_match.get(node_index) != fig_id:
            continue
        node = figure_nodes[node_index]
        figures[fig_id]["bbox"] = node.get("bbox")
        figures[fig_id]["images"] = node.get("images") or []

    for entry in figures.values():
        for image in entry.get("images") or []:
            if image:
                entry["image"] = image
                break
        entry.pop("images", None)
        if not entry["caption"]:
            entry["caption"] = entry["label"]

    def sort_key(item):
        suffix = item["id"][3:]
        match = re.match(r"(\d+)", suffix)
        return (int(match.group(1)) if match else 10**9, suffix)

    return sorted(figures.values(), key=sort_key)


def _paper_prepare_worker(
    pdf_path,
    source_name,
    task_id,
    document_id,
    doc_dir_path,
    marker_options,
    q,
    conversion_slots,
    log_queue=None,
    request_id="",
):
    """一次构建 Document，落盘 Markdown/JSON/图片索引，仅回传紧凑结果。"""
    configure_worker_logging(log_queue)
    worker_token = bind_log_context(
        request_id=request_id, task_id=task_id, document_id=document_id,
    )
    worker_logger = logging.getLogger("paper.worker.paper_prepare")
    worker_logger.info("论文预处理 worker 启动")
    try:
        with (conversion_slots if conversion_slots is not None else nullcontext()):
            def report(stage: str, percent: float) -> None:
                q.put(("stage", stage))
                q.put(("percent", percent))

            doc_dir_path = Path(doc_dir_path)
            engine = MarkerEngine(marker_options)
            rendered = engine.render_analysis_once(pdf_path, doc_dir_path, progress=report)
            markdown = rendered["markdown"]
            internal_json = rendered["internal_json"]
            assets = rendered["assets"]
            report("正在建立 FigureIndex", 82.0)
            figure_index = _build_figure_index(internal_json)
            q.put(("stage", "正在生成完整图像与临时资产索引"))
            repaired = render_complete_figure_images(
                pdf_path,
                internal_json,
                [str(item.get("id") or "") for item in figure_index],
            )
            for figure in figure_index:
                image = repaired.get(str(figure.get("id") or "").lower())
                if image:
                    figure["image"] = image
            figure_index, assets = persist_figure_index_images(
                doc_dir_path, figure_index, assets,
            )
            raw_structure = json.loads(internal_json or "{}")
            clean_structure, assets = persist_json_image_data(
                doc_dir_path, raw_structure, assets,
            )
            for figure in figure_index:
                if figure.get("image_url"):
                    figure["image_url"] = figure["image_url"].replace(
                        "{document_id}", document_id,
                    )
            # JSON 中生成的 URL 同样替换成实际 document_id。
            clean_structure_text = json.dumps(clean_structure, ensure_ascii=False)
            clean_structure_text = clean_structure_text.replace("{document_id}", document_id)
            clean_structure = json.loads(clean_structure_text)

            (doc_dir_path / "document.md").write_text(markdown, encoding="utf-8")
            write_json(doc_dir_path / "structure.json", clean_structure)
            write_json(doc_dir_path / "figure_index.json", figure_index)
            marker_meta = {
                "marker_version": engine.marker_version,
                "marker_config": engine.config,
                "marker_config_hash": engine.config_hash,
                "marker_metadata": rendered["metadata"],
                "page_count": rendered["page_count"],
            }
            write_json(doc_dir_path / "marker_meta.json", marker_meta)
            manifest = create_manifest(
                doc_dir_path,
                task_id=task_id,
                document_id=document_id,
                source_name=source_name,
                pdf_sha256=sha256_file(Path(pdf_path)),
                mode="analysis",
                marker_version=engine.marker_version,
                marker_config_hash=engine.config_hash,
                assets=assets,
                page_count=rendered["page_count"],
                elapsed=rendered["elapsed"],
                extra={
                    "markdown_path": "document.md",
                    "structure_path": "structure.json",
                    "figure_index_path": "figure_index.json",
                    "marker_metadata_path": "marker_meta.json",
                },
            )
            q.put(("result", {
                "name": source_name,
                "document_id": document_id,
                "figure_index": figure_index,
                "figure_count": len(figure_index),
                "image_count": sum(1 for item in figure_index if item.get("temp_asset_id")),
                "page_count": manifest.get("page_count", 0),
                "elapsed": manifest.get("elapsed", 0),
                "manifest_path": str(doc_dir_path / "manifest.json"),
            }))
            q.put(("percent", 100.0))
            q.put(("done", {"success": True}))
    except Exception as e:
        worker_logger.exception("论文预处理 worker 异常退出")
        try:
            q.put(("error", str(e)))
        except Exception:
            pass
    finally:
        reset_log_context(worker_token)


async def _prepare_one_paper_sync(pdf_path, source_name, markdown, marker_config):
    """供 /api/convert_md 的材料模式复用同一预处理子进程，返回 (result, error)。"""
    task_id = uuid.uuid4().hex
    document_id, doc_dir_path = create_document_dir(task_id)
    ctx = _mp()
    q = ctx.Queue()
    proc = ctx.Process(
        target=_paper_prepare_worker,
        args=(
            pdf_path, source_name, task_id, document_id, str(doc_dir_path),
            _load_marker_config(marker_config), q, _get_conversion_slots(),
            get_log_queue(), "",
        ),
        daemon=True,
    )
    proc.start()
    result, error = {}, ""
    try:
        while True:
            try:
                msg = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                if not proc.is_alive():
                    break
                continue
            except (EOFError, OSError, ValueError):
                logger.warning(
                    "同步论文预处理 worker 队列异常或已关闭",
                    extra={"task_id": task_id, "document_id": document_id},
                    exc_info=True,
                )
                break
            if msg[0] == "result":
                result = msg[1]
            elif msg[0] == "error":
                error = msg[1]
                break
            elif msg[0] == "done":
                break
    finally:
        try:
            await asyncio.to_thread(proc.join, 3)
        except Exception:
            pass
        try:
            q.close()
        except Exception:
            pass
    if not result and not error:
        error = "论文预处理进程意外退出"
        logger.error(
            "同步论文预处理 worker 意外退出 exitcode=%s",
            proc.exitcode,
            extra={"task_id": task_id, "document_id": document_id},
        )
    return result, error


def _prune_paper_prep_tasks():
    now = time.time()
    stale = [
        tid for tid, task in PAPER_PREP_TASKS.items()
        if task.get("done") and now - task.get("created", now) > 3600
    ]
    for tid in stale:
        PAPER_PREP_TASKS.pop(tid, None)
    _prune_workspace_tombstones(now)


def _prune_workspace_tombstones(now: float | None = None) -> None:
    now = now or time.time()
    for registry in (CANCELLED_PAPER_PREP_IDS, DELETED_WORKSPACE_DOCUMENTS):
        for key, created in list(registry.items()):
            if now - created > 3600:
                registry.pop(key, None)


def _process_is_alive(proc) -> bool:
    if proc is None:
        return False
    try:
        return bool(proc.is_alive())
    except (AssertionError, ValueError):
        return False


def _record_task_process_identity(task: dict) -> None:
    """Record PID + create_time immediately after spawn to prevent PID-reuse kills."""
    proc = task.get("proc")
    pid = getattr(proc, "pid", None) if proc is not None else None
    if not pid:
        return
    task["process_pid"] = int(pid)
    try:
        task["process_create_time"] = float(psutil.Process(pid).create_time())
        task.setdefault("process_tree_identities", {})[int(pid)] = task["process_create_time"]
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        # The multiprocessing handle remains the source of truth when a worker
        # exits between start() and identity capture.
        task["process_create_time"] = None


def _snapshot_task_process_tree(task: dict) -> None:
    """Remember descendants while the root is alive for orphan cleanup later."""
    pid = task.get("process_pid") or getattr(task.get("proc"), "pid", None)
    if not pid:
        return
    try:
        root = psutil.Process(int(pid))
        expected = task.get("process_create_time")
        if expected is not None and not _same_process(root, float(expected)):
            return
        identities = task.setdefault("process_tree_identities", {})
        identities[int(pid)] = root.create_time()
        for child in root.children(recursive=True):
            try:
                identities[int(child.pid)] = child.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass

def _same_process(process: psutil.Process, create_time: float | None) -> bool:
    try:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        return create_time is None or abs(process.create_time() - create_time) < 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def _terminate_process_tree_sync(task: dict) -> dict:
    """Terminate a tracked worker and every descendant, then verify they are gone."""
    proc = task.get("proc")
    pid = task.get("process_pid") or getattr(proc, "pid", None)
    create_time = task.get("process_create_time")
    errors: list[str] = []

    # Test doubles and a process that failed before start() have no PID. Keep a
    # multiprocessing-handle fallback, but never claim success while it is alive.
    if not pid:
        if _process_is_alive(proc):
            try:
                proc.terminate()
            except Exception as exc:
                errors.append(f"terminate: {exc}")
            try:
                proc.join(1.0)
            except Exception:
                pass
        if _process_is_alive(proc):
            try:
                proc.kill()
                proc.join(3.0)
            except Exception as exc:
                errors.append(f"kill: {exc}")
        alive = _process_is_alive(proc)
        return {
            "terminated_pids": [],
            "remaining_pids": [],
            "process_alive": alive,
            "errors": errors,
        }

    pid = int(pid)
    known_identities = {
        int(known_pid): float(known_time)
        for known_pid, known_time in (task.get("process_tree_identities") or {}).items()
        if known_time is not None
    }
    if create_time is not None:
        known_identities[pid] = float(create_time)

    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, OSError):
        root = None

    if root is not None and create_time is not None and not _same_process(root, float(create_time)):
        # Never kill an unrelated process that inherited a recycled PID.
        alive = _process_is_alive(proc)
        if alive:
            errors.append(f"PID {pid} identity mismatch")
        return {
            "terminated_pids": [],
            "remaining_pids": [pid] if alive else [],
            "process_alive": alive,
            "errors": errors,
        }

    # Suspend the root before enumerating children so it cannot create a new
    # descendant between the snapshot and termination.
    descendants: list[psutil.Process] = []
    if root is not None:
        try:
            root.suspend()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
            if not isinstance(exc, psutil.NoSuchProcess):
                errors.append(f"suspend {pid}: {exc}")
        try:
            descendants = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
            if not isinstance(exc, psutil.NoSuchProcess):
                errors.append(f"enumerate children {pid}: {exc}")
    else:
        # The root may have exited while a descendant survived. Use the last
        # safely captured PID/create-time pairs to remove those orphans.
        for known_pid, known_time in known_identities.items():
            try:
                candidate = psutil.Process(known_pid)
                if _same_process(candidate, known_time):
                    descendants.append(candidate)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass

    targets: list[psutil.Process] = []
    seen: set[int] = set()
    for process in [*descendants, root] if root is not None else descendants:
        if process is not None and process.pid not in seen:
            seen.add(process.pid)
            targets.append(process)
    # Include descendants seen by the consumer before the root disappeared.
    for known_pid, known_time in known_identities.items():
        if known_pid in seen:
            continue
        try:
            candidate = psutil.Process(known_pid)
            if _same_process(candidate, known_time):
                seen.add(known_pid)
                targets.append(candidate)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass

    identities: dict[int, float | None] = {}
    for process in targets:
        try:
            identities[process.pid] = known_identities.get(process.pid, process.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            identities[process.pid] = None

    # Children first, then the suspended root. A short graceful phase permits
    # native libraries to unwind; kill() is the hard deadline.
    for process in targets:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, OSError) as exc:
            errors.append(f"terminate {process.pid}: {exc}")
    _, alive = psutil.wait_procs(targets, timeout=1.0)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, OSError) as exc:
            errors.append(f"kill {process.pid}: {exc}")
    if alive:
        psutil.wait_procs(alive, timeout=3.0)

    remaining_pids = sorted(
        process.pid
        for process in targets
        if _same_process(process, identities.get(process.pid))
    )
    terminated_pids = sorted(set(identities) - set(remaining_pids))
    try:
        proc.join(0.5)
    except Exception:
        pass
    process_alive = _process_is_alive(proc)
    if process_alive and pid not in remaining_pids:
        remaining_pids.append(pid)
        remaining_pids.sort()
    return {
        "terminated_pids": terminated_pids,
        "remaining_pids": remaining_pids,
        "process_alive": process_alive,
        "errors": errors,
    }


async def _terminate_task_process(task: dict) -> dict:
    """Wait for an in-flight spawn, kill its full tree, and return proof."""
    lock = task.get("termination_lock")
    if lock is None:
        lock = asyncio.Lock()
        task["termination_lock"] = lock
    async with lock:
        previous = task.get("termination_result")
        if (
            previous
            and not previous.get("remaining_pids")
            and not previous.get("process_alive")
            and not _process_is_alive(task.get("proc"))
        ):
            return previous
        starter = task.get("start_future")
        if starter is not None and not starter.done():
            try:
                await asyncio.shield(starter)
            except Exception as exc:
                logger.warning(
                    "等待 worker 启动完成失败 error=%s", str(exc)[:200],
                    extra={"task_id": task.get("task_id", "")},
                )
        _record_task_process_identity(task)
        started = time.monotonic()
        result = await asyncio.to_thread(_terminate_process_tree_sync, task)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        task["termination_result"] = result
        log = logger.error if result["remaining_pids"] or result["process_alive"] else logger.info
        log(
            "worker 进程树终止结果 terminated=%s remaining=%s elapsed_ms=%.1f errors=%s",
            result["terminated_pids"], result["remaining_pids"], result["elapsed_ms"],
            result["errors"],
            extra={"request_id": task.get("request_id", ""),
                   "task_id": task.get("task_id", ""),
                   "document_id": task.get("document_id", "")},
        )
        return result


def _close_task_queue(task: dict) -> None:
    if task.get("queue_closed"):
        return
    q = task.get("q")
    if q is not None:
        try:
            q.close()
        except (OSError, ValueError):
            pass
        try:
            q.cancel_join_thread()
        except (AttributeError, OSError, ValueError):
            pass
    task["queue_closed"] = True


async def _wait_for_task_finalization(task: dict, timeout: float = 3.0) -> None:
    consumer = task.get("consumer")
    if consumer is asyncio.current_task():
        return
    if consumer is not None and not consumer.done():
        try:
            await asyncio.wait_for(asyncio.shield(consumer), timeout=timeout)
        except asyncio.TimeoutError:
            _close_task_queue(task)
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass


async def _cancel_paper_prepare_task(task_id: str) -> dict:
    """Idempotently cancel one preparation run, including pre-registration races."""
    CANCELLED_PAPER_PREP_IDS[task_id] = time.time()
    task = PAPER_PREP_TASKS.get(task_id)
    if task is None:
        return {
            "ok": True, "pending": True,
            "terminated_pids": [], "remaining_pids": [],
        }
    if task.get("done"):
        termination = await _terminate_task_process(task)
        if termination.get("remaining_pids") or termination.get("process_alive"):
            return {
                "ok": False, "cancelled": False,
                "error": "已结束的解析任务仍有残留进程",
                "terminated_pids": termination.get("terminated_pids", []),
                "remaining_pids": termination.get("remaining_pids", []),
            }
        _close_task_queue(task)
        await _wait_for_task_finalization(task)
        return {
            "ok": True, "already_done": True,
            "cancelled": bool(task.get("cancelled")),
            "terminated_pids": termination.get("terminated_pids", []),
            "remaining_pids": termination.get("remaining_pids", []),
        }
    task["cancel"] = True
    task["stage"] = "正在取消…"
    cancel_event = task.get("cancel_event")
    if cancel_event is not None:
        cancel_event.set()
    termination = await _terminate_task_process(task)
    if termination["remaining_pids"] or termination["process_alive"]:
        task["stage"] = "进程终止失败"
        return {
            "ok": False, "cancelled": False,
            "error": "PDF 解析进程未能完全退出",
            "terminated_pids": termination["terminated_pids"],
            "remaining_pids": termination["remaining_pids"],
        }
    _close_task_queue(task)
    await _wait_for_task_finalization(task)
    if not task.get("done"):
        task.update({
            "cancelled": True, "done": True, "success": False,
            "stage": "已取消", "result": {},
        })
    document_id = str(task.get("document_id") or "")
    if document_id:
        try:
            await asyncio.to_thread(delete_document_dir, document_id)
        except OSError:
            logger.warning(
                "取消解析后清理临时文档失败",
                extra={"task_id": task_id, "document_id": document_id},
                exc_info=True,
            )
    return {
        "ok": True, "stopped": True, "cancelled": True,
        "terminated_pids": termination["terminated_pids"],
        "remaining_pids": [],
    }


async def _consume_paper_prep_task(task_id: str):
    task = PAPER_PREP_TASKS[task_id]
    proc, q = task.get("proc"), task.get("q")
    try:
        while True:
            _snapshot_task_process_tree(task)
            try:
                msg = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                if proc is None or not proc.is_alive():
                    break
                continue
            except (EOFError, OSError, ValueError):
                logger.warning(
                    "论文预处理 worker 队列异常或已关闭",
                    extra={"request_id": task.get("request_id", ""),
                           "task_id": task_id, "document_id": task.get("document_id", "")},
                    exc_info=True,
                )
                break
            kind = msg[0]
            # Cancellation wins over queued worker messages, including a late
            # successful completion that had not yet been consumed.
            if task.get("cancel"):
                break
            if kind == "stage":
                task["stage"] = msg[1]
            elif kind == "percent":
                task["percent"] = msg[1]
            elif kind == "result":
                task["result"] = msg[1]
            elif kind == "done":
                task["success"] = bool(msg[1].get("success", False))
                task["percent"] = 100.0
                task["stage"] = "完成"
                task["done"] = True
                logger.info(
                    "论文预处理任务完成 success=%s", task["success"],
                    extra={"request_id": task.get("request_id", ""),
                           "task_id": task_id, "document_id": task.get("document_id", "")},
                )
                return
            elif kind == "error":
                task["error"] = msg[1]
                task["stage"] = "失败"
                task["done"] = True
                logger.error(
                    "论文预处理任务失败 error=%s", str(msg[1])[:300],
                    extra={"request_id": task.get("request_id", ""),
                           "task_id": task_id, "document_id": task.get("document_id", "")},
                )
                return
    finally:
        if proc is not None:
            try:
                await asyncio.to_thread(proc.join, 2)
            except Exception:
                pass
        if task.get("cancel"):
            task["cancelled"] = True
            task["success"] = False
            task["result"] = {}
            task["stage"] = "已取消"
            task["done"] = True
        elif not task.get("done"):
            task["error"] = task.get("error") or "论文预处理进程意外退出"
            task["stage"] = "失败"
            task["done"] = True
            logger.error(
                "论文预处理 worker 意外退出 exitcode=%s",
                proc.exitcode if proc is not None else None,
                extra={"request_id": task.get("request_id", ""),
                       "task_id": task_id, "document_id": task.get("document_id", "")},
            )
        try:
            os.remove(task["_pdf_path"])
        except OSError:
            pass
        if task.get("cancel"):
            try:
                await asyncio.to_thread(delete_document_dir, task.get("document_id", ""))
            except OSError:
                logger.warning(
                    "取消解析后清理临时文档失败",
                    extra={"request_id": task.get("request_id", ""),
                           "task_id": task_id, "document_id": task.get("document_id", "")},
                    exc_info=True,
                )
        _close_task_queue(task)


async def _acquire_conversion_slot_or_cancel(task: dict) -> bool:
    """Wait in the parent process without leaking a slot when cancellation wins."""
    slots = _get_async_conversion_slots()
    cancel_event = task["cancel_event"]
    acquire_task = asyncio.create_task(slots.acquire())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if acquire_task in done:
            if cancel_event.is_set() or task.get("cancel"):
                slots.release()
                return False
            return True
        acquire_task.cancel()
        try:
            await acquire_task
        except asyncio.CancelledError:
            pass
        else:
            # The slot became available in the tiny window between wait() and
            # cancel(); return that successfully acquired permit immediately.
            slots.release()
        return False
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass


async def _run_paper_prepare_task(task_id: str) -> None:
    """Own the preparation slot in the parent and supervise the worker lifecycle."""
    task = PAPER_PREP_TASKS.get(task_id)
    if task is None:
        return
    slot_acquired = False
    process_started = False
    try:
        slot_acquired = await _acquire_conversion_slot_or_cancel(task)
        if not slot_acquired or task.get("cancel"):
            task.update({
                "cancelled": True, "done": True, "success": False,
                "stage": "已取消", "result": {},
            })
            return

        task["stage"] = "正在启动解析进程"
        starter = asyncio.create_task(asyncio.to_thread(task["proc"].start))
        task["start_future"] = starter
        await asyncio.shield(starter)
        process_started = True
        task["process_started"] = True
        _record_task_process_identity(task)

        # Cancellation can arrive while Windows spawn() is in progress. Both
        # paths share the same termination lock, so the tree is killed once.
        if task.get("cancel"):
            await _terminate_task_process(task)
            _close_task_queue(task)
        await _consume_paper_prep_task(task_id)
    except Exception as exc:
        logger.exception(
            "论文预处理进程启动或监管失败",
            extra={"request_id": task.get("request_id", ""),
                   "task_id": task_id, "document_id": task.get("document_id", "")},
        )
        if process_started and _process_is_alive(task.get("proc")):
            await _terminate_task_process(task)
        task.update({
            "error": str(exc), "stage": "失败", "done": True, "success": False,
        })
    finally:
        if slot_acquired:
            _get_async_conversion_slots().release()
        if not process_started:
            pdf_path = str(task.get("_pdf_path") or "")
            if pdf_path:
                try:
                    Path(pdf_path).unlink(missing_ok=True)
                except OSError:
                    pass
            if task.get("cancel"):
                try:
                    await asyncio.to_thread(delete_document_dir, task.get("document_id", ""))
                except (OSError, ValueError):
                    logger.warning(
                        "取消排队解析后清理临时文档失败",
                        extra={"task_id": task_id,
                               "document_id": task.get("document_id", "")},
                        exc_info=True,
                    )
            _close_task_queue(task)


@app.post("/api/prepare_paper")
async def prepare_paper(
    request: Request,
    file: UploadFile = File(..., description="需要加入材料提取工作区的原始 PDF"),
    markdown: str = Form(""),
    markdown_file: UploadFile | None = File(
        None,
        description="可选：首次转换得到的 Markdown 文件；优先于 markdown 表单字段",
    ),
    marker_config: str = Form(""),
    prepare_task_id: str = Form(""),
    client_document_id: str = Form(""),
):
    """异步构建共享论文记录；Markdown 与 JSON 强制来自同一 Marker Document。"""
    if not (file.filename or "").lower().endswith(".pdf"):
        return JSONResponse({"error": "仅支持 PDF 文件"}, status_code=400)
    _prune_paper_prep_tasks()
    task_id = str(prepare_task_id or "").strip() or uuid.uuid4().hex
    client_document_id = str(client_document_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", task_id):
        return JSONResponse({"error": "非法解析任务 ID"}, status_code=400)
    if client_document_id and not re.fullmatch(r"[A-Za-z0-9_-]{3,160}", client_document_id):
        return JSONResponse({"error": "非法工作区文档 ID"}, status_code=400)
    if task_id in PAPER_PREP_TASKS:
        return JSONResponse({"error": "解析任务 ID 已存在"}, status_code=409)
    if task_id in CANCELLED_PAPER_PREP_IDS or client_document_id in DELETED_WORKSPACE_DOCUMENTS:
        return JSONResponse({"error": "PDF 解析已取消", "cancelled": True}, status_code=409)

    # A retry is a genuinely new run. The old process tree must be gone before
    # a replacement task can allocate a new staging directory.
    previous_task_ids = [
        previous_id for previous_id, previous in PAPER_PREP_TASKS.items()
        if client_document_id
        and previous_id != task_id
        and previous.get("client_document_id") == client_document_id
    ]
    for previous_id in previous_task_ids:
        previous = PAPER_PREP_TASKS.get(previous_id)
        if previous and not previous.get("done"):
            cancelled = await _cancel_paper_prepare_task(previous_id)
            if not cancelled.get("ok"):
                return JSONResponse(cancelled, status_code=500)
        previous_document_id = str((previous or {}).get("document_id") or "")
        if previous_document_id:
            await asyncio.to_thread(delete_document_dir, previous_document_id)
        PAPER_PREP_TASKS.pop(previous_id, None)

    if markdown_file is not None:
        try:
            markdown = (await markdown_file.read()).decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse({"error": "Markdown 文件必须使用 UTF-8 编码"}, status_code=400)
    pdf_content = await file.read()
    if task_id in CANCELLED_PAPER_PREP_IDS or client_document_id in DELETED_WORKSPACE_DOCUMENTS:
        return JSONResponse({"error": "PDF 解析已取消", "cancelled": True}, status_code=409)

    document_id, doc_dir_path = create_document_dir(task_id)
    source_pdf = doc_dir_path / "source.pdf"
    source_pdf.write_bytes(pdf_content)
    ctx = _mp()
    q = ctx.Queue()
    proc = ctx.Process(
        target=_paper_prepare_worker,
        args=(
            str(source_pdf), file.filename, task_id, document_id, str(doc_dir_path),
            _load_marker_config(marker_config or None), q, None,
            get_log_queue(), getattr(request.state, "request_id", ""),
        ),
        daemon=True,
    )
    task = {
        "task_id": task_id,
        "created": time.time(), "percent": 0.0, "stage": "排队中", "done": False,
        "success": False, "error": "", "result": {}, "proc": proc, "q": q,
        "document_id": document_id, "_pdf_path": str(source_pdf),
        "client_document_id": client_document_id,
        "request_id": getattr(request.state, "request_id", ""),
        "cancel": False, "cancel_event": asyncio.Event(),
        "process_started": False,
    }
    PAPER_PREP_TASKS[task_id] = task
    logger.info(
        "提交论文预处理任务",
        extra={"request_id": task["request_id"],
               "task_id": task_id, "document_id": document_id},
    )
    consumer = asyncio.create_task(_run_paper_prepare_task(task_id))
    task["consumer"] = consumer
    return JSONResponse({"task_id": task_id, "document_id": document_id})


@app.get("/api/paper_prepare_progress/{task_id}")
async def paper_prepare_progress(task_id: str):
    task = PAPER_PREP_TASKS.get(task_id)
    if task is None:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    return JSONResponse({
        "percent": round(task["percent"], 1), "stage": task["stage"], "done": task["done"],
        "success": task["success"], "error": task["error"],
        "cancelling": bool(task.get("cancel") and not task.get("done")),
        "cancelled": bool(task.get("cancelled", False)),
        "result": task["result"] if task["done"] else {},
    })


@app.post("/api/stop_paper_prepare/{task_id}")
async def stop_paper_prepare(task_id: str):
    """立即停止 PDF 预处理；未知 ID 也会登记，覆盖上传中的竞态。"""
    task = PAPER_PREP_TASKS.get(task_id)
    logger.info(
        "请求取消论文预处理",
        extra={"request_id": (task or {}).get("request_id", ""), "task_id": task_id,
               "document_id": (task or {}).get("document_id", "")},
    )
    result = await _cancel_paper_prepare_task(task_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 500)



# ============ 材料分析 API：模块一 part1=摘要与结论总结(summarize)；模块二+三 part2/part3=材料提取+全文结论，自适应合并为一次全文调用（超长自动拆分） ============
import sys as _sys
_sys.path.insert(0, str(REPO_ROOT))  # 兼容 worker 子进程的包导入

from backend.preprocess.clean import preprocess  # noqa: E402
from backend.markdown_utils import escape_approximate_tildes  # noqa: E402
from backend.preprocess.images import (  # noqa: E402
    figure_index_to_image_items,
)
from backend.vision import analyze_images as run_vision_analysis  # noqa: E402
from backend.summarize import summarize_from_clean  # noqa: E402
# 材料提取模块：与摘要模块（summarize）完全独立，互不 import，仅共用 preprocess / ai_client
from backend.extract import (  # noqa: E402
    extract_from_clean_markdown,
    normalize_list_indentation,
    inject_figure_images,
    _strip_code_fence as _strip_fence,
    _downgrade_headings as _downgrade,
    EXTRACT_TEMPERATURE,
    _MAX_TOKENS_BY_CATEGORY as _EXT_TOKENS,
)
# 全文综合结论模块：与摘要/提取独立，复用 master 提示词（mode=conclusion）
from backend.conclusion import (  # noqa: E402
    conclude_from_clean_markdown,
    CONCLUDE_TEMPERATURE,
    _MAX_TOKENS_BY_CATEGORY as _CONCL_TOKENS,
)
# 提示词（master + 自适应合并入口）：extract+conclusion 合并为一次全文调用
from backend.prompts import build_extract_conclusion_calls  # noqa: E402
# AI 调用基础设施
from backend.ai_client import get_model_config, create_client, chat, list_providers, get_vision_config  # noqa: E402

MATERIAL_TASKS: dict = {}               # 材料提取任务表：task_id -> 进度/结果
PAPER_PREP_TASKS: dict = {}             # PDF -> Markdown + FigureIndex 预处理任务表
CANCELLED_PAPER_PREP_IDS: dict[str, float] = {}  # 覆盖“上传中、任务尚未登记”的取消竞态
DELETED_WORKSPACE_DOCUMENTS: dict[str, float] = {}  # 阻止已删除文档的晚到提取请求

_MATERIAL_PARTS = ("part1", "part2", "part3")


def _normalize_material_parts(value) -> list[str]:
    """规范化前端选择的提取部分；未显式选择时不执行任何模块。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    selected = []
    for part in value or []:
        part = str(part or "").strip()
        if part in _MATERIAL_PARTS and part not in selected:
            selected.append(part)
    return selected


def _new_material_part(status: str = "not_selected") -> dict:
    return {
        "status": status,
        "content": "",
        "error": "",
        "model": "",
        "model_label": "",
        "elapsed": 0,
        "generated_at": "",
        "run_id": "",
    }


def _new_material_paper(document_id: str, name: str, index: int, selected_parts, run_id: str) -> dict:
    selected = set(selected_parts or [])
    return {
        "document_id": document_id or f"paper-{index + 1}",
        "index": index,
        "name": name or f"论文{index + 1}",
        "status": "queued",
        "part1": "",
        "part2": "",
        "part3": "",
        "parts": {
            part: _new_material_part("queued" if part in selected else "not_selected")
            for part in _MATERIAL_PARTS
        },
        "merged": False,
        "model": "",
        "model_label": "",
        "vision": {"used": False, "analyzed": 0, "figures": []},
        "elapsed": 0,
        "generated_at": "",
        "run_id": run_id,
    }


def _refresh_material_paper_status(paper: dict, selected_parts) -> str:
    statuses = [paper["parts"][part]["status"] for part in selected_parts]
    if not statuses:
        paper["status"] = "not_selected"
    elif any(status == "running" for status in statuses):
        paper["status"] = "running"
    elif any(status == "queued" for status in statuses):
        paper["status"] = "queued"
    elif all(status == "completed" for status in statuses):
        paper["status"] = "completed"
    elif any(status == "completed" for status in statuses):
        paper["status"] = "partial"
    elif any(status == "cancelled" for status in statuses):
        paper["status"] = "cancelled"
    else:
        paper["status"] = "failed"
    return paper["status"]


def _set_material_part(paper: dict, part: str, *, status: str, content: str = "",
                       error: str = "", provider: str = "", model_label: str = "",
                       elapsed: int = 0, generated_at: str = "", run_id: str = ""):
    # 所有结果入口统一在写入状态前做最终清理。模型即使使用了 mode=extract，
    # 也可能在材料条目后继续输出“全文综合结论/未来研究建议”；这里是独立
    # 调用、合并调用回退和其它调用方共同经过的最后一道边界保护。
    if status == "completed":
        if part == "part2":
            # Module callers already normalize heading depth before publishing;
            # the final write-path cleanup must not downgrade it a second time.
            content = _normalize_material_output(content, downgrade_headings=False)
        elif part == "part3":
            content = _normalize_conclusion_output(content)
    state = paper["parts"][part]
    state.update({
        "status": status,
        "content": content or "",
        "error": error or "",
        "model": provider or "",
        "model_label": model_label or provider or "",
        "elapsed": int(elapsed or 0),
        "generated_at": generated_at or "",
        "run_id": run_id or paper.get("run_id", ""),
    })
    paper[part] = content or ""



# ============ 模块二+三 自适应合并：材料提取 + 全文结论（全文只读一次）============

# 合并输出拆分锚点：模型可能使用不同标题层级、标点或直接输出“未来研究建议”。
# 只把整行 Markdown 标题视为边界，避免正文中提到这些词时误截断。
_CONCL_ANCHOR = re.compile(
    r"^#{1,3}\s*(?:"
    r"一[、.．]?\s*全文综合结论(?:与未来研究建议)?|"
    r"二[、.．]?\s*未来(?:研究)?建议|"
    r"三[、.．]?\s*综合结论与未来建议|"
    r"全文综合结论(?:与未来研究建议)?|"
    r"未来(?:研究)?建议|"
    r"综合结论与未来建议"
    r")\s*$",
    re.MULTILINE,
)
_HORIZONTAL_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def _trim_section_boundary_noise(markdown: str) -> str:
    """Remove separators accidentally left immediately around split sections."""
    lines = (markdown or "").strip().splitlines()
    while lines and _HORIZONTAL_RULE.match(lines[0]):
        lines.pop(0)
    while lines and _HORIZONTAL_RULE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


def _normalize_material_output(markdown: str, *, downgrade_headings: bool = True) -> str:
    cleaned = _trim_section_boundary_noise(markdown)
    leak = _CONCL_ANCHOR.search(cleaned)
    if leak:
        cleaned = _trim_section_boundary_noise(cleaned[:leak.start()])
    if downgrade_headings:
        cleaned = _downgrade(cleaned)
    return normalize_list_indentation(escape_approximate_tildes(cleaned))


def _normalize_conclusion_output(markdown: str) -> str:
    return escape_approximate_tildes(_trim_section_boundary_noise(_strip_fence(markdown)))


def _split_extract_conclusion(combined_md: str) -> tuple[str, str]:
    """把合并调用的输出拆成 (part2 提取, part3 结论)。

    锚点为结论部分的一级标题「# 一、全文综合结论」。找不到锚点（模型漏产出结论）
    时 part3 置空，交由调用方回退到两次独立调用，保证两部分都不丢失。
    """
    m = _CONCL_ANCHOR.search(combined_md or "")
    if not m:
        return "", ""
    part2 = _trim_section_boundary_noise(combined_md[:m.start()])
    part3 = _trim_section_boundary_noise(combined_md[m.start():])
    return part2, part3


def _run_extract_conclusion(clean, title: str, verify: bool = False, provider: str = None,
                            image_summary: str = None):
    """自适应合并执行模块二(extract)+模块三(conclusion)。

    返回 (part2_md, part3_md, merged: bool)：
      - 普通论文（clean_text 未超阈值）：合并为一次全文调用（全文只读一次），
        输出切开为 part2/part3；若合并输出缺任一部分，自动回退两次独立调用。
      - 超长论文：自动拆回两次独立调用（复用 extract_from_clean_markdown /
        conclude_from_clean_markdown，verify 仅在拆分模式生效）。
    provider：选中的模型（None 取 default_provider），三段统一使用该模型。
    image_summary：图片视觉分析结果（视觉专家模型预先生成），注入提示词供交叉核对；
        纯文本论文为 None，不注入。
    """
    cfg = get_model_config(provider)
    client = create_client(cfg)
    model = cfg["model"]
    max_out = int(cfg.get("max_output", 8192))
    calls, merged = build_extract_conclusion_calls(clean, title, image_summary=image_summary)
    if merged:
        cat = getattr(clean, "length_category", "short")
        # 合并输出 ≈ 提取 + 结论两部分之和，按两者上限之和放宽；上限跟随所选模型
        max_tokens = min(
            int((_EXT_TOKENS.get(cat, 24000) + _CONCL_TOKENS.get(cat, 12000)) * 1.3),
            max_out,
        )
        combined = chat(
            client, model, calls[0],
            temperature=EXTRACT_TEMPERATURE,
            max_tokens=max_tokens,
            response_json=False,
        )
        combined = _strip_fence(combined)
        part2, part3 = _split_extract_conclusion(combined)
        part2 = _normalize_material_output(part2)
        part3 = _normalize_conclusion_output(part3)
        # 合并输出不完整（缺任一部分）→ 回退两次独立调用，保证两部分都产出
        if not part2.strip() or not part3.strip():
            merged = False
            part2 = _normalize_material_output(extract_from_clean_markdown(
                clean, title=title, verify=verify, provider=provider, image_summary=image_summary,
            ))
            part3 = _normalize_conclusion_output(conclude_from_clean_markdown(
                clean, title=title, provider=provider, image_summary=image_summary,
            ))
        return part2, part3, merged
    # 拆分模式：复用既有独立函数（含 verify 查漏）
    part2 = _normalize_material_output(extract_from_clean_markdown(
        clean, title=title, verify=verify, provider=provider, image_summary=image_summary,
    ))
    part3 = _normalize_conclusion_output(conclude_from_clean_markdown(
        clean, title=title, provider=provider, image_summary=image_summary,
    ))
    return part2, part3, False



def _run_extract_conclusion_combined_only(clean, title: str, provider: str = None,
                                          image_summary: str = None):
    """仅尝试一次 part2+part3 合并调用；不在内部回退，便于调用方分别记录模块失败。"""
    cfg = get_model_config(provider)
    calls, merged = build_extract_conclusion_calls(clean, title, image_summary=image_summary)
    if not merged:
        return None
    client = create_client(cfg)
    category = getattr(clean, "length_category", "short")
    max_tokens = min(
        int((_EXT_TOKENS.get(category, 24000) + _CONCL_TOKENS.get(category, 12000)) * 1.3),
        int(cfg.get("max_output", 8192)),
    )
    combined = chat(
        client, cfg["model"], calls[0], temperature=EXTRACT_TEMPERATURE,
        max_tokens=max_tokens, response_json=False,
    )
    combined = _strip_fence(combined)
    part2, part3 = _split_extract_conclusion(combined)
    if not part2.strip() or not part3.strip():
        raise ValueError("合并输出缺少材料信息或综合结论部分")
    part2 = _normalize_material_output(part2)
    part3 = _normalize_conclusion_output(part3)
    return part2, part3

def _prune_material_tasks():
    """清理 1 小时前已完成的任务，防止 MATERIAL_TASKS 无限增长。"""
    now = time.time()
    stale = [tid for tid, t in MATERIAL_TASKS.items()
             if t.get("done") and now - t.get("created", now) > 3600]
    for tid in stale:
        MATERIAL_TASKS.pop(tid, None)
    _prune_workspace_tombstones(now)


async def _cancel_material_task(task_id: str) -> dict:
    task = MATERIAL_TASKS.get(task_id)
    if task is None:
        return {
            "ok": True, "already_removed": True,
            "terminated_pids": [], "remaining_pids": [],
        }
    if task.get("done"):
        termination = await _terminate_task_process(task)
        if termination.get("remaining_pids") or termination.get("process_alive"):
            return {
                "ok": False, "cancelled": False,
                "error": "已结束的材料任务仍有残留进程",
                "terminated_pids": termination.get("terminated_pids", []),
                "remaining_pids": termination.get("remaining_pids", []),
            }
        _close_task_queue(task)
        await _wait_for_task_finalization(task)
        return {
            "ok": True, "already_done": True,
            "cancelled": bool(task.get("cancelled")),
            "terminated_pids": termination.get("terminated_pids", []),
            "remaining_pids": termination.get("remaining_pids", []),
        }
    task["cancel"] = True
    task["stage"] = "正在取消…"
    termination = await _terminate_task_process(task)
    if termination["remaining_pids"] or termination["process_alive"]:
        task["stage"] = "进程终止失败"
        return {
            "ok": False, "cancelled": False,
            "error": "材料提取进程未能完全退出",
            "terminated_pids": termination["terminated_pids"],
            "remaining_pids": termination["remaining_pids"],
        }
    _close_task_queue(task)
    await _wait_for_task_finalization(task)
    if not task.get("done"):
        task.update({
            "cancelled": True, "status": "cancelled", "stage": "已取消",
            "done": True, "success": False,
        })
    return {
        "ok": True, "stopped": True, "cancelled": True,
        "terminated_pids": termination["terminated_pids"],
        "remaining_pids": [],
    }


def _format_part1(name: str, md: str) -> str:
    """把单篇摘要/结论总结的 Markdown 直出结果包成小节，供网页 part1 渲染。"""
    return f"### {name}\n\n{md}"


def _merge_result(papers, part1_sections, part2_sections, part3_sections) -> dict:
    """由已回传的单篇结果拼出整份报告（正常完成 / 取消 / 部分失败共用）。"""
    return {
        "papers": papers,
        "part1": "\n\n---\n\n".join(part1_sections) if part1_sections else "（无内容）",
        "part2": "\n\n---\n\n".join(part2_sections) if part2_sections else "（未提取到材料信息）",
        "part3": "\n\n---\n\n".join(part3_sections) if part3_sections else "（未生成结论）",
    }


def _material_task_worker(contents, names, document_ids, figure_indexes, selected_parts,
                          verify, provider, run_id, q, log_queue=None,
                          request_id="", task_id=""):
    """子进程入口：按文件和用户选择的模块执行，逐模块回传结构化状态。"""
    configure_worker_logging(log_queue)
    worker_token = bind_log_context(
        request_id=request_id, task_id=task_id, provider=provider or "",
    )
    worker_logger = logging.getLogger("paper.worker.material")
    worker_logger.info(
        "材料提取 worker 启动 documents=%d parts=%s",
        len(contents), ",".join(selected_parts),
    )
    state = {"p": 0.0, "cap": 0.0}
    stop_evt = threading.Event()

    def _creep():
        while not stop_evt.wait(0.5):
            if state["cap"] > state["p"]:
                state["p"] = min(
                    state["cap"],
                    state["p"] + max(0.1, (state["cap"] - state["p"]) * 0.04),
                )
                q.put(("percent", round(state["p"], 1)))

    t = threading.Thread(target=_creep, daemon=True)
    t.start()
    try:
        asyncio.run(_material_worker_async(
            contents, names, document_ids, figure_indexes, selected_parts,
            verify, provider, run_id, q, state,
        ))
    except Exception as e:
        worker_logger.exception("材料提取 worker 异常退出")
        try:
            q.put(("error", str(e)))
        except Exception:
            pass
    finally:
        stop_evt.set()
        try:
            t.join(timeout=1)
        except Exception:
            pass
        reset_log_context(worker_token)


async def _material_worker_async(contents, names, document_ids, figure_indexes, selected_parts,
                                 verify, provider, run_id, q, state):
    """逐篇执行用户选择的 part1/part2/part3，并独立记录模块状态、模型和耗时。"""
    worker_logger = logging.getLogger("paper.worker.material")
    cfg = get_model_config(provider)
    eff_provider = cfg["provider"]
    model_label = cfg.get("label", eff_provider)
    selected_parts = _normalize_material_parts(selected_parts)
    selected_set = set(selected_parts)
    total = max(1, len(contents))

    def emit(index: int, paper: dict):
        _refresh_material_paper_status(paper, selected_parts)
        q.put(("paper_update", index, copy.deepcopy(paper)))

    for i, (content, name, document_id, figure_index) in enumerate(
        zip(contents, names, document_ids, figure_indexes)
    ):
        paper_start = time.time()
        name = (name or f"论文{i + 1}").strip()
        document_id = str(document_id or f"paper-{i + 1}")
        figure_index = figure_index or []
        paper = _new_material_paper(document_id, name, i, selected_parts, run_id)
        paper["model"] = eff_provider
        paper["model_label"] = model_label
        paper["vision"] = {
            "used": False,
            "analyzed": 0,
            "figures": [item.get("label") or item.get("id") for item in figure_index],
        }
        emit(i, paper)

        width = 100 / total
        seg_start = i * width
        seg_end = (i + 1) * width
        unit_count = 1 + len(selected_parts)
        unit_width = width / max(1, unit_count)
        completed_units = 0

        need_visual = bool(selected_set.intersection({"part2", "part3"}))
        vimg = []
        vision_task = None
        if need_visual:
            vimg = figure_index_to_image_items(
                figure_index, document_id=document_id,
            )[:get_vision_config()["max_images_per_paper"]]
            vision_task = asyncio.create_task(run_vision_analysis(vimg)) if vimg else None
            paper["vision"]["used"] = bool(vimg)
            paper["vision"]["analyzed"] = len(vimg)

        q.put(("stage", f"预处理：{name}（{i + 1}/{total}）"))
        state["p"] = seg_start
        state["cap"] = seg_start + unit_width
        try:
            clean = preprocess(content)
            completed_units += 1
            state["p"] = seg_start + unit_width * completed_units
            q.put(("percent", state["p"]))
        except Exception as exc:
            worker_logger.exception(
                "材料提取预处理失败 document_id=%s", document_id,
                extra={"document_id": document_id},
            )
            generated = time.strftime("%Y-%m-%d %H:%M:%S")
            for part in selected_parts:
                _set_material_part(
                    paper, part, status="failed", error=f"预处理失败：{exc}",
                    provider=eff_provider, model_label=model_label,
                    generated_at=generated, run_id=run_id,
                )
            paper["elapsed"] = round(time.time() - paper_start)
            paper["generated_at"] = generated
            emit(i, paper)
            state["p"] = state["cap"] = seg_end
            q.put(("percent", seg_end))
            continue

        if "part1" in selected_set:
            q.put(("stage", f"摘要与研究结论：{name}（{i + 1}/{total}）"))
            _set_material_part(
                paper, "part1", status="running", provider=eff_provider,
                model_label=model_label, run_id=run_id,
            )
            emit(i, paper)
            state["cap"] = seg_start + unit_width * (completed_units + 1)
            part_start = time.time()
            try:
                summary = await asyncio.to_thread(summarize_from_clean, clean, name, provider)
                content_part1 = _format_part1(name, summary)
                elapsed = round(time.time() - part_start)
                generated = time.strftime("%Y-%m-%d %H:%M:%S")
                _set_material_part(
                    paper, "part1", status="completed", content=content_part1,
                    provider=eff_provider, model_label=model_label,
                    elapsed=elapsed, generated_at=generated, run_id=run_id,
                )
            except Exception as exc:
                worker_logger.exception(
                    "材料提取 part1 失败 document_id=%s", document_id,
                    extra={"document_id": document_id, "part": "part1", "provider": provider or ""},
                )
                _set_material_part(
                    paper, "part1", status="failed", error=str(exc),
                    provider=eff_provider, model_label=model_label,
                    elapsed=round(time.time() - part_start),
                    generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                )
            completed_units += 1
            state["p"] = seg_start + unit_width * completed_units
            q.put(("percent", state["p"]))
            emit(i, paper)

        image_summary = ""
        if need_visual:
            try:
                image_summary = await vision_task if vision_task else ""
            except Exception:
                worker_logger.exception(
                    "视觉分析任务失败 document_id=%s",
                    document_id,
                    extra={"document_id": document_id, "provider": provider or ""},
                )
                image_summary = ""
        image_summary = image_summary or ""
        # 图片与报告正文解耦：正文只保留图号，浏览器/VLM 通过 FigureIndex 取图。
        # 这样归档报告不会固化临时 URL，更不会写入 Base64。
        figure_map = {}

        wants_part2 = "part2" in selected_set
        wants_part3 = "part3" in selected_set
        if wants_part2 and wants_part3:
            q.put(("stage", f"材料信息与综合结论：{name}（{i + 1}/{total}）"))
            for part in ("part2", "part3"):
                _set_material_part(
                    paper, part, status="running", provider=eff_provider,
                    model_label=model_label, run_id=run_id,
                )
            emit(i, paper)
            state["cap"] = seg_start + unit_width * (completed_units + 2)
            combined_start = time.time()
            try:
                combined_parts = _run_extract_conclusion_combined_only(
                    clean, name, provider, image_summary,
                )
                if combined_parts is None:
                    raise ValueError("当前论文按长度策略使用独立模块调用")
                part2, part3 = combined_parts
                merged = True
                part2 = inject_figure_images(part2, figure_map)
                elapsed = round(time.time() - combined_start)
                generated = time.strftime("%Y-%m-%d %H:%M:%S")
                _set_material_part(
                    paper, "part2", status="completed", content=part2,
                    provider=eff_provider, model_label=model_label,
                    elapsed=elapsed, generated_at=generated, run_id=run_id,
                )
                _set_material_part(
                    paper, "part3", status="completed", content=f"### {name}\n\n{part3}",
                    provider=eff_provider, model_label=model_label,
                    elapsed=elapsed, generated_at=generated, run_id=run_id,
                )
                paper["merged"] = merged
            except Exception:
                worker_logger.exception(
                    "材料提取 part2+part3 合并调用失败 document_id=%s",
                    document_id,
                    extra={"document_id": document_id, "part": "part2+part3", "provider": provider or ""},
                )
                # 合并调用失败时分别重试两个模块，保证其中一个失败不会拖累另一个。
                p2_start = time.time()
                try:
                    part2 = extract_from_clean_markdown(
                        clean, title=name, verify=verify, provider=provider,
                        image_summary=image_summary,
                    )
                    part2 = _normalize_material_output(part2)
                    part2 = inject_figure_images(part2, figure_map)
                    _set_material_part(
                        paper, "part2", status="completed", content=part2,
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - p2_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
                except Exception as exc:
                    worker_logger.exception(
                        "材料提取 part2 回退调用失败 document_id=%s", document_id,
                        extra={"document_id": document_id, "part": "part2", "provider": provider or ""},
                    )
                    _set_material_part(
                        paper, "part2", status="failed", error=str(exc),
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - p2_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
                p3_start = time.time()
                try:
                    part3 = conclude_from_clean_markdown(
                        clean, title=name, provider=provider, image_summary=image_summary,
                    )
                    _set_material_part(
                        paper, "part3", status="completed", content=f"### {name}\n\n{part3}",
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - p3_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
                except Exception as exc:
                    worker_logger.exception(
                        "材料提取 part3 回退调用失败 document_id=%s", document_id,
                        extra={"document_id": document_id, "part": "part3", "provider": provider or ""},
                    )
                    _set_material_part(
                        paper, "part3", status="failed", error=str(exc),
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - p3_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
            completed_units += 2
            state["p"] = seg_start + unit_width * completed_units
            q.put(("percent", state["p"]))
            emit(i, paper)
        else:
            if wants_part2:
                q.put(("stage", f"材料与性能信息：{name}（{i + 1}/{total}）"))
                _set_material_part(
                    paper, "part2", status="running", provider=eff_provider,
                    model_label=model_label, run_id=run_id,
                )
                emit(i, paper)
                state["cap"] = seg_start + unit_width * (completed_units + 1)
                part_start = time.time()
                try:
                    part2 = extract_from_clean_markdown(
                        clean, title=name, verify=verify, provider=provider,
                        image_summary=image_summary,
                    )
                    part2 = _normalize_material_output(part2)
                    part2 = inject_figure_images(part2, figure_map)
                    _set_material_part(
                        paper, "part2", status="completed", content=part2,
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - part_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
                except Exception as exc:
                    worker_logger.exception(
                        "材料提取 part2 失败 document_id=%s", document_id,
                        extra={"document_id": document_id, "part": "part2", "provider": provider or ""},
                    )
                    _set_material_part(
                        paper, "part2", status="failed", error=str(exc),
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - part_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
                completed_units += 1
                state["p"] = seg_start + unit_width * completed_units
                q.put(("percent", state["p"]))
                emit(i, paper)

            if wants_part3:
                q.put(("stage", f"综合结论与未来建议：{name}（{i + 1}/{total}）"))
                _set_material_part(
                    paper, "part3", status="running", provider=eff_provider,
                    model_label=model_label, run_id=run_id,
                )
                emit(i, paper)
                state["cap"] = seg_start + unit_width * (completed_units + 1)
                part_start = time.time()
                try:
                    part3 = conclude_from_clean_markdown(
                        clean, title=name, provider=provider, image_summary=image_summary,
                    )
                    _set_material_part(
                        paper, "part3", status="completed", content=f"### {name}\n\n{part3}",
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - part_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
                except Exception as exc:
                    worker_logger.exception(
                        "材料提取 part3 失败 document_id=%s", document_id,
                        extra={"document_id": document_id, "part": "part3", "provider": provider or ""},
                    )
                    _set_material_part(
                        paper, "part3", status="failed", error=str(exc),
                        provider=eff_provider, model_label=model_label,
                        elapsed=round(time.time() - part_start),
                        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), run_id=run_id,
                    )
                completed_units += 1
                state["p"] = seg_start + unit_width * completed_units
                q.put(("percent", state["p"]))
                emit(i, paper)

        paper["elapsed"] = round(time.time() - paper_start)
        paper["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        emit(i, paper)
        state["p"] = state["cap"] = seg_end
        q.put(("percent", seg_end))

    q.put(("done", {}))


def _material_result_from_papers(papers: list[dict]) -> dict:
    part1s = [p.get("part1", "") for p in papers if p.get("part1")]
    part2s = [p.get("part2", "") for p in papers if p.get("part2")]
    part3s = [p.get("part3", "") for p in papers if p.get("part3")]
    return _merge_result(papers, part1s, part2s, part3s)


def _material_run_status(papers: list[dict], selected_parts) -> str:
    statuses = [
        paper.get("parts", {}).get(part, {}).get("status", "queued")
        for paper in papers for part in selected_parts
    ]
    if not statuses or all(status == "queued" for status in statuses):
        return "queued"
    if any(status == "running" for status in statuses) or any(status == "queued" for status in statuses):
        return "running"
    if all(status == "completed" for status in statuses):
        return "completed"
    if any(status == "completed" for status in statuses):
        return "partial"
    if any(status == "cancelled" for status in statuses):
        return "cancelled"
    return "failed"


async def _consume_material_task(task_id: str):
    """消费子进程的逐模块状态，并在任务结束/取消时保留所有已完成内容。"""
    task = MATERIAL_TASKS[task_id]
    proc = task.get("proc")
    q = task.get("q")
    selected_parts = task.get("parts") or list(_MATERIAL_PARTS)
    paper_by_index = {
        int(paper.get("index", index)): copy.deepcopy(paper)
        for index, paper in enumerate(task.get("seed_papers") or [])
    }

    def refresh_result():
        papers = [paper_by_index[index] for index in sorted(paper_by_index)]
        task["result"] = _material_result_from_papers(papers)
        task["status"] = _material_run_status(papers, selected_parts)
        return papers

    refresh_result()
    try:
        while True:
            _snapshot_task_process_tree(task)
            try:
                msg = await asyncio.to_thread(q.get, True, 0.5)
            except queue.Empty:
                if proc is None or not proc.is_alive():
                    break
                continue
            except (EOFError, OSError, ValueError):
                logger.warning(
                    "材料提取 worker 队列异常或已关闭",
                    extra={"request_id": task.get("request_id", ""), "task_id": task_id},
                    exc_info=True,
                )
                break
            kind = msg[0]
            if task.get("cancel"):
                break
            if kind == "stage":
                task["stage"] = msg[1]
            elif kind == "percent":
                task["percent"] = msg[1]
            elif kind == "paper_update":
                paper_by_index[int(msg[1])] = msg[2]
                refresh_result()
            elif kind == "done":
                papers = refresh_result()
                status = _material_run_status(papers, selected_parts)
                task["status"] = status
                task["success"] = status == "completed"
                task["stage"] = {
                    "completed": "完成", "partial": "部分完成",
                    "failed": "失败", "cancelled": "已取消",
                }.get(status, "完成")
                task["percent"] = 100.0
                task["done"] = True
                logger.info(
                    "材料提取任务完成 status=%s", task["status"],
                    extra={"request_id": task.get("request_id", ""), "task_id": task_id},
                )
                return
            elif kind == "error":
                task["error"] = msg[1]
                task["status"] = "failed"
                task["stage"] = "失败"
                task["done"] = True
                logger.error(
                    "材料提取任务失败 error=%s", str(msg[1])[:300],
                    extra={"request_id": task.get("request_id", ""), "task_id": task_id},
                )
                return
    finally:
        if proc is not None:
            try:
                await asyncio.to_thread(proc.join, 2)
            except Exception:
                pass
        if task.get("cancel"):
            for paper in paper_by_index.values():
                for part in selected_parts:
                    state = paper.get("parts", {}).get(part, {})
                    if state.get("status") in {"queued", "running"}:
                        _set_material_part(
                            paper, part, status="cancelled", error="用户已停止提取",
                            provider=state.get("model") or task.get("provider", ""),
                            model_label=state.get("model_label", ""),
                            elapsed=state.get("elapsed", 0),
                            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                            run_id=task.get("run_id", task_id),
                        )
                _refresh_material_paper_status(paper, selected_parts)
            refresh_result()
            task["cancelled"] = True
            task["status"] = "cancelled"
            task["stage"] = "已取消"
            task["done"] = True
            task["success"] = False
        elif not task.get("done"):
            task["error"] = task.get("error") or "提取进程意外退出"
            task["status"] = "failed"
            task["stage"] = "失败"
            task["done"] = True
            task["success"] = False
            logger.error(
                "材料提取 worker 意外退出 exitcode=%s",
                proc.exitcode if proc is not None else None,
                extra={"request_id": task.get("request_id", ""), "task_id": task_id},
            )
        _close_task_queue(task)


@app.get("/api/models")
async def api_models():
    """返回后端已部署的全部可用模型（不含任何密钥），供前端模型选择下拉框使用。"""
    return JSONResponse({"models": list_providers()})


@app.get("/api/temp-assets/{document_id}/{asset_id}")
async def temp_asset(document_id: str, asset_id: str):
    try:
        path, asset = resolve_asset(document_id, asset_id)
    except (FileNotFoundError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return FileResponse(
        path,
        media_type=asset.get("mime_type") or "application/octet-stream",
        filename=Path(path).name,
        content_disposition_type="inline",
    )


@app.get("/api/knowledge")
async def api_knowledge_snapshot():
    return JSONResponse(await asyncio.to_thread(knowledge_snapshot))


@app.post("/api/knowledge/folders")
async def api_create_knowledge_folder(request: Request):
    body = await request.json()
    try:
        folder = await asyncio.to_thread(create_folder, str((body or {}).get("name") or ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(folder, status_code=201)


@app.patch("/api/knowledge/folders/{folder_id}")
async def api_update_knowledge_folder(folder_id: str, request: Request):
    body = await request.json()
    try:
        folder = await asyncio.to_thread(update_folder, folder_id, str((body or {}).get("name") or ""))
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(folder)


@app.delete("/api/knowledge/folders/{folder_id}")
async def api_delete_knowledge_folder(folder_id: str):
    try:
        await asyncio.to_thread(delete_folder, folder_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"ok": True})


@app.post("/api/knowledge/archive")
async def api_archive_knowledge(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    body = body or {}
    document_id = str(body.get("document_id") or "")
    folder_ids = [str(item) for item in (body.get("folder_ids") or []) if str(item)]
    report = body.get("report") if isinstance(body.get("report"), dict) else {}
    if not document_id or not folder_ids or not report:
        return JSONResponse({"error": "归档需要 document_id、folder_ids 和完整报告"}, status_code=400)
    try:
        archived = await asyncio.to_thread(
            archive_document,
            document_id,
            folder_ids,
            report,
            update_existing=bool(body.get("update_existing", False)),
        )
    except ArchiveConflict as exc:
        return JSONResponse({
            "error": "该文章已有归档报告",
            "conflict": True,
            "existing_report_id": exc.report_id,
            "choices": ["update", "cancel"],
        }, status_code=409)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=410)
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "report": archived})


@app.delete("/api/knowledge/folders/{folder_id}/reports/{report_id}")
async def api_remove_knowledge_report(folder_id: str, report_id: str):
    try:
        await asyncio.to_thread(remove_report_from_folder, folder_id, report_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"ok": True})


@app.post("/api/knowledge/folders/{folder_id}/reports/manage")
async def api_manage_knowledge_reports(folder_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    body = body or {}
    report_ids = body.get("report_ids")
    action = str(body.get("action") or "").strip().lower()
    target_folder_id = str(body.get("target_folder_id") or "").strip() or None
    if not isinstance(report_ids, list):
        return JSONResponse({"error": "report_ids 必须是数组"}, status_code=400)
    try:
        result = await asyncio.to_thread(
            manage_reports_in_folder,
            folder_id,
            [str(report_id) for report_id in report_ids],
            action,
            target_folder_id,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **result})


@app.get("/api/knowledge/reports/{report_id}")
async def api_get_knowledge_report(report_id: str):
    try:
        report = await asyncio.to_thread(get_report, report_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(report)


@app.get("/api/knowledge/assets/{asset_id}")
async def api_knowledge_asset(asset_id: str):
    try:
        content, mime_type, extension = await asyncio.to_thread(knowledge_asset, asset_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f"inline; filename={asset_id}.{extension}",
        },
    )


@app.delete("/api/material_documents/{client_document_id}")
async def delete_material_document(client_document_id: str, request: Request):
    """Delete one workspace document and every live task/temporary asset it owns.

    Knowledge-base archives are independent database snapshots and are not
    touched by this workspace-only operation.
    """
    client_document_id = str(client_document_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,160}", client_document_id):
        return JSONResponse({"error": "非法工作区文档 ID"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    prepare_task_id = str(body.get("prepare_task_id") or "").strip()
    server_document_id = str(body.get("server_document_id") or "").strip()

    _prune_paper_prep_tasks()
    _prune_material_tasks()
    DELETED_WORKSPACE_DOCUMENTS[client_document_id] = time.time()

    prep_task_ids = {
        task_id for task_id, task in PAPER_PREP_TASKS.items()
        if task.get("client_document_id") == client_document_id
    }
    if prepare_task_id:
        prep_task_ids.add(prepare_task_id)
    staged_document_ids = {server_document_id} if server_document_id else set()
    for task_id in prep_task_ids:
        task = PAPER_PREP_TASKS.get(task_id)
        if task and task.get("document_id"):
            staged_document_ids.add(str(task["document_id"]))

    material_task_ids = {
        task_id for task_id, task in MATERIAL_TASKS.items()
        if client_document_id in (task.get("document_ids") or [])
    }

    # Stop every owned task concurrently. The tombstone above prevents a late
    # prepare/material request from registering a new worker during this wait.
    cancellation_jobs = [
        (task_id, _cancel_paper_prepare_task(task_id))
        for task_id in sorted(prep_task_ids)
    ] + [
        (task_id, _cancel_material_task(task_id))
        for task_id in sorted(material_task_ids)
    ]
    cancellation_results = await asyncio.gather(
        *(job for _, job in cancellation_jobs), return_exceptions=True,
    ) if cancellation_jobs else []
    terminated_pids: set[int] = set()
    remaining_pids: set[int] = set()
    termination_errors: list[str] = []
    terminated_task_ids: list[str] = []
    for (task_id, _), result in zip(cancellation_jobs, cancellation_results):
        if isinstance(result, BaseException):
            termination_errors.append(f"{task_id}: {result}")
            continue
        terminated_pids.update(int(pid) for pid in result.get("terminated_pids", []))
        remaining_pids.update(int(pid) for pid in result.get("remaining_pids", []))
        if result.get("ok") and not result.get("remaining_pids"):
            terminated_task_ids.append(task_id)
        else:
            termination_errors.append(str(result.get("error") or f"{task_id} 终止失败"))

    if remaining_pids or termination_errors:
        logger.error(
            "删除工作区文档失败：任务进程仍存活 terminated=%s remaining=%s errors=%s",
            sorted(terminated_pids), sorted(remaining_pids), termination_errors,
            extra={"client_document_id": client_document_id,
                   "document_id": server_document_id},
        )
        return JSONResponse({
            "ok": False,
            "deleted": False,
            "error": "任务进程未能完全退出，文档未删除",
            "terminated_task_ids": sorted(terminated_task_ids),
            "terminated_pids": sorted(terminated_pids),
            "remaining_pids": sorted(remaining_pids),
            "termination_errors": termination_errors,
        }, status_code=500)

    removed_directories = 0
    try:
        for document_id in staged_document_ids:
            if document_id and await asyncio.to_thread(delete_document_dir, document_id):
                removed_directories += 1
    except (OSError, ValueError) as exc:
        logger.exception(
            "删除工作区文档临时资产失败",
            extra={"document_id": server_document_id, "client_document_id": client_document_id},
        )
        return JSONResponse({"error": f"临时资产删除失败：{exc}"}, status_code=500)

    for task_id in prep_task_ids:
        PAPER_PREP_TASKS.pop(task_id, None)
    for task_id in material_task_ids:
        MATERIAL_TASKS.pop(task_id, None)
    logger.info(
        "工作区文档已删除 prep_tasks=%d material_tasks=%d staging=%d",
        len(prep_task_ids), len(material_task_ids), removed_directories,
        extra={"client_document_id": client_document_id, "document_id": server_document_id},
    )
    return JSONResponse({
        "ok": True,
        "deleted": True,
        "prepare_tasks": len(prep_task_ids),
        "material_tasks": len(material_task_ids),
        "staging_directories": removed_directories,
        "terminated_task_ids": sorted(terminated_task_ids),
        "terminated_pids": sorted(terminated_pids),
        "remaining_pids": [],
    })


@app.post("/api/material_extract")
async def material_extract(request: Request):
    """创建材料提取运行；支持按 parts 真实执行 part1/part2/part3。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
    body = body or {}
    _prune_material_tasks()
    paper_rows = [paper for paper in (body.get("papers") or []) if isinstance(paper, dict)]
    if paper_rows:
        contents, names, document_ids, figure_indexes = [], [], [], []
        for paper in paper_rows:
            staged_document_id = str(
                paper.get("document_id")
                or paper.get("server_document_id")
                or paper.get("id")
                or ""
            )
            client_document_id = str(paper.get("id") or staged_document_id)
            if client_document_id in DELETED_WORKSPACE_DOCUMENTS:
                return JSONResponse({"error": "工作区文档已删除", "deleted": True}, status_code=410)
            try:
                staged = document_dir(staged_document_id)
                manifest = read_json(staged / "manifest.json")
                markdown = (staged / "document.md").read_text(encoding="utf-8")
                figure_index_path = staged / "figure_index.json"
                figure_index = read_json(figure_index_path) if figure_index_path.exists() else []
                figure_index = [
                    {**item, "document_id": staged_document_id}
                    for item in figure_index
                ]
                name = str(paper.get("name") or manifest.get("source_name") or "")
            except (FileNotFoundError, ValueError):
                # Temporary compatibility for old browser sessions; new clients send only IDs.
                markdown = str(paper.get("markdown") or "")
                figure_index = paper.get("figure_index") or []
                name = str(paper.get("name") or "")
            if "data:image/" in markdown.lower() or any(
                "data:image/" in json.dumps(item, ensure_ascii=False).lower()
                for item in figure_index
            ):
                return JSONResponse({"error": "请求中包含持久 Base64 图片，请重新预处理 PDF"}, status_code=400)
            contents.append(markdown)
            names.append(name)
            document_ids.append(client_document_id)
            figure_indexes.append(figure_index)
    else:
        contents = [str(item or "") for item in (body.get("contents") or [])]
        names = [str(item or "") for item in (body.get("names") or [])]
        document_ids = [str(item or "") for item in (body.get("document_ids") or [])]
        figure_indexes = [[] for _ in contents]

    selected_parts = _normalize_material_parts(body.get("parts"))
    if not selected_parts:
        return JSONResponse({"error": "请明确选择一个提取部分"}, status_code=400)
    # 前端以一个“文件 + 部分”创建独立任务。接口同样强制该边界，避免任意
    # 批量请求重新把同一部分套用到其它文件，或把多个部分绑定到同一次执行中。
    if len(contents) != 1 or len(selected_parts) != 1:
        return JSONResponse({"error": "每次提取只能提交一个文件和一个提取部分"}, status_code=400)
    verify = bool(body.get("verify", False))
    provider = body.get("provider") or None
    valid_names = {item["name"] for item in list_providers()}
    if provider and provider not in valid_names:
        return JSONResponse({"error": f"未知模型：{provider}"}, status_code=400)
    if not contents:
        return JSONResponse({"error": "未收到任何论文内容"}, status_code=400)
    if any(not content.strip() for content in contents):
        return JSONResponse({"error": "存在尚未完成 PDF 解析的文件"}, status_code=400)

    names = (names + [""] * len(contents))[:len(contents)]
    document_ids = (document_ids + [""] * len(contents))[:len(contents)]
    figure_indexes = (figure_indexes + [[] for _ in contents])[:len(contents)]
    document_ids = [item or f"paper-{index + 1}" for index, item in enumerate(document_ids)]
    if any(document_id in DELETED_WORKSPACE_DOCUMENTS for document_id in document_ids):
        return JSONResponse({"error": "工作区文档已删除", "deleted": True}, status_code=410)

    task_id = uuid.uuid4().hex
    run_id = task_id
    seed_papers = [
        _new_material_paper(document_ids[index], names[index], index, selected_parts, run_id)
        for index in range(len(contents))
    ]
    ctx = _mp()
    q = ctx.Queue()
    proc = ctx.Process(
        target=_material_task_worker,
        args=(
            contents, names, document_ids, figure_indexes, selected_parts,
            verify, provider, run_id, q,
            get_log_queue(), getattr(request.state, "request_id", ""), task_id,
        ),
        daemon=True,
    )
    MATERIAL_TASKS[task_id] = {
        "task_id": task_id,
        "created": time.time(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": run_id,
        "request_id": getattr(request.state, "request_id", ""),
        "parts": selected_parts,
        "provider": provider or "",
        "document_ids": document_ids,
        "percent": 0.0,
        "stage": "排队中",
        "status": "queued",
        "done": False,
        "success": False,
        "error": "",
        "result": _material_result_from_papers(seed_papers),
        "seed_papers": seed_papers,
        "proc": proc,
        "q": q,
        "cancel": False,
        "process_started": False,
    }
    # Windows 的 multiprocessing 启动子进程会有明显的同步开销。放到线程里启动，
    # 避免阻塞 FastAPI 事件循环，前端可以立即显示“正在启动”。
    task = MATERIAL_TASKS[task_id]
    starter = asyncio.create_task(asyncio.to_thread(proc.start))
    task["start_future"] = starter
    try:
        await asyncio.shield(starter)
        task["process_started"] = True
        _record_task_process_identity(task)
    except Exception as exc:
        logger.exception(
            "材料提取进程启动失败",
            extra={"request_id": getattr(request.state, "request_id", ""), "task_id": task_id},
        )
        MATERIAL_TASKS.pop(task_id, None)
        _close_task_queue(task)
        return JSONResponse({"error": f"提取进程启动失败：{exc}"}, status_code=500)
    if any(document_id in DELETED_WORKSPACE_DOCUMENTS for document_id in document_ids):
        await _cancel_material_task(task_id)
        return JSONResponse({"error": "工作区文档已删除", "deleted": True}, status_code=410)
    consumer = asyncio.create_task(_consume_material_task(task_id))
    task["consumer"] = consumer
    return JSONResponse({
        "task_id": task_id,
        "run_id": run_id,
        "parts": selected_parts,
        "document_ids": document_ids,
    })


@app.get("/api/material_progress/{task_id}")
async def material_progress(task_id: str):
    task = MATERIAL_TASKS.get(task_id)
    if task is None:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    return JSONResponse({
        "task_id": task_id,
        "run_id": task.get("run_id", task_id),
        "parts": task.get("parts") or [],
        "provider": task.get("provider") or "",
        "status": task.get("status", "queued"),
        "percent": round(task["percent"], 1),
        "stage": task["stage"],
        "done": task["done"],
        "success": task["success"],
        "error": task["error"],
        "cancelled": bool(task.get("cancel", False) or task.get("cancelled", False)),
        "started_at": task.get("started_at", ""),
        "result": task.get("result") or {},
    })


@app.post("/api/stop_material/{task_id}")
async def stop_material(task_id: str):
    """请求终止正在进行的材料提取任务：terminate 子进程实现立即取消
    （当前正在处理的该篇丢弃，已完成篇的结果保留）。"""
    task = MATERIAL_TASKS.get(task_id)
    if task is None:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    logger.info(
        "请求取消材料提取",
        extra={"request_id": task.get("request_id", ""), "task_id": task_id},
    )
    result = await _cancel_material_task(task_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 500)


# ============ 命令行入口（便于 python -m backend.main 启动）============
def main():
    import click
    import uvicorn

    @click.command()
    @click.option("--port", type=int, default=8000, help="监听端口")
    @click.option("--host", type=str, default="127.0.0.1", help="监听地址")
    def run(port: int, host: str):
        # 请求访问日志由 FastAPI middleware 统一记录，避免与 Uvicorn 默认
        # access logger 重复；应用错误仍由 uvicorn.error 进入 app.log。
        uvicorn.run(app, host=host, port=port, access_log=False, log_config=None)

    run()


if __name__ == "__main__":
    main()
