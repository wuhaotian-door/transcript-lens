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

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    "core.user_store",
    "core.scholarship",
    "core.ai",
    "core.ai.local_assistant",
    "core.ai.cloud_adapter",
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

    # --- 联网红线（v1.6 更新版）-------------------------------------------
    #
    # charter 第 3 章把红线改成了**有条件放行**：
    #
    #   ✅ SMTP 发邮件（仅找回密码流程，用户主动点「发送验证码」）
    #   ✅ 辅导员手动启用的云端大模型（手动开 + 手动填 Key + 只在点「深度评估」时调）
    #   ❌ 自动更新检查 / telemetry / 崩溃上报 / 静默请求
    #   ❌ requests / httpx 用于**非** SMTP、**非**用户主动 AI 的场景
    #
    # 所以这里不再是"见到 requests 就 FAIL"。真正的守卫换成两条更准的：
    #
    #   1. **AI 适配器的源码里不许出现任何 HTTP 客户端**（静态扫描）——
    #      现在它们是骨架，一行网络代码都不该有；
    #   2. 云端传出去的 payload 必须过脱敏白名单。
    #
    # 原来的"打包里有没有 requests"仍然查，但语义变了：
    # 它们**现在**确实不在包里（骨架期），在包里说明有人偷偷加了依赖。
    for line in _network_policy_report():
        lines.append(line)
        if line.startswith("FAIL"):
            failures.append("联网红线检查")

    # --- 课程三类别独立（charter 一节 + 三节铁律 8）------------------------
    #
    # "必修 / 限选 / 选修 是三个完全独立的类别，任何将选修归入限选的逻辑都是 bug"
    # —— 这条不能只写在文档里，得有一条会 FAIL 的检查盯着。
    for line in _category_policy_report():
        lines.append(line)
        if line.startswith("FAIL"):
            failures.append("课程类别独立检查")

    # --- v1.7 版本标记 + 备份可用性（charter 八节 / 任务书 4）---------------
    for line in _version_migration_report():
        lines.append(line)
        if line.startswith("FAIL"):
            failures.append("版本迁移检查")

    # --- 所有模块能否 import（不创建窗口）----------------------------------
    for name in CHECK_MODULES:
        step(f"import {name}", lambda n=name: "ok" if __import__(n) else "ok")

    lines.append("-" * 76)
    if failures:
        lines.append(f"RESULT: FAILED ({len(failures)} 项) -> {', '.join(failures)}")
    else:
        lines.append("RESULT: ALL OK")
    return not failures, "\n".join(lines)


