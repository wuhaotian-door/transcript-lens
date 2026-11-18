"""v1.9.8 使用说明书配图抓取（真机渲染，不依赖离屏字体）。

用法：
    env -u PYTHONHOME -u PYTHONPATH /c/Users/ROG/anaconda3/python.exe manual/tools/capture_shots.py

说明：脚本用**假数据**（张三/CS2301）驱动真实界面，不含任何真实学生信息。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.chdir(REPO)
os.environ["QT_QPA_PLATFORM"] = "windows"

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

OUT = REPO / "manual" / "images"
OUT.mkdir(parents=True, exist_ok=True)
DATA = REPO / ".tl_tmp" / "manual_root"
DATA.mkdir(parents=True, exist_ok=True)
CLASS = "CS2301"
USER = "张老师"

captured: list[tuple[str, str]] = []


def pump(app: QApplication, n: int = 12) -> None:
    for _ in range(n):
        app.processEvents()


def settle(app: QApplication, seconds: float = 0.5) -> None:
    """等切页动画（≤250ms）真的跑完 —— 只 processEvents 不等时钟会截到半截。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def save(widget: QWidget, code: str, app: QApplication, *, label: str = "") -> None:
    widget.show()
    widget.move(-6000, -6000)
    pump(app, 16)
    pix = widget.grab()
    pix.save(str(OUT / f"{code}.png"))
    captured.append((code, label))
    print(f"[ok] {code} {pix.width()}x{pix.height()} <- {code}", flush=True)
    widget.hide()


def crop(page: QWidget, target: QWidget, code: str, app: QApplication, *, pad: int = 12) -> None:
    pump(app, 6)
    pix = page.grab()
    top_left = target.mapTo(page, QPoint(0, 0))
    x = max(0, top_left.x() - pad)
    y = max(0, top_left.y() - pad)
    w = min(pix.width() - x, target.width() + 2 * pad)
    h = min(pix.height() - y, target.height() + 2 * pad)
    sub = pix.copy(x, y, w, h)
    sub.save(str(OUT / f"{code}.png"))
    captured.append((code, code))
    print(f"[ok] {code} {w}x{h} (crop)", flush=True)


