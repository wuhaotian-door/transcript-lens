"""档案页 v1.9.1 三块新功能的判据（任务书 2 / 5 / 3）。

覆盖四组：

1. **「AI 辅导」第三标签页**（任务书 2）—— 三个标签页并存、学生信息卡片、
   只读谈话区、输入区；学生数据以 **JSON** 逐字进系统提示词。
2. **流式 + 两级降级** —— 用**假引擎**跑一遍「辅导员：… / 你：…」的逐块追加
   （绝不加载真模型）；缺模型 / 缺引擎两种降级各自的中文文案与按钮状态。
3. **侧写闭环**（任务书 5）—— 生成 → 编辑 → 保存 → 重读一致。
4. **三类单条删除**（任务书 3）—— 成绩 / 德育分 / 侧写各自的确认框文案、
   **取消 = 一个字节都不写**、删掉某学期最后一条成绩时的追加提示。

★ 几条硬规矩（都是本仓库踩过的坑）：

* ``QT_QPA_PLATFORM=offscreen`` **必须在 import PySide6 之前**设好，否则测试
  进程里没有显示器，``QApplication()`` 那一步直接退出。
* 模态弹窗（``QMessageBox``）在无头环境里会把测试**挂住**（AGENTS §32 的教训）
  —— 本文件里每一处弹窗都被换成探针，一个真的弹窗都不许有。
* 真线程一律桩掉：``ProfilePage._start_worker`` 换成"就地调 ``run()``"，
  于是信号是**同步**派发的，不需要转事件循环，也不会有"跑完的 QThread 被回收"
  那类事故（AGENTS §4k 同族）。
* 假引擎只实现被测代码真正用到的那几个成员（``model_path`` / ``load`` /
  ``stream``），**一个真模型都不加载**。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

# ★ 必须在 import PySide6 / 建任何控件之前设好
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
)

from core.archival import (  # noqa: E402
    Archive,
    CourseSnapshot,
    SemesterRecord,
    StudentArchive,
    archive_key,
)
from core.crypto_store import CryptoStore  # noqa: E402
from core.database import GradeHistoryDB, ProfileStore  # noqa: E402
from core.scholarship import (  # noqa: E402
    ClassRecord,
    load_moral,
    save_class,
    save_moral,
)
from ui.pages.profile_page import (  # noqa: E402
    CHAT_HISTORY_DIR,
    CHAT_HISTORY_PREFIX,
    CHAT_HISTORY_SUFFIX,
    CHAT_ROLE_ASSISTANT,
    CHAT_ROLE_USER,
    CHAT_SYSTEM_PROMPT_TEMPLATE,
    DELETE_CONFIRM_TEXT,
    DELETE_TERM_PROMPT_TEXT,
    GRADE_TREE_COLUMNS,
    ProfilePage,
    build_chat_system_prompt,
    build_student_json,
    moral_text,
    read_side_text,
    store_archive,
)

USER = "张老师"
PASSWORD = "tl-test-2026"
CLASS_NAME = "CS2301"
TERM = "大一上"

STUDENT = "张三"
STUDENT_ID = "2023001"
KEY = archive_key(STUDENT_ID, STUDENT)

#: 两门必修课（``Category.REQUIRED`` 的**值**就是"必修" —— 归档序列化后
#: 存的是字符串，这里直接用同一个字符串，round-trip 才不会有惊喜）。
MATH = ("高等数学", 4.0, "必修")
PHYS = ("大学物理", 3.0, "必修")

SIDE_TEXT = "这名学生本学期稳步上升，建议保持当前节奏，下学期重点补英语。"

ANSWER_CHUNKS = ("这名学生", "成绩在上升，", "建议找他聊一次。")


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class _FakeEngine:
    """够 :func:`LocalModelEngine.stream` 那个用法的假引擎（**不加载任何模型**）。"""

    def __init__(self, chunks: tuple[str, ...] = ANSWER_CHUNKS,
                 error: BaseException | None = None) -> None:
        self.model_path = "C:/fake/model.gguf"
        self.chunks = tuple(chunks)
        self.error = error
        self.loaded = False
        self.seen_prompt = ""

    def load(self) -> bool:
        self.loaded = True
        return True

    def stream(self, prompt: str, *args: Any, **kwargs: Any):
        self.seen_prompt = str(prompt)
        if self.error is not None:
            raise self.error
        for piece in self.chunks:
            yield piece


class _FakeModel:
    """够 :func:`list_local_models` 那个用法的假模型条目。"""

    def __init__(self, path: str = "C:/fake/model.gguf", usable: bool = True) -> None:
        self.path = path
        self.name = Path(path).stem
        self.usable = usable

    def display(self) -> str:
        return self.name


class _Stub:
    """一个可调用的探针：记下每次调用的实参，并给一个固定的返回值。"""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.result

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def texts(self) -> list[str]:
        """每次调用的"正文"实参（``QMessageBox.*`` 的第二个位置参数）。"""
        return [str(call["args"][2]) for call in self.calls if len(call["args"]) > 2]

    @property
    def titles(self) -> list[str]:
        return [str(call["args"][1]) for call in self.calls if len(call["args"]) > 1]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> CryptoStore:
    """注册好的临时加密库。★ **必须先 register**，否则 ``user_file`` 解析不到 uid。"""
    box = CryptoStore(root=tmp_path / "vault")
    box.register(USER, PASSWORD)
    return box


@pytest.fixture(scope="module")
def qapp() -> Any:
    """整个模块共用一个 QApplication（offscreen）。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """把后台线程换成"就地跑" —— 信号同步派发，测试不需要事件循环。

    ★ 用 ``*args, **kwargs`` 接：``ProfilePage._start_worker`` 是个
      ``@staticmethod``，调用点可能多传一个模块参数，写死一个形参会炸
      ``takes 1 positional argument but 2 were given``。
    """
    monkeypatch.setattr(
        ProfilePage, "_start_worker",
        # ★ 必须包一层 staticmethod：裸 lambda 挂到类上就成了**绑定方法**，
        #   ``self._start_worker(worker)`` 会把 self 当第一个实参传进来
        #   （症状是 ``'ProfilePage' object has no attribute 'run'``）。
        staticmethod(lambda worker, *args, **kwargs: worker.run()),
        raising=True,
    )


