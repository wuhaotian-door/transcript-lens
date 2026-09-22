# AGENTS.md — 本仓库的硬性施工规则

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

## 2. 红线（Spec 第 9 章，违反即重写）

- 加权算术**全部本地完成**，绝不调用任何大模型 API 做数字
- **空 ≠ 0**：空 / `-` / `/` / `未选` = 没选这门课，学分不进分母；数字 `0` = 缺考，学分进分母
- 学分提取必须有人工回显确认
- 真实成绩文件、真实激活码**一律不入库**。仓库里唯一允许存在的 xlsx 是
  `tests/fixtures/fake_*.xlsx`
- `config/` 下只允许 `.example.json`，绝不生成真实的 `activation.json`
- 不写注册表。**联网：v1.6 起改为有条件放行 —— 见第 13 节**（SMTP +
  用户主动启用云端 AI 允许；自动更新 / telemetry / 静默请求仍然禁止）

## 3. 落盘

- 明文成绩**任何时候都不落盘**；磁盘上只有 `.enc` 密文
- 用户数据在 `%LOCALAPPDATA%\SGMS\`，**不在仓库里**
- 换激活码只锁「导入」，绝不清空或改动已有成绩库

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

### 4j. `QGraphicsDropShadowEffect` 挂在整个窗口上的代价：约 40ms/次切页

实测（1900×1200 窗口，150% DPI）：阴影开中位 523ms、关 483ms，
**+8%**。Qt 会缓存效果的源 pixmap，页面淡入淡出**不会**每帧重新模糊整窗
（否则会是 +1200ms）。所以这个代价可以接受，但**别在动画期间额外触发整窗
`update()`**。要更省的话，把阴影挪到一个没有子控件的图层上。

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

**为什么 400 多项测试一条都没抓到？**

因为那条路径**从来没被跑过**：所有测试为了省 1.5 秒，欢迎页一律
`skip_to_end()`。而这个 bug 恰好只在"欢迎页**真的**淡出"时才成立。

**所以现在有两道新的闸门，都别再拆掉**：

| 闸门 | 跑什么 | 为什么 `--selfcheck` 顶不上 |
|---|---|---|
| `%TEMP%\tl_boot_test.py` | `auto` / `login` / `switch` 三个**真实时序**场景，每个**单独进程**（退出码本身就是断言） | 它跑的是源码，但至少跑的是**真动画** |
| `transcript-lens.exe --bootcheck` | 打包产物里的真启动流程，结果写进 `%TEMP%\transcript-lens-bootcheck.txt` | `--selfcheck` 是**无窗口**的，一行 GUI 代码都不碰 |

`build_exe.ps1` 打完包会**两个都跑**，退出码非 0 直接判失败。

**规矩**：任何"动画结束后销毁触发它的那个对象"的代码，
回调必须延后一拍；并且**必须有测试真的把动画跑完**，不许 skip。

### 4l. ★★ 启动慢的两个真凶：`QSplashScreen` 和标题里的 emoji

用户反馈"启动非常卡顿"。量完之后发现**跟动画时长、跟机器性能都无关**，
是两笔一次性的、谁也没想到的开销。两条都已被 `%TEMP%\tl_splash_probe.py`
和 `tl_perf_bench.py` 实测钉死（对照组 `sleep(0.15)` 量到 150.3ms，校准正常）。

**（一）`QSplashScreen` 本身比普通 `QWidget` 慢 865ms**

每个变体**单独起一个进程**测（排除"第一次创建原生窗口"这个一次性成本
被算到错误对象头上）：

| 变体 | `show()` |
|---|---|
| `QWidget`（普通） | 165 ms |
| `QWidget` + 无边框 + 半透明 | 167 ms |
| `QWidget` + 无边框 + 半透明 + `opacity 0` | 164 ms |
| **`QSplashScreen`（原样）** | **1030 ms** |
| `QSplashScreen` + 无边框 / 半透明 / `opacity 0` | 1028 ~ 1035 ms |

**跟半透明、无边框、透明度全都无关 —— 只要用了 `QSplashScreen` 就慢 865ms。**
那 865ms 里用户盯着一个全黑的屏幕。`ui/splash.py` 已改成自绘 `QWidget`。

**（二）标题里的 emoji 一个字 241ms**

| pixmap 内容 | 绘制耗时 |
|---|---|
| `📊 成绩单透镜` 22pt Bold | **241.7 ms** |
| `成绩单透镜` 22pt Bold | 0.2 ms |
| `transcript-lens` ASCII | 0.8 ms |

emoji 要现场加载 Segoe UI Emoji 并做彩色字形合成。开屏标题里那一个 📊 是
**启动链上最贵的单个字符**。已删。

> 注意范围：侧栏、页面标题里的 emoji 都在**已经映射好的窗口**里，
> 不占启动时间，**不用删**。只有开屏那张 pixmap 里的要去掉。

**（三）开屏的职责是"盖住"，不是"过场"**

改完上面两条之后还剩一段 **765ms 全黑**。逐拍打点（`%TEMP%\tl_splash_trace.py`）
定位到：开屏**先把 finished 发出去、再淡出**，所以淡完到下一个窗口出现之间
是空的。那 765ms 是：

```
load_token              150 ms
MainWindow() 构造       253 ms   ← 6 个页面 + 全量 QSS polish
MainWindow.show()        39 ms
set_user()（解密意愿）   ~310 ms   ← ProfilePage 的意愿解密 + 激活状态
WelcomeWindow + show     14 ms
```

现在的次序是**先叫下一个窗口、再淡出自己**（`ui/splash.py` 的
`_start_fade_out`）：`finished.emit()` 是同步的，等它返回时下一个窗口
已经在屏幕上了，然后才淡出。这 765ms 就落在开屏背后。

**（四）预热要藏在"用户正在看的东西"后面，而且要扣时间**

`AppFlow._prewarm_main` 把主窗提前建好并 `show()`（`MainWindow.__init__`
里已经 `setWindowOpacity(0.0)`，所以映射出来是全透明的，看不见）。

★ 但**不能在 `run()` 之后紧接着预热**：`run()` 只是把淡入动画 start 出去，
那一刻窗口还一帧都没画。在那时跑 600ms 同步工作，事件循环被占住，
**淡入动画会冻在第一帧**上 —— 用户看到的是"点了一下，半天没反应"。
所以挂在 `WelcomeWindow` 的 `on_shown`（淡入结束）上。

★ 而且停留时间要**扣掉**预热的耗时（`HOLD_MS - spent`）。第一版写成
"干完活再计时"，结果欢迎页实际停留 `900 + 600 = 1500ms` —— 白等 600ms。

### 4m. 切页卡顿：淡出那一段是纯粹的等待

实测一次切页的耗时 vs 两段动画时长（真跑 `StackSwitcher`，取中位数）：

| 淡出 + 淡入 | 实测切页 |
|---|---|
| 180 + 240（原） | **439 ms** |
| 120 + 180 | 326 ms |
| 90 + 150 | 260 ms |
| 0 + 150 | 166 ms |
| **0 + 130（现在）** | **~137 ms** |

`180 → 0` 一项就省掉近 300ms，是整条链上最划算的一刀。用户感知的界线大致是
"100ms 以内算瞬时、200ms 以上能感到迟滞"。

**为什么砍淡出是对的**：淡出那一段旧页在变淡、新页还没出现，
用户盯着一个越来越空的屏幕 —— 它不传递任何信息。想要回淡出的话，
把 `ui/anim.py` 的 `FADE_OUT_MS` 设回 180 即可，`switch()` 会自动走回
淡出分支，**两条路径都有测试覆盖**。

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
**不新增口径、不改口径定义**（Spec 第 61 / 246 行：默认必修+限选计入，
**选修默认剔除**。选修 / 限选 / 必修是三个独立的类别）。

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

## 11. 切页锁的看门狗必须**把页面落到位**，不是只放锁

```python
# ⛔ 只放锁：动画还在跑，用户再点侧栏就是两段动画抢同一个 opacity；
#    而且界面会停在"侧栏亮着 A、内容还是 B"且永不自愈
self._busy = False

