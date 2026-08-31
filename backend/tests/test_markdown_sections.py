from backend.markdown_utils import escape_approximate_tildes
from backend.main import (
    _new_material_paper,
    _normalize_conclusion_output,
    _normalize_material_output,
    _set_material_part,
    _split_extract_conclusion,
)
from backend.extract import _downgrade_headings


def test_split_extract_conclusion_removes_leaked_conclusion_and_separator():
    combined = """## 一、材料信息完整总结

材料与性能内容。

---
# 一、全文综合结论

结论内容。

# 二、未来研究建议

建议内容。
"""

    part2, part3 = _split_extract_conclusion(combined)

    assert part2 == "## 一、材料信息完整总结\n\n材料与性能内容。"
    assert "全文综合结论" not in part2
    assert part3.startswith("# 一、全文综合结论")
    assert "未来研究建议" in part3


def test_split_accepts_lower_heading_levels_and_combined_heading():
    part2, part3 = _split_extract_conclusion(
        "材料内容\n\n## 三、综合结论与未来建议\n\n结论和建议。"
    )

    assert part2 == "材料内容"
    assert part3.startswith("## 三、综合结论与未来建议")


def test_split_without_reliable_boundary_requests_fallback():
    assert _split_extract_conclusion("材料内容\n\n结论内容但没有标题") == ("", "")


def test_numeric_approximation_tildes_are_escaped_but_intentional_strike_is_preserved():
    source = "约~735%，约 ~3000%，~~明确删除~~，普通~文字\n\n```text\n~123 should stay raw\n```\n"
    escaped = escape_approximate_tildes(source)

    assert r"\~735%" in escaped
    assert r"\~3000%" in escaped
    assert "~~明确删除~~" in escaped
    assert "普通~文字" in escaped
    assert "~123 should stay raw" in escaped


def test_output_normalizers_apply_markdown_cleanup():
    part2 = _normalize_material_output(
        "## 材料\n\n约~735%\n\n# 一、全文综合结论\n\n不应出现在材料部分。"
    )
    part3 = _normalize_conclusion_output("# 一、全文综合结论\n\n约~3000%")

    assert r"\~735%" in part2
    assert r"\~3000%" in part3
    assert "全文综合结论" not in part2


def test_material_part_write_path_always_removes_leaked_conclusion():
    paper = _new_material_paper("doc-1", "测试论文", 0, ["part2"], "run-1")
    _set_material_part(
        paper,
        "part2",
        status="completed",
        content=(
            "## 二、材料与性能信息\n\n"
            "材料参数。\n\n"
            "---\n"
            "# 一、全文综合结论\n\n"
            "不应写入材料部分。\n\n"
            "# 二、未来研究建议\n\n"
            "也不应写入材料部分。"
        ),
    )

    assert paper["parts"]["part2"]["content"] == "## 二、材料与性能信息\n\n材料参数。"
    assert "全文综合结论" not in paper["part2"]
    assert "未来研究建议" not in paper["part2"]


def test_heading_downgrade_clamps_at_markdown_level_six():
    source = "## 二级\n##### 五级\n###### 六级\n####### 七级"

    assert _downgrade_headings(source) == "### 二级\n###### 五级\n###### 六级\n###### 七级"


def test_heading_downgrade_does_not_rewrite_fenced_code():
    source = "## 标题\n\n```markdown\n###### 代码示例\n####### 仍是代码\n```"

    assert _downgrade_headings(source) == "### 标题\n\n```markdown\n###### 代码示例\n####### 仍是代码\n```"
