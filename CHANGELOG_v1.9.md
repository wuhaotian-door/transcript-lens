# CHANGELOG v1.9

本文件记录 **v1.9** 两个阶段的改动。每条写两件事：**做了什么** + **判据（测试文件名 / 闸门名）**。
（施工细则见 `AGENTS.md`，现行规格与判据见 `docs/DEV_SUMMARY.md`；两者的分工见细则第 0 节。）

* **一期**：解析器表格干扰修复 · 德育分批量导入 · 模型中心骨架 · 更新检查改走清单（已入库）
* **二期**：分片下载与拼接 · 模型中心增强 · AI 推理引擎 · 档案合并与成绩覆盖 · 文档与版本号

版本号：**1.8.1 → 1.9**（`core/version.py` 的 `CURRENT_VERSION` 一行）。

---

## 一、v1.9 一期

### 1.1 成绩导入解析器：表格干扰修复

**做了什么**：教务系统导出的成绩表里混着三类「不是学生成绩」的干扰行/列，v1.9 之前会被当成
学生或课程算进加权分母。现在三类都被认出来并明确跳过：

| 干扰 | 长什么样 | 处置 |
|---|---|---|
| **统计列** | 「不及格门数」「平均分」「总分」「平均学分绩」「平均学分绩点」「排名」 | 不建课程、不进加权分母，留一条中文警告 |
| **末尾汇总行** | 「37人 470 2485 …」「不及格 18 6 0 …」 | 不当学生，跳过并记进 `skipped_rows` |
| **顶部合并大标题** | 跨列的「20xx 学年第一学期 学生成绩表」 | 不许占用真表头行（最多给 3 行候选） |

实现都在 `core/parser.py`：

* `MAX_HEADER_ROWS = 3`（`core/parser.py:230`）—— 合并大标题不吃掉真表头；
* `is_stat_header()`（`core/parser.py:559`）—— 统计列判定；关键词在 `core/parser.py:175` 附近；
* `SUMMARY_ROW_KEYWORDS`（`core/parser.py:208`）+ `normalize_header()`（`core/parser.py:426`）
  —— 汇总行判定与表头归一化；
* 跳过的行落进 `ParseResult.skipped_rows: list[tuple[int, str]]`（`core/parser.py:375`），
  界面据此提示「已跳过 {n} 行统计/汇总行（不是学生成绩）：…」（`core/parser.py:1125`）。

**判据**：`tests/test_parser_filters.py`（49 用例）——三类干扰各有一组断言，外加一条
「空 / `-` 仍不等于 0，而显式数字 0 仍然算成绩」的红线回归。

★ **封板文件的解冻是有意的**：`core/parser.py` 是铁律一封板的四个文件之一。`main.py:886-888`
写明「按规矩**有意解冻 parser.py 一个文件**，并在此更新哈希」，并同步更新了
`main.FROZEN_FILE_SHA256` 里 `parser.py` 的 sha256；`core/calculator.py`、`core/validator.py`、
`tests/test_calc.py` 三个哈希**一个字节没动**。改哈希这个动作本身就是审批记录。

### 1.2 德育分批量导入

**做了什么**：奖学金测算要德育分，以前只能一格一格敲。现在可以整表导入：

* `core/import_workers.py`（**零 Qt import**，pytest 直接可测）+ `ui/moral_import_dialog.py`
  （预览）+ `ui/pages/scholarship_page.py`（入口按钮与流程）；
* **三步，每步都能中止**：后台 `plan_import(path, *, sheet_name=0, known_classes=(), default_class="", frame=None)`
  （`core/import_workers.py:399`，**只读、一个字节都不写库**）→ 主线程预览 → 后台
  `apply_plan(store, username, plan, *, batch_id="", error_dir=None)`（`core/import_workers.py:607`）。
  用户取消 = 一个字节都不写；
* **坏行不中断整批**：坏行收进 `errors`，成批导出到
  `export/import_errors_<batch_id>.csv`（utf-8-sig；**一条错都没有就不建文件**）；
