<#
.SYNOPSIS
    把图形界面入口 (main.py) 打包成 exe。

.DESCRIPTION
    打包产物全部落在 dist/ 与 build/ 里，两者和 *.spec 都已被 .gitignore 拦下
    （红线 11 的延伸：exe 属于构建产物，绝不允许 git add 进仓库）。

    两个目标：

    * ``debug``   —— onedir + 控制台。**开发/自测用**：启动快、能看见 traceback，
                     出问题时改一行重打只要几十秒。
    * ``release`` —— onefile + 窗口版。**给别人试用的**：单个 exe，双击就跑，
                     代价是每次启动都要先把自己解包到 %TEMP%\_MEIxxxx（慢几十秒）。

    打完自动跑一次 ``--selfcheck``（不弹窗，只把"假数据 → 解析 → 算分 → 锚点校验"
    跑一遍），失败就报错退出。**不要跳过这一步** —— pandas / openpyxl 这类库有一堆
    延迟导入，界面能开、一点"解析并核对"才 ModuleNotFoundError 是打包最常见的坑。

.EXAMPLE
    powershell -File build_exe.ps1                  # 打 debug 版并自检
    powershell -File build_exe.ps1 -Target release  # 打单文件窗口版并自检
    powershell -File build_exe.ps1 -Both            # 两个都打

    # ★ 单文件窗口版一旦启动就崩，你是看不到任何报错的：
    #   --windowed 的 exe 属于 GUI 子系统，没有 stdout/stderr，
    #   PyInstaller 的启动错误只会弹一个标题为 "Error" 的原生对话框
    #   （用 Start-Process 调用时，脚本会一直卡在那个看不见的弹窗上）。
    #   排查时用这个：同样的单文件包，但带控制台，错误直接打在屏幕上。
    powershell -File build_exe.ps1 -Target release -ShowConsole

.NOTES
    ★ 已知环境限制：如果当前 shell 跑在受限的文件沙箱里（例如只有工作区可写），
      **单文件 exe 在本机跑不起来** —— PyInstaller 的 onefile 启动器要先把自己
      解包到临时目录，那一步会被拦掉，报
      ``[PYI-xxxx:ERROR] Failed to create parent directory structure``
      或 ``Failed to extract``，然后静默卡住。这是环境限制，不是打包配置的问题：
      同样条件下一个 8MB 的 hello-world 单文件 exe 也会以同样的方式失败。
      沙箱内请用 ``-Target debug``（onedir，不需要自解包，可完整验证）；
      单文件包请在沙箱外双击验证，或加 ``-SkipSelfCheck`` 只产出不验证。

      ★ 实测补充（2026-10-30，本机）：这条限制**不只发生在沙箱里**。用计划任务以当前用户
      身份运行单文件 exe，仍报 ``[PYI-xxxx:ERROR] Could not create temporary directory!``；
      连一个 8MB 的 hello-world 单文件 exe 也一样（同样的报错、同样的退出码 -1）。
      把 TEMP 指到**本项目目录树内**（如 ``dist\_tmp``）就正常（自检 ALL OK，退出码 0），
      指到 %TEMP%、桌面、用户目录根一律失败。结论：**本机安全软件/沙箱策略在拦**，
      不是打包配置问题。

      ★★ 根治办法（2026-11-07 实测）：给单文件包加 ``--runtime-tmpdir``，让启动器
      **解到 exe 自己所在的目录**，根本不碰 %TEMP%。验证方式：同一个 hello-world
      单文件包，默认版本在 %TEMP% 和用户目录根都报 ``Could not create temporary
      directory!``，加了 ``--runtime-tmpdir "."`` 之后**两处都是 exit 0**。
      所以 release 目标固定带 ``--runtime-tmpdir $RuntimeTmpDir``（默认 ``"."``）。

      ⚠️ 两个必须知道的边界：
        1. ``--runtime-tmpdir`` 里的相对路径是相对**启动时的工作目录（CWD）**，
           不是 exe 所在目录。双击启动时 CWD 就是 exe 所在目录（Explorer 的行为），
           所以正常双击没问题；但从别的目录用命令行启动、或快捷方式把"起始位置"
           改到别的目录时，解包会落在那个目录里 —— 那里不可写就会失败。
           交互式排查用 ``dist\launch.bat``（它顺手把 TEMP 指到 dist\_tmp）。
        2. 路径**不支持环境变量**（写 ``%TEMP%`` 会被当成字面量），
           而且上级目录必须**已经存在** —— 指 ``_tmp`` 这种不存在的子目录会直接失败。

