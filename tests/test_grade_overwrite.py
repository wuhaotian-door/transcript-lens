"""同学期成绩覆盖 / 跳过语义的判据（v1.9 二期 D1 + D3）。

这里测的是**两条真链路**：

1. :mod:`core.database` 的逻辑表 API —— 覆盖 = 删旧写新、跳过 = 保留旧记录；
2. :mod:`ui.pages.analysis_page` 里那几个**纯函数** —— 冲突文案、按钮 → 策略
   的映射、冲突去重、批次号、把结果摊成表行。

★ 只跑纯函数、不开窗口：GUI 那部分（弹窗真的弹出来）由
  ``tests/test_profile_merge.py`` 的离屏用例覆盖，两边都不桩 Qt 事件循环。

★ 建临时库**必须先** ``store.register()`` —— 否则 ``save_user_json`` 抛
  ``CryptoError("本机没有账户…")``，所有"落盘成功"的断言都会假红（AGENTS §20c）。

★ 一条硬红线：**明文成绩绝不落盘**。所以除了语义，还要真的去翻
  ``%LOCALAPPDATA%`` 那个临时数据目录，断言里面只有 ``.enc``，
  而且**扫不到明文姓名 / 明文分数**。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.crypto_store import CryptoStore
from core.database import (
    GRADE_HISTORY_FILE,
    GRADE_HISTORY_PK,
    PROFILE_FILE,
    TABLE_GRADE_HISTORY,
    USE_OVERWRITE,
    USE_SKIP,
    ConflictQuestion,
    GradeHistoryDB,
    ProfileStore,
)
from ui.pages.analysis_page import (
    CONFLICT_BUTTONS,
    CONFLICT_DISMISSED,
    CONFLICT_OVERWRITE,
    CONFLICT_OVERWRITE_ALL,
    CONFLICT_SKIP,
    CONFLICT_SKIP_ALL,
    conflict_message,
    conflict_result_text,
    conflicts_of,
    grade_history_records,
    new_batch_id,
    resolve_conflict_choice,
    resolve_conflicts,
)

USER = "验收员"
PASSWORD = "tl-test-2026"

TERM_A = "大一上"
TERM_B = "大一下"
BATCH_1 = "20261027-1"
BATCH_2 = "20261027-2"


# --------------------------------------------------------------- 夹具


@pytest.fixture()
def store(tmp_path: Path) -> CryptoStore:
    """一个注册好的临时加密库（**必须 register**，见模块开头）。"""
    vault = CryptoStore(root=tmp_path / "vault")
    vault.register(USER, PASSWORD)
    return vault


@pytest.fixture()
def db(store: CryptoStore) -> GradeHistoryDB:
    return GradeHistoryDB(username=USER, store=store)


def row(student_id: str, course: str, score: float | None,
        credit: float | None = 4.0, name: str = "") -> dict:
    entry: dict = {
        "student_id": student_id,
        "course_name": course,
        "score": score,
        "credit": credit,
    }
    if name:
        entry["student_name"] = name
    return entry


def fetch(db: GradeHistoryDB, student_id: str, course: str,
          semester: str) -> dict | None:
    for item in db.semester_rows(student_id, semester):
        if item["course_name"] == course:
            return item
    return None


# --------------------------------------------------------------- 基本写入


def test_first_import_is_all_inserted(db: GradeHistoryDB) -> None:
    counts = db.apply(
        [row("2023001", "高等数学", 88.0), row("2023001", "线性代数", 76.0)],
        semester=TERM_A,
        batch_id=BATCH_1,
    )
    assert counts == {"overwritten": 0, "inserted": 2, "skipped": 0}
    assert len(db.rows()) == 2
    assert db.has_semester("2023001", TERM_A) is True


def test_no_conflict_means_nothing_skipped(db: GradeHistoryDB) -> None:
    """★ 回归：第一版这里写错了缺省，**没有冲突的学生也走跳过分支** ——
    表现为 inserted 恒为 0、``rows()`` 恒为空，而返回值全是 0、一个异常都不抛。
    """
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    # 第二个学生是全新的，不该被"跳过"
    counts = db.apply(
        [row("2023002", "高等数学", 91.0)], semester=TERM_A, batch_id=BATCH_2
    )
    assert counts["inserted"] == 1
    assert counts["skipped"] == 0
    assert db.has_semester("2023002", TERM_A) is True
    assert len(db.rows()) == 2


# --------------------------------------------------------------- 覆盖语义


def test_same_key_overwrite_replaces_old_score(db: GradeHistoryDB) -> None:
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    counts = db.apply(
        [row("2023001", "高等数学", 95.0)],
        semester=TERM_A,
        batch_id=BATCH_2,
        policy=USE_OVERWRITE,
    )
    assert counts == {"overwritten": 1, "inserted": 1, "skipped": 0}
    # 删旧写新 —— 同键只有一条，而且是新的那条
    assert len(db.semester_rows("2023001", TERM_A)) == 1
    stored = fetch(db, "2023001", "高等数学", TERM_A)
    assert stored is not None
    assert stored["score"] == 95.0
    assert stored["import_batch_id"] == BATCH_2


def test_overwrite_keeps_other_courses(db: GradeHistoryDB) -> None:
    """覆盖只动**同键**那一行，同批次里别的课照旧。"""
    db.apply(
        [row("2023001", "高等数学", 88.0), row("2023001", "线性代数", 76.0)],
        semester=TERM_A,
        batch_id=BATCH_1,
    )
    db.apply(
        [row("2023001", "高等数学", 90.0)],
        semester=TERM_A,
        batch_id=BATCH_2,
        policy=USE_OVERWRITE,
    )
    assert len(db.semester_rows("2023001", TERM_A)) == 2
    assert fetch(db, "2023001", "线性代数", TERM_A)["score"] == 76.0
    assert fetch(db, "2023001", "高等数学", TERM_A)["score"] == 90.0


def test_skip_keeps_old_record_untouched(db: GradeHistoryDB) -> None:
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    counts = db.apply(
        [row("2023001", "高等数学", 30.0)],
        semester=TERM_A,
        batch_id=BATCH_2,
        policy=USE_SKIP,
    )
    assert counts == {"overwritten": 0, "inserted": 0, "skipped": 1}
    stored = fetch(db, "2023001", "高等数学", TERM_A)
    assert stored["score"] == 88.0      # 旧值原样留着
    assert stored["import_batch_id"] == BATCH_1
    assert len(db.semester_rows("2023001", TERM_A)) == 1


def test_skip_only_drops_the_colliding_course(db: GradeHistoryDB) -> None:
    """★ 跳过 = "这个学生这一批不导入**这一行**"。

    同一批里**没有冲突的课**照常写进去 —— 否则补考后重修一门新课时，
    整批都进不去，那才是真的丢数据。
    """
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    counts = db.apply(
        [
            row("2023001", "高等数学", 30.0),   # 冲突 → 跳过
            row("2023001", "大学物理", 77.0),   # 新课 → 写进去
        ],
        semester=TERM_A,
        batch_id=BATCH_2,
        policy=USE_SKIP,
    )
    assert counts == {"overwritten": 0, "inserted": 1, "skipped": 1}
    assert fetch(db, "2023001", "高等数学", TERM_A)["score"] == 88.0
    assert fetch(db, "2023001", "大学物理", TERM_A)["score"] == 77.0


def test_multiple_students_mixed_decisions(db: GradeHistoryDB) -> None:
    """多学生混合：甲覆盖、乙跳过、丙是新生（照常入库）。

    这正是 D3 弹窗里"逐个问"能造出来的局面 —— 一次 ``apply`` 三种结局。
    """
    db.apply(
        [
            row("2023001", "高等数学", 60.0),
            row("2023002", "高等数学", 60.0),
        ],
        semester=TERM_A,
        batch_id=BATCH_1,
    )
    counts = db.apply(
        [
            row("2023001", "高等数学", 91.0),
            row("2023002", "高等数学", 91.0),
            row("2023003", "高等数学", 91.0),
        ],
        semester=TERM_A,
        batch_id=BATCH_2,
        on_conflict=lambda questions: {
            q.student_id: (USE_OVERWRITE if q.student_id == "2023001" else USE_SKIP)
            for q in questions
        },
    )
    assert counts == {"overwritten": 1, "inserted": 2, "skipped": 1}
    assert fetch(db, "2023001", "高等数学", TERM_A)["score"] == 91.0
    assert fetch(db, "2023002", "高等数学", TERM_A)["score"] == 60.0
    assert fetch(db, "2023003", "高等数学", TERM_A)["score"] == 91.0


# --------------------------------------------------------------- 学期隔离


def test_different_semesters_do_not_touch_each_other(db: GradeHistoryDB) -> None:
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    counts = db.apply(
        [row("2023001", "高等数学", 55.0)],
        semester=TERM_B,
        batch_id=BATCH_2,
        policy=USE_OVERWRITE,
    )
    # 换学期就是**新键**（主键含 semester），所以是新增不是覆盖
    assert counts == {"overwritten": 0, "inserted": 1, "skipped": 0}
    assert fetch(db, "2023001", "高等数学", TERM_A)["score"] == 88.0
    assert fetch(db, "2023001", "高等数学", TERM_B)["score"] == 55.0
    assert db.has_semester("2023001", TERM_A)
    assert db.has_semester("2023001", TERM_B)
    assert db.has_semester("2023001", "大二上") is False


def test_students_in_lists_each_student_once(db: GradeHistoryDB) -> None:
    db.apply(
        [
            row("2023002", "高等数学", 88.0),
            row("2023001", "高等数学", 88.0),
            row("2023001", "线性代数", 70.0),
        ],
        semester=TERM_A,
        batch_id=BATCH_1,
    )
    assert sorted(db.students_in(TERM_A)) == ["2023001", "2023002"]
    assert db.students_in(TERM_B) == []


def test_clear_only_empties_this_table(db: GradeHistoryDB,
                                      store: CryptoStore) -> None:
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    profiles = ProfileStore(username=USER, store=store)
    profiles.save_side_text("2023001", "侧写正文")
    db.clear()
    assert db.rows() == []
    # 另一张表一个字没动
    assert profiles.side_text("2023001") == "侧写正文"


# --------------------------------------------------------------- 批次号与时间


def test_batch_id_and_imported_at_are_written(db: GradeHistoryDB) -> None:
    db.apply([row("2023001", "高等数学", 88.0, name="张三")],
             semester=TERM_A, batch_id=BATCH_1)
    stored = fetch(db, "2023001", "高等数学", TERM_A)
    assert stored["import_batch_id"] == BATCH_1
    assert stored["semester"] == TERM_A
    # ISO 字符串，而且能被 datetime 解析回来（不是随手写的相对时间）
    assert datetime.fromisoformat(stored["imported_at"])
    # 姓名**不落盘**：它只在冲突弹窗里用一下
    assert "student_name" not in stored


def test_semester_argument_overrides_row_value(db: GradeHistoryDB) -> None:
    """一批只能属于一个学期 —— 行里塞了别的学期也要以参数为准。"""
    bad = row("2023001", "高等数学", 88.0)
    bad["semester"] = "大四上"
    db.apply([bad], semester=TERM_A, batch_id=BATCH_1)
    assert db.rows()[0]["semester"] == TERM_A


def test_rows_carry_exactly_the_declared_fields(db: GradeHistoryDB) -> None:
    from core.database import GRADE_HISTORY_FIELDS

    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    stored = db.rows()[0]
    assert set(stored) == set(GRADE_HISTORY_FIELDS)
    assert GRADE_HISTORY_PK == ("student_id", "semester", "course_name")
    assert TABLE_GRADE_HISTORY == "grade_history"


def test_blank_rows_are_dropped_not_written(db: GradeHistoryDB) -> None:
    counts = db.apply(
        [
            {"student_id": "", "course_name": "高等数学", "score": 88.0},
            {"student_id": "2023001", "course_name": "", "score": 88.0},
            "这不是一行",
            row("2023001", "线性代数", 70.0),
        ],
        semester=TERM_A,
        batch_id=BATCH_1,
    )
    assert counts == {"overwritten": 0, "inserted": 1, "skipped": 0}
    assert len(db.rows()) == 1


def test_empty_batch_id_is_refused(db: GradeHistoryDB) -> None:
    with pytest.raises(ValueError):
        db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id="")
    with pytest.raises(ValueError):
        db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A,
                 batch_id=BATCH_1, policy="乱写的策略")


# --------------------------------------------------------------- 冲突回调


def test_conflict_callback_is_not_called_without_conflict(db: GradeHistoryDB) -> None:
    calls: list[list] = []

    def ask(questions: list) -> dict:
        calls.append(list(questions))
        return {}

    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A,
             batch_id=BATCH_1, on_conflict=ask)
    assert calls == []          # 没冲突就不问 —— 也不弹窗
    assert len(db.rows()) == 1


def test_conflict_callback_receives_name_and_existing_count(
    db: GradeHistoryDB,
) -> None:
    db.apply(
        [row("2023001", "高等数学", 88.0), row("2023001", "线性代数", 70.0)],
        semester=TERM_A,
        batch_id=BATCH_1,
    )
    seen: list[ConflictQuestion] = []

    def ask(questions: list) -> dict:
        seen.extend(questions)
        return {q.student_id: USE_OVERWRITE for q in questions}

    db.apply(
        [row("2023001", "高等数学", 91.0, name="张三")],
        semester=TERM_A,
        batch_id=BATCH_2,
        on_conflict=ask,
    )
    assert len(seen) == 1
    assert seen[0].student == "张三"
    assert seen[0].semester == TERM_A
    assert seen[0].existing == 2        # 这个学生这个学期已有 2 条
    assert conflict_message(seen[0]) == "检测到学生 张三 已有 大一上 成绩，是否覆盖？"


def test_callback_crash_falls_back_to_skip(db: GradeHistoryDB) -> None:
    """★ 界面回调抛异常时**按跳过** —— 破坏性更小的那一侧。"""
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)

    def boom(questions: list) -> dict:
        raise RuntimeError("界面炸了")

    counts = db.apply(
        [row("2023001", "高等数学", 10.0)],
        semester=TERM_A,
        batch_id=BATCH_2,
        on_conflict=boom,
    )
    assert counts["skipped"] == 1
    assert fetch(db, "2023001", "高等数学", TERM_A)["score"] == 88.0


def test_callback_unknown_value_falls_back_to_skip(db: GradeHistoryDB) -> None:
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    counts = db.apply(
        [row("2023001", "高等数学", 10.0)],
        semester=TERM_A,
        batch_id=BATCH_2,
        on_conflict=lambda questions: {q.student_id: "随便写的" for q in questions},
    )
    assert counts["skipped"] == 1
    assert fetch(db, "2023001", "高等数学", TERM_A)["score"] == 88.0


# --------------------------------------------------------------- 磁盘：只有 .enc


def test_vault_holds_only_encrypted_payloads(store: CryptoStore,
                                             db: GradeHistoryDB) -> None:
    """★ 红线 3 的判据：磁盘上**只有 .enc**，而且扫不到明文姓名 / 分数。"""
    db.apply(
        [row("2023001", "高等数学", 88.5, name="张三"), row("2023001", "线性代数", 76.0)],
        semester=TERM_A,
        batch_id=BATCH_1,
    )
    ProfileStore(username=USER, store=store).save_side_text("2023001", "这名学生很稳")

    root = Path(store.root)
    files = [p for p in root.rglob("*") if p.is_file()]
    payloads = [p for p in files if p.name not in ("users.json", "app.salt")]
    assert payloads, "至少应该有一个用户载荷文件"

    # 1) 一个明文 json 都不许有
    plain = [p for p in payloads if p.suffix.lower() != ".enc"]
    assert plain == [], f"数据目录里出现了非 .enc 文件：{plain}"
    assert not (root / GRADE_HISTORY_FILE.replace(".enc", "")).exists()
    assert not (root / PROFILE_FILE.replace(".enc", "")).exists()

    # 2) 内容按字节扫，明文姓名 / 分数 / 课程名一个都不许露出来
    blob = b"".join(p.read_bytes() for p in payloads)
    for secret in ("张三", "2023001", "高等数学", "88.5", "这名学生很稳"):
        assert secret.encode("utf-8") not in blob, f"密文里扫到了明文：{secret}"

    # 3) 而加密文件确实在（不是"什么都没写"）
    names = {p.name for p in payloads}
    assert GRADE_HISTORY_FILE in names
    assert PROFILE_FILE in names


def test_missing_account_raises_instead_of_silently_dropping(tmp_path: Path) -> None:
    """没登录时**如实抛错**，绝不悄悄吞掉这一批（否则用户以为存上了）。"""
    from core.crypto_store import CryptoError

    vault = CryptoStore(root=tmp_path / "empty")
    vault.register("另一个人", PASSWORD)      # 建了库，但没有 USER 这个账户
    orphan = GradeHistoryDB(username=USER, store=vault)
    with pytest.raises(CryptoError):
        orphan.apply([row("2023001", "高等数学", 88.0)],
                     semester=TERM_A, batch_id=BATCH_1)


def test_reload_from_disk_keeps_rows(store: CryptoStore, db: GradeHistoryDB) -> None:
    """★ 换一个实例读回来 —— 这是"重启程序"的等价场景。"""
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    fresh = GradeHistoryDB(username=USER, store=store)
    assert len(fresh.rows()) == 1
    assert fresh.has_semester("2023001", TERM_A)


# --------------------------------------------------------------- 侧写正文


def test_profile_side_text_roundtrip(store: CryptoStore) -> None:
    profiles = ProfileStore(username=USER, store=store)
    assert profiles.side_text("2023001") == ""      # 没有就是空串，不是 None
    profiles.save_side_text("2023001", "第一版侧写")
    assert profiles.side_text("2023001") == "第一版侧写"
    # 换实例读回来（落盘过）
    assert ProfileStore(username=USER, store=store).side_text("2023001") == "第一版侧写"
    profiles.save_side_text("2023001", "第二版侧写")
    assert profiles.side_text("2023001") == "第二版侧写"
    assert set(profiles.all_profiles()) == {"2023001"}


def test_all_profiles_is_a_copy(store: CryptoStore) -> None:
    profiles = ProfileStore(username=USER, store=store)
    profiles.save_side_text("2023001", "正文")
    snapshot = profiles.all_profiles()
    snapshot["2023001"]["side_text"] = "被改坏了"
    snapshot["2023009"] = {"side_text": "凭空冒出来的"}
    assert profiles.side_text("2023001") == "正文"
    assert set(profiles.all_profiles()) == {"2023001"}


# --------------------------------------------------------------- D3 纯函数


def test_conflict_buttons_are_verbatim() -> None:
    """任务书逐字指定的四个按钮 + 关窗按跳过。"""
    assert CONFLICT_BUTTONS == ("覆盖", "跳过", "全部覆盖", "全部跳过")
    assert CONFLICT_DISMISSED == USE_SKIP
    assert resolve_conflict_choice(CONFLICT_OVERWRITE) == USE_OVERWRITE
    assert resolve_conflict_choice(CONFLICT_OVERWRITE_ALL) == USE_OVERWRITE
    assert resolve_conflict_choice(CONFLICT_SKIP) == USE_SKIP
    assert resolve_conflict_choice(CONFLICT_SKIP_ALL) == USE_SKIP
    # ★ 关窗（None）与认不出来的文案都按**跳过**
    assert resolve_conflict_choice(None) == USE_SKIP
    assert resolve_conflict_choice("") == USE_SKIP
    assert resolve_conflict_choice("OK") == USE_SKIP


def test_result_text_matches_task_wording() -> None:
    assert conflict_result_text(
        {"overwritten": 3, "inserted": 5, "skipped": 2}
    ) == "覆盖 3 条 / 新增 5 条 / 跳过 2 条"
    assert conflict_result_text({}) == "覆盖 0 条 / 新增 0 条 / 跳过 0 条"


def _question(sid: str, name: str, existing: int = 1) -> ConflictQuestion:
    return ConflictQuestion(sid, name, TERM_A, existing)


def test_blanket_choice_stops_asking() -> None:
    asked: list[str] = []

    def ask(question: ConflictQuestion) -> str:
        asked.append(question.student_id)
        return CONFLICT_SKIP if len(asked) == 1 else CONFLICT_OVERWRITE_ALL

    decisions, stopped = resolve_conflicts(
        [_question("A", "甲"), _question("B", "乙"), _question("C", "丙")], ask
    )
    assert asked == ["A", "B"]                  # 第三个不再问
    assert stopped is True
    assert decisions == {"A": USE_SKIP, "B": USE_OVERWRITE, "C": USE_OVERWRITE}


def test_blanket_skip_stops_asking() -> None:
    asked: list[str] = []

    def ask(question: ConflictQuestion) -> str:
        asked.append(question.student_id)
        return CONFLICT_SKIP_ALL

    decisions, stopped = resolve_conflicts(
        [_question("A", "甲"), _question("B", "乙")], ask
    )
    assert asked == ["A"]
    assert stopped is True
    assert decisions == {"A": USE_SKIP, "B": USE_SKIP}


def test_dismissed_dialog_records_skip() -> None:
    decisions, stopped = resolve_conflicts(
        [_question("A", "甲"), _question("B", "乙")], lambda question: None
    )
    assert decisions == {"A": USE_SKIP, "B": USE_SKIP}
    assert stopped is False


def test_no_questions_means_no_dialog() -> None:
    """★ 没有冲突时**一个窗都不弹**（回调压根不该被调用）。"""
    calls: list[object] = []

    def ask(question: object) -> str:
        calls.append(question)
        return CONFLICT_OVERWRITE

    assert resolve_conflicts([], ask) == ({}, False)
    assert calls == []


def test_new_batch_id_increments_within_the_same_day() -> None:
    day = datetime(2026, 10, 27, 15, 30, 12)
    assert new_batch_id(day) == "20261027-1"
    assert new_batch_id(day, ["20261027-1"]) == "20261027-2"
    assert new_batch_id(day, ["20261027-1", "20261027-7"]) == "20261027-8"
    # 别的日期 / 别的批次号格式都不算数
    assert new_batch_id(day, ["20261026-9", "20261027-a", "乱七八糟"]) == "20261027-1"


# --------------------------------------------------------------- D3 与表 API 接头


class _FakeRow:
    def __init__(self, student: str, student_id: str) -> None:
        self.student = student
        self.student_id = student_id


class _FakeCourse:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeResult:
    def __init__(self, names: list[str], scores: dict[str, float | None]) -> None:
        self.course_scores = [
            type("C", (), {"name": n, "score": scores.get(n), "credit": 4.0})()
            for n in names
        ]


class _FakeParsed:
    def __init__(self, rows: list[_FakeRow], courses: list[str]) -> None:
        self.valid_rows = rows
        self.courses = [_FakeCourse(n) for n in courses]


def test_grade_history_records_keeps_blank_score_as_none() -> None:
    """★ 空 ≠ 0：没考的课写 ``None``，绝不写 0（红线 2）。"""
    from core.calculator import CalculationScope

    parsed = _FakeParsed([_FakeRow("张三", "2023001")], ["高等数学", "音乐鉴赏"])
    results = {CalculationScope.ALL: _FakeResult(["高等数学", "音乐鉴赏"],
                                                 {"高等数学": 88.0})}
    records = grade_history_records(parsed, results)
    assert [r["course_name"] for r in records] == ["高等数学", "音乐鉴赏"]
    assert records[0]["score"] == 88.0
    assert records[1]["score"] is None          # 不是 0
    assert all(r["student_id"] == "2023001" for r in records)
    assert records[0]["student_name"] == "张三"


def test_grade_history_records_before_calculation() -> None:
    """算之前也摊得出来（冲突检测就靠这一步，不需要先算加权）。"""
    parsed = _FakeParsed([_FakeRow("张三", "2023001")], ["高等数学", "线性代数"])
    records = grade_history_records(parsed)
    assert [r["course_name"] for r in records] == ["高等数学", "线性代数"]
    assert all(r["score"] is None and r["credit"] is None for r in records)


def test_conflicts_of_dedupes_and_ignores_blank_ids(db: GradeHistoryDB) -> None:
    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    records = [
        {"student_id": "2023001", "student_name": "张三", "course_name": "高等数学"},
        {"student_id": "2023001", "student_name": "张三", "course_name": "线性代数"},
        {"student_id": "2023002", "student_name": "李四", "course_name": "高等数学"},
        {"student_id": "", "student_name": "无学号", "course_name": "高等数学"},
    ]
    questions = conflicts_of(db, records, TERM_A)
    assert [q.student_id for q in questions] == ["2023001"]   # 去重 + 只挑有冲突的
    assert questions[0].student == "张三"
    assert questions[0].existing == 1
    assert conflicts_of(db, records, TERM_B) == []            # 换学期没有冲突


def test_end_to_end_second_import_with_skip(db: GradeHistoryDB) -> None:
    """把 D3 的两段接起来跑一遍：先摊行 → 查冲突 → 问 → 写库。"""
    from core.calculator import CalculationScope

    parsed = _FakeParsed(
        [_FakeRow("张三", "2023001"), _FakeRow("李四", "2023002")],
        ["高等数学"],
    )
    first = {CalculationScope.ALL: _FakeResult(["高等数学"], {"高等数学": 60.0})}
    db.apply(grade_history_records(parsed, first), semester=TERM_A, batch_id=BATCH_1)

    second = {CalculationScope.ALL: _FakeResult(["高等数学"], {"高等数学": 91.0})}
    records = grade_history_records(parsed, second)
    questions = conflicts_of(db, records, TERM_A)
    assert len(questions) == 2                    # 两个人都冲突
    decisions, _stopped = resolve_conflicts(
        questions, lambda q: CONFLICT_OVERWRITE if q.student_id == "2023001"
        else CONFLICT_DISMISSED
    )
    counts = db.apply(
        records,
        semester=TERM_A,
        batch_id=BATCH_2,
        policy=USE_OVERWRITE,
        on_conflict=lambda qs: {q.student_id: decisions.get(q.student_id, USE_SKIP)
                                for q in qs},
    )
    assert counts == {"overwritten": 1, "inserted": 1, "skipped": 1}
    assert fetch(db, "2023001", "高等数学", TERM_A)["score"] == 91.0
    assert fetch(db, "2023002", "高等数学", TERM_A)["score"] == 60.0


def test_enc_payload_is_decryptable_json(store: CryptoStore,
                                         db: GradeHistoryDB) -> None:
    """密文里装的是我们预期的结构（表名 + rows），不是随手塞的一坨。"""
    from core.crypto_store import load_encrypted
    from core.user_store import user_file

    db.apply([row("2023001", "高等数学", 88.0)], semester=TERM_A, batch_id=BATCH_1)
    path = user_file(store, USER, GRADE_HISTORY_FILE)
    assert path is not None and path.exists()
    payload = load_encrypted(path, root=store.root)
    assert payload["table"] == TABLE_GRADE_HISTORY
    assert [r["course_name"] for r in payload["rows"]] == ["高等数学"]
    # 落盘的 JSON 里没有姓名（它根本没进载荷）
    assert "张三" not in json.dumps(payload, ensure_ascii=False)
