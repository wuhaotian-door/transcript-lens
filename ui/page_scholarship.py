"""奖学金测算页 —— **纯占位壳**。

本页刻意不 import 任何 core 模块，也没有一行逻辑：
综测/德育换算规则还没定，先摆出字段形状让人看见"将来会长这样"，
但**不给出任何看起来像结果的数字**。

红线 1 相关：这里不会有任何"估算"，因为估算就等于编数字。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import ttkbootstrap as ttkb

__all__ = ["build_scholarship_page"]

#: 将来要填的字段（现在全是灰的）
DISABLED_FIELDS: tuple[str, ...] = (
    "综测分",
    "专业排名",
    "一等奖线",
    "二等奖线",
    "三等奖线",
)

_TITLE_FONT = ("Microsoft YaHei UI", 16, "bold")
_MUTED = "#6c757d"


def build_scholarship_page(parent: tk.Misc) -> ttk.Frame:
    """搭出奖学金测算页（灰掉的占位），返回它的根 Frame。"""
    frame = ttk.Frame(parent, padding=20)

    ttk.Label(
        frame, text="🎓  奖学金测算（即将支持）", font=_TITLE_FONT,
    ).pack(anchor="w")
    ttk.Label(
        frame,
        text="下面这些字段是将来要填的，本版本一律不可编辑。",
        foreground=_MUTED,
    ).pack(anchor="w", pady=(4, 0))

    form = ttk.Labelframe(frame, text="参数（未启用）", padding=16)
    form.pack(fill="x", pady=(18, 0))

    for label in DISABLED_FIELDS:
        row = ttk.Frame(form)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=12, anchor="w").pack(side="left")
        # 灰掉的 Entry 里用 StringVar 显示占位符 —— 直接往 disabled 的
        # Entry 里 insert 是不生效的（试过）。
        ttk.Entry(
            row, textvariable=tk.StringVar(value="—"), width=24, state="disabled",
        ).pack(side="left")

    ttk.Separator(frame).pack(fill="x", pady=(22, 14))

    ttk.Label(
        frame,
        text="当前版本仅做智育加权，综测/德育换算将在后续版本开放",
        foreground=_MUTED,
    ).pack(anchor="w")

    ttkb.Button(
        frame, text="开始测算", bootstyle="secondary-outline",
        state="disabled", width=16,
    ).pack(anchor="w", pady=(14, 0))

    return frame
