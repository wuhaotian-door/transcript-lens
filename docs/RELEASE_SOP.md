# 成绩单透镜 · 发版 SOP（分片发布操作手册）

> 面向**维护者**。这是一份"照着敲一遍"的手册：每一步给命令、
> 给**做完应该看到什么**。踩过的坑用 ★ 标出，并且写清**为什么**。
>
> 对应版本：v1.9　　最后更新：2026-10-11
> 配套阅读：`docs/DISTRIBUTION.md`（分发架构 / 更新器怎么工作）、
> `docs/UPDATE_GUIDE.md`（给辅导员看的）、`docs/DEV_SUMMARY.md`（规格真源）。

---

## 一、这份 SOP 管什么

### 1.1 适用范围

| 场景 | 走这份 SOP？ |
|---|---|
| 发一版主程序（onefile exe 挂 Gitee 发行版附件） | ✅ **就是它** |
| 只改 `update/ai_models.json` 里的模型直链 | 走第五节 |
| 改发行版说明文字、回滚到上一版 | 第四 / 第 2.12 步 |
| 把 exe 传到网盘、走"发行版正文"老路线 | ❌ 走 `docs/DISTRIBUTION.md` 第二节 |

**为什么会有这份 SOP**：exe 是 **240MB 量级**，而 Gitee 社区版**单个附件上限 100MB**
（GVP 项目 200MB、单仓库附件总容量 1G）—— **根本传不上去**。所以从 v1.8.1 起
把 exe 顺序切成 3 片，分别当附件上传，程序端逐片校验后拼回来。
分片把"一次上传"变成了"切片 → 上传 → 填清单 → 复查直链"四件事，
每一件都有踩过的坑，所以单独写一份手册。

### 1.2 谁是维护者、需要什么权限

* **Owner / 维护者**：本仓库的写权限（能推 `master`、能建发行版和传附件）。
  发版动作**不要**外包给没有写权限的人 —— 中途要推提交、改标签。
* 需要能读 `docs/DEV_SUMMARY.md` 与 `AGENTS.md` §26（自动更新器四个「绝不」），
  本 SOP 的每条约束都和它们对齐。
* 打出来的包要在一台 **Windows 机器**上验证（`build_exe.ps1` 是 PowerShell）。

### 1.3 令牌（token）怎么给

上传附件、改发行版说明都要 Gitee 的 **私人令牌**。规矩只有三条：

1. **只从环境变量 `GITEE_TOKEN` 读**，命令里永远写 `$env:GITEE_TOKEN`，
   不要写令牌本身；
2. **用完即撤销** —— 发版结束去 Gitee「设置 → 私人令牌」删掉那一枚；
3. ★ **任何情况下不许写进文件、仓库、日志或文档**。
   本仓库只记录"令牌经环境变量传入、用完即撤销"这一句。

```powershell
# 只在当前这个 PowerShell 会话里存在；关掉窗口就没了
$env:GITEE_TOKEN = '<粘贴令牌>'

# 确认它进去了（只会打印长度，不打印内容）
"token 长度 = $($env:GITEE_TOKEN.Length)"
```

> ★ 别把令牌写进 `.env`、`.ps1`、笔记或 issue —— 那等于把写权限公开挂在网上。

### 1.4 手边要有

| 东西 | 干嘛用 |
|---|---|
| `C:\Users\ROG\anaconda3\python.exe`（或任何装了 `pandas` / `PySide6` 的 Python） | 跑自检、切片、核对哈希 |
| `curl.exe` | 查 release_id、上传附件、HEAD 复查直链 |
| `certutil` | 算 sha256（`tools/split_release.py` 自己也会算一遍） |
| 一份**上一次**发版的 `update/manifest.json` | 回滚时的底座 |

---

## 二、分片发布全流程

下面每一步都是**顺序依赖**的：前一步的输出就是后一步的输入。
每步末尾的「✅ 应该看到」是可以逐条核对的，看不到就别往下走。

### 第 0 步：开工前的准备

