# CHANGELOG v1.9.1（紧急修复版）

v1.9.1 是 v1.9 的**紧急修复版**：不新增业务能力，只修七个已经暴露到用户面前的问题，
并把版本号抬到 1.9.1（更新检查按版本号比大小，见 AGENTS.md §23 / §23b）。

**版本与迁移（一次说完）**

* `core/version.py` 的 `CURRENT_VERSION`：`1.9` → `1.9.1`（docstring 里的版本表同步）；
  界面上的版本号**一个字都没改** —— 三处（登录窗 / 开屏 / 个人中心）都是从
  `core.version` 推导的（AGENTS.md §23a 铁律 10）。
* `core/migrator.py` 的 `_STEPS` 补一跳 `("1.9", "1.9.1", _lift_1_9_to_1_9_1)`，
  函数体就是 `return data`（照 §23b：**哪怕没有数据结构变化也要补**）。
* 老库**不需要任何数据转换**：`audit/grade_changes.json.enc` 与 `gpa_settings.json.enc`
  都是**新建载荷**，老库打开时读到"没有这个文件"就当空，已有数据一个字节不动。

---

## 问题 1：模型中心"看不清"——选项之间没有可视化差别

**现象**：模型中心是一张清单 + 复选框，用户看不出"这条我装没装""我的机器跑不跑得动"
"哪几条是一类"；下载按钮在顶上，勾了几条要自己数。

**根因**：界面是"表格 + 复选"结构，`QTableWidget` 的表达力只到单元格文本 —— 负载配色、
安装徽章、悬浮反馈全都塞不进去。

**修法**：`ui/model_center_dialog.py` 改成**卡片流** ——
`ModelCard` + `QScrollArea` + `QGridLayout`（3 列）、按 `group` 分组、
悬浮上移 2px + 阴影、点选 2px `#0078D4` 边框、已安装绿徽章 + × 卸载、
不可用行（🔴）整行不可选并挂气泡说明、底部按钮「下载选中（N）」实时显示条数。

**判据**：`tests/test_model_center_card.py`（**36 用例**）—— 卡片结构、分组、选中态、
绿徽章与卸载入口、🔴 不可选 + 气泡、按钮计数。

---

## 问题 2：全局 AI 助手的落点错了 —— 它不认识"这个学生"

**现象**：顶栏有个全局「AI 谈话助手」，但辅导员想问的是"张三这学期为什么掉了"，
打开助手要从零把成绩念一遍；档案页里反而没有任何谈话入口。

**根因**：AI 能力做成了**独立窗口**（`ui/chat_dialog.py` 的 `ChatDialog`），
它拿不到页面里已经加载好的学生上下文 —— 上下文只能靠用户手输。

**修法**：
* **删掉全局入口**：`ui/main_window.py` 顶栏那个按钮与 `_open_chat_assistant` 一并删除；
* 档案页（`ui/pages/profile_page.py`）新增**第三标签页「AI 辅导」**：学生信息卡 +
  只读谈话记录 + 一行输入区；学生的**成绩 / 德育 / 侧写**以 JSON 注入系统提示词，
  后台线程流式输出；
* 谈话记录**按学生分开存**：`u_<uid>\cache\chat_history_<学号>.json.enc`
  （`CHAT_HISTORY_PREFIX = "chat_history_"`，键里带学号 —— AGENTS.md §9 铁律 4，
  两个人都不填学号时**不许**退化共用一份记录）；
* 缺模型 → 「请先在模型中心下载模型」+ 一个跳转按钮；缺推理引擎 → 给中文原因
  （明确降级，不许静默失败、不许偷偷走云端）。

**判据**：`tests/test_profile_ai.py`（**33 用例**）—— 第三标签页存在、系统提示词注入、
按学生的历史路径、缺模型 / 缺引擎两条降级路径、历史文件是 `.enc` 且扫不到明文。

---

## 问题 3：「总评与侧写」只能看，不能生成、不能改

**现象**：侧写文本只有"AI 侧写"一条生成路径，生成完改一个字都改不了；
总评那一块只显示"有真源才显示"的字段，缺真源时是空白而不是可操作的东西。

**根因**：v1.9 二期的档案页把"显示"做完了，**写回闭环**没做 ——
按钮生成了文本，却没有"编辑 → 保存进加密库"的那一步。

