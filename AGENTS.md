# AGENTS.md — 本仓库的硬性施工规则

## 0. 开工前先读 `docs/DEV_SUMMARY.md`（开发总纲）

本文是**施工细则** —— 每条规则的判据、踩过的坑、为什么这么定。
`docs/DEV_SUMMARY.md` 是**项目全貌** —— 现行规格、已关闭的业务规则、架构、
版本演进史、测试与闸门清单。**动任何代码之前先读它一遍。**

它是 `docs/` 里唯一的开发文档：4 份互相取代的旧规格（v1.3 Spec 与 v1.6 / v1.7 /
v1.8 三份 charter），以及 v1.8.1 删除整套激活码系统的记录，都已合并进这一份并删除。
旧编号（"Spec 第 9 章"、"charter 6.3"）对应新章节的对照表在**总纲第二十节**。

`main._docs_gate_report()` 在 `--selfcheck` 里守这条：`docs/` 下出现白名单之外
的文件、或任何源码 / 文档提到已删除的旧文档名，一律 FAIL。

## 1. 禁止用 PowerShell 文本管道改任何 .py / .md 文件

```powershell
# ⛔ 绝对禁止
(Get-Content x.py -Raw) -replace 'a','b' | Set-Content x.py -Encoding UTF8
```

**原因（不是洁癖，是已经出过事故）**：Windows PowerShell 5.1 的 `Get-Content -Raw`
默认按**系统 ANSI（本机 GBK）** 解码，而仓库里所有源文件都是 **UTF-8**。
中文是三字节，被当成 GBK 双字节读 → 错位；错位的配对还会把**相邻的 ASCII 字节
（引号、括号）一起吃掉**。一次 `-replace` 往返就让 `tests/test_calc.py`
830 行、约 1400 处变成 `?`，文件再也无法 parse。

**改源码只用这三种方式**：

1. IDE / 编辑器的全局替换（或 Agent 的 `edit` / `write` 工具）
2. Python：`Path(p).read_text(encoding="utf-8")` → `write_text(..., encoding="utf-8")`

读文件同理：用 `read` 工具或 Python，**不要**用 `Get-Content`。

> 只读地看一眼（`Select-String`、`Get-ChildItem`、运行 `pytest`、`git status`）
> 不受这条限制 —— 受限的只有"读进来再写回去"这个往返动作。

## 1b. `.ps1` 里的非 ASCII 必须有 **UTF-8 BOM**

Windows PowerShell 5.1 读**没有 BOM** 的 `.ps1` 时，按**系统 ANSI（本机 GBK）**
解码：中文按 GBK 解会错位，错位的配对会把相邻的 ASCII 字节一起吃掉（GBK 的
尾字节范围 `0x40~0xFE` 正好包含 `{` `}` `\`）—— 报出来的是
`Unexpected token '}'` / `Missing closing '}'` 这种**完全看不出根因**的解析错误，
而文件在编辑器里看着好好的。

**教训（第二次踩同一个根因）**：`edit` / `write` 工具写出来的是**不带 BOM** 的
UTF-8 —— 用它改过 `build_exe.ps1` 之后，脚本立刻在 PS 5.1 下解析失败（20 个
解析错误）。所以：**改完任何 `.ps1` 之后，跑一遍补 BOM + 语法检查** —— 补 BOM 就是
"头 3 个字节不是 `EF BB BF` 就补上（正文不动，只加 3 个字节）"；语法检查用
`[System.Management.Automation.Language.Parser]::ParseFile(路径, [ref]$null, [ref]$err)`，
**不要靠"跑一下试试"，那要等几分钟**。两段命令照抄 `build_exe.ps1` 里现成的
那一段即可；当时的现场与报错原文 → §二十三 1b。

## 2. 红线（总纲 §三 铁律 1-10 + §十三 导入解析规则，违反即重写）

- 加权算术**全部本地完成**，绝不调用任何大模型 API 做数字
- **空 ≠ 0**：空 / `-` / `/` / `未选` = 没选这门课，学分不进分母；数字 `0` = 缺考，学分进分母
- 学分提取必须有人工回显确认
- 真实成绩文件、真实凭据（邮件授权码等）**一律不入库**。仓库里唯一允许存在的
  xlsx 是 `tests/fixtures/fake_*.xlsx`
- `config/` 下只允许 `.example.json`
- 不写注册表。**联网：v1.6 起改为有条件放行 —— 见第 13 节**（SMTP + 用户主动启用
  云端 AI；自动更新 / telemetry / 静默请求仍然禁止）

## 3. 落盘

- 明文成绩**任何时候都不落盘**；磁盘上只有 `.enc` 密文
- 用户数据在 `%LOCALAPPDATA%\SGMS\`，**不在仓库里**
- 任何"校验 / 解锁"动作都**绝不清空或改动已有成绩库**（第 30 节）

## 4. Qt / PySide6 界面：踩过的坑（4a–4m）

界面已从 tkinter 全量换成 **PySide6**（`ui/` 旧文件已删干净，没有 .bak）。
以下每一条都是**实测复现过**的，不是理论风险。
**4k 是其中最严重的一条，动窗口动画之前务必先读它。**

### 4a. 一个 widget 只能有一个 `graphicsEffect`

阴影（`QGraphicsDropShadowEffect`）和透明度（`QGraphicsOpacityEffect`）抢的是**同一个槽位**：
卡片挂了阴影再 `fade_in(card)` 会抛 `TypeError`（`ui/anim.py` 故意拦下来）。要淡入淡出的是
**页面容器**，卡片挂在它下面。现场代码 → §二十三 4a。

### 4b. ★ `QGraphicsOpacityEffect` 会缓存源 pixmap —— 后果是整页空白

`ui/anim.py` 的 `fade_in` / `fade_out` **必须在动画结束后把效果摘掉**
（`clear_effect`）。不摘的话：切页动画挂上的效果会留在那儿（opacity=1）→ 页面**内部**增删控件 →
效果缓存的 pixmap 不失效 → **整页画成空白** → 切走再切回来才恢复。**静态断言查不出来**
（控件都在、文字都对、几何也对），只有截图看得见。
实测现场 → §二十三 4c。

### 4c. 验证 GUI 不能只断言控件存在，要量像素

三次事故都是"断言全绿、界面上却是坏的"：「忘记密码？」按钮看不见（登录窗写死 `820x500`，
内容需要 639px）、表格「操作」列的按钮只剩半个字（行高 30px < 容器边距 12 + 按钮最小高
26）、侧写页整页空白（见 4b）。现场与量法 → §二十三 4c。

**规矩**：

1. 需要换行的 label 必须给宽度约束，否则会把窗口撑宽（切页时窗口横向跳动）。
2. `QHeaderView.ResizeToContents` **不把 `setCellWidget` 放进去的控件算进来** ——
   操作列必须写死宽度（`setColumnWidth` + `Fixed`）。
3. 行高要 ≥ 单元格内控件的 `minimumSizeHint().height()` + 容器上下边距。
4. 每个"看起来对"的界面都要**截图看一眼**（见 `%TEMP%\tl_qt_drive.py`）。

### 4d. 打包：`--exclude-module` 里绝不能有 PySide6

`build_exe.ps1` 里绝不能有 `--exclude-module PySide6`（tkinter 时代的遗留）—— 留着的话
PyInstaller 会把整个 Qt 排除，**exe 一启动就 `ModuleNotFoundError`，而源码自检照样全绿**
—— 自检跑的是源码，不是包。`ui/styles.qss` 是**数据文件**，必须 `--add-data` 进包，否则 exe
里窗口能开、界面全白。判据 → §二十三 4d。

### 4e. 无边框窗口（`ui/frameless.py`）的四条硬约束

两个顶层窗口（登录窗 / 主窗）都走 `FramelessWindowHint` +
`WA_TranslucentBackground` 自绘，系统标题栏没了，四件事必须自己扛：

1. **窗口自己必须显式透明**：补一条
   `QMainWindow#FramelessWindow { background: transparent; }`，否则
   `QMainWindow { background }` 会把留给阴影的那圈边距也填成不透明。
2. **★ 圆角不裁剪子控件**：贴边的几块（侧栏 / 顶栏 / 标题栏 / 品牌区）
   **各自要带对应角的圆角**，最大化时一起取消。
3. **不要手算拖拽/缩放的像素**：用 `QWindow.startSystemMove()` /
   `startSystemResize()`（多显示器 DPI、Aero Snap、Win+方向键才全都正确）。
4. **最大化时把阴影边距收成 0、圆角关掉**（否则屏幕边缘留缝、圆角与直角打架）。

### 4f. ★ `QWidget` 自带只读的 `maximized` 属性 —— `setProperty` 会**静默失败**

`Q_PROPERTY(bool maximized READ isMaximized)` 是**只读**的（`isWritable()` 为
`False`），往它 `setProperty("maximized", "true")` 不抛异常、只返回 `False` ——
于是 QSS 里的 `[maximized="true"]` 选择器**永远匹配不上**：样式没生效，日志干净。

