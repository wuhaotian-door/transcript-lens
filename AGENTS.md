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

1. IDE / 编辑器的全局替换
2. Agent 的 `edit` / `write` 工具
3. Python：`Path(p).read_text(encoding="utf-8")` → `write_text(..., encoding="utf-8")`

读文件同理：用 `read` 工具或 Python，**不要**用 `Get-Content`。

> 只读地看一眼（`Select-String`、`Get-ChildItem`、运行 `pytest`、`git status`）
> 不受这条限制 —— 受限的只有"读进来再写回去"这个往返动作。

## 1b. `.ps1` 里的非 ASCII 必须有 **UTF-8 BOM**

Windows PowerShell 5.1 读**没有 BOM** 的 `.ps1` 时，按**系统 ANSI（本机 GBK）** 解码。
中文按 GBK 解会错位，而错位的配对会把相邻的 ASCII 字节一起吃掉 ——
GBK 的尾字节范围是 `0x40~0xFE`，正好包含 `{` `}` `\`。
于是报出来的错是 `Unexpected token '}'` / `Missing closing '}'` 这种**完全看不出根因**的
解析错误，而文件在编辑器里看着好好的。

**教训（第二次踩同一个根因）**：`edit` / `write` 工具写出来的是**不带 BOM** 的 UTF-8。
用它改过 `build_exe.ps1` 之后，脚本立刻在 PS 5.1 下解析失败 —— 20 个解析错误。

所以：**改完任何 `.ps1` 之后，跑一遍补 BOM + 语法检查**：

```powershell
# 补 BOM（只加 3 个字节，正文不动）
$p = 'build_exe.ps1'
$b = [IO.File]::ReadAllBytes((Resolve-Path $p).Path)
if (-not ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)) {
    [IO.File]::WriteAllBytes((Resolve-Path $p).Path, ([byte[]](0xEF,0xBB,0xBF) + $b))
}

# 语法检查（不要靠"跑一下试试"，那要等几分钟）
$err = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $p).Path, [ref]$null, [ref]$err)
if ($err.Count) { $err | % { "L$($_.Extent.StartLineNumber): $($_.Message)" } } else { "OK" }
```

## 2. 红线（总纲 §三 铁律 1-10 + §十三 导入解析规则，违反即重写）

- 加权算术**全部本地完成**，绝不调用任何大模型 API 做数字
- **空 ≠ 0**：空 / `-` / `/` / `未选` = 没选这门课，学分不进分母；数字 `0` = 缺考，学分进分母
- 学分提取必须有人工回显确认
- 真实成绩文件、真实凭据（邮件授权码等）**一律不入库**。仓库里唯一允许存在的
  xlsx 是 `tests/fixtures/fake_*.xlsx`
- `config/` 下只允许 `.example.json`
- 不写注册表。**联网：v1.6 起改为有条件放行 —— 见第 13 节**（SMTP +
  用户主动启用云端 AI 允许；自动更新 / telemetry / 静默请求仍然禁止）

## 3. 落盘

- 明文成绩**任何时候都不落盘**；磁盘上只有 `.enc` 密文
- 用户数据在 `%LOCALAPPDATA%\SGMS\`，**不在仓库里**
- 任何"校验 / 解锁"动作都**绝不清空或改动已有成绩库**（第 30 节）

## 4. Qt / PySide6 界面：踩过的坑（4a–4m）

界面已从 tkinter 全量换成 **PySide6**（`ui/` 旧文件已删干净，没有 .bak）。
以下每一条都是**实测复现过**的，不是理论风险。
**4k 是其中最严重的一条，动窗口动画之前务必先读它。**

### 4a. 一个 widget 只能有一个 `graphicsEffect`

```python
# ⛔ 卡片挂了阴影，就不要再让它淡入淡出
card.setGraphicsEffect(QGraphicsDropShadowEffect(...))
fade_in(card)          # 会抛 TypeError（ui/anim.py 故意拦下来）
```

阴影（`QGraphicsDropShadowEffect`）和透明度（`QGraphicsOpacityEffect`）
抢的是同一个槽位。要淡入淡出的是**页面容器**，卡片挂在它下面。

### 4b. ★ `QGraphicsOpacityEffect` 会缓存源 pixmap —— 后果是整页空白

**这是本项目最阴的一个坑。** `ui/anim.py` 的 `fade_in` / `fade_out`
**必须在动画结束后把效果摘掉**（`clear_effect`）。不摘的话：

1. 切页动画给页面挂上 `QGraphicsOpacityEffect`，动画跑完效果留在那儿（opacity=1）
2. 页面**内部**增删控件（例如侧写页重算"可抢救项"时 `deleteLater()` 掉旧标签）
3. 效果缓存的 pixmap 不会失效 → **整页画成空白**
4. 切走再切回来（重新跑一次淡入）才恢复正常

实测复现：跳到侧写辅导页 → 点「考研」触发重建 → 整页变白。
**静态断言查不出来**（控件都在、文字都对、几何也对），只有截图看得见。

### 4c. 验证 GUI 不能只断言控件存在，要量像素

三次事故都是"断言全绿、界面上却是坏的"：

| 现象 | 真因 |
|---|---|
| 「忘记密码？」按钮看不见 | 登录窗写死 `820x500`，内容需要 639px |
| 表格「操作」列的按钮只剩半个字 | 行高 30px < 容器边距 12 + 按钮最小高 26 |
| 侧写页整页空白 | 透明度效果缓存未失效（见 4b） |

**规矩**：

1. 需要换行的 label 必须给宽度约束，否则会把窗口撑宽（切页时窗口横向跳动）。
2. `QHeaderView.ResizeToContents` **不把 `setCellWidget` 放进去的控件算进来** ——
   操作列必须写死宽度（`setColumnWidth` + `Fixed`）。
3. 行高要 ≥ 单元格内控件的 `minimumSizeHint().height()` + 容器上下边距。
4. 每个"看起来对"的界面都要**截图看一眼**。见 `%TEMP%\tl_qt_drive.py`。

### 4d. 打包：`--exclude-module` 里绝不能有 PySide6

`build_exe.ps1` 里曾有一条 `--exclude-module PySide6`（tkinter 时代的遗留）。
换成 PySide6 之后忘了删的话，PyInstaller 会把整个 Qt 排除，
**打出来的 exe 一启动就 ModuleNotFoundError，而源码自检照样全绿**
（自检跑的是源码，不是包）。

另外：`ui/styles.qss` 是**数据文件**，必须 `--add-data` 进包，
否则 exe 里窗口能开、界面全白。

### 4e. 无边框窗口（`ui/frameless.py`）的四条硬约束

两个顶层窗口（登录窗 / 主窗）都走 `FramelessWindowHint` +
`WA_TranslucentBackground` 自绘。系统标题栏没了，所以下面四件事必须自己扛：

1. **窗口自己必须显式透明。** `styles.qss` 里那条 `QMainWindow { background }`
   会把整个窗口矩形填成不透明 —— 包括留给阴影的那圈边距，于是四个圆角外面
   变成灰白方块，"无边框圆角"根本看不出来。必须补一条
   `QMainWindow#FramelessWindow { background: transparent; }`。
2. **★ 圆角不裁剪子控件。** QSS 的 `border-radius` 只管控件自己画的背景，
   子控件照样画方的。所以贴着窗口边的那几块（侧栏、顶栏、标题栏、品牌区）
   **各自要带对应角的圆角**，最大化时还要能一起取消。
3. **不要手算拖拽/缩放的像素。** `QWindow.startSystemMove()` /
   `startSystemResize()` 把动作交还系统：多显示器 DPI、Aero Snap、
   Win+方向键全都自动正确。手算 `move()` 这些全都没有。
