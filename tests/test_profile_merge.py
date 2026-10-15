"""档案合并 / 按学期分组 / 空 ≠ 0 / 侧写正文 —— v1.9 二期 D 任务判据（AGENTS §35）。

三块判据，各自独立：

1. **同学生同学期重复导入**：``core.archival.build_archive`` 把两次都留着
   （**绝不静默丢掉一次导入**）并记一条中文警告；「成绩明细」标签页把两份都显示
   出来，而「总评与侧写」的合并成绩是**同名课取高、学分只算一次**。
2. **档案页按学期分组**：顺序取自 ``core.archival.SEMESTER_ORDER``（不另抄一份表），
   最新（终结）学期排最前、认不出的学期排最后；组内按分数降序、没分数的排最后；
   行内 **空 ≠ 0** —— 空白就是空白，``0`` 是缺考（红线 2）。
3. **侧写正文**：正文存 ``student_profiles.json.enc``（磁盘上只有密文，
   明文一个字都扫不到），没有正文时「AI 侧写」按钮显示「点击生成」占位。

★★ 会走到模态弹窗的路径**一律**把 ``QMessageBox.information`` 换成探针：
   模态弹窗在无头环境里会把测试挂住（AGENTS §32 记着这个教训）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

# ★ 必须在 import PySide6 / 建任何控件之前设好 —— offscreen 平台插件
#   不需要真实显示器；测试进程里没有它会在 QApplication() 那一步直接退出。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.archival import (  # noqa: E402
    SEMESTER_ORDER,
    archive_key,
    build_archive,
    merge_records,
    semester_rank,
    term_average,
)
from core.crypto_store import CryptoStore  # noqa: E402
from core.database import ProfileStore  # noqa: E402
from core.parser import Category  # noqa: E402
from core.scholarship import save_moral  # noqa: E402
from ui.pages.profile_page import (  # noqa: E402
    SIDE_TEXT_EMPTY_HINT,
    ProfilePage,
    build_side_text_prompt,
    format_credit,
    format_score,
    grade_rows_from_records,
    group_grades_by_term,
    moral_of,
    read_side_text,
)

USER = "张老师"
PASSWORD = "sch12345"

STUDENT = "张三"
STUDENT_ID = "2023001"
KEY = archive_key(STUDENT_ID, STUDENT)

MATH = ("高等数学", 4.0, Category.REQUIRED)
PHYS = ("大学物理", 3.0, Category.REQUIRED)
ENG = ("专业英语", 2.0, Category.LIMITED)

TERM_A = "大一上"
TERM_B = "大一下"

SIDE_TEXT = "这名学生本学期稳步上升，建议保持当前节奏，下学期重点补英语。"


# ---------------------------------------------------------------------------
# 假对象：build_archive 只读 parsed.courses / parsed.rows / parsed.missing_student_id
# ---------------------------------------------------------------------------


class _Course:
    """够 ``build_archive`` 用的课程：它只取 ``name`` / ``credit`` / ``category.value``。"""

    def __init__(self, name: str, credit: float, category: Category) -> None:
        self.name = name
        self.credit = credit
        self.category = category


class _Row:
    def __init__(self, student: str, student_id: str, scores: list[Any]) -> None:
        self.student = student
        self.student_id = student_id
        self.score_list = list(scores)
        self.is_valid = True
        self.warnings: list[str] = []


class _Parsed:
    def __init__(self, courses: list[_Course], rows: list[_Row],
                 missing_student_id: list[str] | None = None) -> None:
        self.courses = list(courses)
        self.rows = list(rows)
        self.missing_student_id = list(missing_student_id or [])


def _parsed(courses: list[tuple[str, float, Category]], scores: list[Any], *,
            student: str = STUDENT, student_id: str = STUDENT_ID) -> _Parsed:
    course_objs = [_Course(name, credit, category) for name, credit, category in courses]
    row = _Row(student, student_id, scores)
    return _Parsed(course_objs, [row])


def _student_of(archive: Any, student: str = STUDENT,
                student_id: str = STUDENT_ID) -> Any:
    return archive.get(archive_key(student_id, student))


def _payload(student_archive: Any = None, *, student: str = STUDENT,
             student_id: str = STUDENT_ID) -> dict[str, Any]:
    """档案页真正吃的学生包（主窗 ``_payload_for`` 那种形状）。

    ★ 键一律由 ``archive_key(student_id, student)`` 推出来，**不抄归档对象上的
    ``key``** —— 换人时键一定跟着换，这正是"不串号"的判据；``student_archive=None``
    表示这名学生还没有归档。
    """
    return {
        "student": student,
        "student_id": student_id,
        "key": archive_key(student_id, student),
        "results": {},
        "archive": student_archive,
    }


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> CryptoStore:
    """临时加密库。★ **必须先 register**，否则 user_file 解析不到 uid。

    ★ 夹具名**不能叫 ``store``** —— 那会在模块级遮蔽 ``core.crypto_store.CryptoStore``
      （``ProfileStore(store=store, ...)`` 就会把夹具**函数**当库传进去）。第一版
      正是这么写的，单独跑本文件时看不出来，两个文件一起跑才炸成 KeyError。
    """
    box = CryptoStore(root=tmp_path / "vault")
    box.register(USER, PASSWORD)
    return box


@pytest.fixture(scope="module")
def qapp() -> Any:
    """整个模块共用一个 QApplication（offscreen）。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def page(vault: CryptoStore, qapp: Any) -> Any:
    widget = ProfilePage(store=vault)
    widget.set_user(USER)
    yield widget
    widget.deleteLater()
    qapp.processEvents()