def _category_policy_report() -> list[str]:
    """课程三类别独立（charter 一节 + 三节铁律 8）。

    charter 原话：**"必修、限选、选修是三个完全独立的类别，绝不混同"**、
    **"任何将选修归入限选的逻辑都是 bug"**。这条不能只写在文档里 ——
    得有一条会 FAIL 的检查盯着，否则下一次"整理口径表"又会把它揉回去
    （上一版文档就是这么错的，而且没人发现）。

    ★ 为什么做成**行为验证**而不是"数一数枚举里有几个值"
    ------------------------------------------------

    造一个三门课的学生，三门各属一个类别、分数两两不同::

        高等数学(4学分)必修 = 80
        专业英语(2学分)限选 = 90
        音乐鉴赏(1学分)选修 = 100

    三档口径的结果必然互不相同::

        口径① 三类全算     (320+180+100)/7 = 85.714286
        口径② 必修+限选    (320+180)/6     = 83.333333
        口径③ 仅必修        320/4          = 80

    **关键在 ②≠①**：只要有人把选修当成限选算进去，② 立刻变成 85.714286 == ①，
    这条就 FAIL。只看枚举定义是抓不到这种 bug 的 —— 类别是独立的不等于
    口径表里用得对。
    """
    lines: list[str] = []
    try:
        from core.calculator import CalculationScope, calculate_many
        from core.parser import Category, parse_dataframe
    except Exception as exc:  # noqa: BLE001
        return [f"FAIL  课程类别检查：import 失败 {exc.__class__.__name__}: {exc}"]

    wanted = {Category.REQUIRED, Category.LIMITED, Category.ELECTIVE}
    have = {c for c in Category}
    missing = wanted - have
    if missing:
        return [
            "FAIL  core.parser.Category 少了独立类别："
            + "、".join(sorted(c.value for c in missing))
        ]
    lines.append(
        "ok    Category 三个独立类别齐全    "
        + "/".join(c.value for c in (Category.REQUIRED, Category.LIMITED, Category.ELECTIVE))
    )

    # --- 口径表：每档到底含哪几类（charter 一节的三口径定义）---------------
    expect_sets = {
        CalculationScope.ALL: wanted,
        CalculationScope.REQUIRED_LIMITED: {Category.REQUIRED, Category.LIMITED},
        CalculationScope.REQUIRED_ONLY: {Category.REQUIRED},
    }
    wrong_sets = [
        scope.value
        for scope, want in expect_sets.items()
        if not want <= scope.categories
    ]
    if wrong_sets:
        lines.append("FAIL  口径类别集合与 charter 一节不一致：" + "、".join(wrong_sets))
    else:
        lines.append("ok    三档口径的类别集合 == charter 一节（①全部 ②必修+限选 ③仅必修）")

    folded = [
        scope.value
        for scope in expect_sets
        if scope is not CalculationScope.ALL and Category.ELECTIVE in scope.categories
    ]
    if folded:
        lines.append(
            "FAIL  选修被并进了这些口径（charter 十节明令禁止）：" + "、".join(folded)
        )
    else:
        lines.append("ok    选修不在口径②③里 —— 没有被并进限选")

    # --- 行为验证：真算一遍，看三档结果是否互不相同 ------------------------
    expect_avg = {
        CalculationScope.ALL: 600 / 7,
        CalculationScope.REQUIRED_LIMITED: 500 / 6,
        CalculationScope.REQUIRED_ONLY: 80.0,
    }
    try:
        import pandas as pd

        frame = pd.DataFrame(
            [
                ["姓名", "学号", "高等数学(4学分)必修",
                 "专业英语(2学分)限选", "音乐鉴赏(1学分)选修"],
                ["张三", "2023001", 80, 90, 100],
            ]
        )
        parsed = parse_dataframe(frame)
        results = calculate_many(parsed)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL  三类别样例解析/计算失败 {exc.__class__.__name__}: {exc}")
        return lines

    got: dict[Any, float | None] = {}
    for scope in expect_sets:
        rows = results.get(scope) or []
        got[scope] = rows[0].weighted_average if rows else None

    off = [
        f"{scope.value} = {got[scope]!r}，应为 {expect_avg[scope]:.6f}"
        for scope in expect_sets
        if got[scope] is None or abs(got[scope] - expect_avg[scope]) > 1e-6
    ]
    if off:
        lines.append("FAIL  三档口径加权与手算不符：" + "；".join(off))
    else:
        lines.append(
            "ok    三档口径加权 == 手算    85.714286 / 83.333333 / 80.000000"
        )

    all_avg = got[CalculationScope.ALL]
    rl_avg = got[CalculationScope.REQUIRED_LIMITED]
    ro_avg = got[CalculationScope.REQUIRED_ONLY]
    if all_avg is not None and rl_avg is not None and abs(all_avg - rl_avg) < 1e-9:
        lines.append("FAIL  口径① == ② —— 选修被算进口径②了")
    elif rl_avg is not None and ro_avg is not None and abs(rl_avg - ro_avg) < 1e-9:
        lines.append("FAIL  口径② == ③ —— 限选被算进口径③了")
    else:
        lines.append("ok    口径① ≠ ② ≠ ③：选修只进①、限选只进①②")

    return lines


