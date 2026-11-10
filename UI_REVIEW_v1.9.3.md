# transcript-lens 前端界面评审报告（v1.9.3）

**信息层级 · 排版 · 间距 · 视觉一致性 —— 诊断与 P0 施工单**

| 项 | 内容 |
|---|---|
| 评审对象 | transcript-lens 桌面端（PySide6 / Qt 6，无边框自绘窗口） |
| 代码基线 | v1.9.3，`ui/` 共 27 个模块 / 19,464 行 |
| 深度诊断基线 | v1.9.1 快照，`ui/` 22 个模块 / 15,556 行（逐行阅读） |
| 本次复核（2026-11-08） | 全局统计重算 + P0 五条复验 + v1.9.2+ 新增模块基线核对 |
| 未逐行覆盖 | `ui/pages/semester_side_profile.py`、`ui/pages/profile_shared.py`、`ui/widgets/backend_setup.py`（仅基线核对，见 §8.3） |
| 评审方法 | ① 逐行读每个 `_build()`；② `objectName` ↔ `styles.qss` 交叉对账（脚本）；③ offscreen 像素探针（真 QSS + 可替换调色板） |
| 字体说明 | 离屏环境缺字体目录，字号结论取自 QSS 声明值，不采信离屏字体度量 |
| 本文性质 | 评审单据 + P0 施工单；**不含代码改动** |

> 放置说明：`docs/` 有严格白名单（`main.py` 的 `DOCS_WHITELIST`，只有总纲与四本手册），新增文件会让 `--selfcheck` 直接 FAIL，所以本报告放在仓库根目录。

---

## 1. 结论摘要

**骨架是好的。** 三件事做对了：

1. **唯一 QSS 源**：`ui/styles.qss`（555 行）是样式真源，没有第二份主题文件；
2. **卡片统一造**：卡片全部经 `ui/anim.py::make_card()` 创建，0 处手写 `setObjectName("Card")`；
3. **无边框窗口的四角分配**：`#Sidebar` 左两角、`#TopBar` 右上角、`#WindowRoot` 全角 + `[tlMaximized="true"]` 归零（`styles.qss:143-200`），且 `#NavButton` 用 `border-left: 4px solid transparent` 占位防跳位（`styles.qss:166-170`）。

**问题集中在三处：**

1. **跨页规格无真源** —— 页面内边距、卡片内边距、字号、按钮高度各页各写一份；
2. **一批控件从未进入 QSS 体系** —— 首页口径切换、全部消息框、滚动区、进度条、标签页；
3. **数值列没有对齐与精度规格** —— 主数据左对齐 + 6 位小数。

**P0 五条在 v1.9.3 全部仍成立**（逐条复验，见 §4）。

**v1.9.1 → v1.9.3 的变化**（详见 §8.3）：

- **已修复**：`ui/chat_dialog.py` 从 0 个 objectName 改为使用 `#H3 / #Muted / #Outline / #Primary / #Ghost / #Warn`（`:338-392`）；
- **新增缺口**：v1.9.2+ 新模块带来 19 个未入 QSS 的 objectName（`semester_side_profile` 14 个、`profile_shared` 5 个）；
- **统计变化**：objectName 缺口 16/56 → **36/77**；`QScrollArea` 7 → **8 实例 / 7 文件**；`QMessageBox` 143 → **142 调用 / 13 文件**；内联 `setStyleSheet` 26 → **27**；圆角 6 → **8 档**。

---

## 2. 四维度诊断

### 2.1 信息层级（A）

**A1 默认首页没有页面标题锚点**
`ui/pages/analysis_page.py:618-621` 的 `_build()` 直接从工具栏开始。以当前代码复核：内容区有 `#H1` 的只有**账号页**（`account_page.py:117`）、**奖学金页**（`scholarship_page.py:252`）、**设置页**（`settings_page.py:236`）三页；**分析页、成长档案页、侧写页都没有内容区页标题**，侧写子页用 16pt 自定义标题（`semester_side_profile.py:231-236`）。用户登进来看到的第一屏，没有"我在哪"的文字锚点。

**A2 页名重复且层级倒置**
`ui/main_window.py:256-295` 的顶栏 = 自定义标题栏：左边 `_user_label`（`#TopUser`，700 字重，`:273-275`）→ `addStretch(1)` → `_page_title`（`#TopMeta`，400 字重、`#94A3B8`，`:279-281`）；三个窗口按钮由 `WindowTitleBar` 加在更右侧。于是：

```
[ 用户名(加粗,最左) ]  ......  [ 页名(灰,最弱) ][ — □ ✕ ]
```

页名落在通常放操作/窗口控件的位置，并且是全屏最弱的文字；而账号/奖学金/设置三页内容区顶部又有一个 26px 的同名 `#H1`。同一信息 0~2 个层级，取决于当前页。低频信息（用户名）占了最强位置。

**A3 `#H1` 一词两义**
26px/700 既当页标题（`account_page.py:117`、`scholarship_page.py:252`、`settings_page.py:236`），又当**数据数值**：`archive_page.py:244-246` 的 `_cumulative`、`profile_page.py:2044-2046` 的 `_avg_label`。标题级字号被用来显示 `85.714286`。

**A4 首页工具栏是一排无分组的混排**
`analysis_page.py:623-688`：导入按钮（42px，`:626`）· 班级号标签+输入框（38，`:638`）· 学期标签+下拉（38，`:654`）· 三个口径按钮（34，`:674`），7 个控件、4 种高度、两种手写间距（`:632` 的 6、`:648` 的 10），`addStretch(1)`（`:662`）再把筛选组推到最右。操作、必填参数、视图状态三种性质混在一行。

**A5 说明文字与状态文字同级**
`_status`（`:690-693`，`#Muted`，初始文案就一句"还没有导入数据。"）与页脚 `_detail`（`:719-721`，`#Muted` + 拼进 `CALC_SCOPE_NOTE`（`:217`）整段三口径定义）都是最弱一档灰字，两段长文本夹住表格。**空状态只有一行 13px 灰字 + 一个空表头表格**，这是产品第一印象。

**A6 安全承诺被降到最弱档**
`main_window.py:249-252` 侧栏页脚"本地离线 · 成绩不出这台电脑"复用 `#BrandSub`（11px `#94A3B8`）并 `setWordWrap(True)`，和英文副标识 `transcript-lens`（`:228`）同一档。

**A7 奖学金页编号体系混用**
卡片标题用 `① ② ③ ④`（`scholarship_page.py:281 / 311 / 377 / 423` 附近），正文按钮却叫"③ 开始测算"（`:406`）——"③"到底是卡片还是动作，得猜。

### 2.2 排版（B）

