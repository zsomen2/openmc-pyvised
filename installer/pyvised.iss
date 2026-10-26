; Inno Setup 6 script of the PyVised installer (UTF-8 with BOM: keep the BOM for the accents).
; `just installer` builds it from the PyInstaller folder dist\PyVised:
;   ISCC /DAppVersion=1.0.0 /DNumericVersion=1.0.0.0 installer\pyvised.iss
; Per-user install by default; all users with the dialog or /ALLUSERS. Silent lab install:
;   PyVised-Setup-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ALLUSERS /MERGETASKS="lab"
; (see installer\LAB_DEPLOYMENT.md). The OpenMC backend image is not in the installer: the app
; downloads it from ghcr.io at its first start.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=1.0.0 /DNumericVersion=1.0.0.0 pyvised.iss
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0.0"
#endif

#define AppName "PyVised"
#define AppExe "PyVised.exe"
#define Repository "https://github.com/zsomen2/openmc-pyvised"
; drawn from the logo by packaging\wizard_images.py (transparent: also for the dark wizard)
#define Images "..\build\installer"
#define LargeImages Images + "\wizard-202.png," + Images + "\wizard-269.png," + Images + "\wizard-336.png," + Images + "\wizard-430.png"
#define SmallImages Images + "\wizard-small-58.png," + Images + "\wizard-small-77.png," + Images + "\wizard-small-97.png," + Images + "\wizard-small-124.png," + Images + "\wizard-small-159.png"

[Setup]
; the AppId identifies PyVised for upgrades and uninstall: never change it
AppId={{B4F1FF0C-E5E3-4E9A-B3EB-6C94077A010A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=zsomen2
AppPublisherURL={#Repository}
AppSupportURL={#Repository}/issues
AppUpdatesURL={#Repository}/releases
AppCopyright=GNU GPL v3 or later
VersionInfoVersion={#NumericVersion}
VersionInfoProductTextVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Docker Desktop needs Windows 10 22H2 or Windows 11
MinVersion=10.0.19045
WizardStyle=modern dynamic
WizardImageFile={#LargeImages}
WizardImageFileDynamicDark={#LargeImages}
WizardSmallImageFile={#SmallImages}
WizardSmallImageFileDynamicDark={#SmallImages}
SetupIconFile=..\app\assets\pyvised_app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\dist
OutputBaseFilename=PyVised-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
; the names are PyVised's language codes: the app starts in the installer's language
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "hu"; MessagesFile: "compiler:Languages\Hungarian.isl"

[CustomMessages]
en.LabGroup=Lab PCs:
hu.LabGroup=Laborgépek:
en.LabTask=Shared lab PC: ask every user for the workspace folder at start-up
hu.LabTask=Közös laborgép: indításkor kérdezze meg a munkaterület mappáját
en.DockerTitle=Docker Desktop is needed
hu.DockerTitle=Szükség van a Docker Desktopra
en.DockerSubtitle=PyVised runs OpenMC in a Docker container.
hu.DockerSubtitle=A PyVised az OpenMC-t Docker-konténerben futtatja.
en.DockerText=Docker Desktop was not found on this PC. PyVised installs anyway, but it needs Docker Desktop (free for education) to open models and run simulations.%n%nInstall Docker Desktop, start it once and accept its terms. At its first start PyVised then downloads its OpenMC backend (about 1 GB, once per PC).
hu.DockerText=A Docker Desktop nem található ezen a gépen. A PyVised enélkül is települ, de a modellek megnyitásához és a szimulációkhoz szüksége van a Docker Desktopra (oktatási célra ingyenes).%n%nTelepítse a Docker Desktopot, indítsa el egyszer, és fogadja el a feltételeit. A PyVised az első indításkor letölti az OpenMC háttérszolgáltatást (kb. 1 GB, gépenként egyszer).
en.DockerLink=Open the Docker Desktop download page
hu.DockerLink=A Docker Desktop letöltőoldalának megnyitása

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "lab"; Description: "{cm:LabTask}"; GroupDescription: "{cm:LabGroup}"; Flags: unchecked; Check: IsAdminInstallMode

[InstallDelete]
; the files of the previous version: library names change between builds
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\PyVised\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "PyVised.Desktop"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "PyVised.Desktop"; Tasks: desktopicon

[INI]
; Defaults for every user of the PC (all-users installs): Qt reads them from
; %PROGRAMDATA%\PyVised\PyVised.ini for settings the user has not changed.
Filename: "{commonappdata}\PyVised\PyVised.ini"; Section: "%General"; Key: "language"; String: "{language}"; Flags: uninsdeleteentry; Check: IsAdminInstallMode
Filename: "{commonappdata}\PyVised\PyVised.ini"; Section: "%General"; Key: "ask_workspace_at_startup"; String: "true"; Flags: uninsdeleteentry; Tasks: lab
Filename: "{commonappdata}\PyVised\PyVised.ini"; Section: "%General"; Key: "reopen_last_model"; String: "false"; Flags: uninsdeleteentry; Tasks: lab
; a per-user install sets the language only in a new settings file (it never edits the user's)
Filename: "{userappdata}\PyVised\PyVised.ini"; Section: "%General"; Key: "language"; String: "{language}"; Check: NewUserSettings

[UninstallDelete]
Type: dirifempty; Name: "{commonappdata}\PyVised"

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  DockerDesktopKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop';
  DockerDesktopUrl = 'https://docs.docker.com/desktop/setup/install/windows-install/';

var
  DockerPage: TOutputMsgWizardPage;
  UserSettingsExisted: Boolean;

{ Docker Desktop registers in HKCU when installed per user, in HKLM for all users. }
function DockerDesktopInstalled: Boolean;
var
  Location: String;
begin
  Result := RegQueryStringValue(HKCU, DockerDesktopKey, 'InstallLocation', Location)
    or RegQueryStringValue(HKLM, DockerDesktopKey, 'InstallLocation', Location)
    or FileExists(ExpandConstant('{commonpf64}\Docker\Docker\Docker Desktop.exe'))
    or FileExists(ExpandConstant('{localappdata}\Programs\DockerDesktop\Docker Desktop.exe'));
end;

function NewUserSettings: Boolean;
begin
  Result := not IsAdminInstallMode and not UserSettingsExisted;
end;

procedure OpenDockerDownload(Sender: TObject);
var
  ErrorCode: Integer;
begin
  ShellExecAsOriginalUser('open', DockerDesktopUrl, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure InitializeWizard;
var
  Button: TNewButton;
begin
  UserSettingsExisted := FileExists(ExpandConstant('{userappdata}\PyVised\PyVised.ini'));
  DockerPage := CreateOutputMsgPage(wpWelcome, CustomMessage('DockerTitle'),
    CustomMessage('DockerSubtitle'), CustomMessage('DockerText'));
  Button := TNewButton.Create(DockerPage);
  Button.Parent := DockerPage.Surface;
  Button.Caption := CustomMessage('DockerLink');
  Button.Left := 0;
  Button.Top := DockerPage.MsgLabel.Top + DockerPage.MsgLabel.Height + ScaleY(16);
  Button.Width := ScaleX(300);
  Button.Height := ScaleY(26);
  Button.OnClick := @OpenDockerDownload;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
#ifdef ShowDockerPage
  { ISCC /DShowDockerPage: the page also with Docker installed (for screenshots) }
  Result := False;
#else
  Result := (PageID = DockerPage.ID) and DockerDesktopInstalled;
#endif
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not DockerDesktopInstalled then
    Log('Docker Desktop was not found: PyVised needs it to run OpenMC.');
end;
