"""v1.9.2：学生主档（students 表）的 CRUD 与「改学号」全量迁移的判据。

被测评块：

1. ``core.database.StudentStore`` —— 学号唯一 / 搜索（姓名模糊・学号前缀・班级模糊）/
   归档隐藏 / 新建・编辑・归档・恢复・删除 / 导入补档 ``upsert_import``
2. ``core.database.migrate_student_id`` —— 改学号时**成绩 + 侧写 + 学期侧写 +
   聊天记录 + 德育分 + 意愿标签**必须一起迁走（少迁一样就是静默丢数据）
3. 审计日志 —— 学生主档的六种操作都留痕，``detail`` 只说"做了什么"，
   **永不写分数**（那条成绩不在日志里，是这一版的红线之一）

★ 与 ``tests/test_grade_delete.py`` 同一套夹具习惯：每个用例一个临时 vault，
  先 ``register()`` 再写库（AGENTS §20c：账户不存在时 ``save_user_json`` 直接抛）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.crypto_store import CryptoStore
from core.database import (
    OP_STUDENT_ADD,
    OP_STUDENT_DELETE,
    OP_STUDENT_MIGRATE,
    STUDENT_OPS,
    STUDENTS_FILE,
    TABLE_STUDENTS,
    DuplicateStudentError,
    GradeHistoryDB,
    ProfileStore,
    StudentStore,
    library_inventory,
    migrate_student_id,
)
from core.scholarship import CLASS_PREFIX, load_moral, moral_filename, save_moral
from core.user_store import list_user_json, load_user_json, save_user_json, user_file

USER = "验收员"
PASSWORD = "tl-test-2026"
CLASS = "计科2301"
TERM_A = "大一上"
TERM_B = "大一下"
SID_A = "sid:20230101"
SID_B = "sid:20230102"
SID_NEW = "sid:20230199"
MATH = "高等数学"
ENGLISH = "专业英语"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> CryptoStore:
    vault = CryptoStore(root=tmp_path / "vault")
    vault.register(USER, PASSWORD)          # ★ 必须先 register（AGENTS §20c）
    return vault


@pytest.fixture()
def students(vault: CryptoStore) -> StudentStore:
    return StudentStore(username=USER, store=vault)


def make(students: StudentStore, sid: str = SID_A, name: str = "张三",
         klass: str = CLASS, **extra) -> dict:
    return students.add(student_id=sid, name=name, class_name=klass, **extra)


def chat_path(vault: CryptoStore, bare: str) -> Path:
    base = user_file(vault, USER, "__probe__.json.enc")
    assert base is not None
    return base.parent / "cache" / f"chat_history_{bare}.json.enc"


# ---------------------------------------------------------------------------
# 1. 主档：新建 / 读 / 唯一性 / 归一
# ---------------------------------------------------------------------------


def test_新建学生后能按学号取回(students: StudentStore) -> None:
    row = make(students)
    assert row["student_id"] == SID_A
    assert row["name"] == "张三"
    assert students.has(SID_A) is True
    assert students.get(SID_A)["class_name"] == CLASS
    assert students.count() == 1
    assert students.get("sid:99999999") is None


def test_学号重复必须抛且带学号(students: StudentStore) -> None:
    make(students)
    with pytest.raises(DuplicateStudentError) as err:
        make(students, name="李四")
    assert "20230101" in str(err.value)
    assert students.count() == 1


def test_裸学号自动补sid前缀(students: StudentStore) -> None:
    row = students.add(student_id="20230505", name="王五")
    assert row["student_id"] == "sid:20230505"
    assert students.has("20230505") is True
    assert students.get("sid:20230505")["name"] == "王五"


def test_空学号被拒绝(students: StudentStore) -> None:
    with pytest.raises(ValueError):
        students.add(student_id="   ", name="无学号")


def test_get返回的是副本改它不影响库(students: StudentStore) -> None:
    make(students)
    copy = students.get(SID_A)
    copy["name"] = "篡改"
    assert students.get(SID_A)["name"] == "张三"


def test_列表按学号排序(students: StudentStore) -> None:
    make(students, SID_B, "李四")
    make(students, SID_A, "张三")
    assert [r["student_id"] for r in students.rows()] == [SID_A, SID_B]


def test_落盘是密文且不含姓名明文(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    path = user_file(vault, USER, STUDENTS_FILE)
    assert path is not None and path.exists()
    raw = path.read_bytes()
    assert "张三".encode("utf-8") not in raw
    assert b"student_id" not in raw          # 连字段名都看不出来（整份是密文）


def test_换一个实例也读得到(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    other = StudentStore(username=USER, store=vault)
    assert other.count() == 1
    assert other.get(SID_A)["name"] == "张三"


# ---------------------------------------------------------------------------
# 2. 编辑 / 归档 / 恢复 / 删除
# ---------------------------------------------------------------------------


def test_编辑姓名班级备注(students: StudentStore) -> None:
    make(students)
    row = students.update(SID_A, name="张三丰", class_name="计科2302", note="转专业")
    assert row["name"] == "张三丰"
    assert row["class_name"] == "计科2302"
    assert row["note"] == "转专业"
    assert students.get(SID_A)["name"] == "张三丰"


def test_编辑不存在的学号抛KeyError(students: StudentStore) -> None:
    with pytest.raises(KeyError):
        students.update(SID_A, name="张三")


def test_不允许用update改学号(students: StudentStore) -> None:
    make(students)
    with pytest.raises(ValueError) as err:
        students.update(SID_A, student_id=SID_B)
    assert "migrate_student_id" in str(err.value)


def test_归档后默认列表里没有(students: StudentStore) -> None:
    make(students)
    students.archive(SID_A)
    assert students.rows(include_archived=False) == []
    assert [r["student_id"] for r in students.rows()] == [SID_A]   # 默认连归档的一起给
    assert students.get(SID_A)["archived"] is True
    assert students.count(include_archived=False) == 0


def test_归档的学生默认搜不到_输入已归档才出现(students: StudentStore) -> None:
    make(students)
    students.archive(SID_A)
    assert students.search("张") == []
    assert [r["student_id"] for r in students.search("已归档")] == [SID_A]
    assert [r["student_id"] for r in students.search("已归档 张")] == [SID_A]


def test_恢复归档(students: StudentStore) -> None:
    make(students)
    students.archive(SID_A)
    row = students.restore(SID_A)
    assert row["archived"] is False
    assert len(students.rows(include_archived=False)) == 1


def test_删除返回真假(students: StudentStore) -> None:
    make(students)
    assert students.remove(SID_A) is True
    assert students.remove(SID_A) is False
    assert students.count() == 0


# ---------------------------------------------------------------------------
# 3. 搜索
# ---------------------------------------------------------------------------


def test_搜索姓名模糊(students: StudentStore) -> None:
    make(students, SID_A, "张三")
    make(students, SID_B, "李四")
    assert [r["name"] for r in students.search("张")] == ["张三"]


def test_搜索学号前缀(students: StudentStore) -> None:
    make(students, SID_A, "张三")
    make(students, SID_B, "李四")
    assert [r["name"] for r in students.search("20230102")] == ["李四"]
    assert len(students.search("2023")) == 2


def test_搜索班级模糊(students: StudentStore) -> None:
    make(students, SID_A, "张三", "计科2301")
    make(students, SID_B, "李四", "软工2302")
    assert [r["name"] for r in students.search("软工")] == ["李四"]


def test_搜索_limit与空串(students: StudentStore) -> None:
    make(students, SID_A, "张三")
    make(students, SID_B, "李四")
    assert len(students.search("")) == 2
    assert len(students.search("", limit=1)) == 1


# ---------------------------------------------------------------------------
# 4. 导入补档
# ---------------------------------------------------------------------------


def test_导入补档会新建(students: StudentStore) -> None:
    row = students.upsert_import(student_id="20230707", name="赵六", class_name=CLASS)
    assert row["student_id"] == "sid:20230707"
    assert row["source"] == "import"


def test_导入补档只补空字段(students: StudentStore) -> None:
    """★ 任务书 B：已存在的行**只补空字段** —— 手工填过的姓名/班级不被导入覆盖。"""
    make(students, SID_A, "张三", "计科2301", note="重点关注")
    students.archive(SID_A)
    row = students.upsert_import(student_id=SID_A, name="张三丰", class_name="计科9999")
    assert row["name"] == "张三"               # 已有非空姓名保持原样
    assert row["class_name"] == "计科2301"     # 已有非空班级保持原样
    assert row["note"] == "重点关注"           # 备注不被导入覆盖
    assert row["archived"] is True             # 归档状态不被导入复活
    # 空字段才补：李四建档时没填班级
    students.add(SID_B, "李四")
    filled = students.upsert_import(student_id=SID_B, name="李四", class_name="软工2302")
    assert filled["class_name"] == "软工2302"


# ---------------------------------------------------------------------------
# 5. 审计日志
# ---------------------------------------------------------------------------


def test_主档操作都留审计且不写分数(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    students.update(SID_A, name="张三丰")
    students.archive(SID_A)
    students.remove(SID_A)
    log = GradeHistoryDB(username=USER, store=vault).audit()
    ops = [row["operation"] for row in log.changes()]
    assert OP_STUDENT_ADD in ops
    assert OP_STUDENT_DELETE in ops
    assert set(ops) <= set(STUDENT_OPS) | {"insert", "overwrite", "delete"}
    for row in log.changes():
        assert row["detail"] == "" or isinstance(row["detail"], str)
    # detail 里不许出现分数（这里只校验字段口径：没有 score 字段）
    assert all("score" not in row for row in log.changes())


def test_审计detail能写能读(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    log = GradeHistoryDB(username=USER, store=vault).audit()
    written = log.record(OP_STUDENT_ADD, student_id=SID_A, detail="新建学生 张三")
    assert written is not None and written["detail"] == "新建学生 张三"
    assert log.changes()[0]["detail"] == "新建学生 张三"


# ---------------------------------------------------------------------------
# 6. 改学号：全量迁移
# ---------------------------------------------------------------------------


def seed_grade(db: GradeHistoryDB) -> None:
    db.apply([
        {"student_id": SID_A, "course_name": MATH, "score": 88.0, "credit": 4.0},
        {"student_id": SID_A, "course_name": ENGLISH, "score": 91.0, "credit": 2.0},
    ], semester=TERM_A, batch_id="b1")


def test_改学号把成绩一起迁走(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    db = GradeHistoryDB(username=USER, store=vault)
    seed_grade(db)
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    assert report.grades == 2
    assert report.student == 1
    assert db.count() == 2
    assert all(row["student_id"] == SID_NEW for row in db.rows())
    assert students.has(SID_A) is False and students.has(SID_NEW) is True


def test_改学号撞键时保留新学号那份并如实汇报(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    db = GradeHistoryDB(username=USER, store=vault)
    seed_grade(db)
    db.apply([{"student_id": SID_NEW, "course_name": MATH, "score": 100.0, "credit": 4.0}],
             semester=TERM_A, batch_id="b2")
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    assert report.grade_dropped == 1
    math = [r for r in db.rows() if r["course_name"] == MATH and r["semester"] == TERM_A]
    assert len(math) == 1 and math[0]["score"] == 100.0   # 新数据不被旧覆盖
    assert "冲突" in report.as_text() or "保留" in report.as_text()


def test_改学号把侧写与学期侧写一起迁(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    profiles = ProfileStore(username=USER, store=vault)
    profiles.save_side_text(SID_A, "整体评价")
    profiles.save_semester_side_text(SID_A, TERM_A, "本学期评价")
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    assert report.profiles == 1
    fresh = ProfileStore(username=USER, store=vault)   # ★ ProfileStore 带内存缓存，迁移后要重读
    assert fresh.side_text(SID_NEW) == "整体评价"
    assert fresh.semester_side_text(SID_NEW, TERM_A) == "本学期评价"
    assert fresh.side_text(SID_A) == ""


def test_改学号把聊天记录文件改名(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    old = chat_path(vault, "20230101")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"\x01\x02\x03")
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    assert report.chats == 1
    assert not old.exists()
    assert chat_path(vault, "20230199").exists()


def test_改学号把德育分键一起迁(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    klass = "CS2301"                         # 班级号只允许字母数字与短横线（§15）
    save_moral(vault, USER, klass, {SID_A: 87})
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    assert report.moral == 1
    moral = load_moral(vault, USER, klass)
    assert moral.get(SID_NEW) == 87
    assert SID_A not in moral
    assert moral_filename(klass).startswith(CLASS_PREFIX)


def test_改学号把意愿标签一起迁(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    # 真实载荷形状是 {"intentions": {sid:<学号>: "考研"}}（见 ui.intentions.load_intentions）
    save_user_json(vault, USER, "intentions.json.enc", {"intentions": {SID_A: "考研"}})
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    assert report.intentions == 1
    payload = load_user_json(vault, USER, "intentions.json.enc", None) or {}
    table = payload.get("intentions") or {}
    assert table.get(SID_NEW) == "考研"
    assert SID_A not in table


def test_改学号汇总文案含新旧学号且无Markdown(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    GradeHistoryDB(username=USER, store=vault)
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    text = report.as_text()
    assert "20230101" in text and "20230199" in text
    assert "**" not in text and "`" not in text
    assert report.total() >= 1


def test_改学号本身也进审计(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    rows = GradeHistoryDB(username=USER, store=vault).audit().changes()
    migrated = [row for row in rows if row["operation"] == OP_STUDENT_MIGRATE]
    assert migrated, "改学号没留审计"
    assert migrated[0]["student_id"] == SID_NEW


def test_同一个学号不改(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    report = migrate_student_id(SID_A, SID_A, store=vault, username=USER)
    assert report.total() == 0


def test_空学号抛ValueError(vault: CryptoStore) -> None:
    with pytest.raises(ValueError):
        migrate_student_id("", SID_NEW, store=vault, username=USER)


def test_迁一个主档里没有的学号不崩(vault: CryptoStore) -> None:
    report = migrate_student_id("sid:40404040", SID_NEW, store=vault, username=USER)
    assert report.total() == 0
    assert "无关联数据" in report.as_text()


# ---------------------------------------------------------------------------
# 7. 与「删除成绩库」盘点的关系
# ---------------------------------------------------------------------------


def test_主档不算成绩库(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    inv = library_inventory(vault, USER)
    assert inv["student_rows"] == 1
    assert inv["grade_rows"] == 0
    # 只有主档、没有成绩 → 界面不该说"有成绩库可删"
    assert inv["has_data"] is False


def test_盘点会数主档(vault: CryptoStore, students: StudentStore) -> None:
    make(students, SID_A, "张三")
    make(students, SID_B, "李四")
    assert library_inventory(vault, USER)["student_rows"] == 2


def test_主档文件在用户目录里(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    names = list_user_json(vault, USER)
    assert STUDENTS_FILE in names
    assert TABLE_STUDENTS == "students"


# ---------------------------------------------------------------------------
# 8. 任务书补充判据（v1.9.2 数据层）
# ---------------------------------------------------------------------------


def test_搜索支持archived_only参数(students: StudentStore) -> None:
    make(students, SID_A, "张三")
    make(students, SID_B, "李四")
    students.archive(SID_B)
    assert [r["name"] for r in students.search(archived_only=True)] == ["李四"]
    assert [r["name"] for r in students.search("", include_archived=True)] == ["张三", "李四"]
    assert students.search("归档") != []          # 单独一个「归档」= 列出归档的


def test_rows三种排序键(students: StudentStore) -> None:
    make(students, "sid:20230005", "末位", "软工2302")
    make(students, "sid:20230002", "乙", "计科2301")
    make(students, "sid:20230001", "甲", "计科2301")
    students.archive("sid:20230005")
    ordered = [r["student_id"] for r in students.rows()]
    # 在册的在前（同班按学号），归档的最后
    assert ordered == ["sid:20230001", "sid:20230002", "sid:20230005"]


def test_add记录来源与时间(vault: CryptoStore, students: StudentStore) -> None:
    row = students.add(SID_A, "张三", note="手工建档")
    assert row["source"] == "manual"
    assert row["created_at"] and row["updated_at"]
    assert row["archived"] is False
    imported = students.add(SID_B, "李四", source="import")
    assert imported["source"] == "import"


def test_审计detail最多200字且绝不写分数(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    log = GradeHistoryDB(username=USER, store=vault).audit()
    long_detail = "说明" * 300
    written = log.record_student("student_edit", student_id=SID_A, detail=long_detail)
    assert written is not None
    assert len(written["detail"]) <= 200
    assert written["detail"] == long_detail[:200]
    assert "score" not in written and "分" not in written["detail"][-5:]
    # 不认识的类型 → 不写、不抛
    assert log.record_student("student_nope", student_id=SID_A) is None


def test_审计覆盖添加编辑归档恢复删除(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    students.update(SID_A, name="张三丰")
    students.archive(SID_A)
    students.restore(SID_A)
    students.remove(SID_A)
    ops = [row["operation"] for row in GradeHistoryDB(username=USER, store=vault).audit().changes()]
    for op in ("student_add", "student_edit", "student_archive",
               "student_restore", "student_delete"):
        assert op in ops, f"缺审计：{op}"


def test_学号迁移聊天记录撞名时留bak不删数据(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    old = chat_path(vault, "20230101")
    new = chat_path(vault, "20230199")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    report = migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    assert report.chats == 0
    assert report.files and report.files[0].endswith(".bak")
    assert new.read_bytes() == b"new"                     # 目标保留
    assert (old.parent / report.files[0]).read_bytes() == b"old"   # 旧数据改名保留
    assert "bak" in report.as_text()


def test_学号迁移的审计detail不含分数(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    db = GradeHistoryDB(username=USER, store=vault)
    seed_grade(db)
    migrate_student_id(SID_A, SID_NEW, store=vault, username=USER)
    rows = [row for row in db.audit().changes() if row["operation"] == OP_STUDENT_MIGRATE]
    assert rows
    detail = rows[-1]["detail"]
    assert "成绩 2 条" in detail
    assert "88" not in detail and "91" not in detail      # ★ 审计不写分数


def test_清库会清掉学生谈话记录(vault: CryptoStore, students: StudentStore) -> None:
    from core.database import clear_library

    make(students)
    GradeHistoryDB(username=USER, store=vault).apply(
        [{"student_id": SID_A, "course_name": MATH, "score": 90.0, "credit": 4.0}],
        semester=TERM_A, batch_id="b9")
    chat = chat_path(vault, "20230101")
    chat.parent.mkdir(parents=True, exist_ok=True)
    chat.write_bytes(b"chat")
    assert library_inventory(vault, USER)["chat_files"] == 1
    report = clear_library(vault, USER)
    assert not chat.exists()
    assert any(name.endswith(".json.enc") and "chat_history" in name for name in report["files"])
    assert report["inventory"]["grade_rows"] == 0          # 清完之后的盘点
    assert report["inventory"]["chat_files"] == 0


def test_名册主档rename_student两个薄封装(vault: CryptoStore, students: StudentStore) -> None:
    make(students)
    GradeHistoryDB(username=USER, store=vault).apply(
        [{"student_id": SID_A, "course_name": MATH, "score": 90.0, "credit": 4.0}],
        semester=TERM_A, batch_id="b10")
    assert GradeHistoryDB(username=USER, store=vault).rename_student(SID_A, SID_B) == 1
    assert ProfileStore(username=USER, store=vault).rename_student(SID_A, SID_B) is False
