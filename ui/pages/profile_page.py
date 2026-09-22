"""🎯 侧写辅导页 —— 左侧学生名单（可搜索） + 右侧三层侧写。

★ 文件名说明：Spec 五/六 把这一页定名为 ``profile_page.py``（"侧写"），
  所以**个人中心**那一页在 :mod:`ui.pages.account_page`，别走错。

布局::

    +------------------+---------------------------------------+
    | 学生名单          |  【张三（2023001）】 学业肖像          |
    | 🔍 [搜索姓名/学号] |   发展意愿                             |
    | 张三（2023001）   |   辅导提纲                             |
    | 李四（2023002）   |                                       |
    +------------------+---------------------------------------+

★ 为什么要有这一层列表
---------------------

原来点侧栏「侧写辅导」直接落到**上一次那一名学生**身上 —— 用户看到的是别人的
数据，还以为是在看自己刚点的那个人。现在：

* 点侧栏 → 落在**名单**上（右侧是谁取决于你在名单里点了谁，没点就是提示语）；
* 从智育分析页点「侧写」→ 才带着具体学生跳过来，并在名单里选中他。

★ 身份用**学号**，不用姓名
-------------------------

同名学生（历届都有）按姓名索引会**互相串号**：意愿互相覆盖、预警取错行。
本页一律用 ``payload["key"]``（``sid:<学号>``，没学号才退回 ``name:<姓名>``）
作为意愿的键。没学号的学生会在界面上**明说**同名风险。

三层结构（Spec 六）::

    第一层 · 学业肖像卡片     均分 / 趋势 / 已定型短板 / 可抢救项
    第二层 · 发展意愿         QButtonGroup + 6 个 QRadioButton → 落加密盘
    第三层 · 辅导提纲         本地规则拼三段文字（只读）+ 一键复制

★ 关于"不重新算"
----------------

Spec 说「数据来源：student dict 里已有的字段，不重新算」。本页严格遵守：
均分、趋势、定型/可救分区全部来自 :mod:`core.archival` 的
``trend()`` / ``partition()`` / ``merge_records()`` —— 那些是 core 里**已经有**的函数，
不是本页新写的算术。

唯一的例外是「提至 85 可 +0.X」这一句：它天生是个假设运算，不算是算不出来的。
处理办法是**复用 core 自己的** ``core.calculator.weighted_average`` ——
把某一门课的分数换掉之后，把整串课程重新喂给它。这样"假设分"和"实际分"
出自同一份实现，**绝不会出现两个数字打架**。本页自己一行加减乘除都没写。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.archival import merge_records, partition, trend
from core.calculator import CalculationScope, weighted_average
from core.crypto_store import CryptoError, CryptoStore
from ui.anim import make_card
from ui.intentions import (
    INTENTION_OPTIONS,
    KEY_PREFIX_NAME,
    load_intentions,
    save_intention,
    student_key,
)

__all__ = ["LIST_WIDTH", "ProfilePage", "RESCUE_TARGET"]

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
        self._build()

    # ---------------------------------------------------------------- 外部
    def set_user(self, username: str) -> None:
        """切账户时调用（登录、切换账号后）。会重新读这个人的意愿。"""
        self._username = username or ""
        self._intentions = (
            load_intentions(self._store, self._username) if self._username else {}
        )
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

    # --- 右：详情 ---
    def _build_detail_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        self._empty = QLabel(
            "👈  在左边点一名学生，这里显示他的学业肖像、发展意愿和辅导提纲。\n\n"
            "名单来自「📊 智育分析」导入的成绩表。还没导入的话，先去那一页导一份。"
        )
        self._empty.setObjectName("Placeholder")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        box.addWidget(self._empty, 1)

        self._body = QWidget()
        body_box = QVBoxLayout(self._body)
        body_box.setContentsMargins(0, 0, 0, 0)
        body_box.setSpacing(14)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._inner_box = QVBoxLayout(inner)
        self._inner_box.setContentsMargins(0, 0, 6, 0)
        self._inner_box.setSpacing(14)

        self._inner_box.addWidget(self._build_portrait())
        self._inner_box.addWidget(self._build_intention())
        self._inner_box.addWidget(self._build_outline())
        self._inner_box.addStretch(1)

        scroll.setWidget(inner)
        body_box.addWidget(scroll, 1)
        box.addWidget(self._body, 1)
        self._body.hide()
        return pane

    # --- 第一层：学业肖像 ---
    def _build_portrait(self) -> QFrame:
        card = make_card()
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 18, 20, 18)
        box.setSpacing(10)

        top = QHBoxLayout()
        self._portrait_title = QLabel("学业肖像")
        self._portrait_title.setObjectName("H2")
        top.addWidget(self._portrait_title)
        top.addSpacing(12)

        self._avg_label = QLabel("—")
        self._avg_label.setObjectName("H1")
        top.addWidget(self._avg_label)

        self._trend_tag = QLabel("—")
        self._trend_tag.setObjectName("TagGray")
        top.addWidget(self._trend_tag)
        top.addStretch(1)
        box.addLayout(top)

        self._avg_sub = QLabel("")
        self._avg_sub.setObjectName("Muted")
        self._avg_sub.setWordWrap(True)
        box.addWidget(self._avg_sub)

        box.addWidget(self._section("已定型短板", "补考不改分（红线 9），不必再纠结"))
        self._locked_box = QVBoxLayout()
        self._locked_box.setSpacing(4)
        box.addLayout(self._locked_box)

        box.addWidget(self._section("可抢救项", "分不高、但提一提能拉均分"))
        self._rescue_box = QVBoxLayout()
        self._rescue_box.setSpacing(4)
        box.addLayout(self._rescue_box)
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

        head = f"{name}（{student_id}）" if student_id else name
        self._portrait_title.setText(f"{head} · 学业肖像")
        self._avg_label.setText(_fmt(default.weighted_average if default else None))

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

        parts = [f"仅必修+限选均分 {_fmt(default.weighted_average if default else None)}"]
        if overall is not None:
            parts.append(f"全口径 {_fmt(overall.weighted_average)}")
        parts.append(terms_text)
        self._avg_sub.setText("　·　".join(parts))

        # --- 已定型短板 / 可抢救项（全部来自 core.archival）---
        self._clear_layout(self._locked_box)
        self._clear_layout(self._rescue_box)

        part = partition(archive) if archive is not None else None
        locked = list(part.get("locked", [])) if part else []
        absent = list(part.get("absent", [])) if part else []

        if locked:
            for entry in locked:
                credit = _credit_of(archive, entry.get("term", ""), entry.get("course", ""))
                text = f"· {entry.get('course', '?')}"
                if credit is not None:
                    text += f"　{credit:g} 学分"
                text += f"　（{entry.get('term', '')} 已定型）"
                self._locked_box.addWidget(self._tagged(text, "TagGray"))
        else:
            self._locked_box.addWidget(self._tagged("· 没有已定型的挂科课", "TagGray"))

        for entry in absent:
            self._locked_box.addWidget(
                self._tagged(
                    f"· {entry.get('course', '?')}　缺考（还能安排补考，"
                    f"{entry.get('term', '')}）",
                    "TagOrange",
                )
            )

        rescues = self._compute_rescues(payload)
        if rescues:
            for course_name, credit, score, gain in rescues:
                self._rescue_box.addWidget(
                    self._tagged(
                        f"· {course_name}　{credit:g} 学分　当前 {score:g} 分　→　"
                        f"提至 {RESCUE_TARGET:g} 可 {gain:+.3f}",
                        "TagOrange",
                    )
                )
        else:
            self._rescue_box.addWidget(
                self._tagged("· 没有明显可抢救的课（分数都已达标或学分太小）", "TagGray")
            )

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
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

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
