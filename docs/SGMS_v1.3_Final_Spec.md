> ⚠️ **本文件已归档（2026-09-22）**
> 本 Spec 已被 `docs/SGMS_v1.6_Dev_Charter.md` 全面取代。
> 所有新开发以 v1.6 为准，本文档仅保留作历史参考，不再更新。
># 成绩单透镜（transcript-lens）· v1.3 终版技术设计文档

> 仓库：`https://gitee.com/wuhaotiansystem/transcript-lens.git`
> 默认分支：`master`（Gitee 初始化默认，沿用即可，不再改）
> 文档版本：v1.3（所有业务规则已关闭，Owner 定死）
> 读者：实现 Agent（Cursor / Claude Code / Copilot / 通义灵码等）
> **禁令**：本文档不包含业务代码实现。Agent 只许按第 8 章清单建文件、写骨架。禁止自行发明业务逻辑、禁止跳过红线。

---

## 零、给 Agent 的第一句话

- **算分永远由本地确定性逻辑完成，禁止让大模型做算术。** 大模型只负责把算好的数字翻译成人话。
- **空单元格 = 未选课（跳过）；数字 0 = 缺考/挂科（必须计入加权）。** 两条不同语义，混淆即事故。
- **打包产物与仓库里，绝对不能出现 `data/`、`*.db`、`*.xlsx`（除测试假数据）、`.key`、真实激活码明文。**
- **传给任何外部 AI 接口的 payload 里，禁止出现学生真名、真学号、身份证。**

---

## 一、仓库约定（Agent 必读）

### 1.1 基本信息
- 仓库地址：`https://gitee.com/wuhaotiansystem/transcript-lens.git`
- 主分支：`master`（Gitee 初始化默认值，**不要改成 main**，避免和远程不同源）
- 仓库当前为**公开 + MIT 许可证**。因此：真实成绩、真实激活码、密钥文件**永不上传**是硬约束。

### 1.2 分支策略
- **单主干开发**：所有人只在 `master` 上 commit，不建 `develop`、不建 `feature/*`。
- 需要存档时打 tag：`git tag v0.1.0-spec-locked`。
- 改崩了回滚：`git checkout <commit-hash>` 或 `git revert`。

### 1.3 提交规范（Agent 每次 commit 必守）
- commit message 格式：`<模块>: <做了什么>`
- 例：`core: 实现加权计算器与三口径切换`
- 例：`ui: 完成导入页学期归属选择`
- 例：`docs: 更新 v1.3 规格说明`
- **每次 commit 前必须跑一遍 `.gitignore` 校验**（见第 3 章白名单）。

### 1.4 首次推送流程（Owner 本地执行一次）
```bash
git clone https://gitee.com/wuhaotiansystem/transcript-lens.git
cd transcript-lens
# 把本文档放进 docs/ 并提交
git add docs/SGMS_v1.3_Final_Spec.md
git commit -m "docs: 锁定 v1.3 技术规格"
git push origin master
```

---

## 二、已关闭的全部业务规则（Agent 不得再问 Owner）

| 规则 | 最终决策 |
|------|---------|
| 初始激活码 | `SGMS-1ABCD-2026`，版本号=1；本地只存版本号不存明文；换码升版本，只锁导入功能，绝不清除已有成绩库 |
| 补考 | **不覆盖原成绩**。原不及格分数继续计入加权均分；补考通过仅体现在"已清挂标记"，谈话提纲提示"已定型不必纠结" |
| 发展意愿标签 | 辅导员在 UI 手动勾选（保研/考研/就业/考公/留学/未定），存本地加密库；v1 不做学生端、不做扫码 |
| 同名课程出现两次（非补考） | **取成绩高那次**，对应那次的学分参与加权；不叠加、不平均 |
| 空单元格 vs 0 分 | 空 / `-` / `/` / `未选` / 纯空格 = 未选跳过；数字 0 = 缺考照算 |
| 学期合并公式 | `[Σ(上成绩×上学分) + Σ(下成绩×下学分)] / [Σ上学分 + Σ下学分]`，**禁止**对两个学期的均分再取平均 |
| 默认计算口径 | **仅必修+限选计入**（选修默认剔除），UI 提供三档切换 |
| 纵向累计 | 按"学号哈希 + 学期标签"append，支持大一到大四任意次追加 |
| 数据目录 | `C:\Users\<系统用户>\AppData\Local\SGMS\` |
| 分发形态 | 绿色 exe + launcher，不写注册表、不装 C 盘 |
| 第三方登录 | v1 **不做**微信/QQ登录（需企业资质+公网服务端，与本地单机定位冲突） |

---

## 三、.gitignore 白名单（Agent 建仓第一步就放）

```gitignore
# ============ 数据：打死不进仓库 ============
data/
*.db
*.enc
*.sqlite
*.db-journal

