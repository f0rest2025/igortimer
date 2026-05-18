#ifndef AppVersion
#define AppVersion "0.1.0"
#endif

[Setup]
AppId={{73A3485A-CA67-4B26-9277-37E8C1085F1E}
AppName=igortimer
AppVersion={#AppVersion}
AppPublisher=f0rest2025
DefaultDirName={localappdata}\Programs\igortimer
DefaultGroupName=igortimer
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=igortimer-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\igortimer.exe
SetupIconFile=..\logo.ico

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "..\dist\igortimer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\igortimer"; Filename: "{app}\igortimer.exe"
Name: "{autodesktop}\igortimer"; Filename: "{app}\igortimer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\igortimer.exe"; Description: "Запустить igortimer"; Flags: nowait postinstall skipifsilent