4. **最大化时要把阴影边距收成 0、圆角关掉。** 不收的话，四周那圈透明边
   会在屏幕边缘留一道缝，圆角还会和屏幕直角打架。

### 4f. ★ `QWidget` 自带只读的 `maximized` 属性 —— `setProperty` 会**静默失败**

```python
widget.setProperty("maximized", "true")   # 返回 False，不抛异常，什么都没发生
```

`QWidget` 的 `Q_PROPERTY(bool maximized READ isMaximized)` 是**只读**的
（`QWidget.metaObject()` 里 `isWritable()` 是 `False`）。往只读属性上
`setProperty` 不会报错，只会返回 `False` —— 于是 QSS 里的
`[maximized="true"]` 选择器**永远匹配不上**：样式没生效，日志干干净净。

**规矩**：动态属性名一律加前缀（本项目用 `tlMaximized`，见
`ui/frameless.py` 的 `MAXIMIZED_PROP`），并且 `setProperty` 之后
**读回来确认**，不要信返回值（实测动态属性也可能返回假值）。
回归自检：`property_name_is_safe()` + `%TEMP%\tl_qt_drive.py`。

### 4g. ★ `Qt.Edge` 是 `enum.Flag`，`int()` 会抛 `TypeError`

```python
return edges if int(edges) else None      # ⛔ TypeError
return edges if edges else None           # ✓ Flag 有定义好的 __bool__
```

PySide6 的 `Qt.Edge` 继承 `Flag` 而不是 `IntFlag`，`int()` 直接抛。
**这个分支鼠标每动一下都会走到** —— 于是异常从 `eventFilter` 里冒出来，
Qt 只打一行 `Error calling Python override of QObject::eventFilter()` 就吞掉：
界面上是"边框光标永远不变"，日志里是看不懂的 TypeError。
需要数值时用 `edges.value`。

### 4h. 验证 GUI 必须**真的动鼠标**，不能只调函数

上面 4g 那个 bug 躲过了源码自检、躲过了截图、躲过了 76 项冒烟
（那些都不产生鼠标事件），**只在自动化里鼠标恰好停在窗口上时才炸**。
现在 `tl_qt_drive.py` 里有一步 `QTest.mouseMove()` 扫过四边四角，
专门逼 `eventFilter` 跑一遍。

**规矩**：凡是挂在 `eventFilter` / `mouseMoveEvent` / `hoverEvent` 上的逻辑，
测试必须用 `QTest.mouseMove()` 真的走一遍，不能只直接调背后的纯函数。

### 4i. `QRadioButton` 互斥组里，已选中的那个**取消不掉**

```python
# ⛔ 六个单选里当前亮着"考研"，切到"还没选意愿的学生"时：
for option, radio in buttons.items():
    radio.setChecked(option == "")        # "考研" 依然是亮的
```

互斥组（`QButtonGroup.setExclusive(True)`）不允许把当前选中项直接
`setChecked(False)`。界面上就是：从"有志愿的学生"切到"没选的学生"，
状态栏写着"还没有选意愿"、按钮却亮着上一个学生的志愿 —— 看着像串号，
其实是单选组的行为。**换人回填必须先 `setExclusive(False)`，填完再打开。**

### 4j. `QGraphicsDropShadowEffect` 挂整窗的代价：约 40ms/次切页

实测（1900×1200，150% DPI）：阴影开中位 523ms、关 483ms（**+8%**）。
Qt 会缓存效果的源 pixmap，页面淡入淡出**不会**每帧重新模糊整窗（否则 +1200ms），
所以这个代价可以接受 —— 但**别在动画期间额外触发整窗 `update()`**。

### 4k. ★★★ 窗口动画的 `finished` 回调里**不许销毁那个窗口** —— 会静默炸进程

**这是本项目至今最严重、最会骗人的一个 bug。** 症状：

> 双击 exe → 开屏 → 打招呼 → **然后就没了**。界面不出来，
> 点任务栏也没反应。**没有任何报错**（`--windowed` 的 exe 连
> stdout/stderr 都没有），退出码是 `0xC0000409`（STATUS_STACK_BUFFER_OVERRUN）。

根因是一条两步的 use-after-free：

1. 动画的 parent 就是它作用的那个窗口
   （`QPropertyAnimation(window, b"windowOpacity", window)`）；
2. 回调里把窗口的最后一个 Python 引用丢掉
   （`AppFlow.welcome = None` / `AppFlow.login = None`）→ 窗口当场析构
   → parent 在它上面的动画**跟着一起没**；
3. 可 Qt 发完 `finished` 之后
   **`QAbstractAnimationPrivate::setState` 还要继续用那个动画对象**
   （注销统一计时器、处理 deleteWhenStopped）→ 踩空 → 进程直接消失。

**光把清理槽排到前面是不够的**（试过，照样崩）。正确的写法是
**让回调延后一拍**，等这一轮事件循环走完、动画彻底收工：

```python
# ⛔ 同步回调：on_done 里一销毁窗口就踩空
anim.finished.connect(lambda: on_done())

# ✓ 延后一拍。用 singleShot 的 context 重载 ——
#   窗口先一步没了的话，这次回调会被自动跳过，不会去碰死对象
anim.finished.connect(lambda: QTimer.singleShot(0, window, on_done))
```

同样地，`ui/anim.py` 的 `_drop` 必须是 `finished` 上的**第一个**槽，
并且用 `shiboken6.isValid()` 判活之后再碰动画对象（见 `is_alive`）。

**为什么 400 多项测试一条都没抓到？** 因为那条路径**从来没被跑过** ——
所有测试为省 1.5 秒一律 `skip_to_end()`，而这 bug 只在"欢迎页**真的**淡出"
时才成立。所以现有两道闸门**都别再拆掉**：`%TEMP%\tl_boot_test.py`
（`auto` / `login` / `switch` 三个真实时序，每个**单独进程**，退出码即断言）
和 `transcript-lens.exe --bootcheck`（打包产物里的真启动流程写进
`%TEMP%\transcript-lens-bootcheck.txt`）。`build_exe.ps1` 打完包**两个都跑**，
非 0 直接判失败。

**规矩**：任何"动画结束后销毁触发它的那个对象"的代码，回调必须延后一拍；
并且**必须有测试真的把动画跑完**，不许 skip。

### 4l. ★★ 启动慢的两个真凶：`QSplashScreen` 和标题里的 emoji

量完发现**跟动画时长、跟机器性能都无关**，是两笔一次性开销
（`%TEMP%\tl_splash_probe.py` / `tl_perf_bench.py` 实测，
对照组 `sleep(0.15)` 量到 150.3ms，校准正常）：

* **`QSplashScreen` 本身比 `QWidget` 慢 865ms**（`QWidget` 164~167ms vs
  `QSplashScreen` 1028~1035ms；每个变体**单独起进程**测，排除"第一次创建原生窗口"
  这笔一次性成本）。跟半透明 / 无边框 / 透明度**全都无关** ——
  那 865ms 用户盯着全黑屏幕。`ui/splash.py` 已改成自绘 `QWidget`。
* **开屏标题里的 📊 一个字 241.7ms**（去掉 emoji 只要 0.2ms）：emoji 要现场加载
  Segoe UI Emoji 做彩色字形合成，是**启动链上最贵的单个字符**。已删。
  > 只删开屏 pixmap 里的；侧栏 / 页面标题的 emoji 在**已映射好的窗口**里，不占启动时间。
* 改完还剩 **765ms 全黑**，`tl_splash_trace.py` 打点定位到：开屏**先发 finished
  再淡出**。现在**先叫下一个窗口、再淡出自己**（`ui/splash.py._start_fade_out`），
  这段就落在开屏背后。

**预热的三条规矩**（`AppFlow._prewarm_main` 把主窗提前建好并 `show()`，
`MainWindow.__init__` 里已经 `setWindowOpacity(0.0)`，映射出来全透明、看不见）：

