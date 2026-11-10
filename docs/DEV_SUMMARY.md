# 成绩单透镜 · 开发总纲

> **开工前先读这一份。**
>
> 本文是项目全貌：现行规格 + 已关闭的业务规则 + 架构 + 版本演进史 + 测试清单。
> **施工细则**（每条规则的具体判据、踩过的坑、为什么这么定）在 `AGENTS.md`，
> 它是自动加载的，不用手动读。
>
> 本文由 `docs/` 里 5 份互相取代的旧文档合并而成（已删除，git 历史可查）：
> `SGMS_v1.3_Final_Spec.md`、`SGMS_v1.6_Dev_Charter.md`、
> `SGMS_v1.7_Dev_Charter.md`、`SGMS_v1.8_Dev_Charter.md`、
> `ACTIVATION_REMOVAL.md`。文末第 20 节有旧引用对照表。
>
> 对应版本：**v1.9.4**（`core/version.py` 的 `CURRENT_VERSION`）
> 最后更新：2026-11-10

---

## 零、这份文档怎么用

### 0.1 三份文档的分工

| 文档 | 读者 | 什么时候读 | 装了什么 |
|---|---|---|---|
| **`docs/DEV_SUMMARY.md`**（本文） | 开发 Agent | **每次开工前** | 要做什么、已经定了什么、现在是什么样、去哪儿查 |
| `AGENTS.md` | 开发 Agent | 自动加载 + 写代码时对照 | 每条规则的**判据**、踩过的坑、为什么这么定 |
| `docs/UPDATE_GUIDE.md` | **辅导员**（使用者） | 用户遇到"更新/数据在哪/出问题"时 | 数据目录在哪、更新三步、10 条 FAQ |
| `docs/DISTRIBUTION.md` | **维护者** | **发版时** | 发版 SOP 六步、发行版描述格式、排错顺序 |

### 0.2 `docs/` 只允许这三个文件

被删掉的 5 份旧文档里，4 份是互相取代的规格（v1.3 Spec + v1.6 / v1.7 / v1.8
三份 charter：v1.3 被 v1.6 取代、v1.6 被 v1.7 取代、v1.7 被 v1.8 取代），
越堆越多、说法互相打架。
**不许再新增开发文档** —— 新知识写进本文或 `AGENTS.md`。

`main._docs_gate_report()` 在 `--selfcheck` 里盯着这件事：`docs/` 下出现
白名单之外的文件就 FAIL；哪份文档提到已删除的旧文档名也 FAIL。

### 0.3 现在项目到哪一步了

**v1.8 / v1.8.1 / v1.8.2 已发布**（v1.8.2 把 5 份开发文档合并成本文，提交 `e2d7edd`）。

**v1.9 的功能已在工作区完成**，三件事 + 一个 Bug 修复：

| 项 | 落地位置 |
|---|---|
| 解析器表格干扰 Bug（统计列 / 末尾汇总行 / 大标题） | `core/parser.py`（唯一一次解冻，见 §7.4、§13.6） |
| 批量导入德育分 | `core/import_workers.py` + `ui/moral_import_dialog.py` + 奖学金页按钮（§二十一） |
| AI 模型中心 | `core/ai/model_registry.py` + `ui/model_center_dialog.py` + 设置页入口（§八） |
| 更新器改走 `update/manifest.json` | `core/updater.py`（§6.3） |

★ **2026-10-10 起按 `v1.8.1` 发版**（对应提交 `e3b6e75`，标签 `1.8.1`）：
程序版本号 `CURRENT_VERSION` 从 `"1.8"` 升到 `"1.8.1"` —— 这是**发版需要**
（更新检查靠版本号比大小，清单版本必须大于用户手上的版本），
不是数据结构变化；迁移链相应补了 `("1.8", "1.8.1")` 一跳空迁移。
理由与判据见 §16.5，闸门在 `tests/test_version_migration.py`。

**v1.9 之后没有待办功能**，只有第 17 节的已知限制（都是"记录在案、暂不修"
的类型）。下一个版本要做什么，先问 Owner。

---

## 一、课程分类与三口径

### 1.1 必修 / 限选 / 选修是**三个完全独立的类别**，绝不混同

真源是 `core/parser.py` 的枚举（写代码时照这个抄）：

```python
class Category(str, Enum):
    REQUIRED = "必修"
    LIMITED  = "限选"
    ELECTIVE = "选修"
    UNKNOWN  = UNKNOWN      # 提取失败，必须走人工确认
```

> ⚠️ 旧 charter（v1.6 / v1.7）第一节那张表里的 `required` / `limited_elective` /
> `free_elective` **不是真值**，是设计期的英文名，代码里从来没有过。
> 照抄那三个字符串会写出查不到的类别。

**任何把选修归入限选的逻辑都是 bug。** 这条不靠文档保证：
`main._category_policy_report()` 是一条会 FAIL 的行为门（AGENTS.md §19）。

### 1.2 三口径

| 口径 | 包含 | 用途 |
|---|---|---|
| ① 总加权 | 必修 + 限选 + 选修 | 全局总评 |
| ② 仅必修+限选 | 必修 + 限选 | 主修能力 |
| ③ 仅必修 | 仅必修 | 核心底线 |

### 1.3 界面上的口径筛选是**互斥三档**，不是自由勾选

界面上是三个互斥档位（全部 / 必修+限选 / 仅必修），按钮气泡和状态行明确标注
"选修不计入"。

> ⚠️ 旧 charter（v1.6 §6.4）写的是"三个独立 Toggle：必修 / 限选 / 选修，
> **可多选组合**，默认全选"。**那条做不到、也不做** —— 口径是算好的三份数字，
> 不是按行过滤；自由勾选组合得解冻 `core/calculator.py` 加口径（铁律 1 封板）。
> 为什么不这么干，见 AGENTS.md §10b。

**表头从 `SCOPE_COLUMNS` 推导，禁止手写元组**（铁律 7，踩过坑）。

---

## 二、技术栈

| 层 | 选择 | 备注 |
|---|---|---|
| 语言 | Python 3.13（开发机 Anaconda 3.13.9） | |
| GUI | `PySide6>=6.6` | **不用 PyQt6（GPL 传染）、不用 tkinter（已淘汰）** |
| 打包 | PyInstaller `--collect-all PySide6` | 单文件 exe |
| 加密 | PBKDF2 派生 + Fernet | 磁盘上只存 `.enc` |
| 邮箱 | `smtplib`（SMTP 465，网易 163） | 授权码存 AppData，**绝不硬编码** |
| Excel | `openpyxl`（.xlsx）/ `xlrd>=2.0.1`（.xls） | 见第七节，`xlrd<2.0` 是错的 |
| 联网 | `urllib.request`（标准库） | 唯一允许的联网方式 |
| AI | 本地 GGUF 预留 / 云端可选（用户主动） | v1.8 只到"下载 + 认得出来" |

**实测环境版本**（`.exe` 就是在这个组合上打出来的）：
pandas 2.3.3 · openpyxl 3.1.5 · xlrd 2.0.2 · cryptography 46.0.3 ·
PySide6 6.9.2 · PyInstaller 6.22.2。

---

## 三、铁律（10 条）

1. `core/calculator.py` / `core/parser.py` / `core/validator.py` / `tests/`
   **一个字符不许改**（封板；`main.FROZEN_FILE_SHA256` 封印四个文件的 sha256）
2. 密码 / 授权码 / 找回码 / 库密钥：**绝不 print、不回显、不进日志、不进异常**
3. 找回密码靠**注册时生成的 24 位找回码**；邮箱是找回码的存放处，不是重置通道
4. 索引键一律用**学号**，废弃纯姓名索引（同名学生绝不串数据）
5. **不发明未定义规则**（算错比不算危险）
6. 旧 tkinter 代码**全删**
7. 表头从 `SCOPE_COLUMNS` **推导**，禁止手写元组
8. **选修不归入限选**（selfcheck 行为门验证，违反即 FAIL）
9. `QLabel` 是纯文本、**不渲染 Markdown**，界面文案里不许出现 `**粗体**` 这类记号
10. **版本号唯一真源：`core/version.py` 的 `CURRENT_VERSION`** ——
    任何界面显示版本号必须从这里读

> 铁律 1 和"要修 Excel 引擎"曾经冲突过（第十节第七节都要动 `parser`），
> 解法是走 `**read_excel_kwargs` 扩展点，不动被封印的文件。见第七节。

---

## 四、联网红线

### ✅ 允许（**每一种都必须用户主动点**）

| 场景 | 条件 |
|---|---|
| SMTP 发邮件 | 找回密码流程，用户点"发送验证码" |
| 云端大模型 | 设置页手动开 + 手填 API Key，仅在点"深度评估"时调用 |
| 更新**检查** | 用户点"检查更新" → `urllib.request` 只读一次 Gitee 接口 |
| 更新**下载** | 用户点"开始下载"，只下他勾选的那几个文件 |
| AI 模型**下载** | 同上（走同一个下载器） |

### ❌ 禁止（永远是禁止）

- **自动更新 / 静默下载**
- **telemetry / 埋点 / 崩溃上报**
- `requests` / `httpx` 用于任何场景
- 激活码联网校验（整套激活码系统已删除，见第十六节）

**程序启动时一次网络请求都不发。** 这条由 `main._network_policy_report()` 守
（5 组检查，含"打包里见到 requests 就 FAIL"），细则见 AGENTS.md §13 / §21。

---

## 五、版本号管理

### 5.1 五个显示点，一个真源

| 位置 | 显示内容 | 来源 |
|---|---|---|
| 欢迎页 | "早上好，{用户名}" + 版本号小字 | `CURRENT_VERSION` |
| 登录窗口标题 | "成绩单透镜 v{CURRENT_VERSION}" | `CURRENT_VERSION` |
| 设置页"关于" | "成绩单透镜 v{CURRENT_VERSION}" | `CURRENT_VERSION` |
| 个人中心"关于" | 版本行 | `CURRENT_VERSION` |
| 更新弹窗 | "发现新版本 v{X}" | Gitee API |

### 5.2 修掉的旧 bug

v1.7 及之前有三处**写死了 `"v1.0"`**：`ui/login_window.py` 的 `APP_VERSION`、
`ui/splash.py` 的开屏版本号默认参数、`ui/pages/account_page.py` 的"关于"表。
三处都不报错，只是一直在说假话（程序是 1.8、界面写着 1.0）。

### 5.3 闸门

`main._version_source_report()` 两件事：**静态**用 `ast` 扫 `ui/` 下所有
字符串字面量，出现纯版本号字面量就 FAIL；**行为**把 `login_window.APP_VERSION`
等三个显示点真的读出来，必须等于 `f"v{CURRENT_VERSION}"`。

> ★ 用 `ast` 而不是 grep：注释里的 "charter v1.7 第七节"、文档字符串里引用的
> 旧文档名都是**引用**不是显示，grep 必然误报，而**误报的闸门最后一定会被
> 人关掉** —— 那比没有闸门更糟。

**铁律**：UI 里一个版本号字面量都不许有；升版本号必须**同时补迁移链一跳**
（见第十二节，v1.7 → v1.8 时漏补，实测报过 `MigrationError: 没有从 v1.7
升级的路径`）。

---

## 六、自动更新流程

### 6.1 全流程

```
点「检查更新」→ GET update/manifest.json 读 app 字段（url / size / sha256）
   ├─ 没有新版本 → 状态行"当前已是最新版本 (v1.9)"
   └─ 有新版本   → 弹窗（版本说明 + 模块勾选）
        → 点「开始下载」→ 进度条 / 速度 / 随时可取消
        → 下载完成 → 弹窗：
             ━━━━━━━━━━━━━━━━━━━━━━━
             ✅ 更新已完成（v1.9）
             是否立即重启应用完成更新？
             ━━━━━━━━━━━━━━━━━━━━━━━
             [稍后]  [立即重启]
```

**点「立即重启」**：备份当前 exe 到 `%LOCALAPPDATA%\SGMS\backup_exe\`
→ 写 `update.json` 到 `%TEMP%\SGMS_Update\` → 生成 `apply_update.bat`
→ `subprocess.Popen(bat)` → `sys.exit(0)`。

**点「稍后」**：关弹窗、标记"待重启"，**下次启动时检测并执行替换**
（不重新下载）。

### 6.2 替换机制

- bat 脚本：等待 2 秒 → `copy /Y` 新 exe 到目标路径 → del 临时文件
  → del `update.json` → `start` 新 exe
- **复制失败重试 60 次**（刚关掉的程序可能还占着文件几秒）
- **不热替换** —— 绝不尝试覆盖正在运行的 exe
- **备份失败就拒绝替换**（磁盘满/没权限 → 明确报错并放弃：
  宁可不更新，也不能让用户既没有新的、也没有旧的）
- 每一步都必须写日志到 `%TEMP%\SGMS_Update\apply_update.log`

四个"绝不"和 `.bat` 的 ASCII 约束见 AGENTS.md §26。

### 6.3 清单优先：`update/manifest.json` 才是下载配置

**v1.9 起 `check_for_update()` 先读清单**（`core/updater.fetch_manifest()`）：

```
GET https://gitee.com/wuhaotiansystem/transcript-lens/raw/master/update/manifest.json
{ "schema": 1, "app": { "version": "1.9", "url": "…", "size": 234567890,
                        "sha256": "…", "notes": "…", "published": "2026-10-08" } }
```

| 规则 | 判据 |
|---|---|
| 版本比较 | 一律用 `core/version.CURRENT_VERSION`（`check_for_update(current=…)` 可注入，给测试用） |
| `app` 缺 `url`/`size`/`sha256` 任一项 | `ManifestApp.usable == False` → 视为"清单没准备好"，**退回旧路线** |
| 清单 404 / 连不上 / JSON 坏 | 不报故障，**退回旧路线**（`source="release"`） |
| 两条路都走不通 | 才报"仓库里还没有发布任何发行版" |
| 结果来自哪条路 | `UpdateCheck.source` = `"manifest"` 或 `"release"`，界面据此显示直链与大小 |

★ **2026-10-10 起仓库里这份清单是"填好的"**（`version: 1.8.1`，`app` 三件套齐全，
指向 Gitee 发行版 `1.8.1` 的附件），不再是空模板。两个踩过的坑各留一句
（清单自己的 `_doc` 里写得更细）：

* **结构必须是顶层 `app`** —— 有人写成 `files.app` 那种嵌套结构，`parse_manifest()`
  直接给 `(None, '清单里没有 app 段')`：**清单路线静默失效**、退回老路线，
  清单里填的 url/size/sha256 一个都没生效，而界面上完全看不出来。
  判据现在在仓库里：`tests/test_manifest.py::test_仓库清单只允许空模板或完全填好`
  —— **半填也不许**（只有 url 没有 sha256 同样等于白填）。
* **直链里的标签名必须照抄真实标签** —— 本仓库标签是 `1.8.1`（**不带 `v` 前缀**），
  写成 `.../releases/download/v1.8.1/...` 一定 404；而且发行版页面上那两个
  `1.8.1.zip` / `1.8.1.tar.gz` 是 Gitee **自动生成**的源码包，
  **不等于**"已经上传了 exe 附件"。

**旧路线（Release 描述正文）保留但已 deprecated**：`parse_release_body()` 与
`latest_release()` 上面都标了 deprecated，`model_downloader.catalog_from_release()`
仍可用它取 AI 模型直链。文件本体**不放 Gitee 附件**（免费版有大小和流量限制），
放网盘、清单/描述里贴直链。

> ⚠️ **打了 tag 不等于建了 Release。** 仓库里没有任何发行版时，
> `/releases/latest` 返回 **404**、`/releases` 返回 **`[]`** —— 那是实话，
> **不是故障**（清单路线通的时候根本不会走到这里）。

> ★ 清单里 `url`/`size`/`sha256` 三个空着是**合法状态**：说明 Owner 还没把
> 网盘直链填进来。程序会给"清单还没填直链"的提示，不会拿一个空 url 去下载。

---

### 6.4 分片发布：包比 Gitee 附件上限大时怎么办（v1.8.1）

**硬限制**：Gitee 社区版**单个附件 ≤ 100MB**（官方帮助「创建 Release」页原文：
"单个附件不能超过 100M（GVP 项目 200M）"；配额页还写了单仓库附件总容量 1G）。
我们打出来的 onefile exe 是 **240MB 量级** —— **根本传不上去**。
发行版页面上那两个 `<标签>.zip` / `<标签>.tar.gz` 是 Gitee **自动生成的源码包**，
**不是** exe 附件（曾经就是被这一点误导过：清单里写了一条永远 404 的 exe 直链）。

所以 v1.8.1 起清单支持 `app.parts`：

| 字段 | 含义 |
|---|---|
| `app.size` / `app.sha256` | **整个 exe** 的字节数与哈希（两种模式含义相同） |
| `app.parts[]` | 顺序切好的每一片：`url` / `size` / `sha256`（每片自己的） |
| `app.url` | 分片模式下**留空**（单文件模式才填） |

程序端 `core.updater.download_parts()` 的保证（比单文件的 `download_exe` 只多不少）：

1. **逐片校验**：每片下完先核自己的 sha256，坏片当场丢弃、绝不混进拼接；
2. **顺序固定**：拼接顺序 = 清单里 `app.parts` 的顺序，不看文件名；
3. **整体再校验**：拼完核 `app.sha256` 与 `app.size`，任何一条不过都**不写目标文件**；
4. **只写 `<目标>.part`**，成功才 `os.replace` —— 失败/取消**绝不损坏已有 exe**；
5. **可续传**：每片缓存在 `<目标>.parts\partNN`，已校验过的片再点一次**连请求都不发**；
6. 成功之后清掉 `<目标>.parts\`。

**发版三步**（`tools/split_release.py` 是仓库里的工具，不是临时脚本）：

```powershell
python tools\split_release.py dist\transcript-lens.exe --parts 3 `
    --tag 1.8.1 --version 1.8.1 --notes "…" --out release
```

它逐片量体积（**超过 100MB 就要求你多切几片**，不让你"传上去再说"），
并打印可直接粘贴的 `app` 段；然后把每片传成发行版附件（网页拖拽，或
`POST /repos/{owner}/{repo}/releases/{release_id}/attach_files`），
最后**用 HEAD/Range 复查每条直链**能下、`Content-Length` 与清单一致。

★ 判据：`tests/test_updater_parts.py`（15 例，全程只打本机 `http.server`：
拼接逐字节一致 / 坏片拒绝且不动旧文件 / 缺片 / 整体体积与哈希对不上 /
取消保留缓存 / 续传不发请求 / 半截缓存要重下 / 下完清理缓存 /
Gitee 超限片 `usable=False`）+ `tests/test_manifest.py` 里的 `app.parts` 七例 +
`main._download_policy_lines()` 三条（静态必须有 `download_parts` 与
`ATTACHMENT_LIMIT_BYTES`；行为要"真跑一次失败的分片下载、目标文件逐字节未变"）。
验牙：`%TEMP%\tl_parts_gate_test.py` 把上限改小 / 把 `download_parts` 改名 /
让它一开始就清空目标文件 —— 三种注入都当场报红，还原后 sha256 一致、闸门重新全绿。

★ **2026-10-10 v1.8.1 真机实测**（首次走完整条链路，结论比设计更重要）：

* 三片都传上去了：附件 id `3358116` / `3358129` / `3358151`，各 80033189 字节；
  直链 `https://gitee.com/wuhaotiansystem/transcript-lens/releases/download/1.8.1/transcript-lens-v1.8.1.exe.partN`
  可下（**标签名是 `1.8.1`，不带 `v` 前缀**）。
* ★ **Gitee 附件直链不认 Range**：带 `Range: bytes=0-0` 去请求，回的是
  **HTTP 200 + 完整的 76MB**（不是 206）—— 所以 `download_exe` 的"**片内**断点续传"
  在这个托管上**不会生效**，它会按既定逻辑"服务器忽略 Range 就从 0 重下"（那条判据
  当初就是为这种服务器写的）。**整片级续传仍然有效**：`<目标>.parts\partNN`
  里已校验过的片**连请求都不发**。要真续传，得换支持 Range 的托管（网盘/对象存储）。
* 端到端复验（`%TEMP%\tl_verify_release.py`）：从三条**真实直链**下载 →
  `download_parts` 逐片校验 → 按清单顺序拼接 → 整体 sha256
  `bc58221ee32644d1b43bc4c0abbba5bc062d3ea9d61f8e32cc7eeb17a215e211` 与清单一致 →
  **拼出来的 exe 自己跑 `--selfcheck`，退出码 0、`RESULT: ALL OK`**。
  229MB 用时 **117s（≈2.0 MB/s）**。
* ★ 老客户端（v1.8，不认 `app.parts`）读到这份清单会判「缺 `app.url`」→
  **退回老的发行版正文路线**（安全降级，不会下错东西）；分片下载只对
  ≥ v1.8.1 的客户端生效。**第一次装 v1.8.1 得手动拿包**（三片下载后按顺序拼、
  或直接用拼好的那个 exe）。

---

## 七、Excel 导入引擎

### 7.1 事实：教务系统的 .xlsx 里有一大半其实是老格式

pandas **不看扩展名、看魔数**。一见 OLE2 头（Excel 97-2003 / BIFF）就去调
`xlrd` —— 没装就在导入 `.xlsx` 时弹出 `xlrd` 的错。这就是"导 .xlsx 却弹 xlrd
错"的真相（AGENTS.md §27b）。

