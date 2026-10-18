# Build dist\LLM-Usage-Widget-<version>-windows-setup.exe (run on Windows).
#
# Freezes the app with PyInstaller (no console window) and packages it with
# Inno Setup 6. Needs: python with `pip install pyinstaller -r requirements.txt`,
# and Inno Setup (https://jrsoftware.org/isdl.php; preinstalled on GitHub's Windows runners).
$ErrorActionPreference = 'Stop'

$Root  = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$Build = Join-Path $Root 'build\windows'
$Version = if ($env:VERSION) { $env:VERSION } else {
    (Select-String -Path (Join-Path $Root 'usage_widget.py') -Pattern '^__version__ = "(.*)"').Matches[0].Groups[1].Value
}

if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
python (Join-Path $Root 'packaging\make_icon.py') | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'make_icon.py failed' }

# Generate comtypes' UI Automation wrapper now so it is bundled; a frozen app can't generate it.
python -c "import comtypes.client; comtypes.client.GetModule('UIAutomationCore.dll')"
if ($LASTEXITCODE -ne 0) { throw 'Generating the UI Automation wrapper failed' }

python -m PyInstaller --noconfirm --clean --windowed `
    --name LLMUsageWidget `
    --icon (Join-Path $Root 'assets\icon.ico') `
    --hidden-import pystray._win32 `
    --hidden-import comtypes.gen.UIAutomationClient `
    --distpath (Join-Path $Build 'dist') --workpath (Join-Path $Build 'work') --specpath $Build `
    (Join-Path $Root 'usage_widget.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    # Machine-wide install, or a per-user one (`winget install JRSoftware.InnoSetup --scope user`).
    $iscc = @((Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
              (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')) |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw 'Inno Setup 6 (ISCC.exe) not found' }
& $iscc "/DAppVersion=$Version" (Join-Path $PSScriptRoot 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }

Get-ChildItem (Join-Path $Root 'dist') -Filter '*-windows-setup.exe' | ForEach-Object { $_.FullName }
