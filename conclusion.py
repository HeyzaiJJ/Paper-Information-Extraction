"""conclusion.py —— 调用项目配置的 AI 模型，以 Markdown 直出方式生成论文「全文综合结论与未来研究建议」。

设计原则（与 summarize.py / extract.py 完全独立）：
  - 不 import summarize / extract，三者只共用 preprocess、ai_client 与 prompts。
  - 模型依据 prompts/material_master_prompt.md（mode=conclusion）输出 Markdown 章节，
    提示词统一来源为 master .md 文件（改 .md 即生效）。
  - 输出经「剥离代码块」后透传，由前端 marked 直接渲染。

依赖：
  - ai_client.py   ：get_model_config / create_client / chat
  - prompts        ：render_conclusion_messages（读 prompts/material_master_prompt.md，mode=conclusion）
  - preprocess/clean：preprocess / CleanResult（预处理）

用法：
  # 离线校验（仅渲染提示词，不调 API，不耗额度）
  python conclusion.py --check "论文1.md"

  # 真实调用（需先配置 .env 中的 MIMO_API_KEY）
  python conclusion.py --live "论文1.md"
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ai_client import get_model_config, create_client, chat
from prompts import render_conclusion_messages
from preprocess.clean import preprocess, CleanResult

# 结论类任务低温度，与摘要/提取链一致
CONCLUDE_TEMPERATURE = 0.1

# 不同篇幅的自适应 max_tokens
_MAX_TOKENS_BY_CATEGORY = {
    "short": 8000,
    "medium": 12000,
    "large": 20000,
    "super_large": 28000,
}


def _strip_code_fence(md: str) -> str:
    """去掉模型偶发输出的 ```markdown … ``` 或 ``` … ``` 包裹。"""
    md = (md or "").strip()
    if md.startswith("```"):
        first_nl = md.find("\n")
        if first_nl != -1:
            body = md[first_nl + 1:]
            if body.rstrip().endswith("```"):
                body = body.rstrip()[:-3]
            return body.strip()
    return md


def conclude_paper_markdown(client, model, clean: CleanResult, title: str = "",
                            max_tokens: int = None, max_output_cap: int = None,
                            image_summary: str = None) -> str:
    """单篇全文生成「全文综合结论 + 未来研究建议」（Markdown 直出）。
    返回 Markdown 字符串（已去除代码块包裹）。
    image_summary：可选，图片视觉分析结果，注入提示词供交叉核对（纯文本时为空/None）。"""
    if max_tokens is None:
        max_tokens = _MAX_TOKENS_BY_CATEGORY.get(clean.length_category, 12000)
    if max_output_cap:
        max_tokens = min(max_tokens, int(max_output_cap * 0.9))
    messages = render_conclusion_messages(clean, title=title, image_summary=image_summary)
    md = chat(
        client, model, messages,
        temperature=CONCLUDE_TEMPERATURE,
        max_tokens=max_tokens,
        response_json=False,
    )
    return _strip_code_fence(md)


def conclude_from_clean_markdown(clean: CleanResult, title: str = "", provider: str = None,
                                 image_summary: str = None) -> str:
    """便利函数：从 config/models.yaml 创建客户端并生成结论（需已配置 API Key）。"""
    cfg = get_model_config(provider)
    client = create_client(cfg)
    return conclude_paper_markdown(client, cfg["model"], clean, title=title,
                                   max_output_cap=cfg.get("max_output"),
                                   image_summary=image_summary)


# ============ 命令行 ============

def _load_md(path: str) -> CleanResult:
    text = Path(path).read_text(encoding="utf-8")
    return preprocess(text)


def main():
    ap = argparse.ArgumentParser(description="论文全文综合结论与未来研究建议生成（Markdown 直出）")
    ap.add_argument("files", nargs="+", help="marker 产出的论文 md 文件")
    ap.add_argument("--live", action="store_true",
                    help="真实调用 AI（默认仅渲染提示词做离线校验）")
    ap.add_argument("--provider", default=None,
                    help="config/models.yaml 中的 provider，默认取 default_provider")
    args = ap.parse_args()

    client = model = None
    if args.live:
        cfg = get_model_config(args.provider)
        client = create_client(cfg)
        model = cfg["model"]

    for f in args.files:
        clean = _load_md(f)
        print(f"\n=== {Path(f).name} ===")
        print(f"  清洗后字符数: {clean.clean_length}  篇幅分级: {clean.length_category}")
        if args.live:
            try:
                md = conclude_from_clean_markdown(clean, title=Path(f).stem)
            except Exception as e:
                print(f"  [调用失败] {e}")
                continue
            print("  ---- Markdown 直出预览 ----")
            print(md)


if __name__ == "__main__":
    main()