### 7.2 三条落地

| 项 | 做法 |
|---|---|
| 引擎选择 | `core/excel_io.engine_for()` **按内容**选，不是按扩展名 |
| 依赖 | `requirements.txt` 里 **`xlrd>=2.0.1`**，见下 |
| 错误文案 | 友好弹窗，**不再显示原始 pip install 指令** |

### 7.3 ★ `xlrd` 必须 `>=2.0.1`

旧 charter（v1.8 §七）和任务书写的是 `xlrd<2.0`，**那是错的**，实测：

```
xlrd 1.2.0  →  ImportError: Pandas requires version '2.0.1' or newer of 'xlrd'
xlrd 2.0.2  →  正常读出
```

pandas 2.3.3 自带版本闸门（`pandas/io/excel/_xlrd.py`）。装 1.2.0 等于把
"缺 xlrd"换成了"xlrd 太老"，**错误信息里照样带着版本指令** —— 正好是 v1.8
要修掉的那个体验。

### 7.4 ★ 铁律 1 和"要修 parser"的冲突怎么解的

旧 charter（v1.8 §七）写"`core/parser.py`：`pd.read_excel(path,
engine='openpyxl')` 显式指定" —— 那要改被封印的文件，与铁律 1 直接冲突。

**解法**：`parser` 只多了一个 `**read_excel_kwargs` 透传扩展点，真正的引擎
决策在 `core/excel_io.py`。封印的 sha256 没变，铁律 1 没破。细则见
AGENTS.md §27d。

---

## 八、AI 模块

| 文件 | 状态 |
|---|---|
| `core/ai/model_downloader.py` | ✅ 实装：GGUF 从网盘下到 `models/`，续传 + sha256 + 体积底线 |
| `core/ai/model_registry.py` | ✅ v1.9 实装：读远端清单 → 分组 → 内存闸门 → 勾选计划 → 装/未装/待更新 |
| `core/ai/local_assistant.py` | 占位，**不实现推理** |
| `core/ai/cloud_adapter.py` | 占位，不实现 |

- 设置页 AI 区域：模型列表 + 下载进度 + 删除模型
- 模型落盘：`%LOCALAPPDATA%\SGMS\models\<名字>\<文件名>`
- `installed_models()` 的判据是"目录里有一个**非空**的 `.gguf" ——
  下载中途断电不会让设置页显示一个装好的假模型
- **`core/ai/` 零 HTTP import**，没有为模型下载放宽（下载走 `core/updater`
  那套 `urllib.request`）
- 不引入 `torch` / `transformers` 等依赖

> ★ **v1.8 只做到"把模型下载下来 + 认出来"，还没有接推理引擎。**
> 下完之后点「评估」仍然显示「模型已加载但未配置推理引擎」——
> 这是**诚实的状态说明**，不是故障。

### 8.1 模型中心（v1.9）

设置页「本地模型」那一行的**第一个**按钮是「模型中心」
（`ui/model_center_dialog.open_model_center(parent, *, on_installed=None)`）。

**数据源**：`core/ai/model_registry.GITEE_RAW_MODELS` =
`https://gitee.com/wuhaotiansystem/transcript-lens/raw/master/update/ai_models.json`
（仓库里 `update/ai_models.json` 就是它的模板；改清单要同时改仓库这份）。
清单形状：`{"schema": 1, "models": [ {id, name, group, size, sha256, url,
minRamGb, defaultCheck, …} ]}`。

| 环节 | 函数 | 判据 |
|---|---|---|
| 取清单 | `fetch_model_list(timeout=…)` | 走 `core.updater._get_json`（**本模块零 HTTP import**） |
| 解析 | `parse_model_list(payload)` | 逐条容错，坏字段收集成**中文警告**返回（`(models, warnings)`） |
| 合并本地 | `merged_with_local(models, dest_dir=None)` | **磁盘上有非空文件优先于账本**，状态为 `已装` / `未装` / `待更新` |
| 内存闸门 | `local_ram_gb()` / `check_memory(need_gb)` | 用 `GlobalMemoryStatusEx`；`need_gb <= 0` = 清单没声明 → 放行 |
| 默认勾选 | `default_checked(model)` | `defaultCheck=true` **且** 本机内存 ≥ `minRamGb` 才默认勾 |
| 下载计划 | `build_choices` / `group_choices` / `describe_download_plan` | 分组默认 `推荐`，本机已有的归 `本机已有` |

**落盘**：`installed_file(dest_dir=None)` = `default_models_dir()/installed.json`
（即 `%LOCALAPPDATA%\SGMS\models\installed.json`，schema 1），
`load_installed` / `save_installed` / `record_installed` / `forget_installed` 四个出口。

★ **`minRamGb` 写错不许静默放行**（v1.9 修）：`_memory_gb()` 认得出
`8` / `"8"` / `"8GB"` / `"8 g"` / `"8吉字节"`；写了但读不懂 → 内存按 0 处理
**并带一句中文警告**（"这条不会被内存拦住，请自己确认机器能不能跑"）。
理由：`check_memory` 对 `need <= 0` 是放行，两件事叠在一起会让 4GB 机器
把 3B 模型判成"够跑"。

**界面行为（`ui/model_center_dialog.py`）**：

| 规则 | 判据 |
|---|---|
| 分组显示 | `QTreeWidget`，`COLUMNS = ("名称","大小","状态")`，推荐组在前 |
| 内存不够的模型 | 复选框留着但**整行不可选**（`flags` 里**没有** `ItemIsEnabled`），不是悄悄隐藏 |
| **绝不自动下载** | 打开清单只读不写；下载必须用户点「下载选中」（延续 §21c） |
| 逐个下载 | 复用 `core.updater` 的下载器（续传 + sha256），**不重写** |
| 校验 | 先比 `size`（容差 `SIZE_TOLERANCE = 0.01`，下限 4096 字节）再比 `sha256`，**都对才写 `installed.json`** |
| 失败处理 | 自动重试 1 次 → 仍失败则**该行标红** + 给「复制链接到浏览器下载」按钮 |
| 线程 | 取清单与下载都走 `ui.workers.run_async`（§5b） |
| 装完回调 | `downloadFinished = Signal(list)` → 设置页 `_on_models_installed(ids)` 刷新模型列表 |

---

## 九、奖学金测算

### 9.1 公式（Owner 定死，不许改）

```
最终成绩 = 智育加权（口径①，必修+限选+选修全口径）× 0.7 + 德育分 × 0.3
```

- 智育加权：`calculator` 输出的总加权
- 德育分：0-100 整数，人工输入，支持从 Excel 粘贴批量录入

### 9.2 数据组织

- 导入成绩表时**人工输入班级号**（非空，字母数字或短横线）
- 按班级号建文件：`class_{班级号}` → 该班所有学生成绩列表
- 学生唯一键：**学号**

### 9.3 名额与特等

- 每班独立配置：一等 X 名、二等 Y 名、三等 Z 名（`QSpinBox`），
  存 `class_{班级号}_quota.json.enc`
- **特等奖**：最终成绩进前 X 名 **且** 每门科目原始成绩 ≥ 90 分；
  不占一/二/三等名额；默认不叠加（设置页可选允许叠加）
- 特等校验遍历 `parser` 返回的 `courses` 列表，逐门判 `score >= 90`

### 9.4 输出

结果表：排名 | 姓名 | 学号 | 智育分 | 德育分 | 最终成绩 | 奖项 | 备注；
着色一等金、二等银、三等铜、特等紫；底部统计总人数/参测人数/各奖项实际与名额；
[导出结果] → Excel。

> ⚠️ 已知限制：**分数 > 100** 会被静默排除出加权分母但仍在 `course_scores` 里。
> 见第十七节。

---

## 十、界面规格

### 10.1 窗口

- 无边框：`FramelessWindowHint` + `WA_TranslucentBackground`
- 主容器 `QFrame`：圆角 12px + `QGraphicsDropShadowEffect`（`blurRadius=15`）
- 自定义标题栏：关闭 / 最小化 + 拖拽
- 全局字体：`Microsoft YaHei UI, 10`

四条硬约束（无边框窗口踩过的坑）见 AGENTS.md §4e；阴影挂整窗每次切页约
+40ms，见 §4j。

### 10.2 启动流程

```
启动 → splash（0.8s）→ 检查 session token
   ├─ 有效 → 欢迎页（1.5s 时间问候）→ 主界面
   └─ 无效 → 登录窗 → 登录成功 → 淡出 → 欢迎页 → 主界面
```

问候时段：**05:00-11:59 早上好 / 12:00-17:59 下午好 / 18:00-04:59 晚上好**，
显示 `"{时段}，{用户名}"`。`HOLD_MS = 1500` 在 `ui/welcome.py`。

> ★ 这 1500ms 是**文档定义值**，不是手感调的。要动先问 AGENTS.md §25c。

★ **次序有讲究，两步都不许挪到开屏后面**（AGENTS.md §26g）：
`run_pending_update()` → `run_startup_migration()` → `flow.start()`。
"先换程序、再迁数据"这个次序是确定的。

### 10.3 切页

- `QStackedWidget` + `QPropertyAnimation`；目标 **≤150ms**，现在 ~137ms
- `FADE_OUT_MS = 0` / `FADE_IN_MS = 130`（淡出 180→0 是性能上最划算的一刀）
- 切页锁 + **看门狗 1500ms** 强制作废（`LOCK_TIMEOUT_MS`），
  看门狗必须把页面**落到位**，不是只解锁
- ★ **切页抖动正解是 `_PageCover` 幕布**，**不给页面挂透明度效果** ——
  `QGraphicsOpacityEffect` 会缓存源 pixmap，导致整页空白（AGENTS.md §4b / §17）

### 10.4 各页要点

| 页面 | 要点 |
|---|---|
| 智育分析 | 班级号输入 + [导入成绩表] + 学期下拉（8 学期）+ 互斥三档筛选 |
| 侧写辅导 | 左侧搜索框（实时过滤）+ 列表，右侧侧写卡片；意愿索引 = **学号** |
| 成长档案 | 学期时间轴 + 趋势 Δ + 定型/可救分区；数据来自 `core/archival.py` |
| 奖学金 | 见第九节 |
| 设置 | 邮箱找回 / AI 助手 / 更新 / 修改密码 |
| 个人中心 | 当前账号、切换账号、修改密码（需库密钥 + 验旧密码） |

搜索用 `findItems` + `MatchContains`。

**界面上"这个学生是谁"一律用学号做键**（`ui.intentions.student_key`、
`ui.flags.build_flags_by_index`），见 AGENTS.md §9。

---

## 十一、数据落盘与加密

### 11.1 目录

```
C:\Users\<系统用户>\AppData\Local\SGMS\
├── users.json                 账号索引（用户名、校验值；没有明文密码）
├── app.salt                   应用级密钥盐 ★ 别删，删了意愿/班级/档案全打不开
├── u_<uid 前 8 位>\
│   ├── grades.enc             加密后的成绩库
│   ├── tags.json.enc          发展意愿标签
│   ├── archive.json.enc       成长档案（学期时间轴）
│   ├── class_XX-1.json.enc    班级成绩快照
│   └── ...                    德育分、名额、AI 设置等
├── backup_exe\                更新时自动备份的旧程序
├── models\                    本地 AI 模型
└── backup_v{旧版本}_{时间戳}\  数据迁移前的完整备份
```

### 11.2 两条硬约束

1. **exe 与 AppData 物理分离** —— exe 放桌面或任意目录，打包时不可能夹带用户数据
2. **每个用户的密钥由其密码 PBKDF2 派生** —— 同一台电脑上用户 A 解不开用户 B 的库

### 11.3 仓库里永远不出现

`data/`、`*.db`、`*.enc`、`*.xlsx`（除 `tests/fixtures/fake_*.xlsx`）、
`.key`、真实激活码明文、**真实成绩**。仓库是**公开 + MIT** 的，这是硬约束。
打包与 commit 前跑白名单自检，命中任一即 abort 并报警。

`backup_exe\` 和 `models\` **在数据目录里、不在程序目录里**：程序可能装在
`C:\Program Files\`（普通用户写不进去），而 AI 模型有 1~3GB。

---

## 十二、数据迁移与版本保护

### 12.1 三个版本比较分支

| 情况 | 程序会做什么 |
|---|---|
| 数据版本**比程序旧** | 弹窗"检测到旧版数据 (v1.0)，是否迁移？" |
| 数据版本**和程序一致** | 什么都不弹，直接进主界面 |
| 数据版本**比程序新** | 弹窗"请先更新程序"，**绝不动用户数据** |

### 12.2 迁移流程

1. **先备份不可再生的数据**到 `backup_v{旧版本}_{时间戳}\`（`core/migrator.backup_vault()`）；
   数据目录不可写时使用持久备用位置，详见第二十九节
2. 再迁移 —— 链在 `core/migrator._STEPS`：
   `("1.0","1.6") → ("1.6","1.7") → ("1.7","1.8")`，
   每一跳**只写版本标记**，成绩/意愿/档案一个字节都不动
3. 告诉用户备份放在哪，建议重启
4. **失败自动回滚**（只回滚 `users.json` —— 整个迁移过程只改过这一个文件）

**备份失败就拒绝迁移**，绝不"先改了再说"。

### 12.3 版本标记的两个键

写盘时**两个键都写、值永远相同**，读取时哪个有值认哪个：

- `__vault_version__`（旧 charter 8.1 写的键名）
- `vault_version`（任务书写的键名）

理由：两边都是权威文档、说的不是一回事，**让实现去猜哪边赢的风险是"某次真
迁移把标记写到一个启动检测读不到的键上"** —— 而那正是版本保护唯一要防的事。
唯一写入点是 `core.version.stamp_vault_version()`，自检里有一条断言两键相等。

**没有版本标记的老库按 `v1.0` 算**（`DEFAULT_VAULT_VERSION`）。

### 12.4 约束

- 迁移前必须备份；备份默认放在数据目录**里面**，所以扫描数据目录时必须
  **排除 `backup_*` 本身**
- 版本降级（用新版 exe 开过旧库，再用旧版 exe 打开）→ 弹窗提示不兼容
- ★ 升版本号必须**同时补迁移链一跳**（哪怕空函数），否则老用户启动直接报
  `MigrationError: 没有从 v1.8 升级的路径`

**三层保护**：① 版本标记 → ② 迁移前强制备份 → ③ 失败自动回滚。

---

## 十三、导入解析规则

1. **三级提取**：正则抠 `学分\s*(\d+\.?\d*)` 与 `(必修|限选|选修|任选)`
   → 独立的权重行 → 都失败则标红进人工弹窗
2. **人工回显弹窗是强制环节，不能 skip**
3. **列数一致性校验**：某行成绩列数 < 课程列数 → **整行标红不参与计算**，
   弹窗提示"第 X 行可能错位"。**禁止静默吞行。**
4. **文本型数字强转 float**：`"85.000000"` → `85.0`；转不了 → 视为异常标黄
5. **校验锚点**：若表里有"所得学分"列，程序算出的总学分与锚点差 > 0.5
   → 弹窗警告

### 13.6 ★ 表格干扰的三类过滤（v1.9 新增，`core/parser.py`）

教务导出的成绩表里有一堆**不是学生成绩**的东西，以前会被当课程列或当学生吃进去。
现在 `parse_dataframe()` 有四个判据（都在 `core/parser.py`）：

| 干扰 | 判据 | 后果 |
|---|---|---|
| **统计列**（"不及格门数"/"平均分"/"总分"/"所得学分"/"平均学分绩"/"平均学分绩点"/"排名"） | `is_stat_header(header)` 查 `STAT_HEADER_KEYWORDS` | 该列**不算课程列**，也不参与"列数错位"判据；列号记进 `ParseResult.stat_columns` |
| **末尾汇总行**（"37人"/"470"/"不及格"/"18"…） | 先过 `SUMMARY_ROW_KEYWORDS` + `_SUMMARY_COUNT_RE`（`\d+\s*人`），再看姓名列 | 整行跳过，理由记进 `ParseResult.skipped_rows`，**不弹错位窗** |
| **合并单元格大标题** | `_row_is_title_like(row)`：只有首格有字、其余全空 | **不吃表头扫描预算**，直接跳过 |
| **半截行** | 学号/姓名全空、或成绩列数 < 课程列数 | 按原 §13.3 处理（整行标红、不参与计算） |

**每一处跳过都留一条中文 warning**（写进 `ParseResult.warnings`），
所以"这行为什么没了"永远能查到。测试见 `tests/test_parser_filters.py`（49 用例）。

> ★ **这是 `core/parser.py` 唯一一次解冻**（铁律 1 封的是 sha256）。
> 新哈希 `14ab62ab2b6fdcdd6cff6c59427263f940c0cbee6c02691b80788a4cde31e450`，
> 已同步进 `main.FROZEN_FILE_SHA256`（`main.py` 里那条注释写明"v1.9 解冻记录，
> 唯一一次，只动 parser.py"）。**下次再动就必须再走一遍这条路。**

**成绩处理规则（不变）**：

- 补考**不覆盖**原成绩
- 同名课出现两次（非补考）→ **取成绩高那次**，学分用那次的，不叠加不平均
- 空单元格 / `-` / `/` / `未选` / 纯空格 = **未选跳过**
- 数字 `0` = **缺考，必须计入加权**
- 学期合并：`[Σ(上成绩×上学分) + Σ(下成绩×下学分)] / [Σ上学分 + Σ下学分]`，
  **禁止**对两个学期的均分再取平均

**谈话提纲三段式**（本地模板，不调 AI 也能出）：
`【事实】` → `【行动】` → `【对标意愿=…】`。
调云端时 payload 只允许：`S 编号 + 均分 + 课程学分 + 分数区间 + 意愿标签`，
**禁止姓名、真学号、身份证**（`_network_policy_report()` 里有脱敏出口检查）。

---

## 十四、已关闭的业务规则（Owner 定死，不再讨论）

| 规则 | 最终决策 |
|---|---|
| 发展意愿标签 | 辅导员在 UI 手动勾选（保研/考研/就业/考公/留学/未定），存本地加密库；**v1 不做学生端、不做扫码** |
| 默认计算口径 | 只有三档，见第 1.2 节 |
| 纵向累计 | 按"学号 + 学期标签"**append**，支持大一到大四任意次追加 |
| 数据目录 | `C:\Users\<系统用户>\AppData\Local\SGMS\` |
| 分发形态 | 绿色 exe，**不写注册表、不装 C 盘** |
| 仓库 | `wuhaotiansystem/transcript-lens`，分支 **`master`**（**不要改成 main**） |
| 合并时 | 同名取高 |
| 补考 | 不覆盖原成绩 |
| **不做** | Web、服务端、多人在线协同、第三方登录（微信/QQ）、4.0 GPA 换算（保留接口）、图表可视化（v2） |

> ⚠️ 旧 v1.3 Spec 第 2 章还有一条"初始激活码 `SGMS-1ABCD-2026`，
> 换码只锁导入不清库" —— **整套激活码系统已在 v1.8.1 删除**，这一条作废。
> 见第十六节。

---

## 十五、仓库与提交约定

- 仓库：`https://gitee.com/wuhaotiansystem/transcript-lens.git`，**公开 + MIT**
- 分支：`master`（Gitee 初始化默认值，**不要改成 main**，避免和远程不同源）
- **单主干开发**：所有人只在 `master` 上 commit，不建 `develop`、不建 `feature/*`。
  需要存档时打 tag
- commit message 格式：`<模块>: <做了什么>`
  （例：`core: 实现加权计算器与三口径切换` / `ui: 完成导入页学期归属选择` /
  `docs: 更新规格说明`）
- **每次 commit 前必须跑一遍白名单校验**（见第 11.3 节）
- `dist/` `build/` `*.spec` `*.exe` 都在 `.gitignore` 里，**绝不 `git add`**
  （`build_exe.ps1` 最后一步会显式检查这一点）

### 现行 exe 产物

