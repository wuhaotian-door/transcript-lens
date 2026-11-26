[Setup]
AppId={{ED219424-1E91-48F3-80AA-973CF7EFCCF0}
AppName=朝夕工作台
AppVersion={#AppVersion}
AppPublisher=朝夕工作台
AppPublisherURL=https://github.com/wuhaotian-door/transcript-lens
DefaultDirName={localappdata}\Programs\Transcript Lens
DefaultGroupName=朝夕工作台
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
CreateUninstallRegKey=WizardIsTaskSelected('shell')
MinVersion=10.0
ArchitecturesAllowed=x64compatible
OutputDir={#ReleaseDir}
OutputBaseFilename=朝夕工作台-{#AppVersion}-安装向导
SetupIconFile={#AppIcon}
WizardImageFile={#WizardBrand}
WizardSmallImageFile={#WizardSmallBrand}
WizardImageStretch=no
WizardImageBackColor=$3B2610
UninstallDisplayIcon={app}\ZhaoxiWorkbench-brand.ico
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
zh.OpenAfterSetup=点击「完成」退出安装向导。之后可打开下方的程序，或使用安装时添加的开始菜单快捷方式。
#else
en.ShellIntegration=Add a Start menu shortcut and register in the Windows app list
en.OpenAfterSetup=Click Finish to close Setup. Then open the program below, or use the Start menu shortcut if you added one.
#endif

[Files]
Source: "{#AppIcon}"; DestDir: "{app}"; DestName: "ZhaoxiWorkbench-brand.ico"; Flags: ignoreversion
Source: "{#AppFile}"; DestDir: "{app}"; DestName: "transcript-lens.exe"; Flags: ignoreversion
Source: "{#LicenseFile}"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "shell"; Description: "{cm:ShellIntegration}"; Flags: unchecked
Name: "shell\desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked dontinheritcheck

[InstallDelete]
; 覆盖升级时清理两个历史品牌的快捷方式。
Type: files; Name: "{userprograms}\成绩单透镜.lnk"
Type: files; Name: "{userdesktop}\成绩单透镜.lnk"
Type: files; Name: "{userprograms}\成绩透析镜.lnk"
Type: files; Name: "{userdesktop}\成绩透析镜.lnk"
Type: files; Name: "{userprograms}\成绩单透镜\成绩单透镜.lnk"
Type: files; Name: "{userprograms}\成绩透析镜\成绩透析镜.lnk"

[Icons]
Name: "{userprograms}\朝夕工作台"; Filename: "{app}\transcript-lens.exe"; WorkingDir: "{app}"; IconFilename: "{app}\ZhaoxiWorkbench-brand.ico"; Tasks: shell
Name: "{userdesktop}\朝夕工作台"; Filename: "{app}\transcript-lens.exe"; WorkingDir: "{app}"; IconFilename: "{app}\ZhaoxiWorkbench-brand.ico"; Tasks: shell\desktopicon

[Code]
procedure NotifyShellItem(EventId: LongWord; Flags: LongWord; Path: String; Reserved: LongWord);
  external 'SHChangeNotify@shell32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { SHCNE_UPDATEITEM, SHCNF_PATHW }
    NotifyShellItem($00002000, $0005, ExpandConstant('{app}\transcript-lens.exe'), 0);
    NotifyShellItem($00002000, $0005, ExpandConstant('{app}\ZhaoxiWorkbench-brand.ico'), 0);
    if FileExists(ExpandConstant('{userprograms}\朝夕工作台.lnk')) then
      NotifyShellItem($00002000, $0005, ExpandConstant('{userprograms}\朝夕工作台.lnk'), 0);
    if FileExists(ExpandConstant('{userdesktop}\朝夕工作台.lnk')) then
      NotifyShellItem($00002000, $0005, ExpandConstant('{userdesktop}\朝夕工作台.lnk'), 0);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and not WizardSilent then
  begin
    WizardForm.FinishedLabel.Caption :=
      CustomMessage('OpenAfterSetup') + #13#10 + #13#10 +
      ExpandConstant('{app}\transcript-lens.exe');
    WizardForm.AdjustLabelHeight(WizardForm.FinishedLabel);
  end;
end;
