"""Runtime configuration for Marker 2.0 and its split inference services.

This module is intentionally imported before any ``marker``/``surya`` module.
Surya settings are constructed at import time, so applying the environment later
would silently keep the old process environment.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from dotenv import load_dotenv

from backend.paths import BACKEND_ROOT, REPO_ROOT, CONFIG_DIR

BASE_DIR = BACKEND_ROOT
DEFAULT_CONFIG_PATH = CONFIG_DIR / "runtime.yaml"
logger = logging.getLogger("paper.runtime")


@dataclass(frozen=True)
class RuntimeConfig:
    raw: dict[str, Any]
    path: Path

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        return value if isinstance(value, dict) else {}

    @property
    def marker(self) -> dict[str, Any]:
        return self.section("marker")

    @property
    def remote_inference(self) -> dict[str, Any]:
        return self.section("remote_inference")

    @property
    def local_ocr_error(self) -> dict[str, Any]:
        return self.section("local_ocr_error")

    @property
    def conversion(self) -> dict[str, Any]:
        return self.section("conversion")

    @property
    def storage(self) -> dict[str, Any]:
        return self.section("storage")

    @property
    def export(self) -> dict[str, Any]:
        return self.section("export")

    @property
    def logging(self) -> dict[str, Any]:
        return self.section("logging")


_CONFIG: RuntimeConfig | None = None


def _env_bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def load_runtime_config(path: str | os.PathLike[str] | None = None) -> RuntimeConfig:
    global _CONFIG
    config_path = Path(path or os.getenv("MARKER_WEB_RUNTIME_CONFIG") or DEFAULT_CONFIG_PATH)
    config_path = config_path.resolve()
    if _CONFIG is not None and _CONFIG.path == config_path:
        return _CONFIG
    if not config_path.exists():
        raise RuntimeError(f"运行时配置不存在：{config_path}")
    load_dotenv(REPO_ROOT / ".env", override=False)
    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"运行时配置根节点必须是对象：{config_path}")
    _CONFIG = RuntimeConfig(payload, config_path)
    return _CONFIG


def apply_runtime_environment(config: RuntimeConfig | None = None) -> RuntimeConfig:
    """Map project YAML/.env values to the exact variables Surya consumes."""
    config = config or load_runtime_config()
    remote = config.remote_inference
    ocr = config.local_ocr_error
    fast_layout = config.section("fast_layout")
    marker = config.marker

    api_key_env = str(remote.get("api_key_env") or "SURYA_VLLM_API_KEY")
    api_key = os.getenv(api_key_env, "EMPTY")
    mapping = {
        "SURYA_INFERENCE_BACKEND": remote.get("backend", "vllm"),
        "SURYA_INFERENCE_URL": remote.get("base_url", ""),
        "SURYA_INFERENCE_AUTOSTART": _env_bool(remote.get("autostart", False)),
        "SURYA_INFERENCE_PARALLEL": remote.get("parallel", 8),
        "SURYA_INFERENCE_TIMEOUT_SECONDS": remote.get("timeout_seconds", 600),
        "VLLM_API_KEY": api_key,
        "TORCH_DEVICE": ocr.get("device", "cpu"),
        "OCR_ERROR_SERVER_HOST": ocr.get("host", "127.0.0.1"),
        "OCR_ERROR_SERVER_PORT": ocr.get("port", 8101),
        "OCR_ERROR_SERVER_AUTOSTART": _env_bool(ocr.get("autostart", True)),
        "OCR_ERROR_SERVER_STARTUP_TIMEOUT": ocr.get("startup_timeout_seconds", 300),
        "OCR_ERROR_SERVER_TIMEOUT": ocr.get("request_timeout_seconds", 600),
        "OCR_ERROR_SERVER_MAX_BATCH": ocr.get("max_batch", 64),
        "FAST_LAYOUT_SERVER_AUTOSTART": _env_bool(fast_layout.get("autostart", False)),
        "DISABLE_TQDM": _env_bool(marker.get("disable_tqdm", True)),
    }
    for key, value in mapping.items():
        if value is not None:
            os.environ[key] = str(value)
    # LAN inference and localhost OCR services must bypass any corporate/system
    # HTTP proxy configured on Windows.
    remote_host = urlparse(str(remote.get("base_url") or "")).hostname
    no_proxy_hosts = {"127.0.0.1", "localhost"}
    if remote_host:
        no_proxy_hosts.add(remote_host)
    existing_no_proxy = {
        item.strip() for item in os.getenv("NO_PROXY", "").split(",") if item.strip()
    }
    no_proxy = ",".join(sorted(existing_no_proxy | no_proxy_hosts))
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy
    return config


def marker_config_values(config: RuntimeConfig | None = None) -> dict[str, Any]:
    """Return Marker converter options controlled by runtime.yaml."""
    config = config or load_runtime_config()
    marker = config.marker
    values = {
        "mode": marker.get("mode", "balanced"),
        "pdftext_workers": marker.get("pdftext_workers", 1),
        "extract_images": marker.get("extract_images", True),
        "disable_tqdm": marker.get("disable_tqdm", True),
        "use_llm": marker.get("use_llm", False),
    }
    # Marker 2 uses DPI settings rather than a persisted base64 image mode.
    if marker.get("image_extraction_mode") == "highres":
        values.setdefault("highres_image_dpi", 192)
    return values


def validate_marker_version(config: RuntimeConfig | None = None) -> str:
    config = config or load_runtime_config()
    required = str(config.marker.get("required_version") or "").strip()
    installed = importlib.metadata.version("marker-pdf")
    if required and installed != required:
        raise RuntimeError(f"Marker 版本不匹配：需要 {required}，当前为 {installed}")
    return installed


def _auth_headers(config: RuntimeConfig) -> dict[str, str]:
    env_name = str(config.remote_inference.get("api_key_env") or "SURYA_VLLM_API_KEY")
    token = os.getenv(env_name, "EMPTY")
    return {"Authorization": f"Bearer {token}"} if token else {}


def check_remote_inference(config: RuntimeConfig | None = None) -> dict[str, Any]:
    """Fail closed when the Ubuntu vLLM endpoint is unavailable.

    No local inference fallback is attempted here or in Marker configuration.
    """
    config = config or load_runtime_config()
    base_url = str(config.remote_inference.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("remote_inference.base_url 未配置")
    root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
    timeout = min(float(config.remote_inference.get("timeout_seconds", 600)), 30.0)
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            trust_env=False,
        ) as client:
            health = client.get(f"{root_url}/health", headers=_auth_headers(config))
            health.raise_for_status()
            models = client.get(f"{root_url}/v1/models", headers=_auth_headers(config))
            models.raise_for_status()
            model_payload = models.json()
    except Exception as exc:
        logger.exception("远程 Surya/vLLM 健康检查失败 url=%s", root_url)
        raise RuntimeError(
            f"远程 Surya/vLLM 不可达（{root_url}）；已禁止回退本地主模型：{exc}"
        ) from exc
    rows = model_payload.get("data") if isinstance(model_payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"远程 vLLM /v1/models 未返回可用模型：{json.dumps(model_payload)[:500]}")
    return {"health": health.json() if health.content else {}, "models": rows}


def prewarm_local_ocr_error(config: RuntimeConfig | None = None) -> None:
    """Start/attach the single local CPU OCR Error service and issue one request."""
    config = config or load_runtime_config()
    if not config.local_ocr_error.get("enabled", True):
        return
    # Imported only after environment mapping has completed.
    from surya.ocr_error import OCRErrorPredictor

    predictor = OCRErrorPredictor()
    result = predictor(["Marker OCR Error service warmup."])
    if not getattr(result, "labels", None):
        raise RuntimeError("本地 OCR Error 服务预热未返回结果")


def check_local_ocr_error(config: RuntimeConfig | None = None) -> dict[str, Any]:
    config = config or load_runtime_config()
    ocr = config.local_ocr_error
    url = f"http://{ocr.get('host', '127.0.0.1')}:{int(ocr.get('port', 8101))}/health"
    try:
        response = httpx.get(url, timeout=10.0, trust_env=False)
        response.raise_for_status()
        return response.json() if response.content else {"status": "ok"}
    except Exception as exc:
        logger.exception("本地 OCR Error 健康检查失败 url=%s", url)
        raise RuntimeError(f"本地 OCR Error 服务健康检查失败（{url}）：{exc}") from exc


# Apply immediately so any module imported after this line sees the correct Surya settings.
RUNTIME_CONFIG = apply_runtime_environment()