**规矩**：动态属性名一律加前缀（本项目 `tlMaximized`），`setProperty` 之后**读回来确认**，
不要信返回值（动态属性也会返回假值）。回归自检 `property_name_is_safe()` +
`%TEMP%\tl_qt_drive.py`。

### 4g. ★ `Qt.Edge` 是 `enum.Flag`，`int()` 会抛 `TypeError`

`return edges if int(edges) else None` ⛔ → `return edges if edges else None` ✓（`Flag` 有
定义好的 `__bool__`）；需要数值时用 `edges.value`。**这个分支鼠标每动一下都会走到** ——
异常从 `eventFilter` 里冒出来，Qt 只打一行
`Error calling Python override of QObject::eventFilter()` 就吞掉：界面是"边框光标永远不变"，
日志是看不懂的 TypeError。

### 4h. 验证 GUI 必须**真的动鼠标**，不能只调函数

4g 那个 bug 躲过了源码自检、截图和 76 项冒烟（那些都不产生鼠标事件），**只在自动化里鼠标
恰好停在窗口上时才炸**（经过 → §二十三 4h）；`tl_qt_drive.py` 因此有一步
`QTest.mouseMove()` 扫过四边四角。

**规矩**：凡是挂在 `eventFilter` / `mouseMoveEvent` / `hoverEvent` 上的逻辑，测试必须用
`QTest.mouseMove()` 真的走一遍，不能只调背后的纯函数。完整经过 → §二十三 4h。

### 4i. `QRadioButton` 互斥组里，已选中的那个**取消不掉**

互斥组（`QButtonGroup.setExclusive(True)`）不允许把当前选中项直接 `setChecked(False)` ——
界面上是"状态栏写着还没有意愿、按钮却亮着上一个学生的志愿"，像串号，其实是单选组的行为。
**换人回填必须先 `setExclusive(False)`，填完再打开。** 现场 → §二十三 4i。

### 4j. `QGraphicsDropShadowEffect` 挂整窗的代价：约 40ms/次切页

实测（1900×1200、150% DPI）：阴影开/关 523/483ms（+8%），可接受；别在动画期间额外触发整窗 `update()`。→ §二十三 4j

## 5. 密码找回：已接通邮箱（网易 163 SMTP）

- **恢复码 = 唯一的第二把钥匙。** 注册成功时生成一串 24 位找回码，本地存一份"用它包起来的库
  密钥"（`u_<uid>/recovery.json.enc`，应用级密文）并把码寄到用户邮箱；忘密码时凭它解出库密钥、
  设新密码，**成绩一条不丢**。
- ★ **不能改成"发个新验证码就重置"**：库密钥只在本地、只被密码包裹，忘密码那天已经解不开；
  唯一能解开它的是**注册时**生成、并已用它包好密钥的那串码。**这个时序是命门。**
- 注册**必须填邮箱并验证**（6 位验证码 / 10 分钟 / 最多试 5 次 / 60 秒冷却）。
- 个人中心可以「重新生成找回码」——要再输一次密码（生成需要库密钥）。

### 5a. ★ 邮件授权码绝不许进仓库

真实凭据在 `%LOCALAPPDATA%\SGMS\email.json`（**仓库外**）；仓库里只有
`config/email.example.json` 模板，字段全空。

`main.py --selfcheck` 里有一条**反扫**：从 AppData 读出密钥，再拿它扫全仓库源码，
命中就报 FAIL。

> ⚠️ 写这条检查时**不要把密钥写进 main.py** —— 那正好犯了要防的错。
> 反过来做：密钥从配置文件读，代码里永远只有"比较"这个动作。
> 见 `_credential_leak_report()`。

### 5b. 网络请求必须走后台线程

`ui/workers.py` 的 `run_async`。在按钮回调里直接 `smtplib.sendmail()` 的话：顺利时卡 1 秒，
**网络不通时卡满 15 秒超时**（`SMTP_TIMEOUT`），期间窗口不重绘、Windows 可能弹"无响应"。
回调在主线程执行，所以回调里可以安全碰控件。

## 6. 命令行开关：`in` 是精确匹配，不是前缀匹配

⛔ 踩过的写法：`if "--mailcheck" in args:` —— `args = ["--mailcheck-send"]` 时它是 False
（列表的 `in` 比的是**整个元素**），于是匹配不上任何分支、**掉进最后的 GUI 分支把窗口开起来**
（症状：卡死、不输出、退出码永远拿不到）。

**规矩**：长的开关先判；不确定就用 `any(a.startswith("--mailcheck") for a in args)`。
回归测试 `%TEMP%\tl_cli_test.py`（含静态检查：断言分支顺序）。

## 7. 诊断命令必须逐行即时输出

`run_mailcheck` 原先攒到最后一次性写报告，卡住时**什么都没有** —— **不输出的诊断等于没有
诊断**。改成 `emit()` 每步立刻落盘 + `flush`，一眼看出停在哪一步 → §二十三 7。

## 8. 验证打包后的 exe 之后，先把它彻底关掉再重打

```
PermissionError: [WinError 5] 拒绝访问: '...\dist\transcript-lens.exe'
```

onefile 的 exe 启动时会**再起一个子进程**（bootloader + 真正的应用）；只 `Stop-Process`
父进程的话，子进程还活着、exe 文件还锁着，下一次打包就炸。**按进程名杀**（不是按 PID）：
`Get-Process -Name 'transcript-lens*' | Stop-Process -Force` 之后
`Start-Sleep -Seconds 3`（句柄释放有延迟）。`build_exe.ps1` 的自检跑这个 exe 的
`--selfcheck` 时自己不会锁文件 —— **锁文件的只可能是手动启动的那一份**。

> ★ 新建出来的 exe 第一次启动会被 Defender 全盘扫描，冷启动可能比之后慢好几倍 —— 别把
> 第一次启动的耗时当成性能基准。完整经过 → §二十三 8。

## 9. ★ 界面上"这个学生是谁"一律用**学号**，不许用姓名

同名学生历届都有 —— 凡是拿姓名当键的地方都会**串号**，而且不报错（`ui/intentions.py` 的
意愿字典、`ui/flags.build_flags()` 的 `{姓名: flag}`、表格按姓名查结果）。

**规矩**：① 身份一律走 `ui.intentions.student_key(student_id, student)` —— 有学号 →
`sid:<学号>`，没有才退回 `name:<姓名>`（**并且界面要明说同名风险**）；② 逐行预警用
`ui.flags.build_flags_by_index()`（**按行序返回列表**），不要用 `build_flags()` 的姓名字典；
③ 表格列下标一律从 `COLUMN_TITLES` 推导（`NAME_COLUMN` / `ID_COLUMN` / `FIRST_SCOPE_COLUMN` /
`FLAG_COLUMN` / `ACTION_COLUMN`），**禁止手写元组**；④ 老数据（裸姓名键）在
`load_intentions` 里自动补 `name:` 前缀，侧写页在**同名唯一**时把 `name:张三` 迁到
`sid:...`。手写列下标那次的现场 → §二十三 9。

## 10. 智育分析页的计算口径筛选

三个 Toggle：`必修` / `必修+限选` / `全部` —— 正好是 `CalculationScope` 的三档，
**不新增口径、不改口径定义**（真源：总纲 **§1.2** 三口径表 —— ① 必修+限选+选修
② 必修+限选 ③ 仅必修；默认必修+限选计入，**选修默认剔除**）。

> ⚠️ 旧规格（v1.3 Spec + v1.6 / v1.7 / v1.8 三份 charter）**已全部合并删除**，现在**只认
> `docs/DEV_SUMMARY.md`（开发总纲）**；旧编号（「61 / 246 行」、「5.2 / 第 3 章 / 第 4 章」）
> **一律不再引用** —— 对应哪一节见总纲**第二十节**对照表。

- 切筛选只做三件事：`showColumn` / `hideColumn` 对齐当前口径列；
  红标**跟着当前口径重算**（分母变了，挂科/缺考的判定基础就变了）；
  把重算后的名单再推给侧写页一次（它名单上的红点也按口径判）。
- 三个口径列**始终都在**，筛选只改可见性。**不要**去增删列 ——
  那会让"表头 ↔ 单元格"重新有机会漂移，而那正是上一版的 bug。

### 10a. 表头三档**共用一句「加权均分」**

同一时刻只有一档列可见（另外两档 `hideColumn`）。三条信息各归各位：**表头**说这一列是什么
数据（恒定，`SCOPE_HEADER`）；**筛选按钮 + 状态行**说现在算的是哪一档（`FILTER_LABELS`）；
**表下方明细行**把三档各是多少逐档写全名（`SCOPE_COLUMNS` 第二项）。

★ 所以 `COLUMN_TITLES` 里三档口径列是**同一个字符串**，**测试不能再靠表头文字
区分是哪一档** —— 要按下标判（`FIRST_SCOPE_COLUMN + SCOPE_COLUMNS 里的档位`）。
`%TEMP%\tl_qt_drive.py` 和 `tl_smoke_new.py` 都已改成按下标判，并加了
"三档表头必须完全相同"的断言。

