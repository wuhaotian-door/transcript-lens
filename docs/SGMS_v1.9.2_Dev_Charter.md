# SGMS v1.9.2 开发章程

> 本文件是 v1.9.2 的**可交付版开发章程**：立项背景、现行规格、架构决策、验收判据、
> 测试与打包证据，一册读完就能接手。
>
> **与其它文档的分工**（别搞混）：
>
> | 文档 | 角色 |
> |---|---|
> | `docs/DEV_SUMMARY.md`（总纲） | **规格真源** —— 全项目现行规格、已关闭规则、架构、版本史 |
> | 本文件 | v1.9.2 这一版的**交付册**：把总纲 §二十六 展开成人能读的完整叙述 |
> | `CHANGELOG_v1.9.2.md` | 改了什么、为什么改、判据在哪（给升级的人看） |
> | `AGENTS.md` | **硬性施工规则**（铁律、踩坑、闸门），动代码之前必读 |
>
> 三处同时写错才叫错，任何一处与另外两处打架 = 缺陷。**改规格时三处一起改。**

---

## 一、版本与基线

| 项 | 值 |
|---|---|
| 版本号 | **1.9.2**（`core/version.py` 的 `CURRENT_VERSION`，唯一真源） |
| 迁移链 | `1.0 → 1.6 → 1.7 → 1.8 → 1.8.1 → 1.9 → 1.9.1 → 1.9.2` |
| 上一版基线 | v1.9.1：全量 `pytest` **660 passed** |
| 本版测试总量 | **865 条**（19 个测试文件，`pytest --collect-only` 实测） |
| 封板文件 | `core/calculator.py` / `core/parser.py` / `core/validator.py` / `tests/test_calc.py`，sha256 写死在 `main.FROZEN_FILE_SHA256`，**本版一个字节都没动** |
| 技术栈 | Python 3.13 + PySide6 6.9 + pandas 2.3 + xlrd 2.0.2 + openpyxl + cryptography |
| 禁止 | `requests` / `httpx` / `aiohttp`（联网只许 `urllib.request`，且必须用户主动触发） |

### 1.1 本版四个任务

1. **模型中心改卡片流布局**（悬浮上浮 + 阴影、点选、底部统一下载、已安装徽章与卸载、
   困难模型置灰、按 `group` 分组）。
2. **删掉全局「AI 谈话助手」入口**，把 AI 辅导**嵌进学生档案页**（学生数据注入系统提示、
   流式输出、按学生加密历史、缺模型/缺引擎明确降级）。
3. **成绩档案页支持四级访问**：主档学生增删改、成绩库里索引不到的学生、彻底删除与归档、
   学号变更全量迁移。
4. **推理后端一键安装**（应用私有 pip 目标目录 + 三态卡片），并把「学期侧写」页并用同一张卡片。

### 1.2 不可违背的六条硬约束

1. 不引入任何联网请求库；所有下载仍走 `core/updater` 的 `urllib` 字节流。
2. AI 推理**全程离线**，不走云端 API。
3. 封板文件 sha256 不变（要解封是 Owner 的决定，不是"顺手改绿"）。
4. 新文件必须登记进 `main.CHECK_MODULES`（自检会 import 全量模块）。
5. 不许把已有测试跑挂；`pytest tests/` 必须全绿。
6. 打包必须过：`build_exe.ps1 -Both`，随后 exe 的 `--selfcheck` 与 `--bootcheck` 全绿。

---

## 二、架构总览