| 目标 | 路径 | 大小量级 | 给谁 |
|---|---|---|---|
| release | `dist\transcript-lens.exe` | ~230MB | **给辅导员的**（单文件、双击就跑） |
| debug | `dist\transcript-lens-debug\` | ~680MB | 自己排查用（带控制台） |

---

## 十六、版本演进史

| 版本 | 做了什么 | 当时的坑 / 备注 |
|---|---|---|
| v1.3 | 锁定技术规格；core 逻辑层 + tkinter 界面 | 仓库里还有 tkinter 时代的文件（已全删） |
| v1.6 | 成长档案 + 奖学金测算实装；修切页抖动；AI 骨架；**联网红线改"有条件放行"**；导入加学期下拉；类别独立行为门 | 切页 500→180ms、启动 6681→3055ms；表头统一 |
| — | 修欢迎页淡出回调销毁窗口导致的**静默崩溃 0xC0000409** | 必须 `QTimer.singleShot(0, window, on_done)` 延后一拍 |
| — | 界面文案去掉 Markdown 记号 | `QLabel` 是纯文本，`**` 会原样显示 |
| v1.7 | 更新检查（`urllib.request` 只读 Gitee）+ 数据迁移与版本保护；QLabel 文案铁律；UI 层拦 >100 分 | 加 `core/version.py` / `core/updater.py` / `core/migrator.py` |
| v1.8 | 自动更新器实装（可下载可替换，**但绝不自动**）+ 版本号全局同步 + Excel 老格式修复 + AI 模型下载器骨架 | 修掉三处硬编码 `"v1.0"` |
| v1.8.1 | **删除整套激活码系统**（提交 `9fea15f`） | 见下 |
| v1.8.2 | 5 份开发文档合并成**这一份总纲** + 「文档唯一真源」自检闸门（提交 `e2d7edd`） | 30 套件 runner 全绿；`docs/` 白名单机制生效 |
| v1.9 | 解析器表格干扰修复（`core/parser.py` 唯一一次解冻）+ 德育分批量导入 + AI 模型中心 + 更新器改走 `update/manifest.json` | 未发版；两段开发指令见 §二十二 |

### 16.1 激活码系统为什么删、删了什么

注册 → 登录 → 忘记密码是**唯一**的准入路径，**登录之后全部功能直接可用**。
激活码这一层不再承担任何职责：它锁的东西（导入）本来就是登录之后才可能做的
动作；成本（发码、回显、年度过期、年份比对、码表维护）全由 Owner 承担，
收益是零；用户还多一步"输码"，而**码在天上、程序在本地**，忘带就进不去。

**删掉的东西**：`core/activation.py`（675 行）、`config/activation.example.json`、
设置页的激活码卡片（`_do_activate()` / `_refresh_activation()`）、
`ui/main_window.py` 顶栏的"已激活"小圆点、`main.CHECK_MODULES` 里的
`core.activation`，以及四处注释/文档字符串里的"激活码"措辞。

★ **一个关键勘察结论：它其实没有真的拦住任何东西。**
动手前先查"谁在调用闸门"，结果是 `is_import_allowed` / `require_import` /
`is_gated` 在整个仓库里**零调用点**，`analysis_page._do_import()` 从头到尾
没问过激活状态。所以删除它**不会解锁任何东西**（本来就畅通），
对现有用户是**零行为变化**。

### 16.2 现在还有哪些"门"（只有三个）

| 门 | 什么时候出现 | 作用范围 |
|---|---|---|
| **注册** | 首次使用 | 必须填邮箱并验证（6 位验证码 / 10 分钟 / 最多 5 次 / 60 秒冷却） |
| **登录** | 每次启动（勾了"记住我"则 7 天内免密） | 解开本用户的库密钥 |
| **忘记密码** | 用户主动点 | 用**注册时生成**的 24 位找回码解出库密钥、设新密码 |

**加第四种准入机制之前先问 Owner。**

### 16.3 老机器上残留的 `activation.enc`

`%LOCALAPPDATA%\SGMS\activation.enc` 可能还在老用户机器上。**它是惰性的**
（新版本里没有任何一行代码读它、写它、删它），**我们刻意不删它** ——
删用户数据目录里的文件必须是用户主动的动作，写一段"启动时顺手删掉"是把一条
本来无害的遗留变成一个有风险的自动行为（路径解析错一次就是删错文件）。

### 16.4 不许复活：判据

`main._removed_gate_report()`，三层：

| 层 | 判据 |
|---|---|
| 文件层 | `core/activation.py` 与 `config/activation.example.json` **必须不存在** |
| 源码层 | `core/` `ui/` `ai/` 的 `.py` 里标识符不许含 `activation` / `activate` / `active_version` / `activated_year` 词根；字符串字面量不许出现"激活" |
| 接线层 | `CHECK_MODULES` 里不许再列出名字含 `activation` 的模块 |

**运行时**（`%TEMP%\tl_qt_drive.py`）：遍历主窗与登录窗的全部控件文字、输入框
占位符、气泡，出现"激活"二字即判失败 —— 静态自检只能证明"源码里没引用"，
证明不了"界面上真的不再向用户要码"。

★ **为什么用 `ast` 而不是 grep `activ`**：`book.active`（openpyxl）就在界面代码
里，一刀切搜字符串必然误报，**误报的闸门最后一定会被人关掉**。判据只认两类
东西：**AST 里的标识符**（注释根本不在 AST 里）和**非文档字符串的字面量**。
文档字符串里提到"激活"**不算违规**（那是该留下的历史记录）。

★ **词根是子串匹配，所以另配了一份"整名"白名单**：
`main._QT_API_IDENTIFIER_ALLOWLIST` 精确豁免 `QWidget.activateWindow()`
（Qt 公开 API，`ui/chat_dialog.open_chat` 用它把非模态窗口提到前台）——
`"activate" in "activatewindow"` 会命中它，正是上面说的那类误报。
★ 豁免的是**整名**、词根表一个字没删：`activate_window()` 照样 FAIL。
实测（v1.9 收尾）：注入 `_activation_probe = 1` → FAIL 并指名
`ui\chat_dialog.py:883`；注入 `def activate_window()` → FAIL 并指名
`ui\chat_dialog.py:885`；两次都按字节还原、sha256 与原始一致，闸门重新全绿。

### 16.5 ⚠️ 提交号 vs 程序版本号

提交前缀（`feat(v1.8.1)` / `docs(v1.8.2)` / `feat(v1.9)`）是**开发批次**的编号，
`CURRENT_VERSION` 是**程序**版本 —— 两者不是一回事，**但发版时必须让它们对上**。
两次不一致都记在案：

* 删激活码那批标的是 **v1.8.1**，当时 `CURRENT_VERSION` 仍是 `"1.8"` ——
  数据格式没变，升它会让所有 v1.8 用户的库凭空变成"比程序旧"、多弹一次
  没有必要的迁移提示，所以当时**刻意不升**；
* **2026-10-10 正式按 `v1.8.1` 发版**（提交 `e3b6e75`，标签 `1.8.1`）：
  `CURRENT_VERSION = "1.8.1"`。理由是**发版需要** ——
  `core.updater.is_newer(清单版本, CURRENT_VERSION)` 决定要不要提示更新，
  **清单里的版本号必须大于用户手上的版本号**，否则更新提示永远不出现。

★ 所以这次升版本号**不是**数据结构变化，迁移链里补的是
`("1.8", "1.8.1", _lift_1_8_to_1_8_1)` 这样一跳空迁移（`return data`）。
规矩仍是 AGENTS.md §23b：**每升一次 `CURRENT_VERSION`，`_STEPS` 就要多一跳**，
哪怕什么都没改。判据现在落在**仓库内**（`%TEMP%` 会被系统清理，靠不住）：
`tests/test_version_migration.py` 遍历 `known_data_versions()`，
任何一个旧版本迁不过来（断档 → `MigrationError`）就直接红。

### 16.6 ★ 2026-10-27 升到 v1.9（第二阶段）

v1.9 第二阶段补完**模型中心增强**（负载指示 / 卸载 / 复制链接）、
**AI 推理引擎**（`core/ai/inference.py`，llama-cpp-python 可选依赖、懒加载）、
**档案合并与成绩覆盖机制**（`core/database.py`）三件事，并按"发版需要"把
`CURRENT_VERSION` 从 `"1.8.1"` 抬到 `"1.9"` —— 改动只有两处：
`core/version.py` 的那一行，和 `core/migrator.py` 的 `_STEPS` 补一跳
`("1.8.1", "1.9", _lift_1_8_1_to_1_9)`。

★ 照旧是**空迁移**（`return data`）：v1.9 新增的两个加密载荷
（`grade_history.json.enc`、`student_profiles.json.enc`）都是**新建文件** ——
老库打开时读到"没有这个文件"就当成空，已有数据一个字节都不动；
`archive.json.enc` 的格式一个字没改，覆盖策略只决定**新导入**怎么写。

判据：`known_data_versions()` = `('1.0','1.6','1.7','1.8','1.8.1','1.9')`；
`--selfcheck` 的版本源闸门报「程序版本号对得上 v1.9（3 处：
`login_window.APP_VERSION` / `SplashScreen(version=)` / `account_page` 的"版本"）」。

---

## 十七、已知限制（记录在案，不是待办）

| # | 限制 | 为什么现在不修 |
|---|---|---|
| 1 | `parser` 把「专业选修」归到**限选** | 历史遗留，改动会动封板的 `parser`；行为门只保证"选修不归入限选"这条底线 |
| 2 | **分数 > 100** 会被静默排除出加权分母，但仍在 `course_scores` 里；特等判定用 `score is not None`，所以 101 分的笔误会**达标** | 修它要改 `calculator.py`（铁律 1 封板）。UI 层已有一道拦截，但数据层没修 |
| 3 | 欢迎页 1.5s 是**文档定义值**，不是性能结论 | 要动先问，不要凭手感调 |
| 4 | AI 模型下下来了但**没有推理引擎** | v1.8 的范围只到"下载 + 认出来"，"评估"会诚实地说"未配置推理引擎" |
| 5 | 云端大模型是**骨架**，未实装 | 同上 |
| 6 | 4.0 GPA 换算**只有接口**，没有实现 | v1.3 就定为 v2 范围 |
| 7 | `/releases/latest` 在仓库没有发行版时返回 **404** | **不是故障**，是 Gitee 的正常行为；程序会照实说 |

---

## 十八、测试与闸门清单

### 18.1 `--selfcheck` 的 11 条闸门（`main.py`）

| 闸门函数 | 管什么 |
|---|---|
| `_credential_leak_report()` | 邮件授权码绝不进仓库 |
| `_frozen_files_report()` | 铁律 1 封印：4 个文件的 sha256 |
| `_removed_gate_report()` | 激活码系统不许复活（三层） |
| `_version_source_report()` | 版本号唯一真源（`ui/` 无字面量 + 行为读值） |
| `_excel_engine_report()` | Excel 引擎可用 + `xlrd>=2.0.1` + 文案不含 pip 指令 |
| `_apply_mechanism_report()` | 启动替换机制（下载只用 stdlib、失败不损坏已有文件） |
| `_network_policy_report()` | 联网红线 5 组检查 + 脱敏出口 |
| `_category_policy_report()` | 必修/限选/选修三类别独立（行为门） |
| `_version_migration_report()` | 版本标记（两键相等）+ 备份可用性 |
| `_docs_gate_report()` | **本文档唯一真源**：`docs/` 白名单 + 无死链接 + `AGENTS.md` 指向本文 |
| `_error_report_gate_report()` | **v1.9.3 补充**：错误弹窗复制报告（traceback / 日志路径 / 截断） |

跑法：`python main.py --selfcheck`；另有 `--bootcheck`（真实启动流程，
退出码本身就是断言）。

另外 `CHECK_MODULES`（`main.py:78`）在自检里逐个 `import` 一遍
（**v1.9 起 38 个模块**，新增 `ui.moral_import_dialog` / `ui.model_center_dialog` /
`core.import_workers` / `core.ai.model_registry`）—— 加新模块要顺手加进去。

### 18.2 回归套件

**pytest**（`python -m pytest tests/ -q`，runner 的第 1 个套件就是它）：

| 文件 | 管什么 | 用例 |
|---|---|---|
| `tests/test_calc.py` | 加权口径（**被封板的那个**） | 8 |
| `tests/test_parser_filters.py` | v1.9 三类表格干扰过滤 | 49 |
| `tests/test_import.py` | 批量导入德育分（校验四规则 / CSV / 落库） | 40 |
| `tests/test_model_registry.py` | 模型清单解析 / 内存闸门 / 装没装 / 勾选 | 109 |
| `tests/test_manifest.py` | `update/manifest.json` 优先 + 退回旧路线 + **仓库那份清单不许半填** + `app.parts` | 58 |
| `tests/test_updater_parts.py` | **分片下载与拼接**（逐片校验 / 顺序 / 整体校验 / 取消与续传，只打本机 http.server） | 15 |
| `tests/test_version_migration.py` | **版本链无断档**（§23b：升版本号必须补一跳）+ 迁移记账 | 19 |
| `tests/test_error_report.py` | **错误信息一键复制**：报告要素 / 超长截断 / Critical 弹窗长按钮 / 点完不关窗 / 未捕获异常钩子 | 9 |

（v1.9.3 全量实测 **887 passed**。加 `test_version_migration.py` 的原因见下。）

★ **`tests/test_version_migration.py` 是 2026-10-10 补的**：以前"升版本号要补迁移跳"
这条只有 `%TEMP%` 里的开发套件守着，而 **`%TEMP%` 会被系统清理** ——
那天实测 `tl_migrate_test.py`（连同 `tl_docs_gate_test.py` / `tl_noact_gate_test.py` /
`tl_category_gate_test.py` 等一批）已经**不见了**，`python %TEMP%\tl_migrate_test.py`
直接 `[Errno 2] No such file or directory`。
**规则落在仓库里才守得住**，所以把版本链的判据搬进了 pytest。

runner 是 `%TEMP%\tl_run_all.ps1`（**不在仓库里**，是本机开发工具；**v1.9 起 31 个套件**）。
⚠️ **2026-10-10 起它已经不完整**：套件文件被系统清理掉了一大半，
`tl_run_all.ps1` 自己还在但跑不起来 —— 所以**发版时的验证以仓库内为准**：
`python -m pytest tests/ -q` + `python main.py --selfcheck` + `--bootcheck`
+ `build_exe.ps1`（打包后自动跑这两个）。下面这段是它完整时的覆盖范围，留作记录：
覆盖四类：源码闸门（selfcheck / bootcheck）、策略闸门
（`tl_category_gate_test.py` / `tl_updater_gate_test.py` / `tl_v18_gate_test.py` /
`tl_noact_gate_test.py` / `tl_docs_gate_test.py`）、链路测试（`tl_migrate_test.py` / `tl_updater_test.py` /
`tl_semester_test.py` / `tl_scholarship_test.py` / `tl_v16_test.py` /
`tl_v18_test.py` / `tl_recovery_test.py` / `tl_regflow_test.py` / `tl_flow_test.py` /
`tl_cli_test.py`）、**GUI 与真实行为探针**（`tl_qt_drive.py` /
`tl_apply_probe.py` 真的把生成的 bat 跑一遍 / `tl_download_probe.py` 起本地
HTTP 服务器验续传 / `tl_boot_test.py` 三种启动模式 / `tl_visual_new.py` 量像素）。

★ runner 给**每个套件**都加了 300 秒硬超时（超时 → `taskkill /T /F` 杀整棵
进程树、记 `exit=124`，已经打出来的部分输出照常计数）。起因是 2026/9/30 实测：
`tl_updater_test.py` 的 4e 漏还原一个模块级桩，4b 于是真的弹了个**没人点的
模态框**，整个套件**静默挂死 40 分钟、一个字都不输出**，把后面 23 个套件全堵住，
而 runner 自己完全没法发现这件事。**一条挂死的用例不该能拖住整套回归。**

★ 这条超时**本身被证明会触发**（v1.9 实测）：`%TEMP%\tl_timeout_probe.ps1` 用 runner
里同一套 .NET 启进程代码，把上限压到 3 秒、去跑一个 `time.sleep(20)` 的进程 →
`timeout=True exit=124 elapsed=3.7s`（真被杀掉，不是只写在注释里）。
另注一个**已定性的计时异常**：2026/10/8 那次 31 套件全绿时，`cover` 一行 elapsed
报成 **4635.8s** 而 `exit=0`、20 条断言全过，当天系统事件日志里没有任何睡眠/唤醒
（Kernel-Power 42/107，最近一次是 10/7 01:24）—— 判断为调度/计时异常，不是产品
卡住、也不是超时没生效；真超时一定同时满足 `exit=124` + 进程被 `taskkill`。

### 18.3 测试纪律

- 测试打印 `[PASS] 标签` / `[FAIL] 标签`
- **替换了模块级符号就必须还原**（`finally:` 里恢复成原对象）。漏还原的下场
  不是"报错"而是**挂死**：`settings_page` 的 `check_for_update` 桩留着
  "body 里有直链"那一版，点「检查更新」就会走更新向导、弹一个没人点的模态框。
  同类事故见 AGENTS.md §26g（`tl_v18_test.py` 第一版没注入假 `Popen`，
  真把生成的 `.bat` 跑起来了）
- GUI 套件自带 `faulthandler.dump_traceback_later(120, exit=True)` 看门狗：
  挂死时会自己打出**每条线程的调用栈**再退出，而不是静静躺着
- **闸门必须被证明会 FAIL**：往真文件里注入违规 → 看它报红 → **逐字节还原并
  核对 sha256**（`tl_v18_gate_test.py` / `tl_noact_gate_test.py` 就是干这个的）
- GUI 验证不能只断言控件存在，要**量像素**；鼠标交互要用 `QTest.mouseMove()`
  真动鼠标
- 跑全套 runner 期间**不得并行跑 GUI 工作**
- 重建 exe 前必须
  `Get-Process -Name 'transcript-lens*' | Stop-Process -Force; Start-Sleep -Seconds 3`
- ★ **断言剪贴板的用例会栽在机器状态上，不是栽在代码上**（v1.9 实测）：
  `tl_qt_drive.py` 的「一键复制进了剪贴板」某次报 `0 字符`，控制组实验证明是
  **explorer 的剪贴板链卡死** —— 那一刻连纯 ctypes 的 `OpenClipboard`（不含本项目
  一行代码）都返回 err=5、PowerShell 的 `Set-Clipboard` 也抛「所请求的剪贴板操作失败」。
  **判定顺序：先跑控制组，再怀疑代码**（`taskkill /f /im explorer.exe` + 重启 explorer
  立刻重跑，同一套件「全部通过」）。跑全套 runner 前重启一次 explorer 当预飞，
  能避开这个假红。`tl_qt_drive.py` 里那条断言现在自带**环境探针**：先测
  `OpenClipboard`，开不了就打印 `[SKIP]` 并换成一条**与剪贴板无关**的替身判据
  （正文够长、值得复制），**不把实质一起跳掉**；探针能双向证明（重启 explorer 后
  同一条断言立刻真跑起来并「全部通过」，2026/10/8 实测）。

---

## 十九、开工 / 收工检查表

### 开工前

- [ ] 读过本文（尤其第三节铁律、第四节联网红线）
- [ ] 要动的东西**没在封板清单里**（`calculator` / `parser` / `validator` / `tests`）
- [ ] 知道这次改动**属于哪一版**，要不要升 `CURRENT_VERSION` + 补迁移链一跳
- [ ] 涉及界面 → 先看 AGENTS.md §4（Qt 踩坑 4a-4m）；涉及联网 → §13 / §21

### 收工前

- [ ] `python main.py --selfcheck` 全绿（**尤其那 10 条策略闸门**）
- [ ] 改了闸门 → **证明它会 FAIL**（注入 → 报红 → 逐字节还原 + 核对 sha256）
- [ ] `%TEMP%\tl_run_all.ps1` 全绿
- [ ] `git status` 干净，`dist/` `build/` 没被 `git add`
- [ ] 改过 UI 文案 → 跑 `tl_label_lint.py`（界面不许出现 Markdown 记号）
- [ ] 改了版本号 → `core/migrator._STEPS` 补了一跳；本文第 0 节的"对应版本"同步
- [ ] 要发版 → 走 `docs/DISTRIBUTION.md` 的六步 SOP

---

## 二十、旧引用对照表

旧文档已删除。如果看到老注释、老 commit、老对话里引用它们，按下表找：

> ★ **代码注释和文档字符串里还留着 120 多处 `charter 8.3` 这类旧编号引用 —— 那是有意的。**
> 它们是"这条规则当年出自哪里"的出处记录，`main._docs_gate_report()` 只判**文件名/路径**，
> 不判 "charter" 这个词。**更不要去批量替换**：v1.6 的 `6.x` 一节里既有 AI 骨架接口
> （现在 §八）又有欢迎页停留时长（现在 §10.2），**同一个旧编号在不同文件里指向不同的
> 新章节**，机械换名必然改错意思。要查就照下面这张表。

| 旧引用 | 现在看本文哪一节 |
|---|---|
| "Spec 第 9 章 / 11 条致命红线" | §三 铁律 + §十三 导入解析规则 |
| "charter v1.3 §一 仓库约定" | §十五 |
| "charter v1.3 §二 / §十 Owner 定死" | §十四 |
| "charter v1.3 §五 数据隔离" | §十一 |
| "charter v1.3 §六 导入解析铁律" | §十三 |
| "charter v1.3 §七 谈话提纲" | §十三 末尾 |
| "charter v1.6 §一 / v1.7 §一 / v1.8 §一 课程分类" | §一 |
| "charter v1.6 §二 / v1.7 §二 / v1.8 §二 技术栈" | §二 |
| "charter v1.6 §三 / v1.7 §三 / v1.8 §三 铁律" | §三 |
| "charter v1.6 §四 / v1.7 §四 / v1.8 §四 联网红线" | §四 |
| "charter v1.6 §5.x / v1.7 §五 奖学金" | §九 |
| "charter v1.6 §6.x / v1.7 §六 UI 架构" | §十 |
| "charter v1.7 §七 更新检查" | §六 |
| "charter v1.7 §八 / v1.8 §五 版本与迁移" | §五 + §十二 |
| "charter v1.8 §六 自动更新" | §六 |
| "charter v1.8 §七 Excel" | §七 |
| "charter v1.8 §八 AI 模块" | §八 |
| "charter v1.8 §九 禁止事项" | §三 + §四 + §十四 |
| `ACTIVATION_REMOVAL.md` | §十六 的 16.1-16.5 |
| "任务书 2.1 / 2.3 / 3.2 / 4" | 任务书是外部输入，不是本文档；其中的 `xlrd<2.0`（任务书 3.2）**是错的**，见 §7.3 |

---

## 二十一、批量导入德育分（v1.9 新模块）

**读者**：要改导入、要加一列新字段的人。**先读这一节，别重新发明一遍。**

| 文件 | 干什么 |
|---|---|
| `core/import_workers.py`（757 行） | 全部逻辑：读表 → 校验 → 预览计划 → 落库 → 错误 CSV → 批次台账。**一行 Qt 都不 import** |
| `ui/moral_import_dialog.py` | 预览弹窗（`MoralImportPreviewDialog.confirm(plan, parent)`） |
| `ui/pages/scholarship_page.py` | 德育分卡片的「批量导入」按钮 + 进度条 + 完成弹窗 |

### 21.1 流程（三步，每一步都能中止）

```
「批量导入」→ QFileDialog（.xlsx / .xls / .csv）
  → [后台] plan_import(path, known_classes=…, default_class=self.current_class)
  → 预览弹窗：能导 N 行 · 失败 K 行 · 跳过 M 行 + 前 5 行 + 全部警告
  → 用户点「确认导入」才 [后台] apply_plan(store, username, plan, batch_id=…)
  → 状态行 + 弹窗：成功 N 行 / 跳过 M 行 / 失败 K 行
