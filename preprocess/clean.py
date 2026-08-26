"""
preprocess/clean.py —— 论文 MD 预处理模块

在调用 AI 模型之前，对 marker 产出的 MD 进行清洗和结构化解析。
所有操作均为纯文本处理，不依赖 AI 模型。

处理流程：
  1. 图片引用清洗 → 替换 base64/路径引用为占位符
  2. 页眉页码清洗 → 重复行检测 + 孤立数字
  3. 参考文献截断 → 多模式匹配 References 标题
  4. 声明/致谢/作者贡献截断
  5. Email/通讯地址清洗 → 提取为元数据
  6. 章节结构解析 → 提取 ##/### 标题层次
  7. 表格检测与质量评估
  8. 公式检测
  9. 图片 Caption 提取
  10. 字数分级 + 分块决策
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ============ 常量 ============

# 参考文献标题的多模式匹配
REF_PATTERNS = [
    r"^#{1,4}\s.*\bReferences?\b.*$",
    r"^#{1,4}\s.*\bREFERENCES\b.*$",
    r"^#{1,4}\s.*\bBibliography\b.*$",
    r"^#{1,4}\s.*\b参考文献\b.*$",
    r"^#{1,4}\s.*\bLiterature Cited\b.*$",
]

# 声明/致谢等非正文章节的多模式匹配
DECLARATION_PATTERNS = [
    r"^#{1,4}\s*CRediT authorship contribution statement.*$",
    r"^#{1,4}\s*Declaration of competing interest.*$",
    r"^#{1,4}\s*Declaration of generative AI.*$",
    r"^#{1,4}\s*Declaration of (Generative )?AI (and AI-assisted technologies )?in the writing process.*$",
    r"^#{1,4}\s*Acknowledgments?\s*$",
    r"^#{1,4}\s*Acknowledgements?\s*$",
    r"^#{1,4}\s*致谢\s*$",
    r"^#{1,4}\s*Data availability\s*$",
    r"^#{1,4}\s*Supplementary (Material|Data|Information).*$",
    r"^#{1,4}\s*Funding\s*$",
    r"^#{1,4}\s*基金项目\s*$",
    r"^#{1,4}\s*Conflict of Interest\s*$",
    r"^#{1,4}\s*Author Contributions?\s*$",
    r"^#{1,4}\s*作者贡献\s*$",
    r"^#{1,4}\s*Appendix\s*$",
    r"^#{1,4}\s*附录\s*$",
]

# Abstract 标题模式
ABSTRACT_PATTERNS = [
    r"^#{1,4}\s*\**\d*\.?\s*ABSTRACT\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*Abstract\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*abstract\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*摘要\s*\**\s*$",
]

# Conclusion 标题模式
CONCLUSION_PATTERNS = [
    r"^#{1,4}\s*\**\d*\.?\s*Conclusion\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*CONCLUSION\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*Conclusions?\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*Summary and Conclusions?\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*Concluding Remarks?\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*结论\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*总结与展望\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*总结\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*Summary\s*\**\s*$",
]

# Introduction 标题模式
INTRODUCTION_PATTERNS = [
    r"^#{1,4}\s*\**\d*\.?\s*Introduction\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*INTRODUCTION\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*引言\s*\**\s*$",
    r"^#{1,4}\s*\**\d*\.?\s*前言\s*\**\s*$",
]

# section 标题（## 或 ###）
SECTION_PATTERN = re.compile(r"^(#{2,4})\s+(.+)$", re.MULTILINE)

# 图片引用模式
IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# base64 图片
B64_PATTERN = re.compile(r"data:image/[^;\"]+;base64,[A-Za-z0-9+/=]+")

# Email 模式
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# LaTeX 公式模式：支持 $...$、$$...$$、\(...\) 和 \[...\]。
FORMULA_PATTERN = re.compile(
    r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|"
    r"(?<!\\)\$(?!\$)(?:\\.|[^$\n])+(?<!\\)\$(?!\$)"
)

# 孤立数字行（可能是页码）
PAGE_NUM_PATTERN = re.compile(r"^\s*\d{1,4}\s*$")

# Markdown 表格行
TABLE_ROW_PATTERN = re.compile(r"^\|.+\|$")

# 图 Table caption
FIG_CAPTION_PATTERN = re.compile(
    r"\*\*Fig\.?\s*\d+[a-zA-Z]?\.?\*\*[^\n]+"
    r"|Fig\.?\s*\d+[a-zA-Z]?\.?\s+[A-Z][^\n]+"
    r"|\*\*Table\s*\d+\.?\*\*[^\n]+"
    r"|Table\s*\d+\.?\s+[A-Z][^\n]+",
    re.IGNORECASE,
)


@dataclass
class Section:
    """章节信息"""
    level: int           # 标题级别（2=##, 3=###, 4=####）
    title: str           # 标题文本
    start: int           # 起始字符位置
    end: int             # 结束字符位置（下一个同级或更高级标题之前）


@dataclass
class CleanResult:
    """预处理结果"""
    clean_text: str = ""                    # 清洗后的正文
    original_length: int = 0                # 原始字符数
    clean_length: int = 0                   # 清洗后字符数
    sections: list[Section] = field(default_factory=list)  # 章节结构
    abstract_text: str = ""                 # Abstract 段落
    abstract_start: int = -1                # Abstract 起始位置
    conclusion_text: str = ""               # Conclusion 段落
    conclusion_start: int = -1              # Conclusion 起始位置
    ref_start: int = -1                     # 参考文献起始位置
    tables: list[dict] = field(default_factory=list)  # 表格信息
    formulas: list[str] = field(default_factory=list)  # 检测到的公式
    figures: list[dict] = field(default_factory=list)  # 图表 caption
    emails: list[str] = field(default_factory=list)    # Email 地址
    authors_info: str = ""                  # 作者/通讯作者信息
    length_category: str = "short"          # short|medium|large|super_large
    chunk_plan: list[tuple[int, int]] = field(default_factory=list)  # 分块方案（起止位置）


def _match_any(pattern_list: list[str], line: str) -> bool:
    """检测行是否匹配任一模式。"""
    for pat in pattern_list:
        if re.match(pat, line):
            return True
    return False


def clean_images(text: str) -> str:
    """清洗图片引用：base64 替换为 [图片]，外部路径保留文件名提示。"""
    def _replace(m):
        alt = m.group(1) or ""
        url = m.group(2) or ""
        if "base64" in url:
            return "[图片]"
        # 文件路径 → 保留文件名作为提示
        fname = Path(url).name if url else "image"
        return f"[图：{fname}]"
    return IMAGE_PATTERN.sub(_replace, text)


def clean_page_headers(text: str) -> str:
    """清洗页眉页码：删除重复出现 ≥3 次的相同行 + 孤立数字行。"""
    lines = text.split("\n")
    # 统计每行出现次数
    line_counts = {}
    for ln in lines:
        stripped = ln.strip()
        if len(stripped) < 3:
            continue
        line_counts[stripped] = line_counts.get(stripped, 0) + 1

    result = []
    for ln in lines:
        stripped = ln.strip()
        # 跳过重复 ≥3 次的行（页眉）
        if line_counts.get(stripped, 0) >= 3:
            continue
        # 跳过孤立数字（页码）
        if PAGE_NUM_PATTERN.match(stripped) and len(stripped) < 5:
            continue
        result.append(ln)
    return "\n".join(result)


def truncate_references(text: str) -> tuple[str, int]:
    """检测并截断参考文献。返回 (截断后文本, ref 起始位置或 -1)。

    从文档末尾向前搜索，找到最后一个 References 标题行。
    """
    lines = text.split("\n")
    # 从后往前找最后一个 References 行
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i].strip()
        if not ln.startswith("#"):
            continue
        if _match_any(REF_PATTERNS, ln):
            return "\n".join(lines[:i]), i
    return text, -1


def truncate_declarations(text: str) -> str:
    """截断声明/致谢/CRediT 等非正文章节。"""
    lines = text.split("\n")
    cut_at = len(lines)
    for i, ln in enumerate(lines):
        if _match_any(DECLARATION_PATTERNS, ln.strip()):
            cut_at = min(cut_at, i)
    if cut_at < len(lines):
        return "\n".join(lines[:cut_at])
    return text


def extract_emails_and_addresses(text: str) -> tuple[str, list[str], str]:
    """提取 Email 和通讯作者信息，从正文删除。返回 (清洗后文本, emails, authors_info)。"""
    lines = text.split("\n")
    emails = []
    authors_lines = []
    result = []

    for ln in lines:
        stripped = ln.strip()
        found = EMAIL_PATTERN.findall(stripped)
        if found:
            emails.extend(found)
            authors_lines.append(stripped)
            # 删除这行（email 行不参与后续提取）
            continue
        # 检测通讯作者标记行
        if re.match(r".*\bcorresponding author\b.*", stripped, re.IGNORECASE):
            authors_lines.append(stripped)
            continue
        if re.match(r".*\b通讯作者\b.*", stripped):
            authors_lines.append(stripped)
            continue
        # 跳过纯单位地址行（含邮政编码+国家的短行）
        if re.match(r".*\b\d{5,6}\b.*\bChina\b.*", stripped) and len(stripped) < 100:
            authors_lines.append(stripped)
            continue
        result.append(ln)

    return "\n".join(result), emails, "\n".join(authors_lines)


def parse_sections(text: str) -> list[Section]:
    """解析章节结构：提取所有 ##/###/#### 标题及其位置。"""
    sections = []
    for m in SECTION_PATTERN.finditer(text):
        level = len(m.group(1))  # ## = 2, ### = 3, #### = 4
        title = m.group(2).strip()
        sections.append(Section(
            level=level,
            title=title,
            start=m.start(),
            end=-1,  # 稍后填充
        ))
    # 填充 end 位置（到下一个同级或更高级标题）
    for i, sec in enumerate(sections):
        if i + 1 < len(sections):
            sec.end = sections[i + 1].start
        else:
            sec.end = len(text)
    return sections


