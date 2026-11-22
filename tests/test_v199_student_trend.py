"""v1.9.9 名单排序和成绩趋势刻度的回归。"""

from PySide6.QtWidgets import QApplication
import pytest

from ui.pages.profile_shared import student_order_key
from ui.widgets.grade_trend import GradeTrendChart


def test_student_order_uses_numeric_id_then_pinyin():
    roster = [
        ("", "周莉"), ("10", "陈同"), ("2", "赵六"), ("2", "李四"), ("", "王五"),
    ]
    assert sorted(roster, key=lambda student: student_order_key(*student)) == [
        ("2", "李四"), ("2", "赵六"), ("10", "陈同"), ("", "王五"), ("", "周莉"),
    ]


def test_grade_trend_keeps_half_point_in_scale_and_missing_point_blank():
    app = QApplication.instance() or QApplication([])
    chart = GradeTrendChart()
    try:
        chart.resize(480, 200)
        chart.set_series([("大一上", 80.0), ("大一下", 80.5), ("大二上", None), ("大二下", 82.0)])
        app.processEvents()
        plot, points = chart._geometry()
        assert points[2] is None
        assert points[0].y() - points[1].y() == pytest.approx(plot.height() * 0.005)
        assert "80.50 分" in chart.accessibleDescription()
        assert not chart.grab().isNull()
    finally:
        chart.close()
