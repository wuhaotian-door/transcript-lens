# 激活码系统移除记录（v1.8.1）

> **结论**：整套激活码系统已从代码库删除。注册 → 登录 → 忘记密码是**唯一**的准入
> 路径，**登录之后全部功能直接可用**，界面不再向用户索要任何「码」。
>
> 本文是这次移除的**唯一记录**。历史文档（`SGMS_v1.3_Final_Spec.md` /
> `SGMS_v1.6_Dev_Charter.md` / `SGMS_v1.7_Dev_Charter.md`）里关于激活码的描述
> **保留不改** —— 它们是当时的事实记录，不是现行规范。

---

## 1. 为什么删

Owner 决定：注册登录 + 邮箱找回密码（`core/email_recovery.py` +
`core/recovery.py`）已经跑通，激活码这一层**不再承担任何职责**：

* 它锁的东西（导入）本来就是登录之后才可能做的动作；
* 它带来的成本（发码、回显、年度过期、方案 A 的年份比对、码表维护）
  全部由 Owner 承担，收益是零；
* 用户多一步「输码」，而且**码在天上（邮箱/聊天记录）、程序在本地**，
  忘带就进不去 —— 这是纯粹的摩擦。

---

## 2. 删掉了什么

| 位置 | 是什么 | 处置 |
|---|---|---|
| `core/activation.py` | 675 行：码表、解析、年度过期（方案 A）、`ActivationEngine`、`activation.enc` 读写、`is_import_allowed()` | **整文件删除** |
| `config/activation.example.json` | 激活码配置模板 | **删除** |
| `ui/pages/settings_page.py` | 页面顶部「激活码」卡片（输入框 + [验证] 按钮 + 状态行）、`_do_activate()`、`_refresh_activation()` | **删除** |
| `ui/main_window.py` | 顶栏那个「● 已激活 / 未激活」小圆点 + 状态文字 + `_refresh_activation()` | **删除** |
| `main.py` | `CHECK_MODULES` 里的 `"core.activation"` | **摘掉** |
| `ui/pages/__init__.py` / `ui/frameless.py` / `core/archival.py` / `core/crypto_store.py` | 提到「激活码 / 激活状态」的注释与文档字符串 | **改写**成现在真实的例子（自动登录令牌等） |

### ★ 一个关键的勘察结论：它其实**没有**真的拦住任何东西

动手之前先把「谁在调用闸门」查清楚了 —— 结果是：

```
core/activation.py 对外提供的闸门（is_import_allowed / require_import /
import_allowed / is_gated）在整个仓库里**零调用点**。
ui/pages/analysis_page.py 的 _do_import() 从头到尾没问过激活状态。
```

也就是说：**删除它不会解锁任何东西**（本来就畅通），
也不会因为「少了一道检查」而放宽安全性 —— 那道检查从来没有接上过。
这一点很重要：如果有真实用户在未激活状态下用过导入功能，
那么删掉它对他们**没有任何行为变化**。

---

## 3. 老机器上残留的 `activation.enc`

`%LOCALAPPDATA%\SGMS\activation.enc` 可能还在老用户的机器上。

* **它是惰性的** —— 新版本里没有任何一行代码读它、写它、删它。
* **我们刻意不删它**。理由：删用户数据目录里的文件必须是**用户主动的动作**；
  为了"清理得干净"而写一段"启动时顺手删掉"，是把一条本来无害的遗留
  变成一个有风险的自动行为（路径解析错一次就是删错文件）。
* 用户想清理就自己删，删掉没有任何后果。

