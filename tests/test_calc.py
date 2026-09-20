"""核心算术回归测试：纯内存 dict 构造，不读 xlsx、不碰加密/UI/AI。"""

from __future__ import annotations

import pytest

from core.calculator import (
    DEFAULT_SCOPE,
    CalculationScope,
    calculate_student,
    dedupe_courses,
    merge_semesters,
    weighted_average,
)
from core.parser import Category, Course, ExamRow, to_float

SPEC = [
    ("英语", "必修", 2),
    ("高数", "必修", 5),
    ("线代", "必修", 3),
    ("数据结构", "限选", 4),
    ("计网", "限选", 3),
    ("数据库", "限选", 2),
    ("大物", "选修", 3),
    ("音乐", "选修", 1),
]
COURSES = [
    Course(name=n, category=Category(c), credit=float(k), col_index=i)
    for i, (n, c, k) in enumerate(SPEC)
]

FAKE = {
    "张三": [88, 91, 76, 85, 79, 90, 82, None],
    "李四": [72, 65, 58, 74, 61, 70, 68, 88],
    "王五": [95, "85.000000", None, 90, 92, None, 0, 77],
    "赵六": [70, 0, 72, 88, 66, 84, None, 91],
    "钱八": [None] * 8,
}
ANCHOR = {"张三": 22.0, "李四": 23.0, "王五": 18.0, "赵六": 20.0, "钱八": 0.0}


def _result(student, scope=DEFAULT_SCOPE, courses=COURSES, scores=None):
    raw = FAKE[student] if scores is None else scores
    row = ExamRow(row_index=0, student=student, score_list=[to_float(s) for s in raw])
    return calculate_student(row, list(courses), scope)


def _avg(student, scope=DEFAULT_SCOPE):
    return _result(student, scope).weighted_average


def test_empty_is_skip():
    r = _result("王五")
    assert r.total_credit == 14.0
    assert r.weighted_sum == 1251.0
    assert r.weighted_average == pytest.approx(1251 / 14)
    assert set(r.skipped_blank) == {"线代", "数据库"}
    assert {c.name for c in r.counted_courses}.isdisjoint({"线代", "数据库"})


def test_zero_counts():
    r = _result("赵六")
    assert r.zero_scores == ["高数"]
    assert r.total_credit == 19.0
    assert r.weighted_sum == 1074.0
    assert r.weighted_average == pytest.approx(1074 / 19)
    assert "高数" in {c.name for c in r.counted_courses}
    assert r.weighted_average < 60.0


def test_text_float():
    assert to_float("85.000000") == 85.0
    assert to_float("85.000000") != 0.0
    r = _result("王五")
    gaoshu = next(c for c in r.course_scores if c.name == "高数")
    assert gaoshu.score == 85.0 and gaoshu.counted
    assert gaoshu.contribution == pytest.approx(425.0)
    assert r.invalid_scores == []
    assert r.weighted_sum == 1251.0


def test_default_scope():
    assert DEFAULT_SCOPE is CalculationScope.REQUIRED_LIMITED
    expected = {
        CalculationScope.ALL: 1862 / 22,
        CalculationScope.REQUIRED_LIMITED: 1616 / 19,
        CalculationScope.REQUIRED_ONLY: 859 / 10,
    }
    for scope, value in expected.items():
        assert _avg("张三", scope) == pytest.approx(value), scope
    r = _result("张三")
    assert r.total_credit == 19.0
    assert {d.name for d in r.excluded_by_scope} == {"大物", "音乐"}
    assert r.weighted_average == pytest.approx(1616 / 19)
    assert r.weighted_average != pytest.approx(1862 / 22)


def test_same_course_take_higher():
    first = Course("高数", Category.REQUIRED, 5.0, 0)
    again = Course("高数", Category.REQUIRED, 5.0, 1)
    pair = [(first, 78.0), (again, 88.0)]
    report = dedupe_courses(pair)
    assert len(report.course_scores) == 1
    winner = report.course_scores[0]
    assert (winner.score, winner.credit, winner.source_count) == (88.0, 5.0, 2)
    assert report.deduped == ["高数"]
    assert weighted_average(pair) == 88.0
    assert weighted_average(pair) != 83.0


def test_semester_merge_not_double_avg():
    upper = [
        Course("高数", Category.REQUIRED, 5.0, 0),
        Course("英语", Category.REQUIRED, 2.0, 1),
    ]
    lower = [Course("线代", Category.REQUIRED, 3.0, 0)]
    a = _result("A", courses=upper, scores=[80, 90])
    b = _result("B", courses=lower, scores=[70])
    assert a.weighted_average == pytest.approx(580 / 7)
    assert b.weighted_average == pytest.approx(70.0)
    credit, total, merged = merge_semesters(a, b)
    assert credit == 10.0
    assert total == 790.0
    assert merged == pytest.approx(79.0)
    naive = (a.weighted_average + b.weighted_average) / 2
    assert naive == pytest.approx(76.42857142857143)
    assert merged != pytest.approx(naive)


def test_all_pass():
    for scope in CalculationScope:
        r = _result("钱八", scope)
        assert r.weighted_average is None
        assert r.total_credit == 0.0
        assert r.weighted_sum == 0.0
        assert r.zero_scores == []
        assert r.counted_courses == []
    assert weighted_average([(c, None) for c in COURSES]) is None


def test_anchor_credits():
    for student, expected in ANCHOR.items():
        scores = [to_float(s) for s in FAKE[student]]
        earned = sum(
            c.credit for c, s in zip(COURSES, scores) if s is not None and s >= 0.0
        )
        assert earned == expected, student
    assert sum(c.credit for c in COURSES) == 23.0
