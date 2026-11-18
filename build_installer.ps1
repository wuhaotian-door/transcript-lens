param(
    [string]$CompilerPath = 'ISCC.exe',
    [string]$Payload = (Join-Path $PSScriptRoot 'dist\transcript-lens.exe'),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'release'),
    [string]$LanguageFile = '',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$compiler = Get-Command $CompilerPath -CommandType Application
$app = (Resolve-Path -LiteralPath $Payload).Path
$output = (New-Item -ItemType Directory -Path $OutputDirectory -Force).FullName
$script = Join-Path $PSScriptRoot 'installer\transcript-lens.iss'
if ($LanguageFile) {
    $LanguageFile = (Resolve-Path -LiteralPath $LanguageFile).Path
}

Push-Location $PSScriptRoot
try {
    $version = & $Python -B -c 'from core.version import CURRENT_VERSION; print(CURRENT_VERSION)'
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') {
        throw '无法读取软件版本。'
    }

    $compilerArgs = @(
        "/DAppVersion=$version",
        "/DAppFile=$app",
        "/DReleaseDir=$output",
        "/DAppIcon=$(Join-Path $PSScriptRoot 'ui\assets\app.ico')",
        "/DLicenseFile=$(Join-Path $PSScriptRoot 'LICENSE')"
    )
    if ($LanguageFile) {
        $compilerArgs += "/DChineseLanguageFile=$LanguageFile"
    }
    & $compiler.Source @compilerArgs $script
    if ($LASTEXITCODE -ne 0) { throw '安装向导编译失败。' }
    $installer = Join-Path $output "成绩透析镜v${version}-安装向导.exe"
    Get-Item -LiteralPath $installer
}
finally {
    Pop-Location
}