★ **不能在 `run()` 之后紧接着预热** —— `run()` 只是把淡入动画 start 出去，那一刻
窗口还一帧都没画；跑 600ms 同步工作会把**淡入动画冻在第一帧**，用户看到"点了没反应"。
所以挂在 `on_shown` 上。
★ 停留时间要**扣掉**预热耗时（`HOLD_MS - spent`）。第一版"干完活再计时"，
欢迎页实际停留 `HOLD_MS + 600ms` —— 白等 600ms。
★ `HOLD_MS` = **1500**（总纲 §10.2 写死的）。因为扣时间，这 1500 里含主窗构造
那 ~600ms，**不是** 1500 + 600。

### 4m. 切页卡顿：淡出那一段是纯粹的等待

实测一次切页的耗时 vs 两段动画时长（真跑 `StackSwitcher`，取中位数）：

| 淡出 + 淡入 | 180+240（原） | 120+180 | 90+150 | 0+150 | **0+130（现在）** |
|---|---|---|---|---|---|
| 实测切页 | **439 ms** | 326 ms | 260 ms | 166 ms | **~137 ms** |

`180 → 0` 一项就省掉近 300ms，是整条链上最划算的一刀。用户感知的界线大致是
"100ms 以内算瞬时、200ms 以上能感到迟滞"。
**砍淡出是对的**：那一段旧页在变淡、新页还没出现，用户盯着一个越来越空的屏幕，
它不传递任何信息。要回淡出就把 `ui/anim.py` 的 `FADE_OUT_MS` 设回 180，
`switch()` 会自动走回淡出分支，**两条路径都有测试覆盖**。

## 5. 密码找回：已接通邮箱（网易 163 SMTP）

- **恢复码 = 唯一的第二把钥匙。** 注册成功时生成一串 24 位找回码，
  本地存一份"用它包起来的库密钥"（`u_<uid>/recovery.json.enc`，应用级密文），
  同时把码寄到用户邮箱。忘密码时凭它解出库密钥、设新密码，**成绩一条不丢**。
- ★ **为什么不能"发个新验证码就重置"**：库密钥只在本地、只被密码包裹。
  忘密码那天它已经解不开了，"现在发一封邮件"发不出任何有用的东西 ——
  能解开它的只有**注册时**生成、并已用它包好密钥的那一串码。
  **这个时序是命门，任何"优化"都不能改它。**
- 注册**必须填邮箱并验证**（6 位验证码 / 10 分钟 / 最多试 5 次 / 60 秒冷却）。
- 个人中心可以「重新生成找回码」——要再输一次密码（生成需要库密钥），
  顺带挡住"别人趁你没锁屏偷偷把找回码换成他的邮箱"。

### 5a. ★ 邮件授权码绝不许进仓库

真实凭据在 `%LOCALAPPDATA%\SGMS\email.json`（**仓库外**）。
仓库里只有 `config/email.example.json` 模板，字段全空。

`main.py --selfcheck` 里有一条**反扫**：从 AppData 读出密钥，
再拿它扫全仓库源码，命中就报 FAIL。

> ⚠️ 写这条检查时**不要把密钥写进 main.py** —— 那正好犯了要防的错。
> 反过来做：密钥从配置文件读，代码里永远只有"比较"这个动作。
> 见 `_credential_leak_report()`。

### 5b. 网络请求必须走后台线程

`ui/workers.py` 的 `run_async`。在按钮回调里直接 `smtplib.sendmail()` 的话：
顺利时卡 1 秒，**网络不通时卡满 15 秒超时**（`SMTP_TIMEOUT`），
期间窗口不重绘、Windows 可能弹"无响应"。
回调在主线程执行，所以回调里可以安全碰控件。

## 6. 命令行开关：`in` 是精确匹配，不是前缀匹配

```python
# ⛔ 踩过的写法
if "--mailcheck" in args:
    return run_mailcheck(send_test="--mailcheck-send" in args)
```

`args = ["--mailcheck-send"]` 时，`"--mailcheck" in args` 是 **False**
（列表的 `in` 比的是整个元素）。于是 `--mailcheck-send` 匹配不上任何分支，
**掉进最后的 GUI 分支把窗口开起来** —— 命令行下表现是
「卡死、一个字都不输出、退出码永远拿不到」，而且源码和打包版**一样卡**，
极难往参数解析上想。

**规矩**：长的开关先判；不确定就用 `any(a.startswith("--mailcheck") for a in args)`。
回归测试见 `%TEMP%\tl_cli_test.py`（含静态检查：断言分支顺序）。

## 7. 诊断命令必须逐行即时输出

`run_mailcheck` 原先攒到最后一次性写报告，结果卡住时**什么都没有** ——
一个不输出的诊断等于没有诊断。改成 `emit()` 每步立刻落盘 + `flush`，
一眼就能看出停在哪一步（实测就是靠它定位到"根本没进 mailcheck 分支"）。

## 8. 验证打包后的 exe 之后，先把它彻底关掉再重打

```
PermissionError: [WinError 5] 拒绝访问: '...\dist\transcript-lens.exe'
```

onefile 的 exe 启动时会**再起一个子进程**（bootloader + 真正的应用）。
只 `Stop-Process` 父进程的话，子进程还活着、exe 文件还锁着，下一次打包就炸。

```powershell
Get-Process -Name 'transcript-lens*' | Stop-Process -Force
Start-Sleep -Seconds 3      # 句柄释放有延迟，马上重试可能还是被占用
```

按进程名杀（而不是按 PID）才杀得干净。
`build_exe.ps1` 的自检也会调 `--selfcheck` 跑这个 exe，自己不会锁文件 ——
**锁文件的只可能是手动启动的那一份**。

> 另外：**新建出来的 exe 第一次启动会被 Defender 全盘扫描**，
> 冷启动可能比之后慢好几倍。别把第一次启动的耗时当成性能基准。

## 9. ★ 界面上"这个学生是谁"一律用**学号**，不许用姓名

同名学生历届都有。凡是拿姓名当键的地方，都会**串号**，而且不报错：

| 位置 | 症状 |
|---|---|
| `ui/intentions.py` 的意愿字典 | 两个张三互相覆盖志愿 |
| `ui/flags.build_flags()` 的 `{姓名: flag}` | 两个张三只留得下一个 flag |
| 表格按姓名查结果 | 两行显示同一份数字 |

**规矩**：

1. 身份一律走 `ui.intentions.student_key(student_id, student)`：
   有学号 → `sid:<学号>`；没有才退回 `name:<姓名>`（**并且界面要明说同名风险**）。
2. 逐行显示的预警用 `ui.flags.build_flags_by_index()`（**按行序返回列表**），
   不要用 `build_flags()` 的姓名字典。
3. 表格的列下标一律从 `COLUMN_TITLES` 推导（`NAME_COLUMN` / `ID_COLUMN` /
   `FIRST_SCOPE_COLUMN` / `FLAG_COLUMN` / `ACTION_COLUMN`），**禁止手写元组** ——
   上次手写表头漏了一个口径列，整体错位一格，「侧写」按钮正好压在
   「红标原因」的单元格上。
4. 老数据（裸姓名键）在 `load_intentions` 里自动补 `name:` 前缀；
   侧写页还会在**同名唯一**时把 `name:张三` 迁到 `sid:...`（不唯一就不猜）。

## 10. 智育分析页的计算口径筛选

三个 Toggle：`必修` / `必修+限选` / `全部` —— 正好是 `CalculationScope` 的三档，
**不新增口径、不改口径定义**（真源：总纲 **§1.2** 三口径表 ——
① 必修+限选+选修 ② 必修+限选 ③ 仅必修。默认必修+限选计入，**选修默认剔除**）。

