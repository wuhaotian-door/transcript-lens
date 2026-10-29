# CHANGELOG v1.9.2 —— 学生主档、两级档案页、一键装推理后端

> 版本号真源：`core/version.py` 的 `CURRENT_VERSION = "1.9.2"`。
> 数据版本：`_lift_1_9_1_to_1_9_2`（`core/migrator.py`，纯标记迁移，不动数据）。
> 封板四文件（`core/calculator.py` / `core/parser.py` / `core/validator.py` /
> `tests/test_calc.py`）**一个字符都没动**，`main.FROZEN_FILE_SHA256` 原样通过。

本次四块功能：

1. **学生主档**（`core/database.py`）：`students.json.enc` 成为"这个班有哪些学生"的唯一真源，
   支持建/改/归档/删除、导入自动补档、**改学号全量迁移**，并把每一次写操作记进审计日志。
2. **档案页二级视图**（`ui/pages/profile_page.py` + 新增 `ui/pages/profile_shared.py`、
   `ui/pages/semester_side_profile.py`）：左边学生名单（搜索、新建、编辑、删除），
   右边详情；详情里一条学期时间轴，点某一学期就进"那个学期的学业侧写"。
3. **推理后端一键安装**（新增 `core/ai/runtime_setup.py` + `ui/widgets/backend_setup.py`）：
   模型中心里多一张三态卡片（未装 → 安装中 → 已就绪），装到程序自己的目录里，
   不污染系统 Python。
4. **几处 v1.9.1 遗留问题的收口**：清成绩库时把该生的 AI 聊天缓存一起清掉（并在确认框里写明）、
   设置页"删除成绩库"的状态文案改成按**成绩历史表是否为空**判断。

---

## 一、学生主档（`core/database.py`）

新增 `StudentStore`（同样走应用级加密，落在 `students.json.enc`）：

| 方法 | 说明 |
|---|---|
| `rows(include_archived=False)` / `all_rows()` | 名单（默认不含归档） |
| `get(sid)` / `has(sid)` / `count()` / `names()` | 单条与计数 |
| `search(text, include_archived=None, limit=200)` | 按学号/姓名/班级/备注模糊搜 |
| `add(*, student_id, name, class_name="", note="", source="manual")` | 新建，学号重复抛 `DuplicateStudentError` |
| `update(sid, **changes)` | 改姓名/班级/备注（改学号要走迁移） |
| `upsert_import(*, student_id, name, class_name="", source="import")` | 导入时**补档**：没有就建，有就只补空字段 |
| `archive(sid)` / `restore(sid)` / `remove(sid)` / `clear()` | 归档 / 恢复 / 彻底删 / 清空 |

- **学号只是标识，不是主键约束**：`_student_id_of()` 统一补 `sid:` 前缀，`_bare_student_id()`
  负责拆回来，避免"同一个学生两种写法"。
- **改学号 = 全量迁移**：`migrate_student_id(old, new, *, store, username, uid)` 一次性把
  ① 成绩历史（主键含学号，需 `GradeHistoryDB.rekey_student()` 重写，并处理"同一学期同名课程撞键"）、
  ② 侧写（含学期侧写）、③ AI 聊天缓存文件、④ 德育分、⑤ 意愿标签、⑥ 班级快照里的键
  全部搬到新学号；返回 `StudentMigrationReport`，`as_text()` 给界面弹"迁了几条"。
- **审计日志**（`audit/grade_changes.json.enc`）：`AUDIT_OPERATIONS` 由 3 种成绩操作扩到
  9 种（`student_add` / `student_edit` / `student_archive` / `student_restore` /
  `student_delete` / `student_migrate` 等），每条记录带 `detail`（**只写学生姓名、旧新学号这类
  索引信息，绝不写分数**）。`AUDIT_MAX_ENTRIES = 2000` 封顶，老的自动滚掉。
- **ProfileStore 扩展**：`semester_sides()` / `semester_side_text()` /
  `save_semester_side_text()`（学期侧写存在 `student_profiles.json.enc` 的 `semester_side`
  行里，一个学生一行一学期）、`move_student()`、`remove()`。
- **`clear_library()` 补一刀**：勾了"同时清侧写"时，把 `cache/chat_history_*.json.enc`
  一并删掉（`library_inventory()` 里新增 `chat_files` 计数与 `cache/...` 条目）。