```

**为什么分两步**：`plan_import` 只读不写库，用户能在**任何数据进库之前**看到
"哪几行会被丢掉、为什么"。取消 = 一个字节都没写。

### 21.2 判据（改之前先看这张表）

| 项 | 值 / 判据 |
|---|---|
| 表头识别 | `detect_columns(df)` 扫前 `HEADER_SCAN_ROWS = 10` 行，**命中 ≥ `HEADER_MIN_HITS = 2` 个关键字**才算表头；找不到 → `ImportFormatError`（界面文案"这份表读不进来"） |
| 四类坏行 | ① 学号重复（**同批内**，理由写"第 N 行已经出现过"）② 姓名为空 ③ 分值不是数字（理由带原值，如 `德育分「优秀」不是数字`）④ **班级不在已建班级里** |
| 坏行后果 | 收进 `plan.failures`，**不中断整批**，其余行照常导入 |
| 分值范围 | `MORAL_MIN = 0` / `MORAL_MAX = 100`（必须与微调框、`save_moral` 的 clamp 是同一个区间） |
| 错误清单 | `export/import_errors_<batch_id>.csv`（`utf-8-sig` 带 BOM，Excel 打开不乱码）；**一行错都没有就不建文件** |
| 成功行落库 | `scholarship.save_moral()`，键是 `sid:<学号>`，一个班一个加密文件 |
| 批次台账 | `BATCH_FILE = "moral_batches.json.enc"` |
| 批次号 | `new_batch_id()`；`record_batch()` 追加进台账，`load_batches()` 读回 |
| 一次上限 | `MAX_BATCH_RECORDS = 50` 条提示 |
| 进度 | `IMPORT_STEPS = 3`（选文件 / 读表 / 落库） |
| 引擎 | 复用 `core/excel_io.engine_for()` **按魔数**选引擎，不另写一遍 |

★ **`ImportPlan.failures` / `.skipped` / `.classes` 是 property，不是方法**
（写 `plan.failures()` 会 `TypeError`）。`plan_import(frame=…)` 传 DataFrame 时
必须是 `header=None` 形状（表头当**第一行数据**传），否则 `detect_columns` 认不出表头。

### 21.3 两个被测试逼出来的真 Bug（别改回去）

| Bug | 症状 | 正解 |
|---|---|---|
| 台账文件名叫 `moral_batches.json` | `save_user_json` → `save_encrypted` **当场拒绝**：`ValueError: 拒绝写入 'moral_batches.json'：应用级载荷只允许 .enc 后缀。明文 .json 一律不许落盘（Spec 红线 11）` | 必须叫 `.json.enc` |
| 班级校验写成 `elif known and raw_class not in known` | **一个班都没建过时静默放行**，德育分写进没有名单的班 —— 数据进库了，界面上谁也看不见 | 改成 `elif raw_class not in known`，空集合走专门文案"先在智育分析里把这个班的成绩导进来" |

---

## 二十二、v1.9 两段开发指令（原文归档 + 落地对照）

> **为什么留这一节**：这两段是 Owner 在 v1.9 下达的原始指令，按要求"合并进开发
> 总纲"。以后翻旧账（"当时到底要求了什么"）看这里，不用去翻对话记录。
> **本节是历史记录，不是待办清单** —— 落地情况看第 21 节和 §6.3 / §8.1 / §13.6。

### 22.1 指令一：SGMS v1.9 开发任务（三件事）

| 原文要求 | 落地 |
|---|---|
| 新文件 `core/import_workers.py`：读 Excel/CSV → 校验 → 落库 | ✅ §21.2 |
| 校验四规则：学号重复 / 姓名为空 / 分值非 float / 班级不在已建班级 | ✅ 四类坏行，理由各不相同 |
| 失败行写 `export/import_errors_<batch>.csv`，**不中断整批** | ✅ 无错不建文件 |
| 成功行带 `batch_id` 追加进德育分表 | ✅ 分数进 `save_moral`，批次元数据进 `moral_batches.json.enc`（**分数记录本身不加字段**，所以数据格式没变、不需要迁移） |
| UI：**设置页或主菜单**加"批量导入德育分"按钮 → 选文件 → 预览前 5 行 → QThread → 进度条 → "成功 N / 跳过 M / 失败 K" | ⚠️ **改落在奖学金测算页德育分卡片**（德育分本来就在那页，设置页放它反而不合直觉），其余照做；线程用 `ui.workers.run_async`（§5b 的统一出口），不自己 new `QThread` |
| 复用 `core/excel_io.py` 的魔数选引擎 | ✅ §21.2 最后一行 |
| 新文件 `core/ai/model_registry.py`：`fetch_model_list()` 读 Gitee raw 的 `update/ai_models.json`，返回 `list[dict]` | ✅ §8.1 |
| 本地已装模型读 `models/installed.json`（不存在则 `{}`） | ⚠️ 实际是 `%LOCALAPPDATA%\SGMS\models\installed.json`（`installed_file()`），且**磁盘上有文件优先于账本** |
| 新文件 `ui/model_center_dialog.py`：`QTreeWidget` 分组（推荐/高级）+ 复选框 + 名称/大小/状态 | ✅ 另加 `本机已有` 组 |
| 默认勾选 `defaultCheck=true` **且** 本机内存 ≥ `minRamGb` | ✅ 内存不够的**整行不可选**（不是隐藏） |
| 底部「下载选中」→ 逐个调 `core.updater` 的下载函数（复用） | ✅ |
| 每个文件校验 size + sha256，**一致才写 `installed.json`** | ✅ 容差 `SIZE_TOLERANCE = 0.01`（下限 4096 字节） |
| 失败顺序：自动重试 1 次 → 仍失败则**标红 + 复制链接到浏览器** | ✅ |
| **不允许自动下载**，必须用户点（延续 §21c） | ✅ 打开清单只读，探针实测 `downloaded == []` |
| `core/ai/model_downloader.py` 保持零 HTTP import | ✅ 5 个 `core/ai/*.py` 都被自检闸门扫 |
| `core/updater.check_for_update()` 先 GET `manifest.json` 拿 `app` 的 url/size/sha256 | ✅ §6.3 |
| 版本比较用 `core/version.CURRENT_VERSION` | ✅ 且 `current=` 可注入 |
| 原 Release 正文解析**保留但标 deprecated** | ✅ `parse_release_body()` / `latest_release()` |
| 下载主流程不变（临时文件 → 校验 → `update.json` → bat 替换） | ✅ §6.1 / §6.2 未动 |
| 硬约束：不引 requests/httpx/aiohttp、不接云端 SDK、必须 Range 续传 + SHA256、下载失败要有浏览器兜底、封板 sha256 硬校验保留 | ✅ 全部满足（parser.py 解冻后哈希已同步进 `main.FROZEN_FILE_SHA256`） |
| 自测：假 `ai_models.json` 放本地 http server 测勾选/下载/校验/写 `installed.json` | ✅ `tests/test_model_registry.py` 109 用例（只打 `127.0.0.1`） |
| 自测：坏 sha256 的假文件确认拒绝写入 | ✅ 同上 |
| 自测：德育分 30 行假数据含 3 行故意错，确认 errors.csv 正确 | ✅ `%TEMP%\tl_v19_ui_test.py`（27 行入、3 行出，三条理由各不相同） |
| 交付：跑 `tests/test_calc.py` 和新增 `tests/test_import.py` | ✅ 见 §18.2 全表 |
| 交付：**更新 AGENTS.md 新增 §30 模型中心、§31 批量导入** | ⚠️ §30 已被"激活码系统已删除"占用 → 实际落 **§31 / §32** |
| 交付：commit 带 **v1.9** 前缀，**不推 Gitee** | ✅ 提交 `e3b6e75`（当时按 Owner 要求没推）。**2026-10-10 Owner 改口"提交推送"，已推 master**；同一天按 **v1.8.1** 正式发版，`CURRENT_VERSION` 随之 1.8 → 1.8.1 —— 见 §16.5（提交号 ≠ 程序版本号；那次不升是"没有数据格式变化"，这次升是"发版需要，清单版本必须大于用户手上版本"） |

### 22.2 指令二：修复成绩导入解析器的表格干扰 Bug

| 原文要求 | 落地 |
|---|---|
| 识别并剔除"不及格门数 / 平均分 / 总分 / 所得学分 / 平均学分绩 / 平均学分绩点 / 排名"这类**统计列**，绝不能当成绩 | ✅ §13.6 第 1 行（`is_stat_header` + `STAT_HEADER_KEYWORDS`） |
| 过滤末尾统计行：含"人"（如"37人"）、"不及格"、纯汇总数值的行 | ✅ §13.6 第 2 行（`SUMMARY_ROW_KEYWORDS` + `_SUMMARY_COUNT_RE`） |
| 跳过表格顶部合并单元格大标题 | ✅ §13.6 第 3 行（`_row_is_title_like`，且**不吃表头扫描预算**） |
| 校验行完整性：必须有非空学号/姓名 + 合理成绩 | ✅ §13.6 第 4 行 + 原 §13.3 |
| 缺失关键字段的行**直接丢弃并记录日志** | ✅ 每处跳过都写 `ParseResult.warnings`（中文理由），列号进 `stat_columns`、行号进 `skipped_rows` |
| 硬约束：复用 `core/excel_io.py` 魔数选引擎，不重写读取 | ✅ `**read_excel_kwargs` 扩展点没动（§7.4） |
| 硬约束：不引新的第三方联网/解析库 | ✅ |
| 硬约束：**保持封板 sha256 硬校验，若改动需同步 `main.py` 里的哈希** | ✅ 改了 `core/parser.py` → 同步 `main.FROZEN_FILE_SHA256`（这是 parser 唯一一次解冻） |
| 自测：用含三类干扰的真实表/fixture 验证；跑通 942+ 测试全绿；打包后复测 | ✅ `tests/test_parser_filters.py` 49 例 + §18.2 全量；**frozen 复测见下** |
| 交付：改解析代码 + 测试用例 + commit（含修复说明） | ✅ |

> ★ **两段指令的收尾要求**："最后合并上述开发提示词到开发总纲里面" ——
> 就是本节。以后同类要求一律**并进本文**，`docs/` 只允许白名单里那四个文件（§0.2）。

## 二十三、施工细则的完整事故记录（AGENTS.md 各节"→ §二十三 N"的落点）

**这一节是 AGENTS.md 的档案柜**：施工细则里只留"规矩 + 一句为什么"，
事故现场、原始报错、实测数字、推导过程全部放这里。编号与 AGENTS.md 的
`→ §二十三 N` 指针一一对应（N = AGENTS.md 那一节的号）。

### 23.1 §1b 的完整命令与当时的报错原文

改完 `.ps1` 必须跑的两件事（照抄 `build_exe.ps1` 里现成的那一段）：

```powershell
# ① 补 BOM：头 3 个字节不是 EF BB BF 就补上（正文不动，只加 3 个字节）
$p = 'build_exe.ps1'
$b = [IO.File]::ReadAllBytes((Resolve-Path $p).Path)
if (-not ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)) {
    [IO.File]::WriteAllBytes((Resolve-Path $p).Path, ([byte[]](0xEF,0xBB,0xBF) + $b))
}

# ② 语法检查：不要靠"跑一下试试"，那要等几分钟
$err = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $p).Path, [ref]$null, [ref]$err)
if ($err.Count) { $err | % { "L$($_.Extent.StartLineNumber): $($_.Message)" } } else { "OK" }
```

**现场**：用 `edit` / `write` 工具改过 `build_exe.ps1` 之后，脚本在 PS 5.1 下
一次报出 **20 个解析错误**（`Unexpected token '{'` / `Missing closing '}'` 一类），
而文件在编辑器里看着完全正常。根因是无 BOM → 按 GBK 解码 → 中文错位 →
错位的尾字节吃掉相邻的 `{` `}` `\`。这是**第二次**踩同一个根因
（第一次是 `.bat` 里的中文注释，见 §23.12 / AGENTS.md §26b）。

### 23.2 §4c 三次"断言全绿、界面上却是坏的"事故

| 现象 | 真因 |
|---|---|
| 登录窗「忘记密码？」按钮看不见 | 登录窗写死 `820x500`，内容实际需要 639px |
| 表格「操作」列的按钮只剩半个字 | 行高 30px < 容器边距 12 + 按钮最小高 26 |
| 侧写辅导页整页空白 | 透明度效果缓存未失效（见 AGENTS.md §4b） |

三次都是**控件都在、文字都对、几何也对**，只有截图看得见。
所以判据是 `%TEMP%\tl_qt_drive.py`（真的截图 + 量像素），不是静态断言。

### 23.3 §4d 打包判据：源码自检绿**证明不了**包是好的

`--selfcheck` 跑的是**源码**，打包出来的 exe 坏掉它一条都报不出来。
唯一判据是 `dist\transcript-lens.exe --selfcheck` 与 `--bootcheck`
（`build_exe.ps1` 两个都跑，非 0 直接判失败）。

* 现场一：`--exclude-module PySide6`（tkinter 时代遗留）→ 整个 Qt 被排除 →
  exe 一启动就 `ModuleNotFoundError`，而源码自检全绿。
* 现场二：`ui/styles.qss` 是**数据文件**，没 `--add-data` 进包 → exe 里
  窗口能开、界面全白。

### 23.4 §4k 逐步推导与当时现场（本项目最会骗人的 bug）

**症状**：双击 exe → 开屏 → 打招呼 → **然后就没了**。界面不出来、点任务栏
也没反应、**没有任何报错**（`--windowed` 连 stdout 都没有），退出码
`0xC0000409`（STATUS_STACK_BUFFER_OVERRUN）。

**定位过程**：① `%TEMP%\tl_boot_test.py` 三个真实时序里只有 `auto`
（真的走完欢迎页淡出）会炸 → 锁定在"欢迎页淡出 + 销毁"这条路径；
② 加 `print` 一个都不输出 → 是**进程级**消失，不是 Python 异常；
③ `faulthandler` 也拿不到东西 → 是 C++ 侧踩空；
④ 最终确认是**两步 use-after-free**：动画的 parent 就是它作用的那个窗口
（`QPropertyAnimation(window, b"windowOpacity", window)`）；回调里
`AppFlow.welcome = None` 丢掉窗口最后一个 Python 引用 → 窗口当场析构 →
动画跟着没；Qt 发完 `finished` 之后 `QAbstractAnimationPrivate::setState`
还要继续用那个动画对象（注销统一计时器、处理 `deleteWhenStopped`）→ 踩空。

**试过但无效的修法**：把清理槽排成 `finished` 上的**第一个**槽 —— 照样崩。

**有效的修法**：回调**延后一拍**，并且用 `singleShot` 的 context 重载
（窗口先一步没了，这次回调会被自动跳过，不会去碰死对象）：

```python
anim.finished.connect(lambda: QTimer.singleShot(0, window, on_done))
```

**为什么 400 多项测试一条都没抓到**：那条路径**从来没被跑过** ——
测试为省 1.5 秒一律 `skip_to_end()`，这个 bug 只在"欢迎页**真的**淡出"
时才成立。所以两道闸门都别再拆掉：`%TEMP%\tl_boot_test.py`（`auto` /
`login` / `switch` 三个真实时序，每个**单独进程**，退出码即断言）和
`transcript-lens.exe --bootcheck`。

### 23.5 §4l 启动慢各变体的完整实测表

每个变体**单独起进程**测（排除"第一次创建原生窗口"那约 165ms 的账）：

| 变体 | `show()` 耗时 |
|---|---|
| `QSplashScreen` | 1028 ~ 1035 ms |
| 自绘 `QWidget`（现在用的） | 164 ~ 167 ms |

**差 865ms**，而且跟半透明 / 无边框 / 透明度**全都无关**。
开屏标题里的 📊 一个字 **241.7ms**（去掉只要 0.2ms）—— emoji 要现场加载
Segoe UI Emoji 做彩色字形合成；**已映射窗口**里的 emoji（侧栏 / 页标题）
不占启动时间。剩下的 **765ms 全黑**缘于开屏**先发 finished 再淡出**，
现在改成**先叫下一个窗口、再淡出自己**（`ui/splash.py._start_fade_out`，
打点脚本 `%TEMP%\tl_splash_trace.py`）。

启动到主窗可见：**6681ms → 3055ms**（`%TEMP%\tl_startup_time.py`）。

**预热三条规矩的现场**：第一版在 `run()` 之后紧接着做 600ms 同步预热，
结果**淡入动画冻在第一帧**（用户看到"点了没反应"）→ 改挂 `on_shown`；
第二版"干完活再计时"，欢迎页实际停留 `HOLD_MS + 600ms`（白等 600ms）
→ 改成 `HOLD_MS - spent`。因为扣时间，那 1500ms 里**含**主窗构造的 ~600ms，
不是 1500 + 600。

### 23.6 §8 打包后重新打包被文件锁挡住

报错原文：

```
PermissionError: [WinError 5] 拒绝访问: '...\dist\transcript-lens.exe'
```

onefile 的 exe 启动时会**再起一个子进程**（bootloader + 真正的应用）。
只 `Stop-Process` 父进程的话，子进程还活着、exe 文件还锁着，下一次打包就炸。
必须**按进程名**杀 + 等句柄释放：

```powershell
Get-Process -Name 'transcript-lens*' | Stop-Process -Force
Start-Sleep -Seconds 3
```

`build_exe.ps1` 的自检调 `--selfcheck` 跑这个 exe，自己不会锁文件 ——
**锁文件的只可能是手动启动的那一份**。另外：新建出来的 exe 第一次启动会被
Defender 全盘扫描，冷启动可能比之后慢好几倍，别把它当性能基准。

### 23.7 §13 五条闸门的完整抄录（`main._network_policy_report`）

1. **静态扫 `core/ai/*.py`**：源码里出现任何
   `import requests / httpx / urllib.request / socket / http.client / openai / anthropic`
   就 FAIL（正则只认真正的 import 语句，不认注释里提到的名字）。
2. **拿脏数据调一次 `redact_student`**：姓名 / 学号 / 身份证漏出去就 FAIL。
3. **打包里非白名单联网库**仍然不该在包里 —— 报错文案是
   "AI/更新都还是骨架，是不是有人偷偷加了依赖"，不再是"违反铁律"
   （这条只能查打包版，源码版什么都查不到）。
4. **更新检查三条**（`_updater_policy_lines`）：只用 stdlib（出现
   `requests/httpx/aiohttp/urllib3/openai/anthropic` 即 FAIL）/
   **import 期零网络**（把 `urllib.request.urlopen` 换成炸弹再重新 import
   一遍模块）/ `DEFAULT_TIMEOUT == 5`。
5. **更新下载三条**（`_download_policy_lines`）：只用 stdlib /
   源码必须含 `def download_parts(` 与 `ATTACHMENT_LIMIT_BYTES` /
   真跑一次失败下载，断言目标文件逐字节未变。

**为什么守卫要落在行为上**：原来那条"打包里见到 requests 就 FAIL"在有条件
放行之后语义已经错了，而且它只能查打包版。静态 grep 抓不住间接调用，
所以第 4 条用"重新 import"这种**行为**验证。

### 23.8 §17 两个色值与那句推导

带对照组校准的实测（`%TEMP%\tl_clean_probe.py`）：挂着 `QGraphicsOpacityEffect`
@1.0 与摘掉效果对比，**17.4% 的像素不同**、最大通道差 **240**；
同一坐标一边是 `#0f173b`、另一边是 `#ffffff`。

推导：挂上效果 → Qt 把整页渲染进**一张离屏 pixmap** → 那条路径上
**文字的光栅化方式不一样**（没有 ClearType，字形次像素取整也不同）→
动画跑完摘掉效果 → 整页**重新光栅化一遍** → 所有字都换了个画法，
视觉上就是"落定之后抖一下"。

顺带修掉的真竞态：原写法在 `setCurrentIndex` **之后**才
`set_opacity(new, 0.0)`，实测中间有 **6.27ms** 的窗口是新页已 current
但**没有效果** —— 任何一次落在那个窗口里的 paint 都会画出全不透明的一帧。

判据脚本 `%TEMP%\tl_cover_verify.py`：**落定后再等 1 秒，两帧必须逐像素相同**。

### 23.9 §19 引文出处的完整铺陈

总纲 §1.1 收录的原话：**"必修、限选、选修是三个完全独立的类别，绝不混同"**、
**"任何将选修归入限选的逻辑都是 bug"**；§一 的十节速查表里也单列了这一条。

界面侧的根因：上一版文档把"选修"写成"归限选"却**没人发现**，
因为按钮上只写"全部"，没人知道"全部"含不含选修 —— 所以现在按钮气泡、
状态行、表下说明三处都要把"这一档算了哪几类"写在明面上。

行为门样例（`main._category_policy_report()` 真算一遍）：

    高等数学(4学分)必修 = 80 ｜ 专业英语(2学分)限选 = 90 ｜ 音乐鉴赏(1学分)选修 = 100
    口径① 600/7 = 85.714286    口径② 500/6 = 83.333333    口径③ 320/4 = 80

验牙脚本 `%TEMP%\tl_category_gate_test.py`：把
`CalculationScope.REQUIRED_LIMITED.categories` 临时改成含选修 →
三条 FAIL 同时报出来（跑完按字节还原）。

### 23.10 §22b 为什么两个键都要写

键名在两份文档之间**对不上**：`__vault_version__`（总纲 §12.3 的值）与
`vault_version`（任务书 2.1 / 旧 v1.8 charter §1.1 两次给的值）。
让实现去猜哪边赢的风险是"某次真迁移把标记写到一个启动检测**读不到**的键上"；
成本只是两个 JSON 字段，收益是"谁读都对"。读取时**哪个有值认哪个**，
`--selfcheck` 断言两键都在且相等。

### 23.11 §23a 三处写死版本号的具体位置与症状

| 位置 | 症状 |
|---|---|
| `ui/login_window.py` 的 `APP_VERSION` | 登录窗左栏永远显示 `v1.0` |
| `ui/splash.py` 的 `version` 默认参数 | 开屏版本号永远 `v1.0` |
| `ui/pages/account_page.py` 的"关于"表 | 个人中心永远写着 `v1.0` |

三处**全都不报错**，只是一直在说假话（程序是 1.8、界面写着 1.0）。
**静态 grep 查不出来**：`ui/anim.py` 的注释里合法地写着"旧 charter v1.6
第 5.2 节"、`ui/pages/settings_page.py` 的文档字符串提到
`docs/DEV_SUMMARY.md`，一刀切地搜 `1.0` / `1.6` 全是误报 —— 所以闸门走
`ast`（只看字符串字面量、显式跳过文档字符串）。

验牙：往 `ui/splash.py` 注入一处 `version: str = "v1.0"`，立刻 FAIL 并指名
`ui\splash.py:183`（`%TEMP%\tl_v18_gate_test.py`）。

### 23.12 §26b 那条 `.bat` 的完整经过

第一版 `rem` 注释写了中文，被自己的断言逮住 —— 这不是洁癖：`cmd.exe` 按
**当前代码页逐字节**解码 bat，中文按 GBK 解错位后，错位配对的尾字节范围
（`0x40~0xFE`）正好包含 `"` 和 `\`，会把后面那句
`copy /Y "%SRC%" "%DST%"` 的**引号吃掉**，报出来的错完全看不出根因。
和 §23.1（`.ps1` 的 BOM）是同一个根因。

配套的两条：**路径不写进 bat**，走命令行参数 `%~1..%~4`（参数走 Windows 的
UTF-16 API，不受代码页影响）；复制要重试（`for /L %%i in (1,1,60)` 每秒重试，
60 次全失败 `goto :giveup`，旧 exe 原样留着）。

### 23.13 §30 删掉的四个文件 / 入口与三层判据的完整覆盖范围

* **文件层**：`core/activation.py`（675 行）、`config/activation.example.json`
  已删除；老机器上可能残留 `%LOCALAPPDATA%\SGMS\activation.enc` ——
  **它是惰性的**，没有任何代码读它，也**不许**写"启动时顺手删掉"那种代码。
* **入口层**：设置页那张激活卡片、主窗顶栏「● 已激活」小圆点已删除。
* **AST 源码层**：`core/` `ui/` `ai/` 里 0 处激活码标识符；`ui/` 的**界面文案**
  0 处「激活」（运行时遍历 `%TEMP%\tl_qt_drive.py`：主窗与登录窗全部控件文字、
  占位符、气泡）。
* **接线层**：`main.CHECK_MODULES` 里不出现 activation 相关模块。

用 `ast` 而不是 grep `activ` 的原因：`ui/pages/scholarship_page.py` 里有
openpyxl 的 `book.active`，一刀切地搜必然误报 —— **误报的闸门最后一定会被关掉**。
判据：`main._removed_gate_report()` + `%TEMP%\tl_noact_gate_test.py`
（24 项，注入违规会 FAIL，跑完逐字节还原）。
### 23.16 被删掉的叙述与实测原文（第 11 批：4k–25b）

**§4k 症状+推导**

症状：双击 exe → 开屏 → 打招呼 → **然后就没了**，界面不出来、点任务栏也没反应、
**没有任何报错**（`--windowed` 连 stdout 都没有），退出码 `0xC0000409`
（STATUS_STACK_BUFFER_OVERRUN）。

根因是两步 use-after-free：① 动画的 parent 就是它作用的那个窗口
（`QPropertyAnimation(window, b"windowOpacity", window)`）；② 回调里把窗口最后一个
Python 引用丢掉（`AppFlow.welcome = None`）→ 窗口当场析构 → 动画跟着没；
③ Qt 发完 `finished` 之后 `QAbstractAnimationPrivate::setState` 还要继续用那个动画对象
（注销统一计时器、处理 deleteWhenStopped）→ 踩空 → 进程直接消失。

**光把清理槽排到前面是不够的**（试过，照样崩）。

**为什么 400 多项测试一条都没抓到**：那条路径从来没被跑过 —— 测试为省 1.5 秒一律
`skip_to_end()`，这 bug 只在"欢迎页**真的**淡出"时才成立。

**§4l 实测**

实测（`%TEMP%\tl_splash_probe.py` / `tl_perf_bench.py`，带 `sleep(0.15)` 对照组校准）：
**跟动画时长、跟机器性能都无关**，是两笔一次性开销 —— `QSplashScreen` 慢 **865ms**
（164~167ms vs 1028~1035ms；每个变体单独起进程测，排除"第一次创建原生窗口"那笔；跟半透明 /
无边框 / 透明度全无关）；开屏标题里的 **emoji 一个字 241.7ms**（去掉只要 0.2ms，要现场加载
Segoe UI Emoji 做彩色字形合成）—— 只删开屏 pixmap 里的，**已映射窗口**里的 emoji（侧栏 /
页标题）不占启动时间；剩下 **765ms 全黑**缘于开屏**先发 finished 再淡出**，现在**先叫下一个
窗口、再淡出自己**（`ui/splash.py._start_fade_out`），量法见 `tl_splash_trace.py` 打点。

**§4l 预热**

第一版直接在 `run()` 之后跑预热：`run()` 只是把淡入动画 start 出去，那一刻窗口还一帧
没画，600ms 同步工作把淡入动画冻在第一帧 —— 用户看到"点了没反应"。第二版"干完活再计时"，
欢迎页实际停留 `HOLD_MS + 600ms`，白等 600ms。主窗构造约 600ms，`HOLD_MS` = 1500 里已经
含它。

**§5 两条**

为什么不能"发个新验证码就重置"：库密钥只在本地、只被密码包裹 —— 忘密码那天它已经解不
开了，"现在发一封邮件"发不出任何有用的东西。能解开它的只有**注册时**生成、并已用它包好
密钥的那一串码。个人中心「重新生成找回码」要再输一次密码，是因为生成需要库密钥，
顺带挡住"别人趁你没锁屏偷偷把找回码换成他的邮箱"。

**§5b**

直接 `sendmail()` 的后果：顺利时卡 1 秒；**网络不通时卡满 `SMTP_TIMEOUT` = 15 秒**，
期间窗口不重绘，Windows 可能弹"无响应"。回调仍在主线程执行，所以回调里碰控件是安全的。

**§6 举例**

代码与现象：`args = ["--mailcheck-send"]` 时 `"--mailcheck" in args` 是 **False**（列表的
`in` 比的是整个元素）→ 匹配不上任何分支 → 掉进最后的 GUI 分支把窗口开起来 → 命令行下表现
是「卡死、一个字都不输出、退出码永远拿不到」，而且源码和打包版**一样卡**，极难往参数解析
上想。

**§7**

第一版把报告攒到最后一次性写盘，于是"卡住"这个最需要诊断的情况恰好什么都看不到（实测就是
靠 `emit()` 定位到"根本没进 mailcheck 分支"）。

**§8**

onefile 的 exe 启动时会再起一个子进程（bootloader + 真正的应用），所以"按 PID 杀父进程"
杀不干净：子进程还活着，exe 文件还锁着，下一次打包立刻炸。必须按进程名杀
（`Get-Process -Name 'transcript-lens*' | Stop-Process -Force`），并等 3 秒让句柄释放。
报错原文：

```
PermissionError: [WinError 5] 拒绝访问: '...\dist\transcript-lens.exe'
```

**§9 四条**

上次手写表头漏了一个口径列，整体错位一格，「侧写」按钮正好压在「红标原因」的单元格上 ——
所以列下标必须从 `COLUMN_TITLES` 推导，不许出现手写元组。

**§10 旧规格**

旧规格（v1.3 Spec 与 v1.6 / v1.7 / v1.8 三份 charter）已全部合并进总纲并删除。旧的
「61 / 246 行」、「5.2 / 第 3 章 / 第 4 章」这类编号一律不再引用 —— 只认 `docs/DEV_SUMMARY.md`。

**§10b**

旧 charter 6.4 当年写的是"三个独立 Toggle：必修 / 限选 / 选修，可多选组合"（现在记在
总纲 §1.3）。按字面做成任意组合是做不到的：能算出什么数由 `CalculationScope` 决定
（`core/calculator.py`，**铁律一冻结**），`{限选}`、`{选修}`、`{限选,选修}` 没有对应口径；
要支持它们只能改封板文件（违反铁律一）或在界面里自己乘一遍加权（违反铁律五）。上一版把
"选修"写成"归限选"却没人发现，根因就是**按钮上只写"全部"**，没人知道"全部"含不含选修。

**§11 阈值**

它一度因为一次 **1513ms** 的抖动被调到 2500 —— 但那是**旧的 420ms 动画 + 首帧绘制重页面**
造成的。动画改成 0+130 之后一次切页中位 ~137ms，1500ms 已经是 10 倍余量。真正会出问题的
是"动画本来会正常结束、却被看门狗砍掉"，所以宁可宽不可紧。

**§12 规矩**

① 这轮有过一次所有数字都是 0.0ms 的假测量（没带对照组），差点据此改了错误的代码 ——
所以每次先跑 `time.sleep(0.2)` 确认量到的是 200ms 左右。② `tl_boot_test.py` 是被"写死
等待"坑坏的：这轮把链上每个时长都改了，所有 `pump(1600)` 式断言同时变成假绿或假红。

**§13 第1条**

五条判据的原文：

1. **静态扫 `core/ai/*.py`**，源码里出现任何
   `import requests/httpx/urllib.request/socket/http.client/openai/anthropic`
   就 FAIL（正则只认真正的 import 语句，不认注释里提到的名字）；
2. **拿脏数据调一次 `redact_student`**，看姓名/学号/身份证会不会漏出去；
3. 非白名单联网库**仍然**不该在包里 —— 但报错文案改成
   "AI/更新都还是骨架，是不是有人偷偷加了依赖"，不再是"违反铁律"；
4. **更新检查**三条（`_updater_policy_lines`，见第 21 节）；
5. **更新下载**三条（`_download_policy_lines`，见第 26 节）。

**§14 算术**

智育分是导入时 `core.calculator` 用口径①算好、存进 `class_{班级号}.json.enc` 的；
`core/scholarship.py` 只做"乘 0.7 / 加 德育×0.3 / 排序 / 截名额"—— 那几步是**奖项规则**的
算术，总纲 §9.1 给了公式，不算"自己发明算术"。

**§25b**

实测（2026-09-26）：张三高等数学 91 改成 101 之后 ——

```
口径② 从 85.052632 → 82.928571      （手算 1161/14 —— 5 个学分凭空少了）
total_credit 从 19 → 14
course_scores 里**仍然列着** ('高等数学', 101.0, 5.0)
```

后果是两边不一致：智育加权把那门课当没算，而奖学金特等奖「每门 ≥90」判的是
`score is not None`，于是**一个 101 的笔误会算作达标**。

### 23.17 被删掉的叙述与实测原文（第 11 批：16–33 节）

**§16**

意愿、德育分这类数据如果按密码级加密，自动登录进来就全是空的（库密钥解不开，只有
session token）。

**§18 信号**

刷新失败为什么只打日志不弹窗：数据已经存进去了，某一页没刷新出来是显示问题，
不该打断用户看成绩。

**§19 行为门**

验证方式：把 `CalculationScope.REQUIRED_LIMITED.categories` 临时改成含选修，三条 FAIL
会同时报出来（`%TEMP%\tl_category_gate_test.py`）。

**§19 遗留**

冻结的 `core/parser.py` 把"专业选修"归到限选，是因为多数培养方案的"专业选修课"就是限选课，
属校名别名；命中数打平时按 `必修 > 限选 > 选修` 取先出现的。真要改是 Owner 的事。

**§20b 代码**

两种写法的差别（第一版就是 ⛔ 那种）：

```python
self._archive = build_archive(parsed, self.term, source=name)                  # ⛔
self._archive = build_archive(parsed, self.term, archive=self._load_archive(),
                              source=name)                                     # ✓
```

**§20c**

第一版 `tl_semester_test.py` 就是因为没先 `store.register()` 建账户，"档案落盘"整条断言
假红。

**§21 判据**

验证方式：往 `core/updater.py` 里真注入违规，三条守卫都要报 FAIL，跑完按字节还原。

**§21b 状态**

四种状态各说什么话：

| 状态 | 界面 |
|---|---|
| `newer` | "发现新版本 vX，是否前往下载？" |
| `current` | "当前已是最新版本" |
| `no-release` | "…还没有发布任何发行版"（**接口通了**，只是没东西可下） |
| `error` | "检查失败，请手动访问仓库" |

**§21c 说辞**

v1.7 那版的原文是"绝不自动下载、绝不自动替换，只打开浏览器"—— 已被总纲 §四/§六 取代。

**§26b**

第一版 `rem` 注释写的中文被自己的断言逮住（`_apply_mechanism_report()` 要求 bat 全 ASCII）。

**§28**

这是"两条规则都不用让步"的写法：`core/ai/model_downloader.py` 只管"下到哪、叫什么、
装好没有"，网络那一步调 `core.updater.download_exe`。

**§29**

不进仓库的三条理由：1~3GB 进仓库是灾难；打包后程序目录常常只读；数据目录才是它的家。

**§29 4**

目录穿越的用例：`download(url, "../../evil")` 不该写到 models/ 外面去 —— `_safe_name()`
清洗目录名，`delete_model()` 再验一次"解析后仍在 models/ 里"。

**§30 为什么**

Owner 的决定：注册登录 + 邮箱找回密码已经跑通，登录即全部功能可用，不再需要第二道闸门。
删掉的四个文件 / 入口：`core/activation.py`（675 行）、`config/activation.example.json`、
设置页那张卡片、主窗顶栏「● 已激活」小圆点。

**§30 判据**

三层判据各管什么：文件层查激活码的残留文件与入口；AST 源码层查 `core/` `ui/` `ai/` 里的
激活码标识符（不用 grep `activ`，因为 `ui/pages/scholarship_page.py` 里有 openpyxl 的
`book.active`，一刀切搜必然误报）；`CHECK_MODULES` 接线层查自检模块有没有真的挂上。

**§31 三**

`tl_v19_ui_test.py` 桩掉了 `fetch_model_list` / `merged_with_local` / `download_exe`，
断言仍旧量像素（不只看控件在不在）。

**§33**

为什么不给自动清理：删用户数据必须是用户主动的动作（与第 30 节第 4 条同一条道理）。

### 23.15 被搬走的过程、推导与量法（同上，只留规则与判据的现场）

**§4a 代码块**

代码块原文：

```python
# ⛔ 卡片挂了阴影，就不要再让它淡入淡出
card.setGraphicsEffect(QGraphicsDropShadowEffect(...))
fade_in(card)          # 会抛 TypeError（ui/anim.py 故意拦下来）
```

`ui/anim.py` 的 `fade_in` 是故意拦下来的 —— 挂不上去比默默不生效好。

**§4c 三次事故**

三次"断言全绿、界面却是坏的"：

| 现象 | 真因 |
|---|---|
| 「忘记密码？」按钮看不见 | 登录窗写死 `820x500`，内容需要 639px |
| 表格「操作」列按钮只剩半个字 | 行高 30px < 容器边距 12 + 按钮最小高 26 |
| 侧写页整页空白 | 透明度效果缓存未失效（见 4b） |

量法一律是截图 + 像素比对（`%TEMP%\tl_qt_drive.py`），不是断言控件存在。

**§4d 起因**

两个坑的完整形状：

1. `build_exe.ps1` 里那条 `--exclude-module PySide6` 是 tkinter 时代的遗留。换成 PySide6
   之后忘了删，PyInstaller 把整个 Qt 排除 → **打出来的 exe 一启动就
   `ModuleNotFoundError`**，而源码自检全绿（自检跑的是源码，不是包）。
2. `ui/styles.qss` 是**数据文件**，必须 `--add-data` 进包，否则 exe 里窗口能开、界面全白。

判据是"打完包真的把 exe 跑起来"（`build_exe.ps1` 跑 exe 的 `--selfcheck` + `--bootcheck`），
不是看源码自检绿不绿。

**§4g 代码对照**

PySide6 的 `Qt.Edge` 继承 `Flag` 而不是 `IntFlag`，`int()` 直接抛 `TypeError`。
这个分支鼠标每动一下都会走到，于是异常从 `eventFilter` 里冒出来，Qt 只打一行
`Error calling Python override of QObject::eventFilter()` 就把它吞掉 ——
界面上表现是"边框光标永远不变"，日志里是一句看不懂的 TypeError。

**§4h 起因**

**为什么 76 项冒烟全没抓到**：那些用例都直接调背后的纯函数，不产生真实鼠标事件 ——
`eventFilter` 那条路径根本没被走过。所以 `tl_qt_drive.py` 里加了一步
`QTest.mouseMove()` 扫过四边四角，专门逼 `eventFilter` 跑一遍。

**§4i 举例与括号**

⛔ 六个单选里当前亮着"考研"，切到"还没选意愿的学生"时：

```python
for option, radio in buttons.items():
    radio.setChecked(option == "")        # "考研" 依然是亮的
```

用户看到的是"状态栏写着还没有选意愿、按钮却亮着上一个学生的志愿" —— 像串号，
其实是互斥组不肯取消当前项。

**§4j 括号**

测法：1900×1200 / 150% DPI，真开真关整窗阴影各跑一次取中位 —— 阴影开 523ms、关 483ms
（**+8%**）。Qt 缓存了效果的源 pixmap，所以淡入淡出不会每帧重新模糊整窗；会的话是 +1200ms
那一档。

**§5 两条**

**为什么"发个新验证码就重置"不行**：库密钥只在本地、只被密码包裹。忘密码那天，它已经
打不开了 —— 这时"再发一封邮件"发不出任何有用的东西。能解开它的只有**注册时**生成、
并当场用它包好库密钥的那一串 24 位码。**这个时序是命门**，任何"优化"都不能改它。

**§5b 举例**

`run_async` 是把阻塞调用挪出主线程的**唯一入口**（`ui/workers.py`）。直接在按钮回调里
`sendmail()` 的后果：顺利时卡 1 秒；**网络不通时卡满 `SMTP_TIMEOUT` = 15 秒**，期间窗口
不重绘，Windows 可能弹"无响应"。回调仍在主线程执行，所以回调里碰控件是安全的。

**§6 举例**

⛔ 踩过的写法：

```python
if "--mailcheck" in args:
    return run_mailcheck(send_test="--mailcheck-send" in args)
```

`args = ["--mailcheck-send"]` 时 `"--mailcheck" in args` 是 **False**（列表的 in 比的是
整个元素）→ `--mailcheck-send` 匹配不上任何分支 → 掉进最后的 GUI 分支把窗口开起来 →
命令行下表现是「卡死、一个字都不输出、退出码永远拿不到」。

**§7 长句**

第一版把报告攒到最后一次性写盘，于是"卡住"这个最需要诊断的情况恰好什么都看不到。
改成 `emit()` 每步立刻落盘 + `flush` 之后，一眼就能看出停在哪一步（实测就是靠它定位到
"根本没进 mailcheck 分支"）。

**§8 背景**

onefile 的 exe 启动时会再起一个子进程（bootloader + 真正的应用），所以"按 PID 杀父进程"
杀不干净：子进程还活着，exe 文件还锁着，下一次打包立刻炸。按进程名杀才干净。

**§11 阈值来历**

它一度因为一次 **1513ms** 的抖动被调到 2500 —— 但那是**旧的 420ms 动画 + 首帧绘制重页面**
造成的。动画改成 0+130 之后一次切页中位 ~137ms，1500ms 已经是 10 倍余量，够宽了。
真正会出问题的是"动画本来会正常结束、却被看门狗砍掉"，所以宁可宽不可紧。

**§13 第1条**

五条判据的原文：

1. **静态扫 `core/ai/*.py`**，源码里出现任何
   `import requests/httpx/urllib.request/socket/http.client/openai/anthropic`
   就 FAIL（正则只认真正的 import 语句，不认注释里提到的名字）；
2. **拿脏数据调一次 `redact_student`**，看姓名/学号/身份证会不会漏出去；
3. 非白名单联网库**仍然**不该在包里 —— 但报错文案改成
   "AI/更新都还是骨架，是不是有人偷偷加了依赖"，不再是"违反铁律"；
4. **更新检查**三条（`_updater_policy_lines`，见第 21 节）；
5. **更新下载**三条（`_download_policy_lines`，见第 26 节）。

**§14 特等**

特等奖三条口径的由来：① 门槛"每门 ≥ 90"判的是**原始成绩**，不是加权分；② **不占名额** ——
拿特等的人从名额池里移出去、后面的人往前补，否则一个班会出现"特等占掉一个一等名额"；
③ **默认不叠加** —— 勾了"允许叠加"才显示成 `特等+一等`，统计时**两边都数**（既算特等
人数、也算一等人数）。

**§17 4c 精简**

竞态的量法：在两个调用之间打点，实测存在 **6.27ms** 的窗口 —— 新页已经 current，但
`QGraphicsOpacityEffect` 还没挂上。任何一次 paint 落进这个窗口就会画出全不透明的一帧
（闪白）。改成先挂 0 再换页之后窗口消失。

**§19 真源句**

为什么真源只有两处：`core.parser.Category` 决定"一门课属于哪一类"，`CalculationScope.
categories` 决定"这一档口径允许计入哪几类" —— 除了这两个地方，代码里任何一处再判一次
类别，都是在制造第三个真源，迟早与这两处漂移。

**§26b 根因**

现场：第一版 bat 的 `rem` 注释写了中文，被 `main._apply_mechanism_report()` 的"全 ASCII"
断言当场逮住。届时的报错长得像 `copy /Y ... was unexpected at this time`，与中文注释**看
不出任何关系** —— 和 §1b（`.ps1` 少 BOM）是同一个根因：GBK 尾字节范围 `0x40~0xFE` 恰好
覆盖 `{` `}` `\` `"` 这些 ASCII 字符。

**§29 理由**

为什么不放仓库 `models/`：单模型 1~3GB 进仓库是灾难；打包后程序目录常常只读（装到
`Program Files` 时根本写不进去）；数据目录才是用户数据的家，也才能被备份/迁移逻辑一并
照顾到。

**§33 说明句**

为什么内存探不到要放行：探测失败的机器如果一律判 hard，界面上一条模型都勾不上 —— 闸门
变成了"看谁运气好"。反过来「显存未知」只按 0.7 倍判紧，因为显存与能不能加载关系更直接。

**§35 说明句**

为什么不做真 sqlite：红线 3 要求明文成绩任何时刻都不落盘，而 sqlite 的正文、WAL、临时
文件都是明文；逻辑表 API 保留"表名 / 字段 / 删除语义"这层手感，底层仍走应用级 `.enc`。

### 23.14 AGENTS.md 后半批搬来的现场

这一小节收 AGENTS.md 后半批瘦身时从正文里拿掉的"现场"，编号仍指 AGENTS.md 的那一节。

**§4c 第 4 条（截图与量像素）**：三次事故（登录窗「忘记密码？」被写死的 `820x500`
裁掉、表格「操作」列按钮只剩半个字、侧写页整页空白）全都是"控件都在、文字都对、
几何也对"，只有截图看得见 —— 所以判据是 `%TEMP%\tl_qt_drive.py` 真的截图 + 量像素，
不是静态断言。

**§4e 第 1 条（无边框窗必须显式透明）**：`styles.qss` 里那条 `QMainWindow { background }`
会把**整个窗口矩形**填成不透明 —— 连留给阴影的那圈边距也一起填，于是四个圆角外面
变成灰白方块，"无边框圆角"根本看不出来。补一条
`QMainWindow#FramelessWindow { background: transparent; }` 才修得掉。

**§10b（"全部"到底含不含选修）**：上一版文档把"选修"写成"归限选"却没人发现，
根因是**按钮上只写"全部"** —— 没人知道"全部"含不含选修。现在按钮气泡与状态行读
`scope_breakdown()`（`CalculationScope.categories`）、表下常驻说明用 `CALC_SCOPE_NOTE`，
三个位置都能一眼看出来。行为门见 §23.9。

**§11（看门狗只放锁的后果）**：只放锁不行 —— 动画还在跑，用户再点侧栏就是两段动画
抢同一个 opacity，界面会停在"侧栏亮着 A、内容还是 B"且**永不自愈**。阈值
`LOCK_TIMEOUT_MS` = 1500ms 的来历：原来因为一次 **1513ms** 的抖动被调到 2500，
但那是**旧的 420ms 动画 + 首帧绘制重页面**造成的；现在一次切页中位 **~137ms**，
1500ms 是 10 倍余量。量法与判据见 AGENTS.md §11。

**§12 两条量性能规矩的现场**：① 这轮有过一次**所有数字都是 0.0ms** 的假测量
（没带对照组），差点据此改了错误的代码 —— 所以每次先跑 `time.sleep(0.2)` 确认量到的
是 200ms 左右；"第一次创建原生窗口"约 **165ms**，在同一进程里连续测会把账算到第二个
变体头上，`tl_splash_probe.py` 就是为这个写成 `argv[1]` 选变体的。② `tl_boot_test.py`
是被"写死等待"坑坏的：这轮把链上每个时长都改了，所有 `pump(1600)` 式断言同时变成
假绿或假红 —— 一律改轮询 `wait_until(pred, timeout)`，确要断言"某件事发生过"就用
锁存器（spy/flag），不靠撞时机。

**§21b 四种状态的实测背景**：2026-09-27 实测，仓库公开可读（200），但 `/releases`
返回 `200 []`、`/releases/latest` 返回 **404** —— **一个发行版都没发过**。所以只打
`latest` 的话，在发布第一个发行版之前这功能永远只能报"检查失败"，而那不是实话。
另外：**列表非空但读不出版本号** ≠ "没有发行版"，第一版把这两种混成一种，界面上会说
"仓库里还没有发布任何发行版"，而仓库里明明挂着东西 —— 那是句假话。

**§21c 被取代的那句话**：v1.7 时这里写的是"绝不自动下载、绝不自动替换，只打开浏览器"；
总纲 §四 把"更新下载"加进白名单（第六节给了完整的下载 → 重启 → 替换流程）之后，
那段话已被取代 —— 现在只有"静默/自动下载"和"热替换"两件仍然禁止。

**§22a 为什么要排除备份目录**：备份按总纲 §12.2 的路径放在数据目录**里面**，
不排除备份目录的话每迁一次目录就翻一倍。

**§22b 为什么两个键都要写**：键名在两份文档之间**对不上**（`__vault_version__` 与
`vault_version`），让实现去猜哪边赢的风险是"某次真迁移把标记写到一个启动检测读不到的
键上"；成本只是两个 JSON 字段，收益是"谁读都对"。读取时哪个有值认哪个。

**§22c 回滚范围的现场**：迁移全程只动过 `users.json` 一个文件，所以回滚**只**恢复它 ——
整目录回滚会把备份之后新写的数据一起盖掉，那才是真的丢数据。

**§23b 忘了补那一跳的报错原文**：

```
MigrationError: 没有从 v1.7 升级的路径（已知：1.0, 1.6, 1.7, 1.8）
```

v1.7 → v1.8 时实测踩到 —— `--selfcheck` 的迁移检查当场 FAIL 拦住了。补一跳只要三行
（哪怕什么数据结构都没改，也写个 `return data` 的空函数）。

**§26f 为什么盯着启动次序**：`run_pending_update()` 必须最先跑，结论可能是"马上退出、
让 bat 去替换 exe"；它自己**不碰 Qt**（越早退出，exe 文件锁松得越快）。
`run_startup_migration()` 可能弹模态框，所以必须早于开屏。自动化环境用
`TL_SKIP_UPDATE=1` 跳过（和 `TL_SKIP_MIGRATION` 同套路）。

## 二十四、v1.9 二期三块功能：现行规格与判据

施工细则（`AGENTS.md`）的 §33 / §34 / §35 三节都写着"详见总纲 §二十四" —— 就是这一节。
细则说"不许违反什么"，这里说"现在到底是怎么规定的、拿什么判、文案逐字是什么"。
**签名与字段一律以磁盘代码为准**（下表行号是 v1.9 二期收尾时复核的）。

### 24.1 模型中心增强（细则 §33）

**一句话**：清单里的每个模型按"本机跑它轻不轻松"分三档，默认**只勾轻松的**；内存不够的行
**整行不可选**；卸载只有用户主动点才会发生。

#### 24.1.1 负载三档

| 档 | 常量 | 界面词 | 配色 | 含义 |
|---|---|---|---|---|
| 绿 | `LOAD_EASY = "easy"` | 轻松 | `#16A34A` | 内存与显存都够 |
| 橙 | `LOAD_TIGHT = "tight"` | 勉强 | `#D97706` | 内存只到最低要求的 70% 以上 |
| 红 | `LOAD_HARD = "hard"` | 困难 | `#DC2626` | 内存低于 70% 那条线 |

常量都在 `core/ai/model_registry.py`：`LOAD_EASY` / `LOAD_TIGHT` / `LOAD_HARD`、`LOAD_LABELS`、
`LOAD_COLORS`。★ 配色只给**纯色值**，界面不用 emoji —— 细则 4l 量过 emoji 的启动代价。

#### 24.1.2 判据的唯一实现

```python
_load_level_of(model, have_ram, have_vram) -> str     # core/ai/model_registry.py:866
```

判定顺序（**别在界面里再写第二份**）：

1. `need_ram = float(model.min_ram_gb or 0.0)`，`need_vram = _required_vram_gb(model)`；
2. `vram_ok`：没声明 `minVramGb` → 真；否则要求显存**探到了**且 ≥ 需求；
3. `ram_ok`：`have_ram is None` → **真**；否则 `have_ram + 1e-9 >= need_ram`；
4. `ram_ok and vram_ok` → `LOAD_EASY`；
5. 内存**探不到** → `LOAD_EASY`（见 24.1.3）；
6. `have_ram + 1e-9 >= need_ram * RAM_TIGHT_RATIO` → `LOAD_TIGHT`；
7. 否则 `LOAD_HARD`。

`RAM_TIGHT_RATIO = 0.7`（`core/ai/model_registry.py:133`）。

对外两个入口：`load_level(model, system=None, *, ram_gb=None, vram_gb=None)`（:918）与
`load_note(model, system=None, *, ram_gb=None, vram_gb=None)`（:957）。
**探测最多一次**，而且**不声明显存的条目不启 `nvidia-smi`**（省一次子进程）。

#### 24.1.3 "不知道"不许写成"是 0"

* **内存探不到 → 一律放行**（`have_ram is None` 判 easy）。理由：判成困难会让探测失败的机器
  **一条都勾不上**，功能直接不可用。
* **"显存未知" ≠ "内存未知"**：显存探不到时按 `RAM_TIGHT_RATIO = 0.7` 判，不按"没有显存"判。
* `load_note()` 照实写：内存没探到时写「未探测到本机内存 / 需要 N GB，按最低要求估算」，
  **不写 `0 GB`**。

#### 24.1.4 默认勾选：只认"轻松"

```python
default_checked(models, *, ram_gb=None, vram_gb=None) -> list[str]   # :770
```

★ **语义在 v1.9 二期有意改过**：以前是"清单里标了 `defaultCheck` **且**内存够"，
现在是"**本机跑它轻松**"。清单里的 `defaultCheck` 降级成「推荐」角标 —— 一个模型不会因为
标了它就在非轻松状态下被预勾。细则第 31 节那句旧话已被 §33 取代。

`ModelChoice`（`core/ai/model_registry.py:1020`）上的对应属性：`checked_by_default`
（`load_level == LOAD_EASY`）、`load_text`、`load_color`、`runnable`（easy 或 tight）、
`selectable`（＝`memory_ok`）、`status_text`。

#### 24.1.5 两条闸门别搞混

| 字段 | 管什么 | 不满足时界面怎么表现 |
|---|---|---|
| `memory_ok`（内存闸门） | 能不能**跑** | 该行 `flags` 里**没有 `ItemIsEnabled`** —— 整行不可选，不只是不勾（`ui/model_center_dialog.py:610-611`） |
| `load_level`（档位提示） | 跑起来**顺不顺** | tight 仍可勾；hard 行下载按钮置灰 + 气泡说明 |

内存闸门的兜底规则（`check_memory(model, *, ram_gb=None)`，:746）：`need <= 0` → 放行并说明
「这个模型没有声明内存要求」；探不到本机内存 → 放行并说明「没测到本机内存，先放行（装之前
自己看一眼内存）」。★ **`minRamGb` 写成 `"8GB"` 要认得出**；认不出必须给中文警告，
**不能退化成 0**（`need <= 0` 就放行等于没闸门）。

#### 24.1.6 卸载、重试、兜底

* **卸载只许用户主动点**：确认框文案逐字为
  `UNINSTALL_CONFIRM_TEXT = "卸载后将无法使用 AI 功能，确定？"`（`ui/model_center_dialog.py:116`，
  注释写明"一个字都不许改"）；确认后 `delete_model()` + `forget_installed()`。
* **绝不自动下载**；下载失败**重试 1 次**，仍失败 → 标红 +「复制链接」兜底（`_on_copy_link`）。
* 装没装好**以磁盘为准**：`installed.json` 只是账本，`merged_with_local()` 让磁盘上真有的
  `.gguf` 优先；写账本的门 = `size` 差在 `SIZE_TOLERANCE`（0.01，下限 4096 字节）以内**且**
  sha256 对得上。

#### 24.1.7 机器探测：`core/system_info.py`（零第三方依赖）

| API | 说明 |
|---|---|
| `memory_status() -> tuple[float, float] \| None`（:105） | `ctypes` 调 `GlobalMemoryStatusEx`（`_MEMORYSTATUSEX`，:84），返回（总量 GB，可用 GB） |
| `total_ram_gb()`（:129）/ `available_ram_gb()`（:139）/ `logical_cpu_count()`（:145） | 单项读数 |
| `gpu_infos(*, timeout=GPU_TIMEOUT_S)`（:221） | 起 `nvidia-smi`（`GPU_EXE`，:65；`GPU_TIMEOUT_S = 5.0`）查 `name,memory.total` |
| `discrete_gpu_vram_gb()`（:248） | 独显显存（GB）；没有独显给 `None` |
| `snapshot(*, force=False, probe_gpu=True)`（:332） | 带缓存的整机快照 → `SystemInfo`（`total_ram_gb` / `available_ram_gb` / `cpu_count` / `gpus` / `probed_gpu`） |
| `clear_cache()`（:359） | 清缓存（测试用） |
| `describe_system(*, probe_gpu=True)`（:365） | 给人看的一句话 |

**本机实测**（v1.9 二期收尾时）：`describe_system()` =「内存 15.4 GB，可用 4.4 GB，
24 个逻辑核，NVIDIA GeForce RTX 5070 Ti Laptop GPU（11.9 GB 显存）」；`snapshot()` =
`total_ram_gb = 15.376502990722656`、`available_ram_gb ≈ 4.4 ~ 4.6`（会浮动）、`cpu_count = 24`、
`gpus = (GPUInfo(name="NVIDIA GeForce RTX 5070 Ti Laptop GPU", vram_gb=11.9404296875, source="nvidia-smi"),)`。
★ 显存 11.94 GB = **12227 MiB**。

#### 24.1.8 判据

`tests/test_model_registry.py`（109 用例：清单解析、内存闸门、三档判定、账本合并）+
`tests/test_model_center_enhanced.py`（47 用例：默认勾选语义、困难行不可选、卸载确认、
失败重试与复制链接）+ `%TEMP%\tl_v19_ui_test.py`（假 `fetch_model_list` /
`merged_with_local` / `download_exe`，并且**量像素**）。

### 24.2 AI 推理引擎（细则 §34）

**一句话**：本地跑量化模型，**离线**、**零 HTTP import**；引擎不在就**明确说清楚** ——
绝不静默失败、绝不偷偷走云端。

#### 24.2.1 四条设计约束（模块文档字符串里就写着）

1. **全离线**：`main._network_policy_report` 静态扫 `core/ai/*.py`，出现真的 `urllib` /
   `requests` / `socket` / `http` import 就 FAIL；
2. **import 期绝不 import `llama_cpp`**：它是可选依赖，只有 `LocalModelEngine.load()` 里那句
   `_import_engine()` 会碰它，装不上就抛 `InferenceUnavailable`；
3. **引擎不可用绝不返回空串**：`generate()` / `stream()` 一律抛 `InferenceUnavailable`；
4. **零 Qt**。

#### 24.2.2 公开 API（`core/ai/inference.py`，`__all__` 就是这张表）

| 名字 | 位置 | 说明 |
|---|---|---|
| `ENGINE_MODULE = "llama_cpp"` | :59 | 可选依赖的模块名 |
| `DEFAULT_N_CTX = 2048` / `DEFAULT_MAX_TOKENS = 512` / `DEFAULT_TEMPERATURE = 0.7` | :63 / :66 / :69 | 默认参数 |
| `NO_ENGINE_MESSAGE` / `ENGINE_READY_MESSAGE` / `ENGINE_INSTALLED_MESSAGE` | :73 / :79 / :82 | 三句中文状态文案 |
| `USABLE_LEVEL_KEYS = frozenset({"easy", "tight"})` / `USABLE_LEVEL_WORDS = ("轻松", "勉强")` | :86 / :87 | 哪些档位算能跑 |
| `class InferenceUnavailable(RuntimeError)` | :101 | 唯一的失败类型 |
| `engine_available() -> tuple[bool, str]` | :114 | **只用 `importlib.util.find_spec` 探一探，不 import**；第二个返回值是中文原因 |
| `_import_engine()` | :140 | **全模块唯一碰 import 的地方** |
| `friendly_error(exc) -> str` | :165 | 翻成人话，**绝不含 pip / conda 这类包管理指令** |
| `class LocalModel` | :188 | `size_text`（:212）/ `usable`（:224）/ `display`（:239） |
| `list_local_models(dest_dir=None) -> list[LocalModel]` | :347 | 只列 easy / tight 的本地模型 |
| `class LocalModelEngine` | :416 | 见下 |

`LocalModelEngine`：`__init__(self, model_path, *, n_ctx=DEFAULT_N_CTX, n_threads=None, chat_format=None)`
（:430）；属性 `model_path`（:452）/ `is_loaded`（:457）/ `n_ctx`（:462）/ `chat_format`（:466）；
`load()`（:470，**幂等**）；
`generate(prompt, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE, *, stop=None) -> str`
（:514）；`stream(同签名) -> Iterator[str]`（:543，**立刻**判"没加载"，不拖到第一次 `next()`）；
`unload()`（:560）/ `close()`（:574）。

#### 24.2.3 界面（`ui/chat_dialog.py`）

| 项 | 值 |
|---|---|
| 入口 | 主窗 `ui/main_window.py:319` 的 `open_chat(self)`；档案页的「AI 侧写」也走它 |
| ★ 冻结接口 | `open_chat(parent, *, system_prompt=None, initial_prompt=None, prefer_model=None)`（`ui/chat_dialog.py:851`）—— **签名冻结，不许改** |
| 历史落盘 | `CHAT_HISTORY_FILE = "chat_history.json.enc"`（:78）—— 应用级加密（`core/user_store`），**明文不落盘**（红线 3）。任务书里写的 `cache/chat_history.json` 与红线冲突，**以红线为准** |
| 历史结构 | `HISTORY_SCHEMA = 1`（:81）；角色 `ROLE_USER = "user"` / `ROLE_ASSISTANT = "assistant"`（:96-97） |
| 上下文上限 | `MAX_CONTEXT_TURNS = 12`（:87）/ `MAX_PROMPT_CHARS = 4000`（:90）/ `MAX_TURNS_PER_DAY = 200`（:84） |
| 线程 | 生成跑在 `_ChatWorker(QThread)`（:191）里 —— 主线程不阻塞；提交入口 `submit(text) -> bool`（:674） |
| 引擎不可用时 | **窗口照样开**，只禁用输入，不弹一串模态框 |
| 导出 | `export_text`（:770）/ `history_as_text`（:791）/ `messages`（:805） |

★ 界面文案不许出现 Markdown 记号（细则第 24 节）—— 判据 `%TEMP%\tl_label_lint.py`。

#### 24.2.4 判据

`tests/test_inference.py`（44 用例：引擎缺失时的降级、懒 import（import 期不许出现
`llama_cpp`）、`generate` / `stream` 的失败都抛 `InferenceUnavailable`、不返回空串）。

### 24.3 档案合并与成绩覆盖（细则 §35）

**一句话**：v1.9 二期要"逻辑表"这套 API（表名、字段、删除语义），但**底层还是应用级加密的
JSON**，**不上明文 sqlite**（红线 3）。

#### 24.3.1 表、字段、主键

| 项 | 值 | 位置 |
|---|---|---|
| 成绩历史表 | `TABLE_GRADE_HISTORY = "grade_history"` | `core/database.py:77` |
| 学生档案表 | `TABLE_STUDENT_PROFILES = "student_profiles"` | `core/database.py:79` |
| 成绩历史落盘 | `GRADE_HISTORY_FILE = "grade_history.json.enc"` | :82 |
| 学生档案落盘 | `PROFILE_FILE = "student_profiles.json.enc"` | :83 |
| 字段 | `GRADE_HISTORY_FIELDS = ("student_id", "semester", "course_name", "score", "credit", "import_batch_id", "imported_at")` | :86 |
| 主键 | `GRADE_HISTORY_PK = ("student_id", "semester", "course_name")` | :96 |
| 覆盖 / 跳过 | `USE_OVERWRITE = "overwrite"` / `USE_SKIP = "skip"` | :99 / :100 |

★ `student_name` **只给弹窗显示用，不落库**（表里没有这一列）。

#### 24.3.2 `GradeHistoryDB.apply()` 的语义

```python
apply(records, *, semester, batch_id, policy=USE_OVERWRITE, on_conflict=None) -> dict[str, int]
# core/database.py:270
```

* 返回值三个键：`{"overwritten": n, "inserted": m, "skipped": k}`。`overwritten` 只数**真的删掉了
  旧记录**的行，`inserted` 只数**真的写进去了**的新行，`skipped` 数因为跳过策略没写的行；
* `on_conflict` 给了就**每个学生问一次**：`ConflictChooser = Callable[[list[ConflictQuestion]], dict[str, str]]`
  （:103），问题对象 `ConflictQuestion(student_id, student, semester, existing)`（:106）；
* ★ 回调**漏掉的学生按跳过处理**（最保守）；回调**抛异常 → 整批跳过，一个字都不写**；
* ★ **没有冲突的学生按 `policy` 直接写** —— 第一版这里写反了，导致"没有冲突的导入什么都不写"，
  `core/database.py:356` 附近的注释记着这个坑；
* `semester` / `batch_id` 空、`policy` 不认识 → 抛 `ValueError`；
* 其余 API：`rows`（:219）、`semester_rows(student_id, semester)`（:249）、
  `has_semester(...)`（:259）、`students_in(semester)`（:263）、`clear()`（:407，**只清
  `grade_history`**，班级快照与归档不动）。

#### 24.3.3 界面：四个按钮与那句结束文案

* 冲突弹窗（`ui/pages/analysis_page.py:371-373`）：「检测到学生 {姓名} 已有 {学期} 成绩，
  是否覆盖？」+ 四个按钮 **覆盖 / 跳过 / 全部覆盖 / 全部跳过**；
* 结束弹窗（`ui/pages/analysis_page.py:520-524`，逐字）：**「覆盖 N 条 / 新增 M 条 / 跳过 K 条」**
  （N / M / K 就是上面那三个计数）；
* 语义：**覆盖 = 删旧写新**；**跳过 = 保留旧记录、该行不导入**（不静默丢数据）。

★ 德育分批量导入那边是另一套文案（`core/import_workers.py` 的 `ImportSummary.as_text()`：
「成功 … 行（新增 … / 覆盖 …）」「跳过 … 行（其中空行 …）」），两处别互相抄。

#### 24.3.4 学生档案与档案页

`class ProfileStore`（`core/database.py:487`）：`side_text(student_id)`（:519）、
`all_profiles()`（:527）、`save_side_text(student_id, text)`（:532）、`current_username()`（:568）。

档案页两个标签页（`ui/pages/profile_page.py:759-760`）：
「**成绩明细**」（按学期折叠，学期顺序取自 `SEMESTER_ORDER`，**空 ≠ 0** —— 空格子就是空）
与「**总评与侧写**」（**只显示有真源的平均分 / 加权均分 / GPA / 排名**，没有真源就不编）。

「AI 侧写」按钮（`ui/pages/profile_page.py:1396`）：**懒 import** `open_chat()`，包在
`try/except` 里（没装引擎时只提示、不崩），侧写存进加密库；没登录时提示
「侧写暂时存不进加密库（没登录？）」（:1418）；空态提示
`SIDE_TEXT_EMPTY_HINT = "还没有侧写。点右上角的「AI 侧写」生成。"`（:145）。

★ **页面不许挂 `QGraphicsEffect`** —— 细则 4b（会缓存 pixmap、整页画成空白）与第 17 节
（切页抖动）都是实测过的。

#### 24.3.5 判据

`tests/test_grade_overwrite.py`（36 用例：覆盖 / 跳过 / 全部覆盖 / 全部跳过、冲突回调抛异常
时整批不写、主键去重、空参数报错）+ `tests/test_profile_merge.py`（28 用例：档案合并与两个
标签页的数据源）。

---

## 二十五、v1.9.1 紧急修复：现行规格与判据

v1.9.1 **不新增业务能力** —— 它把 v1.9 上线后暴露的七个问题修掉，顺手结清三笔旧账
（版本号 / 迁移跳、`CHECK_MODULES` 漏登记、`delete_user()` 的 `OSError`）。逐条的
现象 / 根因 / 修法 / 判据在仓库根 **`CHANGELOG_v1.9.1.md`**；**本节只记现行规格**。

**版本与迁移**：`core/version.py` 的 `CURRENT_VERSION` = **`"1.9.1"`**（1.9 → 1.9.1）；
`core/migrator.py` 的 `_STEPS` 补一跳 `("1.9", "1.9.1", _lift_1_9_to_1_9_1)` —— 函数体
只有 `return data`，照细则 **§23b**（**升版本就必须补一跳，哪怕数据结构没变**）。
老库**零转换**：`audit/grade_changes.json.enc` 与 `gpa_settings.json.enc` 都是**新建载荷**，
读到"没有这个文件"就当空。★ 界面里的版本号**一个字都没改** —— 三处全从 `core.version`
推导（细则 §23a / 铁律 10）。

### 25.0 ★ 规格例外：`grade_history` / 侧写走**应用级**加密（Owner 已拍板）

**这是一条正式记录的例外。它与细则 §16 那句「成绩绝不许往应用级里塞」冲突时，以本条为准。**

* 决定：**Owner 在 v1.9 二期拍板** —— 「**成绩加密方案 C：应用级加密 + 审计日志**」。
  `core/database.py` 的两份载荷 `grade_history.json.enc`（成绩历史库）与
  `student_profiles.json.enc`（学生侧写 / 总评与侧写）落在**应用级**（`core/user_store`），
  **不需要密码**即可读写。
* 为什么只能这样：档案页要"登录进来就能看历史"，而**自动登录只有 session token、没有
  密码**，库密钥解不开（§16 那条分工的同一条道理）—— 走密码级的话，自动登录的用户看到
  的是**空档案**。
* **例外只到这里为止**：§16 的旧底线对其余落点**照旧生效** —— 单次导入的 `grades`
  原始库、意愿标签等**仍然只走密码级**；不许拿这条当"成绩可以随便塞应用级"的通行证。
* 代价用**审计日志**抵（25.5）：应用级成绩库的每一次写 / 覆盖 / 删都留痕，
  且**只记"谁在什么时候动了哪条"，不记分数**。

### 25.1 模型中心：卡片流（细则 §33 的界面侧）

`ui/model_center_dialog.py` 从"表格 + 复选"改成**卡片流**：`ModelCard` +
`QScrollArea` + `QGridLayout`（**3 列**）、按 `group` 分组、悬浮**上移 2px + 阴影**、
点选 **2px `#0078D4` 边框**、已安装**绿徽章** + `×` 卸载、不可用行（🔴）**整行不可选**
并挂气泡说明、底部按钮「**下载选中（N）**」实时显示条数。

★ 数据侧规则**一个字没改**（细则 §33 仍然全量生效）：负载三档、默认**只勾 🟢**、
**内存探不到一律放行**、装没装**以磁盘为准**。

判据：`tests/test_model_center_card.py`（**36 用例**）—— 卡片结构 / 分组 / 选中态 /
徽章与卸载入口 / 🔴 不可选 + 气泡 / 按钮计数。

### 25.2 档案页第三标签页「AI 辅导」（细则 §34）

`ui/pages/profile_page.py:1299` `self._tabs.addTab(self._build_chat_pane(), "AI 辅导")`。
结构 = **学生卡 + 只读历史 + 输入区**；后台线程**流式**返回（工作线程契约与
`ui/chat_dialog.py` 一致 —— 它是 `ChatDialog` 的**姊妹实现**，见 25.9）。

* **系统提示词注入**：该学生的成绩 / 德育 / 侧写以 **JSON** 原样注入（不做自然语言摘要
  —— 数字与空值都保持原样，**空 ≠ 0** 在提示词里也是空）。
* **历史落盘**：`u_<uid>\cache\chat_history_<学号>.json.enc`
  （`CHAT_HISTORY_PREFIX = "chat_history_"`，:204）。★ 文件名**按学号**不按姓名
  （细则 §9：同名学生会串号）；**没学号**才退回一份共用文件。★ 明文聊天记录
  **绝不落盘**（红线 3）。
* **降级**：缺模型 → 「请先在模型中心下载模型」+ 跳转按钮；缺引擎 → 给**中文原因**
  （§34 的"明确降级，不许静默失败、不许偷偷走云端"）。
* 「总评与侧写」补全：**生成 → 可编辑 → 保存**闭环。

判据：`tests/test_profile_ai.py`（**33 用例**）。

### 25.3 单条删除（成绩 / 德育 / 侧写）

* 数据层（`core/database.py`）：`GradeHistoryDB.delete_record(student_id, semester,
  course_name)`（:500）、`delete_semester(student_id, semester)`（:520）、`clear()`（:488）
  —— **都返回删除行数**；`ProfileStore.clear_side_text(student_id)`（:898）、
  `clear_all()`（:918）。
* 确认文案：「**确定删除此条记录？此操作不可恢复。**」；删掉某学期**最后一条**成绩时
  追加「**该学期已无成绩，是否同时删除学期记录？**」（不追问就会留下一个点进去什么都没有
  的空学期壳）。

判据：`tests/test_grade_delete.py`（**74 用例**，含数据层 / 界面 / 审计 / GPA）。

### 25.4 「删除成绩库」：判据 + 接线（细则 §35）

* **判据只有一条**：`core.database.library_inventory()`（:993）返回的 **`has_data`**
  = `bool(grade_rows or profile_rows or files)`（:1066）—— **不算 `audit_rows`**
  （审计日志要留着，它本身就是"删过什么"的凭据）。旧的"拿文件名当判据"会说假话。
* **删除动作全在数据层**：`core.database.clear_library()`（:1070，可单测）；设置页只做
  `library_inventory()` → **两次确认** → `clear_library()`（`ui/pages/settings_page.py`
  :1055 / :1093），删完把清单如实报出来。
* **删完发信号** `library_cleared(int, int, int)`（`ui/pages/settings_page.py:144`，
  发在 :1113；三个数 = 成绩行数 / 侧写份数 / 文件数）。
* **主窗接线**：`ui/main_window.py:299` 连到 `_on_library_cleared`（:330）—— 用
  `getattr(target, name, None)` + `callable()` 探 `profile_page.show_no_data` /
  `archive_page.reload` / `scholarship_page.reload_classes`；刷新失败**只
  `traceback.print_exc()`、不弹窗**（§18：数据已经删干净了），并打一行汇总日志。
* **档案页的空钩子**：`ui/pages/profile_page.py:1114 show_no_data()` —— 名单是
  `set_roster()` 推进内存的，清库不会自己收回去；它显式清空名单 / 载荷 / 谈话状态并显示
  「暂无数据：成绩库已清空，先在「📊 智育分析」导入成绩表。」（**只动显示状态**，
  不碰 AI 辅导与侧写的既有逻辑）。
* ★ **智育分析页不接这个刷新**：它的表格是"本次导入的内存结果"，页面上没有"重新读库"
  的入口（`set_user()` 只清 `_archive` / `_grade_decisions`）。

### 25.5 审计日志 `audit/grade_changes.json.enc`

* `class GradeAuditLog`（`core/database.py:659`，`filename = AUDIT_FILE` :678）；
  `AUDIT_DIR = "audit"`（:120）、`AUDIT_FILE = f"{AUDIT_DIR}/grade_changes.json.enc"`
  （:122）、`TABLE_GRADE_AUDIT = "grade_audit"`。
* **字段**：`at` / `operation` / `student_id` / `semester` / `course_name` / `count`。
  `AUDIT_OPERATIONS = (OP_INSERT, OP_OVERWRITE, OP_DELETE)`（:131，不在白名单的 operation
  直接拒收）；`AUDIT_MAX_ENTRIES = 2000`（:124，超了砍最旧的）。
* ★★ **不记分数**：审计只回答"谁在什么时候动了哪条记录"，`score` **一个字节都不进**审计
  文件 —— 成绩本体只在成绩库里，审计是最小化的旁证（也是 25.0 那条规格例外的代价）。
* 写在**应用级**（`u_<uid>/audit/…json.enc`），所以 `delete_user()` 会连着它一起清（25.7）。
* ★ **坏了只降级为警告**：`record()` / `record_many()` 失败只 `warnings` 并返回
  `None` / `[]` —— **审计不许挡住用户删记录**；`changes()` 读到坏文件返回 `[]`。
* ★ **目前只写不读**：没有界面入口（`changes()` 是留给以后 / 脚本的只读查询 API，
  见 25.9 未做）。

### 25.6 GPA 方案 1（`core/gpa.py`）

新建 **`core/gpa.py`**（**没有**写进封板的 `core/calculator.py`，理由见 25.9）：
`SCHEME_STANDARD_4 = "standard4"`、`DEFAULT_SCHEME`、`STANDARD_4_TABLE`（:69）、
`grade_point(score, scheme)`（:244）、`gpa_detail(courses, scheme)`（:262）、
`gpa_of(courses, scheme)`（:309）、`scheme_options()`（:138）、`available_schemes()`（:143）、
`load_scheme()`（:332）/ `save_scheme()`（:351）。

换算表（标准 4.0）—— ★ **一行都不在界面里，真源只有 `core/gpa.py`**：

| 百分制 | ≥90 | ≥85 | ≥82 | ≥78 | ≥75 | ≥72 | ≥68 | ≥64 | ≥60 | <60 |
|---|---|---|---|---|---|---|---|---|---|---|
| 绩点 | 4.0 | 3.7 | 3.3 | 3.0 | 2.7 | 2.3 | 2.0 | 1.5 | 1.0 | 0.0 |

`GPA = Σ(绩点 × 学分) / Σ学分`。

* ★ **空 ≠ 0（红线 2 的同一条）**：`BLANK_TOKENS`（:90）=
  `{"", "-", "/", "—", "–", "未选", "无", "未修"}` → 这门课**不进分母**；
  数字 `0` = **缺考** → 绩点 0.0、**学分进分母**。
* 一门可算的课都没有 → `gpa_of()` 返回 **`None`**（界面"暂无数据"，**不是 0.00**）。
* 设置存 **`gpa_settings.json.enc`**（`GPA_SETTINGS_FILE` :327，键
  `GPA_SETTINGS_KEY = "scheme"` :329），走**应用级**；设置页下拉在
  `ui/pages/settings_page.py:292`（`_build_gpa_card`）—— 换算表在界面上**一个字都没有**。
* **方案 2 / 3 / 4 预留**：`SCHEMES`（:96）里 `available=False`，界面 label
  「方案 N（待定）」+ 禁用（未实现，见 25.9）。

判据：`tests/test_grade_delete.py` 的 GPA 部分（`gpa_of([]) is None`、空学分不算、
`0` 进分母、90+78 混合、`Course(85, 2.0)` ≈ 3.7、未实现方案报错）。

### 25.7 附带修复（三笔旧账）

1. **`core/crypto_store.delete_user()`**（:835）：旧写法结尾 `user_dir.rmdir()` ——
   只要 `u_xxx/audit/` 子目录存在就抛
   `OSError: [WinError 145] 目录不是空的`，而且异常发生在 `save_users()` **之前**，
   结果是"**载荷删了、账户记录还在**"。现在按**深度倒序** `rglob`：目录 `rmdir()`、
   文件 `unlink()`，单个失败 `except OSError: continue`（删不掉的**不报成删掉了**）；
   最后 `user_dir.rmdir()` 包在 `try/except OSError: pass` 里（**清不掉空壳不算失败**，
   账户记录照样移除）。**签名与"删账户"语义未变。**
2. **`main.py` 的 `CHECK_MODULES` 补 `core.gpa`**（42 → **43** 项）—— 自检的模块导入闸门
   要能发现新模块坏了。
3. **版本号 + 迁移跳**（见本节开头）。

### 25.8 导入「核对提取结果」：QSplitter + `ui_state.ini`

* `ui/review_dialog.py`：横向 `QSplitter`，默认 `_DEFAULT_SIZES = (700, 300)`（≈ 0.70，:69），
  窗口初始 `_DEFAULT_SIZE = (1000, 700)`，单栏最小 `_MIN_PANE_WIDTHS = (280, 200)`（:72），
  `setStretchFactor(0, 7)` / `(1, 3)`；**右栏套 `QScrollArea` 自己滚动**；表头 `Interactive`
  + `minimumSectionSize = MIN_COLUMN_WIDTH = 72`（:60），初始列宽
  `_COLUMN_WIDTHS = (72, 260, 96, 84, 150, 150)`（:63）。
* 拖动位置**记在应用级 ini**：**`%LOCALAPPDATA%\SGMS\ui_state.ini`**，键
  **`grade_import/splitter_state`**（`SPLITTER_KEY` :75，`_STATE_FILE_NAME = "ui_state.ini"`
  :78，`state_path()` :106）—— 用 `QSettings(IniFormat)` **显式指路径**，
  **刻意不用注册表**（红线 2）。`saveState()` 在 :403，`restoreState()` 在 :381
  （读不出来 / 长度不对 → 退回 `_DEFAULT_SIZES`，:397）。

判据：`tests/test_import_review_ui.py`（**50 用例**）。

### 25.9 v1.9.1 判据汇总 / 两处差异 / 未做

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_model_center_card.py` | **36** | 卡片流结构 / 分组 / 选中态 / 徽章 / 🔴 不可选 |
| `tests/test_profile_ai.py` | **33** | 档案页 AI 辅导 / 侧写闭环 |
| `tests/test_grade_delete.py` | **74** | 单条删除 / 删库判据 / 审计日志 / GPA |
| `tests/test_import_review_ui.py` | **50** | QSplitter / ini 落点 / 列宽 |

四份合计 **193** 用例（`--collect-only` 实测）。另跑
`main.py --selfcheck` → **RESULT: ALL OK**；
`pytest tests/test_version_migration.py tests/test_manifest.py tests/test_profile_merge.py -q`
→ **119 passed**。

**与任务书字面的两处差异（有意为之，不是漏做）**：

1. **GPA 落在新建的 `core/gpa.py`**，**没有**写进 `core/calculator.py` —— 后者是铁律 1 的
   封板文件（`main.FROZEN_FILE_SHA256`，细则 §27f），往里加一行就要解冻铁律 1。
2. 删掉的是**全局** AI 助手入口（主窗顶栏按钮 + `_open_chat_assistant`），
   **`ui/chat_dialog.py` 的 `ChatDialog` 类保留**为内部组件（细则 §34 的冻结接口
   `ui.chat_dialog.open_chat(...)` 签名不许改），档案页「AI 辅导」是它的**姊妹实现**
   —— 两者工作线程契约一致。

**未做 / 待定**（记录在案，不是待办清单）：

* **GPA 方案 2 / 3 / 4** 只有壳（`SCHEMES` 里 `available=False`，界面禁用 +「待定」）；
  真要实现是 Owner 定口径的事。
* **审计日志只写不读** —— 没有界面查看入口，`GradeAuditLog.changes()` 是留给以后 /
  脚本的只读 API。
* **模型中心的悬浮 / 阴影 / 点选边框未做人工鼠标验收** —— 目前只有结构断言 + 自动化像素
  断言（细则 4h 的"必须真的动鼠标"留给下一轮回归）。

## 二十六、v1.9.2：学生主档、两级档案页与一键装推理后端

这一版解决的是**"这个班有哪些学生"到底以谁为准**的问题，以及三个一直没闭环的入口：
档案页不能新增/删除学生、不能看单个学期的侧写、推理后端只能靠命令行手动装。

### 26.1 学生主档（`students.json.enc`）

* 唯一真源是 `core.database.StudentStore`，载荷 `students.json.enc`（应用级加密，第 16 节）。
  字段：`student_id`（`sid:<学号>`）、`name`、`class_name`、`note`、`archived`、`source`、
  `created_at` / `updated_at`。
* ★ **学号是标识不是约束**：写入口一律 `_student_id_of()` 补 `sid:` 前缀，读出来用
  `_bare_student_id()` 拆回去。两种写法混用会让同一个人在名单里出现两次（第 9 节同一条道理）。
* `add()` 撞学号抛 `DuplicateStudentError`（界面捕获后给中文提示，**不是**崩溃）；
  `update()` 只改姓名/班级/备注（要改学号必须走 26.2 的迁移）；
  `archive()` 只把 `archived` 置真（**数据一条不删**），`restore()` 反过来；
  `remove()` 才是真删（界面叫"彻底删除"）。
* **导入补档**：`upsert_import()` 没有就建、有就**只补空字段**（绝不用 Excel 里的名字
  覆盖辅导员手改过的名字）。
* 判据：`tests/test_student_crud.py`（37 项）。

### 26.2 改学号 = 全量迁移（`migrate_student_id`）

学号变了不是改一个字段，而是要把**七处**引用一起搬：

| # | 来源 | 怎么搬 |
|---|---|---|
| 1 | 成绩历史 `grade_history` | `GradeHistoryDB.rekey_student()` 重建主键；撞键（同学生同学期同课程）时保分高的一条、旧的丢并计数 |
| 2 | 总评侧写 `student_profiles.side_text` | `ProfileStore.move_student()` |
| 3 | 学期侧写 `student_profiles.semester_side` | 同上（一行一学期） |
| 4 | AI 聊天缓存 `cache/chat_history_<裸学号>.json.enc` | 文件改名（新名已存在则合并保留旧的） |
| 5 | 德育分 `class_<班>_moral.json.enc` 的 `scores` 键 | `_rename_key()` + 回写 |
| 6 | 意愿标签 `tags.json.enc` 的 `intentions` 键 | `_rename_nested()` |
| 7 | 班级快照 `class_<班>.json.enc` 里的学生键 | `rename_student_key()`（找不到就计 0，不算失败） |

* 返回 `StudentMigrationReport`（`grades` / `grade_dropped` / `profiles` / `chats` / `moral` /
  `intentions` / `files`，`total()` 与 `as_text()`），界面用它弹"迁了几条"。
* 审计记一条 `student_migrate`，`detail` 写旧学号→新学号（**不写分数**）。
* ★ 两个坑：① `ProfileStore` 在内存里有 `_cache`，**迁完必须重建实例**再读，否则读回旧键；
  ② 德育分与意愿的载荷是**嵌套字典**（`{"scores": {...}}` / `{"intentions": {...}}`），
  按顶层键改名等于什么都没改。

### 26.3 审计日志扩展

* `AUDIT_OPERATIONS` 由 3 种（`insert` / `overwrite` / `delete`）扩到 9 种：加上
  `student_add` / `student_edit` / `student_archive` / `student_restore` /
  `student_delete` / `student_migrate`（+ `semester_delete` 由 `delete_semester` 记）。
* 每条多一个 `detail` 字段（索引信息，如姓名、旧新学号）——★ **永远不写分数**
  （`tests/test_grade_delete.py` 有一条断言 `set(entry) == {at, operation, student_id,
  semester, course_name, count, detail}`，就是在守这条）。
* `AUDIT_MAX_ENTRIES = 2000` 封顶，超出丢最老的；日志损坏只警告、不清空。
* 已知：**没有界面查看器**，`GradeAuditLog.changes()` 是只读 API。

### 26.4 档案页两级视图

* `ui/pages/profile_page.py` 用 `QStackedWidget` 两层：第 0 层详情（左名单 + 右学生卡 +
  学期时间轴 + 三标签页），第 1 层学期侧写（`ui/pages/semester_side_profile.py`）。
* 名单数据来源**两级兜底**：`StudentBook.durable` 为真用主档；否则用 `set_roster()`
  推来的内存名单。★ 这一条是硬要求 —— 主档坏了不能把 v1.9.1 的名单一起弄没。
* 时间轴节点（`profile_shared.TermNode`）只列**成绩历史里真出现过的学期**，
  按 `_ordered_records()` 的顺序；点节点 = `open_semester(term)`。
* 主窗穿透：分析页双击 → `show_student()` → `ask_semester_and_open()` 弹选学期 → 第 1 层；
  清库 → `reset_view()` 把**两级都**复位（只复位一级会让用户切回去还看到上个学生的学期侧写）。
* ★ 仍然**不许挂 `QGraphicsOpacityEffect` / `QGraphicsDropShadowEffect`**（第 4b/17 节），
  三个新 UI 文件里对这两个名字只有文档字符串里的"禁止"说明。

### 26.5 清成绩库的判据与聊天缓存

* 界面判据改成 `library_inventory()["grade_rows"]`（= `grade_history` 表为空）。
  旧版查的是早已废弃的 `grades.enc`，于是出现"档案页有学生、设置页说没有成绩库"。
* `library_inventory()` 新增 `student_rows` 与 `chat_files`；`clear_library()` 在
  `with_profiles=True` 时顺带删 `cache/chat_history_*.json.enc`，并把它们写进
  `report["files"]`；`with_profiles=False` 时**保留**（谈话记录属于辅助数据）。
* 确认框必须如实写「· 该生的 AI 聊天缓存也会一并清除」——删了用户不知道是红线 3 的同类问题。

### 26.6 推理后端一键安装

* `core/ai/runtime_setup.py`（零 Qt、零 HTTP import）：`pip install --target
  %LOCALAPPDATA%\SGMS\runtime\site llama-cpp-python`，然后 `ensure_runtime_on_path()`；
  **绝不改系统 Python、不写注册表**。
* 默认带 `-i https://mirrors.aliyun.com/pypi/simple/`（`PIP_EXTRA_INDEX`）：
  国内直连 PyPI 基本必然超时（`INSTALL_TIMEOUT = 600`）。
* `find_system_python()` 只认"能跑起来"的解释器（`_probe_python` 真跑一次），
  依次试 `sys.executable` / `python` / `py -3` / 常见安装目录；**全都不是硬编码猜测**。
* 安装线程 `BackendInstallThread` 可以取消（`cancel()` 置位 + 杀进程树），
  逐行回调给界面滚动日志（最多 2000 行）；失败给中文原因 + `install_command_text()` 复制命令。
* `ui/widgets/backend_setup.py` 的三态卡片是**唯一**安装入口（AGENTS §36）；
  它 import 失败时模型中心照常打开（按"可用"处理，不误报"未安装"）。

### 26.7 判据与闸门

* 新增测试：`tests/test_student_crud.py`（37）、`tests/test_archive_navigation.py`（62）、
  `tests/test_runtime_setup.py`（41）、`tests/test_profile_navigation.py`（16）；
  `tests/test_grade_delete.py` 从 74 增至 77（聊天缓存三条）。
* `main.CHECK_MODULES` 增加 `ui.pages.profile_shared`、`ui.pages.semester_side_profile`、
  `ui.widgets`、`ui.widgets.backend_setup`、`core.ai.runtime_setup`。
* 封板四文件 sha256 **未变**；`--selfcheck` 全绿；`known_data_versions()` 到 `"1.9.2"`。

### 26.8 并发开发的两条教训

* ★ **同一个文件只能有一个写者**。这一版里 `ui/pages/profile_page.py` 同时被
  "档案页重构"和"集成接线"两个子代理认领，结果集成代理先落地了一整套两级视图（含
  `SemesterSideProfilePage`），重构代理读到旧版后准备从头重写 —— 差点把已完成的实现覆盖掉。
  **正确做法**：先由主控把目标文件落到磁盘（哪怕只有接口骨架），子代理只补测试或在**互斥**
  的文件上工作。
* ★ **契约先落磁盘再并行**。`core.ai.runtime_setup` 被两个代理各写了一套 API
  （`install()` vs `BackendInstallThread` + `backend_status()`），最后靠主控裁决保留一套、
  并让 `ui/model_center_dialog.py` 用 `try/except` 探测两端 —— 这种"探测兜底"只能是过渡，
  版本发布前必须收敛成单一契约（本版已收敛到 `runtime_setup`）。

## 二十七、v1.9.3：启动可靠性（日志 / 备份 / 登录 / 淡入 / 单文件解包）

v1.9.2 发出去之后，现场报回来四个症状。四个都不是"功能没做"，而是
**出错的时候不吭声**：程序要么卡在某个中间态，要么弹一句用户看不懂的英文。
所以这一版的主题只有一个 —— **把"静默失败"改成"要么成功，要么说清为什么"**。

### 27.1 四个症状与根因

| 症状（用户原话） | 根因 | 修法 |
|---|---|---|
| 「输入账户密码无法进入程序只会显示正在校验密码 PBKDF2 派生中」 | `_do_login` 只 `except` 了 `CryptoError` 家族；密码通过之后 `_handle_remember` / `_leave` 里任何别的异常都从按钮槽里**冒出去**。`--windowed` 的 exe 没有 stderr，Qt 那行 traceback 等于丢进黑洞，界面就永久停在「PBKDF2 派生中」 | 密码通过之后的每一步整体 `except Exception`：落日志（含完整 traceback）+ 弹窗 + 状态改回 + **解锁 `_leaving` 允许重试** |
| 「密码错误倒是可以正常显示」 | 同上 —— 错密码走的是 `WrongPasswordError` 分支，本来就有文案。**这条反而证明程序没崩，是"对的那条路"缺兜底** | —— |
| 「旧版迁移数据报异常但不影响程序打开」 | `backup_vault()` 对整个数据目录 `copytree`，而现场有 1.1 GB 模型 + 两份 GB 级历史备份；拷贝期间被占用就整次迁移失败（旧数据没坏，所以程序照常打开） | 备份**跳过** `models` / `runtime` / `logs` / `cache`（都可重建），并给出"备份了什么、为什么快"的说明 |
| 「点击运行 exe 显示 cannot create tempdirctor」 | PyInstaller 单文件启动器要先把自己解包到 `%TEMP%\`；本机的安全策略拦这一步（同一个 hello-world 单文件包同样失败） | 打包加 `--runtime-tmpdir "."`：**解到 exe 自己所在目录**，不碰 `%TEMP%` |
| 「登录界面有多余线条」 | 未定位（见 27.6 的说明） | 圆角半径按"父级描边 1px → 子级小 1px"统一；**需要用户截图才能精准修** |

### 27.2 运行期日志：`core/applog.py`

零依赖、不联网，只做四件事：

* `write(tag, message)` —— 关键步骤各写一行（`boot` / `login` / `flow` / `migrate` / `prewarm` / `crash`）。
* `install_excepthook()` —— 未捕获异常先落盘，**再**交给原来的钩子（开发时照样看得见栈）。
* `install_faulthandler()` —— 线程栈 dump 到 `logs/fault.log`，真卡死时能取证。
* `tail(lines)` —— 错误弹窗里直接给用户看最后几行。

★ 三条硬约束：

1. **日志永远不许抛**。写不进去（目录只读、磁盘满、路径是个文件）一律 `except Exception: pass` ——
   日志把程序搞崩就荒唐了。
2. 位置固定在 `<数据根>/logs/app.log`（默认 `%LOCALAPPDATA%\SGMS\logs\`），超过 512 KB 滚成 `.1`。
3. **不进备份**（见 27.3）—— 它是恢复过程里最该被覆盖的东西之一。

### 27.3 迁移备份：可重建的目录不该拖慢甚至拖死迁移

`backup_vault()` 原来只跳过"已有的备份"和 `*.tmp`。现场数据目录里：

* `models/` 1.1 GB（一个 1.5B 的 gguf）；
* 每个历史备份各 1.1 GB（`backup_v1.8.1_*` / `backup_v1.9_*`）。

于是"点一次迁移"要拷 GB 级数据，几十秒起步，中途被杀软扫一下就可能失败。
现在 `SKIP_DIR_NAMES = {"models", "runtime", "logs", "cache"}` 一律不进备份 ——
它们**都能重建**（重新下载模型 / 重装后端 / 日志本来就可丢 / 缓存随时可清），
而真正不可再生的 `users.json`、`app.salt`、`u_*/`（成绩、档案、侧写、主档）一个字都不省。

★ 判据：`tests/test_startup_robustness.py::test_备份跳过可重建的大目录` 造一个"像真的"
数据目录（五个目录各放一个 payload），断言备份里**没有**这四个目录、而核心密文**逐字节相同**。

### 27.4 登录：密码对了也必须能进得去

`ui/login_window._do_login()` 现在的结构：

```
校验密码 → [三种 CryptoError 分支：原有文案]
        → 密码通过：写一行登录日志
        → try: _handle_remember() + _leave()
          except Exception: 落完整 traceback + 状态改红 + 弹窗（附日志路径）+ 解锁重试
