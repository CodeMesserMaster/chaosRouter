; chaosRouter Inno Setup installer script
; Compile after running build_windows.ps1 (needs dist\chaosRouter\)

[Setup]
AppName=chaosRouter
AppVersion=0.1.0
AppPublisher=CodeMesserMaster
DefaultDirName={autopf}\chaosRouter
DefaultGroupName=chaosRouter
OutputBaseFilename=chaosRouter-0.1.0-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes

[Files]
Source: "..\dist\chaosRouter\*"; DestDir: "{app}"; Flags: recursesubdirs

[Icons]
Name: "{group}\chaosRouter"; Filename: "{app}\chaosRouter.exe"
Name: "{autodesktop}\chaosRouter"; Filename: "{app}\chaosRouter.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a &desktop icon"; Flags: unchecked

[Run]
Filename: "{app}\chaosRouter.exe"; Description: "Launch chaosRouter"; Flags: postinstall nowait skipifsilent