```
main.py                 启动流程 / 命令行开关 / --selfcheck（全部闸门住在这里）
core/                   纯逻辑，零 Qt
  ├─ parser.py          封板：Excel → Parsed（课程 / 学生 / 成绩）
  ├─ calculator.py      封板：三口径加权
  ├─ validator.py       封板：学分锚点校验
  ├─ archival.py        纵向档案（学期累加）
  ├─ crypto_store.py    密码级库 + 用户索引
  ├─ user_store.py      应用级加密载荷（自动登录也能读）
  ├─ database.py        ★ v1.9 起的"逻辑表 API"：成绩历史 / 侧写 / 学生主档 / 审计
  ├─ gpa.py             ★ v1.9.1：GPA 换算方案（方案 1 = 标准 4.0）
  ├─ scholarship.py     奖学金测算（只做 ×0.7 + 德育×0.3 + 排序 + 截名额）
  └─ ai/
      ├─ model_registry.py   模型清单 + 负载三档（零 Qt / 零 HTTP）
      ├─ model_downloader.py 下载落盘（字节流借 updater）
      ├─ inference.py        LocalModelEngine（llama_cpp 懒加载）
      └─ runtime_setup.py    ★ v1.9.2：pip --target 装到应用私有目录
ui/                     PySide6
  ├─ main_window.py     无边框主窗 + 侧栏 + 页面栈 + 信号接线
  ├─ pages/
  │   ├─ analysis_page.py      智育分析（导入 / 三档筛选 / 侧写入口）
  │   ├─ profile_page.py       ★ 学生档案：名单 + 两级视图 + 三标签页
  │   ├─ profile_shared.py     ★ v1.9.2：StudentBook / TermNode / 学期侧写读写
  │   ├─ semester_side_profile.py ★ v1.9.2：学期侧写页（单学期上下文）
  │   ├─ archive_page.py / scholarship_page.py / settings_page.py / account_page.py
  ├─ model_center_dialog.py   ★ 卡片流模型中心
  ├─ review_dialog.py         导入核对（QSplitter 可拖动）
  ├─ widgets/backend_setup.py ★ v1.9.2：推理后端三态卡片
  └─ chat_dialog.py           内部 ChatDialog（全局入口已删，留作组件）
```

### 2.1 数据落盘（全部在 `%LOCALAPPDATA%\SGMS\`）

```
users.json                    用户索引（明文；**数据版本标记就写在这里**）
app.salt                      应用级盐
u_<uid>/
  ├─ grades.json.enc          成绩原始库（密码级）
  ├─ grade_history.json.enc   ★ 成绩历史（应用级 + 审计，§16 例外）
  ├─ student_profiles.json.enc★ 侧写（含学期侧写，按学期一行的 key 形态）
  ├─ students.json.enc        ★ v1.9.2 学生主档
  ├─ archive.json.enc         纵向档案
  ├─ class_<班级>.json.enc    班级快照（含计算好的加权分）
  ├─ audit/grade_changes.json.enc ★ 成绩变更审计（不记分数）
  └─ cache/chat_history_<学号>.json.enc ★ 档案页 AI 辅导聊天历史
models/<模型名>/*.gguf        模型文件
runtime/site/                 ★ v1.9.2 推理引擎安装目标（pip --target）
```

★ **红线 3**：明文成绩任何时候都不落盘，磁盘上只有 `.enc`。
★ **§16 例外**（Owner 拍板）：成绩**历史**与侧写走应用级密文 + 审计日志 —— 因为自动登录
时没有密码，档案页必须能读；导入的原始成绩库仍然只能走密码级。

---

## 三、任务一：模型中心卡片流

### 3.1 目标

旧版是一张表格：一行一个模型，勾选框密、看不出"这台电脑能不能跑"。新版改成**卡片流**：
一眼看到模型名、体积、负载档位，点卡片选中，底部一次性下载。

### 3.2 界面规格