* 成功行按 **`sid:<学号>`**（学号，不是姓名 —— 细则第 9 节）追加进
  `class_{班级号}_moral.json.enc`；批次账本写 `moral_batches.json.enc`（应用级加密载荷**只收
  `.enc`**，写成 `.json` 会被 `save_encrypted` 当场拒绝）；
* 分数区间 `MORAL_MIN = 0` / `MORAL_MAX = 100`；列名自动识别（`detect_columns()`，配
  `ColumnMap.missing` / `describe` 给中文提示）；
* ★ **班级必须已经建过**：`raw_class` 不在已知班级里就报错（一个班都没建过也要报）——
  否则德育分写进一个没有名单的班，数据进去了、界面上一个人也看不到。

**判据**：`tests/test_import.py`（40 用例）。

### 1.3 AI 模型中心（骨架）

**做了什么**：

* `core/ai/model_registry.py`（**零 Qt、零 HTTP import** —— 字节流借 `core.updater.download_exe`，
  照细则第 28 节的模式，见 `AGENTS.md` §28）+ `ui/model_center_dialog.py`；
* 清单来自 Gitee raw 的 `update/ai_models.json`；**读不到只是警告、界面照开**
  （给「复制链接手动下载」兜底）；
* ★ **装没装好以磁盘为准**：`installed.json` 只是账本，`merged_with_local()` 让磁盘上真有的
  `.gguf` 优先；写账本的门是「size 差在 `SIZE_TOLERANCE`（0.01，下限 4096 字节）以内**且**
  sha256 对得上」；
* 内存闸门：`minRamGb` 写成 `"8GB"` 要认得出；认不出必须给中文警告，**不能退化成 0**
  （见 `need <= 0` 就放行等于没闸门）；内存不够的行 `flags` 里**没有 `ItemIsEnabled`**，
  整行不可选（不只是不勾）；
* 绝不自动下载；失败重试 1 次 → 标红 + 复制链接。

**判据**：`tests/test_model_registry.py`（109 用例）+ `%TEMP%\tl_v19_ui_test.py`
（假 `fetch_model_list` / `merged_with_local` / `download_exe`，并量像素）。

### 1.4 更新检查改走 `update/manifest.json`（清单优先）

**做了什么**：`core/updater.check_for_update()` 先读 Gitee raw 的 `update/manifest.json`
（`GITEE_RAW_MANIFEST`，`SOURCE_MANIFEST = "manifest"`）；清单可用就走 `_from_manifest()`
（`core/updater.py`），否则**退回老的发行版正文路线**（`SOURCE_RELEASE = "release"`，
deprecated，仅兜底 —— 行为与 v1.8 完全一致，不炸老用户）。

* ★ **顶层必须是 `app` 段**：写成 `files.app` 会让 `parse_manifest()` 返回
  `(None, "清单里没有 app 段")` 并**静默退回老路线** —— 清单结构检查因此显式要求 `app`；
* 四种状态一个字没改：`newer` / `current` / `no-release` / `error`；
  `check_latest_version()` 的契约也一个字没改（成功给版本号字符串、失败给 `None`）；
* `MANIFEST_SCHEMA = 1`。

**判据**：`tests/test_manifest.py`（67 用例）。

### 1.5 施工细则新增 §31 / §32

**做了什么**：`AGENTS.md` 补两节 —— §31 模型中心、§32 批量导入德育分；把「判据与踩过的坑」
留在细则，把「现行规格」留给 `docs/DEV_SUMMARY.md`。

---

## 二、v1.9 二期

### 2.1 主程序分片下载与拼接（Gitee 单附件上限 100MB）

**做了什么**：240MB 的 exe 传不进 Gitee 的单个附件（社区版上限 100MB），所以主程序也分片发布：

