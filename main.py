"""transcript-lens 图形界面入口 —— 同时是 PyInstaller 的打包入口。

跑法::

    python main.py                    # 开发时直接跑
    dist\\transcript-lens.exe          # 打包后

    # 打包自检：不开窗口，把"假数据 → 解析 → 三口径算分"跑一遍
    # 结果写到 %TEMP%\\transcript-lens-selfcheck.txt，退出码 0=全过 / 1=有失败
    dist\\transcript-lens.exe --selfcheck

启动顺序（就这两条路）::

    QApplication（先设字体，再上 QSS）
      → SplashScreen 淡入 → 停留 800ms → 淡出
      → 检查 AppData\\Local\\SGMS\\session.enc
          ├─ 有效（7 天内）→ 直接 MainWindow（不弹登录）
          └─ 没有 / 过期 / 损坏 → LoginWindow → 登录成功再起 MainWindow

★ 为什么入口要单独放仓库根，而不是拿 ``ui/xxx.py`` 去打包：

PyInstaller 以入口脚本所在目录作为模块搜索起点。``ui/`` 里的绝对导入
（``from core.parser import ...``）需要**仓库根**在搜索路径上，而 ``ui/`` 不是。

★ ``--selfcheck`` 是给打包用的，不是产品功能：

打包最容易坏的从来不是界面代码，而是 pandas / openpyxl / PySide6 这类带
**数据文件**或**延迟导入**的库 —— 窗口能开、一点按钮就 ModuleNotFoundError，
或者 QSS / 图标素材没进包导致界面全白。这个自检在不进主循环、不弹任何窗口的
前提下把整条链路走一遍，几秒内就能定位。
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

#: 中文界面字体。**必须在建任何控件之前设**，否则 Qt 会退回宋体，字会糊。
UI_FONT_FAMILY = "Microsoft YaHei UI"
UI_FONT_SIZE = 10


def resource_root() -> Path:
    """只读资源（假数据 xlsx、QSS 等）所在目录。

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

#: 全局样式表（Spec 一：写在 ui/styles.qss 里加载）
QSS_RELATIVE = Path("ui") / "styles.qss"

#: 自检要确认能 import 的模块（界面 + 核心 + 本地模板）
CHECK_MODULES: tuple[str, ...] = (
    "ui.anim",
    "ui.splash",
    "ui.frameless",
    "ui.welcome",
    "ui.intentions",
    "ui.workers",
    "ui.flags",
    "ui.review_dialog",
    "ui.recover_dialog",
    "ui.login_window",
    "ui.main_window",
    "ui.pages",
    "ui.pages.analysis_page",
    "ui.pages.profile_page",
    "ui.pages.archive_page",
    "ui.pages.scholarship_page",
    "ui.pages.settings_page",
    "ui.pages.account_page",
    "core.parser",
    "core.calculator",
    "core.validator",
    "core.exporter",
    "core.archival",
    "core.activation",
    "core.crypto_store",
    "core.email_recovery",
    "core.recovery",
    "core.library",
    "core.session_token",
    "ai.local_template",
)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