# ============ 真实成绩：绝不在仓库出现 ============
*.xls
*.xlsx
# 例外：仅允许测试假数据
!tests/fixtures/fake_*.xlsx

# ============ 密钥与真实配置 ============
config/activation.json
config/rules.json
config/ai_settings.json
.env
*.key
*.pem
*.p12

# ============ 打包产物 ============
build/
dist/
__pycache__/
*.pyc
*.spec
*.exe

# ============ 日志 ============
logs/
*.log

# ============ 编辑器与系统 ============
.vscode/
.idea/
.DS_Store
Thumbs.db
```

**自检脚本**：Agent 在完成打包或 commit 前，扫描工作区，命中 `data/`、`*.db`、`*.xlsx`（非 `tests/fixtures/fake_` 开头）、`.key` 任一即 **abort 并报警**。

---

## 四、目录结构（Agent 建骨架用）

```
transcript-lens/
├─ .gitignore
├─ README.md                  ← 已有，Agent 可补充"功能清单"一节
├─ LICENSE                    ← 已有（MIT），不动
├─ requirements.txt           ← pandas openpyxl cryptography requests
├─ config/
│  ├─ rules.example.json      ← 课程类别别名、绩点换算占位（进仓库）
│  └─ activation.example.json ← {"MIN_VERSION":1,"NOTE":"真实码不入库"}
├─ core/                      ← 纯逻辑，无 UI
│  ├─ __init__.py
│  ├─ parser.py               ← 表头三级提取 + 回显数据结构
│  ├─ calculator.py           ← 加权、合并、取高、三口径
│  ├─ archival.py             ← 学期 append、纵向累计、趋势 Δ
│  ├─ crypto_store.py         ← PBKDF2 派生 + Fernet 加密落盘
│  └─ activation.py           ← ActivationEngine.verify(code)
├─ ui/                        ← tkinter
│  ├─ __init__.py
│  ├─ auth_page.py            ← 注册/登录/激活码输入
│  ├─ import_page.py          ← 选文件+学期归属+单表/双表+回显确认弹窗
│  ├─ result_page.py          ← 全班列表+三口径切换+红标
│  ├─ profile_page.py         ← 个人纵向时间轴+意愿标签+谈话提纲
│  └─ settings_page.py        ← 改码、导出、清库、AI 开关
├─ ai/
│  ├─ __init__.py
│  ├─ local_template.py       ← 三段式规则模板（不联网也能出提纲）
│  └─ cloud_adapter.py        ← 云端调用，payload 强制脱敏，key 从配置读
├─ launcher/
│  └─ version_check.py        ← 读远程 version.json，v1 只弹窗不静默下载
├─ tests/
│  └─ fixtures/
│     └─ fake_grades.xlsx     ← 假数据：张三/李四/王五，成绩 60~95，绝不用真表
└─ docs/
   └─ SGMS_v1.3_Final_Spec.md ← 本文档
