"""补录与工作台的回归。姓名、联系方式和照片全为合成数据。"""

from pathlib import Path

import openpyxl
from PIL import Image
import pytest

from core.crypto_store import CryptoStore
from core.database import GradeHistoryDB, StudentStore
from core.sensitive_vault import SensitiveVault
from core.student_import import apply_import, plan_supplement
from ui.pages.workbench_page import read_overview


@pytest.fixture
def roster(tmp_path):
    store = CryptoStore(tmp_path / "data", iterations=2000)
    store.register("teacher", "synthetic-test")
    students = StudentStore(store=store, username="teacher")
    for sid, name, group in (("0001", "小林", "甲班"), ("0002", "小林", "乙班"),
                             ("003-4", "小夏", "甲班"), ("0005", "小秋", "乙班")):
        students.add(sid, name, group)
    vault = SensitiveVault(store, "teacher")
    vault.unlock("synthetic-test")
    return vault, students


def photo(path: Path, color=(90, 120, 160)):
    Image.new("RGB", (120, 200), color).save(path)


def phones(path: Path, *rows) -> Path:
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    book.save(path)
    return path


def test_photo_only_supports_combined_id_name_and_name_only(tmp_path, roster):
    vault, students = roster
    folder = tmp_path / "photos"
    folder.mkdir()
    for name in ("0001_小林.jpg", "小林.jpg", "小夏（003-4）.jpg", "小秋.jpg", "0002_小夏.jpg"):
        photo(folder / name)
    plan = plan_supplement(vault.store, vault.username, None, folder)
    found = {item.sid: item.photo.name for item in plan.students}
    assert found == {"0001": "0001_小林.jpg", "003-4": "小夏（003-4）.jpg", "0005": "小秋.jpg"}
    assert any("同名" in issue[3] and "小林.jpg" in issue[3] for issue in plan.issues)
    assert any("未匹配" in issue[3] and "0002_小夏.jpg" in issue[3] for issue in plan.issues)
    before = students.all_rows()
    count, _ = apply_import(vault, plan)
    assert count == 3 and students.all_rows() == before
    assert vault.student("003-4")["photo"]
    assert not vault.student("0002")

    selected = plan_supplement(vault.store, vault.username, None, folder, photo_class="乙班")
    assert {item.sid: item.photo.name for item in selected.students}["0002"] == "小林.jpg"


def test_phone_only_matches_id_links_name_and_accepts_class_to_disambiguate(tmp_path, roster):
    vault, students = roster
    sheet = phones(tmp_path / "phones.xlsx", ("学号", "学生姓名", "班级", "手机号", "父亲联系方式"),
                   ("0001", "小林", "甲班", 13800001111, ""),
                   ("", "小林", "乙班", "", "010-12345678"),
                   ("003-4", "小林", "", "13800002222", ""),
                   ("", "小林", "", "13800003333", ""),
                   ("", "小秋", "", "13800004444", ""),
                   ("DOES-NOT-EXIST", "小夏", "", "13800005555", ""))
    plan = plan_supplement(vault.store, vault.username, sheet, None)
    assert {item.sid for item in plan.students} == {"0001", "0002", "0005"}
    assert len(plan.issues) == 3
    before = students.all_rows()
    assert apply_import(vault, plan)[0] == 3
    assert students.all_rows() == before
    assert vault.phone("0001", "student_phone") == "13800001111"
    assert vault.phone("0002", "father_phone") == "010-12345678"
    assert vault.phone("003-4", "student_phone") == ""
    assert all("1380000" not in issue[3] for issue in plan.issues)


def test_supplement_keeps_existing_and_revalidates_identity(tmp_path, roster):
    vault, students = roster
    sheet = phones(tmp_path / "phones.xlsx", ("学号", "姓名", "本人电话"), ("0001", "小林", "13800001111"))
    original = plan_supplement(vault.store, vault.username, sheet, None)
    assert apply_import(vault, original)[0] == 1
    sheet = phones(sheet, ("学号", "本人电话", "母亲电话"), ("0001", "13800002222", "13900003333"))
    update = plan_supplement(vault.store, vault.username, sheet, None)
    assert apply_import(vault, update)[0] == 1
    assert vault.phone("0001", "student_phone") == "13800001111"
    assert vault.phone("0001", "mother_phone") == "13900003333"
    assert apply_import(vault, update, replace_existing=True)[0] == 1
    assert vault.phone("0001", "student_phone") == "13800002222"
    students.update("0001", name="已改名")
    count, issues = apply_import(vault, update, replace_existing=True)
    assert count == 0 and any("重新预览" in issue[3] for issue in issues)


def test_workbench_reads_only_current_accounts_roster_and_grades(roster):
    vault, students = roster
    students.archive("0005")
    peer = "other"
    vault.store.register(peer, "test-only-other")
    StudentStore(store=vault.store, username=peer).add("0001", "另一个账户", "丙班")
    grades = GradeHistoryDB(store=vault.store, username=vault.username)
    grades.apply([{"student_id":"0001","course_name":"合成课","score":90,"credit":3}],
                 semester="大一上", batch_id="synthetic")
    own = read_overview(vault.store, vault.username)
    other = read_overview(vault.store, peer)
    assert (own.students, own.archived, own.grades, own.terms) == (3, 1, 1, 1)
    assert dict(own.classes) == {"甲班": 2, "乙班": 1}
    assert other.students == 1 and other.grades == 0
    assert other.classes == (("丙班", 1),)