★ 明细行仍然把三档**同时**列出来、各带全名和 `Σ学分 / Σ成绩×学分`。

### 10b. ★ 为什么是**互斥三档**，而不是三个可自由勾选的类别

旧 charter 6.4 当年写的是（现在记在总纲 §1.3）"三个独立 Toggle：必修 / 限选 / 选修，可多选
组合"。**按字面做成任意组合是做不到的**：能算出什么数由 `CalculationScope` 决定
（`core/calculator.py`，**铁律一冻结**，只有①全部 ②必修+限选 ③仅必修三个成员），
`{限选}`、`{选修}`、`{限选,选修}` **没有对应口径**；要支持它们只能改封板文件（违反铁律一）
或在界面里自己乘一遍加权（违反铁律五）。**所以界面保留互斥三档**，把"这一档算了哪几类"
写在明面上：按钮气泡与状态行走 `scope_breakdown()`（读 `CalculationScope.categories`），
表下常驻说明用 `CALC_SCOPE_NOTE`。行为门见第 19 节；推导 → §二十三 10b。

## 11. 切页锁的看门狗必须**把页面落到位**，不是只放锁

⛔ 只放锁（`self._busy = False`）不行：动画还在跑，用户再点侧栏就是两段动画抢
同一个 opacity，界面会停在"侧栏亮着 A、内容还是 B"且永不自愈。
✓ 必须**把 index 落到位 + 摘掉残留的透明度效果 + 再放锁**
（`self._stack.setCurrentIndex(self._pending)` → `clear_effect(...)` →
`self._land(...)`）。

阈值 `LOCK_TIMEOUT_MS` = **1500ms**（总纲 §10.3 指定）。**它的职责是"绝不能把一个本来会
正常结束的动画砍掉"**，不是"卡住多久算卡住"（现在一次切页中位 ~137ms，1500ms 是 10 倍
余量）。动画又变长了就先重量再决定要不要放宽，**不要**因为"感觉不够快"就往下调；回归测试
见 `%TEMP%\tl_qt_drive.py` 的"看门狗落位"那一段。

## 12. 性能基线（改完要重量，别凭感觉）

两处都是用户反馈过的卡顿；数字全部实测，脚本留在 `%TEMP%`。

| 指标 | 改之前 → 现在 | 怎么量 |
|---|---|---|
| 切页（中位） | 439 ~ 500 ms → **~137 ms** | `%TEMP%\tl_perf_bench.py` |
| 启动到主窗可见（源码） | 6681 ms → **3055 ms** | `%TEMP%\tl_startup_time.py` |
| 开屏 `show()` | 1030 ms → **165 ms** | `%TEMP%\tl_splash_probe.py` |
| 开屏 pixmap 绘制 | 242 ms → **~1 ms** | `%TEMP%\tl_perf_bench.py` |
| 欢迎页停留 | 1500 ms → **1500 ms（总纲 §10.2 定死）** | `ui/welcome.py` 的 `HOLD_MS` |

> ⚠️ 欢迎页那 1500ms **不要**再"优化"掉：总纲 §10.2 明确写了 1.5s，**文档优先** ——
> 要动先问，不要凭手感调（第 25c 条）。

**量性能的两条规矩**：① 必须带对照组，一次性成本要单独起进程测；② 别用写死的
`pump(1600)` 等状态，用轮询 `wait_until()` / 锁存器 → §二十三 12。

## 13. ★ 联网红线（**有条件放行**）

真源：`docs/DEV_SUMMARY.md` **第四节 联网红线**（即旧 v1.8 charter 第四节）。

### ✅ 允许（**只有这四件**）

| 场景 | 条件 | 实现 |
|---|---|---|
| SMTP 发邮件 | 仅找回密码流程，用户点「发送验证码」触发 | `core/email_recovery.py` |
| 辅导员手动启用云端大模型 | 设置页手动开 + 填 API Key，**只在点「深度评估」时**调用 | `core/ai/`（骨架） |
| **更新检查** | 用户点「检查更新」→ `urllib.request` 只读一次 Gitee 版本号 | `core/updater.py` |
| **更新下载 / AI 模型下载** | 用户点「开始下载」并勾选文件 → 流式下载 | `core/updater.download_exe` |

★ **v1.8 把"更新下载"加进了白名单，但 ❌ 那一条没变**：

```
❌ 自动更新/静默下载 · telemetry · 埋点 · 崩溃上报 ·
   任何形式的「输码解锁 / 联网校验用户资格」 · requests/httpx 用于上述场景之外
```

所以"允许下载"的含义是：**每一次下载前面都必须有一个用户点过确认的弹窗**；代码里
不许有"启动时顺便检查/下载一下"的路径 —— 判据之一是 `_updater_policy_lines()` 里
那条"import 期零网络"（`urlopen` 换成炸弹后重新 import，见第 21 节）。

### selfcheck 怎么守（`main._network_policy_report`）

★ **守卫要落在行为上，不能落在"这个包在不在"上**（原来那条"打包里见到 requests 就 FAIL"
只能查打包版、源码版什么都查不到）。现在五条：① **静态扫
`core/ai/*.py`**，出现任何
`import requests/httpx/urllib.request/socket/http.client/openai/anthropic` 就 FAIL（正则只认
真正的 import 语句，不认注释里提到的名字）；② **拿脏数据调一次 `redact_student`**，看姓名 /
学号 / 身份证会不会漏出去；③ 非白名单联网库**仍然**不该在包里 —— 但报错文案改成"AI/更新都还是
骨架，是不是有人偷偷加了依赖"；④ **更新检查**三条（`_updater_policy_lines`，见第 21 节）；
⑤ **更新下载**三条（`_download_policy_lines`，见第 26 节）。

★ 第 1 条**不需要为了 v1.8 放宽**（理由见第 28 节）："`core/ai/` 零 HTTP import" 这条闸门
照旧全量生效，扫描范围一个字没动。完整原文 → §二十三 13。

## 14. 奖学金测算（总纲 §九，**规则已定死**）

    final = 智育加权(口径① 全口径) × 0.7 + 德育分 × 0.3

* 班级内按 final 降序；前 X / 接下来 Y / 再 Z 名分一、二、三等（X/Y/Z **每班独立配置**，
  存 `class_{班级号}_quota.json.enc`）。输出列、四种奖配色、名额与特等的完整口径 →
  **总纲 §9.1-§9.4**。
* **特等奖**：final 进入前 X 名 **且** 每门原始成绩 ≥ 90。**不占名额**（拿特等的人移出名额
  池、后面的人往前补）；**默认不叠加**，勾了"允许叠加"显示 `特等+一等`（统计**两边都数**）。

★ **德育分没录 ≠ 0 分**（红线 2 的同一条道理）：没录的人按 0 参与测算，但**必须备注**
"德育分未录入，按 0 计"（表格里没录的格子是**空白**，不是 `0`）。

★ **`core/scholarship.py` 里一行加权算术都没有。** 智育分是导入时 `core.calculator` 用
口径①算好、存进 `class_{班级号}.json.enc` 的；这里只做"乘 0.7 / 加 德育×0.3 / 排序 /
截名额" —— 公式见总纲 §9.1。**除此之外不许再加算术。**

★ 特等奖的"每门 ≥90"只检查**这个学生真考了的课**（`score is not None`）：没选的课不参与
判定，**不会**被当成 0 分拖累他；缺考（数字 0）**算考了**，0 < 90，拿不到特等。

## 15. 班级号规范

* 导入成绩表时**必须**有人工确认的班级号（总纲 §9.2）。
  分析页顶部有输入框；空着就在"选完文件之后、确认课程弹窗之前"弹窗问。
* 格式：**非空、字母数字或短横线**，以字母或数字开头，1~32 位。
  校验函数 `core.scholarship.validate_class_name`（唯一真源，别在别处再写一份正则）。
* 一个班一个文件：`class_{班级号}.json.enc`；
  德育分 `class_{班级号}_moral.json.enc`；名额 `class_{班级号}_quota.json.enc`。
* **班级列表靠扫目录**（`list_classes`），不另存索引 ——
  索引和文件一旦对不上，界面就会显示一个点进去没内容的班。
  扫的时候用严格正则排除 `_moral` / `_quota`，否则它们会被当成班。
* 德育分的键是**学生 `key`（`sid:<学号>`）**，不是姓名（铁律 4）。

## 16. 加密库的两层分工（别搞混）

| 层 | 函数 | 需要密码？ | 放什么 |
|---|---|---|---|
| **密码级** | `store.save(session, ...)` | ✅ 要 | **成绩**、意愿标签的原始库 |
| **应用级** | `core/user_store.save_user_json` | ❌ 不要 | 班级快照、德育分、名额、纵向归档、AI 设置 |

★ 为什么辅助数据走应用级：**自动登录时没有密码**（只有 session token），库密钥解不开 ——
按密码级加密的话，自动登录进来就全是空的。

★ **成绩绝不许往应用级里塞。** 这是这条分工的底线。