> ⚠️ 旧规格（v1.3 Spec + v1.6 / v1.7 / v1.8 三份 charter）**已全部合并删除**，
> 现在**只认 `docs/DEV_SUMMARY.md`（开发总纲）**。旧的「61 / 246 行」、
> 「5.2 / 第 3 章 / 第 4 章」这些编号**一律不再引用**；要查旧编号对应现在
> 哪一节，看总纲**第二十节**那张对照表。

- 切筛选只做三件事：`showColumn` / `hideColumn` 对齐当前口径列；
  红标**跟着当前口径重算**（分母变了，挂科/缺考的判定基础就变了）；
  把重算后的名单再推给侧写页一次（它名单上的红点也按口径判）。
- 三个口径列**始终都在**，筛选只改可见性。**不要**去增删列 ——
  那会让"表头 ↔ 单元格"重新有机会漂移，而那正是上一版的 bug。

### 10a. 表头三档**共用一句「加权均分」**

同一时刻只有一档列可见（另外两档 `hideColumn`），表头再写"口径①/②/③"
就是重复信息。三条信息各归各位：

| 位置 | 职责 | 真源 |
|---|---|---|
| **表头** | 这一列是什么数据（恒定） | `SCOPE_HEADER` |
| **筛选按钮 + 状态行** | 现在算的是哪一档 | `FILTER_LABELS` |
| **表下方明细行** | 三档各是多少，逐档写全名 | `SCOPE_COLUMNS` 第二项 |

★ 所以 `COLUMN_TITLES` 里三档口径列是**同一个字符串**，
**测试不能再靠表头文字区分是哪一档** —— 要按下标判
（`FIRST_SCOPE_COLUMN + SCOPE_COLUMNS 里的档位`）。
`%TEMP%\tl_qt_drive.py` 和 `tl_smoke_new.py` 都已经改成按下标判，
并额外加了一条"三档表头必须完全相同"的断言。

★ 证据链一点没丢：明细行仍然把三档**同时**列出来、各带全名和
`Σ学分 / Σ成绩×学分`。

### 10b. ★ 为什么是**互斥三档**，而不是三个可自由勾选的类别

旧 charter 6.4 当年写的是（现在记在总纲 §1.3）"三个独立 Toggle：必修 / 限选 / 选修，可多选组合"。
**按字面做成任意组合是做不到的**，理由是硬的：

* 能算出什么数由 `CalculationScope` 决定，而它在 `core/calculator.py` 里、
  **铁律一冻结**，只有三个成员（①全部 ②必修+限选 ③仅必修）。
  `{限选}`、`{选修}`、`{限选,选修}` 这些组合**没有对应的口径**。
* 要支持它们，只有两条路：改 `core/calculator.py`（违反铁律一），
  或者在界面里自己乘一遍加权（违反铁律五"不发明未定义规则"＋
  "本页只做格式化，不做任何二次算术"）。

所以界面保留**互斥三档**，但把"这一档到底算了哪几类"写在明面上，
让总纲 §一 那条纠正**在界面上看得见**：

| 位置 | 内容 | 真源 |
|---|---|---|
| 按钮气泡 | `计入：必修＋限选（选修不计入）` | `scope_breakdown()` 读 `CalculationScope.categories` |
| 状态行 | `当前口径「必修+限选」（必修＋限选（选修不计入））` | 同上 |
| 表下常驻说明 | `CALC_SCOPE_NOTE` | 常量 |

★ 上一版文档把"选修"写成"归限选"却没人发现，根因就是**按钮上只写"全部"**，
没人知道"全部"含不含选修。现在任何一个位置都能一眼看出来。
行为门见 `main._category_policy_report()`（第 19 节）。

## 11. 切页锁的看门狗必须**把页面落到位**，不是只放锁

```python
# ⛔ 只放锁：动画还在跑，用户再点侧栏就是两段动画抢同一个 opacity；
#    而且界面会停在"侧栏亮着 A、内容还是 B"且永不自愈
self._busy = False

# ✓ 把 index 落到位 + 摘掉残留的透明度效果 + 再放锁
self._stack.setCurrentIndex(self._pending); clear_effect(...); self._land(...)
```

阈值 `LOCK_TIMEOUT_MS` = **1500ms**（总纲 §10.3 指定；原来因为一次 1513ms 的
抖动被调到 2500，但那是**旧的 420ms 动画 + 首帧绘制重页面**造成的。
现在一次切页中位 ~137ms，1500ms 是 10 倍余量，够宽了）。

**但它仍然不是"卡住多久算卡住"** —— 它的职责是"绝不能把一个本来会
正常结束的动画砍掉"。哪天动画又变长了，先重量再决定要不要放宽，
**不要**因为"感觉不够快"就往下调。
回归测试见 `%TEMP%\tl_qt_drive.py` 的"看门狗落位"那一段。

## 12. 性能基线（改完要重量，别凭感觉）

两处都是用户明确反馈过的卡顿。数字全部来自实测，脚本留在 `%TEMP%`。

| 指标 | 改之前 | 现在 | 怎么量 |
|---|---|---|---|
| 切页（中位） | 439 ~ 500 ms | **~137 ms** | `%TEMP%\tl_perf_bench.py` |
| 启动到主窗可见（源码） | 6681 ms | **3055 ms** | `%TEMP%\tl_startup_time.py` |
| 开屏 `show()` | 1030 ms | **165 ms** | `%TEMP%\tl_splash_probe.py` |
| 开屏 pixmap 绘制 | 242 ms | **~1 ms** | `%TEMP%\tl_perf_bench.py` |
| 欢迎页停留 | 1500 ms | **1500 ms（总纲 §10.2 定死）** | `ui/welcome.py` 的 `HOLD_MS` |

> ⚠️ 欢迎页那 1500ms **不要**再"优化"掉。它一度是 900ms，
> 但总纲 §10.2 明确写了 1.5s，文档优先。要动先问，不要凭手感调。

**量性能的三条规矩**（都是这轮踩出来的）：

1. **必须带对照组。** 每次先跑 `time.sleep(0.2)` 确认量到的是 200ms 左右。
   这轮有过一次所有数字都是 0.0ms 的假测量 —— 差点据此改了错误的代码。
2. **一次性的成本要单独起进程测。** "第一次创建原生窗口"约 165ms，
   在同一个进程里连续测会把账算到第二个变体头上（`tl_splash_probe.py`
   就是为这个写成 `argv[1]` 选变体的）。
3. **别用写死的 `pump(1600)` 去等状态。** 这轮把链上每个时长都改了，
   所有写死等待的断言同时变成假绿或假红（`tl_boot_test.py` 就是这么坏的）。
   一律用轮询 `wait_until(pred, timeout)`；确需断言"某件事发生过"时用**锁存器**
   （spy/flag），不要靠撞时机。

## 13. ★ 联网红线（**有条件放行**）

真源：`docs/DEV_SUMMARY.md` **第四节 联网红线**（即旧 v1.8 charter 第四节）。

### ✅ 允许（**只有这四件**）

| 场景 | 条件 | 实现 |
|---|---|---|
| SMTP 发邮件 | 仅找回密码流程，用户主动点「发送验证码」触发 | `core/email_recovery.py` |
| 辅导员手动启用云端大模型 | 设置页手动开 + 手动填 API Key，**只在点「深度评估」时**调用 | `core/ai/`（骨架） |
| **更新检查** | 用户点「检查更新」→ `urllib.request` 只读一次 Gitee 版本号 | `core/updater.py` |
| **更新下载 / AI 模型下载** | 用户点「开始下载」并勾选文件 → `urllib.request` 流式下载 | `core/updater.download_exe` |

★ **v1.8 把"更新下载"加进了白名单，但 ❌ 那一条没变**：