* `core/updater.download_parts(parts, dest_path, progress_callback=None, *, timeout=DOWNLOAD_TIMEOUT, resume=True, cancel=None, expected_sha256="", expected_size=0, min_bytes=MIN_EXE_BYTES)`
  （`core/updater.py:1208`），六条硬保证：
  1. 逐片核 `part.sha256`，坏片丢弃重下；
  2. 拼接顺序 = **清单 `app.parts` 的顺序**（不看文件名排序）；
  3. 拼完再核整体的 `expected_sha256` + `expected_size`，任何一条不过**不写目标文件**；
  4. 全程只写 `<dest>.part`，成功了才 `os.replace`（失败/取消**绝不损坏已有 exe**）；
  5. 可续传 —— 每片缓存在 `<dest>.parts/partNN`，已校验的片连请求都不发；
  6. 全部成功后才清 `<dest>.parts/`；
  * 空清单抛 `DownloadError("没有任何分片可下 —— 清单里的 app.parts 是空的。")`；
* 常量：`ATTACHMENT_LIMIT_BYTES = 100MB`（`core/updater.py:202`）、`CHUNK_SIZE = 8192`、
  `DOWNLOAD_TIMEOUT = 30`、`MIN_EXE_BYTES = 1MB`；
* `update/manifest.json` 的结构：`app.size` / `app.sha256` 指**整个 exe**，`app.parts` 每片带
  自己的 `url` / `size` / `sha256`。现行值：`version = "1.8.1"`、`size = 240099567`、
  3 片各 `80033189` 字节；
* `tools/split_release.py`（135 行，**不联网、不上传**）：顺序切片成
  `transcript-lens-v<version>.exe.partN` → 逐片自检 ≤ `ATTACHMENT_LIMIT_BYTES` → 打印可直接
  粘进清单 `app` 段的 JSON。用法：
  `python tools\split_release.py dist\transcript-lens.exe --parts 3 --tag <标签> --version <版本> --notes "…" --out release`。

**判据**：`tests/test_updater_parts.py`（15 用例）+ `tests/test_manifest.py`（67 用例，含
`app.parts` 的解析分支）；发版流程与真机实测见 `docs/DISTRIBUTION.md` 的「二·五 分片发布」
与 `docs/RELEASE_SOP.md`。

### 2.2 模型中心增强（细则 §33）

**做了什么**：

* **负载三档** `LOAD_EASY` / `LOAD_TIGHT` / `LOAD_HARD` =「轻松 / 勉强 / 困难」，
  标签 `LOAD_LABELS`、配色 `LOAD_COLORS`（绿 / 橙 / 红 —— **不用 emoji**，见细则 4l）；
* ★ 判据的**唯一实现**是 `_load_level_of(model, have_ram, have_vram)`
  （`core/ai/model_registry.py:866`）：内存 ≥ `minRamGb` 且（没声明 `minVramGb`，或有独显且显存够）
  → easy；否则内存 ≥ `minRamGb × RAM_TIGHT_RATIO` → tight；否则 hard；
  `RAM_TIGHT_RATIO = 0.7`（`core/ai/model_registry.py:133`）；
  `load_level(model, system=None, *, ram_gb=None, vram_gb=None)`（`core/ai/model_registry.py:918`）
  **最多探测一次**，不声明显存的条目**不启 `nvidia-smi`**（省一次子进程）；
* ★ **内存探不到一律放行**（判困难会让探测失败的机器一条都勾不上）；「显存未知」≠「内存未知」，
  那种按 0.7 判。`load_note()`（`core/ai/model_registry.py:957`）照实写「未探测到本机内存 /
  需要 N GB，按最低要求估算」—— **不许把"不知道"写成"是 0"**；
* ★ **默认只勾「轻松」**：`default_checked(models, *, ram_gb=None, vram_gb=None)`
  （`core/ai/model_registry.py:770`）语义**有意改成**「本机跑它轻松」，**不再看清单里的
  `defaultCheck`**（那个字段降级为「推荐」角标）；
