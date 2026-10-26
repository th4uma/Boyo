param([Parameter(Mandatory=$true)][string]$ScriptPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$taskPhotoshop = $null
try {
    $taskPhotoshop = New-Object -ComObject Photoshop.Application
    $taskResult = $taskPhotoshop.DoJavaScriptFile($ScriptPath)
    Write-Output $taskResult
} catch {
    Write-Output $_.Exception.Message
    exit 1
} finally {
    if ($null -ne $taskPhotoshop) {
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($taskPhotoshop)
    }
}