def extract_abstract(text: str, sections: list[Section]) -> str:
    """从文本中提取 Abstract 段落。"""
    # 方法 1：按标题模式匹配
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if _match_any(ABSTRACT_PATTERNS, ln.strip()):
            # 从标题后取到下一个 ## 或 2000 字
            abstract_lines = []
            for j in range(i + 1, len(lines)):
                if re.match(r"^#{2,4}\s", lines[j]):
                    break
                abstract_lines.append(lines[j])
            abstract = "\n".join(abstract_lines).strip()
            if len(abstract) > 50:
                return abstract[:3000]  # Abstract 一般不超过 3000 字

    # 方法 2：找 Introduction 前的长段落
    intro_pos = -1
    for sec in sections:
        if _match_any(INTRODUCTION_PATTERNS, sec.title):
            intro_pos = sec.start
            break
    if intro_pos > 100:
        pre_intro = text[max(0, intro_pos - 5000):intro_pos]
        # 取最后一个长段落
        paragraphs = pre_intro.split("\n\n")
        for p in reversed(paragraphs):
            p = p.strip()
            if 200 < len(p) < 5000 and not p.startswith("#"):
                return p

    return ""


def extract_conclusion(text: str, sections: list[Section]) -> tuple[str, int]:
    """提取 Conclusion 段落及其起始位置。"""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if _match_any(CONCLUSION_PATTERNS, ln.strip()):
            conc_lines = []
            for j in range(i + 1, len(lines)):
                if _match_any([r"^#{2,4}\s"], lines[j]) and not _match_any(CONCLUSION_PATTERNS, lines[j]):
                    break
                conc_lines.append(lines[j])
            return "\n".join(conc_lines).strip(), i
    return "", -1