def _archive(scores: list[Any], *, courses: list[tuple[str, float, str]] | None = None,
             term: str = TERM, student: str = STUDENT,
             student_id: str = STUDENT_ID) -> Archive:
    """亲手拼一份归档（**不经过解析器**，测试才不依赖任何别人的夹具）。

    ★ 归档 ``as_dict()`` 之后存进加密库、读回来是 ``StudentArchive.from_dict()``
      —— 本工厂出来的对象与那条往返路径**同构**（字段逐一对齐）。
    """
    items = list(courses if courses is not None else [MATH, PHYS])
    snapshots = [
        CourseSnapshot(name=name, credit=float(credit), category=str(category),
                       score=scores[index] if index < len(scores) else None)
        for index, (name, credit, category) in enumerate(items)
    ]
    record = SemesterRecord(term=term, student=student, student_id=student_id,
                            courses=snapshots)
    key = archive_key(student_id, student)
    return Archive(students={
        key: StudentArchive(key=key, student=student, records=[record])
    })


def _archive_for(scores: list[Any]) -> Archive:
    """两名必修课、一个学期的归档（``scores`` 与 ``[MATH, PHYS]`` 一一对应）。"""
    return _archive(scores)


def _student(archive: Any, student: str = STUDENT,
             student_id: str = STUDENT_ID) -> Any:
    """从整库归档里取出**一名学生**的档案。

    ★ 传 ``Archive`` 进来是常犯的错：``core.archival.trend()`` 吃的是
      ``StudentArchive``（它要 ``.terms``），不是整库。
    """
    if archive is None:
        return None
    found = archive.get(archive_key(student_id, student))
    if found is not None:
        return found
    students = getattr(archive, "students", None) or {}
    return next(iter(students.values()), None)


def _payload(archive: Any = None, *, student: str = STUDENT,
             student_id: str = STUDENT_ID) -> dict[str, Any]:
    """档案页真正吃的学生包（主窗 ``_payload_for`` 那种形状）。

    ``archive`` 那一格放的是**这个学生**的 :class:`StudentArchive`。
    """
    return {
        "student": student,
        "student_id": student_id,
        "key": archive_key(student_id, student),
        "results": {},
        "archive": _student(archive, student, student_id),
    }


