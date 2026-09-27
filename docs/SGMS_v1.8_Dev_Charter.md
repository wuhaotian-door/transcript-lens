# SGMS Dev Charter v1.8（完整开发准则）

> 本文件是 Agent 开发的唯一权威准则。取代 v1.7 及之前所有规格。
> 最后更新：2026-09-27
> 核心变更：自动更新器实装 / 版本号全局同步 / Excel导入修复 / AI下载器骨架

---

## 一、课程分类（不变）

必修、限选、选修三个完全独立。口径①全含、②必修+限选、③仅必修。

---

## 二、技术栈

| 层 | 选择 |
|----|------|
| GUI | PySide6>=6.6 |
| 打包 | PyInstaller `--collect-all PySide6` |
| 加密 | PBKDF2 + Fernet |
| 邮箱 | smtplib SMTP 465 |
| Excel | openpyxl（.xlsx）/ xlrd<2.0（.xls） |
| 更新下载 | urllib.request（标准库，用户主动） |
| AI模型 | 预留 GGUF 加载接口 |

---

## 三、铁律

1. calculator/parser/validator/tests 一个字符不许改
2. 密码/密钥绝不 print、不回显
3. 找回密码靠 24 位找回码
4. 索引键用学号
5. 不发明未定义规则
6. 旧 tkinter 全删
7. 表头从 SCOPE_COLUMNS 推导
8. 选修不归入限选（selfcheck 行为门验证）
9. QLabel 不渲染 Markdown
10. **版本号唯一真源：`core/version.py` 的 `CURRENT_VERSION`，任何界面显示版本号必须从此读取**

---

## 四、联网红线

### ✅ 允许
- SMTP（用户主动）
- 云端大模型（用户主动+手动API Key）
- 更新检查（urllib.request → Gitee API）
- 更新下载（urllib.request → 网盘直链/Release附件）
- AI模型下载（urllib.request → 网盘直链）

### ❌ 禁止
- 自动更新/静默下载
- telemetry
- requests/httpx 用于非上述场景

---

## 五、版本号管理（v1.8 新增，修正旧 bug）

| 位置 | 显示内容 | 来源 |
|------|---------|------|
| 欢迎页 | "早上好，{用户名}" + 版本号小字 | `CURRENT_VERSION` |
| 设置页"关于" | "成绩单透镜 v{CURRENT_VERSION}" | `CURRENT_VERSION` |
| 更新弹窗 | "发现新版本 v{X}" | Gitee API |
| 更新完成弹窗 | "已更新至 v{CURRENT_VERSION}" | `CURRENT_VERSION` |
| 登录窗口标题 | "成绩单透镜 v{CURRENT_VERSION}" | `CURRENT_VERSION` |

**旧 bug 修正**：v1.7 及之前版本，欢迎页/标题栏硬编码或读取了旧值（如 "1.0"），v1.8 必须全部统一从 `core/version.py` 读取。

---

## 六、自动更新流程（v1.8 重设计）

检查更新 → 发现新版本 → 弹窗（版本说明+模块勾选）

→ 开始下载 → 进度条

→ 下载完成 → 弹窗：

"━━━━━━━━━━━━━━━━━━━━━━━

✅ 更新已完成（v1.8）

纯文本
是否立即重启应用完成更新？
━━━━━━━━━━━━━━━━━━━━━━━"
[稍后]  [立即重启]

→ 点"立即重启"：

写 update.json 到 %TEMP%/SGMS_Update/
生成 apply_update.bat
subprocess.Popen(bat) → sys.exit(0)

→ 点"稍后"：

关闭弹窗，标记"待重启"，下次启动时检测并执行替换

纯文本
### 替换机制
- bat 脚本：等待2秒 → copy /Y 新exe到目标路径 → del 临时文件 → del update.json → start 新exe
- 替换前备份当前 exe 到 `%LOCALAPPDATA%/SGMS/backup_exe/`
- 不热替换（不尝试覆盖正在运行的 exe）

---

## 七、Excel 导入修复（紧急）

- `core/parser.py`：`pd.read_excel(path, engine='openpyxl')` 显式指定
- `requirements.txt`：加 `openpyxl>=3.1.0`
- 打包脚本：`--hidden-import=openpyxl`
- 错误捕获：友好弹窗，不再显示原始 pip install 指令

---

## 八、AI 模块下载器（骨架+预留）

- `core/ai/model_downloader.py`：GGUF 模型从网盘下载到 `models/`
- 设置页 AI 区域：模型列表+下载进度
- `core/ai/local_assistant.py`：占位，不实现推理

---

## 九、禁止事项

（继承 v1.7 全部，新增）
- 版本号不得硬编码在 UI 文件里
- 不得用 requests/httpx 下载更新
- 不得热替换正在运行的 exe