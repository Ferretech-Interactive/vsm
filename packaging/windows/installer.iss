; Inno Setup script for Vein Server Manager. Build with packaging/windows/build.ps1,
; which builds the program and downloads the runtime installers into <repo>\build first.
;
; Unattended install (host):    "VSM x.y.z Setup.exe" /VERYSILENT /DATADIR="D:\Vein" /PASSWORDFILE="C:\secret.txt" /TASKS="remote"
; Unattended install (client):  "VSM x.y.z Setup.exe" /VERYSILENT /MODE=client
; Unattended update:            "VSM x.y.z Setup.exe" /VERYSILENT        (keeps every earlier choice)
;
; Run over an existing install, Setup offers to update and asks nothing else. Passing any of
; /MODE, /DATADIR, /PASSWORDFILE or /TASKS means "change the setup options" instead.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "0.0.0.0"
#endif
#ifndef AppCopyright
  #define AppCopyright "Copyright (c) FerreTech Interactive"
#endif
; Where build.ps1 put the built program and the downloaded runtime installers.
#ifndef AppDir
  #define AppDir "..\..\build\windows\app\VeinServerManager"
#endif
#ifndef RedistDir
  #define RedistDir "..\..\build\windows\redist"
#endif
#define AppName "Vein Server Manager"
#define AppExe "VeinServerManager.exe"
#define ServiceName "VeinServerManager"
#define GameExe "VeinServer-Win64-Test.exe"