# ---------------------------------------------------------------------------
# 一、同学生同学期重复导入
# ---------------------------------------------------------------------------


def test_same_term_import_twice_keeps_both_records() -> None:
    """重复导入同一个学期：两份记录都留着，只在 warnings 里提示一句。"""
    first = build_archive(_parsed([MATH], [76.0]), TERM_A, source="第一次.xlsx")
    second = build_archive(_parsed([MATH], [91.0]), TERM_A,
                           archive=first, source="第二次.xlsx")

    student = _student_of(second)
    assert student is not None
    assert len(student.records) == 2
    assert [record.term for record in student.records] == [TERM_A, TERM_A]
    assert second.imported_terms[KEY] == [TERM_A, TERM_A]

    assert any("已经导入过" in text for text in second.warnings)
    assert any(STUDENT in text and TERM_A in text for text in second.warnings)


def test_second_import_shares_the_first_archive_instances() -> None:
    """★ 记录一条实测行为：``build_archive(..., archive=...)`` 会**就地追加**到入参的
    ``StudentArchive`` 上（两份档案共用同一个学生对象），所以调用方手里那份旧 Archive
    也会跟着变成两条记录。

    这跟"不改入参"的口头契约不一致，但**不会**丢掉数据：分析页每次都用新返回的
    Archive 覆盖 ``self._archive``（并落盘），界面上永远只有一份真源。
    真要修就该在 ``core/archival.py`` 里先深拷一份 StudentArchive —— 那不在本次
    改动范围内，所以这里**先把它钉成判据**，免得将来有人以为它是"意外"。
    """
    first = build_archive(_parsed([MATH], [76.0]), TERM_A)
    assert len(_student_of(first).records) == 1

    second = build_archive(_parsed([MATH], [91.0]), TERM_A, archive=first)

    assert second is not first
    assert len(_student_of(second).records) == 2
    # 同一个学生对象被追加过一次 —— 老档案也跟着长了
    assert len(_student_of(first).records) == 2
    assert _student_of(first) is _student_of(second)


def test_duplicate_term_keeps_two_rows_in_the_grades_tab() -> None:
    """两次导入都在「成绩明细」里看得见（91 在前、76 在后），一门课都没被吞掉。"""
    archive = build_archive(_parsed([MATH], [76.0]), TERM_A)
    archive = build_archive(_parsed([MATH], [91.0]), TERM_A, archive=archive)

    grouped = group_grades_by_term(grade_rows_from_records(_student_of(archive)))
    assert [term for term, _rows in grouped] == [TERM_A]
    rows = grouped[0][1]
    assert [row["course"] for row in rows] == ["高等数学", "高等数学"]
    assert [row["score"] for row in rows] == [91.0, 76.0]


def test_terms_are_deduped_but_records_are_not() -> None:
    """学期列表去重（页面上只出现一个分组），记录**不去重**（两份数据都在）。"""
    archive = build_archive(_parsed([MATH], [76.0]), TERM_A)
    archive = build_archive(_parsed([MATH], [91.0]), TERM_A, archive=archive)
    student = _student_of(archive)
    assert list(student.terms) == [TERM_A]
    assert len(student.records) == 2


