$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$localRoot = Join-Path (Split-Path -Parent $repoRoot) 'local'
New-Item -ItemType Directory -Path (Join-Path $localRoot 'tmp'),(Join-Path $localRoot 'cache\pycache') -Force | Out-Null
$env:BOYO_LOCAL = $localRoot
$env:TEMP = Join-Path $localRoot 'tmp'
$env:TMP = $env:TEMP
$env:PYTHONPATH = Join-Path $repoRoot 'src'
$env:PYTHONPYCACHEPREFIX = Join-Path $localRoot 'cache\pycache'
$env:OMP_NUM_THREADS='1'
$env:OPENBLAS_NUM_THREADS='1'
$env:MKL_NUM_THREADS='1'
$env:NUMEXPR_NUM_THREADS='1'
& (Join-Path $localRoot 'environment\Scripts\python.exe') -m unittest discover -s (Join-Path $repoRoot 'tests') -v
exit $LASTEXITCODE