* 困难行没有 `ItemIsEnabled`（`ui/model_center_dialog.py:610-611`）、下载按钮置灰 + 气泡说明；
* **卸载只许用户主动点**：确认框文案逐字为 `UNINSTALL_CONFIRM_TEXT = "卸载后将无法使用 AI 功能，确定？"`
  （`ui/model_center_dialog.py:116`），确认后 `delete_model()` + `forget_installed()`；
* 下载重试 1 次仍失败 →「复制链接」兜底；
* `core/system_info.py`（367 行，**零第三方依赖**）：`ctypes` 的 `GlobalMemoryStatusEx`
  （`_MEMORYSTATUSEX`，`core/system_info.py:84`）+ `nvidia-smi` 子进程（`GPU_EXE`，
  `core/system_info.py:65`；`GPU_TIMEOUT_S = 5.0`）。API：`memory_status()` /
  `total_ram_gb()` / `available_ram_gb()` / `logical_cpu_count()` / `gpu_infos()` /
  `discrete_gpu_vram_gb()` / `snapshot(*, force=False, probe_gpu=True)` / `clear_cache()` /
  `describe_system()`。
  **本机实测**：内存 15.38 GB、可用约 4.4~4.6 GB、24 个逻辑核、
  NVIDIA GeForce RTX 5070 Ti Laptop GPU 11.94 GB 显存（= 12227 MiB）。

**判据**：`tests/test_model_registry.py`（109 用例）+ `tests/test_model_center_enhanced.py`
（47 用例）+ `%TEMP%\tl_v19_ui_test.py`（量像素）。规格与文案见 `docs/DEV_SUMMARY.md` §二十四。

### 2.3 AI 推理引擎（细则 §34）

**做了什么**：

* `core/ai/inference.py`（634 行）+ `ui/chat_dialog.py`（882 行）；主窗挂「AI 谈话助手」入口；
* ★ **零 HTTP import，且 import 期绝不 import `llama_cpp`**：`ENGINE_MODULE = "llama_cpp"`，
  `engine_available() -> tuple[bool, str]`（`core/ai/inference.py:114`）只用
  `importlib.util.find_spec` 探一探，真正的 import 只发生在 `_import_engine()`
  （`core/ai/inference.py:140`，全模块唯一碰 import 的地方）；
* ★ **明确降级，不许静默失败、不许偷偷走云端**：没引擎时给中文原因（`NO_ENGINE_MESSAGE`）
  并抛 `InferenceUnavailable`；`generate()` / `stream()` 里任何异常都翻成
  `InferenceUnavailable(friendly_error(exc))`，**绝不返回空串**；
* 公开 API：`LocalModelEngine(model_path, *, n_ctx=DEFAULT_N_CTX, n_threads=None, chat_format=None)`
  （`core/ai/inference.py:416`）、`load()`（幂等）、
  `generate(prompt, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE, *, stop=None) -> str`
  （`core/ai/inference.py:514`）、`stream(...) -> Iterator[str]`（`core/ai/inference.py:543`，
  **立刻**判「没加载」，不等到第一次 `next()`）、`unload()` / `close()`、
  `list_local_models(dest_dir=None)`（`core/ai/inference.py:347`，只列 easy / tight ——
  `USABLE_LEVEL_KEYS`）。默认 `DEFAULT_N_CTX = 2048` / `DEFAULT_MAX_TOKENS = 512` /
  `DEFAULT_TEMPERATURE = 0.7`；
* `llama-cpp-python` 是**可选依赖**：不进 exe、不动 `build_exe.ps1`（`requirements.txt` 里只写注释）；
* `ui/chat_dialog.py`：**聊天历史不落明文**（红线 3），存应用级加密的
  `chat_history.json.enc`（`CHAT_HISTORY_FILE`，走 `core.user_store`）；
  `MAX_TURNS_PER_DAY = 200` / `MAX_CONTEXT_TURNS = 12` / `MAX_PROMPT_CHARS = 4000`；
  生成在 `_ChatWorker(QThread)` 里跑（主线程不阻塞）；引擎不可用时**窗口照样开**，只禁用输入，
  不弹一串模态框；
