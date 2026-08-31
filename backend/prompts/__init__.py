"""prompts 包 —— 提示词加载与消息渲染（统一来源：material_master_prompt.md）。

所有渲染函数共用同一份 master 提示词 material_master_prompt.md，由顶部 mode= 路由到
对应任务（summary / extract / conclusion / extract_conclusion），实现「单一事实源」；
改 master .md 即对所有入口（CLI 与网页管线）同时生效，无需任何模板渲染引擎。

提供：
  - summary_mode(clean_result) -> "precise" | "locate"
        根据预处理结果判断走“精准模式”还是“定位模式”。
  - render_summary_messages(clean_result, title=None) -> list[dict]
        返回 [system_message, user_message]；系统提示词 = master（mode=summary），
        用户消息按精准/定位模式在代码内拼装，模型直接输出 Markdown 章节
        （## 摘要总结 / ## 结论总结）。
  - render_material_md_messages(clean_result, title=None, image_summary=None) -> list[dict]
        材料提取（Markdown 直出）消息：系统提示词 = master（mode=extract），
        用户消息仅投喂标题与全文；可选 image_summary 注入图片视觉分析结果。
  - render_conclusion_messages(clean_result, title=None, image_summary=None) -> list[dict]
        全文综合结论与未来研究建议（Markdown 直出）消息：系统提示词 = master
        （mode=conclusion），用户消息投喂标题与全文；可选 image_summary 注入。
  - render_extract_conclusion_messages(clean_result, title=None, image_summary=None) -> list[dict]
        材料提取 + 全文结论 合并单次调用（Markdown 直出）：系统提示词 = master
        （mode=extract_conclusion），全文只读一次；可选 image_summary 注入。
  - build_extract_conclusion_calls(clean_result, title=None, *, force_split=False, image_summary=None)
        -> (list[list[dict]], merged: bool)
        自适应合并入口：默认返回 1 组消息（合并单次调用）；全文超阈值时自动
        退回 2 组消息（分别走 master 的 mode=extract / mode=conclusion 独立调用）。

精准模式：preprocess 已切出 abstract_text 与 conclusion_text，直接投喂切片。
定位模式：切片缺失时，投喂 clean_text 的头部 6000 字 + 尾部 6000 字
          （摘要必在头、结论必在尾，仍不用全文），由模型先定位再总结。

注意：image_summary 仅注入材料提取与全文结论（part2/part3），**不注入** part1(summarize)，
因为 summarize 只投喂 abstract+conclusion 切片，无需看图。
"""

from __future__ import annotations

from pathlib import Path

from backend.preprocess.clean import CleanResult

_PROMPT_DIR = Path(__file__).parent

# —— Markdown 直出提示词文件（统一来源：master，含 summary/extract/conclusion/extract_conclusion 四模式）——
_MASTER_MD = _PROMPT_DIR / "material_master_prompt.md"

# 自适应合并阈值：全文（clean_text）字符数超过此值，extract+conclusion 退回两次分开调用。
#
# 设定依据（模型输出 token 上限 = 131072）：
#   - 合并单次输出（材料提取 + 全文结论）最坏情况也极少超过数万字符；131072 tokens
#     约等价于 13~20 万字符的输出预算，因此「输出截断」已不再是主要风险。
#   - 该阈值现主要作为「超长论文质量安全网」：全文超过此值（多见于长综述 / 多样品
#     巨量数据论文，通常 >3~5 万字）时拆成两次独立全文调用，避免单次过长导致注意力
#     分散、提取漏点或结论泛化。普通实验论文（全文约 8k~20k 字符）一律走合并单次调用。
#   - 可按实际论文体量、模型上下文窗口与质量观测微调；仅作质量兜底，不影响正确性。
MERGE_CHAR_THRESHOLD = 50000

# 定位模式投喂的字符窗口：摘要必在头、结论必在尾，仍不用全文
HEAD_CHARS = 6000
TAIL_CHARS = 6000


def _read_md(path: Path) -> str:
    """读取 .md 系统提示词（进程内缓存，首次读取后不再重读，改 .md 需重启进程）。"""
    cache = getattr(_read_md, "_cache", None)
    if cache is None:
        cache = {}
        _read_md._cache = cache
    key = str(path)
    if key not in cache:
        cache[key] = path.read_text(encoding="utf-8") if path.exists() else ""
    return cache[key]


