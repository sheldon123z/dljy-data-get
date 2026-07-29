param(
    [switch]$SkipDownload,
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$DistRoot = Join-Path $ProjectRoot "dist-windows"
$AppFolder = Join-Path $DistRoot "电力现货价格工作台-SmartAppControl兼容版"
$RuntimeFolder = Join-Path $AppFolder "runtime"
$DownloadCache = Join-Path $ProjectRoot "build-windows\python-3.13.14-embed-amd64.zip"
$Archive = Join-Path $DistRoot "电力现货价格工作台-SmartAppControl兼容版.zip"
$PythonUrl = "https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip"
$ExpectedSha256 = "90B4E5B9898B72D744650524BFF92377C367F44BD5FBD09E3148656C080AD907"

if (Test-Path -LiteralPath $AppFolder) {
    $resolved = [IO.Path]::GetFullPath($AppFolder)
    if (-not $resolved.StartsWith([IO.Path]::GetFullPath($DistRoot), [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理非输出目录：$resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Path $RuntimeFolder -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path $DownloadCache) -Force | Out-Null

if (-not $SkipDownload -or -not (Test-Path -LiteralPath $DownloadCache)) {
    Invoke-WebRequest -Uri $PythonUrl -OutFile $DownloadCache
}
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $DownloadCache).Hash
if ($actualHash -ne $ExpectedSha256) {
    throw "Python 官方运行时校验失败：期望 $ExpectedSha256，实际 $actualHash"
}

Expand-Archive -LiteralPath $DownloadCache -DestinationPath $RuntimeFolder -Force
$signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $RuntimeFolder "python.exe")
if ($signature.Status -ne "Valid") {
    throw "Python 官方运行时的 Authenticode 签名无效：$($signature.Status)"
}

$sitePackages = Join-Path $RuntimeFolder "Lib\site-packages"
New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
foreach ($package in @("openpyxl", "et_xmlfile")) {
    $source = python -c "import $package; print($package.__path__[0])"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "缺少构建依赖目录：$source"
    }
    Copy-Item -LiteralPath $source -Destination $sitePackages -Recurse -Force
}

$pth = @(
    "python313.zip"
    "."
    "Lib\site-packages"
    ".."
    "import site"
) -join [Environment]::NewLine
Set-Content -LiteralPath (Join-Path $RuntimeFolder "python313._pth") -Value $pth -Encoding ASCII

Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts") -Destination $AppFolder -Recurse -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "data") -Destination $AppFolder -Recurse -Force
foreach ($file in @(
    "run.py",
    "README.md",
    "DEPENDENCIES.md",
    ".env.example",
    "requirements.lock.txt"
)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot $file) -Destination $AppFolder -Force
}

$launcher = @(
    "@echo off"
    "chcp 65001 >nul"
    "setlocal"
    "cd /d ""%~dp0"""
    "title 电力现货价格工作台"
    "echo."
    "echo   正在启动电力现货价格工作台..."
    "echo   启动后浏览器会自动打开，请保持此窗口开启。"
    "echo."
    """%~dp0runtime\python.exe"" ""%~dp0run.py"" serve"
    "set ""APP_EXIT=%ERRORLEVEL%"""
    "if not ""%APP_EXIT%""==""0"" ("
    "  echo."
    "  echo   启动失败，错误码：%APP_EXIT%"
    "  echo   请将本窗口内容截图后反馈。"
    "  pause"
    ")"
    "endlocal"
) -join [Environment]::NewLine
Set-Content -LiteralPath (Join-Path $AppFolder "启动工作台.cmd") -Value $launcher -Encoding UTF8

$quickStart = @(
    "Smart App Control 兼容版"
    "========================="
    ""
    "1. 双击「启动工作台.cmd」，不要运行旧的未签名 EXE。"
    "2. 保持命令窗口开启；浏览器会自动打开操作页面。"
    "3. 如果浏览器未打开，请复制命令窗口中 http://127.0.0.1 开头的完整地址。"
    "4. 使用完毕后关闭命令窗口，或在网页中点击退出。"
    ""
    "该版本使用 Python.org 官方签名的 Windows 嵌入式运行时，"
    "不需要关闭 Windows Smart App Control，也不需要预装 Python。"
) -join [Environment]::NewLine
Set-Content -LiteralPath (Join-Path $AppFolder "使用说明.txt") -Value $quickStart -Encoding UTF8

if (-not $SkipZip) {
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }
    Compress-Archive -LiteralPath $AppFolder -DestinationPath $Archive -CompressionLevel Optimal
}

Write-Host "Smart App Control 兼容版已生成：$AppFolder"
Write-Host "官方 Python 签名：$($signature.Status) / $($signature.SignerCertificate.Subject)"
if (-not $SkipZip) {
    Write-Host "便携包已生成：$Archive"
}