```

另外 `_leave()` 里挂了 `LEAVE_FAILSAFE_MS = 1600` 的看门狗：淡出动画如果因为任何原因
不回调（窗口被隐藏、显示器休眠、动画对象被回收），`_force_leave()` 会把 `logged_in` **补发一次**
（且只发一次 —— `_left` 标志位保证幂等）。

★ 判据（`tests/test_startup_robustness.py`，5 条）：密码通过后人为让 `_handle_remember` 抛
`RuntimeError` → 不许发 `logged_in`、`_leaving` 必须复位、状态行与弹窗都要有、日志里要有 traceback；
错密码的既有行为不许被改坏；正常路径发一次；看门狗在"动画不回调"时补发一次、在正常路径上不重复发。

### 27.5 主窗淡入：透明度卡在 0 也是"进不去"

`ui/main_window` 构造时 `setWindowOpacity(0.0)`，靠 `show_and_fade_in()` 的 200ms 动画拉回 1。
动画被任何东西打断（最小化、远程桌面、合成器抽风），窗口就是**"已显示但全透明"** ——
用户看到的是"点了登录，程序没出来"。

现在多了一个 `FADE_IN_FAILSAFE_MS = 900` 的看门狗：到点 `windowOpacity() < 0.99` 就直接补成 1，
并写一行 `flow` 日志（"淡入看门狗介入：opacity=0.00"）。900ms 是动画时长的 4 倍多，正常路径碰不到。

### 27.6 单文件 exe 解包失败：`--runtime-tmpdir` 的实测结论

现场：双击 `dist\transcript-lens.exe` 弹一个标题为 **Error** 的原生对话框，进程随即消失。
用控制台版单文件包（`-ShowConsole`）接住 stderr，逐字是：

```
[PYI-19100:ERROR] Could not create temporary directory!
```

★ 这是 **PyInstaller 启动器**在 Python 起来之前报的错，应用层完全插不上手 ——
所以要修的是"解包到哪"。

实测矩阵（同一个 exe，只改 TEMP / 解包根）：

| 解包位置 | 结果 |
|---|---|
| `%TEMP%\` | ✗ `Could not create temporary directory!`（exit -1） |
| 用户目录根、桌面、`AppData\Local\*` | ✗ 同上 |
| **项目目录树内**（`dist\_tmp`、`<repo>\_MEI*`） | ✓ 正常（exit 0） |
| **`--runtime-tmpdir "."`（解到 exe 所在目录）** | ✓ **在 `%TEMP%` 和用户目录根都是 exit 0** |

对照组：同样条件下一个 8 MB 的 hello-world 单文件 exe 报**一模一样**的错 ——
与本项目代码、打包参数无关，是本机策略在拦"未签名程序在常见目录创建解包目录"。

所以 `build_exe.ps1` 的 release / console 目标现在固定带 `--runtime-tmpdir $RuntimeTmpDir`
（默认 `"."`，见脚本 `.NOTES`）。★ 两个边界必须记住：

1. 参数里的相对路径是相对**启动时的工作目录**。双击时 CWD 就是 exe 所在目录（Explorer 的行为），
   所以正常双击没问题；但从别的目录命令行启动、或快捷方式把"起始位置"改到别处，就会落到那个目录。
2. **不支持环境变量**（写 `%TEMP%` 会被当字面量），而且**上级目录必须已存在** ——
   指 `_tmp` 这种不存在的子目录会直接失败（实测）。

### 27.7 判据清单

| 文件 | 覆盖 |
|---|---|
| `tests/test_startup_robustness.py`（10 条） | 日志落盘/读回/不可写不炸/滚动；备份跳过四目录且核心密文逐字节一致；登录口令通过后异常被兜住、错密码行为不变、看门狗补发且幂等 |
| `tests/test_version_migration.py` | 版本链 `1.0 → 1.9.3` 九跳齐全 |
| `main.py --selfcheck` | `core.applog` 已进 `CHECK_MODULES`（50 个），docs 白名单 5 份 |
| `build_exe.ps1 -Both` + exe `--selfcheck` / `--bootcheck` | 打包与启动时序 |

### 27.8 未完成 / 待用户确认

* **登录界面「多余线条」没有定位到**。离屏渲染在 100% 和 150% DPI 下都拍过：
  深色品牌区、1px `#E2E8F0` 描边、14px 阴影留白、分隔线（248px 宽 1px）都正常，
  四角只有"描边 + 投影"两层。**只有真机上合成器参与时才可能出现**那类线，
  需要用户截一张图（或指一下位置：窗口四角？标题栏下面？输入框上下？）。
  已顺手按 AGENTS §4e 第 2 条把"与父级共用同一个圆"的子控件半径统一成 `父级 - 1px`
  （`#WindowTitleBar` / `#LoginBrand` / `#Sidebar` / `#TopBar`），但它不是已证实的修复。