| 元素 | 规格 |
|---|---|
| 布局 | `QScrollArea` + `QGridLayout`，`GRID_COLUMNS = 3`；窗口拉宽只增宽不增列 |
| 分组 | 按清单 `group` 字段分节（「推荐」/「高级」），节标题 + 一条分隔线 |
| 卡片内容 | 模型名（加粗、`wordWrap=True`）+ 体积 + 负载圆点 + 一句话说明 + 底部状态（「未装」/「已安装」徽章） |
| 悬浮 | 鼠标进入：卡片内容上浮 2px + 阴影加深（`blur 14→26`、`offset 2→6`）；离开还原；光标 `PointingHandCursor` |
| 选中 | 点击切换选中；选中态 2px `#0078D4` 边框；**卡片里没有下载按钮** |
| 底部按钮 | 「刷新清单」/「下载选中（N）」/「复制链接」/「关闭」；未选中时下载按钮禁用并显示 `(0)` |
| 已安装 | 绿色「已安装」徽章；该卡片**不可再选**（点它不改变选择）；右上角小「×」= 卸载（确认框「卸载后将无法使用 AI 功能，确定？」） |
| 困难（🔴） | 卡片整体降透明度（`opacity 0.6`）、不可选、tooltip「当前电脑配置可能无法流畅运行此模型」 |
| 缺引擎 | 顶部一条提示 + `BackendSetupCard`（见任务四），清单照常展示 |

### 3.3 实现要点

* `ui/model_center_dialog.py`：`ModelCard(QFrame)` 负责单卡渲染与点击；`_populate()` 重建卡片时
  **必须把选中集合写回** `_checked_ids`（否则重绘一次选择就丢 —— v1.9.1 实测踩到）。
* 卡片的"上浮"用**内层容器的 margin** 实现，不是移动窗口；阴影用
  `QGraphicsDropShadowEffect` —— 于是卡片**不能再挂 `QGraphicsOpacityEffect`**
  （同一个槽位，见 `AGENTS.md` §4a）。困难态因此用**调色板/样式**降透明度，
  代码里有 `has_opacity_effect()` 运行时兜底。
* 负载档位来自 `core.ai.model_registry.load_level()`，**唯一实现是 `_load_level_of()`**：
  内存够且（无显存要求或有独显且显存够）→ 轻松；否则内存 ≥ `minRamGb × 0.7` → 勉强；
  否则困难。**内存探不到一律放行**，探不到不等于 0。
* 下载队列串行执行；每个模型失败**重试 1 次**，仍失败 → 标红 + 「复制链接」兜底
  （提示「请浏览器下载后放入 models/ 目录」）。

### 3.4 判据

* `tests/test_model_center_card.py`（36 条）：卡片数 = 清单条数、分组顺序、选中集合回写、
  已安装不可选、困难卡不可选且 tooltip 正确、下载按钮文案与禁用态、卸载走确认框。
* `tests/test_model_center_enhanced.py`（47 条）：负载三档、默认勾选只认轻松、内存闸门不放行。
* 截图验人工量：卡片区背景色占比、卡片之间的空隙一致、无空白页（见 §八 的像素判据）。

---

## 四、任务二：档案页「AI 辅导」

### 4.1 变更要点

* **删掉全局入口**：`ui/main_window.py` 顶栏的「AI 谈话助手」按钮、对应信号与
  `_open_chat_assistant()` 全部移除（grep 应只剩注释里的一处历史说明）。
* `ui/chat_dialog.py` **保留为内部组件**，`open_chat(...)` 签名冻结、不改（档案页以外还有人调）。

### 4.2 界面

档案页「AI 辅导」为第三个标签页，自上而下三块：

1. **学生信息卡**：姓名（学号）、班级；右侧一句话「本机离线推理，谈话记录加密存在本机」。
2. **对话区**：只读 `QTextEdit`；空态提示「这里显示这名学生的谈话记录。」
3. **输入区**：`QLineEdit`（占位「说点什么…（回车发送）」）+「发送」；发送中置灰。

### 4.3 上下文注入

系统提示由学生数据拼成（`build_chat_system_prompt` / `build_student_json`）：

```
你是一名高校辅导员助理，正在协助辅导员与一名学生进行谈话。
以下是该学生的数据：{学生 JSON}。
请根据这些数据提供谈话建议、侧写分析或评语润色。
```

学生 JSON 只放结构化事实：学号/姓名/班级、按学期的成绩行（课程、分数、学分、
是否计入）、德育分、已有侧写。**不放姓名以外的敏感字段**（身份证等一律不出现）。

### 4.4 线程与流式