#>
param(
    [ValidateSet('debug', 'release')]
    [string]$Target = 'debug',
    [switch]$Both,
    #: release 也带控制台（产物叫 transcript-lens-console.exe），用来查启动崩溃
    [switch]$ShowConsole,
    [switch]$SkipSelfCheck,
    #: 单文件版的解包根目录。**默认 "."：解到 exe 自己所在目录**（见文件头 NOTES）
    [string]$RuntimeTmpDir = '.'
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ★ 还得让**子进程**也说 UTF-8。
#   `[Console]::OutputEncoding` 只管 PowerShell 自己这一侧的编码；被管道接住的
#   python 会去猜 stdout 的编码，猜到的往往是系统 ANSI（本机 GBK）——
#   于是它写 UTF-8 的字节、我们按 UTF-8 解，中文全变乱码
#   （打包日志里那段"源码自检"就是这么糊掉的，而报告文件本身是好的）。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$Root = $PSScriptRoot
Set-Location $Root

# --- 跑外部命令的小工具 -----------------------------------------------------
#
# ★ 坑：PyInstaller 把日志写到 **stderr**，而在 $ErrorActionPreference='Stop' 下
#   PowerShell 会把原生命令的 stderr 当成终止性错误，脚本直接死在 `python ...` 这一行，
#   exe 明明打了一半却被判失败。所以这里显式把 stderr 当普通文本收下来。
function Invoke-Native {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandLine)

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $CommandLine[0] @($CommandLine[1..($CommandLine.Count - 1)]) 2>&1 |
            ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) { Write-Host $_.ToString() }
                else { Write-Host $_ }
            }
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

# --- 打包前自检：源码都不跑通，打出来的 exe 一定更跑不通 ---------------------
Write-Host '=== 1/4 打包前：源码自检 ===' -ForegroundColor Cyan
if ((Invoke-Native python -B main.py --selfcheck) -ne 0) {
    throw '源码自检失败，先修好再打包'
}

