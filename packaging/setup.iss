#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{9C4B9820-17EE-445B-9F1B-A6C7D0428A32}
AppName=YT Downloader
AppVersion={#AppVersion}
AppPublisher=YT Downloader
AppPublisherURL=https://github.com/pr3ston1989/YT_Downloader
DefaultDirName={localappdata}\Programs\YTDownloader
DefaultGroupName=YT Downloader
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\dist
OutputBaseFilename=YTDownloader-Setup
SetupIconFile=..\app.ico
UninstallDisplayIcon={app}\YTDownloader.exe
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\YTDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installed.marker"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\YT Downloader"; Filename: "{app}\YTDownloader.exe"
Name: "{userdesktop}\YT Downloader"; Filename: "{app}\YTDownloader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\YTDownloader.exe"; Description: "{cm:LaunchProgram,YT Downloader}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "YTDownloader"; Flags: uninsdeletevalue dontcreatekey