★ **例外（Owner 在 v1.9 二期拍板，v1.9.1 正式记录）**：**成绩历史**
（`u_<uid>/grade_history.json.enc`）与**侧写**（`student_profiles.json.enc`）走
**应用级密文 + 审计日志**（"成绩加密方案 C：应用级加密 + 审计日志"）—— 自动登录时没有密码，
档案页要能读；**与本条旧底线的冲突以这一条为准**。**其余成绩**（导入的原始成绩库、按密码级
保存的那份）**仍然绝不许**进应用级。

## 17. ★★ 切页抖动的正解：不要给页面挂透明度效果

**症状**：点侧栏切页，落定之后界面**略微抖一下**。

**根因（实测钉死）**：给新页挂 `QGraphicsOpacityEffect` 会把整页渲染进**一张离屏 pixmap**
（那条路径**没有 ClearType**）—— 效果一摘整页重光栅化一遍，字全换了画法。量法
`%TEMP%\tl_clean_probe.py`（带对照组校准）：摘掉效果前后 **17.4% 的像素不同**、最大通道差
**240**。推导 → §二十三 17。

**修法**：页面**永远不挂效果** —— 要淡入的改成盖在上面的一层**纯色幕布**淡出
（`ui/anim.py` 的 `_PageCover` + `StackSwitcher._after_out`）：先 `setCurrentIndex`（新页立刻
走**正常渲染路径**画出来）→ 再盖一块 `#F1F5F9` 幕布 → 幕布淡出，新页从背景里浮出来，视觉
上和"淡入"一样。幕布是整块纯色，离屏渲染与直接渲染**逐像素一致**；判据：**落定后再等 1 秒，
两帧必须逐像素相同**（改之前差 17.4%，`%TEMP%\tl_cover_verify.py`）。顺带修掉的真竞态（**6.27ms** 窗口，
先挂 0 再换页）→ §二十三 17。

## 18. 加一个页面前先想清楚"数据从哪来"

v1.6 新增的两个页面（成长档案、奖学金测算）都遵守同一条：

* **不重新解析 Excel、不重新算加权** —— 导入时算一遍，把结果存进加密库
  （`class_{班级号}.json.enc` / `archive.json.enc`），页面只负责显示；
* **口径固定**：档案页固定用「必修+限选」，**不跟智育分析页的筛选按钮联动** ——
  跟着屏幕上的临时视图变，会出现"上学期看是 84、这学期看是 86"。

★ 页面之间的数据流一律走**一处真源 + 信号刷新**：分析页导入完 → `class_saved` 信号 →
主窗 `_on_class_saved` 通知另外两页 `reload_classes()` / `reload()`。刷新失败**只打日志、
不弹窗**（数据已经存进去了）。

## 19. ★★ 必修 / 限选 / 选修是**三个独立类别**（总纲 §一 + §三 铁律 8）

总纲 §1.1 的原话：**"必修、限选、选修是三个完全独立的类别，绝不混同"**、**"任何将
选修归入限选的逻辑都是 bug"**。

真源两处，别在别处再判断：`core.parser.Category`（`REQUIRED="必修"` /
`LIMITED="限选"` / `ELECTIVE="选修"`，+ `UNKNOWN`）与
`core.calculator.CalculationScope.categories`（每档允许计入的类别集合）。三档口径及枚举值
（总纲 §1.2）：① 总加权 = 必修＋限选＋选修（＋未标类别），`CalculationScope.ALL`；② 仅必修
+限选，`CalculationScope.REQUIRED_LIMITED`；③ 仅必修，`CalculationScope.REQUIRED_ONLY`。

★ **行为门** `main._category_policy_report()`（`--selfcheck` 里跑）：不只数枚举值，而是**真算
一遍**三门各属一类的样例 —— 高等数学(4学分)必修 = 80 ｜ 专业英语(2学分)限选 = 90 ｜ 音乐鉴赏
(1学分)选修 = 100 → 口径① 600/7 = 85.714286、口径② 500/6 = 83.333333、口径③ 320/4 = 80。
**只要有人把选修当成限选算进②，② 立刻变成 85.714286 == ①，检查当场 FAIL**
（`%TEMP%\tl_category_gate_test.py` 注入了这条违规验证过）。

★ ⚠️ **一处已知的历史遗留，别当成 bug 顺手改**：冻结的 `core/parser.py` 里 `CATEGORY_KEYWORDS`
把 **"专业选修"** 归到 **限选**（校名别名）—— 且 parser 铁律一封板，**一个字符都不许动**。
（取值规则与推导 → §二十三 19）

## 20. ★ 学期标签：导入必须选学期，档案必须**累加**

总纲 §10.2 的欢迎页 / §10.4 的成长档案页都依赖它。

### 20a. 导入流程

* 分析页顶部有 **学期下拉框**（`大一上 → 大四下`，8 个）。
* 选项来自 **`SEMESTER_ORDER[:TOTAL_TERMS]`**，不另抄一份表 ——
  档案排序（`core.archival.semester_rank`）用的就是这张表，抄第二份会漂移，
  症状是"时间轴上大二下排在大一上前头"。
* **不含大五**：本项目的剩余学期按 `TOTAL_TERMS = 8` 算，列出来只会误导。

### 20b. ★★ 光加下拉框不够 —— 必须把已有档案带进去 append

调用必须带上 `archive=` 这个实参（把加密库里的档案读回来当底座，用
`self._load_archive()`）：⛔ `build_archive(parsed, self.term, source=name)` 每导一次就把上一个
学期整份覆盖掉（档案永远只有一个学期、趋势 Δ 永远显示"单次导入，暂无趋势"）；
✓ `build_archive(parsed, self.term, archive=self._load_archive(), source=name)`。

`core.archival.build_archive(..., archive=...)` **不改入参**，返回追加后的新 `Archive`；同一个
`(key, term)` 重复导入会**保留两份**并记一条 warning —— 绝不静默丢掉某一次导入。

### 20c. 落盘与串号

* 归档走**应用级**加密（`core/user_store`，第 16 节）：自动登录没有密码。
* `AnalysisPage.set_user()` 必须**清掉内存里那份 `_archive`** —— 它是上一个人的，
  留着会让下一个人导入时把两个人的学期并进同一份档案。
* `user_file()` 按 `users.json` 里的 **uid** 解析路径：**账户不存在时 `save_user_json`
  直接抛 `CryptoError("本机没有账户…")`** —— 测试里建临时 store **必须先
  `store.register()`**。

### 20d. 判据

`%TEMP%\tl_semester_test.py`（31 项）真的连导三个学期：大一上（同页）→ 大二下（同页）→
**新建页面**再导大三上（= 重启程序的等价场景）。断言：三份记录都在、趋势 Δ 是**真涨的**
（第三份表分数确实更高，Δ>0）、磁盘上 `archive.json.enc` 读回来也是三个学期、文件里
**扫不到明文姓名**。

## 21. ★ 更新检查：`urllib.request` 是唯一允许的 stdlib 联网

总纲 §四 把它加进了白名单。**"允许"不等于"不管"**，
`:func:`main._updater_policy_lines`` 在 `--selfcheck` 里守三条：

| 守卫 | 怎么守 | 为什么这么守 |
|---|---|---|
| 只用 stdlib | 扫 `core/updater.py`，出现 `requests/httpx/aiohttp/urllib3/openai/anthropic` 就 FAIL | 防"顺手加个依赖" |
| **import 期零网络** | 把 `urllib.request.urlopen` 换成"一调用就抛"的桩，**重新 import 一遍模块** | 静态 grep 抓不住间接调用；重新 import 是行为验证 |
| 超时 == 5s | 直接断言 `DEFAULT_TIMEOUT == 5` | 总纲 §六 写死的 |

★ 三条守卫**都验证过会 FAIL**（`%TEMP%\tl_updater_gate_test.py` 真注入违规再按字节还原）
—— 不验一次就不算有牙。

### 21a. 请求必须走后台线程

设置页的「检查更新」走 :func:`ui.workers.run_async`。
在按钮回调里直接 `urlopen` 的话，**断网时要卡满 5 秒超时**，
期间窗口不重绘（`ui/workers.py` 开头讲的就是这个）。

### 21b. ★ 实测：`/releases/latest` 是 404，那不是"故障"

2026-09-27 实测：仓库公开可读（200），但 `/releases` 返回 `200 []`、
`/releases/latest` 返回 **404** —— **一个发行版都没发过**。

所以只打 `latest` 的话，发布第一个发行版之前这功能永远只能报"检查失败"，而那不是实话。
`check_for_update()` 因此有四种状态：`newer`（弹窗"发现新版本 vX，是否前往下载？"）、
`current`（"当前已是最新版本"）、`no-release`（"…还没有发布任何发行版"）、`error`
（"检查失败，请手动访问仓库"）。

★ **`check_latest_version()` 的契约一个字没改**（总纲 §六 指定的那个）：成功给版本号字符串，
失败给 `None`；要分状态用 `check_for_update()`。★ **列表非空但读不出版本号** ≠ "没有发行版"
（第一版把两者混成一种 → 界面说假话）。