**B1 全局没有字体基线，也没有行高概念**
`styles.qss` 全文**无 `font-family`、无 `font-size` 基线**，只有 `QWidget { color: #1F2430 }`。正文尺寸来自 `main.py` 的 `UI_FONT_FAMILY = "Microsoft YaHei UI"` / `UI_FONT_SIZE = 10`（pt → 13.33px），启动段 `setFont()` 后再 `setStyleSheet()`。行高无处设置（Qt QSS 不支持 `line-height`，Qt 也没开 `QFont` 行距）。

**B2 字阶 9 档，其中 5 档只差 1px**
QSS 实际声明值：`30 / 26 / 22 / 17 / 15 / 14 / 13 / 12 / 11`（计数：11×3、12×11、13×3、14×3、15×1、17×2、22×1、26×1、30×1）。11→12→13→14→15 肉眼不可辨（尤其在被缩放显示的 13.33px 正文旁边），而 17→26 一跨 9px，18–21 整档缺失。**卡片标题（17）与页面标题（26）之间读不出"中间层"。**

**B3 `#H4` 有名字没规则**
`update_dialog.py:519` 的 `notes_title.setObjectName("H4")`，`styles.qss` 里**不存在 `#H4`**（脚本复核规则数 = 0）→ 回落到 13.33px 正文，与紧邻的 `#H3`（14px）差别 <1px。

**B4 字重只有 400 和 700，没有中间档**
取消 / 禁用 / 次要强调全都无处安放；`#Ghost`（`styles.qss:225-234`）也是 `font-weight: 700`，还配 `1px solid #3B82F6` 主色实心描边——次按钮和主按钮强度完全一样。

**B5 表格里数字是左对齐的，一张表四种对齐**
`analysis_page.py`：

| 列 | 对齐 | 位置 |
|---|---|---|
| 姓名 | `AlignCenter` | `:1183` |
| 学号 | `AlignCenter` | `:1187` |
| 三档加权均分 | **`AlignLeft \| AlignVCenter`** | `:1199-1201` |
| 红标原因 | 未设置（默认左） | `:1221-1234` 附近 |
| 操作 | cellWidget 居中 | `:1213` 附近 |

加权均分是这张表的**主数据**，左对齐意味着小数点不齐、位数不同（`85.714286` vs `9.5`）时无法纵向比较。对照 `scholarship_page.py:148-160` 的 `_cell()` 默认全 `AlignCenter`。

**B6 数值精度页间不一致，且表格用了 6 位小数**
`analysis_page.py:288-289`：

```python
def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.6f}"
```

表格（`:1198` 附近）与页下明细行（`:1277` 附近）都走它，显示 `85.714286`（8 字符宽）；`core/scholarship.py:204-209` 的 `academic_text` / `final_text` 也是 `.6f`；而全站趋势 Δ 是 `.2f`（`archive_page.py:431`、`profile_page.py:1841 / 1845 / 1849` 附近）。两位小数足够辅导员做判断，6 位只是把列撑宽。

**B7 数字不是等宽对齐**
表格数字走中文正文字体（YaHei），无等宽数字特性；即使改成右对齐，`1` 和 `8` 的宽度差仍会让个位错位。

### 2.3 间距（C）

**C1 页面内边距事实统一，但是巧合而非 token**
`setContentsMargins(22, 20, 22, 20)` 出现在 `analysis_page.py:620`、`archive_page.py:151`、`profile_page.py:866`、`account_page.py:113`、`scholarship_page.py:248`、`settings_page.py:232` 六处，各写一份，没有共享常量。

**C2 卡片内边距四套并存**
当前全量统计：`20,16`（×12）、`24,20`（×9）、`22,20`（×6）、`20,18`（×3）。

| 页面 | 卡片内边距 | 正文实际左边缘 |
|---|---|---|
| 设置页（`settings_page.py:242 / 280 / 336 / 455 / 838`） | 24 | 22 + 1（边框）+ 24 = **47px** |
| 账号页（`account_page.py:123 / 167 / 194`） | 24 | **47px** |
| 奖学金页（`scholarship_page.py:277 / 306 / 373 / 418`） | 20 | **43px** |
| 成长档案页（`archive_page.py:237 / 262 / 294` 附近） | 20 | **43px** |

切换页面时正文左边缘**漂移 4px**，肉眼可感。另有账号页子卡 `22,18`（`account_page.py:396 / 478`）、`StudentDialog` 的 `20,18,20,16`（`profile_shared.py:1740`）、`TermNode` 的 `12,8`（`profile_shared.py:1656`）。

**C3 按钮高度有六档以上，且 QSS 与代码同时定义**
`setMinimumHeight` 当前分布：32（×9）、34（×9）、36（×25）、38（×20）为主，另有 22（×4）、26（×2）、30（×2）、40（×4）、42（×1）、44（×1）。QSS 自己还有两套 padding（`9px 18px` 给 `#Primary` / `#Success`，`8px 17px` 给 `#Ghost` / `#Danger` → 自然高差 2px）。同一张卡片里就混着 34 和 38（`scholarship_page.py:320` 保存 = 34，`:408` 开始测算 = 38）。

**C4 左列三段 2px 错位**
`archive_page.py` 名单卡：头部 `14,13,12,10`（`:165`）、搜索 `12,0,12,10`（`:178`）、页脚 `14,8,12,10`（`:201`）→ 标题左边缘 14、搜索框左边缘 12。`profile_page.py:887 / 900 / 924` 同构且同样错位，`:1160-1162` 附近的注释还自述了这套偏移。

**C5 页内节奏两套**
卡片堆叠间距：`profile_page.py:867` = 14、`analysis_page.py:621` = 12；`scholarship_page.py:249` = 14、`settings_page.py:200` 附近 = 14。`setSpacing` 全局主用 10（×29）/ 8（×17）/ 14（×10）/ 12（×6）四档并行。

**C6 圆角八档**
QSS 当前取值：4 / 5 / 6 / 8 / 9 / 10 / 12 / 16。按钮/输入 8、卡片 10、窗口 12 是主线；`#RowAction` / `UNINSTALL_STYLE` 用 6、`#Tag*` 用 9、`#WelcomeCard` / 开屏用 16。

**C7 三个窗口级尺寸互不派生**
`SIDEBAR_WIDTH = 190`（`main_window.py:89`）、`TOPBAR_HEIGHT = 54`（`:92`）、导航 `setMinimumHeight(44)`（`:241`）——54 不是 44 的整数关系，190 也整除不了 44。

### 2.4 视觉一致性（D）

**D1 77 个 objectName 里 36 个在 QSS 里没有任何规则**

