"""🎯 个人档案页 —— 左侧学生名单（可搜索） + 右侧两个标签页。

★ 文件名说明：Spec 五/六 把这一页定名为 ``profile_page.py``（"侧写"），
  所以**个人中心**那一页在 :mod:`ui.pages.account_page`，别走错。

布局（v1.9 第二阶段改成"左侧保留 + 右侧两个标签页"）::

    +------------------+---------------------------------------+
    | 学生名单          |  【张三（2023001）】                   |
    | 🔍 [搜索姓名/学号] |  [ 成绩明细 | 总评与侧写 ]  ← QTabWidget|
    | 张三（2023001）   |   按学期分组的课程树 / 肖像 + AI 侧写   |
    | 李四（2023002）   |  -------------------------------------|
    |                  |  发展意愿       |  辅导提纲             |
    +------------------+---------------------------------------+

标签页 1「成绩明细」：按**学期**分组的 课程 / 成绩 / 学分 树（可展开折叠）。
学期顺序**只认** :data:`core.archival.SEMESTER_ORDER`（经
``sorted_records`` / ``semester_rank``），本页不另抄一份表 —— 抄了就会漂移，
症状是"大二下排在大一上前头"。
★ **空 ≠ 0**（红线 2）：没选 / 没考的课，成绩列显示**空白**，绝不显示 0。

标签页 2「总评与侧写」：学业肖像（均分 / 三档口径加权 / 趋势 / 短板 / 可抢救项）
+ **AI 侧写正文**（``core.database.ProfileStore.side_text``，读不到就给
「点击生成」按钮）。

★ 关于"不重新算"
----------------

Spec 说「数据来源：student dict 里已有的字段，不重新算」。本页严格遵守：
均分、趋势、定型/可救分区全部来自 :mod:`core.archival` 的
``trend()`` / ``partition()`` / ``merge_records()``，三档加权来自
``core.calculator`` —— 那些是 core 里**已经有**的函数，不是本页新写的算术。

唯一的例外是「提至 85 可 +0.X」这一句：它天生是个假设运算，不算是算不出来的。
处理办法是**复用 core 自己的** ``core.calculator.weighted_average`` ——
把某一门课的分数换掉之后，把整串课程重新喂给它。这样"假设分"和"实际分"
出自同一份实现，**绝不会出现两个数字打架**。本页自己一行加减乘除都没写。

★ 界面上**没有** GPA / 排名：仓库里没有它们的真源函数（已全库 grep 确认），
  按铁律五"没有真源就不显示该字段"。有真源才加得回来。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.archival import SEMESTER_ORDER, merge_records, partition, semester_rank, trend
from core.calculator import CalculationScope, weighted_average
from core.crypto_store import CryptoError, CryptoStore
from core.database import GradeHistoryDB, ProfileStore
from core.scholarship import list_classes, load_moral
from ui.anim import make_card
from ui.intentions import (
    INTENTION_OPTIONS,
    KEY_PREFIX_NAME,
    load_intentions,
    save_intention,
    student_key,
)

#: 口径的中文说法**只从智育分析页拿**（那一页是 ``scope_breakdown`` 的真源），
#: 本页不另写一份翻译 —— 两份翻译迟早会有一份说错（上一版就把"选修"写成了
#: "归限选"，而且没人发现）。
from ui.pages.analysis_page import scope_breakdown

__all__ = [
    "GRADE_TREE_COLUMNS",
    "ITEM_HEIGHT",
    "LIST_WIDTH",
    "RESCUE_TARGET",
    "SIDE_TEXT_EMPTY_HINT",
    "StudentItemDelegate",
    "ProfilePage",
    "build_side_text_prompt",
    "format_credit",
    "format_score",
    "grade_rows_from_records",
    "group_grades_by_term",
    "moral_of",
    "read_side_text",
    "term_of_archive",
    # 下面这几个是给成长档案页复用的（同一个"学生名单"长相同一份实现，
    # 不复制第二遍 —— 复制出来的那份迟早会和这份长得不一样）
    "_ROLE_HAS_ID",
    "_ROLE_ID",
    "_ROLE_INDEX",
    "_ROLE_LEVEL",
    "_ROLE_NAME",
    "_ROLE_SEED",
    "_avatar_color",
]

#: "提一提"的目标分（Spec 六：附一句"提至85可+0.X"）
RESCUE_TARGET = 85.0

#: 趋势判定的死区：|Δ| 小于这个值就当"平稳"
TREND_EPS = 0.5

#: 可抢救项最多列几门（列表太长反而看不清重点）
MAX_RESCUE = 5

#: 左侧名单宽度
LIST_WIDTH = 236

#: 名单里一行的高度。要同时放得下头像圆(34) + 上下各 8px 呼吸，还要让
#: 两行字（姓名 13px / 学号 11px）不挤在一起。
ITEM_HEIGHT = 54

#: 头像圆的直径
_AVATAR = 34

#: 右侧第 1 个标签页的表头（三列：课程 / 成绩 / 学分）
GRADE_TREE_COLUMNS = ("课程", "成绩", "学分")

#: 没有侧写正文时显示的那句话（判据：`test_profile_merge.py` 认这一串）
SIDE_TEXT_EMPTY_HINT = "还没有侧写。点右上角的「AI 侧写」生成。"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 成绩明细 / 侧写：**纯逻辑**（零 Qt，pytest 直接测）
# ---------------------------------------------------------------------------


def format_score(score: Any) -> str:
    """成绩列的文字。

    ★ **空 ≠ 0**（红线 2）：没选 / 没考的课显示**空白**，绝不显示 ``0``。
    """
    if score is None or score == "":
        return ""
    try:
        return f"{float(score):g}"
    except (TypeError, ValueError):
        return str(score)


def format_credit(credit: Any) -> str:
    """学分列的文字。缺失（``None``）同样显示**空白**，不是 0。"""
    if credit is None or credit == "":
        return ""
    try:
        return f"{float(credit):g}"
    except (TypeError, ValueError):
        return str(credit)


def term_of_archive(archive: Any) -> str:
    """归档里**最近一个**学期的标签（用来给没有 term 的明细行标学期）。

    取 ``_ordered_records`` 的最后一个 —— 那是 order 最大的学期，
    也就是"最近一次导入的学期"。
    """
    records = _ordered_records(archive)
    return str(getattr(records[-1], "term", "") or "") if records else ""


def _ordered_records(archive: Any) -> list[Any]:
    """归档里这个学生的学期记录，按 ``core.archival`` 的顺序。

    ★ 学期顺序**必须**来自 :mod:`core.archival`（``sorted_records`` 用的是
      ``semester_rank`` / ``SEMESTER_ORDER``）—— 本页不另抄一份表，
      抄了就会漂移，症状是"大二下排在大一上前头"。
    """
    if archive is None:
        return []
    ordered = getattr(archive, "sorted_records", None)
    if callable(ordered):
        try:
            return list(ordered())
        except Exception:  # noqa: BLE001 - 坏档案不该拦住看明细
            pass
    records = list(getattr(archive, "records", ()) or ())
    return sorted(records, key=lambda rec: semester_rank(getattr(rec, "term", "")))


def grade_rows_from_records(archive: Any) -> list[dict[str, Any]]:
    """归档 → ``[{"term": ..., "course": ..., "score": ..., "credit": ...}]``。

    只搬运，不改口径：``score is None``（没选这门课）原样留着，
    由 :func:`format_score` 决定显示成空白。
    """
    rows: list[dict[str, Any]] = []
    for record in _ordered_records(archive):
        term = str(getattr(record, "term", "") or "")
        for course in getattr(record, "courses", ()) or ():
            rows.append({
                "term": term,
                "course": str(getattr(course, "name", "") or ""),
                "score": getattr(course, "score", None),
                "credit": getattr(course, "credit", None),
                "category": str(getattr(course, "category", "") or ""),
            })
    return rows


def group_grades_by_term(rows: list[dict[str, Any]],
                         terminal_terms: Any = None) -> list[tuple[str, list[dict[str, Any]]]]:
    """按学期分组，并把学期排成时间顺序。

    参数：
        rows: :func:`grade_rows_from_records` 那种行；也接受 ``core.database``
            的原始行（键是 ``course_name`` / ``score`` / ``credit``）
        terminal_terms: **已终结**的学期（转成 ``set`` 再传进来最省）。
            归档记录本身带的 ``term`` 就是终结学期；成绩历史表的行没有这个
            概念，所以只有调用方显式喂进来时才用得上。

    规则：
        * 终结学期**永远排在最前**（和 :func:`core.archival.merge_records`
          的既有行为一致，不再另立一套）；
        * 其余学期按 ``SEMESTER_ORDER`` 的时间顺序；认不出的排在最后；
        * 同一个学期里，有分数的排前面（高的在前），没分数的排最后、按课程名；
        * 课程名为空的（终结学期里的占位）单独归一堆，名字统一成
          「（历史课程名缺失）」。

    返回 ``[(学期, [行, ...]), ...]``，学期已去重且排好序。
    """
    terminal = {str(t or "").strip() for t in (terminal_terms or ())}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        term = str(row.get("term") or row.get("semester") or "").strip()
        course = str(row.get("course") or row.get("course_name") or "").strip()
        if not course:
            if term not in terminal:
                # 非终结学期里课程名不该为空 —— 空课程名无法按主键去重，
                # 也就无从显示，直接跳过（不编一个假名字出来）。
                continue
            course = "（历史课程名缺失）"
        buckets.setdefault(term, []).append({
            "term": term,
            "course": course,
            "score": row.get("score", None),
            "credit": row.get("credit", None),
            "category": str(row.get("category") or ""),
        })

    def rank(term: str) -> tuple[int, int, str]:
        known = term in SEMESTER_ORDER
        return (
            0 if term in terminal else 1,
            0 if known else 1,
            f"{semester_rank(term):03d}" if known else term,
        )

    ordered: list[tuple[str, list[dict[str, Any]]]] = []
    for term in sorted(buckets, key=rank):
        courses = buckets[term]
        courses.sort(key=lambda row: (
            row["score"] is None,
            -(row["score"] if isinstance(row["score"], (int, float)) else 0.0),
            row["course"],
        ))
        ordered.append((term, courses))
    return ordered


def moral_of(store: CryptoStore, username: str, key: str,
             class_names: Any = None) -> int | None:
    """在某一个班里找这名学生的德育分。

    ★ **没录 ≠ 0**（红线 2）：找不到就返回 ``None``，界面把它显示成空白。
      这也是为什么这个函数不返回 ``0`` —— 0 分和"没录"在奖学金那一页
      是两种完全不同的待遇（备注里必须写"德育分未录入，按 0 计"）。

    ``class_names`` 给了就只查那些班（省一次扫目录）；没给就 :func:`list_classes`。
    """
    if not username or not key:
        return None
    if class_names is None:
        try:
            class_names = [record.class_name for record in list_classes(store, username)]
        except (CryptoError, ValueError, OSError):
            return None
    for name in class_names:
        try:
            scores = load_moral(store, username, name)
        except (CryptoError, ValueError, OSError):
            continue
        if key in scores:
            return scores[key]
    return None


def build_side_text_prompt(student: str, student_id: str, key: str, grades: Any,
                           moral: Any = None) -> str:
    """把"已有的成绩与德育分"拼成一段提示词，交给 AI 谈话助手。

    ★ **只拼已有字段，不做任何算术**（铁律五）：平均分 / 加权 / 排名一概不
      在这里算，也一概不往里写 —— 提示词里出现一个本页算出来的数字，
      就等于绕开了"算术只在 core 里做"这条规矩。
    ★ 键一律用 ``sid:<学号>``；没有学号才退回 ``name:<姓名>``，
      并且**明说同名风险**（AGENTS §9）。
    """
    lines = [
        "请根据下面这名学生已有的成绩，写一段学业侧写（优势、短板、"
        "下学期可以怎么补），不要编造没有出现的数字。",
        "",
        f"学生：{student or '（未填）'}",
        f"学号：{student_id.strip() or '（成绩表里没有学号）'}",
    ]
    if key.startswith(KEY_PREFIX_NAME):
        lines.append("注意：这名学生没有学号，若班上有同名的人，不要把他人的成绩算进来。")
    lines.extend(["", "已有成绩（只列已导入的字段，空白＝没选或没考）："])
    grouped = group_grades_by_term(list(grades or ()))
    if grouped:
        for term, courses in grouped:
            lines.append(f"【{term or '未标注学期'}】")
            for row in courses:
                score = format_score(row["score"])
                credit = format_credit(row["credit"])
                detail = f"成绩 {score}" if score else "成绩（空白）"
                detail += f"　学分 {credit}" if credit else "　学分（空白）"
                lines.append(f"· {row['course']}　{detail}")
    else:
        lines.append("（还没有导入任何成绩明细）")
    if moral is None:
        lines.append("")
        lines.append("德育分：未录入")
    else:
        lines.extend(["", f"德育分：{moral}"])
    return "\n".join(lines)


def read_side_text(store: Any, student_id: str, key: str) -> str:
    """读侧写正文（``ProfileStore.side_text``）。

    读不到（没登录 / 库坏了 / 这个学生没存过）一律返回空串 ——
    侧写是便利数据，坏掉不该拦住看成绩。
    """
    sid = (key or student_id or "").strip()
    if not sid or store is None:
        return ""
    getter = getattr(store, "side_text", None)
    if not callable(getter):
        return ""
    try:
        return getter(sid) or ""
    except Exception:  # noqa: BLE001 - 读不出来就当没有
        return ""



# ---- 条目上挂的数据（delegate 绘制时读）-----------------------------------
_ROLE_INDEX = Qt.ItemDataRole.UserRole          #: 行号（原有，别改）
_ROLE_NAME = Qt.ItemDataRole.UserRole + 1       #: 姓名
_ROLE_ID = Qt.ItemDataRole.UserRole + 2         #: 学号（可能是空串）
_ROLE_HAS_ID = Qt.ItemDataRole.UserRole + 3     #: 有没有学号
_ROLE_LEVEL = Qt.ItemDataRole.UserRole + 4      #: "" / "danger" / "warning"
_ROLE_SEED = Qt.ItemDataRole.UserRole + 5       #: 头像配色的稳定种子

#: 头像底色。按学号取模选一个 —— 同一个人每次打开都是同一个颜色。
#:
#: ★ 绝不能用内置 ``hash()`` 当种子：Python 对 str 的 hash **每个进程都加盐**，
#:   同一个人这次是蓝的、下次开程序就变绿了，看着像换了个人。
_AVATAR_COLORS = (
    "#3B82F6", "#0EA5E9", "#8B5CF6", "#EC4899",
    "#F59E0B", "#10B981", "#6366F1", "#14B8A6",
)

_SEL_BG = "#EFF6FF"
_HOVER_BG = "#F8FAFC"
_ACCENT = "#3B82F6"
_TEXT = "#0F172A"
_MUTED = "#94A3B8"

_DANGER = "#EF4444"
_WARNING = "#F59E0B"


def _avatar_color(seed: str) -> str:
    """稳定的头像配色。**不用 hash()**（见 _AVATAR_COLORS 的注释）。"""
    if not seed:
        return "#94A3B8"
    return _AVATAR_COLORS[sum(ord(ch) for ch in seed) % len(_AVATAR_COLORS)]


def _initial(name: str) -> str:
    """头像里那个字：中文取姓氏，英文取首字母。"""
    text = (name or "").strip()
    return text[0].upper() if text else "?"


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
                sub_text = "⚠ 无学号 · 同名会串意愿"
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


class ProfilePage(QWidget):
    """侧写辅导页：左名单 + 右详情。"""

    def __init__(self, store: CryptoStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store if store is not None else CryptoStore()
        self._username = ""
        self._payload: dict[str, Any] | None = None
        self._intentions: dict[str, str] = {}
        #: 全班名单（:class:`ui.pages.analysis_page.TableRow`），由智育分析页推过来
        self._roster: list[Any] = []
        #: 行号 → 稳定标识（选中名单时用，避免每次线性扫名单）
        self._key_by_index: dict[int, str] = {}
        #: 由主窗口注入：``index -> payload``（名单里的行号 → 侧写原料）
        self._provider: Callable[[int], dict[str, Any] | None] | None = None
        #: ``core.database.ProfileStore``（懒建；建失败时记 ``False`` 不再重试）
        self._profile_store: Any = None
        #: ``core.database.GradeHistoryDB``（懒建，同上）
        self._grade_db: Any = None
        #: 德育分查过的班级名（一次登录只扫一遍目录）
        self._moral_cache: list[str] | None = None
        #: 标签池：``(布局, 名字) -> [QLabel, ...]``。**控件复用、不 deleteLater**
        self._tag_pool: dict[str, list[QLabel]] = {}
        #: 当前标签页下标（只用来记住位置，切页不做重新计算）
        self._active_tab = 0
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        """切账户时调用（登录、切换账号后）。会重新读这个人的意愿。

        ★ 顺带丢掉"上一个人的库句柄"：``ProfileStore`` 与 ``GradeHistoryDB``
          都是按用户名解析路径的，留着就会把 A 的侧写写到 B 的目录里
          （和 :meth:`AnalysisPage.set_user` 清 ``_archive`` 是同一条道理）。
        """
        self._username = username or ""
        self._intentions = (
            load_intentions(self._store, self._username) if self._username else {}
        )
        self._profile_store = None
        self._grade_db = None
        self._moral_cache = None
        if self._payload is not None:
            self._render()

    def set_payload_provider(self, provider: Callable[[int], dict[str, Any] | None]) -> None:
        """注入"行号 → 侧写原料"的取值函数（智育分析页提供，不复制数据）。"""
        self._provider = provider

    def set_roster(self, rows: list[Any]) -> None:
        """接住智育分析页导入后推过来的全班名单。

        Args:
            rows: :class:`ui.pages.analysis_page.TableRow` 列表。
        """
        self._roster = list(rows)
        self._migrate_name_keys()
        self._fill_list()
        if self._payload is not None:
            self._select_key(self._payload.get("key", ""))

    def show_student(self, payload: dict[str, Any]) -> None:
        """接住智育分析页传来的学生包，并在名单里把他选中。"""
        self._payload = payload
        self._select_key(payload.get("key", ""))
        self._render()

    def focus_list(self) -> None:
        """侧栏点进来时调用：**不换人**，只是把焦点交给搜索框。

        ★ 这就是"禁止点击侧栏直接跳转个人侧写"：侧栏落到名单上，
          具体看谁由用户在名单里点。
        """
        self._search.setFocus()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        root.addWidget(self._build_list_pane())
        root.addWidget(self._build_detail_pane(), 1)

    # --- 左：名单 + 搜索 ---
    def _build_list_pane(self) -> QFrame:
        card = make_card()
        card.setFixedWidth(LIST_WIDTH)
        box = QVBoxLayout(card)
        # ★ 上下左右全 0：让列表的选中底色能**顶到卡片边缘**。
        #   原来留 14px 内边距，选中色块是一小块浮在中间的方块，很土。
        #   内边距改由各段自己带（头部 14、搜索 12、列表 0、页脚 12）。
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        # --- 头部：标题 + 人数 ---
        head = QWidget()
        head.setObjectName("ListHead")
        head_box = QHBoxLayout(head)
        head_box.setContentsMargins(14, 13, 12, 10)
        head_box.setSpacing(8)
        title = QLabel("学生名单")
        title.setObjectName("H2")
        head_box.addWidget(title)
        head_box.addStretch(1)
        self._count_pill = QLabel("0")
        self._count_pill.setObjectName("CountPill")
        self._count_pill.setToolTip("当前名单人数")
        head_box.addWidget(self._count_pill)
        box.addWidget(head)

        # --- 搜索 ---
        search_holder = QWidget()
        search_box = QVBoxLayout(search_holder)
        search_box.setContentsMargins(12, 0, 12, 10)
        search_box.setSpacing(0)
        self._search = QLineEdit()
        self._search.setObjectName("ListSearch")
        self._search.setPlaceholderText("搜索姓名或学号")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(32)
        self._search.textChanged.connect(self._on_search)
        search_box.addWidget(self._search)
        box.addWidget(search_holder)

        # --- 列表本体 ---
        self._list = QListWidget()
        self._list.setObjectName("StudentList")
        self._list.setAlternatingRowColors(False)
        self._list.setUniformItemSizes(True)
        self._list.setMouseTracking(True)          # hover 高亮要它才会来
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setItemDelegate(StudentItemDelegate(self._list))
        self._list.itemClicked.connect(self._on_item_clicked)
        box.addWidget(self._list, 1)

        # --- 页脚：人数 / 过滤说明 ---
        self._list_note = QLabel("")
        self._list_note.setObjectName("ListNote")
        self._list_note.setWordWrap(True)
        self._list_note.setContentsMargins(14, 8, 12, 10)
        box.addWidget(self._list_note)
        return card

    def _fill_list(self) -> None:
        """按名单重建列表。带学号的显示学号 —— 同名也能一眼分开。"""
        blocked = self._list.blockSignals(True)
        self._list.clear()
        self._key_by_index = {}
        for row in self._roster:
            item = QListWidgetItem(row.label)      # text 保留：搜索靠它
            item.setData(_ROLE_INDEX, row.index)
            item.setData(_ROLE_NAME, row.student)
            item.setData(_ROLE_ID, row.student_id)
            item.setData(_ROLE_HAS_ID, bool(row.has_id))
            # ★ 预警级别来自智育分析页**当前口径**现填的 flag（见 TableRow.flag）。
            #   这里不自己判、不缓存 —— 口径一变那边会重新推一份名单过来。
            item.setData(_ROLE_LEVEL, getattr(row.flag, "level", "") or "")
            item.setData(_ROLE_SEED, row.student_id or row.student)
            item.setSizeHint(QSize(0, ITEM_HEIGHT))
            item.setToolTip(
                f"学号 {row.student_id}" if row.has_id
                else "⚠ 表里没有学号，同名学生会共用同一份意愿"
            )
            self._list.addItem(item)
            self._key_by_index[row.index] = row.key
        self._list.blockSignals(blocked)
        self._search.clear()
        self._update_list_note()

    def _update_list_note(self) -> None:
        total = self._list.count()
        shown = sum(1 for i in range(total) if not self._list.item(i).isHidden())
        missing = sum(1 for row in self._roster if not row.has_id)
        self._count_pill.setText(str(total))
        flagged = sum(
            1
            for row in self._roster
            if (getattr(row.flag, "level", "") or "") == "danger"
        )
        if not total:
            self._list_note.setText("还没有名单。先在「📊 智育分析」导入成绩表。")
            return
        text = f"共 {total} 人" if shown == total else f"筛出 {shown} / {total} 人"
        if flagged:
            text += f"　·　红标 {flagged} 人"
        if missing:
            text += f"\n⚠ {missing} 人没有学号，同名会共用意愿"
        self._list_note.setText(text)

    @property
    def roster_size(self) -> int:
        return len(self._roster)

    @property
    def visible_count(self) -> int:
        """搜索过滤之后还看得见的条数（自检用）。"""
        return sum(1 for i in range(self._list.count()) if not self._list.item(i).isHidden())

    # --- 右：详情（两个标签页） ---
    def _build_detail_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        self._empty = QLabel(
            "👈  在左边点一名学生，这里显示他的成绩明细、学业肖像、"
            "发展意愿和辅导提纲。\n\n"
            "名单来自「📊 智育分析」导入的成绩表。还没导入的话，先去那一页导一份。"
        )
        self._empty.setObjectName("Placeholder")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        box.addWidget(self._empty, 1)

        self._body = QWidget()
        body_box = QVBoxLayout(self._body)
        body_box.setContentsMargins(0, 0, 0, 0)
        body_box.setSpacing(12)

        # ★ 标签页**不放进滚动区**：滚起来会把标签头一起滚走。
        #   滚动区在标签页**里面**（下面两个 pane 各自带一个）。
        self._tabs = QTabWidget()
        self._tabs.setObjectName("ProfileTabs")
        self._tabs.addTab(self._build_grades_pane(), "成绩明细")
        self._tabs.addTab(self._build_review_pane(), "总评与侧写")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        body_box.addWidget(self._tabs, 1)

        # 标签页下面：发展意愿（左）+ 辅导提纲（右）。
        # ★ 这两张卡**常驻不重建** —— 页面里绝不 deleteLater 重建控件，
        #   也就绝不会踩到"透明度效果缓存 pixmap → 整页变白"那个坑（AGENTS §4b）。
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        intention = self._build_intention()
        outline = self._build_outline()
        intention.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        bottom.addWidget(intention, 1)
        bottom.addWidget(outline, 1)
        body_box.addLayout(bottom)

        box.addWidget(self._body, 1)
        self._body.hide()
        return pane

    # --- 标签页 1：成绩明细 ---
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
        self._grade_tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self._grade_tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # ★ 列宽写死 + Fixed：`ResizeToContents` 不把"展开出来的子行"算进来，
        #   点开一个学期之后列宽会跳（AGENTS §4c 第 2 条）。
        header = self._grade_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setStretchLastSection(False)
        self._grade_tree.setColumnWidth(1, 88)
        self._grade_tree.setColumnWidth(2, 88)
        box.addWidget(self._grade_tree, 1)
        return pane

    # --- 标签页 2：总评与侧写 ---
    def _build_review_pane(self) -> QWidget:
        pane = QWidget()
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 10, 6, 0)
        box.setSpacing(14)

        box.addWidget(self._build_portrait())
        box.addWidget(self._build_side_text())
        box.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return pane

    # --- 第一层：学业肖像 ---
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

    # --- 第四层：AI 侧写（正文落 ProfileStore，不落明文） ---
    def _build_side_text(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("AI 侧写")
        title.setObjectName("H2")
        head.addWidget(title)
        note = QLabel("把已有成绩与德育分拼成提示词，交给 AI 谈话助手")
        note.setObjectName("Muted")
        head.addWidget(note)
        head.addStretch(1)

        self._ai_btn = QPushButton("点击生成")
        self._ai_btn.setObjectName("Ghost")
        self._ai_btn.setMinimumHeight(36)
        self._ai_btn.setToolTip("生成的内容会存进本地加密文件")
        self._ai_btn.clicked.connect(self._generate_side_text)
        head.addWidget(self._ai_btn)
        box.addLayout(head)

        self._side_hint = QLabel(SIDE_TEXT_EMPTY_HINT)
        self._side_hint.setObjectName("Muted")
        self._side_hint.setWordWrap(True)
        self._side_hint.setMinimumWidth(320)
        box.addWidget(self._side_hint)

        self._side_text = QTextEdit()
        self._side_text.setObjectName("Outline")
        self._side_text.setReadOnly(True)
        self._side_text.setMinimumHeight(150)
        self._side_text.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
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

    # --- 第二层：发展意愿 ---
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

    # --- 第三层：辅导提纲 ---
    def _build_outline(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("辅导提纲")
        title.setObjectName("H2")
        head.addWidget(title)
        note = QLabel("本地规则拼出来的，不联网、不调 AI")
        note.setObjectName("Muted")
        head.addWidget(note)
        head.addStretch(1)

        self._copy_btn = QPushButton("📋  一键复制")
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

    # ---------------------------------------------------------------- 名单
    def _on_search(self, text: str) -> None:
        """实时过滤：不匹配的条目 ``setHidden(True)``。

        匹配用 ``findItems`` + ``MatchContains`` —— 条目文字是
        ``张三（2023001）``，所以搜姓名和搜学号都能命中。
        """
        keyword = (text or "").strip()
        for i in range(self._list.count()):
            self._list.item(i).setHidden(True)
        if keyword:
            # MatchContains：条目文字是「张三（2023001）」，搜姓名和搜学号都能中。
            # **不要加 MatchCaseInsensitive** —— PySide6 里没这个名字（只有
            # MatchCaseSensitive），加了会抛 AttributeError；Qt 默认就是
            # 大小写不敏感，不写即可。
            hit = self._list.findItems(keyword, Qt.MatchFlag.MatchContains)
            for item in hit:
                item.setHidden(False)
        else:
            for i in range(self._list.count()):
                self._list.item(i).setHidden(False)
        self._update_list_note()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        payload = self._provider(index) if self._provider is not None else None
        if payload is None:
            self._intent_status.setText("这一行没有可显示的侧写数据。")
            return
        self._payload = payload
        self._render()

    def _select_key(self, key: str) -> None:
        """按稳定标识在名单里选中对应条目（**同名不会选错人**）。"""
        if not key:
            return
        for i in range(self._list.count()):
            item = self._list.item(i)
            if self._key_by_index.get(item.data(Qt.ItemDataRole.UserRole)) == key:
                self._list.setCurrentItem(item)
                self._list.scrollToItem(item)
                return

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

    # ---------------------------------------------------------------- 渲染
    def _render(self) -> None:
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
                "⚠ 成绩表里这名学生没有学号，意愿只能按姓名存 —— "
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

        # --- 标签页 2：AI 侧写正文 ---
        self._render_side_text(key)

    # ------------------------------------------------- 标签页 1（成绩明细）
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
            return [dict(row) for row in db.rows() if row.get("student_id") == key]
        except Exception:  # noqa: BLE001 - 明细读不出来不该拦住整页
            logger.exception("读取成绩历史失败（%s）", key)
            return []

    def _render_grades(self, key: str, archive: Any) -> None:
        if not hasattr(self, "_grade_tree"):
            return
        tree = self._grade_tree
        tree.clear()

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
            head = QTreeWidgetItem([semester or "未标注学期", "", ""])
            font = head.font(0)
            font.setBold(True)
            head.setFont(0, font)
            head.setFirstColumnSpanned(True)
            tree.addTopLevelItem(head)
            for row in courses:
                child = QTreeWidgetItem([
                    row["course"],
                    format_score(row["score"]),
                    format_credit(row["credit"]),
                ])
                child.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter))
                child.setTextAlignment(2, int(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter))
                head.addChild(child)
        tree.expandAll()

        if not grouped:
            note = "还没有这名学生的成绩明细。先在「📊 智育分析」导入他的成绩表。"
        else:
            course_count = sum(len(courses) for _term, courses in grouped)
            note = (f"{len(grouped)} 个学期，共 {course_count} 门课。"
                    "空白＝没选或没考（不是 0 分）。")
        if hasattr(self, "_grades_note"):
            self._grades_note.setText(note)

    # ------------------------------------------------- 标签页 2（AI 侧写）
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
        self._side_text.setPlainText(text)
        if text:
            self._side_hint.setText("侧写正文存放在本地加密文件里，只在本机可见。")
            self._ai_btn.setText("重新生成")
        else:
            self._side_hint.setText(SIDE_TEXT_EMPTY_HINT)
            self._ai_btn.setText("点击生成")
        self._ai_btn.setEnabled(bool(key))

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
        """「AI 侧写」按钮：拼提示词 → 交给 AI 谈话助手 → 正文落加密库。

        ★ ``ui/chat_dialog.py`` 由 C 任务并行新建：开发期它可能还不存在 /
          还不完整，所以**必须懒 import + try/except** —— 导入期绝不能崩，
          失败只弹一句中文提示。
        """
        payload = self._payload
        if not payload:
            QMessageBox.information(self, "AI 侧写", "先在左边点一名学生。")
            return
        name = str(payload.get("student") or "")
        student_id = str(payload.get("student_id") or "").strip()
        key = payload.get("key") or student_key(student_id, name)

        prompt = build_side_text_prompt(
            name, student_id, key,
            self._student_grades(key, payload),
            self._student_moral(key),
        )

        opener = self._chat_opener()
        if opener is None:
            QMessageBox.information(self, "AI 侧写", "AI 谈话助手暂不可用")
            return

        system_prompt = "你是高校辅导员的学业分析助手，回答用中文，不编造数字。"
        try:
            result = opener(self, system_prompt=system_prompt, initial_prompt=prompt)
        except Exception:  # noqa: BLE001 - 助手内部的异常不该把整页带崩
            logger.exception("调用 AI 谈话助手失败")
            QMessageBox.information(self, "AI 侧写", "AI 谈话助手暂不可用")
            return

        # 助手把生成的正文直接回吐（有的实现会返回字符串）→ 立刻回写加密库。
        text = result.get("side_text") if isinstance(result, dict) else result
        if isinstance(text, str) and text.strip():
            self._save_side_text(key, text)

    @staticmethod
    def _chat_opener() -> Any:
        """拿到 ``ui.chat_dialog.open_chat``；拿不到返回 ``None``（**不抛**）。"""
        try:
            from ui.chat_dialog import open_chat  # 懒 import：文件可能还没建
        except Exception:  # noqa: BLE001 - 包括 ImportError / 语法错 / 缺依赖
            logger.info("ui.chat_dialog 尚不可用，AI 侧写按钮只提示不崩")
            return None
        return open_chat if callable(open_chat) else None

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

    def _save_side_text(self, key: str, text: str) -> None:
        store = self._side_store()
        if store is None or not key:
            QMessageBox.information(self, "AI 侧写", "侧写暂时存不进加密库（没登录？）")
            return
        try:
            store.save_side_text(key, text)
        except Exception:  # noqa: BLE001 - 存不进也要让用户看到刚生成的正文
            logger.exception("写侧写正文失败（%s）", key)
            self._side_text.setPlainText(text)
            QMessageBox.information(self, "AI 侧写", "侧写暂时存不进加密库。")
            return
        self._render_side_text(key)

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

    # ---------------------------------------------------------------- 提纲
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

    # ---------------------------------------------------------------- 动作
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
        self._copy_btn.setText("✅  已复制")
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1600, lambda: self._copy_btn.setText("📋  一键复制"))

    # ---------------------------------------------------------------- 杂项
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
