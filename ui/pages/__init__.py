"""六个页面。

     analysis_page      📊 智育分析     ← 唯一接真逻辑
     profile_page       🎯 侧写辅导     ← 核心新增（三层）
     archive_page       📈 成长档案     占位
     scholarship_page   💰 奖学金测算   纯壳，零逻辑
     settings_page      ⚙️ 设置         激活码 / 导出 / 清库
     account_page       👤 个人中心

★ 命名提醒：Spec 六 把「侧写辅导」定名为 ``profile_page``，
  所以"个人中心"是 :mod:`ui.pages.account_page`。别搜错。
"""

from ui.pages.account_page import AccountPage
from ui.pages.analysis_page import AnalysisPage
from ui.pages.archive_page import ArchivePage
from ui.pages.profile_page import ProfilePage
from ui.pages.scholarship_page import ScholarshipPage
from ui.pages.settings_page import SettingsPage

__all__ = [
    "AccountPage",
    "AnalysisPage",
    "ArchivePage",
    "ProfilePage",
    "ScholarshipPage",
    "SettingsPage",
]
