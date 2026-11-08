"""v1.9.3 补充：错误信息一键复制（traceback + 日志路径）。

现场诉求（Owner 原话）：

> 在程序运行异常报错时提示框添加一个按钮叫「复制错误信息」，点击后把
> traceback 和日志路径复制到剪贴板，方便我直接粘贴反馈。

判据分三层：

1. **纯函数**：``build_error_report`` 真的把标题 / 正文 / traceback /
   日志路径 / 日志末尾 / 版本号拼进同一段文本，超长会截断；
2. **运行时**：Critical 图标的 ``QMessageBox`` 显示时真的多出一个
   「复制错误信息」按钮；点它会把完整报告交给剪贴板，**且不关闭对话框**；
3. **接线**：``main.py`` 真的安装了过滤器与未捕获异常钩子。

★ 全程离屏；**不点真模态框、不依赖真剪贴板**（``_copy_to_clipboard`` 被
换成记录器）。剪贴板链在某些机器上会卡死，测试不该被它拖住（见 AGENTS §18.3）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from ui import error_report as er  # noqa: E402


@pytest.fixture()
def qapp() -> Any:
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def clean_filter(qapp: Any) -> Any:
    """每个用例前后都把事件过滤器摘掉，免得上一个用例的 root 漏进来。"""

    def drop() -> None:
        flt = getattr(qapp, er._ATTR_FILTER, None)
        if flt is not None:
            qapp.removeEventFilter(flt)
            try:
                delattr(qapp, er._ATTR_FILTER)
            except AttributeError:
                pass

    drop()
    yield
    drop()


@pytest.fixture()
def fake_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(er.applog, "log_path", lambda root=None: "C:/fake/logs/app.log")
    monkeypatch.setattr(
        er.applog, "tail", lambda lines=40, root=None: "2026-01-01 [boot] 日志样例"
    )


# ---------------------------------------------------------------------------
# 1. 纯函数
# ---------------------------------------------------------------------------


def test_报告包含按钮要复制的所有要素() -> None:
    report = er.build_error_report(
        title="进不去主界面",
        message="密码是对的，但下一步出错",
        traceback_text="Traceback (most recent call last):\nRuntimeError: 样例",
        log_path=r"C:\Users\me\AppData\Local\SGMS\logs\app.log",
        log_tail="2026-01-01 00:00:00 [login] 进入主界面前出错",
        version="9.9.9",
        now="2026-01-01 00:00:00",
    )

    assert er.COPY_BUTTON_TEXT == "复制错误信息"
    assert "时间：2026-01-01 00:00:00" in report
    assert "版本：v9.9.9" in report
    assert "标题：进不去主界面" in report
    assert "RuntimeError: 样例" in report
    assert r"C:\Users\me\AppData\Local\SGMS\logs\app.log" in report
    assert "[login] 进入主界面前出错" in report
    assert report.endswith("\n")


def test_没有异常时如实写没有traceback() -> None:
    report = er.build_error_report(
        title="密码错误",
        message="密码不对",
        version="9.9.9",
        now="2026-01-01 00:00:00",
    )

    assert er.NO_TRACEBACK in report
    assert "没有 traceback" in report
    assert er.NO_LOG_TAIL in report


def test_detailedText和traceback相同时不重复() -> None:
    same = "Traceback (most recent call last):\nRuntimeError: 同一段"
    report = er.build_error_report(
        title="t",
        message="m",
        traceback_text=same,
        details=same,
        version="9.9.9",
        now="2026-01-01 00:00:00",
    )

    assert report.count("RuntimeError: 同一段") == 1
    assert "----- 详细信息 -----" not in report


def test_超长报告会截断并注明() -> None:
    report = er.build_error_report(
        title="t",
        message="x" * (er.MAX_REPORT_CHARS + 100),
        traceback_text="tb",
        version="9.9.9",
        now="2026-01-01 00:00:00",
    )

    assert len(report) <= er.MAX_REPORT_CHARS + 100
    assert "（错误信息过长，已截断）" in report


# ---------------------------------------------------------------------------
# 2. 运行时：真加按钮、真点一次
# ---------------------------------------------------------------------------


def _copy_button(box: QMessageBox) -> QPushButton | None:
    return box.findChild(QPushButton, "CopyErrorInfoButton")


def test_给Critical弹窗加复制按钮且点完不关窗(
    qapp: Any, clean_filter: Any, fake_log: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        er, "_copy_to_clipboard", lambda text: (captured.append(text), True)[1]
    )
    assert er.install_error_copy(qapp, root="C:/fake") is True

    box = QMessageBox(
        QMessageBox.Icon.Critical,
        "进不去主界面",
        "密码是对的，但下一步出错",
        QMessageBox.StandardButton.Ok,
    )
    try:
        try:
            raise RuntimeError("弹窗里的异常")
        except Exception:
            box.show()
            QTest.qWait(20)

        button = _copy_button(box)
        assert button is not None, "Critical 弹窗没有长出「复制错误信息」按钮"
        assert button.text() == er.COPY_BUTTON_TEXT
        assert button.isVisible(), "复制按钮被布局挤没了"
        assert button.width() > 0
        button.click()
        QTest.qWait(20)

        assert captured, "点了按钮却没有把报告交给剪贴板"
        report = captured[0]
        assert "RuntimeError: 弹窗里的异常" in report
        assert "C:/fake/logs/app.log" in report
        assert "日志样例" in report
        assert button.text() == er.COPY_DONE_TEXT
        assert box.isVisible(), "复制按钮不许把对话框关掉"
    finally:
        box.close()


def test_非错误弹窗不出现复制按钮(
    qapp: Any, clean_filter: Any, fake_log: None
) -> None:
    assert er.install_error_copy(qapp, root="C:/fake") is True
    box = QMessageBox(
        QMessageBox.Icon.Information,
        "提示",
        "这是一条普通提示",
        QMessageBox.StandardButton.Ok,
    )
    try:
        box.show()
        QTest.qWait(20)
        assert _copy_button(box) is None, "普通提示不该长出复制错误按钮"
    finally:
        box.close()


def test_重复安装过滤器是幂等的(
    qapp: Any, clean_filter: Any, fake_log: None
) -> None:
    assert er.install_error_copy(qapp, root="a") is True
    assert er.install_error_copy(qapp, root="b") is True
    flt = getattr(qapp, er._ATTR_FILTER, None)
    assert flt is not None
    # 只剩一个自己装的过滤器（Qt 自己的过滤器不算）
    assert flt.parent() is qapp


# ---------------------------------------------------------------------------
# 3. 未捕获异常钩子
# ---------------------------------------------------------------------------


def test_未捕获异常走弹框钩子并保留完整traceback(
    qapp: Any, clean_filter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, type, BaseException]] = []
    monkeypatch.setattr(
        er,
        "_show_crash_dialog",
        lambda text, exc_type, exc: captured.append((text, exc_type, exc)),
    )
    old_hook = sys.excepthook
    sys.excepthook = lambda *_args: None  # 不让上一个钩子把测试判成未处理异常
    old_state = dict(er._CRASH_STATE)
    try:
        assert er.install_crash_handler("C:/fake") is True
        try:
            raise ValueError("崩溃样例")
        except Exception:
            sys.excepthook(*sys.exc_info())
        QTest.qWait(30)

        assert captured, "未捕获异常没有排到弹框队列"
        text, exc_type, exc = captured[0]
        assert exc_type is ValueError
        assert "ValueError: 崩溃样例" in text
        assert "====" not in text  # 钩子传的是纯 traceback，报告由弹窗再拼
    finally:
        sys.excepthook = old_hook
        er._CRASH_STATE.clear()
        er._CRASH_STATE.update(old_state)


def test_main安装了过滤器与崩溃钩子() -> None:
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "install_error_copy(app, flow.root)" in source
    assert "install_crash_handler(flow.root)" in source
