"""智育分析页 —— **本次唯一接真逻辑的页面**。

链路（严格照 Spec，一步都不省）::

    选文件 → pandas 读原始表 → core.validator.parse_and_validate
                                （= core.parser 三级提取 + 锚点校验）
           → 红线 3 人工回显确认（不可跳过）
           → core.calculator.calculate_many 三口径
           → core.archival.build_archive 归档（供侧写页用）
           → 表格显示 + 红标预警

**calculator / parser / validator / archival 一个字符都没改**，
它们返回什么这里就显示什么；本页只做格式化，不做任何二次算术。

红标判定见 :mod:`ui.flags`（只决定标不标色，不参与加权）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.archival import build_archive
from core.calculator import CalculationScope, calculate_many
from core.validator import describe_alerts, parse_and_validate
from ui.anim import make_card
from ui.flags import build_flags
from ui.review_dialog import confirm_courses

__all__ = ["AnalysisPage", "SCOPE_COLUMNS"]

#: 表格里的三档口径列（顺序即列顺序）
SCOPE_COLUMNS: tuple[tuple[str, str, CalculationScope], ...] = (
    ("all", "口径① 含全部", CalculationScope.ALL),
    ("req_lim", "口径② 仅必修+限选", CalculationScope.REQUIRED_LIMITED),
    ("req", "口径③ 仅必修", CalculationScope.REQUIRED_ONLY),
)

#: 完整列标题：**姓名 + 三口径 + 红标原因 + 操作**。
#:
#: ★ 必须从 SCOPE_COLUMNS **推出来**，绝不能手写。
#:   踩过的坑：加「操作」列时手写了一份表头元组，漏掉一个口径列 ——
#:   表头 5 个、数据 6 列，整体错位一格，结果「侧写」按钮正好压在
#:   「红标原因」的单元格上（表头写着"红标原因"的那一列显示的其实是口径③）。
#:   自动推导之后，两边再也不可能漂移。
COLUMN_TITLES: tuple[str, ...] = (
    ("姓名",)
    + tuple(title for _key, title, _scope in SCOPE_COLUMNS)
    + ("红标原因", "操作")
)

#: 列下标（同样推导，不写魔法数字）
NAME_COLUMN = 0
FIRST_SCOPE_COLUMN = 1
FLAG_COLUMN = len(COLUMN_TITLES) - 2
ACTION_COLUMN = len(COLUMN_TITLES) - 1

#: 「操作」列宽度。「侧写」按钮 + 左右各 6px 容器边距，留点余量。
_ACTION_COLUMN_WIDTH = 96

#: 行高。必须 ≥ 容器上下边距(12) + 按钮最小高(26)，否则按钮会被压扁。
_ROW_HEIGHT = 44

#: 操作列里那个小按钮的最小高度
_ROW_BUTTON_HEIGHT = 28

#: 导入时假设的学期标签。表里没有学期信息，用"未标注学期"，
#: **不猜** —— 猜错会让纵向档案的排序和趋势全乱。
DEFAULT_TERM = "未标注"


def _read_raw_frame(path: str) -> pd.DataFrame:
    """把原始表读成 DataFrame（保留原样，不做任何清洗）。

    ``with pd.ExcelFile(...)`` 是必须的：Windows 上不显式关闭句柄的话，
    Excel 还开着这个文件时会读失败（踩过）。
    """
    if path.lower().endswith(".csv"):
        return pd.read_csv(path, header=None, dtype=object)
    with pd.ExcelFile(path) as book:
        return pd.read_excel(book, header=None, dtype=object)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.6f}"


class AnalysisPage(QWidget):
    """智育分析页。

    Signals:
        student_selected(dict): 用户双击某行或点该行「侧写」按钮。
            dict 里带着侧写辅导页需要的**全部**原料，它不需要重算任何东西。
    """

    student_selected = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: 页面状态。（这一页是有状态的：表格、结果、红标都挂在这里）
        self._parsed: Any = None
        self._results: dict[Any, list[Any]] = {}
        self._flags: dict[str, Any] = {}
        self._archive: Any = None
        self._source: str = ""
        self._build()

    # ---------------------------------------------------------------- 搭界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        head = QHBoxLayout()
        self._import_btn = QPushButton("📂  导入成绩表")
        self._import_btn.setObjectName("Primary")
        self._import_btn.setMinimumHeight(42)
        self._import_btn.setMinimumWidth(170)
        self._import_btn.clicked.connect(self._do_import)
        head.addWidget(self._import_btn)

        hint = QLabel("选一份成绩单 → 人工核对学分与类别 → 三口径加权")
        hint.setObjectName("Muted")
        head.addWidget(hint)
        head.addStretch(1)
        root.addLayout(head)

        self._status = QLabel("还没有导入数据。")
        self._status.setObjectName("Muted")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._table = QTableWidget(0, len(COLUMN_TITLES))
        self._table.setHorizontalHeaderLabels(list(COLUMN_TITLES))
        self._table.setAlternatingRowColors(True)          # Spec：斑马纹
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        # ★ 行高必须容得下操作列里的按钮：容器上下边距 12 + 按钮最小高 26 = 38。
        #   默认行高约 30px，会把按钮压成 12px 高、只剩半个字（踩过）。
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self._table.setShowGrid(False)
        self._table.doubleClicked.connect(self._on_double_click)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(NAME_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(FIRST_SCOPE_COLUMN, ACTION_COLUMN):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        # ★ 操作列必须写死宽度：ResizeToContents **不会**把 setCellWidget 放进去的
        #   按钮算进来，列会被压成表头「操作」两个字的宽度，按钮只剩半个字。
        header.setSectionResizeMode(ACTION_COLUMN, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(ACTION_COLUMN, _ACTION_COLUMN_WIDTH)
        root.addWidget(self._table, 1)

        self._detail = QLabel(
            "双击任意一行（或点该行的「侧写」）即可跳到「🎯 侧写辅导」。"
        )
        self._detail.setObjectName("Muted")
        self._detail.setWordWrap(True)
        root.addWidget(self._detail)

    # ---------------------------------------------------------------- 导入
    def _do_import(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择成绩表（.xlsx / .xls / .csv）",
            "",
            "Excel 工作簿 (*.xlsx *.xls);;CSV 文件 (*.csv);;全部文件 (*)",
        )
        if not path:
            return

        self._status.setText("正在解析…")
        self._status.setObjectName("Muted")
        self._repolish(self._status)
        QWidget.repaint(self)

        try:
            raw = _read_raw_frame(path)
        except Exception as exc:  # noqa: BLE001 - 读文件的意外都要给人看
            QMessageBox.critical(
                self,
                "读不出这个文件",
                f"{exc.__class__.__name__}: {exc}\n\n"
                "请确认它是完整的 .xlsx/.xls/.csv，且没有被别的程序占用。",
            )
            self._set_status("读取失败。", error=True)
            return

        # core 的便捷入口：解析 + 锚点校验一次做完
        try:
            parsed, anchor_report = parse_and_validate(raw)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "解析出错", f"{exc.__class__.__name__}: {exc}"
            )
            self._set_status("解析失败。", error=True)
            return

        if not parsed.courses:
            QMessageBox.critical(
                self,
                "没识别出任何课程",
                "程序找不到表头行。需要有一行包含「姓名」或「学号」，"
                "其余列是课程名（最好带上学分和类别，例如“高等数学(5学分)必修”）。\n\n"
                + "\n".join(f"· {w}" for w in parsed.warnings[:6]),
            )
            self._set_status("解析失败：没有课程列。", error=True)
            return

        # ★ 红线 3：强制人工回显确认。没确认就绝不进入计算。
        confirmed = confirm_courses(
            self,
            parsed.courses,
            extra_alerts=describe_alerts(anchor_report),
            warnings=parsed.warnings,
        )
        if not confirmed:
            self._set_status(
                "已取消：没有确认课程表，本次未计算、未显示任何数字。", warn=True
            )
            return

        self._status.setText("正在计算三档口径…")
        self._repolish(self._status)
        QWidget.repaint(self)
        try:
            results = calculate_many(parsed)
        except Exception as exc:  # noqa: BLE001 - 计算异常必须暴露，不静默吞
            QMessageBox.critical(
                self, "计算过程出错", f"{exc.__class__.__name__}: {exc}"
            )
            self._set_status("计算失败。", error=True)
            return

        # 归档：侧写辅导页的"趋势 / 定型·可救"从这里来，本页算一次就够
        try:
            self._archive = build_archive(
                parsed, DEFAULT_TERM, source=_base_name(path)
            )
        except Exception:  # noqa: BLE001 - 归档失败不该拦住看数字
            self._archive = None

        # 红标以默认口径（仅必修+限选）为准，和辅导员日常看的那张表一致
        default_rows = results.get(CalculationScope.REQUIRED_LIMITED, [])
        self._flags = build_flags(
            default_rows,
            invalid_rows=parsed.invalid_rows,
            anchor_exceeded=anchor_report.exceeded if anchor_report is not None else (),
        )
        self._parsed = parsed
        self._results = results
        self._source = _base_name(path)
        self._fill_table(parsed, results)

        danger = sum(1 for f in self._flags.values() if f.level == "danger")
        warning = sum(1 for f in self._flags.values() if f.level == "warning")
        if anchor_report is None or not anchor_report.anchors:
            anchor_text = "表内无「所得学分」锚点列"
        elif describe_alerts(anchor_report):
            anchor_text = f"锚点超标 {len(anchor_report.exceeded)} 处"
        else:
            anchor_text = "锚点校验通过"
        self._status.setText(
            f"已导入 {self._source}　·　{len(parsed.courses)} 门课 / "
            f"{parsed.total_credit:g} 学分　·　{len(parsed.valid_rows)} 名学生　·　"
            f"红标 {danger} 人 / 黄标 {warning} 人　·　{anchor_text}"
        )
        self._repolish(self._status)

    # ---------------------------------------------------------------- 填表
    def _fill_table(self, parsed: Any, results: dict[Any, list[Any]]) -> None:
        primary = results.get(CalculationScope.ALL, [])
        by_scope = {scope: {r.student: r for r in rows} for scope, rows in results.items()}

        self._table.setRowCount(len(primary))
        for row, item in enumerate(primary):
            name = item.student
            flag = self._flags.get(name)

            # 姓名
            name_cell = QTableWidgetItem(name)
            name_cell.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            self._table.setItem(row, NAME_COLUMN, name_cell)

            # 三档口径（列下标紧跟姓名，由 COLUMN_TITLES 保证对齐）
            for offset, (_key, _title, scope) in enumerate(SCOPE_COLUMNS):
                result = by_scope.get(scope, {}).get(name)
                cell = QTableWidgetItem(_fmt(result.weighted_average) if result else "—")
                cell.setTextAlignment(
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                )
                self._table.setItem(row, FIRST_SCOPE_COLUMN + offset, cell)

            # 红标原因
            flag_cell = QTableWidgetItem(flag.summary if flag else "")
            flag_cell.setToolTip(flag.summary if flag else "")
            self._table.setItem(row, FLAG_COLUMN, flag_cell)

            # 操作
            button = QPushButton("侧写")
            button.setObjectName("RowAction")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(_ROW_BUTTON_HEIGHT)
            button.clicked.connect(lambda _=False, n=name: self._emit_student(n))
            holder = QWidget()
            holder_box = QHBoxLayout(holder)
            holder_box.setContentsMargins(6, 6, 6, 6)
            holder_box.addWidget(button)
            self._table.setCellWidget(row, ACTION_COLUMN, holder)

        if primary:
            self._table.selectRow(0)
            self._show_detail(primary[0].student)

    def _show_detail(self, student: str) -> None:
        """表下那一行口径明细 —— Σ学分 / Σ成绩×学分 是加权算术的证据链。"""
        if self._parsed is None or not student:
            return
        parts: list[str] = []
        for _key, title, scope in SCOPE_COLUMNS:
            result = next(
                (r for r in self._results.get(scope, []) if r.student == student), None
            )
            if result is None:
                continue
            parts.append(
                f"{title}＝{_fmt(result.weighted_average)}"
                f"（Σ学分 {result.total_credit:g}，Σ成绩×学分 {result.weighted_sum:g}）"
            )
        flag = self._flags.get(student)
        if flag is not None and flag.reasons:
            parts.append(f"预警（{flag.label}）：{'；'.join(flag.reasons)}")
        self._detail.setText(f"【{student}】" + "　|　".join(parts))
        self._repolish(self._detail)

    # ---------------------------------------------------------------- 选中
    def _on_double_click(self, index) -> None:
        row = index.row()
        item = self._table.item(row, NAME_COLUMN)
        if item is not None:
            self._emit_student(item.text())

    def _emit_student(self, name: str) -> None:
        payload = self.student_payload(name)
        if payload is not None:
            self.student_selected.emit(payload)

    def student_payload(self, name: str) -> dict[str, Any] | None:
        """把侧写辅导页需要的东西**一次性打包**，那边一个数都不用再算。

        里面放的是 core 的原始对象（``CalculationResult`` / ``StudentArchive``），
        不是重新拼的字典 —— 免得两份数字打架。
        """
        if self._parsed is None or not name:
            return None
        results = {
            scope: next((r for r in rows if r.student == name), None)
            for scope, rows in self._results.items()
        }
        if all(value is None for value in results.values()):
            return None
        student_archive = None
        if self._archive is not None:
            student_archive = next(
                (s for s in self._archive.students.values() if s.student == name), None
            )
        return {
            "student": name,
            "results": results,
            "flag": self._flags.get(name),
            "archive": student_archive,
            "source": self._source,
        }

    def current_payload(self) -> dict[str, Any] | None:
        """当前选中的那行（侧栏切过来时用）。"""
        row = self._table.currentRow()
        item = self._table.item(row, NAME_COLUMN) if row >= 0 else None
        return self.student_payload(item.text()) if item is not None else None

    def export_records(self) -> list[dict[str, Any]]:
        """给设置页的导出功能提供记录。

        列名按 :data:`core.exporter.ANONYMIZED_COLUMNS` 来 —— 脱敏导出会按
        白名单裁列，**姓名不在白名单里**，所以明文版能带姓名、脱敏版带不了。
        本方法只负责把已有数字摆成表，不做任何新算术。
        """
        if not self._results:
            return []
        rows = self._results.get(CalculationScope.REQUIRED_LIMITED) or []
        by_student = {r.student: r for r in rows}
        archive_students = {}
        if self._archive is not None:
            archive_students = {
                s.student: s for s in self._archive.students.values()
            }

        records: list[dict[str, Any]] = []
        for index, (name, result) in enumerate(by_student.items(), 1):
            student_archive = archive_students.get(name)
            records.append(
                {
                    "S编号": f"S{index:03d}",
                    "姓名": name,
                    "学期": DEFAULT_TERM,
                    "口径": "仅必修+限选",
                    "加权均分": result.weighted_average,
                    "总学分": result.total_credit,
                    "挂科数": getattr(student_archive, "failed_total", 0),
                    "缺考数": getattr(student_archive, "absent_total", 0),
                    "意愿标签": "",
                }
            )
        return records

    # ---------------------------------------------------------------- 杂项
    def _set_status(self, text: str, *, error: bool = False, warn: bool = False) -> None:
        self._status.setText(text)
        self._status.setObjectName("Danger" if error else ("Warn" if warn else "Muted"))
        self._repolish(self._status)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        """换了 objectName 之后必须重新应用样式表，否则颜色不跟着变。"""
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _base_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]
