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
> 对应版本：**v1.8**（`core/version.py` 的 `CURRENT_VERSION`）
> 最后更新：2026-09-27

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

**v1.9 已在工作区完成（未发版）**，三件事 + 一个 Bug 修复：

| 项 | 落地位置 |
|---|---|
| 解析器表格干扰 Bug（统计列 / 末尾汇总行 / 大标题） | `core/parser.py`（唯一一次解冻，见 §7.4、§13.6） |
| 批量导入德育分 | `core/import_workers.py` + `ui/moral_import_dialog.py` + 奖学金页按钮（§二十一） |
| AI 模型中心 | `core/ai/model_registry.py` + `ui/model_center_dialog.py` + 设置页入口（§八） |
| 更新器改走 `update/manifest.json` | `core/updater.py`（§6.3） |

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

1. **先备份整个数据目录**到 `backup_v{旧版本}_{时间戳}\`（`core/migrator.backup_vault()`）
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

- 迁移前必须备份；备份放在数据目录**里面**，所以扫描数据目录时必须
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

### 16.5 ⚠️ 提交号 vs 程序版本号

删除激活码的提交标的是 **v1.8.1**，但 `CURRENT_VERSION` **仍然是 `"1.8"`** ——
因为数据格式一个字节都没变，升 `CURRENT_VERSION` 会凭空让所有 v1.8 用户的库
变成"比程序旧"、触发一次没有必要的迁移提示。**下次真有数据格式变化时，
把版本号和迁移链一起升。**

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

### 18.1 `--selfcheck` 的 10 条闸门（`main.py`）

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
| `tests/test_manifest.py` | `update/manifest.json` 优先 + 退回旧路线 | 58 |

（v1.9 全量实测 **264 passed**。）

runner 是 `%TEMP%\tl_run_all.ps1`（**不在仓库里**，是本机开发工具；**v1.9 起 31 个套件**）。
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
| 交付：commit 带 **v1.9** 前缀，**不推 Gitee** | ✅ 见 §16.5（提交号 ≠ 程序版本号，`CURRENT_VERSION` 仍是 `1.8`，没有数据格式变化就不升） |

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
> 就是本节。以后同类要求一律**并进本文**，`docs/` 只允许那三个文件（§0.2）。
