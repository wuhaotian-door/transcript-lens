# SGMS Dev Charter v1.7（完整开发准则）

> 本文件是 Agent 开发的唯一权威准则。取代 v1.6 及之前所有规格。
> 最后更新：2026-09-24
> 基于 v1.6 合并全部已完成项 + 新增更新检查/数据迁移 + 修正已知限制

---

## 一、课程分类（铁律，不变）

必修、限选、选修是**三个完全独立的类别**。

| 类别 | 标识 |
|------|------|
| 必修 | `required` |
| 限选 | `limited_elective` |
| 选修 | `free_elective` |

| 口径 | 包含 | 用途 |
|------|------|------|
| ① 总加权 | 必修+限选+选修 | 全局总评 |
| ② 仅必修+限选 | 必修+限选 | 主修能力 |
| ③ 仅必修 | 仅必修 | 核心底线 |

> **UI 筛选**：当前为互斥三档（全部/必修+限选/仅必修），按钮气泡和状态行明确标注"选修不计入"。
> 如需支持限选单独看/选修单独看，需解冻 calculator.py 加口径——当前不做。

---

## 二、技术栈

| 层 | 选择 |
|----|------|
| GUI | PySide6>=6.6 |
| 打包 | PyInstaller `--collect-all PySide6` |
| 加密 | PBKDF2 + Fernet |
| 邮箱 | smtplib SMTP 465 |
| 导出 | openpyxl |
| 更新检查 | urllib.request（标准库，不走第三方） |
| AI | 本地预留 / 云端可选（用户主动） |

---

## 三、铁律

1. **calculator/parser/validator/tests/ 一个字符不许改**（封板）
2. **密码/授权码/找回码/库密钥：绝不 print、不回显、不进日志**
3. **找回密码**：邮箱存找回码，忘密码靠 24 位找回码解密
4. **索引键必须是学号**，废弃纯姓名索引
5. **不发明未定义规则**
6. **旧 tkinter 代码全删**
7. **表头从 SCOPE_COLUMNS 推导，禁止手写元组**
8. **选修不归入限选**（selfcheck 行为门验证，违反即 FAIL）
9. **QLabel 纯文本不渲染 Markdown**，所有气泡/状态行用纯文本或 Qt 富文本，不用 `**粗体**` 语法

---

## 四、联网红线（v1.7 更新）

### ✅ 允许
| 场景 | 条件 |
|------|------|
| SMTP 发邮件 | 找回密码流程，用户主动 |
| 辅导员手动启用云端大模型 | 设置页手动开 + API Key，仅主动点"深度评估" |
| **更新检查** | 用户点"检查更新" → urllib.request 访问 Gitee API，只读 |

### ❌ 禁止
- 自动更新/静默下载
- telemetry / 埋点
- requests/httpx 用于非上述场景

---

## 五、奖学金测算（已完成，v1.6 实装）

- 公式：`最终 = 智育口径①×0.7 + 德育×0.3`
- 按班级号人工输入建文件
- 名额独立配置（一/二/三等）
- 特等：前X名 + 每门≥90（遍历 courses 原始数据）
- ⚠️ **已知限制**：分数>100 会被静默排除出加权分母但仍在 course_scores 里。特等判定用 `score is not None`，101 笔误会达标。修复需改 calculator.py（铁律一封板），当前记录为已知问题。

---

## 六、UI 架构（已完成项汇总）

| 组件 | 状态 |
|------|------|
| 无边框+圆角12px+阴影blur15 | ✅ |
| 切页≤250ms + 切页锁 | ✅ 130ms |
| 欢迎页 1.5s 时间问候 | ✅（05-11早/12-17下/18-4晚） |
| 智育分析：学期下拉（8学期）+ 档案累加 | ✅ |
| 智育分析：筛选互斥三档+说明文字 | ✅ |
| 侧写辅导：左侧列表+搜索+学号索引 | ✅ |
| 成长档案：学期时间轴+趋势Δ+定型分区 | ✅ |
| 设置页：邮箱/AI开关/修改密码 | ✅ |
| 个人中心：切换账号/改密 | ✅ |

---

## 七、v1.7 新增：更新检查（Update Checker）

### 7.1 设计原则
- **用户主动触发**：设置页 [检查更新] 按钮
- **不自动下载**：只告知有新版本，打开浏览器让用户自己下载
- **不引入第三方库**：用 `urllib.request`（标准库）

### 7.2 实现规范
- 新建 `core/updater.py`：
  - `check_latest_version(timeout=5) -> str | None`：GET `https://gitee.com/api/v5/repos/wuhaotiansystem/transcript-lens/releases/latest`，解析 `tag_name`
  - 超时/失败返回 None，不弹错
- 设置页"更新"区域：
  - 显示 `CURRENT_VERSION = "1.7"`
  - [检查更新] 按钮
  - 结果：已最新 / 有新版本弹窗（"发现 v{X}，去下载"→ 打开浏览器）

### 7.3 红线合规
- urllib.request 用于更新检查是**唯一允许的 stdlib 联网方式**
- 不写自动替换 exe 的逻辑

---

## 八、v1.7 新增：数据迁移与版本保护

### 8.1 版本标记
- 加密库初始化时写入 `__vault_version__ = "1.7"`
- `CURRENT_VERSION` 常量定义在 `core/version.py`

### 8.2 迁移机制
- 新建 `core/migrator.py`：
  - `check_compatibility(vault_data) -> bool`：对比版本号
  - `migrate(old_version, vault_data, password) -> dict`：执行迁移
  - 迁移前**自动备份**到 `%LOCALAPPDATA%/SGMS/backup_v{old}_{timestamp}/`
- 启动时检测：版本不匹配 → 弹窗"检测到旧版数据，是否迁移" → 用户确认后执行

### 8.3 数据安全铁律
- 迁移前必须备份，备份失败则拒绝迁移
- 迁移过程不删除旧数据（只新增备份+写新格式）
- 版本降级（用新版 exe 打开旧库后，再用旧版 exe 打开）→ 弹窗提示不兼容

---

## 九、文件清单

| 文件 | 状态 |
|------|------|
| `docs/SGMS_v1.7_Dev_Charter.md` | 本文件 |
| `docs/SGMS_v1.3_Final_Spec.md` | 已归档（过期标头） |
| `config/rules.example.json` | ✅ 已存在 |
| `config/activation.example.json` | ✅ 已存在 |
| `core/updater.py` | 🆕 需新建 |
| `core/migrator.py` | 🆕 需新建 |
| `core/version.py` | 🆕 需新建 |
| `%LOCALAPPDATA%/SGMS/` | 数据目录（不进仓库） |

---

## 十、禁止事项速查

| 禁止 | 原因 |
|------|------|
| 改 calculator/parser/validator | 封板 |
| print 密码/密钥 | 安全 |
| 硬编码凭据 | 安全 |
| 用 PyQt6 | GPL |
| 用 tkinter | 淘汰 |
| 自动联网/静默更新 | 红线 |
| 自动下载替换 exe | 安全+红线 |
| 发明未定义规则 | 正确性 |
| QLabel 里写 Markdown 语法 | 不渲染 |
| 选修归入限选 | 铁律8 |