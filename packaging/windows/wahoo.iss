; Wahoo LSL installer: Trainer Server + Trainer Dashboard (+ wahoo-worker). Build with build.ps1.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#if AppVersion == ""
  #error AppVersion is empty: build with build.ps1 or pass /DAppVersion=<version>
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
UninstallDisplayIcon={app}\TrainerServer.exe

[Tasks]
Name: "desktopicons"; Description: "Create desktop icons"; Flags: unchecked

[Dirs]
Name: "{commonappdata}\wahoo-lsl"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\wahoo-lsl\logs"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
Source: "..\..\build\pyinstaller\dist\WahooLSL\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\config.example.json"; DestDir: "{commonappdata}\wahoo-lsl"; DestName: "config.json"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\..\ftms.example.json"; DestDir: "{commonappdata}\wahoo-lsl"; DestName: "ftms.json"; Flags: onlyifdoesntexist uninsneveruninstall

[InstallDelete]
; Apps renamed from Wahoo Server / Wahoo Dashboard: remove the old programs and shortcuts on upgrade.
Type: files; Name: "{app}\WahooServer.exe"
Type: files; Name: "{app}\WahooDashboard.exe"
Type: files; Name: "{group}\Wahoo Server.lnk"
Type: files; Name: "{group}\Wahoo Dashboard.lnk"
Type: files; Name: "{autodesktop}\Wahoo Server.lnk"
Type: files; Name: "{autodesktop}\Wahoo Dashboard.lnk"

[Icons]
Name: "{group}\Trainer Server"; Filename: "{app}\TrainerServer.exe"
Name: "{group}\Trainer Dashboard"; Filename: "{app}\TrainerDashboard.exe"
Name: "{autodesktop}\Trainer Server"; Filename: "{app}\TrainerServer.exe"; Tasks: desktopicons
Name: "{autodesktop}\Trainer Dashboard"; Filename: "{app}\TrainerDashboard.exe"; Tasks: desktopicons

[Run]
; LSL: let LabRecorder on other lab PCs (private network) see the stream without a firewall prompt.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Wahoo LSL worker"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Wahoo LSL worker"" dir=in action=allow program=""{app}\wahoo-worker.exe"" profile=private enable=yes"; Flags: runhidden

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Wahoo LSL worker"""; Flags: runhidden; RunOnceId: "DeleteFirewallRule"
