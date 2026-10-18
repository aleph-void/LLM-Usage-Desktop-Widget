; Inno Setup script: standalone uninstaller shipped next to the setup .exe in each release.
; It installs nothing. It finds the per-user install that installer.iss made and runs that
; install's own uninstaller (the same one Settings > Apps runs).
; Built by build.ps1:  iscc /DAppVersion=1.0.0 uninstaller.iss
;
;   LLM-Usage-Widget-<ver>-windows-uninstall.exe              asks, then removes the app
;   LLM-Usage-Widget-<ver>-windows-uninstall.exe /VERYSILENT  removes it without asking

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "LLM Usage Widget"
#define AppPublisher "Aleph Void LLC"
; installer.iss's AppId; Inno registers the install under "<AppId>_is1".
#define InstalledAppId "{9B3C4E2A-6F1D-4A8B-9C7E-2D5F0A1B3C4D}"

[Setup]
; Its own AppId, so it never reads or touches the app's install record.
AppId={{4F6D2B8E-1A3C-4E5F-8B7D-9C0E2A4F6B1D}
AppName={#AppName} Uninstaller
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=LLM-Usage-Widget-{#AppVersion}-windows-uninstall
SetupIconFile=..\..\assets\icon.ico
WizardStyle=modern

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#InstalledAppId}_is1';

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

// All the work happens here and setup then quits, so no wizard window is shown.
// The app's uninstaller asks for confirmation, stops the widget and reports the result.
function InitializeSetup(): Boolean;
var
  Uninstaller, Params: String;
  ResultCode: Integer;
begin
  Result := False;
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