* 主界面 6 页的接触表（`%TEMP%\tl_main_pages.png`）在 100% DPI 下没有看到杂线，
  但 50% 缩放会把 1px 线抹掉，同样需要 150% DPI 的真机截图才能定论。

## 二十八、v1.9.3 补充：错误信息一键复制

**诉求**：报错弹窗里加「复制错误信息」，点一下把 traceback 和日志路径放进剪贴板。

**实现**：`ui/error_report.py`（零依赖 / 不联网）

* `install_error_copy(app, root)`：QApplication 事件过滤器。任何 **Critical 图标**的
  `QMessageBox` 在 Show 时自动多一个按钮。按钮用普通 `QPushButton` 塞进
  `QDialogButtonBox` 的布局 —— **不能用 `QMessageBox.addButton()`**：实测任何 role
  （Action/Help/Reset/Apply/No/Yes）都会触发 `done()` 关窗，复制失败时用户就看不到错误了。
* 报告 = 时间 / 版本 / 标题 / 正文 / traceback / 日志路径 / 最近 40 行日志，超 20 万字符截断。
  traceback 在 Show 事件里用 `sys.exc_info()` 抓（此时 `QMessageBox.critical` 还停在
  调用方的 except 块里，实测拿得到）。
* `install_crash_handler(root)`：包住 `sys.excepthook`，先让 `core.applog` 落盘，再在主线程
  用 `QTimer.singleShot(0, ...)` 弹带完整 traceback 的 Critical 框。PySide6 6.9.2 的槽函数
  异常会走 `sys.excepthook`（实测），所以这条兜住"按钮点下去没反应"。