`_PageChatWorker(QThread)`：跑 `LocalModelEngine.stream()`，逐段 `chunk` 信号回主线程追加；
结束发 `done`。**不能用 `QThread.finished` 当完成信号** —— 同步测试模式下它不触发。

### 4.5 历史落盘

`u_<uid>/cache/chat_history_<学号>.json.enc`（应用级密文，每天最多 200 轮）。
清库时随成绩一起清（`clear_library` 的 `with_profiles` 分支），并写进清库报告。

### 4.6 降级（三条明确路径）

| 情况 | 表现 |
|---|---|
| 没装任何模型 | 提示「请先在模型中心下载模型」+「打开模型中心」按钮 |
| 没装推理引擎 | 中文原因 + 「一键安装」卡片（或"去模型中心安装"按钮） |
| 引擎在、模型在、生成失败 | 气泡里给中文错误（`friendly_error`），不弹栈、不卡死 |

### 4.7 判据

`tests/test_profile_ai.py`（33 条）：系统提示含学生 JSON 且不含敏感字段、流式分片顺序、
历史按学生隔离（甲的历史不出现在乙的对话区）、三条降级路径、`QThread` 不泄漏。

---

## 五、任务三：学生主档与四级访问

### 5.1 学生主档 `students.json.enc`

| 字段 | 说明 |
|---|---|
| `student_id` | **裸学号**（存储层不带 `sid:` 前缀；接口层统一归一） |
| `name` / `class_name` | 姓名 / 班级（班级可空） |
| `note` | 备注 |
| `source` | `import`（导入自动补档）/ `manual`（手工添加） |
| `archived` | 是否归档（归档=隐藏，数据全留） |
| `created_at` / `updated_at` | 时间戳 |

`StudentStore` 是唯一读写口：`rows(include_archived=False)` / `get` / `has` / `count` /
`search` / `names` / `add` / `update` / `upsert_import` / `archive` / `restore` / `rename` / `remove`。
**不许另立第二份名单。**

### 5.2 导入自动补档

`GradeHistoryDB.apply(records, ...)` 落盘后，调用方用 `StudentStore.upsert_import()` 把
本批学生补进主档（已存在只更新姓名/班级，不覆盖备注与归档位）。导入 N 个学生 → 主档至少 N 条。

### 5.3 学号变更：全量迁移（本版真 bug 的修复点）

`core.database.migrate_student_id(old, new, *, store, username)` 一次迁六类数据：

1. 成绩历史行；2. 侧写（含学期侧写）；3. 聊天历史文件；4. 德育分键；5. 意愿标签；6. 主档行。
返回 `StudentMigrationReport`（每类计数 + `total()` + `as_text()`），**失败只 warning 不中断**。

★ **两套学号写法必须归一**（v1.9.2 实测踩到、最贵的一条）：

* 主档、聊天文件名用的是 `sid:<学号>`；
* **成绩历史是导入时按裸学号落盘的**（`apply()` 的 `_clean_records()` 只做去空白、**不补 `sid:`**）。

于是"按字符串相等迁成绩"一条也迁不走，而且**不报错**（报表上成绩 0 条、界面看着正常）。
修法：`rekey_student()` 按**裸学号**比对、写回跟随**那一行原来的写法**；
`ProfileStore._resolve_key()` 让侧写读写同时认 `sid:` 与裸号两种键。
回归用例 4 条（`tests/test_student_crud.py` 的 `BARE_A` / `BARE_NEW`）。

### 5.4 删除策略（二选一，不许第三种）

* **彻底删除**：先清该生成绩历史、侧写、聊天文件、德育分键、意愿，再删主档行 ——
  不许留下"孤儿数据"。
* **归档**：只置 `archived=True`，数据全留；列表默认不显示，搜索框输入「已归档」才看得到。

### 5.5 清库即清学生

`clear_library(with_profiles=True)` 同时清：成绩历史、侧写、**学生主档**、
聊天缓存文件、纵向档案与班级快照（勾选时）。清完发 `SettingsPage.library_cleared(int, int, int)`，
主窗收到后刷新档案页/侧写页；档案页显示空态「暂无数据」，**不再出现"有名单没成绩"的假象**。