```
❌ 自动更新/静默下载 · telemetry · 埋点 · 崩溃上报 ·
   任何形式的「输码解锁 / 联网校验用户资格」 · requests/httpx 用于上述场景之外
```

所以"允许下载"的准确含义是：**每一次下载前面都必须有一个用户点过确认的弹窗**。
代码里不许有"启动时顺便检查/下载一下"的路径 —— 判据之一是
`_updater_policy_lines()` 里那条"import 期零网络"
（把 `urlopen` 换成炸弹再重新 import 一遍模块，见第 21 节）。

### ❌ 禁止

自动更新检查 · 静默下载 · telemetry / 埋点 / 崩溃上报 ·
任何形式的「输码解锁 / 联网校验用户资格」 ·
拿 `requests` / `httpx` 干**非** SMTP、**非**用户主动下载/AI 的事。

### selfcheck 怎么守（`main._network_policy_report`）

★ **守卫要落在行为上，不能落在"这个包在不在"上。** 原来那条
"打包里见到 requests 就 FAIL"在改成"有条件放行"之后就错了，而且它只能查打包版，
源码版什么都查不到。现在五条：

1. **静态扫 `core/ai/*.py`**，源码里出现任何
   `import requests/httpx/urllib.request/socket/http.client/openai/anthropic`
   就 FAIL（正则只认真正的 import 语句，不认注释里提到的名字）；
2. **拿脏数据调一次 `redact_student`**，看姓名/学号/身份证会不会漏出去；
3. 非白名单联网库**仍然**不该在包里 —— 但报错文案改成
   "AI/更新都还是骨架，是不是有人偷偷加了依赖"，不再是"违反铁律"；
4. **更新检查**三条（`_updater_policy_lines`，见第 21 节）；
5. **更新下载**三条（`_download_policy_lines`，见第 26 节）。

★ 第 1 条**不需要为了 v1.8 放宽** —— `core/ai/model_downloader.py`
刻意一行 `urllib` 都没有，字节流复用 `core.updater.download_exe`。
所以"`core/ai/` 零 HTTP import"这条闸门照旧全量生效，扫描范围一个字没动。

## 14. 奖学金测算（总纲 §九，**规则已定死**）

    final = 智育加权(口径① 全口径) × 0.7 + 德育分 × 0.3

* 班级内按 final 降序；前 X 名一等、接下来 Y 名二等、再接下来 Z 名三等
  （X/Y/Z **每班独立配置**，存 `class_{班级号}_quota.json.enc`）；
* **特等奖**：final 进入前 X 名 **且** 每门科目原始成绩 ≥ 90。
  **不占名额**（拿特等的人从名额池里移出去、后面的人往前补）；
  **默认不叠加**，勾了"允许叠加"就显示成 `特等+一等`（统计时**两边都数**）。
* 输出列：`排名 | 姓名 | 学号 | 智育分 | 德育分 | 最终成绩 | 奖项 | 备注`；
  着色 一等金 `#FEF3C7` / 二等银 `#E5E7EB` / 三等铜 `#FFEDD5` / 特等紫 `#F3E8FF`。

★ **德育分没录 ≠ 0 分**（红线 2 的同一条道理）。没录的人按 0 参与测算，
但**必须在备注里写明**"德育分未录入，按 0 计" —— 不写的话辅导员会以为
这个人真的德育 0 分。表格里没录的格子是**空白**，不是 `0`。

★ **`core/scholarship.py` 里一行加权算术都没有。** 智育分是导入时
`core.calculator` 用口径①算好、存进 `class_{班级号}.json.enc` 的；
这里只做"乘 0.7 / 加 德育×0.3 / 排序 / 截名额"。那几步是**奖项规则**的算术，
总纲 §9.1 明确给了公式，不算"自己发明算术"。

★ 特等奖的"每门 ≥90"只检查**这个学生真考了的课**（`score is not None`）：
没选的课不参与判定，**不会**被当成 0 分拖累他；缺考（数字 0）**算考了**，
0 < 90，所以拿不到特等 —— 这是对的。

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

★ 为什么辅助数据走应用级：**自动登录时没有密码**（只有 session token），
库密钥解不开。意愿、德育分这类"辅导员自己录进去的东西"如果按密码级加密，
自动登录进来就全是空的。

★ **成绩绝不许往应用级里塞。** 这是这条分工的底线。

## 17. ★★ 切页抖动的正解：不要给页面挂透明度效果

**症状**：点侧栏切页，落定之后界面**略微抖一下**。

**根因（实测钉死）**：原来给新页挂 `QGraphicsOpacityEffect` 做 0→1 淡入。
一旦挂了效果，Qt 就把整页渲染进**一张离屏 pixmap**，而那条路径上
**文字的光栅化方式不一样**（没有 ClearType，字形次像素取整也不同）。
动画跑完、效果一摘，整页**重新光栅化一遍**，所有字都换了个画法。

量法（`%TEMP%\tl_clean_probe.py`，带对照组校准 —— 把名单换成 1 个人时
差异必须明显出现，证明方法有灵敏度）：

| 对比 | 结果 |
|---|---|
| 挂着效果 @1.0 vs 摘掉效果 | **17.4% 的像素不同**，最大通道差 **240** |
| 同一坐标 | 一个版本是深色字 `#0f173b`，另一个是纯白 `#ffffff` |

**修法**：页面**永远不挂效果**。要淡入的改成盖在上面的一层**纯色幕布**淡出
（`ui/anim.py` 的 `_PageCover` + `StackSwitcher._after_out`）：

1. 先 `setCurrentIndex` —— 新页立刻走**正常渲染路径**画出来；
2. 再盖一块 `#F1F5F9` 的纯色幕布；
3. 幕布淡出 → 新页从背景里浮出来，视觉上和"淡入"一模一样。

幕布是一整块纯色，离屏渲染和直接渲染**逐像素一致**，摘效果时没有任何可看出来的
变化。判据（`%TEMP%\tl_cover_verify.py`）：**落定后再等 1 秒，两帧必须逐像素相同**
（改之前这里差 17.4%）。

★ 顺带修掉的一个真竞态：原来 `setCurrentIndex` 之后才 `set_opacity(new, 0.0)`，
实测中间有 **6.27ms** 的窗口是新页已 current 但**没有效果** ——
任何一次落在那个窗口里的 paint 都会画出全不透明的一帧。现在是先挂 0 再换页。

## 18. 加一个页面前先想清楚"数据从哪来"

v1.6 新增的两个页面（成长档案、奖学金测算）都遵守同一条：

* **不重新解析 Excel、不重新算加权** —— 导入时算一遍，把结果存进加密库
  （`class_{班级号}.json.enc` / `archive.json.enc`），页面只负责显示；
* **口径固定**：档案页固定用「必修+限选」，**不跟智育分析页的筛选按钮联动**
  —— 跟着屏幕上的临时视图变，会出现"上学期看是 84、这学期看是 86"。

★ 页面之间的数据流一律走**一处真源 + 信号刷新**：
分析页导入完 → `class_saved` 信号 → 主窗 `_on_class_saved` 通知另外两页
`reload_classes()` / `reload()`。刷新失败**只打日志、不弹窗**
（数据已经存进去了，某一页没刷新出来是显示问题，不该打断用户看成绩）。

## 19. ★★ 必修 / 限选 / 选修是**三个独立类别**（总纲 §一 + §三 铁律 8）

总纲 §1.1 收录的原话：**"必修、限选、选修是三个完全独立的类别，绝不混同"**、
**"任何将选修归入限选的逻辑都是 bug"**。十节速查表里也单列了这一条。

真源只有两处，别在别处再判断一遍：

| 真源 | 内容 |
|---|---|
| `core.parser.Category` | `REQUIRED="必修"` / `LIMITED="限选"` / `ELECTIVE="选修"`（+ `UNKNOWN`） |
| `core.calculator.CalculationScope.categories` | 每档口径允许计入的类别集合 |