```powershell
Set-Location 'C:\Users\ROG\Desktop\school\transcript-lens\transcript-lens'
git status --short

# 老 exe 还在跑的话会锁住 dist\transcript-lens.exe，打包必炸
Get-Process -Name 'transcript-lens*' | Stop-Process -Force
Start-Sleep -Seconds 3
```

> ★ `dist\`、`build\`、`*.spec` 都在 `.gitignore` 里，**绝不要 `git add` 它们**
> —— `build_exe.ps1` 最后一步会检查这一点。
> ★ 杀进程要**按进程名**（`-Name`），不要按 PID：onefile 的 exe 会再起一个
> 子进程，只杀父进程的话文件还锁着。

✅ 应该看到：`git status --short` 只列你这次要提交的文件；没有 `[WinError 5]`
级别的文件占用。

### 第 1 步：改版本号（只改一行）+ 补迁移链一跳

```python
# core/version.py
CURRENT_VERSION = "1.9"
```

```python
# core/migrator.py
def _lift_1_8_to_1_9(data: dict[str, Any]) -> dict[str, Any]:
    """1.8 → 1.9：没有数据结构变化就原样返回。"""
    return data


_STEPS = (
    ("1.0", "1.6", _lift_1_0_to_1_6),
    ("1.6", "1.7", _lift_1_6_to_1_7),
    ("1.7", "1.8", _lift_1_7_to_1_8),
    ("1.8", "1.9", _lift_1_8_to_1_9),      # ← 新增这一跳
)
```

> ★ **版本号只改 `core/version.py` 这一行。** 界面、更新检查、迁移、开屏
> 全都从它读；`--selfcheck` 有一条闸门会用 `ast` 扫 `ui/`，
> 出现写死的版本号字面量直接 FAIL。
> ★ **补一跳别省**，哪怕什么数据结构都没改，也写个 `return data` 的空函数。
> 不补的话老用户启动当场报
> `MigrationError: 没有从 v1.8 升级的路径（已知：1.0, 1.6, 1.7, 1.8）`
> —— v1.7 → v1.8 那次实测踩到，是 `--selfcheck` 的迁移检查拦下来的。

✅ 应该看到：`core/version.py` 里 `CURRENT_VERSION = "1.9"`；
`core/migrator.py` 的 `_STEPS` 末项指向新版本。

### 第 2 步：跑测试

```powershell
& 'C:\Users\ROG\anaconda3\python.exe' -m pytest -q
```

✅ 应该看到：`passed`，0 failed。**有红就别打包** —— 打包只是把坏版本装进 exe。

### 第 3 步：源码自检

```powershell
& 'C:\Users\ROG\anaconda3\python.exe' main.py --selfcheck
"exit=$LASTEXITCODE"
```

✅ 应该看到：末行 `RESULT: ALL OK`，退出码 **0**。
报告同时落在 `%TEMP%\transcript-lens-selfcheck.txt`（窗口版 exe 没有 stdout，
所以它总是写文件；用记事本或 `read` 工具看，**不要**用文本管道读改写它）。

★ 这一步里有**文档闸门**：`docs/` 下只许有白名单里的 4 个文件
（总纲 / 辅导员手册 / 发版手册 / **本 SOP**），且任何 `.py` / `.ps1` / `.md` / `.json`
都不许提到已删除的 5 份旧文档名。所以：

* **新建本文件时它是白名单内的**，不会报"多了白名单之外的文件"；
* 但**本文件一旦被删掉**，自检会立刻报 `FAIL docs/ 少了该有的文件：RELEASE_SOP.md`；
* 写文档时**不要**把旧文档名当成历史引用抄进来（总纲第二十节的对照表除外）。

### 第 4 步：打包（onefile + onedir）

```powershell
.\build_exe.ps1 -Both
```

`-Both` 会依次打两个目标，并且**打完自动跑两道闸门**：

| 目标 | 产物 | 用途 |
|---|---|---|
| `release` | `dist\transcript-lens.exe`（onefile，~240MB） | **给辅导员的**，也**只有它会发布** |
| `debug` | `dist\transcript-lens-debug\`（onedir，~680MB） | 自己排查用，**不发布** |

打包脚本内部的三道检查（任何一道不过就直接中止）：

1. 打包前：源码 `main.py --selfcheck`；
2. 打包后：`dist\transcript-lens.exe --selfcheck`；
3. 打包后：`dist\transcript-lens.exe --bootcheck`（真实启动流程：开屏 → 欢迎页
   → 主界面，**退出码本身就是断言**，超时 180s 也算失败）。

✅ 应该看到：脚本最后打印两个目标都通过，
`dist\transcript-lens.exe` 存在且体积在 **240MB** 量级
（v1.8.1 实测 **240099567 字节**）。
报告分别落在 `%TEMP%\transcript-lens-selfcheck.txt` 与
`%TEMP%\transcript-lens-bootcheck.txt`。

> ★ **重打包一定会换 sha256**：PyInstaller 的产物里带时间戳。
> 所以每次发版都要**重新切、重新算**，不许抄上一版的值。
> ★ 新打出来的 exe **第一次启动会被 Defender 全盘扫描**，冷启动慢几倍是正常的，
> 别拿第一次启动的耗时当性能基准。

### 第 5 步：算整体 sha256

```powershell
certutil -hashfile dist\transcript-lens.exe SHA256
& 'C:\Users\ROG\anaconda3\python.exe' -c "import hashlib,pathlib; p=pathlib.Path(r'dist\transcript-lens.exe'); print(p.stat().st_size); print(hashlib.sha256(p.read_bytes()).hexdigest())"
```

✅ 应该看到：两条命令给出**同一个 64 位十六进制字符串**（小写），
以及 exe 的字节数。
v1.8.1 实测：`240099567` 字节，
`bc58221ee32644d1b43bc4c0abbba5bc062d3ea9d61f8e32cc7eeb17a215e211`。

这两个值后面要原样填进清单的 `app.size` / `app.sha256`（它们是**整个 exe** 的，
不是某一片的）。

### 第 6 步：切片

```powershell
& 'C:\Users\ROG\anaconda3\python.exe' tools\split_release.py dist\transcript-lens.exe `
    --parts 3 --tag 1.9 --version 1.9 --notes "这一版改了什么" --out release
```

