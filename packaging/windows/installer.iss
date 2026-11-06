; Inno Setup script: per-user installer (no admin rights) that adds a Start Menu
; entry and, by default, starts the widget at sign-in.
; Built by build.ps1:  iscc /DAppVersion=1.0.10 /DAppArch=x64 installer.iss  (or /DAppArch=arm64)
;
; The same .exe also uninstalls, by running the installed copy's own uninstaller
; (the one Settings > Apps runs):
;
;   LLM-Usage-Widget-<ver>-windows-<arch>-setup.exe /uninstall              asks, then removes the app
;   LLM-Usage-Widget-<ver>-windows-<arch>-setup.exe /uninstall /VERYSILENT  removes it without asking

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppArch
  #define AppArch "x64"
#endif
#define AppName "LLM Usage Widget"
#define AppExe "LLMUsageWidget.exe"
; Shown as the publisher in Settings > Apps; matches the code-signing certificate's name.
#define AppPublisher "Aleph Void LLC"
; AppId below; Inno registers the install under "<AppId>_is1", where /uninstall looks for it.
#define AppIdGuid "{9B3C4E2A-6F1D-4A8B-9C7E-2D5F0A1B3C4D}"

[Setup]
AppId={{9B3C4E2A-6F1D-4A8B-9C7E-2D5F0A1B3C4D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=LLM-Usage-Widget-{#AppVersion}-windows-{#AppArch}-setup
; The x64 build also runs on Arm64 PCs under emulation; the arm64 build runs only there, natively.
#if AppArch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "startup"; Description: "Start automatically when I sign in"; GroupDescription: "Startup:"

[Files]
Source: "..\..\build\windows\dist\LLMUsageWidget\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userprograms}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopWidget"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdGuid}_is1';

function HasParam(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then begin
      Result := True;
      Exit;
    end;
end;

// The app installs per-user (HKCU); also look in HKLM in case it was installed for all users.
function FindUninstaller(var Path: String): Boolean;
begin
  Result := RegQueryStringValue(HKCU, UninstallKey, 'UninstallString', Path);
  if not Result then
    Result := RegQueryStringValue(HKLM, UninstallKey, 'UninstallString', Path);
  if Result then begin
    Path := RemoveQuotes(Path);
    Result := FileExists(Path);
  end;
end;

// With /uninstall, run the installed uninstaller and quit before the wizard shows.
// That uninstaller asks for confirmation, stops the widget and reports the result.
function InitializeSetup(): Boolean;
var
  Uninstaller, Params: String;
  ResultCode: Integer;
begin
  Result := not HasParam('/uninstall');
  if Result then
    Exit;
  if not FindUninstaller(Uninstaller) then begin
    SuppressibleMsgBox('{#AppName} is not installed.', mbInformation, MB_OK, IDOK);
    Exit;
  end;
  if WizardSilent then
    Params := '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
  else
    Params := '';
  if not Exec(Uninstaller, Params, '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
    SuppressibleMsgBox('Could not run ' + Uninstaller + ':' + #13#10 + SysErrorMessage(ResultCode),
      mbError, MB_OK, IDOK);
end;

// Stop a running copy before replacing its files.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