**修法**：档案页「总评与侧写」补全**生成 → 可编辑 → 保存**闭环：文本控件可编辑，
保存落到 `student_profiles.json.enc`（应用级密文，见 §16 例外），
存不进去时给中文提示、不静默丢弃。

**判据**：`tests/test_profile_ai.py`（**33 用例**，与问题 2 同一份用例集）。

---

## 问题 4：单条记录删不掉 —— 只能整库清空

**现象**：录错一条成绩、一条德育分、一份侧写，界面里**没有任何删除入口**；
用户要么忍，要么把整个成绩库删掉重导。

**根因**：v1.9 二期的数据层只做了"整表 `clear()`"这一种删除语义，
`GradeHistoryDB` / `ProfileStore` 上没有"按主键删一行"的 API。

**修法**：
* 数据层（`core/database.py`）：`GradeHistoryDB.delete_record()`（:500）、
  `delete_semester()`（:520）、`clear()`（:488，返回删除行数）；
  `ProfileStore.clear_side_text()`（:898）、`clear_all()`（:918）；
* 界面：档案页成绩 / 德育 / 侧写**三类都支持单条删除**，确认文案逐字为
  **「确定删除此条记录？此操作不可恢复。」**；删掉某学期的**最后一条**成绩时追加一句
  **「该学期已无成绩，是否同时删除学期记录？」**（删学期 = 连学期标签一起收走）；
* ★ **审计留痕**：`core/database.py` 新增 `GradeAuditLog`（:659），落盘
  `<user_dir>/audit/grade_changes.json.enc`（`AUDIT_DIR = "audit"` :120、
  `AUDIT_FILE = f"{AUDIT_DIR}/grade_changes.json.enc"` :122，应用级加密）。
  一条 = `at / operation / student_id / semester / course_name / count`，
  表名 `TABLE_GRADE_AUDIT = "grade_audit"`，操作类型
  `AUDIT_OPERATIONS = (OP_INSERT, OP_OVERWRITE, OP_DELETE)`，上限 `AUDIT_MAX_ENTRIES = 2000`
  （超了只丢最旧的，绝不整份丢）。
  ★★ **只记变动本身，不记分数** —— 审计日志**不是第二份成绩库**（红线 3）；
  ★ 写不进去 / 读不出来**一律降级成警告**：`record()` 返回 `None`、`changes()` 返回 `[]`，
  绝不因为记日志失败把用户那次导入整批作废。

**判据**：`tests/test_grade_delete.py`（**74 用例**）—— 单条 / 单学期 / 整表三种粒度、
返回行数、删最后一条的追问、以及审计日志的字段与"**日志里扫不到分数**"。

---

## 问题 5：「删除成绩库」的判据错了，而且删完**别的页面不刷新**

**现象**：导完成绩再点「删除成绩库」，界面说"库里本来就是空的"，什么也不删；
真删掉之后，成长档案页**还在列着已经删掉的学生**。

**根因**（两条，都是"拿错的东西当判据"）：
1. 旧实现判"有没有成绩库"是**看磁盘上有没有某个文件名**（`grades.enc` / `tags.json.enc`）——
   v1.9 起成绩在 `grade_history.json.enc` 里，于是永远判成"空的"；
2. 第一波只把信号**发**出来了：`SettingsPage.library_cleared` **没有接收方** ——
   档案页左栏名单是智育分析页推进内存的（AGENTS.md §18），清库不会自动把那份名单收回去。

**修法**：
* 判据换成 `core.database.library_inventory()`（:993）—— **只读盘点**，
  返回 `{"account", "grade_rows", "profile_rows", "legacy_files", "class_files",
  "archive", "audit_rows", "files", "has_data"}`；界面只看 **`has_data`**，
  它等于 `bool(grade_rows or profile_rows or files)`：
  ★ **不算 `audit_rows`**（审计日志自己不是成绩库，否则删干净之后下次进来又说"还有数据"）；
  ★ `files` 里**只列真要 `unlink` 的文件**，`grade_history.json.enc` /
  `student_profiles.json.enc` 是"把表清空"、清完文件还在 —— 列进来就又犯了同一类错；
* 删除动作全在 `clear_library()`（:1070，数据层、可单测）：清 `grade_history` 全部记录、
  侧写（勾选项）、归档快照与 `class_*.json.enc` 班级快照（生成物），并**返回删了什么**；
