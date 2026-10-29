"""档案页两级视图的名单 / 导航行为测试（v1.9.2）。

覆盖任务书 ①–⑬：

* ① 有主档时名单行数 == ``StudentBook.records()`` 行数
* ② 主档为空、只有 ``set_roster`` 推来的名单时，名单仍然不为空
* ③ 搜索框按姓名 / 学号 / 班级过滤
* ④ 新建学生 → 名单多一行，学生主档里也有
* ⑤ 学号重复 → ``DuplicateStudentError`` → 中文提示（弹窗被替换）
* ⑥ 编辑时改学号 → 先二次确认；选「迁移历史数据」会调用迁移并记录实参
* ⑦ 「仅归档」→ 名单少一行，但 ``records(include_archived=True)`` 仍在
* ⑧ 点学期节点 / ``open_semester("大一上")`` → 第 1 层 + 学期页拿到 term 与记录
* ⑨ 学期页「← 返回档案」→ 回到第 0 层
* ⑩ ``reset_view()`` 后回到第 0 层
* ⑪ ``show_no_data()`` 后名单清空
* ⑫ ``current_student_id()``：选中非空、未选空串（``sid:`` 前缀会被剥掉）
* ⑬ 界面 ``QLabel`` / 按钮 / 名单行 / 搜索框占位符的文字里没有 Markdown 记号

★ 本文件**只新增、只观察**：``ui/pages/profile_page.py``、``profile_shared.py``、
  ``semester_side_profile.py`` 一个字节都不动 —— 发现对不上的地方只记录在
  测试名 / 断言里，不改实现。
★ 所有模态框（``QMessageBox`` / ``QInputDialog`` / ``StudentDialog``）都被替换：
  真弹模态会把测试看门狗（120 秒）跑满（AGENTS.md §32）。
"""

from __future__ import annotations

import os

# ★ 必须在 import PySide6 之前：离屏渲染，测试不需要真的开窗口。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
from typing import Any

import pytest

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QToolButton,
)

from core.crypto_store import CryptoStore
from core.database import GradeHistoryDB, StudentStore
from ui.pages import profile_page as page_module
from ui.pages import profile_shared
from ui.pages.analysis_page import TableRow
from ui.pages.profile_page import ProfilePage
from ui.pages.profile_shared import (
    DuplicateStudentError,
    TermNode,
    build_empty_student_archive,
    open_student_book,
)

USER = "张老师"
PASSWORD = "sch12345"

SID_A = "20230101"
SID_B = "20230102"
SID_C = "20230103"
SID_NEW = "20230199"

NAME_A = "张三"
NAME_B = "李四"
NAME_C = "王五"

CLASS_A = "计科2301"
CLASS_C = "软工2302"

TERM_A = "大一上"
TERM_B = "大一下"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Any) -> CryptoStore:
    """注册好的临时加密库。

    ★ **必须先 register**：``user_file`` 按 ``users.json`` 里的 uid 解析路径，
      没有账户的话写盘会抛 ``CryptoError``（AGENTS §20c）。
    ★ 夹具名不能叫 ``store`` —— 会遮蔽 ``CryptoStore`` 这个名字。
    """
    box = CryptoStore(root=tmp_path / "vault")
    box.register(USER, PASSWORD)
    return box


@pytest.fixture(scope="module")
def qapp() -> Any:
    """整个模块共用一个 ``QApplication``（offscreen）。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """把后台线程换成"就地跑"：信号同步派发，测试不需要事件循环。

    ★ 必须包一层 ``staticmethod``：裸 lambda 挂到类上会变成绑定方法，
      ``self._start_worker(worker)`` 会把 self 当第一个实参传进去。
    """
    monkeypatch.setattr(
        ProfilePage,
        "_start_worker",
        staticmethod(lambda worker, *args, **kwargs: worker.run()),
        raising=True,
    )


@pytest.fixture()
def make_page(vault: CryptoStore, qapp: Any) -> Any:
    """按需创建档案页（创建前可以先往加密库里铺数据）。"""
    created: list[ProfilePage] = []

    def _make(username: str = USER) -> ProfilePage:
        widget = ProfilePage(store=vault)
        widget.set_user(username)
        created.append(widget)
        return widget

    yield _make
    for widget in created:
        # ★ 先关窗再删：``ProfilePage`` 持有后台线程 / 定时器，
        #   直接 ``deleteLater`` 会把它们留到下一个用例里（实测：本文件最后
        #   三条用例前面有“学期页”产生的残留，单跑能过、连着跑就挂住）。
        widget.close()
        widget.deleteLater()
    qapp.processEvents()


@pytest.fixture()
def page(make_page: Any) -> ProfilePage:
    """一个已经 ``set_user`` 过的空档案页。"""
    return make_page()


# ---------------------------------------------------------------------------
# 模态框替身（★ 全部替换，谁都不许真 exec）
# ---------------------------------------------------------------------------


class _Button:
    """``QMessageBox.addButton`` 的返回值：只用来做 ``is`` 比较。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - 只为排查时好看
        return f"<按钮 {self.text}>"


