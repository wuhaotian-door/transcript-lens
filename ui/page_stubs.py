"""档案 / 谈话提纲 / 设置 / 个人中心 —— 本次全部是占位页。

Spec 第 8 章的清单里这四页各有内容，但本次重画的范围只到"侧边栏点得到、
能切换、不报错"。所以这里**只有一句「开发中」**，不摆假控件 ——
摆一堆点了没反应的按钮比空白更让人困惑。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

__all__ = [
    "build_archive_page",
    "build_outline_page",
    "build_profile_page",
    "build_settings_page",
]

_TITLE_FONT = ("Microsoft YaHei UI", 16, "bold")
_MUTED = "#6c757d"


def _stub(parent: tk.Misc, title: str, todo: str) -> ttk.Frame:
    """一页占位：标题 + 「开发中」+ 一句"将来放什么"。"""
    frame = ttk.Frame(parent, padding=20)
    ttk.Label(frame, text=title, font=_TITLE_FONT).pack(anchor="w")
    ttk.Label(
        frame, text="开发中", font=("Microsoft YaHei UI", 12), foreground=_MUTED,
    ).pack(anchor="w", pady=(14, 0))
    ttk.Label(frame, text=todo, foreground=_MUTED, justify="left").pack(
        anchor="w", pady=(6, 0)
    )
    return frame


def build_archive_page(parent: tk.Misc) -> ttk.Frame:
    """档案页（Spec：纵向时间轴 / 多次导入汇总）。"""
    return _stub(
        parent, "档案",
        "计划：按学号哈希纵向累计多个学期，展示总加权与趋势。",
    )


def build_outline_page(parent: tk.Misc) -> ttk.Frame:
    """谈话提纲页（Spec：三段式提纲，由 ai/local_template 本地生成）。"""
    return _stub(
        parent, "谈话提纲",
        "计划：事实 / 行动 / 对标意愿三段式，本地模板生成，不联网。",
    )


def build_settings_page(parent: tk.Misc) -> ttk.Frame:
    """设置页（Spec：改激活码 / 导出 / 清库 / AI 开关）。"""
    return _stub(
        parent, "设置",
        "计划：改激活码、导出明文与脱敏 xlsx、清库（二次确认）、AI 开关。",
    )


def build_profile_page(parent: tk.Misc) -> ttk.Frame:
    """个人中心页（计划：账户、改密码、邮箱绑定）。"""
    return _stub(
        parent, "个人中心",
        "计划：改密码、绑定/更换找回邮箱、查看自动登录状态。"
        "（恢复码已下线；邮箱找回待接入邮件 API）",
    )