工具做三件事：

1. 按 `--parts` **顺序**切成几片，落在 `--out` 目录里，
   文件名 `transcript-lens-v<版本>.exe.partN`（N 从 1 开始，**没有前导零**）；
2. **逐片量体积** —— 超过附件上限（`core.updater.ATTACHMENT_LIMIT_BYTES`，
   唯一真源，= 100MB）就当场让你多切几片，而不是"传上去再说"；
3. 打印每片的 `size` / `sha256`，并打印**可直接粘贴进 `update/manifest.json`
   的 `app` 段**。

✅ 应该看到：源文件字节数 + 整体 sha256，然后每片一行
`第 N 片 <片名>: <字节数>  <sha256>`，最后一段 JSON。

v1.8.1 实测（3 片 × `80033189` 字节 = 76.3 MB）：

| 片 | sha256 |
|---|---|
| `transcript-lens-v1.8.1.exe.part1` | `d72048d670fed15a1ab1253057e04a9719eddfbd09d9728f138962adb6128d16` |
| `transcript-lens-v1.8.1.exe.part2` | `f32dd312b498a85827a4186687a495bfc46602802cdc46d476e256113b65c326` |
| `transcript-lens-v1.8.1.exe.part3` | `abea2b2a3fa60cffae5fdff72f2bf27ad20175b4a5c1ea7389f0c12fc1ce3059` |

> ★ `--tag` 是**直链里的标签名**，`--version` 是程序版本号。本仓库的标签
> **不带 `v` 前缀**（是 `1.8.1`，不是 `v1.8.1`），直链写成 `v1.8.1` 一定 404。
> ★ `split_release.py` **不联网、不上传**，只切本地文件。