三档口径（总纲 §1.2）：

| 口径 | 类别 | 枚举值 |
|---|---|---|
| ① 总加权 | 必修＋限选＋选修（＋未标类别） | `CalculationScope.ALL` |
| ② 仅必修+限选 | 必修＋限选 | `CalculationScope.REQUIRED_LIMITED` |
| ③ 仅必修 | 必修 | `CalculationScope.REQUIRED_ONLY` |

★ **行为门**：`main._category_policy_report()`（`--selfcheck` 里跑）。它不只数
枚举值，而是**真算一遍**三门各属一类的样例：

    高等数学(4学分)必修 = 80 ｜ 专业英语(2学分)限选 = 90 ｜ 音乐鉴赏(1学分)选修 = 100
    口径① 600/7 = 85.714286    口径② 500/6 = 83.333333    口径③ 320/4 = 80

**只要有人把选修当成限选算进②，② 立刻变成 85.714286 == ①，检查当场 FAIL。**
（已实测：把 `CalculationScope.REQUIRED_LIMITED.categories` 临时改成含选修，
三条 FAIL 同时报出来 —— 见 `%TEMP%\tl_category_gate_test.py`。）

★ 界面上的对应物：`ui/pages/analysis_page.py` 的 `scope_breakdown()` 把每档
"算了哪几类"翻成人话，挂在按钮气泡 / 状态行 / 表下说明上（见 10b）。

★ ⚠️ **一处已知的历史遗留，别当成 bug 顺手改**：冻结的 `core/parser.py` 里
`CATEGORY_KEYWORDS` 把 **"专业选修"** 归到 **限选**（关键字命中数打平时按
`必修 > 限选 > 选修` 取先出现的）。那是个**校名别名**（多数培养方案里
"专业选修课"就是限选课），而且 parser 铁律一封板，**一个字符都不许动**。
真要改，是 Owner 的事，不是 Agent 的事。

## 20. ★ 学期标签：导入必须选学期，档案必须**累加**

总纲 §10.2 的欢迎页 / §10.4 的成长档案页都依赖它。

### 20a. 导入流程

* 分析页顶部有 **学期下拉框**（`大一上 → 大四下`，8 个）。
* 选项来自 **`SEMESTER_ORDER[:TOTAL_TERMS]`**，不另抄一份表 ——
  档案排序（`core.archival.semester_rank`）用的就是这张表，抄第二份会漂移，
  症状是"时间轴上大二下排在大一上前头"。
* **不含大五**：本项目的剩余学期按 `TOTAL_TERMS = 8` 算，列出来只会误导。

### 20b. ★★ 光加下拉框不够 —— 必须把已有档案带进去 append

```python
# ⛔ 每导一次就把上一个学期整份覆盖掉：档案永远只有一个学期，
#    趋势 Δ 永远显示"单次导入，暂无趋势"
self._archive = build_archive(parsed, self.term, source=name)

# ✓ 先把加密库里的档案读回来当底座
self._archive = build_archive(
    parsed, self.term, archive=self._load_archive(), source=name
)
```

`core.archival.build_archive(..., archive=...)` **不改入参**，返回追加后的新
`Archive`；同一个 `(key, term)` 重复导入会**保留两份**并记一条 warning ——
绝不静默丢掉某一次导入。

### 20c. 落盘与串号

* 归档走**应用级**加密（`core/user_store`，第 16 节）：自动登录没有密码，
  密码级的话重新打开就空了。
* `AnalysisPage.set_user()` 必须**清掉内存里那份 `_archive`** ——
  它是上一个人的，留着会让下一个人导入时把两个人的学期并进同一份档案。
* `user_file()` 是按 `users.json` 里的 **uid** 解析路径的：
  **账户不存在时 `save_user_json` 直接抛 `CryptoError("本机没有账户…")`**。
  测试里建临时 store 时**必须先 `store.register()`**，否则"档案落盘"整条
  断言会假红（第一版 `tl_semester_test.py` 就是这么翻车的）。

### 20d. 判据

`%TEMP%\tl_semester_test.py`（31 项）真的连导三个学期：
大一上（同页）→ 大二下（同页）→ **新建页面**再导大三上（= 重启程序的等价场景）。
断言：三份记录都在、趋势 Δ 是**真涨的**（第三份表分数确实更高，Δ>0）、
磁盘上 `archive.json.enc` 读回来也是三个学期、文件里**扫不到明文姓名**。

## 21. ★ 更新检查：`urllib.request` 是唯一允许的 stdlib 联网

总纲 §四 把它加进了白名单。**"允许"不等于"不管"**，
`:func:`main._updater_policy_lines`` 在 `--selfcheck` 里守三条：

| 守卫 | 怎么守 | 为什么这么守 |
|---|---|---|
| 只用 stdlib | 扫 `core/updater.py`，出现 `requests/httpx/aiohttp/urllib3/openai/anthropic` 就 FAIL | 防"顺手加个依赖" |
| **import 期零网络** | 把 `urllib.request.urlopen` 换成"一调用就抛"的桩，**重新 import 一遍模块** | 静态 grep 抓不住间接调用；重新 import 是行为验证 |
| 超时 == 5s | 直接断言 `DEFAULT_TIMEOUT == 5` | 总纲 §六 写死的 |

★ 三条守卫**都验证过会 FAIL**（`%TEMP%\tl_updater_gate_test.py` 往
`core/updater.py` 里真注入违规，跑完按字节还原）—— 不验一次就不算有牙。

### 21a. 请求必须走后台线程

设置页的「检查更新」走 :func:`ui.workers.run_async`。
在按钮回调里直接 `urlopen` 的话，**断网时要卡满 5 秒超时**，
期间窗口不重绘（`ui/workers.py` 开头讲的就是这个）。

### 21b. ★ 实测：`/releases/latest` 是 404，那不是"故障"

2026-09-27 实测：仓库公开可读（200），但 `/releases` 返回 `200 []`、
`/releases/latest` 返回 **404** —— **一个发行版都没发过**。

所以只打 `latest` 的话，在发布第一个发行版之前这功能永远只能报"检查失败"，
而那不是实话。`check_for_update()` 因此有四种状态，界面对每种说不同的话：

| status | 界面上的话 | 含义 |
|---|---|---|
| `newer` | 弹窗"发现新版本 vX，是否前往下载？" | 真有新的 |
| `current` | "当前已是最新版本 (v1.7)" | 比过了，一样或更旧 |
| `no-release` | "…仓库里还没有发布任何发行版" | **接口通了**，只是没东西可下 |
| `error` | "检查失败，请手动访问仓库" | 真连不上 |

★ **`check_latest_version()` 的契约一个字没改**（总纲 §六 指定的那个）：
成功给版本号字符串，失败给 `None`。要分状态用 `check_for_update()`。

★ **列表非空但读不出版本号** ≠ "没有发行版"。第一版把这两种混成一种，
界面上会说"仓库里还没有发布任何发行版"，而仓库里明明挂着东西 —— **那是句假话**。

### 21c. ★ v1.8 起：**可以下载，但绝不自动**

v1.7 时这里写的是"绝不自动下载、绝不自动替换，只打开浏览器"。
**总纲 §四 改了这条**（把"更新下载"加进白名单，
第六节给了完整的下载 → 重启 → 替换流程），所以那段话已被取代。

**现在正确的说法是**：

| | |
|---|---|
| ✅ 可以 | 用户点「开始下载」之后流式下载；用户点「立即重启」之后备份 + 替换 |
| ❌ 仍然禁止 | **静默/自动下载** —— 启动时、定时、后台，一律不许 |
| ❌ 仍然禁止 | **热替换**（趁自己还活着去覆盖自己的 exe）—— 那是必失败或写出半截文件 |