# --- 共享参数 ---------------------------------------------------------------
$Common = @(
    '-m', 'PyInstaller',
    '--noconfirm',
    '--clean',
    '--log-level', 'WARN',

    # 假数据必须进包：界面上的两个演示按钮直接读它们（tests/ 本身不进包）
    '--add-data', 'tests/fixtures/fake_grades.xlsx;tests/fixtures',
    '--add-data', 'tests/fixtures/fake_grades_with_anchor.xlsx;tests/fixtures',

    # ★ 全局样式表也必须进包 —— 它同样**不是 .py**，是数据文件。
    #   漏了的话 exe 里窗口能开、界面全白（main.load_stylesheet 读不到会返回空串，
    #   自检里有一条专门查这个文件在不在）。
    '--add-data', 'ui/styles.qss;ui',

    # pandas 是延迟 import openpyxl 的，静态分析看不到，必须显式带上
    '--hidden-import', 'openpyxl',
    '--hidden-import', 'openpyxl.styles',
    '--hidden-import', 'openpyxl.worksheet._writer',
    '--hidden-import', 'pandas._libs.tslibs.base',

    # ★ v1.8：老格式 .xls 的读取引擎。和 openpyxl 一样是**延迟 import** ——
    #   pandas 在 ExcelFile(engine="xlrd") 那一刻才 import 它，
    #   PyInstaller 的静态分析看不见。漏了的话表现是最难查的那种：
    #   源码里跑得好好的、exe 里一导老格式成绩表就报"缺少读取组件"。
    '--hidden-import', 'xlrd',
    # openpyxl 带一堆数据文件（样式表、主题），--collect-all 一起收进来
    '--collect-all', 'openpyxl',

    # 让 PyInstaller 找得到 ui / core / ai 这三个顶层包
    '--paths', '.',

    # ★★ 这一段是**必须**的，漏了会出现最阴险的那种 bug：
    #
    # 旧版 ui/app.py 用 `__import__(f"ui.{item.module}", ...)` 动态加载页面，模块名是
    # 拼出来的，PyInstaller 的静态分析**看不见**。而 _instantiate() 遇到 ImportError
    # 会优雅降级成"本页尚未实现"占位页 —— 于是 exe 能正常打开、导航能点、
    # 每一页却都是空白占位，**一个报错都不弹**。
    #
    # 第一次打包就中了：ui.import_page / ui.result_page / ui.profile_page /
    # ui.auth_page / ui.settings_page / core.exporter / core.activation /
    # ai.local_template 全都没进包。是 `--selfcheck` 把这 8 个 ModuleNotFoundError
    # 揪出来的 —— 光看窗口一点异常都发现不了。
    #
    # 新版 UI 改成了**静态 import**（`from ui.page_academic import build_academic_page`），
    # 静态分析本来就看得见。这里的 --collect-submodules 保留，理由是同样的：
    # 这三个包以后每加一个文件都不用回来改打包脚本，天然不会腐烂。
    '--collect-submodules', 'ui',
    '--collect-submodules', 'core',
    '--collect-submodules', 'ai',

    # ★ PySide6 用 PyInstaller 自带的 hook 就够了（它知道 Qt 插件在哪）。
    #   曾经这里写的是 `--collect-all ttkbootstrap` —— 那是 tkinter 时代的遗留，
    #   换成 PySide6 之后整块删掉。
    #
    # ★★ 但有个**致命**的坑：下面 --exclude-module 那一串里原本排掉了 PySide6
    #    （因为当时界面是 tkinter，Qt 是废重量）。换成 PySide6 之后如果忘了删那条，
    #    PyInstaller 会把整个 Qt 排除掉，打出来的 exe 一启动就 ModuleNotFoundError,
    #    **而源码自检照样全绿** —— 因为自检跑的是源码不是包。已经删掉了。

    # 明确排掉的东西。这一长串不是洁癖，是**必须**的：
    #
    # Anaconda base 环境里装了 ~500 个包。PyInstaller 分析时会顺着 hook 链把
    # pandas → numpy → 各种"可选集成"一路捡进来，最后把 skimage / sklearn /
    # astropy / statsmodels / plotly / panel / dask / sphinx 整套科研全家桶都拖进
    # 分析图。第一次打包就是这么卡了 18 分钟以上、exe 也白胖几百 MB。
    #
    # ★ sympy / gmpy2 / mpmath 尤其要排：sympy 会拉 gmpy2，而 gmpy2 的 DLL 在这台
    #   机器的 conda 环境里起不来（STATUS_ENTRYPOINT_NOT_FOUND 0xc0000139）。
    #   分析阶段一 import 就崩，日志里刷 "Windows fatal exception"。
    #
    # ★ requests / urllib3 / httpx / aiohttp 也排掉是**故意的**：本项目铁律是
    #   "不联网"。把网络库整个从包里拿掉之后，"不会偷偷发请求"就不再靠自觉，
    #   而是**包里根本没有能发请求的东西**。自检里有一条专门验证这件事。

    # 测试 / 文档 / notebook
    '--exclude-module', 'pytest',
    '--exclude-module', 'tests',
    '--exclude-module', '_pytest',
    '--exclude-module', 'sphinx',
    '--exclude-module', 'sphinxcontrib',
    '--exclude-module', 'docutils',
    '--exclude-module', 'pygments',
    '--exclude-module', 'markdown',
    '--exclude-module', 'mistune',
    '--exclude-module', 'IPython',
    '--exclude-module', 'jupyter',
    '--exclude-module', 'notebook',
    '--exclude-module', 'nbconvert',
    '--exclude-module', 'nbformat',

    # 科研全家桶（与成绩单加权一分钱关系都没有）
    '--exclude-module', 'sympy',
    '--exclude-module', 'mpmath',
    '--exclude-module', 'gmpy2',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'scipy',
    '--exclude-module', 'sklearn',
    '--exclude-module', 'skimage',
    '--exclude-module', 'astropy',
    '--exclude-module', 'statsmodels',
    '--exclude-module', 'patsy',
    '--exclude-module', 'plotly',
    '--exclude-module', 'panel',
    '--exclude-module', 'bokeh',
    '--exclude-module', 'holoviews',
    '--exclude-module', 'dask',
    '--exclude-module', 'distributed',
    '--exclude-module', 'numba',
    '--exclude-module', 'llvmlite',
    '--exclude-module', 'numexpr',
    '--exclude-module', 'bottleneck',
    '--exclude-module', 'h5py',
    '--exclude-module', 'pyarrow',
    '--exclude-module', 'tables',
    '--exclude-module', 'imageio',
    '--exclude-module', 'cv2',
    # ★ PIL / Pillow 曾经被排掉、后来又放行（ttkbootstrap 时代它要加载 PNG 素材）。
    #   现在换成 PySide6，Pillow 不再是界面依赖 —— 但**不再排它**：
    #   排掉它收益很小（几 MB），而 pandas 的某些可选路径会 import 它，
    #   排掉之后可能变成运行时才炸的 ImportError。留着不动。

    # 界面：**只用 PySide6**（铁律九）。别的 GUI 框架一律不进包。
    #   ⚠ 这里**绝对不能**再出现 PySide6 —— 那会把整个 Qt 排除，
    #     打出来的 exe 一启动就 ModuleNotFoundError。
    '--exclude-module', 'qtpy',
    '--exclude-module', 'PyQt5',
    '--exclude-module', 'PyQt6',        # 铁律：不引入 PyQt6，只用 PySide6
    '--exclude-module', 'PySide2',
    '--exclude-module', 'wx',
    '--exclude-module', 'tkinter',      # 旧界面已全部删除，不再需要

    # 网络：不打包 = 物理上发不出请求
    '--exclude-module', 'requests',
    '--exclude-module', 'urllib3',
    '--exclude-module', 'httpx',
    '--exclude-module', 'aiohttp',
    '--exclude-module', 'chardet',
    '--exclude-module', 'charset_normalizer',
    '--exclude-module', 'certifi',
    '--exclude-module', 'tornado',
    '--exclude-module', 'zmq',
    '--exclude-module', 'anyio',
    '--exclude-module', 'sniffio',
    '--exclude-module', 'websockets',

    # 其它与本项目无关的
    '--exclude-module', 'psutil',
    '--exclude-module', 'fsspec',
    '--exclude-module', 'bcrypt',
    '--exclude-module', 'argon2',
    '--exclude-module', 'sqlalchemy',
    '--exclude-module', 'MySQLdb',
    '--exclude-module', 'psycopg2',
    '--exclude-module', 'pydoc',
    '--exclude-module', 'doctest'
)