def _selfcheck_report() -> tuple[bool, str]:
    """跑一遍数据链路，返回 ``(是否全过, 报告文本)``。不创建任何窗口。"""
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
    # cryptography 是 C 扩展，打包后最容易出现"界面能开、一存盘就崩"
    step("import cryptography", lambda: __import__("cryptography").__version__)
    step("import PySide6", lambda: __import__("PySide6").__version__)
    step(
        "from PySide6.QtWidgets import QApplication",
        lambda: "ok" if __import__("PySide6.QtWidgets", fromlist=["QApplication"]).QApplication else "ok",
    )
    step(
        "PySide6 平台插件 (qwindows)",
        lambda: _qt_platform_plugin_hint(),
    )
    # ★ 铁律：只用 PySide6，不许混进 PyQt
    try:
        __import__("PyQt6")
    except ImportError:
        lines.append(f"ok    {'未安装 PyQt6':<32} 只用 PySide6，符合铁律")
    else:
        failures.append("环境里有 PyQt6")
        lines.append(f"FAIL  {'环境里有 PyQt6':<32} 与「只用 PySide6」冲突")

    # ★ QSS 是**数据文件**，不是 .py。漏了它窗口能开、界面全白。
    qss_path = PROJECT_ROOT / QSS_RELATIVE
    check("全局样式表 ui/styles.qss", qss_path.exists(), str(qss_path))
    if qss_path.exists():
        try:
            text = qss_path.read_text(encoding="utf-8")
            check("样式表非空且含主色", "#3B82F6" in text and len(text) > 800,
                  f"{len(text)} 字符")
        except OSError as exc:
            failures.append("QSS 读不出来")
            lines.append(f"FAIL  QSS 读不出来 {exc}")

    lines.append(f"root        {PROJECT_ROOT}   (frozen={IS_FROZEN})")
    check("假数据文件存在", FAKE_FIXTURE.exists(), str(FAKE_FIXTURE))
    check("锚点假数据存在", FAKE_ANCHOR_FIXTURE.exists(), str(FAKE_ANCHOR_FIXTURE))

    # --- ★ 邮件授权码绝不能进仓库 -----------------------------------------
    for line in _credential_leak_report():
        lines.append(line)
        if line.startswith("FAIL"):
            failures.append("邮件凭据泄漏检查")

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

    # --- 侧写辅导页依赖的两个 core 能力（趋势 / 分区）-----------------------
    try:
        from core.archival import build_archive, partition, trend

        archive = build_archive(parsed, "未标注", source="selfcheck")
        student = next(
            (s for s in archive.students.values() if s.student == "张三"), None
        )
        if student is None:
            failures.append("归档里找不到张三")
            lines.append("FAIL  归档里找不到张三")
        else:
            info = trend(student)
            part = partition(student)
            check("archival.trend 可用", "delta" in info and "series" in info,
                  f"学期 {info.get('term_count')} 个，Δ={info.get('delta')}")
            check("archival.partition 可用",
                  "locked" in part and "remaining_terms" in part,
                  f"定型 {len(part.get('locked', []))} 门 / "
                  f"缺考 {len(part.get('absent', []))} 门")
    except Exception as exc:  # noqa: BLE001
        failures.append("侧写页依赖的 archival")
        lines.append(f"FAIL  侧写页依赖的 archival\n          {exc.__class__.__name__}: {exc}")

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


def _credential_leak_report() -> list[str]:
    """确认邮件授权码**没有**被抄进仓库。

    ★ 做法很关键：授权码本身**绝不能写进本文件**（写进来就正好犯了要防的错）。
      所以反过来做 —— 从 AppData 的真实配置里读出密钥，再拿它去扫仓库源码。
      密钥为空就跳过（没配的机器上这条无从查起）。

    返回 ``["ok ..."]`` 或 ``["FAIL ..."]``。
    """
    from core.email_recovery import config_path, load_config

    lines: list[str] = []

    # 1. 仓库里不能有真实的 email.json（只允许 .example.json）
    in_repo = _HERE / "config" / "email.json"
    if in_repo.exists():
        lines.append(f"FAIL  仓库里不该有 config/email.json：{in_repo}")
    else:
        lines.append(f"ok    {'仓库里没有 config/email.json':<32} 只有 .example.json 模板")

    # 2. 配置文件必须在仓库外
    outside = config_path()
    if "transcript-lens" in str(outside).lower():
        lines.append(f"FAIL  邮件配置落在仓库里了：{outside}")
    else:
        lines.append(f"ok    {'邮件配置在仓库外':<32} {outside.parent}")

    # 3. 拿真密钥反扫源码
    try:
        secret = load_config().api_key
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL  读邮件配置出错：{type(exc).__name__}: {exc}")
        return lines

    if not secret:
        lines.append(f"ok    {'未配置邮件密钥':<32} 本机还没填，跳过反扫")
        return lines

    hits: list[str] = []
    for suffix in ("*.py", "*.md", "*.json", "*.ps1", "*.cmd", "*.txt", "*.qss"):
        for path in _HERE.rglob(suffix):
            parts = set(path.parts)
            if parts & {"dist", "build", "__pycache__", ".git", "logs"}:
                continue
            try:
                if secret in path.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(path.relative_to(_HERE)))
            except OSError:
                continue
    if hits:
        lines.append(f"FAIL  仓库源码里出现了邮件授权码：{hits}")
    else:
        lines.append(f"ok    {'授权码没有进仓库':<32} 反扫 {len(secret)} 字符密钥，0 命中")
    return lines


