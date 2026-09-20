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

## 4. Tkinter 布局：不要写死窗口尺寸

```python
# ⛔ 别这样
self.geometry("820x500")
self.resizable(False, False)
```

**原因（已出过事故）**：登录页本来是 `820x500` + 禁止缩放。后来加了一个
「忘记密码？」按钮和两行说明，内容长到 **639px** —— 窗口还是 500px，于是：

- 那个按钮被切在可视区之外：**控件树里查得到、用户看不见**；
- 它被分配的高度被挤成 **3px**（单独量是正常的 44px），根本点不到。

而且**两页高度不一样**，只在构造时算一次也不够 —— 切到更高的那一页照样被切。

**规矩**：

1. 尺寸在**所有控件建完之后**按 `winfo_reqwidth()/winfo_reqheight()` 算
   （见 `LoginWindow._fit_to_content`），并且**翻页后重算**。
2. 需要换行的 `ttk.Label` 必须给 `wraplength`，否则它会把窗口撑宽 ——
   翻页时窗口横向跳动就是这么来的。
3. **验证 GUI 不能只断言控件存在**，要量像素：控件的
   `winfo_rooty() + winfo_height()` 必须落在窗口内。见
   `%TEMP%\tl_verify_layout.py` 的 `overflow()`。

## 5. 密码找回：恢复码已删除，邮箱接口是壳

- **恢复码（recovery code）已整体删除**，`core/crypto_store.py` 里一行不剩。
  `UserRecord` 上的 `recovery` 字段、`UserSession.via_recovery`、
  `enable_recovery` / `recover` / `reset_password_with_recovery` 都没了。
  老 `users.json` 里残留的 `"recovery"` 键会被 `data.get()` 静默忽略，
  下一次写盘自然消失 —— **不会报错**。
- **密码忘了就是打不开了**，这是本阶段的产品决定。登录页、注册页各有提示，
  **不要**改成"可以找回"。
- 邮箱找回的**接口**在 `core/email_recovery.py`：只有壳，`send()` 一律抛
  `EmailNotConfiguredError`，**即使配置齐全也不发信**。
  下一阶段接入时要动的地方在该模块开头写清楚了。
- ★ **邮箱做不到"服务器帮你重置密码"**：库密钥只在本地，任何外部服务都拿不到。
  要能找回，恢复材料必须在**注册时**（库密钥还在手里时）就生成并寄出。
  这个时序是命门。
- 邮件 API 配置在 `%LOCALAPPDATA%\SGMS\email.json`（**不在仓库里**）；
  仓库里只有 `config/email.example.json` 模板，且 `config/email.json`
  已在 `.gitignore` 里。

