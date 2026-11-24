; ls6t.nsi - the Windows installer of labspec-translator (ls6t). Built by installer/build_installer.py:
;   makensis /DVERSION=<version> /DSTAGE=<staged files> /DOUTFILE=<setup exe> ls6t.nsi
; STAGE holds python\ (the embeddable Python with every package in Lib\site-packages), bin\ (launchers), docs\,
; examples\. Per-user install (no administrator rights): %LOCALAPPDATA%\Programs\ls6t.
; Python does the rest (labspec_translator.winsetup): checks, LabSpec scripts, PATH, the LLM apps' MCP configuration.

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
!define MUI_WELCOMEPAGE_TEXT "This installs ls6t, which lets an LLM app (the Claude desktop app or Claude Code, ChatGPT / Codex, Gemini CLI, Cursor, GitHub Copilot, Windsurf or LM Studio) run Raman measurements on a HORIBA instrument through LabSpec 6, while the operator watches and can stop it at any time.$\r$\n$\r$\nEverything is installed for your Windows account only (no administrator rights): a private Python, the ls6t commands, and the LabSpec scripts in $PROFILE\ls6t\vbs.$\r$\n$\r$\nInstall it with the Windows account that runs LabSpec.$\r$\n$\r$\nBefore continuing: QUIT the LLM apps that use ls6t (Claude: right-click its tray icon, Quit; closing its window is not enough): the installation cannot go on while they run. LabSpec can stay open: a running ls6t bridge is ended cleanly, and you start ls6t_bridge.vbs again in LabSpec yourself after the installation (the installer cannot start it). LabSpec need not be restarted."
!define MUI_COMPONENTSPAGE_SMALLDESC
!define MUI_FINISHPAGE_TITLE "ls6t is installed"
!define MUI_FINISHPAGE_TEXT "Next steps (INSTALL.md, 'The installer'):$\r$\n$\r$\n1. In LabSpec 6: Processing > Scripts, start ls6t_bridge.vbs from $PROFILE\ls6t\vbs (add it first if it is not listed). If an ls6t bridge was running, the installer has ended it (its window closed): start it again now. It reads the instrument (lasers, gratings, filters, objectives) and ls6t builds its machine profile from that by itself (cautious: lasers capped at 10 %).$\r$\n2. Quit and restart the LLM apps connected: the ls6t tools appear in a new chat. (An app installed later: ls6t connect auto.)$\r$\n3. With the maintainer, review the profile and set the limits (INSTALL.md step 7, TESTING.md T1)."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open the LabSpec scripts folder"
!define MUI_FINISHPAGE_RUN_FUNCTION OpenScripts
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Show the installation guide"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenGuide
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
; the finish page's third option (FinishShow / FinishLeave below): prompts to try
!define MUI_PAGE_CUSTOMFUNCTION_SHOW FinishShow
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE FinishLeave
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ------------------------------------------------------------------------------------------------ helpers

; The finish page has room for MUI's two options only: a third checkbox goes under them at the same spacing, with
; their font and background ("Show prompts to try"), checked. Its state is kept in $PromptsWanted (on every click and
; when the page is left), and docs\PROMPTS.html opens in .onGUIEnd, after the window has closed: 2026-11-23 it did not
; open from the page's leave function. When MUI's checkboxes cannot be measured, it goes at a fixed place.
Var PromptsBox
Var PromptsWanted
Var InstallDone

Function FinishShow
  StrCpy $PromptsWanted "1"
  System::Call '*(i 0, i 0, i 0, i 0) p .r1'
  System::Call '*(i 0, i 0, i 0, i 0) p .r2'
  System::Call 'user32::GetWindowRect(p $mui.FinishPage.Run, p r1)'
  System::Call 'user32::GetWindowRect(p $mui.FinishPage.ShowReadme, p r2)'
  System::Call 'user32::MapWindowPoints(p 0, p $mui.FinishPage, p r1, i 2)'
  System::Call 'user32::MapWindowPoints(p 0, p $mui.FinishPage, p r2, i 2)'
  System::Call '*$1(i .r3, i .r4, i, i)'
  System::Call '*$2(i .r5, i .r6, i .r7, i .r8)'
  System::Free $1
  System::Free $2
  IntOp $9 $6 - $4                ; the spacing between MUI's two checkboxes
  IntOp $9 $6 + $9                ; the top of the third
  IntOp $7 $7 - $5                ; its width
  IntOp $8 $8 - $6                ; its height
  ${If} $8 > 4
  ${AndIf} $7 > 40
  ${AndIf} $9 > $6
    ${NSD_CreateCheckbox} $5 $9 $7 $8 "Show prompts to try (first steps and advanced)"
  ${Else}
    ${NSD_CreateCheckbox} 120u 170u 195u 10u "Show prompts to try (first steps and advanced)"
  ${EndIf}
  Pop $PromptsBox
  ${If} $PromptsBox == "error"
    StrCpy $PromptsWanted "0"
    Return
  ${EndIf}
  SetCtlColors $PromptsBox "" "${MUI_BGCOLOR}"
  SendMessage $mui.FinishPage.ShowReadme ${WM_GETFONT} 0 0 $0
  ${If} $0 <> 0
    SendMessage $PromptsBox ${WM_SETFONT} $0 1
  ${EndIf}
  ${NSD_Check} $PromptsBox
  ${NSD_OnClick} $PromptsBox PromptsClick
