; Inno Setup script for Kremzer Péter ModbusTCP Windows installer.
;
; Prerequisites:
;   * scripts\build_exe.ps1 must have produced dist\ModbusSimulator\
;   * Inno Setup 6+ installed (`winget install JRSoftware.InnoSetup`)
;
; Build: from the repo root, run:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\installer.iss
;
; Output: dist\KremzerPeterModbusTCP-Setup-<version>.exe

#define MyAppName "Kremzer Peter ModbusTCP"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Kremzer Peter"
#define MyAppExeName "ModbusSimulator.exe"

[Setup]
AppId={{B41A9F1E-5C8D-4A7F-9E2C-MODBUSSIM01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=KremzerPeterModbusTCP-Setup-{#MyAppVersion}
SetupIconFile=..\resources\icons\AppIcon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "hungarian"; MessagesFile: "compiler:Languages\Hungarian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Pull every file under dist\ModbusSimulator\ that PyInstaller emitted.
Source: "..\dist\ModbusSimulator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
