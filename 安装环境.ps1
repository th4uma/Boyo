$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$localRoot = Join-Path (Split-Path -Parent $repoRoot) 'local'
New-Item -ItemType Directory -Path (Join-Path $localRoot 'tmp'),(Join-Path $localRoot 'cache\pip') -Force | Out-Null
$env:TEMP = Join-Path $localRoot 'tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $localRoot 'cache\pip'
$env:PYTHONPYCACHEPREFIX = Join-Path $localRoot 'cache\pycache'
$portableRoot = Join-Path $localRoot 'python'
$bootstrapPython = Join-Path $portableRoot 'python.exe'
if (-not (Test-Path -LiteralPath $bootstrapPython)) {
    $basePython = (& python -c 'import sys; print(sys.base_prefix)').Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $basePython 'python.exe'))) { throw '找不到可复制的 Windows Python。' }
    New-Item -ItemType Directory -Path $portableRoot -Force | Out-Null
    Get-ChildItem -LiteralPath $basePython -File | Where-Object { $_.Name -match '^python.*\.(exe|dll)$|\.dll$|^LICENSE' } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $portableRoot $_.Name)
    }
    foreach ($part in @('DLLs','Lib','include','libs')) {
        $sourcePart = Join-Path $basePython $part
        if (Test-Path -LiteralPath $sourcePart) {
            & robocopy $sourcePart (Join-Path $portableRoot $part) /E /COPY:DAT /R:1 /W:1 /XJ /NP /NFL /NDL /XD '__pycache__' 'site-packages'
            if ($LASTEXITCODE -ge 8) { throw "复制 Python 组件失败：$part" }
        }
    }
}
$pythonPath = Join-Path $localRoot 'environment\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & $bootstrapPython -m venv (Join-Path $localRoot 'environment')
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败，需要 Python 3.11 或更新版本。' }
}
& $pythonPath -m pip install -r (Join-Path $repoRoot 'requirements-lock.txt') --disable-pip-version-check --retries 1
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
& $pythonPath -m pip install 'torch>=2.14,<2.15' 'torchvision>=0.29,<0.30' --index-url https://download.pytorch.org/whl/cpu --disable-pip-version-check --retries 1
if ($LASTEXITCODE -ne 0) { throw '本地视觉运行库安装失败。' }
& $pythonPath -m pip install -r (Join-Path $repoRoot 'requirements-learning.txt') --disable-pip-version-check --retries 1
if ($LASTEXITCODE -ne 0) { throw '个人学习依赖安装失败。' }
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $pythonPath -m boyo.model_setup --home $localRoot
if ($LASTEXITCODE -ne 0) { throw '本地视觉权重下载失败；可重新运行本脚本续用缓存。' }
& $bootstrapPython (Join-Path $repoRoot 'portable_env.py')
if ($LASTEXITCODE -ne 0) { throw '迁移环境初始化失败。' }
Write-Host '安装完成。双击 启动Boyo.cmd。'
