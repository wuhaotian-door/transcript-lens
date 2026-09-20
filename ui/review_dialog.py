"""红线 3 的强制人工回显弹窗 —— **Spec 写死的必经环节，不能 skip**。

Spec 原文（``docs/SGMS_v1.3_Final_Spec.md``）：

    第 175 行  三级提取：正则抠学分与类别 → 独立权重行 → 都失败则标红进人工弹窗。
    第 176 行  人工回显弹窗是强制环节，不能 skip。
    第 230 行  红线 3：学分提取必须人工回显确认。

所以这个弹窗**没有"跳过"按钮**，关窗按钮也算"返回修正"，不产生确认。
唯一能往下走的路是点「确认无误，开始计算」。

为什么这么犟：学分是加权算法的**分母**。抠错一个学分，整张表的均分都是错的，
而且错得毫无痕迹 —— 用户会拿着一份看起来很正常、实际全错的数字去和学生谈话。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Sequence

import ttkbootstrap as ttkb

__all__ = ["confirm_courses"]

#: 需要人工确认的行底色（浅红）。故意用显式颜色而不是 bootstyle ——
#: Treeview 的 tag 在 ttkbootstrap 各主题下表现不一致，显式色反而稳。
_WARN_BG = "#FDE8E8"
_WARN_FG = "#9B1C1C"


def confirm_courses(
    parent: tk.Misc,
    courses: Sequence[Any],
    *,
    title: str = "请人工核对课程学分与类别",
    extra_alerts: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> bool:
    """把提取出的课程表摆给辅导员确认。**这是红线 3。**

    Args:
        parent: 父窗口。
        courses: :class:`core.parser.Course` 序列（任何有
            ``name/category/credit/credit_source/category_source/warnings`` 的对象都行）。
        extra_alerts: 额外要点名的预警（锚点校验、错位行等）。
        warnings: 解析阶段的告警文案。

    Returns:
        ``True`` = 确认无误，可以进入计算；``False`` = 返回修正（含直接关窗）。
    """
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(True, True)

    outer = ttk.Frame(dialog, padding=18)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="第一步：核对课程学分与类别",
              font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
    ttk.Label(
        outer,
        text="程序按「表头正则 → 独立权重行 → 类别行」三级提取的结果如下。\n"
             "学分和类别是加权算法的分母，请逐行确认 —— 确认之后才会开始计算。",
        justify="left", foreground="#6c757d",
    ).pack(anchor="w", pady=(6, 0))

    # --- 课程表 ---
    table_box = ttk.Frame(outer)
    table_box.pack(fill="both", expand=True, pady=(14, 0))

    columns = ("idx", "name", "category", "credit", "credit_src", "cat_src")
    tree = ttk.Treeview(table_box, columns=columns, show="headings", height=11)
    headings = {
        "idx": ("#", 40, "center"),
        "name": ("课程名称", 200, "w"),
        "category": ("类别", 70, "center"),
        "credit": ("学分", 60, "e"),
        "credit_src": ("学分来源", 160, "w"),
        "cat_src": ("类别来源", 150, "w"),
    }
    for key, (text, width, anchor) in headings.items():
        tree.heading(key, text=text)
        tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))

    tree.tag_configure("warn", background=_WARN_BG, foreground=_WARN_FG)

    bar = ttk.Scrollbar(table_box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    for index, course in enumerate(courses):
        category = getattr(course.category, "value", str(course.category))
        tags = ("warn",) if list(getattr(course, "warnings", []) or []) else ()
        tree.insert(
            "", "end", iid=str(index),
            values=(
                index + 1,
                course.name,
                category,
                f"{float(course.credit):g}",
                course.credit_source or "—",
                course.category_source or "—",
            ),
            tags=tags,
        )

    total_credit = sum(float(c.credit) for c in courses)
    ttk.Label(
        outer,
        text=f"合计 {len(courses)} 门课，总学分 {total_credit:g}",
        font=("Consolas", 10, "bold"),
    ).pack(anchor="w", pady=(10, 0))

    # --- 需要注意的地方 ---
    problems: list[str] = []
    for course in courses:
        for text in list(getattr(course, "warnings", []) or []):
            problems.append(f"· {course.name}：{text}")
    problems.extend(f"· {text}" for text in extra_alerts)
    problems.extend(f"· {text}" for text in warnings)

    if problems:
        box = ttk.Labelframe(outer, text=f"需要注意 {len(problems)} 处", padding=10)
        box.pack(fill="x", pady=(12, 0))
        for text in problems[:8]:
            ttk.Label(box, text=text, justify="left", wraplength=700,
                      foreground="#9B1C1C").pack(anchor="w", pady=1)
        if len(problems) > 8:
            ttk.Label(box, text=f"…还有 {len(problems) - 8} 条，见导入页状态栏",
                      foreground="#6c757d").pack(anchor="w", pady=(2, 0))

    # --- 按钮：注意这里**没有「跳过」** ---
    result = {"ok": False}

    def _accept() -> None:
        result["ok"] = True
        dialog.destroy()

    def _reject() -> None:
        result["ok"] = False
        dialog.destroy()

    footer = ttk.Frame(outer)
    footer.pack(fill="x", pady=(16, 0))
    ttkb.Button(footer, text="确认无误，开始计算", bootstyle="primary",
                command=_accept, width=20).pack(side="right")
    ttkb.Button(footer, text="返回修正", bootstyle="secondary-outline",
                command=_reject, width=12).pack(side="right", padx=(0, 8))
    ttk.Label(footer, text="（本步骤不可跳过）",
              foreground="#6c757d").pack(side="left")

    dialog.protocol("WM_DELETE_WINDOW", _reject)   # 关窗 = 返回修正，不算确认

    dialog.update_idletasks()
    width, height = 860, 660
    x = parent.winfo_toplevel().winfo_rootx() + (
        parent.winfo_toplevel().winfo_width() - width
    ) // 2
    y = parent.winfo_toplevel().winfo_rooty() + (
        parent.winfo_toplevel().winfo_height() - height
    ) // 3
    dialog.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")
    dialog.minsize(720, 520)

    dialog.grab_set()
    dialog.focus_set()
    dialog.wait_window()
    return result["ok"]