| 模块 | 未入 QSS 的 objectName |
|---|---|
| `analysis_page.py` | `#FilterToggle` |
| `main_window.py` | `#Content` |
| `update_dialog.py` | `#H4`、`#NotesScroll` |
| `settings_page.py` | `#PageScroll` |
| `review_dialog.py` | `#ReviewHintScroll`、`#ReviewHints`、`#ReviewSplit` |
| `model_center_dialog.py` | `#ModelCard`、`#ModelCardContent`、`#ModelCardHost`、`#ModelGroupHeader` |
| `profile_page.py` | `#GradeTree`、`#ProfileStack`、`#ProfileTabs`、`#TimelineScroll` |
| `archive_page.py` | `#TimelineRow` |
| `profile_shared.py` | `#FormInput`、`#TermAvg`、`#TermName`、`#TermNode`、`#TermTrend` |
| `semester_side_profile.py` | `#aiStatus`、`#aiTextEdit`、`#backButton`、`#courseNote`、`#courseTable`、`#emptyHint`、`#generateSideButton`、`#pageTitle`、`#saveSideButton`、`#semesterSplitter`、`#statLine`、`#statNote`、`#studentLine`、`#talkButton` |

其中 `#ModelCard*` 与 `#ReviewSplit` 靠内联样式顶替，其余是真正的无样式控件。

**D2 首页口径三档实测是原生控件**
`#FilterToggle`（`analysis_page.py:671`，QSS 规则数 = 0）：未选中近白 `#FAFAFA`、边框 **`#ABABAB`**（原生灰，既非 `#CBD5E1` 也非 `#E2E8F0`）；选中整块 **`#DBDBDB`** 中灰。全站"选中"的语言是**蓝**（`#NavButton:checked` = `rgba(59,130,246,0.26)` + 4px 蓝线，`styles.qss:179-188`）——首页核心交互读起来像"被按下去的按钮"或"禁用态"。选中/未选中像素差 5.3%，最大通道差 34（探针实测，v1.9.1 基线；代码路径未变）。

**D3 滚动区底色跟着系统调色板走，深色主题下整页变黑**
7 个文件里 8 个 `QScrollArea`（`settings_page` / `scholarship_page` / `archive_page` / `profile_page` ×2 / `review_dialog` / `update_dialog` / `model_center_dialog`）全部 `setFrameShape(NoFrame)`，但 **viewport 默认 `autoFillBackground() == True`**，刷的是 `QPalette::Window`，不是 QSS 的 `#F1F5F9`。QSS 里 `QScrollArea` 规则数 = 0（脚本复核）。实测：

| 场景 | 窗口中心色 |
|---|---|
| 浅色系统调色板（现状） | `#EFEFEF`（设计色 `#F1F5F9`，差 2–10/255） |
| **深色系统调色板（现状）** | **`#2B2B2B`（整页变黑，`#1F2430` 深字读不出来）** |
| 修法：QSS 透明选择器 | `#F1F5F9` ✓ |

8 个实例里只有 `model_center_dialog.py:933` 打过内联补丁（`self._grid_host.setStyleSheet("background: transparent;")`）——说明坑踩过一次，但没回填到别处。

**D4 142 处 `QMessageBox` 完全在 QSS 体系之外**
分布在 13 个文件（`login_window` 16 / `profile_page` 25 / `settings_page` 26 / `scholarship_page` 15 / `semester_side_profile` 15 / `account_page` 13 / `model_center_dialog` 9 / `analysis_page` 8 / `update_dialog` 6 / `recover_dialog` 5 / `profile_shared` 2 / `backend_setup` 1 / `workers` 1），QSS 里 `QMessageBox` 规则数 = 0。消息框是用户看到最多的反馈面，却是唯一一套没被设计过的界面。

**D5 `QProgressBar` 与 `QTabWidget` 无规则**
QSS 里 `QProgressBar` / `QTabWidget` / `QTabBar` 规则数均为 0。实测进度条 chunk `#3599D9`（不是主色 `#3B82F6`）、槽 `#FFFFFF`；`#ProfileTabs` 走原生 tab（25px 高、`#FEFEFE` 条、无 10px 圆角也无 `#E2E8F0` 边框）——侧写页三个标签页是这个页面的主结构，却是唯一的原生态。

**D6 `ui/chat_dialog.py` 已修复（v1.9.3 复核）**
v1.9.1 时全窗 0 个 objectName；现在按钮与文字已挂 `#H3 / #Muted / #Outline / #Primary / #Ghost / #Warn`（`:338-392`），对话历史是 `QTextEdit#Outline`（`:361`），已进入 QSS 体系。**本条从问题清单移除。**

**D7 强调蓝 5 个值**
`#3B82F6`（QSS 主色）· `#2F76E8`（`#Primary:hover`）· `#2563EB`（`#Primary:pressed`、`#RowAction` 文字、`COPY_LINK_STYLE`）· `#1D4ED8`（`#Link:hover`、表格 `selection-color`）· **`#0078D4`（`model_center_dialog.py:169` 选中卡片的 2px 边框，Windows 系统蓝）**。第 5 个最刺眼：卡片选中态用的是系统色而不是品牌色。

**D8 危险红 4 个值**
`#EF4444`（QSS）· `#DC2626`（`analysis_page.py:270` 附近 + `model_center_dialog.py:138` 附近，后者注释还写着"和分析页的警示色保持一致"）· `#B91C1C`（`#TagRed` 文字）· `#9B1C1C`（`review_dialog`）。

**D9 灰阶漂移**
`model_center_dialog.py:163` 的 `MUTED_COLOR = "#9CA3AF"` 不在 QSS 调色板（QSS 用 `#94A3B8`）；`archive_page.py:77` 的 `_DOWN_COLOR = "#C2410C"` 内联覆盖了 `#TagOrange` 的 `#B45309`（`styles.qss:425`）；`scholarship_page.py:119` 的银奖用了 `#E5E7EB / #374151`（同样是体系外灰）。

**D10 奖学金页自建第四套标签配色**
`scholarship_page.py:116-120` 的 `_AWARD_STYLE`（紫 / 金 / 银 / 铜，银奖为 `#E5E7EB / #374151`），未复用 `styles.qss:409-440` 已有的 `TagGreen / TagGray / TagOrange / TagRed`。

**D11 所有按钮都没有 `:focus`**
`styles.qss` 里 `:focus` 只出现在 `QLineEdit` / `QComboBox` / `QSpinBox`（约 `:302`）；`#Primary` / `#Ghost` / `#Danger` / `#Success` / `#Link` / `#RowAction` / `#NavButton` / `#FilterToggle` 一律没有。表格和列表还显式 `outline: none`。用 Tab 键走一遍界面，**除系统虚线框外没有任何焦点提示**——不符合 WCAG 2.4.7。

**D12 卡片有两套实现**
`#Card` 与 `#CardFlat` 在 `styles.qss:132-141` **逐字相同**，而 `#CardFlat` 在代码里 0 引用（`make_card()` 默认 `name="Card"`）；同时 `model_center_dialog.py:236` 又内联了 `CARD_STYLE` 定义 `QFrame#ModelCard`。

