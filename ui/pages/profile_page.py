"""成绩档案页：左学生名单 + 右两级视图（详情 / 学期侧写）。

布局（v1.9.2 任务书第 2 节）：

    +----------------+----------------------------------------------+
    | 学生名单  [12] | 学生信息卡                                    |
    | [搜索框      ] | 学期时间轴（横向滚动，点一个学期穿透）          |
    | 学生 A         | [成绩明细][总评与侧写][AI 辅导]                |
    | 学生 B         |                                              |
    | [新建][编辑][删]|                                              |
    +----------------+----------------------------------------------+

★ 学生身份一律用**学号**（AGENTS.md §9）：名单、审计、迁移、聊天文件都以学号为键；
  没有学号的名单行只显示、不落进学生主档（同名会串意愿，界面会提示）。

★ 学期均分是**算术平均**（有分数的课程求平均），加权均分与 GPA 一律复用
  ``core.calculator`` / ``core.gpa`` —— 这个页面自己不做任何加权算术（AGENTS §14）。

★ 加密两层（AGENTS §16）：成绩历史、侧写、学生主档、聊天记录全部是应用级密文，
  明文一个字节都不落盘。

★ 界面文案里不写 Markdown 记号（AGENTS §24），也不写版本号字面量（AGENTS §23a）。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable, Sequence

from PySide6.QtCore import QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ai.inference import InferenceUnavailable, friendly_error
from core.archival import SEMESTER_ORDER, merge_records, partition, semester_rank, trend
from core.calculator import CalculationScope, weighted_average
from core.crypto_store import CryptoError, CryptoStore
from core.database import GradeHistoryDB, ProfileStore
from core.scholarship import list_classes, load_moral, save_moral
from ui.anim import make_card
from ui.intentions import (
    INTENTION_OPTIONS,
    KEY_PREFIX_NAME,
    KEY_PREFIX_SID,
    load_intentions,
    save_intention,
    student_key,
)
from ui.pages.analysis_page import scope_breakdown
from ui.pages.profile_shared import (
    ACTION_TEXT,
    ARCHIVED_SEARCH_TEXT,
    CHAT_CONTEXT_TURNS,
    CHAT_HISTORY_DIR,
    CHAT_HISTORY_PREFIX,
    CHAT_HISTORY_SCHEMA,
    CHAT_HISTORY_SUFFIX,
    CHAT_MAX_PROMPT_CHARS,
    CHAT_ROLE_ASSISTANT,
    CHAT_ROLE_USER,
    CHAT_STOP_WORDS,
    CHAT_SYSTEM_PROMPT_TEMPLATE,
    DELETE_CONFIRM_TEXT,
    DELETE_TERM_PROMPT_TEXT,
    GRADE_TREE_COLUMNS,
    ITEM_HEIGHT,
    LIST_WIDTH,
    MAX_RESCUE,
    NO_MORAL_TEXT,
    RESCUE_TARGET,
    SIDE_TEXT_EMPTY_HINT,
    TREND_EPS,
    DuplicateStudentError,
    StudentBook,
    StudentDialog,
    StudentRecord,
    TermNode,
    _ACCENT,
    _AVATAR,
    _DANGER,
    _HOVER_BG,
    _MUTED,
    _SEL_BG,
    _TEXT,
    _WARNING,
    _ROLE_HAS_ID,
    _ROLE_ID,
    _ROLE_INDEX,
    _ROLE_LEVEL,
    _ROLE_NAME,
    _ROLE_SEED,
    _avatar_color,
    _bare_sid,
    _fmt,
    _initial,
    build_chat_prompt,
    build_chat_system_prompt,
    build_empty_student_archive,
    build_side_text_prompt,
    build_student_json,
    delete_all_semester_sides,
    delete_semester_side,
    format_credit,
    format_score,
    grade_rows_from_records,
    group_grades_by_term,
    history_rows_for,
    load_archive,
    migrate_student_id_for,
    migration_report_text,
    moral_of,
    moral_text,
    open_student_book,
    read_side_text,
    record_student_audit,
    rows_arithmetic_mean,
    safe_student_file,
    same_student_id,
    student_archive_of,
    student_archive_of_key,
    student_semester_rows,
    store_archive,
    term_of_archive,
)
from ui.pages.semester_side_profile import SemesterSideProfilePage

logger = logging.getLogger(__name__)

#: 还在跑的线程，防止被 GC 掉（Qt 里 QThread 被回收会直接崩）。
_LIVE_WORKERS: set[Any] = set()

__all__ = [
    "ACTION_TEXT",
    "CHAT_HISTORY_DIR",
    "CHAT_HISTORY_PREFIX",
    "CHAT_HISTORY_SUFFIX",
    "CHAT_SYSTEM_PROMPT_TEMPLATE",
    "DELETE_CONFIRM_TEXT",
    "DELETE_TERM_PROMPT_TEXT",
    "GRADE_TREE_COLUMNS",
    "ITEM_HEIGHT",
    "LIST_WIDTH",
    "ProfilePage",
    "RESCUE_TARGET",
    "RowActionDelegate",
    "SIDE_TEXT_EMPTY_HINT",
    "StudentItemDelegate",
    "_ROLE_HAS_ID",
    "_ROLE_ID",
    "_ROLE_INDEX",
    "_ROLE_LEVEL",
    "_ROLE_NAME",
    "_ROLE_SEED",
    "_avatar_color",
    "build_chat_prompt",
    "build_chat_system_prompt",
    "build_side_text_prompt",
    "build_student_json",
    "format_credit",
    "format_score",
    "grade_rows_from_records",
    "group_grades_by_term",
    "moral_of",
    "moral_text",
    "read_side_text",
    "safe_student_file",
    "term_of_archive",
]


class StudentItemDelegate(QStyledItemDelegate):
    """名单条目自绘 —— 头像 + 姓名 + 学号 + 预警点。

    ★ 为什么用 delegate 而不是给每条 ``setItemWidget`` 塞一个 QWidget：
      ``setItemWidget`` 每个条目都要建一整棵控件树。一个班 40 人还行，
      一届几百人就是几百个 QWidget，滚动会明显掉帧。delegate 只画画，
      没有控件对象，几千条也不会卡。

    ★ 每一条画出来的东西都来自 ``index.data(...)``，**没有一处另算**：
      姓名/学号来自 :class:`TableRow`，预警级别来自智育分析页**当前口径**
      现填的 flag。所以这个列表不会自己编数据。
    """

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(0, ITEM_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect: QRect = option.rect
        # ★ 用 QStyle.StateFlag 枚举按位与 —— **不要**写 option.state.__class__，
        #   那拿到的是 flag 的运行时类型，不是 QStyle。
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # --- 底 ---
        if selected:
            painter.fillRect(rect, QColor(_SEL_BG))
            # 左侧 4px 主色线：跟侧栏导航按钮的选中态用同一种语言
            painter.fillRect(QRect(rect.left(), rect.top(), 4, rect.height()),
                             QColor(_ACCENT))
        elif hovered:
            painter.fillRect(rect, QColor(_HOVER_BG))

        # --- 头像圆 ---
        seed = index.data(_ROLE_SEED) or ""
        name = index.data(_ROLE_NAME) or ""
        ax = rect.left() + 14
        ay = rect.top() + (rect.height() - _AVATAR) // 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_avatar_color(seed)))
        painter.drawEllipse(ax, ay, _AVATAR, _AVATAR)

        initial_font = QFont(option.font)
        # pointSizeF() 在字体是用 pixelSize 设的时候返回 -1 —— 兜一个底，
        # 否则 setPointSizeF(-1+1) 会让字直接消失（实测过）。
        base_pt = option.font.pointSizeF()
        if base_pt <= 0:
            base_pt = 10.0
        initial_font.setPointSizeF(base_pt + 1.0)
        initial_font.setBold(True)
        painter.setFont(initial_font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRect(ax, ay, _AVATAR, _AVATAR),
                         Qt.AlignmentFlag.AlignCenter, _initial(name))

        # --- 右侧预留：预警点 / 缺学号角标 ---
        level = index.data(_ROLE_LEVEL) or ""
        has_id = bool(index.data(_ROLE_HAS_ID))
        right_reserve = 22

        # --- 文字区 ---
        tx = ax + _AVATAR + 11
        tw = rect.right() - right_reserve - tx
        if tw > 20:
            name_font = QFont(option.font)
            name_font.setBold(True)
            painter.setFont(name_font)
            painter.setPen(QColor(_TEXT if (selected or hovered) else "#1F2430"))
            metrics = QFontMetrics(name_font)
            name_text = metrics.elidedText(name, Qt.TextElideMode.ElideRight, tw)

            sid = index.data(_ROLE_ID) or ""
            name_rect = QRect(tx, rect.top() + 9, tw, 18)
            painter.drawText(name_rect,
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             name_text)

            sub_font = QFont(option.font)
            sub_font.setPointSizeF(max(8.0, base_pt - 1.0))
            painter.setFont(sub_font)
            if has_id:
                painter.setPen(QColor(_MUTED))
                sub_text = sid
            else:
                # 没学号 = 只能靠姓名区分，同名会串意愿。**在名单上就说清楚**，
                # 不要等用户点进去才告诉他。
                painter.setPen(QColor(_WARNING))
                sub_text = "无学号 · 同名会串意愿"
            sub_metrics = QFontMetrics(sub_font)
            painter.drawText(
                QRect(tx, rect.top() + 28, tw, 16),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                sub_metrics.elidedText(sub_text, Qt.TextElideMode.ElideRight, tw),
            )

        # --- 预警点：红=必须当场处理，黄=数据可疑 ---
        if level in ("danger", "warning"):
            color = _DANGER if level == "danger" else _WARNING
            d = 8
            cx = rect.right() - 16
            cy = rect.top() + rect.height() // 2
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(cx - d // 2, cy - d // 2, d, d)

        # --- 分隔线（最后一条不画）---
        painter.setPen(QColor("#F1F5F9"))
        painter.drawLine(rect.left() + 14, rect.bottom(),
                         rect.right() - 14, rect.bottom())
        painter.restore()


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


#: 「操作」列那个小按钮的配色（照 ``ui/styles.qss`` 的 ``QPushButton#RowAction``）。
_ACTION_BG = "#EFF6FF"
_ACTION_BG_HOVER = "#DBEAFE"
_ACTION_BG_PRESSED = "#BFDBFE"
_ACTION_BORDER = "#BFDBFE"

#: 小按钮的尺寸与内边距（``paint`` 与 ``on_grade_action`` 的命中判定共用一份，
#: 免得"画在哪儿"和"点哪儿算数"两份数字漂移）。
_ACTION_SIZE = QSize(58, 24)
_ACTION_PAD = 4

#: 文本框光标移到末尾（模块级常量：不用每次去 ``textCursor()`` 上取枚举）
_CURSOR_END = QTextCursor.MoveOperation.End


class RowActionDelegate(QStyledItemDelegate):
    """在表格/树的一列里画一个**纯文字**的小按钮（「删除」）。

    ★ 为什么不用 ``setCellWidget``：一个学期八行、一名学生八学期，控件数量
      立刻上百；而且真表里 ``setCellWidget`` 放进去的东西
      ``QHeaderView.ResizeToContents`` 是不算的（AGENTS §4c 第 2 条），
      列宽会跳。委托只画像素，滚动/重建都不留控件。

    ★ 点击**不由本类处理**：``cell_rect()`` 把按钮矩形算出来，
      ``ProfilePage._on_grade_clicked`` 拿它做命中判定 —— 命中才删，
      点这一列的其他地方什么都不做。
    """

    def __init__(self, text: str = ACTION_TEXT, parent: Any = None) -> None:
        super().__init__(parent)
        self._text = text

    # -- 几何（画与点共用） --------------------------------------------------
    def cell_rect(self, rect: QRect) -> QRect:
        """给定单元格矩形，算出里面那个按钮的位置（**不依赖任何控件状态**）。"""
        width = min(_ACTION_SIZE.width(), max(0, rect.width() - _ACTION_PAD * 2))
        height = min(_ACTION_SIZE.height(), max(0, rect.height() - _ACTION_PAD * 2))
        left = rect.left() + max(0, (rect.width() - width) // 2)
        top = rect.top() + max(0, (rect.height() - height) // 2)
        return QRect(left, top, width, height)

    # --- 画 -----------------------------------------------------------------
    def paint(self, painter: QPainter, option: Any, index: Any) -> None:
        super().paint(painter, option, index)
        rect = QRect(option.rect)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        area = self.cell_rect(rect)
        if area.width() <= 0 or area.height() <= 0:
            return

        state = option.state
        pressed = bool(state & QStyle.StateFlag.State_Sunken)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        background = _ACTION_BG_PRESSED if pressed else (
            _ACTION_BG_HOVER if hovered else _ACTION_BG
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(background))
        painter.setPen(QColor(_ACTION_BORDER))
        painter.drawRoundedRect(area, 6, 6)

        font = QFont(option.font)
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.0))
        painter.setFont(font)
        painter.setPen(QColor(_ACCENT))
        painter.drawText(area, int(Qt.AlignmentFlag.AlignCenter), self._text)
        painter.restore()

    def sizeHint(self, option: Any, index: Any) -> QSize:  # noqa: N802 - Qt 命名
        hint = super().sizeHint(option, index)
        return QSize(max(hint.width(), _ACTION_SIZE.width()),
                     max(hint.height(), _ACTION_SIZE.height() + _ACTION_PAD * 2))


# ---------------------------------------------------------------------------
# 推理接线：**只有这里碰 core.ai**（零 HTTP，import 期绝不 import llama_cpp）
# ---------------------------------------------------------------------------


def engine_available() -> tuple[bool, str]:
    """本机推理引擎装没装（``(可用, 中文原因)``）。

    ★ 真源是 :func:`core.ai.inference.engine_available`（零 HTTP，
      只用 ``importlib.util.find_spec`` 探一探、**不 import** llama_cpp）。
      本函数只是一层"探不动也别炸"的包装：core 那边万一有问题，这里给中文原因，
      页面照样开得起来。
    """
    from core.ai import inference

    return inference.engine_available()


def list_local_models() -> list[Any]:
    """列出本机**装好了**的模型（``%LOCALAPPDATA%\\SGMS\\models``）。"""
    from core.ai import inference

    return list(inference.list_local_models())


def _load_chat_worker() -> Any:
    """拿到 ``ui.chat_dialog`` 模块（里面有 ``_ChatWorker`` / ``LocalModelEngine``）。

    ★ 懒 import：不点「发送」就不进内存（AGENTS §34 的写法）；拿不到就抛
      :class:`InferenceUnavailable` —— 让**调用方**去说中文人话，不在这里静默。
    """
    try:
        from ui import chat_dialog
    except Exception as exc:  # noqa: BLE001 - 界面模块缺失 / 语法错 / 缺依赖
        logger.exception("加载 ui.chat_dialog 失败")
        raise InferenceUnavailable(f"谈话界面模块没装上：{exc}") from exc
    return chat_dialog


def _clock_now() -> str:
    """谈话记录上那个时间戳（只要"几点几分"，不要日期 —— 记录本身按天开）。"""
    from datetime import datetime

    return datetime.now().strftime("%H:%M")


class _SideWorker(QThread):
    """后台生成侧写正文（AGENTS §5b / §21a：主线程里跑推理会冻住窗口）。

    ★ 判据不认线程：真跑时用 ``start()``；测试把
      :meth:`ProfilePage._start_worker` 换掉就**同步跑**（进程里没有真线程，
      也就不会有"跑完的 QThread 被回收"那类事故 —— §4k 同族）。
    """

    chunk = Signal(str)
    failed = Signal(str)
    done = Signal()

    def __init__(self, engine: Any, prompt: str, *, max_tokens: int = 512,
                 parent: Any = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._prompt = prompt
        self._max_tokens = max_tokens
        self._cancelled = False
        self.answer = ""

    def cancel(self) -> None:
        """请求停止（下一块就不再追加；不等它 —— 等的是收尾回调）。"""
        self._cancelled = True

    def run(self) -> None:  # noqa: D102 - QThread 约定
        """跑完（无论成败）都要发一次 :attr:`done`。

        ★ **不能只连 ``QThread.finished``**：测试把 :meth:`ProfilePage._start_worker`
          换成"就地调 ``run()``"时进程里没有线程、``finished`` 根本不会发，
          收尾回调永远不执行（症状是状态行一直停在"正在生成…"、按钮永远灰着）。
          自己发一次才是"判据不认线程"（同 :class:`ui.chat_dialog._ChatWorker`
          那边的 ``QThread.finished`` 约定：真线程里它由 Qt 发）。
        """
        try:
            load = getattr(self._engine, "load", None)
            if callable(load):
                load()
            for piece in self._engine.stream(self._prompt, max_tokens=self._max_tokens):
                if self._cancelled:
                    break
                if not piece:
                    continue
                self.answer += piece
                self.chunk.emit(piece)
        except InferenceUnavailable as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 一律翻成人话，绝不留英文堆栈
            logger.exception("侧写生成失败")
            self.failed.emit(friendly_error(exc))
        finally:
            self.done.emit()


_CHAT_WORKER: Any = None


class _PageChatWorker(QThread):
    """谈话页自己的谈话线程：**契约照抄** ``ui.chat_dialog._ChatWorker``（冻结文件）。

    ``chunk`` / ``failed`` 两个信号与 ``answer`` / ``cancel()`` 与它逐字同形，
    另外补一个 ``done``。**不继承它**是因为 ``ui/chat_dialog`` 只能懒 import
    （它自己带 Qt 依赖，缺了不该让整页打不开），而继承关系必须写死在定义期
    —— 于是复制契约比制造耦合更安全（那个文件的 ``open_chat`` 是 §34 冻结接口，
    本来也不许改）。

    ★ ``done`` 为什么非有不可：收尾回调原来连的是 ``QThread.finished`` —— 测试把
      :meth:`ProfilePage._start_worker` 换成"就地调 ``run()``"时进程里没有线程，
      ``finished`` **永远不会发**，于是"回答写盘 / 恢复输入区"整段不执行
      （症状：状态行一直卡在"正在生成…"、输入框永远灰着）。自己发一次才是
      "判据不认线程"。
    """

    chunk = Signal(str)
    failed = Signal(str)
    done = Signal()

    def __init__(self, engine: Any, prompt: str, *, max_tokens: int = 512,
                 temperature: float = 0.7,
                 stop: Sequence[str] | None = None, **kwargs: Any) -> None:
        parent = kwargs.pop("parent", None)
        super().__init__(parent)
        self._engine = engine
        self._prompt = prompt
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._stop = stop
        self._cancelled = False
        self.answer = ""

    def cancel(self) -> None:
        """请求停止（下一块就不再追加；不等它 —— 等的是收尾回调）。"""
        self._cancelled = True
        self.requestInterruption()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # noqa: D102 - QThread 约定
        try:
            load = getattr(self._engine, "load", None)
            if callable(load):
                load()
            for piece in self._engine.stream(
                self._prompt, self._max_tokens, self._temperature,
                stop=self._stop or None,
            ):
                if self._cancelled:
                    break
                if not piece:
                    continue
                self.answer += piece
                self.chunk.emit(piece)
        except InferenceUnavailable as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 一律翻成人话，绝不留英文堆栈
            logger.exception("谈话生成失败")
            self.failed.emit(friendly_error(exc))
        finally:
            self.done.emit()


class ProfilePage(QWidget):
    """成绩档案页：左边学生名单，右边两级视图（详情页 / 学期侧写页）。

    ★ v1.9.2 的布局（任务书第 2 节）：

    * 左：学生名单卡片（300 宽）—— 标题 + 人数、搜索框（姓名 / 学号 / 班级）、
      名单列表、新建 / 编辑 / 删除三个按钮；
    * 右：:class:`QStackedWidget` 两页 ——

      - page0 详情：学生信息卡 + 学期时间轴（横向滚动，点一个学期穿透）
        + 三个标签页（成绩明细 / 总评与侧写 / AI 辅导）；
      - page1 学期侧写：:class:`SemesterSideProfilePage`，带「← 返回」。

    ★ 学生身份一律用**学号**（AGENTS §9）：名单行、审计、迁移、聊天文件都以
      学号为键，没有学号的名单行只显示、不落档。
    """

    # -- 构造 / 对外接口 --------------------------------------------------
    def __init__(
        self,
        store: CryptoStore | None = None,
        parent: QWidget | None = None,
        *,
        chat_models: list[Any] | None = None,
        chat_engine: Any = None,
    ) -> None:
        super().__init__(parent)
        self._store: CryptoStore = store or CryptoStore()
        self._username = ""
        self._payload: dict[str, Any] | None = None
        self._intentions: dict[str, str] = {}
        self._roster: list[Any] = []
        self._key_by_index: dict[int, str] = {}
        self._provider: Callable[[int], dict[str, Any] | None] | None = None
        self._profile_store: Any = None
        self._grade_db: Any = None
        self._moral_cache: dict[str, Any] | None = None
        self._tag_pool: dict[str, list[QWidget]] = {}
        self._active_tab = 0
        self._grade_payloads: dict[int, dict[str, Any]] = {}
        self._hidden_terms: set[str] = set()

        self._chat_engine = chat_engine
        self._chat_models_given = chat_models is not None
        self._chat_models: list[Any] = list(chat_models or [])
        self._chat_key = ""
        self._chat_student_id = ""
        self._chat_history: list[dict[str, str]] = []
        self._chat_worker: Any = None
        self._chat_answer = ""
        self._chat_open = False
        self._chat_error = ""
        self._chat_engine_reason = ""
        self._chat_model: Any = None
        self._chat_system_prompt = ""
        self._chat_student_json = ""

        self._side_key = ""
        self._side_busy = False
        self._side_worker: Any = None
        self._side_error = ""
        self._side_text_guard = False

        # v1.9.2 新增状态
        self._book: Any = None
        self._entries: list[dict[str, Any]] = []
        self._selected_key = ""
        self._selected_sid = ""
        self._current_term = ""
        self._search_text = ""
        self._total_people = 0
        self._no_data = False
        self._stack: QStackedWidget | None = None
        self._semester_page: SemesterSideProfilePage | None = None
        self._build()

    def set_user(self, username: str) -> None:
        """换人：清掉上一个人的库句柄与聊天状态，名单按新账户重建。"""
        self._username = (username or "").strip()
        try:
            self._intentions = load_intentions(self._store, self._username) or {}
        except Exception:  # noqa: BLE001 - 意愿是便利数据，坏掉不该拦住整页
            logger.exception("读取发展意愿失败")
            self._intentions = {}
        self._profile_store = None
        self._grade_db = None
        self._moral_cache = None
        self._hidden_terms = set()
        self._chat_key = ""
        self._chat_student_id = ""
        self._chat_history = []
        self._chat_system_prompt = ""
        self._chat_student_json = ""
        self._stop_chat_worker()
        self._book = open_student_book(self._store, self._username)
        self._current_term = ""
        self._no_data = False
        self._fill_list(self._search_text)
        if self._payload:
            self._render()

    def set_payload_provider(
        self, provider: Callable[[int], dict[str, Any] | None]
    ) -> None:
        """分析页把「第 index 行是谁」的取数口交过来。"""
        self._provider = provider

    def set_roster(self, rows: list[Any]) -> None:
        """导入完成后把名单推过来：有学号的补进学生主档，然后刷新列表。"""
        self._roster = list(rows or [])
        self._no_data = False
        self._migrate_name_keys()
        self._upsert_roster_into_book()
        self._fill_list(self._search_text)
        if self._payload:
            self._select_key(self._payload.get("key", ""))

    def show_no_data(self) -> None:
        """成绩库被清空 / 还没导入：名单与详情都回到空态。"""
        self._no_data = True
        self._roster = []
        self._payload = None
        self._selected_key = ""
        self._selected_sid = ""
        self._current_term = ""
        self._chat_key = ""
        self._chat_student_id = ""
        self._chat_history = []
        self._chat_system_prompt = ""
        self._chat_student_json = ""
        self._stop_chat_worker()
        if self._book is not None:
            try:
                self._book.refresh()
            except Exception:  # noqa: BLE001
                logger.exception("刷新学生主档失败")
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._fill_list("")
        self._list_note.setText(
            "暂无数据：成绩库已清空，先在「智育分析」导入成绩表。"
        )
        self._clear_semester_page()
        if self._stack is not None:
            self._stack.setCurrentIndex(0)
        self._render()

    def reset_view(self) -> None:
        """两级视图都复位：回详情层、清空右栏与学期侧写页（名单保留）。"""
        self._payload = None
        self._selected_key = ""
        self._selected_sid = ""
        self._current_term = ""
        self._chat_key = ""
        self._chat_student_id = ""
        self._chat_history = []
        self._chat_system_prompt = ""
        self._chat_student_json = ""
        self._stop_chat_worker()
        self._clear_semester_page()
        if self._stack is not None:
            self._stack.setCurrentIndex(0)
        self._list.setCurrentRow(-1)
        self._sync_buttons()
        self._render()

    def _clear_semester_page(self) -> None:
        """把学期侧写页也清空，免得切回第二级还能看到上一个学生。"""
        page = self._semester_page
        if page is None:
            return
        try:
            page.set_records([])
            page.set_term("")
            page.set_student({})
        except Exception:  # noqa: BLE001 - 清空失败只打日志，不许拦住复位
            logger.exception("清空学期侧写页失败")

    def show_student(self, payload: dict[str, Any]) -> None:
        """明确指定一名学生：选中名单行并重画详情。"""
        self._payload = payload or None
        if not payload:
            self._render()
            return
        self._selected_key = str(payload.get("key") or "")
        self._selected_sid = _bare_sid(payload.get("student_id"))
        self._select_key(self._selected_key)
        self._render()

    def focus_list(self) -> None:
        """把焦点交给搜索框（主窗点侧栏时用）。"""
        self._search.setFocus()

    def current_student_id(self) -> str:
        """当前选中的学号（裸学号；没有选中就空串）。

        任务书第 1 节只要求 ``current_student_id() -> str``：界面上、聊天文件名、
        学期侧写页标题一律用裸学号，带 ``sid:`` 前缀的键形式只留在档案与意愿里。
        """
        return self._bare_current_sid()

    def _bare_current_sid(self) -> str:
        """内部统一用裸学号：显示、查库、写文件名都不带前缀。"""
        if self._payload:
            sid = _bare_sid(self._payload.get("student_id"))
            if sid:
                return sid
        sid = _bare_sid(self._selected_sid)
        if sid:
            return sid
        entry = self._current_entry()
        return _bare_sid(entry["sid"]) if entry is not None else ""

    def ask_semester_and_open(self, payload: dict[str, Any] | None = None) -> None:
        """打开「选学期」框，选完切到那一学期的学业侧写。"""
        if payload:
            self.show_student(payload)
        terms = self._timeline_terms()
        if not terms:
            QMessageBox.information(
                self, "学期侧写", "这名学生还没有成绩记录，先导入成绩表。"
            )
            return
        current = self._current_term if self._current_term in terms else terms[-1]
        term, accepted = QInputDialog.getItem(
            self,
            "选择学期",
            "要看哪一个学期的学业侧写？",
            terms,
            terms.index(current),
            False,
        )
        if accepted and term:
            self.open_semester(semester=str(term))

    def open_semester(self, student_id: str = "", semester: str = "") -> None:
        """切到 page1：显示某名学生某个学期的学业侧写。

        Args:
            student_id: 学号（``sid:`` 或裸学号都行）；省略 = 当前选中的学生。
            semester: 学期名，例如 ``大二下``；空字符串不切页。
        """
        sid = _bare_sid(student_id)
        term = (semester or "").strip()
        if not self._payload:
            return
        # 兼容旧调用：open_semester("大二下") —— 唯一实参其实是学期名
        if not term and sid and sid not in self._known_sids():
            if sid in self._timeline_terms():
                term, sid = sid, ""
        if not term:
            return
        if sid and sid != self._bare_current_sid():
            if not self._select_student_by_sid(sid):
                logger.warning("open_semester 找不到学号 %s 对应的学生", sid)
                return
        self._current_term = term
        self._render_timeline()
        if self._semester_page is None or self._stack is None:
            return
        try:
            self._semester_page.set_context(
                self._store,
                self._username,
                self._bare_current_sid(),
                str(self._payload.get("student") or ""),
                term,
                on_back=self._back_to_detail,
            )
        except Exception:  # noqa: BLE001 - 学期页打不开要说人话，不许冒到事件循环
            logger.exception("打开学期侧写页失败")
            QMessageBox.warning(self, "学期侧写", "这一学期的侧写页没有打开，请稍后再试。")
            return
        self._stack.setCurrentIndex(1)

    def _known_sids(self) -> set[str]:
        return {_bare_sid(entry.get("sid")) for entry in self._entries if entry.get("sid")}

    def _select_student_by_sid(self, sid: str) -> bool:
        """按学号在名单里选中并载入 payload（找不到就 False）。"""
        bare = _bare_sid(sid)
        for position, entry in enumerate(self._entries):
            if _bare_sid(entry.get("sid")) != bare:
                continue
            payload = self._payload_for_entry(entry)
            if payload is None:
                return False
            self._list.setCurrentRow(position)
            self._selected_key = entry["key"]
            self._selected_sid = bare
            self._payload = payload
            self._sync_buttons()
            return True
        return False

    def _back_to_detail(self) -> None:
        if self._stack is not None:
            self._stack.setCurrentIndex(0)

    # -- 列表 -------------------------------------------------------------
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        root.addWidget(self._build_list_pane())
        root.addWidget(self._build_stack(), 1)

    def _build_stack(self) -> QWidget:
        self._stack = QStackedWidget()
        self._stack.setObjectName("ProfileStack")
        self._stack.addWidget(self._build_detail_pane())
        self._semester_page = SemesterSideProfilePage()
        self._stack.addWidget(self._semester_page)
        return self._stack

    def _build_list_pane(self) -> QFrame:
        card = make_card()
        card.setFixedWidth(LIST_WIDTH)
        box = QVBoxLayout(card)
        box.setContentsMargins(0, 0, 0, 12)
        box.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(14, 13, 12, 10)
        head.setSpacing(8)
        title = QLabel("学生名单")
        title.setObjectName("H2")
        head.addWidget(title)
        head.addStretch(1)
        self._count_pill = QLabel("0")
        self._count_pill.setObjectName("CountPill")
        self._count_pill.setToolTip("当前名单人数")
        head.addWidget(self._count_pill)
        box.addLayout(head)

        search_holder = QHBoxLayout()
        search_holder.setContentsMargins(12, 0, 12, 10)
        self._search = QLineEdit()
        self._search.setObjectName("ListSearch")
        self._search.setPlaceholderText("搜索姓名或学号")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(32)
        self._search.textChanged.connect(self._on_search)
        search_holder.addWidget(self._search)
        box.addLayout(search_holder)

        self._list = QListWidget()
        self._list.setObjectName("StudentList")
        self._list.setAlternatingRowColors(False)
        self._list.setUniformItemSizes(True)
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setItemDelegate(StudentItemDelegate(self._list))
        self._list.itemClicked.connect(self._on_item_clicked)
        box.addWidget(self._list, 1)

        self._list_note = QLabel("")
        self._list_note.setObjectName("ListNote")
        self._list_note.setWordWrap(True)
        self._list_note.setContentsMargins(14, 8, 12, 10)
        box.addWidget(self._list_note)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(12, 6, 12, 0)
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

        self._fill_list("")
        return card

    def _fill_list(self, keyword: str = "") -> None:
        """按关键字重建名单（默认只看未归档；输入「已归档」只看归档）。"""
        self._search_text = keyword or ""
        all_entries = self._list_entries("")
        self._total_people = len(all_entries)
        entries = self._list_entries(self._search_text)
        self._entries = entries
        self._list.blockSignals(True)
        try:
            self._list.clear()
            self._key_by_index = {}
            for position, entry in enumerate(entries):
                label = (
                    f"{entry['name']}（{entry['sid']}）"
                    if entry["sid"]
                    else entry["name"]
                )
                item = QListWidgetItem(label)
                item.setData(_ROLE_INDEX, position)
                item.setData(_ROLE_NAME, entry["name"])
                item.setData(_ROLE_ID, entry["sid"])
                item.setData(_ROLE_HAS_ID, bool(entry["sid"]))
                item.setData(_ROLE_LEVEL, entry.get("level", ""))
                item.setData(_ROLE_SEED, entry["sid"] or entry["name"])
                item.setSizeHint(QSize(0, ITEM_HEIGHT))
                if entry["sid"]:
                    tip = f"学号 {entry['sid']}"
                    if entry.get("class_name"):
                        tip += f" · 班级 {entry['class_name']}"
                    if entry.get("archived"):
                        tip += " · 已归档"
                else:
                    tip = "表里没有学号，同名学生会共用同一份意愿"
                item.setToolTip(tip)
                self._list.addItem(item)
                self._key_by_index[position] = entry["key"]
        finally:
            self._list.blockSignals(False)
        self._update_list_note()
        self._sync_buttons()

    def _list_entries(self, keyword: str) -> list[dict[str, Any]]:
        """名单数据源：学生主档优先，没学号的名单行只显示不落档。

        ``show_no_data()`` 只表示"成绩库清空了"，学生主档（``students.json.enc``）
        是独立的一份数据，主档里还有人时名单照常显示。
        """
        if self._no_data:
            kept = 0
            if self._book is not None and getattr(self._book, "durable", False):
                try:
                    kept = int(self._book.count())
                except Exception:  # noqa: BLE001 - 数不出来就按空档处理
                    logger.exception("读取学生主档数量失败")
            if kept <= 0:
                return []
        text = (keyword or "").strip()
        archived_only = ARCHIVED_SEARCH_TEXT in text
        needle = text.replace(ARCHIVED_SEARCH_TEXT, "").strip()
        entries: list[dict[str, Any]] = []
        durable = bool(self._book is not None and getattr(self._book, "durable", False))
        book_has_rows = True
        if durable:
            try:
                book_has_rows = int(self._book.count()) > 0
            except Exception:  # noqa: BLE001 - 数不出来就按"有档"处理
                logger.exception("读取学生主档数量失败")
        if durable:
            try:
                records = self._book.search(needle, archived_only=archived_only)
            except Exception:  # noqa: BLE001 - 主档读不出来就退回名单
                logger.exception("搜索学生主档失败")
                records = []
            for record in records:
                sid = _bare_sid(getattr(record, "student_id", ""))
                name = str(getattr(record, "name", "") or "").strip() or sid
                entries.append(
                    {
                        "sid": sid,
                        "name": name,
                        "class_name": str(getattr(record, "class_name", "") or ""),
                        "archived": bool(getattr(record, "archived", False)),
                        "key": student_key(sid, name),
                        "index": self._roster_index(sid, name),
                        "level": "",
                    }
                )
        if not archived_only:
            seen = {entry["sid"] for entry in entries if entry["sid"]}
            for row in self._roster:
                sid = _bare_sid(getattr(row, "student_id", ""))
                name = str(getattr(row, "student", "") or "").strip()
                if sid and sid in seen:
                    continue
                # 主档里已经有这个学号，不重复显示；但主档整体为空时必须退回
                # 内存名单，否则"导入完成、还没建档"的学生会一个都看不到。
                if sid and durable and book_has_rows:
                    continue
                if needle and needle.casefold() not in f"{name}{sid}".casefold():
                    continue
                entries.append(
                    {
                        "sid": sid,
                        "name": name or "（未命名）",
                        "class_name": "",
                        "archived": False,
                        "key": student_key(sid, name),
                        "index": int(getattr(row, "index", -1) or -1),
                        "level": str(getattr(getattr(row, "flag", None), "level", "") or ""),
                    }
                )
        return entries

    def _roster_index(self, sid: str, name: str) -> int | None:
        """在分析页名单里找这个人的行号（找不到就 None）。"""
        if self._provider is None:
            return None
        unique_name = False
        if not sid and name:
            same = [
                row
                for row in self._roster
                if str(getattr(row, "student", "") or "").strip() == name
            ]
            unique_name = len(same) == 1
        for row in self._roster:
            row_sid = _bare_sid(getattr(row, "student_id", ""))
            row_name = str(getattr(row, "student", "") or "").strip()
            if sid and row_sid == sid:
                return int(getattr(row, "index", -1))
            if not sid and unique_name and row_name == name and not row_sid:
                return int(getattr(row, "index", -1))
        return None

    def _entry_of(self, position: int) -> dict[str, Any] | None:
        if 0 <= position < len(self._entries):
            return self._entries[position]
        return None

    def _current_entry(self) -> dict[str, Any] | None:
        row = self._list.currentRow()
        return self._entry_of(row) if row >= 0 else None

    def _update_list_note(self) -> None:
        total = self._total_people
        shown = len(self._entries)
        missing = sum(1 for entry in self._list_entries("") if not entry["sid"])
        self._count_pill.setText(str(total))
        if total == 0:
            self._list_note.setText(
                "还没有名单。先在「智育分析」导入成绩表，或点下面的「新建学生」。"
            )
            return
        if self._search_text and shown != total:
            text = f"筛出 {shown} / {total} 人"
        else:
            text = f"共 {total} 人"
        archived = sum(1 for entry in self._entries if entry.get("archived"))
        if archived:
            text += f"　·　含 {archived} 人已归档"
        if missing:
            text += f"\n注意：{missing} 人没有学号，同名会共用意愿"
        self._list_note.setText(text)

    def _sync_buttons(self) -> None:
        entry = self._current_entry()
        has_row = entry is not None
        self._edit_btn.setEnabled(has_row and bool(entry and entry["sid"]))
        self._delete_btn.setEnabled(has_row and bool(entry and entry["sid"]))

    @property
    def roster_size(self) -> int:
        return self._total_people

    @property
    def visible_count(self) -> int:
        return len(self._entries)

    def _on_search(self, text: str) -> None:
        self._fill_list(text)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        position = item.data(_ROLE_INDEX)
        entry = self._entry_of(int(position))
        if entry is None:
            return
        self._selected_key = entry["key"]
        self._selected_sid = entry["sid"]
        self._current_term = ""
        self._sync_buttons()
        payload = self._payload_for_entry(entry)
        if payload is None:
            self._list_note.setText("这一行没有可显示的侧写数据。")
            return
        self._payload = payload
        if self._stack is not None:
            self._stack.setCurrentIndex(0)
        self._render()

    def _payload_for_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        index = entry.get("index")
        if index is not None and index >= 0 and callable(self._provider):
            try:
                payload = self._provider(int(index))
            except Exception:  # noqa: BLE001 - 一行读不出来不该带崩整页
                logger.exception("读取这一行的侧写数据失败")
                payload = None
            if payload:
                return payload
        key = entry["key"]
        sid = entry["sid"]
        name = entry["name"]
        archive = None
        try:
            full = load_archive(self._store, self._username)
            archive = student_archive_of_key(full, key)
        except Exception:  # noqa: BLE001
            logger.exception("读取档案失败")
            archive = None
        if archive is None:
            archive = build_empty_student_archive(key, name, sid)
        return {
            "student": name,
            "student_id": sid,
            "key": key,
            "index": index,
            "results": {},
            "archive": archive,
        }

    def _select_key(self, key: str) -> None:
        target = key or ""
        for position, entry_key in self._key_by_index.items():
            if entry_key == target:
                self._list.setCurrentRow(position)
                self._select_item(position)
                return

    def _select_item(self, position: int) -> None:
        item = self._list.item(position)
        if item is None:
            return
        self._list.setCurrentItem(item)
        self._list.scrollToItem(item)

    def _upsert_roster_into_book(self) -> None:
        if self._book is None:
            return
        for row in self._roster:
            sid = _bare_sid(getattr(row, "student_id", ""))
            if not sid:
                continue
            name = str(getattr(row, "student", "") or "").strip()
            if not name:
                continue
            try:
                self._book.upsert_import(sid, name)
            except Exception:  # noqa: BLE001 - 主档漏一个人是显示问题，不许影响导入
                logger.warning("补档失败（%s / %s）", sid, name, exc_info=True)

    # -- 详情页 -----------------------------------------------------------
    def _build_detail_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        self._empty = QLabel(
            "在左边点一名学生，这里显示他的成绩明细、总评与侧写、AI 辅导。\n\n"
            "名单来自「智育分析」导入的成绩表；也可以点下面的「新建学生」手动补。"
        )
        self._empty.setObjectName("Placeholder")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        box.addWidget(self._empty, 1)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        body.addWidget(self._build_info_card())
        body.addWidget(self._build_timeline_card())

        self._tabs = QTabWidget()
        self._tabs.setObjectName("ProfileTabs")
        self._tabs.addTab(self._build_grades_pane(), "成绩明细")
        self._tabs.addTab(self._build_review_pane(), "总评与侧写")
        self._tabs.addTab(self._build_chat_pane(), "AI 辅导")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        body.addWidget(self._tabs, 1)

        box.addWidget(self._body, 1)
        self._body.hide()
        return pane

    def _build_info_card(self) -> QFrame:
        card = make_card()
        row = QHBoxLayout(card)
        row.setContentsMargins(20, 14, 20, 14)
        row.setSpacing(14)

        self._info_avatar = QLabel("—")
        self._info_avatar.setObjectName("H2")
        self._info_avatar.setFixedWidth(40)
        row.addWidget(self._info_avatar)

        column = QVBoxLayout()
        column.setSpacing(2)
        self._info_name = QLabel("（还没选学生）")
        self._info_name.setObjectName("H2")
        column.addWidget(self._info_name)
        self._info_meta = QLabel("")
        self._info_meta.setObjectName("Muted")
        self._info_meta.setWordWrap(True)
        self._info_meta.setMinimumWidth(260)
        column.addWidget(self._info_meta)
        row.addLayout(column, 1)

        self._info_moral = QLabel("德育总分：未录入")
        self._info_moral.setObjectName("Muted")
        self._info_moral.setWordWrap(True)
        self._info_moral.setMinimumWidth(150)
        row.addWidget(self._info_moral)
        return card

    def _build_timeline_card(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 12, 20, 12)
        box.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("学期时间轴")
        title.setObjectName("H3")
        head.addWidget(title)
        note = QLabel("点一个学期看那一学期的学业侧写；均分是算术平均。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setMinimumWidth(240)
        head.addWidget(note, 1)
        box.addLayout(head)

        self._timeline_scroll = QScrollArea()
        self._timeline_scroll.setObjectName("TimelineScroll")
        self._timeline_scroll.setWidgetResizable(True)
        self._timeline_scroll.setFrameShape(QFrame.NoFrame)
        self._timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._timeline_scroll.setFixedHeight(96)
        self._timeline_box = QWidget()
        self._timeline_layout = QHBoxLayout(self._timeline_box)
        self._timeline_layout.setContentsMargins(0, 0, 0, 0)
        self._timeline_layout.setSpacing(8)
        self._timeline_layout.addStretch(1)
        self._timeline_scroll.setWidget(self._timeline_box)
        box.addWidget(self._timeline_scroll)
        return card

    def _timeline_data(self) -> dict[str, list[dict[str, Any]]]:
        if self._store is None or not self._username:
            return {}
        sid = self._bare_current_sid()
        if not sid:
            return {}
        key = str((self._payload or {}).get("key") or student_key(sid, ""))
        try:
            return student_semester_rows(self._store, self._username, sid, key)
        except Exception:  # noqa: BLE001 - 读不到就画一条空时间轴
            logger.exception("读取学期时间轴失败")
            return {}

    def _timeline_terms(self) -> list[str]:
        return [term for term, rows in self._timeline_data().items() if rows]

    def _render_timeline(self) -> None:
        layout = self._timeline_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        data = self._timeline_data()
        terms = [term for term, rows in data.items() if rows]
        if not terms:
            hint = QLabel("暂无学期成绩记录")
            hint.setObjectName("Muted")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            layout.addStretch(1)
            return
        previous_mean: float | None = None
        for term in terms:
            mean = rows_arithmetic_mean(data.get(term, []))
            delta = None
            if mean is not None and previous_mean is not None:
                delta = mean - previous_mean
            node = TermNode(term, mean, delta, self._timeline_box)
            node.set_selected(term == self._current_term)
            node.clicked.connect(
                lambda clicked_term: self.open_semester(semester=clicked_term)
            )
            layout.addWidget(node)
            if mean is not None:
                previous_mean = mean
        layout.addStretch(1)

    def _render_info_card(self) -> None:
        payload = self._payload or {}
        name = str(payload.get("student") or "").strip() or "（未命名）"
        sid = self._bare_current_sid()
        self._info_name.setText(f"{name}（{sid}）" if sid else name)
        self._info_avatar.setText(name[:1] or "—")
        class_name = self._class_of(sid) if sid else ""
        meta = f"学号：{sid or '未标注'}"
        meta += f"　·　班级：{class_name or '未标注'}"
        self._info_meta.setText(meta)
        key = str(payload.get("key") or "")
        moral = self._student_moral(key) if key else None
        if moral is None:
            self._info_moral.setText("德育总分：未录入")
        else:
            self._info_moral.setText(f"德育总分：{moral_text(moral)}")

    def _render(self) -> None:
        """重画详情页：空态 → 信息卡 + 时间轴 + 三个标签页。"""
        if not self._payload:
            self._empty.show()
            self._body.hide()
            return
        self._empty.hide()
        self._body.show()
        self._render_info_card()
        self._render_timeline()
        self._render_detail()

    def _build_review_pane(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        box.addWidget(self._build_portrait())
        box.addWidget(self._build_side_text())
        box.addWidget(self._build_intention())
        box.addWidget(self._build_outline())
        box.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    # -- 学生 CRUD --------------------------------------------------------
    def _refresh_students(self) -> None:
        if self._book is not None:
            try:
                self._book.refresh()
            except Exception:  # noqa: BLE001 - 刷新失败只打日志
                logger.exception("刷新学生主档失败")
        self._fill_list(self._search_text)

    def _audit_student(
        self, operation: str, *, student_id: str, detail: str
    ) -> None:
        record_student_audit(
            self._store,
            self._username,
            operation,
            student_id=_bare_sid(student_id),
            detail=detail,
        )

    def _open_new_student(self) -> None:
        if self._book is None:
            QMessageBox.information(self, "新建学生", "学生主档还没准备好，请稍后再试。")
            return
        dialog = StudentDialog(self, title="新建学生")
        if dialog.exec() != QDialog.Accepted:
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
        except Exception:  # noqa: BLE001 - 写不进去也要说人话
            logger.exception("新建学生失败")
            QMessageBox.critical(self, "新建学生", "这名学生没有存进去，请稍后再试。")
            return
        self._audit_student(
            "student_add", student_id=sid, detail=f"新建学生 {name}（{sid}）"
        )
        self._no_data = False
        self._refresh_students()
        self._select_student_by_sid(sid)

    def _open_edit_student(self) -> None:
        entry = self._current_entry()
        if entry is None or not entry["sid"] or self._book is None:
            return
        old_sid = entry["sid"]
        record = None
        try:
            record = self._book.get(old_sid)
        except Exception:  # noqa: BLE001
            logger.exception("读取学生主档失败")
        dialog = StudentDialog(self, record=record, title="编辑学生")
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        new_sid = _bare_sid(values.get("student_id", ""))
        name = values.get("name", "").strip()
        if not new_sid:
            QMessageBox.warning(self, "信息不完整", "学号是必填的。")
            return
        if not name:
            QMessageBox.warning(self, "信息不完整", "姓名是必填的。")
            return
        if new_sid != old_sid:
            choice = self._confirm_sid_change(old_sid, new_sid)
            if choice is None:
                return
            if choice == "info":
                QMessageBox.information(
                    self,
                    "仅修改信息",
                    f"学生信息已经改成 {new_sid}；历史成绩仍挂在旧学号下（{old_sid}），"
                    "需要时请手动迁移。",
                )
            else:
                try:
                    report = migrate_student_id_for(
                        old_sid, new_sid, store=self._store, username=self._username
                    )
                except Exception:  # noqa: BLE001 - 迁移失败只警告，学生信息照样能改
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
        try:
            self._book.update(
                old_sid,
                student_id=new_sid,
                name=name,
                class_name=values.get("class_name", ""),
                note=values.get("note", ""),
            )
        except Exception:  # noqa: BLE001
            logger.exception("编辑学生失败")
            QMessageBox.critical(self, "编辑学生", "这名学生的信息没有存进去，请稍后再试。")
            return
        self._audit_student(
            "student_edit", student_id=new_sid, detail=f"编辑学生 {name}（{new_sid}）"
        )
        self._refresh_students()

    def _migrate_grades_fallback(self, old_sid: str, new_sid: str, report) -> None:
        """补搬成绩历史（core 的 migrate_student_id 用键形式调用 rekey 时不匹配）。

        core.database.migrate_student_id 内部把学号归一成 sid:<学号> 之后
        交给 GradeHistoryDB.rekey_student()，而 rekey_student 是拿**裸学号**
        逐行比较的 —— 结果一行也搬不过去（report.grades 为 0）。
        这里用公开 API 再搬一次：裸学号对裸学号；已经搬过的再调一次是 0 条，幂等。
        """
        if report is None or int(getattr(report, "grades", 0) or 0) > 0:
            return
        db = self._history()
        if db is None:
            return
        try:
            moved, dropped = db.rekey_student(old_sid, new_sid)
        except Exception:  # noqa: BLE001 - 补搬失败只警告，主流程继续
            logger.warning("补搬成绩历史失败", exc_info=True)
            return
        if not (moved or dropped):
            return
        try:
            report.grades = int(getattr(report, "grades", 0) or 0) + int(moved)
            report.grade_dropped = (
                int(getattr(report, "grade_dropped", 0) or 0) + int(dropped)
            )
        except Exception:  # noqa: BLE001 - 报告是 dataclass，字段动了只影响文案
            logger.debug("回填迁移报告失败", exc_info=True)

    def _confirm_sid_change(self, old_sid: str, new_sid: str) -> str | None:
        """改学号前的二次确认（任务书第 5 节）。

        返回 ``"migrate"``（把历史数据一起迁到新学号）、``"info"``（只改主档，
        历史仍挂在旧学号下）或 ``None``（取消，什么都不做）。
        两个选项**都**是用户按下的按钮 —— 不许默认替用户选。
        """
        if new_sid == old_sid:
            return None
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("修改学号")
        box.setText(
            f"学号要从 {old_sid} 改成 {new_sid}。\n"
            "要不要把历史数据（成绩、侧写、谈话记录、德育分）一起迁移到新学号？"
        )
        migrate_btn = box.addButton("迁移历史数据", QMessageBox.AcceptRole)
        info_btn = box.addButton("仅修改信息，不迁移历史", QMessageBox.ActionRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is migrate_btn:
            return "migrate"
        if clicked is info_btn:
            return "info"
        return None

    def _delete_student(self) -> None:
        entry = self._current_entry()
        if entry is None or not entry["sid"] or self._book is None:
            return
        sid = entry["sid"]
        name = entry["name"]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("删除学生")
        box.setText(
            f"要删除 {name}（{sid}）吗？仅归档只是从列表隐藏，数据全部保留；"
            "彻底删除会清掉成绩、侧写、谈话记录与德育分。"
        )
        hard_btn = box.addButton(
            "彻底删除（成绩、侧写、谈话记录一起删）", QMessageBox.DestructiveRole
        )
        archive_btn = box.addButton("归档隐藏", QMessageBox.ActionRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is hard_btn:
            answer = QMessageBox.question(
                self,
                "确认彻底删除",
                f"确定彻底删除 {name}（{sid}）吗？成绩、侧写、谈话记录与德育分"
                "会一起删除，无法恢复。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._delete_student_hard(sid, name)
        elif clicked is archive_btn:
            self._delete_student_archive(sid, name)

    def _delete_student_hard(self, sid: str, name: str) -> None:
        self._delete_hard_data(sid)
        try:
            self._book.remove(sid)
        except Exception:  # noqa: BLE001
            logger.exception("删除学生主档失败")
            QMessageBox.critical(self, "删除学生", "这名学生没有删掉，请稍后再试。")
            return
        self._audit_student(
            "student_delete", student_id=sid, detail=f"彻底删除学生 {name}（{sid}）"
        )
        self._clear_selection()
        self._refresh_students()

    def _delete_student_archive(self, sid: str, name: str) -> None:
        try:
            self._book.archive(sid)
        except Exception:  # noqa: BLE001
            logger.exception("归档学生失败")
            QMessageBox.critical(self, "归档学生", "这名学生没有归档成功，请稍后再试。")
            return
        self._audit_student(
            "student_archive", student_id=sid, detail=f"归档学生 {name}（{sid}）"
        )
        self._clear_selection()
        self._refresh_students()

    def _clear_selection(self) -> None:
        self._payload = None
        self._selected_key = ""
        self._selected_sid = ""
        self._current_term = ""
        self._render()

    def _delete_hard_data(self, sid: str) -> None:
        """彻底删除前先清关联数据：成绩历史、侧写（含学期侧写）、聊天文件。"""
        db = self._history()
        if db is not None:
            try:
                terms = sorted(
                    {
                        str(row.get("semester") or "")
                        for row in db.rows()
                        if same_student_id(row.get("student_id"), sid)
                    }
                    - {""}
                )
                for term in terms:
                    db.delete_semester(sid, term)
            except Exception:  # noqa: BLE001 - 清理失败不该拦住删除主档
                logger.exception("删除成绩历史失败")
        profile_store = self._side_store()
        if profile_store is not None:
            try:
                remove_side = getattr(profile_store, "remove", None)
                if callable(remove_side):
                    remove_side(sid)
                else:
                    logger.warning("侧写库没有 remove，退回清空侧写正文")
                    profile_store.clear_side_text(sid)
            except Exception:  # noqa: BLE001
                logger.exception("删除侧写失败")
        try:
            delete_all_semester_sides(self._store, self._username, sid)
        except Exception:  # noqa: BLE001
            logger.exception("删除学期侧写失败")
        # 档案快照（archive.json.enc）里这一份也一起摘掉，否则同一个学号
        # 重新导入时会把旧学期整份带回来。
        try:
            archive = load_archive(self._store, self._username)
            students = getattr(archive, "students", None)
            key = student_key(sid, "")
            if isinstance(students, dict) and key in students:
                students.pop(key, None)
                store_archive(self._store, self._username, archive)
        except Exception:  # noqa: BLE001 - 快照清不掉不该拦住删除主档
            logger.exception("删除档案快照失败")
        try:
            from core.user_store import delete_user_json

            chat_name = (
                f"{CHAT_HISTORY_DIR}{CHAT_HISTORY_PREFIX}"
                f"{safe_student_file(student_key(sid, ''))}{CHAT_HISTORY_SUFFIX}"
            )
            delete_user_json(self._store, self._username, chat_name)
        except Exception:  # noqa: BLE001
            logger.exception("删除聊天记录失败")

    # -- 关页 -------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """关页：停掉还在跑的线程，放开引擎占的内存（1~3 GB）。"""
        self._stop_chat_worker()
        if self._side_worker is not None:
            cancel = getattr(self._side_worker, "cancel", None)
            if callable(cancel):
                cancel()
            self._side_worker = None
        if self._semester_page is not None:
            try:
                self._semester_page.stop()
            except Exception:  # noqa: BLE001
                logger.exception("停止学期侧写页失败")
        engine = self._chat_engine
        self._chat_engine = None
        unload = getattr(engine, "unload", None)
        if callable(unload):
            try:
                unload()
            except Exception:  # noqa: BLE001 - 释放失败不该拦住关窗
                logger.exception("释放本地推理引擎失败")
        super().closeEvent(event)

    def _render_detail(self) -> None:
        payload = self._payload
        if payload is None:
            self._empty.show()
            self._body.hide()
            return
        self._empty.hide()
        self._body.show()

        name = payload["student"]
        key = payload.get("key") or student_key(payload.get("student_id", ""), name)
        student_id = (payload.get("student_id") or "").strip()
        results = payload.get("results") or {}
        archive = payload.get("archive")

        default = results.get(CalculationScope.REQUIRED_LIMITED)
        overall = results.get(CalculationScope.ALL)
        required = results.get(CalculationScope.REQUIRED_ONLY)

        head = f"{name}（{student_id}）" if student_id else name
        self._portrait_title.setText(f"{head} · 学业肖像")
        self._avg_label.setText(_fmt(default.weighted_average if default else None))
        # GPA / 排名**不显示**：仓库里没有真源（见模块 docstring）。
        # 这句话是给用户看的说明，免得他以为是加载失败。
        self._blank_label.setText("（GPA 与排名暂无数据来源）")

        # --- 三档口径的加权均分（数字全部来自 core.calculator）---
        scope_bits: list[str] = []
        for scope, result in ((CalculationScope.ALL, overall),
                              (CalculationScope.REQUIRED_LIMITED, default),
                              (CalculationScope.REQUIRED_ONLY, required)):
            if result is None:
                continue
            scope_bits.append(f"{scope_breakdown(scope)} {_fmt(result.weighted_average)}")
        self._scope_label.setText("　·　".join(scope_bits) if scope_bits else "暂无加权均分")

        # --- 趋势（core.archival.trend）---
        trend_info = trend(archive) if archive is not None else None
        delta = trend_info.get("delta") if trend_info else None
        if delta is None:
            self._trend_tag.setText("→ 单次导入，暂无趋势")
            self._trend_tag.setObjectName("TagGray")
            terms_text = "只导入了 1 个学期"
        elif delta > TREND_EPS:
            self._trend_tag.setText(f"↑ 进步 {delta:+.2f}")
            self._trend_tag.setObjectName("TagGreen")
            terms_text = _terms_text(trend_info)
        elif delta < -TREND_EPS:
            self._trend_tag.setText(f"↓ 下滑 {delta:+.2f}")
            self._trend_tag.setObjectName("TagOrange")
            terms_text = _terms_text(trend_info)
        else:
            self._trend_tag.setText(f"→ 平稳 {delta:+.2f}")
            self._trend_tag.setObjectName("TagGray")
            terms_text = _terms_text(trend_info)
        self._repolish(self._trend_tag)
        self._trend_sub.setText(terms_text)

        # --- 已定型短板 / 可抢救项（全部来自 core.archival）---
        # ★ 用**复用标签池**（`_fill_tags`）而不是 deleteLater 重建：
        #   本页任何 `deleteLater()` 都会撞上 AGENTS §4b 那个"透明度效果缓存
        #   旧 pixmap → 整页画成空白"的坑，代价是只有截图才看得见。
        part = partition(archive) if archive is not None else None
        locked = list(part.get("locked", [])) if part else []
        absent = list(part.get("absent", [])) if part else []

        locked_rows: list[tuple[str, str]] = []
        for entry in locked:
            credit = _credit_of(archive, entry.get("term", ""), entry.get("course", ""))
            text = f"· {entry.get('course', '?')}"
            if credit is not None:
                text += f"　{credit:g} 学分"
            text += f"　（{entry.get('term', '')} 已定型）"
            locked_rows.append((text, "TagGray"))
        if not locked:
            locked_rows.append(("· 没有已定型的挂科课", "TagGray"))
        for entry in absent:
            locked_rows.append((
                f"· {entry.get('course', '?')}　缺考（还能安排补考，"
                f"{entry.get('term', '')}）",
                "TagOrange",
            ))
        self._fill_tags(self._locked_box, locked_rows)

        rescues = self._compute_rescues(payload)
        rescue_rows: list[tuple[str, str]] = []
        if rescues:
            for course_name, credit, score, gain in rescues:
                rescue_rows.append((
                    f"· {course_name}　{credit:g} 学分　当前 {score:g} 分　→　"
                    f"提至 {RESCUE_TARGET:g} 可 {gain:+.3f}",
                    "TagOrange",
                ))
        else:
            rescue_rows.append(
                ("· 没有明显可抢救的课（分数都已达标或学分太小）", "TagGray")
            )
        self._fill_tags(self._rescue_box, rescue_rows)

        # --- 没学号的警告 ---
        if student_id:
            self._id_warn.hide()
        else:
            self._id_warn.setText(
                "注意：成绩表里这名学生没有学号，意愿只能按姓名存 —— "
                "如果班上有同名的人，两个人会共用同一份意愿。"
                "建议在成绩表里补上学号列再导入一次。"
            )
            self._id_warn.show()

        # --- 意愿回填（键是学号，不是姓名）---
        current = self._intentions.get(key, "")
        # ★ 必须先关掉互斥：互斥组里**已经选中的那个按钮 setChecked(False) 是无效的**，
        #   于是从"有志愿的学生"切到"还没选的学生"时，上一个学生的志愿会留在界面上
        #   ——状态栏写着"还没有选意愿"，按钮却亮着一个。踩过。
        self._intent_group.setExclusive(False)
        for option, radio in self._intent_buttons.items():
            radio.blockSignals(True)
            radio.setChecked(option == current)
            radio.blockSignals(False)
        self._intent_group.setExclusive(True)
        self._intent_status.setText(
            f"当前意愿：{current}" if current else "还没有选意愿。"
        )

        # --- 提纲 ---
        self._outline.setPlainText(self._build_outline_text(payload, rescues, current))

        # --- 标签页 1：成绩明细 ---
        self._render_grades(key, archive)
        self._render_moral(key)

        # --- 标签页 2：AI 侧写正文 ---
        self._render_side_text(key)

        # --- 标签页 3：AI 辅导（换人 = 换一份谈话记录）---
        self._render_chat(key, student_id, name)

    def _build_grades_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 10, 0, 0)
        box.setSpacing(8)

        self._grades_note = QLabel("")
        self._grades_note.setObjectName("Muted")
        self._grades_note.setWordWrap(True)
        box.addWidget(self._grades_note)

        self._grade_tree = QTreeWidget()
        self._grade_tree.setObjectName("GradeTree")
        self._grade_tree.setColumnCount(len(GRADE_TREE_COLUMNS))
        self._grade_tree.setHeaderLabels(list(GRADE_TREE_COLUMNS))
        self._grade_tree.setRootIsDecorated(True)
        self._grade_tree.setUniformRowHeights(True)
        self._grade_tree.setAlternatingRowColors(False)
        # ★ 从 v1.9.1 起**不再是 NoSelection**：第 4 列那个「删除」按钮要收到
        #   点击事件（选中态只是副作用，真正判命中用 :meth:`on_grade_action` 里的
        #   按钮矩形）。鼠标跟踪打开，悬停时按钮才变色。
        self._grade_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._grade_tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._grade_tree.setMouseTracking(True)
        # ★ 列宽写死 + Fixed：`ResizeToContents` 不把"展开出来的子行"算进来，
        #   点开一个学期之后列宽会跳（AGENTS §4c 第 2 条）。
        header = self._grade_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setStretchLastSection(False)
        self._grade_tree.setColumnWidth(1, 88)
        self._grade_tree.setColumnWidth(2, 88)
        # 「操作」列必须写死宽度（§4c 第 2 条：放进去的控件不参与 ResizeToContents）
        self._grade_tree.setColumnWidth(3, 88)
        self._grade_action_column = len(GRADE_TREE_COLUMNS) - 1
        self._grade_delegate = RowActionDelegate()
        self._grade_tree.setItemDelegateForColumn(
            self._grade_action_column, self._grade_delegate
        )
        self._grade_tree.itemClicked.connect(self._on_grade_clicked)
        box.addWidget(self._grade_tree, 1)

        box.addWidget(self._build_moral_card())
        return pane

    def _build_moral_card(self) -> QFrame:
        card = make_card()
        box = QHBoxLayout(card)
        box.setContentsMargins(16, 10, 16, 10)
        box.setSpacing(10)

        title = QLabel("德育分")
        title.setObjectName("H3")
        box.addWidget(title)

        self._moral_row = QLabel(moral_text(None))
        self._moral_row.setObjectName("Muted")
        box.addWidget(self._moral_row, 1)

        self._moral_delete = QPushButton(ACTION_TEXT)
        self._moral_delete.setObjectName("RowAction")
        self._moral_delete.setToolTip("删除这名学生的德育分记录")
        self._moral_delete.setMaximumWidth(84)
        self._moral_delete.setMinimumHeight(26)
        self._moral_delete.clicked.connect(self._delete_moral_row)
        box.addWidget(self._moral_delete)
        return card

    def _build_chat_pane(self) -> QWidget:
        """学生信息卡 + 只读谈话记录 + 一行输入框。

        ★ 容器上**绝不挂任何 QGraphicsEffect**（AGENTS §17：挂了整页可能画成
          空白，静态断言查不出来）。本函数里一个 ``setGraphicsEffect`` 都没有。
        """
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 10, 0, 0)
        box.setSpacing(10)

        box.addWidget(self._build_student_card())

        self._chat_status = QLabel("")
        self._chat_status.setObjectName("Muted")
        self._chat_status.setWordWrap(True)
        self._chat_status.setMinimumWidth(320)
        box.addWidget(self._chat_status)

        # 缺模型时的兜底入口（懒 import + try/except，见 _open_model_center）
        self._model_center_btn = QPushButton("打开模型中心")
        self._model_center_btn.setObjectName("Ghost")
        self._model_center_btn.setMinimumHeight(32)
        self._model_center_btn.setMaximumWidth(160)
        self._model_center_btn.clicked.connect(self._open_model_center)
        self._model_center_btn.hide()
        box.addWidget(self._model_center_btn)

        self._chat_view = QTextEdit()
        self._chat_view.setObjectName("Outline")
        self._chat_view.setReadOnly(True)
        self._chat_view.setPlaceholderText("这里显示这名学生的谈话记录。")
        box.addWidget(self._chat_view, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("说点什么…（回车发送）")
        self._chat_input.returnPressed.connect(self._on_chat_send_clicked)
        row.addWidget(self._chat_input, 1)
        self._chat_send = QPushButton("发送")
        self._chat_send.setObjectName("Primary")
        self._chat_send.setMinimumHeight(34)
        self._chat_send.setMaximumWidth(120)
        self._chat_send.clicked.connect(self._on_chat_send_clicked)
        row.addWidget(self._chat_send)
        box.addLayout(row)

        # ★ 建页面时还没选学生：输入区先关上、状态行给出怎么开始
        #   （不这么写的话，窗口一打开输入框就能打字，发出去才发现没人可谈）
        self._chat_status.setText("先在左边点一名学生，再开始谈话。")
        self._chat_input.setEnabled(False)
        self._chat_send.setEnabled(False)
        return pane

    def _build_student_card(self) -> QFrame:
        """当前学生的基本信息卡片（姓名 / 学号 / 班级）。"""
        card = make_card()
        box = QHBoxLayout(card)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(14)

        self._chat_avatar = QLabel("—")
        self._chat_avatar.setObjectName("H2")
        self._chat_avatar.setFixedWidth(40)
        box.addWidget(self._chat_avatar)

        info = QVBoxLayout()
        info.setSpacing(2)
        self._chat_name = QLabel("（还没选学生）")
        self._chat_name.setObjectName("H3")
        info.addWidget(self._chat_name)
        self._chat_meta = QLabel("")
        self._chat_meta.setObjectName("Muted")
        self._chat_meta.setWordWrap(True)
        self._chat_meta.setMinimumWidth(240)
        info.addWidget(self._chat_meta)
        box.addLayout(info, 1)

        note = QLabel("本机离线推理，谈话记录加密存在本机")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setFixedWidth(220)
        box.addWidget(note)
        return card

    def _build_portrait(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 18, 20, 18)
        box.setSpacing(10)

        top = QHBoxLayout()
        self._portrait_title = QLabel("总评")
        self._portrait_title.setObjectName("H2")
        top.addWidget(self._portrait_title)
        top.addSpacing(12)

        self._avg_label = QLabel("—")
        self._avg_label.setObjectName("H1")
        top.addWidget(self._avg_label)

        self._blank_label = QLabel("")
        self._blank_label.setObjectName("Muted")
        top.addWidget(self._blank_label)
        top.addStretch(1)
        box.addLayout(top)

        self._avg_sub = QLabel("")
        self._avg_sub.setObjectName("Muted")
        self._avg_sub.setWordWrap(True)
        self._avg_sub.setMinimumWidth(320)
        box.addWidget(self._avg_sub)

        # 三档口径的加权均分（数字来自 ``core.calculator``，本页一行都没算）
        self._scope_label = QLabel("")
        self._scope_label.setObjectName("Muted")
        self._scope_label.setWordWrap(True)
        self._scope_label.setMinimumWidth(320)
        box.addWidget(self._scope_label)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)
        self._trend_tag = QLabel("—")
        self._trend_tag.setObjectName("TagGray")
        sub_row.addWidget(self._trend_tag)
        self._trend_sub = QLabel("")
        self._trend_sub.setObjectName("Muted")
        self._trend_sub.setWordWrap(True)
        self._trend_sub.setMinimumWidth(320)
        sub_row.addWidget(self._trend_sub, 1)
        box.addLayout(sub_row)

        box.addWidget(self._section("已定型短板", "补考不改分（红线 9），不必再纠结"))
        self._locked_box = QVBoxLayout()
        self._locked_box.setSpacing(4)
        box.addLayout(self._locked_box)

        box.addWidget(self._section("可抢救项", "分不高、但提一提能拉均分"))
        self._rescue_box = QVBoxLayout()
        self._rescue_box.setSpacing(4)
        box.addLayout(self._rescue_box)
        return card

    def _build_side_text(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("AI 侧写")
        title.setObjectName("H2")
        head.addWidget(title)
        note = QLabel("把已有成绩与德育分拼成提示词，交给本机 AI 引擎生成")
        note.setObjectName("Muted")
        head.addWidget(note)
        head.addStretch(1)

        self._ai_btn = QPushButton("点击生成")
        self._ai_btn.setObjectName("Ghost")
        self._ai_btn.setMinimumHeight(36)
        self._ai_btn.setToolTip("生成的内容会存进本地加密文件")
        self._ai_btn.clicked.connect(self._generate_side_text)
        head.addWidget(self._ai_btn)

        # 生成完之后正文是**可编辑**的，改完点这里才写回加密库。
        self._side_save = QPushButton("保存")
        self._side_save.setObjectName("Primary")
        self._side_save.setMinimumHeight(36)
        self._side_save.setToolTip("把文本框里的内容存进本地加密文件")
        self._side_save.clicked.connect(self._on_side_save_clicked)
        head.addWidget(self._side_save)

        # 侧写记录的「删除」＝把这个学生的 side_text 清空（任务书 3 的第三类）。
        # ★ 纯文字，不许 emoji（AGENTS §4l）。
        self._side_delete = QPushButton(ACTION_TEXT)
        self._side_delete.setObjectName("RowAction")
        self._side_delete.setMinimumHeight(36)
        self._side_delete.setMaximumWidth(84)
        self._side_delete.setToolTip("清空这名学生的侧写记录")
        self._side_delete.clicked.connect(self._delete_side_text)
        head.addWidget(self._side_delete)
        box.addLayout(head)

        self._side_hint = QLabel(SIDE_TEXT_EMPTY_HINT)
        self._side_hint.setObjectName("Muted")
        self._side_hint.setWordWrap(True)
        self._side_hint.setMinimumWidth(320)
        box.addWidget(self._side_hint)

        # 生成期间的状态行（「正在生成侧写…」/ 失败的中文原因）。
        self._side_status = QLabel("")
        self._side_status.setObjectName("Muted")
        self._side_status.setWordWrap(True)
        self._side_status.setMinimumWidth(320)
        box.addWidget(self._side_status)

        self._side_text = QTextEdit()
        self._side_text.setObjectName("Outline")
        # ★ **可编辑**（任务书 5）：生成或读回来的正文都要能让辅导员改，
        #   改完「保存」才写回 ProfileStore。生成期间临时置只读。
        self._side_text.setReadOnly(False)
        self._side_text.setMinimumHeight(150)
        self._side_text.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._side_text.textChanged.connect(self._on_side_text_changed)
        box.addWidget(self._side_text)
        return card

    def _section(self, title: str, hint: str) -> QWidget:
        holder = QWidget()
        box = QHBoxLayout(holder)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("H3")
        box.addWidget(label)
        note = QLabel(hint)
        note.setObjectName("Muted")
        box.addWidget(note)
        box.addStretch(1)
        return holder

    def _build_intention(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(8)

        title = QLabel("发展意愿")
        title.setObjectName("H2")
        box.addWidget(title)

        note = QLabel("选完立即存进本地加密文件；下次打开这名学生还在。")
        note.setObjectName("Muted")
        box.addWidget(note)

        #: 没学号时的警告条（有学号时藏起来）
        self._id_warn = QLabel("")
        self._id_warn.setObjectName("Warn")
        self._id_warn.setWordWrap(True)
        box.addWidget(self._id_warn)
        self._id_warn.hide()

        row = QHBoxLayout()
        row.setSpacing(10)
        self._intent_group = QButtonGroup(self)
        self._intent_group.setExclusive(True)
        self._intent_buttons: dict[str, QRadioButton] = {}
        for option in INTENTION_OPTIONS:
            radio = QRadioButton(option)
            radio.toggled.connect(
                lambda checked, name=option: self._on_intention(name) if checked else None
            )
            self._intent_group.addButton(radio)
            self._intent_buttons[option] = radio
            row.addWidget(radio)
        row.addStretch(1)
        box.addLayout(row)

        self._intent_status = QLabel("")
        self._intent_status.setObjectName("Muted")
        self._intent_status.setWordWrap(True)
        box.addWidget(self._intent_status)
        return card

    def _build_outline(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("辅导提纲")
        title.setObjectName("H2")
        head.addWidget(title)
        # ★ 任务书 4.1：这条提示在 1268 宽的窗口里会被裁掉半截 —— 必须换行，
        # 并且给一个够宽的最小宽度，否则它会把整行撑开、切页时窗口横向跳动。
        self._outline_note = QLabel("本地规则拼出来的，不联网、不调 AI")
        self._outline_note.setObjectName("Muted")
        self._outline_note.setWordWrap(True)
        self._outline_note.setMinimumWidth(200)
        head.addWidget(self._outline_note, 1)

        self._copy_btn = QPushButton("一键复制")
        self._copy_btn.setObjectName("Ghost")
        self._copy_btn.setMinimumHeight(36)
        self._copy_btn.clicked.connect(self._do_copy)
        head.addWidget(self._copy_btn)
        box.addLayout(head)

        self._outline = QTextEdit()
        self._outline.setObjectName("Outline")
        self._outline.setReadOnly(True)
        self._outline.setMinimumHeight(210)
        self._outline.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        box.addWidget(self._outline)
        return card

    def _migrate_name_keys(self) -> None:
        """把老的"裸姓名"意愿迁到学号上（**只在指代唯一时才迁**）。

        上一版按姓名存意愿（``name:张三``）。现在有了学号，如果全班只有**一个**
        张三，那这份意愿指的一定是他 —— 迁过去，用户不用重选。
        有两个及以上同名时**不猜**，原样留在 ``name:`` 键上。
        """
        if not self._username or not self._roster:
            return
        by_name: dict[str, list[Any]] = {}
        for row in self._roster:
            by_name.setdefault(row.student, []).append(row)

        moved = 0
        for name, rows in by_name.items():
            if len(rows) != 1:
                continue
            row = rows[0]
            if not row.has_id:
                continue
            old_key = f"{KEY_PREFIX_NAME}{name}"
            if old_key not in self._intentions:
                continue
            value = self._intentions.pop(old_key)
            self._intentions[row.key] = value
            try:
                save_intention(self._store, self._username, old_key, "")
                save_intention(self._store, self._username, row.key, value)
            except (CryptoError, ValueError):
                continue
            moved += 1
        if moved:
            print(f"[ProfilePage] 已把 {moved} 条按姓名的意愿迁移到学号键上。")

    def _history(self) -> Any:
        """``GradeHistoryDB`` 懒建（库坏了 / 没登录时返回 ``None``）。"""
        if self._grade_db is None:
            try:
                self._grade_db = GradeHistoryDB(store=self._store,
                                                username=self._username)
            except Exception:  # noqa: BLE001 - 历史表读不了，归档那份照样显示
                logger.exception("初始化 GradeHistoryDB 失败")
                self._grade_db = False
        return self._grade_db or None

    def _grade_rows(self, key: str, archive: Any) -> list[dict[str, Any]]:
        """优先用归档里的课程明细；归档没有就查成绩历史表（**带学期**的行）。

        两个来源都空了就返回空列表 —— 界面上说明"还没有导入明细"，
        绝不编一个数字出来。
        """
        rows = grade_rows_from_records(archive)
        if rows:
            return rows
        db = self._history()
        if db is None or not key:
            return []
        try:
            return [dict(row) for row in db.rows() if same_student_id(row.get("student_id"), key)]
        except Exception:  # noqa: BLE001 - 明细读不出来不该拦住整页
            logger.exception("读取成绩历史失败（%s）", key)
            return []

    def _render_grades(self, key: str, archive: Any) -> None:
        if not hasattr(self, "_grade_tree"):
            return
        tree = self._grade_tree
        tree.clear()
        #: 行号 → 这一行对应成绩历史表里的哪一行（按学期分组**之前**的原始行号）
        self._grade_payloads = {}

        rows = self._grade_rows(key, archive)
        # 归档记录自己带 term；成绩历史表的行本来就逐行带 semester。
        # 只有"两边都没有"时才留空，界面上显示成「未标注学期」。
        default_term = term_of_archive(archive)
        for row in rows:
            row.setdefault("term", "")
            if not row["term"]:
                row["term"] = default_term
        grouped = group_grades_by_term(rows, terminal_terms={default_term} if default_term else None)

        for semester, courses in grouped:
            # 「同时删除学期记录」删掉的学期不再显示（本页内，换人时清空）
            if semester and semester in self._hidden_terms:
                continue
            head = QTreeWidgetItem([semester or "未标注学期", "", "", ""])
            font = head.font(0)
            font.setBold(True)
            head.setFont(0, font)
            head.setFirstColumnSpanned(True)
            tree.addTopLevelItem(head)
            for row in courses:
                item = QTreeWidgetItem([
                    row["course"],
                    format_score(row["score"]),
                    format_credit(row["credit"]),
                    "",                     # 「操作」列由委托画按钮
                ])
                item.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight
                                             | Qt.AlignmentFlag.AlignVCenter))
                item.setTextAlignment(2, int(Qt.AlignmentFlag.AlignRight
                                             | Qt.AlignmentFlag.AlignVCenter))
                self._grade_payloads[id(item)] = {
                    "term": str(semester or ""),
                    "row": row,
                }
                head.addChild(item)
        tree.expandAll()

        visible_terms = [term for term, _courses in grouped if term not in self._hidden_terms]
        if not visible_terms:
            note = "还没有这名学生的成绩明细。先在「智育分析」导入他的成绩表。"
            if grouped:
                note += "（本页显示的学期已被「同时删除学期记录」移除）"
        else:
            course_count = sum(
                len(courses) for term, courses in grouped if term not in self._hidden_terms
            )
            note = (f"{len(visible_terms)} 个学期，共 {course_count} 门课。"
                    "空白＝没选或没考（不是 0 分）。")
        if hasattr(self, "_grades_note"):
            self._grades_note.setText(note)

    def on_grade_action(self, item: Any) -> bool:
        """点「操作」列里那个「删除」：确认后删掉这一条成绩。

        Args:
            item: 被点的 ``QTreeWidgetItem``（学期分组头传进来直接忽略）。

        Returns:
            True 表示**真的删掉了一条**（取消 / 点空 / 没这一行都是 False）。
        """
        info = self._grade_payloads.get(id(item)) if item is not None else None
        if not info:
            return False
        row = info.get("row") or {}
        if not self._confirm_delete():
            return False

        term = str(info.get("term") or "")
        removed = self._remove_grade_row(row, term)
        if not removed:
            QMessageBox.information(self, "删除成绩", "这条成绩没有删掉，请稍后再试。")
            return False

        self._save_archive()
        # ★ 删的是这个学期最后一条 → 追加问一次"要不要连学期分组一起删掉"
        payload = self._payload or {}
        key = payload.get("key") or student_key(
            str(payload.get("student_id") or "").strip(),
            str(payload.get("student") or ""),
        )
        if term and not self._term_has_grades(key, term):
            answer = QMessageBox.question(
                self, "删除学期记录", DELETE_TERM_PROMPT_TEXT,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._hidden_terms.add(term)
                self._delete_history_term(term)
                # ★ 学期记录既然删了，这个学期的侧写也一起清掉
                #   （档案快照里的该学期条目已由 _remove_grade_row 删掉）。
                self._delete_semester_side_file(term)

        self._render()
        return True

    def _on_grade_clicked(self, item: Any, column: int) -> None:
        """树上的点击：**只有落在「删除」按钮那片矩形里**才算数。

        ★ 命中判定用 :meth:`RowActionDelegate.cell_rect` —— 与 ``paint`` 共用
          同一份几何，不写第二遍（写两遍就会漂移：画在这儿、点在那儿）。
        """
        if column != getattr(self, "_grade_action_column", -1) or item is None:
            return
        tree = self._grade_tree
        rect = tree.visualItemRect(item)
        if rect.isEmpty():
            return
        cell = QRect(rect)
        cell.setLeft(rect.left() + tree.columnViewportPosition(column))
        cell.setWidth(tree.columnWidth(column))
        area = self._grade_delegate.cell_rect(cell)
        position = tree.viewport().mapFromGlobal(tree.cursor().pos())
        if not area.contains(position):
            return
        self.on_grade_action(item)

    def grade_delete_button_rect(self, item: Any) -> QRect:
        """某个课程行「删除」按钮在视口里的矩形（测试按它模拟点击）。"""
        tree = self._grade_tree
        column = getattr(self, "_grade_action_column", -1)
        rect = tree.visualItemRect(item)
        cell = QRect(rect)
        cell.setLeft(rect.left() + tree.columnViewportPosition(column))
        cell.setWidth(tree.columnWidth(column))
        return self._grade_delegate.cell_rect(cell)

    def _confirm_delete(self) -> bool:
        """单条删除的确认框。**文案逐字照任务书**，默认按钮是「否」。"""
        answer = QMessageBox.question(
            self, "确认删除", DELETE_CONFIRM_TEXT,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _term_has_grades(self, key: str, term: str) -> bool:
        """这个学生这个学期**还有没有**成绩（两个数据源都看）。"""
        if not term:
            return False
        if self._history_rows(key, term):
            return True
        archive = (self._payload or {}).get("archive")
        student = student_archive_of(archive, key)
        records = getattr(student, "records", None) or ()
        return any(getattr(record, "term", "") == term for record in records)

    def _history_rows(self, key: str, term: str) -> list[dict[str, Any]]:
        """成绩历史表里这个学生这个学期的行（库不可用时返回空表）。"""
        payload = self._payload or {}
        student_id = str(payload.get("student_id") or "").strip()
        if not student_id or not term:
            return []
        history = self._history()
        if history is None:
            return []
        try:
            return list(history.semester_rows(student_id, term))
        except Exception:  # noqa: BLE001 - 读不出来就当没有
            logger.exception("读成绩历史失败（%s / %s）", student_id, term)
            return []

    def _remove_grade_row(self, row: dict[str, Any], term: str) -> bool:
        """把一个课程行从**归档**里删掉，并尽力同步成绩历史表。

        ★ 界面上的成绩来自归档；但**持久化**的那一份在成绩历史表里。本方法
          两条路都走：归档删掉（界面立刻变），历史表**有单条删除 API 才调**
          （``core/database.py`` 当前还没有 —— 见 ``_delete_history_row``）。
        """
        payload = self._payload or {}
        archive = payload.get("archive")
        key = payload.get("key") or student_key(
            str(payload.get("student_id") or "").strip(),
            str(payload.get("student") or ""),
        )
        course = str(row.get("course") or "")
        score = row.get("score")
        removed = False

        # ★ 两种形状都要认：主窗给的是**归档**（``Archive``，要按 key 取），
        #   而档案页/测试给的常常就是**这名学生自己的** ``StudentArchive``
        #   （症状是 ``'StudentArchive' object has no attribute 'get'``）。
        student = student_archive_of(archive, key)
        records = list(getattr(student, "records", None) or ())
        for record in records:
            if getattr(record, "term", "") != term:
                continue
            courses = list(getattr(record, "courses", None) or ())
            for index, course_item in enumerate(courses):
                if getattr(course_item, "name", "") != course:
                    continue
                # 同名的课可能不止一条（重复导入）→ 用分数再对一次，仍对不上就取第一条
                if score is not None and getattr(course_item, "score", None) != score:
                    continue
                courses.pop(index)
                record.courses = courses
                removed = True
                break
            if removed:
                if not courses:
                    # 这个学期的课全删光了：记录自己也一并去掉（学期分组随之消失）
                    student.records = [item for item in records if item is not record]
                break
        if removed:
            self._save_archive()
        if self._delete_history_row(payload, term, course, score):
            removed = True
        return removed

    def _delete_history_row(self, payload: dict[str, Any], term: str, course: str,
                            score: Any) -> bool:
        """从 ``grade_history`` 表删掉这一条（主键三元组）。

        ★ v1.9.1 拿到数据层写的单条删除 API 了（另一个代理的写范围）：
          :meth:`core.database.GradeHistoryDB.delete_record`
          ``(student_id, semester, course_name) -> int``（返回真删掉的行数）。
          本函数把它翻成布尔 —— 调用方只关心"删掉没有"。
        ★ 没有这个 API 时**不报错、也不静默**：返回 False，界面照样把归档那
          一份删掉（成绩明细的主源是归档）。
        """
        student_id = str(payload.get("student_id") or "").strip()
        history = self._history()
        remover = getattr(history, "delete_record", None) if history is not None else None
        if not student_id or not course or not callable(remover):
            return False
        try:
            return int(remover(student_id, term, course)) > 0
        except Exception:  # noqa: BLE001 - 删不掉不该把界面带崩
            logger.exception("从成绩历史表删除失败（%s / %s / %s）",
                             student_id, term, course)
            return False

    def _delete_history_term(self, term: str) -> int:
        """把当前学生这个学期的成绩历史**整学期**删掉（追加提示里点「是」时用）。

        Returns:
            真删掉的行数（``0`` = 表里本来就没有，或没有这个 API）。
        """
        student_id = str((self._payload or {}).get("student_id") or "").strip()
        history = self._history()
        remover = getattr(history, "delete_semester", None) if history is not None else None
        if not student_id or not term or not callable(remover):
            return 0
        try:
            return int(remover(student_id, term))
        except Exception:  # noqa: BLE001 - 删不掉不该把界面带崩
            logger.exception("从成绩历史表删除学期失败（%s / %s）", student_id, term)
            return 0

    def _delete_semester_side_file(self, term: str) -> bool:
        """删学期记录时，把该生这个学期的侧写也清掉（任务书 4.1 的追加要求）。

        学期侧写有两个落点（ProfileStore 的学期侧写表 + ``semester_side`` 文件），
        :func:`ui.pages.profile_shared.delete_semester_side` 已经两处一起清。
        删不掉只警告 —— 成绩已经删了，不能因为侧写文件让整条流程报错。
        """
        student_id = _bare_sid(str((self._payload or {}).get("student_id") or ""))
        if not student_id or not term:
            return False
        try:
            return bool(delete_semester_side(self._store, self._username, student_id, term))
        except Exception:  # noqa: BLE001 - 侧写删不掉不影响成绩删除
            logger.warning("删除学期侧写失败（%s / %s）", student_id, term, exc_info=True)
            return False

    def _save_archive(self) -> None:
        """把改过的归档写回（写不进去只记日志，界面照样刷新）。"""
        payload = self._payload or {}
        archive = payload.get("archive")
        if archive is None or not self._username:
            return
        try:
            store_archive(self._store, self._username, archive)
        except Exception:  # noqa: BLE001 - 存不下不该把界面带崩
            logger.exception("保存归档失败（删除之后）")

    def _render_moral(self, key: str) -> None:
        value = self._student_moral(key) if key else None
        if hasattr(self, "_moral_row"):
            self._moral_row.setText(
                f"当前：{moral_text(value)}" if value is not None
                else f"{NO_MORAL_TEXT}（没录 ≠ 0 分）"
            )
        if hasattr(self, "_moral_delete"):
            self._moral_delete.setEnabled(value is not None and bool(key))

    def _delete_moral_row(self) -> None:
        """删掉这名学生的德育分记录（确认框 → 写回德育表）。"""
        payload = self._payload or {}
        key = payload.get("key") or ""
        if not key:
            return
        if not self._confirm_delete():
            return
        if not self._delete_moral_values(key):
            QMessageBox.information(self, "删除德育分", "这条德育分没有删掉，请稍后再试。")
            return
        self._moral_cache = None            # 下次重新扫班级目录
        self._render_moral(key)

    def _delete_moral_values(self, key: str) -> bool:
        """把 ``key`` 从每个录过他的班级德育表里删掉。

        ★ 班级列表照 :meth:`_student_moral` 的办法扫目录（``list_classes`` 是
          唯一真源，不另存索引）。删法是把该键写成空串再存 —— 读的时候
          ``load_moral`` 会把空串当成"没录"（AGENTS §14：没录 ≠ 0 分）。
        """
        savers: list[Callable[[], None]] = []
        names: list[str] = []
        try:
            names = [row.class_name for row in list_classes(self._store, self._username)]
        except Exception:  # noqa: BLE001 - 扫不到就是没有班级录过德育分
            logger.exception("扫班级目录失败（删除德育分降级）")
            return False
        for class_name in names:
            try:
                table = load_moral(self._store, self._username, class_name)
            except Exception:  # noqa: BLE001
                logger.exception("读德育分失败（%s）", class_name)
                continue
            if not isinstance(table, dict) or key not in table:
                continue
            table = dict(table)
            table[key] = ""
            savers.append(
                (lambda name=class_name, data=table:
                 save_moral(self._store, self._username, name, data))
            )
        if not savers:
            return False
        for save in savers:
            try:
                save()
            except Exception:  # noqa: BLE001
                logger.exception("写德育分失败（删除）")
                return False
        return True

    def _side_store(self) -> Any:
        """``ProfileStore`` 懒建：库坏了 / 没登录不该让页面开不起来。"""
        if self._profile_store is None:
            try:
                self._profile_store = ProfileStore(store=self._store,
                                                   username=self._username)
            except Exception:  # noqa: BLE001 - 侧写存不了，页面照用
                logger.exception("初始化 ProfileStore 失败")
                self._profile_store = False
        return self._profile_store or None

    def _render_side_text(self, key: str) -> None:
        if not hasattr(self, "_side_text"):
            return
        text = read_side_text(self._side_store(), "", key)
        self._side_text_guard = True
        try:
            self._side_text.setPlainText(text)
        finally:
            self._side_text_guard = False
        self._side_key = key or ""
        if text:
            self._side_hint.setText("侧写正文存放在本地加密文件里，只在本机可见。")
            self._ai_btn.setText("重新生成")
        else:
            self._side_hint.setText(SIDE_TEXT_EMPTY_HINT)
            self._ai_btn.setText("点击生成")
        self._ai_btn.setEnabled(bool(key) and not self._side_busy)
        if hasattr(self, "_side_delete"):
            # 没有侧写记录时没什么可删的 → 按钮置灰（避免"点了没反应"）
            self._side_delete.setEnabled(bool(key) and bool(text) and not self._side_busy)
        if hasattr(self, "_side_save"):
            # 刚读回来的正文还没改过 → 「保存」先不亮（避免"假保存"）
            self._side_save.setEnabled(False)
        if hasattr(self, "_side_status") and not self._side_busy:
            self._side_status.setText("")

    def _student_grades(self, key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._grade_rows(key, payload.get("archive"))

    def _student_moral(self, key: str) -> Any:
        if self._moral_cache is None:
            names: list[str] = []
            try:
                names = [row.class_name for row in list_classes(self._store, self._username)]
            except Exception:  # noqa: BLE001 - 查不到就当没录
                logger.exception("扫班级目录失败（德育分查询降级）")
            self._moral_cache = names
        try:
            return moral_of(self._store, self._username, key, self._moral_cache)
        except Exception:  # noqa: BLE001
            logger.exception("读德育分失败（%s）", key)
            return None

    def _generate_side_text(self) -> None:
        """「点击生成」：拼提示词 → **本机引擎流式生成** → 正文进可编辑文本框。

        ★ 生成走 :class:`_SideWorker`（后台线程，AGENTS §5b / §21a：主线程里
          跑推理会把窗口冻住）。生成完**不自动落库** —— 正文先给辅导员改，
          点「保存」才写进 :class:`core.database.ProfileStore`（任务书 5）。
        ★ 引擎 / 模型缺失时**不静默失败**：状态行给中文原因，按钮恢复可用。
        """
        payload = self._payload
        if not payload:
            QMessageBox.information(self, "AI 侧写", "先在左边点一名学生。")
            return
        name = str(payload.get("student") or "")
        student_id = str(payload.get("student_id") or "").strip()
        key = payload.get("key") or student_key(student_id, name)
        if not key or self._side_busy:
            return

        prompt = build_side_text_prompt(
            name, student_id, key,
            self._student_grades(key, payload),
            self._student_moral(key),
        )
        try:
            engine = self._ensure_engine()
        except InferenceUnavailable as exc:
            self._set_side_busy(False, f"生成失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 兜底也要给人话
            logger.exception("初始化侧写引擎失败")
            self._set_side_busy(False, f"生成失败：{friendly_error(exc)}")
            return

        self._side_key = key
        self._set_side_busy(True, "正在生成侧写，请稍候…")
        worker = _SideWorker(engine, prompt)
        worker.chunk.connect(self._on_side_chunk)
        worker.failed.connect(self._on_side_failed)
        worker.done.connect(self._on_side_finished)
        self._side_worker = worker
        self._start_worker(worker)

    def _on_side_text_changed(self) -> None:
        """正文被改过 → 「保存」按钮亮起来（没改过就不亮，避免假保存）。"""
        if getattr(self, "_side_text_guard", False) or self._side_busy:
            return
        button = getattr(self, "_side_save", None)
        if button is None:
            return
        stored = read_side_text(self._side_store(), "", self._side_key)
        button.setEnabled(bool(self._side_key) and self._side_text.toPlainText() != stored)

    def _on_side_save_clicked(self) -> None:
        """「保存」：把文本框里的正文写回 ``ProfileStore``。"""
        payload = self._payload
        if not payload:
            return
        key = self._side_key or payload.get("key") or ""
        text = self._side_text.toPlainText()
        if self._save_side_text(key, text):
            self._set_side_busy(False, "已保存到本地加密文件。")

    def _delete_side_text(self) -> None:
        """「删除」：清空这名学生的侧写记录（确认框 → 写空串 → 刷新）。

        ★ 走的是 ``ProfileStore.save_side_text(key, "")`` —— 侧写表里不留
          "墓碑行"，读回来就是空串（:func:`read_side_text` 的既有语义）。
        """
        payload = self._payload or {}
        key = self._side_key or payload.get("key") or ""
        if not key:
            return
        if not self._confirm_delete():
            return
        if not self._save_side_text(key, ""):
            return
        self._set_side_busy(False, "侧写记录已删除。")

    def _set_side_busy(self, busy: bool, status: str = "") -> None:
        """生成期间：按钮置灰 + 状态行给中文提示（失败原因也从这里贴）。"""
        self._side_busy = bool(busy)
        has_key = bool(self._side_key or (self._payload or {}).get("key"))
        if hasattr(self, "_ai_btn"):
            self._ai_btn.setEnabled(has_key and not busy)
        if hasattr(self, "_side_text"):
            self._side_text.setReadOnly(busy)
        if hasattr(self, "_side_status"):
            self._side_status.setText(status)
            self._side_status.setObjectName(
                "Warn" if status.startswith("生成失败") else "Muted"
            )
            self._repolish(self._side_status)
        if hasattr(self, "_side_save"):
            if busy:
                self._side_save.setEnabled(False)
            else:
                # 生成结束：正文已经落在框里，但还没被改过 → 走一次同一套判据
                # （不这么做的话「保存」一直是灰的，用户会以为生成没成功）
                self._on_side_text_changed()
        if hasattr(self, "_side_delete"):
            self._side_delete.setEnabled(
                bool(has_key) and bool(self._side_text.toPlainText()) and not busy
            )

    def _on_side_chunk(self, piece: str) -> None:
        if not piece or not self._side_busy:
            return
        self._side_text.moveCursor(_CURSOR_END)
        self._side_text.insertPlainText(piece)
        self._side_text.moveCursor(_CURSOR_END)

    def _on_side_failed(self, message: str) -> None:
        self._side_error = message or "这次生成没有成功，可以再试一次。"

    def _on_side_finished(self) -> None:
        """生成结束：收文案、把正文交给用户编辑、恢复按钮。"""
        worker = self.sender()
        if isinstance(worker, QThread) and worker is not self._side_worker:
            worker.deleteLater()                 # 迟到的旧线程：什么都别动
            return
        self._side_worker = None
        if worker is not None:
            worker.deleteLater()
        error = self._side_error
        self._side_error = ""
        self._set_side_busy(False, "")
        if error:
            self._set_side_busy(False, f"生成失败：{error}")
            return
        self._side_text_guard = True
        try:
            self._on_side_text_changed()
        finally:
            self._side_text_guard = False
        self._side_status.setText(
            "已生成，正文可以修改；点「保存」写进本地加密文件。" if self._side_text.toPlainText()
            else "这次生成没有拿到内容，可以再试一次。"
        )

    @staticmethod
    def _chat_opener() -> Any:
        """兼容层的旧钩子：**v1.9.1 起侧写不再走对话框**（改成本机引擎直出）。

        留着它是为了不删调用方认得的接口（主窗 / 其它页面按名字引用过）；
        返回值永远是 ``None`` 之外的东西就得真能调 —— 这里直接给 ``None``。
        """
        return None

    def save_side_text(self, text: str) -> None:
        """把一段侧写正文存给**当前选中的学生**（也给谈话助手回调调用）。"""
        payload = self._payload
        if not payload:
            return
        name = str(payload.get("student") or "")
        key = payload.get("key") or student_key(
            str(payload.get("student_id") or "").strip(), name
        )
        self._save_side_text(key, text)

    def _save_side_text(self, key: str, text: str) -> bool:
        """写回 ``ProfileStore``。返回"真的存下去了没有"。"""
        store = self._side_store()
        if store is None or not key:
            QMessageBox.information(self, "AI 侧写", "侧写暂时存不进加密库（没登录？）")
            return False
        try:
            store.save_side_text(key, text)
        except Exception:  # noqa: BLE001 - 存不进也要让用户看到刚生成的正文
            logger.exception("写侧写正文失败（%s）", key)
            self._side_text_guard = True
            try:
                self._side_text.setPlainText(text)
            finally:
                self._side_text_guard = False
            QMessageBox.information(self, "AI 侧写", "侧写暂时存不进加密库。")
            return False
        self._render_side_text(key)
        return True

    def set_chat_engine(self, engine: Any) -> None:
        """注入一个引擎（**测试用**：给了它就绝不加载真模型）。"""
        self._chat_engine = engine

    def set_chat_models(self, models: list[Any] | None) -> None:
        """注入本地模型清单（测试用：``[]`` = 本机一个模型都没有）。

        ★ 顺带把"上一次挑中的那个"清掉：清单换了，之前那个选择不再作数
          （不清的话 :meth:`_ensure_chat_engine` 会拿着旧模型去建引擎，
          症状是注入了 ``[]`` 却还是能跑起来）。
        """
        self._chat_models_given = models is not None
        self._chat_models = list(models) if models is not None else []
        self._chat_model = None

    def _render_chat(self, key: str, student_id: str, name: str) -> None:
        """换人 = 换一份谈话记录 + 换一份学生数据。"""
        self._stop_chat_worker()
        self._chat_student_id = str(student_id or "").strip()
        if self._chat_key != key:
            self._chat_key = key
            self._chat_history = []
            self._chat_view.clear()
            self._load_chat_history()
        self._chat_system_prompt = self._chat_system_for(key, student_id, name)
        self._render_chat_history()
        self._refresh_chat_availability()
        if hasattr(self, "_chat_name"):
            head = f"{name}（{student_id}）" if student_id else (name or "（还没选学生）")
            self._chat_name.setText(head)
            self._chat_avatar.setText(_initial(name) or "—")
            self._chat_meta.setText(
                f"学号：{student_id or '（成绩表里没写学号）'}　"
                f"班级：{self._class_of(student_id)}"
            )
        if self._chat_system_prompt and hasattr(self, "_chat_system_label"):
            self._chat_system_label.setToolTip(self._chat_system_prompt)

    def _class_of(self, student_id: str) -> str:
        """这名学生的班级号（**真源三级，都没有就写"未标注"**，绝不编一个）。

        ① 成绩历史表里的 ``class_name`` —— ``GRADE_HISTORY_FIELDS`` 里**没有**
           这一列，所以正常路径上永远落空，留着是为了"将来真加了这列就能用"；
        ② 这份归档里这名学生自带的班级字段（``class_name`` / ``class_id``
           / ``class`` —— 智育分析页建档案时可能带上了它）；
        ③ **班级库**（真源）：``core.scholarship.list_classes`` 扫出来的
           ``class_<班级号>.json.enc``，名单里有这个学号就是那个班。

        ★ 三级都落空时返回「未标注」而**不是**空串：界面上要看得见"这里没有
          班级号"，而不是一个看起来像加载失败的空白（AGENTS §4c 的同一条道理）。
        """
        if student_id:
            history = self._history()
            if history is not None:
                try:
                    for row in history.rows():
                        if row.get("student_id") == student_id and row.get("class_name"):
                            return str(row["class_name"]).strip() or "未标注"
                except Exception:  # noqa: BLE001 - 班级只是提示信息，读不到就算了
                    logger.exception("读班级号失败（%s）", student_id)

        payload = self._payload or {}
        archive = payload.get("archive")
        for source in (archive, payload):
            for name in ("class_name", "class_id", "class"):
                value = self._class_field(source, name)
                if value:
                    return value

        found = self._class_from_library(student_id)
        return found or "未标注"

    def _class_from_library(self, student_id: str) -> str:
        """去**班级库**里找这个学号属于哪个班（真源：``class_*.json.enc``）。

        ★ 靠 ``students`` 里的 ``student_id`` 认人（不是姓名，AGENTS §9）；
          库读不出来就返回空串，绝不猜一个班号。
        """
        if not student_id or not self._username:
            return ""
        try:
            records = list(list_classes(self._store, self._username))
        except Exception:  # noqa: BLE001 - 班级库不可用不该拦住整页
            logger.exception("读班级库失败（%s）", self._username)
            return ""
        wanted = {student_id, f"{KEY_PREFIX_SID}{student_id}"}
        for record in records:
            class_name = str(getattr(record, "class_name", "") or "").strip()
            if not class_name:
                continue
            for student in getattr(record, "students", None) or ():
                if not isinstance(student, dict):
                    continue
                values = {
                    str(student.get("student_id") or "").strip(),
                    str(student.get("key") or "").strip(),
                }
                if values & wanted:
                    return class_name
        return ""

    @staticmethod
    def _class_field(source: Any, name: str) -> str:
        """从归档 / 快照 / 字典里抠一个班级字段（取不到就是空串）。"""
        if source is None:
            return ""
        if isinstance(source, dict):
            return str(source.get(name) or "").strip()
        return str(getattr(source, name, "") or "").strip()

    def _chat_system_for(self, key: str, student_id: str, name: str) -> str:
        """把这名学生的数据拼成 JSON，逐字塞进系统提示词。"""
        if not key:
            return ""
        payload = self._payload or {}
        grades = self._grade_rows(key, payload.get("archive"))
        moral = self._student_moral(key)
        side = read_side_text(self._side_store(), student_id, key)
        student_json = build_student_json(
            student_id, name, self._class_of(student_id), grades, moral, side,
        )
        self._chat_student_json = student_json
        return build_chat_system_prompt(student_json)

    def _chat_file(self, key: str) -> str:
        """这名学生的谈话记录文件名（``cache/chat_history_<学号>.json.enc``）。

        ★ 文件名用**学号**（AGENTS §9）：``key`` 是 ``sid:<hash>`` 形状的归档键，
          拿它当文件名人类认不出来；没有学号（比如只有姓名的老数据）才退回 key。
        """
        name = self._chat_student_id or key
        return (f"{CHAT_HISTORY_DIR}{CHAT_HISTORY_PREFIX}"
                f"{safe_student_file(name)}{CHAT_HISTORY_SUFFIX}")

    def _load_chat_history(self) -> None:
        """从加密库读回这名学生的谈话记录（读不到就是空表，不报错）。"""
        self._chat_history = []
        if not self._username or not self._chat_key:
            return
        try:
            from core.user_store import load_user_json

            payload = load_user_json(
                self._store, self._username, self._chat_file(self._chat_key), None
            )
        except Exception:  # noqa: BLE001 - 便利数据，读不出来就当没有
            logger.exception("读谈话记录失败（%s）", self._chat_key)
            return
        if not isinstance(payload, dict):
            return
        turns = payload.get("turns")
        if not isinstance(turns, list):
            return
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            role = (CHAT_ROLE_ASSISTANT if str(turn.get("role") or "") == CHAT_ROLE_ASSISTANT
                    else CHAT_ROLE_USER)
            self._chat_history.append({
                "role": role,
                "text": text,
                "time": str(turn.get("time") or ""),
            })

    def _save_chat_history(self) -> bool:
        """把谈话记录加密落盘。返回"真的写下去了没有"。"""
        if not self._username or not self._chat_key:
            return False
        try:
            from core.user_store import save_user_json

            save_user_json(
                self._store, self._username, self._chat_file(self._chat_key),
                {"schema": CHAT_HISTORY_SCHEMA, "turns": self._chat_history},
            )
        except Exception:  # noqa: BLE001 - 存不下不该打断谈话
            logger.exception("写谈话记录失败（%s）", self._chat_key)
            return False
        return True

    def _remember_chat(self, role: str, text: str) -> None:
        self._chat_history.append({
            "role": role,
            "text": text,
            "time": _clock_now(),
        })

    def _render_chat_history(self) -> None:
        """把记录画进只读框（**纯文本，不带任何 Markdown 记号**）。"""
        self._chat_view.clear()
        for turn in self._chat_history:
            speaker = "辅导员" if turn.get("role") == CHAT_ROLE_USER else "你"
            clock = turn.get("time") or ""
            stamp = f"（{clock}）" if clock else ""
            self._chat_view.append(f"{speaker}{stamp}：{turn.get('text', '')}")
        self._chat_view.moveCursor(self._chat_view.textCursor().MoveOperation.End)

    def _append_chat_line(self, text: str) -> None:
        cursor = self._chat_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if not self._chat_view.toPlainText().endswith("\n"):
            cursor.insertText("\n")
        cursor.insertText(text)
        self._chat_view.setTextCursor(cursor)
        self._chat_view.ensureCursorVisible()

    def _append_chat_chunk(self, piece: str) -> None:
        """流式：第一块写"你："并换行，后面的**原地续写**（每块都换行会竖成一列）。"""
        if not piece:
            return
        if not self._chat_open:
            self._chat_open = True
            self._append_chat_line(f"你：{piece}")
            return
        cursor = self._chat_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(piece)
        self._chat_view.setTextCursor(cursor)
        self._chat_view.ensureCursorVisible()

    def _refresh_chat_availability(self) -> None:
        """刷新状态行 + 输入区状态。**一次都不弹窗。**

        两级降级（任务书 2）：
        1. ``llama-cpp-python`` 没装 → 贴 :func:`engine_available` 给的中文原因；
        2. 引擎装好了但本机一个模型都没有 → 「请先在模型中心下载模型」。

        ★ 两级**分开判、分开说** —— 混成一句会让用户去下模型，而下完模型仍然
          跑不起来（因为引擎根本没装），那是把人往坑里带。
        """
        if not self._chat_key:
            self._chat_status.setText("先在左边点一名学生，再开始谈话。")
            self._set_chat_enabled(False)
            return

        # ★ 注入了引擎（``set_chat_engine`` / 构造参数）时**不再要求磁盘上有模型**
        #   —— 引擎本身就是"能跑"的证明；测试注入的假引擎也没有磁盘模型。
        injected = self._chat_engine is not None
        if injected:
            engine_ready, engine_reason = True, ""
        else:
            engine_ready, engine_reason = engine_available()
            self._chat_engine_reason = engine_reason

        if not injected and not self._chat_models_given:
            self._scan_chat_models()
        self._chat_model = self._first_usable_chat_model()

        if engine_ready and (injected or self._chat_model is not None):
            self._chat_status.setText("可以开始谈话（本机离线推理）。")
            self._set_chat_enabled(True)
            return

        reasons: list[str] = []
        if not engine_ready and engine_reason:
            reasons.append(engine_reason)
        if self._chat_model is None:
            reasons.append("请先在模型中心下载模型")
        self._chat_status.setText("　".join(reasons) or "本地推理暂时不可用。")
        self._set_chat_enabled(False)

    def _set_chat_enabled(self, enabled: bool) -> None:
        busy = bool(self._chat_worker is not None and self._chat_worker.isRunning())
        usable = bool(enabled) and not busy
        if hasattr(self, "_chat_input"):
            self._chat_input.setEnabled(usable)
        if hasattr(self, "_chat_send"):
            self._chat_send.setEnabled(usable)
        missing_model = self._chat_engine is None and self._chat_model is None
        if hasattr(self, "_model_center_btn"):
            self._model_center_btn.setVisible(bool(missing_model) and bool(self._chat_key))

    def _scan_chat_models(self) -> None:
        """扫磁盘上真的装好了的模型（拿不到清单就是空表，不报错）。"""
        try:
            self._chat_models = list(list_local_models())
        except Exception:  # noqa: BLE001 - 清单坏了不该让这一页打不开
            logger.exception("扫描本地模型失败")
            self._chat_models = []

    def _first_usable_chat_model(self) -> Any:
        """挑一个能用的本地模型（先用"轻松/勉强"档的，一个都没有就退回第一个）。"""
        choices = [model for model in self._chat_models if getattr(model, "usable", True)]
        if not choices and self._chat_models:
            choices = list(self._chat_models)
        return choices[0] if choices else None

    def _ensure_engine(self) -> Any:
        """「临时要一个引擎」的取法：**注入的优先**，没有才去磁盘找模型 / 建实例。

        ★ 侧写那条链用它，不直接用 :meth:`_ensure_chat_engine`：注入了引擎
          （测试的假引擎）时不该被"本机一个模型都没下载"卡住 —— 那会让
          「AI 侧写」在没有模型的机器上永远生不出正文。
        """
        if self._chat_engine is not None:
            return self._chat_engine
        return self._ensure_chat_engine()

    def _ensure_chat_engine(self) -> Any:
        """拿到这次要用的引擎（注入的优先，否则按选中的模型建一个常驻实例）。"""
        if self._chat_engine is not None:
            return self._chat_engine
        if self._chat_model is None:
            if not self._chat_models_given:
                self._scan_chat_models()
            self._chat_model = self._first_usable_chat_model()
            if self._chat_model is None:
                raise InferenceUnavailable("请先在模型中心下载模型")
        available, reason = engine_available()
        if not available:
            raise InferenceUnavailable(reason)
        path = getattr(self._chat_model, "path", None)
        try:
            module = _load_chat_worker()
            self._chat_engine = module.LocalModelEngine(path)
        except InferenceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 界面模块缺失等
            logger.exception("初始化本地推理引擎失败")
            raise InferenceUnavailable(friendly_error(exc)) from exc
        return self._chat_engine

    def _on_chat_send_clicked(self) -> None:
        text = self._chat_input.text().strip()
        if not text:
            return
        self._chat_input.clear()
        self.send_chat(text)

    def send_chat(self, text: str) -> bool:
        """把一段文字当辅导员的话发出去，并在**后台线程**里开始生成。"""
        text = (text or "").strip()
        if not text or not self._chat_key:
            return False
        if self._chat_worker is not None and self._chat_worker.isRunning():
            self._chat_status.setText("上一条还在回答，等它写完再来。")
            return False
        if not self._chat_input.isEnabled():
            return False
        try:
            engine = self._ensure_engine()
        except InferenceUnavailable as exc:
            self._chat_status.setText(str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            logger.exception("初始化本地推理引擎失败")
            self._chat_status.setText(friendly_error(exc))
            return False

        self._remember_chat(CHAT_ROLE_USER, text)
        self._append_chat_line(f"辅导员：{text}")
        prompt = build_chat_prompt(
            self._chat_system_prompt or "", self._chat_history
        )

        self._chat_answer = ""
        self._chat_open = False
        self._chat_error = ""
        self._chat_status.setText("正在生成，请稍候…")
        self._set_chat_enabled(False)

        worker = _PageChatWorker(engine, prompt, stop=CHAT_STOP_WORDS)
        worker.chunk.connect(self._on_chat_chunk)
        worker.failed.connect(self._on_chat_failed)
        worker.done.connect(self._on_chat_finished)
        self._chat_worker = worker
        self._start_worker(worker)
        return True

    @staticmethod
    def _start_worker(worker: Any) -> None:
        """启动后台线程。

        ★ 测试把它换掉就**同步跑**（不发线程、不加载模型、不碰事件循环）——
          进程里没有真线程，就不会有"跑完的 QThread 被回收"那类事故（§4k）。
        """
        worker.start()

    def _on_chat_chunk(self, piece: str) -> None:
        if self._chat_worker is None:
            return
        self._chat_answer += piece
        self._append_chat_chunk(piece)

    def _on_chat_failed(self, message: str) -> None:
        # ★ 中文人话由 ``_ChatWorker`` 翻好（它已经过了 ``friendly_error``）
        self._chat_error = message or "这次生成没有成功，可以再试一次。"

    def _on_chat_finished(self) -> None:
        """生成结束：记下回答、写盘、恢复输入区。"""
        sent = self.sender()
        if sent is not None and sent is not self._chat_worker:
            if isinstance(sent, QThread):
                sent.deleteLater()               # 上一轮迟到的收尾回调
            return
        current = self._chat_worker
        self._chat_worker = None
        if current is not None:
            # 线程自己攒的那一份最完整（排队的 chunk 信号可能还没派发到主线程）
            answer = str(getattr(current, "answer", "") or "").strip()
            if not answer:
                answer = self._chat_answer.strip()
            if answer:
                self._remember_chat(CHAT_ROLE_ASSISTANT, answer)
            current.deleteLater()

        self._chat_answer = ""
        self._save_chat_history()
        # ★ 先刷新可用性（把输入区按引擎/模型现状摆正），**再**贴失败原因 ——
        #   反过来的话"可以开始谈话"会把刚写上去的中文原因当场抹掉（症状：
        #   引擎明明报了错，状态行却是一片祥和）。
        self._refresh_chat_availability()
        if self._chat_error:
            self._chat_status.setText(self._chat_error)
            self._chat_error = ""
        if self._chat_input.isEnabled():
            self._chat_input.setFocus()

    def _stop_chat_worker(self, wait_ms: int = 3000) -> None:
        """停掉还在跑的生成。

        ★ 等不到**也不许销毁它** —— 一个还在运行的 ``QThread`` 被回收会让进程
          直接消失（AGENTS §4k 同族）：交给模块级集合留着，跑完自动移除。
        """
        worker = self._chat_worker
        self._chat_worker = None
        if worker is None:
            return
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            cancel()
        if not worker.isRunning() or worker.wait(wait_ms):
            worker.deleteLater()
            return
        _LIVE_WORKERS.add(worker)

        def _drop() -> None:
            _LIVE_WORKERS.discard(worker)

        worker.finished.connect(_drop)

    def _open_model_center(self) -> None:
        """打开模型中心。拿不到就用中文提示兜底，**绝不抛异常**。

        ★ 懒 import 用的是 ``import ui.model_center_dialog as module`` 再取属性
          （不是 ``from ... import open_model_center``）：这样测试能把
          ``module.open_model_center`` 换成桩，**不必真的开模态窗**
          —— 无头环境里一个真模态窗会把测试挂死（AGENTS §32 的教训）。
        """
        try:
            from ui import model_center_dialog as module
        except Exception:  # noqa: BLE001 - 包括 ImportError / 语法错 / 缺依赖
            logger.exception("加载模型中心失败")
            self._chat_status.setText("模型中心暂时打不开，请看日志。")
            return
        try:
            module.open_model_center(self)
        except Exception:  # noqa: BLE001 - 中心自己的异常不该把这一页带崩
            logger.exception("打开模型中心失败")
            self._chat_status.setText("模型中心暂时打不开，请看日志。")
            return
        # 回来之后重新扫一遍本地模型（用户可能刚下好一个）
        if not self._chat_models_given:
            self._scan_chat_models()
        self._chat_model = None
        self._refresh_chat_availability()

    def _compute_rescues(self, payload: dict[str, Any]) -> list[tuple[str, float, float, float]]:
        """算出"提至 85 能拉多少"。

        ★ 算术全部由 ``core.calculator.weighted_average`` 做 —— 本函数里
          **一行加减乘除都没有**。做法是把某一门课的分数换掉，再把整串课程
          原样喂回 core，看它给出的均分差多少。
        """
        archive = payload.get("archive")
        base = None
        if archive is not None:
            base = merge_records(archive)
        else:
            base = payload.get("results", {}).get(CalculationScope.REQUIRED_LIMITED)
        if base is None or base.weighted_average is None:
            return []

        courses = list(base.counted_courses)
        if not courses:
            return []

        out: list[tuple[str, float, float, float]] = []
        for course in courses:
            if course.score is None or course.score >= RESCUE_TARGET:
                continue
            hypothetical = [
                dataclasses.replace(item, score=RESCUE_TARGET) if item is course else item
                for item in courses
            ]
            new_average = weighted_average(hypothetical)
            if new_average is None:
                continue
            gain = new_average - base.weighted_average
            if gain <= 0:
                continue
            out.append((course.name, float(course.credit), float(course.score), gain))

        out.sort(key=lambda row: row[3], reverse=True)
        return out[:MAX_RESCUE]

    def _on_tab_changed(self, index: int) -> None:
        """切标签页：记住位置（没有别的副作用，切页不做任何重新计算）。"""
        self._active_tab = int(index)

    def _fill_tags(self, layout, rows: list[tuple[str, str]]) -> None:
        """把若干 ``(文字, 样式名)`` 填进一个 ``QVBoxLayout``，**控件复用**。

        ★ 为什么不是先 deleteLater 再 addWidget：本页任何"销毁再重建"的写法
          都可能撞上 AGENTS §4b —— 页面一旦挂过 ``QGraphicsOpacityEffect``，
          效果会缓存源 pixmap，内部增删控件后不会失效，**整页画成空白**，
          而且静态断言全绿、只有截图才看得见。控件池没有这个风险。
        """
        key = str(id(layout))
        pool = self._tag_pool.setdefault(key, [])
        # 不够就补
        while len(pool) < len(rows):
            label = QLabel("")
            label.setWordWrap(True)
            layout.addWidget(label)
            pool.append(label)
        # 多余的藏起来（**不删**：删了下次还得重建）
        for index, label in enumerate(pool):
            if index < len(rows):
                text, tag = rows[index]
                if label.objectName() != tag:
                    label.setObjectName(tag)
                    self._repolish(label)
                label.setText(text)
                label.show()
            else:
                label.hide()

    def _build_outline_text(
        self,
        payload: dict[str, Any],
        rescues: list[tuple[str, float, float, float]],
        intention: str = "",
    ) -> str:
        """三段式提纲（Spec 七）。

        **纯字符串拼接**：不联网、不调 AI、不做算术 —— 数字全部来自上面
        已经算好的结果。
        """
        name = payload["student"]
        results = payload.get("results") or {}
        archive = payload.get("archive")
        default = results.get(CalculationScope.REQUIRED_LIMITED)
        overall = results.get(CalculationScope.ALL)
        flag = payload.get("flag")

        avg = default.weighted_average if default is not None else None
        all_avg = overall.weighted_average if overall is not None else None
        trend_info = trend(archive) if archive is not None else None
        delta = trend_info.get("delta") if trend_info else None
        part = partition(archive) if archive is not None else None
        locked = list(part.get("locked", [])) if part else []
        absent = list(part.get("absent", [])) if part else []
        if not intention:
            key = payload.get("key") or student_key(payload.get("student_id", ""), name)
            intention = self._intentions.get(key, "")

        # --- 一、事实陈述：念数 ---
        fact: list[str] = [f"{name}　仅必修+限选均分 {_fmt(avg)}"]
        if all_avg is not None:
            fact.append(f"全口径 {_fmt(all_avg)}")
        if delta is not None:
            if delta > TREND_EPS:
                fact.append(f"趋势较首次导入上升 {delta:+.2f}")
            elif delta < -TREND_EPS:
                fact.append(f"趋势较首次导入下滑 {delta:+.2f}")
            else:
                fact.append(f"趋势平稳（{delta:+.2f}）")
        else:
            fact.append("目前只有一个学期的数据，趋势还判断不了")
        if flag is not None and flag.reasons:
            fact.append(f"预警：{'；'.join(flag.reasons)}")
        if locked:
            names = "、".join(entry.get("course", "?") for entry in locked)
            fact.append(f"已定型（补考不改分）的课：{names}")
        if absent:
            names = "、".join(entry.get("course", "?") for entry in absent)
            fact.append(f"缺考待安排：{names}")
        paragraph1 = "【事实】" + "。".join(fact) + "。"

        # --- 二、行动建议：抓大放小 ---
        act: list[str] = []
        if rescues:
            top = rescues[0]
            act.append(
                f"先抓 {top[0]}（{top[1]:g} 学分，当前 {top[2]:g} 分）："
                f"提到 {RESCUE_TARGET:g} 能把均分拉 {top[3]:+.3f}，"
                f"这是当前性价比最高的一门"
            )
            if len(rescues) > 1:
                rest = "、".join(
                    f"{row[0]}（{row[3]:+.3f}）" for row in rescues[1:4]
                )
                act.append(f"其次可以看：{rest}")
            act.append("学分大的课优先 —— 同样提 5 分，5 学分的课顶 1 学分的课五倍")
        else:
            act.append("眼下没有明显需要抢救的课，重心可以放在保持上")
        if locked:
            act.append("已经定型的那几门不必再纠结，补考不覆盖第一次成绩，谈它只会消耗情绪")
        if absent:
            act.append("缺考的课尽快安排补考，那是唯一还能改写的分数")
        remaining = part.get("remaining_terms") if part else None
        if remaining:
            act.append(f"按学期算后面还剩约 {remaining} 个学期可以冲")
        paragraph2 = "【行动】" + "。".join(act) + "。"

        # --- 三、对标意愿 ---
        if not intention:
            paragraph3 = (
                "【对标意愿】还没有登记发展意愿 —— 在上面「发展意愿」里选一个，"
                "这一段会自动换成对应的对标建议。"
            )
        else:
            paragraph3 = f"【对标意愿={intention}】" + _intention_advice(intention, avg, rescues)

        return "\n\n".join((paragraph1, paragraph2, paragraph3))

    def _on_intention(self, option: str) -> None:
        payload = self._payload
        if payload is None:
            return
        key = payload.get("key") or student_key(
            payload.get("student_id", ""), payload.get("student", "")
        )
        if not self._username:
            self._intent_status.setText("还没有登录用户，意愿没地方存。")
            return
        try:
            path = save_intention(self._store, self._username, key, option)
        except (CryptoError, ValueError) as exc:
            QMessageBox.warning(self, "意愿没能保存", str(exc))
            self._intent_status.setText(f"保存失败：{exc}")
            return

        self._intentions[key] = option
        self._intent_status.setText(
            f"已保存：{payload.get('student', '')} → {option}（{path.name}）"
        )
        self._render()

    def _do_copy(self) -> None:
        text = self._outline.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "还没有内容", "先选一名学生。")
            return
        QApplication.clipboard().setText(text)
        self._copy_btn.setText("已复制")
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1600, lambda: self._copy_btn.setText("一键复制"))

    @staticmethod
    def _tagged(text: str, tag: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(tag)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)



# ---------------------------------------------------------------------------
# 纯字符串的辅助函数
# ---------------------------------------------------------------------------


def _terms_text(trend_info: dict[str, Any]) -> str:
    count = int(trend_info.get("term_count") or 0)
    first = trend_info.get("first_term") or ""
    last = trend_info.get("last_term") or ""
    if count >= 2 and first and last and first != last:
        return f"{first} → {last}（{count} 个学期）"
    return f"{count} 个学期"


def _credit_of(archive: Any, term: str, course: str) -> float | None:
    """从档案里翻出某门课的学分（只为显示，不参与任何算术）。"""
    if archive is None:
        return None
    for record in getattr(archive, "records", []):
        if record.term != term:
            continue
        for snapshot in record.courses:
            if snapshot.name == course:
                return float(snapshot.credit)
    return None


def _intention_advice(
    intention: str, average: float | None, rescues: list[tuple[str, float, float, float]]
) -> str:
    """按意愿给一句对标建议。**纯文案，不含任何计算。**"""
    gap = ""
    if average is not None:
        gap = f"当前 {average:.2f} 分"

    if intention == "保研":
        body = (
            f"保研看的是核心课均分，通常要 80 以上。{gap}。"
            "先说结论：哪几门高学分的必修课还能动，就先动它们；"
            "保研名额是排位赛，不是及格赛，要盯的是相对位置而不是绝对分数。"
        )
        if rescues:
            body += f"眼下最值得动的是 {rescues[0][0]}。"
        body += "若本学期结束仍没有起色，建议早点做两手准备，别把宝全压在一条路上。"
    elif intention == "考研":
        body = (
            f"考研更看专业课和数学的底子，{gap}，但初试才是主战场。"
            "本科成绩的作用主要在复试材料和推免资格上 —— "
            "所以策略是：专业课别掉队，同时把时间往初试科目上让。"
        )
        body += "如果前面已经挂过科，务必确认补考和清挂记录都对得上，别卡在资格审查上。"
    elif intention == "就业":
        body = (
            f"就业最要紧的是顺利毕业，{gap}。"
            "先把毕业学分和必修课兜住 —— 挂科会直接影响毕业证和报到时间，"
            "这个代价远比均分低几分大。"
        )
        body += "余下的精力建议放到实习和项目上，那部分对求职的边际收益更高。"
    elif intention == "考公":
        body = (
            f"考公主要看笔试面试，{gap}，本科成绩通常只在政审和个别岗位的资格里出现。"
            "所以底线是：别挂科、别延毕，把学位证和毕业证稳稳拿到手。"
        )
        body += "行测申论是长期功夫，建议从大三就开始每天固定投入，别等到公告出来才动。"
    elif intention == "留学":
        body = (
            f"留学看的是均分和绩点，{gap}，而且每一门都算，"
            "包括选修 —— 这和国内保研只看核心课不一样。"
        )
        body += "所以哪怕是 1 学分的课也别放弃，能提就提；同时尽早准备语言考试，别卡在时间上。"
    else:
        body = (
            f"暂未确定也没关系，{gap}。"
            "这个阶段最划算的做法是把选择权留住："
            "均分别掉、别挂科、英语别断 —— 这三件事对哪条路都有用。"
        )
        body += "等方向清楚了再针对性发力，比现在盲目投入更有效率。"
    return body