每一次下载前面都必须有一个**用户点过确认的弹窗**，
代码里不许有"启动时顺便查一下/下一下"的路径。

★ 判断"是不是偷偷联网了"的判据仍然是第 21 节那条**行为**检查
（`urlopen` 换成炸弹后重新 import 模块），不是读源码找关键字。

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
成绩全在 `u_*/` 里。

★ **备份目录本身必须排除**：备份放在数据目录**里面**（总纲 §12.2 的路径），
不排除的话每迁一次目录就翻一倍。

### 22b. 标记写在哪、什么时候写

* 键：**两个键同时写**（`core.version.VAULT_VERSION_KEYS`）：
  `__vault_version__`（总纲 §12.3 的值）和 `vault_version`
  （任务书 2.1 / 旧 v1.8 charter §1.1 两次给的值）。**读取时哪个有值认哪个。**
  * 为什么写两份：这件事在文档之间**对不上**，而让实现去猜哪边赢的风险是
    **某次真迁移把标记写到一个启动检测读不到的键上** —— 那正是版本保护
    唯一要防的事。写两份的成本是两个 JSON 字段，收益是"谁读都对"。
  * 唯一写入点是 `core.version.stamp_vault_version()`，
    所以"两个键打架"这种状态从写入这一侧就不可能产生。
    `--selfcheck` 里断言两个键都在且相等。
* 位置：`users.json` 顶层，跟着 `UserIndex` 走 —— 因为 `save_users()`
  每次都从 `UserIndex.to_dict()` 重写整个文件，标记**不挂在 UserIndex 上
  就会被下一次写盘悄悄抹掉**。
* 时机：`CryptoStore.register()`（建第一个用户 = 初始化这个库）调
  `init_vault()`。**只在没有标记时盖** —— 覆盖已有标记会让"检测到旧版数据"
  永远发现不了旧数据。写在**明文**的 `users.json` 里，是因为启动检测发生在
  **登录之前**，那时没有密码，密文一概打不开。

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
**启动路径上传空**（那时候还没登录）。当前 1.0→1.7 这条链不需要密码
（没有数据结构变化、不重包裹密钥），参数留着是为了将来那一跳不用改签名。

★ **回滚只恢复 `users.json`** —— 迁移全程只动过这一个文件。
整目录回滚会把备份之后新写的数据盖掉，那才是真的丢数据。

### 22d. 版本比程序新时

`check_compatibility` 返回 `(False, "9.9")`（不需要"迁移"），
启动时**只弹窗提示"请更新程序"，绝不动它**。别把降级当成迁移。

### 22e. 判据

`%TEMP%\tl_migrate_test.py`（62 项）用**真实目录**跑完整条链：
新建库带标记 / 老库判成 1.0 / 备份内容齐全（含 `app.salt` 和成绩密文）/
同秒连备两次不覆盖 / 迁移后仍能登录且成绩一条不丢 /
**备份失败时 users.json 一个字节没动** / **写回失败时逐字节回滚**。

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

★ 各写一份字符串，迟早出现"设置页写着 1.7、更新检查却按 1.6 比"
这种事，而且**不报错，只是行为不对**。`--selfcheck` 里有一条断言
`core.updater.CURRENT_VERSION is core.version.CURRENT_VERSION`。

### 23a. ★★ UI 里**一个版本号字面量都不许有**（总纲 §三 铁律 10）

这不是假想风险 —— v1.8 开工时在 `ui/` 里抓到**三处**写死的 `"v1.0"`：

| 位置 | 症状 |
|---|---|
| `ui/login_window.py` 的 `APP_VERSION` | 登录窗左栏永远显示 `v1.0` |
| `ui/splash.py` 的 `version` 默认参数 | 开屏版本号永远 `v1.0` |
| `ui/pages/account_page.py` 的"关于"表 | 个人中心永远写着 `v1.0` |

三处都**不报错**，只是一直在说假话（程序是 1.8、界面写着 1.0）。
而且**静态 grep 查不出来** —— `ui/anim.py` 的注释里合法地写着
"旧 charter v1.6 第 5.2 节"、`ui/pages/settings_page.py` 的文档字符串提到
`docs/DEV_SUMMARY.md`，一刀切地搜 `1.0` / `1.6` 全是误报。

**判据**（`main._version_source_report()`，`--selfcheck` 里跑）：

1. **静态**：用 `ast` 扫 `ui/**/*.py` 的**字符串字面量**，
   出现"整串就是个版本号"（`^v?\d+\.\d+(\.\d+)*$`）就 FAIL。
   注释根本不在 AST 里，文档字符串被显式跳过 —— 所以"引用"不会误伤。
2. **行为**：把 `login_window.APP_VERSION`、`SplashScreen` 的默认参数、
   `account_page._ABOUT` 真的读出来，必须等于 `f"v{CURRENT_VERSION}"`。

★ 闸门**验过有牙**：往 `ui/splash.py` 注入一处 `version: str = "v1.0"`，
立刻 FAIL 并指名 `ui\splash.py:183`（见 `%TEMP%\tl_v18_gate_test.py`）。

★ 写文档（`.md`）时该写版本号就写 —— 这条只管**界面代码**。

★ 比较一律走 `compare_versions()`（**数值**逐位比）。
`"1.10" > "1.9"` 用字符串比会得出 False，等真出 1.10 那天才暴露。

★ `normalize_version()` **不削末尾的 0**（第一版削了，`"1.0"`→`"1"`，
备份名和迁移链查表同时坏掉）。`"1.7.0"` 和 `"1.7"` 算不算同一个版本
**是 `compare_versions()` 的事**（缺位按 0 补），不是规范化的事。

### 23b. 升版本号 = **同时补迁移链的一跳**

改完 `CURRENT_VERSION`，必须去 `core/migrator.py` 的 `_STEPS` 补一跳 ——
**哪怕什么数据结构都没改，也要写个 `return data` 的空函数**。

不补的后果是**老用户启动时直接报错**：

```
MigrationError: 没有从 v1.7 升级的路径（已知：1.0, 1.6, 1.7, 1.8）
```

v1.7 → v1.8 时实测踩到（`--selfcheck` 的迁移检查当场 FAIL，拦住了）。
补一跳只要三行，忘掉的代价是老用户升不了级。

## 24. ★ 界面上不许出现 Markdown 记号（总纲 §三 铁律 9）

`QLabel` / tooltip 都是**纯文本**（或用 Qt 富文本），`**加粗**` 会**原样显示**
两个星号。踩过两次：

* 分析页表下常驻说明 `**绝不并进限选**`
* 奖学金页「允许特等叠加」的气泡 `**不占**`

两次都是**所有静态断言全绿、只有截图才看见**。

判据：`%TEMP%\tl_label_lint.py`（7 项）。它是**运行时遍历**，不是扫源码 ——
文案可能是拼出来的（`CALC_SCOPE_NOTE` 就是模块级常量拼进 QLabel 的）。
主窗 162 处控件文字 / 气泡 + 登录窗 + 欢迎页全过一遍，
出现 `**` / `__` / 反引号即 FAIL，并且第 4 节带反向自检。

★ 写文档（`.md`）时该加粗就加粗 —— 这条只管**界面文案**。

## 25. 已知限制清单（**记录，不是待办**）

这三条都是**确认过的真实行为**，不是"感觉可能有问题"。
动它们之前先想清楚要不要解封 `core/calculator.py`（铁律一）。

### 25a. `core/parser.py` 把「专业选修」归到限选

冻结的 `CATEGORY_KEYWORDS` 里，`"专业选修"` 同时命中限选和选修的关键字，
打平时按 `必修 > 限选 > 选修` 取先出现的 → 归**限选**。

