param(
    [switch]$SkipInstall,
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$BuildRoot = Join-Path $ProjectRoot "build-windows"
$DistRoot = Join-Path $ProjectRoot "dist-windows"
$AppFolder = Join-Path $DistRoot "电力现货价格工作台"
$Archive = Join-Path $DistRoot "电力现货价格工作台-Windows便携版.zip"

if (-not $SkipInstall) {
    python -m pip install -r (Join-Path $ProjectRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败" }
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name "电力现货价格工作台" `
    --distpath $DistRoot `
    --workpath $BuildRoot `
    --specpath $BuildRoot `
    --paths (Join-Path $ProjectRoot "scripts") `
    --add-data "$ProjectRoot\scripts\templates;templates" `
    --hidden-import collect `
    --hidden-import export_json `
    --hidden-import export_excel `
    --hidden-import export_tree `
    --hidden-import weekly_report `
    --hidden-import dashboard `
    --hidden-import llm_summary `
    --hidden-import analysis_agents `
    --hidden-import common `
    --hidden-import config `
    --hidden-import serve `
    (Join-Path $ProjectRoot "windows_app.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

Copy-Item -LiteralPath (Join-Path $ProjectRoot "data") -Destination $AppFolder -Recurse -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination $AppFolder -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "DEPENDENCIES.md") -Destination $AppFolder -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination $AppFolder -Force

$QuickStart = @(
    "电力现货价格工作台"
    "=================="
    ""
    "1. 双击「电力现货价格工作台.exe」。"
    "2. 默认浏览器会自动打开操作页面。"
    "3. 在「① 数据采集 Authorization」中粘贴完整值并保存。"
    "4. 设置天数，点击「采集并更新看板」。"
    "5. 如需 AI 总结，在「② 模型 API Key」中填写密钥，并在「③ Agent 协作模式」选择模式。"
    "6. 使用完毕请点击网页中的「退出 Windows 应用 / 本地服务」。"
    ""
    "注意：请保留整个文件夹，不要只移动 EXE。"
) -join [Environment]::NewLine
Set-Content -LiteralPath (Join-Path $AppFolder "使用说明.txt") -Value $QuickStart -Encoding UTF8

if (-not $SkipZip) {
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }
    Compress-Archive -LiteralPath $AppFolder -DestinationPath $Archive -CompressionLevel Optimal
}

$Exe = Join-Path $AppFolder "电力现货价格工作台.exe"
if (-not (Test-Path -LiteralPath $Exe)) { throw "构建产物不存在：$Exe" }
Write-Host "Windows 应用已生成：$Exe"
if (-not $SkipZip) { Write-Host "便携包已生成：$Archive" }