> 同理，`%LOCALAPPDATA%\SGMS\` 下的其它载荷（`users.json` / `app.salt` /
> `u_*/`）一个字节都没有因为这次移除而改动。

---

## 4. 现在还有哪些「门」

| 门 | 什么时候出现 | 作用范围 |
|---|---|---|
| **注册** | 首次使用 | 必须填邮箱并验证（6 位验证码 / 10 分钟 / 最多 5 次 / 60 秒冷却） |
| **登录** | 每次启动（勾了「记住我」则 7 天内免密） | 解开本用户的库密钥 |
| **忘记密码** | 用户主动点 | 用**注册时生成**的 24 位找回码解出库密钥、设新密码 |

**只有这三个。** 加第四种准入机制之前先问 Owner（`AGENTS.md` §30）。

---

## 5. 不许复活：三道判据

### 5.1 静态（`main._removed_gate_report()`，`--selfcheck` 里跑）

| 层 | 判据 |
|---|---|
| **文件层** | `core/activation.py` 与 `config/activation.example.json` **必须不存在**（`main.REMOVED_GATE_PATHS`） |
| **源码层** | `core/` `ui/` `ai/` 的 `.py` 里，标识符不许含 `activation` / `activate` / `active_version` / `activated_year` 词根；字符串字面量不许出现「激活」 |
| **接线层** | `main.CHECK_MODULES` 里不许再列出名字含 `activation` 的模块 |

### 5.2 运行时（`%TEMP%\tl_qt_drive.py`）

静态自检只能证明「源码里没引用」，证明不了「界面上真的不再向用户要码」。
所以运行时要**遍历主窗与登录窗的全部控件文字、输入框占位符、气泡**，
出现「激活」二字即判失败。

### 5.3 闸门有牙的证据（`%TEMP%\tl_noact_gate_test.py`，24 项）

逐个注入**真实违规**，跑闸门，然后**逐字节还原**并核对 sha256：

* 复活 `core/activation.py` → FAIL
* 复活 `config/activation.example.json` → FAIL
* `ui/flags.py` 里加一个 `_try_activate()` → FAIL 并指名行号
* `core/excel_io.py` 里加一个 `active_version = 99` → FAIL
* 从 `core.activation` import → FAIL
* 界面文案里写「请先输入激活码」 → FAIL
* 往 `CHECK_MODULES` 里塞 `core.activation` → FAIL
* **反向自检**：文档字符串里提到「激活」**不**算违规（那是该留下的记录）
* **反向自检**：`ui/pages/scholarship_page.py` 里 openpyxl 的 `book.active`
  **不**被误报

### ★ 为什么用 `ast` 而不是 grep `activ`

`book.active` 就在界面代码里。一刀切地搜字符串必然误报，
而**误报的闸门最后一定会被人关掉** —— 那比没有闸门更糟。
所以判据只认两类东西：**AST 里的标识符**（注释根本不在 AST 里）
和**非文档字符串的字面量**。

---

## 6. 受影响的测试（诚实记录）

| 测试 | 原来 | 现在 |
|---|---|---|
| `%TEMP%\tl_qt_drive.py` | 「顶部条有激活小圆点」「设置页有激活码输入框」+ 调 `_do_activate()` 喂一个乱码 | 改成**反向断言**：顶栏无 `_dot`/`_act_label`、设置页无 `_code_edit`/`_do_activate`/`_act_status`、主窗与登录窗控件文案 0 处「激活」 |
| `%TEMP%\tl_check_2a.py`~`tl_check_4.py` | v1.0 时代的激活码探针（**不在 `tl_run_all.ps1` 里**，早已是历史脚本） | **不参与回归**；它们调用的模块已经不存在，跑起来会 ImportError —— 这是**预期**，不是回归失败 |
| `main.py --selfcheck` | 无激活码相关检查 | **新增** `_removed_gate_report()`（三层），并在报告里显示 `激活码系统已删除（无残留）` |

> **没有删掉任何"还在跑"的测试。** 被改的只有 `tl_qt_drive.py` 里那两条
> 断言 —— 它们断言的对象已经不存在了，继续留着只会永远是红的。
> 替换它们的是更强的反向断言（不是"删掉检查"，是"检查反过来了"）。

---

## 7. 移除后的自检输出长什么样

```
ok    封板文件封印（铁律 1）              4 个文件的 sha256 全部吻合
ok    激活码系统已删除（无残留）          core/ ui/ ai/ 共 43 个文件，0 处引用
ok    CHECK_MODULES 已摘掉激活码          34 个模块
ok    版本号唯一真源（ui/ 无字面量）      扫了 20 个文件，0 处硬编码
...
RESULT: ALL OK
```
