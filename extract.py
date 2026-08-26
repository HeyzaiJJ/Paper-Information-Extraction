"""extract.py —— 调用项目配置的 AI 模型，以 Markdown 直出方式提取论文中的材料成分配比与性能参数。

设计原则（与 summarize.py 完全独立）：
  - 不 import summarize，不共享其任何内部状态；两个模块只共用 preprocess 与 ai_client。
  - 模型直接依据 master 提示词（material_master_prompt.md, mode=extract）输出 Markdown 章节，
    不再走 JSON schema / 渲染层。提示词统一来源为 master .md 文件（改 .md 即生效）。
  - 输出经「剥离代码块 + 标题降一级」后透传给前端，由前端 marked 直接渲染。

依赖：
  - ai_client.py   ：get_model_config / create_client / chat
  - prompts        ：render_material_md_messages（读取 master .md，mode=extract 作为系统提示词）
  - preprocess/clean：preprocess / CleanResult（预处理）

用法：
  # 离线校验（仅渲染提示词，不调 API，不耗额度）
  python extract.py "论文1.md" "论文2.md"

  # 真实调用（需先配置 .env 中的 MIMO_API_KEY）
  python extract.py --live "论文1.md"
  python extract.py --live --verify "超长综述.md"     # 二次查漏复核
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from ai_client import get_model_config, create_client, chat
from prompts import render_material_md_messages
from preprocess.clean import preprocess, CleanResult

# 提取类任务低温度；与摘要链一致
EXTRACT_TEMPERATURE = 0.1

# 不同篇幅的自适应 max_tokens（MiMo max_output 上限 131072，远够用）
_MAX_TOKENS_BY_CATEGORY = {
    "short": 16000,
    "medium": 24000,
    "large": 48000,
    "super_large": 64000,
}


# ============ Markdown 直出模式（v2 自适应提示词）============

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


def _downgrade_headings(md: str) -> str:
    """把 v2 输出的标题整体降一级，使其在「二、材料成分配比与性能参数对比」之下正确嵌套。"""
    out = []
    for line in (md or "").splitlines():
        m = re.match(r"^(#{2,6})\s", line)
        if m:
            lvl = len(m.group(1))
            out.append("#" * (lvl + 1) + line[lvl:])
        else:
            out.append(line)
    return "\n".join(out)


def escape_approximate_tildes(md: str) -> str:
    """Escape ``~25%``-style approximate values before Markdown rendering.

    marked enables single-tilde strikethrough, so two approximate values in
    one paragraph can accidentally turn everything between them into deleted
    text. Preserve intentional ``~~deleted~~`` syntax while escaping an
    unescaped tilde immediately followed by a numeric value.
    """
    return re.sub(r"(?<![\\~])~(?=\s*\d)", r"\\~", md or "")


def normalize_list_indentation(md: str) -> str:
    """Prevent model-generated list items from becoming indented code blocks.

    The report format only needs one nested list level. Some model responses
    indent sibling list items with four or more spaces; Markdown then treats
    them as code in some contexts and displays the raw ``- **text**`` syntax.
    Normalize those items to the supported two-space nested level. Fenced code
    blocks are left untouched because indentation is meaningful there.
    """
    normalized: list[str] = []
    in_fence = False
    list_item = re.compile(r"^( {4,})(?:[-+*]|\d+[.)])(?=\s)")

    for line in (md or "").splitlines(keepends=True):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            normalized.append(line)
            continue
        if not in_fence:
            match = list_item.match(line)
            if match:
                line = "  " + line[len(match.group(1)):]
        normalized.append(line)
    return "".join(normalized)


# ====== 图片注入：在模型输出中把图号引用替换为实际图片 markdown ======

# Match a complete grouped figure reference, such as ``Fig. 8, 9`` or
# ``Fig. 14a, b``. The group is resolved only against FigureIndex keys that
# have already been verified from Marker JSON.
_FIG_REF_RE = re.compile(
    r"(?:Fig(?:ure)?\.?\s*|图\s*)"
    r"(?P<refs>\d+[a-z]?(?![a-z0-9])"
    r"(?:\s*(?:,|，|、|\b(?:and|to)\b|和|及|-|–|~)\s*"
    r"(?:\d+[a-z]?(?![a-z0-9])|[a-z](?![a-z0-9])))*)",
    re.IGNORECASE,
)
_FIG_REF_TOKEN_RE = re.compile(r"\d+[a-z]?|[a-z](?![a-z0-9])", re.IGNORECASE)


def _figure_keys_from_reference(refs: str, figure_map: dict[str, str]) -> list[str]:
    """Resolve a Fig. reference group to JSON-verified FigureIndex keys.

    A subfigure reference such as ``Fig. 14a`` may point to the single Marker
    JSON image for the parent ``fig14``. That parent fallback is allowed only
    when it already exists in ``figure_map``; no image is inferred otherwise.
    """
    keys: list[str] = []
    current_number = ""
    for token in _FIG_REF_TOKEN_RE.findall(refs or ""):
        token = token.lower()
        number_match = re.fullmatch(r"(\d+)([a-z]?)", token)
        if number_match:
            current_number = number_match.group(1)
            requested = f"fig{token}"
            parent = f"fig{current_number}"
        elif current_number:
            requested = f"fig{current_number}{token}"
            parent = f"fig{current_number}"
        else:
            continue

        if requested in figure_map:
            key = requested
        elif requested != parent and parent in figure_map:
            key = parent
        else:
            continue
        if key not in keys:
            keys.append(key)
    return keys


def inject_figure_images(md: str, figure_map: dict[str, str]) -> str:
    """在模型输出的提取结果中，把图号引用注入为可渲染的图片 markdown。

    首次出现某个图号时，在引用行后追加 ![]() 格式的图片标记；若引用位于 Markdown
    表格行，图片延后到整张表结束后再插入，避免空行中断表格。同一图号后续出现不再重复
    插入。figure_map 的键为归一化图号 "fig3"，值为 data URI 字符串。
    支持一行中含多个图号引用和紧凑分组（如 "据 Fig. 5 和 Fig. 6"、
    "Fig. 8, 9"、"Fig. 14a, b"）。

    返回注入后的 markdown。marked 原生渲染 data URI 图片，前端无需改动。
    """
    if not figure_map or not md:
        return md

    injected: set[str] = set()
    deferred_inserts: list[str] = []
    result: list[str] = []

    def append_images(images: list[str]) -> None:
        if not images:
            return
        # A blank line keeps images outside the preceding paragraph or table.
        if result:
            if result[-1].strip():
                result.append("\n" if result[-1].endswith("\n") else "\n\n")
        result.append("\n".join(images) + "\n\n")

    for line in (md or "").splitlines(True):         # keepends=True
        is_table_row = bool(re.match(r"^\s*\|.*\|\s*$", line.rstrip("\n")))
        if not is_table_row and deferred_inserts:
            append_images(deferred_inserts)
            deferred_inserts = []

        # Collect all first-seen verified figure references in the current line.
        inserts: list[str] = []
        for m in _FIG_REF_RE.finditer(line):
            for key in _figure_keys_from_reference(m.group("refs"), figure_map):
                img = figure_map[key]
                if key not in injected:
                    injected.add(key)
                    inserts.append(f"![{key}]({img})")
        result.append(line)
        if is_table_row:
            deferred_inserts.extend(inserts)
        else:
            append_images(inserts)
    append_images(deferred_inserts)
    return "".join(result)


def extract_paper_markdown(client, model, clean: CleanResult, title: str = "",
                           max_tokens: int = None, verify: bool = False,
                           max_output_cap: int = None, image_summary: str = None) -> str:
    """单篇全文提取（Markdown 直出）：用 v2 自适应提示词，模型直接输出 Markdown 章节。

    返回 Markdown 字符串（已去除代码块包裹、标题降一级以正确嵌套）。
    verify=True 时追加一次查漏复核（仅补充遗漏项，不重写已有内容）。
    image_summary：可选，图片视觉分析结果，注入提示词供交叉核对（纯文本时为空/None）。
    """
    if max_tokens is None:
        max_tokens = _MAX_TOKENS_BY_CATEGORY.get(clean.length_category, 24000)
    # Markdown 直出比 JSON 更冗长，输出上限适当放宽
    max_tokens = int(max_tokens * 1.5)
    # 输出上限跟随所选模型（不再写死 96000），避免超过该模型的 max_output 触发 400
    if max_output_cap:
        max_tokens = min(max_tokens, int(max_output_cap * 0.9))
    else:
        max_tokens = min(max_tokens, 96000)
    messages = render_material_md_messages(clean, title=title, image_summary=image_summary)
    md = chat(
        client, model, messages,
        temperature=EXTRACT_TEMPERATURE,
        max_tokens=max_tokens,
        response_json=False,
    )
    md = normalize_list_indentation(
        escape_approximate_tildes(_downgrade_headings(_strip_code_fence(md)))
    )
    if verify:
        md = _verify_markdown_once(client, model, clean, md, title=title, max_tokens=max_tokens)
    return normalize_list_indentation(escape_approximate_tildes(md))


def _verify_markdown_once(client, model, clean: CleanResult, base_md: str, title: str = "",
                          max_tokens: int = None) -> str:
    """查漏复核：只找第一轮遗漏条目，以「补充与查漏」小节追加，不重写已有内容。"""
    if max_tokens is None:
        max_tokens = _MAX_TOKENS_BY_CATEGORY.get(clean.length_category, 24000)
    system = (
        "你是材料信息提取的查漏复核引擎。你已对同一篇论文做过第一轮 Markdown 提取，"
        "现在只负责找出第一轮遗漏的材料与性能条目。请只输出【补充内容】："
        "仅包含遗漏项的 Markdown 小节（严格沿用第一轮相同的章节格式、字段规范与测试条件绑定），"
        "不要重复、不要改写已提取内容，不要输出 JSON 或代码块标记。"
        "若确认无遗漏，仅输出「无遗漏」三个字。"
    )
    user = (
        f"论文标题：{title}\n\n"
        "【第一轮提取结果】\n" + (base_md or "") + "\n\n"
        "【任务】对照下方论文全文，只补充第一轮遗漏的：材料体系、成分配比、制备工艺、"
        "微观结构、性能参数（每项必须绑定测试条件并标注出处，如「据图3」「表2」）。\n\n"
        "===== 论文全文开始 =====\n"
        f"{clean.clean_text or ''}\n"
        "===== 论文全文结束 ====="
    )
    extra = chat(
        client, model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=EXTRACT_TEMPERATURE,
        max_tokens=max_tokens,
        response_json=False,
    )
    extra = _downgrade_headings(_strip_code_fence(extra)).strip()
    if not extra or extra == "无遗漏":
        return base_md
    return base_md + "\n\n---\n\n### 补充与查漏\n\n" + extra


def extract_from_clean_markdown(clean: CleanResult, title: str = "", provider: str = None,
                                verify: bool = False, max_tokens: int = None,
                                image_summary: str = None) -> str:
    """便利函数：从 config/models.yaml 创建客户端并以 Markdown 直出模式提取（需已配置 API Key）。"""
    cfg = get_model_config(provider)
    client = create_client(cfg)
    return extract_paper_markdown(client, cfg["model"], clean, title=title,
                                  verify=verify, max_tokens=max_tokens,
                                  max_output_cap=cfg.get("max_output"),
                                  image_summary=image_summary)


# ============ 命令行 ============

def _load_md(path: str) -> CleanResult:
    text = Path(path).read_text(encoding="utf-8")
    return preprocess(text)


def main():
    ap = argparse.ArgumentParser(description="论文材料成分配比与性能参数提取（Markdown 直出，基于项目配置的 AI 模型）")
    ap.add_argument("files", nargs="+", help="marker 产出的论文 md 文件")
    ap.add_argument("--live", action="store_true",
                    help="真实调用 AI（默认仅渲染提示词做离线校验）")
    ap.add_argument("--verify", action="store_true",
                    help="提取后追加一次查漏复核（多一次调用）")
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
        print(f"  检测表格数: {len(clean.tables)}")
        if args.live:
            try:
                md = extract_from_clean_markdown(clean, title=Path(f).stem, verify=args.verify)
            except Exception as e:
                print(f"  [调用失败] {e}")
                continue
            print("  ---- Markdown 直出预览 ----")
            print(md)


if __name__ == "__main__":
    main()