* 这是**校名别名**（多数培养方案里"专业选修课"就是限选课）；
* `parser.py` 铁律一封板，**一个字符都不许动**；
* 真要改是 Owner 的事。详见第 19 节。

### 25b. ★ 分数 > 100 会**静默掉出加权分母**，但仍留在 `course_scores` 里

实测（2026-09-26）：张三高等数学 91 改成 101 之后

```
口径② 从 85.052632 → 82.928571      （手算 1161/14 —— 5 个学分凭空少了）
total_credit 从 19 → 14
course_scores 里**仍然列着** ('高等数学', 101.0, 5.0)
```

后果两边不一致：智育加权把那门课当没算，而奖学金特等奖「每门 ≥90」判的是
`score is not None`，于是**一个 101 的笔误会算作达标**。

★ **已经做的缓解**：`ui.pages.analysis_page.out_of_range_scores()` 在
**强制人工确认**弹窗（红线 3）里把超范围的格子单独列一栏。
**不动 calculator**，也不会算错。

★ **没做**：改 `core/calculator.py` 让它报错或夹取 —— 铁律一封板。

### 25c. 欢迎页 1.5s：启动比 900ms 那版多约 600ms

总纲 §10.2 写的是"欢迎页 1.5s 时间问候"（原文出自旧 charter v1.7 第六节）。
中间一度被压到 900ms（性能整改时自己定的，不是文档要求），v1.7 已按文档回到 1.5s。

**这是文档定义值，不是 Agent 自定的**，不要再"优化"掉。要动先问。

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

第一版 `rem` 注释写了中文，被自己的断言逮住。**中文注释在这里真会出事**：
`cmd.exe` 按当前代码页逐字节解码 bat，中文按 GBK 解错位后，
错位配对的尾字节范围（`0x40~0xFE`）正好包含 `"` 和 `\` ——
会把后面那句 `copy /Y "%SRC%" "%DST%"` 的引号**吃掉**，报错完全看不出根因。
和 §1b（`.ps1` 的 BOM）是同一个根因。

### 26c. ★ 路径不写进 bat，走命令行参数（`%~1..%~4`）

路径里可能有中文（用户名）。参数走 Windows 的 UTF-16 API，不受代码页影响；
写进文件就要赌用户机器的代码页。

### 26d. 复制要重试，失败就什么都不做

`for /L %%i in (1,1,60)` 每秒重试（刚退出的进程可能还占着句柄几秒）。
60 次全失败 → `goto :giveup`，旧 exe 原样留着。
**最坏情况是"更新没生效"，不是"程序没了"。**

### 26e. 必须写日志

bat 在**本进程退出之后**跑，那时没有控制台、没有异常、没有反馈。
每次交接都往 `%TEMP%\SGMS_Update\apply_update.log` 写一行 ——
没有它，"点了立即重启但版本没变"完全查不下去。

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

★ **必须显式指定引擎**：实测即使 xlrd 装好了，`pd.read_excel("真的.xls")`
对**裸 BIFF 流**依然报 `ValueError: Excel file format cannot be determined,
you must specify an engine manually.` —— pandas 自己的 sniff 只认 zip 和 OLE2。

### 27b. ★★ "导 .xlsx 却弹出 xlrd 错误"的根因（实测复现）

pandas **不看扩展名，看内容的魔数**。一个"扩展名 `.xlsx`、内容其实是 OLE2"
的文件（**教务系统导出的老格式表经常长这样**）被判成 `"xls"` → 调 xlrd →
没装就抛 `ImportError: Missing optional dependency 'xlrd'. … Use pip or conda
to install xlrd.` —— 用户看到的是"我导的是 xlsx，为什么跟我说 xlrd"，
而且错误里还带着 pip 指令。
**修法**：按内容选引擎 + 兜底链（认定的引擎失败就换另一个试）。

### 27c. ★ `xlrd` 必须 **>= 2.0.1**，任务书写的 `<2.0` 是错的

| xlrd 版本 | pandas 2.3.3 读 .xls |
|---|---|
| 1.2.0（`<2.0`） | `ImportError: Pandas requires version '2.0.1' or newer of 'xlrd'` |
| 2.0.2 | 正常读出 |

1.2.0 那条例外里**同样带着版本指令** —— 等于花力气把错误信息换了个写法。
`requirements.txt` 已写 `xlrd>=2.0.1`，自检里断言这个底线。

### 27d. ★ 铁律 1 与「要改 parser 引擎」的冲突，怎么解的

旧 v1.8 charter 第七节说"`core/parser.py`：`pd.read_excel(path, engine='openpyxl')`
显式指定"，同一份文档第三节铁律 1 又说"parser 一个字符不许改"。**自己跟自己打架。**

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

于是两条规则都不用让步：`core/ai/` 仍然零 HTTP import（**扫描范围无需放宽**），
模型下载仍然是 `urllib.request`（在 `core.updater` 里）。

★ 以后往 `core/ai/` 加下载类功能**照这个模式做**：把网络关在 `core.updater` 里。
口子一开，那条闸门就只能放宽 —— 而放宽之后的闸门等于没有。

## 29. 模型文件的落盘位置与判据

* 位置：**`%LOCALAPPDATA%\SGMS\models\<名字>\`**，不是仓库里的 `models/`。
  理由：1~3GB 进仓库是灾难；打包后程序目录常常只读；数据目录才是它的家。
* 落盘顺序：`models/temp/<名字>__<文件名>` → 校验 → **原子改名**进 `models/<名字>/`。
* ★ 判据是"目录里有一个非空的 `.gguf`"，**不是"目录存在"** ——
  下载中途断电会留下空目录，那个不算装好。
* ★ `_safe_name()` 清洗目录名，`delete_model()` 再验一次"解析后仍在 models/ 里"
  —— **防目录穿越**：`download(url, "../../evil")` 不该写到 models/ 外面去。
* `ModelDownloader.progress` 发**百分比 0~100**；**总大小未知时发 `-1`**
  （界面切不确定态）。字节数在 `downloaded_bytes` / `total_bytes` 上读。

## 30. ★★ 激活码系统**已整套删除**（Owner 决定）—— 不许复活

注册登录 + 邮箱找回密码已经跑通，**登录即全部功能可用**。于是整套删除：
`core/activation.py`（675 行）、`config/activation.example.json`、
设置页那张卡片、主窗顶栏「● 已激活」小圆点。
完整清单与遗留文件处置见 `docs/DEV_SUMMARY.md` 第 16.1-16.4 节。

**四条硬约束**：

1. **不许以任何形式复活**「输一个码才能解锁功能」：没有闸门、没有试用期、
   没有"激活才能导入"。
2. 准入只有三个门：**注册 → 登录 → 忘记密码**。加第四种之前先问 Owner。
3. `core/` `ui/` `ai/` 里不许再出现激活码的标识符；`ui/` 的**界面文案**
   不许出现「激活」二字。
4. 老机器上可能残留 `%LOCALAPPDATA%\SGMS\activation.enc` —— **它是惰性的**，
   没有任何代码读它。**不许**写"启动时顺手删掉"那种代码：
   删用户数据必须是用户主动的动作。

**判据**：`main._removed_gate_report()`（`--selfcheck` 里跑，三层：文件层 /
AST 源码层 / `CHECK_MODULES` 接线层）+ `%TEMP%\tl_qt_drive.py`（运行时遍历
主窗与登录窗全部控件文字、占位符、气泡，0 处「激活」）+
`%TEMP%\tl_noact_gate_test.py`（24 项，注入违规会 FAIL，跑完逐字节还原）。

★ 用 `ast` 而不是 grep `activ`：`ui/pages/scholarship_page.py` 里有 openpyxl 的
`book.active`，一刀切地搜必然误报 —— **误报的闸门最后一定会被关掉**。
