# 组装小書房 Windows 便携版 zip
# 前置: pyinstaller packaging/xsf.spec --noconfirm  (产出 dist/xsf-portable/)
# 用法: pwsh packaging/make_portable.ps1 [-DistDir dist]
param(
    [string]$DistDir = "dist"
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$coll = Join-Path $repo "$DistDir/xsf-portable"

if (-not (Test-Path $coll)) {
    throw "未找到 $coll — 先跑: pyinstaller packaging/xsf.spec --noconfirm --distpath $DistDir"
}

foreach ($f in @('小書房.exe', 'xsf.exe', 'xsf-mcp.exe', '_internal')) {
    if (-not (Test-Path (Join-Path $coll $f))) { throw "产物缺失: $f" }
}

$version = (python -c "import importlib.metadata as m; print(m.version('xsf'))").Trim()
$zip = Join-Path $repo "$DistDir/xsf-portable-win64-$version.zip"

Copy-Item (Join-Path $repo '.env.example') $coll -Force
Copy-Item (Join-Path $PSScriptRoot 'README-使用说明.txt') $coll -Force
Copy-Item (Join-Path $PSScriptRoot 'add-to-path.bat') $coll -Force

if (Test-Path $zip) { Remove-Item $zip -Force }
& 7z a -tzip $zip "$coll\*" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "7z 打包失败" }
Write-Host "OK: $zip"
