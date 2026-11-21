; D Studio - Inno Setup 6 script (per-user, no admin).
; Compiled by installer\build_windows.ps1, which passes:
;   /DAppVersion=0.2.0 /DAppVersionNumeric=0.2.0 /DSourceDir=<staged app folder>
;   /DOutputDir=<dist> /DZhIsl=<ChineseSimplified.isl>
; This file is UTF-8 with a BOM (it contains Chinese text) - keep it that way.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric AppVersion
#endif
#ifndef SourceDir
  #error SourceDir is not defined - run installer\build_windows.ps1
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef ZhIsl
  #define ZhIsl "lang\ChineseSimplified.isl"
#endif

#define AppName "D Studio"
#define AppPublisher "D Studio"
#define PyW "{app}\python\pythonw.exe"
#define Py "{app}\python\python.exe"
; -E -s: ignore PYTHONHOME/PYTHONPATH and user site-packages of other Python
; installs on the user's PC (CAD suites often set them globally)
#define PyFlags "-E -s"

[Setup]
; Fixed AppId: every version upgrades the same installation in place. Never change it.
AppId={{6E0B5F2C-8D3A-4B7E-9C21-5A4F7D1E3B90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={localappdata}\Programs\DStudio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
; Per-user app with a fixed folder: no 'Select Destination' page. A typed-in
; existing folder (D:\Tools...) could hold the user's own python\, docs\,
; tools\ that [InstallDelete]/[UninstallDelete] would remove.
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=DStudio-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\dstudio.ico
UninstallDisplayIcon={app}\dstudio.ico
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=2
WizardStyle=modern
ShowLanguageDialog=auto
LanguageDetectionMethod=uilanguage
; the recorder bar holds this mutex while it runs (recorder/gui.py acquire_single_instance)
AppMutex=DStudio.RecorderBar
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
SetupLogging=yes
ChangesEnvironment=no
ChangesAssociations=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "zh"; MessagesFile: "{#ZhIsl}"

[CustomMessages]
en.AnnotatorName=D Studio Annotator
zh.AnnotatorName=D Studio 标注台
en.SelfTestName=D Studio Self-test
zh.SelfTestName=D Studio 自检
en.RecordingsName=D Studio Recordings
zh.RecordingsName=D Studio 录制文件夹
en.AppComment=Record the screen and keyboard/mouse in the Markov computer-use format
zh.AppComment=录制屏幕与键鼠操作（Markov computer-use 数据格式）
en.AnnotatorComment=Review recordings and write narration.json in the browser
zh.AnnotatorComment=在浏览器中回看录制并标注 narration.json
en.SelfTestComment=Check Python, FFmpeg, screen capture and the recordings folder
zh.SelfTestComment=检查 Python、FFmpeg、屏幕采集和录制文件夹
en.RecordingsComment=Open the folder that holds your D Studio recordings
zh.RecordingsComment=打开 D Studio 录制文件所在的文件夹
en.RecordingsKept=D Studio was removed. Your recordings were kept in:%n%1
zh.RecordingsKept=D Studio 已卸载。您的录制文件已保留在：%n%1

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Upgrades: remove the previous version's code first so no stale module (e.g. an
; older numpy file) survives next to the new one. Recordings are never here
; (installed mode writes them to Documents\DStudio or the folder chosen in
; settings). Check: IsOurInstall - only inside a folder that is really ours.
Type: filesandordirs; Name: "{app}\python"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\ffmpeg"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\recorder"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\packager"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\viewer"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\tools"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\schemas"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\docs"; Check: IsOurInstall
; shortcuts created by an install in the other language
Type: files; Name: "{group}\D Studio Annotator.lnk"
Type: files; Name: "{group}\D Studio 标注台.lnk"
Type: files; Name: "{group}\D Studio Self-test.lnk"
Type: files; Name: "{group}\D Studio 自检.lnk"
Type: files; Name: "{group}\D Studio Recordings.lnk"
Type: files; Name: "{group}\D Studio 录制文件夹.lnk"

[Dirs]
; default recordings folder (recorder/paths.py: Documents\DStudio when INSTALLED exists)
Name: "{userdocs}\DStudio"; Flags: uninsneveruninstall

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{#PyW}"; Parameters: "{#PyFlags} ""{app}\dstudio.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\dstudio.ico"; Comment: "{cm:AppComment}"; AppUserModelID: "DStudio.Recorder"
Name: "{group}\{cm:AnnotatorName}"; Filename: "{#PyW}"; Parameters: "{#PyFlags} ""{app}\dstudio.pyw"" --viewer"; WorkingDir: "{app}"; IconFilename: "{app}\dstudio.ico"; Comment: "{cm:AnnotatorComment}"; AppUserModelID: "DStudio.Annotator"
Name: "{group}\{cm:SelfTestName}"; Filename: "{#Py}"; Parameters: "{#PyFlags} ""{app}\dstudio.pyw"" --selftest"; WorkingDir: "{app}"; IconFilename: "{app}\dstudio.ico"; Comment: "{cm:SelfTestComment}"
; opens recorder.paths.data_dir() (honours a recordings folder moved in settings)
Name: "{group}\{cm:RecordingsName}"; Filename: "{#PyW}"; Parameters: "{#PyFlags} ""{app}\dstudio.pyw"" --open-data"; WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 3; Comment: "{cm:RecordingsComment}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{#PyW}"; Parameters: "{#PyFlags} ""{app}\dstudio.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\dstudio.ico"; Comment: "{cm:AppComment}"; AppUserModelID: "DStudio.Recorder"; Tasks: desktopicon

[Run]
Filename: "{#PyW}"; Parameters: "{#PyFlags} ""{app}\dstudio.pyw"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; files Python may have written at runtime inside the program folder; the
; recordings folder (Documents\DStudio or settings data_dir) and settings
; (%APPDATA%\DStudio) are kept. Check: IsOurInstall (decided before anything
; is copied or removed).
Type: filesandordirs; Name: "{app}\python"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\ffmpeg"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\recorder"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\packager"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\viewer"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\tools"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\schemas"; Check: IsOurInstall
Type: filesandordirs; Name: "{app}\docs"; Check: IsOurInstall
Type: dirifempty; Name: "{app}"

[Code]
// ---------------------------------------------------------------- folder guard
// Decided once, before Setup deletes or copies anything (and at the start of
// uninstall, while our INSTALLED marker still exists): the program folder is
// ours when it has the INSTALLED marker, or none of the folders that
// [InstallDelete]/[UninstallDelete] remove. A foreign folder that happens to
// contain python\, docs\, tools\... is never touched.
var
  OwnDirDecided: Boolean;
  OwnDir: Boolean;

function IsOurInstall: Boolean;
var
  App: String;
begin
  if not OwnDirDecided then
  begin
    App := ExpandConstant('{app}');
    OwnDir := FileExists(App + '\INSTALLED') or not (
      DirExists(App + '\python') or DirExists(App + '\ffmpeg') or DirExists(App + '\recorder') or
      DirExists(App + '\packager') or DirExists(App + '\viewer') or DirExists(App + '\tools') or
      DirExists(App + '\schemas') or DirExists(App + '\docs'));
    OwnDirDecided := True;
    if OwnDir then
      Log('D Studio program folder is ours: ' + App)
    else
      Log('Foreign folder, nothing will be deleted in it: ' + App);
  end;
  Result := OwnDir;
end;

function InitializeUninstall: Boolean;
var
  Ours: Boolean;
begin
  Ours := IsOurInstall;
  Result := True;
end;

// ---------------------------------------------------------------- recordings folder
// D Studio's settings.json is UTF-8 (json.dump ensure_ascii=False).
const
  CP_UTF8 = 65001;

function MultiByteToWideChar(CodePage: Cardinal; dwFlags: Cardinal; const lpMultiByteStr: AnsiString;
  cchMultiByte: Integer; lpWideCharStr: String; cchWideChar: Integer): Integer;
  external 'MultiByteToWideChar@kernel32.dll stdcall';

function Utf8ToStr(const S: AnsiString): String;
var
  Len: Integer;
begin
  Result := '';
  if Length(S) = 0 then
    Exit;
  Len := MultiByteToWideChar(CP_UTF8, 0, S, Length(S), Result, 0);
  if Len <= 0 then
    Exit;
  SetLength(Result, Len);
  MultiByteToWideChar(CP_UTF8, 0, S, Length(S), Result, Len);
end;

// "data_dir" from %APPDATA%\DStudio\settings.json ('' when unset or unreadable).
function SettingsDataDir: String;
var
  Raw: AnsiString;
  Json, Value, C: String;
  I, N: Integer;
begin
  Result := '';
  if not LoadStringFromFile(ExpandConstant('{userappdata}\DStudio\settings.json'), Raw) then
    Exit;
  Json := Utf8ToStr(Raw);
  I := Pos('"data_dir"', Json);
  if I = 0 then
    Exit;
  I := I + Length('"data_dir"');
  N := Length(Json);
  while (I <= N) and ((Json[I] = ' ') or (Json[I] = ':') or (Json[I] = #9) or (Json[I] = #13) or (Json[I] = #10)) do
    I := I + 1;
  if (I > N) or (Json[I] <> '"') then
    Exit;
  I := I + 1;
  Value := '';
  while I <= N do
  begin
    C := Json[I];
    if C = '"' then
    begin
      Result := Trim(Value);
      Exit;
    end;
    if C = '\' then
    begin
      I := I + 1;
      if I > N then
        Exit;
      C := Json[I];
      // only \\ \/ \" occur in a Windows path; anything else: use the default folder
      if (C <> '\') and (C <> '/') and (C <> '"') then
        Exit;
    end;
    Value := Value + C;
    I := I + 1;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DocsDir, Folders: String;
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    DocsDir := ExpandConstant('{userdocs}\DStudio');
    Folders := RemoveBackslashUnlessRoot(SettingsDataDir);
    if Folders = '' then
      Folders := DocsDir
    else if (CompareText(Folders, DocsDir) <> 0) and DirExists(DocsDir) then
      Folders := Folders + #13#10 + DocsDir;
    MsgBox(FmtMessage(CustomMessage('RecordingsKept'), [Folders]), mbInformation, MB_OK);
  end;
end;
