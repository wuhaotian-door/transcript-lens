"""v1.9 表格干扰过滤回归测试。

背景（Owner 实测反馈）：真实的教务导出表里除了学生成绩，还夹着三类"不是学生"
的东西，修复前它们会被当成课程和学生吃掉：

1. **统计列** —— 「不及格门数 / 平均分 / 总分 / 平均学分绩 / 平均学分绩点 /
   排名」这类**算出来的**汇总，修复前每一列都被当成一门课，于是这些数字
   混进加权均分的分母（口径直接算错，且完全静默）。
2. **末尾汇总行** —— 「37人 470 2485 …」「不及格 18 6 0 …」。修复前
   姓名列取不到就一路兜底到"第一个非空单元格"，于是"37人"和"不及格"
   变成了两个学生。
3. **顶部大标题** —— 跨列合并的大标题。修复前它可能被当成表头行，
   白吃掉 MAX_HEADER_ROWS 的一格。

这个文件只走内存 DataFrame（不读 xlsx、不碰加密/UI/AI），和 tests/test_calc.py
同一个路数。★ 注意 core/parser.py 是封板文件：v1.9 解冻它是**有意**的，理由与
新哈希记录在 main.FROZEN_FILE_SHA256 上方的注释里。
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.parser import ParseResult, is_stat_header, parse_dataframe

# ---------------------------------------------------------------------------
# 造一张和 Owner 实测那张表**同构**的表
# ---------------------------------------------------------------------------

TITLE = "2024-2025学年第一学期学生成绩汇总表"
HEADERS = [
    "学号", "姓名",
    "高等数学(4学分)必修", "大学英语(3学分)必修", "体育(1学分)选修",
    "不及格门数", "平均分", "总分", "所得学分", "平均学分绩", "平均学分绩点", "排名",
]


def messy_frame() -> pd.DataFrame:
    """顶部合并大标题 + 3 门真课 + 6 个统计列 + 3 个学生 + 2 行汇总。"""
    return pd.DataFrame([
        [TITLE, None, None, None, None, None, None, None, None, None, None, None],
        HEADERS,
        ["2023001", "张三", 90, 85, 88, 0, 87.67, 263, 8, 87.67, 3.5, 1],
        ["2023002", "李四", 55, 70, 60, 1, 61.67, 185, 8, 61.67, 1.8, 3],
        ["2023003", "王五", 78, 82, None, 0, 80.0, 160, 7, 80.0, 2.9, 2],
        ["37人", None, 470, 237, 148, None, None, 2485, 37, None, None, None],
        ["不及格", None, 18, 6, 0, None, None, None, None, None, None, None],
    ])


@pytest.fixture()
def messy() -> ParseResult:
    return parse_dataframe(messy_frame())


# ---------------------------------------------------------------------------
# 一、统计列：不许变成课程，不许混进加权分母
# ---------------------------------------------------------------------------


def test_stat_columns_are_not_courses(messy: ParseResult) -> None:
    assert [c.name for c in messy.courses] == ["高等数学", "大学英语", "体育"]


def test_stat_columns_recorded(messy: ParseResult) -> None:
    # 第 5、6、7、9、10、11 列（0-based）。第 8 列「所得学分」走的是**锚点列**
    # 那条路（ANCHOR_HEADER_KEYWORDS 里已有"所得学分"），不重复登记。
    assert messy.stat_columns == [5, 6, 7, 9, 10, 11]
    assert 8 in messy.anchor_columns
    assert not set(messy.stat_columns) & set(messy.anchor_columns)


def test_each_stat_column_leaves_a_warning(messy: ParseResult) -> None:
    """排除统计列必须留痕：辅导员要知道"这几列被丢掉了、为什么"。"""
    hits = [w for w in messy.warnings if "是统计列" in w]
    assert len(hits) == len(messy.stat_columns)
    assert any("不及格门数" in w for w in hits)
    assert any("不计入加权分母" in w for w in hits)


def test_stat_values_do_not_enter_weighted_denominator(messy: ParseResult) -> None:
    """口径命门：统计列的分数不许出现在任何一个学生的成绩字典里。"""
    for row in messy.rows:
        for stat in ("不及格门数", "平均分", "总分", "平均学分绩", "平均学分绩点", "排名"):
            assert stat not in row.scores


# ---------------------------------------------------------------------------
# 二、末尾汇总行：不许变成学生
# ---------------------------------------------------------------------------


def test_summary_rows_are_not_students(messy: ParseResult) -> None:
    assert [r.student for r in messy.rows] == ["张三", "李四", "王五"]
    assert [r.student_id for r in messy.rows] == ["2023001", "2023002", "2023003"]


def test_summary_rows_recorded_with_reason(messy: ParseResult) -> None:
    assert messy.skipped_rows == [
        (5, "「37人」是人数统计"),
        (6, "「不及格」含汇总词「不及格」"),
    ]


def test_skipping_is_announced(messy: ParseResult) -> None:
    """跳过行必须留痕 —— 静默丢数据比丢数据更危险。"""
    joined = " ".join(messy.warnings)
    assert "已跳过 2 行统计/汇总行" in joined
    assert "不是学生成绩" in joined


def test_rows_start_after_title_and_header(messy: ParseResult) -> None:
    assert messy.header_rows == [1]
    assert messy.data_start_row == 2


def test_no_false_column_mismatch(messy: ParseResult) -> None:
    """统计列被判为课程时会顺手让每一行都"错位"；排除后这类误报必须消失。"""
    assert not any(r.has_column_mismatch for r in messy.rows)


def test_selected_and_missing_are_different(messy: ParseResult) -> None:
    """空 = 没选这门课，不是 0 分。修复不该把这条老规矩带坏。"""
    wangwu = messy.rows[2]
    assert wangwu.scores["体育"] is None
    assert wangwu.scores["高等数学"] == 78.0


def test_still_asks_for_manual_review(messy: ParseResult) -> None:
    """过滤掉干扰之后仍然要回显给辅导员确认（从来不是"静默相信解析器"）。"""
    assert messy.needs_manual_review


# ---------------------------------------------------------------------------
# 三、顶部大标题
# ---------------------------------------------------------------------------


def test_merged_title_row_is_not_a_header() -> None:
    df = pd.DataFrame([
        ["XX大学学生成绩表", None, None, None],
        ["姓名", "学号", "高等数学(4学分)必修", "备注"],
        ["张三", "2023001", 90, None],
    ])
    result = parse_dataframe(df)
    # 大标题里带"学生"二字，修复前会被 _is_subject_header 命中而占据表头行
    assert result.header_rows == [1]
    assert [c.name for c in result.courses] == ["高等数学"]
    assert [r.student for r in result.rows] == ["张三"]


def test_title_row_does_not_eat_header_budget() -> None:
    """MAX_HEADER_ROWS = 3：大标题不许白吃一格，否则真表头就被挤出去了。"""
    df = pd.DataFrame([
        ["成绩单", None, None],
        ["学号", "姓名", "高等数学(4学分)必修"],
        ["2023001", "张三", 90],
    ])
    result = parse_dataframe(df)
    assert result.header_rows == [1]
    assert len(result.rows) == 1 and result.rows[0].scores["高等数学"] == 90.0


# ---------------------------------------------------------------------------
# 四、反向回归：过滤不能误伤正常表
# ---------------------------------------------------------------------------


def test_plain_sheet_untouched() -> None:
    df = pd.DataFrame([
        ["姓名", "学号", "高等数学(4学分)必修", "体育(1学分)选修"],
        ["张三", "2023001", 90, 88],
    ])
    result = parse_dataframe(df)
    assert [c.name for c in result.courses] == ["高等数学", "体育"]
    assert result.stat_columns == []
    assert result.skipped_rows == []
    assert len(result.rows) == 1 and result.rows[0].student == "张三"


def test_narrow_sheet_without_identity_columns_keeps_rows() -> None:
    """没有学号/姓名列的窄表：不能因为"缺身份标识"就被整表判死。

    这条是那道新判据的**守卫**：判据只在表里确实有学号或姓名列时才生效。
    """
    df = pd.DataFrame([
        ["课程", "学分", "成绩"],
        ["高等数学", 4, 90],
    ])
    result = parse_dataframe(df)
    assert len(result.rows) == 1
    assert result.rows[0].scores == {"成绩": 90.0}


def test_blank_marker_zero_is_still_a_score() -> None:
    """0 分是缺考要计入，不是空值 —— BLANK_MARKERS 里没有"0"。"""
    df = pd.DataFrame([
        ["姓名", "学号", "高等数学(4学分)必修"],
        ["张三", "2023001", 0],
    ])
    result = parse_dataframe(df)
    assert result.rows[0].scores["高等数学"] == 0.0


# ---------------------------------------------------------------------------
# 五、判据本身
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "不及格门数", "不及格", "平均分", "平均成绩", "总分", "总评", "合计", "小计",
    "平均学分绩", "平均学分绩点", "绩点", "GPA", "排名", "名次", "rank",
    "  平 均 分  ",  # normalize_header 会把空白抹掉
])
def test_is_stat_header_hits(text: str) -> None:
    assert is_stat_header(text)


@pytest.mark.parametrize("text", [
    "姓名", "学号", "班级", "高等数学(4学分)必修", "大学英语", "体育",
    "所得学分", "备注", "任课教师",
])
def test_is_stat_header_misses_real_headers(text: str) -> None:
    assert not is_stat_header(text)


@pytest.mark.parametrize("value", [None, 0, 90, 87.67, 37, True, ["排名"]])
def test_is_stat_header_ignores_non_strings(value: object) -> None:
    """数据区里的数字绝不能被判成统计列 — normalize_header 对非字符串返回空串。"""
    assert not is_stat_header(value)  # type: ignore[arg-type]


def test_parse_result_new_fields_have_defaults() -> None:
    """新字段追加在最后：全仓库都靠无参 ParseResult() 构造，不能有必填项。"""
    fresh = ParseResult()
    assert fresh.stat_columns == [] and fresh.skipped_rows == []