FunctionEnd

Function PromptsClick
  Pop $0                          ; the control
  ${NSD_GetState} $PromptsBox $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $PromptsWanted "1"
  ${Else}
    StrCpy $PromptsWanted "0"
  ${EndIf}
FunctionEnd

Function FinishLeave
  ${If} $PromptsBox != ""
  ${AndIf} $PromptsBox != "error"
    ${NSD_GetState} $PromptsBox $0
    ${If} $0 == ${BST_CHECKED}
      StrCpy $PromptsWanted "1"
    ${Else}
      StrCpy $PromptsWanted "0"
    ${EndIf}
  ${EndIf}
FunctionEnd

; Opens a file with its default program through Explorer, a process of its own: ExecShell from the installer, which
; exits right after, showed nothing (2026-11-23: the prompts page "tried to open" and no browser appeared)
!macro OpenWithExplorer path
  ${If} ${FileExists} "${path}"
    Exec '"$WINDIR\explorer.exe" "${path}"'
  ${EndIf}
!macroend

Function OpenGuide
  !insertmacro OpenWithExplorer "$INSTDIR\docs\INSTALL.html"
FunctionEnd

Function .onGUIEnd
  ${If} $InstallDone == "1"
  ${AndIf} $PromptsWanted == "1"
    !insertmacro OpenWithExplorer "$INSTDIR\docs\PROMPTS.html"
  ${EndIf}
FunctionEnd

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
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "ls6t is running from $INSTDIR: an LLM app using it (Claude, Claude Code, ChatGPT, Codex, Gemini CLI, Cursor, VS Code / Copilot, Windsurf, LM Studio), an ls6t window, or a measurement in progress.$\r$\n$\r$\nQuit those apps (Claude: right-click its tray icon, Quit), close ls6t windows, let any measurement finish, then press Retry." /SD IDCANCEL IDRETRY +2
    Abort "ls6t is still running: nothing was changed."
  ${Loop}
!macroend

Function OpenScripts
  Exec '"$WINDIR\explorer.exe" "$PROFILE\ls6t\vbs"'     ; a folder (FileExists wants a file)
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

SectionGroup /e "Connect ls6t to the LLM apps found" SecApps
  Section /o "Claude desktop app" SecClaude
  SectionEnd
  Section /o "ChatGPT desktop app / Codex" SecCodex
  SectionEnd
  Section /o "Gemini CLI" SecGemini
  SectionEnd
  Section /o "Cursor" SecCursor
  SectionEnd
  Section /o "Claude Code" SecClaudeCode
  SectionEnd
  Section /o "GitHub Copilot (VS Code, Visual Studio, Copilot CLI)" SecCopilot
  SectionEnd
  Section /o "Windsurf" SecWindsurf
  SectionEnd
  Section /o "LM Studio (local models)" SecLMStudio
  SectionEnd
  Section /o "LM Studio Bionic (the agent app)" SecBionic
  SectionEnd
  Section /o "Also the simulator (ls6t-sim, to practise)" SecSim
  SectionEnd
SectionGroupEnd

Section "Add ls6t to PATH" SecPath
SectionEnd

Section "Start menu shortcuts" SecMenu
  CreateDirectory "$SMPROGRAMS\ls6t"
  CreateShortcut "$SMPROGRAMS\ls6t\ls6t PowerShell.lnk" "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" \
    '-NoExit -NoLogo -ExecutionPolicy Bypass -File "$INSTDIR\bin\ls6t-shell.ps1"' "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" 0
  CreateShortcut "$SMPROGRAMS\ls6t\LabSpec scripts (ls6t).lnk" "$PROFILE\ls6t\vbs"
  CreateShortcut "$SMPROGRAMS\ls6t\ls6t guides.lnk" "$INSTDIR\docs"
  CreateShortcut "$SMPROGRAMS\ls6t\Prompts to try.lnk" "$INSTDIR\docs\PROMPTS.html"
  CreateShortcut "$SMPROGRAMS\ls6t\Uninstall ls6t.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

