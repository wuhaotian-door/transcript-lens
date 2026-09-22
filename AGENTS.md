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
- 不联网、无自动更新、不写注册表

## 3. 落盘

- 明文成绩**任何时候都不落盘**；磁盘上只有 `.enc` 密文
- 用户数据在 `%LOCALAPPDATA%\SGMS\`，**不在仓库里**
- 换激活码只锁「导入」，绝不清空或改动已有成绩库

## 4. Qt / PySide6 界面：踩过的坑（4a–4k）

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

- 切筛选只做两件事：`showColumn` / `hideColumn` 对齐当前口径列；
  红标**跟着当前口径重算**（分母变了，挂科/缺考的判定基础就变了）。
- 三个口径列**始终都在**，筛选只改可见性。**不要**去增删列 ——
  那会让"表头 ↔ 单元格"重新有机会漂移，而那正是上一版的 bug。

## 11. 切页锁的看门狗必须**把页面落到位**，不是只放锁

```python
# ⛔ 只放锁：动画还在跑，用户再点侧栏就是两段动画抢同一个 opacity；
#    而且界面会停在"侧栏亮着 A、内容还是 B"且永不自愈
self._busy = False

# ✓ 把 index 落到位 + 摘掉残留的透明度效果 + 再放锁
self._stack.setCurrentIndex(self._pending); clear_effect(...); self._land(...)
```

阈值 `LOCK_TIMEOUT_MS`：实测一次切页中位 ~520ms、最慢 ~600ms，机器忙时
见过 1513ms。**1500ms 太紧**（会去砍一次本来会正常结束的动画），现在是 2500ms。
回归测试见 `%TEMP%\tl_qt_drive.py` 的"看门狗落位"那一段。