* `main()` 的安装次序：`install_error_copy` → `core.applog.install_excepthook` →
  `install_crash_handler` → `install_faulthandler`。

**判据**：`tests/test_error_report.py`（9 条，离屏，桩掉剪贴板）+
`main._error_report_gate_report()`（报告要素 / 截断 / main 接线）。全量 `pytest` 887 passed；
`--selfcheck` 与 `--bootcheck` 全绿。

**边界**：只给 Critical 图标弹窗加按钮；普通提示/警告不加。没有关联 Python 异常的弹窗
（比如"密码错误"）报告里会如实写"没有 traceback"，但仍带日志路径和日志末尾。

## 二十九、v1.9.4：存储、备份与自动登录故障边界

本次不改数据格式。打包版本升为 1.9.4，并补齐 1.9.3 → 1.9.4 的空迁移步骤。
本节的备用备份位置补充 §12.4 与施工细则 §22a 的默认路径规定。

* **原子写入**：`core.crypto_store._atomic_write_bytes()` 在目标同目录独占创建唯一临时文件，
  写入、flush、fsync 并关闭后再替换。只对 Windows 访问/共享/锁错误（5/32/33）最多
  尝试 4 次，间隔 50/100/150ms；其他 I/O 错误立即上报。失败尽力清理本次临时文件，
  不删除旧目标。这保证单文件写入完整，不代表多个文件或读改写业务具备事务隔离。