def _derive_title(clean: CleanResult) -> str:
    """从清洗后正文首行推断论文标题（marker 输出首行常为标题）。"""
    for line in (clean.clean_text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:200]
    return ""


def _formula_inventory(formulas: list[str] | None) -> str:
    """提供正文公式清单，帮助模型核对并原样保留关键 LaTeX。"""
    formulas = formulas or []
    if not formulas:
        return ""
    items = [f"[{index}] {formula}" for index, formula in enumerate(formulas[:80], start=1)]
    omitted = len(formulas) - len(items)
    suffix = f"\n（另有 {omitted} 个公式仍保留在下方全文中。）" if omitted else ""
    return (
        "===== 检测到的 LaTeX 公式（来自正文，供提取核对）=====\n"
        + "\n".join(items)
        + suffix
        + "\n===== LaTeX 公式清单结束 =====\n\n"
    )


def _with_image_summary(user_core: str, image_summary, clean_text: str,
                        formulas: list[str] | None = None) -> str:
    """在「论文全文」块之前插入可选的图片视觉分析结果和公式清单。

    image_summary 为空 / None 时不注入（纯文本模式）。
    """
    block = user_core
    if image_summary:
        block += (
            "===== 图片视觉分析结果（视觉专家模型预先生成，供交叉核对图文一致性、"
            "补充图中微观结构/曲线趋势等仅图片可见的信息）=====\n"
            f"{image_summary}\n"
            "===== 图片视觉分析结果结束 =====\n\n"
        )
    block += _formula_inventory(formulas)
    block += (
        "===== 论文全文开始 =====\n"
        f"{clean_text or ''}\n"
        "===== 论文全文结束 ====="
    )
    return block


def summary_mode(clean: CleanResult) -> str:
    """两切片都非空 → 精准模式；否则 → 定位模式。"""
    abstract = (clean.abstract_text or "").strip()
    conclusion = (clean.conclusion_text or "").strip()
    return "precise" if (abstract and conclusion) else "locate"


