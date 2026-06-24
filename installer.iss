; Inno Setup script for Net Split-Tunneler — per-user, no-admin installer.
;
; Build (after PyInstaller produces the onedir folder dist\NetSplitTunnel\):
;   ISCC /DAppVersion=4.9.5 installer.iss
; Output: Output\NetSplitTunnel_Setup_v<ver>.exe
;
; Per-user (PrivilegesRequired=lowest) means no admin/UAC to install or update.
; A silent self-update (run with /VERYSILENT by nst.updater) closes the running
; app, replaces the folder and relaunches the new version via [Run].

#ifndef AppVersion
  #define AppVersion "4.12.6"
#endif
#define AppName "Net Split-Tunneler"
#define ExeName "NetSplitTunnel.exe"

[Setup]
; Stable AppId — never change this; it lets new versions upgrade in place.
AppId={{A7E3F2C1-9B4D-4E6A-8F12-3C5D7E9A1B2C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher=Hackers-lab
DefaultDirName={localappdata}\Programs\NetSplitTunnel
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=NetSplitTunnel_Setup_v{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#ExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Detect/close the running instance via its single-instance mutex.
AppMutex=NetSplitTunnel_SingleInstance_Mutex_3248
CloseApplications=yes
; Relaunch is handled by the [Run] entries below (works for silent self-updates
; too). Restart Manager restart is disabled to avoid a double launch.
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start {#AppName} automatically when Windows starts"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; One-folder (onedir) PyInstaller build — copy the whole folder. The exe is
; already named NetSplitTunnel.exe so shortcuts/Run key/updater stay stable.
Source: "dist\NetSplitTunnel\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Registry]
; HKCU Run-key autostart. The --autostart flag makes the app start to the tray
; (no main window) at logon. The in-app toggle manages the same value.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "NetSplitTunnel"; \
    ValueData: """{app}\{#ExeName}"" --autostart"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Interactive install: offer "Launch now" (opens the main window normally).
Filename: "{app}\{#ExeName}"; Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent
; Silent self-update: relaunch hidden (tray only) and announce the new version
; via a toast. Runs only when Setup is silent (i.e. driven by the updater).
Filename: "{app}\{#ExeName}"; Parameters: "--updated={#AppVersion}"; \
    Flags: nowait postinstall; Check: WizardSilent
