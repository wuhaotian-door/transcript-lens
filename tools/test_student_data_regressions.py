"""针对原始导出格式和删除学生控制面的回归用例。"""

from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pandas as pd
import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

from core import student_deletion
from core.archival import Archive, CourseSnapshot, SemesterRecord, StudentArchive, archive_key, build_archive
from core.calculator import calculate_many
from core.crypto_store import CryptoError, CryptoStore, StoreCorruptError
from core.database import StudentStore
from core.grade_import import parse_grade_frame
from core.scholarship import ClassRecord, save_class
from core.user_store import load_user_json, save_user_json
from ui.pages.analysis_page import grade_history_records
from ui.pages import archive_page
from ui.pages.profile_shared import student_semester_rows


def test_raw_headers_preserve_each_students_course_score():
    long_name = "以完整的课程名称验证教务导出在自动识别时可以保留超过四十字的标题"
    long_name = long_name + "与各种补充说明（第二部分）"
    frame = pd.DataFrame([
        ["学号", "姓名", f"{long_name},必修,学分 2,学时 32,2025-2026-2",
         f"{long_name},限选,学分 3,学时 48,2025-2026-2",
         "其它课,选修,学分 1,学时 16,2025-2026-2", "课程门数", "所得学分"],
        ["001", "同名测试", 72, 93, None, 2, 3],
        ["002", "同名测试", 85, None, 0, 2, 3],
    ])
    parsed, anchor = parse_grade_frame(frame)
    assert [c.name for c in parsed.courses] == [long_name, long_name, "其它课"]
    assert not anchor.exceeded
    records = grade_history_records(parsed, calculate_many(parsed))
    assert {(r["student_id"], r["course_name"]): (r["score"], r["credit"]) for r in records} == {
        ("001", long_name): (93, 3),
        ("001", "其它课"): (None, 1),
        ("002", long_name): (85, 2),
        ("002", "其它课"): (0, 1),
    }
    archive = build_archive(parsed, "大一上")
    first = archive.students[archive_key("001")].records[0]
    assert [(c.name, c.score) for c in first.courses] == [(long_name, 93), ("其它课", None)]


@pytest.fixture
def student_files(tmp_path: Path):
    store = CryptoStore(root=tmp_path, iterations=1000)
    user = "test-delete"
    store.register(user, "Synthetic-pass-73821")
    StudentStore(store=store, username=user).upsert_import_many([("001", "同名"), ("002", "同名")])
    files = {
        "grade_history.json.enc": {"rows": [{"student_id": sid} for sid in ("001", "sid:001", "002")]},
        "student_profiles.json.enc": {"profiles": {"sid:001": {"side_text": "删除"}, "002": {}}},
        "archive.json.enc": {
            "students": {archive_key(sid): {"student": "同名", "records": [{"student_id": sid}]}
                         for sid in ("001", "002")},
            "imported_terms": {archive_key(sid): ["大一上"] for sid in ("001", "002")},
        },
        "class_A.json.enc": {"students": [{"student_id": sid} for sid in ("001", "002")]},
        "class_A_moral.json.enc": {"scores": {"sid:001": 91, "sid:002": 52}},
        "intentions.json.enc": {"intentions": {"sid:001": "删除", "sid:002": "保留"}},
        "cache/chat_history_001.json.enc": {"turns": []},
        "semester_side/001/term.json.enc": {"text": "删除"},
    }
    paths = {name: save_user_json(store, user, name, data) for name, data in files.items()}
    paths["students.json.enc"] = paths["grade_history.json.enc"].with_name("students.json.enc")
    return store, user, paths


def test_delete_cleans_bare_and_prefixed_keys_without_touching_same_name(student_files):
    store, user, paths = student_files
    student_deletion.purge_student(store, user, "sid:001")
    assert load_user_json(store, user, "grade_history.json.enc")["rows"] == [{"student_id": "002"}]
    assert load_user_json(store, user, "class_A.json.enc")["students"] == [{"student_id": "002"}]
    assert set(load_user_json(store, user, "student_profiles.json.enc")["profiles"]) == {"002"}
    assert set(load_user_json(store, user, "archive.json.enc")["imported_terms"]) == {archive_key("002")}
    assert load_user_json(store, user, "class_A_moral.json.enc")["scores"] == {"sid:002": 52}
    assert load_user_json(store, user, "intentions.json.enc")["intentions"] == {"sid:002": "保留"}
    assert StudentStore(store=store, username=user).names() == {"sid:002": "同名"}
    assert all(not path.exists() for name, path in paths.items() if "/" in name)
    student_deletion.purge_student(store, user, "001")


def test_failed_roster_write_restores_every_changed_file(student_files, monkeypatch):
    store, user, paths = student_files
    before = {name: path.read_bytes() for name, path in paths.items()}
    write = student_deletion.save_user_json

    def fail_on_roster(store, username, filename, payload):
        if filename == "students.json.enc":
            raise PermissionError("模拟写入被拒绝")
        return write(store, username, filename, payload)

    monkeypatch.setattr(student_deletion, "save_user_json", fail_on_roster)
    with pytest.raises(CryptoError, match="已恢复本次改动"):
        student_deletion.purge_student(store, user, "001")
    assert {name: path.read_bytes() for name, path in paths.items()} == before