def detect_tables(text: str) -> list[dict]:
    """检测 markdown 表格并评估质量。"""
    lines = text.split("\n")
    tables = []
    in_table = False
    table_lines = []
    table_start = 0

    for i, ln in enumerate(lines):
        is_table_row = TABLE_ROW_PATTERN.match(ln.strip())
        if is_table_row:
            if not in_table:
                in_table = True
                table_start = i
                table_lines = []
            table_lines.append(ln.strip())
        else:
            if in_table:
                # 结束当前表格
                in_table = False
                if len(table_lines) >= 2:
                    # 检查列数一致性
                    col_counts = [len(row.split("|")) for row in table_lines]
                    is_clean = len(set(col_counts)) == 1
                    tables.append({
                        "start_line": table_start,
                        "end_line": i - 1,
                        "rows": len(table_lines),
                        "columns": col_counts[0] - 2 if col_counts else 0,  # 去掉首尾空列
                        "is_clean": is_clean,
                    })
    return tables


def detect_formulas(text: str) -> list[str]:
    """检测并去重保留正文中的 LaTeX 公式。"""
    formulas = []
    seen = set()
    for m in FORMULA_PATTERN.finditer(text):
        formula = m.group().strip()
        if formula and formula not in seen:
            seen.add(formula)
            formulas.append(formula)
    return formulas