**D13 开屏自建一套色板与字体**
`splash.py:100-106`：`_BG = "#1E293B"`、`_ACCENT = "#3B82F6"`、`_TITLE = "#F8FAFC"`、`_MUTED = "#94A3B8"`、`UI_FONT = "Microsoft YaHei UI"` —— 与 QSS 靠人工同步。

**D14 内联样式绕过 QSS 的口子共 27 处**
`model_center_dialog.py` 18 处、`archive_page.py` 6 处、`login_window.py` 1 处、`profile_shared.py` 1 处、`review_dialog.py` 1 处。

**D15 奖牌整行染色取代斑马纹**
`scholarship_page.py:156` 与 `:765-775` 给中奖行的**全部列**都 `setBackground`，斑马纹（`#F8FAFC`）在这一行消失。染色与选中高亮（`#DBEAFE`）都能正常渲染——不是 bug，是**横向色带与纵向斑马纹两种节奏打架**，一张表里最多 6 种底色。

**D16 界面文案里出现内部文档名**
`analysis_page.py:680` 的气泡写着"三档口径是 charter 一节定死的，不新增、不改定义。"——"charter" 是已删除的旧文档（`AGENTS.md` §0 明令不再引用），用户看不懂；而且这句话在解释"为什么我不能自由组合"，是写给开发者看的，不是给辅导员看的。

**D17 新增模块的标题与内边距（v1.9.3 基线核对）**
`semester_side_profile.py`：根内边距 `20,16`（`:217`），与既有六页的 `22,20` 不同；标题 `#pageTitle`（`:232`）+ 手写 `pointSize + 6`、`max(..., 16)`（`:233-236`），既不等于 `#H1`（26px），也不等于 `#H2`（17px），是第三种标题尺寸。若这是"子页要小一号"的有意设计，应明确落成 `#H2` 并写进设计说明；若是顺手写的，应统一。

---

## 3. 建议清单（31 条）

### 3.1 P0 — 先修（影响"看得见 / 读得出"）

| # | 问题 | 位置 | 具体改法 | 预期效果 |
|---|---|---|---|---|
| **P0-1** | 滚动区底色跟系统调色板走，深色主题整页变黑 | 8 个 `QScrollArea`；QSS 无规则 | `styles.qss` 末尾追加 `QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }`；保留 `model_center_dialog.py:933` 内联补丁 | 四个长页在任何系统主题下保持 `#F1F5F9`；深色主题不再"黑底深字" |
| **P0-2** | 首页口径三档是原生灰块 | `analysis_page.py:671` + 缺 `#FilterToggle` | QSS 追加四条（基态 / hover / checked / checked:hover），见 §4.2；保持 `setMinimumHeight(34)` 不变 | 选中态回到全站唯一"蓝＝选中"语言，与 `#NavButton:checked` 呼应 |
| **P0-3** | 加权均分左对齐、六位小数 | `analysis_page.py:1199-1201`、`:288-289`、`core/scholarship.py:204-209` | 数值列右对齐 + `_fmt` 改 `.2f` + 姓名/学号左对齐 + 表头同侧 + `academic_text` / `final_text` 改 `.2f` | 小数点纵向成列，列宽省约 40px；同一指标全站同一精度 |
| **P0-4** | 页名重复且层级倒置；标题 0~2 个 | `main_window.py:273-281` / `:509`、`styles.qss:201-207`、三页 `#H1` | 二选一：A 删顶栏页名、内容区 `#H1` 唯一；B 保留顶栏页名并提升，删三页内容 `#H1`（见 §4.4） | 单个视口内"这是哪一页"只出现一次；六页标题策略一致 |
| **P0-5** | 142 处消息框零样式 | 13 个文件 + QSS | QSS 追加 `QMessageBox` 最小套装（背景 / 文字 / 按钮 / hover / default），见 §4.5 | 全站最高频反馈面进入设计体系；危险确认框的"确定"不再和系统默认按钮抢 |

### 3.2 P1 — 第二批（一致性缺口，改动小、收益直观）

| # | 问题 | 位置 | 具体改法 |
|---|---|---|---|
| P1-1 | `#H4` 有名字没规则 | `update_dialog.py:519` | 加 `QLabel#H4 { font-size:15px; font-weight:700; color:#1F2430; }`，或改用 `#H3` 删掉该名 |
| P1-2 | `#ProfileTabs` 是原生 tab | `profile_page.py:1236` | `QTabWidget#ProfileTabs::pane { border:1px solid #E2E8F0; border-radius:10px; background:#FFFFFF; }` + `QTabBar::tab` 四态（默认 / hover / selected / 无边框、底部 2px 蓝线） |
| P1-3 | `QProgressBar` chunk 是 `#3599D9` | QSS 无规则 | `QProgressBar { background:#E2E8F0; border:none; border-radius:5px; height:10px; }` + `::chunk { background:#3B82F6; border-radius:5px; }` |
| P1-4 | 所有按钮无 `:focus`（WCAG 2.4.7） | `styles.qss` 按钮段 | 照抄 `#NavButton` "基态透明"防位移：`#Primary` / `#Success` 基态加 `border:1px solid transparent`，`:focus { border-color:#93C5FD; }`；`#Ghost` / `#Danger` / `#RowAction` / `#FilterToggle` 各自 `:focus { border-color:#3B82F6; }` |
| P1-5 | 按钮高度六档以上，QSS padding 与 minHeight 打架 | `styles.qss:210-292` + 各页 | 定两档 `CONTROL_H_SM=34` / `CONTROL_H_MD=38`；`#Primary` / `#Success` padding `9px 18px` → `8px 17px` 对齐 `#Ghost` / `#Danger`；代码里 42/40/36 收敛到 38 |
| P1-6 | `#Ghost` 用主色实心描边 + 700 字重 | `styles.qss:225-234` | 改 `border:1px solid #CBD5E1; color:#475569; font-weight:400;`，`:hover { border-color:#3B82F6; color:#3B82F6; background:#EFF6FF; }` |
| P1-7 | 卡片内边距四套 → 左边缘漂移 4px | 设置页 / 账号页（24,20）；奖学金页 / 档案页（20,16） | 统一 `20,16`（十二处已是它，改动面最小），页面保持 `22,20`；`account_page.py:396 / 478` 的 `22,18` 与 `profile_shared.py:1740` 的 `20,18,20,16` 一并收敛 |
| P1-8 | 左列三段 2px 错位 | `archive_page.py:165/178/201`、`profile_page.py:887/900/924` | 三段左右一律 14（搜索段 `12,0,12,10` → `14,0,14,10`） |
| P1-9 | 卡片两套实现 + `#CardFlat` 空转 | `styles.qss:132-141`、`model_center_dialog.py:236` | 删 `#CardFlat`；把 `CARD_STYLE` 可搬的搬回 QSS（`QFrame#ModelCard` + `[tlSelected="true"]` + 状态属性），模块只留 `setProperty` |
| P1-10 | 颜色漂移（蓝 5 / 红 4 / 灰 2） | 见 D7–D9 | 蓝统一 `#3B82F6` / `#2F76E8` / `#2563EB`，`#1D4ED8`→`#2563EB`，**`#0078D4`→`#3B82F6`**；红统一 `#EF4444` + `#B91C1C`；`#9CA3AF`→`#94A3B8`；`#C2410C`→`#B45309`；`#E5E7EB/#374151`→`#E2E8F0/#475569` |
| P1-11 | 奖学金页奖牌配色在 Tag 族之外 | `scholarship_page.py:116-120` | 保留四色语义；`_AWARD_STYLE` 已是模块级唯一常量（复核：全仓仅 `:116` 定义、`:765` 使用），本次只做配色收敛：金 `#FEF3C7/#92400E`、银 `#E2E8F0/#475569`、铜 `#FFEDD5/#9A3412`、紫 `#F3E8FF/#6B21A8` |
| P1-12 | 首页空状态只有一行灰字 + 空表头 | `analysis_page.py:690`、`:695-717` | 无数据时 `_table.setVisible(False)`，插入居中空状态块（`#H2`"还没有成绩" + `#Muted` 一句引导 + `#Primary` 主按钮） |
| P1-13 | 奖学金页编号混用 | `scholarship_page.py:406` 等 | 按钮文案去掉编号改"开始测算"，编号只留给卡片 |
| P1-14 | 界面文案引用已删文档 | `analysis_page.py:680` | 改"三档口径是固定的，不能自由组合"（纯文本，不加任何 Markdown 记号） |
| P1-15 | 学期侧写页标题与内边距不统一 | `semester_side_profile.py:217`、`:231-236` | 若子页有意小一号 → 用 `#H2`（17px）替换手写 `pointSize+6`，并把 `20,16` 写进设计说明；否则统一 `#H1` + `22,20` |

