"""vision.py —— 视觉专家：并发调用 qwen3.6-flash 分析论文图片，产出 image_summary。

设计要点：
  - 视觉专家固定由 config/models.yaml 的 vision: 段指定（默认 qwen3.6-flash），
    与文本分析所选模型解耦（文本按前端下拉选中的 provider）。
  - 信号量限并发（concurrency），每调用带 batch_per_call 张图，429/超时指数退避重试。
  - 全失败返回 ''，由调用方纯文本兜底，不中断整篇。
"""

import asyncio
import base64
import io
import logging
import time
from openai import OpenAI
from PIL import Image

from backend.ai_client import get_model_config, create_client, get_vision_config
from backend.runtime_config import RUNTIME_CONFIG

logger = logging.getLogger("paper.vision")

VISION_SYSTEM = (
    "你是材料科学论文的图表分析专家。下面给出一篇论文的若干张图，每张图都有真实图号。"
    "请【分别、独立】描述每张图，严格沿用输入中给出的图号逐段输出，例如“Fig. 3：”或“Fig. 3a：”，"
    "不得自行重新编号或改写图号。\n"
    "Fig. N：图的类型（显微图/曲线/示意图/表格截图等）；坐标轴与物理量；关键趋势或结构特征；"
    "图中标注的文字与数据要点。\n"
    "不要在不同图之间互相引用，不要编造图中不存在的信息；含子图(a)(b)时分别说明。"
)


def _build_user_text(batch: list) -> str:
    lines = [
        f"{_figure_label(it)}（{(it.caption or '无标注')[:240]}）"
        for it in batch
    ]
    return "待分析图片清单：\n" + "\n".join(lines) + "\n请按编号分别描述上述每张图。"


def _figure_label(item) -> str:
    """Use only the JSON-verified FigureIndex number for visual analysis."""
    fig_id = str(getattr(item, "figure_id", "") or "").strip()
    if fig_id.lower().startswith("fig"):
        return "Fig. " + fig_id[3:]
    return "未编号图片"


def _vision_call(client: OpenAI, model: str, batch: list) -> str:
    content = [{"type": "text", "text": _build_user_text(batch)}]
    for it in batch:
        # Base64 exists only for the lifetime of this outbound request. The source
        # bytes come from staging or SQLite via an asset ID.
        max_edge = int(RUNTIME_CONFIG.storage.get("vlm_max_edge", 2048))
        image_bytes = it.data
        mime_type = getattr(it, "mime_type", "image/png") or "image/png"
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                if max(image.size) > max_edge:
                    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                fmt = "JPEG" if mime_type == "image/jpeg" else "PNG"
                if fmt == "JPEG" and image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                stream = io.BytesIO()
                image.save(stream, format=fmt, optimize=True)
                image_bytes = stream.getvalue()
        except Exception:
            pass
        data_uri = f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": VISION_SYSTEM},
            {"role": "user", "content": content},
        ],
        temperature=0.1,
        max_tokens=4000,
    )
    return resp.choices[0].message.content or ""


async def analyze_images(images: list, provider: str = None) -> str:
    """并发分析图片，返回拼接的 image_summary。全失败返回 ''（由调用方纯文本兜底）。"""
    if not images:
        return ""
    cfg = get_vision_config()
    provider = provider or cfg["provider"]
    mc = get_model_config(provider)
    client = create_client(mc)            # 共享客户端（OpenAI SDK 线程安全）
    model = mc["model"]
    sem = asyncio.Semaphore(cfg["concurrency"])
    size = max(1, cfg["batch_per_call"])
    batches = [images[i:i + size] for i in range(0, len(images), size)]

    async def _one(batch):
        async with sem:
            last = None
            started = time.perf_counter()
            for attempt in range(cfg["max_retries"] + 1):
                try:
                    result = await asyncio.to_thread(_vision_call, client, model, batch)
                    logger.info(
                        "视觉批次完成 model=%s images=%d attempts=%d elapsed_ms=%.1f",
                        model, len(batch), attempt + 1,
                        (time.perf_counter() - started) * 1000,
                    )
                    return result
                except Exception as e:        # 含 429 RateLimitError
                    last = e
                    if attempt < cfg["max_retries"]:
                        logger.warning(
                            "视觉批次失败，将重试 model=%s images=%d attempt=%d/%d error=%s",
                            model, len(batch), attempt + 1, cfg["max_retries"] + 1,
                            str(e)[:240],
                        )
                        await asyncio.sleep(min(30, 2 ** attempt))   # 指数退避
                    else:
                        logger.exception(
                            "视觉批次最终失败 model=%s images=%d attempts=%d",
                            model, len(batch), attempt + 1,
                        )
            return f"[看图失败] {last}"

    parts = await asyncio.gather(*[_one(b) for b in batches])
    return "\n\n".join(p for p in parts if not p.startswith("[看图失败]")).strip()
