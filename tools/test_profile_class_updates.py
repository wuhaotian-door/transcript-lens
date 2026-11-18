"""覆盖这次侧写、学年口径和班级批量删除的边界。"""

from types import SimpleNamespace

import pytest

from core import student_deletion
from core.archival import CourseSnapshot, SemesterRecord, StudentArchive, archive_key
from core.crypto_store import CryptoError, CryptoStore
from core.database import GradeHistoryDB, StudentStore
from core.scholarship import (
    ClassRecord, Quota, compute_scholarship, load_year_class, save_class, save_moral, save_quota,
)
from core.user_store import load_user_json, save_user_json
from ui.pages.profile_shared import grade_rows_from_records


USER = "班级回归"
TARGET = "QA-A"
OTHER = "QA-A-1"


@pytest.fixture
def store(tmp_path):
    vault = CryptoStore(root=tmp_path)
    vault.register(USER, "Test135790")
    return vault


def course(name, category, score, credit=1):
    return {"name": name, "category": category, "score": score, "credit": credit}


def student(sid, *courses):
    return {"student_id": sid, "key": f"sid:{sid}", "academic": 96, "courses": list(courses)}


def test_annual_scope_excludes_electives_before_deduplication(store):
    first = student("1", course("数学", "必修", 80, 4), course("数学", "选修", 100, 5))
    second = student("1", course("英语", "限选", 90, 2), course("选修低分", "选修", 50, 10))
    for term, row in (("大二上", first), ("大二下", second)):
        save_class(store, USER, ClassRecord(TARGET, "", term, [row], "2025-2026"))
    combined = load_year_class(store, USER, TARGET, "大二")
    assert combined.students[0]["academic"] == pytest.approx(500 / 6)
    assert [c["name"] for c in combined.students[0]["courses"]] == ["数学", "英语"]
    assert compute_scholarship(combined, {"sid:1": 85})[0].final == pytest.approx(500 / 6 * 0.7 + 85 * 0.3)

    # 完整学年快照也按课程回算，不能沿用旧版本记下的全口径均分。
    full = student("1", *first["courses"], *second["courses"])
    save_class(store, USER, ClassRecord(TARGET, "", "大二", [full], "2025-2026"))
    assert load_year_class(store, USER, TARGET, "大二").students[0]["academic"] == pytest.approx(500 / 6)


def test_profile_filters_unselected_electives_but_keeps_zero_and_valid_scores():
    modules = [
        course("未选课", "选修", None), course("空白课", "选修", "/"),
        course("缺考课", "选修", 0), course("已选课", "选修", 91),
        course("待录必修", "必修", None),
    ]
    record = SimpleNamespace(term="大二上", courses=[SimpleNamespace(**item) for item in modules])
    archive = SimpleNamespace(records=[record])
    assert [row["course"] for row in grade_rows_from_records(archive)] == ["缺考课", "已选课", "待录必修"]


def _seed_classes(store):
    book = StudentStore(store=store, username=USER)
    book.add("1", "甲", class_name=TARGET)
    book.add("2", "乙", class_name=OTHER)
    book.add("3", "丙", class_name=TARGET)
    save_class(store, USER, ClassRecord(TARGET, "", "大一", [student("1"), student("3")]))
    save_class(store, USER, ClassRecord(TARGET, "", "大二下", [student("1"), student("3")]))
    save_class(store, USER, ClassRecord(OTHER, "", "大二", [student("2"), student("3")]))
    save_moral(store, USER, TARGET, {"sid:1": 90}, year="大一")
    save_moral(store, USER, TARGET, {"sid:1": 81})
    save_quota(store, USER, TARGET, Quota(1, 0, 0), year="大二")
    save_moral(store, USER, OTHER, {"sid:3": 92}, year="大二")
    keys = [f"sid:{i}" for i in (1, 2, 3)]
    save_user_json(store, USER, GradeHistoryDB.filename, {"rows": [{"student_id": key} for key in keys]})
    save_user_json(store, USER, student_deletion.ARCHIVE_PAYLOAD_FILE, {
        "students": {archive_key(key[4:]): {"student_id": key, "records": []} for key in keys},
        "imported_terms": {archive_key(key[4:]): ["大二"] for key in keys},
    })
    save_user_json(store, USER, "intentions.json.enc", {"intentions": dict.fromkeys(keys, "就业")})
    return book


def test_delete_class_preserves_other_classes_and_shared_students(store):
    book = _seed_classes(store)
    deleted, sids = student_deletion.purge_class(store, USER, TARGET)
    assert deleted and sids == ("1",)
    assert not book.has("1")
    assert book.get("3")["class_name"] == ""
    assert book.get("2")["class_name"] == OTHER
    assert load_year_class(store, USER, TARGET, "大一") is None
    assert load_year_class(store, USER, TARGET, "大二") is None
    assert load_year_class(store, USER, OTHER, "大二").size == 2
    names = student_deletion.list_user_json(store, USER)
    assert not any(name.startswith(f"class_{TARGET}_") for name in names)
    assert f"class_{OTHER}_y2_moral.json.enc" in names
    history = load_user_json(store, USER, GradeHistoryDB.filename)["rows"]
    assert [row["student_id"] for row in history] == ["sid:2", "sid:3"]
    archive = load_user_json(store, USER, student_deletion.ARCHIVE_PAYLOAD_FILE)
    assert set(archive["students"]) == {archive_key("2"), archive_key("3")}


def test_delete_class_rolls_back_whole_batch_on_roster_write_error(store, monkeypatch):
    _seed_classes(store)
    root = student_deletion._user_dir(store, USER)
    before = {p.name: p.read_bytes() for p in root.glob("*.enc")}
    original = save_user_json

    def fail_roster(vault, user, filename, payload):
        if filename == StudentStore.filename:
            raise OSError("模拟写盘失败")
        return original(vault, user, filename, payload)

    monkeypatch.setattr(student_deletion, "save_user_json", fail_roster)
    with pytest.raises(CryptoError, match="已恢复本次改动"):
        student_deletion.purge_class(store, USER, TARGET)
    assert {p.name: p.read_bytes() for p in root.glob("*.enc")} == before


def test_overview_shows_all_terms_and_returns_from_semester_detail(store):
    from PySide6.QtWidgets import QApplication
    from ui.pages.archive_page import ArchivePage

    app = QApplication.instance() or QApplication([])
    page = ArchivePage(store)
    page.set_user(USER)
    sid = "1"
    name = "示例"
    student = StudentArchive(archive_key(sid, name), name, roster_id=sid, records=[
        SemesterRecord("大一上", name, sid, [CourseSnapshot("数学", 4, "必修", 80)]),
        SemesterRecord("大一下", name, sid, [CourseSnapshot("物理", 1, "限选", 95)]),
    ])
    save_user_json(store, USER, student_deletion.ARCHIVE_PAYLOAD_FILE, {
        "students": {student.key: student.as_dict()},
    })
    page.show_student(student)
    page.open_current_profile()
    overview = page._overview_page
    assert page._detail_stack.currentWidget() is overview
    assert overview._terms == ["大一上", "大一下"]
    assert overview._metrics[1].text() == "83.00"
    assert overview._metrics[3].text() == "+15.00"
    overview._table.selectRow(1)
    overview._semester.click()
    assert page._semester_page.current_semester() == "大一下"
    assert page._detail_stack.currentWidget() is page._semester_page
    page._semester_page._back_button.click()
    assert page._detail_stack.currentWidget() is overview
    assert overview._table.currentRow() == 1
    page.close()
    app.processEvents()
