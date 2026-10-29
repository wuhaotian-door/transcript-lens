"""v1.9.1：删除成绩库 / 审计日志 / GPA 方案 1 的判据。

三块被测对象：

1. ``core.database.GradeHistoryDB.delete_record / delete_semester / clear``
   —— 删除语义（只动目标行、空库返回 0、写盘失败如实抛）
2. ``core.database.GradeAuditLog``（``audit/grade_changes.json.enc``）
   —— 自动落日志、只读查询、**日志坏掉不许拦住成绩写入**
3. ``core.database.library_inventory / clear_library`` 与 ``core.gpa``
   —— "删除成绩库"那个误报 bug 的正解 + GPA 换算表边界

★ 本文件**先 register 再写库**（AGENTS §20c：账户不存在时 ``save_user_json``
  直接抛 ``CryptoError``），每个用例一个临时 vault。
★ 与本文件同源的旧用例在 ``tests/test_grade_overwrite.py`` / ``test_profile_merge.py``
  —— 那边守覆盖/合并，这里守删除/审计/GPA，别把断言抄两遍。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# UI 用例要走 offscreen；必须在 import PySide6 之前设好（其余用例不碰 Qt）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.crypto_store import CryptoStore
from core.database import (
    ARCHIVE_PAYLOAD_FILE,
    AUDIT_FILE,
    AUDIT_MAX_ENTRIES,
    GRADE_HISTORY_FILE,
    LEGACY_GRADE_FILES,
    OP_DELETE,
    OP_INSERT,
    OP_OVERWRITE,
    PROFILE_FILE,
    TABLE_GRADE_AUDIT,
    USE_SKIP,
    GradeAuditLog,
    GradeHistoryDB,
    ProfileStore,
    clear_library,
    library_inventory,
)
from core.gpa import (
    DEFAULT_SCHEME,
    GPA_SETTINGS_FILE,
    SCHEME_STANDARD_4,
    SCHEMES,
    available_schemes,
    gpa_detail,
    gpa_of,
    grade_point,
    is_available,
    load_scheme,
    save_scheme,
    scheme_label,
    scheme_options,
)
from core.scholarship import class_filename, moral_filename, quota_filename
from core.user_store import save_user_json, user_file

REPO = Path(__file__).resolve().parents[1]

USER = "验收员"
PASSWORD = "tl-test-2026"
TERM_A = "大一上"
TERM_B = "大一下"
BATCH_1 = "20261027-1"
BATCH_2 = "20261027-2"

SID_A = "sid:20230101"
SID_B = "sid:20230102"

MATH = "高等数学"
ENGLISH = "专业英语"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> CryptoStore:
    vault = CryptoStore(root=tmp_path / "vault")
    vault.register(USER, PASSWORD)          # ★ 必须先 register（AGENTS §20c）
    return vault


@pytest.fixture()
def db(store: CryptoStore) -> GradeHistoryDB:
    return GradeHistoryDB(username=USER, store=store)


@pytest.fixture()
def profiles(store: CryptoStore) -> ProfileStore:
    return ProfileStore(username=USER, store=store)


def row(student_id: str, course: str, score: object, credit: float = 4.0) -> dict:
    return {"student_id": student_id, "course_name": course,
            "score": score, "credit": credit}


def fetch(db: GradeHistoryDB, student_id: str, course: str, semester: str):
    hits = [r for r in db.rows()
            if r["student_id"] == student_id
            and r["semester"] == semester
            and r["course_name"] == course]
    return hits[0] if hits else None


def seed(db: GradeHistoryDB) -> None:
    """两个学生、三门课：SID_A 两门、SID_B 一门（都在大一上）。"""
    db.apply(
        [row(SID_A, MATH, 88.0), row(SID_A, ENGLISH, 91.0), row(SID_B, MATH, 75.0)],
        semester=TERM_A, batch_id=BATCH_1,
    )


def audit_blob(store: CryptoStore) -> bytes:
    path = user_file(store, USER, AUDIT_FILE)
    assert path is not None and path.exists(), "审计日志文件不在"
    return path.read_bytes()


def disk_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------
# 一、删除单条 / 整学期
# ---------------------------------------------------------------------------


def test_delete_record_removes_only_the_named_course(db: GradeHistoryDB) -> None:
    seed(db)
    before = db.count()

    assert db.delete_record(SID_A, TERM_A, MATH) == 1

    assert db.count() == before - 1
    assert fetch(db, SID_A, MATH, TERM_A) is None, "要删的那条还在"
    assert fetch(db, SID_A, ENGLISH, TERM_A) is not None, "同学期别的课被误删"
    assert fetch(db, SID_B, MATH, TERM_A) is not None, "别的学生同名课被误删"

    again = GradeHistoryDB(username=USER, store=db.store)
    assert again.count() == before - 1
    assert fetch(again, SID_A, MATH, TERM_A) is None


def test_delete_record_absent_returns_zero_and_writes_nothing(db: GradeHistoryDB) -> None:
    seed(db)
    rows_before = db.rows()
    log_before = len(db.changes())

    assert db.delete_record(SID_A, TERM_B, MATH) == 0

    assert db.rows() == rows_before
    assert len(db.changes()) == log_before, "没删东西却记了日志"


def test_delete_record_rejects_blank_keys(db: GradeHistoryDB) -> None:
    with pytest.raises(ValueError):
        db.delete_record("", TERM_A, MATH)
    with pytest.raises(ValueError):
        db.delete_record(SID_A, "", MATH)
    with pytest.raises(ValueError):
        db.delete_record(SID_A, TERM_A, "")


def test_delete_semester_touches_only_that_student_and_term(db: GradeHistoryDB) -> None:
    seed(db)
    db.apply([row(SID_A, ENGLISH, 77.0)], semester=TERM_B, batch_id=BATCH_2)
    before = db.count()

    assert db.delete_semester(SID_A, TERM_A) == 2

    assert db.count() == before - 2
    assert db.semester_rows(SID_A, TERM_A) == []
    assert [r["course_name"] for r in db.semester_rows(SID_A, TERM_B)] == [ENGLISH]
    assert fetch(db, SID_B, MATH, TERM_A) is not None

    again = GradeHistoryDB(username=USER, store=db.store)
    assert again.semester_rows(SID_A, TERM_A) == []
    assert again.semester_rows(SID_A, TERM_B), "别的学期被一起删了"


def test_delete_semester_absent_returns_zero(db: GradeHistoryDB) -> None:
    seed(db)
    log_before = len(db.changes())

    assert db.delete_semester(SID_B, TERM_B) == 0
    assert db.count() == 3
    assert len(db.changes()) == log_before

    with pytest.raises(ValueError):
        db.delete_semester(SID_A, "")


# ---------------------------------------------------------------------------
# 二、clear()：空库的行为 + "清完了" ≠ "本来就没有"
# ---------------------------------------------------------------------------


def test_clear_on_empty_db_returns_zero_and_logs_nothing(db: GradeHistoryDB) -> None:
    assert db.count() == 0

    assert db.clear() == 0

    assert db.count() == 0
    assert db.changes() == []
    assert user_file(db.store, USER, AUDIT_FILE) is None or not (
        user_file(db.store, USER, AUDIT_FILE).exists()
    ), "空库 clear() 不该产生审计日志"


def test_clear_returns_row_count_and_leaves_table_empty(db: GradeHistoryDB) -> None:
    seed(db)

    assert db.clear() == 3

    assert db.count() == 0
    assert db.rows() == []
    assert GradeHistoryDB(username=USER, store=db.store).count() == 0
    # ★ 第二次就是"本来就没有"：返回值让界面分得开这两种情况
    assert db.clear() == 0


def test_clear_only_empties_grade_history(store: CryptoStore, db: GradeHistoryDB,
                                         profiles: ProfileStore) -> None:
    """clear() 是**这一张表**的事：侧写 / 班级快照一个都不许动。"""
    seed(db)
    profiles.save_side_text(SID_A, "该生动手能力强。")
    save_user_json(store, USER, class_filename("2301"), {"class_name": "2301"})

    db.clear()

    assert db.count() == 0
    assert profiles.side_text(SID_A) == "该生动手能力强。"
    assert user_file(store, USER, class_filename("2301")).exists()


# ---------------------------------------------------------------------------
# 三、审计日志：自动落、只读查、坏了不炸
# ---------------------------------------------------------------------------


def test_apply_logs_insert_per_student_without_scores(db: GradeHistoryDB,
                                                      store: CryptoStore) -> None:
    db.apply([row(SID_A, MATH, 87.5, credit=3.5), row(SID_A, ENGLISH, 91.0)],
             semester=TERM_A, batch_id=BATCH_1)

    entries = db.changes()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["operation"] == OP_INSERT
    assert entry["student_id"] == SID_A
    assert entry["semester"] == TERM_A
    assert entry["count"] == 2
    assert entry["at"], "没记操作时间"
    # ★ 一行一条的字段集合：**没有 score / credit**（审计日志不是第二份成绩库）
    assert set(entry) == {"at", "operation", "student_id", "semester",
                          "course_name", "count", "detail"}

    payload = user_file(store, USER, AUDIT_FILE).read_bytes()
    assert b"87.5" not in payload, "分数泄进审计日志了"
    assert b"3.5" not in payload, "学分泄进审计日志了"


def test_overwrite_is_logged_as_overwrite(db: GradeHistoryDB) -> None:
    """覆盖 = 删旧（overwrite）+ 写新（insert）—— 两件事都真发生了，分两条记。"""
    seed(db)
    db.apply([row(SID_A, MATH, 95.0), row(SID_A, ENGLISH, 96.0)],
             semester=TERM_A, batch_id=BATCH_2)

    entries = db.changes()[:2]                      # 这次覆盖写下的两条
    assert {e["operation"] for e in entries} == {OP_OVERWRITE, OP_INSERT}
    by_op = {e["operation"]: e for e in entries}
    assert by_op[OP_OVERWRITE]["count"] == 2, "删掉的旧行没记上"
    assert by_op[OP_INSERT]["count"] == 2, "写进去的新行没记上"
    assert all(e["student_id"] == SID_A and e["semester"] == TERM_A
               for e in entries)


def test_skip_policy_writes_no_audit_entry(db: GradeHistoryDB) -> None:
    """跳过 = 保留旧记录、这一行不导入 —— 什么都没变就不该有日志。"""
    seed(db)
    before = len(db.changes())

    result = db.apply([row(SID_A, MATH, 95.0)], semester=TERM_A,
                      batch_id=BATCH_2, policy=USE_SKIP)

    assert result["skipped"] == 1
    assert len(db.changes()) == before


def test_delete_methods_log_delete_entries(db: GradeHistoryDB) -> None:
    seed(db)

    db.delete_record(SID_A, TERM_A, MATH)
    db.delete_semester(SID_A, TERM_A)

    newest = db.changes()
    assert newest[0]["operation"] == OP_DELETE
    assert newest[0]["student_id"] == SID_A
    assert newest[0]["semester"] == TERM_A
    assert newest[0]["count"] == 1          # delete_semester 剩一门
    assert newest[1]["operation"] == OP_DELETE
    assert newest[1]["course_name"] == MATH
    assert newest[1]["count"] == 1

    assert db.clear() == 1
    assert db.changes()[0]["operation"] == OP_DELETE
    assert db.changes()[0]["count"] == 1    # 最后那一门


def test_changes_filters_and_newest_first(db: GradeHistoryDB) -> None:
    db.apply([row(SID_A, MATH, 88.0)], semester=TERM_A, batch_id=BATCH_1)
    db.apply([row(SID_B, MATH, 75.0)], semester=TERM_B, batch_id=BATCH_2)
    db.delete_record(SID_A, TERM_A, MATH)

    everything = db.changes()
    assert [e["operation"] for e in everything] == [OP_DELETE, OP_INSERT, OP_INSERT]
    assert everything[1]["student_id"] == SID_B, "不是最新在前"

    only_a = db.changes(student_id=SID_A)
    assert len(only_a) == 2
    assert {e["student_id"] for e in only_a} == {SID_A}

    assert len(db.changes(semester=TERM_B)) == 1
    assert db.changes(semester="大二上") == []
    assert db.changes(limit=2) == everything[:2]
    assert db.changes(limit=0) == []


def test_audit_lands_in_audit_dir_as_enc_only(store: CryptoStore,
                                             db: GradeHistoryDB) -> None:
    seed(db)

    assert AUDIT_FILE == "audit/grade_changes.json.enc"
    path = user_file(store, USER, AUDIT_FILE)
    assert path is not None and path.exists()
    assert path.parent.name == "audit"
    assert path.suffix == ".enc"

    blob = path.read_bytes()
    assert SID_A.encode("utf-8") not in blob, "学号明文落盘"
    assert MATH.encode("utf-8") not in blob, "课程名明文落盘"

    user_dir = path.parent.parent
    for item in user_dir.iterdir():
        if item.is_file():
            assert item.suffix == ".enc" or item.name == "users.json"


def test_corrupt_audit_log_does_not_break_writes(store: CryptoStore,
                                                 db: GradeHistoryDB) -> None:
    """日志文件坏了：写入照常成功，查询给空列表，一个异常都不许冒出来。"""
    seed(db)
    path = user_file(store, USER, AUDIT_FILE)
    path.write_bytes(b"\x00" * 64)

    assert db.changes() == []               # 读不出来 = 空，不抛
    assert db.audit().count() == 0

    result = db.apply([row(SID_B, ENGLISH, 66.0)], semester=TERM_A,
                      batch_id=BATCH_2)
    assert result["inserted"] == 1
    assert fetch(db, SID_B, ENGLISH, TERM_A) is not None
    assert len(db.changes()) == 1, "坏日志之后没能重新记起来"

    # 另一种坏法：看起来像明文 JSON，其实不是密文
    path.write_bytes(b'{"table": "grade_audit", "rows": []}')
    assert db.changes() == []
    assert db.delete_record(SID_B, TERM_A, ENGLISH) == 1
    assert [e["operation"] for e in db.changes()] == [OP_DELETE]


def test_audit_write_failure_degrades_to_warning(store: CryptoStore) -> None:
    """账户不存在（写不进去）→ record() 返回 None + 记下原因，**绝不抛**。"""
    log = GradeAuditLog(username="查无此人", store=store)

    assert log.record(OP_INSERT, student_id=SID_A, semester=TERM_A) is None
    assert log.last_error, "失败了却没留下原因"

    written = log.record_many([
        {"operation": OP_INSERT, "student_id": SID_A, "semester": TERM_A},
        {"operation": "hack", "student_id": SID_A},
    ])
    assert written == []


def test_unknown_operation_is_dropped(db: GradeHistoryDB) -> None:
    assert db.audit().record("hack", student_id=SID_A) is None
    assert db.changes() == []

    written = db.audit().record_many([
        {"operation": OP_INSERT, "student_id": SID_A, "semester": TERM_A, "count": 2},
        {"operation": "rm -rf", "student_id": SID_A},
        "不是字典",
    ])
    assert len(written) == 1
    assert [e["operation"] for e in db.changes()] == [OP_INSERT]


def test_audit_cap_drops_oldest_first(store: CryptoStore, db: GradeHistoryDB) -> None:
    rows = [{"at": f"2026-01-01T00:00:{i % 60:02d}", "operation": OP_INSERT,
             "student_id": SID_A, "semester": TERM_A, "course_name": "",
             "count": 1}
            for i in range(AUDIT_MAX_ENTRIES)]
    save_user_json(store, USER, AUDIT_FILE,
                   {"table": TABLE_GRADE_AUDIT, "version": 1, "rows": rows})

    db.audit().record(OP_DELETE, student_id=SID_A, semester=TERM_A, count=1)

    assert db.audit().count() == AUDIT_MAX_ENTRIES
    assert db.changes(limit=1)[0]["operation"] == OP_DELETE, "新条目没保住"


# ---------------------------------------------------------------------------
# 四、library_inventory / clear_library（"删除成绩库"那个 bug 的正解）
# ---------------------------------------------------------------------------


def test_inventory_sees_imported_grades_even_with_no_legacy_file(
        store: CryptoStore, db: GradeHistoryDB) -> None:
    """★ 回归：导入成绩后，判据必须是**表里的行数**，不是"磁盘上有没有 grades.enc"。"""
    empty = library_inventory(store, USER)
    assert empty["account"] is True
    assert empty["has_data"] is False
    assert empty["files"] == []

    seed(db)

    inv = library_inventory(store, USER)
    assert inv["grade_rows"] == 3
    assert inv["has_data"] is True, "导入完成绩还说没有数据 —— 这就是那个 bug"
    # 一个"文件在不在"式的判据仍然什么都看不见（旧实现就是这么误报的）
    assert inv["files"] == []
    assert inv["legacy_files"] == []
    assert inv["archive"] is False
    assert inv["audit_rows"] >= 1


def test_old_payload_path_guard_explains_the_bug(store: CryptoStore) -> None:
    """旧实现连 ``grade_history.json.enc`` 的路径都拿不到 —— 这才是根因。"""
    record = store.load_users().find(USER)
    assert record is not None
    with pytest.raises(ValueError):
        store.payload_path(record.uid, GRADE_HISTORY_FILE)

    assert user_file(store, USER, GRADE_HISTORY_FILE) is not None


def test_clear_library_empties_grades_profiles_and_generated(
        store: CryptoStore, db: GradeHistoryDB, profiles: ProfileStore) -> None:
    seed(db)
    profiles.save_side_text(SID_A, "侧写正文甲")
    profiles.save_side_text(SID_B, "侧写正文乙")
    save_user_json(store, USER, ARCHIVE_PAYLOAD_FILE, {"students": {}})
    save_user_json(store, USER, class_filename("2301"), {"class_name": "2301"})

    report = clear_library(store, USER)

    assert report["found"] is True
    assert report["grade_rows"] == 3
    assert report["profile_rows"] == 2
    assert ARCHIVE_PAYLOAD_FILE in report["files"]
    assert class_filename("2301") in report["files"]
    assert report["failed"] == []

    fresh_db = GradeHistoryDB(username=USER, store=store)
    assert fresh_db.count() == 0 and fresh_db.rows() == []
    assert ProfileStore(username=USER, store=store).count() == 0
    assert not user_file(store, USER, ARCHIVE_PAYLOAD_FILE).exists()
    assert not user_file(store, USER, class_filename("2301")).exists()

    after = library_inventory(store, USER)
    assert after["has_data"] is False, "删完了还说有数据"
    assert after["grade_rows"] == 0 and after["profile_rows"] == 0

    # ★ "清完了" 与 "本来就没有" 必须分得开
    second = clear_library(store, USER)
    assert second["found"] is False
    assert second["grade_rows"] == 0 and second["profile_rows"] == 0
    assert second["files"] == []


def test_clear_library_keeps_moral_quota_sessions_and_audit(
        store: CryptoStore, db: GradeHistoryDB) -> None:
    seed(db)
    save_user_json(store, USER, moral_filename("2301"), {"sid:20230101": 90})
    save_user_json(store, USER, quota_filename("2301"), {"first": 3})
    save_user_json(store, USER, "sessions.json.enc", {"token": "x"})

    report = clear_library(store, USER)

    assert report["grade_rows"] == 3
    for name in (moral_filename("2301"), quota_filename("2301"), "sessions.json.enc"):
        assert user_file(store, USER, name).exists(), f"{name} 被误删了"

    # 审计日志是"这次删除的记录本身"，必须留下，而且不算成"还有数据"
    assert user_file(store, USER, AUDIT_FILE).exists()
    assert library_inventory(store, USER)["audit_rows"] >= 2
    assert library_inventory(store, USER)["has_data"] is False


def test_clear_library_with_profiles_off_keeps_side_text(
        store: CryptoStore, db: GradeHistoryDB, profiles: ProfileStore) -> None:
    seed(db)
    profiles.save_side_text(SID_A, "别删我")
    save_user_json(store, USER, ARCHIVE_PAYLOAD_FILE, {"students": {}})

    report = clear_library(store, USER, with_profiles=False, with_generated=False)

    assert report["grade_rows"] == 3
    assert report["profile_rows"] == 0
    assert report["files"] == []
    assert profiles.side_text(SID_A) == "别删我"
    assert user_file(store, USER, ARCHIVE_PAYLOAD_FILE).exists()
    assert GradeHistoryDB(username=USER, store=store).count() == 0


def test_clear_library_logs_a_delete_even_when_only_profiles_exist(
        store: CryptoStore, profiles: ProfileStore) -> None:
    """只有侧写、没有成绩行 —— 也是一次真删除，不能从日志里失踪。"""
    profiles.save_side_text(SID_A, "只有侧写")

    report = clear_library(store, USER)

    assert report["found"] is True
    assert report["grade_rows"] == 0
    assert report["profile_rows"] == 1
    assert [e["operation"] for e in GradeHistoryDB(
        username=USER, store=store).changes()] == [OP_DELETE]


def test_clear_library_reports_failed_files(store: CryptoStore,
                                            db: GradeHistoryDB) -> None:
    """删不掉的文件要**如实报出来**，而且不许影响别的删除。"""
    seed(db)
    archive = user_file(store, USER, ARCHIVE_PAYLOAD_FILE)
    archive.mkdir(parents=True)             # 用目录冒充文件：unlink 必失败

    report = clear_library(store, USER)

    assert report["grade_rows"] == 3
    assert report["files"] == []
    assert len(report["failed"]) == 1
    assert ARCHIVE_PAYLOAD_FILE in report["failed"][0]
    assert GradeHistoryDB(username=USER, store=store).count() == 0
    assert library_inventory(store, USER)["has_data"] is True   # 归档还在


def test_clear_library_without_account_is_a_noop(store: CryptoStore) -> None:
    report = clear_library(store, "查无此人")

    assert report["found"] is False
    assert report["grade_rows"] == 0 and report["files"] == []

    empty = library_inventory(store, "")
    assert empty["account"] is False
    assert empty["has_data"] is False
    assert empty["files"] == [] and empty["grade_rows"] == 0


# ---------------------------------------------------------------------------
# 五、GPA 方案 1（core.gpa）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("score,expected", [
    (59, 0.0), (60, 1.0), (63, 1.0), (64, 1.5), (67, 1.5), (68, 2.0), (71, 2.0),
    (72, 2.3), (74, 2.3), (75, 2.7), (77, 2.7), (78, 3.0), (81, 3.0), (82, 3.3),
    (84, 3.3), (85, 3.7), (89, 3.7), (90, 4.0), (100, 4.0),
])
def test_standard_4_table_boundaries(score: float, expected: float) -> None:
    assert grade_point(score) == pytest.approx(expected)


def test_table_edge_cases() -> None:
    assert grade_point(0) == 0.0            # 缺考：真的 0 分
    assert grade_point(89.5) == pytest.approx(3.7)
    assert grade_point(59.9) == 0.0
    assert grade_point("88") == pytest.approx(3.7)   # 字符串数字照样认


@pytest.mark.parametrize("blank", ["", "  ", "-", "/", "—", "未选", "无", None])
def test_blank_score_is_not_zero(blank: object) -> None:
    assert grade_point(blank) is None


def test_blank_courses_stay_out_of_the_denominator() -> None:
    detail = gpa_detail([
        {"course_name": MATH, "score": 88, "credit": 3.0},
        {"course_name": ENGLISH, "score": "", "credit": 2.0},
        {"course_name": "体育", "score": "-", "credit": 1.0},
        {"course_name": "选修甲", "score": None, "credit": 2.0},
        {"course_name": "选修乙", "score": "未选", "credit": 2.0},
    ])

    assert detail["counted"] == 1
    assert detail["skipped"] == 4
    assert detail["credits"] == pytest.approx(3.0)
    assert detail["gpa"] == pytest.approx(3.7)


def test_absent_zero_score_counts_in_denominator() -> None:
    detail = gpa_detail([
        {"course_name": MATH, "score": 90, "credit": 4.0},
        {"course_name": "缺考课", "score": 0, "credit": 4.0},
    ])

    assert detail["counted"] == 2
    assert detail["credits"] == pytest.approx(8.0)
    assert detail["gpa"] == pytest.approx(2.0)      # (4.0*4 + 0*4) / 8


def test_none_is_not_zero() -> None:
    assert gpa_of([]) is None
    assert gpa_of([{"score": "", "credit": 3.0}]) is None
    assert gpa_of([{"score": 88, "credit": None}]) is None
    assert gpa_detail([{"score": 88, "credit": None}])["counted"] == 0
    # 真的 0 分（缺考）算得出来，而且是 0.0 不是 None
    assert gpa_of([{"score": 0, "credit": 3.0}]) == 0.0


def test_gpa_formula_is_credit_weighted() -> None:
    detail = gpa_detail([
        {"course_name": MATH, "score": 90, "credit": 4.0},    # 4.0
        {"course_name": ENGLISH, "score": 78, "credit": 1.0},  # 3.0
    ])

    assert detail["points"] == pytest.approx(19.0)
    assert detail["credits"] == pytest.approx(5.0)
    assert detail["gpa"] == pytest.approx(3.8)
    assert gpa_of([{"score": 90, "credit": 4.0}, {"score": 78, "credit": 1.0}]) \
        == pytest.approx(3.8)


def test_field_aliases_and_objects() -> None:
    class Course:
        def __init__(self, score: float, credit: float) -> None:
            self.score = score
            self.credit = credit

    assert gpa_of([{"成绩": 90, "学分": 4.0}]) == pytest.approx(4.0)
    assert gpa_of([Course(85, 2.0)]) == pytest.approx(3.7)


def test_only_scheme_one_is_available() -> None:
    options = scheme_options()
    assert [o["id"] for o in options] == [SCHEME_STANDARD_4, "scheme2", "scheme3", "scheme4"]
    assert options[0]["available"] is True
    assert all(o["available"] is False for o in options[1:])
    assert all("待定" in o["label"] for o in options[1:])

    assert available_schemes() == [SCHEME_STANDARD_4]
    assert DEFAULT_SCHEME == SCHEME_STANDARD_4
    assert is_available(DEFAULT_SCHEME) is True
    assert is_available("scheme2") is False
    assert is_available(None) is True            # None = 缺省方案
    assert scheme_label("scheme2") == "方案 2（待定）"
    assert scheme_label("不认识") == "不认识"      # 原样返回，界面上看得出不对


def test_pending_schemes_raise_instead_of_guessing() -> None:
    with pytest.raises(ValueError):
        gpa_of([{"score": 90, "credit": 1.0}], scheme="scheme2")
    with pytest.raises(ValueError):
        grade_point(90, scheme="scheme4")


def test_ui_facing_scheme_text_has_no_markdown() -> None:
    """下拉框里的文案会原样显示 —— 不许有 Markdown 记号（AGENTS §24）。"""
    for item in SCHEMES:
        text = f"{item['label']}{item['note']}"
        for token in ("**", "__", "`"):
            assert token not in text, f"{item['id']} 的文案里有 {token}"


def test_scheme_choice_round_trip_is_app_level(store: CryptoStore, tmp_path: Path) -> None:
    assert load_scheme(store, USER) == DEFAULT_SCHEME          # 没存过 = 默认

    assert save_scheme(store, USER, DEFAULT_SCHEME) == DEFAULT_SCHEME
    assert load_scheme(store, USER) == DEFAULT_SCHEME

    path = user_file(store, USER, GPA_SETTINGS_FILE)
    assert path is not None and path.exists()
    assert GPA_SETTINGS_FILE.endswith(".enc")
    assert b"standard4" not in path.read_bytes(), "方案 id 明文落盘"

    # 应用级：**不需要密码**，换一个 store 实例照样读得回来（AGENTS §16）
    assert load_scheme(CryptoStore(root=store.root), USER) == DEFAULT_SCHEME

    # 存进去的值已经不认识了 → 回默认，界面下拉框永远有合法选中项
    save_user_json(store, USER, GPA_SETTINGS_FILE, {"scheme": "scheme9"})
    assert load_scheme(store, USER) == DEFAULT_SCHEME


def test_scheme_save_rejects_bad_input(store: CryptoStore) -> None:
    with pytest.raises(ValueError):
        save_scheme(store, USER, "scheme2")       # 待定的方案不许存
    with pytest.raises(ValueError):
        save_scheme(store, "", DEFAULT_SCHEME)    # 没登录，没地方存
    assert load_scheme(store, "") == DEFAULT_SCHEME


# ---------------------------------------------------------------------------
# 六、边界与红线：零 Qt、明文不落盘、常量对齐
# ---------------------------------------------------------------------------


def test_archive_name_matches_the_ui_constant() -> None:
    """``core`` 不许 import ``ui``，所以两个字符串只能靠这条断言对齐。"""
    text = (REPO / "ui" / "pages" / "analysis_page.py").read_text(encoding="utf-8")
    match = re.search(r'^ARCHIVE_FILE\s*=\s*"([^"]+)"', text, re.M)
    assert match is not None, "ui/pages/analysis_page.py 里找不到 ARCHIVE_FILE"
    assert match.group(1) == ARCHIVE_PAYLOAD_FILE


@pytest.mark.parametrize("relative", ["core/database.py", "core/gpa.py"])
def test_new_core_modules_stay_offline_and_qt_free(relative: str) -> None:
    text = (REPO / relative).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", text, re.M)
    banned = ("PySide6", "requests", "httpx", "aiohttp", "urllib3", "socket",
              "openai", "anthropic")
    for name in imports:
        assert not name.startswith(banned), f"{relative} 里出现了 {name}"
    assert "urllib.request" not in text


def test_nothing_sensitive_lands_in_plaintext(store: CryptoStore,
                                              db: GradeHistoryDB,
                                              profiles: ProfileStore) -> None:
    """成绩 / 侧写 / 学号都只许在 .enc 里（红线 3）。"""
    db.apply([row(SID_A, "秘传课程XYZ", 87.5)], semester=TERM_A, batch_id=BATCH_1)
    profiles.save_side_text(SID_A, "侧写密文标记ABC")
    save_user_json(store, USER, ARCHIVE_PAYLOAD_FILE, {"students": {SID_A: {}}})

    markers = [b"87.5", "秘传课程XYZ".encode("utf-8"),
               "侧写密文标记ABC".encode("utf-8"), SID_A.encode("utf-8")]

    files = disk_files(store.root)
    assert files, "临时库里一个文件都没有，说明夹具没建起来"
    for path in files:
        blob = path.read_bytes()
        for marker in markers:
            assert marker not in blob, f"{path.name} 里有明文：{marker!r}"

    user_dir = user_file(store, USER, GRADE_HISTORY_FILE).parent
    for path in user_dir.iterdir():
        if path.is_file():
            assert path.suffix == ".enc", f"用户目录里有非密文文件：{path.name}"


def test_legacy_files_are_named_after_the_v19_payloads() -> None:
    """老库残留清单只认这两个密码级文件名（别顺手加新名字）。"""
    assert set(LEGACY_GRADE_FILES) == {"grades.enc", "tags.json.enc"}
    assert PROFILE_FILE == "student_profiles.json.enc"
    assert GRADE_HISTORY_FILE == "grade_history.json.enc"


# ---------------------------------------------------------------------------
# 七、界面路径：点「删除成绩库」这条路（需要 PySide6，offscreen）
# ---------------------------------------------------------------------------
#
# ★ 用户报的 bug 就在这条路上：导入成绩后点按钮，界面说"没有成绩库"。
#   数据层的判据在第四节已经守住了，这一节量的是**按钮这条路**：
#   先问人、问的时候数据还在、删完发信号让别的页面 reload。


def _settings_page(store: CryptoStore):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: F401

    from ui.pages.settings_page import SettingsPage

    app = QApplication.instance() or QApplication([])
    page = SettingsPage(store=store)
    page.set_user(USER)
    return app, page


def _stub_dialogs(monkeypatch, answer=None, on_call=None) -> list[dict]:
    """把所有模态弹窗换成"直接点确定"，并把每次调用记下来。"""
    from PySide6.QtWidgets import QMessageBox

    calls: list[dict] = []

    def make(kind: str, default):
        def stub(parent, title, text, *args, **kwargs):
            if on_call is not None:
                on_call(kind)
            calls.append({"kind": kind, "title": str(title), "text": str(text)})
            return default if answer is None else answer

        return stub

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(make("question", QMessageBox.StandardButton.Yes)))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(make("warning", QMessageBox.StandardButton.Yes)))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(make("information", QMessageBox.StandardButton.Ok)))
    monkeypatch.setattr(QMessageBox, "critical",
                        staticmethod(make("critical", QMessageBox.StandardButton.Ok)))
    return calls


def test_settings_says_no_library_only_when_truly_empty(
        store: CryptoStore, db: GradeHistoryDB, monkeypatch) -> None:
    _, page = _settings_page(store)
    calls = _stub_dialogs(monkeypatch)

    page._do_clear()
    assert [c["title"] for c in calls] == ["没有成绩库"]
    assert "没有成绩库" in page._clr_status.text()

    seed(db)
    calls.clear()
    page._do_clear()
    assert "没有成绩库" not in [c["title"] for c in calls], \
        "★ 导入完成绩还说没有成绩库（就是用户报的那条）"
    assert "确认要删除吗" in [c["title"] for c in calls]


def test_settings_clear_flow_asks_before_deleting(store: CryptoStore,
                                                  db: GradeHistoryDB,
                                                  profiles: ProfileStore,
                                                  monkeypatch) -> None:
    seed(db)
    profiles.save_side_text(SID_A, "侧写正文")
    save_user_json(store, USER, ARCHIVE_PAYLOAD_FILE, {"students": {}})

    counted_at_question: list[int] = []

    def probe(kind: str) -> None:
        if kind == "question":
            counted_at_question.append(
                GradeHistoryDB(username=USER, store=store).count()
            )

    _, page = _settings_page(store)
    calls = _stub_dialogs(monkeypatch, on_call=probe)
    fired: list[tuple] = []
    page.library_cleared.connect(lambda *args: fired.append(args))

    page._do_clear()

    assert counted_at_question == [3], "确认框弹出来之前数据就没了 —— 静默删数据"
    kinds = [c["kind"] for c in calls]
    assert kinds.index("question") < kinds.index("information")
    assert [c["title"] for c in calls].count("最后确认") == 1

    assert GradeHistoryDB(username=USER, store=store).count() == 0
    assert ProfileStore(username=USER, store=store).count() == 0
    assert not user_file(store, USER, ARCHIVE_PAYLOAD_FILE).exists()
    assert [c["title"] for c in calls][-1] == "已删除"
    assert fired == [(3, 1, 1)], "删完没发 reload 信号（档案页会继续显示旧名单）"


def test_settings_clear_cancel_keeps_everything(store: CryptoStore,
                                                db: GradeHistoryDB,
                                                monkeypatch) -> None:
    seed(db)
    from PySide6.QtWidgets import QMessageBox

    pytest.importorskip("PySide6")
    _, page = _settings_page(store)
    _stub_dialogs(monkeypatch, answer=QMessageBox.StandardButton.No)
    fired: list[tuple] = []
    page.library_cleared.connect(lambda *args: fired.append(args))

    page._do_clear()

    assert GradeHistoryDB(username=USER, store=store).count() == 3
    assert fired == []
    assert "已取消" in page._clr_status.text()


def test_settings_text_has_no_markdown(store: CryptoStore) -> None:
    """AGENTS §24：QLabel / 按钮 / 气泡里出现 ``**`` 会原样显示成两个星号。"""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QWidget

    _, page = _settings_page(store)

    texts = [w.text() for w in page.findChildren(QLabel)]
    texts += [w.text() for w in page.findChildren(QAbstractButton)]
    texts += [w.toolTip() for w in page.findChildren(QWidget) if w.toolTip()]
    for combo in page.findChildren(QComboBox):
        for index in range(combo.count()):
            texts.append(combo.itemText(index))
            tip = combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
            if tip:
                texts.append(str(tip))

    assert texts, "设置页一个控件文字都没读到，说明页面没建起来"
    for text in texts:
        for token in ("**", "__", "`"):
            assert token not in text, f"界面文案里有 Markdown 记号：{text!r}"


def test_settings_gpa_combo_shows_only_scheme_one_as_usable(store: CryptoStore) -> None:
    pytest.importorskip("PySide6")

    _, page = _settings_page(store)
    combo = page._gpa_combo

    assert combo.count() == len(scheme_options())
    assert combo.currentData() == DEFAULT_SCHEME
    assert page._gpa_status.text().startswith("当前方案")

    model = combo.model()
    assert model.item(0).isEnabled() is True
    assert all(model.item(i).isEnabled() is False for i in range(1, combo.count()))

    page._on_gpa_scheme_changed(0)
    assert load_scheme(store, USER) == DEFAULT_SCHEME
    assert user_file(store, USER, GPA_SETTINGS_FILE).exists()

# ---------------------------------------------------------------------------
# v1.9.2：AI 谈话记录（cache/chat_history_<学号>.json.enc）也要随"删除成绩库"清掉
# ---------------------------------------------------------------------------


def _chat_file(store: CryptoStore, bare: str):
    base = user_file(store, USER, "__probe__.json.enc")
    assert base is not None
    return base.parent / "cache" / f"chat_history_{bare}.json.enc"


def _seed_chat(store: CryptoStore, bare: str = "20230101") -> object:
    path = _chat_file(store, bare)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"encrypted-chat")
    return path


def test_盘点会列出聊天缓存(store: CryptoStore) -> None:
    _seed_chat(store)
    inv = library_inventory(store, USER)
    assert inv["chat_files"] == 1
    assert any(name.startswith("cache/chat_history_") for name in inv["files"])


def test_清库会把聊天缓存一起删(store: CryptoStore) -> None:
    path = _seed_chat(store)
    report = clear_library(store, USER)
    assert report["found"] is True
    assert not path.exists(), "聊天缓存没被清掉"
    assert any(name.startswith("cache/chat_history_") for name in report["files"])


def test_不勾清侧写时保留聊天缓存(store: CryptoStore) -> None:
    path = _seed_chat(store)
    report = clear_library(store, USER, with_profiles=False)
    assert path.exists(), "没勾清侧写却把谈话记录删了"
    assert not any(name.startswith("cache/chat_history_") for name in report["files"])
    assert report["found"] is True