def _seed(vault: CryptoStore, *, moral: bool = True, grades: bool = True) -> Any:
    """把一名学生的数据铺好：归档 + 成绩历史表 + 班级库 + 德育分。

    ★ **班级库那一份不能省**：德育分是按 ``class_<班级号>_moral.json.enc`` 存的，
      而 :func:`ui.pages.profile_page.moral_of` 找班的路径是
      ``core.scholarship.list_classes``（扫 ``class_<班级号>.json.enc``）——
      不建班档案的话，德育分写进去了也读不回来（症状：界面写「未录入」），
      班级号同理取不到（症状：「未标注」）。真实流程里分析页导入时就建了班。
    """
    archive = _archive_for([88.0, 91.0])
    store_archive(vault, USER, archive)
    save_class(vault, USER, ClassRecord(
        class_name=CLASS_NAME,
        source="test",
        term=TERM,
        students=[{"student": STUDENT, "student_id": STUDENT_ID, "key": KEY}],
    ))
    if grades:
        GradeHistoryDB(store=vault, username=USER).apply(
            [
                {"student_id": STUDENT_ID, "semester": TERM, "course_name": MATH[0],
                 "score": 88.0, "credit": 4.0, "class_name": CLASS_NAME},
                {"student_id": STUDENT_ID, "semester": TERM, "course_name": PHYS[0],
                 "score": 91.0, "credit": 3.0, "class_name": CLASS_NAME},
            ],
            semester=TERM,
            batch_id="test-batch",
        )
    if moral:
        save_moral(vault, USER, CLASS_NAME, {KEY: 86})
    return archive


@pytest.fixture()
def page(vault: CryptoStore, qapp: Any) -> Any:
    widget = ProfilePage(store=vault)
    widget.set_user(USER)
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _fresh(vault: CryptoStore, qapp: Any) -> ProfilePage:
    """另起一页（= 重启程序的等价场景）。"""
    widget = ProfilePage(store=vault)
    widget.set_user(USER)
    return widget


def _show(page: ProfilePage, archive: Any = None) -> None:
    page.show_student(_payload(archive))


def _student_of(archive: Any, key: str = KEY) -> Any:
    """一份**学生**档案（传进来的本来就是 :class:`StudentArchive`）。"""
    if archive is None:
        return None
    if hasattr(archive, "records"):
        return archive
    return archive.get(key)


def _term_record(archive: Any, term: str = TERM, key: str = KEY) -> Any:
    student = _student_of(archive, key)
    for record in getattr(student, "records", None) or ():
        if getattr(record, "term", "") == term:
            return record
    return None


def _course_names(archive: Any, term: str = TERM) -> list[str]:
    record = _term_record(archive, term)
    if record is None:
        return []
    return [course.name for course in record.courses]


def _grade_items(page: ProfilePage) -> list[QTreeWidgetItem]:
    """树上所有**课程行**（学期分组头不在 ``_grade_payloads`` 里）。"""
    tree = page._grade_tree
    items: list[QTreeWidgetItem] = []
    for index in range(tree.topLevelItemCount()):
        head = tree.topLevelItem(index)
        items.extend(head.child(row) for row in range(head.childCount()))
    return items


def _grade_item(page: ProfilePage, course: str) -> QTreeWidgetItem:
    for item in _grade_items(page):
        if item.text(0) == course:
            return item
    raise AssertionError(f"树上没有这门课：{course}")