def test_merged_view_takes_the_higher_score_and_counts_credit_once() -> None:
    """「总评与侧写」的合并口径：同名课取高，学分只算一次（不是两次相加）。"""
    archive = build_archive(_parsed([MATH], [76.0]), TERM_A)
    archive = build_archive(_parsed([MATH], [91.0]), TERM_A, archive=archive)
    student = _student_of(archive)

    merged = [course for course in student.merged_courses if course.name == "高等数学"]
    assert len(merged) == 1
    assert merged[0].score == 91.0

    result = merge_records(student)
    assert result.total_credit == pytest.approx(4.0)      # 4 学分，不是 8
    assert result.weighted_sum == pytest.approx(91.0 * 4.0)
    assert result.weighted_average == pytest.approx(91.0)
    # 两次的均值 83.5 出现在这里就是 bug
    assert result.weighted_average != pytest.approx(83.5)


def test_term_average_of_a_duplicate_term_follows_the_sigma_rule() -> None:
    """同一学期两份记录：``term_average`` 走 Σ(成绩×学分)/Σ学分（core/archival.py:495）。

    ★ 它**不是**"两次均分再平均"：两份不等学分的记录里，分母是两份的学分之和
      （8 + 3 = 11）；也不是 :func:`merge_records` 的"同名课取高"。
      这里故意用一组不等学分的记录，好把三种算法区分开。
    """
    first = build_archive(_parsed([MATH], [76.0]), TERM_A)
    archive = build_archive(_parsed([MATH, PHYS], [91.0, 60.0]), TERM_A, archive=first)
    student = _student_of(archive)

    sigma = (76.0 * 4 + 91.0 * 4 + 60.0 * 3) / (4 + 4 + 3)
    naive = (76.0 + (91.0 * 4 + 60.0 * 3) / 7) / 2       # 两条记录的均分再平均

    assert term_average(student, TERM_A) == pytest.approx(sigma)
    assert term_average(student, TERM_A) != pytest.approx(naive)
    # 合并口径（同名课取高）又是另一个数 —— 三条路各自成立，别互相顶替
    assert merge_records(student).weighted_average == pytest.approx(
        (91.0 * 4 + 60.0 * 3) / 7
    )


# ---------------------------------------------------------------------------
# 二、空 ≠ 0
# ---------------------------------------------------------------------------


def test_grade_rows_keep_a_blank_score_as_none() -> None:
    """没考的课在行里是 ``None``，**不许**被换成 0.0。"""
    archive = build_archive(_parsed([MATH, ENG], [None, 91.0]), TERM_A)
    rows = grade_rows_from_records(_student_of(archive))
    by_course = {row["course"]: row for row in rows}

    assert by_course["高等数学"]["score"] is None
    assert by_course["高等数学"]["credit"] == 4.0
    assert by_course["专业英语"]["score"] == 91.0
    assert by_course["高等数学"]["term"] == TERM_A
    assert {"term", "course", "score", "credit", "category"} <= set(
        by_course["高等数学"]
    )


def test_missing_moral_is_none_not_zero(vault: CryptoStore) -> None:
    """德育分**没录 ≠ 0 分**（AGENTS §14）：返回 ``None``，界面显示空白。"""
    assert moral_of(vault, USER, KEY, ["CS2301"]) is None
    assert moral_of(vault, USER, KEY) is None            # 连班级都没有
    assert moral_of(vault, USER, "", ["CS2301"]) is None   # 空键直接放弃


def test_recorded_moral_comes_back_including_a_real_zero(vault: CryptoStore) -> None:
    """录进去的分数读得回来；**录了 0 就是 0**，不会被当成"没录"。"""
    save_moral(vault, USER, "CS2301", {KEY: 88, "sid:2023002": 0})
    assert moral_of(vault, USER, KEY, ["CS2301"]) == 88
    assert moral_of(vault, USER, "sid:2023002", ["CS2301"]) == 0
    assert moral_of(vault, USER, "sid:2023999", ["CS2301"]) is None


def test_format_score_treats_blank_as_blank_and_zero_as_zero() -> None:
    assert format_score(None) == ""
    assert format_score("") == ""
    assert format_score(0) == "0"          # 数字 0 = 缺考，要显示出来
    assert format_score(0.0) == "0"
    assert format_score(88.0) == "88"
    assert format_score(88.5) == "88.5"
    assert format_score("91") == "91"


def test_format_credit_treats_blank_as_blank_and_zero_as_zero() -> None:
    assert format_credit(None) == ""
    assert format_credit("") == ""
    assert format_credit(0) == "0"
    assert format_credit(3.5) == "3.5"
    assert format_credit(4.0) == "4"


