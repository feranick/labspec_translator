; ls6t.nsi - the Windows installer of labspec-translator (ls6t). Built by installer/build_installer.py:
;   makensis /DVERSION=<version> /DSTAGE=<staged files> /DOUTFILE=<setup exe> ls6t.nsi
; STAGE holds python\ (the embeddable Python with every package in Lib\site-packages), bin\ (launchers), docs\,
; examples\. Per-user install (no administrator rights): %LOCALAPPDATA%\Programs\ls6t.
; Python does the rest (labspec_translator.winsetup): checks, LabSpec scripts, PATH, the Claude app's configuration.

Unicode true
SetCompressor /SOLID lzma
ManifestDPIAware true

!ifndef VERSION
  !error "VERSION is not defined: build with installer/build_installer.py"
!endif
!ifndef STAGE
  !error "STAGE is not defined: build with installer/build_installer.py"
!endif
!ifndef OUTFILE
  !define OUTFILE "ls6t-setup-${VERSION}.exe"
!endif
; the staged files, with the separator of the computer that builds (makensis for Windows wants backslashes)
!ifdef NSIS_WIN32_MAKENSIS
  !define STAGEFILES "${STAGE}\*"
!else
  !define STAGEFILES "${STAGE}/*"
!endif

!define APP "ls6t"
!define APPNAME "labspec-translator (ls6t)"
!define PUBLISHER "Nicola Ferralis"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ls6t"
!define PY "$INSTDIR\python\python.exe"

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\ls6t"
InstallDirRegKey HKCU "Software\ls6t" "InstallDir"
RequestExecutionLevel user
BrandingText "${APPNAME} ${VERSION}"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${VERSION}"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "${APPNAME} installer"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "MIT license"

!include "MUI2.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "${APPNAME} ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "This installs ls6t, which lets the Claude desktop app run Raman measurements on a HORIBA instrument through LabSpec 6, while the operator watches and can stop it at any time.$\r$\n$\r$\nEverything is installed for your Windows account only (no administrator rights): a private Python, the ls6t commands, and the LabSpec scripts in $PROFILE\ls6t\vbs.$\r$\n$\r$\nInstall it with the Windows account that runs LabSpec. Close the Claude app before continuing."
!define MUI_COMPONENTSPAGE_SMALLDESC
!define MUI_FINISHPAGE_TITLE "ls6t is installed"
!define MUI_FINISHPAGE_TEXT "Next steps (INSTALL.md, 'With the installer'):$\r$\n$\r$\n1. In LabSpec 6: Processing > Scripts, add ls6t_discover.vbs from $PROFILE\ls6t\vbs and run it once.$\r$\n2. Start menu > ls6t PowerShell:  ls6t profile import <the dump's path>, then review the limits with the maintainer.$\r$\n3. In LabSpec, start ls6t_bridge.vbs whenever ls6t should drive the instrument.$\r$\n4. Quit and restart the Claude app: the ls6t tools appear in a new chat."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open the LabSpec scripts folder"
!define MUI_FINISHPAGE_RUN_FUNCTION OpenScripts
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\docs\INSTALL.html"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Show the installation guide"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ------------------------------------------------------------------------------------------------ helpers

; Python (or a measurement started by ls6t) still running from this folder: its files cannot be replaced
!macro WaitUnlocked
  ${Do}
    ${IfNot} ${FileExists} "$INSTDIR\python\python.exe"
      ${ExitDo}
    ${EndIf}
    ClearErrors
    FileOpen $0 "$INSTDIR\python\python.exe" a
    ${IfNot} ${Errors}
      FileClose $0
      ${ExitDo}
    ${EndIf}
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "ls6t is running from $INSTDIR: the Claude app, an ls6t window, or a measurement in progress.$\r$\n$\r$\nQuit the Claude app (right-click its tray icon, Quit), close ls6t windows, let any measurement finish, then press Retry." /SD IDCANCEL IDRETRY +2
    Abort "ls6t is still running: nothing was changed."
  ${Loop}
!macroend

Function OpenScripts
  ExecShell "open" "$PROFILE\ls6t\vbs"
FunctionEnd

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "ls6t needs 64-bit Windows (10 or 11)." /SD IDOK
    Abort
  ${EndIf}
FunctionEnd