def _vault_digest(vault: CryptoStore) -> dict[str, str]:
    """整个数据目录的逐文件 sha256（多层子目录都算）——判"一个字节都没写"。"""
    digest: dict[str, str] = {}
    for path in sorted(vault.root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(vault.root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest


def _plug_engine(page: ProfilePage, engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """装上一个假引擎，并把"引擎装没装"探针一起改成"装了"。"""
    page.set_chat_engine(engine)
    monkeypatch.setattr(
        "ui.pages.profile_page.engine_available", lambda: (True, "")
    )


# ---------------------------------------------------------------------------
# 一、三个标签页
# ---------------------------------------------------------------------------


def test_three_tabs_live_side_by_side(page: ProfilePage) -> None:
    assert page._tabs.count() == 3
    assert [page._tabs.tabText(i) for i in range(3)] == [
        "成绩明细", "总评与侧写", "AI 辅导"
    ]


def test_ai_tab_has_a_card_a_read_only_view_and_an_input_row(page: ProfilePage) -> None:
    ai = page._tabs.widget(2)

    assert ai is not None
    views = ai.findChildren(QTextEdit)
    assert views and all(view.isReadOnly() for view in views)
    inputs = ai.findChildren(QLineEdit)
    assert inputs and inputs[0].isEnabled() is False      # 没选学生 → 还不能发
    buttons = [button.text() for button in ai.findChildren(QPushButton)]
    assert "发送" in buttons
    assert "打开模型中心" in buttons


def test_ai_tab_container_never_carries_a_graphics_effect(page: ProfilePage) -> None:
    """★ AGENTS §17：页面容器挂透明度效果会让整页画成空白。"""
    ai = page._tabs.widget(2)
    assert ai is not None
    assert ai.graphicsEffect() is None


def test_student_card_shows_name_id_and_class(page: ProfilePage, vault: CryptoStore) -> None:
    """姓名 / 学号一定在卡片上；班级取**真源**（班级库 ``class_CS2301.json.enc``）。

    ★ ``grade_history`` 表**不存班级号**（``_clean_records`` 只保留学号/学期/
      课程/成绩/学分/批次/时间 —— 全库 grep 确认过）。班级的真源是
      ``core.scholarship`` 的班级库，所以 :func:`_seed` 里那份 ``save_class``
      不能省；"没有真源时写「未标注」"由
      :func:`test_without_a_class_record_the_card_says_unmarked` 覆盖。
    """
    _seed(vault)
    _show(page, _archive_for([88.0, 91.0]))

    assert STUDENT in page._chat_name.text()
    assert STUDENT_ID in page._chat_name.text()
    assert STUDENT_ID in page._chat_meta.text()
    assert page._chat_meta.text().endswith(f"班级：{CLASS_NAME}")
    assert page._chat_avatar.text() == STUDENT[0]


def test_without_a_class_record_the_card_says_unmarked(
    page: ProfilePage, vault: CryptoStore
) -> None:
    """一个班档案都没有时，班级写「未标注」—— **绝不编一个班号**。"""
    archive = _archive_for([88.0, 91.0])
    store_archive(vault, USER, archive)
    _show(page, archive)

    assert page._chat_meta.text().endswith("班级：未标注")


def test_class_name_reaches_the_card_and_the_json(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """历史表里**有**班级号时，卡片与注入的 JSON 都要带上它。"""
    archive = _seed(vault, grades=False)
    monkeypatch.setattr(
        ProfilePage, "_class_of", lambda self, student_id: CLASS_NAME, raising=True
    )
    _show(page, archive)

    assert f"班级：{CLASS_NAME}" in page._chat_meta.text()
    body = page._chat_system_prompt.split("以下是该学生的数据：", 1)[1]
    data = json.loads(body.rsplit("。请根据", 1)[0])
    assert data["班级"] == CLASS_NAME


# ---------------------------------------------------------------------------
# 二、学生数据以 JSON 进系统提示词
# ---------------------------------------------------------------------------


def test_system_prompt_keeps_the_template_wording_verbatim() -> None:
    prompt = build_chat_system_prompt('{"学号": "2023001"}')
    assert prompt == CHAT_SYSTEM_PROMPT_TEMPLATE.replace(
        "{学生JSON}", '{"学号": "2023001"}'
    )
    assert prompt.startswith("你是一名高校辅导员助理，正在协助辅导员与一名学生进行谈话。")
    assert prompt.endswith("请根据这些数据提供谈话建议、侧写分析或评语润色。")
    assert "**" not in prompt and "`" not in prompt        # §24：界面文案零 Markdown


def test_student_json_carries_id_grades_moral_and_side_text() -> None:
    text = build_student_json(
        STUDENT_ID, STUDENT, CLASS_NAME,
        [{"term": TERM, "course": MATH[0], "score": 88.0, "credit": 4.0},
         {"term": TERM, "course": PHYS[0], "score": None, "credit": 3.0}],
        86, SIDE_TEXT,
    )
    data = json.loads(text)

    assert data["学号"] == STUDENT_ID
    assert data["姓名"] == STUDENT
    assert data["班级"] == CLASS_NAME
    assert data["德育分"] == 86
    assert data["已有侧写"] == SIDE_TEXT
    by_course = {row["course"]: row for row in data["成绩"]}
    assert by_course[MATH[0]]["score"] == 88.0
    assert by_course[MATH[0]]["semester"] == TERM
    assert by_course[PHYS[0]]["score"] is None            # 空 ≠ 0（红线 2）


def test_missing_moral_stays_null_in_the_json() -> None:
    data = json.loads(build_student_json(STUDENT_ID, STUDENT, CLASS_NAME, [], None, ""))
    assert data["德育分"] is None
    assert data["成绩"] == []
    assert moral_text(None) == "未录入"                    # 没录 ≠ 0 分


def test_page_injects_this_students_json_into_the_system_prompt(
    page: ProfilePage, vault: CryptoStore
) -> None:
    archive = _seed(vault)
    ProfileStore(store=vault, username=USER).save_side_text(KEY, SIDE_TEXT)
    _show(page, archive)

    prompt = page._chat_system_prompt
    assert prompt.startswith("你是一名高校辅导员助理，")
    assert SIDE_TEXT in prompt

    body = prompt.split("以下是该学生的数据：", 1)[1].rsplit("。请根据", 1)[0]
    data = json.loads(body)
    assert data["学号"] == STUDENT_ID
    assert data["姓名"] == STUDENT
    assert data["班级"] == CLASS_NAME
    assert data["德育分"] == 86
    assert data["已有侧写"] == SIDE_TEXT
    assert {row["course"] for row in data["成绩"]} == {MATH[0], PHYS[0]}
    assert json.loads(page._chat_student_json) == data


# ---------------------------------------------------------------------------
# 三、流式追加（假引擎）
# ---------------------------------------------------------------------------


def test_streaming_chunks_are_appended_in_order(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault)
    engine = _FakeEngine()
    _plug_engine(page, engine, monkeypatch)
    _show(page, archive)

    assert page._chat_input.isEnabled() is True
    assert page.send_chat("最近成绩怎么样？") is True

    shown = page._chat_view.toPlainText()
    assert "辅导员：最近成绩怎么样？" in shown
    assert "你：" + "".join(ANSWER_CHUNKS) in shown
    assert engine.loaded is True
    assert "最近成绩怎么样？" in engine.seen_prompt


def test_a_finished_turn_is_written_to_the_encrypted_history(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault)
    engine = _FakeEngine()
    _plug_engine(page, engine, monkeypatch)
    _show(page, archive)

    page.send_chat("聊两句")
    assert [turn["role"] for turn in page._chat_history] == [
        CHAT_ROLE_USER, CHAT_ROLE_ASSISTANT
    ]
    assert page._chat_history[1]["text"] == "".join(ANSWER_CHUNKS)

    # 磁盘上只有 .enc，而且明文谈话内容一个字都扫不到（红线 3）
    folder = vault.user_dir(vault.load_users().find(USER).uid)
    files = sorted(path.relative_to(folder).as_posix()
                   for path in folder.rglob("*") if path.is_file())
    assert (f"{CHAT_HISTORY_DIR}{CHAT_HISTORY_PREFIX}{STUDENT_ID}"
            f"{CHAT_HISTORY_SUFFIX}") in files
    assert all(name.endswith(".enc") for name in files), files
    secret = "".join(ANSWER_CHUNKS).encode("utf-8")
    assert [
        path for path in vault.root.rglob("*")
        if path.is_file() and secret in path.read_bytes()
    ] == []


def test_chat_history_is_per_student_and_survives_a_restart(
    page: ProfilePage, vault: CryptoStore, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault)
    _plug_engine(page, _FakeEngine(), monkeypatch)
    _show(page, archive)
    page.send_chat("第一次谈话")

    second = _fresh(vault, qapp)
    second.show_student(
        _payload(archive, student="李四", student_id="2023002")
    )
    # ★ 按学生分开存：换一个人就是一份空记录（不串号）
    assert second._chat_history == []
    assert second._chat_view.toPlainText() == ""

    second.show_student(_payload(archive))
    texts = [turn["text"] for turn in second._chat_history]
    assert "第一次谈话" in texts
    assert "".join(ANSWER_CHUNKS) in texts
    second.deleteLater()


def test_a_failing_engine_reports_a_chinese_reason_and_keeps_the_page_alive(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.ai.inference import InferenceUnavailable

    archive = _seed(vault)
    _plug_engine(page, _FakeEngine(error=InferenceUnavailable("模型文件不见了")), monkeypatch)
    _show(page, archive)

    assert page.send_chat("在吗") is True
    assert "模型文件不见了" in page._chat_status.text()
    # 失败也不许静默：这一轮没有回答，但窗口照常可用，输入框也回来了
    assert page._chat_view.toPlainText().strip() == "辅导员：在吗"
    assert page._chat_input.isEnabled() is True


# ---------------------------------------------------------------------------
# 四、两级降级：缺模型 / 缺引擎
# ---------------------------------------------------------------------------


def test_missing_model_asks_the_user_to_download_one(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault, grades=False)
    page.set_chat_models([])
    monkeypatch.setattr("ui.pages.profile_page.engine_available", lambda: (True, ""))
    _show(page)

    assert "请先在模型中心下载模型" in page._chat_status.text()
    assert page._chat_input.isEnabled() is False
    assert page._chat_send.isEnabled() is False
    # ★ 用 isHidden() 而不是 isVisible()：离屏用例里页面本身没 show()，
    #   子控件的 isVisible() 永远 False（那样断言等于什么都没验）。
    assert page._model_center_btn.isHidden() is False
    assert page.send_chat("你好") is False


def test_missing_engine_quotes_the_reason_from_engine_available(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault, grades=False)
    reason = "本机没有装本地推理引擎（llama-cpp-python），AI 功能用不了。"
    page.set_chat_models([_FakeModel()])
    monkeypatch.setattr(
        "ui.pages.profile_page.engine_available", lambda: (False, reason)
    )
    _show(page)

    assert page._chat_status.text() == reason
    assert page._chat_input.isEnabled() is False
    # 模型是有的（只是引擎没装）→ **不**弹「打开模型中心」的按钮：
    # 那按钮治的是"缺模型"，跟"缺引擎"是两回事（点了也解决不了）。
    assert page._model_center_btn.isHidden() is True


def test_both_missing_reports_both_reasons(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """引擎和模型都没有时**两条都要说** —— 只报一条会把人带进坑里。"""
    _seed(vault, grades=False)
    page.set_chat_models([])
    monkeypatch.setattr(
        "ui.pages.profile_page.engine_available", lambda: (False, "引擎没装")
    )
    _show(page)

    status = page._chat_status.text()
    assert "引擎没装" in status
    assert "请先在模型中心下载模型" in status


def test_model_center_button_reports_a_missing_dialog_instead_of_crashing(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型中心**打不开**（import 失败）时只写一句中文，绝不抛异常、绝不静默。"""
    _seed(vault, grades=False)
    page.set_chat_models([])
    monkeypatch.setattr("ui.pages.profile_page.engine_available", lambda: (True, ""))
    _show(page)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ImportError("模型中心模块缺失（测试桩）")

    from ui import model_center_dialog as module
    monkeypatch.setattr(module, "open_model_center", _boom, raising=False)

    page._open_model_center()                              # 不抛异常
    assert page._chat_status.text() == "模型中心暂时打不开，请看日志。"


def test_model_center_button_opens_the_dialog_without_a_modal_hang(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 真弹窗在无头环境里会把测试挂死 —— 这里把 ``open_model_center`` 换成桩，
    只断言它**被真的调到**（懒 import 走通），并且调用后状态行刷新。"""
    _seed(vault, grades=False)
    page.set_chat_models([])
    monkeypatch.setattr("ui.pages.profile_page.engine_available", lambda: (True, ""))
    _show(page)
    calls: list[Any] = []

    def _open(parent: Any = None, **kwargs: Any) -> int:
        calls.append(parent)
        return 0

    from ui import model_center_dialog as module
    monkeypatch.setattr(module, "open_model_center", _open, raising=False)

    page._open_model_center()

    assert calls == [page]                                 # 真的把这一页当父窗传进去了
    assert "请先在模型中心下载模型" in page._chat_status.text()


# ---------------------------------------------------------------------------
# 五、侧写闭环：生成 → 编辑 → 保存 → 重读
# ---------------------------------------------------------------------------


def test_side_text_is_editable_and_saves_back(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault)
    engine = _FakeEngine(("初稿：", "建议多做题。"))
    _plug_engine(page, engine, monkeypatch)
    _show(page, archive)

    assert page._side_text.isReadOnly() is False
    page._generate_side_text()
    assert page._side_text.toPlainText() == "初稿：建议多做题。"
    assert page._side_save.isEnabled() is True            # 生成完就能保存了

    page._side_text.setPlainText("改过的正文：建议先补英语。")
    assert page._side_save.isEnabled() is True
    page._on_side_save_clicked()

    stored = read_side_text(
        ProfileStore(store=vault, username=USER), "", KEY
    )
    assert stored == "改过的正文：建议先补英语。"
    assert "已保存" in page._side_status.text()

    # 换一个实例读回来（= 真的落盘了）
    other = ProfilePage(store=vault)
    other.set_user(USER)
    other.show_student(_payload(archive))
    assert other._side_text.toPlainText() == "改过的正文：建议先补英语。"
    assert other._ai_btn.text() == "重新生成"
    other.deleteLater()


def test_side_text_generation_without_a_model_gives_a_chinese_reason(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault)
    page.set_chat_models([])
    monkeypatch.setattr("ui.pages.profile_page.engine_available", lambda: (True, ""))
    _show(page, archive)

    page._generate_side_text()
    assert page._side_status.text() == "生成失败：请先在模型中心下载模型"
    assert page._ai_btn.isEnabled() is True               # 按钮恢复可用
    assert page._side_text.toPlainText() == ""


def test_empty_side_text_shows_the_generate_button(
    page: ProfilePage, vault: CryptoStore
) -> None:
    archive = _seed(vault, grades=False)
    _show(page, archive)
    assert page._ai_btn.text() == "点击生成"


# ---------------------------------------------------------------------------
# 六、三类单条删除
# ---------------------------------------------------------------------------


def test_grade_tree_has_an_action_column(page: ProfilePage) -> None:
    assert GRADE_TREE_COLUMNS[-1] == "操作"
    assert page._grade_tree.columnCount() == len(GRADE_TREE_COLUMNS)
    assert page._grade_tree.headerItem().text(len(GRADE_TREE_COLUMNS) - 1) == "操作"


def test_deleting_a_grade_row_asks_first_and_writes_nothing_on_cancel(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault, moral=False, grades=False)
    question = _Stub(QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, archive)

    before = _vault_digest(vault)
    assert page.on_grade_action(_grade_item(page, MATH[0])) is False

    assert question.count == 1
    assert question.texts == [DELETE_CONFIRM_TEXT]
    assert _vault_digest(vault) == before                 # ★ 取消 = 一个字节都不写
    assert _course_names(archive) == [MATH[0], PHYS[0]]


def test_deleting_a_grade_row_removes_exactly_that_row(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault)
    question = _Stub(QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, archive)

    assert page.on_grade_action(_grade_item(page, MATH[0])) is True

    assert question.texts == [DELETE_CONFIRM_TEXT]         # 学期里还剩一条 → 不再追问
    assert _course_names(archive) == [PHYS[0]]
    assert [item.text(0) for item in _grade_items(page)] == [PHYS[0]]


def test_deleting_the_last_grade_of_a_term_asks_about_the_term(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉某学期最后一条成绩 → 追加问一次"要不要连学期记录一起删"。"""
    archive = _archive([88.0], courses=[MATH])
    store_archive(vault, USER, archive)
    question = _Stub(QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, archive)

    assert page.on_grade_action(_grade_item(page, MATH[0])) is True

    assert question.count == 2
    assert question.texts == [DELETE_CONFIRM_TEXT, DELETE_TERM_PROMPT_TEXT]
    assert question.texts[1] == "该学期已无成绩，是否同时删除学期记录？"
    # 学期分组从本页移除
    assert page._grade_tree.topLevelItemCount() == 0
    assert TERM in page._hidden_terms


def test_deleting_the_last_grade_can_keep_the_term_group(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """追加提示里选「否」→ 只删这一条，学期记录留着。"""
    archive = _archive([88.0], courses=[MATH])
    store_archive(vault, USER, archive)
    question = _Stub(QMessageBox.StandardButton.No)
    question.result = QMessageBox.StandardButton.No
    answers = [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No]

    def _ask(*args: Any, **kwargs: Any) -> Any:
        question.calls.append({"args": args, "kwargs": kwargs})
        return answers[min(len(question.calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(QMessageBox, "question", _ask)
    _show(page, archive)

    assert page.on_grade_action(_grade_item(page, MATH[0])) is True
    assert question.count == 2
    assert TERM not in page._hidden_terms
    assert _course_names(archive) == []


def test_grade_group_header_is_not_deletable(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault)
    question = _Stub(QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, archive)

    head = page._grade_tree.topLevelItem(0)
    assert page.on_grade_action(head) is False
    assert question.count == 0                            # 连确认框都不该弹


def test_deleting_the_moral_row_asks_first_and_writes_nothing_on_cancel(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(vault)
    page._moral_cache = [CLASS_NAME]                      # 先扫好，免得多写一次盘
    question = _Stub(QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, _archive_for([88.0, 91.0]))
    assert "86" in page._moral_row.text()

    before = _vault_digest(vault)
    page._delete_moral_row()

    assert question.texts == [DELETE_CONFIRM_TEXT]
    assert _vault_digest(vault) == before                 # ★ 取消 = 一个字节都不写
    assert load_moral(vault, USER, CLASS_NAME)[KEY] == 86


def test_deleting_the_moral_row_clears_it_and_refreshes(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 建一个班（德育分挂在班级上），先把分录进去
    _seed(vault)
    question = _Stub(QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, _archive_for([88.0, 91.0]))
    assert "86" in page._moral_row.text()

    page._delete_moral_row()

    assert question.texts == [DELETE_CONFIRM_TEXT]
    assert moral_text(load_moral(vault, USER, CLASS_NAME).get(KEY)) == "未录入"
    assert "未录入" in page._moral_row.text()             # 列表刷新过了


def test_deleting_a_side_text_record_asks_first_and_writes_nothing_on_cancel(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault, grades=False)
    ProfileStore(store=vault, username=USER).save_side_text(KEY, SIDE_TEXT)
    question = _Stub(QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, archive)
    assert page._side_text.toPlainText() == SIDE_TEXT
    assert page._side_delete.isEnabled() is True

    before = _vault_digest(vault)
    page._delete_side_text()

    assert question.texts == [DELETE_CONFIRM_TEXT]
    assert _vault_digest(vault) == before                 # ★ 取消 = 一个字节都不写
    assert read_side_text(ProfileStore(store=vault, username=USER), "", KEY) == SIDE_TEXT


def test_deleting_a_side_text_record_clears_it(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _seed(vault, grades=False)
    ProfileStore(store=vault, username=USER).save_side_text(KEY, SIDE_TEXT)
    question = _Stub(QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", question)
    _show(page, archive)

    page._delete_side_text()

    assert question.texts == [DELETE_CONFIRM_TEXT]
    assert read_side_text(ProfileStore(store=vault, username=USER), "", KEY) == ""
    assert page._side_text.toPlainText() == ""
    assert page._side_delete.isEnabled() is False         # 没记录了 → 按钮置灰
    assert "已删除" in page._side_status.text()


def test_switching_students_keeps_each_ones_deletions_apart(
    page: ProfilePage, vault: CryptoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换人之后删除只影响当前这个人（学号是键，AGENTS §9）。"""
    archive = _seed(vault, grades=False)
    other_key = archive_key("2023002", "李四")
    ProfileStore(store=vault, username=USER).save_side_text(other_key, "李四的侧写")
    question = _Stub(QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", question)

    page.show_student(_payload(archive, student="李四", student_id="2023002"))
    page._delete_side_text()

    store = ProfileStore(store=vault, username=USER)
    assert read_side_text(store, "", other_key) == ""
    # 张三那条**一个字都没动**
    ProfileStore(store=vault, username=USER).save_side_text(KEY, SIDE_TEXT)
    # ★ 换一个实例再读：``ProfileStore`` 是"先写盘、成功了才更新内存"，
    #   上面那句写盘走的是**另一个**实例，留在手里的这份 `_cache` 还是旧的。
    assert read_side_text(ProfileStore(store=vault, username=USER), "", KEY) == SIDE_TEXT