### 5.6 审计日志

`u_<uid>/audit/grade_changes.json.enc`，每条记：时间、操作
（`insert` / `overwrite` / `delete` / `student_add` / `student_edit` / `student_archive` /
`student_restore` / `student_delete` / `student_migrate`）、学生 ID、学期、课程名、`detail`。
**永不写分数与学分**，最多 2000 条（滚动）。`main.py` 里有闸门断言审计文件里没有 `score` 字段。

### 5.7 判据

* `tests/test_student_crud.py`（50 条）：增删改查、重复学号抛 `DuplicateStudentError`、
  归档隐藏、搜索命中、**裸学号迁移回归 4 条**。
* `tests/test_archive_navigation.py`（74 条）：档案页名单 → 详情 → 学期侧写 → 返回。
* `tests/test_profile_navigation.py`（30 条）：两次点击级联、返回按钮、空态、无 Markdown 记号。
* `tests/test_grade_delete.py`（77 条）：单条删除/学期删除/清库/审计只记课程名。

---

## 六、任务四：推理后端一键安装

### 6.1 为什么需要

`llama-cpp-python` 是可选依赖、**不进 exe**（体积与编译成本都不划算）。但打包版的
`sys.executable` 是 exe 自己，里面没有 pip —— 用户想用 AI 就得自己开命令行装，
这一步劝退所有人。一键安装把这一步变成一次点击。

### 6.2 落盘位置

`%LOCALAPPDATA%\SGMS\runtime\site`，由 `pip install --target` 装进去；
进程启动时 `ensure_runtime_on_path()` 把它插到 `sys.path` 前面。
**不装进系统 Python**、不写注册表、不需要管理员权限。

### 6.3 流程

```
点「一键安装」
  → find_system_python() 找本机 Python（找不到 → 显示「复制命令」，绝不静默降级）
  → subprocess.Popen(pip install --target <runtime> llama-cpp-python
                     CREATE_NO_WINDOW)     ← Windows 上绝不能弹黑框
  → 逐行 emit 进度；用户可取消（terminate + kill 兜底）
  → 成功：写 runtime 清单，refresh → ready 信号 → 自动继续加载模型
  → 失败：显示完整命令 + 「复制命令」「重试」，异常不许冒到事件循环里
```

`InstallResult` 带 `ok / command / output / error`；`backend_status()` 供自检与卡片判断。
**绝不静默安装**：只有用户点按钮才跑。

### 6.4 三态卡片与放置位置

`ui/widgets/backend_setup.py` 的 `BackendSetupCard` 三态：
**未安装**（原因 + 命令预览 + 「一键安装」）/ **安装中**（进度条 + 「取消」）/ **已就绪**（绿灯）。

* 模型中心顶部摆一张；
* 「学期侧写」页摆一张（同一个组件、同一套逻辑）；
* 档案页「AI 辅导」页**不重复摆**（那页控件已经很多），只贴中文原因 + 「打开模型中心」。

### 6.5 判据

`tests/test_runtime_setup.py`（46 条）：pip 参数组装、`--target` 路径、取消、
失败文案含完整命令、`CREATE_NO_WINDOW` 在 Windows 上为真、`backend_status()` 三态。
自检那条"`core/ai/*.py` 零 HTTP import"照旧生效（pip 是子进程，不是我们的 HTTP 库）。

---

## 七、版本与迁移

* `core/version.py`：`CURRENT_VERSION = "1.9.2"`（**唯一真源**；UI 里一个版本号字面量都没有）。
* `core/migrator.py`：新增 `_lift_1_9_1_to_1_9_2` 一跳并登记进 `_STEPS` ——
  哪怕本版没改数据结构，也必须补这一跳（否则老用户启动报
  `MigrationError: 没有从 v1.9.1 升级的路径`）。