* 界面走**盘点 → 两次确认 → 才删**，删完发
  `SettingsPage.library_cleared = Signal(int, int, int)`（`ui/pages/settings_page.py:144`，
  发射点 :1113，三个数 = 成绩条数 / 侧写份数 / 文件数）；
* 接线：`ui/main_window.py:299` 把这条信号接到 `_on_library_cleared`（:330）——
  用 `getattr` 探一下再调，缺一个不让整条刷新链断在半路；
  刷新失败**只打日志、不弹窗**（§18：删除本身已经成功了，显示问题不该打断用户）；
* 空态：`ui/pages/profile_page.py` 新增公开钩子 `show_no_data()`（:1114），
  清空内存名单 + 谈话上下文 + 渲染空态，提示语逐字为
  **「暂无数据：成绩库已清空，先在「📊 智育分析」导入成绩表。」**
  ★ 它只动显示状态，**不碰** AI 辅导 / 侧写的既有逻辑。

**判据**：`tests/test_grade_delete.py`（**74 用例**：盘点 / 判据 / 三种粒度 /
`clear_library` 的返回 / 审计只报数不参与 `has_data`）。

---

## 问题 6：GPA 一栏没有真源

**现象**：「总评与侧写」里的 GPA 永远是"暂无数据"—— 界面上有这个字段，
但**没有任何地方算过它**。

**根因**：v1.9 二期只规定了"有真源才显示"，GPA 的换算规则一直没定，
代码里自然一行都没有。

**修法**：新建 `core/gpa.py`（**零 Qt、零 HTTP、★ 不 import 封板的 `core.calculator`**），
实现**方案 1 · 标准 4.0**：

| 分数（含下界） | 绩点 |
|---|---|
| ≥90 | 4.0 |
| 85–89 | 3.7 |
| 82–84 | 3.3 |
| 78–81 | 3.0 |
| 75–77 | 2.7 |
| 72–74 | 2.3 |
| 68–71 | 2.0 |
| 64–67 | 1.5 |
| 60–63 | 1.0 |
| <60 | 0 |

`GPA = Σ(绩点 × 学分) ÷ Σ学分`；分数带小数时按边界判（89.5 落在 85 档 → 3.7）。

★ **空 ≠ 0（红线 2）**：没有成绩（`None` / 空串 / `-` / `/` / `未选`）＝ 这门课
**不进分母**，连学分都不算；**数字 `0` ＝ 缺考，学分进分母、绩点按 0 算**；
学分为空的课同样不进分母（权重 0）。★ 一份**全都没有成绩**的名单
→ `gpa_of()` 返回 `None`（"暂无数据"），**不是 `0.0`**。

API：`gpa_of` / `gpa_detail` / `grade_point` / `scheme_options` / `available_schemes` /
`load_scheme` / `save_scheme`；方案选择存**应用级**加密设置 `gpa_settings.json.enc`
（`GPA_SETTINGS_KEY = "scheme"`）。界面在设置页新增「GPA 换算规则」下拉
（`ui/pages/settings_page.py:292`），四个方案**从 `core.gpa.SCHEMES` 读**、
不在界面里抄一份换算表。

**判据**：`tests/test_grade_delete.py`（**74 用例**，第五部分专测 `core.gpa`：
边界分、小数分、空 / 缺考 / 学分为空三种输入、全空返回 `None`、设置读写）。

---

## 问题 7：导入「核对提取结果」界面右栏被挤扁、比例不可调、位置不记忆

**现象**：核对界面是"表在上、提示在下"的竖排；表格列一多就把提示区挤没；
用户每次拖好的分栏位置，关掉窗口就丢。

**根因**：布局用固定方向的堆叠，右栏没有自己的滚动区；窗口初始尺寸偏小；
没有任何"记住布局"的落点（而且**不许写注册表** —— 红线，AGENTS.md §2）。

**修法**：`ui/review_dialog.py` 主体改成横向 `QSplitter`
（默认 `_DEFAULT_SIZES = (700, 300)` ≈ 0.70、初始 `_DEFAULT_SIZE = (1000, 700)`、
两栏最小宽 `_MIN_PANE_WIDTHS = (280, 200)`），右栏套 `QScrollArea` 自己滚动，
表头 `Interactive` + `minimumSectionSize = MIN_COLUMN_WIDTH = 72`（双保险：
用户自己拖也拖不到 72 以下）。

