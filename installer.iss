; Inno Setup script for Net Split-Tunneler — per-user, no-admin installer.
;
; Build (after PyInstaller produces dist\NetSplitTunnel_v<ver>.exe):
;   ISCC /DAppVersion=4.9.2 installer.iss
; Output: Output\NetSplitTunnel_Setup_v<ver>.exe
;
; Per-user (PrivilegesRequired=lowest) means no admin/UAC to install or update.
; CloseApplications + RestartApplications + AppMutex let a silent self-update
; (run with /VERYSILENT by nst.updater) close the running app, swap the exe and
; relaunch it.

#ifndef AppVersion
  #define AppVersion "4.9.3"
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
RestartApplications=yes

[Tasks]
Name: "autostart"; Description: "Start {#AppName} automatically when Windows starts"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The versioned PyInstaller exe is installed under a stable name so shortcuts,
; the Run key and the updater don't change between versions.
Source: "dist\NetSplitTunnel_v{#AppVersion}.exe"; DestDir: "{app}"; DestName: "{#ExeName}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Registry]
; HKCU Run-key autostart (the app's in-app toggle manages the same value).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "NetSplitTunnel"; \
    ValueData: """{app}\{#ExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Offer to launch after an interactive install; skipped during silent self-update
; (there, RestartApplications relaunches the app instead).
Filename: "{app}\{#ExeName}"; Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent
