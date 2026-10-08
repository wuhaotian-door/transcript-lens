"""v1.9 德育分批量导入测试。

覆盖 :mod:`core.import_workers` 的四件事：**认列**（表头行不在第一行也认得出）、
**逐行校验**（Owner 定的四条失败规则 + 两条补漏 + 两类跳过）、**错误清单**
（UTF-8 BOM 的 CSV，Excel 打得开）、**落库**（一个班一个加密文件、键是班级
快照里的 ``key``、台账单独一个文件）。

★ 本文件会真的建一个临时加密库（``CryptoStore``），走的是和界面完全一样的
``save_moral`` / ``load_moral`` / ``load_class`` 路径 —— 不用替身，因为"导入
进去的分界面上看不看得见"正是这里最容易错的地方。

Owner 指定的验收样本：**30 行假数据、其中 3 行故意错**，见 ``batch_frame()``。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from core import scholarship
from core.crypto_store import CryptoStore
from core.import_workers import (
    BATCH_FILE,
    CSV_HEADER,
    ERROR_CSV_PREFIX,
    ImportFormatError,
    MORAL_MAX,
    MORAL_MIN,
    _cell_text,
    apply_plan,
    detect_columns,
    load_batches,
    new_batch_id,
    plan_import,
    read_table,
    run_import,
    student_key,
    write_error_csv,
)
from core.scholarship import ClassRecord
from ui.intentions import student_key as ui_student_key

USER = "张老师"
PASSWORD = "sch12345"
CLASS = "CS2023-1"

# 30 个名字，带序号造数据用（真名不进场：测试数据必须一眼看出是假的）
NAMES = [
    "张伟", "王芳", "李娜", "刘洋", "陈静", "杨帆", "赵磊", "黄敏", "周涛", "吴倩",
    "徐鹏", "孙丽", "马超", "朱婷", "胡军", "郭燕", "何伟", "高翔", "林霞", "罗刚",
    "郑爽", "梁静", "谢军", "宋佳", "唐勇", "许诺", "韩雪", "冯强", "曹颖", "邓超",
]

#: 故意错的三行（数据行的 1-based 序号）以及它们错在哪儿
BAD_ROWS = {5: "学号重复", 12: "姓名为空", 20: "分值不是数字"}


def sid(index: int) -> str:
    """第 index 个学生的学号（``2023001`` 起）。"""
    return f"2023{index:03d}"


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------


def data_rows() -> list[list[object]]:
    """30 行原始数据，含 3 行故意错（学号重复 / 姓名为空 / 分值不是数字）。"""
    rows: list[list[object]] = []
    for i, name in enumerate(NAMES, start=1):
        rows.append([i, sid(i), name, CLASS, 60 + (i * 7) % 41])
    rows[4] = [5, sid(3), "李娜", CLASS, 88]            # 学号重复（第 3 个人已经用了）
    rows[11] = [12, sid(12), "", CLASS, 75]             # 姓名为空
    rows[19] = [20, sid(20), "罗刚", CLASS, "优秀"]      # 分值不是数字
    rows[7][4] = 87.5                                   # 小数：88（银行家舍入）
    return rows


TITLE = "2025-2026学年第一学期德育分统计表"
HEADER = ["序号", "学号", "姓名", "班级", "德育分"]


def batch_frame() -> pd.DataFrame:
    """大标题 + 表头 + 30 行数据（和辅导员从 Excel 里导出来的一模一样）。"""
    return pd.DataFrame([ [TITLE, None, None, None, None], HEADER, *data_rows() ])


def small_frame(rows: list[list[object]]) -> pd.DataFrame:
    return pd.DataFrame([HEADER, *rows])


# ---------------------------------------------------------------------------
# 夹具：一个真的临时加密库 + 一个真的班级快照
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> CryptoStore:
    vault = CryptoStore(root=tmp_path / "vault")
    vault.register(USER, PASSWORD)
    return vault


@pytest.fixture()
def roster_store(store: CryptoStore) -> CryptoStore:
    """库里有 ``CS2023-1`` 这个班，30 个学生都带好了 ``key``。"""
    scholarship.save_class(store, USER, ClassRecord(
        class_name=CLASS, source="智育分析", term="2025-2026-1",
        students=[
            {"student": name, "student_id": sid(i), "key": f"sid:{sid(i)}",
             "academic": 80.0, "total_credit": 20.0, "courses": []}
            for i, name in enumerate(NAMES, start=1)
        ],
    ))
    return store


# ---------------------------------------------------------------------------
# 一、认列
# ---------------------------------------------------------------------------


def test_detect_columns_skips_title_and_blank() -> None:
    frame = batch_frame()
    columns = detect_columns(frame)
    assert columns.header_row == 1              # 第 0 行是大标题
    assert (columns.id_col, columns.name_col, columns.class_col, columns.score_col) == (1, 2, 3, 4)
    assert columns.missing == []
    assert "学号=第2列" in columns.describe()
    assert "班级=第4列" in columns.describe()


def test_detect_columns_accepts_table_without_class_column() -> None:
    frame = pd.DataFrame([["学号", "姓名", "德育分"], [sid(1), "张伟", 90]])
    columns = detect_columns(frame)
    assert columns.class_col is None
    assert columns.missing == []                # 班级列可以缺：界面上指定目标班级
    assert "班级=没找到" in columns.describe()


def test_detect_columns_reports_missing_columns_in_plain_words() -> None:
    frame = pd.DataFrame([["学号", "姓名", "备注"], [sid(1), "张伟", "无"]])
    with pytest.raises(ImportFormatError) as info:
        detect_columns(frame)
    assert "德育分" in str(info.value)


def test_detect_columns_gives_up_when_nothing_matches() -> None:
    frame = pd.DataFrame([["甲", "乙"], [1, 2]])
    with pytest.raises(ImportFormatError) as info:
        detect_columns(frame)
    assert "认不出表头" in str(info.value)


def test_header_row_not_first_row() -> None:
    """大标题 + 制表日期两行在前面，表头在第 3 行。"""
    frame = pd.DataFrame([
        ["XX中学德育分表", None, None, None],
        ["制表日期：2025-09-01", None, None, None],
        ["学号", "姓名", "班级", "德育分"],
        [sid(1), "张伟", CLASS, 88],
    ])
    columns = detect_columns(frame)
    assert columns.header_row == 2


# ---------------------------------------------------------------------------
# 二、小工具
# ---------------------------------------------------------------------------


def test_student_key_matches_ui() -> None:
    """core 里那份最小实现必须和 ui.intentions 算得一模一样。"""
    cases = [(sid(1), "张伟"), ("", "张伟"), ("  ", " 张伟 "), (sid(2), ""), ("", "")]
    for student_id, name in cases:
        assert student_key(student_id, name) == ui_student_key(student_id, name)
    assert student_key(sid(1), "张伟") == f"sid:{sid(1)}"
    assert student_key("", "张伟") == "name:张伟"


def test_cell_text_drops_the_float_tail_of_excel_ids() -> None:
    """Excel 里的数字学号读出来是 ``2023001.0``，直接 str() 就永远匹配不上。"""
    assert _cell_text(2023001.0) == "2023001"
    assert _cell_text(2023001) == "2023001"
    assert _cell_text(" 2023001 ") == "2023001"
    assert _cell_text(float("nan")) == ""
    assert _cell_text(None) == ""


def test_new_batch_id_is_sortable_and_unique() -> None:
    first, second = new_batch_id(), new_batch_id()
    assert first != second
    assert len(first.split("-")) == 3


# ---------------------------------------------------------------------------
# 三、逐行校验（Owner 的 30 行样本）
# ---------------------------------------------------------------------------


def test_thirty_rows_with_three_bad_ones(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "德育分.xlsx", known_classes=[CLASS], frame=batch_frame())
    assert plan.total_rows == 30
    assert len(plan.rows) == 27
    assert len(plan.failures) == 3
    assert plan.skipped == []
    assert plan.classes == [CLASS]

    reasons = {e.row_index + 1: e.reason for e in plan.failures}
    assert "学号重复" in reasons[7]                 # 第 5 个学生那行 -> 表内第 7 行
    assert "第 5 行" in reasons[7]                  # 指明和谁撞了
    assert reasons[14] == "姓名为空"
    assert "不是数字" in reasons[22]
    assert "优秀" in reasons[22]

    for bad in BAD_ROWS:
        assert any(e.row_index + 1 == bad + 2 for e in plan.failures)


def test_duplicate_id_is_only_rejected_in_the_same_class(tmp_path: Path) -> None:
    frame = pd.DataFrame([
        HEADER,
        [1, sid(1), "张伟", CLASS, 90],
        [2, sid(1), "张伟", "CS2023-2", 91],     # 另一个班，同一个学号：不算重复
    ])
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS, "CS2023-2"], frame=frame)
    assert len(plan.rows) == 2
    assert plan.failures == []


def test_blank_id_fails(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, "", "张伟", CLASS, 90]]))
    assert plan.failures[0].reason == "学号为空"


def test_unknown_class_fails_with_actionable_message(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=["CS2023-9"],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, 90]]))
    assert "还没建过" in plan.failures[0].reason
    assert plan.rows == []


def test_no_class_at_all_fails_every_row(tmp_path: Path) -> None:
    """一个班都没有的时候不许"先写进去再说"：那些分永远不会显示在任何人身上。"""
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, 90]]))
    assert len(plan.failures) == 1
    assert "一个班都还没建过" in plan.failures[0].reason


def test_default_class_fills_in_for_tables_without_a_class_column(tmp_path: Path) -> None:
    frame = pd.DataFrame([["学号", "姓名", "德育分"], [sid(1), "张伟", 90]])
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], default_class=CLASS, frame=frame)
    assert plan.rows[0].class_name == CLASS


def test_missing_default_class_reports_itself(tmp_path: Path) -> None:
    frame = pd.DataFrame([["学号", "姓名", "德育分"], [sid(1), "张伟", 90]])
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=frame)
    assert "没有班级" in plan.failures[0].reason


def test_score_out_of_range_is_a_failure_not_a_clamp(tmp_path: Path) -> None:
    """101 分写进去只会被 clamp 成 100：那是错的分，不如不写。"""
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, MORAL_MAX + 1],
                                          [2, sid(2), "王芳", CLASS, MORAL_MIN - 1]]))
    assert len(plan.failures) == 2
    assert all("超出" in e.reason for e in plan.failures)


def test_empty_score_fails(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, None]]))
    assert plan.failures[0].reason == "德育分是空的"


def test_blank_marker_score_is_empty_not_zero(tmp_path: Path) -> None:
    """缺考标记（``-``/``缺``）是"没分"，不是 0 分。"""
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, "-"]]))
    assert plan.failures[0].reason == "德育分是空的"


def test_fully_blank_row_is_skipped_not_failed(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, 90],
                                          [None, None, None, None, None]]))
    assert len(plan.rows) == 1
    assert plan.blank_rows == 1
    assert plan.failures == []
    assert plan.skipped[0].reason == "整行是空的"
    assert any("空" in note for note in plan.warnings)


def test_decimal_score_is_rounded_with_a_note(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    row = next(r for r in plan.rows if r.student_id == sid(8))
    assert row.score == 88.0                     # int(round(87.5)) == 88（和手填同一条路）
    assert plan.rounded == [sid(8)]
    assert any("取整" in note for note in plan.warnings)


def test_header_only_table_is_not_an_error(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=pd.DataFrame([HEADER]))
    assert plan.rows == []
    assert plan.failures == []
    assert any("只有表头" in note for note in plan.warnings)


def test_plan_import_never_touches_the_vault(tmp_path: Path, roster_store: CryptoStore) -> None:
    """预览阶段（点确认之前）一个字节都不许写进库。"""
    plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    assert scholarship.load_moral(roster_store, USER, CLASS) == {}
    assert load_batches(roster_store, USER) == []


# ---------------------------------------------------------------------------
# 四、错误清单
# ---------------------------------------------------------------------------


def test_error_csv_is_written_with_bom_and_one_based_rows(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    target = write_error_csv(plan.errors, tmp_path / "errors.csv")
    assert target is not None
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")        # 没有 BOM，Excel 打开全是乱码

    with target.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == CSV_HEADER
    assert [r[0] for r in rows[1:]] == ["7", "14", "22"]
    assert [r[1] for r in rows[1:]] == ["失败", "失败", "失败"]
    assert rows[2][2] == "姓名为空"


def test_error_csv_is_not_created_when_nothing_went_wrong(tmp_path: Path) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, 90]]))
    target = tmp_path / "errors.csv"
    assert write_error_csv(plan.errors, target) is None
    assert not target.exists()


# ---------------------------------------------------------------------------
# 五、落库
# ---------------------------------------------------------------------------


def test_apply_plan_writes_moral_by_key(tmp_path: Path, roster_store: CryptoStore) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    summary = apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)

    assert summary.succeeded == 27
    assert summary.added == 27
    assert summary.updated == 0
    assert summary.failed == 3
    assert summary.errors_path is not None
    assert summary.errors_path.name == f"{ERROR_CSV_PREFIX}B-1.csv"
    assert summary.errors_path.exists()

    moral = scholarship.load_moral(roster_store, USER, CLASS)
    assert len(moral) == 27
    assert moral[f"sid:{sid(1)}"] == 60 + 7      # 60 + (1*7)%41 = 67
    assert moral[f"sid:{sid(3)}"] == 60 + 21
    assert f"sid:{sid(5)}" not in moral          # 那行学号重复，被拒了
    assert f"sid:{sid(12)}" not in moral         # 那行姓名为空


def test_second_import_counts_as_update_not_add(tmp_path: Path, roster_store: CryptoStore) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    summary = apply_plan(roster_store, USER, plan, batch_id="B-2", error_dir=tmp_path)
    assert (summary.added, summary.updated) == (0, 27)
    assert len(scholarship.load_moral(roster_store, USER, CLASS)) == 27


def test_manual_score_is_reported_as_overwritten(tmp_path: Path, roster_store: CryptoStore) -> None:
    """手填过的分被导入覆盖，必须在"覆盖"里报出来 —— 追加 ≠ 悄悄改分数。"""
    scholarship.save_moral(roster_store, USER, CLASS, {f"sid:{sid(1)}": 100})
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    summary = apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    assert (summary.added, summary.updated) == (26, 1)
    assert scholarship.load_moral(roster_store, USER, CLASS)[f"sid:{sid(1)}"] == 67


def test_ids_outside_the_roster_are_skipped_not_written(tmp_path: Path, store: CryptoStore) -> None:
    """名单里没有的学号绝不写：写了也没人认领，只会让库里多出永不显示的分。"""
    scholarship.save_class(store, USER, ClassRecord(
        class_name=CLASS, students=[
            {"student": "张伟", "student_id": sid(1), "key": f"sid:{sid(1)}"},
        ],
    ))
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, 90],
                                          [2, sid(2), "王芳", CLASS, 80]]))
    summary = apply_plan(store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    assert (summary.added, summary.skipped) == (1, 1)
    assert scholarship.load_moral(store, USER, CLASS) == {f"sid:{sid(1)}": 90}

    with summary.errors_path.open(encoding="utf-8-sig", newline="") as fh:  # type: ignore[union-attr]
        rows = list(csv.reader(fh))
    assert rows[1][1:3] == ["跳过", "这个学号不在「CS2023-1」的名单里"]


def test_empty_roster_falls_back_to_plain_key(tmp_path: Path, store: CryptoStore) -> None:
    """班级文件在但名单是空的（没有学号）→ 退回 ``sid:<学号>`` 拼法。"""
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "张伟", CLASS, 90]]))
    summary = apply_plan(store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    assert summary.added == 1
    assert scholarship.load_moral(store, USER, CLASS) == {f"sid:{sid(1)}": 90}


def test_two_classes_land_in_two_files(tmp_path: Path, store: CryptoStore) -> None:
    for name in (CLASS, "CS2023-2"):
        scholarship.save_class(store, USER, ClassRecord(
            class_name=name, students=[
                {"student": "张伟", "student_id": sid(1), "key": f"sid:{sid(1)}"},
            ],
        ))
    frame = small_frame([[1, sid(1), "张伟", CLASS, 90],
                         [2, sid(1), "张伟", "CS2023-2", 70]])
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS, "CS2023-2"], frame=frame)
    summary = apply_plan(store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    assert summary.classes == [CLASS, "CS2023-2"]
    assert scholarship.load_moral(store, USER, CLASS) == {f"sid:{sid(1)}": 90}
    assert scholarship.load_moral(store, USER, "CS2023-2") == {f"sid:{sid(1)}": 70}


def test_batch_ledger_is_a_separate_file(tmp_path: Path, roster_store: CryptoStore) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    summary = apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)

    batches = load_batches(roster_store, USER)
    assert len(batches) == 1
    assert batches[0]["batch_id"] == "B-1"
    assert batches[0]["added"] == 27
    assert batches[0]["failed"] == 3
    assert batches[0]["classes"] == [CLASS]

    # 台账文件名绝不能长得像 class_*.json.enc，否则 list_classes 会把它当成一个班，
    # 而且后缀必须是 .json.enc（save_user_json 拒绝落明文 .json）
    assert BATCH_FILE == "moral_batches.json.enc"
    assert not BATCH_FILE.startswith("class_")
    assert [c.class_name for c in scholarship.list_classes(roster_store, USER)] == [CLASS]


def test_ledger_survives_a_later_moral_save(tmp_path: Path, roster_store: CryptoStore) -> None:
    """``save_moral`` 只写 ``{"scores": ...}``：台账硬塞进去的话会被下一次保存抹掉。"""
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    scholarship.save_moral(roster_store, USER, CLASS, {f"sid:{sid(1)}": 100})
    assert len(load_batches(roster_store, USER)) == 1
    assert scholarship.load_moral(roster_store, USER, CLASS)[f"sid:{sid(1)}"] == 100


def test_ledger_deduplicates_the_same_batch_and_keeps_order(tmp_path: Path,
                                                            roster_store: CryptoStore) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    apply_plan(roster_store, USER, plan, batch_id="B-2", error_dir=tmp_path)
    assert [b["batch_id"] for b in load_batches(roster_store, USER)] == ["B-1", "B-2"]


def test_nothing_saved_means_no_ledger_entry(tmp_path: Path, store: CryptoStore) -> None:
    """一行都没写成（班级名对不上）→ 不记台账：台账是"我导进去了什么"。"""
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS],
                       frame=small_frame([[1, sid(1), "", CLASS, 90]]))
    summary = apply_plan(store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    assert summary.succeeded == 0
    assert load_batches(store, USER) == []


# ---------------------------------------------------------------------------
# 六、汇总文案
# ---------------------------------------------------------------------------


def test_summary_text_is_plain_and_counts_everything(tmp_path: Path,
                                                     roster_store: CryptoStore) -> None:
    plan = plan_import(tmp_path / "a.xlsx", known_classes=[CLASS], frame=batch_frame())
    summary = apply_plan(roster_store, USER, plan, batch_id="B-1", error_dir=tmp_path)
    text = summary.as_text()
    assert "成功 27 行（新增 27 / 覆盖 0）" in text
    assert "失败 3 行" in text
    assert "跳过 0 行" in text
    assert "涉及班级：CS2023-1" in text
    for mark in ("**", "##", "|", "`", "- ["):
        assert mark not in text                  # 界面不许出现 Markdown 记号


# ---------------------------------------------------------------------------
# 七、一步到位 + 真文件（磁盘路径）
# ---------------------------------------------------------------------------


def test_run_import_reports_progress_synchronously(tmp_path: Path,
                                                   roster_store: CryptoStore) -> None:
    seen: list[tuple[int, int, str]] = []
    summary = run_import(tmp_path / "a.xlsx", roster_store, USER, known_classes=[CLASS],
                         error_dir=tmp_path, frame=batch_frame(),
                         progress=lambda done, total, message: seen.append((done, total, message)))
    assert summary.succeeded == 27
    assert [step for step, _total, _msg in seen] == [0, 1, 2, 3]
    assert all(total == 3 for _step, total, _msg in seen)
    assert "正在读表" in seen[0][2]


def test_read_table_rejects_a_missing_file_in_plain_words(tmp_path: Path) -> None:
    with pytest.raises(ImportFormatError) as info:
        read_table(tmp_path / "根本没有这个文件.xlsx")
    assert "找不到文件" in str(info.value)


def test_end_to_end_xlsx_on_disk(tmp_path: Path, roster_store: CryptoStore) -> None:
    """真 .xlsx 走一遍：魔数选引擎读进来 → 认得列 → 落库（学号是数字的那种）。"""
    frame = pd.DataFrame([[TITLE, None, None, None, None], HEADER])
    numeric = [[i, int(sid(i)), name, CLASS, 60 + (i * 7) % 41]
               for i, name in enumerate(NAMES, start=1)]
    numeric[11] = [12, int(sid(12)), None, CLASS, 75]        # 姓名为空
    frame = pd.concat([frame, pd.DataFrame(numeric)], ignore_index=True)

    target = tmp_path / "德育分.xlsx"
    frame.to_excel(target, index=False, header=False)

    summary = run_import(target, roster_store, USER, known_classes=[CLASS], error_dir=tmp_path)
    assert summary.total == 30
    assert summary.succeeded == 29
    assert summary.failed == 1
    assert summary.errors_path is not None and summary.errors_path.exists()

    moral = scholarship.load_moral(roster_store, USER, CLASS)
    assert len(moral) == 29
    assert moral[f"sid:{sid(1)}"] == 67


def test_end_to_end_csv_on_disk(tmp_path: Path, roster_store: CryptoStore) -> None:
    """CSV 也走同一个入口（魔数选引擎），不是另写一条读表路径。"""
    target = tmp_path / "德育分.csv"
    frame = pd.DataFrame([HEADER, *data_rows()])
    frame.to_csv(target, index=False, header=False, encoding="utf-8-sig")

    summary = run_import(target, roster_store, USER, known_classes=[CLASS], error_dir=tmp_path)
    assert summary.succeeded == 27
    assert summary.failed == 3
    assert len(scholarship.load_moral(roster_store, USER, CLASS)) == 27


def test_end_to_end_persists_across_a_reopened_vault(tmp_path: Path,
                                                    roster_store: CryptoStore) -> None:
    """重开一次加密库（模拟重启程序）：导入的分必须还在。"""
    run_import(tmp_path / "a.xlsx", roster_store, USER, known_classes=[CLASS],
               error_dir=tmp_path, frame=batch_frame())
    reopened = CryptoStore(root=tmp_path / "vault")
    assert len(scholarship.load_moral(reopened, USER, CLASS)) == 27
    assert len(load_batches(reopened, USER)) == 1