def test_corrupt_related_ciphertext_does_not_delete_any_file(student_files):
    store, user, paths = student_files
    paths["class_A_moral.json.enc"].write_bytes(b"corrupt")
    before = {name: path.read_bytes() for name, path in paths.items()}
    with pytest.raises(StoreCorruptError):
        student_deletion.purge_student(store, user, "001")
    assert {name: path.read_bytes() for name, path in paths.items()} == before


def test_cancel_while_preparing_leaves_all_ciphertext_untouched(student_files, monkeypatch):
    store, user, paths = student_files
    before = {name: path.read_bytes() for name, path in paths.items()}
    control = student_deletion.StudentDeletionControl()
    original = student_deletion.load_encrypted

    def read_and_cancel(*args, **kwargs):
        payload = original(*args, **kwargs)
        assert control.cancel_before_write()
        return payload

    monkeypatch.setattr(student_deletion, "load_encrypted", read_and_cancel)
    with pytest.raises(CryptoError, match="未写入数据"):
        student_deletion.purge_student(store, user, "001", control=control)
    assert {name: path.read_bytes() for name, path in paths.items()} == before


@pytest.fixture
def delete_page(student_files, monkeypatch):
    store, user, _paths = student_files
    app = QApplication.instance() or QApplication([])
    page = archive_page.ArchivePage(store)
    page.set_user(user)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda _p, _title, message: errors.append(message))
    yield page, errors
    assert QThreadPool.globalInstance().waitForDone(3000)
    page.close()
    app.processEvents()


def _wait_until(condition, timeout=2):
    deadline = monotonic() + timeout
    while not condition():
        assert monotonic() < deadline, "删除流程没有进入预期状态"
        QApplication.processEvents()
        sleep(0.01)


def test_late_queued_delete_times_out_without_changing_data(delete_page, student_files, monkeypatch):
    page, errors = delete_page
    _store, _user, paths = student_files
    before = {name: path.read_bytes() for name, path in paths.items()}
    queued = []
    monkeypatch.setattr(archive_page, "DELETE_PREPARE_TIMEOUT", 0.08)
    monkeypatch.setattr(archive_page, "run_async", lambda _owner, fn: queued.append(fn))
    page._delete_student_hard("001", "同名")

    _wait_until(lambda: page._delete_progress is None)
    assert "本次未写入数据" in errors[0]
    queued.pop()()  # 任务随后被线程池取出，也不能开始修改数据。
    assert {name: path.read_bytes() for name, path in paths.items()} == before


def test_delete_releases_progress_if_worker_cannot_be_started(delete_page, monkeypatch):
    page, errors = delete_page

    def rejected(*_args):
        raise RuntimeError("模拟任务启动失败")

    monkeypatch.setattr(archive_page, "run_async", rejected)
    page._delete_student_hard("001", "同名")
    assert page._delete_progress is None
    assert errors == ["删除未完成，请检查数据目录后重试。"]


def test_delete_reports_success_before_best_effort_audit(delete_page, student_files, monkeypatch):
    page, errors = delete_page
    store, user, _paths = student_files
    audit_started = Event()
    release_audit = Event()

    def slow_audit(*_args):
        audit_started.set()
        release_audit.wait(3)

    monkeypatch.setattr(archive_page, "audit_student_deletion", slow_audit)
    try:
        page._delete_student_hard("001", "同名")
        _wait_until(lambda: audit_started.is_set() and page._delete_progress is None)
        assert page._note.text() == "删除完成。"
        assert not errors and not StudentStore(store=store, username=user).has("001")
    finally:
        release_audit.set()


def test_slow_write_keeps_result_pending_until_store_finishes(delete_page, monkeypatch):
    page, errors = delete_page
    release_write = Event()

    def slow_write(*_args, control):
        control.begin_write()
        release_write.wait(3)
        return True

    monkeypatch.setattr(archive_page, "purge_student", slow_write)
    monkeypatch.setattr(archive_page, "DELETE_PREPARE_TIMEOUT", 0.12)
    try:
        page._delete_student_hard("001", "同名")
        _wait_until(lambda: "比平时长" in page._delete_progress._message.text())
        assert not errors
        release_write.set()
        _wait_until(lambda: page._delete_progress is None)
        assert page._note.text() == "删除完成。"
    finally:
        release_write.set()


def test_roster_uses_book_class_and_recovers_legacy_archive_class(delete_page, student_files):
    page, _errors = delete_page
    store, user, _paths = student_files
    StudentStore(store=store, username=user).update("001", class_name="CS-NEW")
    save_class(store, user, ClassRecord("CS-OLD", students=[{"student_id": "001"}, {"student_id": "003"}]))
    archive = Archive()
    for sid, name in (("001", "同名"), ("003", "同名")):
        key = archive_key(sid)
        archive.students[key] = StudentArchive(
            key, name, [SemesterRecord("大一上", name, sid, [
                CourseSnapshot("示例课程", 3, "必修", 80 if sid == "001" else 91),
            ])],
        )
    save_user_json(store, user, "archive.json.enc", archive.as_dict())
    page.reload()
    page._search.setText("CS-NEW")
    assert page.visible_count == 1
    assert page._select_student_by_id("001") and page._current.student_id == "001"
    page._search.setText("CS-OLD")
    assert page.visible_count == 1
    assert page._select_student_by_id("003") and page._current.student_id == "003"
    assert student_semester_rows(store, user, "003", "sid:003")["大一上"][0]["score"] == 91