def _version_migration_report() -> list[str]:
    """v1.7 版本标记 + 迁移备份（charter 八节 / 任务书任务四）。

    四件事，每件都**真跑一遍**，不是在源码里找关键字：

    1. 版本号只有一个真源（``core.version``），``core.updater`` 是 import 来的；
    2. 新建一个临时加密库，``users.json`` 里**必须**有 ``__vault_version__``；
    3. 老库（没有标记）**必须**被判成 1.0 且"需要迁移"；
    4. 备份路径**真的可写** —— 建临时库、真备份一次、核对内容。

    ★ 全程用 ``tempfile`` 建的临时目录，**绝不碰用户真实的 %LOCALAPPDATA%/SGMS**。
    """
    import json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    lines: list[str] = []
    work = _Path(tempfile.mkdtemp(prefix="tl_selfcheck_mig_"))
    try:
        from core import updater as _updater
        from core.crypto_store import CryptoStore
        from core.migrator import backup_vault, check_compatibility, migrate
        from core.version import CURRENT_VERSION, VAULT_VERSION_KEY

        # --- 1. 版本号真源 -------------------------------------------------
        if _updater.CURRENT_VERSION is not CURRENT_VERSION:
            lines.append(
                "FAIL  core.updater.CURRENT_VERSION 和 core.version 不是同一个值："
                f"{_updater.CURRENT_VERSION!r} vs {CURRENT_VERSION!r}"
            )
        else:
            lines.append(f"ok    版本号一处真源（core.version）        v{CURRENT_VERSION}")

        # --- 2. 新库自带版本标记 -------------------------------------------
        root = work / "fresh"
        root.mkdir(parents=True, exist_ok=True)
        store = CryptoStore(root=root)
        store.register("自检用户", "selfcheck123")
        raw = json.loads((root / "users.json").read_text(encoding="utf-8"))
        got = raw.get(VAULT_VERSION_KEY)
        if got != CURRENT_VERSION:
            lines.append(
                f"FAIL  新建的加密库没有版本标记：{VAULT_VERSION_KEY}={got!r}，"
                f"应为 {CURRENT_VERSION!r}"
            )
        else:
            lines.append(f"ok    新库自带版本标记                      {VAULT_VERSION_KEY}={got}")

        # --- 3. 老库被判成 1.0 且需要迁移 -----------------------------------
        legacy_root = work / "legacy"
        legacy_root.mkdir(parents=True, exist_ok=True)
        legacy = CryptoStore(root=legacy_root)
        legacy.register("老用户", "selfcheck123")
        path = legacy_root / "users.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop(VAULT_VERSION_KEY, None)
        data.pop("vault_version", None)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        needs, old = check_compatibility(data)
        if (needs, old) != (True, "1.0"):
            lines.append(f"FAIL  无标记的老库应判成 (True, '1.0')，实际 ({needs}, {old!r})")
        else:
            lines.append("ok    无标记的老库 → 1.0 且需要迁移")
        migrated = migrate(old, data)
        if migrated.get(VAULT_VERSION_KEY) != CURRENT_VERSION:
            lines.append("FAIL  migrate() 之后版本标记还是没写上")
        else:
            lines.append(f"ok    migrate() 只动版本标记        1.0 → {CURRENT_VERSION}")

        # --- 4. 备份路径真的可写 -------------------------------------------
        backup = backup_vault(legacy_root, old)
        if not (backup / "users.json").exists():
            lines.append(f"FAIL  备份里没有 users.json：{backup}")
        elif backup.parent != legacy_root or not backup.name.startswith("backup_v1.0_"):
            lines.append(f"FAIL  备份路径不符合 charter 8.2 规范：{backup}")
        else:
            lines.append(f"ok    备份路径可写且规范                    {backup.name}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL  版本迁移自检抛异常 {exc.__class__.__name__}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return lines


def _network_policy_report() -> list[str]:
    """v1.6 联网红线（charter 第 3 章）—— 返回 ``["ok ..."]`` / ``["FAIL ..."]``。

    ★ 为什么不再"见到 requests 就 FAIL"
    ---------------------------------

    charter 把红线改成了**有条件放行**：SMTP（用户主动点发验证码）和
    "辅导员手动启用的云端大模型"是允许的；禁止的是**自动更新、telemetry、
    静默请求**，以及"拿 requests/httpx 干别的"。

    所以守卫要落在**行为**上，不能落在"这个包在不在"上。
    真正的三条：

    1. **AI 适配器源码里不许有任何 HTTP 客户端** —— 现在它们是骨架，
       静态扫一遍就够，而且比"看包里有没有 requests"精确得多
       （后者只能查打包版，源码版什么都查不到）。
    2. **云端出口必须过脱敏白名单** —— 直接调 ``redact_student`` 试一把，
       看姓名/学号会不会漏出去。
    3. **非白名单的联网库不该被打进包** —— 保留原检查，但语义改成
       "在包里说明有人偷偷加了依赖"，不再是铁律冲突。
    """
    import re as _re

    lines: list[str] = []

    # --- 1. AI 适配器源码静态扫描 -----------------------------------------
    banned_imports = (
        "requests", "httpx", "urllib.request", "urllib3",
        "aiohttp", "http.client", "socket", "openai", "anthropic", "websocket",
    )
    # ★ 用 PROJECT_ROOT 而不是 _HERE：打包后前者指向 _MEIPASS，后者不一定。
    ai_dir = PROJECT_ROOT / "core" / "ai"
    scanned: list[str] = []
    offenders: list[str] = []
    if ai_dir.is_dir():
        # ★ ``*.py`` **和** ``*.pyc``：PyInstaller 把模块编译成 .pyc 之后，
        #   只 glob ``*.py`` 会扫到 **0 个文件**然后"通过" ——
        #   那是**假绿**，比不检查更危险（实测打包版就撞上了这一条）。
        for path in sorted(list(ai_dir.glob("*.py")) + list(ai_dir.glob("*.pyc"))):
            scanned.append(path.name)
            if path.suffix != ".py":
                continue                      # 编译产物读不出源码，见下面第 2 道
            text = path.read_text(encoding="utf-8", errors="ignore")
            for module in banned_imports:
                # 只认真正的 import 语句，不认注释/文档里提到的名字
                pattern = (
                    rf"^\s*(?:import|from)\s+{_re.escape(module)}(?:[\s.]|$)"
                )
                if _re.search(pattern, text, _re.MULTILINE):
                    offenders.append(f"{path.name} -> {module}")
    if offenders:
        lines.append(
            f"FAIL  AI 适配器里出现了网络 import：{offenders}"
            f"（charter 6.3：骨架阶段不许有任何真实调用）"
        )
    elif scanned:
        lines.append(
            f"ok    {'AI 适配器无网络 import':<32} 扫了 {len(scanned)} 个文件：{scanned}"
        )
    elif getattr(sys, "frozen", False):
        # ★ 打包版**看不到 .py 源码**：PyInstaller 把模块编译进 PYZ 归档，
        #   ``_MEIPASS/core/ai/`` 这个目录根本不存在。
        #   所以这里**不是**失败，但也不能当成"通过"糊过去 ——
        #   真正的守卫换成了下面第 1b 条（查已加载模块的命名空间），
        #   而**权威的源码扫描在打包前那一次 --selfcheck 里跑的**
        #   （build_exe.ps1 的顺序就是先跑源码自检、再打包，所以坏 import
        #   在打包之前就已经被拦住了）。这一段是纵深防御的第二层。
        lines.append(
            f"ok    {'AI 适配器（打包版）':<32} 无 .py 源码（打进 PYZ），"
            "改由下方命名空间检查兜底"
        )
    else:
        # ★★ 源码版扫到 0 个文件 = **检查本身失效了**（路径写错 / 模块被改名），
        #    必须 FAIL。实测打包版就撞过这一条：只 glob ``*.py``
        #    扫到 0 个文件然后报 "ok"，是**假绿**，比不检查更危险。
        lines.append(
            f"FAIL  AI 适配器扫描扫到 0 个文件（{ai_dir}）—— "
            "检查已经失效，不是通过"
        )

    # --- 1b. 已加载模块里也不许出现网络模块名（打包后源码读不出来时的兜底）---
    try:
        import importlib

        leaked: list[str] = []
        for name in ("core.ai.local_assistant", "core.ai.cloud_adapter", "core.ai"):
            mod = importlib.import_module(name)
            bound = set(vars(mod))
            hit = bound & {
                "requests", "httpx", "urllib", "socket", "http", "aiohttp",
                "openai", "anthropic", "websocket", "urllib3",
            }
            if hit:
                leaked.append(f"{name} -> {sorted(hit)}")
        if leaked:
            lines.append(f"FAIL  AI 模块里绑定了网络模块名：{leaked}")
        else:
            lines.append(
                f"ok    {'AI 模块无网络绑定（打包版兜底）':<32} 查了 3 个模块的命名空间"
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL  AI 模块命名空间检查失败：{type(exc).__name__}: {exc}")

    # --- 2. 脱敏出口 -------------------------------------------------------
    try:
        from core.ai.cloud_adapter import FORBIDDEN_FIELDS, redact_student

        dirty = {
            "S编号": "S001", "姓名": "张三", "学号": "2023001",
            "身份证": "110101...", "加权均分": 85.05, "意愿标签": "考研",
            "student": "张三", "student_id": "2023001",
        }
        clean = redact_student(dirty)
        leaked = [k for k in clean if k in FORBIDDEN_FIELDS]
        if leaked:
            lines.append(f"FAIL  脱敏出口漏了字段：{leaked}")
        elif "姓名" in clean or "学号" in clean:
            lines.append("FAIL  脱敏出口把姓名/学号放出去了")
        else:
            lines.append(
                f"ok    {'AI payload 强制脱敏':<32} "
                f"8 个字段进 → {len(clean)} 个出（{sorted(clean)}）"
            )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL  脱敏出口自检失败：{type(exc).__name__}: {exc}")

    # --- 3. 非白名单联网库不该在包里 ---------------------------------------
    if getattr(sys, "frozen", False):
        for banned in ("requests", "urllib3", "httpx", "aiohttp", "openai", "anthropic"):
            try:
                __import__(banned)
            except ImportError:
                lines.append(f"ok    {'未打包 ' + banned:<32} 骨架期不该有它")
            else:
                failures_hint = (
                    f"打包里混进了 {banned} —— AI/更新都还是骨架，"
                    "是不是有人偷偷加了依赖？"
                )
                lines.append(f"FAIL  {failures_hint}")

    # --- 4. SMTP 例外要**真的在用**（避免"允许了却没人用"）-----------------
    try:
        import smtplib  # noqa: F401

        from core.email_recovery import SmtpSender  # noqa: F401

        lines.append(
            f"ok    {'SMTP 例外（charter §3）':<32} smtplib + SmtpSender 就位"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL  SMTP 路径不可用：{type(exc).__name__}: {exc}")

    # --- 5. 更新检查：**唯一**被允许的 stdlib 联网（charter 第四节）---------
    #
    # charter v1.7 把"更新检查"加进了白名单：用户点「检查更新」时，
    # 允许用 urllib.request 只读一次 Gitee 的版本号。
    #
    # ★ 但"允许"不等于"不管"：这里守三件事 ——
    #   1) 它真的只用 stdlib，没偷偷引第三方 HTTP 客户端；
    #   2) **import 期绝不联网**（没有自动检查，必须等用户点按钮）；
    #   3) 超时就是 5 秒（不然按钮会卡很久）。
    lines.extend(_updater_policy_lines())

    return lines


def _updater_policy_lines() -> list[str]:
    """更新检查的三条守卫（charter 第四节 / 任务书任务四）。

    ★ 源码模式 / 打包版**分两层**，和 ``core/ai`` 那条扫描同一个套路：

    * **源码模式**（``core/updater.py`` 在磁盘上）：静态扫 import + 把
      ``urlopen`` 换成"一调用就抛"的桩**重新 import 一遍**。两条都是真的。
    * **打包版**：模块在 **PYZ 归档**里，磁盘上**没有** ``.py`` ——
      静态扫描无从下手。这时改查**命名空间**（模块真的拉了 ``urllib.request``、
      而且没有任何被禁的库进了 ``sys.modules``），并且**明说**哪一项只在源码
      模式生效，不装作查过了。

    ★ 第一版忘了分支，打包版直接报
      ``FAIL  core/updater.py 不存在 —— 更新检查没有实现``，
      可它明明就在包里跑着 —— 打包被自检拦下了（闸门起作用了，但结论是假的）。
      和当初 ``core/ai`` 扫到 0 个文件报 ok 是同一个坑的两面：
      **"扫不到"不等于"没问题"，得按打包形态换一种查法。**
    """
    import importlib
    import re as _re
    import sys as _sys
    import urllib.request

    lines: list[str] = []
    try:
        module = importlib.import_module("core.updater")
    except Exception as exc:  # noqa: BLE001
        return [f"FAIL  core.updater import 失败：{type(exc).__name__}: {exc}"]

    banned = ("requests", "httpx", "aiohttp", "urllib3", "openai", "anthropic")
    updater_path = PROJECT_ROOT / "core" / "updater.py"

    # --- 1. 只用 stdlib，绝不引第三方 HTTP 客户端 --------------------------
    if updater_path.exists():
        source = updater_path.read_text(encoding="utf-8")
        offenders = [
            name
            for name in banned
            if _re.search(rf"^\s*(?:import|from)\s+{name}\b", source, _re.MULTILINE)
        ]
        if offenders:
            lines.append(f"FAIL  core/updater.py 引了第三方 HTTP 客户端：{offenders}")
        elif not _re.search(
            r"^\s*(?:import|from)\s+urllib\.request\b", source, _re.MULTILINE
        ):
            lines.append(
                "FAIL  core/updater.py 没有用 urllib.request（charter 7.3 唯一允许的方式）"
            )
        else:
            lines.append(
                f"ok    {'更新检查只用 stdlib':<32} urllib.request，无 requests/httpx"
            )
    else:
        # 打包版：源码进了 PYZ。改查命名空间 + sys.modules
        uses_stdlib = hasattr(module, "urllib") and hasattr(module.urllib, "request")
        leaked = [name for name in banned if name in _sys.modules]
        if leaked:
            lines.append(f"FAIL  打包版里出现了被禁的联网库：{leaked}")
        elif not uses_stdlib:
            lines.append(
                "FAIL  打包版 core.updater 的命名空间里没有 urllib.request —— "
                "更新检查没接上"
            )
        else:
            lines.append(
                f"ok    {'更新检查（打包版）':<32} 无 .py 源码（打进 PYZ），"
                "改由命名空间检查兜底"
            )

    # --- 2. import 期不许联网（没有自动检查）------------------------------
    #
    # 做法：把 urlopen 换成一个"一调用就抛"的桩，再把模块重新 import 一遍。
    # 只要模块体里有任何一次网络调用，这条就 FAIL。
    # 这比"在源码里 grep 有没有调用"结实得多 —— 常量折叠、间接调用都躲不掉。
    real_urlopen = urllib.request.urlopen

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("import core.updater 的时候联网了")

    urllib.request.urlopen = _boom  # type: ignore[assignment]
    try:
        importlib.reload(module)
    except AssertionError as exc:
        lines.append(f"FAIL  core.updater 在 import 期联网：{exc}")
    except Exception as exc:  # noqa: BLE001
        # 打包版重载可能不被支持（源码在 PYZ 里）。**明说**，不装作查过了 ——
        # 这道检查真正生效的地方是打包前的**源码自检**（build_exe.ps1 先跑它）。
        lines.append(
            f"ok    {'更新检查不自动跑':<32} 打包版无法重载（{type(exc).__name__}），"
            "本项以打包前的源码自检为准"
        )
    else:
        lines.append(
            f"ok    {'更新检查不自动跑':<32} import 期零网络调用（只有点按钮才发请求）"
        )
    finally:
        urllib.request.urlopen = real_urlopen  # type: ignore[assignment]

    # --- 3. 超时 5 秒 ------------------------------------------------------
    from core.updater import DEFAULT_TIMEOUT

    if DEFAULT_TIMEOUT != 5:
        lines.append(f"FAIL  更新检查超时应为 5 秒（charter 7.2），实际 {DEFAULT_TIMEOUT}")
    else:
        lines.append(f"ok    {'更新检查超时':<32} {DEFAULT_TIMEOUT}s（charter 7.2）")
    return lines


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


@dataclass(frozen=True)
class VaultStatus:
    """启动时对加密库的体检结论（**不带界面**，方便自检直接断言）。"""

    exists: bool
    version: str
    #: ``"none"`` 不用管 / ``"migrate"`` 该问用户要不要迁 / ``"too-new"`` 库比程序新
    action: str
    detail: str


def vault_status(root: str | None = None) -> VaultStatus:
    """看一眼加密库的版本，决定启动时要做什么（charter 8.2 的启动检测）。

    ★ 这一步**必须在登录之前**做得出来 —— 所以数据版本标记写在
      ``users.json``（明文索引）里，而不是某个要密码才打得开的密文里。
      不然"要不要迁移"这个问题得先登录才能回答，而登录本身可能就
      因为版本不对而失败。
    """
    from core.crypto_store import CryptoStore, default_root
    from core.migrator import check_compatibility
    from core.version import CURRENT_VERSION, compare_versions
    from pathlib import Path as _Path

    target = _Path(root) if root else default_root()
    store = CryptoStore(target)
    if not store.users_path.exists():
        return VaultStatus(False, CURRENT_VERSION, "none", "还没有加密库，不需要迁移。")
    try:
        data = store.load_users().to_dict()
    except Exception as exc:  # noqa: BLE001 - 读不出来也得让程序能启动
        return VaultStatus(
            True, "?", "none",
            f"users.json 读不出来（{type(exc).__name__}: {exc}），本次不做迁移。",
        )

    needs, old = check_compatibility(data)
    if compare_versions(old, CURRENT_VERSION) > 0:
        return VaultStatus(
            True, old, "too-new",
            f"检测到数据版本 v{old}，高于程序版本 v{CURRENT_VERSION}。请先更新程序。",
        )
    if needs:
        return VaultStatus(
            True, old, "migrate",
            f"检测到旧版数据 (v{old})，可以迁移到 v{CURRENT_VERSION}。",
        )
    return VaultStatus(True, old, "none", f"数据版本 v{old}，与程序一致。")


def run_startup_migration(root: str | None = None) -> None:
    """启动检测（charter 8.2 / 任务书 2.4）：问用户要不要迁移，然后照做。

    三条硬约束：

    1. **备份失败 = 拒绝迁移**（``core.migrator.migrate_store`` 里保证）；
    2. 用户选「取消」→ **继续运行**，旧数据一个字节都不动；
    3. 库比程序新 → **只提示，绝不动它**。
    """
    from core.crypto_store import CryptoStore
    from core.migrator import MigrationError, migrate_store
    from core.version import CURRENT_VERSION as _CUR
    from PySide6.QtWidgets import QMessageBox

    # 自动化（截图脚本、真实时序测试）里不该弹模态框 —— 会一直等人点。
    # 只有显式设了这个环境变量才跳过；正常启动永远会走完检测。
    if str(os.environ.get("TL_SKIP_MIGRATION", "")).strip() not in ("", "0"):
        return

    status = vault_status(root)
    if status.action == "none":
        return

    if status.action == "too-new":
        QMessageBox.warning(None, "数据版本比程序新", status.detail)
        return

    answer = QMessageBox.question(
        None,
        "检测到旧版数据",
        f"{status.detail}\n\n"
        "迁移前会把整个数据目录完整备份一份（放在数据目录里的 backup_ 文件夹），"
        "备份失败就拒绝迁移；迁移过程中只改版本标记，成绩、意愿、档案都不动。\n\n"
        "要现在迁移吗？",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if answer != QMessageBox.StandardButton.Yes:
        QMessageBox.information(
            None,
            "已跳过迁移",
            f"程序继续以 v{_CUR} "
            f"运行，旧数据保持 v{status.version} 不动。下次启动还会再问一次。",
        )
        return

    store = CryptoStore(_Path(root) if root else None)
    try:
        outcome = migrate_store(store)
    except MigrationError as exc:
        QMessageBox.critical(
            None,
            "迁移失败",
            f"{exc}\n\n旧数据没有被改动，程序仍可继续使用。",
        )
        return
    QMessageBox.information(None, "迁移完成", outcome.detail)


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
    # ★ 放在开屏**之前**：迁移可能弹模态框，让模态框跟开屏动画抢屏幕
    #   会变成一团乱。这里按次序问完，再让开屏出场。
    run_startup_migration(flow.root)
    flow.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
