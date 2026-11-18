"""📈 成长档案页 —— 把 :mod:`core.archival` 的纵向数据画出来。

    ┌ 学生名单 ────┬ 【张三（2023001）】累计均分 84.64  ↑进步 +1.90 ──────┐
    │ 🔍 搜索      │ ┌ 学期时间轴（QScrollArea）──────────────────────┐ │
    │ 张三         │ │ ● 大二上   82.74   8 门 / 22 学分              │ │
    │ 李四         │ │ │                                             │ │
    │ 王五         │ │ ● 大二下   84.64   8 门 / 23 学分  挂科 1 门   │ │
    │              │ └───────────────────────────────────────────────┘ │
    │              │ 已定型短板（补考不改分）· 可抢救项 · 剩余可冲学期    │
    └──────────────┴────────────────────────────────────────────────────┘

★ 本页**一行算术都没有**（charter 铁律 1 / v1.3 红线 1）。

  累计均分、每学期均分、趋势 Δ、定型/可救分区、剩余学期数，全部来自
  :mod:`core.archival` 的 ``cumulative_average`` / ``term_average`` /
  ``trend()`` / ``partition()`` —— 那些是 core 里**已经有**的函数。
  本页只做排版。

★ 数据从**加密库**读（``archive.json.enc``，导入成绩时顺手写进去的），
  所以关掉程序再打开，档案还在。见
  :data:`ui.pages.analysis_page.ARCHIVE_FILE`。

★ 身份用**学号**（charter 铁律 4）：列表条目的键是
  ``sid:<学号>``，没学号才退回 ``name:<姓名>`` 并且界面明说同名风险。
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from time import monotonic
from typing import Any

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core import applog
from core.archival import Archive, archive_key, partition, semester_rank, trend
from core.calculator import CalculationScope
from core.crypto_store import CryptoError, CryptoStore
from core.database import GradeHistoryDB
from core.scholarship import list_classes
from core.student_deletion import StudentDeletionControl, audit_student_deletion, purge_class, purge_student
from core.user_store import load_user_json, save_user_json
from ui.anim import make_card
from ui.pages.analysis_page import ARCHIVE_FILE
from ui.pages.profile_page import (
    ITEM_HEIGHT,
    LIST_WIDTH,
    _ROLE_HAS_ID,
    _ROLE_ID,
    _ROLE_INDEX,
    _ROLE_LEVEL,
    _ROLE_NAME,
    _ROLE_SEED,
    StudentItemDelegate,
    _avatar_color,
)
from ui.pages.profile_shared import (
    ARCHIVED_SEARCH_TEXT,
    DuplicateStudentError,
    StudentBook,
    StudentDialog,
    StudentRecord,
    _bare_sid,
    build_empty_student_archive,
    migrate_student_id_for,
    migration_report_text,
    open_student_book,
    record_student_audit,
    store_archive,
)
from ui.pages.semester_side_profile import SemesterSideProfilePage
from ui.pages.student_overview import StudentOverviewPage
from ui.widgets.profile_reading import profile_scroll
from ui.workers import run_async

logger = logging.getLogger(__name__)

__all__ = ["ARCHIVE_SCOPE", "ArchivePage", "build"]

#: 档案用哪一档口径展示。
#:
#: charter / v1.3 的默认口径是「必修+限选」（Spec 第 61 行）。
#: 档案页**不跟智育分析页的筛选按钮联动** —— 它是纵向的、可归档的固定口径，
#: 跟着屏幕上的临时视图变会让"上学期看是 84、这学期看是 86"这种事发生。
ARCHIVE_SCOPE = CalculationScope.REQUIRED_LIMITED

#: 趋势死区：|Δ| 小于它就算"平稳"，不标颜色（避免 0.01 分也号称进步）
TREND_EPS = 0.5

_UP_COLOR = "#15803D"
_DOWN_COLOR = "#C2410C"
_FLAT_COLOR = "#475569"

DELETE_PREPARE_TIMEOUT = 8.0
DELETE_POLL_INTERVAL_MS = 50
_ROLE_CLASS_GROUP = Qt.ItemDataRole.UserRole + 40
_ROLE_CLASS_NAME = Qt.ItemDataRole.UserRole + 41
CLASS_SHORTCUTS_FILE = "profile_class_shortcuts.json.enc"


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


class _ClassRosterDelegate(StudentItemDelegate):
    def sizeHint(self, option, index):  # noqa: N802 - Qt API
        if index.data(_ROLE_CLASS_GROUP):
            return QSize(0, 32)
        return super().sizeHint(option, index)

    def paint(self, painter, option, index) -> None:
        if not index.data(_ROLE_CLASS_GROUP):
            super().paint(painter, option, index)
            return
        painter.save()
        frame = option.rect.adjusted(10, 5, -10, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EAF2FC"))
        painter.drawRoundedRect(frame, 5, 5)
        font = option.font
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#264F88"))
        text = QFontMetrics(font).elidedText(str(index.data()), Qt.TextElideMode.ElideRight, frame.width() - 18)
        painter.drawText(frame.adjusted(9, 0, -9, 0), Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


class _StudentDeletionProgress(QDialog):
    def __init__(self, parent: QWidget, title: str = "删除学生") -> None:
        super().__init__(parent)
        self.setWindowTitle(f"正在{title}")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        layout = QVBoxLayout(self)
        self._message = QLabel(f"正在{title}，请稍候。", self)
        layout.addWidget(self._message)
        bar = QProgressBar(self)
        bar.setRange(0, 0)
        layout.addWidget(bar)

    def show_slow_write(self) -> None:
        self._message.setText("正在完成本地写入，等待时间比平时长。请保持程序开启。")

    def reject(self) -> None:
        # 已开始写盘时，Esc 不应关闭窗口并释放其他可能写入同一库的操作。
        pass


class ArchivePage(QWidget):
    """成长档案页：左名单 + 右时间轴 / 趋势 / 定型分区。"""

    student_deleted = Signal(str, str)
    class_deleted = Signal(str, object)
    model_settings_requested = Signal()

    def __init__(self, store: CryptoStore | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore()
        self._username = ""
        self._book: StudentBook | None = None
        self._archive: Archive | None = None
        self._current: Any = None
        self._delete_progress: _StudentDeletionProgress | None = None
        self._show_archived = False
        self._key_by_row: dict[int, str] = {}
        self._class_by_sid: dict[str, str] = {}
        self._archived_sids: set[str] = set()
        self._semester_page: SemesterSideProfilePage | None = None
        self._detail_stack: QStackedWidget | None = None
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        self._username = username or ""
        self._book = open_student_book(self._store, self._username) if self._username else None
        self.reload()

    def set_archive(self, archive: Archive | None) -> None:
        """直接塞一份现成的归档（智育分析页刚导入完时用，省一次解密）。"""
        self._archive = archive
        self._fill_list()

    def reload(self) -> None:
        """从加密库读回纵向归档。

        读不出来就是"还没有档案"，界面给一句明确指引，
        **不报错** —— 没导入过不是错误。
        """
        self._archive = None
        self._class_by_sid.clear()
        self._archived_sids.clear()
        if self._username:
            payload = load_user_json(self._store, self._username, ARCHIVE_FILE)
            if isinstance(payload, dict):
                try:
                    self._archive = Archive.from_dict(payload)
                except Exception:  # noqa: BLE001 - 坏档案不该拦住整页
                    self._archive = None

        # 同步学生主档（students.json.enc）到成长档案
        if self._book is not None and getattr(self._book, "durable", False):
            try:
                records = self._book.records(include_archived=True)
                self._archived_sids = {_bare_sid(r.student_id) for r in records if r.archived}
                self._class_by_sid.update({
                    _bare_sid(r.student_id): r.class_name.strip()
                    for r in records
                })
                if records and self._archive is None:
                    self._archive = Archive()
                if self._archive is not None:
                    archive_sids = {
                        _bare_sid(s.student_id) for s in self._archive.students.values() if s.student_id
                    }
                    for r in records:
                        rsid = _bare_sid(getattr(r, "student_id", ""))
                        rname = str(getattr(r, "name", "") or "").strip()
                        if rsid and rsid not in archive_sids:
                            key = archive_key(rsid, rname)
                            if key in self._archive.students:
                                self._archive.students[key].roster_id = rsid
                            else:
                                self._archive.students[key] = build_empty_student_archive(key, rname, rsid)
                            archive_sids.add(rsid)
            except Exception:
                logger.exception("同步学生主档到档案失败")

        self._fill_missing_classes()
        self._fill_list()

    def _fill_missing_classes(self) -> None:
        """旧档案没有班级字段时，只使用能按学号唯一确定的导入班级。"""
        if self._archive is None or not self._username:
            return
        wanted = {_bare_sid(s.student_id) for s in self._archive.students.values()}
        wanted.discard("")
        wanted.difference_update(sid for sid, name in self._class_by_sid.items() if name)
        if not wanted:
            return
        candidates: dict[str, set[str]] = {}
        try:
            for record in list_classes(self._store, self._username):
                for student in record.students:
                    sid = _bare_sid(student.get("student_id"))
                    if sid in wanted:
                        candidates.setdefault(sid, set()).add(record.class_name)
        except (CryptoError, OSError):
            logger.warning("无法读取旧档案的班级快照", exc_info=True)
        self._class_by_sid.update({
            sid: next(iter(classes)) for sid, classes in candidates.items() if len(classes) == 1
        })

    @property
    def student_count(self) -> int:
        return len(self._key_by_row)

    def all_terms(self) -> list[str]:
        """整份档案里出现过的学期标签（去重、按学期顺序排）。

        ★ ``Archive`` 本身**没有** ``terms`` 属性 —— 学期是挂在每个
          ``StudentArchive`` 上的（``archive.students[i].terms``）。
          这里按排序权重合并去重，不是简单 set（否则"大二下"会排到"大一上"前面）。
        """
        if self._archive is None:
            return []
        seen: dict[str, int] = {}
        for student in self._archive.students.values():
            for term in student.terms:
                seen.setdefault(term, semester_rank(term))
        return [t for t, _rank in sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))]

    @property
    def visible_count(self) -> int:
        return sum(1 for i in self._key_by_row
                   if not self._list.item(i).isHidden())

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 20)
        outer.setSpacing(14)

        self._archive_title = QLabel("📈  成长档案")
        self._archive_title.setObjectName("H1")
        outer.addWidget(self._archive_title)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        self._list_panel = self._build_list_pane()
        body.addWidget(self._list_panel)
        body.addWidget(self._build_detail_pane(), 1)
        outer.addLayout(body, 1)

    def _build_list_pane(self) -> QFrame:
        card = make_card()
        card.setFixedWidth(LIST_WIDTH)
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        head = QWidget()
        head_box = QHBoxLayout(head)
        head_box.setContentsMargins(14, 13, 14, 10)
        head_box.setSpacing(8)
        title = QLabel("学生名单")
        title.setObjectName("H2")
        head_box.addWidget(title)
        head_box.addStretch(1)
        self._count_pill = QLabel("0")
        self._count_pill.setObjectName("CountPill")
        head_box.addWidget(self._count_pill)
        box.addWidget(head)

        holder = QWidget()
        holder_box = QVBoxLayout(holder)
        holder_box.setContentsMargins(14, 0, 14, 10)
        self._search = QLineEdit()
        self._search.setObjectName("ListSearch")
        self._search.setPlaceholderText("搜索姓名、学号或班级")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(32)
        self._search.textChanged.connect(self._on_search)
        holder_box.addWidget(self._search)
        box.addWidget(holder)

        self._list = QListWidget()
        self._list.setObjectName("StudentList")
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setItemDelegate(_ClassRosterDelegate(self._list))
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._roster_context_menu)
        box.addWidget(self._list, 1)

        self._note = QLabel("")
        self._note.setObjectName("ListNote")
        self._note.setWordWrap(True)
        self._note.setContentsMargins(14, 8, 14, 10)
        box.addWidget(self._note)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(14, 6, 14, 12)
        buttons.setSpacing(6)
        self._new_btn = QPushButton("新建")
        self._new_btn.setObjectName("Ghost")
        self._new_btn.setMinimumHeight(32)
        self._new_btn.clicked.connect(self._open_new_student)
        buttons.addWidget(self._new_btn)

        self._edit_btn = QPushButton("编辑")
        self._edit_btn.setObjectName("Ghost")
        self._edit_btn.setMinimumHeight(32)
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._open_edit_student)
        buttons.addWidget(self._edit_btn)

        self._delete_btn = QPushButton("删除")
        self._delete_btn.setObjectName("RowAction")
        self._delete_btn.setMinimumHeight(32)
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_student)
        buttons.addWidget(self._delete_btn)
        box.addLayout(buttons)

        shortcuts = QHBoxLayout()
        shortcuts.setContentsMargins(14, 0, 14, 12)
        self._class_shortcuts = QToolButton()
        self._class_shortcuts.setObjectName("ClassShortcuts")
        self._class_shortcuts.setText("快捷班级")
        self._class_shortcuts.setToolTip("展开班级堆叠，定位或编辑快捷班级")
        self._class_shortcuts.setMinimumHeight(32)
        self._class_shortcuts.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._class_menu = QMenu(self._class_shortcuts)
        self._class_menu.aboutToShow.connect(self._rebuild_class_menu)
        self._class_shortcuts.setMenu(self._class_menu)
        shortcuts.addWidget(self._class_shortcuts)
        shortcuts.addStretch()
        box.addLayout(shortcuts)
        return card

    def _class_names(self) -> list[str]:
        return sorted({name for name in self._class_by_sid.values() if name}, key=str.casefold)

    def _favorite_classes(self, names: list[str]) -> list[str]:
        stored = (load_user_json(self._store, self._username, CLASS_SHORTCUTS_FILE, {})
                  if self._username else {})
        pinned = stored.get("classes") if isinstance(stored, dict) else None
        return [name for name in pinned if name in names] if isinstance(pinned, list) else names

    def _rebuild_class_menu(self) -> None:
        names = self._class_names()
        self._class_menu.clear()
        for name in self._favorite_classes(names):
            action = self._class_menu.addAction(name)
            action.triggered.connect(lambda _checked=False, value=name: self._jump_to_class(value))
        self._class_menu.addSeparator()
        edit = self._class_menu.addAction("编辑快捷班级…")
        edit.setEnabled(bool(names) and bool(self._username))
        edit.triggered.connect(self._edit_class_shortcuts)

    def _edit_class_shortcuts(self) -> None:
        names = self._class_names()
        selected = self._favorite_classes(names)
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑快捷班级")
        dialog.resize(340, 350)
        box = QVBoxLayout(dialog)
        hint = QLabel("勾选要快捷访问的班级，可拖动调整顺序。")
        hint.setWordWrap(True)
        box.addWidget(hint)
        items = QListWidget()
        items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        for name in dict.fromkeys([*selected, *names]):
            item = QListWidgetItem(name, items)
            item.setCheckState(Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked)
        box.addWidget(items)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        order = [items.item(i).text() for i in range(items.count())
                 if items.item(i).checkState() == Qt.CheckState.Checked]
        try:
            save_user_json(self._store, self._username, CLASS_SHORTCUTS_FILE, {"classes": order})
        except (CryptoError, OSError) as exc:
            QMessageBox.warning(self, "快捷班级", f"保存失败。{exc}")

    def _jump_to_class(self, name: str) -> None:
        for pos in range(self._list.count()):
            item = self._list.item(pos)
            if item.data(_ROLE_CLASS_GROUP) and item.data(_ROLE_CLASS_NAME) == name and not item.isHidden():
                self._list.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
                return
        search = self._search.text()
        if search not in (name, f"{ARCHIVED_SEARCH_TEXT} {name}"):
            self._search.setText(name)
            self._jump_to_class(name)

    def _roster_context_menu(self, position) -> None:
        item = self._list.itemAt(position)
        if item is None or self._delete_progress is not None:
            return
        menu = QMenu(self)
        name = item.data(_ROLE_CLASS_NAME)
        if item.data(_ROLE_CLASS_GROUP):
            if not name:
                return
            menu.addAction("定位此班级", lambda: self._jump_to_class(name))
            menu.addSeparator()
            menu.addAction("删除整个班级…", lambda: self._confirm_class_deletion(name))
        else:
            self._list.setCurrentItem(item)
            self._on_item_clicked(item)
            menu.addAction("编辑学生…", self._open_edit_student)
            menu.addAction("删除学生…", self._delete_student)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self._list.viewport().mapToGlobal(position))

    def _confirm_class_deletion(self, name: str) -> None:
        if not self._username or self._delete_progress is not None:
            return
        answer = QMessageBox.question(
            self, "删除整个班级",
            f"确定删除 {name} 的全部学年成绩、德育和奖项名额，以及只属于该班的学生档案吗？"
            "同时属于其他班的学生及其个人记录会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        store, user = self._store, self._username

        def finished(result):
            deleted, removed = result
            search = self._search.text().strip()
            if search in (name, f"{ARCHIVED_SEARCH_TEXT} {name}"):
                self._search.clear()
            self._note.setText("班级已删除。" if deleted else "该班级已无可删除数据。")
            self.class_deleted.emit(name, removed)

        self._run_deletion("删除班级", "class.delete",
                           lambda control: purge_class(store, user, name, control=control), finished)

    def _build_detail_pane(self) -> QWidget:
        self._detail_stack = QStackedWidget()
        self._detail_stack.setObjectName("ArchiveDetailStack")

        # 一级页面：档案概览（肖像、时间轴、定型分区）
        self._level0_pane = QWidget()
        box = QVBoxLayout(self._level0_pane)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        self._empty = QLabel("选择一名学生查看档案。")
        self._empty.setObjectName("Placeholder")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        box.addWidget(self._empty, 1)

        content = QWidget()
        body_box = QVBoxLayout(content)
        body_box.setContentsMargins(0, 0, 6, 2)
        body_box.setSpacing(14)
        body_box.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        body_box.addWidget(self._build_portrait_card())
        body_box.addWidget(self._build_timeline_card())
        body_box.addWidget(self._build_partition_card())
        body_box.addStretch(1)
        self._body = profile_scroll(content)
        box.addWidget(self._body, 1)

        self._body.hide()
        self._detail_stack.addWidget(self._level0_pane)

        # 二级页面：个人专属侧写（学期画像与 AI 侧写辅导）
        self._semester_page = SemesterSideProfilePage(parent=self)
        self._semester_page.model_settings_requested.connect(self.model_settings_requested.emit)
        self._detail_stack.addWidget(self._semester_page)
        self._overview_page = StudentOverviewPage(self)
        self._overview_page.back_requested.connect(self._back_to_archive)
        self._overview_page.semester_requested.connect(self.open_semester_profile)
        self._overview_page.model_settings_requested.connect(self.model_settings_requested.emit)
        self._detail_stack.addWidget(self._overview_page)
        self._detail_stack.currentChanged.connect(self._on_detail_page_changed)

        return self._detail_stack

    def _on_detail_page_changed(self, index: int) -> None:
        self._archive_title.setVisible(index == 0)
        self._list_panel.setVisible(index == 0)

    def _build_portrait_card(self) -> QFrame:
        card = make_card()
        box = QHBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(12)

        self._who = QLabel("—")
        self._who.setObjectName("H2")
        self._who.setWordWrap(True)
        box.addWidget(self._who)

        self._cumulative = QLabel("—")
        self._cumulative.setObjectName("H1")
        box.addWidget(self._cumulative)

        self._cum_sub = QLabel("")
        self._cum_sub.setObjectName("Muted")
        self._cum_sub.setWordWrap(True)
        box.addWidget(self._cum_sub)

        box.addStretch(1)

        self._delta_tag = QLabel("—")
        self._delta_tag.setObjectName("TagGray")
        box.addWidget(self._delta_tag)

        self._profile_btn = QPushButton("🎯  个人专属侧写")
        self._profile_btn.setObjectName("Ghost")
        self._profile_btn.setMinimumHeight(32)
        self._profile_btn.setToolTip("查看全部已导入学期的总览")
        self._profile_btn.clicked.connect(self.open_current_profile)
        box.addWidget(self._profile_btn)

        return card

    def _build_timeline_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("学期时间轴")
        title.setObjectName("H2")
        head.addWidget(title)
        self._timeline_hint = QLabel("")
        self._timeline_hint.setObjectName("Muted")
        head.addWidget(self._timeline_hint)
        head.addStretch(1)
        box.addLayout(head)

        self._timeline_scroll = QScrollArea()
        self._timeline_scroll.setWidgetResizable(True)
        self._timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._timeline_box = QVBoxLayout(inner)
        self._timeline_box.setContentsMargins(2, 2, 6, 2)
        self._timeline_box.setSpacing(8)
        self._timeline_box.addStretch(1)
        self._timeline_scroll.setWidget(inner)
        self._timeline_scroll.setFixedHeight(84)
        self._timeline_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        box.addWidget(self._timeline_scroll, 1)
        return card

    def _build_partition_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(8)

        title = QLabel("定型 / 可救分区")
        title.setObjectName("H2")
        box.addWidget(title)

        self._locked = QLabel("—")
        self._locked.setObjectName("Muted")
        self._locked.setWordWrap(True)
        box.addWidget(self._locked)

        self._rescue = QLabel("—")
        self._rescue.setObjectName("Muted")
        self._rescue.setWordWrap(True)
        box.addWidget(self._rescue)

        self._remaining = QLabel("—")
        self._remaining.setObjectName("Notice")
        self._remaining.setWordWrap(True)
        self._remaining.setMinimumHeight(42)
        box.addWidget(self._remaining)
        return card

    # ---------------------------------------------------------------- 名单
    def _fill_list(self) -> None:
        blocked = self._list.blockSignals(True)
        self._list.clear()
        self._key_by_row = {}
        students = self._archive.students.values() if self._archive else ()
        search_text = self._search.text().strip()
        self._show_archived = search_text.startswith(ARCHIVED_SEARCH_TEXT)
        by_class: dict[str, list[Any]] = {}
        for student in students:
            sid = _bare_sid(student.student_id)
            if (sid in self._archived_sids) == self._show_archived:
                by_class.setdefault(self._class_by_sid.get(sid, ""), []).append(student)

        for klass in sorted(by_class, key=lambda value: (not value, value.casefold())):
            title = klass or "未填写班级"
            heading = QListWidgetItem(title)
            heading.setData(_ROLE_CLASS_GROUP, True)
            heading.setData(_ROLE_CLASS_NAME, klass)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
            heading.setToolTip(title)
            self._list.addItem(heading)
            for student in sorted(by_class[klass], key=lambda s: (s.student, s.student_id)):
                row = self._list.count()
                label = (f"{student.student}（{student.student_id}）"
                         if student.student_id else student.student)
                item = QListWidgetItem(label)
                item.setData(_ROLE_INDEX, row)
                item.setData(_ROLE_CLASS_NAME, klass)
                item.setData(_ROLE_NAME, student.student)
                item.setData(_ROLE_ID, student.student_id)
                item.setData(_ROLE_HAS_ID, bool(student.student_id))
                item.setData(_ROLE_LEVEL, "danger" if student.failed_total else "")
                item.setData(_ROLE_SEED, student.student_id or student.student)
                item.setSizeHint(QSize(0, ITEM_HEIGHT))
                item.setToolTip(f"{label}\n{title}")
                self._list.addItem(item)
                self._key_by_row[row] = student.key
        self._list.blockSignals(blocked)

        count = self.student_count
        self._class_shortcuts.setEnabled(bool(self._class_names()))
        self._count_pill.setText(str(count))
        if count == 0:
            if self._show_archived:
                self._note.setText("没有已归档的学生。")
            else:
                self._note.setText("还没有档案。先在「📊 智育分析」导入成绩表或新建学生。")
            self._body.hide()
            self._empty.show()
            self._empty.setText(
                "还没有可看的档案。\n\n"
                "去「📊 智育分析」导入一份成绩表，或点击下方「新建」添加学生。"
            )
        else:
            terms = len(self.all_terms())
            self._note.setText(f"共 {count} 人　·　{terms} 个学期标签")
            if self._current is None:
                self._empty.setText(
                    "👈  在左边点一名学生，这里显示他的学期时间轴和趋势。"
                )
        self._filter_list(search_text)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_sel = self._current is not None
        if hasattr(self, "_edit_btn"):
            self._edit_btn.setEnabled(has_sel)
        if hasattr(self, "_delete_btn"):
            self._delete_btn.setEnabled(has_sel)

    def _update_note_filter(self) -> None:
        total = self.student_count
        if total == 0:
            return
        shown = self.visible_count
        terms = len(self.all_terms())
        self._note.setText(
            f"筛出 {shown} / {total} 人　·　{terms} 个学期标签" if shown != total
            else f"共 {total} 人　·　{terms} 个学期标签"
        )

    def _on_search(self, text: str) -> None:
        keyword = (text or "").strip()
        if keyword.startswith(ARCHIVED_SEARCH_TEXT) != self._show_archived:
            self.reset_view()
            self._fill_list()
            return
        self._filter_list(keyword)

    def _filter_list(self, keyword: str) -> None:
        if self._show_archived:
            keyword = keyword.removeprefix(ARCHIVED_SEARCH_TEXT).strip()
        needle = keyword.casefold()
        counts: dict[str, int] = {}
        headings: list[QListWidgetItem] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            klass = item.data(_ROLE_CLASS_NAME)
            if item.data(_ROLE_CLASS_GROUP):
                headings.append(item)
                continue
            shown = needle in item.text().casefold() or needle in (klass or "未填写班级").casefold()
            item.setHidden(not shown)
            if shown:
                counts[klass] = counts.get(klass, 0) + 1
        for item in headings:
            klass = item.data(_ROLE_CLASS_NAME)
            count = counts.get(klass, 0)
            item.setHidden(count == 0)
            item.setText(f"{klass or '未填写班级'}   {count} 人")
        selected = self._list.currentItem()
        if selected is not None and selected.isHidden():
            self._list.setCurrentRow(-1)
            self.reset_view()
        self._count_pill.setText(str(self.visible_count))
        self._update_note_filter()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        key = self._key_by_row.get(self._list.row(item))
        student = self._archive.get(key) if key and self._archive is not None else None
        if student is not None:
            if self._detail_stack is not None:
                self._detail_stack.setCurrentIndex(0)
            self.show_student(student)

    # ---------------------------------------------------------------- 渲染
    def show_student(self, student: Any) -> None:
        """渲染一名学生的档案。``student`` 是 ``core.archival.StudentArchive``。"""
        if student is None:
            return
        self._current = student
        self._sync_buttons()
        self._empty.hide()
        self._body.show()

        label = (f"{student.student}（{student.student_id}）"
                 if student.student_id else student.student)
        self._who.setText(label)
        self._cumulative.setText(_fmt(student.cumulative_average))
        self._cum_sub.setText(
            f"{len(student.terms)} 个学期　·　{len(student.records)} 条记录　·　"
            f"挂科 {student.failed_total} 门　·　缺考 {student.absent_total} 门"
        )

        info = trend(student, ARCHIVE_SCOPE)
        self._render_delta(info)
        self._render_timeline(student, info)
        self._render_partition(student)

    def _render_delta(self, info: dict[str, Any]) -> None:
        delta = info.get("delta")
        if delta is None:
            self._delta_tag.setText("单次导入，暂无趋势")
            self._delta_tag.setObjectName("TagGray")
            self._delta_tag.setStyleSheet("")
            self._repolish(self._delta_tag)
            return
        if delta > TREND_EPS:
            text, color, obj = f"↑ 进步 +{delta:.2f}", _UP_COLOR, "TagGreen"
        elif delta < -TREND_EPS:
            text, color, obj = f"↓ 下滑 {delta:.2f}", _DOWN_COLOR, "TagOrange"
        else:
            text, color, obj = f"→ 平稳 {delta:+.2f}", _FLAT_COLOR, "TagGray"
        self._delta_tag.setText(text)
        self._delta_tag.setObjectName(obj)
        # 下滑用橙色（用户要求：↑进步绿 / ↓下滑橙）
        self._delta_tag.setStyleSheet(
            f"color:{color};" if obj == "TagGray" else ""
        )
        self._repolish(self._delta_tag)

    def _render_timeline(self, student: Any, info: dict[str, Any]) -> None:
        """重画时间轴。

        ★ 先清空再重建 —— 而且**必须**把旧的 deleteLater 掉，
          否则切学生时旧节点会叠在新节点下面（越点越乱）。
        """
        while self._timeline_box.count() > 1:      # 末尾那个 stretch 留着
            item = self._timeline_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        series = info.get("series") or []
        self._timeline_scroll.setFixedHeight(min(330, max(84, 48 * len(series) + 12)))
        self._timeline_scroll.verticalScrollBar().setValue(0)
        if not series:
            hint = QLabel("这个学生还没有可显示的学期记录。")
            hint.setObjectName("Muted")
            self._timeline_box.insertWidget(0, hint)
            self._timeline_hint.setText("")
            return

        self._timeline_hint.setText(
            f"{info.get('first_term') or '—'} → {info.get('last_term') or '—'}"
        )
        averages = [p["average"] for p in series if p["average"] is not None]
        lo = min(averages) if averages else 0.0
        hi = max(averages) if averages else 100.0

        for index, point in enumerate(series):
            self._timeline_box.insertWidget(
                index,
                self._timeline_row(
                    point,
                    first=(index == 0),
                    last=(index == len(series) - 1),
                    lo=lo, hi=hi,
                    records=self._records_of(student, point["term"]),
                ),
            )

    @staticmethod
    def _records_of(student: Any, term: str) -> list[Any]:
        return [r for r in student.records if r.term == term]

    def _timeline_row(self, point: dict[str, Any], *, first: bool, last: bool,
                      lo: float, hi: float, records: list[Any]) -> QWidget:
        """时间轴上的一个节点：左边一个圆点 + 竖线，右边是那一学期的明细。"""
        holder = QWidget()
        box = QHBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        # --- 刻度（圆点 + 上下连线）---
        rail = QWidget()
        rail.setFixedWidth(18)
        rail_box = QVBoxLayout(rail)
        rail_box.setContentsMargins(0, 0, 0, 0)
        rail_box.setSpacing(0)

        top = QFrame()
        top.setFixedWidth(2)
        top.setStyleSheet(
            "background: transparent;" if first else "background: #CBD5E1;"
        )
        top.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        rail_box.addWidget(top, 1, Qt.AlignmentFlag.AlignHCenter)

        dot = QFrame()
        dot.setFixedSize(11, 11)
        dot.setStyleSheet(
            f"background: {_avatar_color(point['term'])}; border-radius: 5px;"
        )
        rail_box.addWidget(dot, 0, Qt.AlignmentFlag.AlignHCenter)

        bottom = QFrame()
        bottom.setFixedWidth(2)
        bottom.setStyleSheet(
            "background: transparent;" if last else "background: #CBD5E1;"
        )
        bottom.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        rail_box.addWidget(bottom, 1, Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(rail)

        # --- 内容 ---
        card = QFrame()
        card.setObjectName("TimelineRow")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        inner = QHBoxLayout(card)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(10)

        term = QLabel(str(point["term"]))
        term.setObjectName("H3")
        term.setMinimumWidth(96)
        inner.addWidget(term)

        average = point["average"]
        avg = QLabel(_fmt(average))
        avg.setObjectName("H2")
        inner.addWidget(avg)

        # 一条按分数量出来的横条 —— 让"哪学期掉了"一眼可见，
        # 不用去读数字。宽度是**纯展示**，不参与任何计算。
        if average is not None and hi > lo:
            bar = QFrame()
            bar.setFixedHeight(6)
            bar.setMinimumWidth(40)
            bar.setMaximumWidth(220)
            ratio = (float(average) - lo) / (hi - lo) if hi > lo else 1.0
            bar.setFixedWidth(int(40 + ratio * 180))
            color = _UP_COLOR if float(average) >= (lo + hi) / 2 else "#94A3B8"
            bar.setStyleSheet(f"background:{color}; border-radius:3px;")
            inner.addWidget(bar)

        inner.addStretch(1)

        credits = sum(getattr(r, "total_credit", 0.0) or 0.0 for r in records)
        courses = sum(len(getattr(r, "courses", ()) or ()) for r in records)
        meta = QLabel(f"{courses} 门 / {credits:g} 学分")
        meta.setObjectName("Muted")
        inner.addWidget(meta)

        failed = [name for r in records for name in (r.failed_courses or ())]
        absent = [name for r in records for name in (r.absent_courses or ())]
        if failed:
            tag = QLabel(f"挂科 {len(failed)} 门")
            tag.setObjectName("TagRed")
            inner.addWidget(tag)
        if absent:
            tag = QLabel(f"缺考 {len(absent)} 门")
            tag.setObjectName("TagOrange")
            inner.addWidget(tag)

        side_btn = QPushButton("学期侧写")
        side_btn.setObjectName("Ghost")
        side_btn.setMinimumHeight(28)
        term_name = str(point["term"])
        side_btn.setToolTip(f"查看 {term_name} 的个人专属侧写")
        side_btn.clicked.connect(
            lambda _checked=False, t=term_name: self.open_semester_profile(t)
        )
        inner.addWidget(side_btn)

        box.addWidget(card, 1)
        return holder

    def _render_partition(self, student: Any) -> None:
        part = partition(student, ARCHIVE_SCOPE)
        locked = part.get("locked") or []
        absent = part.get("absent") or []
        remaining = part.get("remaining_terms", 0)
        elapsed = part.get("terms_elapsed", 0)

        if locked:
            shown = "、".join(f"{item['term']} {item['course']}" for item in locked[:6])
            more = f" 等 {len(locked)} 门" if len(locked) > 6 else ""
            self._locked.setText(
                f"已定型（补考不改分，红线 9，不必再纠结）：{shown}{more}"
            )
        else:
            self._locked.setText("已定型短板：没有 —— 目前没有需要放下的挂科。")

        if absent:
            shown = "、".join(f"{item['term']} {item['course']}" for item in absent[:6])
            self._rescue.setText(f"缺考（还能安排）：{shown}")
        else:
            self._rescue.setText(
                "可抢救项：没有缺考记录。具体哪几门能拉分，看「🎯 侧写辅导」的可抢救项。"
            )

        if not remaining:
            self._remaining.setText(
                "剩余可冲学期：0 —— 已经走到最后一个学期，重点应转向稳妥收尾。"
            )
        elif not elapsed:
            # 学期标签认不出来（导入用的是默认的"未标注"），别硬说"走到第 0 个学期"
            self._remaining.setText(
                "剩余可冲学期：无法判断 —— 导入时没有学期信息（标签是"
                f"「{self._timeline_hint.text() or '未标注'}」）。"
                "带上学期标签再导入，这里就能算出还剩几个学期可冲。"
            )
        else:
            self._remaining.setText(
                f"剩余可冲学期：{remaining} 个"
                f"（按日历走到第 {elapsed} 个学期，共 8 个）。"
            )

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ---------------------------------------------------------------- 个人专属侧写二级页面
    def _back_to_archive(self) -> None:
        """从个人侧写二级页面返回成长档案一级概览。"""
        if self._detail_stack is not None:
            self._detail_stack.setCurrentIndex(0)

    def reset_view(self) -> None:
        """重置两级视图回到未选中态（清库或切换时用）。"""
        self._current = None
        if self._detail_stack is not None:
            self._detail_stack.setCurrentIndex(0)
        self._body.hide()
        self._empty.show()
        if self._semester_page is not None:
            try:
                self._semester_page.stop()
            except Exception:
                pass
        self._sync_buttons()

    def _back_to_overview(self) -> None:
        self._detail_stack.setCurrentWidget(self._overview_page)

    def open_current_profile(self) -> None:
        """打开当前学生的跨学期个人侧写。"""
        if self._current is None or self._detail_stack is None:
            return
        class_name = self._class_by_sid.get(_bare_sid(self._current.student_id), "")
        self._overview_page.set_context(self._store, self._username, self._current, class_name)
        self._detail_stack.setCurrentWidget(self._overview_page)

    def open_semester_profile(self, semester: str) -> None:
        """切到二级页面（SemesterSideProfilePage）展示指定学期的侧写。"""
        if self._current is None or not semester or self._semester_page is None:
            return
        sid = getattr(self._current, "student_id", "") or ""
        name = getattr(self._current, "student", "") or ""
        from_overview = self._detail_stack.currentWidget() is self._overview_page
        back = self._back_to_overview if from_overview else self._back_to_archive
        try:
            self._semester_page.set_context(
                self._store,
                self._username,
                sid,
                name,
                semester,
                on_back=back,
            )
        except Exception:
            logger.exception("打开学期侧写页失败")
            QMessageBox.warning(self, "学期侧写", "打开该学期的个人侧写失败，请稍后再试。")
            return
        if self._detail_stack is not None:
            self._detail_stack.setCurrentIndex(1)

    def open_student_profile(self, payload: dict, semester: str | None = None) -> None:
        """从外部（如智育分析页）指定学生及学期跳转至二级侧写页面。"""
        if not payload:
            return
        sid = _bare_sid(str(payload.get("student_id") or payload.get("sid") or ""))
        name = str(payload.get("student") or payload.get("name") or "").strip()

        target_student = None
        if self._archive and self._archive.students:
            for s in self._archive.students.values():
                if (sid and s.student_id == sid) or (not sid and name and s.student == name):
                    target_student = s
                    break

        if target_student is None:
            self.reload()
            if self._archive and self._archive.students:
                for s in self._archive.students.values():
                    if (sid and s.student_id == sid) or (not sid and name and s.student == name):
                        target_student = s
                        break

        if target_student is not None:
            self.show_student(target_student)
            for i in range(self._list.count()):
                item = self._list.item(i)
                item_sid = item.data(_ROLE_ID)
                item_name = item.data(_ROLE_NAME)
                if (sid and item_sid == sid) or (not sid and name and item_name == name):
                    self._list.setCurrentItem(item)
                    break
        else:
            key = archive_key(sid, name)
            target_student = build_empty_student_archive(key, name, sid)
            self.show_student(target_student)

        if semester:
            self.open_semester_profile(semester)
        else:
            self.open_current_profile()

    # ---------------------------------------------------------------- 学生管理（增/删/改）
    def _save_archive(self) -> bool:
        if self._archive is None or not self._username:
            return False
        return store_archive(self._store, self._username, self._archive)

    def _select_student_by_id(self, sid_or_name: str) -> bool:
        bare = _bare_sid(sid_or_name)
        for i in range(self._list.count()):
            item = self._list.item(i)
            item_sid = item.data(_ROLE_ID)
            item_name = item.data(_ROLE_NAME)
            if (bare and item_sid == bare) or item_name == sid_or_name:
                if item.isHidden():
                    self._search.setText(ARCHIVED_SEARCH_TEXT if self._show_archived else "")
                self._list.setCurrentItem(item)
                self._list.scrollToItem(item)
                self._on_item_clicked(item)
                return True
        return False

    def _open_new_student(self) -> None:
        if self._book is None and self._username:
            self._book = open_student_book(self._store, self._username)
        if self._book is None:
            QMessageBox.information(self, "新建学生", "学生主档还没准备好，请稍后再试。")
            return
        dialog = StudentDialog(self, title="新建学生")
        if dialog.exec() != QDialog.DialogCode.Accepted and dialog.result() != 1:
            return
        values = dialog.values()
        sid = _bare_sid(values.get("student_id", ""))
        name = values.get("name", "").strip()
        if not sid:
            QMessageBox.warning(self, "信息不完整", "学号是必填的。")
            return
        if not name:
            QMessageBox.warning(self, "信息不完整", "姓名是必填的。")
            return
        try:
            self._book.add(
                student_id=sid,
                name=name,
                class_name=values.get("class_name", ""),
                note=values.get("note", ""),
            )
        except DuplicateStudentError:
            QMessageBox.critical(
                self, "学号重复", f"学号 {sid} 已存在，不能重复录入。"
            )
            return
        except Exception:
            logger.exception("新建学生失败")
            QMessageBox.critical(self, "新建学生", "这名学生没有存进去，请稍后再试。")
            return

        if self._archive is None:
            self._archive = Archive()
        key = archive_key(sid, name)
        empty_sa = build_empty_student_archive(key, name, sid)
        self._archive.students[key] = empty_sa
        self._save_archive()

        self._audit_student(
            "student_add", student_id=sid, detail=f"新建学生 {name}（{sid}）"
        )
        self.reload()
        self._select_student_by_id(sid)

    def _open_edit_student(self) -> None:
        if self._current is None:
            return
        old_sid = getattr(self._current, "student_id", "") or ""
        old_name = getattr(self._current, "student", "") or ""
        record = None
        if self._book is not None and old_sid:
            try:
                record = self._book.get(old_sid)
            except Exception:
                logger.exception("读取学生主档失败")
        dialog = StudentDialog(self, record=record, title="编辑学生")
        if dialog.exec() != QDialog.DialogCode.Accepted and dialog.result() != 1:
            return
        values = dialog.values()
        new_sid = _bare_sid(values.get("student_id", ""))
        new_name = values.get("name", "").strip()
        if not new_sid:
            QMessageBox.warning(self, "信息不完整", "学号是必填的。")
            return
        if not new_name:
            QMessageBox.warning(self, "信息不完整", "姓名是必填的。")
            return
        if new_sid != old_sid and old_sid:
            choice = self._confirm_sid_change(old_sid, new_sid)
            if choice is None:
                return
            if choice == "info":
                QMessageBox.information(
                    self,
                    "仅修改信息",
                    f"学生信息已经改成 {new_sid}；历史成绩仍挂在旧学号下（{old_sid}），需要时请手动迁移。",
                )
            else:
                try:
                    report = migrate_student_id_for(
                        old_sid, new_sid, store=self._store, username=self._username
                    )
                except Exception:
                    logger.warning("迁移学号关联数据失败", exc_info=True)
                    report = None
                if report is None:
                    QMessageBox.warning(
                        self,
                        "迁移历史数据",
                        "历史数据迁移没有完成；这次仍会修改学生信息，旧数据需要时请手动核对。",
                    )
                else:
                    self._migrate_grades_fallback(old_sid, new_sid, report)
                    QMessageBox.information(
                        self, "迁移历史数据", migration_report_text(report)
                    )
                self._audit_student(
                    "student_migrate",
                    student_id=new_sid,
                    detail=f"学号 {old_sid} 改为 {new_sid}",
                )

        if self._archive is not None:
            self._archive.rename_student(old_sid, new_sid, new_name, old_name)
            self._save_archive()

        if self._book is not None and old_sid:
            try:
                self._book.update(
                    old_sid,
                    student_id=new_sid,
                    name=new_name,
                    class_name=values.get("class_name", ""),
                    note=values.get("note", ""),
                )
            except Exception:
                logger.exception("编辑学生主档失败")

        self._audit_student(
            "student_edit", student_id=new_sid, detail=f"编辑学生 {new_name}（{new_sid}）"
        )
        self.reload()
        self._select_student_by_id(new_sid or new_name)

    def _delete_student(self) -> None:
        if self._current is None or self._delete_progress is not None:
            return
        sid = getattr(self._current, "student_id", "") or ""
        name = getattr(self._current, "student", "") or ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("删除学生")
        box.setText(
            f"要删除 {name}（{sid}）吗？仅归档只是从列表隐藏，数据全部保留；"
            "彻底删除会清掉成绩、侧写、谈话记录、德育分和意愿。"
        )
        hard_btn = box.addButton(
            "彻底删除（成绩、侧写、谈话记录一起删）", QMessageBox.ButtonRole.DestructiveRole
        )
        archive_btn = box.addButton("归档隐藏", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is hard_btn:
            answer = QMessageBox.question(
                self,
                "确认彻底删除",
                f"确定彻底删除 {name}（{sid}）吗？关联数据会一起删除，无法恢复。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._delete_student_hard(sid, name)
        elif clicked is archive_btn:
            self._delete_student_archive(sid, name)

    def _delete_student_hard(self, sid: str, name: str) -> None:
        store, username = self._store, self._username

        def finished(deleted):
            self._note.setText("删除完成。" if deleted else "该学生已不在本地记录中。")
            self.student_deleted.emit(sid, name)

        self._run_deletion(
            "删除学生", "student.delete",
            lambda control: purge_student(store, username, sid, name, control=control), finished,
            audit=lambda deleted: audit_student_deletion(store, username, sid) if deleted else None,
        )

    def _run_deletion(self, title, operation, remove, done, *, audit=None) -> None:
        if self._delete_progress is not None:
            return
        root = self._store.root
        control = StudentDeletionControl()
        result: Future[Any] = Future()
        deadline = monotonic() + DELETE_PREPARE_TIMEOUT
        progress = _StudentDeletionProgress(self, title)
        self._delete_progress = progress
        timer = QTimer(progress)
        timer.setInterval(DELETE_POLL_INTERVAL_MS)
        progress.open()
        applog.write(operation, "started", root=root)

        def finish() -> None:
            timer.stop()
            self._delete_progress = None
            progress.accept()
            progress.deleteLater()

        def work() -> None:
            if not result.set_running_or_notify_cancel():
                return
            try:
                deleted = remove(control)
            except BaseException as error:
                result.set_exception(error)
                return
            result.set_result(deleted)
            applog.write(operation, "storage_finished", root=root)
            if audit is not None:
                audit(deleted)

        def failed(error: BaseException) -> None:
            finish()
            applog.write(operation, f"failed: {type(error).__name__}", root=root)
            message = str(error) if isinstance(error, CryptoError) else "删除未完成，请检查数据目录后重试。"
            QMessageBox.critical(self, title, message)

        slow_write = False

        def poll() -> None:
            nonlocal slow_write
            # 以后台提交的结果为准。即使线程池的回调没有送达，也能结束进度。
            if result.done():
                try:
                    deleted = result.result()
                except BaseException as error:
                    failed(error)
                    return
                finish()
                applog.write(operation, "completed", root=root)
                refresh_failed = False
                try:
                    self.reset_view()
                    self.reload()
                except Exception as error:
                    applog.write(operation, f"refresh_failed: {type(error).__name__}", root=root)
                    refresh_failed = True
                done(deleted)
                if refresh_failed:
                    self._note.setText("删除已完成。重新打开档案页可刷新名单。")
                return
            if monotonic() < deadline or slow_write:
                return
            if control.cancel_before_write():
                result.cancel()
                failed(CryptoError("等待删除任务超时，本次未写入数据。请稍后重试。"))
                return
            slow_write = True
            progress.show_slow_write()
            applog.write(operation, "slow_write", root=root)

        timer.timeout.connect(poll)
        try:
            run_async(self, work)
        except Exception as error:
            control.cancel_before_write()
            failed(error)
            return
        timer.start()

    def _delete_student_archive(self, sid: str, name: str) -> None:
        if self._book is not None and sid:
            try:
                self._book.archive(sid)
            except Exception:
                logger.exception("归档学生失败")
                QMessageBox.critical(self, "归档学生", "这名学生没有归档成功，请稍后再试。")
                return
        self._audit_student(
            "student_archive", student_id=sid, detail=f"归档学生 {name}（{sid}）"
        )
        self.reset_view()
        self.reload()

    def _confirm_sid_change(self, old_sid: str, new_sid: str) -> str | None:
        if new_sid == old_sid:
            return None
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("修改学号")
        box.setText(
            f"学号要从 {old_sid} 改成 {new_sid}。\n"
            "要不要把历史数据（成绩、侧写、谈话记录、德育分）一起迁移到新学号？"
        )
        migrate_btn = box.addButton("迁移历史数据", QMessageBox.ButtonRole.AcceptRole)
        info_btn = box.addButton("仅修改信息，不迁移历史", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is migrate_btn:
            return "migrate"
        if clicked is info_btn:
            return "info"
        return None

    def _migrate_grades_fallback(self, old_sid: str, new_sid: str, report: Any) -> None:
        if report is None or int(getattr(report, "grades", 0) or 0) > 0:
            return
        try:
            db = GradeHistoryDB(store=self._store, username=self._username)
            moved, dropped = db.rekey_student(old_sid, new_sid)
            if moved or dropped:
                report.grades = int(getattr(report, "grades", 0) or 0) + int(moved)
                report.grade_dropped = (
                    int(getattr(report, "grade_dropped", 0) or 0) + int(dropped)
                )
        except Exception:
            logger.warning("补搬成绩历史失败", exc_info=True)

    def _audit_student(self, action: str, student_id: str = "", detail: str = "") -> None:
        try:
            record_student_audit(
                self._store,
                self._username,
                action,
                student_id=student_id,
                detail=detail,
            )
        except Exception:
            logger.exception("记录学生审计日志失败")


def build(parent: QWidget | None = None) -> QWidget:
    """预留的构建入口（Spec 七：``build(parent)`` 函数签名）。"""
    return ArchivePage(None, parent)