### 3.3 P2 — 第三批（打磨，可与 P1 同批）

| # | 问题 | 具体改法 | 预期效果 |
|---|---|---|---|
| P2-1 | 字阶 9 档、5 档 1px 递进 | 收敛 7 档：`11`（BrandSub / CountPill / ListNote）· `12`（Tag / WindowTitle）· **`13`（LoginSlogan / Placeholder / WelcomeDate 并入）** · `14`（H3 / NavButton）· `17`（H2 / BrandTitle）· `22`（LoginBrandTitle）· `26`（H1）· `30`（WelcomeGreeting） | 相邻档至少差 2px，肉眼可分 |
| P2-2 | 圆角八档 | 收敛 8（按钮/输入）/ 10（卡片/面板）/ 12（窗口）；Tag 9→8、WelcomeCard 16→12、UNINSTALL 6→8，删掉 4/5 | 圆角只表达"控件 < 卡片 < 窗口" |
| P2-3 | `#TopMeta` 只有颜色没有字号 | 方案 A 下该规则随页名删除；方案 B 下补 `font-size:17px; font-weight:700; color:#1F2430;` | 顶栏文字不再直接吃 13.33px 正文 |
| P2-4 | 登录窗标签与输入框零间距、字段间距六种 | `login_window.py:227` `setSpacing(0)` → `6`，让标签贴住输入框；`:237 / 245 / 253 / 265 / 283 / 322 / 331 / 337 / 355 / 362 / 369 / 377` 的 12/18/22/26 统一 `addSpacing(16)` | 标签-输入框成组，组间距一律 16 |
| P2-5 | 内联 `setStyleSheet` 27 处 | 能搬的搬进 QSS（`login_window.py:205` → `QFrame#LoginDivider`；`archive_page.py` 的 Tag 覆盖 → 用 `#TagGray` 原色）；`model_center` 的 `*_STYLE` 集中到文件头部并注释"为什么不能进 QSS"（选中态随 `tlSelected` 变） | 改一次颜色不用满仓库搜 |
| P2-6 | 开屏自建色板 | 抽 `ui/tokens.py`（纯常量、零 Qt import），`splash` / `welcome` / `model_center` / `scholarship` 的颜色常量全部从它取 | 开屏、欢迎页、主界面同一份色板 |
| P2-7 | 奖牌整行染色与斑马纹打架 | `scholarship_page.py:765-775` 只给「名次」「奖项」两列染色，其余列保持白/斑马 | 表格回到纵向节奏，奖项仍一眼可见 |
| P2-8 | 顶栏 54 / 侧栏 190 / 导航 44 无派生关系 | 导航 44→48（190 宽里六项更松），品牌区 `18,20,14,16` 与页脚 `18,8,14,16` 统一 | 侧栏视觉密度更匀 |
| P2-9 | 空 objectName（`#Content` / `#GradeTree` / …） | 要么补规则、要么删 `setObjectName`——留着名字最糟，后来人以为它有样式；新增 19 个尤其要清 | 名字与样式一一对应 |
| P2-10 | `NAV_ITEMS` 用双空格排版图标与文字，`_sync_nav` 又把双空格压成单空格（`main_window.py:105-116`、`:509`） | 改 `setIcon()` + `setIconSize(QSize(16,16))` + `text-align:left`，文字里不再有空格 | 图标与文字间距由布局控制 |
| P2-11 | 头像色板 8 色（6 个新值）未登记 | `profile_shared.py:522-525` | 保留多彩头像（语义合理），把 8 个值登记进 `ui/tokens.py` 的 `AVATAR_COLORS`，注明"唯一允许的强调色例外" | 颜色闸门不会误判，也不会再出现第 9 个 |

---

## 4. P0 施工单

> 施工顺序建议：**P0-1 → P0-2 → P0-5 → P0-3 → P0-4**。前三条是纯 QSS，半小时内可见；P0-3 改两行但影响最大；P0-4 是结构调整，放最后统一截图验收。

### 4.1 P0-1 滚动区底色

**目标**：8 个滚动区在任何系统主题下都呈现 `#F1F5F9`。

**现状（v1.9.3 复核）**：
- 8 个 `QScrollArea` 分布在 7 个文件：`settings_page` / `scholarship_page` / `archive_page` / `profile_page`（2 个）/ `review_dialog` / `update_dialog` / `model_center_dialog`；
- `styles.qss` 中 `QScrollArea` 规则数 = 0；
- 代码中 `setAutoFillBackground` 调用数 = 0（全仓库）；
- 唯一补丁：`model_center_dialog.py:933`。

**改动**（只改 `ui/styles.qss`，末尾追加）：

```css
QScrollArea,
QScrollArea > QWidget > QWidget {
    background: transparent;
    border: none;
}
```

