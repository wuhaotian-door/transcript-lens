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

## 4. Qt / PySide6 界面：三条踩过的坑

界面已从 tkinter 全量换成 **PySide6**（`ui/` 旧文件已删干净，没有 .bak）。
以下三条都是**实测复现过**的，不是理论风险。

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