def extract_figure_captions(text: str) -> list[dict]:
    """提取图片和表格的 Caption。"""
    captions = []
    for m in FIG_CAPTION_PATTERN.finditer(text):
        cap = m.group().strip()
        if len(cap) > 10:
            fig_id = ""
            fig_match = re.match(r"\*{0,2}(Fig\.?\s*\d+[a-zA-Z]?\.?|Table\s*\d+\.?)\*{0,2}", cap)
            if fig_match:
                fig_id = fig_match.group(1).strip("*").strip()
            captions.append({"id": fig_id, "caption": cap[:500]})
    return captions


def classify_length(char_count: int) -> str:
    """按字符数分级。"""
    if char_count < 15000:
        return "short"
    elif char_count < 100000:
        return "medium"
    elif char_count < 1500000:
        return "large"
    else:
        return "super_large"


def decide_chunking(text: str, sections: list[Section], length_cat: str) -> list[tuple[int, int]]:
    """根据字数级别决定分块方案，返回 (起始位置, 结束位置) 列表。"""
    if length_cat == "short":
        # 全量，不分块
        return [(0, len(text))]

    if length_cat == "medium":
        # 按 section 分块，每块尽量不超过 80000 字符
        return _chunk_by_sections(text, sections, max_chars=80000)

    # large / super_large：分层提取
    # L1：Abstract + Introduction + Conclusion（概览）
    # L2：每 section 独立
    chunks = []
    for sec in sections:
        sec_text = text[sec.start:sec.end]
        if len(sec_text) > 80000:
            # 超长 section 再按段落切
            chunks.append((sec.start, sec.start + 80000))
        else:
            chunks.append((sec.start, sec.end))
    return chunks


def _chunk_by_sections(text: str, sections: list[Section], max_chars: int) -> list[tuple[int, int]]:
    """按 section 边界分块，每块不超过 max_chars。"""
    chunks = []
    buf_start = 0
    buf_end = 0

    for sec in sections:
        sec_len = sec.end - sec.start
        if (buf_end - buf_start) + sec_len > max_chars and buf_end > buf_start:
            chunks.append((buf_start, buf_end))
            buf_start = sec.start
            buf_end = sec.end
        else:
            if buf_end == 0:
                buf_start = sec.start
            buf_end = sec.end

    if buf_end > buf_start:
        chunks.append((buf_start, buf_end))

    # 如果没有 section 或分块为空，退化为全量
    if not chunks:
        chunks = [(0, len(text))]
    return chunks


# ============ 主入口 ============

def preprocess(raw_md: str) -> CleanResult:
    """完整预处理流水线。每步独立运行，失败不影响其他步骤。"""
    result = CleanResult()
    result.original_length = len(raw_md)
    text = raw_md

    # 1. 清洗图片引用
    text = clean_images(text)

    # 2. 清洗页眉页码
    text = clean_page_headers(text)

    # 3. 截断参考文献
    text, result.ref_start = truncate_references(text)

    # 4. 截断声明/致谢
    text = truncate_declarations(text)

    # 5. 提取 Email 和地址
    text, result.emails, result.authors_info = extract_emails_and_addresses(text)

    # 6. 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 7. 章节结构解析
    result.sections = parse_sections(text)

    # 8. 提取 Abstract
    result.abstract_text = extract_abstract(text, result.sections)

    # 9. 提取 Conclusion
    result.conclusion_text, result.conclusion_start = extract_conclusion(text, result.sections)

    # 10. 表格检测
    result.tables = detect_tables(text)

    # 11. 公式检测
    result.formulas = detect_formulas(text)

    # 12. 图表 Caption
    result.figures = extract_figure_captions(text)

    result.clean_text = text.strip()
    result.clean_length = len(result.clean_text)

    # 13. 字数分级
    result.length_category = classify_length(result.clean_length)

    # 14. 分块方案
    result.chunk_plan = decide_chunking(text, result.sections, result.length_category)

    return result