**注意**：
- 不要同时给 viewport 设 `setAutoFillBackground(False)` 再叠 QSS——两种修法选一；选 QSS 是为了打包版一致。
- `QScrollArea > QWidget > QWidget` 是全局后代选择器，会给 QSS polish 增加一点成本；加完必须重量切页耗时（当前中位约 137ms，目标 ≤150ms）。

**验收**：
1. Windows 设置 → 个性化 → 颜色 → 深色；打开设置页 / 奖学金页 / 成长档案页 / 侧写页，各截一张图，窗口中心像素 ≈ `#F1F5F9`（±2/255）；
2. 浅色主题同样四页；
3. 模型中心仍正常（内联补丁保留，幂等）；
4. 切页 10 次取中位 ≤150ms。

### 4.2 P0-2 首页口径三档

**目标**：三档按钮进入全站"选中＝蓝"的语言。

**现状（v1.9.3 复核）**：
- `analysis_page.py:671` 设 `#FilterToggle`；QSS 规则数 = 0；
- 三个按钮 `setCheckable(True)` + `QButtonGroup` 互斥（`:666-687`），`setMinimumHeight(34)`（`:674`）；
- 原生渲染：未选中 `#FAFAFA` / 边框 `#ABABAB`；选中 `#DBDBDB`。

**改动**（只改 `ui/styles.qss`，追加四条）：

```css
QPushButton#FilterToggle {
    background: #FFFFFF;
    color: #475569;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    padding: 6px 14px;
    font-size: 13px;
}
QPushButton#FilterToggle:hover {
    background: #F8FAFC;
    border-color: #CBD5E1;
}
QPushButton#FilterToggle:checked {
    background: #EFF6FF;
    border-color: #3B82F6;
    color: #1D4ED8;
    font-weight: 700;
}
QPushButton#FilterToggle:checked:hover {
    background: #DBEAFE;
}
```

**验收**：
1. 三档各截一张：选中那枚底色落在 `#EFF6FF`–`#DBEAFE`，边框 `#3B82F6`，文字 `#1D4ED8`；
2. 三枚等高（34px），并排间距一致；
3. hover 不覆盖 selected（`:checked:hover` 独立一条）；
4. Tab 键聚焦到三枚按钮时，焦点可见（若 P1-4 未做，先记录为缺口，不阻塞本项）。

### 4.3 P0-3 加权均分：右对齐 + 两位小数

**目标**：同一列的小数点纵向对齐；同一指标全站同一精度。

**现状（v1.9.3 复核，全部命中）**：
- `analysis_page.py:288-289` 的 `_fmt` 用 `.6f`；
- 表格数值列 `:1199-1201` 用 `AlignLeft | AlignVCenter`；
- 姓名 / 学号 `:1183` / `:1187` 用 `AlignCenter`；
- `core/scholarship.py:204-209` 的 `academic_text` / `final_text` 用 `.6f`；
- 已核：`academic_text` / `final_text` 只在 `scholarship_page.py:771` / `:775` 两处表格显示使用，**导出路径不经过它们**，改精度不影响 xlsx；
- `tests/` 中无 `.6f` / `academic_text` / `85.714286` 断言（grep 无命中）。

**改动**：
1. `analysis_page.py:288-289`：`f"{value:.2f}"`（明细行 `:1277` 也走它，一并变两位）；
2. `analysis_page.py:1199-1201`：`AlignRight | AlignVCenter`；
3. `analysis_page.py:1183 / 1187`：改 `AlignLeft | AlignVCenter`（推荐；文字左、数字右，形成固定语言）；
4. 表头 `horizontalHeaderItem(i).setTextAlignment(...)` 与数据列同侧（当前未设置）；
5. `core/scholarship.py:204-209`：`academic_text` / `final_text` 改 `.2f`；
6. 跑一遍 `pytest` 确认无显示字符串断言被打破。

**验收**：
1. 表格三条样本 `9.50 / 85.71 / 100.00` 右缘差 ≤1px；
2. 列宽较现状省约 40px；
3. 页下明细行显示两位小数；
4. 奖学金页同表两位小数；
5. 导出 xlsx 数值不变（导出不经过 text 属性）。

### 4.4 P0-4 页名：一个视口一个标题

**目标**：用户在任何时刻只需要在一个位置看到当前页名。

**现状（v1.9.3 复核）**：
- 顶栏：`main_window.py:273-281` 用户名（`#TopUser`，700）→ stretch → 页名（`#TopMeta`，400、`#94A3B8`）→ 窗口按钮；
- 内容区 `#H1`：账号页（`account_page.py:117`）、奖学金页（`scholarship_page.py:252`）、设置页（`settings_page.py:236`）三页有；
- 分析页、成长档案页、侧写页**没有**内容区页标题；侧写子页用 16pt 自定义标题（`semester_side_profile.py:231-236`）；
- `main_window.py:509` 在 `_sync_nav` 里写页名。

**方案 A（推荐）**：删顶栏页名，内容区 `#H1` 唯一。
1. `main_window.py:279-281` 删除 `_page_title` 及其 `addWidget`；`:509` 删掉 `setText` 行；
2. `_user_label` 从 stretch 左侧移到 stretch 右侧（窗口按钮左侧），对象名改 `#TopMeta`；`styles.qss:201-207` 的 `#TopUser` 规则随之删除或合并；
3. 给分析页（`analysis_page.py:618` 起）、成长档案页（`archive_page.py:151` 起）、侧写页（`profile_page.py:866` 起）各加一行 `QLabel("…")` + `#H1` 作为首行；
4. 侧写子页标题改用 `#H2`（见 P1-15）。

**方案 B（更省事）**：保留顶栏页名并提升，删三页内容 `#H1`。
1. `styles.qss:205-207` 的 `#TopMeta` 补 `font-size:17px; font-weight:700; color:#1F2430;`；
2. 删 `account_page.py:117`、`scholarship_page.py:252`、`settings_page.py:236` 三条 `#H1` 页标题；
3. `#TopUser` 降为 `13px/400/#64748B`；
4. 侧写子页同样改用 `#H2`。

**验收**：
1. 六页各截一张：页名只出现一次，字号/字重一致；
2. 常规窗口宽度（≥1280px）下顶栏不挤占窗口按钮（用户名 + 按钮总宽留 24px 余量）；
3. 最大化/还原时顶栏圆角与内容不打架（`[tlMaximized="true"]` 分支仍生效）。

### 4.5 P0-5 消息框最小套装

**目标**：最高频的反馈面进入设计体系，默认按钮有明确主次。

**现状（v1.9.3 复核）**：13 个文件、142 处 `QMessageBox` 调用；QSS 规则数 = 0。

**改动**（只改 `ui/styles.qss`，追加）：

