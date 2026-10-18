<#
.SYNOPSIS
    Install LLM Usage Widget for the current user and start it at login.

.DESCRIPTION
    Copies the app to %LOCALAPPDATA%\llm-usage-widget with its own virtualenv
    (requirements.txt), adds Start Menu and Startup shortcuts, and starts it.
    No administrator rights are needed. Settings in %APPDATA%\llm-usage-widget
    are left in place on uninstall.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$App       = 'llm-usage-widget'
$Name      = 'LLM Usage Widget'
$SrcDir    = $PSScriptRoot
$Dest      = Join-Path $env:LOCALAPPDATA $App
$Startup   = Join-Path ([Environment]::GetFolderPath('Startup')) "$Name.lnk"
$StartMenu = Join-Path ([Environment]::GetFolderPath('Programs')) "$Name.lnk"

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Stop-Widget {
    # Any running copy (installed or run from a checkout) holds the single-instance lock.
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*usage_widget.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}

function Find-Python {
    # Prefer the py launcher; skip the Microsoft Store "python.exe" stub, which only opens the Store.
    $candidates = @(
        @{ Exe = 'py';      Args = @('-3') },
        @{ Exe = 'python3'; Args = @() },
        @{ Exe = 'python';  Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $exe = & $c.Exe @($c.Args + @('-c', 'import sys; assert sys.version_info >= (3, 9); print(sys.executable)')) 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        } catch { }
    }
    return $null
}

function New-Shortcut($path, $target, $arguments, $workdir) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $workdir
    $lnk.Description = 'Claude and Codex usage limits in the notification area'
    $lnk.Save()
}

if ($Uninstall) {
    Say "Stopping and removing $Name"
    Stop-Widget
    Remove-Item -Force -ErrorAction SilentlyContinue $Startup, $StartMenu
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    Say "Removed. Settings remain in $(Join-Path $env:APPDATA $App)"
    exit 0
}

$python = Find-Python
if (-not $python) {
    Write-Error "Python 3.9+ not found. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this again."
}

Say "Installing to $Dest"
Stop-Widget
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Copy-Item -Force (Join-Path $SrcDir 'usage_widget.py') $Dest

$venvPy = Join-Path $Dest 'venv\Scripts\python.exe'
Say 'Creating virtualenv and installing dependencies'
if (-not (Test-Path $venvPy)) { & $python -m venv (Join-Path $Dest 'venv') }
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r (Join-Path $SrcDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Write-Error 'Installing dependencies failed.' }

$pythonw = Join-Path $Dest 'venv\Scripts\pythonw.exe'
$script  = '"' + (Join-Path $Dest 'usage_widget.py') + '"'
New-Shortcut $StartMenu $pythonw $script $Dest
New-Shortcut $Startup   $pythonw $script $Dest
Say "Added to login startup: $Startup"

Say 'Starting it now'
Start-Process -FilePath $pythonw -ArgumentList $script -WorkingDirectory $Dest
Say 'Done. Your usage is shown in an empty spot on the taskbar; click it for details.'
Say 'It will start each time you sign in.'