### 第 7 步：上传三片当发行版附件

**7.1 先查 release_id**（发版前必须**先建好发行版**：标签名 = 第 6 步的 `--tag`）：

```powershell
curl.exe -s -H "Authorization: token $env:GITEE_TOKEN" `
  "https://gitee.com/api/v5/repos/wuhaotiansystem/transcript-lens/releases" `
  | & 'C:\Users\ROG\anaconda3\python.exe' -c "import json,sys; [print(r['id'], r['tag_name']) for r in json.load(sys.stdin)]"
```

✅ 应该看到：每行一个 `release_id` + 标签名，找到刚建的那条的 id
（v1.8.1 那次是 **1195636**）。

★ **发行版页面上自动生成的 `<标签>.zip` / `<标签>.tar.gz` 是源码包，不是 exe 附件。**
曾经就是被这一点误导：清单里写了一条永远 404 的 exe 直链。

**7.2 传附件**（multipart 流式，不占内存；`access_token` 走请求头，**不落命令行**）：

```powershell
$RID = 1195636        # ← 换成刚查到的 id
foreach ($n in 1..3) {
    $p = "release\transcript-lens-v1.9.exe.part$n"
    curl.exe -s -o "upload_p$n.json" -w "part$n HTTP %{http_code}`n" `
      -H "Authorization: token $env:GITEE_TOKEN" `
      -F "file=@$p;filename=transcript-lens-v1.9.exe.part$n" `
      "https://gitee.com/api/v5/repos/wuhaotiansystem/transcript-lens/releases/$RID/attach_files"
}
```

✅ 应该看到：三行 `partN HTTP 201`，并且 `upload_p1.json` 里有 `id`（附件 id）
和 `download` 字段。v1.8.1 那次三条附件 id 是 **3358116 / 3358129 / 3358151**。

> ★ 单附件超 100MB 会被 Gitee 直接拒（240MB 的 exe 整包传一定会失败）——
> 这正是切片的原因。若某片被拒，回第 6 步加 `--parts`。
> ★ 令牌只从 `$env:GITEE_TOKEN` 读，**用完即撤销**（见 §1.3）。

**7.3 三条直链都要 HEAD 复查**

```powershell
foreach ($n in 1..3) {
    curl.exe -s -I -o NUL -w "part$n HTTP %{http_code}  size %{size_download}  len %{header_json}`n" `
      "https://gitee.com/wuhaotiansystem/transcript-lens/releases/download/1.9/transcript-lens-v1.9.exe.part$n"
}
```

✅ 应该看到：三条都是 **HTTP 200**，`Content-Length` 与本地片的字节数一致
（v1.8.1 实测三条各 80033189 字节）。

★ **Gitee 附件直链不认 `Range`**：带 `Range: bytes=0-0` 去请求，回的是
**HTTP 200 + 完整的 76MB**（不是 206）。后果有两条，都要知道：

* 更新器的**片内**断点续传在这种托管上**不生效** —— 它会按既定逻辑
  "服务器忽略 Range 就从 0 重下"（这条判据当初就是为这种服务器写的）；
* **整片级续传仍然有效**：已经校验过的片缓存在 `<目标>.parts\partNN`，
  再点一次「开始下载」**连请求都不发**。

### 第 8 步：填 `update/manifest.json`

把第 6 步打印的 `app` 段（补上 `version` / `published`）贴成**下面这个形状**：

```json
{
  "schema": 1,
  "version": "1.9",
  "published": "2026-10-11",
  "app": {
    "url": "",
    "size": 240099567,
    "sha256": "<整个 exe 的 sha256>",
    "notes": "这一版改了什么",
    "parts": [
      {"url": "https://gitee.com/wuhaotiansystem/transcript-lens/releases/download/1.9/transcript-lens-v1.9.exe.part1",
       "size": 80033189, "sha256": "<第 1 片的 sha256>"},
      {"url": "https://gitee.com/wuhaotiansystem/transcript-lens/releases/download/1.9/transcript-lens-v1.9.exe.part2",
       "size": 80033189, "sha256": "<第 2 片的 sha256>"},
      {"url": "https://gitee.com/wuhaotiansystem/transcript-lens/releases/download/1.9/transcript-lens-v1.9.exe.part3",
       "size": 80033189, "sha256": "<第 3 片的 sha256>"}
    ]
  }
}
```

**字段含义**（`core.updater.parse_manifest()` 认的就是这个形状）：

| 字段 | 含义 |
|---|---|
| `app.size` / `app.sha256` | **整个 exe** 的字节数与哈希（单文件 / 分片两种模式含义相同） |
| `app.parts[]` | 顺序切好的每一片：`url` / `size` / `sha256`（每片自己的） |
| `app.url` | 分片模式下**留空**（只有单文件模式才填） |
| `app.notes` / `published` | 发版说明 / 发布日期 |

★ **坑一：清单的顶层必须就是 `app`。** 写成 `{"files": {"app": {...}}}` 这种嵌套，
`parse_manifest()` 直接给 `(None, '清单里没有 app 段')` ——
**清单路线静默失效**、退回老的"发行版正文"路线，界面上**完全看不出来**，
你填的 url / size / sha256 一个都没生效。

★ **坑二：`url` 里的标签名照抄真实标签**（`1.9`，不带 `v`）。

★ **坑三：半填也不许。** `app` 缺 `url` / `size` / `sha256` 任一项
（分片模式缺 `app.size` / `app.sha256` / 任何一片的完整字段），
`ManifestApp.usable` 就是 `False` → 程序**宁可退回老路线，也不拿没校验的直链
去替换用户的程序**。判据在仓库里：`tests/test_manifest.py::test_仓库清单只允许空模板或完全填好`。

### 第 9 步：提交推送

```powershell
git add update/manifest.json core/version.py core/migrator.py docs/ 
git status --short          # 再确认一次 dist\ / build\ 没进去
git commit -m "release(v1.9): 分片发布清单 + 版本号 + 迁移跳"
git push origin master
```

程序读的是 **Gitee raw 的 master**（`core.updater.GITEE_RAW_MANIFEST`
= `https://gitee.com/wuhaotiansystem/transcript-lens/raw/master/update/manifest.json`），
**不推 master 就等于没发**。

✅ 应该看到：`git push` 成功，`git status --short` 干净。

### 第 10 步：把版本标签挪到发版提交上

```powershell
git tag -f 1.9 <发版提交的短哈希>
git push -f origin 1.9
```

★ **为什么必须挪**：发行版里那两个自动生成的源码包（`1.9.zip` / `1.9.tar.gz`）
是**按标签指向的提交**打包的。标签还停在旧提交上，用户下到的源码包就**比程序旧**
—— 而这件事**不报错**，只是悄悄给出错的东西。v1.8.1 那次就是强推把标签挪到位。

> 只想看提交号：`git rev-parse --short HEAD`。

✅ 应该看到：`git push -f origin 1.9` 报 `forced update`；
`git ls-remote --tags origin 1.9` 的哈希与发版提交一致。

### 第 11 步：端到端验收（**必须用真实直链**）

这一步**不能省**，也不能用本地文件冒名顶替 —— 它验的是"用户那条路真的通"。
写一个**一次性脚本**（放 `%TEMP%`，不进仓库），依次做五件事：

1. 从**三条真实直链**（第 7.3 步那三条）下载；
2. `core.updater.download_parts()` **逐片校验**；
3. 按清单顺序**拼接**；
4. 核对**整体** sha256 == 清单里的 `app.sha256`；
5. 让**拼出来的 exe 自己跑 `--selfcheck`**，退出码必须是 0。

```powershell
& 'C:\Users\ROG\anaconda3\python.exe' "$env:TEMP\tl_verify_release.py"
```

✅ 应该看到：三片校验通过、整体哈希与清单一致、拼出来的 exe
打印 `RESULT: ALL OK` 且退出码 0。

v1.8.1 实测：整体 sha256
`bc58221ee32644d1b43bc4c0abbba5bc062d3ea9d61f8e32cc7eeb17a215e211` 与清单一致，
拼出来的 exe `--selfcheck` 退出码 0（`RESULT: ALL OK`）。
**229MB 用时 117 秒（≈2.0 MB/s）** —— 验收要占几分钟，别以为卡住了。

**给用户的兜底写法**（发行版说明里可以直接贴）：

```bat
copy /b part1+part2+part3 transcript-lens.exe
```

### 第 12 步：改发行版说明

```powershell
$RID = 1195636
@'
{"tag_name": "1.9", "body": "## v1.9 更新内容\n\n- …\n\n### 下载\n\n见 update/manifest.json；分片附件 3 个，下载后校验 sha256 再运行。"}
'@ | Set-Content -Encoding UTF8 "$env:TEMP\rel_body.json"

curl.exe -s -o NUL -w "PATCH HTTP %{http_code}\n" -X PATCH `
  -H "Authorization: token $env:GITEE_TOKEN" `
  -H "Content-Type: application/json" `
  --data-binary "@$env:TEMP\rel_body.json" `
  "https://gitee.com/api/v5/repos/wuhaotiansystem/transcript-lens/releases/$RID"
```

★ **`PATCH` 必须同时带 `tag_name`**，否则 Gitee 直接回 **400** ——
只改 `body` 不带标签名是不行的。

✅ 应该看到：`PATCH HTTP 200`，发行版页面上说明已经更新。

### 第 13 步：收尾

```powershell
Remove-Item "$env:TEMP\rel_body.json","upload_p1.json","upload_p2.json","upload_p3.json" -ErrorAction SilentlyContinue
Remove-Item Env:\GITEE_TOKEN      # 会话里也别留着
```

然后去 Gitee「设置 → 私人令牌」**撤销这一枚令牌**（§1.3 第 2 条）。

---

## 三、验收清单（逐条可判定）

发完版逐条对一遍，**每一条都能当场看到结果**：

- [ ] `core/version.py` 的 `CURRENT_VERSION` 已是新版本
- [ ] `core/migrator.py` 的 `_STEPS` 补了新的一跳（哪怕是空函数）
- [ ] `pytest -q` 全绿
- [ ] `main.py --selfcheck` 退出码 0、末行 `RESULT: ALL OK`
- [ ] 自检报告里 `docs/ 白名单` 是 **4 个文件**（含本节这份 SOP）
- [ ] `build_exe.ps1 -Both` 两个目标都过 selfcheck + bootcheck
- [ ] `dist\transcript-lens.exe` 的字节数与 sha256 已记录
- [ ] `tools\split_release.py` 每片都 < 100MB（工具自己会拦）
- [ ] 每片的 `size` / `sha256` 已从工具输出抄进清单（不是手抄旧值）
- [ ] 三条附件都回 **HTTP 201**，附件 id 已记下
- [ ] 三条**直链 HEAD** 都是 200 且 `Content-Length` == 本地片体积
- [ ] 清单**顶层就是 `app`**（不是 `files.app`），且 `app.url` 留空
- [ ] 清单里的标签名 = 真实标签名（**不带 `v`**）
- [ ] `app.size` == 切片前 exe 的字节数；`sum(parts.size)` == `app.size`
- [ ] 清单已推到 **master**（程序读 raw master）
- [ ] 版本标签已挪到**发版提交**上（`git push -f origin <标签>`）
- [ ] 端到端验收过：真实直链下载 → 逐片校验 → 拼接 → 整体 sha256 → 拼出来的
      exe `--selfcheck` 退出码 0
- [ ] 发行版说明已更新（`PATCH` 带了 `tag_name`）
- [ ] 令牌已撤销，临时文件已清理
- [ ] 拿一台**干净机器/新 Windows 用户**试一遍：装上一版（v1.8.x）能否按说明
      手动拿到包（见下表"第一次升级"那一行）

---

## 四、回滚方案

### 4.1 怎么回滚

程序**只读清单**。所以回滚 = **把清单改回上一版的值**，然后提交推送：

1. `git log -- update/manifest.json` 找到上一版的提交；
2. 把 `app.size` / `app.sha256` / `app.notes` / `app.parts` 整体换回上一版
   （**三条 part 一起换**，不要只换其中一条）；
3. 提交并 `git push origin master`。

客户端下一次点「检查更新」读到的就是上一版 —— **改清单即完成回滚**，
不需要动服务器上的任何文件、也不需要重新上传。

### 4.2 ★ 为什么不许删附件

* 清单里的 `app.size` / `app.sha256` / `app.parts[*]` 必须与附件**逐字节对应**；
* 删掉附件 → 直链当场 **404** → **还在用旧清单的客户端全部下载失败**
  （它们不知道你已经回滚了，只看到链接坏了）；
* 附件删了也不一定能重传成同名文件（附件 id 会变，直链倒是不变，
  但你没传回去之前，所有老清单都是坏的）。

**所以：宁可留着旧附件占配额（单仓库附件总容量 1G），也不要删。**
要腾地方，先想清楚哪些清单还指着它。

### 4.3 回滚之后还要做什么

* 如果回滚是因为**新包本身坏了**（校验不过、跑不起来），
  那就把坏的那一版**整个作废**：附件留着（见 4.2），但清单里的
  `app.notes` 写清楚"这版已撤回，请用 <上一版>"；
* 发行版说明同步改（`PATCH` 记得带 `tag_name`）；
* 在提交信息里写清**为什么回滚** —— 半年后有人会需要这句话。

---

## 五、AI 模型清单（`update/ai_models.json`）的发布流程

**模型清单和主程序是两条独立的线**：模型走直链（当前是 ModelScope），
不进附件、也不走分片。

1. 改 `update/ai_models.json`（字段说明在文件自己的 `_doc` 里：
   `id` / `label` / `group` / `file` / `url` / `size` / `sha256` / `minRamGb` /
   `defaultCheck` / `note`）；
2. **提交推送 `master`** —— 程序读的是 Gitee raw 的 master
   （`core/ai/model_registry.GITEE_RAW_MODELS`），不推等于没改；
3. 打开程序 → **设置 → 模型中心**，确认列表**能刷出来**、新条目在、能勾选。

★ **清单读不到只是警告，界面照开** —— 模型中心会给"复制链接手动下载"的兜底，
不会把用户堵在门外。所以"刷不出新条目"时先确认推送成功、再确认 raw 链接
（浏览器直接开一次那个 URL）能返回 JSON。

★ **`size` / `sha256` 必须是真值**，别估：size 差超过容忍度会被判成
"下回来的不是这个文件"，sha256 对不上**直接拒绝落盘**。

★ 内存闸门（`minRamGb`）写在清单里，**机器内存不够的行整行不可选**
—— 这是有意的，不要为了"让大家都能下"把它调小。

> ⚠️ 这份文件可能与其它任务并行编辑。**改它之前先 `git status`**，
> 确认没有别人正在改；发版流程本身**不需要动它**。

---

## 六、故障对照表（现象 → 根因 → 怎么办）

| 现象 | 根因 | 怎么办 |
|---|---|---|
| 清单填好了，但程序仍走老路线、行为没变 | 清单写成了 `{"files": {"app": {...}}}` 这种嵌套 —— `parse_manifest()` 只认**顶层 `app`**，返回 `(None, '清单里没有 app 段')`，**清单路线静默失效**，界面看不出任何异常 | 把 `app` 提到顶层；改完跑 `tests/test_manifest.py` 确认 |
| 直链 404 | URL 里的标签名写成了 `v1.8.1`，而真实标签是 `1.8.1`（**不带 `v`**） | 照抄 `git tag` 里的真实标签名；三条 URL 都核对一遍 |
| 附件的直链指向一个源码包 | 发行版页面上自动生成的 `<标签>.zip` / `<标签>.tar.gz` 是 Gitee 的**源码包**，不是 exe 附件 | 用**附件**的直链（`/releases/download/<标签>/<片名>`）；先传附件再填清单 |
| 传附件报错 / 上传中断 | onefile exe 是 240MB，**社区版单附件上限 100MB**（GVP 200MB） | 切片：`tools\split_release.py --parts 3`；某片仍超限就加片数 |
| 下载中断后"续传"却从 0 重下 | **Gitee 附件直链不认 `Range`**：带 `Range: bytes=0-0` 仍回 200 + 完整 76MB（不是 206） | 片内续传在这里不生效是**已知行为**，不是 bug；整片级续传仍然有效（已校验的片不发请求）。要真续传就换支持 Range 的托管 |
| 用户下到的**源码包比程序旧** | 标签没挪到发版提交上（标签还指着旧提交） | `git tag -f <标签> <发版提交>` + `git push -f origin <标签>` |
| **第一次**升级到分片版本失败 / 提示缺直链 | 老客户端（v1.8）**不认 `app.parts`** → 判「缺 `app.url`」→ **安全退回**"发行版正文"老路线（不会下错东西，但分片下不了） | **第一次升级必须手动拿包**：把三片下下来按顺序拼，或直接用拼好的 exe（`copy /b part1+part2+part3 transcript-lens.exe`）。这一步要写进发行版说明 |
| `--bootcheck` 超时 180s | 启动流程卡住（GUI 起不来） | 看 `%TEMP%\transcript-lens-bootcheck.txt`；源码版跑一次 `main.py --bootcheck` 定位 |
| 打包报 `PermissionError: [WinError 5] ... dist\transcript-lens.exe` | 上一次的 exe 还在跑（onefile 有子进程），文件被锁 | `Get-Process -Name 'transcript-lens*' \| Stop-Process -Force`，`Start-Sleep -Seconds 3`，重打 |
| 自检报 `FAIL docs/ 少了该有的文件：RELEASE_SOP.md` | 白名单要求 `docs/` 下这 4 个文件都在，本文件被删/改名了 | 恢复 `docs/RELEASE_SOP.md`（或由 Owner 改白名单） |
| 自检报 `docs/ 下多了白名单之外的文件：X` | 又往 `docs/` 里堆了一份文档 | 新知识写进 `docs/DEV_SUMMARY.md` 或 `AGENTS.md`；不要新建文档 |
| `PATCH` 改发行版说明回 400 | 请求体里**没带 `tag_name`** | 带上 `tag_name`（见第 12 步） |
| 回滚后老用户下载失败 | 回滚时**删了附件** → 旧清单里的直链 404 | 不许删附件；回滚只改清单（§4.2） |
| 重新打包后哈希对不上清单 | PyInstaller 产物带时间戳，重打包 = 换哈希 | 每次发版都重新切片、重新算，不许抄旧值 |

---

## 七、相关代码与工具

| 环节 | 在哪 |
|---|---|
| 版本号真源 | `core/version.py` 的 `CURRENT_VERSION` |
| 迁移链 | `core/migrator.py` 的 `_STEPS` |
| 清单解析 / 拉取 | `core/updater.parse_manifest()` / `fetch_manifest()` |
| 分片下载与拼接 | `core.updater.download_parts()` |
| 附件上限常量（唯一真源） | `core.updater.ATTACHMENT_LIMIT_BYTES`（100MB） |
| 切片工具（不联网、不上传） | `tools/split_release.py` |
| 打包 + 自动自检 / 启动检查 | `build_exe.ps1`（`-Both` = onefile + onedir） |
| 自检报告（窗口版也写文件） | `%TEMP%\transcript-lens-selfcheck.txt` |
| 启动检查报告 | `%TEMP%\transcript-lens-bootcheck.txt` |
| 文档白名单闸门 | `main._docs_gate_report()`（`--selfcheck` 里跑） |
| 分片相关判据 | `tests/test_updater_parts.py`、`tests/test_manifest.py` |