* **冻结接口**（档案页要用）：`open_chat(parent, *, system_prompt=None, initial_prompt=None, prefer_model=None)`
  （`ui/chat_dialog.py:851`）—— 签名冻结，不许改。

**判据**：`tests/test_inference.py`（44 用例）。

### 2.4 档案合并与成绩覆盖（细则 §35）

**做了什么**：

* `core/database.py`（596 行）是**逻辑表 API**（表名 / 字段 / 删除语义照任务书），底层仍是
  `core/user_store` 的**应用级加密**：`grade_history.json.enc` + `student_profiles.json.enc`
  —— ★ **不上明文 sqlite**（红线 3）；
* 表与主键：`TABLE_GRADE_HISTORY = "grade_history"`（`core/database.py:77`）、
  `TABLE_STUDENT_PROFILES = "student_profiles"`（`core/database.py:79`）、
  `GRADE_HISTORY_FIELDS`（`core/database.py:86`）、
  `GRADE_HISTORY_PK = ("student_id", "semester", "course_name")`（`core/database.py:96`）；
* `GradeHistoryDB.apply(records, *, semester, batch_id, policy=USE_OVERWRITE, on_conflict=None) -> dict[str, int]`
  （`core/database.py:270`）返回 `{"overwritten": n, "inserted": m, "skipped": k}`；
  `USE_OVERWRITE = "overwrite"` / `USE_SKIP = "skip"`（`core/database.py:99-100`）；
  * `on_conflict` 回调**每个学生问一次**；回调漏掉的学生按**跳过**处理（最保守）；
    回调抛异常 → **整批跳过，一个字不写**；
  * 没有冲突的学生按 `policy` 直接写 —— ★ 第一版这里写反了，导致「没有冲突的导入什么都不写」，
    `core/database.py:356` 附近的注释记着这个坑；
  * `ConflictQuestion(student_id, student, semester, existing)`（`core/database.py:106`）；
    `student` 只给弹窗显示用，**不落库**（表里没有这一列）；
* 界面文案（逐字）：冲突弹窗「检测到学生 {姓名} 已有 {学期} 成绩，是否覆盖？」
  （`ui/pages/analysis_page.py:371-373`）+ 四个按钮**覆盖 / 跳过 / 全部覆盖 / 全部跳过**；
  结束弹「覆盖 N 条 / 新增 M 条 / 跳过 K 条」（`ui/pages/analysis_page.py:520-524`）。
  覆盖 = 删旧写新；跳过 = 保留旧记录、该行不导入；
* `ProfileStore`（`core/database.py:487` 起）：`side_text()` / `all_profiles()` /
  `save_side_text()`；
* 档案页两个标签页：「成绩明细」与「总评与侧写」（`ui/pages/profile_page.py:759-760`）；
  「AI 侧写」按钮懒 import `open_chat()` 并 try/except（`ui/pages/profile_page.py:1396`），
  侧写存进加密库；没登录时提示「侧写暂时存不进加密库（没登录？）」
  （`ui/pages/profile_page.py:1418`）。★ 页面**不许挂 `QGraphicsEffect`**（细则 4b / 17）。

**判据**：`tests/test_grade_overwrite.py`（36 用例）+ `tests/test_profile_merge.py`（28 用例）。

### 2.5 文档

**做了什么**：

* `AGENTS.md` **瘦身到 ≤ 64000 字节**（瘦身前 63974 字节）：把各节里「当时的现场 / 推导 / 量法」
  成批挪进 `docs/DEV_SUMMARY.md` 的 §二十三，细则里只留规则与判据；新增 §33 / §34 / §35 三节；
