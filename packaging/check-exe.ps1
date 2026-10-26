<#
Runs `PyVised.exe --self-test` and prints its report; the exit code is the self-test's.
The app gets its own LOCALAPPDATA and settings file under build\check, so neither your
settings nor a running PyVised are touched. A crash dialog of the windowed app (it would
wait forever) counts as a failure.

    ./packaging/check-exe.ps1 [-Exe dist\PyVised\PyVised.exe] [-Backend]

-Backend also checks Docker, the backend image (downloaded when missing) and the backend's
own self-test.
#>
param(
    [string]$Exe = "dist\PyVised\PyVised.exe",
    [switch]$Backend
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path $root "build\check"
New-Item -ItemType Directory -Force $work | Out-Null
$report = Join-Path $work "report.txt"
Remove-Item $report -ErrorAction SilentlyContinue

$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = (Resolve-Path $Exe).Path
$info.Arguments = "--self-test --report `"$report`"" + $(if ($Backend) { "" } else { " --no-backend" })
$info.UseShellExecute = $false  # also keeps the exit code reliable
$info.EnvironmentVariables["LOCALAPPDATA"] = $work
$info.EnvironmentVariables["PYVISED_SETTINGS"] = Join-Path $work "PyVised.ini"
$minutes = if ($Backend) { 60 } else { 5 }

$process = [System.Diagnostics.Process]::Start($info)
if (-not $process.WaitForExit($minutes * 60000)) {
    $process.Kill()
    Get-Content -Encoding UTF8 (Join-Path $work "PyVised\logs\pyvised.log") -Tail 40 -ErrorAction SilentlyContinue
    throw "$Exe did not finish within $minutes minutes (an error dialog?)"
}
if (Test-Path $report) { Get-Content -Encoding UTF8 $report } else { Write-Host "no report written" }
exit $process.ExitCode