### 21c. ★ v1.8 起：**可以下载，但绝不自动**

v1.7 时这里写的是"绝不自动下载、绝不自动替换，只打开浏览器"；**总纲 §四 已经把"更新下载"
加进白名单**（第六节给了完整的下载 → 重启 → 替换流程），那段话已被取代：

| | |
|---|---|
| ✅ 可以 | 用户点「开始下载」之后流式下载；用户点「立即重启」之后备份 + 替换 |
| ❌ 仍然禁止 | **静默/自动下载** —— 启动时、定时、后台，一律不许 |
| ❌ 仍然禁止 | **热替换**（趁自己还活着去覆盖自己的 exe）—— 那是必失败或写出半截文件 |

每一次下载前面都必须有一个**用户点过确认的弹窗**，代码里不许有"启动时顺便查一下/下一下"
的路径。★ "是不是偷偷联网了"的判据仍是第 21 节那条**行为**检查（`urlopen` 换成炸弹后重新
import 模块），不是读源码找关键字。

## 22. ★ 数据迁移与版本保护（总纲 §十二）

三层防护，缺一层都不算数：

```
版本标记 → 迁移前强制备份 → 失败回滚
```

### 22a. "加密库"= **整个数据目录**，不是某一个文件

这个项目里**没有** `vault.enc`：

```
%LOCALAPPDATA%/SGMS/
    users.json      ← 用户索引（明文，而且**数据版本标记就写在这里**）
    app.salt        ← 应用级盐。**丢了它，意愿/班级/档案/AI 设置全部解不开**
    u_xxxxxxxx/     ← 每个用户的密文载荷（grades / tags / archive / class_* / ...）
```

所以 `backup_vault()` 备份的是**整个目录**。只备份 `users.json` 等于没备份 ——
成绩全在 `u_*/` 里。★ **备份目录自己必须排除**（备份就放在数据目录**里面**，总纲 §12.2 的
路径）—— 不排除的话每迁一次就翻一倍。

### 22b. 标记写在哪、什么时候写

* 键：**两个键同时写**（`core.version.VAULT_VERSION_KEYS`）：
  `__vault_version__`（总纲 §12.3 的值）和 `vault_version`
  （任务书 2.1 / 旧 v1.8 charter §1.1 两次给的值）。**读取时哪个有值认哪个。**
  唯一写入点是 `core.version.stamp_vault_version()`，`--selfcheck` 断言两键都在且相等
  （为什么非写两份不可 → §二十三 22b）。
* 位置：`users.json` 顶层，跟着 `UserIndex` 走 —— `save_users()` 每次都从
  `UserIndex.to_dict()` 重写整个文件，标记**不挂在 UserIndex 上就会被下一次写盘悄悄
  抹掉**。
* 时机：`CryptoStore.register()`（建第一个用户 = 初始化这个库）调 `init_vault()`。
  **只在没有标记时盖**（覆盖已有标记会让"检测到旧版数据"永远发现不了旧数据）。写在
  **明文**的 `users.json` 里，因为启动检测发生在**登录之前** —— 那时没有密码，密文打不开。

### 22c. 流程（`core.migrator`）

```python
needs, old = check_compatibility(data)      # (bool, str) —— 任务书 2.3 的签名
if needs_migration(data):                   # ★ 判真假**必须**用这个
    backup_vault(root, old)                 # 备份失败 → 抛 MigrationError，拒绝迁移
    data = migrate(old, data)               # 按版本链走，只改标记
    store.save_users(UserIndex.from_dict(data))
```

★ **`check_compatibility()` 返回的是元组，元组永远是真值** ——
写成 `if check_compatibility(data):` 会恒为真。判真假请用 `needs_migration()`。

★ `migrate()` 的 `password` 参数：给了就先验证它真能开这个库；
**启动路径上传空**（那时候还没登录）。当前这条链不需要密码（没有数据结构变化、
不重包裹密钥），参数留着是为了将来那一跳不用改签名。

★ **回滚只恢复 `users.json`** —— 迁移全程只动过这一个文件；整目录回滚会把备份之后新写的
数据盖掉，那才是真的丢数据。

### 22d. 版本比程序新时

`check_compatibility` 返回 `(False, "9.9")`（不需要"迁移"），
启动时**只弹窗提示"请更新程序"，绝不动它**。别把降级当成迁移。

### 22e. 判据

`%TEMP%\tl_migrate_test.py`（62 项）用**真实目录**跑完整条链：
新建库带标记 / 老库判成 1.0 / 备份内容齐全（含 `app.salt` 和成绩密文）/
同秒连备两次不覆盖 / 迁移后仍能登录且成绩一条不丢 / **备份失败时 users.json 一个字节没动** /
**写回失败时逐字节回滚**。

## 23. 版本号管理规范

**改版本号只改 `core/version.py` 的 `CURRENT_VERSION` 一行。**

| 位置 | 怎么拿 |
|---|---|
| `core/updater.py` | `from core.version import CURRENT_VERSION` |
| `core/migrator.py` | 同上，另加 `compare_versions` |
| 设置页 | 同上 |
| `main.vault_status()` | 同上 |
| `ui/login_window.APP_VERSION` | `f"v{CURRENT_VERSION}"`（模块级常量） |
| `ui/splash.SplashScreen(version=)` | 默认参数 `f"v{CURRENT_VERSION}"` |
| `ui/welcome.py` 底部版本小字 | `f"v{CURRENT_VERSION}"` |
| `ui/pages/account_page._ABOUT` | `("版本", f"v{CURRENT_VERSION}")` |
| 三个顶层窗口标题 | `setWindowTitle(f"... v{CURRENT_VERSION}")` |

★ 各写一份字符串，迟早出现"设置页写着 1.7、更新检查却按 1.6 比"这种事，而且**不报错，
只是行为不对** —— `--selfcheck` 断言
`core.updater.CURRENT_VERSION is core.version.CURRENT_VERSION`。

### 23a. ★★ UI 里**一个版本号字面量都不许有**（总纲 §三 铁律 10）

不是假想风险 —— v1.8 开工时在 `ui/` 里抓到**三处**写死的 `"v1.0"`：
`login_window.APP_VERSION`、`splash.py` 的默认参数、`account_page._ABOUT`。**三处全都不报错**，
只是一直在说假话（程序是 1.8、界面写着 1.0；静态 grep 也查不出来）—— 位置与症状 → §二十三 23a。

**判据**（`main._version_source_report()`，`--selfcheck` 里跑）：① **静态** —— 用 `ast` 扫
`ui/**/*.py` 的**字符串字面量**，出现"整串就是个版本号"（`^v?\d+\.\d+(\.\d+)*$`）就
FAIL（注释不在 AST 里，文档字符串显式跳过）；② **行为** —— 真读
`login_window.APP_VERSION`、`SplashScreen` 默认参数、`account_page._ABOUT`，必须等于
`f"v{CURRENT_VERSION}"`。

★ 闸门**验过有牙**：往 `ui/splash.py` 注入一处 `version: str = "v1.0"`，立刻 FAIL
并指名 `ui\splash.py:183`（`%TEMP%\tl_v18_gate_test.py`）。

★ 写文档（`.md`）时该写版本号就写 —— 这条只管**界面代码**。

★ 比较一律走 `compare_versions()`（**数值**逐位比）——`"1.10" > "1.9"` 用字符串比会得出
False，等真出 1.10 才暴露。★ `normalize_version()` **不削末尾的 0**（第一版削了，
`"1.0"`→`"1"`，备份名和迁移链查表同时坏掉）；`"1.7.0"` 和 `"1.7"` 算不算同一个版本**是
`compare_versions()` 的事**（缺位按 0 补），不是规范化的事。

### 23b. 升版本号 = **同时补迁移链的一跳**

改完 `CURRENT_VERSION`，必须去 `core/migrator.py` 的 `_STEPS` 补一跳 —— **哪怕什么数据结构
都没改，也要写个 `return data` 的空函数**。

不补的后果是**老用户启动时直接报错**：

```
MigrationError: 没有从 v1.7 升级的路径（已知：1.0, 1.6, 1.7, 1.8）
```

v1.7 → v1.8 时实测踩到（`--selfcheck` 的迁移检查当场 FAIL，拦住了）。
补一跳只要三行，忘掉的代价是老用户升不了级。

## 24. ★ 界面上不许出现 Markdown 记号（总纲 §三 铁律 9）

`QLabel` / tooltip 都是**纯文本**（或用 Qt 富文本），`**加粗**` 会**原样显示**两个星号。
踩过两次：分析页表下常驻说明 `**绝不并进限选**`、奖学金页「允许特等叠加」的气泡
`**不占**` —— 两次都是**所有静态断言全绿、只有截图才看见**。

判据：`%TEMP%\tl_label_lint.py`（7 项）。它是**运行时遍历**，不是扫源码（文案可能是拼出来的
—— `CALC_SCOPE_NOTE` 就是模块级常量拼进 QLabel 的）：主窗 162 处控件文字 / 气泡 + 登录窗 +
欢迎页全过一遍，出现 `**` / `__` / 反引号即 FAIL，并且第 4 节带反向自检。

★ 写文档（`.md`）时该加粗就加粗 —— 这条只管**界面文案**。

## 25. 已知限制清单（**记录，不是待办**）

三条都是**确认过的真实行为**。动它们之前先想清楚要不要解封 `core/calculator.py`（铁律一）。

* **25a** `core/parser.py` 把「专业选修」归**限选**（校名别名）—— 铁律一封板，**一个字符都
  不许动**；要改是 Owner 的事。
* **25b ★ 分数 > 100 会静默掉出加权分母**，却仍留在 `course_scores` 里（实测 91 改成 101：
  口径② 85.052632→82.928571、`total_credit` 19→14）—— 智育把它当没算，奖学金「每门 ≥90」却
  算它达标。缓解只在界面（`out_of_range_scores()` 进强制确认弹窗），**不动 calculator**。
* **25c** 欢迎页 1.5s 是总纲 §10.2 的**文档定义值** —— 不要再优化掉，要动先问。

## 26. ★★ 自动更新器：四个"绝不"（总纲 §六）

完整流程与排查手册见 `docs/UPDATE_GUIDE.md` / `docs/DISTRIBUTION.md`；
这里只列**不许违反的硬约束**。

### 26a. 四个绝不

| 绝不 | 靠什么保证 |
|---|---|
| **绝不静默下载** | 每个下载前面都有用户点过的弹窗；`download_exe` 没有任何自动调用点 |
| **绝不热替换** | 只生成 `.bat`，本进程退出后由 bat 复制（运行中的 exe 是锁着的） |
| **绝不在下载失败时损坏已有 exe** | 全程只写 `<dest>.part`，成功了才 `os.replace` |
| **绝不在备份失败时替换** | `backup_current_exe()` 抛异常 → 直接放弃，旧 exe 一个字节不动 |

### 26b. ★ `.bat` 正文必须是纯 ASCII（踩过一次）

第一版 `rem` 注释写了中文，被自己的断言逮住：`cmd.exe` 按当前代码页**逐字节**解码 bat，
中文按 GBK 解错位后的尾字节范围（`0x40~0xFE`）正好包含 `"` 和 `\` —— 会把后面那句
`copy /Y "%SRC%" "%DST%"` 的引号**吃掉**，报错完全看不出根因。和 §1b（`.ps1` 的 BOM）
**同一个根因**，完整经过 → §二十三 26b。

### 26c. ★ 路径不写进 bat，走命令行参数（`%~1..%~4`）

路径里可能有中文（用户名）。参数走 Windows 的 UTF-16 API，不受代码页影响；
写进文件就要赌用户机器的代码页。

### 26d. 复制要重试，失败就什么都不做

`for /L %%i in (1,1,60)` 每秒重试（刚退出的进程可能还占着句柄几秒）。
60 次全失败 → `goto :giveup`，旧 exe 原样留着 —— **最坏情况是"更新没生效"，不是"程序没了"。**

### 26e. 必须写日志

bat 在**本进程退出之后**跑，那时没有控制台、没有异常、没有反馈。每次交接都往
`%TEMP%\SGMS_Update\apply_update.log` 写一行 —— 没有它，"点了立即重启但版本没变"
完全查不下去。

### 26f. 启动次序（`main()` 里）

```
run_pending_update()      ← 最先。结论可能是"马上退出让 bat 去替换"
run_startup_migration()   ← 可能弹模态框，必须早于开屏
flow.start()              ← 开屏
```

`run_pending_update()` 自己**不碰 Qt**；越早退出，exe 文件锁松得越快。
自动化环境用 `TL_SKIP_UPDATE=1` 跳过（和 `TL_SKIP_MIGRATION` 同套路）。

### 26g. 判据

| 测试 | 覆盖 |
|---|---|
| `%TEMP%\tl_apply_probe.py`（17 项） | **真的用 cmd 跑一遍生成的 bat**：目标 exe 被换掉、临时文件被删、exit code 0/2 正确 |
| `%TEMP%\tl_download_probe.py`（24 项） | 本地 HTTP 服务器：续传 / **服务器忽略 Range 时从头重下** / 取消 / sha256 / 失败不损坏已有文件 |
| `%TEMP%\tl_v18_test.py`（96 项） | 交接全路径（源码模式不替换 / 哈希不符拒绝 / 备份失败拒绝 / 正常交接） |
| `main._apply_mechanism_report()` | bat 内容五要素齐全 + **全 ASCII** |
| `main._download_policy_lines()` | 只用 stdlib + **真跑一次失败下载**验证目标文件未变 |

★ `tl_v18_test.py` 里所有会走到"拉起脚本"的用例**都注入了假 `Popen`** ——
第一版没注入，于是真的把 bat 跑起来了。**测试绝不能有这种不受控的副作用。**

## 27. ★ Excel 引擎依赖（总纲 §七）

### 27a. 引擎按**内容**选，不按扩展名（`core/excel_io.py` 是唯一真源）

| 文件头 | 引擎 | 是什么 |
|---|---|---|
| `PK\x03\x04` | `openpyxl` | 真 xlsx/xlsm（zip 容器） |
| `D0 CF 11 E0 A1 B1 1A E1` | `xlrd` | 老格式 xls（OLE2 复合文档） |
| `09 00 04 00` + version | `xlrd` | 裸 BIFF 流（Excel 2.0/3.0，无 OLE2 外壳） |

★ **必须显式指定引擎**：实测即使 xlrd 装好了，`pd.read_excel("真的.xls")` 对**裸 BIFF 流**
依然报 `ValueError: Excel file format cannot be determined, you must specify an engine
manually.` —— pandas 自己的 sniff 只认 zip 和 OLE2。

### 27b. ★★ "导 .xlsx 却弹出 xlrd 错误"的根因（实测复现）

pandas **不看扩展名、看内容魔数**：扩展名 `.xlsx` 而内容是 OLE2 的老表会被判成 `xls`，
没装 xlrd 就抛带 pip 指令的 `ImportError`。**修法**：按内容选引擎 + 兜底链。→ §二十三 27b

### 27c. ★ `xlrd` 必须 **>= 2.0.1**，任务书写的 `<2.0` 是错的

| xlrd 版本 | pandas 2.3.3 读 .xls |
|---|---|
| 1.2.0（`<2.0`） | `ImportError: Pandas requires version '2.0.1' or newer of 'xlrd'` |
| 2.0.2 | 正常读出 |

1.2.0 那条例外里**同样带着版本指令** —— 等于花力气把错误信息换了个写法。
`requirements.txt` 已写 `xlrd>=2.0.1`，自检断言这个底线。

### 27d. ★ 铁律 1 与「要改 parser 引擎」的冲突，怎么解的

旧 v1.8 charter 第七节说"`core/parser.py`：`pd.read_excel(path, engine='openpyxl')`
显式指定"，同一份文档的第三节铁律 1 又说"parser 一个字符不许改"—— 自己跟自己打架。

**解法**：`core/parser.parse_excel` 的签名里本来就有 `**read_excel_kwargs`
并原样转发给 `pd.read_excel` —— 那个扩展点就是为这种情况留的：

```python
# core/parser.py:947（冻结，未改动）
df = pd.read_excel(path, sheet_name=sheet_name, header=None, **read_excel_kwargs)