```css
QMessageBox {
    background: #FFFFFF;
}
QMessageBox QLabel {
    color: #1F2430;
    font-size: 13px;
}
QMessageBox QPushButton {
    background: #FFFFFF;
    color: #475569;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    padding: 6px 16px;
    min-width: 72px;
}
QMessageBox QPushButton:hover {
    background: #F8FAFC;
    border-color: #94A3B8;
}
QMessageBox QPushButton:default {
    background: #3B82F6;
    color: #FFFFFF;
    border: none;
}
QMessageBox QPushButton:default:hover {
    background: #2F76E8;
}
```

**注意**：
- 不动系统图标、不动按钮排序（Qt 自己排）；
- 危险确认框若需要"红色确定"，用 `setObjectName` + 选择器，不要全局改红；
- 消息框文案是纯文本，不能出现 Markdown 记号（`**` / `__` / 反引号）——AGENTS §24 的 `tl_label_lint` 会扫。

**验收**：
1. 打开 5 类对话框（登录失败 / 导入确认 / 删除确认 / 更新提示 / 模型中心），样式一致；
2. 默认按钮为主色，次按钮白底描边；
3. 深色系统主题下文字可读（`QMessageBox` 背景显式白色，不跟 QPalette 走）。

---

## 5. 必须保住的现有做法

改动时不许破坏：

1. **`#NavButton` 基态就有 `border-left: 4px solid transparent`**（`styles.qss:166-170`）——选中换成 4px 蓝线，文字不左右跳。**任何新增的左侧标记都必须照抄这一招**（P1-4 的焦点边框同理，基态透明）。
2. **阴影只走 `ui/anim.py::apply_shadow`**（QSS 不支持 `box-shadow`）；卡片统一由 `make_card()` 造。
3. **选中态语言**：`rgba(59,130,246,0.26)` 半透明蓝叠底 + 4px 蓝线（`styles.qss:179-188`）——深色侧栏上的正确做法，P0-2 / P1-2 必须与它同族。
4. **表格四件套**：`gridline-color:#EEF2F7` + `alternate-background-color:#F8FAFC` + `selection-background-color:#DBEAFE` + `outline:none`（`styles.qss` 表格段）——`outline` 是表格专用，不要顺手给按钮也设。
5. **状态标签四色语义齐备**（`TagGreen / TagGray / TagOrange / TagRed`，`styles.qss:409-440`）——先去复用，不要新建第五套。
6. **无边框窗口的圆角分配**：`#Sidebar` 只带左两角、`#TopBar` 只带右上角、`#WindowRoot` 带全部 + `[tlMaximized="true"]` 归零（`styles.qss:143-200`）。新增贴边区块必须带对应角。
7. **滚动条已完整自绘**（纵横、10px、圆角 5、无箭头）——新滚动区不需要再写。
8. **`review_dialog` 的 `_HANDLE_QSS` 是内联合规范例**：内联样式表必须带 objectName 限定（`QSplitter#ReviewSplit::handle`），不污染全局。

---

## 6. Token 与长期治理

> Qt 的限制先说清：QSS 没有变量、没有 `calc()`、不支持 `box-shadow`。所以 token 只能落成 **Python 常量模块 + QSS 顶部取值表 + selfcheck 对账**，不能指望 QSS 里写 `var(--space-4)`。

### 6.1 建议固化的 token（值全部取自现有）

| 类别 | 建议名 | 现值 |
|---|---|---|
| 页面内边距 | `PAGE_PAD` | `22, 20, 22, 20` |
| 卡片内边距 | `CARD_PAD` | `20, 16`（拟统一） |
| 卡片间距 / 卡内间距 | `CARD_GAP` / `CARD_INNER_GAP` | `14` / `10` |
| 控件高度 | `CONTROL_H` / `CONTROL_H_SM` | `38` / `34` |
| 圆角 | `RADIUS_CTRL / CARD / WINDOW` | `8 / 10 / 12` |
| 字阶 | `FONT_XS…DISPLAY` | `11 / 12 / 13 / 14 / 17 / 22 / 26 / 30` |
| 主色阶 | `BLUE / BLUE_HOVER / BLUE_PRESSED` | `#3B82F6 / #2F76E8 / #2563EB` |
| 语义色 | `DANGER / DANGER_TEXT / SUCCESS / WARNING` | `#EF4444 / #B91C1C / #22C55E / #B45309` |
| 中性阶 | `INK / INK_2 / MUTED / MUTED_2` | `#1F2430 / #475569 / #64748B / #94A3B8` |
| 边框阶 | `LINE / LINE_2 / LINE_3` | `#E2E8F0 / #EEF2F7 / #CBD5E1` |
| 底色阶 | `SURFACE / SURFACE_2 / PAGE` | `#FFFFFF / #F8FAFC / #F1F5F9` |

### 6.2 落地三步（不改视觉方向）

1. 新建 `ui/tokens.py`（纯常量、零 Qt import），先只放**已经统一的值**，不强行改任何现有像素；`profile_shared.py:527-534` 已有模块级颜色常量，可作为雏形；
2. `styles.qss` 开头加一张「取值表」注释块，把 §6.1 抄进去作为阅读索引；
3. `main.py --selfcheck` 加一条 `_ui_token_report()`（与现有 `_version_source_report()`、`_category_policy_report()` 同构）：
   - 断言 `ui/tokens.py` 里每个颜色值都能在 `styles.qss` 里找到；
   - `styles.qss` 里出现的颜色值都在表内或白名单里；
   - `ui/**/*.py` 里 `setStyleSheet` 的硬编码颜色也在表内。

**这是本次评审性价比最高的一件事**——它把"下一个颜色漂移"挡在门外。

---

## 7. 验收清单

### 7.1 必须量的像素

- 六页 + 登录窗 + 4 个对话框各一张截图，**100% 与 150% DPI 各一轮**；切页后再等 1 秒抓第二帧，两帧必须逐像素相同（AGENTS §17）。
- **深色主题专项**（P0-1 的唯一有效验收方式）：Windows 深色模式 → 设置页 / 奖学金页 / 成长档案页 / 侧写页，整页底色仍是 `#F1F5F9`。
- 首页口径三档：截三张，断言选中那枚底色在 `#EFF6FF`–`#DBEAFE`、边框 `#3B82F6`、三枚等高 34（P0-2）。
- 表格：三条样本 `9.50 / 85.71 / 100.00` 右缘差 ≤1px；操作列仍 `setColumnWidth(96)` + `Fixed`、行高 44 ≥ 28 + 12（AGENTS §4c）。
- 键盘：`QTest.keyClick(Qt.Key_Tab)` 走完六页，逐个断言"焦点处 3px 内像素变化"（P1-4）。

### 7.2 必须跑的既有闸门

