"""智育分析页 —— 本次唯一接真逻辑的页面。

链路（严格照 Spec，一步都不省）::

    选文件 → pandas 读原始表 → core.parser 三级提取
           → 红线 3 人工回显确认（不可跳过）
           → core.calculator.calculate_many 三口径
           → 表格显示 + 红标预警

**calculator / parser 一个字符都没改**，它们返回什么这里就显示什么；
本页只做格式化，不做任何二次计算。

红标判定见 :mod:`ui.flags`（只决定标不标色，不参与加权）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

import ttkbootstrap as ttkb

from core.calculator import CalculationScope, calculate_many
from core.parser import parse_dataframe
from core.validator import check_credit_anchor, describe_alerts
from ui.flags import build_flags
from ui.review_dialog import confirm_courses

__all__ = ["build_academic_page"]

#: 表格里的三档口径列（顺序即列顺序）
SCOPE_COLUMNS: tuple[tuple[str, str, CalculationScope], ...] = (
    ("all", "口径① 含全部", CalculationScope.ALL),
    ("req_lim", "口径② 仅必修+限选", CalculationScope.REQUIRED_LIMITED),
    ("req", "口径③ 仅必修", CalculationScope.REQUIRED_ONLY),
)

_TABLE_FONT = ("Microsoft YaHei UI", 10)
_HEAD_FONT = ("Microsoft YaHei UI", 10, "bold")


def _read_frame(path: str) -> Any:
    """把原始表读成 DataFrame（保留原样，不做任何清洗）。

    ``with pd.ExcelFile(...)`` 是必须的：Windows 上不显式关闭句柄的话，
    Excel 还开着这个文件时会读失败（踩过）。
    """
    import pandas as pd

    if path.lower().endswith(".csv"):
        return pd.read_csv(path, header=None, dtype=object)
    with pd.ExcelFile(path) as book:
        return pd.read_excel(book, header=None, dtype=object)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.6f}"


def build_academic_page(parent: tk.Misc) -> ttk.Frame:
    """搭出智育分析页，返回它的根 Frame。"""
    frame = ttk.Frame(parent, padding=20)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(2, weight=1)

    #: 页面状态都收在这里（这一页是函数不是类，用闭包变量会被垃圾回收盯上）
    state: dict[str, Any] = {
        "parsed": None,
        "results": {},          # scope -> list[CalculationResult]
        "flags": {},            # 学生名 -> StudentFlag
        "path": "",
    }

    # --- 顶部：导入按钮 + 状态 ---
    head = ttk.Frame(frame)
    head.grid(row=0, column=0, sticky="ew")

    import_button = ttkb.Button(
        head, text="📂  导入成绩表", bootstyle="primary",
        command=lambda: _do_import(), width=18,
    )
    import_button.pack(side="left")

    ttk.Label(
        head,
        text="  选一份成绩单 → 人工核对学分与类别 → 三口径加权",
        foreground="#6c757d",
    ).pack(side="left")

    status = ttk.Label(frame, text="还没有导入数据。", foreground="#6c757d")
    status.grid(row=1, column=0, sticky="w", pady=(10, 12))

    # --- 中部：全班表格 ---
    table_box = ttk.Frame(frame)
    table_box.grid(row=2, column=0, sticky="nsew")
    table_box.columnconfigure(0, weight=1)
    table_box.rowconfigure(0, weight=1)

    columns = ("name",) + tuple(key for key, _, _ in SCOPE_COLUMNS) + ("flag",)
    tree = ttk.Treeview(table_box, columns=columns, show="headings", height=12)
    tree.heading("name", text="姓名")
    tree.column("name", width=130, anchor="w", stretch=False)
    for key, title, _ in SCOPE_COLUMNS:
        tree.heading(key, text=title)
        tree.column(key, width=150, anchor="center", stretch=True)
    tree.heading("flag", text="红标列")
    tree.column("flag", width=290, anchor="w", stretch=True)

    tree.tag_configure("danger", background="#FDE8E8", foreground="#9B1C1C")
    tree.tag_configure("warning", background="#FDF6E3", foreground="#8A6D1F")
    tree.tag_configure("even", background="#F7F9FC")

    ybar = ttk.Scrollbar(table_box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ybar.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ybar.grid(row=0, column=1, sticky="ns")

    # --- 底部：选中那一行的详情 ---
    detail = scrolledtext.ScrolledText(
        frame, height=9, wrap="word", font=_TABLE_FONT,
        relief="solid", borderwidth=1,
    )
    detail.grid(row=3, column=0, sticky="ew", pady=(12, 0))
    detail.configure(state="disabled")

    # ------------------------------------------------------------------ 逻辑

    def _clear_table() -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _set_detail(text: str) -> None:
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.configure(state="disabled")

    def _do_import() -> None:
        path = filedialog.askopenfilename(
            parent=frame.winfo_toplevel(),
            title="选择成绩表（.xlsx / .xls / .csv）",
            filetypes=[
                ("Excel 工作簿", "*.xlsx *.xls"),
                ("CSV 文件", "*.csv"),
                ("全部文件", "*.*"),
            ],
        )
        if not path:
            return

        status.configure(text="正在解析…", foreground="#6c757d")
        frame.update_idletasks()

        try:
            raw = _read_frame(path)
        except Exception as exc:  # noqa: BLE001 - 读文件的意外都要给人看
            messagebox.showerror(
                "读不出这个文件",
                f"{exc.__class__.__name__}: {exc}\n\n"
                "请确认它是完整的 .xlsx/.xls/.csv，且没有被别的程序占用。",
                parent=frame.winfo_toplevel(),
            )
            status.configure(text="读取失败。", foreground="#9B1C1C")
            return

        parsed = parse_dataframe(raw)
        if not parsed.courses:
            messagebox.showerror(
                "没识别出任何课程",
                "程序找不到表头行。需要有一行包含「姓名」或「学号」，"
                "其余列是课程名（最好带上学分和类别，例如“高等数学(5学分)必修”）。\n\n"
                + "\n".join(f"· {w}" for w in parsed.warnings[:6]),
                parent=frame.winfo_toplevel(),
            )
            status.configure(text="解析失败：没有课程列。", foreground="#9B1C1C")
            return

        # 锚点校验（表里没有「所得学分」列时静默跳过）
        try:
            anchor_report = check_credit_anchor(raw, parsed)
        except Exception:  # noqa: BLE001 - 锚点校验失败不该拦住主链路
            anchor_report = None

        # ★ 红线 3：强制人工回显确认。没确认就绝不进入计算。
        confirmed = confirm_courses(
            frame,
            parsed.courses,
            extra_alerts=describe_alerts(anchor_report),
            warnings=parsed.warnings,
        )
        if not confirmed:
            status.configure(
                text="已取消：没有确认课程表，本次未计算、未显示任何数字。",
                foreground="#8A6D1F",
            )
            return

        status.configure(text="正在计算三档口径…", foreground="#6c757d")
        frame.update_idletasks()
        try:
            results = calculate_many(parsed)
        except Exception as exc:  # noqa: BLE001 - 计算异常必须暴露，不静默吞
            messagebox.showerror(
                "计算过程出错", f"{exc.__class__.__name__}: {exc}",
                parent=frame.winfo_toplevel(),
            )
            status.configure(text="计算失败。", foreground="#9B1C1C")
            return

        # 红标以默认口径（仅必修+限选）为准，和辅导员日常看的那张表一致
        default_rows = results.get(CalculationScope.REQUIRED_LIMITED, [])
        flags = build_flags(
            default_rows,
            invalid_rows=parsed.invalid_rows,
            anchor_exceeded=anchor_report.exceeded if anchor_report is not None else (),
        )

        state["parsed"] = parsed
        state["results"] = results
        state["flags"] = flags
        state["path"] = path
        _fill_table(parsed, results, flags)

        danger = sum(1 for f in flags.values() if f.level == "danger")
        warning = sum(1 for f in flags.values() if f.level == "warning")
        if anchor_report is None or not anchor_report.anchors:
            anchor_text = "表内无「所得学分」锚点列"
        elif describe_alerts(anchor_report):
            anchor_text = f"锚点超标 {len(anchor_report.exceeded)} 处"
        else:
            anchor_text = "锚点校验通过"
        status.configure(
            text=f"已导入 {path.split('/')[-1].split(chr(92))[-1]}　·　"
                 f"{len(parsed.courses)} 门课 / {parsed.total_credit:g} 学分　·　"
                 f"{len(parsed.valid_rows)} 名学生　·　红标 {danger} 人 / 黄标 {warning} 人　·　"
                 f"{anchor_text}",
            foreground="#1F2430",
        )

    def _fill_table(parsed: Any, results: dict[Any, list[Any]], flags: dict[str, Any]) -> None:
        _clear_table()
        primary = results.get(CalculationScope.ALL, [])
        by_scope = {
            scope: {r.student: r for r in rows} for scope, rows in results.items()
        }

        for index, row in enumerate(primary):
            flag = flags.get(row.student)
            values = [row.student]
            for key, _, scope in SCOPE_COLUMNS:
                result = by_scope.get(scope, {}).get(row.student)
                values.append(_fmt(result.weighted_average) if result else "—")
            values.append(flag.summary if flag else "")

            tag = "even" if index % 2 else ""
            if flag is not None and flag.is_flagged:
                tag = flag.level
            tree.insert("", "end", iid=row.student, values=values, tags=(tag,))

        if primary:
            first = primary[0].student
            tree.selection_set(first)
            tree.focus(first)
            _show_detail(first)

    def _show_detail(student: str) -> None:
        parsed = state["parsed"]
        results = state["results"]
        flag = state["flags"].get(student)
        if parsed is None or not student:
            return

        lines: list[str] = [f"【{student}】"]
        for key, title, scope in SCOPE_COLUMNS:
            result = next(
                (r for r in results.get(scope, []) if r.student == student), None
            )
            if result is None:
                continue
            lines.append(
                f"  {title}：{_fmt(result.weighted_average)}"
                f"　（Σ学分 {result.total_credit:g}，Σ成绩×学分 {result.weighted_sum:g}）"
            )

        if flag is not None and flag.reasons:
            lines.append("")
            lines.append(f"预警（{flag.label or '无'}）：")
            lines.extend(f"  · {reason}" for reason in flag.reasons)
        elif flag is not None:
            lines.append("")
            lines.append("预警：无")

        default = next(
            (r for r in results.get(CalculationScope.REQUIRED_LIMITED, [])
             if r.student == student),
            None,
        )
        if default is not None:
            lines.append("")
            lines.append("仅必修+限选 · 计入的课：")
            for course in default.counted_courses:
                lines.append(
                    f"  · {course.name}　{float(course.credit):g} 学分　"
                    f"{course.score:g} 分　贡献 {course.contribution:g}"
                )
            if default.skipped_blank:
                lines.append(
                    f"  （未选跳过：{'、'.join(default.skipped_blank)}）"
                )

        _set_detail("\n".join(lines))

    def _on_select(_event: Any) -> None:
        selection = tree.selection()
        if selection:
            _show_detail(selection[0])

    tree.bind("<<TreeviewSelect>>", _on_select)

    _set_detail(
        "还没有数据。点左上角「📂  导入成绩表」选一份成绩单。\n\n"
        "程序会先按三级规则提取学分与类别，然后弹出人工核对窗口 ——\n"
        "那是 Spec 写死的强制环节，不能跳过；确认之后才会开始计算。"
    )
    return frame