parse_excel(path, engine=engine_for(path))   # 调用方传引擎进去
```

"显式指定"逐字满足，**封板文件零改动**。自检里两条不变量护着：
`core/parser.py` 无 `engine=`，且 `**read_excel_kwargs` 扩展点还在。

### 27e. 错误文案绝不含包管理指令

`core/excel_io.friendly_error()` 把任何异常翻成人话，`sanitize_message()`
在**出口**抹掉 `pip install` / `conda install` / `Use pip or conda to install` /
`Missing optional dependency` / `install xlrd`。自检拿真 pandas ImportError
喂进去，断言 0 处 pip/conda。

### 27f. 铁律 1 的封印（`main.FROZEN_FILE_SHA256`）

四个封板文件（`core/calculator.py`、`core/parser.py`、`core/validator.py`、
`tests/test_calc.py`）的 sha256 写死在 `main.py` 里，`--selfcheck` 每次核对。

**要解冻 → 同时更新哈希。** 那个动作必须是**有意的**：不是"顺手改一下让它绿"，
而是"我们决定解冻铁律 1"，改哈希本身就是审批记录。
★ 验过有牙：给 `core/parser.py` 末尾加一个换行，`--selfcheck` 立刻 FAIL。

## 28. ★ `core/ai/` 的零 HTTP import 规则**没有**为模型下载放宽

那条"`core/ai/*.py` 出现任何 HTTP import 就 FAIL"（v1.6 立的）**一个字没改**。
做法：`core/ai/model_downloader.py` 里**一行 `urllib` 都没有** ——
字节流复用 `core.updater.download_exe`，本模块只管"下到哪、叫什么、装好没有"。

于是两条规则都不用让步：`core/ai/` 仍零 HTTP import（**扫描范围无需放宽**），模型下载仍是
`urllib.request`（在 `core.updater` 里）。

★ 以后往 `core/ai/` 加下载类功能**照这个模式做**：把网络关在 `core.updater` 里 ——
口子一开，那条闸门只能放宽，而放宽之后的闸门等于没有。

## 29. 模型文件的落盘位置与判据

* 位置：**`%LOCALAPPDATA%\SGMS\models\<名字>\`**，不是仓库里的 `models/` —— 1~3GB 进仓库
  是灾难，打包后程序目录常常只读。
* 落盘顺序：`models/temp/<名字>__<文件名>` → 校验 → **原子改名**进 `models/<名字>/`。
* ★ 判据是"目录里有一个非空的 `.gguf`"，**不是"目录存在"** ——
  下载中途断电会留下空目录，那个不算装好。
* ★ `_safe_name()` 清洗目录名，`delete_model()` 再验一次"解析后仍在 models/ 里"
  —— **防目录穿越**：`download(url, "../../evil")` 不该写到 models/ 外面去。
* `ModelDownloader.progress` 发**百分比 0~100**；**总大小未知时发 `-1`**
  （界面切不确定态）。字节数在 `downloaded_bytes` / `total_bytes` 上读。

## 30. ★★ 激活码系统**已整套删除**（Owner 决定）—— 不许复活

**为什么删**：注册登录 + 邮箱找回密码已经跑通，**登录即全部功能可用** —— 删掉的四个文件 /
入口与遗留文件处置 → `docs/DEV_SUMMARY.md` 第 16.1-16.4 节 + §二十三 30。

**四条硬约束**：

1. **不许以任何形式复活**「输一个码才能解锁功能」：没有闸门、没有试用期、
   没有"激活才能导入"。
2. 准入只有三个门：**注册 → 登录 → 忘记密码**。加第四种之前先问 Owner。
3. `core/` `ui/` `ai/` 里不许再出现激活码的标识符；`ui/` 的**界面文案**
   不许出现「激活」二字。
4. 老机器上可能残留 `%LOCALAPPDATA%\SGMS\activation.enc` —— **它是惰性的**，
   没有任何代码读它。**不许**写"启动时顺手删掉"那种代码：
   删用户数据必须是用户主动的动作。

**判据**：`main._removed_gate_report()`（`--selfcheck` 里跑，三层：文件层 / AST 源码层 /
`CHECK_MODULES` 接线层；覆盖范围 → §二十三 30）+ `%TEMP%\tl_qt_drive.py`（运行时遍历主窗与
登录窗全部控件文字、占位符、气泡，0 处「激活」）+ `%TEMP%\tl_noact_gate_test.py`（24 项，
注入违规会 FAIL，跑完逐字节还原）。

★ 用 `ast` 而不是 grep `activ`：`ui/pages/scholarship_page.py` 里有 openpyxl 的 `book.active`，
一刀切搜必然误报 —— **误报的闸门最后一定会被关掉**。

## 31. ★ 模型中心（v1.9 新增）

`core/ai/model_registry.py`（**零 Qt、零 HTTP import** —— 字节流借
`core.updater.download_exe`，照第 28 节的模式）+ `ui/model_center_dialog.py`。

* 清单是 Gitee raw `update/ai_models.json`：**读不到只是警告，界面照开**
  （给"复制链接手动下载"兜底）；默认勾选**只认「轻松」**（见第 33 节）—— 不再看清单里的 `defaultCheck`。
* ★ **内存闸门不许放行**：`minRamGb` 写成 `"8GB"` 要认得出；认不出必须给中文警告，
  **不能退化成 0**（见 `need <= 0` 就放行等于没闸门）。内存不够的行 `flags` 里
  **没有 `ItemIsEnabled`** —— 整行不可选，不只是不勾。
* ★ **装没装好以磁盘为准**：`installed.json` 只是账本，`merged_with_local()` 让磁盘上
  真有的 `.gguf` 优先。写账本的门：`size` 差在 `SIZE_TOLERANCE`（0.01，下限 4096
  字节）以内**且** sha256 对得上。绝不自动下载；失败重试 1 次 → 标红 + 复制链接。
* 判据：`tests/test_model_registry.py`（109 用例）+ `%TEMP%\tl_v19_ui_test.py`（假
  `fetch_model_list` / `merged_with_local` / `download_exe`，量像素）。

## 32. ★ 批量导入德育分（v1.9 新增）

`core/import_workers.py`（**零 Qt import**，pytest 可直接测）+
`ui/moral_import_dialog.py`（预览）+ `ui/pages/scholarship_page.py`（按钮与流程）。

* 三步：后台 `plan_import()`（**只读不写库**）→ 主线程预览 → 后台 `apply_plan()`；
  **取消 = 一个字节都不写**。
* 坏行**不中断整批**：`errors` 收全 → `export/import_errors_<batch_id>.csv`
  （utf-8-sig，无错不建文件）；成功行按 `sid:<学号>` 追加，批次账本写
  `moral_batches.json.enc` —— ★ **应用级载荷只收 `.enc`**，写成 `.json` 会被
  `save_encrypted` 当场拒绝。
* ★ 班级必须**已经建过**（`raw_class not in known` 就报错，一个班都没建过也要报）——
  否则德育分写进没有名单的班：数据进去了，界面上一个人也看不到。
* ★ `ImportPlan.failures` / `skipped` / `classes` 是 **property 不是方法**。
* 判据：`tests/test_import.py`（40 用例）+ `%TEMP%\tl_v19_ui_test.py`；探针
  **必须桩掉 `QMessageBox.information`**（模态弹窗会把 120 秒看门狗跑满）。


## 33. ★ 模型中心增强（v1.9 二期）

`core/ai/model_registry.py`（零 Qt / 零 HTTP）+ `ui/model_center_dialog.py`：

* **负载三档** `LOAD_EASY / LOAD_TIGHT / LOAD_HARD` =「轻松 / 勉强 / 困难」
  （配色 `LOAD_COLORS`），**唯一实现是 `_load_level_of()`**：内存 ≥ `minRamGb`
  且（无 `minVramGb` 或有独显且显存够）→ easy；否则 ≥ `minRamGb × 0.7`
  （`RAM_TIGHT_RATIO`）→ tight；否则 hard。探测最多一次 —— 不声明显存的条目不启
  `nvidia-smi`。
* ★ **内存探不到一律放行**（判困难会让探测失败的机器一条都勾不上）；「显存未知」≠
  「内存未知」，后者按 `RAM_TIGHT_RATIO` = 0.7 判。**不许把"不知道"写成"是 0"**。
* ★ **默认只勾 🟢**（easy ＋ `minRamGb` 过关），**不再看清单里的 `defaultCheck`**；
  困难行没有 `ItemIsEnabled`、下载按钮置灰 + 气泡说明。
* **卸载**只许用户主动点（确认框 → `delete_model()` + `forget_installed()`）；下载重试
  1 次仍失败 →「复制链接」兜底。**文案、`load_note()` 的写法与判据 → 总纲 §二十四。**

## 34. ★ AI 推理引擎（v1.9 二期）

* `core/ai/inference.py` **零 HTTP import，且 import 期绝不 import `llama_cpp`**
  （可选依赖藏在 `_import_engine()` 里）；★ **全局 AI 助手入口已删**（v1.9.1 摘掉顶栏按钮与
  `_open_chat_assistant`），`ui/chat_dialog.py` 的 `ChatDialog` **保留为内部组件**；档案页
  「AI 辅导」是它的**姊妹实现**（同一套 `ui/workers.py` 工作线程契约）。
* `llama-cpp-python` **不进 exe、不动 `build_exe.ps1`**（`requirements.txt` 里只写
  注释）。没引擎时 `engine_available()` 给中文原因、抛 `InferenceUnavailable` ——
  **明确降级，不许静默失败、不许偷偷走云端**。
* ★ **聊天历史不落明文**（红线 3）：存应用级 `chat_history.json.enc`；档案页「AI 辅导」
  按学生存 `u_<uid>\cache\chat_history_<学号>.json.enc`。
* 冻结接口（档案页要用）：`ui.chat_dialog.open_chat(parent, *, system_prompt=None,
  initial_prompt=None, prefer_model=None)`（**签名冻结，不许改**）。

## 35. ★ 档案合并与成绩覆盖（v1.9 二期）

* `core/database.py` 是**逻辑表 API**（表名 / 字段 / 删除语义照任务书），底层仍是
  `core/user_store` 应用级加密：`grade_history.json.enc` + `student_profiles.json.enc`
  —— **不上明文 sqlite**（红线 3）。
* 导入前冲突检测 → 弹窗「检测到学生 {姓名} 已有 {学期} 成绩，是否覆盖？」+
  覆盖 / 跳过 / 全部覆盖 / 全部跳过；结束弹「覆盖 N 条 / 新增 M 条 / 跳过 K 条」。
  覆盖 = 删旧写新；跳过 = 保留旧记录、该行不导入。
* 档案页两个标签页：「成绩明细」（按学期折叠、顺序取自 `SEMESTER_ORDER`、**空 ≠ 0**）与
  「总评与侧写」（只显示有真源的平均分/加权均分/GPA/排名）；「AI 侧写」按钮懒 import
  `open_chat()` 并 try/except。★ 不许挂 `QGraphicsEffect`。**完整 API 与判据 → 总纲
  §二十四。**
* ★ v1.9.1：档案页新增第三标签页「AI 辅导」（见 §34）；成绩/德育/侧写支持**单条删除**，确认
  文案「确定删除此条记录？此操作不可恢复。」，删掉某学期最后一条成绩时追问「该学期已无成绩，
  是否同时删除学期记录？」。**「删除成绩库」的判据 = `library_inventory().has_data`**（不是
  "目录存在"）+ 两次确认，删完发 `SettingsPage.library_cleared(int, int, int)` → 主窗刷新档案/
  侧写页（§18：只打日志、不弹窗）；**审计日志** `u_<uid>/audit/grade_changes.json.enc` 只记
  时间/操作/学生ID/学期/课程名，**不记分数**（见 §16 例外）。

## 36. ★ 推理后端一键安装（v1.9.2）

* `core/ai/runtime_setup.py`：**零 HTTP import**，pip 走子进程（Windows 必须
  `subprocess.CREATE_NO_WINDOW`，绝不弹黑框）。安装目标是**应用私有目录**
  `%LOCALAPPDATA%\SGMS\runtime\site`（`pip install --target`），启动时由
  `ensure_runtime_on_path()` 插进 `sys.path` —— 打包版 `sys.executable` 是 exe 自己、
  里面没有 pip，所以**必须借本机的系统 Python**；找不到就给「复制命令」，
  **绝不静默降级**（§13 仍生效：pip 是子进程，不是我们的 HTTP 库）。
* ★ **绝不静默安装**：只有用户点「一键安装」才跑；取消 = terminate + kill 兜底；
  失败把**完整命令**摆出来并给「复制命令」「重试」，异常不许冒到事件循环里。
* `ui/widgets/backend_setup.py` 的三态卡片是**唯一**入口，模型中心与「学期侧写」页摆卡片；档案页「AI 辅导」页**不重复摆**（那页控件已经很多），只贴中文原因 + 「打开模型中心」按钮；装成功后 `ready` 信号自动继续加载模型。
* llama-cpp-python **仍不进 exe**（`requirements.txt` 只有注释）。判据
  `tests/test_runtime_setup.py` + `--selfcheck` 那条"`core/ai/*.py` 零 HTTP import"。

## 37. ★ 成绩档案穿透导航与学生主档（v1.9.2）

* `students.json.enc` 是**学生主档**（学号/姓名/班级/备注/归档/来源），
  `StudentStore` 是唯一读写口；导入成绩时 `upsert_import()` 自动补档 ——
  **不许另立第二份名单**。列表默认不显示归档，搜索框输入「已归档」才看得到。
* 档案页是**两级视图**（`QStackedWidget`）：详情页的学期时间轴点一下**穿透**到
  「学期侧写」页（`ui/pages/semester_side_profile.py`，上下文只注入该学期），
  返回按钮回详情。导入页「侧写」按钮只发 `student_selected`，主窗切页后由
  `ProfilePage.ask_semester_and_open()` 让用户选学期。
* ★ **改学号必须全量迁移**：成绩历史、侧写（含学期侧写）、聊天历史文件、
  德育分键（`sid:`）、意愿标签 —— 少一样就是**静默丢数据**；走
  `core.database.migrate_student_id()`，失败只 warning 不中断。删除学生二选一：
  彻底删除（先清关联数据）或归档隐藏（数据全留）。
* ★ **两套学号写法必须归一**：主档/聊天文件是 `sid:<学号>`，**成绩历史是导入时
  按裸学号**落盘的。迁移一律按裸学号比对、写回跟随那一行原来的写法
  （`_bare_student_id()` / `ProfileStore._resolve_key()`）—— 不归一的话
  "改学号"一条成绩也迁不走，而且**不报错**（v1.9.2 实测踩到）。
* 学期均分是**算术平均**（界面标签必须写明），加权均分/GPA 一律复用
  `core/calculator` 与 `core/gpa`，**不许自创换算**；审计日志新增 `detail` 字段，
  **永不写分数**。完整 API 与判据 → 总纲 §二十六。


## 38. ★★ 启动期"静默失败"一律不许留（v1.9.3）

v1.9.2 之后连着报了四个现场：登录卡住、迁移报错、exe 起不来、界面上有杂线。
前三个的共同点是**出错时什么都没有**——`--windowed` 的 exe 没有 stdout/stderr，
异常从槽里冒出去只会在一个不存在的地方打一行 traceback。

### 38a. 关键步骤必须落日志

新增 `core/applog.py`（零依赖、不联网）：`write()` / `install_excepthook()` /
`install_faulthandler()` / `tail()`。日志位置 `<数据根>/logs/app.log`，超 512 KB 滚成 `.1`。

**规矩**：① `main()` 里要在**更新交接与迁移之前**装钩子并写一行 `boot`；
② 任何"用户点了一下但界面没反应"的路径，都必须至少有一行日志；
③ `applog.write()` **永远不许抛**（写不进去就 `pass`）——日志把程序搞崩就荒唐了。

### 38b. 备份只抄"不可再生"的东西

`core.migrator.SKIP_DIR_NAMES = {"models", "runtime", "logs", "cache"}` 不进备份：
现场 `models/` 1.1 GB，整目录 `copytree` 要几十秒，中途被占用就整次迁移失败。
这四个目录**都能重建**；`users.json` / `app.salt` / `u_*/` 一个字都不省。
改这一条就等于改"备份可信"的定义，**动之前先想清楚**。

### 38c. 密码通过之后的那一步，比密码本身更容易坏

`ui/login_window._do_login()` 里，密码通过之后的 `_handle_remember()` / `_leave()`
必须包在 `except Exception` 里：落 traceback + 改状态 + 弹窗（附日志路径）+ **解锁重试**。
外加 `LEAVE_FAILSAFE_MS = 1600` 看门狗，动画不回调就补发 `logged_in`（且只发一次）。
⚠️ 错密码那三条 `CryptoError` 分支的文案**一个字都不许改**——它们本来是好的。

### 38d. 主窗透明度卡在 0 = "程序没出来"

`ui/main_window` 构造时 `setWindowOpacity(0.0)`，靠 200ms 动画拉回 1。
现在 `FADE_IN_FAILSAFE_MS = 900` 兜底：到点还 < 0.99 就直接补 1 并写日志。
★ 任何"先设成不可见/透明、再靠动画出现"的地方，都要按这个模式加看门狗。

### 38e. 单文件 exe 的解包目录：`--runtime-tmpdir`

现场 `[PYI-…:ERROR] Could not create temporary directory!` 是 PyInstaller 启动器
在 Python 起来之前报的，应用层插不上手。实测：`%TEMP%` / 桌面 / 用户目录根全失败，
**解到 exe 自己所在目录就成功**（同一个 hello-world 包对照验证）。

`build_exe.ps1` 的 release / console 目标因此固定带 `--runtime-tmpdir`（默认 `"."`）。
★ **两个边界**：① 相对路径按**启动时的 CWD** 解析（双击时 = exe 目录）；
② 不支持环境变量，且上级目录必须**已存在**（指 `_tmp` 这种不存在的子目录会直接失败）。

### 38f. 界面"多余线条"这类问题，没有真机截图不要瞎改

离屏渲染（100% / 150% DPI）都拍过登录页与主窗六页：描边、圆角、阴影、分隔线都正常，
没能复现用户说的杂线。★ 规矩：① 先把能查的查干净（像素级、逐层拆）；
② **没有被证据指到的改动不要当成"修复"上报**；③ 直接请用户截一张图 —— 这比猜三轮快。
（已按 §4e 第 2 条统一了子控件圆角半径，但那只是"顺手对齐规矩"，不是修复。）

## 39. ★ 错误弹窗必须能一键复制（v1.9.3 补充）

现场反馈常常只有"报错了"三个字，没有 traceback、没有日志路径。`ui/error_report.py`：

* `install_error_copy(app, root)` 在 QApplication 上装事件过滤器：所有 **Critical 图标**
  的 QMessageBox 自动多一个「复制错误信息」按钮，点它把 **traceback + 日志路径 + 日志末尾**
  写进剪贴板。★ **不要改回 `box.addButton()`** —— QMessageBox 会把它当"确定"关窗；
  现在用普通 QPushButton 塞进 `QDialogButtonBox` 布局，点完窗口留着，复制失败也看得见。
* `install_crash_handler(root)` 包住 `sys.excepthook`：先让 `core.applog` 落日志，
  再在主线程弹带完整 traceback 的错误框。`main()` 里两行都必须有，且过滤器在钩子之前装。
* 判据：`tests/test_error_report.py`（9 条，离屏；点完不关窗、不碰真剪贴板）+
  `main._error_report_gate_report()`。日志/凭据仍遵守铁律 2，报告里不许出现密码。