def test_custom_phone_template_handles_workbook_and_editable_rows(tmp_path, roster):
    from core.student_import import grid_workbook, phone_template, save_phone_template, write_template

    vault, students = roster
    columns = (("校内编号", "学号"), ("联系电话", "父亲电话"), ("部门备注", ""))
    save_phone_template(vault.store, vault.username, columns)
    assert phone_template(vault.store, vault.username) == columns
    xlsx = tmp_path / "custom.xlsx"
    write_template(xlsx, supplement=True, columns=columns)
    book = openpyxl.load_workbook(xlsx)
    assert tuple(cell.value for cell in book.active[1]) == ("校内编号", "联系电话", "部门备注")
    book.active.append(("0001", "0111-12345678", "自定义表头"))
    book.save(xlsx)
    plan = plan_supplement(vault.store, vault.username, xlsx, None, columns=columns)
    assert apply_import(vault, plan)[0] == 1
    assert vault.phone("0001", "father_phone") == "0111-12345678"
    assert vault.phone("0001", "student_phone") == ""
    grid = grid_workbook(columns, (("0002", "13800001111", ""), ("", "", "")))
    plan = plan_supplement(vault.store, vault.username, None, None, columns=columns, grid=grid)
    assert apply_import(vault, plan)[0] == 1
    assert vault.phone("0002", "father_phone") == "13800001111"
    assert students.get("0002")["name"] == "小林"


def test_ambiguous_photos_support_manual_assignment_and_skip(tmp_path, roster):
    from core.student_import import resolve_photos

    vault, _ = roster
    photo(tmp_path / "0001-小林.jpg")
    photo(tmp_path / "小林.jpg")
    photo(tmp_path / "没有学号.jpg")
    plan = plan_supplement(vault.store, vault.username, None, tmp_path)
    conflicts = {item.path.name: item for item in plan.photo_conflicts}
    assert conflicts["小林.jpg"].candidates == ("0001", "0002")
    with pytest.raises(ValueError, match="同一学生"):
        resolve_photos(plan, {tmp_path / "小林.jpg": "0001"})
    reviewed = resolve_photos(plan, {tmp_path / "小林.jpg": "0002"})
    assert apply_import(vault, reviewed)[0] == 2
    assert vault.student("0002")["photo"]
    assert any(issue[2] == "跳过照片" and "没有学号.jpg" in issue[3] for issue in reviewed.issues)
    assert not vault.student("0005")



def test_roster_template_needs_class_only_for_rows_without_one(tmp_path, roster):
    from core.student_import import MissingClassError, import_template, plan_import, save_import_template

    vault, students = roster
    columns = (("编号", "学号"), ("学生", "姓名"), ("称谓", "性别"))
    save_import_template(vault.store, vault.username, columns, "roster")
    assert import_template(vault.store, vault.username, "roster") == columns

    file = tmp_path / "自定义名册.xlsx"
    book = openpyxl.Workbook()
    book.active.append(["编号", "学生", "称谓"])
    book.active.append(["0007", "测试同学", "女"])
    book.active.append(["0008", "旧档无性别", ""])
    book.save(file)
    with pytest.raises(MissingClassError, match="默认班级"):
        plan_import(file, columns=columns)
    plan = plan_import(file, columns=columns, default_class="演示班", include_contacts=False)
    assert apply_import(vault, plan)[0] == 2
    assert students.get("0007")["class_name"] == "演示班"
    assert students.get("0007")["gender"] == "女"
    assert not students.get("0008")["gender"]
    students.update("0007", gender="男")
    assert students.get("0007")["gender"] == "男"


def test_phones_across_wps_style_workbook_and_title_rows(tmp_path, roster):
    import re
    from zipfile import ZipFile

    vault, students = roster
    for number, name in (("19001", "测试林"), ("19002", "测试王"), ("19003", "测试李")):
        students.add(number, name, "乙班")
    original = tmp_path / "源.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "甲班"
    sheet.append(["甲班最新名单"])
    sheet.append(["姓名", "学号", "联系方式（本人）", "父亲联系方式（父亲）", "母亲联系方式"])
    sheet.append(["小林", "0001", 13800001234, "格式错误", "010-12345678"])
    sheet = book.create_sheet("乙班")
    sheet.append(["乙班最新名单"])
    sheet.append(["姓名", "学号", "联系方式", "父亲联系方式", "母亲联系方式（母亲）"])
    for number, name in ((19001, "测试林"), (19002, "测试王"), (19003, "测试李")):
        sheet.append([number, name, 13800001234, None, 13900001234])
    book.save(original)
    phone = tmp_path / "原始名单.xlsx"
    with ZipFile(original) as clean, ZipFile(phone, "w") as broken:
        for part in clean.namelist():
            payload = clean.read(part)
            if part == "xl/styles.xml":
                payload, count = re.subn(rb"<fill><patternFill\s*/></fill>", b"<fill/>", payload, count=1)
                assert count == 1
            broken.writestr(part, payload)
    plan = plan_supplement(vault.store, vault.username, phone, None)
    assert len(plan.students) == 4
    assert any(kind == "列校正" and "乙班" in reason for _, _, kind, reason in plan.issues)
    assert any("父亲电话格式" in reason for _, _, _, reason in plan.issues)
    assert apply_import(vault, plan)[0] == 4
    assert vault.phone("0001", "guardian_phone") == ""
    assert vault.phone("0001", "mother_phone") == "010-12345678"
    assert vault.phone("19003", "mother_phone") == "13900001234"
