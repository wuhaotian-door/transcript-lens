"""学年成绩与德育表的回归测试，仅使用模拟记录。"""

import pandas as pd
import pytest

from core.crypto_store import CryptoStore
from core.grade_import import grade_card_scope, parse_grade_frame
from core.import_workers import ImportApplyError, ImportFormatError, apply_plan, plan_import
from core.scholarship import (
    ClassRecord,
    Quota,
    compute_scholarship,
    load_moral,
    load_quota,
    load_year_class,
    save_class,
    save_moral,
    save_quota,
)


USER = "学年回归"
CLASS = "T-1"
OTHER = "T-2"
KEY = "sid:1"


@pytest.fixture
def store(tmp_path):
    vault = CryptoStore(root=tmp_path)
    vault.register(USER, "Test135790")
    return vault


def student(sid, score, course, credit=1):
    return {
        "key": f"sid:{sid}", "student_id": sid, "student": "同学" + sid,
        "academic": score,
        "courses": [{"name": course, "score": score, "credit": credit, "category": "必修"}],
    }


def moral_sheet():
    return pd.DataFrame([
        [f"学院 {CLASS} 班学生2025--2026学年品行诚信与素质测评", None, None, None, None, None, None],
        ["序号", "学号", "姓名", "基本分", "加分小计", "扣分小计", "合计总分（≦100分）"],
        [None, None, None, None, None, None, None],
        [1, "1", "同学1", 75, "10.25", -1.5, 83.75],
        [2, "2", "同学2", 75, 30, 0, 100],
        [3, "3", "同学3", 75, 10, 0, 89],
        ["班长签名", None, None, None, None, None, None],
    ])


def test_earned_credit_check_for_grade_card_keeps_failed_course_in_analysis():
    raw = pd.DataFrame([
        [f"大学2025-2026-2学期{CLASS}班班级成绩卡", None, None, None],
        ["学号", "姓名", "数学,必修,学分 4,学时 64,2025-2026-2", "所得学分"],
        ["1", "同学1", 50, 0],
    ])
    parsed, anchor = parse_grade_frame(raw)
    assert parsed.valid_rows[0].score_list == [50]
    assert len(anchor.passed) == 1
    scope = grade_card_scope(raw, parsed)
    assert (scope.class_name, scope.school_year, scope.half) == (CLASS, "2025-2026", "下")


def test_apply_official_moral_scores_matches_subtotals_and_isolates_year(store, tmp_path):
    save_class(store, USER, ClassRecord(CLASS, "下半学年", "大二下", [
        student("1", 90, "数学"), student("2", 80, "数学"),
    ], "2025-2026"))
    plan = plan_import(tmp_path / "moral.xlsx", frame=moral_sheet(), known_classes=[CLASS],
                       default_class=CLASS, year="大二")
    assert plan.total_rows == 3
    assert "不一致" in plan.failures[0].reason
    assert plan.school_year == "2025-2026"
    assert [r.score for r in plan.rows] == [83.75, 100]
    assert load_moral(store, USER, CLASS, year="大二") == {}
    result = apply_plan(store, USER, plan, error_dir=tmp_path)
    assert (result.succeeded, result.failed, result.skipped) == (2, 1, 0)
    assert load_moral(store, USER, CLASS, year="大二") == {KEY: 83.75, "sid:2": 100}
    assert load_moral(store, USER, CLASS, year="大一") == {}
    assert load_moral(store, USER, OTHER, year="大二") == {}
    assert load_moral(store, USER, CLASS) == {}


def test_moral_table_rejects_wrong_class_or_calendar_year(store, tmp_path):
    save_class(store, USER, ClassRecord(CLASS, "", "大一", [student("1", 90, "数学")], "2024-2025"))
    target = tmp_path / "moral.xlsx"
    with pytest.raises(ImportFormatError, match="表头属于"):
        plan_import(target, frame=moral_sheet(), known_classes=[CLASS, OTHER],
                    default_class=OTHER, year="大一")
    plan = plan_import(target, frame=moral_sheet(), known_classes=[CLASS],
                       default_class=CLASS, year="大一")
    with pytest.raises(ImportApplyError, match="2024-2025"):
        apply_plan(store, USER, plan, error_dir=tmp_path)
    assert load_moral(store, USER, CLASS, year="大一") == {}


def test_full_year_ranking_requires_both_halves_and_uses_course_credits(store):
    save_class(store, USER, ClassRecord(CLASS, "上半学年", "大二上", [
        student("1", 80, "数学", 4), student("2", 99, "数学", 4),
    ], "2025-2026"))
    partial = load_year_class(store, USER, CLASS, "大二")
    assert partial.annual_complete is False
    with pytest.raises(ValueError, match="完整学年"):
        compute_scholarship(partial, {KEY: 83.75})
    save_class(store, USER, ClassRecord(CLASS, "下半学年", "大二下", [
        student("1", 100, "英语", 1),
    ], "2025-2026"))
    save_class(store, USER, ClassRecord(CLASS, "大一", "大一", [student("1", 60, "大学语文")],
                                       "2024-2025"))
    save_class(store, USER, ClassRecord(OTHER, "另一班", "大二", [student("1", 90, "英语")],
                                       "2025-2026"))
    save_moral(store, USER, CLASS, {KEY: 83.75}, year="大二", school_year="2025-2026")
    save_moral(store, USER, CLASS, {KEY: 90}, year="大一")
    save_moral(store, USER, OTHER, {KEY: 99}, year="大二")
    save_quota(store, USER, CLASS, Quota(1, 0, 0), year="大二")
    year = load_year_class(store, USER, CLASS, "大二")
    assert year.annual_complete
    ranks = compute_scholarship(year, load_moral(store, USER, CLASS, year="大二"),
                                load_quota(store, USER, CLASS, year="大二"))
    assert ranks[0].academic == 84
    assert ranks[0].final == pytest.approx(83.925)
    assert ranks[0].moral_text == "83.75"
    assert ranks[0].rank == 1 and ranks[0].award == "一等"
    assert ranks[1].rank is None and "个人学年成绩不完整" in ranks[1].note
    assert load_year_class(store, USER, CLASS, "大一").students[0]["academic"] == 60
    assert load_year_class(store, USER, OTHER, "大二").students[0]["academic"] == 90
    assert load_quota(store, USER, CLASS, year="大一").total == 0


def test_rejects_saving_same_calendar_year_to_two_grade_years(store):
    save_class(store, USER, ClassRecord(CLASS, "", "大二下", [], "2025-2026"))
    with pytest.raises(ValueError, match="已归入大二"):
        save_class(store, USER, ClassRecord(CLASS, "", "大一", [], "2025-2026"))
    with pytest.raises(ValueError, match="已登记"):
        save_class(store, USER, ClassRecord(CLASS, "", "大二上", [], "2024-2025"))
    assert load_year_class(store, USER, CLASS, "大二").term == "大二下"