## 二、档案页：学生名单 + 两级视图

`ui/pages/profile_page.py` 从"左侧名单 + 右侧三标签页"改成 **`QStackedWidget` 两级**：

- **第 0 层 = 详情**：左栏学生名单（`QListView` + 自定义 delegate，带头像色块与学号），
  上方搜索框（占位"搜索姓名或学号"，输入"已归档"看归档名单），下方「新建」「编辑」「删除」；
  右栏是学生卡 + **学期时间轴**（`TermNode` 一排节点，来自成绩历史里真实出现过的学期）+ 三个标签页。
- **第 1 层 = 学期侧写**：新增 `ui/pages/semester_side_profile.py` 的
  `SemesterSideProfilePage`（继承 `SemesterSideProfile`，多一个「← 返回」）。
  顶部是该学期的四宫格（加权均分 / 算术均分 / 学分合计 / 门数），左边课程表，
  右边「本机 AI 学期侧写」（没有引擎就明确说明，不许静默失败）。
- **主窗接线**（`ui/main_window.py`）：智育分析页双击 → 切到档案页 →
  `show_student()` → `ask_semester_and_open()` 弹学期选择 → 进第 1 层；
  名单变化时 `_on_roster_changed()` 顺手把学生**补进主档**（`upsert_import`，失败只打 warning）；
  清库后 `_on_library_cleared()` 优先调 `reset_view()` 把两级都复位。
- **删除学生**（`ui/pages/profile_page.py`）：「仅归档」= 从名单隐藏、数据全留；
  「彻底删除」= 二次确认后删成绩历史 + 侧写（含学期侧写）+ 聊天缓存文件。
- 共用件抽到 `ui/pages/profile_shared.py`（1805 行）：`StudentBook` 主档门面、
  `TermNode` 学期节点、`open_student_book()`，以及从 v1.9.1 `profile_page.py` 原地搬来的
  纯逻辑 helper（`format_score` / `group_grades_by_term` / `build_student_json` …），
  **一个值都没改**。

## 三、推理后端一键安装

- `core/ai/runtime_setup.py`（589 行，零 Qt、零 HTTP import）：
  - 装到 **`%LOCALAPPDATA%\SGMS\runtime\site`**（`pip install --target`），
    `ensure_runtime_on_path()` 把它插到 `sys.path` 前面；**绝不碰系统 Python**。
  - `find_system_python()` 依次试 `sys.executable`、`python`、`py -3`、常见安装目录
    （不读注册表；本机返回 `C:\Users\ROG\anaconda3\python.EXE`）。
  - `pip_argv()` / `install_command_text()`：默认带
    `-i https://mirrors.aliyun.com/pypi/simple/`（国内直连 PyPI 常超时），
    `INSTALL_TIMEOUT = 600`。
  - `install(*, on_line, cancel, python, timeout) -> InstallResult`：后台线程跑 pip，
    逐行回调；可取消；失败给中文原因与"复制命令"兜底。
  - `is_backend_available()` / `backend_status()`：先 `ensure_runtime_on_path()` 再试
    `import llama_cpp`，失败给中文原因。
- `ui/widgets/backend_setup.py`（398 行）：三态卡片（未装 → 安装中（滚动日志，最多 2000 行）
  → 已就绪），按钮「一键安装」「取消」「复制命令」，全部中文文案。
- `ui/model_center_dialog.py`：引擎没装时在说明下方插这张卡片（`_make_backend_card()`）；
  `core.ai.runtime_setup` 还没落地时**按"可用"处理**，模型中心照常打开。

## 四、几处收口

- `ui/pages/settings_page.py`：「删除成绩库」的状态与按钮改按
  `library_inventory()["grade_rows"]` 判断（旧版查的是早已废弃的 `grades.enc`，于是
  "档案页有学生、这里说没有成绩库"）；`showEvent` 每次进页刷新状态；确认框补一行
  「· 该生的 AI 聊天缓存也会一并清除」。
- `ui/pages/analysis_page.py`：「侧写」按钮的气泡改成「在成绩档案页查看该生的学期侧写」，
  按钮本身只发信号（跳页由主窗做）。
- `main.py`：`CHECK_MODULES` 加 `ui.pages.profile_shared` / `ui.pages.semester_side_profile` /
  `ui.widgets` / `ui.widgets.backend_setup` / `core.ai.runtime_setup`。