* **备份位置**：先用原数据目录，失败后依次尝试同级 `SGMS_backups/<源路径摘要>/`、
  `%LOCALAPPDATA%/SGMS_backups/<源路径摘要>/`（重复位置合并）。不使用可能被系统清理的
  `%TEMP%` 作为唯一备份；不同数据根分开存放。实际位置由 `backup_dir` 与成功提示返回。
* **完整性**：备份前生成全量文件摘要清单，复制后核对目标与源清单，任何缺失、内容变化
  或备份期间源数据变化都拒绝迁移；无法确认范围的链接目录同样拒绝。只在根层排除
  models/runtime/logs/cache，用户目录内聊天记录与审计记录全部保留。失败清理仅针对
  本次独占创建的备份，清理失败报告残留位置；所有位置失败时原数据不改动。
* **回滚**：仍仅恢复 `users.json`，复用同一原子写入口，避免恢复中途截断账户索引。
* **自动登录**：`save_token()` 将写入 I/O 错误转为可处理的 `CryptoError`；读令牌失败
  回到密码登录。界面保留对 `OSError` 的兜底，保存或取消自动登录失败均继续本次登录。
  取消失败时明确告知旧凭证可能仍有效，不能声称下次一定要求密码。
* **故障回归**：`tools/test_storage_resilience.py` 使用假数据与临时目录验证锁冲突、并发
  完整写入、磁盘错误、备用路径、内容校验、失败清理、回滚及登录信号；原封板 `tests/` 不改。
* **本次验证**：新增 23 项故障回归、包含原有登录/版本用例的 61 项专项测试全部通过，
  `--selfcheck` 为 ALL OK。首次全量 890 通过、17 项因本机连接被执行环境拒绝
  （WinError 10013）失败；正常本机网络权限下复测这 17 项全部通过。两种异常提示已截图检查。
  以上为修复阶段验证；1.9.4 打包须使用同一提交的源码，并验证 exe 的 selfcheck 与 bootcheck。