def run_mailcheck(send_test: bool = False) -> int:
    """连一次邮件服务器确认配置可用（``--mailcheck``）。

    ``--mailcheck-send`` 会真的往配置的发件人地址寄一封测试信。

    这是给**打包后的 exe** 用的：源码能发信不代表 exe 能 —— exe 里可能缺
    ``ssl`` 的根证书、可能读不到 AppData 的配置、可能被 --exclude-module 误伤。
    ★ 输出里**永远不含授权码**，只报服务器、账号和结果。

    ★ 每一步都**立刻写出去**（不攒到最后）：这个诊断一旦卡住，
      攒着写的话用户看到的就是"什么都没输出"，等于没诊断。
    """
    import tempfile
    import time

    target = Path(tempfile.gettempdir()) / "transcript-lens-mailcheck.txt"
    stream = getattr(sys, "stdout", None)

    def emit(line: str, *, fresh: bool = False) -> None:
        if fresh:
            try:
                target.write_text(line + "\n", encoding="utf-8")
            except OSError:
                pass
        else:
            try:
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
        if stream is not None:
            try:
                stream.write(line + "\n")
                stream.flush()
            except OSError:
                pass

    def timed(label: str, fn):
        started = time.perf_counter()
        emit(f"  … {label} …")
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - 诊断就是要抓住一切
            emit(f"FAIL  {label} —— {type(exc).__name__}: {exc}")
            raise
        emit(f"ok    {label}（{time.perf_counter() - started:.1f}s）{value}")
        return value

    ok = True
    emit(f"邮件连通性自检   frozen={getattr(sys, 'frozen', False)}", fresh=True)
    emit("-" * 76)
    try:
        from core.email_recovery import (
            SmtpSender,
            config_path,
            get_sender,
            load_config,
            send_verification_mail,
        )

        config = load_config()
        emit(f"配置文件   {config_path()}")
        emit(f"  provider = {config.provider!r}")
        emit(f"  endpoint = {config.endpoint!r}")
        emit(f"  sender   = {config.sender!r}")
        emit(f"  api_key  = {'已设置' if config.api_key else '（空）'}")

        sender = get_sender()
        emit(f"  sender 实现 = {type(sender).__name__}")
        if not isinstance(sender, SmtpSender):
            ok = False
            emit("FAIL  配置没生效，拿到的不是 SmtpSender")
        else:
            timed("连接 + 登录", sender.verify)
            if send_test:
                timed(
                    "寄测试信",
                    lambda: send_verification_mail(
                        config.sender, "000000", purpose="打包自检"
                    )
                    or f"已寄到 {config.sender}",
                )
    except Exception as exc:  # noqa: BLE001
        ok = False
        emit(f"FAIL  {type(exc).__name__}: {exc}")

    emit("-" * 76)
    emit("MAILCHECK: ALL OK" if ok else "MAILCHECK: FAILED")
    return 0 if ok else 1