```

---

## 五、数据隔离与落盘路径

```
C:\Users\<系统用户>\AppData\Local\SGMS\
├─ users.json                 ← UID + 用户名 + salt + 密码哈希（无明文）
├─ u_<uid 前 8 位>\
│  ├─ grades.enc              ← 加密后的成绩库
│  ├─ tags.json.enc           ← 意愿标签（加密）
│  └─ sessions.json.enc       ← 学期导入记录（哪学期、什么文件、什么时间）
```

- exe 放在桌面或任意目录，与 AppData **物理分离**，打包时不可能夹带用户数据。
- 每个用户密钥由其密码 PBKDF2 派生 → 同一台电脑上用户 A 解不开用户 B 的库。

---

## 六、导入解析铁律（Agent 最容易写错的地方）

1. **三级提取**：正则抠 `学分\s*(\d+\.?\d*)` 与 `(必修|限选|选修|任选)` → 独立权重行 → 都失败则标红进人工弹窗。
2. **人工回显弹窗是强制环节**，不能 skip。
3. **列数一致性校验**：某行成绩列数 < 课程列数 → 整行标红不参与计算，弹窗提示"第 X 行可能错位"。
4. **文本型数字强转 float**：`"85.000000"` → `85.0`；转不了 → 视为异常标黄。
5. **校验锚点**：若表里有"所得学分"列，程序算出总学分与锚点差 > 0.5 → 弹窗警告。

---

## 七、谈话提纲三段式（本地模板，不调 AI 也能出）

```
【事实】累计均分 XX，大二下高数 58（已定型/5 学分，补过已清挂），大三上英语 68→82（进步）
【行动】本学期可抓：大物 72/3 学分提到 85 能拉回 0.1；已挂的不必再纠结
【对标意愿=保研】核心课均分需 80+，当前差 2.3 分，剩余高学分课还有 3 门可冲；若本学期无起色建议早做两手准备
```

调云端时 payload 只允许：`S 编号 + 均分 + 课程学分 + 分数区间 + 意愿标签`。禁止姓名、真学号。

---

## 八、v1 交付清单（Agent 按此顺序，逐项打勾）

### 第一批：逻辑层（不依赖 UI）
- [ ] `core/parser.py` —— 表头三级提取 + 人工回显数据结构，用假数据跑通
- [ ] `core/calculator.py` —— 单表加权、三口径切换、同名取高
- [ ] `core/archival.py` —— 学期 append、纵向累计、趋势 Δ、定型/可救分区
- [ ] `core/crypto_store.py` —— PBKDF2 + Fernet 加密落盘
- [ ] `core/activation.py` —— 激活码版本号校验

### 第二批：界面层
- [ ] `ui/auth_page.py` —— 注册/登录/激活码输入
- [ ] `ui/import_page.py` —— 选文件 + 学期归属下拉 + 单表/双表切换 + 回显确认弹窗
- [ ] `ui/result_page.py` —— 全班列表 + 三口径切换 + 红标预警
- [ ] `ui/profile_page.py` —— 个人纵向时间轴 + 意愿标签 + 谈话提纲
- [ ] `ui/settings_page.py` —— 改码、导出（明文/脱敏两种 xlsx）、清库、AI 开关

### 第三批：AI 与更新
- [ ] `ai/local_template.py` —— 三段式提纲（不联网先能出）
- [ ] `ai/cloud_adapter.py` —— 云端调用，payload 强制脱敏，key 从配置读
- [ ] `launcher/version_check.py` —— 读远程 version.json，v1 只弹窗提示，不静默下载

### 收尾
- [ ] `tests/fixtures/fake_grades.xlsx` —— 造假数据，用于对齐数字
- [ ] `requirements.txt`
- [ ] PyInstaller 打包脚本（含白名单自检）
- [ ] 每完成一个模块就 commit 一次，message 符合第 1.3 节规范

---

## 九、致命红线（共 11 条，Agent 违反即重写）

| # | 红线 |
|---|------|
| 1 | 加权算术禁止交给大模型 |
| 2 | 空 ≠ 0（未选跳过 vs 缺考计入） |
| 3 | 学分提取必须人工回显确认 |
| 4 | 文本成绩必须强转 float |
| 5 | 打包与 commit 前必须跑白名单自检 |
| 6 | 传 AI 强制脱敏 |
| 7 | 换激活码只锁导入，不清成绩库 |
| 8 | 学年合并用总加权，不对两个均分再平均 |
| 9 | 补考不覆盖原分，原不及格继续拉低绩点 |
| 10 | 列数不一致标红禁参与，禁止静默吞行 |
| 11 | 仓库与打包产物里永远不出现真实成绩文件 |

---

## 十、Owner 已定死、不再讨论的参数

- 仓库：`wuhaotiansystem/transcript-lens`，分支 `master`
- 初始激活码：`SGMS-1ABCD-2026`（版本号 1）
- 默认计算口径：必修+限选计入（选修剔除）
- 合并时同名取高
- 补考不覆盖原成绩
- 数据目录：`AppData/Local/SGMS/`
- 分发：绿色 exe + launcher，不写注册表不装 C 盘
- 不做：Web、服务端、多人在线协同、第三方登录、4.0 GPA 换算（保留接口）、图表可视化（v2）、自动静默下载更新（v2）

---

## 十一、给 Agent 的交接语

> 你拿到本文档后，严格按第 8 章顺序实现：先 `core/` 全部逻辑 → 再 `ui/` → 再 `ai/` 与 `launcher/`。每一步写完对照第 9 章红线自检，用 `tests/fixtures/fake_grades.xlsx` 对齐数字。开发素材只用假数据，绝不用真实成绩表。每次 commit 遵循第 1.3 节规范。
