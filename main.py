"""transcript-lens 图形界面入口 —— 同时是 PyInstaller 的打包入口。

跑法::

    python main.py                    # 开发时直接跑
    dist\\transcript-lens.exe          # 打包后

    # 打包自检：不开窗口，把"假数据 → 解析 → 三口径算分"跑一遍
    # 结果写到 %TEMP%\\transcript-lens-selfcheck.txt，退出码 0=全过 / 1=有失败
    dist\\transcript-lens.exe --selfcheck

启动顺序（就这两条路）::

    检查 AppData\\Local\\SGMS\\session.enc
      ├─ 有效（7 天内）→ 直接 ui.main_window.show_main(user)
      └─ 没有 / 过期 / 损坏 → ui.login_window.LoginWindow

★ 为什么入口要单独放仓库根，而不是拿 ``ui/xxx.py`` 去打包：

PyInstaller 以入口脚本所在目录作为模块搜索起点。``ui/`` 里的绝对导入
（``from core.parser import ...``）需要**仓库根**在搜索路径上，而 ``ui/`` 不是。

★ ``--selfcheck`` 是给打包用的，不是产品功能：

打包最容易坏的从来不是 tkinter，而是 pandas / openpyxl / ttkbootstrap 这类带
**数据文件**或**延迟导入**的库 —— 界面能开、一点按钮就 ModuleNotFoundError，
或者主题素材没进包导致控件全白。这个自检在不进主循环、不弹任何窗口的前提下
把整条链路走一遍，几秒内就能定位。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 仓库根 / PyInstaller 解包目录。必须在 import ui / core 之前进 sys.path，
#: 否则直接双击 exe 时找不到包。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

SELFCHECK_NAME = "transcript-lens-selfcheck.txt"


def resource_root() -> Path:
    """只读资源（假数据 xlsx 等）所在目录。

    * 源码运行：仓库根
    * PyInstaller 打包后：解包目录 ``sys._MEIPASS`` —— ``--add-data`` 进去的
      东西运行时都在那里

    ★ 踩过的坑：打包后 ``Path(__file__).parents[1]`` 指向一层临时解包目录，
    不再是仓库根，于是"用假数据试跑"会因为文件不存在而静默失效。
    """
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle) if bundle else _HERE


IS_FROZEN = bool(getattr(sys, "_MEIPASS", None))
PROJECT_ROOT = resource_root()
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
FAKE_FIXTURE = FIXTURES / "fake_grades.xlsx"
FAKE_ANCHOR_FIXTURE = FIXTURES / "fake_grades_with_anchor.xlsx"

#: 自检要确认能 import 的模块（界面 + 核心 + 本地模板）
CHECK_MODULES: tuple[str, ...] = (
    "ui.login_window",
    "ui.main_window",
    "ui.page_academic",
    "ui.page_scholarship",
    "ui.page_stubs",
    "ui.review_dialog",
    "ui.flags",
    "core.parser",
    "core.calculator",
    "core.validator",
    "core.exporter",
    "core.archival",
    "core.activation",
    "core.crypto_store",
    "core.email_recovery",
    "core.library",
    "core.session_token",
    "ai.local_template",
)


def _selfcheck_report() -> tuple[bool, str]:
    """跑一遍数据链路，返回 ``(是否全过, 报告文本)``。不碰 tkinter 主循环。"""
    import platform

    lines: list[str] = []
    failures: list[str] = []

    def step(label: str, fn, *, width: int = 32):
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - 自检就是要抓住一切异常
            failures.append(label)
            lines.append(f"FAIL  {label}\n          {exc.__class__.__name__}: {exc}")
            return None
        detail = str(value)
        if len(detail) > 110:
            detail = detail[:107] + "..."
        lines.append(f"ok    {label:<{width}} {detail}")
        return value

    def check(label: str, condition: bool, detail: str) -> None:
        if condition:
            lines.append(f"ok    {label:<32} {detail}")
        else:
            failures.append(label)
            lines.append(f"FAIL  {label:<32} {detail}")

    # --- 环境 ---------------------------------------------------------------
    lines.append(f"python      {sys.version.split()[0]}  ({platform.machine()})")
    lines.append(f"frozen      {getattr(sys, 'frozen', False)}")
    lines.append(f"meipass     {getattr(sys, '_MEIPASS', '(none)')}")
    lines.append(f"executable  {sys.executable}")
    lines.append(f"cwd         {Path.cwd()}")
    lines.append("-" * 76)

    # --- 依赖（打包最常坏的地方）-------------------------------------------
    step("import pandas", lambda: __import__("pandas").__version__)
    step("import openpyxl", lambda: __import__("openpyxl").__version__)
    step("import tkinter", lambda: f"Tk {__import__('tkinter').TkVersion}")
    # cryptography 是 C 扩展，打包后最容易出现"界面能开、一存盘就崩"
    step("import cryptography", lambda: __import__("cryptography").__version__)
    step("import ttkbootstrap", lambda: __import__("ttkbootstrap").__version__)
    step("import PIL", lambda: __import__("PIL").__version__)
    step("from ttkbootstrap import Window",
         lambda: "ok" if __import__("ttkbootstrap").Window else "ok")

    # ★ ttkbootstrap 的主题和图标是**数据文件**，不是 .py。
    #   只 collect-submodules 的话，exe 里窗口能开、主题素材却是缺的。
    #   这里逐个点名，缺一个就报红 —— 比等到用户看见白板强。
    try:
        import ttkbootstrap as _ttkb

        assets = Path(_ttkb.__file__).parent / "assets"
        for rel in (
            "icons/bootstrap.ttf",
            "icons/glyphmap.json",
            "elements/manifest.json",
            "app_icons/ttkbootstrap.png",
        ):
            target = assets / rel
            check(f"ttkbootstrap 素材 {rel}", target.exists(), str(target))
    except Exception as exc:  # noqa: BLE001
        failures.append("ttkbootstrap 素材")
        lines.append(f"FAIL  ttkbootstrap 素材\n          {exc.__class__.__name__}: {exc}")

    lines.append(f"root        {PROJECT_ROOT}   (frozen={IS_FROZEN})")
    check("假数据文件存在", FAKE_FIXTURE.exists(), str(FAKE_FIXTURE))
    check("锚点假数据存在", FAKE_ANCHOR_FIXTURE.exists(), str(FAKE_ANCHOR_FIXTURE))

    # --- 主链路：假数据 xlsx → parser → 三口径算分 ---------------------------
    from core.calculator import CalculationScope, calculate_many
    from core.parser import parse_excel
    from core.validator import check_credit_anchor

    parsed = None
    try:
        parsed = parse_excel(str(FAKE_FIXTURE))
        lines.append(
            f"ok    {'解析假数据':<32} {len(parsed.courses)} 门课 / "
            f"{parsed.total_credit} 学分 / {len(parsed.rows)} 个学生"
        )
    except Exception as exc:  # noqa: BLE001
        failures.append("解析假数据")
        lines.append(f"FAIL  解析假数据\n          {exc.__class__.__name__}: {exc}")

    if parsed is not None:
        check("课程数 8 / 总学分 23",
              len(parsed.courses) == 8 and parsed.total_credit == 23.0,
              f"{len(parsed.courses)} 门 / {parsed.total_credit} 学分")

        rows = None
        try:
            rows = calculate_many(parsed, [CalculationScope.REQUIRED_LIMITED])[
                CalculationScope.REQUIRED_LIMITED
            ]
        except Exception as exc:  # noqa: BLE001
            failures.append("calculate_many")
            lines.append(f"FAIL  calculate_many\n          {exc.__class__.__name__}: {exc}")

        if rows:
            check("学生数 6", len(rows) == 6, str(len(rows)))
            counted = [r for r in rows if r.average is not None]
            zhang = next((r for r in rows if r.student == "张三"), None)
            # 手算锚点：张三 必修+限选 = 1616/19
            check(
                "张三 必修+限选 加权均分",
                zhang is not None and zhang.average is not None
                and abs(zhang.average - 85.052632) < 1e-6,
                f"{zhang.average:.6f} (手算 1616/19 = 85.052632)"
                if zhang and zhang.average else "缺失",
            )
            # 红线 8：整班是总加权 6550/87，不是各人均分的平均（那是 76.308929）
            total_credit = sum(r.total_credit for r in counted)
            total_sum = sum(r.weighted_sum for r in counted)
            class_avg = total_sum / total_credit if total_credit else 0.0
            check(
                "整班 必修+限选（红线 8）",
                abs(class_avg - 75.287356) < 1e-6,
                f"{class_avg:.6f} (总加权 6550/87；均分之均会是 76.308929)",
            )

    # --- 锚点校验（Spec 第 6.5 节）-----------------------------------------
    try:
        import pandas as pd

        frame = pd.read_excel(FAKE_ANCHOR_FIXTURE, header=None, dtype=object)
        from core.parser import parse_dataframe

        direct = parse_dataframe(frame)
        report = check_credit_anchor(frame, direct)
        check("锚点列被识别", bool(report.anchors), f"{[a.label for a in report.anchors]}")
        check("锚点差额全部 <= 0.5", not report.exceeded,
              f"超阈值 {len(report.exceeded)} 人 / 共 {len(report.checks)} 人")
        zhang_anchor = next((c for c in report.checks if c.student == "张三"), None)
        check(
            "张三 锚点 22 == 算得 22",
            zhang_anchor is not None
            and zhang_anchor.anchor_value == 22.0
            and zhang_anchor.computed_credit == 22.0,
            f"锚点 {zhang_anchor.anchor_value} / 算得 {zhang_anchor.computed_credit}"
            if zhang_anchor else "缺少张三",
        )
    except Exception as exc:  # noqa: BLE001
        failures.append("锚点校验")
        lines.append(f"FAIL  锚点校验\n          {exc.__class__.__name__}: {exc}")

    # --- 红线自检：打包里不许有能发网络请求的东西 -------------------------
    if getattr(sys, "frozen", False):
        for banned in ("requests", "urllib3", "httpx", "aiohttp", "openai", "anthropic"):
            try:
                __import__(banned)
            except ImportError:
                lines.append(f"ok    {'未打包 ' + banned:<32} 物理上发不出请求")
            else:
                failures.append(f"打包里混进了 {banned}")
                lines.append(f"FAIL  打包里混进了 {banned} —— 与「不联网」铁律冲突")

    # --- 所有模块能否 import（不创建窗口）----------------------------------
    for name in CHECK_MODULES:
        step(f"import {name}", lambda n=name: "ok" if __import__(n) else "ok")

    lines.append("-" * 76)
    if failures:
        lines.append(f"RESULT: FAILED ({len(failures)} 项) -> {', '.join(failures)}")
    else:
        lines.append("RESULT: ALL OK")
    return not failures, "\n".join(lines)


def run_selfcheck() -> int:
    """跑自检并把报告写到临时目录（窗口版 exe 没有 stdout，只能落文件）。"""
    import tempfile

    try:
        ok, report = _selfcheck_report()
    except Exception as exc:  # noqa: BLE001 - 自检自身崩了也要留下痕迹
        ok, report = False, f"自检自身崩溃：{exc.__class__.__name__}: {exc}"

    target = Path(tempfile.gettempdir()) / SELFCHECK_NAME
    try:
        target.write_text(report, encoding="utf-8")
    except OSError:
        pass

    # 窗口版 exe 的 sys.stdout 是 None，直接 print 会 AttributeError
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            stream.write(report + "\n")
            stream.write(f"报告已写入：{target}\n")
        except OSError:
            pass
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selfcheck" in args:
        return run_selfcheck()

    if "--help" in args or "-h" in args:
        stream = getattr(sys, "stdout", None)
        if stream is not None:
            stream.write(
                "transcript-lens 图形界面\n"
                "用法：transcript-lens.exe [--selfcheck]\n"
            )
        return 0

    # --- 启动：先看自动登录令牌，再决定进主界面还是弹登录窗 -----------------
    from core.session_token import load_token

    user = load_token()
    if user:
        from ui.main_window import show_main

        show_main(user)
        return 0

    from ui.login_window import LoginWindow

    LoginWindow().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