# ---------------------------------------------------------------------------
# 三、按学期分组
# ---------------------------------------------------------------------------


def _row(term: str, course: str, score: Any, credit: Any = 1.0) -> dict[str, Any]:
    return {"term": term, "course": course, "score": score, "credit": credit}


def test_group_grades_by_term_follows_semester_order() -> None:
    """学期顺序取自 SEMESTER_ORDER —— 不按字母序、不按插入序。"""
    rows = [_row("大二下", "B", 80.0), _row("大一上", "A", 80.0), _row("大三上", "C", 80.0)]
    assert [term for term, _ in group_grades_by_term(rows)] == ["大一上", "大二下", "大三上"]
    assert [term for term, _ in group_grades_by_term(rows)] == sorted(
        ["大一上", "大二下", "大三上"], key=semester_rank
    )


def test_terminal_term_group_comes_first() -> None:
    """终结（最新）学期永远排在最前 —— 用户先看现在。"""
    rows = [_row("大一上", "A", 80.0), _row("大二下", "B", 80.0)]
    grouped = group_grades_by_term(rows, terminal_terms={"大二下"})
    assert [term for term, _ in grouped] == ["大二下", "大一上"]


def test_unknown_term_lands_last() -> None:
    """认不出的学期（转专业、小学期……）排在所有已知学期后面，且不丢。"""
    rows = [_row("小学期", "X", 80.0), _row("大一上", "A", 80.0)]
    assert [term for term, _ in group_grades_by_term(rows)] == ["大一上", "小学期"]
    assert semester_rank("小学期") > semester_rank(SEMESTER_ORDER[-1])


def test_rows_inside_a_term_sort_by_score_desc_and_blanks_last() -> None:
    rows = [
        _row("大一上", "低", 60.0),
        _row("大一上", "空", None),
        _row("大一上", "高", 91.0),
    ]
    grouped = group_grades_by_term(rows)
    assert [row["course"] for row in grouped[0][1]] == ["高", "低", "空"]


def test_blank_course_name_is_dropped_unless_the_term_is_terminal() -> None:
    """空课程名在普通学期直接跳过；在终结学期改成「（历史课程名缺失）」。"""
    rows = [_row("大一上", "", 80.0)]
    assert group_grades_by_term(rows) == []

    grouped = group_grades_by_term(rows, terminal_terms={"大一上"})
    assert [term for term, _ in grouped] == ["大一上"]
    assert grouped[0][1][0]["course"] == "（历史课程名缺失）"


# ---------------------------------------------------------------------------
# 四、侧写正文（ProfileStore）
# ---------------------------------------------------------------------------


def test_side_text_round_trips_through_a_new_store_instance(vault: CryptoStore) -> None:
    """写进去 → 读得回来；换一个 ProfileStore 实例也一样（真的落盘了）。"""
    ProfileStore(store=vault, username=USER).save_side_text(KEY, SIDE_TEXT)

    assert ProfileStore(store=vault, username=USER).side_text(KEY) == SIDE_TEXT
    # 没存过的学生是**空串**，不是 None
    assert ProfileStore(store=vault, username=USER).side_text("sid:2023999") == ""

    profiles = ProfileStore(store=vault, username=USER).all_profiles()
    assert set(profiles) == {KEY}                     # 返回 {学号键: 行} 的字典
    assert profiles[KEY]["side_text"] == SIDE_TEXT


def test_side_text_lands_in_an_encrypted_file_only(vault: CryptoStore) -> None:
    """磁盘上只有 .enc，而且**明文正文一个字都扫不到**（红线 3）。"""
    ProfileStore(store=vault, username=USER).save_side_text(KEY, SIDE_TEXT)
    uid = vault.load_users().find(USER).uid
    folder = vault.user_dir(uid)

    names = sorted(path.name for path in folder.iterdir() if path.is_file())
    assert "student_profiles.json.enc" in names
    assert all(name.endswith(".enc") for name in names), names

    secret = SIDE_TEXT.encode("utf-8")
    leaks = [
        path
        for path in vault.root.rglob("*")
        if path.is_file() and secret in path.read_bytes()
    ]
    assert leaks == []


def test_save_side_text_rejects_a_blank_student_id(vault: CryptoStore) -> None:
    with pytest.raises(ValueError):
        ProfileStore(store=vault, username=USER).save_side_text("   ", SIDE_TEXT)