Section "-setup" SecSetup
  StrCpy $1 "--compile"
  ${If} ${SectionIsSelected} ${SecClaude}
    StrCpy $1 "$1 --claude"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecCodex}
    StrCpy $1 "$1 --codex"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecGemini}
    StrCpy $1 "$1 --gemini"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecCursor}
    StrCpy $1 "$1 --cursor"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecClaudeCode}
    StrCpy $1 "$1 --claudecode"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecCopilot}
    StrCpy $1 "$1 --copilot"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecWindsurf}
    StrCpy $1 "$1 --windsurf"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecLMStudio}
    StrCpy $1 "$1 --lmstudio"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecBionic}
    StrCpy $1 "$1 --bionic"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecSim}
    StrCpy $1 "$1 --sim"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecPath}
    StrCpy $1 "$1 --path"
  ${EndIf}
  DetailPrint "Setting up (checks, precompiling, LabSpec scripts, PATH, LLM apps): this takes a minute or two"
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
  StrCpy $InstallDone "1"         ; .onGUIEnd opens the prompts only after an installation that went through
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore} "The ls6t commands, the MCP server, a private Python with every package, the guides and the LabSpec scripts (in $PROFILE\ls6t\vbs)."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecApps} "Adds the 'ls6t' MCP server to the apps ticked (each file is backed up first; your other servers and ls6t settings are kept). The apps found on this PC are ticked. Later: ls6t connect."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecClaude} "Claude desktop app: claude_desktop_config.json, where the app reads it (Microsoft Store builds: %LOCALAPPDATA%\Claude-Data\claude_...). Quit Claude (tray icon > Quit) before installing: it rewrites the file when it quits."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCodex} "ChatGPT desktop app, Codex CLI and Codex IDE extension (they share %USERPROFILE%\.codex\config.toml)."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCursor} "Cursor (its agent and chat, every project): %USERPROFILE%\.cursor\mcp.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecClaudeCode} "Claude Code (terminal and IDE extensions), every project: %USERPROFILE%\.claude.json. End Claude Code sessions before installing: it rewrites the file while it runs."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCopilot} "GitHub Copilot's agent in VS Code and Visual Studio, and Copilot CLI: %USERPROFILE%\.copilot\mcp-config.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecWindsurf} "Windsurf (Cascade): %USERPROFILE%\.codeium\windsurf\mcp_config.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecLMStudio} "LM Studio, with models running on this PC (nothing leaves it): %USERPROFILE%\.lmstudio\mcp.json. Small local models follow long instructions less reliably."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecBionic} "LM Studio Bionic keeps its own MCP list (%USERPROFILE%\.lmstudio\apps\bionic\.internal\ng-mcp.json), not LM Studio's mcp.json. Quit Bionic before installing; start it once first if it never ran."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecGemini} "Gemini CLI: %USERPROFILE%\.gemini\settings.json. (The Gemini app and ChatGPT on the web take only internet servers and cannot use ls6t.)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecPath} "Lets any new PowerShell window run ls6t, ls6t-mcp and ls6t-typelib."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMenu} "Start menu: ls6t PowerShell, the LabSpec scripts folder, the guides, Uninstall."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecSim} "Also adds 'ls6t-sim' to the apps ticked: the same tools on the built-in simulator (results in $PROFILE\ls6t_sim_runs)."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

;------------------------------------------------------------------------------------------------- choosing the apps

; ticks the apps found on this PC (the same checks as labspec_translator.clients.detect). From the command line
; (/S installs), /CLAUDE /CODEX /GEMINI /CURSOR /CLAUDECODE /COPILOT /WINDSURF /LMSTUDIO /BIONIC /SIM choose instead, and /NOAPPS connects none.
!macro TickIf sec
  ${If} $R9 == "1"
    !insertmacro SelectSection ${sec}
  ${EndIf}
!macroend

!macro FoundGlob pattern
  FindFirst $R7 $R8 "${pattern}"
  ${If} $R8 != ""
    StrCpy $R9 "1"
  ${EndIf}
  FindClose $R7
