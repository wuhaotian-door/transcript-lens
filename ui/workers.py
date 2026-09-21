"""把会阻塞的事情挪到后台线程 —— 发信、连服务器这类。

为什么必须这么做
----------------

Qt 的界面**全部在主线程**。在按钮回调里直接 ``smtplib.sendmail()`` 的话：

* 顺利时卡 ~1 秒，窗口一动不动；
* 网络不通时要卡满 **15 秒超时**（``SMTP_TIMEOUT``），
  期间窗口不重绘、按钮没反应，用户会以为程序死了然后去点关闭；
* Windows 还可能给标题栏加上"无响应"，系统弹"结束进程"。

所以凡是碰网络、碰磁盘大文件的，一律走 :func:`run_async`。

用法::

    run_async(
        self,
        lambda: send_verification_mail(email, code, root=root),
        on_done=lambda _r: self._status("验证码已发出"),
        on_error=lambda exc: QMessageBox.critical(self, "发送失败", str(exc)),
    )

★ 两个必须守住的规矩：

1. **回调在主线程执行**（信号槽跨线程是队列连接），所以回调里可以安全地碰控件。
2. **必须留引用**：``QRunnable`` 交给 ``QThreadPool`` 之后，如果 Python 这边没人
   持有它，可能在跑完之前被 GC 掉。这里挂在 ``_LIVE`` 上，结束再摘。
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

__all__ = ["run_async", "run_sync"]


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = _Signals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # pragma: no cover - 在后台线程里跑
        try:
            result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - 后台异常必须传回主线程
            # 带上 traceback，否则只剩一句"发送失败"没法查
            exc._tl_traceback = traceback.format_exc()  # type: ignore[attr-defined]
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


#: 正在跑的任务，防止被 GC（见模块开头第 2 条）
_LIVE: set[_Task] = set()


def run_async(
    parent: QObject | None,
    fn: Callable[[], Any],
    *,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """在后台线程跑 ``fn``，结果通过回调送回**主线程**。"""
    task = _Task(fn)
    _LIVE.add(task)

    def _finish() -> None:
        _LIVE.discard(task)

    if on_done is not None:
        task.signals.done.connect(on_done)
    if on_error is not None:
        task.signals.failed.connect(on_error)
    task.signals.done.connect(lambda _r: _finish())
    task.signals.failed.connect(lambda _e: _finish())

    # parent 只用来表达"这个任务属于哪个窗口"，不参与所有权
    _ = parent
    QThreadPool.globalInstance().start(task)


def run_sync(fn: Callable[[], Any]) -> tuple[bool, Any]:
    """同步跑一次，返回 ``(成功?, 结果或异常)``。

    **只给自检和测试用** —— 产品路径一律走 :func:`run_async`，
    在界面线程里同步跑网络请求就是上面说的那个坑。
    """
    try:
        return True, fn()
    except BaseException as exc:  # noqa: BLE001
        return False, exc


def live_task_count() -> int:
    """给自检用：还有几个后台任务在跑。"""
    return len(_LIVE)