def test_read_side_text_never_raises() -> None:
    """库没有 / 方法没有 / 方法抛异常 —— 一律给空串，绝不把档案页带崩。"""

    class _NoMethod:
        pass

    class _Boom:
        def side_text(self, student_id: str) -> str:
            raise RuntimeError("库坏了")

    assert read_side_text(None, STUDENT_ID, KEY) == ""
    assert read_side_text(object(), STUDENT_ID, KEY) == ""
    assert read_side_text(_NoMethod(), STUDENT_ID, KEY) == ""
    assert read_side_text(_Boom(), STUDENT_ID, KEY) == ""
    assert read_side_text(_Boom(), "", "") == ""          # 连学号都没有


def test_side_text_prompt_states_missing_moral_and_blank_scores() -> None:
    """提示词只念已有字段：没录的德育分写「未录入」，空成绩写「（空白）」。"""
    grades = grade_rows_from_records(
        _student_of(build_archive(_parsed([MATH, ENG], [None, 91.0]), TERM_A))
    )
    prompt = build_side_text_prompt(STUDENT, STUDENT_ID, KEY, grades, moral=None)

    assert f"学生：{STUDENT}" in prompt
    assert f"学号：{STUDENT_ID}" in prompt
    assert "德育分：未录入" in prompt
    assert "成绩（空白）" in prompt
    assert "成绩 91" in prompt
    assert f"【{TERM_A}】" in prompt
    assert "同名" not in prompt                     # 有学号就不该出现同名提醒

    with_moral = build_side_text_prompt(STUDENT, STUDENT_ID, KEY, grades, moral=88)
    assert "德育分：88" in with_moral
    assert "未录入" not in with_moral

    empty = build_side_text_prompt(STUDENT, STUDENT_ID, KEY, [], moral=None)
    assert "（还没有导入任何成绩明细）" in empty


def test_side_text_prompt_warns_when_the_key_is_a_bare_name() -> None:
    """没有学号（name: 键）时必须明说同名风险（AGENTS §9）。"""
    prompt = build_side_text_prompt(STUDENT, "", "name:abc123", [], moral=90)
    assert "（成绩表里没有学号）" in prompt
    assert "不要把他人的成绩算进来" in prompt


# ---------------------------------------------------------------------------
# 五、档案页（真控件，offscreen）
# ---------------------------------------------------------------------------


def test_profile_page_has_three_document_tabs(page: ProfilePage) -> None:
    """v1.9.1 起是**三个**标签页：成绩明细 / 总评与侧写 / AI 辅导（任务书 2）。"""
    assert page._tabs.count() == 3
    assert page._tabs.tabText(0) == "成绩明细"
    assert page._tabs.tabText(1) == "总评与侧写"
    assert page._tabs.tabText(2) == "AI 辅导"


def test_grades_tree_groups_by_term_and_leaves_blank_cells_empty(
    page: ProfilePage,
) -> None:
    """真控件判据：最新学期在最上面；没考的格子是**空白**，不是 0。"""
    archive = build_archive(_parsed([MATH], [76.0]), TERM_A)
    archive = build_archive(_parsed([MATH], [None]), TERM_B, archive=archive)
    student = _student_of(archive)

    page.show_student(_payload(student))

    tree = page._grade_tree
    tops = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert tops == [TERM_B, TERM_A]                   # 终结学期（最新）排最前

    latest = tree.topLevelItem(0)
    blank = latest.child(0)
    assert blank.text(0) == "高等数学"
    assert blank.text(1) == ""                        # ★ 空白 ≠ "0"
    assert blank.text(2) == "4"                       # 学分照旧显示

    older = tree.topLevelItem(1)
    assert older.child(0).text(1) == "76"

    note = page._grades_note.text()
    assert "2 个学期，共 2 门课。" in note
    assert "空白＝没选或没考（不是 0 分）" in note


def test_empty_side_text_shows_the_generate_placeholder(page: ProfilePage) -> None:
    """还没有正文时显示「点击生成」占位，不是空白一片。"""
    archive = build_archive(_parsed([MATH], [76.0]), TERM_A)
    page.show_student(_payload(_student_of(archive)))

    assert page._side_text.toPlainText() == ""
    assert page._side_hint.text() == SIDE_TEXT_EMPTY_HINT
    assert page._ai_btn.text() == "点击生成"
    assert page._ai_btn.isEnabled()

    assert "还没有侧写" in SIDE_TEXT_EMPTY_HINT
    assert "「AI 侧写」" in SIDE_TEXT_EMPTY_HINT
    for mark in ("**", "__", "`"):                    # 界面文案不许有 Markdown 记号
        assert mark not in SIDE_TEXT_EMPTY_HINT


