# build_exe.ps1 — 把 subtranslate.py 打包成单文件 SubTranslate.exe
#
# 原则：只在本目录的 .venv-build 里装 PyInstaller，不碰全局 Python。
# 完全回退：删除 .venv-build、build、dist 三个目录即可，系统环境零残留。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -Clean
#
param([switch]$Clean)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "找不到 python，请先安装 Python 3.10+ 并加入 PATH"
}

# ---- 1) 隔离 venv ----
$vpy = ".venv-build\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Write-Host "[1/4] 创建独立 venv：.venv-build"
    python -m venv .venv-build
} else {
    Write-Host "[1/4] 复用已有 .venv-build"
}

# ---- 2) 依赖（只装进 venv） ----
& $vpy -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[2/4] 安装 PyInstaller 到 venv（不污染全局）..."
    & $vpy -m pip install --disable-pip-version-check --no-input `
        -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
}

# ---- 3) 打包 ----
if ($Clean) {
    Remove-Item build, dist -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "[3/4] PyInstaller 打包（onefile + windowed）..."
& $vpy -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name SubTranslate --distpath dist --workpath build --specpath build `
    subtranslate.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }

# ---- 4) 验证 ----
Write-Host "[4/4] 验证 exe ..."
$exe = Join-Path $here "dist\SubTranslate.exe"
if (-not (Test-Path $exe)) { throw "打包失败：找不到 $exe" }

$p = Start-Process -FilePath $exe -ArgumentList "--selftest" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "exe 自检失败，退出码 $($p.ExitCode)" }

# 干净目录独立运行（不依赖本目录任何文件）
$tmp = Join-Path $env:TEMP ("subtranslate_check_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force $tmp | Out-Null
Copy-Item $exe $tmp
$p2 = Start-Process -FilePath (Join-Path $tmp "SubTranslate.exe") -ArgumentList "--selftest" -Wait -PassThru
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
if ($p2.ExitCode -ne 0) { throw "干净目录运行失败，退出码 $($p2.ExitCode)" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "完成：$exe  ($size MB)"
Write-Host "自检通过（本目录 + 干净临时目录）"