| 闸门 | 关注点 |
|---|---|
| `python main.py --selfcheck` | `_docs_gate_report()`（docs 白名单 + 无死链）、`_version_source_report()`（界面无版本字面量）、`_removed_gate_report()`（界面 0 处"激活"） |
| `pytest` | 全量用例；P0-3 改精度后重点看 `tests/` 里显示字符串断言 |
| 标签 lint | 运行时遍历主窗 162 处控件文字 + 气泡，0 处 `**` / `__` / 反引号（AGENTS §24）。**说明**：`tl_label_lint.py` 历史上放在 `%TEMP%`，本次核查未在工作区与 `%TEMP%` 找到（可能已被清理）；按 AGENTS §24 的判据重建，或改用等价的运行时遍历。 |
| 切页驱动 | 看门狗落位 + `QTest.mouseMove()` 四边四角（AGENTS §4h / §11）。`tl_qt_drive.py` 同样不在当前工作区，需按判据重建。 |
| 切页性能 | 加 P0-1 的后代选择器后重量；当前中位约 137ms（`tl_perf_bench.py` 不在工作区，需重建或手测 10 次中位）。 |
| 切页洁净度 | 若有人为"卡片淡入"挂 `QGraphicsOpacityEffect`，立刻红（AGENTS §17）。本次所有建议**都不需要**给页面挂任何效果。 |

### 7.3 每次改 QSS 后的自检

- [ ] 新加的每个 objectName 都能在 `styles.qss` 找到规则（当前 36/77 不合格）
- [ ] 新加的每个颜色值都在 `ui/tokens.py` 里（或明确写进白名单并注明理由）
- [ ] 新按钮有 5 个状态：默认 / hover / pressed / **focus** / disabled（当前 focus 全缺）
- [ ] 新左侧标记在基态写 `transparent`（防选中时文字跳位）
- [ ] 新增贴边区块带对应窗口圆角 + `[tlMaximized="true"]` 归零分支
- [ ] 页面容器**没有**挂任何 `QGraphicsEffect`
- [ ] 所有界面文案是纯文本，无 `**` / `__` / 反引号；不出现内部文档名
- [ ] 数值列右对齐、两位小数；导出仍保留原始精度

---

## 8. 附录

### 8.1 未入 QSS 的 36 个 objectName（按模块）

| 模块 | 数量 | objectName |
|---|---|---|
| `semester_side_profile.py` | 14 | `#aiStatus`、`#aiTextEdit`、`#backButton`、`#courseNote`、`#courseTable`、`#emptyHint`、`#generateSideButton`、`#pageTitle`、`#saveSideButton`、`#semesterSplitter`、`#statLine`、`#statNote`、`#studentLine`、`#talkButton` |
| `profile_shared.py` | 5 | `#FormInput`、`#TermAvg`、`#TermName`、`#TermNode`、`#TermTrend` |
| `model_center_dialog.py` | 4 | `#ModelCard`、`#ModelCardContent`、`#ModelCardHost`、`#ModelGroupHeader` |
| `profile_page.py` | 4 | `#GradeTree`、`#ProfileStack`、`#ProfileTabs`、`#TimelineScroll` |
| `review_dialog.py` | 3 | `#ReviewHintScroll`、`#ReviewHints`、`#ReviewSplit` |
| `update_dialog.py` | 2 | `#H4`、`#NotesScroll` |
| `analysis_page.py` | 1 | `#FilterToggle` |
| `main_window.py` | 1 | `#Content` |
| `settings_page.py` | 1 | `#PageScroll` |
| `archive_page.py` | 1 | `#TimelineRow` |

### 8.2 普查数据（2026-11-08 脚本复核）

| 指标 | v1.9.1 基线 | v1.9.3 复核 |
|---|---|---|
| UI 模块 / 行数 | 22 / 15,556 | **27 / 19,464** |
| objectName 总数 / 未入 QSS | 56 / 16 | **77 / 36** |
| `QScrollArea` 实例 | 7 | **8（7 文件）** |
| `QMessageBox` 调用 | 143 | **142（13 文件）** |
| 内联 `setStyleSheet` | 26 | **27** |
| 圆角档位 | 6 | **8（4/5/6/8/9/10/12/16）** |
| 字号档位 | 9 | **9（11/12/13/14/15/17/22/26/30）** |
| QSS 色值 / py 色值 | — | **39 / 38** |
| `#FilterToggle` / `#H4` / `#CardFlat` 规则 | 0 / 0 / 1 | **0 / 0 / 1** |
| `QMessageBox` / `QScrollArea` / `QTabWidget` / `QProgressBar` 规则 | 0 | **0** |

> 统计口径：只计 2026-11-08 23:42 快照时 `ui/**/*.py` 中已存在的文件。此后工作区出现的并发/未跟踪开发文件未纳入；如需最新数字，重跑 `objectName` 对账脚本即可。

### 8.3 v1.9.1 → v1.9.3 复核差异

**已修复**

- `ui/chat_dialog.py`：v1.9.1 时全窗 0 个 objectName；现在 `#H3`（`:338`）、`#Muted`（`:342`）、`#Outline`（`:361`）、`#Primary`（`:380`）、`#Ghost`（`:386`）、`#Warn`（`:523 / 732 / 759`）均已挂上，D6 从问题清单移除。

**新增缺口（v1.9.2+ 模块）**

- `semester_side_profile.py`：14 个 objectName 未入 QSS；根内边距 `20,16`（`:217`）；标题 `#pageTitle` + 手写 `pointSize+6`（`:231-236`）。
- `profile_shared.py`：5 个 objectName 未入 QSS；`StudentDialog` 内边距 `20,18,20,16`（`:1740`）；`TermNode` 内边距 `12,8`（`:1656`）；新增 `_AVATAR_COLORS` 8 色（`:522-525`，其中 6 个是体系外色值）。
- `backend_setup.py`：无 objectName 缺口；有 1 处 `QMessageBox`（归入 P0-5 覆盖面）。

**统计修正**

- 模块数 22 → 27，行数 15,556 → 19,464（新增 3 个模块 + 既有模块增长）；
- objectName 缺口 16/56 → 36/77（新增模块贡献 19 个）；
- `QScrollArea` 7 → 8（`profile_page` 内为 2 个实例）；
- `QMessageBox` 143 → 142（调用点统计口径只算 `information / warning / critical / question / about`）；
- 内联 `setStyleSheet` 26 → 27；
- 圆角 6 → 8 档（新增 4 / 5）。

---

## 9. 一句话给施工顺序

**P0-1（滚动区）和 P0-2（口径三档）各是一段 QSS，半小时内能落地且立刻可见；P0-5（消息框）同样只动 QSS；P0-3（对齐+精度）改两行代码但影响最大——它决定这张默认首页能不能被"读"；P0-4（页名）属于结构调整，建议同批做完再统一截图验收。P1/P2 跟下一轮迭代走，不阻塞。**