* **新增 `docs/RELEASE_SOP.md`**：端到端验收 → 回滚方案 → 故障对照表；
* `docs/DISTRIBUTION.md` 新增「二·五 分片发布（v1.8.1 起）」，并把 AI 模型分发策略、发版检查
  清单、排查顺序、相关代码与测试补全（端到端验收 / 回滚指向 `docs/RELEASE_SOP.md`）；
* `docs/DEV_SUMMARY.md` 新增 **§二十四**：v1.9 二期三块功能（模型中心增强 / AI 推理引擎 /
  档案合并与覆盖）的现行规格与判据。

**判据**：`main._docs_gate_report()`（`--selfcheck` 里跑）——`docs/` 下只认
`DEV_SUMMARY.md` / `UPDATE_GUIDE.md` / `DISTRIBUTION.md` / `RELEASE_SOP.md` 四个文件，
出现白名单之外的文件、或任何源码 / 文档提到已删除的旧文档名，一律 FAIL。

### 2.6 版本号 1.8.1 → 1.9（含迁移链补一跳）

**做了什么**：

* 只改 `core/version.py` 的 `CURRENT_VERSION = "1.9"` 一行；界面上的版本号一律
  `f"v{CURRENT_VERSION}"`（细则 23a：UI 里一个版本号字面量都不许有）；
* `core/migrator.py` 的 `_STEPS` **补一跳** `("1.8.1", "1.9", _lift_1_8_1_to_1_9)` —— 哪怕没有
  数据结构变化也要补（细则 23b：不补的后果是老用户启动直接
  `MigrationError: 没有从 v1.8.1 升级的路径`）。

**判据**：`tests/test_version_migration.py`（21 用例）+ `main._version_source_report()`
（静态扫 `ui/**/*.py` 的版本号字面量 + 行为读三处界面版本值）+ `--selfcheck` 的迁移链检查。

---

## 三、未纳入本版（别在文档里假装做了）

* **批量导入德育分之后的「AI 分析班级整体情况」没有做**。v1.9 的 AI 能力只有
  `ui/chat_dialog.py` 的谈话助手 + 档案页的「AI 侧写」，**没有**把整班成绩 / 德育分喂给模型的
  入口。真要做得先想清楚脱敏（`core/ai/` 的 `redact_student`）与红线 3（明文成绩不落盘），
  所以不在本版。
* **云端大模型仍然只有骨架**（细则第 13 节白名单里的第 2 条）：设置页能开、能填 API Key，
  但没有接上任何真实调用。

## 四、测试清点

`python -m pytest tests/ -q`（用例计数为文档写作时 `--collect-only` 的结果）：

| 测试文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_calc.py` | 8 | 铁律一：加权算术（封板） |
| `tests/test_parser_filters.py` | 49 | 表格干扰三类（统计列 / 汇总行 / 合并大标题） |
| `tests/test_import.py` | 40 | 德育分批量导入（计划 / 预览 / 落库 / 坏行） |
| `tests/test_model_registry.py` | 109 | 模型清单、内存闸门、负载三档、账本 |
| `tests/test_model_center_enhanced.py` | 47 | 模型中心增强（默认勾选 / 卸载 / 失败兜底） |
| `tests/test_manifest.py` | 67 | 更新清单解析与检查优先级 |
| `tests/test_updater_parts.py` | 15 | 分片下载与拼接 |
| `tests/test_inference.py` | 44 | 本地推理引擎（引擎缺失降级、流式、懒 import） |
| `tests/test_grade_overwrite.py` | 36 | 逻辑表：覆盖 / 跳过 / 冲突回调 |
| `tests/test_profile_merge.py` | 28 | 档案合并与标签页数据源 |
| `tests/test_version_migration.py` | 21 | 版本链与数据迁移 |

合计 **464** 用例；另有 `%TEMP%` 下的 GUI / 闸门探针（`tl_qt_drive.py`、`tl_label_lint.py`、
`tl_v19_ui_test.py` 等），以及 `main.py --selfcheck`（10 条闸门）与 `--bootcheck`。