def main() -> int:
    app = QApplication([])
    app.setStyleSheet((REPO / "ui" / "styles.qss").read_text(encoding="utf-8"))

    import ui.pages.analysis_page as ap
    from core.crypto_store import CryptoStore
    from ui.login_window import LoginWindow
    from ui.main_window import MainWindow
    from ui.recovery_code_dialog import RecoveryCodeDialog
    from ui.recover_dialog import RecoverDialog

    fixture = REPO / "tests" / "fixtures" / "fake_grades.xlsx"

    class _StubFileDialog:
        @staticmethod
        def getOpenFileName(*_a, **_k):
            return (str(fixture), "")

    class _StubInputDialog:
        @staticmethod
        def getItem(*_a, **_k):
            return ("大一", True)

        @staticmethod
        def getText(*_a, **_k):
            return (CLASS, True)

    class _StubMessageBox:
        @staticmethod
        def critical(*_a, **_k):
            return 0

        @staticmethod
        def warning(*_a, **_k):
            return 0

        @staticmethod
        def information(*_a, **_k):
            return 0

    ap.QFileDialog = _StubFileDialog
    ap.QInputDialog = _StubInputDialog
    ap.QMessageBox = _StubMessageBox
    ap.confirm_courses = lambda *_a, **_k: True

    store = CryptoStore(str(DATA))
    if USER not in [u.username for u in store.load_users().users]:
        store.register(USER, "Demo12345")
    store.login(USER, "Demo12345")

    # ---------------- S01 / S02 ----------------
    login = LoginWindow(store=store, root=str(DATA))
    save(login, "S01", app)
    login._goto_page(1)
    pump(app, 10)
    save(login, "S02", app)
    login.close()

    # ---------------- S03 / S04 ----------------
    code_dlg = RecoveryCodeDialog("K7QM-3XZP-9TRW-2HJD-8NVC-5BFL")
    save(code_dlg, "S03", app)
    code_dlg._confirmed = True
    code_dlg.deleteLater()

    recover = RecoverDialog(store, root=str(DATA))
    save(recover, "S04", app)
    recover.deleteLater()

    # ---------------- 主窗 ----------------
    win = MainWindow(store=store, root=str(DATA))
    win.set_user(USER)
    win.setWindowOpacity(1.0)
    win.show()
    win.move(-6000, -6000)
    pump(app, 20)

    analysis = win.analysis_page
    analysis.set_class_name(CLASS)
    win.goto(0)
    settle(app)
    save(win, "S05", app)
    crop(analysis, analysis, "S06", app, pad=0)

    # ---------------- S07 课程确认 ----------------
    from core.excel_io import read_frame
    from core.grade_import import parse_grade_frame
    from core.validator import describe_alerts
    from ui.review_dialog import ReviewDialog

    raw = read_frame(str(fixture))
    parsed, report = parse_grade_frame(raw)
    review = ReviewDialog(
        parsed.courses,
        extra_alerts=describe_alerts(report),
        score_alerts=ap.out_of_range_scores(parsed),
        warnings=parsed.warnings,
    )
    save(review, "S07", app)
    review.deleteLater()

    # ---------------- S08 导入后 ----------------
    analysis._do_import()
    pump(app, 12)
    win.goto(0)
    settle(app)
    save(analysis, "S08", app)
    win.goto(0)
    settle(app)
    save(win, "S09", app)  # 主界面有数据（原 S09=冲突弹窗，暂以此图占位）

    # ---------------- S10 / S11 成长档案 ----------------
    try:
        win.archive_page.reload()
        pump(app, 8)
        win.goto(1)
        settle(app)
        save(win, "S10", app)
        page = win.archive_page
        keys = list(page._key_by_row.values())
        if keys and page._archive is not None:
            student = page._archive.get(keys[0])
            if student is not None:
                page.show_student(student)
                settle(app, 0.3)
                save(win, "S11", app)
                try:
                    page.open_semester_profile("大一上")
                    settle(app, 0.4)
                    target = page._semester_page or win
                    save(target, "S12", app)
                except Exception as exc:  # noqa: BLE001
                    print("[skip] S12", repr(exc), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("[skip] S10/S11", repr(exc), flush=True)

    # ---------------- S13–S15 奖学金 ----------------
    try:
        sch = win.scholarship_page
        sch.set_user(USER)
        sch.reload_classes(select=CLASS)
        pump(app, 10)
        win.goto(2)
        settle(app)
        save(sch, "S13", app)
        crop(sch, sch._moral_table, "S14", app, pad=40)
        for key, value in (("first", 1), ("second", 2), ("third", 3)):
            sch._quota_spins[key].setValue(value)
        sch._run_btn.click()
        pump(app, 12)
        quota_card = sch._quota_spins["first"].parentWidget()
        top = quota_card.mapTo(sch, QPoint(0, 0))
        table = sch._result_table
        bottom = table.mapTo(sch, QPoint(0, table.height()))
        pix = sch.grab()
        y = max(0, top.y() - 16)
        h = min(pix.height() - y, bottom.y() - top.y() + 32)
        sub = pix.copy(0, y, pix.width(), h)
        sub.save(str(OUT / "S15.png"))
        captured.append(("S15", "S15"))
        print(f"[ok] S15 {sub.width()}x{sub.height()} (band)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("[skip] S13-S15", repr(exc), flush=True)

    # ---------------- S16–S19 设置 ----------------
    try:
        st = win.settings_page
        win.goto(4)
        settle(app)
        inner = st._scroll.widget()
        save(inner, "S16", app)  # 设置页整页（可滚动内容）
        from ui.pages.settings_page import GpaSchemeDialog

        dlg = GpaSchemeDialog(None, win)
        save(dlg, "S17", app)
        dlg.deleteLater()
    except Exception as exc:  # noqa: BLE001
        print("[skip] S16-S19", repr(exc), flush=True)

    # ---------------- S18 模型中心 ----------------
    try:
        win.goto(3)
        settle(app, 0.8)
        save(win, "S18", app)
    except Exception as exc:  # noqa: BLE001
        print("[skip] S18", repr(exc), flush=True)

    win.close()
    print("captured:", len(captured), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