def _qt_platform_plugin_hint() -> str:
    """PySide6 的平台插件目录在不在。缺了它 exe 会报"could not find qwindows"."""
    from PySide6.QtCore import QLibraryInfo

    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    target = plugins / "platforms" / "qwindows.dll"
    if not target.exists():
        raise FileNotFoundError(f"缺少平台插件：{target}")
    return str(target)


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


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def load_stylesheet() -> str:
    """读全局 QSS。读不到就返回空串 —— 界面会变朴素，但不该开不起来。"""
    path = PROJECT_ROOT / QSS_RELATIVE
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def make_app(argv: list[str]):
    """建 QApplication 并**先设字体、再上 QSS**。

    ★ ``setFont`` 必须是第一件事：晚于控件创建的话，已经建好的那些控件
      还是旧字体，屏幕上会出现两种字体混排。
    """
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    app = QApplication(argv)
    app.setApplicationName("transcript-lens")
    app.setApplicationDisplayName("成绩单透镜")
    app.setFont(QFont(UI_FONT_FAMILY, UI_FONT_SIZE))
    style = load_stylesheet()
    if style:
        app.setStyleSheet(style)
    return app


class AppFlow:
    """把"splash → 登录 / 主界面 → 切换账号"这条流程收在一个对象里。

    做成类而不是一串闭包，是因为**窗口必须留引用** —— 局部变量一出作用域
    就被 GC，窗口会"自己消失"。这里所有窗口都挂在 ``self`` 上。

    ``root`` 会一路传给 LoginWindow / MainWindow / 令牌读写。
    默认 ``None`` = ``AppData\\Local\\SGMS``；自检和测试传临时目录，
    免得往真实用户目录里写东西（也免得**误删**真实的自动登录令牌）。
    """

    def __init__(self, app, root: str | None = None) -> None:
        self.app = app
        self.root = root
        self.splash = None
        self.login = None
        self.welcome = None
        self.main = None
        #: 正在淡出的开屏。**必须留着引用**（见 _after_splash 的注释）
        self._retiring_splash = None

    # ---------------------------------------------------------------- 入口
    def start(self) -> None:
        from ui.splash import SplashScreen

        self.splash = SplashScreen()
        self.splash.finished.connect(self._after_splash)
        self.splash.run()

    def _after_splash(self) -> None:
        """开屏结束：有有效令牌就直接进主界面，否则弹登录。

        ★ 这一段是**在开屏还盖着屏幕的时候**跑的。

        ``SplashScreen._start_fade_out`` 的次序是"先发 ``finished``、
          再淡出自己"，所以这个槽返回之前，用户看到的仍然是开屏。
          下面这一整段 —— ``load_token`` + 建主窗 + 建欢迎页 ——
          实测 **765ms**，全都被开屏遮住。改这个次序之前先读
          ``ui/splash.py`` 的 ``HOLD_MS`` 注释。

        ★ 开屏的 Python 引用**不能在这里就丢**（踩过 0xC0000409）。

        顶层窗口没有 parent：引用一没，它的 C++ 对象就被析构，
        而它的淡出动画 parent 就在它身上 —— 动画跑到一半源没了，
        整个进程直接消失、一个字都不打。所以交给 ``_retiring_splash``
        挂住，等它自己 ``dismissed`` 再放手。
        """
        from core.session_token import load_token

        splash = self.splash
        self.splash = None
        if splash is not None:
            self._retiring_splash = splash
            splash.dismissed.connect(self._splash_gone)

        user = load_token(self.root)
        if user:
            # 自动登录也算"登录成功"，同样先打一句招呼
            self.greet_then_main(user)
        else:
            self.show_login()

    def _splash_gone(self) -> None:
        """开屏已经淡出并关闭 —— 现在才可以放开它。"""
        self._retiring_splash = None

    # ---------------------------------------------------------------- 欢迎页
    def greet_then_main(self, username: str) -> None:
        """登录成功之后：先欢迎页（停 0.9s），再淡入主界面。

        ★ 这一页不是装饰。登录窗淡出后直接跳出主界面，人会懵一下；
          中间垫一句带时间的问候，也是确认"进来的是我，账号没选错"。

        ★ 主窗在**这一页的停留里**建好（``on_shown`` → :meth:`_prewarm_main`）。
          实测 ``MainWindow()`` 构造 + ``set_user`` 要 **600ms**（6 个页面
          全量 QSS polish + 解密这个人的意愿）。这 600ms 原来花在
          "欢迎页还没出现、屏幕上什么都没有"的那一刻，现在落在
          用户正看着问候语的时候。

        ★ 两条路径都被盖住了：
          * 自动登录 → 这一段跑在开屏背后（开屏还没淡出）；
          * 手动登录 → 登录窗淡出后欢迎页立刻 show 出来，再预热。
        """
        from ui.welcome import WelcomeWindow

        self.welcome = WelcomeWindow(
            username,
            on_shown=lambda: self._prewarm_main(username),
        )
        self.welcome.finished.connect(
            lambda name=username: self._after_welcome(name)
        )
        self.welcome.run()

    def _prewarm_main(self, username: str) -> None:
        """提前把主窗建出来并映射（**透明度 0，看不见**）。

        为什么敢 show()：``MainWindow.__init__`` 里已经 ``setWindowOpacity(0.0)``，
        所以映射出来的是一扇全透明窗口 —— Windows 照样为它建好原生句柄、
        跑完布局、加载好 QSS，但这些开销全落在用户看不见的时候。
        等欢迎页淡出，主窗只剩一次 opacity 0→1。

        失败**不抛**：预热是优化，不是功能。真出问题就退回
        :meth:`show_main` 里现建现显的老路，启动不能因为优化而挂掉。
        """
        if self.main is not None:
            return
        from ui.main_window import MainWindow

        try:
            window = MainWindow(root=self.root)
            window.switch_account.connect(self._on_switch_account)
            window.set_user(username)
            window.show()
        except Exception:  # noqa: BLE001 - 优化失败不能拦住启动
            import traceback as _tb

            _tb.print_exc()
            self.main = None
            return
        self.main = window

    def _after_welcome(self, username: str) -> None:
        self.welcome = None
        self.show_main(username)

    # ---------------------------------------------------------------- 主界面
    def show_main(self, username: str, *, fade: bool = True) -> None:
        """起主界面。已经被 :meth:`_prewarm_main` 建好时直接复用那一扇。"""
        if self.main is None:
            from ui.main_window import MainWindow

            self.main = MainWindow(root=self.root)
            self.main.switch_account.connect(self._on_switch_account)
        if fade:
            self.main.show_and_fade_in(username)
        else:
            self.main.set_user(username)
            self.main.show()

    def _on_switch_account(self) -> None:
        """个人中心 →「切换账号」：清掉自动登录令牌再回登录窗。

        ★ 必须清令牌：否则用户"切换"完，下次启动又被那个旧令牌直接送回
          上一个账号 —— 他会以为切换没生效。
        ★ 必须带 ``self.root``：不带的话清的是默认目录的令牌，
          测试时是在动真实用户的文件。
        """
        from core.session_token import clear_token

        try:
            clear_token(self.root)
        except Exception:  # noqa: BLE001 - 清不掉也要能继续切
            pass
        if self.main is not None:
            self.main.close()
            self.main = None
        self.show_login()

    # ---------------------------------------------------------------- 登录
    def show_login(self) -> None:
        from ui.login_window import LoginWindow

        self.login = LoginWindow(root=self.root)
        self.login.logged_in.connect(self._on_logged_in)
        self.login.run()

    def _on_logged_in(self, username: str) -> None:
        """登录窗已经自己淡出并 hide 了，这里接欢迎页，再让主窗口淡入。"""
        self.login = None
        self.greet_then_main(username)