function Build-One {
    param([ValidateSet('debug', 'release')][string]$Kind)

    if ($Kind -eq 'debug') {
        $name = 'transcript-lens-debug'
        $extra = @('--onedir', '--console')
    }
    elseif ($ShowConsole) {
        # 诊断用：单文件但不隐藏控制台，启动期的报错能直接看见
        $name = 'transcript-lens-console'
        $extra = @('--onefile', '--console', '--runtime-tmpdir', $RuntimeTmpDir)
    }
    else {
        $name = 'transcript-lens'
        $extra = @('--onefile', '--windowed', '--runtime-tmpdir', $RuntimeTmpDir)
    }

    Write-Host "`n=== 打包 $Kind -> dist\$name$(if ($Kind -eq 'release') { '.exe' }) ===" -ForegroundColor Cyan
    # 注意别用 $args 当变量名：那是 PowerShell 的自动变量
    $pyArgs = $Common + $extra + @('--name', $name, 'main.py')
    if ((Invoke-Native python @pyArgs) -ne 0) { throw "PyInstaller 打包失败（$Kind）" }

    if ($Kind -eq 'debug') {
        $exe = Join-Path $Root "dist\$name\$name.exe"
    }
    else {
        $exe = Join-Path $Root "dist\$name.exe"
    }
    if (-not (Test-Path $exe)) { throw "没找到打包产物：$exe" }

    if ($Kind -eq 'debug') {
        # onedir 的体积要看整个目录，光看那个 launcher 小 exe 会严重低估
        $folder = Get-ChildItem (Split-Path $exe) -Recurse -File
        $sizeMb = [math]::Round(($folder | Measure-Object Length -Sum).Sum / 1MB, 1)
        Write-Host "产物：$exe" -ForegroundColor Green
        Write-Host "      整个目录 $sizeMb MB（$($folder.Count) 个文件）" -ForegroundColor Green
    }
    else {
        $sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
        Write-Host "产物：$exe  ($sizeMb MB，单文件)" -ForegroundColor Green
    }

    if (-not $SkipSelfCheck) {
        Write-Host "=== 打包后自检：$name.exe --selfcheck ===" -ForegroundColor Cyan
        # ★ exe 必须在**专用空临时目录**里跑：实测本机 %TEMP%（约 2000 个条目）下，
        #   单文件 exe 每次解包都以 -1 退出、连报告都不写；换成一个新建的空目录立刻
        #   正常（RESULT: ALL OK）。所以这里给 exe 单开一个 build\_selfcheck_tmp。
        $exeTmp = Join-Path (Join-Path (Get-Location) 'build') '_selfcheck_tmp'
        New-Item -ItemType Directory -Force -Path $exeTmp | Out-Null
        $savedTemp = $env:TEMP
        $savedTmp = $env:TMP
        $env:TEMP = $exeTmp
        $env:TMP = $exeTmp
        $report = Join-Path $exeTmp 'transcript-lens-selfcheck.txt'
        Remove-Item $report -Force -ErrorAction SilentlyContinue   # 免得读到上一次的旧报告

        # ★ 必须用 Start-Process 而不是 `& $exe`：--windowed 的 exe 属于 GUI 子系统，
        #   用 & 调用 PowerShell **不会等它跑完**，退出码也拿不到。
        #
        # ★ 必须带超时：窗口版 exe 启动期一崩，PyInstaller 只会弹一个标题为 "Error"
        #   的原生对话框，而 --windowed 没有控制台 —— 那个框在无人值守的脚本里
        #   永远不会被点掉，脚本就永久卡死（第一次打 release 就卡在这儿，
        #   表现是"进程活着、CPU 0%、什么都不发生"）。
        # ★ 必须带超时：单文件 exe 卡在"解包阶段"时不会有任何输出，
        #   窗口版更只会弹一个标题为 "Error" 的原生对话框 —— 在无人值守的脚本里
        #   那个框永远不会被点掉，脚本就永久卡死（第一次打 release 就卡在这儿，
        #   表现是"进程活着、CPU 0%、什么都不发生"）。
        $proc = Start-Process -FilePath $exe -ArgumentList '--selfcheck' -PassThru -WindowStyle Hidden
        $finished = $true
        try { Wait-Process -Id $proc.Id -Timeout 300 -ErrorAction Stop }
        catch { $finished = $false }

        if (-not $finished) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            Get-Process $name -ErrorAction SilentlyContinue |
                Stop-Process -Force -ErrorAction SilentlyContinue
            throw ("exe 自检超时 300s —— 单文件 exe 卡住基本等于**解包阶段就失败了**。" +
                   "用 -ShowConsole 重打一个带控制台的版本，真实报错会直接打在屏幕上：" +
                   "  .\build_exe.ps1 -Target release -ShowConsole")
        }

        $proc.Refresh()
        $code = $proc.ExitCode

        if (-not (Test-Path $report)) {
            # 兜底：打包后的 exe 拿不到可用的 %TEMP% 时，main.run_selfcheck() 会退到
            # “当前目录 → exe 所在目录”落盘（见那里注释）。三处都看一眼，免得报告
            # 其实写出来了、脚本却只认 %TEMP% 一个位置而误判成“没找到报告”。
            foreach ($alt in @((Join-Path (Get-Location) 'transcript-lens-selfcheck.txt'),
                               (Join-Path (Split-Path $exe) 'transcript-lens-selfcheck.txt'))) {
                if (Test-Path $alt) { $report = $alt; break }
            }
        }
        if (Test-Path $report) {
            Write-Host "--- exe 自检报告 ---"
            Get-Content $report -Encoding UTF8 | ForEach-Object { Write-Host $_ }
        }
        else {
            Write-Host "!! 没找到自检报告 $report" -ForegroundColor Red
        }
        if ($code -ne 0) { throw "exe 自检失败（退出码 $code），看上面的报告" }
        Write-Host "exe 自检通过" -ForegroundColor Green

        # --- ★ 真实启动流程自检 ---
        #
        # --selfcheck 是**无窗口**的：它把数据链路跑通了，但一行 GUI 代码都没碰。
        # 而 --windowed 的 exe 没有 stdout/stderr —— 界面代码一旦在启动路径上
        # 崩掉（不是 Python 异常，是 0xC0000409 那种直接把进程抹掉），
        # 用户看到的就是"打完招呼就没反应了"，开发者这边一个字都拿不到。
        #
        # 踩过一次：欢迎页淡出 → 销毁窗口 → 动画被一起销毁 → Qt 发完 finished
        # 还在用那个动画对象 → 进程消失。**当时 400 多项测试全绿、源码自检全绿、
        # 两个 exe 都"自检通过"** —— 因为它栽在一条从来没被跑过的路径上。
        Write-Host "=== 打包后：$name.exe --bootcheck（真实启动流程）===" -ForegroundColor Cyan
        $bootReport = Join-Path $exeTmp 'transcript-lens-bootcheck.txt'
        Remove-Item $bootReport -Force -ErrorAction SilentlyContinue
        $boot = Start-Process -FilePath $exe -ArgumentList '--bootcheck' -PassThru -WindowStyle Hidden
        $bootDone = $true
        try { Wait-Process -Id $boot.Id -Timeout 180 -ErrorAction Stop }
        catch { $bootDone = $false }

        if (-not $bootDone) {
            Stop-Process -Id $boot.Id -Force -ErrorAction SilentlyContinue
            Get-Process $name -ErrorAction SilentlyContinue |
                Stop-Process -Force -ErrorAction SilentlyContinue
            throw "exe --bootcheck 超时 180s（启动流程卡住了）"
        }
        $boot.Refresh()
        if (Test-Path $bootReport) {
            Write-Host "--- exe 启动流程报告 ---"
            Get-Content $bootReport -Encoding UTF8 | ForEach-Object { Write-Host $_ }
        }
        else {
            Write-Host "!! 没找到启动流程报告 $bootReport" -ForegroundColor Red
        }
        # ★ 退出码是关键：崩溃（0xC0000409）会是一个很大的负数
        if ($boot.ExitCode -ne 0) {
            throw "exe --bootcheck 失败（退出码 $($boot.ExitCode)）—— 启动流程有问题，看上面的报告"
        }
        Write-Host "exe 启动流程自检通过" -ForegroundColor Green

        # 收工：临时目录与两个环境变量都还原（否则后面的打包步骤会继续用它）
        $env:TEMP = $savedTemp
        $env:TMP = $savedTmp
        $cleanupRoot = [IO.Path]::GetFullPath((Join-Path $Root 'build')) + [IO.Path]::DirectorySeparatorChar
        $cleanupTarget = [IO.Path]::GetFullPath($exeTmp)
        if (-not $cleanupTarget.StartsWith($cleanupRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "自检临时目录不在本次 build 目录内，已停止清理：$cleanupTarget"
        }
        Remove-Item -LiteralPath $cleanupTarget -Recurse -Force -ErrorAction SilentlyContinue
    }

    return $exe
}

Write-Host "`n=== 2/4 打包 ===" -ForegroundColor Cyan
if ($Both) {
    $built = @()
    $built += Build-One -Kind debug
    $built += Build-One -Kind release
}
else {
    $built = @(Build-One -Kind $Target)
}

# --- 红线 11：确认构建产物真的被 git 忽略 --------------------------------
Write-Host "`n=== 3/4 红线 11：构建产物必须被 git 忽略 ===" -ForegroundColor Cyan
foreach ($probe in @('dist/', 'build/', 'transcript-lens.spec')) {
    git check-ignore -q $probe
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  已忽略  $probe" -ForegroundColor Green
    }
    else {
        throw "$probe 没有被 .gitignore 拦住 —— 严禁把 exe 提交进仓库"
    }
}

Write-Host "`n=== 4/4 完成 ===" -ForegroundColor Cyan
$built | ForEach-Object { Write-Host "  $_" }
Write-Host "`n提示：dist/ 与 build/ 已被忽略，不要 git add 它们。" -ForegroundColor Yellow
