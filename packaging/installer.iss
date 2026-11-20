#define AppVersion GetEnv("BOARDVIEWER_APP_VERSION")
#define NumericVersion GetEnv("BOARDVIEWER_NUMERIC_VERSION")
#define PayloadDir GetEnv("BOARDVIEWER_PAYLOAD_DIR")
#define InstallerOutputDir GetEnv("BOARDVIEWER_INSTALLER_OUTPUT_DIR")
#define InstallerBaseName GetEnv("BOARDVIEWER_INSTALLER_BASENAME")
#define SetupIcon GetEnv("BOARDVIEWER_ICON_PATH")

#if AppVersion == ""
  #error BOARDVIEWER_APP_VERSION is required
#endif
#if PayloadDir == ""
  #error BOARDVIEWER_PAYLOAD_DIR is required
#endif
#if NumericVersion == ""
  #error BOARDVIEWER_NUMERIC_VERSION is required
#endif
#if InstallerOutputDir == ""
  #error BOARDVIEWER_INSTALLER_OUTPUT_DIR is required
#endif
#if InstallerBaseName == ""
  #error BOARDVIEWER_INSTALLER_BASENAME is required
#endif
#if SetupIcon == ""
  #error BOARDVIEWER_ICON_PATH is required
#endif

[Setup]
AppId={{A7A2E492-C1EF-47AD-A634-84454A32A2A1}
AppName=Thermetery Boardviewer
AppVersion={#AppVersion}
AppPublisher=Thermetery Technology LLC
AppPublisherURL=https://github.com/jiacheng-thermetery/thermetery-boardview
AppSupportURL=https://github.com/jiacheng-thermetery/thermetery-boardview/issues
AppUpdatesURL=https://github.com/jiacheng-thermetery/thermetery-boardview/releases
VersionInfoVersion={#NumericVersion}
VersionInfoCompany=Thermetery Technology LLC
VersionInfoDescription=Thermetery Boardviewer installer
VersionInfoProductName=Thermetery Boardviewer
DefaultDirName={localappdata}\Programs\Thermetery Boardviewer
DefaultGroupName=Thermetery Boardviewer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#InstallerOutputDir}
OutputBaseFilename={#InstallerBaseName}
SetupIconFile={#SetupIcon}
LicenseFile={#PayloadDir}\licenses\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern dynamic
SetupLogging=yes
UninstallDisplayIcon={app}\ThermeteryBoardviewer.exe
CloseApplications=yes
RestartApplications=no
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Thermetery Boardviewer"; Filename: "{app}\ThermeteryBoardviewer.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Thermetery Boardviewer"; Filename: "{app}\ThermeteryBoardviewer.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Register per-user, preserving existing defaults (including system defaults).
Root: HKCU; Subkey: "Software\Classes\ThermeteryBoardviewer.Board"; ValueType: string; ValueData: "Thermetery Boardviewer board"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ThermeteryBoardviewer.Board\DefaultIcon"; ValueType: string; ValueData: "{app}\ThermeteryBoardviewer.exe,0"
Root: HKCU; Subkey: "Software\Classes\ThermeteryBoardviewer.Board\shell\open\command"; ValueType: string; ValueData: """{app}\ThermeteryBoardviewer.exe"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\.cad"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.cad')
Root: HKCU; Subkey: "Software\Classes\.cad\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.brd"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.brd')
Root: HKCU; Subkey: "Software\Classes\.brd\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.brd2"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.brd2')
Root: HKCU; Subkey: "Software\Classes\.brd2\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.bv"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.bv')
Root: HKCU; Subkey: "Software\Classes\.bv\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.tvw"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.tvw')
Root: HKCU; Subkey: "Software\Classes\.tvw\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.fz"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.fz')
Root: HKCU; Subkey: "Software\Classes\.fz\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.pcb"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.pcb')
Root: HKCU; Subkey: "Software\Classes\.pcb\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.asc"; ValueType: string; ValueData: "ThermeteryBoardviewer.Board"; Flags: createvalueifdoesntexist; Check: CanAssociate('.asc')
Root: HKCU; Subkey: "Software\Classes\.asc\OpenWithProgids"; ValueType: string; ValueName: "ThermeteryBoardviewer.Board"; ValueData: ""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\ThermeteryBoardviewer.exe"; Description: "Launch Thermetery Boardviewer"; Flags: nowait postinstall skipifsilent

[Code]
function CanAssociate(Ext: String): Boolean;
var
  Current: String;
begin
  Result := not RegQueryStringValue(HKCR, Ext, '', Current) or
            (Current = '') or (Current = 'ThermeteryBoardviewer.Board');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  I: Integer;
  Exts: array[0..7] of String;
  Key, Current: String;
begin
  if CurUninstallStep = usUninstall then begin
    Exts[0] := '.cad'; Exts[1] := '.brd'; Exts[2] := '.brd2';
    Exts[3] := '.bv'; Exts[4] := '.tvw'; Exts[5] := '.fz';
    Exts[6] := '.pcb'; Exts[7] := '.asc';
    for I := 0 to 7 do begin
      Key := 'Software\Classes\' + Exts[I];
      if RegQueryStringValue(HKCU, Key, '', Current) and
         (Current = 'ThermeteryBoardviewer.Board') then
        RegDeleteValue(HKCU, Key, '');
    end;
  end;
end;