[Setup]
AppId={{8B5B8E0E-6C1E-4E57-9B62-6D1C0F3A7A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=FerreTech Interactive
AppPublisherURL=https://github.com/Ferretech-Interactive/vsm
AppCopyright={#AppCopyright}
; Shown in the file's Properties > Details. Windows needs a purely numeric file version, so a
; pre-release such as 0.1.0b1 is 0.1.0.0 there and keeps its full name as the product version.
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoCompany=FerreTech Interactive
VersionInfoCopyright={#AppCopyright}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
SetupIconFile=vsm.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\..\build\release
OutputBaseFilename=VSM {#AppVersion} Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
#ifdef Sign
; build.ps1 -Sign defines the "vsmsign" tool; this signs Setup.exe and the uninstaller it carries.
SignTool=vsmsign
SignedUninstaller=yes
#endif

[Tasks]
Name: "remote"; Description: "Let other devices on my network manage this server (needs a password; Private and Domain networks only)"; Flags: unchecked; Check: IsHost
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
; The settings file is written right after the executable so every later step can rely on it.
Source: "{#AppDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: WriteSettings
Source: "{#AppDir}\*"; Excludes: "{#AppExe},desktop.json"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RedistDir}\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: IsHost and NeedsVCRuntime
Source: "{#RedistDir}\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; The Unreal server needs the Visual C++ runtime; the window needs the WebView2 runtime.
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing the Visual C++ runtime..."; Check: IsHost and NeedsVCRuntime
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing the WebView2 runtime..."; Check: NeedsWebView2
; The service, firewall task and password are set up in [Code] (CurStepChanged) so a failure is reported.
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: postinstall nowait skipifsilent runasoriginaluser
; A one-click update from the app runs Setup silently and asks for the app to be reopened afterwards.
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: WantsRelaunch

[UninstallRun]
; Stop the game server through the manager (so it can finish saving), remove the service, the
; firewall task and the rules, then close any windows. The data folder is kept.
Filename: "{app}\{#AppExe}"; Parameters: "--uninstall-service"; Flags: runhidden; RunOnceId: "RemoveService"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "CloseWindows"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#GameExe}"; Flags: runhidden; RunOnceId: "StopGame"

[UninstallDelete]
Type: files; Name: "{app}\desktop.json"
Type: files; Name: "{app}\setup-errors.log"

[Code]
var
  UpdatePage: TInputOptionWizardPage;
  ModePage: TInputOptionWizardPage;
  DataDirPage: TInputDirWizardPage;
  PasswordPage: TInputQueryWizardPage;

function IsHost: Boolean;
begin
  Result := ModePage.SelectedValueIndex = 0;
end;

function WantsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function IsUpgrade: Boolean;
begin
  Result := GetPreviousData('DataDir', '') <> '';
end;

{ Updating an existing install with every earlier choice kept: no questions are asked, and the
  password and remote-access setting are left exactly as they are now (they may have been
  changed in the app since Setup last ran). }
function IsUpdate: Boolean;
begin
  Result := IsUpgrade and (UpdatePage.SelectedValueIndex = 0);
end;

function InstalledVersion: String;
begin
  if not RegQueryStringValue(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8B5B8E0E-6C1E-4E57-9B62-6D1C0F3A7A11}_is1', 'DisplayVersion', Result) then
    Result := '';
end;

function OptionsOnCommandLine: Boolean;
begin
  Result := (ExpandConstant('{param:MODE|}') <> '') or (ExpandConstant('{param:DATADIR|}') <> '')
    or (ExpandConstant('{param:PASSWORDFILE|}') <> '') or (ExpandConstant('{param:TASKS|}') <> '');
end;

procedure InitializeWizard;
var
  Installed, Verb: String;
begin
  Installed := InstalledVersion;
  if Installed = '{#AppVersion}' then
    Verb := 'Reinstall version {#AppVersion}'
  else
    Verb := 'Update to version {#AppVersion}';
  if Installed <> '' then
    Installed := 'Version ' + Installed + ' is installed on this PC. '
  else
    Installed := '{#AppName} is already installed on this PC. ';
  UpdatePage := CreateInputOptionPage(wpWelcome,
    '{#AppName} is already installed', '',
    Installed + 'Setup can keep everything as it is: your server, saves, password and settings ' +
    'are not touched, and a running game server keeps running.',
    True, False);
  UpdatePage.Add(Verb + ' and keep my settings');
  UpdatePage.Add('Change the setup options (host or connect only, data folder, password, remote access)');
  if OptionsOnCommandLine then
    UpdatePage.SelectedValueIndex := 1
  else
    UpdatePage.SelectedValueIndex := 0;

  ModePage := CreateInputOptionPage(wpLicense,
    'How will you use this PC?', '',
    'Vein Server Manager can run a game server on this PC, or just be a window onto servers running on other machines.',
    True, False);
  ModePage.Add('Host a Vein server on this PC');
  ModePage.Add('Only connect to servers on other machines');
  { /MODE= on the command line wins over the choice remembered from the last install. }
  if CompareText(ExpandConstant('{param:MODE|' + GetPreviousData('Mode', 'host') + '}'), 'client') = 0 then
    ModePage.SelectedValueIndex := 1
  else
    ModePage.SelectedValueIndex := 0;

  DataDirPage := CreateInputDirPage(wpSelectDir,
    'Server data folder',
    'Where should the game server, saves and backups be stored?',
    'The Vein server needs about 15 GB here. This folder is kept if you uninstall.' + #13#10 +
    'It is locked down so that only the manager''s service and administrators can open it.',
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := GetPreviousData('DataDir', ExpandConstant('{param:DATADIR|{sd}\VeinServerManager}'));

  PasswordPage := CreateInputQueryPage(DataDirPage.ID,
    'Password for remote access (optional)',
    'Needed to manage this server from a web browser or another device.',
    'Administrators of this PC can always open the manager window without a password. ' +
    'Leave both boxes empty to skip; you can set or change the password later under System.');
  PasswordPage.Add('Password (at least 8 characters):', True);
  PasswordPage.Add('Type it again:', True);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  if PageID = UpdatePage.ID then
    Result := not IsUpgrade
  else if IsUpdate then
    { Straight to "Ready to Install": nothing is asked again. }
    Result := (PageID = wpLicense) or (PageID = ModePage.ID) or (PageID = wpSelectDir) or (PageID = DataDirPage.ID)
      or (PageID = PasswordPage.ID) or (PageID = wpSelectTasks)
  else
    Result := ((PageID = DataDirPage.ID) or (PageID = PasswordPage.ID)) and not IsHost;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = PasswordPage.ID then
  begin
    if PasswordPage.Values[0] <> PasswordPage.Values[1] then
    begin
      MsgBox('The two passwords are not the same.', mbError, MB_OK);
      Result := False;
    end
    else if (PasswordPage.Values[0] <> '') and (Length(PasswordPage.Values[0]) < 8) then
    begin
      MsgBox('The password must be at least 8 characters, or left empty.', mbError, MB_OK);
      Result := False;
    end;
  end
  else if (CurPageID = wpSelectTasks) and IsHost and WizardIsTaskSelected('remote')
    and (PasswordPage.Values[0] = '') and (ExpandConstant('{param:PASSWORDFILE|}') = '') and not IsUpgrade then
  begin
    MsgBox('Managing this server from other devices needs a password. Go back and set one, or untick that option.', mbError, MB_OK);
    Result := False;
  end;
end;

{ The "Ready to Install" summary. Inno Setup only knows about its own pages, so the choices made
  on ours (host or connect only, the data folder, whether a password was given) are added here.
  The password itself is never shown. }
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  Password: String;
begin
  Result := '';
  if IsUpdate then
    Result := 'Update:' + NewLine + Space + 'Your server, saves, password and settings are kept as they are.' + NewLine + NewLine;
  Result := Result + 'This PC will:' + NewLine + Space;
  if IsHost then
    Result := Result + 'Host a Vein server'
  else
    Result := Result + 'Only connect to servers on other machines';
  if MemoDirInfo <> '' then
    Result := Result + NewLine + NewLine + MemoDirInfo;
  if IsHost then
  begin
    Result := Result + NewLine + NewLine + 'Server data folder (game server, saves and backups):' + NewLine + Space + DataDirPage.Values[0];
    if not IsUpdate then
    begin
      if (PasswordPage.Values[0] <> '') or (ExpandConstant('{param:PASSWORDFILE|}') <> '') then
        Password := 'Will be set'
      else if IsUpgrade then
        Password := 'Left as it is now'
      else
        Password := 'None: only administrators of this PC can open the manager';
      Result := Result + NewLine + NewLine + 'Password for remote access:' + NewLine + Space + Password;
    end;
  end;
  if (MemoTasksInfo <> '') and not IsUpdate then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataDirPage.Values[0]);
  if IsHost then
    SetPreviousData(PreviousDataKey, 'Mode', 'host')
  else
    SetPreviousData(PreviousDataKey, 'Mode', 'client');
end;

function NeedsVCRuntime: Boolean;
var
  Installed: Cardinal;
begin
  Result := not (RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1));
end;

function NeedsWebView2: Boolean;
var
  Version: String;
begin
  Result := not (
    (RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
      or RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version))
    and (Version <> '') and (Version <> '0.0.0.0'));
end;

{ Stop the manager so its files can be replaced. "net stop" waits until the service has
  really stopped. The game server is left running and is adopted again afterwards, except
  when moving from an older build that ran everything as the installing user: that game
  process belongs to a different account, could not be adopted, and would otherwise end up
  running beside a second copy on the same save. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  ServiceExists: Boolean;
begin
  ServiceExists := Exec(ExpandConstant('{sys}\sc.exe'), 'query {#ServiceName}', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  if ServiceExists and (not IsHost) and FileExists(ExpandConstant('{app}\{#AppExe}')) then
    { Switching to client-only: let the running manager stop the game server (so it can save), then remove the service. }
    Exec(ExpandConstant('{app}\{#AppExe}'), '--uninstall-service', '', SW_HIDE, ewWaitUntilTerminated, Code)
  else if ServiceExists then
    Exec(ExpandConstant('{sys}\net.exe'), 'stop {#ServiceName}', '', SW_HIDE, ewWaitUntilTerminated, Code)
  else
  begin
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN {#ServiceName}', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#GameExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1000);
  Result := '';
end;

procedure WriteSettings;
var
  DataDir, Json: String;
begin
  if IsHost then
  begin
    DataDir := DataDirPage.Values[0];
    ForceDirectories(DataDir);
    StringChangeEx(DataDir, '\', '/', True);
    Json := '{"mode": "service", "data_dir": "' + DataDir + '", "port": 8810}';
  end
  else
    Json := '{"mode": "client"}';
  SaveStringToFile(ExpandConstant('{app}\desktop.json'), Json, False);
end;

function RunHelper(const Params, What: String): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{app}\{#AppExe}'), Params, '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  if not Result then
  begin
    { Params never contain the password: it travels in a file (see below). }
    Log('Helper failed (' + IntToStr(Code) + '): ' + Params);
    SuppressibleMsgBox('Setup could not ' + What + '. Everything else was installed; details are in setup-errors.log in the program folder.',
      mbError, MB_OK, IDOK);
  end;
end;

{ The password is handed to the running service in a file inside Setup's private temp
  folder, which the helper deletes after reading. It is never placed on a command line
  (visible to other processes) or written to the installation log. }
procedure ApplyHostChoices;
var
  PasswordFile, Supplied, Params: String;
begin
  PasswordFile := ExpandConstant('{tmp}\vsm-password.txt');
  Supplied := ExpandConstant('{param:PASSWORDFILE|}');
  if PasswordPage.Values[0] <> '' then
    SaveStringToFile(PasswordFile, PasswordPage.Values[0], False)
  else if Supplied <> '' then
    CopyFile(Supplied, PasswordFile, False)
  else
    SaveStringToFile(PasswordFile, '', False);
  Params := '--apply-setup "' + PasswordFile + '"';
  if WizardIsTaskSelected('remote') then
    Params := Params + ' --allow-remote';
  RunHelper(Params, 'apply the password and remote-access choice');
  DeleteFile(PasswordFile);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    exit;
  if IsHost then
  begin
    if RunHelper('--install-service', 'set up the Vein Server Manager service') and not IsUpdate then
      ApplyHostChoices;
  end
  else
    { Switching a host install to client-only: the service goes, the data folder stays. }
    RunHelper('--uninstall-service', 'remove the previous service');
end;
