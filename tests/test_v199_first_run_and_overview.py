"""新账户引导与个人侧写全学期数据的回归。"""

from PySide6.QtWidgets import QApplication, QMessageBox

from core.archival import CourseSnapshot, SemesterRecord, StudentArchive
from core.crypto_store import CryptoStore
from ui.first_run import mark_first_run, show_first_run_guide
from ui.pages.student_overview import StudentOverviewPage
from ui.widgets.grade_trend import GradeTrendChart


def test_only_marked_new_account_sees_first_run_guide(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    vault = CryptoStore(root=tmp_path)
    vault.register("existing", "test-pass-123")
    vault.register("new", "test-pass-456")
    mark_first_run(vault, "new")
    shown = []

    def choose_import(dialog):
        shown.append(dialog.informativeText())
        next(button for button in dialog.buttons() if button.text() == "导入成绩表").click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", choose_import)
    assert not show_first_run_guide(None, vault, "existing")
    assert show_first_run_guide(None, vault, "new")
    assert not show_first_run_guide(None, vault, "new")
    assert len(shown) == 1 and "核对课程" in shown[0] and "找回码" in shown[0]
    app.processEvents()


def test_overview_and_review_cover_all_imported_terms():
    app = QApplication.instance() or QApplication([])
    page = StudentOverviewPage()
    terms = ("大一上", "大一下", "大二上", "大二下", "大三上")
    student = StudentArchive(
        key="test", student="不应进入提示词的姓名", roster_id="test-id",
        records=[
            SemesterRecord(term, "示例", courses=[CourseSnapshot(f"课程{index}", 3, "必修", 60 + index * 5)])
            for index, term in enumerate(terms)
        ],
    )
    try:
        page.set_context(None, "", student)
        assert page.findChild(GradeTrendChart) is None
        review = page._review_text.toPlainText()
        assert "纳入 5 个有成绩学期" in review
        assert all(term in review for term in terms)
        assert "加权均分 70.00" in review
        context = page._academic_context
        assert context.course_count == 5
        prompt = context.render("请回答", "课程0", 4000, len)
        assert all(term in prompt and f"课程{index}" in prompt for index, term in enumerate(terms))
        assert student.student not in prompt and "test-id" not in prompt
        restricted = context.render("", "请说课程0", len(context.summary) + 104, len)
        assert all(term in restricted for term in terms)
        assert "课程0" in restricted
        assert "选取" in restricted
    finally:
        page.close()
        app.processEvents()
