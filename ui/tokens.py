"""transcript-lens · 设计规范常量 (Design Tokens)

根据 UI_REVIEW_v1.9.3 §6.1 固化。
本模块为纯常量、零 Qt import，用于开屏、欢迎页、各页面以及 QSS 间统一标准。
"""

from __future__ import annotations

# --- 布局与尺寸 ---
PAGE_PAD: tuple[int, int, int, int] = (22, 20, 22, 20)
CARD_PAD: tuple[int, int, int, int] = (20, 16, 20, 16)
CARD_GAP: int = 14
CARD_INNER_GAP: int = 10

CONTROL_H: int = 38
CONTROL_H_SM: int = 34

# --- 圆角 ---
RADIUS_CTRL: int = 8
RADIUS_CARD: int = 10
RADIUS_WINDOW: int = 12

# --- 字阶 ---
FONT_XS: int = 11        # BrandSub / CountPill / ListNote
FONT_SM: int = 12        # Tag / WindowTitle
FONT_BASE: int = 13      # 正文基线 / 输入框 / 按钮 / 提示
FONT_MD: int = 14        # H3 / NavButton
FONT_LG: int = 17        # H2 / BrandTitle
FONT_XL: int = 22        # 登录品牌大字
FONT_2XL: int = 26       # H1 页面主标题
FONT_DISPLAY: int = 30   # 欢迎语大字

# --- 色板：品牌蓝 ---
COLOR_PRIMARY: str = "#3B82F6"
COLOR_PRIMARY_HOVER: str = "#2F76E8"
COLOR_PRIMARY_PRESSED: str = "#2563EB"
COLOR_PRIMARY_LIGHT: str = "#EFF6FF"
COLOR_PRIMARY_BORDER: str = "#BFDBFE"

# --- 色板：语义反馈 ---
COLOR_DANGER: str = "#EF4444"
COLOR_DANGER_TEXT: str = "#B91C1C"
COLOR_SUCCESS: str = "#22C55E"
COLOR_WARNING: str = "#F59E0B"
COLOR_WARNING_TEXT: str = "#92400E"

# --- 色板：中性文字 ---
COLOR_INK: str = "#1F2430"
COLOR_INK_HEADING: str = "#0F172A"
COLOR_INK_MUTED: str = "#64748B"
COLOR_INK_SUBTLE: str = "#94A3B8"

# --- 色板：边框与分割线 ---
COLOR_LINE: str = "#E2E8F0"
COLOR_LINE_LIGHT: str = "#EEF2F7"
COLOR_LINE_DARK: str = "#CBD5E1"

# --- 色板：底色与表面 ---
COLOR_SURFACE: str = "#FFFFFF"
COLOR_SURFACE_ALT: str = "#F8FAFC"
COLOR_PAGE_BG: str = "#F1F5F9"
COLOR_SIDEBAR_BG: str = "#1E293B"

# --- 头像色板（8 色封闭系统） ---
AVATAR_COLORS: tuple[str, ...] = (
    "#3B82F6", "#0EA5E9", "#8B5CF6", "#EC4899",
    "#F59E0B", "#10B981", "#6366F1", "#14B8A6",
)