# ✓ 把 index 落到位 + 摘掉残留的透明度效果 + 再放锁
self._stack.setCurrentIndex(self._pending); clear_effect(...); self._land(...)
```

阈值 `LOCK_TIMEOUT_MS` = **1500ms**（charter 5.2 指定；原来因为一次 1513ms 的
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

## 13. ★ 联网红线（v1.6 起**有条件放行**）

`docs/SGMS_v1.6_Dev_Charter.md` 第 3 章推翻了 v1.3 的"一律不联网"。
**两份文档冲突时以 charter 为准。**

### ✅ 允许

| 场景 | 条件 |
|---|---|
| SMTP 发邮件 | 仅找回密码流程，用户主动点「发送验证码」触发 |
| 辅导员手动启用云端大模型 | 设置页手动开 + 手动填 API Key，**只在点「深度评估」时**调用 |

### ❌ 禁止

自动更新检查 · telemetry / 埋点 / 崩溃上报 · 激活码联网校验 ·
拿 `requests` / `httpx` 干**非** SMTP、**非**用户主动 AI 的事。

### selfcheck 怎么守（`main._network_policy_report`）

★ **守卫要落在行为上，不能落在"这个包在不在"上。** 原来那条
"打包里见到 requests 就 FAIL"在 charter 之后语义就错了，而且它只能查打包版，
源码版什么都查不到。现在三条：

1. **静态扫 `core/ai/*.py`**，源码里出现任何
   `import requests/httpx/urllib.request/socket/http.client/openai/anthropic`
   就 FAIL（正则只认真正的 import 语句，不认注释里提到的名字）；
2. **拿脏数据调一次 `redact_student`**，看姓名/学号/身份证会不会漏出去；
3. 非白名单联网库**仍然**不该在包里 —— 但报错文案改成
   "AI/更新都还是骨架，是不是有人偷偷加了依赖"，不再是"违反铁律"。

## 14. 奖学金测算（charter 第 4 章，**规则已定死**）

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
charter 第 4 章明确给了公式，不算"自己发明算术"。

★ 特等奖的"每门 ≥90"只检查**这个学生真考了的课**（`score is not None`）：
没选的课不参与判定，**不会**被当成 0 分拖累他；缺考（数字 0）**算考了**，
0 < 90，所以拿不到特等 —— 这是对的。

## 15. 班级号规范

* 导入成绩表时**必须**有人工确认的班级号（charter 4.1）。
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