class _FakeMessageBox(QMessageBox):
    """可编排的 ``QMessageBox``。

    * ``pick``：要"点"的按钮文字；``exec()`` 不阻塞，``clickedButton()`` 返回它。
    * ``instances``：构造过的自定义框（二次确认 / 删除确认），用来断言文案。
    * ``information_calls`` / ``critical_calls``：静态提示调用的 ``(标题, 正文)``。
    """

    pick: str = ""
    instances: list["_FakeMessageBox"] = []
    information_calls: list[tuple[str, str]] = []
    critical_calls: list[tuple[str, str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.buttons: dict[str, _Button] = {}
        self.text_value = ""
        self.title_value = ""
        self._picked: _Button | None = None
        type(self).instances.append(self)

    # -- 只是记下来，不弹 --
    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        self.text_value = str(text)
        super().setText(text)

    def setWindowTitle(self, title: str) -> None:  # noqa: N802 - Qt 命名
        self.title_value = str(title)
        super().setWindowTitle(title)

    def addButton(self, text: str, role: Any = None) -> _Button:  # noqa: N802
        button = _Button(str(text))
        self.buttons[str(text)] = button
        return button

    def exec(self) -> int:  # ★ 不跑模态循环
        self._picked = self.buttons.get(type(self).pick)
        return 0

    def clickedButton(self) -> _Button | None:  # noqa: N802
        return self._picked

    @classmethod
    def information(cls, parent: Any = None, title: str = "", text: str = "",
                    *args: Any, **kwargs: Any) -> None:
        cls.information_calls.append((str(title), str(text)))
        return None

    @classmethod
    def critical(cls, parent: Any = None, title: str = "", text: str = "",
                 *args: Any, **kwargs: Any) -> None:
        cls.critical_calls.append((str(title), str(text)))
        return None


@pytest.fixture()
def boxes(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把档案页模块里的 ``QMessageBox`` 整体换掉（含 ``QMessageBox(self)`` 的用法）。"""

    class _MB(_FakeMessageBox):
        pass

    _MB.pick = ""
    _MB.instances = []
    _MB.information_calls = []
    _MB.critical_calls = []
    monkeypatch.setattr(page_module, "QMessageBox", _MB)
    return _MB


class _FakeInputDialog:
    """``QInputDialog.getItem`` 的替身：``answer`` 决定返回值。"""

    answer: tuple[str | None, bool] = (None, False)
    calls: list[dict[str, Any]] = []

    @classmethod
    def getItem(  # noqa: N802 - Qt 命名
        cls,
        parent: Any,
        title: str,
        label: str,
        items: list[str],
        current: int = 0,
        editable: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[str | None, bool]:
        cls.calls.append(
            {
                "title": str(title),
                "label": str(label),
                "items": list(items),
                "current": int(current),
            }
        )
        return cls.answer


@pytest.fixture()
def input_dialog(monkeypatch: pytest.MonkeyPatch) -> Any:
    class _D(_FakeInputDialog):
        pass

    _D.answer = (None, False)
    _D.calls = []
    monkeypatch.setattr(page_module, "QInputDialog", _D)
    return _D


class _FakeStudentDialog:
    """``StudentDialog`` 的替身：不弹表单，直接给出 ``values()``。

    ``values_to_return is None`` 表示用户点了「取消」。
    """

    values_to_return: dict[str, str] | None = None
    records: list[Any] = []
    titles: list[str] = []

    def __init__(self, parent: Any = None, *, record: Any = None,
                 title: str = "新建学生") -> None:
        self.parent = parent
        self.record = record
        type(self).records.append(record)
        type(self).titles.append(str(title))

    def exec(self) -> int:  # ★ 不跑模态循环
        return QDialog.Accepted if type(self).values_to_return is not None else QDialog.Rejected

    def values(self) -> dict[str, str]:
        return dict(type(self).values_to_return or {})

    def student_id(self) -> str:
        return str((type(self).values_to_return or {}).get("student_id", ""))


@pytest.fixture()
def student_dialog(monkeypatch: pytest.MonkeyPatch) -> Any:
    class _D(_FakeStudentDialog):
        pass

    _D.values_to_return = None
    _D.records = []
    _D.titles = []
    monkeypatch.setattr(page_module, "StudentDialog", _D)
    return _D


class _MigrateRecorder:
    """记录 ``migrate_student_id_for`` 的每次调用。"""

    def __init__(self) -> None:
        self.report = SimpleNamespace(as_text=lambda: "已迁移 3 项历史数据。")
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.report

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture()
def migration(monkeypatch: pytest.MonkeyPatch) -> _MigrateRecorder:
    """替换迁移助手。

    ★ 要**两个名字都换**：``profile_shared`` 是定义处，而
      ``profile_page`` 在 import 期就把函数绑进了自己的命名空间 ——
      只换 ``profile_shared`` 的话，档案页调的还是原来那个。
    """
    recorder = _MigrateRecorder()
    monkeypatch.setattr(profile_shared, "migrate_student_id_for", recorder)
    monkeypatch.setattr(page_module, "migrate_student_id_for", recorder)
    return recorder


# ---------------------------------------------------------------------------
# 数据 / 交互小工具
# ---------------------------------------------------------------------------


def _add_student(vault: CryptoStore, sid: str, name: str,
                 class_name: str = CLASS_A) -> None:
    """往学生主档（``students.json.enc``）里塞一名学生。"""
    StudentStore(username=USER, store=vault).add(
        student_id=sid, name=name, class_name=class_name
    )


def _seed_grades(vault: CryptoStore, sid: str, term: str,
                 rows: list[tuple[str, float, float]]) -> None:
    """往成绩历史表里铺一个学期的成绩（``(课程, 分数, 学分)``）。"""
    GradeHistoryDB(store=vault, username=USER).apply(
        [
            {"student_id": sid, "course_name": course, "score": score, "credit": credit}
            for course, score, credit in rows
        ],
        semester=term,
        batch_id=f"test-{term}",
    )


def _roster_row(index: int, student: str, student_id: str = "") -> TableRow:
    """真名单行类型（分析页 ``roster_changed`` 推的就是它）。"""
    return TableRow(index=index, student=student, student_id=student_id)


def _click_row(page: ProfilePage, position: int = 0) -> Any:
    """模拟鼠标点名单第 ``position`` 行。

    ★ 真点一行是两件事：``currentRow`` 挪过去（``_current_entry`` 读它），
      再发 ``itemClicked``（``_on_item_clicked`` 的接线）。
    """
    item = page._list.item(position)
    assert item is not None, f"名单里没有第 {position} 行"
    page._list.setCurrentRow(position)
    page._list.itemClicked.emit(item)
    return item


def _click_widget(widget: Any) -> None:
    """给 ``TermNode`` 发一个真的左键释放事件（不用显示窗口）。"""
    event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(4.0, 4.0),
        QPointF(4.0, 4.0),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.mouseReleaseEvent(event)


def _payload(sid: str, name: str) -> dict[str, Any]:
    """主窗 ``_payload_for`` 那种形状的学生包。"""
    key = f"sid:{sid}"
    return {
        "student": name,
        "student_id": sid,
        "key": key,
        "results": {},
        "archive": build_empty_student_archive(key, name, sid),
    }


# ---------------------------------------------------------------------------
# ① 名单行数 == 学生主档行数
# ---------------------------------------------------------------------------


def test_list_rows_come_from_student_book(make_page: Any, vault: CryptoStore) -> None:
    """① 主档有 3 个人 → 名单正好 3 行，且与 ``records()`` 一致。"""
    _add_student(vault, SID_A, NAME_A)
    _add_student(vault, SID_B, NAME_B)
    _add_student(vault, SID_C, NAME_C, CLASS_C)

    page = make_page()
    book = page._book

    assert book is not None and book.durable, "这块加密库里学生主档应当可用"
    assert page._list.count() == 3
    assert page.visible_count == 3
    assert page.roster_size == 3
    assert page._list.count() == len(book.records())
    assert [page._list.item(i).text() for i in range(3)] == [
        f"{NAME_A}（{SID_A}）",
        f"{NAME_B}（{SID_B}）",
        f"{NAME_C}（{SID_C}）",
    ]
    assert page._count_pill.text() == "3"
    assert page._list_note.text() == "共 3 人"


def test_list_row_label_uses_bare_sid(make_page: Any, vault: CryptoStore) -> None:
    """名单行文案是 ``姓名（裸学号）`` —— 不带 ``sid:`` 前缀。"""
    _add_student(vault, SID_A, NAME_A)

    page = make_page()

    assert page._list.item(0).text() == f"{NAME_A}（{SID_A}）"
    assert "sid:" not in page._list.item(0).text()


# ---------------------------------------------------------------------------
# ② 主档为空，但 roster 有数据 → 名单不为空
# ---------------------------------------------------------------------------


def test_roster_without_ids_fills_empty_list(make_page: Any) -> None:
    """② 主档空、roster 只有姓名（没有学号）→ 名单靠 roster 兜底显示。

    ★ 没有学号的行**不会**被补进学生主档（AGENTS §37：只显示、不落档），
      所以这里能同时断言"名单非空"与"主档仍然空"。
    """
    page = make_page()
    book = page._book
    assert book is not None
    assert page.visible_count == 0
    assert book.records() == []

    page.set_roster([_roster_row(0, "刘一"), _roster_row(1, "陈二")])

    assert page.visible_count == 2
    assert page._list.count() == 2
    assert [page._list.item(i).text() for i in range(2)] == ["刘一", "陈二"]
    assert book.records() == [], "没有学号的名单行不该进主档"


def test_roster_with_ids_upserts_into_student_book(make_page: Any) -> None:
    """②'/④' 的中间环节：带学号的名单行会被补进主档，再经主档显示。"""
    page = make_page()
    book = page._book
    assert book is not None

    page.set_roster([_roster_row(0, NAME_A, SID_A)])

    assert page.visible_count == 1
    assert book.get(SID_A) is not None
    assert len(book.records()) == 1
    assert page._list.item(0).text() == f"{NAME_A}（{SID_A}）"


# ---------------------------------------------------------------------------
# ③ 搜索过滤
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("李四", [NAME_B]),
        (SID_C, [NAME_C]),
        (CLASS_C, [NAME_C]),
        (CLASS_A, [NAME_A, NAME_B]),
        ("查无此人", []),
    ],
)
def test_search_filters_list(
    make_page: Any, vault: CryptoStore, keyword: str, expected: list[str]
) -> None:
    """③ 关键词命中姓名 / 学号 / 班级；空结果不该报错。"""
    _add_student(vault, SID_A, NAME_A)
    _add_student(vault, SID_B, NAME_B)
    _add_student(vault, SID_C, NAME_C, CLASS_C)
    page = make_page()

    page._search.setText(keyword)

    names = [page._entries[i]["name"] for i in range(page.visible_count)]
    assert names == expected
    assert page._list.count() == len(expected)


def test_search_updates_count_and_note(make_page: Any, vault: CryptoStore) -> None:
    """搜索后人数角标与说明行跟着变：``筛出 2 / 3 人``。"""
    _add_student(vault, SID_A, NAME_A)
    _add_student(vault, SID_B, NAME_B)
    _add_student(vault, SID_C, NAME_C, CLASS_C)
    page = make_page()

    page._search.setText(CLASS_A)

    assert page.visible_count == 2
    assert page.roster_size == 3
    assert page._count_pill.text() == "3", "角标永远是总人数"
    assert page._list_note.text() == "筛出 2 / 3 人"

    page._search.setText("")
    assert page.visible_count == 3
    assert page._list_note.text() == "共 3 人"


# ---------------------------------------------------------------------------
# ④ 新建学生
# ---------------------------------------------------------------------------


def test_new_student_appends_row_and_book_record(
    make_page: Any, vault: CryptoStore, student_dialog: Any, boxes: Any
) -> None:
    """④ 新建学生 → 名单多一行，``records()`` 里也有。"""
    _add_student(vault, SID_A, NAME_A)
    page = make_page()
    assert page.visible_count == 1

    student_dialog.values_to_return = {
        "student_id": SID_NEW,
        "name": "赵六",
        "class_name": CLASS_A,
        "note": "",
    }
    page._new_btn.click()

    assert student_dialog.titles == ["新建学生"]
    assert boxes.critical_calls == []
    assert page.visible_count == 2
    assert page._list.count() == 2
    assert len(page._book.records()) == 2
    assert page._book.get(SID_NEW) is not None
    assert any(
        page._list.item(i).text() == f"赵六（{SID_NEW}）" for i in range(page._list.count())
    )


def test_new_student_cancelled_changes_nothing(
    make_page: Any, vault: CryptoStore, student_dialog: Any, boxes: Any
) -> None:
    """取消表单（``exec() != Accepted``）→ 一个字节都不写。"""
    _add_student(vault, SID_A, NAME_A)
    page = make_page()

    student_dialog.values_to_return = None
    page._new_btn.click()

    assert page.visible_count == 1
    assert len(page._book.records()) == 1
    assert boxes.critical_calls == []
    assert boxes.information_calls == []


# ---------------------------------------------------------------------------
# ⑤ 学号重复
# ---------------------------------------------------------------------------


def test_duplicate_student_id_raises_and_warns(
    make_page: Any, vault: CryptoStore, student_dialog: Any, boxes: Any
) -> None:
    """⑤ 学号重复 → ``DuplicateStudentError`` → 弹中文提示，名单不变。"""
    _add_student(vault, SID_A, NAME_A)
    page = make_page()
    book = page._book
    assert book is not None

    with pytest.raises(DuplicateStudentError) as info:
        book.add(student_id=SID_A, name="冒名者")
    assert info.value.student_id == SID_A
    assert "已存在" in str(info.value) and "不能重复录入" in str(info.value)
    assert page.visible_count == 1, "异常路径不许写盘"

    student_dialog.values_to_return = {
        "student_id": SID_A,
        "name": "冒名者",
        "class_name": "",
        "note": "",
    }
    page._new_btn.click()

    assert boxes.critical_calls == [("学号重复", f"学号 {SID_A} 已存在，不能重复录入。")]
    assert page.visible_count == 1
    assert len(book.records()) == 1
    assert book.get(SID_A).name == NAME_A, "重复学号不许改掉原来那个人的姓名"


# ---------------------------------------------------------------------------
# ⑥ 编辑学生 / 改学号迁移
# ---------------------------------------------------------------------------


def test_edit_student_id_asks_then_migrates(
    make_page: Any,
    vault: CryptoStore,
    student_dialog: Any,
    boxes: Any,
    migration: _MigrateRecorder,
) -> None:
    """⑥ 改学号：先弹二次确认，选「迁移历史数据」→ 调迁移并记录实参。"""
    _add_student(vault, SID_A, NAME_A)
    page = make_page()
    _click_row(page, 0)
    assert page._edit_btn.isEnabled()

    student_dialog.values_to_return = {
        "student_id": SID_NEW,
        "name": NAME_A,
        "class_name": CLASS_A,
        "note": "",
    }
    boxes.pick = "迁移历史数据"
    page._edit_btn.click()

    # 二次确认确实弹过，而且是"改学号"那一种
    assert len(boxes.instances) == 1
    confirm = boxes.instances[0]
    assert confirm.title_value == "修改学号"
    assert SID_A in confirm.text_value and SID_NEW in confirm.text_value
    assert "迁移" in confirm.text_value
    assert set(confirm.buttons) == {"迁移历史数据", "仅修改信息，不迁移历史", "取消"}

    # 迁移助手被调用，实参就是新旧学号 + 库 / 账户
    assert migration.count == 1
    args, kwargs = migration.calls[0]
    assert args == (SID_A, SID_NEW)
    assert kwargs == {"store": vault, "username": USER}

    # 主档里的学号真的换掉了，名单跟着变
    assert page._book.get(SID_A) is None
    assert page._book.get(SID_NEW) is not None
    assert page._book.get(SID_NEW).name == NAME_A
    assert page.visible_count == 1
    assert page._list.item(0).text() == f"{NAME_A}（{SID_NEW}）"

    # 迁移结果被回显给用户
    assert boxes.information_calls == [("迁移历史数据", "已迁移 3 项历史数据。")]


def test_edit_student_id_keep_info_skips_migration(
    make_page: Any,
    vault: CryptoStore,
    student_dialog: Any,
    boxes: Any,
    migration: _MigrateRecorder,
) -> None:
    """⑥' 选「仅修改信息，不迁移历史」→ 不调迁移助手，只改主档并如实提示。"""
    _add_student(vault, SID_A, NAME_A)
    page = make_page()
    _click_row(page, 0)

    student_dialog.values_to_return = {
        "student_id": SID_NEW,
        "name": NAME_A,
        "class_name": CLASS_A,
        "note": "",
    }
    boxes.pick = "仅修改信息，不迁移历史"
    page._edit_btn.click()

    assert migration.count == 0, "没选迁移就不许动历史数据"
    assert page._book.get(SID_NEW) is not None
    assert page._book.get(SID_A) is None
    assert len(boxes.information_calls) == 1
    title, text = boxes.information_calls[0]
    assert title == "仅修改信息"
    assert "历史成绩仍挂在旧学号下" in text


# ---------------------------------------------------------------------------
# ⑦ 归档
# ---------------------------------------------------------------------------


def test_archive_row_keeps_record_in_book(
    make_page: Any, vault: CryptoStore, boxes: Any
) -> None:
    """⑦ 「仅归档」→ 名单少一行，但 ``records(include_archived=True)`` 仍在。"""
    _add_student(vault, SID_A, NAME_A)
    _add_student(vault, SID_B, NAME_B)
    page = make_page()
    book = page._book
    assert book is not None
    assert page.visible_count == 2

    _click_row(page, 1)  # 李四
    boxes.pick = "归档隐藏"
    page._delete_btn.click()

    assert page.visible_count == 1
    assert page._list.count() == 1
    assert page._list.item(0).text() == f"{NAME_A}（{SID_A}）"

    kept = book.records(include_archived=True)
    assert len(kept) == 2
    assert SID_B in [record.student_id for record in kept]
    assert book.get(SID_B).archived is True
    assert [record.student_id for record in book.records(include_archived=False)] == [SID_A]


def test_archived_search_reveals_hidden_row(
    make_page: Any, vault: CryptoStore, boxes: Any
) -> None:
    """⑦' 搜索框输入「已归档」才看得到归档的人（AGENTS §37）。"""
    _add_student(vault, SID_A, NAME_A)
    _add_student(vault, SID_B, NAME_B)
    page = make_page()

    _click_row(page, 1)
    boxes.pick = "归档隐藏"
    page._delete_btn.click()
    assert page.visible_count == 1

    page._search.setText("已归档")
    assert page.visible_count == 1
    assert page._list.item(0).text() == f"{NAME_B}（{SID_B}）"

    page._search.setText("")
    assert page.visible_count == 1, "普通视图仍然不显示归档的人"


# ---------------------------------------------------------------------------
# ⑧ 学期节点穿透
# ---------------------------------------------------------------------------


def _seed_one_student_two_terms(vault: CryptoStore) -> None:
    _add_student(vault, SID_A, NAME_A)
    _seed_grades(vault, SID_A, TERM_A, [("高等数学", 88.0, 4.0), ("大学物理", 91.0, 3.0)])
    _seed_grades(vault, SID_A, TERM_B, [("线性代数", 79.0, 3.0)])


def test_open_semester_switches_stack_and_injects_context(
    make_page: Any, vault: CryptoStore
) -> None:
    """⑧ 调 ``open_semester`` → 第 1 层，学期页拿到 term 与这名学生的记录。"""
    _seed_one_student_two_terms(vault)
    page = make_page()
    _click_row(page, 0)

    assert page._stack.currentIndex() == 0
    page.open_semester(TERM_A)

    side = page._semester_page
    assert page._stack.currentIndex() == 1
    assert side.current_semester() == TERM_A
    assert side.current_student_id() == SID_A
    rows = side._term_rows()
    assert {row["course"] for row in rows} == {"高等数学", "大学物理"}
    assert side._table.rowCount() == 2
    assert NAME_A in side._title_label.text()
    assert TERM_A in side._title_label.text()

    # 时间轴把这一学期标成"当前"
    nodes = [node for node in page.findChildren(TermNode) if node.term == TERM_A]
    assert nodes and any(node.is_selected() for node in nodes)


def test_clicking_term_node_opens_semester(make_page: Any, vault: CryptoStore) -> None:
    """⑧' 真给时间轴节点发鼠标左键释放事件 → 穿透到那个学期。"""
    _seed_one_student_two_terms(vault)
    page = make_page()
    _click_row(page, 0)

    nodes = {node.term: node for node in page.findChildren(TermNode)}
    assert set(nodes) == {TERM_A, TERM_B}

    _click_widget(nodes[TERM_B])

    assert page._stack.currentIndex() == 1
    assert page._semester_page.current_semester() == TERM_B
    assert [row["course"] for row in page._semester_page._term_rows()] == ["线性代数"]


# ---------------------------------------------------------------------------
# ⑨ / ⑩ 返回与重置
# ---------------------------------------------------------------------------


def test_back_button_returns_to_detail_page(make_page: Any, vault: CryptoStore) -> None:
    """⑨ 学期页「← 返回档案」→ index 回到 0。"""
    _seed_one_student_two_terms(vault)
    page = make_page()
    _click_row(page, 0)
    page.open_semester(TERM_A)
    assert page._stack.currentIndex() == 1

    back = page._semester_page._back_button
    assert back is not None and "返回" in back.text()
    back.click()

    assert page._stack.currentIndex() == 0
    assert page.current_student_id() == SID_A, "返回不该把选中的学生也清掉"


def test_reset_view_returns_to_detail(make_page: Any, vault: CryptoStore) -> None:
    """⑩ ``reset_view()`` 后 index == 0（它是 ``show_no_data`` 的别名）。"""
    _seed_one_student_two_terms(vault)
    page = make_page()
    _click_row(page, 0)
    page.open_semester(TERM_A)
    assert page._stack.currentIndex() == 1

    page.reset_view()

    assert page._stack.currentIndex() == 0
    assert page.current_student_id() == ""
    assert page._search_text == ""


# ---------------------------------------------------------------------------
# ⑪ show_no_data
# ---------------------------------------------------------------------------


def test_show_no_data_clears_roster_rows(make_page: Any) -> None:
    """⑪ ``show_no_data()`` → 名单与详情都回空态（主档本来就空）。"""
    page = make_page()
    page.set_roster([_roster_row(0, "刘一"), _roster_row(1, "陈二")])
    assert page.visible_count == 2

    page.show_no_data()

    assert page.visible_count == 0
    assert page._list.count() == 0
    assert page.roster_size == 0
    assert page._list_note.text() == "暂无数据：成绩库已清空，先在「智育分析」导入成绩表。"
    assert page.current_student_id() == ""


def test_show_no_data_keeps_main_book_rows(make_page: Any, vault: CryptoStore) -> None:
    """⑪' 记录实际行为：主档里的人**不会**被 ``show_no_data`` 删掉。

    ★ 这条不是"通过的期望"，而是把现状钉住：``show_no_data`` 只清 payload /
      roster 与详情，学生主档（``students.json.enc``）原样保留 → 名单仍显示
      那几个人，但说明行写着"暂无数据"。任务书 ⑪ 的字面预期是"名单为空"，
      两者不一致 —— 这里如实记录，不改实现。
    """
    _add_student(vault, SID_A, NAME_A)
    page = make_page()
    assert page.visible_count == 1

    page.show_no_data()

    assert page.visible_count == 1, "主档还在，名单就还在"
    assert page._list_note.text() == "暂无数据：成绩库已清空，先在「智育分析」导入成绩表。"
    assert page.current_student_id() == ""


# ---------------------------------------------------------------------------
# ⑫ 当前学生身份
# ---------------------------------------------------------------------------


def test_current_student_id_selected_and_unselected(
    make_page: Any, vault: CryptoStore
) -> None:
    """⑫ 选中学生非空、未选空串；``show_no_data`` 之后又回到空串。"""
    _add_student(vault, SID_A, NAME_A)
    page = make_page()

    assert page.current_student_id() == ""

    _click_row(page, 0)
    assert page.current_student_id() == SID_A
    assert page._selected_sid == SID_A

    page.show_no_data()
    assert page.current_student_id() == ""


def test_current_student_id_strips_sid_prefix(make_page: Any) -> None:
    """⑫' payload 里带 ``sid:`` 前缀时，对外仍然只给裸学号。"""
    page = make_page()

    page.show_student(_payload(SID_A, NAME_A))

    assert page._payload is not None
    assert page._payload["student_id"] == SID_A
    assert page.current_student_id() == SID_A
    assert "sid:" not in page.current_student_id()

    page.show_student({"student_id": f"sid:{SID_B}", "student": NAME_B})
    assert page.current_student_id() == SID_B


# ---------------------------------------------------------------------------
# 学期选择框（ask_semester_and_open）
# ---------------------------------------------------------------------------


def test_ask_semester_and_open_uses_input_dialog(
    make_page: Any, vault: CryptoStore, input_dialog: Any, boxes: Any
) -> None:
    """⑧'' 选学期框列出的就是这名学生的学期；选中后直接打开。"""
    _seed_one_student_two_terms(vault)
    page = make_page()
    _click_row(page, 0)

    input_dialog.answer = (TERM_A, True)
    page.ask_semester_and_open()

    assert len(input_dialog.calls) == 1
    call = input_dialog.calls[0]
    assert call["title"] == "选择学期"
    assert call["items"] == [TERM_A, TERM_B]
    assert call["current"] == 1, "默认停在最近一个学期（大一下）"
    assert page._stack.currentIndex() == 1
    assert page._semester_page.current_semester() == TERM_A
    assert boxes.information_calls == []


def test_ask_semester_without_records_shows_hint(
    make_page: Any, vault: CryptoStore, input_dialog: Any, boxes: Any
) -> None:
    """没有分学期成绩 → 不弹选择框，只给一句中文提示。"""
    _add_student(vault, SID_A, NAME_A)  # 有主档、没成绩
    page = make_page()
    _click_row(page, 0)

    page.ask_semester_and_open()

    assert input_dialog.calls == []
    assert boxes.information_calls == [("学期侧写", "这名学生还没有成绩记录，先导入成绩表。")]
    assert page._stack.currentIndex() == 0


def test_open_semester_without_payload_is_silent_noop(page: ProfilePage) -> None:
    """没有选中学生时 ``open_semester`` 静默不动（接口语义记录）。"""
    page.open_semester(TERM_A)

    assert page._stack.currentIndex() == 0
    assert page._current_term == ""
    assert page._semester_page.current_semester() == ""


def test_open_semester_blank_term_is_ignored(make_page: Any, vault: CryptoStore) -> None:
    """空学期名不切页（``strip()`` 之后为空就直接返回）。"""
    _seed_one_student_two_terms(vault)
    page = make_page()
    _click_row(page, 0)

    page.open_semester("   ")

    assert page._stack.currentIndex() == 0
    assert page._current_term == ""


# ---------------------------------------------------------------------------
# ⑬ 界面文案不许有 Markdown 记号（AGENTS §24）
# ---------------------------------------------------------------------------


def test_ui_texts_have_no_markdown(make_page: Any, vault: CryptoStore) -> None:
    """⑬ 标签 / 按钮 / 名单行 / 占位符里不许有 ``**`` / ``__`` / 反引号。"""
    _seed_one_student_two_terms(vault)
    page = make_page()
    _click_row(page, 0)

    texts: list[str] = [page._search.placeholderText()]

    # ★ 只扫档案详情页：学期侧重页的文案由 ``%TEMP%`` 的离屏富文本 lint 与
    #   ``tests/test_profile_navigation.py`` 之外的用例覆盖 —— 在本进程里先
    #   ``open_semester`` 再 ``findChildren`` 有可复现的卡死（见 CHANGELOG）。
    for widget in (page,):
        for label in widget.findChildren(QLabel):
            texts.append(label.text())
            texts.append(label.toolTip())
        for button in widget.findChildren(QPushButton):
            texts.append(button.text())
            texts.append(button.toolTip())
        for button in widget.findChildren(QToolButton):
            texts.append(button.text())
            texts.append(button.toolTip())

    for index in range(page._list.count()):
        texts.append(page._list.item(index).text())

    assert len(texts) > 20, "至少真的走了一遍界面文字"

    bad: list[tuple[str, str]] = []
    for text in texts:
        for mark in ("**", "__", "`"):
            if mark in text:
                bad.append((mark, text))
    assert bad == [], f"界面文案里出现了 Markdown 记号：{bad}"