def test_saved_side_text_flips_the_hint_and_the_button(
    vault: CryptoStore, page: ProfilePage
) -> None:
    """存过正文之后再渲染：正文回填、按钮变「重新生成」、气泡换成落库说明。"""
    archive = build_archive(_parsed([MATH], [76.0]), TERM_A)
    payload = _payload(_student_of(archive))
    page.show_student(payload)

    page.save_side_text(SIDE_TEXT)

    assert page._side_text.toPlainText() == SIDE_TEXT
    assert page._ai_btn.text() == "重新生成"
    assert page._side_hint.text() != SIDE_TEXT_EMPTY_HINT
    assert ProfileStore(store=vault, username=USER).side_text(KEY) == SIDE_TEXT

    # 换一名没存过的学生 → 回到占位态（不串号）
    other = _payload(student_archive=None, student="李四", student_id="2023002")
    assert other["key"] != payload["key"]
    page.show_student(other)
    assert page._side_text.toPlainText() == ""
    assert page._side_hint.text() == SIDE_TEXT_EMPTY_HINT
    assert page._ai_btn.text() == "点击生成"


def test_generate_without_a_student_asks_to_pick_one(
    page: ProfilePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *args, **kwargs: seen.append(args[2] if len(args) > 2 else ""),
    )
    page._generate_side_text()
    assert seen == ["先在左边点一名学生。"]


def test_generate_without_a_model_reports_it_in_chinese(
    page: ProfilePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本机没有可用的模型 / 引擎时：状态行给一句中文原因，**不抛异常、不静默**。

    ★ v1.9.1 起侧写不再经「AI 谈话助手」对话框（那条路是弹窗），改成页内
      直接调本机引擎 —— 拿不到引擎时走的是**状态行**，也不再弹模态框。
    """
    archive = build_archive(_parsed([MATH], [76.0]), TERM_A)
    page.set_chat_models([])                       # 本机一个模型都没有
    monkeypatch.setattr("ui.pages.profile_page.engine_available", lambda: (True, ""))
    page.show_student(_payload(_student_of(archive)))

    seen: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *args, **kwargs: seen.append(args[2] if len(args) > 2 else ""),
    )

    page._generate_side_text()

    assert seen == []                              # 不弹模态框，走状态行
    assert page._side_status.text() == "生成失败：请先在模型中心下载模型"
    assert page._side_text.toPlainText() == ""
    assert page._side_text.isReadOnly() is False   # 失败之后文本框照样能用
    assert page._ai_btn.isEnabled() is True        # 按钮恢复，可以再试


def test_generate_hands_the_prompt_over_and_stores_what_comes_back(
    vault: CryptoStore, page: ProfilePage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点「点击生成」的完整一条链：拼提示词 → 本机引擎流式出稿 → 改 → 「保存」落库。

    ★ 引擎是**假的**（一个真模型都不加载）；``_start_worker`` 换成"就地跑"，
      于是信号同步派发，不需要事件循环。
    ★ 生成完**不自动落库**（任务书 5：正文先给辅导员改，点「保存」才写）。
    """
    archive = build_archive(_parsed([MATH, ENG], [76.0, 91.0]), TERM_A)
    page.show_student(_payload(_student_of(archive)))

    prompts: list[str] = []

    class _Engine:
        def load(self) -> None:
            pass

        def stream(self, prompt: str, *args: Any, **kwargs: Any):
            prompts.append(prompt)
            yield SIDE_TEXT

    page.set_chat_engine(_Engine())
    monkeypatch.setattr("ui.pages.profile_page.engine_available", lambda: (True, ""))
    monkeypatch.setattr(
        ProfilePage, "_start_worker", staticmethod(lambda worker: worker.run())
    )

    page._generate_side_text()

    assert len(prompts) == 1
    assert "不要编造没有出现的数字" in prompts[0]
    assert f"学生：{STUDENT}" in prompts[0]
    assert page._side_text.toPlainText() == SIDE_TEXT
    assert page._side_save.isEnabled() is True     # 有改动 → 「保存」亮着
    # 还没点「保存」→ 加密库里仍然是空的
    assert ProfileStore(store=vault, username=USER).side_text(KEY) == ""

    page._on_side_save_clicked()

    assert ProfileStore(store=vault, username=USER).side_text(KEY) == SIDE_TEXT
    assert page._ai_btn.text() == "重新生成"
    assert ProfileStore(store=vault, username=USER).side_text(KEY) == SIDE_TEXT
