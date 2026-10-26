<#
Installs a PyVised setup silently for the current user, checks the installation (files,
Start-menu shortcut, uninstall entry, the installed app's self-test) and uninstalls it
silently again. CI runs it after `just installer`.

    ./installer/check-install.ps1 [-Setup dist\PyVised-Setup-1.0.0.exe] [-Backend] [-Keep]

-Backend also runs the backend part of the self-test (needs Docker); -Keep leaves PyVised
installed.
#>
param(
    [string]$Setup = "",
    [switch]$Backend,
    [switch]$Keep
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Setup) {
    $Setup = (Get-ChildItem (Join-Path $root "dist\PyVised-Setup-*.exe") |
        Sort-Object LastWriteTime | Select-Object -Last 1).FullName
}
$work = Join-Path $root "build\check"
New-Item -ItemType Directory -Force $work | Out-Null
$appId = "{B4F1FF0C-E5E3-4E9A-B3EB-6C94077A010A}"  # AppId in installer\pyvised.iss

function Invoke-Silently([string]$File, [string[]]$Arguments, [string]$What) {
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -PassThru
    $null = $process.Handle  # without it Windows PowerShell may lose the exit code
    if (-not $process.WaitForExit(600000)) {
        $process.Kill()
        throw "$What did not finish within 10 minutes"
    }
    if ($process.ExitCode -ne 0) { throw "$What failed with exit code $($process.ExitCode)" }
}

Write-Host "Installing $Setup"
$log = Join-Path $work "install.log"
Invoke-Silently $Setup @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
    '/MERGETASKS="!desktopicon"', "/LOG=`"$log`""
) "Setup"
$app = Join-Path $env:LOCALAPPDATA "Programs\PyVised"
$exe = Join-Path $app "PyVised.exe"
$expected = @(
    $exe,
    (Join-Path $app "unins000.exe"),
    (Join-Path $app "_internal\pyvised\i18n\pyvised_hu.qm"),
    (Join-Path $app "_internal\PySide6\translations\qtbase_hu.qm"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\PyVised.lnk")
)
foreach ($path in $expected) {
    if (-not (Test-Path $path)) { throw "missing after the install: $path" }
}
$entry = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\${appId}_is1"
Write-Host "Installed $($entry.DisplayName) $($entry.DisplayVersion) ($($entry.Publisher)) in $($entry.InstallLocation)"

& (Join-Path $root "packaging\check-exe.ps1") -Exe $exe -Backend:$Backend
if ($LASTEXITCODE -ne 0) { throw "the self-test of the installed app failed" }
if ($Keep) { return }

Write-Host "Uninstalling"
Invoke-Silently (Join-Path $app "unins000.exe") @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") "Uninstall"
# the uninstaller continues in a copy of itself and returns at once: wait for the files to go
$deadline = (Get-Date).AddMinutes(2)
while ((Test-Path $exe) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Seconds 1 }
if (Test-Path $exe) { throw "the uninstaller left $exe" }
if (Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\${appId}_is1") {
    throw "the uninstaller left its registry entry"
}
Write-Host "Uninstalled"
