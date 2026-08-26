"""summarize.py —— 调用项目配置的 AI 模型，以 Markdown 直出方式总结论文摘要与结论。

依赖：
  - ai_client.py   ：get_model_config / create_client / chat
  - prompts        ：render_summary_messages（读 material_master_prompt.md，mode=summary，代码拼装 user 消息）
  - preprocess/clean：preprocess / CleanResult（预处理）

返回：Markdown 字符串（含「## 摘要总结」「## 结论总结」两节），供前端 marked 直接渲染。

用法：
  # 离线校验（仅渲染提示词，不调 API，不耗额度）
  python summarize.py --check "论文1.md" "论文2.md"

  # 真实调用（需先配置 .env 中的 MIMO_API_KEY 等）
  python summarize.py --live "论文1.md"
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ai_client import get_model_config, create_client, chat
from prompts import render_summary_messages, summary_mode
from preprocess.clean import preprocess, CleanResult

SUMMARIZE_TEMPERATURE = 0.1


def summarize_abstract_conclusion(
    client, model, clean_result: CleanResult, title: str = "", max_output_cap: int = None
) -> str:
    """对单篇预处理结果调用模型，返回 Markdown 字符串（摘要总结 + 结论总结）。
    调用异常时重试一次。"""
    messages = render_summary_messages(clean_result, title=title)
    # 输出上限跟随所选模型，避免超过该模型的 max_output（默认 2048 仍适用大模型）
    max_tokens = 2048
    if max_output_cap:
        max_tokens = min(max_tokens, int(max_output_cap * 0.9))
    last_err = None
    for _ in range(2):
        try:
            md = chat(
                client,
                model,
                messages,
                temperature=SUMMARIZE_TEMPERATURE,
                max_tokens=max_tokens,
                response_json=False,
            )
            return (md or "").strip()
        except Exception as e:  # 重试一次：网络抖动等
            last_err = e
    raise RuntimeError(f"摘要/结论总结失败：{last_err}")


def summarize_from_clean(
    clean_result: CleanResult, title: str = "", provider: str = None
) -> str:
    """便利函数：从 config/models.yaml 创建客户端并总结（需已配置 API Key），返回 Markdown 字符串。"""
    cfg = get_model_config(provider)
    client = create_client(cfg)
    return summarize_abstract_conclusion(client, cfg["model"], clean_result, title=title,
                                         max_output_cap=cfg.get("max_output"))


# ============ 命令行 ============

def _load_md(path: str) -> CleanResult:
    text = Path(path).read_text(encoding="utf-8")
    return preprocess(text)


def main():
    ap = argparse.ArgumentParser(description="论文摘要/结论总结（Markdown 直出，基于项目配置的 AI 模型）")
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
        mode = summary_mode(clean)
        print(f"  提示词模式: {'精准(切片直投)' if mode == 'precise' else '定位(头部+尾部)'}")
        msgs = render_summary_messages(clean)
        print(f"  system 字数: {len(msgs[0]['content'])}  user 字数: {len(msgs[1]['content'])}")
        if args.live:
            try:
                md = summarize_abstract_conclusion(client, model, clean)
            except Exception as e:
                print(f"  [调用失败] {e}")
                continue
            print("  ---- Markdown 直出预览 ----")
            print(md)


if __name__ == "__main__":
    main()