def _any_visible_splash(app) -> bool:
    """还有没有一块开屏挂在屏幕上（截图/启动检查用）。"""
    from ui.splash import SplashScreen

    for widget in app.topLevelWidgets():
        if isinstance(widget, SplashScreen) and widget.isVisible():
            return True
    return False


def run_bootcheck() -> int:
    """跑一遍**真实启动流程**（开屏 → 欢迎页 → 主界面），把结果写进报告文件。

    ★ 为什么需要它：``--selfcheck`` 是**无窗口**的，它把数据链路跑通了，
      但一行 GUI 代码都没碰。而打包成 ``--windowed`` 的 exe **没有 stdout/
      stderr** —— 界面代码一旦在启动路径上崩掉（不是 Python 异常，是
      ``0xC0000409`` 那种直接把进程抹掉），用户看到的就是"没反应"，
      开发者这边一个字都拿不到。

      踩过一次：欢迎页淡出时销毁窗口 → 动画被一起销毁 → Qt 发完 finished
      还在用那个动画对象 → 进程消失。**400 多项测试全绿、自检全绿、
      两个 exe 都"自检通过"**，因为它栽在一条从来没被跑过的路径上。

      所以这个开关跑的就是那条路：真动画、真计时、一步不跳，
      最后把"主窗口到底可不可见"写进报告文件。见 ``build_exe.ps1``。

    用临时数据目录，**不碰真实用户数据**。窗口会一闪而过（这是真的 GUI）。
    """
    import tempfile
    import time
    from pathlib import Path as _Path

    #: 性能计时的起点。
    #:
    #: ⚠ 它**不含** PyInstaller onefile 的解包时间（那一段在 Python 起来
    #:   之前），也不含本函数下面准备临时账户的 PBKDF2 —— 只覆盖
    #:   "建 QApplication → 开屏 → 欢迎页 → 主窗可见"这条真实界面路径。
    started_at = time.perf_counter()

    report_path = _Path(tempfile.gettempdir()) / "transcript-lens-bootcheck.txt"
    lines: list[str] = []
    failed = False

    def say(text: str = "") -> None:
        lines.append(text)
        # 每步立刻落盘 + flush：卡住时也要能看出停在哪一步（见 AGENTS.md §7）
        try:
            report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        if not ok:
            failed = True
        say(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f"    {detail}" if detail else ""))

    say(f"python      {sys.version.split()[0]}")
    say(f"frozen      {getattr(sys, 'frozen', False)}")
    say(f"executable  {sys.executable}")
    say("-" * 74)

    work = _Path(tempfile.mkdtemp(prefix="tl_bootcheck_"))
    data_root = work / "SGMS"
    app = None
    try:
        from core.crypto_store import CryptoStore
        from core.session_token import save_token

        store = CryptoStore(root=data_root)
        store.register("自检用户", "bootcheck123")
        save_token("自检用户", root=str(data_root))
        check("准备了临时账户 + 自动登录令牌", True, str(data_root))

        app = make_app(["transcript-lens-bootcheck"])
        flow = AppFlow(app, root=str(data_root))

        from PySide6.QtCore import QEventLoop, QTimer

        def pump(ms: int) -> None:
            """推事件循环 ms 毫秒（QTest 不在打包产物里，所以手搓一个）。"""
            loop = QEventLoop()
            QTimer.singleShot(ms, loop.quit)
            loop.exec()

        def wait_until(pred, timeout_ms: int, label: str) -> bool:
            """轮询等条件成立。

            ★ 别用"睡够 X 毫秒应该就好了"：这条链上有 4 段动画 +
              一个 1.5 秒停留，机器一忙就错开。第一次写的时候就
              因为少等了一次 260ms 的淡入，把 opacity=0.000 判成了 FAIL。
            """
            waited = 0
            while waited < timeout_ms:
                if pred():
                    return True
                pump(50)
                waited += 50
            return bool(pred())

        flow.start()

        check(
            "开屏让位给欢迎页",
            wait_until(lambda: flow.splash is None and flow.welcome is not None, 4000,
                       "welcome"),
        )
        check(
            "欢迎页让位、主窗建出来",
            wait_until(lambda: flow.welcome is None and flow.main is not None, 12000,
                       "main"),
        )
        # 主窗还有一次 200ms 的淡入要跑完才真正"看得见"
        pump(700)

        main = flow.main
        check("★ 主窗口建出来了", main is not None, type(main).__name__ if main else "None")
        if main is not None:
            check("★ 主窗口 isVisible()", main.isVisible())
            check("★ 主窗口不透明（淡入真的跑完了）",
                  main.windowOpacity() > 0.99, f"opacity={main.windowOpacity():.3f}")
            check("★ 主窗口没被最小化", not main.isMinimized())
            check("★ 原生窗口句柄可见",
                  main.windowHandle() is not None and main.windowHandle().isVisible())
            check("★ 主窗口没被藏起来", not main.isHidden())
            # ★ 开屏必须真的被关掉、并且引用被放开。
            #   开屏现在自己负责淡出收尾（_start_fade_out 先 finished 再淡出），
            #   如果 dismissed 没接上，AppFlow 会永远挂着它 —— 而且它是置顶窗口，
            #   会一直盖在主界面上（界面上看就是"主窗出来了但被什么挡住"）。
            pump(500)
            check("★ 开屏已经关掉", not _any_visible_splash(app),
                  f"retiring={flow._retiring_splash is not None}")
            check("★ 开屏的引用已经放开", flow._retiring_splash is None)
            say(f"     （建 app → 主窗可见：{time.perf_counter() - started_at:.2f}s，"
                f"不含 PyInstaller 解包）")
            pump(300)
            shot = _Path(tempfile.gettempdir()) / "transcript-lens-bootcheck.png"
            try:
                main.grab().save(str(shot))
                check("截了一张主窗口的图", True, str(shot))
            except Exception as exc:  # noqa: BLE001
                check("截图", False, str(exc))
    except BaseException:  # noqa: BLE001 - 报告里要留下完整 traceback
        import traceback

        say("")
        say("!!!! bootcheck 抛异常 !!!!")
        say(traceback.format_exc())
        failed = True
    finally:
        if app is not None:
            for widget in app.topLevelWidgets():
                try:
                    widget.close()
                except Exception:  # noqa: BLE001
                    pass
        import shutil

        shutil.rmtree(work, ignore_errors=True)

    say("-" * 74)
    say("BOOTCHECK: " + ("FAIL" if failed else "ALL OK"))
    say(f"报告：{report_path}")
    # --windowed 没有 stdout，所以报告**只**写文件；能打印就顺便打一份
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            stream.write("\n".join(lines) + "\n")
        except Exception:  # noqa: BLE001
            pass
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selfcheck" in args:
        return run_selfcheck()

    # ★ 顺序有讲究：`"--mailcheck-send"` 和 `"--mailcheck"` 是两个不同的字符串，
    #   `in` 对列表做的是**精确匹配**，不是前缀匹配。先判长的那一个。
    #   （踩过：只判 "--mailcheck" 时，"--mailcheck-send" 匹配不上，
    #     于是掉进 GUI 分支把窗口开起来 —— 命令行下看着就是"卡死且无输出"。）
    if "--mailcheck-send" in args:
        return run_mailcheck(send_test=True)
    if "--mailcheck" in args:
        return run_mailcheck(send_test=False)
    if "--bootcheck" in args:
        return run_bootcheck()

    if "--help" in args or "-h" in args:
        stream = getattr(sys, "stdout", None)
        if stream is not None:
            stream.write(
                "transcript-lens 图形界面\n"
                "用法：transcript-lens.exe [--selfcheck] [--mailcheck[-send]]\n"
                "                        [--bootcheck]\n"
                "  --selfcheck    无窗口跑一遍数据链路（解析 → 算分 → 锚点）\n"
                "  --mailcheck    检查邮件配置能不能连上（不寄信）\n"
                "  --mailcheck-send  连上并真的寄一封测试信\n"
                "  --bootcheck    跑一遍真实启动流程（开屏 → 欢迎页 → 主界面）\n"
            )
        return 0

    app = make_app([sys.argv[0], *args])
    flow = AppFlow(app)
    flow.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
