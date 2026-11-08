# CHANGELOG v1.9.3 —— 启动可靠性：日志、备份、登录卡住、单文件解包

> 版本号真源：`core/version.py` 的 `CURRENT_VERSION = "1.9.3"`。
> 数据版本：`_lift_1_9_2_to_1_9_3`（`core/migrator.py`，纯标记迁移，不动数据）。
> 封板四文件（`core/calculator.py` / `core/parser.py` / `core/validator.py` /
> `tests/test_calc.py`）**一个字符都没动**，`main.FROZEN_FILE_SHA256` 原样通过。

v1.9.2 发出去之后，现场报回来四个症状：登录卡住、迁移报错、exe 起不来、界面上有杂线。
它们的共同点是**出错时不吭声**（`--windowed` 的 exe 没有 stderr，异常等于丢进黑洞）。
这一版只做一件事：把"静默失败"改成"要么成功，要么说清为什么"。

本版五块：

1. **运行期日志**（新增 `core/applog.py`）：关键步骤落盘，未捕获异常与卡死栈都能取证。
2. **迁移备份瘦身**：跳过 `models` / `runtime` / `logs` / `cache` 四个**可重建**目录，
   迁移不再因为拷 1.1 GB 模型而失败；核心密文一个不少。
3. **登录兜底 + 看门狗**：密码通过之后任何异常都不许再让界面停在「PBKDF2 派生中」；
   淡出动画不回调时看门狗补发 `logged_in`。
4. **主窗淡入看门狗**：`windowOpacity` 卡在 0 时强制补成 1（否则用户看到"程序没出来"）。
5. **单文件 exe 解包修复**：打包加 `--runtime-tmpdir "."`，解到 exe 自己所在目录，
   不再碰会被拦的 `%TEMP%`。

---

## 一、运行期日志（`core/applog.py`，新增 124 行）

| 函数 | 作用 |
|---|---|
| `write(tag, message="", *, root=None)` | 写一行 `时间 [tag] 内容`，**任何失败都吞掉** |
| `install_excepthook(root)` | 未捕获异常先落盘，**再**交给原钩子 |
| `install_faulthandler(root)` | 线程栈 dump 到 `logs/fault.log` |
| `dump_stack(note="")` | 疑似卡死时手动 dump |
| `tail(lines=40, *, root=None)` | 读最后几行（错误弹窗里直接给用户看） |

- 位置 `<数据根>/logs/app.log`，默认 `%LOCALAPPDATA%\SGMS\logs\app.log`；
  超过 `MAX_BYTES = 512 * 1024` 滚成 `.1`（只留一代）。
- `main()` 在 `make_app` 之后、**更新交接与迁移之前**装钩子，并写一行 `boot`
  （版本 / 是否 frozen / 数据根）。
- 已接入的埋点：`boot`、`login`（开始校验 / 密码通过 / 进主界面前出错 + traceback）、
  `flow`（登录后流程 / 淡入看门狗介入）、`prewarm`（预热失败）、`migrate`（开始 / 失败 / 完成）、
  `crash`（未捕获异常）。

## 二、迁移：只备份"不可再生"的东西

`core/migrator.py`：

```python
SKIP_DIR_NAMES = frozenset({"models", "runtime", "logs", "cache"})
```

`backup_vault()` 的 `_skip()` 现在同时跳过：已有的备份（防套娃）、`*.tmp`、以及上面四个目录。

- 现场数据目录：`models/` 1.1 GB、两份历史备份各 1.1 GB。原来"点一次迁移"要拷 GB 级数据，
  几十秒起步，中途被占用就整次失败（用户看到的就是「旧版迁移数据报异常」）。
- 四个目录**都能重建**：模型重新下载、后端重装、日志本就可丢、缓存随时可清。
- `users.json` / `app.salt` / `u_*/`（成绩、档案、侧写、学生主档、审计）**一个字都不省**。
- 迁移失败弹窗现在会附上：备份跳过了什么、`models` 占多大、磁盘剩多少、日志路径。

## 三、登录：密码对了必须进得去

`ui/login_window.py`：

- `_do_login()` 在密码通过之后，把 `_handle_remember()` + `_leave()` 整体包进
  `except Exception`：落完整 traceback → `_leaving` 复位 → 状态改红（「进入主界面失败：…」）
  → 弹「进不去主界面」并给出日志路径 → **允许直接再点一次**。
- `LEAVE_FAILSAFE_MS = 1600` 看门狗：淡出动画不回调时 `_force_leave()` 补发 `logged_in`
  （`_left` 保证幂等，窗口透明度与 `hide()` 也一并补上）。
- 三种 `CryptoError` 分支（没有这个账号 / 密码错误 / 其它）的**既有文案一字未改**。

## 四、主窗淡入看门狗

`ui/main_window.py`：