def render_summary_messages(clean: CleanResult, title: str | None = None) -> list[dict]:
    """[system, user]，系统提示词 = master（mode=summary），模型直接输出
    Markdown 章节（## 摘要总结 / ## 结论总结）。

    精准模式：preprocess 已切出 abstract_text 与 conclusion_text，直接投喂切片。
    定位模式：切片缺失时，投喂 clean_text 的头部 + 尾部，由模型先定位再总结。

    注：part1 不使用 image_summary（只用 abstract+conclusion 切片）。
    """
    title = (title or "").strip() or _derive_title(clean)
    system = _read_md(_MASTER_MD)
    if summary_mode(clean) == "precise":
        abstract = (clean.abstract_text or "").strip()
        conclusion = (clean.conclusion_text or "").strip()
        user = (
            f"<paper_title>{title}</paper_title>\n"
            f"<abstract>{abstract}</abstract>\n"
            f"<conclusion>{conclusion}</conclusion>\n\n"
            "说明：以上 <abstract> 与 <conclusion> 已是论文对应的原文片段，请严格按照上方"
            "【系统提示词】中 mode=summary 的规范，直接输出 Markdown 章节"
            "（## 摘要总结 / ## 结论总结），不要额外查找其它内容、不要输出 JSON 或代码块"
            "标记、不要写额外解释。"
        )
    else:
        text = clean.clean_text or ""
        head = text[:HEAD_CHARS]
        tail = text[-TAIL_CHARS:] if len(text) > TAIL_CHARS else ""
        user = (
            f"<paper_title>{title}</paper_title>\n"
            f"<paper_head>{head}</paper_head>\n"
            f"<paper_tail>{tail}</paper_tail>\n\n"
            "说明：以上文本是论文“正文开头部分”（摘要通常在此）与“正文结尾部分”"
            "（结论通常在此）的原文。请严格按照上方【系统提示词】中 mode=summary 的规范，"
            "先在其中定位摘要(abstract)与结论(conclusion)的起止，再分别总结并直接输出"
            'Markdown 章节（## 摘要总结 / ## 结论总结），不要输出 JSON 或代码块标记、'
            '不要写额外解释；若某部分确无对应内容，将对应章节写"无此内容"。'
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def render_material_md_messages(clean: CleanResult, title: str | None = None,
                                image_summary: str | None = None) -> list[dict]:
    """材料提取（Markdown 直出）消息，系统提示词 = master（mode=extract）。

    直接驱动模型输出「## 一、材料信息完整总结 / ## 二、性能参数完整总结」章节；
    用户消息提供论文标题与全文，并约束不要输出 JSON / 代码块 / 额外解释。
    可选 image_summary 注入图片视觉分析结果（在全文之前）。
    """
    title = (title or "").strip() or _derive_title(clean)
    system = _read_md(_MASTER_MD)
    user_core = (
        f"论文标题：{title}\n\n"
        "请严格按照上方【系统提示词】中 mode=extract 的规范，从下面这篇论文全文中提取全部材料信息与性能参数，"
        "直接输出 Markdown 章节（不要输出 JSON、不要加 ``` 代码块标记、不要写任何额外解释，"
        "也不要重复系统提示词中的规则说明或示例）。\n\n"
    )
    user = _with_image_summary(user_core, image_summary, clean.clean_text, clean.formulas)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def render_conclusion_messages(clean: CleanResult, title: str | None = None,
                               image_summary: str | None = None) -> list[dict]:
    """全文综合结论与未来研究建议（Markdown 直出）消息，系统提示词 = master（mode=conclusion）。

    用户消息提供论文标题与全文，约束直接输出 Markdown 章节。
    可选 image_summary 注入图片视觉分析结果（在全文之前）。
    """
    title = (title or "").strip() or _derive_title(clean)
    system = _read_md(_MASTER_MD)
    user_core = (
        f"论文标题：{title}\n\n"
        "请严格按照上方【系统提示词】中 mode=conclusion 的规范，对下面这篇论文全文生成「全文综合结论」与"
        "「未来研究建议」，直接输出 Markdown 章节（不要输出 JSON、不要加 ``` 代码块标记、"
        "不要写任何额外解释，也不要重复系统提示词中的规则说明或示例）。\n\n"
    )
    user = _with_image_summary(user_core, image_summary, clean.clean_text)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def render_extract_conclusion_messages(clean: CleanResult, title: str | None = None,
                                       image_summary: str | None = None) -> list[dict]:
    """材料提取 + 全文结论 合并单次调用（mode=extract_conclusion）。

    系统提示词 = material_master_prompt.md；用户消息投喂标题与全文一次，
    模型在同一次回复中依次产出【任务二 材料提取】与【任务三 全文结论】两部分，
    全文只读一次。长文场景请改用 build_extract_conclusion_calls 做自适应回退。
    可选 image_summary 注入图片视觉分析结果（在全文之前）。
    """
    title = (title or "").strip() or _derive_title(clean)
    system = _read_md(_MASTER_MD)
    user_core = (
        f"论文标题：{title}\n\n"
        "请严格按照上方【系统提示词】中 mode=extract_conclusion 的规范，从下面这篇论文全文"
        "一次性产出「材料与性能信息提取」与「全文综合结论与未来研究建议」两部分："
        "先输出材料提取，空一行后输出全文结论与建议；直接输出 Markdown 章节"
        "（不要输出 JSON、不要加 ``` 代码块标记、不要写任何额外解释，"
        "也不要重复系统提示词中的规则说明或示例）。\n\n"
    )
    user = _with_image_summary(user_core, image_summary, clean.clean_text, clean.formulas)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_extract_conclusion_calls(
    clean: CleanResult, title: str | None = None, *, force_split: bool = False,
    image_summary: str | None = None,
) -> tuple[list[list[dict]], bool]:
    """自适应合并：返回需要依次发送的 messages 列表，以及是否走了合并单次调用。

    - 默认（全文未超阈值）：返回长度为 1 的列表，元素为 render_extract_conclusion_messages
      （一次全文调用，mode=extract_conclusion），全文只读一次。image_summary 透传注入。
    - 超阈值或 force_split=True：返回长度为 2 的列表，依次调用
      render_material_md_messages（master, mode=extract）与 render_conclusion_messages
      （master, mode=conclusion），各自仍是全文，拆成两次独立调用兜底。image_summary 透传注入。

    调用方按返回列表逐个发送即可；merged 标记可用于日志/前端区分「合并」与「拆分」。
    阈值见模块级常量 MERGE_CHAR_THRESHOLD。
    """
    text_len = len(clean.clean_text or "")
    merged = (not force_split) and text_len <= MERGE_CHAR_THRESHOLD
    if merged:
        return [render_extract_conclusion_messages(clean, title, image_summary=image_summary)], True
    return [
        render_material_md_messages(clean, title, image_summary=image_summary),
        render_conclusion_messages(clean, title, image_summary=image_summary),
    ], False