- `AGENTS.md`：新增 §36（推理后端一键安装）与 §37（穿透导航与学生主档）。

## 五、测试

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_student_crud.py`（新） | 50 | 主档增删改归档、学号迁移六个来源、**裸学号归一 4 条**、审计、聊天缓存清除 |
| `tests/test_archive_navigation.py`（新） | 74 | 档案页两级视图、学期节点、学期侧写页 |
| `tests/test_runtime_setup.py`（新） | 46 | 安装目录、sys.path、pip 参数、取消、失败文案、后端状态 |
| `tests/test_profile_navigation.py`（新） | 30 | 名单来源、搜索、新建/编辑（迁移）/删除、进学期页与返回 |
| `tests/test_model_center_card.py`（新） | 36 | 卡片流：分组、选中回写、已安装/困难不可选、下载按钮文案 |
| `tests/test_profile_ai.py`（新） | 33 | 档案页「AI 辅导」：学生 JSON 注入、流式、按生隔离、三条降级 |
| `tests/test_grade_delete.py`（改） | 77 | 原 74 条 + 3 条聊天缓存随清库删除 |
| `tests/test_import_review_ui.py`（改） | 50 | 加"模态弹窗不许泄漏"的 autouse 夹具（修跨文件污染） |
| `tests/test_profile_merge.py`（改） | 29 | 3 条断言跟着档案页三标签页改 |

全量：`pytest tests/ -q` → **865 passed / 0 failed / 0 errors（416.91s）**
（v1.9.1 基线 660 passed；本次新增 **6 个测试文件共 269 条**、改 3 个文件共 6 条）。

## 六、已知限制（记录，不是待办）

- **GPA 只有方案 1**（标准 4.0 换算），方案 2/3/4 在 `core/gpa.py` 里是保留位；
  排名仍然没有真源（`core/parser.py` 只把"排名"当统计列排除），档案页如实显示"暂无排名"。
- **后端安装依赖系统里有 Python**：都找不到时只给「复制命令」兜底，不改 PATH、不写注册表。
- **pip 走阿里云镜像**：内网/离线环境装不上时同样是"复制命令"兜底，不会静默失败。
- 聊天缓存随"删除成绩库"清除的条件是**勾选了"同时清侧写"**（不勾则按辅助数据保留）。
- `Qt` 的 `setTextAlignment(int)` 在 `ui/pages/profile_page.py` 里仍有 DeprecationWarning（无害）。
- `tests/test_profile_navigation.py::test_ui_texts_have_no_markdown` **只扫档案详情页**：在同一进程里先 `open_semester()` 再 `findChildren()` 有可复现的卡死（实测把整条用例挂住），学期侧重页的文案改由离屏 lint 复核。
- **`show_no_data()` 不删学生主档**：它只清当前视图与名单缓存；主档里还有学生时，
  名单照常显示（"删除成绩库"删的是成绩/侧写/聊天，不是人）。两条语义都写了用例。
- 名单下方说明行是「筛出 N / M 人」（搜索态），不是"共 M 人"——`tests/test_profile_navigation.py`
  按实测文案断言。
- `open_semester()` 在没有选中学生、或学期名为空时**静默 no-op**（不弹窗、不切页）；
  这是刻意的：它挂在"点学期节点"的鼠标事件上，弹窗会打断连续点击。

## 打包验证的两个坑（已修进脚本）

1. **%TEMP% 太脏时，单文件 exe 每次解包都以 -1 退出**，连 `--selfcheck` 的报告都不写；
   换成新建的空目录就立刻 `RESULT: ALL OK`。所以 `build_exe.ps1` 现在给 exe 单开
   `build\_selfcheck_tmp`，跑完还原 `TEMP/TMP` 并删掉。
2. **报告落点要有兜底**：`main.run_selfcheck()` 依次尝试"临时目录 → 当前目录 → exe 所在目录"
   写报告（打包脚本也三处都找）。没拿到 `%TEMP%` 时至少不会"明明跑过了却找不到报告"。
3. 从 bash 调 PowerShell 时**不要把 `TMP` 留在 `/tmp`**：单文件 exe 解包要用真实的
   Windows 临时目录，否则 bootloader 在解包阶段就死，表现是退出码 -1、无任何输出。
