; Wahoo LSL installer: Wahoo Server + Wahoo Dashboard (+ wahoo-worker). Build with build.ps1.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{072F395A-88CF-400F-935E-D3EE050E6F75}
AppName=Wahoo LSL
AppVersion={#AppVersion}
DefaultDirName={autopf}\Wahoo LSL
DefaultGroupName=Wahoo LSL
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\build\installer
OutputBaseFilename=WahooLSL-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\WahooServer.exe

[Tasks]
Name: "desktopicons"; Description: "Create desktop icons"; Flags: unchecked

[Dirs]
Name: "{commonappdata}\wahoo-lsl"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\wahoo-lsl\logs"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
Source: "..\..\build\pyinstaller\dist\WahooLSL\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\config.example.json"; DestDir: "{commonappdata}\wahoo-lsl"; DestName: "config.json"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\..\ftms.example.json"; DestDir: "{commonappdata}\wahoo-lsl"; DestName: "ftms.json"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\Wahoo Server"; Filename: "{app}\WahooServer.exe"
Name: "{group}\Wahoo Dashboard"; Filename: "{app}\WahooDashboard.exe"
Name: "{autodesktop}\Wahoo Server"; Filename: "{app}\WahooServer.exe"; Tasks: desktopicons
Name: "{autodesktop}\Wahoo Dashboard"; Filename: "{app}\WahooDashboard.exe"; Tasks: desktopicons

[Run]
; LSL: let LabRecorder on other lab PCs (private network) see the stream without a firewall prompt.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Wahoo LSL worker"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Wahoo LSL worker"" dir=in action=allow program=""{app}\wahoo-worker.exe"" profile=private enable=yes"; Flags: runhidden

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Wahoo LSL worker"""; Flags: runhidden; RunOnceId: "DeleteFirewallRule"
