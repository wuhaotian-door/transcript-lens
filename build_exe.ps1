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
#>
param(
    [ValidateSet('debug', 'release')]
    [string]$Target = 'debug',
    [switch]$Both,
    #: release 也带控制台（产物叫 transcript-lens-console.exe），用来查启动崩溃
    [switch]$ShowConsole,
    [switch]$SkipSelfCheck
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

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

    # pandas 是延迟 import openpyxl 的，静态分析看不到，必须显式带上
    '--hidden-import', 'openpyxl',
    '--hidden-import', 'openpyxl.styles',
    '--hidden-import', 'openpyxl.worksheet._writer',
    '--hidden-import', 'pandas._libs.tslibs.base',

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

    # ★ ttkbootstrap **必须用 --collect-all**（不能只 collect-submodules）：
    #   它的主题和控件素材是**数据文件**，不是 .py ——
    #     assets/icons/bootstrap.ttf / glyphmap.json
    #     assets/elements/*.png / manifest.json
    #     assets/app_icons/ttkbootstrap.png
    #   少一个，exe 里就会变成"窗口能开、主题报错"或者图标全变豆腐块。
    #   --collect-all = submodules + data + binaries，一次说清。
    '--collect-all', 'ttkbootstrap',
    # ttkbootstrap 的图标/图片走 Pillow
    '--hidden-import', 'PIL',
    '--hidden-import', 'PIL.Image',
    '--hidden-import', 'PIL.ImageTk',

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
    # ★ PIL / Pillow 曾经在这里被排掉（那时它只是 pandas 的闲置邻居）。
    #   换成 ttkbootstrap 之后**必须放行**：它的复选框/开关/图标素材都是 PNG，
    #   走 Pillow 加载。排掉的话 exe 里会变成"窗口能开、控件素材全缺"。
    #   代价是 exe 大几 MB，认了。

    # 界面：tkinter 之外都不需要
    '--exclude-module', 'qtpy',
    '--exclude-module', 'PyQt5',
    '--exclude-module', 'PyQt6',
    '--exclude-module', 'PySide2',
    '--exclude-module', 'PySide6',
    '--exclude-module', 'wx',

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
        $extra = @('--onefile', '--console')
    }
    else {
        $name = 'transcript-lens'
        $extra = @('--onefile', '--windowed')
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
        $report = Join-Path ([System.IO.Path]::GetTempPath()) 'transcript-lens-selfcheck.txt'
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
        $proc = Start-Process -FilePath $exe -ArgumentList '--selfcheck' -PassThru
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

        if (Test-Path $report) {
            Write-Host "--- exe 自检报告 ---"
            Get-Content $report -Encoding UTF8 | ForEach-Object { Write-Host $_ }
        }
        else {
            Write-Host "!! 没找到自检报告 $report" -ForegroundColor Red
        }
        if ($code -ne 0) { throw "exe 自检失败（退出码 $code），看上面的报告" }
        Write-Host "exe 自检通过" -ForegroundColor Green
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