* `known_data_versions()` = `('1.0', '1.6', '1.7', '1.8', '1.8.1', '1.9', '1.9.1', '1.9.2')`。
* 升级前强制备份整个数据目录；失败回滚只恢复 `users.json`（迁移全程只动过它）。

---

## 八、测试与自检

### 8.1 测试分布（865 条，19 个文件，实测）

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_parser_filters.py` | 49 | 封板 parser 的分类与过滤（回归） |
| `tests/test_calc.py` | 8 | 封板 calculator（回归） |
| `tests/test_manifest.py` | 67 | 更新清单 / 分片 |
| `tests/test_updater_parts.py` | 15 | 分片下载 |
| `tests/test_version_migration.py` | 25 | 版本链与迁移 |
| `tests/test_model_registry.py` | 109 | 清单解析、负载三档、默认勾选 |
| `tests/test_model_center_enhanced.py` | 47 | 模型中心增强（v1.9 二期） |
| `tests/test_model_center_card.py` | **36** | ★ 卡片流 |
| `tests/test_inference.py` | 44 | 推理引擎（无引擎时的降级） |
| `tests/test_profile_ai.py` | **33** | ★ 档案页 AI 辅导 |
| `tests/test_student_crud.py` | **50** | ★ 学生主档 + 学号迁移 |
| `tests/test_archive_navigation.py` | **74** | ★ 档案页两级视图 |
| `tests/test_profile_navigation.py` | **30** | ★ 穿透导航 |
| `tests/test_grade_delete.py` | 77 | 单条删除 / 清库 / 审计 |
| `tests/test_grade_overwrite.py` | 36 | 同学期覆盖与跳过 |
| `tests/test_profile_merge.py` | 29 | 档案合并 |
| `tests/test_runtime_setup.py` | **46** | ★ 推理后端安装 |
| `tests/test_import.py` | 40 | 德育分批量导入 |
| `tests/test_import_review_ui.py` | 50 | 导入核对 QSplitter |

★ = v1.9.2 新增或大幅扩充。

### 8.2 自检（`python main.py --selfcheck`）

必须全部 `ok`，任何一条 `FAIL` 都算没交付：

* 封板 4 文件 sha256 全吻合；
* 激活码系统 0 处残留（文件层 / AST 源码层 / `CHECK_MODULES` 接线层）；
* `CHECK_MODULES` 全量 import 通过（本版新增 `core.ai.runtime_setup`、`ui.widgets`、
  `ui.widgets.backend_setup`、`ui.pages.profile_shared`、`ui.pages.semester_side_profile`）；
* `docs/` 白名单 5 个文件（含本文件）、无死链接；
* 版本号唯一真源：`core.updater.CURRENT_VERSION is core.version.CURRENT_VERSION`，
  UI 里 0 处版本字面量；
* 迁移链 1.0 → 1.9.2 每一步都在；
* 三档口径算分 85.714286 / 83.333333 / 80.000000（防"选修混进限选"）；
* `core/ai/*.py` 零 HTTP import。

### 8.3 GUI 判据（截图 + 像素）

静态断言全绿但界面空白，在本项目出过三次。所以交付前必须**截图并量像素**：

* 模型中心：卡片区**非背景像素 ≥ 5%**、可辨颜色数（distinct）≥ 200、卡片数 = 清单数、
  分组标题齐全、无 `QGraphicsOpacityEffect`、界面文案 0 处 Markdown 记号；
* 档案页：三标签页齐全、名单行数 = 主档条数、学生卡可见、输入框与发送按钮可用、
  窗口不小于最小尺寸；
* 悬浮动效必须用 `QTest.mouseMove()` **真的动鼠标**验（调私有函数不算数）。

---

## 九、打包与验收

```
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1 -Both
dist\transcript-lens.exe --selfcheck
dist\transcript-lens.exe --bootcheck
```

两条铁律（都踩过）：

1. **打包前把 `TEMP` 指回真实临时目录**。在 bash/CI 里跑 PowerShell 时 `TEMP=/tmp`
   会被继承，onefile 版 exe 解压 `_MEI*` 失败，`--selfcheck` 退出码 -1 ——
   那不是代码坏了，是环境错了。正确做法：`$env:TEMP = $env:TMP = "C:\Users\<用户>\AppData\Local\Temp"`。
2. **验证完的 exe 必须彻底关掉再重打**：onefile 会起子进程，按**进程名**杀
   （`Get-Process -Name 'transcript-lens*' | Stop-Process -Force`）+ 等 3 秒。

`--bootcheck` 跑真实的启动时序（auto / login / switch），退出码 0 才算过 ——
它守的是 v1.6 那次"开屏后进程静默退出（0xC0000409）"的坑。

---

## 十、已知限制与未做

| 项 | 说明 |
|---|---|
| GPA 方案 2/3/4 | 只在 `core/gpa.py` 里占了位（下拉框里显示为不可用），本版只实现**方案 1 标准 4.0** |
| 排名 | 没有真源（`archival.semester_rank` 是学期排序，`scholarship` 是含德育权重的班级名次），档案页显示「GPA 与排名暂无数据来源」 |
| 审计日志 UI | 只写不读，暂无查看界面（列为后续任务） |
| 学期均分 | 是**算术平均**，界面标签必须写明；加权均分/GPA 一律复用 `core/calculator` 与 `core/gpa` |
| 推理后端 | `llama-cpp-python` 仍需用户点一键安装；装失败要给命令而不是"未知错误" |
| 文档 | `AGENTS.md` 体量有 64000 字节上限（自检不管这条，但维护者守着） |

---

## 十一、附录 A：本版文件清单

**新增**

```
core/ai/runtime_setup.py           推理后端安装（零 HTTP import）
ui/widgets/__init__.py             包入口
ui/widgets/backend_setup.py        三态安装卡片
ui/pages/profile_shared.py         StudentBook / TermNode / 学期侧写读写
ui/pages/semester_side_profile.py  学期侧写页
tests/test_model_center_card.py    36 条
tests/test_profile_ai.py           33 条
tests/test_student_crud.py         50 条
tests/test_archive_navigation.py   74 条
tests/test_profile_navigation.py   30 条
tests/test_runtime_setup.py        46 条
CHANGELOG_v1.9.2.md                本版改动说明
docs/SGMS_v1.9.2_Dev_Charter.md    本文件
```

**改动**

```
core/database.py       学生主档 StudentStore、学号迁移、聊天缓存清理、审计 detail
core/version.py        1.9.2
core/migrator.py       _lift_1_9_1_to_1_9_2 一跳
main.py                CHECK_MODULES 增 5 个模块；docs 白名单增本文件
ui/model_center_dialog.py  卡片流重写
ui/main_window.py      删全局 AI 入口；侧栏"侧写辅导"→档案页；清库信号接线
ui/pages/profile_page.py   名单 + 两级视图 + 三标签页
ui/pages/analysis_page.py  「侧写」按钮改发 student_selected（不再自己开窗）
ui/pages/settings_page.py  清库文案改用 library_inventory() 判据
AGENTS.md             §36 推理后端一键安装 / §37 穿透导航与学生主档
docs/DEV_SUMMARY.md   §二十六 v1.9.2 现行规格与判据
```

## 十二、附录 B：术语表

| 术语 | 含义 |
|---|---|
| 三档口径 | ① 必修+限选+选修 ② 必修+限选 ③ 仅必修（`CalculationScope`） |
| 四级访问 | 主档学生增删改 / 成绩库里索引不到的学生 / 彻底删除与归档 / 学号变更 |
| 两级视图 | 档案页的"学生名单"与"学生详情（含学期侧写）"两层 `QStackedWidget` |
| 负载三档 | 轻松 / 勉强 / 困难（`LOAD_EASY/TIGHT/HARD`） |
| 封板文件 | sha256 写死在 `main.py` 的四个文件，改一个字节自检就 FAIL |
| 应用级加密 | 不需要密码即可解开的密文（自动登录也要能读的数据） |
| 密码级加密 | 需要用户密码解开的密文（原始成绩库） |
