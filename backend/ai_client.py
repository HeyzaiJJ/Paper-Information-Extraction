"""
ai_client.py —— AI 模型调用核心基础设施（v3.1 多模型版）

提供能力：
  - get_model_config()   → 读取模型配置（显式指定 provider 时以 yaml 为准）
  - list_providers()     → 返回后端已部署模型列表（不含任何密钥），供前端选择
  - create_client()      → 创建 OpenAI 兼容客户端
  - chat()               → 通用单次模型调用（自动剥离 <think> 思维链）
  - parse_json()         → 从 LLM 回复中提取 JSON
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import yaml
import httpx
from openai import OpenAI

from backend.paths import BACKEND_ROOT, REPO_ROOT, CONFIG_DIR

logger = logging.getLogger("paper.ai")

# ============ 配置加载 ============

def _load_dotenv() -> dict:
    """极简 .env 解析。"""
    env_path = REPO_ROOT / ".env"
    cfg = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def _load_model_config() -> dict:
    """加载 backend/config/models.yaml。"""
    config_path = CONFIG_DIR / "models.yaml"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


def get_model_config(provider: str = None) -> dict:
    """获取模型配置。

    优先级规则（v3.1）：
      - 显式传入的 provider 一律以 models.yaml 中该 provider 的配置为准；
      - 未传 provider 时取 default_provider；
      - .env 里的 AI_API_BASE_URL / AI_MODEL 仅作为「provider 缺该项」时的兜底，
        不再覆盖 yaml 中已明确写出的 base_url / chat 模型名（修复旧版选模型失效问题）。

    返回：{provider, label, api_key_env, base_url, api_key, model, context_window, max_output}
    """
    dotenv = _load_dotenv()
    yaml_cfg = _load_model_config()

    provider = provider or yaml_cfg.get("default_provider", "mimo-v2.5-pro")
    prov = yaml_cfg.get("providers", {}).get(provider, {})

    api_key_env = prov.get("api_key_env", "")
    api_key = os.getenv(api_key_env, dotenv.get(api_key_env, ""))

    # yaml 优先，.env 兜底
    base_url = prov.get("base_url") or os.getenv("AI_API_BASE_URL", dotenv.get("AI_API_BASE_URL", ""))
    model_name = prov.get("models", {}).get("chat") or os.getenv("AI_MODEL", dotenv.get("AI_MODEL", ""))

    return {
        "provider": provider,
        "label": prov.get("label", provider),
        "api_key_env": api_key_env,
        "base_url": base_url,
        "api_key": api_key,
        "model": model_name,
        "context_window": prov.get("context_window", {}).get("chat", 1048576),
        "max_output": prov.get("max_output", 8192),
    }


def list_providers() -> list:
    """返回后端已部署的全部 provider 列表（不含任何密钥）。

    每项：{name, label, default, configured, order}
      - configured：对应 api_key_env 是否在环境变量 / .env 中存在（前端据此置灰未配置项）
      - default：   是否为 default_provider（前端默认选中）
    """
    yaml_cfg = _load_model_config()
    dotenv = _load_dotenv()
    providers = yaml_cfg.get("providers", {})
    default = yaml_cfg.get("default_provider")
    out = []
    for name, prov in providers.items():
        # Providers marked selectable=false are reserved for internal jobs
        # (currently the vision expert) and must not be accepted by the text
        # extraction model picker/API.
        if prov.get("selectable", True) is False:
            continue
        ak = prov.get("api_key_env", "")
        configured = bool(os.getenv(ak, dotenv.get(ak, "")))
        out.append({
            "name": name,
            "label": prov.get("label", name),
            "default": name == default,
            "configured": configured,
            "order": prov.get("order", 999),
        })
    out.sort(key=lambda x: x["order"])
    return out


def get_vision_config() -> dict:
    """读取 models.yaml 的 vision: 段，带默认值。

    视觉专家固定由该段指定的 provider（默认 qwen3.6-flash）承担图片分析，
    与文本分析所选模型解耦。
    """
    yaml_cfg = _load_model_config()
    v = yaml_cfg.get("vision", {})
    return {
        "provider": v.get("provider", "qwen3.6-flash"),
        "concurrency": int(v.get("concurrency", 10)),
        "batch_per_call": int(v.get("batch_per_call", 3)),
        "max_images_per_paper": int(v.get("max_images_per_paper", 60)),
        "max_retries": int(v.get("max_retries", 3)),
    }


def create_client(cfg: dict = None) -> OpenAI:
    """创建 OpenAI 兼容客户端。"""
    cfg = cfg or get_model_config()
    if not cfg["api_key"]:
        logger.warning("模型 API Key 未配置 provider=%s", cfg.get("provider", ""))
        raise RuntimeError(
            f"API Key 未配置（provider={cfg['provider']}，请在 .env 中设置 "
            f"{cfg.get('api_key_env') or '对应的 API Key 环境变量'}）"
        )
    # 这台机器启用了系统 HTTP(S) 代理；该代理会让小米模型服务的 TLS 握手提前断开
    # （UNEXPECTED_EOF_WHILE_READING）。模型 API 统一直连，避免环境变量中的代理干扰。
    # timeout：长文 + 慢模型（如 qwen/deepseek）容易超时，统一给足；网络层重试交给调用方。
    http_client = httpx.Client(
        trust_env=False,
        timeout=httpx.Timeout(600.0, connect=30.0),
    )
    try:
        client = OpenAI(
            base_url=cfg["base_url"], api_key=cfg["api_key"],
            timeout=600, http_client=http_client,
        )
    except Exception:
        logger.exception("创建模型客户端失败 provider=%s", cfg.get("provider", ""))
        raise
    try:
        client._paper_provider = cfg.get("provider", "")
    except Exception:
        pass
    return client


# ============ 工具函数 ============

def parse_json(text: str) -> dict:
    """从模型回复中提取 JSON，兼容 <think> 标签、代码块等格式。"""
    text = re.sub(r"<think[\s\S]*?</think>", "", text).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ============ 模型调用封装 ============

def chat(
    client: OpenAI,
    model: str,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 16384,
    response_json: bool = False,
) -> str:
    """通用单次模型调用（公开接口）。

    自动剥离推理类模型（qwen / deepseek-v4 等）返回的 <think>…</think> 思维链，
    避免污染后续 Markdown 渲染。
    """
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_json:
        kwargs["response_format"] = {"type": "json_object"}

    started = time.perf_counter()
    try:
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
    except Exception:
        logger.exception(
            "模型调用失败 provider_model=%s/%s elapsed_ms=%.1f",
            getattr(client, "_paper_provider", "-"), model,
            (time.perf_counter() - started) * 1000,
        )
        raise
    content = re.sub(r"<think[^>]*>[\s\S]*?</think>", "", content, flags=re.IGNORECASE).strip()
    logger.info(
        "模型调用完成 model=%s elapsed_ms=%.1f response_chars=%d",
        model, (time.perf_counter() - started) * 1000, len(content),
    )
    return content


# ============ 数据结构（保留供后续使用） ============

@dataclass
class ExtractionResult:
    """提取结果（占位，待重建提示词后恢复）。"""
    paper_type: str = ""
    research_field: str = ""
    keywords: list[str] = dc_field(default_factory=list)
    compositions: dict = dc_field(default_factory=dict)
    performance: dict = dc_field(default_factory=dict)
    summary: str = ""
    analysis: str = ""
    figures: list[dict] = dc_field(default_factory=list)
    tables: list[dict] = dc_field(default_factory=list)
    metadata: dict = dc_field(default_factory=dict)
    extraction_notes: list[str] = dc_field(default_factory=list)
    model_used: str = ""
    total_calls: int = 0
    total_time: float = 0.0


# ============ 命令行入口 ============

if __name__ == "__main__":
    cfg = get_model_config()
    print(f"[ai_client v3.1] 配置读取 OK")
    print(f"  provider:  {cfg['provider']}")
    print(f"  label:     {cfg['label']}")
    print(f"  base_url:  {cfg['base_url']}")
    print(f"  model:     {cfg['model']}")
    print(f"  api_key:   {'已配置' if cfg['api_key'] else '未配置（请设置环境变量或 .env）'}")
    print(f"  context:   {cfg.get('context_window', '?')} tokens")
    print(f"\n  可用模型：")
    for p in list_providers():
        flag = "✓" if p["configured"] else "✗(未配置key)"
        dft = " [默认]" if p["default"] else ""
        print(f"    - {p['name']}  ({p['label']})  {flag}{dft}")
