"""GPA 档位、账户兼容性与两个侧写页的联动。"""

import logging

import pytest

from core.archival import CourseSnapshot, SemesterRecord, StudentArchive
from core.crypto_store import CryptoStore
from core.gpa import (
    DEFAULT_SCHEME, GPA_SETTINGS_FILE, SchemeError, custom_id, gpa_detail,
    gpa_of, grade_point, load_settings, resolve_scheme, save_settings,
    scheme_options, validate_scheme,
)
from core.user_store import save_user_json, user_file


USER = "gpa-test"
OTHER = "gpa-other"


@pytest.fixture
def store(tmp_path):
    vault = CryptoStore(root=tmp_path)
    vault.register(USER, "example-pass-101")
    vault.register(OTHER, "example-pass-102")
    return vault


@pytest.fixture
def table():
    return {
        "id": "custom:12ab34", "name": "本校 GPA", "kind": "table",
        "rows": [[90.5, 4.2], [60, 1.1], [0, 0]], "round": 3,
    }


@pytest.mark.parametrize(("floor", "point", "below"), [
    (90, 4, 3.7), (85, 3.7, 3.3), (82, 3.3, 3), (78, 3, 2.7), (75, 2.7, 2.3),
    (72, 2.3, 2), (68, 2, 1.5), (64, 1.5, 1), (60, 1, 0), (0, 0, 0),
])
def test_original_points_are_unchanged(floor, point, below):
    assert grade_point(floor) == point
    assert grade_point(floor - 0.01) == below


@pytest.mark.parametrize(("scheme", "score", "expected"), [
    ("standard4_band", 90, 4), ("standard4_band", 89.9, 3),
    ("standard4_band", 80, 3), ("standard4_band", 79.9, 2),
    ("standard4_band", 70, 2), ("standard4_band", 69.9, 1),
    ("standard4_band", 60, 1), ("standard4_band", 59.9, 0),
    ("standard5_linear", 60, 1), ("standard5_linear", 85, 3.5),
    ("standard5_linear", 100, 5), ("standard5_linear", 105, 5),
    ("standard5_linear", 40, 0), ("scale43", 95, 4.3), ("scale43", 94.9, 4),
    ("wes_cn", 85, 4), ("wes_cn", 84.9, 3), ("wes_cn", 75, 3),
    ("wes_cn", 74.9, 2), ("wes_cn", 60, 2), ("wes_cn", 59.9, 0),
    ("five_level", " 优秀 ", 4), ("five_level", "良", 3), ("five_level", "中", 2),
    ("five_level", "及格", 1), ("five_level", "不及格", 0), ("five_level", 0, 0),
    ("five_level", 65, 1), ("five_level", 64.9, 0), ("five_level", "", None),
])
def test_presets(scheme, score, expected):
    assert grade_point(score, scheme) == pytest.approx(expected) if expected is not None else (
        grade_point(score, scheme) is None
    )


@pytest.mark.parametrize(("patch", "message"), [
    ({"rows": [[0, 0]]}, "至少"),
    ({"rows": [[90, 4], [90, 4], [0, 0]]}, "必须小于"),
    ({"rows": [[90, 4], [60, 1]]}, "最后一档"),
    ({"rows": [[90, 4], [70, 4.5], [0, 0]]}, "不能高于"),
    ({"rows": [[90, 5.1], [0, 0]]}, "绩点须在"),
    ({"rows": [[90.25, 4], [0, 0]]}, "最低分须在"),
    ({"rows": [[101, 4], [0, 0]]}, "最低分须在"),
    ({"name": " "}, "不能为空"), ({"name": "字" * 41}, "40"),
    ({"name": "__加粗__"}, "Markdown"), ({"round": 0.5}, "小数位数"),
    ({"grade_map": {"": 93}}, "等级映射"), ({"grade_map": {"90": 93}}, "等级映射"),
    ({"grade_map": {"优": "inf"}}, "等级映射"),
    ({"kind": "linear", "a": 0.1, "b": -5, "min_point": 3, "max_point": 2}, "上下限"),
    ({"kind": "linear", "a": "1/10", "b": -5, "min_point": 0, "max_point": 5}, "有限数字"),
])
def test_validation(table, patch, message):
    definition = {**table, **patch}
    assert any(message in error for error in validate_scheme(definition))
    with pytest.raises(SchemeError, match=message):
        resolve_scheme(definition)


def test_live_rules_skip_blank_and_preserve_zero(table):
    table["grade_map"] = {"优": 95}
    result = gpa_detail([
        {"score": "优", "credit": 3},
        {"score": 0, "credit": 2},
        {"score": "", "credit": 4},
        {"score": float("nan"), "credit": 9},
        {"score": 90, "credit": float("inf")},
    ], table)
    assert result["gpa"] == pytest.approx(2.52)
    assert (result["credits"], result["counted"], result["skipped"]) == (5, 2, 3)
    assert gpa_of([{"score": 60, "credit": 3}], {**table, "round": 0}) == 1.1
    assert grade_point(89, table) == 1.1
    assert grade_point(90.5, table) == 4.2
    assert grade_point(-1, table) == 0


def test_scheme_options_isolate_nested_mapping():
    options = scheme_options()
    options[-1]["grade_map"]["优"] = 0
    assert grade_point("优", "five_level") == 4