!macroend

!macro FoundExe exe
  ClearErrors
  SearchPath $R7 "${exe}"
  ${IfNot} ${Errors}
    StrCpy $R9 "1"
  ${EndIf}
!macroend

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "ls6t needs 64-bit Windows (10 or 11)." /SD IDOK
    Abort
  ${EndIf}
  ${GetParameters} $R0
  StrCpy $R1 "0"
  ClearErrors
  ${GetOptions} $R0 "/CLAUDE" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecClaude}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/CODEX" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecCodex}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/GEMINI" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecGemini}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/CURSOR" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecCursor}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/CLAUDECODE" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecClaudeCode}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/COPILOT" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecCopilot}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/WINDSURF" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecWindsurf}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/LMSTUDIO" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecLMStudio}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/BIONIC" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
    !insertmacro SelectSection ${SecBionic}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/NOAPPS" $R2
  ${IfNot} ${Errors}
    StrCpy $R1 "1"
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/SIM" $R2
  ${IfNot} ${Errors}
    !insertmacro SelectSection ${SecSim}
  ${EndIf}
  ${If} $R1 == "1"
    Return
  ${EndIf}
  ; Claude desktop app
  StrCpy $R9 "0"
  ${If} ${FileExists} "$APPDATA\Claude\*.*"
  ${OrIf} ${FileExists} "$LOCALAPPDATA\AnthropicClaude\*.*"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundGlob "$LOCALAPPDATA\Packages\Claude_*"
  !insertmacro FoundGlob "$LOCALAPPDATA\Packages\AnthropicPBC.Claude_*"
  !insertmacro FoundGlob "$LOCALAPPDATA\Claude-Data\claude_*"
  !insertmacro TickIf ${SecClaude}
  ; ChatGPT desktop app / Codex
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.codex\*.*"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundGlob "$LOCALAPPDATA\Packages\OpenAI.ChatGPT*"
  !insertmacro FoundGlob "$LOCALAPPDATA\Packages\OpenAI.Codex*"
  !insertmacro FoundExe "codex.cmd"
  !insertmacro FoundExe "codex.exe"
  !insertmacro TickIf ${SecCodex}
  ; Gemini CLI
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.gemini\*.*"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundExe "gemini.cmd"
  !insertmacro FoundExe "gemini.exe"
  !insertmacro TickIf ${SecGemini}
  ; Cursor
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.cursor\*.*"
  ${OrIf} ${FileExists} "$LOCALAPPDATA\Programs\cursor\Cursor.exe"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundExe "cursor.cmd"
  !insertmacro TickIf ${SecCursor}
  ; Claude Code
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.claude.json"
  ${OrIf} ${FileExists} "$PROFILE\.claude\*.*"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundExe "claude.exe"
  !insertmacro FoundExe "claude.cmd"
  !insertmacro TickIf ${SecClaudeCode}
  ; GitHub Copilot
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.copilot\*.*"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundGlob "$PROFILE\.vscode\extensions\github.copilot*"
  !insertmacro FoundExe "copilot.cmd"
  !insertmacro FoundExe "copilot.exe"
  !insertmacro TickIf ${SecCopilot}
  ; Windsurf
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.codeium\windsurf\*.*"
  ${OrIf} ${FileExists} "$LOCALAPPDATA\Programs\Windsurf\*.*"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundExe "windsurf.cmd"
  !insertmacro TickIf ${SecWindsurf}
  ; LM Studio
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.lmstudio\*.*"
  ${OrIf} ${FileExists} "$LOCALAPPDATA\Programs\LM Studio\*.*"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro FoundExe "lms.exe"
  !insertmacro TickIf ${SecLMStudio}
  ; LM Studio Bionic (its own MCP list)
  StrCpy $R9 "0"
  ${If} ${FileExists} "$PROFILE\.lmstudio\apps\bionic\*.*"
  ${OrIf} ${FileExists} "$PROGRAMFILES64\Bionic\Bionic.exe"
  ${OrIf} ${FileExists} "$LOCALAPPDATA\Programs\Bionic\Bionic.exe"
    StrCpy $R9 "1"
  ${EndIf}
  !insertmacro TickIf ${SecBionic}
FunctionEnd

; ------------------------------------------------------------------------------------------------ uninstall

Section "Uninstall"
  !insertmacro WaitUnlocked
  ${If} ${FileExists} "${PY}"
    DetailPrint "Removing the PATH entry and the ls6t entries of the LLM apps"
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