- `FADE_IN_FAILSAFE_MS = 900`；`show_and_fade_in()` 末尾挂
  `QTimer.singleShot(FADE_IN_FAILSAFE_MS, self, self._ensure_opaque)`。
- `_ensure_opaque()`：`windowOpacity() < 0.99` 才介入 —— 写一行 `flow` 日志并 `setWindowOpacity(1.0)`。
  900ms 是 200ms 动画的 4 倍多，正常路径碰不到。

## 五、单文件 exe：解包不再碰 `%TEMP%`

- 现场报错逐字：`[PYI-19100:ERROR] Could not create temporary directory!`（PyInstaller 启动器，
  Python 起来之前就失败，应用层插不上手）。
- 实测矩阵：`%TEMP%` / 桌面 / 用户目录根 / `AppData\Local\*` 全部失败；项目目录树内成功；
  同一个 8 MB hello-world 单文件包表现完全一致 → **本机策略在拦"未签名程序在常见目录建解包目录"**。
- 修法：`build_exe.ps1` 新增 `-RuntimeTmpDir`（默认 `"."`），release / console 目标固定带
  `--runtime-tmpdir $RuntimeTmpDir` → 解到 **exe 所在目录**。实测 hello-world 在 `%TEMP%`
  与用户目录根**都是 exit 0**。
- 两个边界（已写进脚本 `.NOTES`）：相对路径按**启动时 CWD** 解析（双击时 = exe 目录）；
  不支持环境变量；上级目录必须已存在。
- `build_exe.ps1` 的 BOM 也补回来了（`edit`/`write` 写出来的是无 BOM，PS 5.1 会按 GBK 解、
  把 `{` `}` 吃掉，报一堆看不懂的解析错误 —— 见 AGENTS §1b）。

## 六、界面：圆角半径统一（未定论的"杂线"）

- `ui/styles.qss`：与父级共用同一个圆角的子控件半径统一成「父级 − 1px」
  （`#WindowTitleBar` / `#LoginBrand` / `#Sidebar` / `#TopBar`：12 → 11），
  对应 AGENTS §4e 第 2 条。
- ★ **这一条不是已证实的修复**：离屏渲染在 100% / 150% DPI 下都没能复现用户说的「多余线条」，
  四角只有"1px 描边 + 投影"两层。需要真机截图才能定位（见总纲 §27.8）。

## 七、判据

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_startup_robustness.py`（新增） | 10 | 日志落盘/读回/不可写不炸/滚动；备份跳过四目录且核心密文逐字节一致；登录异常被兜住、错密码行为不变、看门狗补发且幂等 |
| `tests/test_version_migration.py` | 27 | 版本链 `1.0 → 1.9.3` 九跳齐全 |
| `main.py --selfcheck` | — | `core.applog` 进 `CHECK_MODULES`（50 个）；docs 白名单 5 份 |
| `build_exe.ps1 -Both` + exe `--selfcheck` / `--bootcheck` | — | 打包与启动时序全绿 |

## 八、已知限制（记录，非待办）

- 单文件版解包目录跟随**启动时工作目录**。双击 = exe 目录（正常）；从别的目录命令行启动
  或快捷方式改了"起始位置"，解包会落在那个目录里 —— 那里不可写就会报同样的错。
  交互式排查用 `dist\launch.bat`（它顺手把 `TEMP` 指到 `dist\_tmp`）。
- 登录界面「多余线条」仍未定位（见第六节）。
- 模型下载失败后的「复制链接」兜底、GPA 方案 2/3/4 等 v1.9.2 的"未纳入"项不变。

## 九、v1.9.3 补充：错误弹窗「复制错误信息」

**现场诉求**：报错时只能截图或手打一句"报错了"，没有 traceback、没有日志路径。

- 新增 `ui/error_report.py`：
  - `install_error_copy(app, root)` 在 QApplication 上装事件过滤器，任何 **Critical
    图标**的 `QMessageBox` 自动多一个「复制错误信息」按钮；
  - 点按钮把 **traceback + 日志路径 + 最近 40 行日志** 写进剪贴板，窗口不关；
  - `install_crash_handler(root)` 接管未捕获异常：先由 `core.applog` 落盘，再在主线程
    弹带完整 traceback 的错误框（PySide6 槽函数异常会走 `sys.excepthook`，实测）。
- ★ 按钮不能用 `QMessageBox.addButton()`：实测任何 role 都会触发 `done()` 关窗；
  现在是普通 `QPushButton` 塞进 `QDialogButtonBox` 的布局。
- `main()` 的安装次序：`install_error_copy` → `applog.install_excepthook` →
  `install_crash_handler` → `install_faulthandler`。
- 判据：`tests/test_error_report.py`（9 条，离屏，桩掉剪贴板，点完不关窗）+
  `main._error_report_gate_report()`；全量 `pytest` **887 passed**；
  `--selfcheck` / `--bootcheck` 全绿。
