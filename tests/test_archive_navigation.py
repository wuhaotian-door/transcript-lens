"""学期侧写页（ui/pages/semester_side_profile.py）与共享模块的回归测试。

覆盖三块：
* 成绩表只显示本学期；空成绩/空学分显示「—」而不是 0；三列值正确；
* 学业画像的课程数 / 本学期算术平均 / 挂科数 / 学分合计 / 德育分；
* 返回信号、AI 引擎不可用 / 无模型 / 流式生成 / 失败 / 空输出、按学期保存与读回。

★ 全部在 offscreen 下跑，**不启动真实推理、不联网、不碰真实用户库**：
  ``ProfileStore`` 被整体打桩；唯一一个真实加密库用例只用 ``tmp_path``。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from types import SimpleNamespace

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QToolButton,
)

import ui.pages.profile_page as pp
import ui.pages.profile_shared as shared
import ui.pages.semester_side_profile as ssp
from core.database import GradeHistoryDB as _GradeHistoryDB
from core.database import ProfileStore as _ProfileStore
from ui.pages.analysis_page import TableRow

TERM = "大二下"
OTHER_TERM = "大二上"
SID = "2023001"
NAME = "张三"

RECORDS = [
    {"student_id": SID, "semester": TERM, "course_name": "高等数学", "score": 90, "credit": 4},
    {"student_id": SID, "semester": TERM, "course_name": "大学物理", "score": 80, "credit": 3},
    {"student_id": SID, "semester": TERM, "course_name": "体育", "score": None, "credit": 1},
    {"student_id": SID, "term": TERM, "course_name": "线性代数", "score": 85, "credit": 3},
    {"student_id": SID, "semester": OTHER_TERM, "course_name": "大学英语", "score": 70, "credit": 2},
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def message_spy(monkeypatch):
    """拦掉所有 QMessageBox，记录 (标题, 正文)，无头环境里绝不弹模态框。"""

    class Spy:
        calls: list[tuple[str, str]] = []

        @classmethod
        def information(cls, parent, title, text, *args, **kwargs):
            cls.calls.append((str(title), str(text)))

        @classmethod
        def warning(cls, parent, title, text, *args, **kwargs):
            cls.calls.append((str(title), str(text)))

        @classmethod
        def critical(cls, parent, title, text, *args, **kwargs):
            cls.calls.append((str(title), str(text)))

    monkeypatch.setattr(ssp, "QMessageBox", Spy)
    return Spy


@pytest.fixture(autouse=True)
def fake_profile_store(monkeypatch):
    """默认的 ProfileStore 假实现：只有老接口，用来覆盖回退路径。"""

    class FakeStore:
        instances: list["FakeStore"] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.saved: list[tuple[str, str]] = []
            self._text = ""
            FakeStore.instances.append(self)

        def side_text(self, student_id):
            return self._text

        def save_side_text(self, student_id, text):
            self.saved.append((student_id, text))
            self._text = text

    monkeypatch.setattr(ssp, "ProfileStore", FakeStore)
    return FakeStore


@pytest.fixture
def page(qapp, fake_profile_store):
    widget = ssp.SemesterSideProfile()
    yield widget
    widget.deleteLater()


@pytest.fixture
def sync_worker(monkeypatch):
    """把线程启动点换成同步执行，信号在当前线程直接派发。"""
    started = []

    def run_now(worker, *args, **kwargs):
        started.append(worker)
        worker.run()

    monkeypatch.setattr(ssp.SemesterSideProfile, "_start_worker", staticmethod(run_now))
    return started


@pytest.fixture
def fake_engine(monkeypatch):
    """假引擎：可控输出块、可控异常，并记录提示词。"""

    class Engine:
        chunks = ["第一块", "第二块"]
        error = None
        instances: list["Engine"] = []

        def __init__(self, path):
            self.path = path
            self.prompts = []
            self.loaded = False
            Engine.instances.append(self)

        def load(self):
            self.loaded = True

        def stream(self, prompt):
            self.prompts.append(prompt)
            if Engine.error is not None:
                raise Engine.error
            return iter(list(Engine.chunks))

    monkeypatch.setattr(ssp, "LocalModelEngine", Engine)
    monkeypatch.setattr(ssp, "engine_available", lambda: (True, ""))
    monkeypatch.setattr(
        ssp,
        "list_local_models",
        lambda: [SimpleNamespace(usable=True, path="fake-model.gguf", name="fake")],
    )
    return Engine


def seed(page, records=None, term=TERM, moral=None, session=None):
    if session is not None:
        page.set_session(session)
    page.set_student({"student_id": SID, "name": NAME, "class_name": "软件2301"})
    page.set_records(list(RECORDS if records is None else records))
    page.set_term(term)
    page.set_moral(moral)
    return page


class SemesterStore:
    """带新接口的假库（原生按学期读写）。"""

    def __init__(self, text=""):
        self.texts = {}
        if text:
            self.texts[(SID, TERM)] = text
        self.semester_saved = []
        self.plain_saved = []

    def semester_side_text(self, student_id, term):
        return self.texts.get((student_id, term), "")

    def save_semester_side_text(self, student_id, term, text):
        self.semester_saved.append((student_id, term, text))
        self.texts[(student_id, term)] = text

    def side_text(self, student_id):
        return ""

    def save_side_text(self, student_id, text):
        self.plain_saved.append((student_id, text))


class PlainStore:
    """只有老接口的假库（回退路径）。"""

    def __init__(self, text=""):
        self.text = text
        self.saved = []

    def side_text(self, student_id):
        return self.text

    def save_side_text(self, student_id, text):
        self.saved.append((student_id, text))
        self.text = text


# ---------------------------------------------------------------------------
# 成绩表
# ---------------------------------------------------------------------------
def test_table_only_current_term(page):
    seed(page)
    assert page._table.rowCount() == 4
    courses = [page._table.item(row, 0).text() for row in range(4)]
    assert courses == ["高等数学", "大学物理", "体育", "线性代数"]
    assert "大学英语" not in courses


def test_table_three_columns_values(page):
    seed(page)
    assert [page._table.item(0, col).text() for col in range(3)] == ["高等数学", "4", "90"]
    assert [page._table.item(1, col).text() for col in range(3)] == ["大学物理", "3", "80"]
    assert [page._table.item(3, col).text() for col in range(3)] == ["线性代数", "3", "85"]


def test_empty_score_shows_dash_not_zero(page):
    seed(page)
    assert page._table.item(2, 0).text() == "体育"
    assert page._table.item(2, 1).text() == "1"
    assert page._table.item(2, 2).text() == "—"
    assert page._table.item(2, 2).text() != "0"


def test_empty_credit_shows_dash_not_zero(page):
    seed(page, records=[{"semester": TERM, "course_name": "无学分课", "score": 88, "credit": ""}])
    assert page._table.item(0, 1).text() == "—"
    assert page._table.item(0, 2).text() == "88"


def test_header_labels_and_interactive(page):
    seed(page)
    header = page._table
    assert [header.horizontalHeaderItem(col).text() for col in range(3)] == list(ssp.COLUMNS)
    assert header.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive


def test_row_height_at_least_30(page):
    seed(page)
    assert page._table.rowHeight(0) >= 30
    assert page._table.verticalHeader().defaultSectionSize() >= 30
    assert ssp.MIN_ROW_HEIGHT >= 30


def test_empty_hint_when_no_records(page):
    seed(page, records=[RECORDS[-1]])
    assert page._table.rowCount() == 0
    assert page._empty_hint.isVisibleTo(page) is True
    assert page._empty_hint.text() == "本学期暂无成绩记录"


def test_empty_hint_hidden_with_records(page):
    seed(page)
    assert page._empty_hint.isVisibleTo(page) is False


def test_table_and_ai_box_are_read_only(page):
    seed(page)
    assert page._table.editTriggers() == QAbstractItemView.NoEditTriggers
    assert page._ai_text.isReadOnly() is True


# ---------------------------------------------------------------------------
# 学业画像
# ---------------------------------------------------------------------------
def test_portrait_course_count(page):
    seed(page)
    assert page._stat_courses.text() == "本学期课程数：4（有成绩 3 门）"


def test_portrait_arithmetic_average(page):
    seed(page)
    assert page._stat_average.text() == "本学期算术平均：85.00"


def test_average_excludes_empty_score(page):
    seed(page)
    text = page._stat_average.text()
    assert "85.00" in text
    # 若把空成绩当 0 计入分母，会得到 63.75
    assert "63.75" not in text


def test_fail_count_ignores_empty_score(page):
    seed(page)
    assert page._stat_fails.text() == "挂科数：0"


def test_zero_score_counts_as_fail(page):
    rows = [
        {"semester": TERM, "course_name": "缺考", "score": 0, "credit": 2},
        {"semester": TERM, "course_name": "五十九", "score": 59, "credit": 2},
        {"semester": TERM, "course_name": "及格", "score": 60, "credit": 2},
    ]
    seed(page, rows)
    assert page._stat_fails.text() == "挂科数：2"
    assert page._stat_average.text() == "本学期算术平均：39.67"
    assert page._stat_credits.text() == "学分合计：6"
    assert page._table.item(0, 2).text() == "0"


def test_single_zero_average_is_zero_not_dash(page):
    seed(page, records=[{"semester": TERM, "course_name": "缺考", "score": 0, "credit": 2}])
    assert page._stat_average.text() == "本学期算术平均：0.00"
    assert page._stat_fails.text() == "挂科数：1"


def test_credit_total_only_scored_rows(page):
    seed(page)
    # 4 + 3 + 3；体育没有成绩，学分 1 不进合计
    assert page._stat_credits.text() == "学分合计：10"
    assert "11" not in page._stat_credits.text()


def test_credit_total_missing_shows_dash(page):
    seed(page, records=[{"semester": TERM, "course_name": "无学分课", "score": 88, "credit": None}])
    assert page._stat_credits.text() == "学分合计：—"
    assert page._stat_average.text() == "本学期算术平均：88.00"


def test_moral_value_is_displayed(page):
    seed(page, moral=88)
    assert page._stat_moral.text() == "德育分：88"


def test_moral_missing_is_not_zero(page):
    seed(page, moral=None)
    assert page._stat_moral.text() == "德育分：未录入"


def test_moral_zero_is_zero_not_missing(page):
    seed(page, moral=0)
    assert page._stat_moral.text() == "德育分：0"


def test_title_and_student_line(page):
    seed(page)
    assert page._title_label.text() == f"学期侧写 · {TERM}"
    assert page._student_label.text() == f"{NAME} · {SID}"


# ---------------------------------------------------------------------------
# 返回 / 布局 / 文案
# ---------------------------------------------------------------------------
def test_back_button_emits_signal(page):
    spy = QSignalSpy(page.backRequested)
    assert page._back_button.text() == "← 返回档案"
    page._back_button.click()
    assert spy.count() == 1


def test_splitter_has_two_horizontal_panels(page):
    splitter = page.findChild(QSplitter)
    assert splitter is not None
    assert splitter.count() == 2
    assert splitter.orientation() == splitter.orientation().Horizontal


def test_ui_text_has_no_markdown_markers(page):
    seed(page, moral=88)
    page._ai_text.setPlainText("本学期的表现比较稳定。")

    texts = []
    for widget_type in (QLabel, QPushButton, QToolButton):
        for widget in page.findChildren(widget_type):
            texts.append(widget.text())
            texts.append(widget.toolTip())
    for group in page.findChildren(QGroupBox):
        texts.append(group.title())
    texts.append(page._ai_text.placeholderText())

    for text in texts:
        assert "**" not in text
        assert "__" not in text
        assert "`" not in text


# ---------------------------------------------------------------------------
# AI：前置检查
# ---------------------------------------------------------------------------
def test_engine_unavailable_shows_message_without_starting_thread(page, monkeypatch, message_spy):
    seed(page)
    monkeypatch.setattr(ssp, "engine_available", lambda: (False, "缺少推理运行库"))
    started = []
    monkeypatch.setattr(
        ssp.SemesterSideProfile,
        "_start_worker",
        staticmethod(lambda worker, *args, **kwargs: started.append(worker)),
    )

    page._generate_button.click()

    assert started == []
    assert page._side_worker is None
    assert message_spy.calls
    assert message_spy.calls[0][0] == "本地推理引擎未就绪"
    assert "缺少推理运行库" in message_spy.calls[0][1]


def test_no_models_shows_model_center_message(page, monkeypatch, message_spy):
    seed(page)
    monkeypatch.setattr(ssp, "engine_available", lambda: (True, ""))
    monkeypatch.setattr(ssp, "list_local_models", lambda: [])

    page._generate_button.click()

    assert any("请先在模型中心下载模型" in text for _, text in message_spy.calls)
    assert page._side_worker is None


def test_model_without_path_shows_message(page, monkeypatch, message_spy):
    seed(page)
    monkeypatch.setattr(ssp, "engine_available", lambda: (True, ""))
    monkeypatch.setattr(
        ssp, "list_local_models", lambda: [SimpleNamespace(usable=True, path="")]
    )

    page._generate_button.click()

    assert any("模型文件缺失" in text for _, text in message_spy.calls)


def test_generation_requires_term_records(page, fake_engine, sync_worker, message_spy):
    seed(page, records=[RECORDS[-1]])

    page._generate_button.click()

    assert fake_engine.instances == []
    assert any("先导入成绩再生成侧写" in text for _, text in message_spy.calls)


# ---------------------------------------------------------------------------
# AI：流式生成
# ---------------------------------------------------------------------------
def test_generation_streams_chunks_in_order(page, fake_engine, sync_worker, message_spy):
    seed(page)
    fake_engine.chunks = ["第一段", "，第二段", "。"]

    page._generate_button.click()

    assert len(fake_engine.instances) == 1
    engine = fake_engine.instances[0]
    assert engine.path == "fake-model.gguf"
    assert engine.loaded is True
    assert page._ai_text.toPlainText() == "第一段，第二段。"
    assert page._ai_status.text() == "已生成本学期侧写。"
    assert page._generate_button.isEnabled() is True
    assert page._save_button.isEnabled() is True
    assert message_spy.calls == []


def test_prompt_only_contains_current_term(page, fake_engine, sync_worker):
    seed(page, moral=88)
    fake_engine.chunks = ["ok"]

    page._generate_button.click()

    prompt = fake_engine.instances[0].prompts[0]
    assert f"只写 {TERM}" in prompt
    assert "高等数学" in prompt
    assert "线性代数" in prompt
    assert "大学英语" not in prompt
    assert "德育分：88" in prompt
    assert f"学生：{NAME}" in prompt
    assert f"学号：{SID}" in prompt


def test_generation_failure_shows_reason(page, fake_engine, sync_worker, message_spy):
    seed(page)
    fake_engine.error = ssp.InferenceUnavailable("模型文件损坏")

    page._generate_button.click()

    assert page._ai_status.text() == "生成失败"
    assert page._ai_text.toPlainText() == ""
    assert page._generate_button.isEnabled() is True
    assert any("模型文件损坏" in text for _, text in message_spy.calls)


def test_generation_empty_output_shows_message(page, fake_engine, sync_worker, message_spy):
    seed(page)
    fake_engine.chunks = []

    page._generate_button.click()

    assert page._ai_status.text() == "没有生成内容"
    assert any("这次没有生成任何内容" in text for _, text in message_spy.calls)


# ---------------------------------------------------------------------------
# 保存与读回
# ---------------------------------------------------------------------------
def test_save_prefers_semester_method(page):
    seed(page)
    store = SemesterStore()
    page.set_profile_store(store)
    page._ai_text.setPlainText("本学期表现稳定")

    page._save_button.click()

    assert store.semester_saved == [(SID, TERM, "本学期表现稳定")]
    assert store.plain_saved == []
    assert page._ai_status.text() == "已保存到本地加密文件。"


def test_save_falls_back_to_plain_text_with_term_prefix(page):
    seed(page)
    store = PlainStore()
    page.set_profile_store(store)
    page._ai_text.setPlainText("本学期表现稳定")

    page._save_button.click()

    assert len(store.saved) == 1
    student_id, text = store.saved[0]
    assert student_id == SID
    assert text == f"[{TERM}]\n本学期表现稳定"
    assert TERM in text


def test_save_without_login_shows_message(page, message_spy):
    seed(page)
    page._ai_text.setPlainText("有内容")

    page._save_button.click()

    assert any("没登录" in text for _, text in message_spy.calls)


def test_save_empty_text_shows_message(page, message_spy):
    seed(page)

    page._save_button.click()

    assert any("还没有可保存的内容" in text for _, text in message_spy.calls)


def test_saved_semester_text_is_loaded(page):
    store = SemesterStore()
    store.texts[(SID, TERM)] = "存过的侧写"
    page.set_profile_store(store)

    seed(page)

    assert page._ai_text.toPlainText() == "存过的侧写"
    assert page._ai_status.text() == "已读取这个学期存过的侧写。"


def test_switching_term_clears_and_loads_text(page):
    store = SemesterStore()
    store.texts[(SID, TERM)] = "大二下的侧写"
    store.texts[(SID, "大三上")] = "大三上的侧写"
    page.set_profile_store(store)

    seed(page)
    assert page._ai_text.toPlainText() == "大二下的侧写"

    page.set_term("大三上")
    assert page._ai_text.toPlainText() == "大三上的侧写"

    page.set_term("大四上")
    assert page._ai_text.toPlainText() == ""
    assert page._ai_status.text() == "尚未生成"


def test_load_fallback_strips_current_term_prefix(page):
    page.set_profile_store(PlainStore(f"[{TERM}]\n旧文本"))

    seed(page)

    assert page._ai_text.toPlainText() == "旧文本"


def test_load_fallback_ignores_other_term_prefix(page):
    page.set_profile_store(PlainStore(f"[{OTHER_TERM}]\n别的学期"))

    seed(page)

    assert page._ai_text.toPlainText() == ""
    assert page._ai_status.text() == "尚未生成"


def test_real_profile_store_roundtrip(tmp_path, page, message_spy):
    from core.crypto_store import CryptoStore
    from core.database import ProfileStore as RealProfileStore

    vault = CryptoStore(root=tmp_path / "vault")
    vault.register("tester", "Passw0rd!123")
    store = RealProfileStore(username="tester", store=vault)

    page.set_session(SimpleNamespace(username="tester"))
    page.set_profile_store(store)
    seed(page)
    page._ai_text.setPlainText("真实加密库往返")

    page._save_button.click()

    assert store.semester_side_text(SID, TERM) == "真实加密库往返"
    assert message_spy.calls == []


# ---------------------------------------------------------------------------
# session 语义
# ---------------------------------------------------------------------------
def test_session_accepts_none_string_and_object(page):
    page.set_session(SimpleNamespace(username="tester"))
    assert page._username == "tester"

    page.set_session(None)
    assert page._username == ""

    page.set_session("alice")
    assert page._username == "alice"

    page.set_session({"username": "bob"})
    assert page._username == "bob"


# ---------------------------------------------------------------------------
# 共享模块（profile_shared）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [(None, ""), ("", ""), (0, "0"), (90, "90"), ("90.5", "90.5")],
)
def test_shared_format_score(value, expected):
    assert shared.format_score(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(None, ""), ("", ""), (0, "0"), (4.0, "4"), ("4.5", "4.5")],
)
def test_shared_format_credit(value, expected):
    assert shared.format_credit(value) == expected


def test_shared_term_of_archive_uses_latest_term():
    archive = SimpleNamespace(
        records=[
            SimpleNamespace(term="大一上"),
            SimpleNamespace(term="大二下"),
            SimpleNamespace(term="大二上"),
        ]
    )
    assert shared.term_of_archive(archive) == "大二下"


def test_shared_ordered_records_none_is_empty():
    assert shared._ordered_records(None) == []


def test_shared_group_grades_orders_terms():
    rows = [
        {"term": "大二下", "course_name": "B", "score": 90, "credit": 2},
        {"term": "大一上", "course_name": "A", "score": 80, "credit": 3},
    ]
    grouped = shared.group_grades_by_term(rows)
    assert [term for term, _ in grouped] == ["大一上", "大二下"]
    assert isinstance(grouped, list)
    assert grouped[1][1][0]["course"] == "B"


def test_shared_group_grades_keeps_blank_score_none():
    grouped = shared.group_grades_by_term(
        [{"term": TERM, "course_name": "体育", "score": None, "credit": 1}]
    )
    assert grouped[0][1][0]["score"] is None


def test_shared_moral_of_blank_arguments_returns_none():
    assert shared.moral_of(None, "", "") is None
    assert shared.moral_of(None, "tester", "") is None


def test_shared_student_json_keeps_blank_score_null():
    raw = shared.build_student_json(
        SID,
        NAME,
        "软件2301",
        [{"term": TERM, "course": "高等数学", "score": None, "credit": 4}],
        None,
        "",
    )
    data = json.loads(raw)
    assert data["学号"] == SID
    assert data["姓名"] == NAME
    assert data["德育分"] is None
    assert data["成绩"][0]["score"] is None


def test_shared_side_prompt_marks_missing_moral():
    prompt = shared.build_side_text_prompt(
        NAME,
        SID,
        f"sid:{SID}",
        [{"term": TERM, "course": "高等数学", "score": 90, "credit": 4}],
        None,
    )
    assert "高等数学" in prompt
    assert "德育分：未录入" in prompt
    assert TERM in prompt


def test_shared_side_prompt_warns_about_same_name():
    prompt = shared.build_side_text_prompt(
        NAME,
        "",
        f"name:{NAME}",
        [{"term": TERM, "course": "高等数学", "score": 90, "credit": 4}],
        88,
    )
    assert "同名" in prompt
    assert "德育分：88" in prompt


def test_shared_moral_text_variants():
    assert shared.moral_text(None) == "未录入"
    assert shared.moral_text(0) == "0"
    assert shared.moral_text(True) == "未录入"
    assert shared.moral_text("") == "未录入"


def test_blank_score_helpers_never_return_zero():
    assert ssp._score_value(None) is None
    assert ssp._score_value("") is None
    assert ssp._score_value("-") is None
    assert ssp._score_value(0) == 0.0
    assert ssp._score_text(0) == "0"
    assert ssp._score_text(None) == "—"


# ---------------------------------------------------------------------------
# 档案页本体：名单 / 学期时间轴穿透 / 学生主档 CRUD（任务书第 5、7 节）
#
# ★ 这里所有库都建在 tmp_path 下，碰不到 %LOCALAPPDATA% 的真库。
# ---------------------------------------------------------------------------
USER = "张老师"
PASSWORD = "tl-test-2026"


class FakeMessageBox:
    """QMessageBox 的替身：既顶掉静态弹窗，也能假装用户点了第 N 个按钮。"""

    calls: list[tuple[str, str, str]] = []
    choices: list[int] = []

    Question = Warning = Critical = Information = 0
    AcceptRole = ActionRole = RejectRole = DestructiveRole = Yes = No = 0
    # 页面里用 QMessageBox.StandardButton.Yes / .No 取按钮（枚举而不是裸常量），
    # 替身也得有这个名字，否则 AttributeError。
    StandardButton = SimpleNamespace(Yes=0, No=0, Ok=0, Cancel=0)

    def __init__(self, parent=None):
        self.buttons: list[SimpleNamespace] = []
        self.text = ""

    @classmethod
    def information(cls, parent, title, text, *args, **kwargs):
        cls.calls.append(("information", str(title), str(text)))
        return 0

    @classmethod
    def warning(cls, parent, title, text, *args, **kwargs):
        cls.calls.append(("warning", str(title), str(text)))
        return 0

    @classmethod
    def critical(cls, parent, title, text, *args, **kwargs):
        cls.calls.append(("critical", str(title), str(text)))
        return 0

    @classmethod
    def question(cls, parent, title, text, *args, **kwargs):
        cls.calls.append(("question", str(title), str(text)))
        return 0

    def setIcon(self, *args):
        pass

    def setWindowTitle(self, *args):
        pass

    def setText(self, text):
        self.text = str(text)

    def addButton(self, label, role=None):
        button = SimpleNamespace(label=str(label), role=role)
        self.buttons.append(button)
        return button

    def exec(self):
        return 0

    def clickedButton(self):
        if not FakeMessageBox.choices or not self.buttons:
            return None
        index = FakeMessageBox.choices.pop(0)
        if index is None or index >= len(self.buttons):
            return None
        return self.buttons[index]


class FakeStudentDialog:
    """新建 / 编辑学生的表单替身：直接给一份值并当作点了「确定」。"""

    values_to_return: dict[str, str] = {}
    titles: list[str] = []

    def __init__(self, parent=None, *, record=None, title="新建学生"):
        FakeStudentDialog.titles.append(str(title))
        self.record = record

    def exec(self):
        return QDialog.Accepted

    def values(self):
        return dict(FakeStudentDialog.values_to_return)

    def student_id(self):
        return FakeStudentDialog.values_to_return.get("student_id", "")


@pytest.fixture
def school(tmp_path):
    """临时加密库（必须 register，否则 user_store 会拒绝写盘）。"""
    from core.crypto_store import CryptoStore as _CryptoStore

    store = _CryptoStore(root=tmp_path / "vault")
    store.register(USER, PASSWORD)
    return store


@pytest.fixture
def box_spy(monkeypatch):
    FakeMessageBox.calls = []
    FakeMessageBox.choices = []
    monkeypatch.setattr(pp, "QMessageBox", FakeMessageBox)
    return FakeMessageBox


@pytest.fixture
def dialog_spy(monkeypatch):
    FakeStudentDialog.values_to_return = {}
    FakeStudentDialog.titles = []
    monkeypatch.setattr(pp, "StudentDialog", FakeStudentDialog)
    return FakeStudentDialog


@pytest.fixture
def audit_spy(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    def record(store, username, operation, *, student_id="", detail=""):
        calls.append((operation, student_id, detail))

    monkeypatch.setattr(pp, "record_student_audit", record)
    return calls


@pytest.fixture
def archive_page(qapp, school, box_spy):
    widget = pp.ProfilePage(store=school)
    widget.set_user(USER)
    yield widget
    widget.deleteLater()


def add_grade(store, semester, course, score, credit, student_id=SID):
    _GradeHistoryDB(store=store, username=USER).apply(
        [
            {
                "student_id": student_id,
                "semester": semester,
                "course_name": course,
                "score": score,
                "credit": credit,
            }
        ],
        semester=semester,
        batch_id="archive-nav-test",
    )


def open_student(page, index=0):
    page._list.setCurrentRow(index)
    page._on_item_clicked(page._list.item(index))


def test_roster_rows_show_up_in_the_left_list(archive_page):
    archive_page.set_roster([TableRow(index=0, student=NAME, student_id=SID)])

    assert archive_page._list.count() == 1
    assert NAME in archive_page._list.item(0).text()
    assert SID in archive_page._list.item(0).text()
    assert "共 1 人" in archive_page._list_note.text()


def test_clicking_a_student_renders_the_info_card(archive_page, school):
    add_grade(school, TERM, "高等数学", 90, 4)
    archive_page.set_roster([TableRow(index=0, student=NAME, student_id=SID)])

    open_student(archive_page)

    assert archive_page._stack.currentIndex() == 0
    assert NAME in archive_page._info_name.text()
    assert SID in archive_page._info_meta.text()
    assert archive_page.current_student_id() == SID


def test_semester_node_opens_the_side_page_and_back_returns(archive_page, school):
    add_grade(school, TERM, "高等数学", 90, 4)
    add_grade(school, OTHER_TERM, "大学物理", 80, 3)
    archive_page.set_roster([TableRow(index=0, student=NAME, student_id=SID)])
    open_student(archive_page)

    nodes = archive_page._timeline_box.findChildren(shared.TermNode)
    assert len(nodes) == 2
    node = next(item for item in nodes if item.term == OTHER_TERM)
    node.clicked.emit(OTHER_TERM)

    assert archive_page._stack.currentIndex() == 1
    title = archive_page._semester_page._title_label.text()
    assert NAME in title and OTHER_TERM in title

    archive_page._semester_page._back_button.click()
    assert archive_page._stack.currentIndex() == 0


def test_ask_semester_and_open_goes_through_the_input_dialog(
    archive_page, school, monkeypatch
):
    add_grade(school, TERM, "高等数学", 90, 4)
    add_grade(school, OTHER_TERM, "大学物理", 80, 3)
    archive_page.set_roster([TableRow(index=0, student=NAME, student_id=SID)])
    open_student(archive_page)

    monkeypatch.setattr(
        pp.QInputDialog,
        "getItem",
        staticmethod(lambda *args, **kwargs: (OTHER_TERM, True)),
    )
    archive_page.ask_semester_and_open()

    assert archive_page._stack.currentIndex() == 1
    assert archive_page._current_term == OTHER_TERM


def test_archive_search_only_shows_archived_students(archive_page):
    archive_page._book.add(student_id=SID, name=NAME, class_name="CS2301")
    archive_page._book.add(student_id="2023002", name="李四", class_name="CS2301")
    archive_page._book.archive(SID)
    archive_page._refresh_students()

    assert archive_page._list.count() == 1  # 默认不显示归档
    assert "李四" in archive_page._list.item(0).text()

    archive_page._on_search("已归档")

    assert archive_page._list.count() == 1
    assert NAME in archive_page._list.item(0).text()
    assert "李四" not in archive_page._list.item(0).text()


def test_new_student_is_saved_and_duplicate_id_is_rejected(
    archive_page, dialog_spy, box_spy, audit_spy
):
    dialog_spy.values_to_return = {
        "student_id": SID,
        "name": NAME,
        "class_name": "CS2301",
        "note": "",
    }

    archive_page._open_new_student()
    assert archive_page._book.has(SID)
    assert archive_page._list.count() == 1
    assert [call[0] for call in audit_spy].count("student_add") == 1

    box_spy.calls.clear()
    archive_page._open_new_student()

    texts = [text for _kind, _title, text in box_spy.calls]
    assert any("已存在" in text and SID in text for text in texts)
    assert archive_page._book.count() == 1


def test_edit_student_with_migration_moves_history(
    archive_page, school, dialog_spy, box_spy, audit_spy
):
    add_grade(school, TERM, "高等数学", 90, 4)
    archive_page.set_roster([TableRow(index=0, student=NAME, student_id=SID)])
    open_student(archive_page)

    box_spy.choices = [0]  # 迁移历史数据
    dialog_spy.values_to_return = {
        "student_id": "2023999",
        "name": NAME,
        "class_name": "CS2301",
        "note": "",
    }
    archive_page._open_edit_student()

    assert archive_page._book.get(SID) is None
    saved = archive_page._book.get("2023999")
    assert saved is not None and saved.name == NAME

    rows = _GradeHistoryDB(store=school, username=USER).rows()
    assert rows
    assert all(str(row.get("student_id")) not in {SID, f"sid:{SID}"} for row in rows)
    operations = [call[0] for call in audit_spy]
    assert "student_migrate" in operations
    assert "student_edit" in operations


def test_edit_student_without_migration_keeps_old_history(
    archive_page, school, dialog_spy, box_spy
):
    add_grade(school, TERM, "高等数学", 90, 4)
    archive_page.set_roster([TableRow(index=0, student=NAME, student_id=SID)])
    open_student(archive_page)

    box_spy.choices = [1]  # 仅修改信息，不迁移历史
    dialog_spy.values_to_return = {
        "student_id": "2023888",
        "name": NAME,
        "class_name": "CS2301",
        "note": "",
    }
    archive_page._open_edit_student()

    assert archive_page._book.get("2023888") is not None
    rows = _GradeHistoryDB(store=school, username=USER).rows()
    assert [row.get("student_id") for row in rows] == [SID]  # 历史还挂在旧学号


def test_delete_student_hard_clears_grades_side_text_and_chat(
    archive_page, school, box_spy, audit_spy
):
    from core.user_store import save_user_json, user_file

    add_grade(school, TERM, "高等数学", 90, 4)
    archive_page._book.add(student_id=SID, name=NAME, class_name="CS2301")
    _ProfileStore(store=school, username=USER).save_side_text(SID, "这是侧写")
    chat_name = (
        f"{pp.CHAT_HISTORY_DIR}{pp.CHAT_HISTORY_PREFIX}{SID}{pp.CHAT_HISTORY_SUFFIX}"
    )
    save_user_json(school, USER, chat_name, {"schema": 1, "messages": []})

    archive_page._refresh_students()
    archive_page._list.setCurrentRow(0)
    box_spy.choices = [0]  # 彻底删除
    archive_page._delete_student()

    assert archive_page._book.get(SID) is None
    assert _GradeHistoryDB(store=school, username=USER).rows() == []
    assert _ProfileStore(store=school, username=USER).side_text(SID) == ""
    # user_file() 只算路径、不判存在 —— 要自己看文件还在不在。
    chat_path = user_file(school, USER, chat_name)
    assert chat_path is None or not chat_path.exists()
    assert "student_delete" in [call[0] for call in audit_spy]


def test_delete_student_archive_keeps_everything(
    archive_page, school, box_spy, audit_spy
):
    add_grade(school, TERM, "高等数学", 90, 4)
    archive_page._book.add(student_id=SID, name=NAME, class_name="CS2301")
    archive_page._refresh_students()
    archive_page._list.setCurrentRow(0)

    box_spy.choices = [1]  # 归档隐藏
    archive_page._delete_student()

    saved = archive_page._book.get(SID)
    assert saved is not None and saved.archived is True
    assert len(_GradeHistoryDB(store=school, username=USER).rows()) == 1
    assert "student_archive" in [call[0] for call in audit_spy]
    assert archive_page._list.count() == 0  # 归档的不出现在默认列表里


def test_outline_note_wraps_at_narrow_width(archive_page):
    archive_page.resize(1268, 808)

    assert archive_page._outline_note.wordWrap() is True
    assert archive_page._outline_note.text() == "本地规则拼出来的，不联网、不调 AI"
    # 换行之后最小宽度够用，不会把卡片撑到窗口外
    assert archive_page._outline_note.minimumWidth() >= 180


def test_profile_ui_text_has_no_markdown_markers(archive_page):
    texts = [label.text() for label in archive_page.findChildren(QLabel)]
    joined = "\n".join(text for text in texts if text)
    assert "**" not in joined
    assert "`" not in joined
