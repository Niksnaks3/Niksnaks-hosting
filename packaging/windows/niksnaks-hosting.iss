#define AppName "Niksnaks-Hosting"
#define AppPublisher "Sugarycandybar"
#define AppURL ""
#define AppExeName "Niksnaks-Hosting.exe"

#ifndef AppVersion
  #define FileHandle FileOpen("..\..\niksnaks_hosting\version.py")
  #if FileHandle
    #define FileLine FileRead(FileHandle)
    #expr FileClose(FileHandle)
    #define Quote1Pos Pos('"', FileLine)
    #define TempStr Copy(FileLine, Quote1Pos + 1, Len(FileLine) - Quote1Pos)
    #define Quote2Pos Pos('"', TempStr)
    #define AppVersion Copy(TempStr, 1, Quote2Pos - 1)
  #else
    #define AppVersion "1.0.0"
  #endif
#endif

[Setup]
AppId={{E1CA2F67-27C8-4DE3-A1F0-FA8B2EB4B127}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppMutex=com.niksnakshosting.NiksnaksHosting
DefaultDirName={autopf}\{#AppName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=Niksnaks-Hosting-{#AppVersion}-Setup
SetupIconFile=..\..\build\Niksnaks-Hosting.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\Niksnaks-Hosting\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