拖动位置存**应用级 ini**：`%LOCALAPPDATA%\SGMS\ui_state.ini`，
键 `SPLITTER_KEY = "grade_import/splitter_state"`（任务书指定，别改）。
★ 用 `QSettings(路径, QSettings.Format.IniFormat)` **显式文件路径** ——
Windows 上 `QSettings()` 默认是 `NativeFormat`，那就会往注册表里写。
读到的值坏了（空 / 非字节 / 比例不合理）一律**静默退回默认**，不抛、不弹窗。

**判据**：`tests/test_import_review_ui.py`（**50 用例**）—— 分栏比例与最小宽、右栏滚动、
表头模式、ini 落点与键名、坏状态 / 空文件 / 目录冒充文件的静默降级。

---

## 与任务书字面的两处差异（如实记录）

1. **GPA 落在新建的 `core/gpa.py`，不在 `core/calculator.py`。**
   任务书字面写的是"GPA 换算（标准 4.0，方案 2/3/4 预留）"，
   位置暗示在计算层；但 `core/calculator.py` 是**铁律一封板文件**（AGENTS.md §2 / §27f，
   sha256 写死在 `main.FROZEN_FILE_SHA256`），一个字节都不许动。
   所以 GPA 单独成模块：它**只吃"课程名 / 学分 / 成绩"这种已经算好、已经在库里的结构**，
   **自己一行加权都不重算**（铁律五：加权算术的真源只有 `core.calculator`）。
2. **删掉的是"全局 AI 助手入口"，`ChatDialog` 这个类保留。**
   `ui/main_window.py` 顶栏的按钮与 `_open_chat_assistant` 已删；
   `ui/chat_dialog.py` 的 `ChatDialog` 与冻结接口
   `open_chat(parent, *, system_prompt=None, initial_prompt=None, prefer_model=None)`
   **保留为内部组件**（签名不许改）—— 档案页的「AI 辅导」是它的**姊妹实现**，
   与它共用同一套 `ui/workers.py` 工作线程契约。

---

## 附带修复（不属于七个问题）

* **`core/crypto_store.delete_user()` 的 `OSError` 隐患**：旧写法只删**顶层文件**、
  随后直接 `user_dir.rmdir()`，而 v1.9 起 `u_<uid>/audit/` 这个子目录**真的会被建出来** ——
  多一个子目录就抛 `OSError: [WinError 145] 目录不是空的`，且异常发生在
  `save_users()` **之前**，结果是"载荷删了、账户记录还在"。现在改成
  **先按"深的先删"清掉整棵子树，再自底向上删目录**，每一步都如实记进返回值；
  删不掉的（占用 / 只读 / 权限）跳过并保留目录，**绝不把没删掉的东西报成删掉了**。
  函数签名与"删账户"的语义**一个字没改**（当前全仓仍无调用者）。
* **`main.py` 的 `CHECK_MODULES` 登记 `core.gpa`**：清单从 42 项变成 **43 项**
  （`--selfcheck` 的模块接线闸门要能看见新模块）。

---

## 未做 / 待定

* **GPA 方案 2 / 3 / 4 只留了预留位**（`SCHEMES` 里 `available=False`，
  界面列出来但禁用、注明"待定"）。换算表是**业务规则**：把某个方案填成
  `available=True` 之前，必须先把它的换算表写清楚，**不许在界面里现编一套**。
* **模型中心卡片未做人工鼠标验收**：悬浮上移 2px、阴影、点选 2px 边框这些
  目前靠结构断言 + 像素断言覆盖；真人在真鼠标下的手感（尤其 150% DPI 下的位移量与
  阴影边缘）还没有人工过一遍 —— 这是已知的验收缺口。
* **审计日志目前"只写不读"**：`GradeAuditLog.changes()` 有只读查询（最新在前、
  可按学生 / 学期过滤），但**界面上还没有查看入口** —— 排查时要从
  `u_<uid>/audit/grade_changes.json.enc` 读。要不要做成页面，等 Owner 定。
* 封板文件（`core/calculator.py`、`core/parser.py`、`core/validator.py`、
  `tests/test_calc.py`）本次**一个字节都没动**，`FROZEN_FILE_SHA256` 四条哈希照旧。