def test_account_settings_migrate_only_on_save_and_remain_encrypted(store, table):
    save_user_json(store, USER, GPA_SETTINGS_FILE, {"scheme": "standard4", "preserved": 1})
    path = user_file(store, USER, GPA_SETTINGS_FILE)
    old = path.read_bytes()
    assert load_settings(store, USER) == {"version": 1, "scheme": DEFAULT_SCHEME, "custom": []}
    assert path.read_bytes() == old
    selected = save_settings(store, USER, scheme=table["id"], custom=[table])
    assert load_settings(store, USER) == selected
    assert b"custom:12ab34" not in path.read_bytes()
    assert load_settings(store, OTHER)["custom"] == []
    assert not user_file(store, OTHER, GPA_SETTINGS_FILE).exists()

    # 编辑保持标识和当前选择，删除当前方案时同一次写入回到默认。
    table["name"] = "本校修订 GPA"
    edited = save_settings(store, USER, custom=[table])
    assert edited["scheme"] == table["id"]
    assert edited["custom"][0]["name"] == table["name"]
    assert save_settings(store, USER, custom=[])["scheme"] == DEFAULT_SCHEME
    assert load_settings(store, USER)["custom"] == []


def test_load_skips_invalid_entries_and_missing_selection(store, caplog, table):
    malformed = {**table, "id": "custom:f1a912", "rows": [[90, 4], [60, 1]]}
    save_user_json(store, USER, GPA_SETTINGS_FILE,
                   {"scheme": malformed["id"], "custom": [malformed, table, table]})
    with caplog.at_level(logging.WARNING):
        restored = load_settings(store, USER)
    assert restored["scheme"] == DEFAULT_SCHEME
    assert [item["id"] for item in restored["custom"]] == [table["id"]]
    assert "使用默认方案" in caplog.text


def test_custom_limit_and_invalid_save_preserve_previous_settings(store, table):
    items = [{**table, "id": f"custom:{i:06x}"} for i in range(11)]
    assert len(save_settings(store, USER, custom=items[:10])["custom"]) == 10
    path = user_file(store, USER, GPA_SETTINGS_FILE)
    previous = path.read_bytes()
    with pytest.raises(SchemeError, match="最多 10 个"):
        save_settings(store, USER, custom=items)
    with pytest.raises(SchemeError, match="标识不能重复"):
        save_settings(store, USER, custom=[table, table])
    with pytest.raises(SchemeError, match="未实现"):
        save_settings(store, USER, scheme=custom_id())
    assert path.read_bytes() == previous


def test_gpa_editor_and_switch_refresh_both_profiles(store, monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox
    from ui.main_window import MainWindow
    from ui.pages.settings_page import GpaSchemeDialog

    app = QApplication.instance() or QApplication([])
    window = MainWindow(store=store)
    window.set_user(USER)
    settings = window.settings_page
    archive = window.archive_page
    overview, semester = archive._overview_page, archive._semester_page
    student = StudentArchive(
        key="sample",
        student="测试学生", roster_id="0001",
        records=[
            SemesterRecord("大一上", "测试学生", courses=[CourseSnapshot("课甲", 3, "必修", 85)]),
            SemesterRecord("大一下", "测试学生", courses=[CourseSnapshot("课乙", 3, "必修", 90)]),
        ],
    )
    try:
        overview.set_context(store, USER, student, "测试班")
        overview._table.selectRow(0)
        semester.set_records([{"term": "大一上", "course": "课甲", "score": 85, "credit": 3}])
        semester.set_term("大一上")
        assert overview._metrics[2].text() == "3.85"
        assert semester._extra_gpa.text().startswith("GPA 3.70")
        combo = settings._gpa_combo
        combo.setCurrentIndex(combo.findData("standard4_band"))
        assert overview._metrics[2].text() == "3.50"
        assert overview._table.item(0, 3).text() == "3.00"
        assert overview._table.selectedItems()
        assert "标准 4.0（四档）" in overview._scheme_caption.text()
        assert semester._extra_gpa.text() == "GPA 3.00\n标准 4.0（四档）"

        def create(dialog):
            dialog._name.setText("线性试算")
            dialog._mode.setCurrentIndex(dialog._mode.findData("linear"))
            dialog._precision.setValue(3)
            dialog._mapping.setText("优秀=95")
            dialog._sample.setText("优秀")
            dialog._linear["min_point"].setText("6")
            assert "上下限" in dialog._errors.text()
            assert not dialog._buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
            dialog._linear["min_point"].setText("0")
            assert dialog._result.text() == "4.500"
            dialog.accept()
            return dialog.result()

        monkeypatch.setattr(GpaSchemeDialog, "exec", create)
        combo.setCurrentIndex(combo.count() - 1)
        scheme_id = combo.currentData()
        assert scheme_id.startswith("custom:")
        assert overview._metrics[2].text() == "3.750"
        assert semester._extra_gpa.text() == "GPA 3.500\n线性试算"

        def edit(dialog):
            dialog._name.setText("本校线性")
            dialog._linear["b"].setText("-6")
            dialog.accept()
            return dialog.result()

        monkeypatch.setattr(GpaSchemeDialog, "exec", edit)
        settings._gpa_edit.click()
        assert combo.currentData() == scheme_id
        assert overview._metrics[2].text() == "2.750"
        assert semester._extra_gpa.text() == "GPA 2.500\n本校线性"

        monkeypatch.setattr(GpaSchemeDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
        combo.setCurrentIndex(combo.count() - 1)
        assert combo.currentData() == scheme_id
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        settings._gpa_delete.click()
        assert combo.currentData() == DEFAULT_SCHEME
        assert overview._metrics[2].text() == "3.85"
        assert semester._extra_gpa.text().startswith("GPA 3.70")
        assert load_settings(store, USER)["custom"] == []

        save_settings(store, OTHER, scheme="wes_cn")
        window.set_user(OTHER)
        assert settings.load_gpa_scheme() == "wes_cn"
        assert overview._gpa_scheme["id"] == semester._gpa_scheme["id"] == "wes_cn"
    finally:
        window.close()
        app.processEvents()