; ------------------------------------------------------------------------------------------------ sections

Section "ls6t (required)" SecCore
  SectionIn RO
  !insertmacro WaitUnlocked
  SetOutPath "$INSTDIR"
  DetailPrint "Removing the previous version's program files (your profile and results are kept)"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\bin"
  RMDir /r "$INSTDIR\docs"
  RMDir /r "$INSTDIR\examples"
  File /r "${STAGEFILES}"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\ls6t" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\ls6t" "Version" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegStr HKCU "${UNINSTKEY}" "URLInfoAbout" "https://github.com/feranick/labspec_translator"
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" "$0"
SectionEnd

Section "Connect to the Claude app" SecClaude
SectionEnd

Section "Add ls6t to PATH" SecPath
SectionEnd

Section "Start menu shortcuts" SecMenu
  CreateDirectory "$SMPROGRAMS\ls6t"
  CreateShortcut "$SMPROGRAMS\ls6t\ls6t PowerShell.lnk" "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" \
    '-NoExit -NoLogo -ExecutionPolicy Bypass -File "$INSTDIR\bin\ls6t-shell.ps1"' "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" 0
  CreateShortcut "$SMPROGRAMS\ls6t\LabSpec scripts (ls6t).lnk" "$PROFILE\ls6t\vbs"
  CreateShortcut "$SMPROGRAMS\ls6t\ls6t guides.lnk" "$INSTDIR\docs"
  CreateShortcut "$SMPROGRAMS\ls6t\Uninstall ls6t.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Section /o "Simulator connection (practice without the instrument)" SecSim
SectionEnd

Section "-setup" SecSetup
  StrCpy $1 "--compile"
  ${If} ${SectionIsSelected} ${SecClaude}
    StrCpy $1 "$1 --claude"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecSim}
    StrCpy $1 "$1 --sim"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecPath}
    StrCpy $1 "$1 --path"
  ${EndIf}
  DetailPrint "Setting up (checks, precompiling, LabSpec scripts, PATH, Claude app): this takes a minute or two"
  nsExec::ExecToLog '"${PY}" -m labspec_translator.winsetup install $1'
  Pop $0
  ${If} $0 == "1"
    MessageBox MB_OK|MB_ICONSTOP "The setup check failed: see the details above and $INSTDIR\install.log." /SD IDOK
    Abort "Setup check failed"
  ${ElseIf} $0 == "2"
    MessageBox MB_OK|MB_ICONEXCLAMATION "ls6t is installed, with warnings: see the details above (also in $INSTDIR\install.log)." /SD IDOK
  ${ElseIf} $0 != "0"
    MessageBox MB_OK|MB_ICONSTOP "The setup step could not run ($0): see $INSTDIR\install.log." /SD IDOK
    Abort "Setup step failed"
  ${EndIf}
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore} "The ls6t commands, the MCP server, a private Python with every package, the guides and the LabSpec scripts (in $PROFILE\ls6t\vbs)."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecClaude} "Adds the 'ls6t' server to the Claude app's configuration (backed up first; your other servers and ls6t settings are kept)."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecPath} "Lets any new PowerShell window run ls6t, ls6t-mcp and ls6t-typelib."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMenu} "Start menu: ls6t PowerShell, the LabSpec scripts folder, the guides, Uninstall."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecSim} "Also adds 'ls6t-sim', the same tools on the built-in simulator (results in $PROFILE\ls6t_sim_runs). Remove it from the Claude configuration when no longer needed."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ------------------------------------------------------------------------------------------------ uninstall

Section "Uninstall"
  !insertmacro WaitUnlocked
  ${If} ${FileExists} "${PY}"
    DetailPrint "Removing the PATH entry and the Claude app's ls6t entries"
    nsExec::ExecToLog '"${PY}" -m labspec_translator.winsetup uninstall'
    Pop $0
  ${EndIf}
  RMDir /r "$SMPROGRAMS\ls6t"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\bin"
  RMDir /r "$INSTDIR\docs"
  RMDir /r "$INSTDIR\examples"
  Delete "$INSTDIR\install.log"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "Software\ls6t"
  DetailPrint "Kept: $PROFILE\ls6t (profile, bridge folder, LabSpec scripts) and the results folders."
SectionEnd
