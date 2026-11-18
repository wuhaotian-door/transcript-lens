[Setup]
AppId={{ED219424-1E91-48F3-80AA-973CF7EFCCF0}
AppName=成绩透析镜
AppVersion={#AppVersion}
AppPublisher=Transcript Lens
AppPublisherURL=https://github.com/wuhaotian-door/transcript-lens
DefaultDirName={localappdata}\Programs\Transcript Lens
DefaultGroupName=成绩透析镜
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
CreateUninstallRegKey=WizardIsTaskSelected('shell')
MinVersion=10.0
ArchitecturesAllowed=x64compatible
OutputDir={#ReleaseDir}
OutputBaseFilename=成绩透析镜v{#AppVersion}-安装向导
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\transcript-lens.exe
WizardStyle=modern
Compression=lzma2/fast
SolidCompression=yes
CloseApplications=yes
CloseApplicationsFilter=transcript-lens.exe
RestartApplications=no

[Languages]
#ifdef ChineseLanguageFile
Name: "zh"; MessagesFile: "{#ChineseLanguageFile}"
#else
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[CustomMessages]
#ifdef ChineseLanguageFile
zh.ShellIntegration=添加开始菜单快捷方式并登记到 Windows 应用列表
#else
en.ShellIntegration=Add a Start menu shortcut and register in the Windows app list
#endif

[Files]
Source: "{#AppFile}"; DestDir: "{app}"; DestName: "transcript-lens.exe"; Flags: ignoreversion
Source: "{#LicenseFile}"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "shell"; Description: "{cm:ShellIntegration}"; Flags: unchecked
Name: "shell\desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked dontinheritcheck

[Icons]
Name: "{userprograms}\成绩透析镜"; Filename: "{app}\transcript-lens.exe"; WorkingDir: "{app}"; Tasks: shell
Name: "{userdesktop}\成绩透析镜"; Filename: "{app}\transcript-lens.exe"; WorkingDir: "{app}"; Tasks: shell\desktopicon

[Run]
Filename: "{app}\transcript-lens.exe"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,成绩透析镜}"; Flags: nowait postinstall skipifsilent
