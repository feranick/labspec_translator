# labspec-translator: development log

What was done, found and decided, by date (oldest first). The current design is in [planning.md](planning.md); what changed per version, in [CHANGELOG.md](CHANGELOG.md).

## State log

- 2026-11-19: v1 written. 40 tests pass (sandbox, Python 3.10, mcp 2.3). 6/6 benchmarks pass on the simulator.
  examples/studies.json (6 studies) ran end to end through the detached runner in sim mode: all reliable.
  VBS reviewed against the manual by an independent agent; its findings were fixed (stale stop.flag, popup test
  closing the bridge's window, path escaping, stale GetAcqID -2, Unicode files, label index > 9, discover crash,
  error checks before starting hardware, never running a command twice).
- Repo moved from ~/Software/labspec-translator (now only its .git and .gitignore: can be deleted) to
  ~/Software/labspec_translator (remote git@github.com:feranick/labspec_translator.git).
- 2026-11-19 (later): README.md written (with the Windows 11 installation, offline install, Claude app entry,
  troubleshooting) and TESTING.md (the hardware plan T0-T9 with the maintainer, open questions Q1-Q12, results
  table). Added `ls6t bridge call VERB key=value` (maintainer tool; acquire/autofocus need --laser; refused while a
  request holds the instrument). MCP stdio handshake checked with the SDK client: initialize 0.26 s, 11 tools,
  run_studies + get_image + instrument on the simulator. Simulator now recognises another process's simulator by
  pid. 42 tests pass. Committed and tagged 2026.11.19.1 ("First release").
- 2026-11-19, 2026.11.19.2: video snapshots. Bridge verb `video` clones the live frame (Video(2) + Exec(id, 3)),
  writes axes and pixels for Python and tries LabSpec's image formats. With laser_s, it takes a frame without the
  laser, opens the shutter (ShutterMode manual + motors.laser_shutter) for laser_s, takes the lit frame, and closes
  and restores on every path. video.py decodes (COLORREF/RGB, x/y fastest), marks the laser position (stage +
  video.laser_offset_um) and finds the spot from the difference of the two frames: on the simulator the offset
  (1.5, -0.8) µm is recovered to 0.01 µm at x100/x50 and 0.06 µm at x10. Tool `snapshot` (image + details),
  `ls6t instrument snapshot [--laser]`, study key `video` (before on by default for calibration). Guards: shutter
  motor in the profile, current filter within caps, operator confirmation, refused while a request runs. Discovery
  looks for shutter motors and records the video state. TESTING T5b and Q13-Q16 added. 48 tests pass; the MCP
  snapshot tool returns [image, text] through the stdio protocol (SDK 2.3).
- 2026-11-19, 2026.11.19.3: alignment and guided adjustments, from HORIBA's AutoAlignment quick start guide (2013:
  check Si intensity -> standard / high-precision alignment -> re-check -> laser marker; per laser; calibrate after;
  no automation command for it). Kind `alignment`: the Si check at the laser's fixed settings vs the baseline the
  maintainer records (`ls6t alignment record`, CLI only); guided: on failure the bridge's `notice` shows HORIBA's
  checklist, the operator runs AutoAlignment and presses OK, the check repeats (max_retries 1); then a snapshot
  re-measures the beam's offset (AutoAlignment moves LabSpec's marker); alignment_log.jsonl; readiness warnings
  once baselines exist. Kind `guide` (Nicola's idea: the tool proposes a continuous manual action and says STOP):
  the fast loop runs in the bridge (short acquisitions, line height/s, reach/drop/maximize, saturation guard,
  LabSpec dynamic Dialog with Done/Abort, or the status bar), so STOP never waits for Python or the LLM; the LLM
  sets the goal and reads the result. Simulator: misalignment, AutoAlignment, an operator ramping power or passing
  focus. `ls6t profile set`. 55 tests pass; 7/7 benchmarks (new: guided_focus). TESTING T5c-T5d, Q17-Q20.
- 2026-11-19, 2026.11.19.4: stage and focus. Kind `move` (no laser; offset from home, snapshot, `stay` makes it
  home), kind `focus` and `autofocus: "scan"` (Z steps over range_um, line height after a 3-point median, weighted
  Gaussian through the points above 30 % of the best; back to the start without an interior maximum). The request
  keeps a home spot and a focus (ctx.home_xy, ctx.z_focus): offsets start from them, autofocus/scan update the
  focus, and at the end of a normal request Z returns to the focus first, then XY home (before, a depth profile left
  Z off focus and the next request took it as the focus: fixed). stage.z_um absolute limit. The guided loop's
  metric now uses a 3-point median (cosmic rays: the loop runs without spike removal). Simulator: depth of focus
  by objective, sim_defocus. 61 tests; 8/8 benchmarks (new focus_scan: 2.293 for 2.3). TESTING T6.5-T6.6, Q21-Q22.
- 2026-11-19, 2026.11.19.5: the laser spot (Nicola: the beam drifts from the screen centre; find it by trial and
  error with focus/signal, and remember it). spot.py keeps the offset (beam - LabSpec marker) per objective@laser
  with history and drift (LS6T_HOME/spot.json; plausibility spot.max_offset_um; drift warning spot.drift_warn_um).
  Kind `spot`: methods shutter (frames without/with the beam), acquisition (bridge `video` during_acq_s: a frame
  half-way through a Raman acquisition, no shutter motor needed), raman (snapshot -> feature (given, or 'auto':
  the DoG blob nearest the centre) refined on the video -> grid scan of the stage around feature - prior offset,
  line height each -> dip or peak -> weighted centroid -> offset = feature - stage). Snapshots now list
  'features' (numbered on the PNG) and draw the marker with the remembered offset; lit snapshots in any study
  update the record. move.to_um aims the beam (stage = point - offset). Simulator: Raman follows the real beam
  (stage + offset), a dust particle 15 µm -X / 10 µm +Y of the start, sim_spot_offset. 65 tests; 10/10
  benchmarks (spot_raman 1.073/0.584 for 1.1/0.6; spot_video 1.52/-0.80 for 1.5/-0.8). TESTING T5e, Q23-Q25.
- 2026-11-19, 2026.11.19.6: depth profiles (kind depth -> a Z-only map, depth_result: profiles vs nominal and
  n-corrected depth, interfaces at half-way crossings with 10-90 % widths, interfaces_by; stage.z_into_sample;
  live profile in LabSpec; Everall caveat), the spectral zero (kind calibration: reference si/polystyrene/
  cyclohexane/naphthalene/diamond or lines_cm1; offset, or offset and scale with lines >= 300 cm-1 apart;
  adjust labspec = notice with the shift in cm-1 and nm, re-check up to max_retries, then drop ls6t's correction;
  adjust ls6t = xcal.json per laser@grating applied to later spectra with a warning; no automation command for
  LabSpec's Zero/Koeff), white-light backgrounds (kind background: notices for the lamp and, without a shutter
  motor, for blocking the laser; bridge acquire laser_off closes the shutter in manual mode; stored as cps per
  accumulation; acquire.subtract_background scales by exposure and interpolates; narrow lines in a background
  warn). Tool/command references. INSTALL.md split from the README; FUNCTIONALITY.md. Simulator: sim_film,
  sim_xshift, sim_lamp, laser blocked by the operator. 72 tests; 13/13 benchmarks. TESTING T4.7, T4.8, T6.3,
  Q26-Q28.
- 2026-11-19, 2026.11.19.7: native mapping. map.engine auto|points|native (+swift); auto -> native from
  mapping.native_min_points (400) when nothing is needed between points (one window, fixed exposure, no autofocus
  every N). Bridge map_native: Spectro to the centre, SetMappingParams (all off, then x/y/z by step), PutValue
  LabSpecExposureTime/Accumulation, SWIFT, Acq(ACQ_SPECTRAL_IMAGE, -1 or exposure), wait/cancel, WriteData (the
  generic writer the video uses) + Save .l6s and txt. ls6t parse_native_map: axes, data order (profile), first
  spatial axis fastest -> per-point records with grid indices, analysed like points maps. The open points are
  profile settings (mapping.positions, data_order, acq_time, native_point_s) for TESTING T6.7 (Q29-Q33).
  75 tests; 14/14 benchmarks (native_map = the points map).
- 2026-11-19, 2026.11.19.8: ls6t-typelib (typelib.py, separate console script, also `python typelib.py`):
  LoadTypeLibEx (REGKIND_NONE) via comtypes, or pythoncom; finds NFActiveX.ocx / NGSActiveX.ocx in LabSpec's folders
  or registered type libraries naming LabSpec/HORIBA; lists types, methods (signatures, in/out/optional, dispid,
  doc), properties, enum values; marks methods not in the 2018 manual (NEW) and words of interest; text, markdown,
  JSON. Not run on Windows yet (TESTING T1.3, Q34). 77 tests.
- 2026-11-20, 2026.11.19.9: Nicola pulled NFActiveX.ocx from the Raman PC (32-bit, 2021-03-11, 995 kB; 64-bit,
  2021-10-07, 865 kB; both "NFActiveX ActiveX Control Module" 1.0.0.1). ls6t-typelib now reads the TYPELIB resource
  itself (PE resource tree + MSFT format, pure Python; Windows loaders only as fallback). Both builds carry the same
  type library: NFACTIVEXLib 1.0 {8E2C42D5-A3C9-4D8D-996C-DD2D669F59DA}, MIDL 7.00.0500, 2019-01-02; dispinterface
  _DNFActiveX (60 methods), _DNFActiveXEvents (no events: polling stays the only way), coclass NFActiveX.
  In the manual: 45 (Autofocus is spelled AutoFocus). Not in this library though documented: GetValueEx,
  SetExtendedVideo. Not in the manual (signatures in ~/Desktop/activeX/NFActiveX_commands.md):
  GetLastErrorInfo() -> VARIANT (now appended to the bridge's error messages); SetScriptPath(path);
  GetConfigParam(names, units, types, values) / SetConfigParam(description, names, units, types, values, show)
  (look like script configuration parameters); SetMotorInfo(position, state) (script-controlled motors);
  SaveConfig(ini, show) / LoadConfig(ini, show) (a whole configuration: could save and restore the instrument's
  set-up around a request); Translate(text); GUI(mode, name, param, id, value*, state, format), OperVar(mode,
  index, value*, param), PutGUI(index, value, mode), PutArrGUI(index, value*, mode) (generic access to the
  interface's controls/variables: possibly the way to calibration settings, lamp, etc., semantics unknown: ask
  HORIBA before trying); OpenCL(...) (GPU processing); GetValueSimplePart(id, name, value, vtype, start, size)
  (chunked data reads: useful for exporting large native maps faster than GetValue); InitNA(root, status).
  No method named for AutoAlignment, AutoCalibration, zero/coefficient, lamp or map progress.
- 2026-11-20, 2026.11.20.1: the type library's extra methods put to use, each guarded and tested on the simulator:
  SaveConfig/LoadConfig (bridge verb `config`; `ls6t instrument config save|load|list`, LS6T_HOME/configs/NAME.ini,
  load asks the operator; profile `session.restore_config` saves at request start, loads at normal end);
  GetValueSimplePart (map_native writes the values in chunks of `mapping.export_chunk`, falls back to whole reads;
  results carry `data_via`); GetArr helper: GetValue -> GetValueEx -> GetValueSimple; MotorLabel falls back to
  GetValueEx "CurrentPositionName:" (past index 9); GetLastErrorInfo: verb `last_error`, `status.last_error`;
  extended video: `video` with extended=1 calls Video(3) (manual; SetExtendedVideo absent so the area is LabSpec's),
  clones a returned id > 0, else the active video; profile video.allow_extended false + operator Yes; no laser.
  Discovery probes GetValueEx, GetValueSimplePart (16-point scratch spectrum, removed) and GetConfigParam (lists
  config_param.*); profile import notes them and sets mapping.export_chunk 0 when GetValueSimplePart is missing.
  ls6t_script_motor.vbs: virtual script motor logging GetMotorAction / SetMotorInfo (Q38). Deliberately not called:
  SetConfigParam, SetScriptPath (change the script set-up, semantics unknown), GUI/OperVar/PutGUI/PutArrGUI.
  TESTING Q35-Q40, T1.4, T5b.6. 84 tests.
- 2026-11-20, 2026.11.20.2: Windows installer. installer/ls6t.nsi (NSIS 3, MUI2, per user, RequestExecutionLevel
  user, %LOCALAPPDATA%\Programs\ls6t, HKCU uninstall key); installer/build_installer.py stages python.org's embeddable
  Python 3.12.10 (._pth 'import site' enabled), pip-installs the wheel [mcp,render,windows] + Windows-only deps
  (comtypes, pywin32, colorama: markers are evaluated for the build OS) with --platform win_amd64 --only-binary into
  Lib/site-packages (numpy/scipy/matplotlib tests dropped; ~215 MB staged), the .cmd launchers and a PowerShell
  shell (installer/bin), docs as md + HTML, examples; then makensis. labspec_translator/winsetup.py (run by the
  installer with the installed Python): check imports, compileall, `bridge install` (with LS6T_HOME/BRIDGE_DIR of an
  existing Claude entry), HKCU PATH + WM_SETTINGCHANGE, Claude config merge (APPDATA and the Store package folder;
  backup; existing env kept; entry = python.exe -m labspec_translator.mcp_server; optional ls6t-sim); uninstall
  removes only entries pointing at this Python. Running ls6t (python.exe open for write fails) blocks an update with
  Retry/Cancel. .github/workflows/installer.yml builds on windows-latest per tag, installs /S, runs winsetup check
  and three sim benchmarks, uninstalls, attaches to the release. Staging tested here (Linux, fake embeddable zip);
  the NSIS script itself is not compiled yet: first compile on the user's Mac (brew makensis) or in CI. 86 tests.
- 2026-11-20, 2026.11.20.3: first CI build failed: Windows makensis found no files for `File /r "<stage>/*"`
  (mixed separators); the script now picks the separator (NSIS_WIN32_MAKENSIS). Workflow actions moved to Node 24
  (checkout v5, setup-python v6, upload-artifact v6); release upload via `gh` instead of softprops/action-gh-release.
- 2026-11-20, 2026.11.20.4: other LLM apps (the instrument PC may run ChatGPT or Gemini). clients.py: detect + connect
  / disconnect / status for claude (claude_desktop_config.json, APPDATA + Store package; macOS/Linux paths too),
  codex (ChatGPT desktop app, Codex CLI and IDE extension share ~/.codex/config.toml or %CODEX_HOME%; TOML edited
  by removing [mcp_servers.<name>] tables and appending ours, validated with tomllib; inline/dotted forms refused;
  tool_timeout_sec 3600, startup 60) and gemini (Gemini CLI ~/.gemini/settings.json, trust false, timeout 1 h).
  Every entry gets the same LS6T_HOME/LS6T_OUT (one lock and bridge for all apps). `ls6t connect [auto|apps]
  [--sim] [--remove]`. Installer: a section group with one section per app, ticked in .onInit when found (folders,
  Store packages, codex/gemini on PATH); silent /CLAUDE /CODEX /GEMINI /NOAPPS /SIM; CI installs with all three and
  checks each file, then that uninstall removed them. Not supported: the Gemini app and ChatGPT web (remote servers
  only). Known: a 2026 report that the ChatGPT Windows desktop app may not expose local stdio tools to ordinary chats
  (openai/codex #38162). tomli added for Python 3.10. 88 tests.
- 2026-11-20, 2026.11.20.5: test mode. testmode.py parses TESTING.md (sections, bold-titled and table-row steps,
  sections without steps as one step, the Q table; laser flag from LASER in the title/section) and offers
  hardware_test (MCP, only with LS6T_TEST_MODE=1: `ls6t connect APP --test`): plan, step, call, record, report,
  status, last_error, config. call = bridge_call plus guards: no quit, verb list, refused during a request, out/
  native/path forced into LS6T_HOME/testing/files, filter by label within caps, X/Y/Z within limits of the current
  position, laser verbs need filter within caps + exposure/accumulation limits + the operator's Yes in LabSpec every
  time; acquire returns a spectrum summary; every call logged. record validates step/result/Q ids; report.md is the
  results table filled in plus the answers to Q1-Q40. `ls6t test plan|step|record|report`. 91 tests.
- 2026-11-20, 2026.11.20.6: Nicola: every request had a long "looking for..." phase (e.g. "where to turn the camera
  on"): the LLM searched for how to do simple things (there was no way to turn the live view on). Now: bridge verb
  `camera` (Video START/STOP/GET_VIDEO_ID, waits up to 5 s for the live id), `instrument camera_on/off`, status
  `live_video`; the instructions map direct requests to tools in two lines; in test mode the hardware_test
  description starts with the direct-request map and `move` takes roles (filter, grating, z...). 92 tests.
- 2026-11-20, 2026.11.20.7: Nicola's screenshot: asked to turn on the top camera, the LLM "ran commands" (its own
  shell) and drove NFActiveX directly: LabSpec showed "About NFActiveX Control" (AboutBox) and the bridge hung (a
  modal dialog blocks LabSpec's script thread). ls6t never calls AboutBox. Changes: instructions (3 lines) and
  hardware_test: only ls6t's tools reach the instrument, never scripts/shell/COM/bridge files, say so when no tool
  fits; INSTALL step 8: make Codex (approval_policy on-request, sandbox_mode read-only), Gemini CLI (tools.exclude
  run_shell_command or confirmationRequired) and Claude (no terminal extensions) ask before commands; TESTING ground
  rule 7. Cameras by name: profile video.cameras {name: number}, P.camera_number, instrument(camera_on, camera),
  status 'camera', study_format profile.cameras. BridgeGone mentions an open LabSpec dialog. Not done: a token in
  bridge commands (an LLM with a shell could read it too). 93 tests.
- 2026-11-20, 2026.11.20.8: Nicola installed with Claude ticked, but Claude did not see ls6t. Cause (GitHub
  TGoodhew/GPIB-MCP#175, jacob-bd/gemini-notebook-mcp-cli#378): current Microsoft Store builds of Claude Desktop
  move their data to %LOCALAPPDATA%\Claude-Data\<package family name, lower case> (run with --user-data-dir) and
  no longer read Packages\Claude_*\LocalCache\Roaming\Claude (left with a .user-data-left.<name> marker); two
  identities (Claude_pzs8sxrjxfjjc, AnthropicPBC.Claude_fnn82j28hfe8t); CLAUDE_USER_DATA_DIR overrides; Claude-3p
  for the third-party-cloud variant; the app rewrites the config while running. clients.claude_dirs follows that
  order (running --user-data-dir via Get-CimInstance, env, packages -> Claude-Data or LocalCache, Claude-3p,
  APPDATA); connect removes ours from stale files and warns when Claude runs; the NSIS detection adds the new
  folders. `ls6t connect check`: an MCP stdio handshake with each app's entry (initialize + tools/list). 95 tests.
- 2026-11-20, 2026.11.20.9: the entry is in Claude-Data (the folder the running app reads) and Claude still does not
  show ls6t. Nicola tests on another PC: tools/ls6t_diagnose.ps1 (PowerShell 5.1, ASCII, CRLF, read-only) collects
  processes and --user-data-dir, packages, every candidate config (validity, BOM, .user-data-left markers, server
  names, the ls6t entries), the ls6t python and install.log, the MCP handshake with the entry's env, and Claude's
  logs (mcp.log, mcp-server-ls6t.log, lines about ls6t/policies). Not run here (no PowerShell in the sandbox).
- 2026-11-20, 2026.11.20.10: the diagnosis (Claude 5.11492.5.0, Store, reads Claude-Data\claude_pzs8sxrjxfjjc):
  that file has no mcpServers (rewritten 13:29 by the running app: the hand edit was lost); the installer (.7) had
  written APPDATA\Claude and the stale LocalCache file (marker .user-data-left present); ls6t's server answers the
  MCP handshake in 1.5 s with all tools; Codex config.toml has ls6t. tools/ls6t_fix_claude.ps1: waits for Claude to
  close (or force-closes on request), then merges the entry with clients.merge_server into each Claude-Data\claude_*.
- 2026-11-20, 2026.11.20.11: Claude then loaded the entry but could not start it: "spawn C:\Users\<you>\...\python.exe
  ENOENT" (and cmd's "The filename, directory name, or volume label syntax is incorrect": the < >): the hand-made
  entry kept the docs' <you> placeholder (Nicola fixed it by hand; ls6t then started). merge_server / toml_merge /
  existing_env drop env values with <...>; the fix script does too. 96 tests.
- 2026-11-20, 2026.11.20.12: in Claude, "switch to 532 nm" could not be done: no tool changed the set-up without a
  measuring study (the LLM rightly refused to work around it). api.set_instrument / instrument(action="set",
  setup) / `ls6t instrument set`: filter (caps of the target laser), laser, grating, objective (operator's Yes in
  LabSpec first), hole, slit; holds the instrument lock while moving; read-back. Also LabSpec labels x10_VIS vs the
  default profile's x10: profile.match_label (same name any case, else the unique label starting with it and not
  continuing with a letter/digit), bridge verb `labels`, used by set, by format's objective check, plan and the
  runner's set-up moves. 97 tests.
- 2026-11-20, 2026.11.20.13: first real discovery (XploRA Plus, LabSpec 6.8.1, bridge 2026.11.20.7, via the
  discover verb): lasers 785.04 (785nm_Edge), 637.75 (638nm_Edge), 532 (532nm_Edge); gratings 600/1200/1800/2400
  (labels "1800 (450-850nm)"); filters 100/50/25/10/1/0.1 %; objectives x100_VIS, x10_VIS, x5_VIS (no x50); hole
  100/300/500, slit 50/100/200 (labels with µm); motors also Spectro, XT, YT, LaserShutter (Off/On); detector -60 C;
  active camera 1, live video off; GetValueEx missing; GetValueSimplePart(start 4, size 8) returned 2 values: sizes
  are not value counts (bytes?) -> chunked export off by default, enabled only when the ramp probe returns 4..11; the
  bridge checks chunk lengths. Nicola: the installation should use discovery: the bridge now records an inventory at
  each start (DiscoverTo(out, popup, probes=False) -> LS6T_HOME/discovery_latest.txt); P.load() builds an automatic
  profile from it when none exists (lasers capped at 10 %, laser_shutter None, 'auto' block) and refreshes it from
  newer inventories until a person imports/sets (then P.drift reports differences in tool_status). The dump is in
  tests/data. 98 tests.
- 2026-11-20, 2026.11.20.14: Claude on the instrument: camera_on with a number failed (the tool's camera was typed
  str: now str | int); cameras cannot be listed by LabSpec's API, so names stay a person's (video.cameras; the
  instrument showed top = 0, internal = 1); "turn the laser on so I can see it" had no tool: bridge verb `laser`
  (on: manual shutter mode + shutter motor 1 for `seconds`; globals gLaserOpen/T0/S/Motor/OldMode; LaserClose on
  time up in the main loop, halt/quit, safe, bridge end, and LaserGuard before any verb but
  ping/status/camera/labels/last_error/message/laser), api.laser (shutter motor from the profile, filter within the
  caps, seconds <= limits.max_laser_on_s = 60, the operator's Yes each time), instrument laser_on/laser_off, status
  laser_open/laser_left_s; hardware_test routes `laser` through api.laser. Risk left: if the VBS script dies with an
  error while open, the shutter stays open (the operator closes it in LabSpec; TESTING T5b.7). 100 tests.
- 2026-11-20, 2026.11.20.16: Nicola sent LabSpec's OLConfig.ini (C:\HORIBA\LabSpec_6_8_1, Latin-1, numeric hash keys;
  134243433 = internal name, 1835884910 = display name; Names[i]/Values[i] in sub-sections <motor>_<config>; active
  configuration Hardware_Devices OLService[-605497649] = -355149504 = XploRA Plus). It names the cameras (Top = 0,
  Internal = 1, IDS U3-388xLE-C and U3-356xXLE-C), objectives with NA (0.9/0.25/0.1) and arrays 85.6x58.3,
  863x590, 1713x1170 (field of view in µm, inferred), motors discovery missed (LaserPol Circular/Horizontal/
  Vertical, RamanPol Horizontal/Vertical/None, LaserFilter Notch/Edge/PL, FilterV, DuoPos Camera/Mirror/DuoScan,
  Output Camera/Trinocular, IlluminatorV Dark/Bright field, Microscope, LaserSwitch, E-Cal_Neon, Laser position
  VISU, Slit 0 µm), and per-grating device arrays (198.8, 12.2, 1.047e-5, 7.7-8...: likely focal length, angle,
  Koeff, Zero: Q41). labspec_ini.py parses it (read only); import_discovery/auto_profile enrich the profile
  (video.cameras when unset, objectives_info, labspec_config); `ls6t profile ini`; discovery's name list adds those
  motors. Test fixture: a reduced made-up ini (the real file, with camera serial numbers, is not in the public
  repo). Ideas: setup keys for the polarisers; the e-Cal neon lamp for the spectral zero. 101 tests.
- 2026-11-20, 2026.11.20.17: locations confirmed: <LabSpec>\Parameters\ACQUISITION\OLConfig.ini and
  <LabSpec>\Parameters\COMMON\OLSetup.ini (software set-up: users and roles, licences and owner names - not
  copied anywhere; external programs; OneClick presets per configuration: -355149504 Blue (off), Green = laser 2
  (532), Red = 1 (638), Near-IR = 0 (785), IR (off), all grating 0 (600), centre 1001.38, range 100-3500; -355149503
  RAW/AUTO; and the AutoCalibration script C:\HORIBA\LabSpec_6_1_174\AutoCalibration\UniversalAutoCalibrationv3.vbs
  (an older install's path). HORIBA's own calibration is a VBS script: reading it may show how to adjust LabSpec's
  zero/coefficient from a script (Q42, ask Nicola for it). labspec_ini.find knows both paths;
  setup_summary(presets, autocalibration_script) goes into profile.labspec_config.
- 2026-11-20, 2026.11.20.18: Nicola shared the LabSpec 6.7.1 program folder (SERVICE: ~195 HORIBA service/QC
  VBS scripts for LabRAM Soleil, HR Evolution, XploRA PLUS, T64000; macros\constants.vbm and horiba.vbm; SCRIPTS\
  SERVICE\pixelunit.vbs). Read, not copied (HORIBA's code). Learned:
  * calls used (count): Dialog 6715 (VBS-only custom dialogs), GetValue, GetMotorPosition, MoveMotor, PutValue,
    Message, Exec, GetMotorStatus, PutDataInfo, Acq, Treat, OperVar 57, GUI 9, SetSingleScriptParam,
    SetScriptParamOptions, Get/SetDetectorZone, SetAutoExposure, MoveXY, MapEx, Send ("RS232", "HTTP_POST");
  * OperVar(mode 0 read / 1 write / 2 ?, "name", value, 0): the instrument calibration per grating is
    ACT_INSTRUMENT[AF|BF|Diode|Focal|Angle|Gamma|SpMin|SpMax][grating index]; pixelunit.vbs (the service "pixel
    unit" panel) treats AF/BF as the Spectro motor's coefficients and sets Diode from the Spectro position; the
    Soleil calibration script computes Angle/Focal/Gamma from neon lines and writes them after a Yes. OLConfig's
    per-grating arrays (198.8, 12.2...) are these. Other OperVar names: G[Params][OLAccessory][...],
    G[Params][OLMapping][MapType][i], GLOBAL[Datas][id][Type], ActMotor, OldMotor, MotorStep, MotorDelta.
  * The XploRA spectro QC uses a neon lamp under the objective (RaiesCalibration_XPL.csv: Ne lines in nm per
    grating and centre) with LabSpec's PeakFitting Treat; ShutterMode 1 = manual (laser), CCDShutter has its own
    0/1/2; GetMotorPosition codes in 6.7: INDEXTOVALUE 100, INDEXTOSTRING 200, FULL_* 300/400, INDEXTOSTEP 500.
  Changes: bridge verb calib_params (read), api.labspec_calibration / instrument calibration, run_calibration
  (adjust labspec) records labspec_parameters before/after/changed; labels and discovery use 100+/200+ codes for
  motors with more than ten positions. Writing LabSpec's calibration (OperVar 1) stays off until Q27 says which
  parameter to change and the maintainer agrees. 101 tests.
- 2026-11-20, 2026.11.20.19: decision with Nicola: ls6t checks the calibration (fast, structured, no dialogs);
  LabSpec's own AutoCalibration corrects it (HORIBA's validated procedure) rather than ls6t writing OperVar values;
  the service/QC scripts are not called (interactive, results to Excel/QC folders, slow). autocal.py: candidates
  (OLSetup's script, <LabSpec>\AutoCalibration\*.vbs), assess (LabSpec calls, operator dialogs, what it changes,
  includes, top-level names shared with the composed bridge = 'Name redefined' at ExecuteGlobal). Bridge verb
  run_vbs (ExecuteGlobal of a .vbs under LabSpecPath; heartbeat pauses: Bridge.alive treats busy/run_vbs like a
  modal question). calibration.adjust 'auto' + profile calibration.autocal {enabled false, script, timeout_s,
  sample}; instrument autocal_info / autocal_run (values changed, read with calib_params). TESTING T4.9.
  CI (.18) failed in the installed copy's benchmarks on Windows (native_map passed; which failed is not in the
  pasted tail; they pass on Linux): the workflow now prints the failing case, its checks, state.json and runner.log,
  and keeps bench.json. 102 tests.
- 2026-11-20, 2026.11.20.20: Nicola: build all five checks inspired by HORIBA's service scripts (own code, no
  HORIBA code or spec tables copied). perf.py (performance.json: filters per laser, axial per objective@laser@hole,
  resolution per laser@grating@slit, cutoff per laser@grating; history, first = baseline). Kind filters
  (plan.filters_steps: within the caps, strongest first = reference, exposure x ref%/%, skipped above
  max_exposure_s; line_signal per s; measured = ref% x ratio; flagged beyond performance.filters_tol; restores the
  operator's filter; profile_command for a person). Kind cutoff (white light + laser off via shutter or operator,
  or the laser; smoothed; transmission (y-dark)/(plateau-dark), plateau 200-380 cm-1; crossings at 10/50/90 %
  walking down from the plateau; format warns ranges below perf.cutoff_for). Focus scan: FWHM of the log-Gaussian
  (2 sqrt(ln2/-c2)) -> axial_fwhm_um, recorded. Calibration: fwhm of the first line -> resolution, vs baseline x
  resolution_tol and performance.resolution_cm1 {grating}. Polarisers: profile motors.laser_pol/raman_pol and
  polarizers{} from discovery (LaserPol/RamanPol), setup keys (match_label), plan.setup_steps, instrument set, status.
  Sim: edge filter (45 cm-1, sim_edge_cm1), sim_filter_actual, LaserPol/RamanPol motors. 106 tests, 14/14 bench.
- 2026-11-20, 2026.11.20.21: Nicola: TESTING.md audited against the code. Stale: T0.2 (14 benchmarks), installer
  and any LLM app (not only Claude), T1.1 (GetValueSimplePart ramp probe), T1.2 (the automatic profile), T3 (via
  `instrument set`; no x50 here), T4.1 placeholder path; Q1, Q5, Q13, Q15, Q27, Q34, Q35, Q37, Q40, Q41, Q43 marked
  with what the 2026-11-20 files already answered. Order fixed (T4.8 before T4.9-4.11, T5b.6 before T5b.7, T6.4
  before T6.5-6.7; Q table numeric; results table complete). Redundant: T5b.4's hand copy of the offset (spot.json
  keeps it per objective and laser) reduced to a repeatability check; T1.3 only after a LabSpec update. New: T3.10
  polarisers, T3.11 cameras, T8.6 direct requests. 73 steps, 43 questions. 106 tests.
- 2026-11-20, 2026.11.20.22: report from the instrument: discovery (instrument discover twice, ls6t_discover.vbs
  once with the bridge stopped) opened LabSpec's About NFActiveX Control box and LabSpec crashed seconds after it was
  dismissed; a run on another Windows account worked. That run was bridge 2026.11.20.7, not .21: the account is
  probably not the cause, the version is. The bridge's start inventory (DiscoverTo without probes) ran fine on the
  failing account, so the crash is in the probe block, whose new part since .7 is the ramp probe (CreateDataObject,
  PutValue of a VBScript Variant array into the scratch spectrum's Data, GetValueSimplePart). Heap damage from that
  write would fit a crash a few seconds later; nothing in discovery calls AboutBox by name. Not confirmed on the
  instrument. Fix: discovery only reads (probe block removed; Q35 left open, Q37/Q40 already answered), motor names
  trimmed (no LaserSwitch = laser power, E-Cal_Neon = a lamp device, Fiber, Output, DuoPos, IlluminatorV, LaserFilter,
  FilterV); step log discovery_progress.txt (begin/end per step, file closed each time) and discovery_skip.txt (a step
  begun and never ended is skipped by later runs); bridge.labspec_hint names LabSpec's #32770 dialogs or says LabSpec
  is gone (EnumWindows, Windows only); api.instrument logs actions to instrument_log.jsonl, jobs shows them; discover
  failures report stopped_in; profile import takes a reviewed JSON (import_reviewed: nm and max_filter_pct required);
  format refuses the built-in profile in labspec mode; diagnose flags placeholders. 108 tests.
- 2026-11-20, 2026.11.20.23: Nicola's review of .22: power_mw required at import too; the laser may fire on the
  instrument only with a reviewed profile (also laser_on and snapshots with the laser, keeping the unconfirmed
  LaserShutter out of play); the About box stays unexplained (ls6t never calls AboutBox; possibly an early symptom of
  the same corruption): watch for it on .22+; both Windows accounts to run the same version (the installer is per
  user). profile.laser_problems / review_gate; import_reviewed sets 'reviewed' {time, from}; import_discovery drops
  it; format.validate_study refuses laser-firing kinds (all but move, background, white-light cutoff; or
  video.with_laser) in labspec mode without review; api.laser / snapshot(with_laser) likewise; tool_status
  profile.reviewed / review_needed. One review route (P.REVIEW_STEPS, used by the automatic profile's hint,
  review_gate, drift, discovery's message box and the docs): `ls6t profile propose` (api.profile_propose: the dump
  or discovery_latest.txt -> profile.proposed.json, current limits kept; also what instrument discover does), edit,
  `ls6t profile import` it. Editing profile.json in place is not the route: an automatic profile.json is rebuilt at
  the next bridge start. 109 tests.
- 2026-11-20, 2026.11.20.24: from the instrument (.23): camera_on top/0 left LabSpec on camera 1 though the
  answer said ok (the bridge never checked Video(10+id)'s return nor read the camera back); camera_off with the live
  video on never returned: heartbeat 'busy', no dialog, LabSpec running, i.e. Video(STOP_VIDEO) blocked inside
  LabSpec (the manual's example is ActiveX-mode, with GET_VIDEO_ID polled throughout; from a script it may wait for
  a loop that never comes). Fix: SetCamera (Video(10+id), Pause 300, read back; camera_before/after/
  set_returned in the answer) used by VCamera and VVideo, failing when LabSpec did not follow (message differs with
  the video live: stop it first; or the ids may not be OLConfig's order); STOP_VIDEO only with stop_video=1 from
  profile video.stop_from_script (default false; testmode forces the profile's value); VVideo leaves a video it
  started on (video_left_on). labspec_hint(verb): LabSpec running, no dialog -> stuck in a LabSpec call. Sim mirrors
  (sim_camera_locked). TESTING T3.11 / Q44. 110 tests.
- 2026-11-20, 2026.11.20.25: Nicola on .24: GET_ACTIVE_CAMERA is the selection, not the camera shown; with the
  video running a switch is reported while the view (and snapshots) stay on the old camera; a switch takes effect at
  a video start, which ls6t cannot cause while the video runs (STOP hangs); stopping/starting the video in LabSpec's
  UI ends the bridge script (last heartbeat idle), so .24's advice ("use LabSpec's video button") ends the bridge;
  one internal -> top switch with the video stopped failed (repeat to tell reproducible from timing). Fix: CameraFor
  (cam, live): same camera -> ok; another while live -> refused (what to do: stop by hand, restart the bridge, ask
  again); video stopped -> SetCamera (read-back polled up to 2 s, camera_set_s) then the video is started
  (video_started). VCamera and VVideo use it; camera_off refused while live (unless video.stop_from_script).
  Messages and BridgeGone say the UI's video button ends the bridge. Sim mirrors. TESTING T3.11 (three repeats; look
  at the view), Q44. 110 tests.
- 2026-11-20, 2026.11.20.26: from the instrument (.25 advice followed): video stopped by hand (ends the bridge),
  bridge restarted: it never answered, heartbeat busy from start, ls6t said stuck in 'ping' then, seconds later,
  'not running' (two code paths, two stories: the pre-call check said not running for any stale heartbeat). No
  script stop button in this LabSpec (the popup's Cancel only); after Cancel LabSpec froze (no restart of the script,
  STOP ALL dead). Reading: ping is one GetValue(SystemName); if it blocks, LabSpec's scripting was already wedged,
  most likely left so when the UI's video control ended the previous script (cause not proven). Fix: bridge.diagnose
  (hb) for both paths (no heartbeat / stopped / ended without stopping = last beat idle or halted / busy in <verb>,
  'inventory' at start-up) + labspec_hint with STUCK_ADVICE (nothing frees it; wait, restart LabSpec; not Cancel);
  instrument end_bridge (quit verb; refused while a request holds the lock) so the operator never uses LabSpec's
  video buttons under a running script; camera refusals give that order; popup text and all docs drop the 'script
  stop button'; TESTING ground rule 5, T2.4, T3.11, T9.2, Q2, Q44. Then T3.11's optional step: with
  stop_from_script true, camera_off hung inside Video(STOP_VIDEO) again (command camera, no dialog). Conclusion
  (Nicola): no reliable camera switching on this XploRA. Video(STOP_VIDEO) removed from the bridge altogether
  (stop_from_script removed); switching only with profile video.switch_camera (default false; for another
  instrument, video stopped): CameraFor(cam, live, allowSwitch) refuses another camera with the end_bridge
  sequence. Then, from LabSpec 6.8.1's SERVICE scripts (Nicola's request): StandardMapping / SWIFTMapping
  XploRA.vbs start the video with `Video START_VIDEO` + GET_VIDEO_ID polled until > 0, and stop it with `Video
  STOP_VIDEO` (a statement) followed by `GetMotorStatus("Video", MoveID)` polled until 0, with no Video() call in
  between; Cut-Off.vbs uses the manual's pattern. No script uses SET_ACTIVE_CAMERA. ls6t's stop called
  Video(GET_VIDEO_ID) 200 ms after STOP: a call into the video while it stops is the likely place of the hang (not
  proven). StopVideoHoriba reproduces HORIBA's sequence (15 s limit, heartbeat kept), opt-in via
  video.stop_from_script for TESTING T3.12. 111 tests.
- 2026-11-20, 2026.11.20.27: T3.12 result: StopVideoHoriba returned (live 0, status 0, 3.38 s); the next status
  timed out (15 s, 'no result ... (stop requested)'), then was reported stuck in status: its Video(GET_VIDEO_ID) /
  Video(GET_ACTIVE_CAMERA) right after the stop (the overlap HORIBA's scripts never make). Fix: common.vbs VidId /
  VidCam (no LabSpec video call while gVidOff, set by the bridge's own successful stop, with the camera read just
  before STOP); cleared when the bridge starts the video (then GET_VIDEO_ID polled, as HORIBA); VidSettle waits
  LS6T_VIDEO_SETTLE_S = 5 s after the stop before a START (to size from a hand-timed stop: T3.13). Status reports
  video_known_off. Bridge._timeout_text: stuck in <verb> (busy, heartbeat > 5 s old) with labspec_hint, or still
  working; says ls6t asked the bridge to drop the command. Popup: Cancel acts when LabSpec's current call ends.
  112 tests.
- 2026-11-20, 2026.11.20.28: Nicola's review of .27: every command must honour the bridge's own stop, not only
  status; a new bridge must not inherit it; camera 0 is a separate problem (ignored with the video stopped, even
  after a LabSpec restart): when switching returns, first check Video(10+0) after a clean stop + 5 s. Audit of every
  LabSpec.Video call: VidId/VidCam gated; SetCamera now VidSettle + VidCam first, and keeps gVidCam; StopVideoHoriba
  only when live; VCamera/VVideo START after VidSettle, then gVidOff cleared; GrabFrame only once the video runs;
  VAutofocus 'video' and the extended video refused while gVidOff; GetMotorStatus (moves, safe) is what HORIBA polls
  right after STOP, so left. test_every_video_call_respects_the_bridges_own_stop pins the call sites. gVidOff starts
  False in each script run and is never written to a file. CI (Windows, .27 tag): bench graphene_map failed; passes
  5/5 here (7.1 s): the job's printed check list is needed. 113 tests.
- 2026-11-20, 2026.11.20.29: T3.13 passed on .28 (stop 3.09 s, restart honoured the settle, no hang).
  stop_from_script default true. Camera switching rebuilt for T3.14 (switch_camera still false): CameraFor(cam,
  live, allowSwitch, canStop) stops a running video with StopVideoHoriba (camera_restart), SelectCamera (VidSettle,
  Video(10+id), no read-back while off: the T3.12 lesson), the caller starts the video (GET_VIDEO_ID polled, as
  HORIBA) and CameraCheck reads the camera while it runs ('LabSpec ignored the switch' otherwise). VVideo keeps a
  video it restarted for a switch running (keepOn). SetCamera (read-back polling while off) removed. Sim mirrors.
  Snapshot on the instrument: the top camera's raw frame had 888 x 606 x 3 values: video.decode now takes 1, 3 or 4
  values per pixel (_channels: interleaved or planar, whichever gives the smoother image; channel order from
  video.packing: colorref R,G,B, rgb B,G,R; data_dims with a channel axis); api.image_for_view: PNG <= 900 kB as is,
  anything else (size, JPEG/BMP/TIFF) reduced with Pillow (PNG, else JPEG, down to <= 900 kB) and saved as
  <name>.view.*; get_image uses it; an undecodable frame points to LabSpec's own image ('view'). 114 tests.
  CI: the Windows graphene_map benchmark failure (.18, .27 tags) is fixed (Nicola, 2026-11-20).
- 2026-11-20, 2026.11.20.30: .29 on the instrument: get_image shows the marked plot and LabSpec's JPEG (full size,
  under the limit); frames 888 x 606 x 3 (RGB) decode right (black: light off, holder empty). LabSpec's own PNG was
  overwritten by the plot (both <base>.png): the plot is now <base>.marked.png. Open: 888 x 606 is neither camera's
  sensor (OLConfig names IDS uEye USB3 cameras: the top one's family is 3088 x 2076, the internal one's 1920 x 1200,
  from public datasheets; models only, no serials here): likely LabSpec's video buffer (binning or the window);
  check the internal camera's frame size and LabSpec's camera settings (TESTING T5b.1).
- 2026-11-20, 2026.11.20.31: from the instrument (.29/.30): all motorised settings reach every value asked
  (laser 532/785.04/637.75, gratings 1800/600/2400/1200, filters 10/1/0.1 %, pinhole 100/500/300, slit 50/200/100),
  but set reported ok false with the old values: VMove waited only `If moveId > 0`, and MoveMotor (+NO_WAIT) gave no
  id here. Now WaitMotor always (gNoId, an Empty MoveID, as HORIBA's scripts' unset MoveID), then SettleReadback
  (value within 0.5 or the label, 200 ms polls, at most 30 s; settle_s, settle_timeout): mid-move positions (grating
  1800 between 2400 and 1200, pinhole 100 between 500 and 300) are not taken as arrived. The filter cap refused a
  whole request (right): errors now say nothing was changed. Objectives: the turret is manual; LabSpec's objective
  is a record, not a reading, and discovery/OLConfig list configured lenses, not mounted ones (hence the earlier
  x50). api.objective_question (manual_objective, default true): change by hand, confirm; used by set, the runner's
  set-up and kind move; the laser go-ahead asks whether LabSpec's lens is the one on the turret; checks.objective /
  set's objective / snapshot objective_source say confirmed-by-operator or LabSpec's unchecked setting; move.to_um
  warns when unchecked. 115 tests.
- 2026-11-20, 2026.11.20.32: Nicola's review of .31: read-back matching must not depend on formats (a mismatch
  would now cost 30 s and a false failure). profile.same_position (numbers within 0.5 with units %, nm, µm, gr/mm
  stripped; labels without spaces/case) used by runner._matches, set_instrument and kind move; the bridge's
  SettleReadback compares numbers numerically (it already did) and labels with NormLabel, and ends as soon as the
  reading is unchanged for 1.5 s (settle_stable). move.to_um on a manual turret now asks the operator to confirm the
  objective (the snapshot's µm scale comes from it; confirmations off: a warning). .31 at the instrument: set waits
  right (every move reported accurately, first read-back), objective source and the refusal message as intended;
  settle_s was measured by the bridge but dropped by set_instrument and the runner: api._move_timing passes seconds,
  settle_s, settle_stable, settle_timeout. 116 tests.
- 2026-11-20, 2026.11.20.33: Nicola: the 0.5 absolute tolerance must not apply to filters (0.1 % vs 0.01 % differ by
  0.09; the filter is the setting tied to laser power). same_position: a value in % matches only within 0.1 %
  relative (0.10 % = 0.1 %); positions keep 0.5 absolute. Filters move and read back by label (plan.setup_steps,
  set_instrument), compared exactly by the bridge (NormLabel). 116 tests.
- 2026-11-20, 2026.11.20.34: T3.15 passed on .32/.33: all moves ok, settle_s 0.0; whole-call times: filter ~1 s
  (36.6 s for the first move after the bridge started: watch), laser 1.9-2.8 s, pinhole 2.1-5.7 s (slowest; the
  'slow slit' of .29 was only last in line), slit 1.7-2.9 s, grating 0.2-0.3 s although it moves for seconds (.29).
  So GetMotorStatus("Grating", no id) said idle and the value read was the target at once: the grating was not
  waited on. HORIBA's scripts do MoveMotor("Grating", g, "", MOTOR_VALUE) (blocking, an id) then GetMotorStatus with
  that id; the manual documents WaitForAllMotors = GetMotorStatus("", 0). VMove now waits on the motor, then on all
  motors, then reads back; move_call_s / wait_s / all_wait_s / settle_s reported (api._move_timing). Also from the
  manual: MOTOR_NO_MESSAGE (100) stops LabSpec's own prompt for a manual motor: T3.2 records whether LabSpec asks
  as well as ls6t. 116 tests.
- 2026-11-20, 2026.11.20.35: .34 at the instrument: round trip ok, breakdown works. First set after each bridge
  start 30.9-45.4 s (.31, .32, .34; later sets 9-12 s): most likely the start-up inventory (the bridge takes
  commands only after it); DStep now runs BeatNow (Execute, ignored in the discovery script) and set_instrument
  reports timing {total_s, status_before_s, checks_s, moves_s, status_after_s, note when the heartbeat said
  'inventory'}. The 30.9 s call was dropped by the app ('No client-side handler found') though done: the
  instrument tool is async and reports progress every 5 s (_keepalive: asyncio.to_thread + ctx.report_progress; the
  error wrapper handles coroutines). Grating: move_call_s 0, wait_s 0.05-0.09 s, even 2400 -> 1200; with 637.75 nm
  1200 -> 2400 stopped at 1800 silently (settle_stable caught it). HORIBA's scripts move the grating blocking
  (MoveMotor without NO_WAIT, then GetMotorStatus with its id): VMove blocking=1, profile blocking_motors
  ['grating'] (runner.move_extra, used by set and the runner's set-up). OLConfig names gratings with ranges but has
  no laser pairing: no up-front refusal; a short stop reports laser and SpMin / SpMax (_grating_short). TESTING
  T3.16, Q45. Sim: sim_grating_stuck. 117 tests.
- 2026-11-20, 2026.11.20.36: Nicola's review of .35: (1) a blocking MoveMotor holds the single VBS thread, so no
  beats: Beat writes blocking=<motor> before and clears it after; Bridge.alive allows busy+blocking up to
  config.blocking_move_s (LS6T_BLOCKING_MOVE_S, 180 s); diagnose / _timeout_text say 'blocking move of the
  <motor>'. (2) the 2400 gr/mm grating vs 785 nm is physics: first order reaches at most 2/G (833 nm; Raman up to
  ~739 cm-1 at 785 nm, ~3680 cm-1 at 637.75 nm), less with the spectrometer's deviation angle. plan.grating_limit_nm
  / grating_reach (2e6/G, or gratings.<G>.max_nm when set from a measured limit / LabSpec's SpMax once its unit is
  known); format refuses ranges beyond it; set refuses a grating that cannot reach the laser line and reports reach
  + a note below 3200 cm-1. 118 tests.
- 2026-11-20, 2026.11.20.37: .36 at the instrument: the blocking grating move returned in 0.02-0.04 s (move_id 0,
  no wait) like NO_WAIT; the laser's status wait works (1.7-1.9 s). LabSpec changes the grating at once in
  software; whether the turret moves then or at the next acquisition needs someone at the instrument (T3.16 now has
  the observation and a first-vs-second acquisition timing). blocking_motors back to [] (the option stays). SpMax
  per grating reads as nm (600 1138, 1200 1112, 1800 935, 2400 690): DiscoverTo step 'spectro_range' (OperVar
  ACT_INSTRUMENT[SpMin|SpMax][i], read only, crash-skippable); import_discovery keeps it as gratings.<G>.spectro_nm
  when plausible (200 < SpMax <= 1.05 x 2e6/G); plan.grating_limit_nm takes the lowest of 2e6/G, max_nm, SpMax;
  grating_min_nm (SpMin); format refuses ranges outside; set no longer refuses a grating for a laser (LabSpec
  accepts 785 + 2400), notes the reach and that the turret's motion is not visible. T3.16 sequence fixed: laser and
  grating together from a different grating. First set after a bridge start: 0.21 s when idle first (inventory
  explanation holds). Sim discovery reports the same SpMin/SpMax. 120 tests.
- 2026-11-20, 2026.11.20.38: Nicola: this account's profile is a person's (stop_from_script set) but not reviewed,
  so the SpMax limits would wait for the review (import needs power_mw); and the two-spectra timing needs the laser.
  profile.spectro_ranges (shared with import_discovery) and _inventory_facts: load() puts the inventory's
  spectro_nm into a person's profile (only restricts; facts_from_inventory). Dark timing: kind background with
  laser_off operator passes the review gate (no laser) and now reports seconds.acquire and setup_moves seconds.
  TESTING: .37's T3.16 rewrite had gone to the wrong row (the first '| T3.16 |' is Q45's test column); rewritten
  now with the fixed sequence and the dark timing. 121 tests.
- 2026-11-20, 2026.11.20.39: Nicola asked whether anything keeps the laser off in a background with laser_off
  operator: no. The bridge's acquire closes the laser shutter only with laser_off=1 and a shutter motor (the
  'shutter' mode); 'operator' is a plain acquisition, LabSpec's shutter opening as usual. runner.BLOCK_BY_HAND in
  the background and cutoff notices (OK only at the instrument with the beam physically blocked; not from a remote
  session); format._blocked_by_hand: a warning always, and with an unreviewed profile (labspec mode) the filter
  must be the strongest attenuation. TESTING T3.16's dark timing: someone in the room, filter 0.1 %. 121 tests.
- 2026-11-20, 2026.11.20.40: .39 at the instrument: SpMax reached the person's profile and every set reports its
  reach (600 1138, 1200 1112, 1800 935, 2400 690 nm; the 2400 note fires). study_format's summary lacked it:
  profile.grating_reach_table -> summary['grating_reach']. Grating moves after the first answer in 0.06-0.09 s;
  the laser's wait is real (1.6-2.0 s); all_wait_s 0.0 throughout (kept: harmless). The slow first move is not the
  inventory: 24.1 s on the grating with status_before_s 0.21 s (bridge idle), 36.6 s on the filter another session:
  the first motor command after LabSpec starts is slow, whichever motor; possibly the only real turret move (T3.16
  now starts with it, after a LabSpec restart). The 600 s grating timeout and the MCP progress cover it. 121 tests.
- 2026-11-20, 2026.11.20.41: Nicola: both slow first moves followed a bridge restart, LabSpec perhaps not; T3.16 now
  has (a) bridge-only restart (remote, no watcher), (b) LabSpec + bridge restart with a watcher. VMove counts its
  MoveMotor calls (gMoveN -> bridge_move_n in each move's answer). If (a) is slow: a deliberate warm-up at bridge
  start (to design then: which harmless command warms it). 121 tests.
- 2026-11-20, 2026.11.20.42: remote check of .40: grating_reach right (2400: 1187 cm-1 at 637.75, 4304 at 532,
  none at 785.04). SpMin = 100 for every grating: not plausible: grating_min_nm uses only a person's
  gratings.<G>.min_nm; the table shows labspec_spmin. Third slow first move: 31.7 s (grating 1200 -> 600, all in
  wait_s, move_call_s 0, bridge idle). WarmUp at bridge start (after the inventory): GetMotorStatus by name for
  Grating, Filter, Laser, Hole, Slit, Spectro, polled while busy (<= 60 s each, beats), times in ping 'warmup' and
  LS6T_HOME/warmup.txt; nothing moves. Remote Desktop (Nicola's earlier suggestion, unanswered until now):
  config.remote_session (SM_REMOTESESSION or SESSIONNAME RDP-*; LS6T_REMOTE_SESSION for tests): hand-blocked
  backgrounds / cut-offs refused in labspec mode unless LS6T_HAND_BLOCK_REMOTE_OK=1; TESTING ground rule 7. Not
  detected: TeamViewer, Quick Assist. Still open, people's: the turret watch (T3.16), cameras with light and a
  sample (T3.14), the profile review and the laser shutter confirmation (the most useful step). 123 tests.
- 2026-11-21, 2026.11.20.43: .42 at the instrument (RDP): warm-up works: Grating 36.0 s, the other motors 0, the
  first grating move then 0.10 s (bridge_move_n 1): a one-time cost of the grating's first status query after a
  bridge start, LabSpec already running. The Remote Desktop refusal works. Cancel halted the bridge, its window
  went away, a second ls6t_bridge.vbs start did nothing (the halted bridge ran on; LabSpec may run one script at a
  time), resume fixed it. Now: OpenPopup says Cancel = HALT; when halted the main loop opens OpenHaltedPopup, whose
  closing ends the bridge (OperatorStop -> quit via gHaltPopup); VResume closes it; BridgeMain starts with
  OtherBridge (heartbeat <= 5 s old, not stopped): a message box with what to do, and exits. TESTING T2.3, rule 5.
  124 tests.
- 2026-11-21, 2026.11.20.44: T2.3 on .43 (RDP): the HALTED window works; a second bridge start does nothing (LabSpec
  runs one script at a time: OtherBridge kept, harmless, but its message never shows here); the main window
  'reappeared' after Cancel until LabSpec's call ended; the window reopened after resume looked like a new request;
  resume took 58.8 / 99.6 s (very likely queued behind the warm-up, 50.9 s this time, plus the operator's answer);
  both windows titled 'Non blocking'; two status calls timed out at 15 s during the warm-up. Now: HORIBA's
  '#title#button' suffix on the non-blocking messages ('ls6t: in control' / Halt, 'ls6t: HALTED' / End bridge);
  StopRequested and the warm-up open the HALTED window at once; WarmUp polls OperatorStop (Halt or the stop button
  ends it) and allows 120 s per motor; OpenMainPopup(resumed) says RESUMED; Bridge.call extends its deadline while
  the heartbeat says inventory/warmup (STARTUP_MAX_S 600; waited_for_bridge_start_s).
- 2026-11-21, 2026.11.20.45: Nicola: the slow resumes were
  not the warm-up (it had finished; status answered quickly while halted, so the halted loop takes commands): the
  time is inside VResume, i.e. the Yes/No prompt (shown late, hidden behind the HALTED window, or slow to answer over
  RDP). VResume now closes the HALTED window first (reopens it on No) and reports prompt_shown_after_s /
  answer_after_s (gCmdT0 set when HandleFile takes a command); api adds picked_up_after_s. Bridge.heartbeat retries a
  read that catches the file mid-rewrite (empty / no state), which also made a test flaky. 126 tests.
- 2026-11-21, 2026.11.20.46: Nicola: prompt_shown_after_s is when the bridge asked; a question LabSpec shows late
  (or behind a window over RDP) counts as answer_after_s. api resume watches labspec_windows() every 0.5 s during
  the call (Bridge.call on_wait) and reports dialog_appeared_after_s / dialog_title (first LabSpec dialog not titled
  'ls6t:'); z-order is not visible, so TESTING T2.3 asks the person to note when it became visible. 126 tests.
- 2026-11-21, 2026.11.21.1 (versions follow the date from now on): fresh Windows login, LabSpec and bridge started
  fresh: warm-up 0 s for every motor. The slow first grating status (36.0, 50.9 s) followed bridge restarts within
  a running session: neither LabSpec's start nor the bridge's alone. Logged per bridge session from now on:
  WarmUp's gWarm starts with started=<time>;grating_at=<value>; api.log_warmup (on instrument status) appends one
  record per bridge start to warmup_log.jsonl with bridge.session_context() (LabSpec's process start via
  GetProcessTimes, labspec_running_min, windows_up_min via GetTickCount64) and the last grating set from
  instrument_log (set rows now carry their setup). Sim ping reports a warm-up. T2.3 on .46: all works; resume
  picked_up 0.22 / prompt 0.02 / answer 10.1 s (the old 58.8 / 99.6 s: the question hidden behind the HALTED window);
  #title#button confirmed. dialog_appeared_after_s missing: the watch looked only at #32770 dialogs of labspec*.exe;
  now any new top-level window (hwnd not there before) of labspec* or OL* helper processes, not titled 'ls6t:',
  with class and process; None when watched and nothing seen. Warm-up 0 s on a fresh login and on a bridge-only
  restart: Nicola now tests whether grating moves before a restart are the trigger. Then: 0 s also after two clean
  End-bridge restarts (one after two grating moves); all five slow cases followed a deploy. The installer never
  touched a running bridge (it only rewrote the scripts): winsetup.end_running_bridge (quit, wait for 'stopped';
  not while a request holds the lock) runs before install_bridge in the installer and in `ls6t bridge install`,
  logging deploy_log.jsonl; log_warmup adds last_deploy. 128 tests.
- 2026-11-21, 2026.11.21.2: 2026.11.21.1 at the instrument: dialog_appeared_after_s 0.31 s ('LabSpec', class OLGui,
  labspec64.exe: not a #32770, hence the earlier miss); resume picked up 0.18 / prompt 0.02 / answer 10.9 s.
  prompt_shown_after_s was '1.953125E-02' (CStr): common.vbs Secs(x) = Fmt(Round(x, 2)) for every Elapsed(); api
  rounds resume's timings. Warm-up: this deploy found the bridge already stopped, yet Grating 29.5 s with LabSpec
  started 1.2 min before: the likelier cause is the grating initialising for about a minute after LabSpec starts
  (warmup_log's labspec_running_min will tell); the installer's clean ending still to test with a running bridge.
  129 tests.
- 2026-11-21, 2026.11.21.3: 2026.11.21.1 at the instrument: the installer ended a running idle bridge cleanly
  (deploy_log bridge_was idle, ended cleanly); its window closed at Finish with no message: the welcome page now
  says to quit the LLM apps (Claude from the tray) and that LabSpec may stay open, the finish page that a running
  bridge was ended and must be started again; INSTALL.md the same. Warm-up settled: Grating 29.5 s with LabSpec
  restarted right after the deploy (bridge 43 s later; ready ~73 s after LabSpec's launch), 0 s with LabSpec
  running 8.3 min: the grating initialises for about a minute after LabSpec starts (later revised: 2026-11-22 a
  bridge 26 s after LabSpec had 0 s: likely at best, under observation); WarmUp absorbs it. The
  scientific-notation duration was fixed in 2026.11.21.2. 129 tests.
- 2026-11-21, 2026.11.21.4: Nicola: the welcome page said a running bridge 'gets started again at the end'; the
  installer cannot start a LabSpec script: it now says the operator starts ls6t_bridge.vbs again after the
  installation, as the finish page does. (The 2026.11.21.2 tag was pushed after .3 on the same code; removed.)
- 2026-11-22, 2026.11.22.1: stage tests on 2026.11.21.4 (remote, empty holder): XY right (encoder read-backs,
  0.73 s / 580 µm, 1.14 s / 4950 µm), stay true/false right, max_xy_um radial (5657 refused), Halt mid-request
  right, Y creep 0.1 -> 0.3 µm over three round trips; video stop/start x4 fine (stops 3.05-3.40 s, starts 6.9-10.2
  s). Changes: runner state 'returned' {done, why} and 'stage_at_end' (with from_home_um) for every request, also
  stopped ones (status exposes both); moves report objective + objective_source (_objective_source); summary
  limits_meaning; maps checked radially (farthest corner). Nicola's Z safety: plan.WD_BY_MAG_UM (Olympus MPlan N
  class: 100x 210, 50x 350, 20x 1300, 10x 10600, 5x 20000 µm) / objectives_wd_um; plan.z_limits (toward =
  min(max_dz, z_toward_wd_fraction x WD), away = max_dz, toward_sign from stage.z_into_sample or None = both ways
  limited), z_check, toward; format._z_safety (map z_um, points / move / focus dz, focus-scan half range;
  sample.unevenness_um >= WD/2 -> XY within focus_valid_xy_um); runner._z_guard in move_z (limits; toward + stale
  focus -> Stop unless a focus scan), _focus_here after a focus is found, _retract_before_xy in the map loop. TESTING
  T3.17 (Z direction by watching, x10, empty holder). VBS: res.Exists for camera_asked / camera_restart (reading a
  missing Dictionary key adds it, empty). 131 tests.
- 2026-11-22, 2026.11.22.2: Nicola: dry runs: api.inputs drops the review-gate error into review_note (nothing is
  sent); acq_range single window adds center_nm, window_nm (from_nm = to_nm = centre is LabSpec's parameter: the
  report read it as the range, 659.66 nm vs 646.6-672.7); estimate: plan.setup_parts (commands, laser, grating,
  objective, filter, hole, slit), acquisitions, video_snapshots, not_included (the operator's answers); default
  timing laser 3 s, grating 5 s (measured; a saved profile keeps its own). T3.17 rewritten: a 5 µm move cannot be
  seen against a 10 mm gap; the maintainer moves ~1 mm toward the objective in LabSpec with x10/x5 while ls6t reads
  Z before and after (no exception to ls6t's limits), or HORIBA's documentation; unset unless unmistakable. Depth:
  z_limits(purpose='depth') uses limits.depth_toward_wd_fraction (0.3); format marks the derived map _kind depth
  (internal '_' keys skip the unknown-key check); runner._confirm_depth asks in LabSpec when the profile goes past
  the ordinary toward limit; ctx.z_purpose while it runs. Then (Nicola): after a halt with nothing moved the
  message suggested the stage was away (runner._not_returned: 'nothing moved away; at the home spot', or the
  distance it stays); 'resume it in LabSpec' -> `ls6t instrument resume` (runner, bridge, sim); _stage_now gives
  from_home_um also when the request stopped before taking its home spot (home = here). 134 tests.
- 2026-11-22, 2026.11.22.3: Nicola's retract-travel-return: plan.retract_um (limits.retract_um 2000, away only),
  travel_mode (move.travel, or auto with sample.unevenness_um beyond focus_valid_xy_um), coarse_focus_step_um
  (half the measured axial FWHM, else by magnification); z_limits purpose 'travel' (away up to retract_um);
  runner.travel_xy (retract from the focus, travel, descend to focus - unevenness; _z_direct for these away moves),
  run_move uses it and says Z is short of the focus; focus_scan from_below -> _focus_from_below (coarse steps toward
  the sample to old focus + toward limit, stop past a clear peak (> 3x the first steps' median), fine symmetric scan
  around it). Optional stage.retract_for_objective_change (api.objective_retract, set and the runner's set-up;
  ctx.focus_stale until a focus is found). format: move.travel, focus.scan.from_below, both need
  stage.z_into_sample; a retract move is exempt from the unevenness refusal. sample.notes -> LabSpec Remark (bridge
  'remark'). Warm-up cause revised to 'likely, under observation' (a bridge 26 s after LabSpec: 0 s). TESTING T6.8.
  CI: graphene_map failed again on Windows, intermittently. Likely cause: on Windows os.replace raises
  PermissionError while another process has the target or source open (study_status reading progress/state while
  the runner rewrites them, a virus scanner on new files); in the runner (write_json) that ended the request, in the
  simulator's command loop it killed its thread. bridge.replace_retry (100 x 20 ms) for write_json, the bridge's
  command files, the sim's results / heartbeat / spectra, profile and spectra saves; the sim's loop no longer dies
  on a write error and retries a locked command file's removal. Not reproducible on Linux; the next tag's CI runs
  will tell. 134 tests.
- 2026-11-22, 2026.11.22.4: CI log (graphene_map, Windows): state cancelled 2.4 s in, 'move_xy: the bridge ended
  without stopping (last heartbeat idle 0 s ago)': Bridge.heartbeat returned None on an OSError (PermissionError while
  the simulated bridge replaced heartbeat.txt), alive() then False, while the next read (diagnose) saw it fresh. Now
  heartbeat() retries locked / half-written reads (10 x 20 ms; None only if the file is really absent: 3 misses) and
  call() looks a second time (0.3 s) before raising BridgeGone, before and during a call. Nicola: the focus search
  from below needs the laser (and a reviewed profile); move.focus 'video' ends a retract-travel-return with LabSpec's
  video autofocus (bridge autofocus how=video; no laser; the live video must run: the bridge refuses it after its
  own video stop), accepted only within unevenness + toward limit of the old focus (_video_focus_after_travel), else
  back to the descended height with a warning. TESTING T6.8 starts without the laser. Then (2026.11.22.3 at the
  instrument): dry run, window_nm, per-motor estimate and halt wording confirmed; settings already in place were still
  sent and charged (the objective: 20 s, and the operator would have been asked for the lens already mounted).
  plan.setup_steps carry a role; plan.already_set (same_position, or match_label for the objective) against a status;
  marked_setup_steps / setup_parts / estimate / inputs take current; api.inputs reads the status when the bridge runs
  (state_read); runner._setup and api.set_instrument skip steps already in place (already_set in the read-back).
  TESTING T4.0 (dry run), T4.2 (sample.notes in LabSpec's Remark). 137 tests.
- 2026-11-22, 2026.11.22.5: Nicola: a post-check of LabSpec's autofocus comes too late if its search passes the
  safe limit on the way. The ActiveX manual's Autofocus has START, GET_STATUS, STOP (restores the previous
  settings), GET_OFFSET, enable/disable and the method; no range; templates have only 'AutoFocus' on/off; HORIBA's
  service scripts set no range either. So: VAutofocus takes z_bound / z_sign, reads Z at every 100 ms poll, stops
  the autofocus (2) and the Z motor when Z passes the bound, fails with z_bound_hit; reports z_seen_min/max.
  runner._af_bound for autofocus inside studies (focus + toward limit); _video_focus_after_travel: bound =
  min(unevenness + toward, WD - unevenness - WD/10), none (skipped) when <= 0; the result records z_bound_um,
  z_seen_um, stopped_at_bound. Sim: sim_af_range (+-20 µm by default) and the bound. Overshoot after the bound is
  passed: one poll (100 ms) of Z travel plus the stop. Also: set_instrument lists the objective already in place
  (already list, already_set) and adds the grating note only when a grating move was sent. 138 tests.
- 2026-11-22, 2026.11.22.6: Nicola: measure Z speed and the stop latency, trip earlier by the worst-case overshoot,
  refuse when no room; x10/x5 first. VAutofocus measures z_speed_um_s (max |dz|/dt between polls), poll_s (max
  interval), and on a trip stop_s (until two equal Z readings 50 ms apart) and overshoot_um. plan.af_motion (largest
  measured from autofocus_motion.json, else profile autofocus.*, else AF_ASSUMED 200 µm/s, 0.5 s, 0.15 s;
  margin = 1.25 x speed x (poll + stop)) and record_af_motion (after every guarded autofocus). runner._af_bound
  (studies) and _video_focus_after_travel trip at bound - margin and refuse (Stop / skipped 'no safe room') when that
  is <= 0. TESTING T6.8 (x10/x5 first; one deliberate trip to measure the stop), T5 note. Sim reports a speed and,
  on a trip, a 2 µm coast. 138 tests.
- 2026-11-22, 2026.11.22.7: Nicola (on .22.6): set with the objective already in place reported objective.source
  'changed by hand and confirmed by the operator'; the 'asked' test counted the already_set entry; now it needs a
  change actually made. 138 tests.
- 2026-11-22, 2026.11.22.8: Nicola's first real snapshot (Si, x10, top camera): frame rows reversed and R/B swapped
  against LabSpec's JPEG; scale 1/px (LabSpec's axes); marker outside; +100 µm X -> -101 px, +100 µm Y -> +102 px
  (flipped). video.detect_orientation (correlates with the saved jpg/png: flips by grayscale, swap by channels;
  stored per camera in video_orientation.json when score >= 0.5 and margin >= 0.05), apply_orientation, stage_axes
  (video_scale.json "<objective>@<camera>" if measured, else field_um / pixels around stage X,Y; signs
  video.image_x_right / image_y_up), image_shift (phase correlation, sub-pixel). api.video_scale (instrument action,
  CLI video-scale, step 5-500 µm, default 50; cross-talk > 10 % fails; returns home). Bridge VVideo returns the
  camera. Nicola's per-objective test (x10 100 µm -> 101-102 px, 0.98-0.99 µm/px; x100 10 µm -> 103-105 px, 0.096;
  profile fields 0.972 / 0.0963; ls6t's axes 1.00 for all, x5 field stayed 887 µm) confirms it. T3.2: LabSpec asked a
  second time after ls6t's question: runner.move_extra(asked) adds no_message=1 (profile suppress_labspec_prompt,
  default on), the bridge adds LS_MOTOR_NO_MESSAGE (100, 2018 manual; not in 6.8.1's constants.vbm) and retries
  without it if refused (no_message_refused); api._move_timing reports labspec_prompt. Black x5 frame: features
  divided by a ~1e-15 MAD (strength 1e9): video.frame_too_dark (p99 < 8 or p99 - p1 < 4) skips features and
  warns (frame_dark), DOG_NOISE_FLOOR 0.1. Snapshot with LabSpec's 1/px axes and no field warns. 140 tests.
  T3.14 on .22.7: clean stop, settle, Video(11) -> 0, restart: the view stayed on camera 0. Nicola asked whether
  10 + id is the right code: LabSpec 6.8.1's HELP\VBS\Video.html documents long Video(long Mode) with
  '10+CameraID : SET_ACTIVE_CAMERA' (constants.vbm SET_ACTIVE_CAMERA = 10): it is. CameraCheck now adds
  camera_note (GET_ACTIVE_CAMERA is the selection, not the view). TESTING T3.14b: Video(11) from LabSpec's own
  script window with no bridge, then a start by hand. video.switch_camera stays false.
- 2026-11-22, 2026.11.22.9: Nicola's review of .22.8: (1) orientation learnt from a black frame would be stored
  and applied later: video.orientation_for learns only when not frame_too_dark, score >= 0.5, margin >= 0.05;
  swap_rb only when the channels can tell (|crossed - same| > 0.1, ref R/B corr < 0.98), else kept
  (swap_from); otherwise the stored one applies; a warning only when a reference existed. video_scale uses it.
  (2) video_scale: --um / um (MCP, api), default SCALE_STEP_FRACTION 0.2 of the field width (measured scale,
  OLConfig field, or LabSpec's axes when not 1/px), two significant figures; > half the field refused; black
  first frame refused; nothing moves before the checks. (3) move_extra(confirmed_now): only the objective
  motor, only right after the Yes (runner move study: local confirmed_now, not objective_by_operator).
  (4) marker_measured / marker_source ('ASSUMED ...'), plot label 'assumed'. 141 tests.
- 2026-11-22, 2026.11.22.10: Nicola asked for Codex and Cursor at install. Codex was there (SecCodex,
  ~/.codex/config.toml). Cursor added: clients 'cursor' (~/.cursor/mcp.json, JSON mcpServers, entry type 'stdio';
  detect: ~/.cursor, %LOCALAPPDATA%\Programs\cursor\Cursor.exe, /Applications/Cursor.app, `cursor` on PATH);
  winsetup --cursor; installer SecCursor, /CURSOR, ticked when found; CI installs /CURSOR and checks the entry.
  No timeout key in Cursor's file (progress keepalive covers long waits): to be tried on the instrument PC. 141 tests.
- 2026-11-22, 2026.11.22.11: Nicola: all four more apps. clients: claudecode (CLAUDE_CONFIG_DIR or ~/.claude.json,
  top-level mcpServers = user scope, edited directly: `claude mcp add --scope user` misplaced entries in some versions,
  anthropics/claude-code#16728; type stdio; warning to end sessions), copilot (COPILOT_HOME or
  ~/.copilot/mcp-config.json, portable format type stdio + tools ['*'], a person's tools kept; VS Code's profile
  mcp.json, deprecated 'servers' format, untouched), windsurf (~/.codeium/windsurf[-next]/mcp_config.json),
  lmstudio (~/.lmstudio/mcp.json, Cursor's format). ALIASES (claude-code, lm-studio, vscode, github-copilot), case-
  insensitive. detect: folders, %LOCALAPPDATA%\Programs\{Windsurf,LM Studio}, VS Code's github.copilot* extension,
  claude / copilot / windsurf / lms on PATH. Installer: four sections, /CLAUDECODE /COPILOT /WINDSURF /LMSTUDIO,
  welcome text 910 chars (< 1024). CI installs all and checks each file. INSTALL step 8 notes and approval settings.
  None tried at the instrument yet. 141 tests.
- 2026-11-22, 2026.11.22.12: Nicola: a third option on the installer's finish page, prompts to try. PROMPTS.md
  (first steps, first measurements, advanced, tips; rendered to docs\PROMPTS.html by build_installer DOCS). NSIS:
  MUI2's finish page has two options only: MUI_PAGE_CUSTOMFUNCTION_SHOW FinishShow adds an nsDialogs checkbox under
  ShowReadme at the Run-to-ShowReadme spacing (GetWindowRect + MapWindowPoints), their font and MUI_BGCOLOR,
  checked; FinishLeave opens PROMPTS.html. Start menu 'Prompts to try'. CI checks docs\PROMPTS.html. The page's
  look to be checked on the built installer (makensis not available here). 141 tests.
- 2026-11-22, 2026.11.22.13: Nicola installed .12: it ended 'with warnings' because connect always set the
  Claude Code warning (no session was open). clients.claudecode_running (Win32_Process claude.exe / node.exe:
  is_claudecode_process: claude.exe outside WindowsApps / AnthropicClaude / Claude-3p, or node with claude-code on
  the command line; an unreadable path counts as not Claude Code) gates the warning.
  install.log (Nov 20-22): 'ended (cleanly)' only for .21.1 / .21.4. Sim check: end_running_bridge ends a running
  bridge cleanly; the .12 20:23 install found it stopped (Nicola: deploy_log bridge_was stopped, ended not running),
  so not a proven regression; .22.4 / .22.6 to be checked in deploy_log. Now: 'no running bridge found' logged either
  way (no heartbeat / stopped N s ago / stale heartbeat state, verb, age, with what to do), a failed check warns
  (was silent), deploy_log adds heartbeat_age_s, verb, bridge_version. Store Packages copy kept an old ls6t entry
  from .24: clients.clean_stale removes any ls6t server (is_ls6t_entry: args name labspec_translator.mcp_server)
  from files Claude no longer reads, reports each (removed / not ls6t's / no entry / error). Warm-up: LabSpec
  20:29:56, bridge 24 s later, grating 0 s (with 18 s and 26 s also 0 s today): LabSpec-start explanation weak;
  Nov 20-21 slow cases unexplained. 142 tests.
- 2026-11-22, 2026.11.22.14 (.13 was already tagged): Nicola: the Store build read the Packages copy on Nov 20 (the placeholder error
  came from it), so a Claude-Data folder is no proof: claude_dirs' 'moved' now needs the app's .user-data-left marker;
  without it both the Packages copy and Claude-Data are written and nothing is removed. TESTING T0.3: after .14, quit
  and restart Claude, tools present? running_app_reads, marker present? deploy_log for .22.4 / .22.6: bridge_was
  stopped, ended not running: only the bridge's own exit writes 'stopped' (a running one beats every second), so no
  regression. 142 tests.
  T0.3 passed on .14: running_app_reads Claude-Data\claude_pzs8sxrjxfjjc, 15 tools in 2.48 s, tools present after a
  restart; marker .user-data-left.claude_pzs8sxrjxfjjc present (122 bytes, 21:17, the install and restart minute:
  written by the app; ls6t only reads it: clients.claude_dirs iterdir, ls6t_diagnose.ps1 lists it). Deploy: bridge
  stopped, not running; warm-up 0 s, bridge 20 s after LabSpec (a fourth early start at 0 s).
- 2026-11-22, 2026.11.22.15: Nicola: the old Packages folder holds only the marker (no config file): the install log
  now says so (claude_stale_files(existing_only=False) in clean_stale: 'no config file here: ... moved to Claude-Data').
  142 tests.
- 2026-11-23, 2026.11.22.16: Nicola on .22.15 (Si, x10, top camera): orientation learnt (flip_y, score 0.996, no
  swap); video-scale x10 0.977 / 0.982 µm/px, cross-talk 0.05 px, image up = stage -Y, stored x10_VIS@0; bright field
  0.981 / 0.979, cross-talk 0.84 px (about 0.4 % repeatable; features dark spots); move test within ~0.6 µm over
  100 µm. Fallback axes had Y reversed (+153 vs -155 µm). video.axes_directions: measured > learned from the same
  camera (any objective in video_scale.json) > profile video.image_x_right / image_y_up when set > assumed;
  field_um helper; snapshot directions_from + warning when assumed; runner move.to_um raises Stop when the
  objective has a field and the directions are assumed. Nicola: twice LabSpec showed bright field with dark-field
  light after a video restart / objective change (toggling fixed it). LabSpec's own reading would repeat its error,
  so video.brightness_check (video_brightness.json, mean grey per objective@camera, warn at 2.5x either way) on
  every snapshot without the laser. Re-asserting the mode needs the illumination motor: IlluminatorV appears among
  LabSpec's configured motors (labspec_ini); its positions and role to be confirmed (TESTING T5b.2).
  Nicola (x100 video-scale table): 17 µm 4 % mismatch, 35 µm Y left the 58 µm field, 25 µm consistent. Now:
  SCALE_STEP_FRACTION 0.4 of the smaller side, X / Y limits against width / height, aspect and rotation_deg stored and
  compared per camera (SCALE_AGREE 1 %, SCALE_ROT_DEG 0.5; 'disagrees' kept in video_scale.json, warning). Sim: the
  periodic texture made phase correlation pick false peaks: specks added (seeded, 1500 within 600 µm), sinusoid
  weakened; image_shift checks the phase-correlation result against the frames' overlap mismatch and otherwise
  searches (4x reduced, along the moved axis, +-10 % across), integer then parabola. Nicola: IlluminatorV (Dark field,
  Bright field) and uLight (Off, BrightField, DarkField1-4, Full) are the illumination controls; LaserSwitch (Off, On)
  switches a laser. motors.illumination / illumination_light from discovery and labspec_ini; status and the video
  verb read both; illumination_mismatch warns; api.illumination_set (through the other position when LabSpec
  already shows it) for set --illumination, after an objective change in set and after a camera_on restart.
  Bridge VMove and the sim refuse LaserSwitch. Proposal: laser_shutter never set (laser_shutter_candidate + note),
  caps matched by line (+-0.5 nm), a new laser at AUTO_MAX_FILTER_PCT. autocal.assess written_for_labspec;
  info version_warning against discovered.labspec_version (now recorded). 147 tests.
- 2026-11-23 (note, no code change): Nicola: while the bridge runs, LabSpec's own grating control is blocked (the
  operator cannot change the grating in LabSpec's interface). Possibly linked to T3.16 (grating `set` answered in
  0.02-0.3 s while the turret moves for seconds; first grating status 0-51 s after some bridge starts): LabSpec may
  treat a running script as holding the spectrometer, and carry out script-requested grating changes differently
  (queued, deferred to the next acquisition, or reported before the motor moves). To find out (TESTING T3.16):
  which controls are blocked (grating only, or laser / filter / slit / hole too); blocked from the bridge's start
  or only after its first grating command; released at once by End bridge; and whether a grating changed in
  LabSpec with the bridge ended, then the bridge started, gives a fast or slow first status.

- 2026-11-23, 2026.11.23.1: Nicola (.22.15, 637.75 nm, x100): 1800 -> data "-1016 to -974 nm", Spectro -996.66; 1200
  -> 646-728 nm centred ~687 (asked 659.66); 520 and 1500 cm-1 studies with all motors set gave the same window; the
  1200's window measures 85-87 nm (profile 39). Cause found: the bridge passed the centre only as Acq From = To, which
  LabSpec does not use to move the spectrometer. VAcquireInner: single window -> WaitMotor all, MoveMotor Spectro
  (MOTOR_VALUE, blocking, beat), wait, read back spectro_nm. plan.grating_window_check + window_store
  (grating_windows.json, learnt when centred) + runner.grating_guard on the first spectrum of every study (and
  cut-off, background): stop on impossible nm, centre > 1/4 window off, Spectro read-back > 1 nm off, width > 20 %
  from the learnt one; warn on profile window_nm (defaults look ~2.2x small). Also: save_native was overwritten by
  `native = map_engine == native` (no .l6s, Remark not checkable): native_map; note_grating_set
  (last_grating_set.json) from study setups and set, read by the warm-up log. T3.16 item 4: after a hand grating
  change, the restarted bridge's inventory sat in LabSpec > 76 s (LabSpec restarted). Installer: PROMPTS.html did not
  open from FinishLeave: state kept in $PromptsWanted (OnClick and leave), opened in .onGUIEnd after a completed
  install ($InstallDone); fixed fallback position when MUI's checkboxes cannot be measured. 148 tests.

- 2026-11-23, 2026.11.23.2: Nicola: the prompts checkbox shows and something tries to open, but nothing appears.
  ExecShell from .onGUIEnd (and from the finish page's leave) runs in the installer, which exits at once; the shell's
  hand-off to the browser can die with it. OpenWithExplorer: Exec explorer.exe "<file>" (its own process) for the
  prompts, the guide (MUI_FINISHPAGE_SHOWREADME_FUNCTION OpenGuide) and the scripts folder. 148 tests.

- 2026-11-23, 2026.11.23.3: the maintainer's Codex session (.22.16, rollout log): connect (old halted heartbeat 67 h:
  diagnose now says halted); profile reviewed and imported (powers 532/638/785: 100/35/100 mW) from the Nov 20
  proposal, so no objectives_info, cameras or illumination motor (profile._ini_facts: OLConfig descriptions added
  at load when missing, cached by file mtime, never replacing a person's value; facts_from_ini). video_scale x10 on
  the internal camera (1): X -213 px for 240 µm (1.126 µm/px), Y NaN (move out of view: image_shift returns NaN only
  when nothing overlaps; frac guards inf); with ls6t's live video on, no shift at all: the live image froze (LabSpec
  help: in ActiveX mode GET_VIDEO_ID must be called during the entire video) -> common.VidKeep (gVidOwn, every 0.2 s
  from the main loop, WaitStoppable, WaitMotor). Illumination fell to dark field after video starts -> IllumBefore /
  IllumRestore around the bridge's video starts (VCamera on, VVideo), camera verb now gets motor_args. Top camera = 0
  confirmed (camera 1 internal); x10@0: 0.977 / 0.982 µm/px with 50 µm (repeats Nicola's). Silicon 532 nm 100-1000
  cm-1 5 s: ls6t declared the bridge stuck after 20 s inside 'acquire' while LabSpec was still collecting (Acq holds
  the script for an extended range) -> gBlocking 'acquire' + blocking_s (the command's timeout) in the heartbeat,
  bridge.blocking_allowance. Seen once: a snapshot read Y -81.1 µm right after status read -0.2 (unexplained).
  149 tests.
- 2026-11-23, 2026.11.23.4: code audit (audit.pdf): Nicola chose the low-risk cleanups now, the core refactors
  for later (planning.md, Deferred). Done: version only in __init__.py (pyproject dynamic attr; 25 'Version:' lines
  and the guides' version stamps removed); README Versions -> CHANGELOG.md (78 rows, newest first); planning State
  log -> planning_log.md; store.py (path / read / write, sim_ prefix, replace_retry) used by perf, xcal (as jstore:
  xcal has its own store()), spot, background index, video's three stores and plan.window_store; profile.stamp and
  profile.isfinite removed (no callers; the audit's render.is_finite does not exist); tools/ls6t_fix_claude.ps1
  removed; tests split (conftest.py autouse env, helpers.py, 9 files, 139 test functions, 148 with parameters).
  Not done, with reason: typelib.py stays (the installed ls6t-typelib command); testmode.py and
  script_motor_main.vbs stay during bring-up. Release routine from now on: bump __init__.py, a CHANGELOG.md row, a
  planning_log.md entry.
- 2026-11-23, 2026.11.23.5: the maintainer's afternoon session (Codex rollout, on .23.3): status, snapshot, set,
  video-scale x10@0 (0.9798 / 0.9802 µm/px, 240 µm default move, aspect 0.9996, rotation -0.12 deg), get_inputs,
  map validation and jobs all as expected. Si 532 nm 1 %, 5 s: 520.94 cm-1, FWHM 8.31. Calibration at 1 %, 1 s: weak
  line fitted at the window edge, 525.12 cm-1, notice asked -4.42 cm-1; the operator ran LabSpec's AutoCalibration
  (bridge ended): PASS, 520.64, Offset shift 34 -> 44 (not a reliable unit estimate: the 525.12 was a bad fit).
  Restarted bridge stuck in 'inventory' (LabSpec froze, restarted). Calibration at 10 %: 520.327 (-0.373), pass.
  Alignment: no baseline / settings for 532 (expected). x10 -> x100 + focus scan: best at the edge (old focus),
  kept; spectrum off. Fixes: _calibration_result 'trusted' (edge warning or snr < checks.min_snr) -> unreliable, no
  notice, no xcal store; notice adds Offset-shift steps from calibration.offset_step_cm1 when set; range check
  tolerance max(3, 0.5 % of span); bridge InventoryNeeded (missing, >= 7 days, inventory.flag); objective question
  says focus by eye. AutoCalibration script still not found by autocal_info (LabSpec's menu has it). 149 tests.
- 2026-11-23, 2026.11.23.6: Nicola ran the T3.16 list (Codex, .23.3, guardian logs): centre fix confirmed - 1200 @
  637.75 nm, 520 / 1500 cm-1: Spectro asked / read 659.6251 / 659.6308 and 705.2124 / 705.2123 nm, windows differ,
  widths 87.37 / 85.36 nm; 1200 -> 1800 -> 1200 -> 600: widths 46.66, 87.37, 197.79 nm, all centred, checks passed,
  .l6s saved. 2400 @ 637.75: LabSpec's modal 'ICS correction file does not exist, Error Code=2070' on every
  acquisition (2400 blazed at 400 nm; LabSpec labels 600 (750nm), 1200 (750nm), 1800 (450-850nm), 2400 (400nm)).
  ACQ_NO_ICS = 100000 / ACQ_ICS = 200000 (ActiveX manual): profile ics.off_for, acquire.ics, Ctx.call adds
  ACQ_NO_ICS where listed, timeout text names the dialog. Nicola: 785 nm reach 600 3951, 1200 3745 cm-1 (= ls6t's
  SpMax numbers); flag ranges no grating reaches: validation without setup.grating lists the gratings that reach,
  refuses when none, runner stops if LabSpec's grating cannot; < 100 cm-1 without a measured cut-off warned.
  The default profile's window_nm stay generic (the simulator uses them); the XploRA's are set from the warnings.
  151 tests.
- 2026-11-23, 2026.11.23.7: Nicola: no ICS for 785; shared the HORIBA service folder (Horiba Jobin Yvon: 1-QC
  QC Auto reports, 2-Production Files, 3-Install, a LabSpec 6.7.1 backup). Found: ICS spectra in QC Auto\Spectro\ICS
  for 532 (600/1200/1800/2400) and 638 (1200/1800); LabSpec's runtime corrections are
  Parameters\ACQUISITION\Responses\Response_<CCD>_<laser label>_<grating label>.l6s (the 6.7.1 backup also had 638/600
  and 785/600/1200/1800; the 6.8.1 install has none for 785, per Nicola) -> plan.ics_files / ics_off auto (profile
  ics.auto, default on), `ls6t profile ics`. QC reports (key=value, UTF-16): cut-off 532 41.6-77.5, 638 35.3-58.7,
  785 41.7-54.9 cm-1; laser power at sample 85 / 40 / 50 mW (QC), 96.6 / 33.8 / 86.6 mW (install, no objective);
  axial FWHM 1.35 / 2.11 / 2.17 µm (hole 100); spectral resolution per grating x slit x hole at Ne lines -> qc.py
  (parse, import_dir -> store qc.json + perf cutoff '<laser>@any' (cutoff_for falls back), summary in references),
  `ls6t profile qc`. No HORIBA file or value is copied into the repository. LabSpec 6.7.1 help: AutoCalibration and
  AutoCalibrationEx are script calls (Mode 0 full, 2 offset only, 1 status; result 0 ok, 1-3 failed; 'only with
  HORIBA's script'); the 6.7.1 AutoCalibration is AutoCalibration.vbc + OLAutoCalibration.dll (why autocal_info
  found no .vbs) -> bridge verb autocal (VAutoCal, polls status, beats, stop), sim v_autocal, api.autocal_call /
  autocal_question used by autocal_run and run_calibration (method 'call'). To try once with the maintainer (T4.9).
  155 tests.
- 2026-11-23, 2026.11.23.8: Nicola's suggestion: record the ICS state with each spectrum. Ctx.last_ics set
  from the Acq mode's ICS digit (ics_off reason, asked on / off, LabSpec's setting); per-point 'ics', _point_summary,
  the .shift.txt header, the study's 'ics' (a list and a warning when they differ). 156 tests.
- 2026-11-23, 2026.11.23.9: From the instrument: ferralis's LabSpec ran jalavall's ls6t_bridge.vbs (the script list is machine-wide), so ferralis's heartbeat said 'stopped'. compose_vbs's header now resolves LS6T_BRIDGE_DIR / LS6T_HOME_DIR at run time from %USERPROFILE% (WScript.Shell), falling back to the installing account's folders with a note; install_bridge writes installed_version.txt, which the bridge compares at start-up (status-bar message); the heartbeat and ping carry account, account_version and note. bridge.other_accounts scans Users\*\ls6t\bridge for fresh heartbeats and diagnose names one when ours is silent; api.bridge_mismatch in instrument status. qc.find_dir looks on every fixed drive (known places, then a depth-3 search for 1-QC\QC Auto) and picks the folder with the newest report: the instrument PC keeps it on D: with a copy under 'LabSpec Software & Drivers'. 157 tests.
- 2026-11-23, 2026.11.23.10: Nicola at the instrument on .23.8: 2400 @ 637.75 ICS off and recorded, no 2070 dialog; spectrometer centred (659.628 for 659.625); Si 523.80 (2400) vs 520.98 (1200); acquisitions 42.4 s and 64.1 s for 2 x 2 s, 23.1 s for 1 x 1 s with nothing to move, against 10-15 s on .22.15 (before the bridge moved the spectrometer itself); the grating move reported 0.31 s. Added: per-phase t_*_s in the bridge's acquire (Secs), Ctx.calls (every bridge command with its time and phases), runner.timing() in each study's result with operator_s (confirm, notice) apart; the Spectro move skipped when already within spectro_tol_nm. Note: the laser's Yes was never inside seconds.acquisition (it is asked in _setup), so the 23 s is inside the acquire command: the phases will say where. Calibration per pair: perf kind 'zero', runner.zero_state / combo, checks.zero and warnings on spectra, applies_to, the pair in the guided notice and the AutoCalibration question. 158 tests. Also: LabSpec's Title / Project / Site set per spectrum (study name, 'ls6t <request>', sample.site or empty; new SAMPLE_KEYS 'site'); the simulator writes them as info.* header lines. 159 tests. Then from Nicola's next notes: profile qc chose D: by itself; profile.set_key matches keys with dots (longest existing run, or brackets); calibration 'outcome' in words and 'corrected' (adjust ls6t: 523.79 -> stored 637.75@2400 -> next spectrum 520.59). The brightness check caught a black frame after LabSpec's restart reset the illumination to dark field. 'seconds.acquisition' never held the laser's Yes (asked in set-up): the 25-37 s are inside the acquire command; timing.acquire_phases_s will say where.
- 2026-11-23, 2026.11.23.11: Q15 answered: LaserShutter confirmed spectrally (RTD on Si, Laser On / Off; the beam is invisible on the top camera). profile.HOW (the method) in the discovery and import notes and the laser_on / snapshot refusals; TESTING T1.2 and INSTALL describe it for other instruments.
- 2026-11-23, 2026.11.23.12: Nicola: minutes after .23.11, ls6t's own use of LaserShutter did not block the beam (a laser_off 'shutter' background kept the Si line), while LabSpec's status-bar toggle does. .23.11's closing advice (profile set motors.laser_shutter) withdrawn for the XploRA. T1.2 gets a second step (background with laser_off 'shutter' must show no Si line, else null); profile.HOW names both steps in every message that points to it; Q15 reopened. Still to do: the bridge's shutter fix (why MoveMotor LaserShutter, 0 with ShutterMode manual left the beam on), then T1.2 step 2 again.
- 2026-11-23, 2026.11.23.13: Nicola's details on the shutter failure (run_20261123_154736_fbb6e8: 2870 vs 3319 counts, 86 %; LabSpec's laser On by hand; status shutter_mode 0, laser_open 0; OLConfig LaserShutter [Off, On]) and the AutoExposure finding (2 s asked, 15 s used). Bridge: ShutterTo (by label with LS_MOTOR_STRING, or by value; waits; reads label / value / ShutterMode back), ShutterOpened (polled during a laser_off acquisition, cancels), RestoreShutterMode, VShutter (read / set / mode) for the diagnostic; laser_on, LaserClose and the video snapshot use them; VAcquire switches AutoExposure off for a fixed exposure (PutValue 0, 'AutoExposure', 0: untested name) and back after; status reports auto_exposure and laser_shutter_label. Python: profile 'shutter' (off / on / by), runner.shutter_args in motor_args, Ctx._exposure_used (spectra.header / acq_exposure: LabSpec's Exposition replaces the asked exposure; a mismatch stops the study), background not stored when it has narrow lines with the shutter trusted, api.shutter_test (five moves, operator answers, verdict, mode and position put back). Simulator: Shutter labels Off / On, sim_shutter_reopens / _ignored / _value_swap, sim_autoexposure / sim_ae_sticky. Unknown until the instrument: whether 'AutoExposure' is the GetValue/PutValue name, and which shutter move blocks.
- 2026-11-23, 2026.11.23.14: Nicola's review of .23.13: (1) the bridge may not read the shutter during an acquisition at all (Acq holds the script); (2) the narrow-line net only works on samples like Si. Bridge: gShutterReads / gShutterT0 (set right before Acq) / gShutterFirstS, returned as shutter_reads and shutter_first_read_s. runner.shutter_report: acq_call_held_s, after_acq_until_data_s, watched_fraction, during_checked (reads and >= 0.5 s of acquisition after Acq returned), a warning when false, and a note on what protects. Simulator sim_acq_holds. To confirm at the instrument with T1.2 step 2.
- 2026-11-23, 2026.11.23.15: Nicola's suggestion: require a passed spectral test on Si before relying on the shutter. New shutter.py (REV, records per laser in shutter_verified.json, gate(prof, laser), default_laser_off); api.shutter_verify (reference and shutter-closed acquisitions at 520 cm-1, line_signal per second from LabSpec's exposure, pass at <= 2 % and below detect_snr, reference SNR >= 20; record only on a pass); the gate in format (background, cutoff, video.with_laser, spot shutter), plan.cutoff_params and the background's default, runner (_shutter_gate before laser_off acquisitions), api.laser and api.snapshot. Kept outside the profile (written by ls6t, not by a person, so the reviewed profile stays untouched). Tests: the helper verifies in the simulator; a test for the gate (missing, per laser, another way of moving, too old, another REV, refused at start and at run time) and for a failed verification (sim_shutter_ignored). 162 tests.
  Then Nicola's results on .23.14 folded into .23.15: AutoExposure is not the GetValue name (AeName tries six); the exposure stop held; AE off: 10.5 s total, flush 1.62 s (now stops on three equal answers; staleId never taken, fails after ex*acc+60 s); Acq holds the bridge for the whole exposure even for one window (during_checked always false here: a note when verified); shutter-test redone at 10 % with the display zoomed: blocks by value and label, Off holds in automatic mode (the .23.9 failure: LabSpec reopening in automatic mode); step 2 backgrounds (8.5 vs 257 counts) rejected for cosmic rays: 5-point median before the narrow-line check, noise from the raw spectrum, and with a verified shutter a warning instead of 'not stored'. shutter_verify: 2 x 2 s with double spike removal, pass on 'not detectable' with sensitivity (detect_snr x noise / reference) <= 5 %, else 'not conclusive'.
- 2026-11-23, 2026.11.23.16: Nicola on .23.15: shutter-verify failed a 99.96 % block (ref 1185.4 cps, closed 0.494 cps, SNR 14.8): api.leak_verdict (leak_frac vs shutter.max_leak_frac 0.001; not_conclusive only when sensitivity > limit and no leak above it is seen), _pct for messages, the record keeps leak_frac / sensitivity / both signals; default 2 x 2 s in the CLI too. _operator_yes (timed confirm; < 0.4 s on the instrument not taken) for laser_on and shutter_verify. run_filters: measure() per step with LabSpec's exposure, saturation of every step, read-back, move timing, and the reference again at the end (drift > 10 %: unreliable). AutoExposure name still unknown. The sim tests set shutter.max_leak_frac 0.05 (the simulator's line is too weak for 0.1 %).
- 2026-11-23, 2026.11.23.17: Nicola found AE in LabSpec 6.8.1's help: Template.html / constants.vbm (ADD_TO_TEMPLATE_LIST 2, TEMPLATE_SET_VALUE 3, TEMPLATE_GET_VALUE 4, GET_TEMPLATE_STATUS 6, APPLY_TEMPLATE_NO_SPECTRO 9; values "AutoExposure", "AutoExposureMin/Max", "Expo", "Accum", "Spike"...). Checked: the bridge already passes the asked exposure to Acq, so LabSpec's AE overrides it. Bridge: TplGet (records the third argument and the return), TplSet, AeIsOn; VAcquire writes only with ae_off=1 (profile autoexposure.switch_off, default false, passed by Ctx.call and shutter_verify) and restores the value read; status reports it. AeName (GetValue guesses) removed. Open: Template.html lines 31-53 (GET/SET semantics, live or stored template).
- 2026-11-23, 2026.11.23.18: T3.19 passed (GET in the third argument, 0/1; SET live; restored). switch_off default true; runner.ae_record per spectrum and the study's auto_exposure summary; the simulator reports auto_exposure_after. Flush 0.05 s confirmed. To watch: t_spectro_call_s 6.0 s just after a LabSpec restart. Then the full Template.html (Nicola): Template(long Mode, LPCTSTR Param, VARIANT* Value), modes 0-9 (LOAD, SAVE, ADD_TO_LIST, SET 3, GET 4, APPLY 5, STATUS 6, SAVE_CURRENT_CONFIG 7, TEST 8, APPLY_NO_SPECTRO 9), no return codes for SET/GET. TplTest at start (TEST_TEMPLATE with no name, plus an AE read), in the ping as template_test; not yet a gate (its answer is not documented).
- 2026-11-23 (no release): T3.19 passed on .23.18 with the default switch_off (before 1, off during, after 1; 8.85 s total for 1 x 2 s). template_test 'return=0, value=, autoexposure=1': not discriminating without a template name, kept as a record; the AE read (0/1 following the status bar) is the evidence; an empty read leaves AE alone (AeIsOn false) and the exposure check remains. The deploy ended the bridge cleanly.
- 2026-11-23, 2026.11.23.19: Nicola: shutter-verify at its threshold (0.042-0.172 % over seven runs; lower after a LabSpec restart; 1.3-3.5 counts per acquisition). shutter_verify(exposure_s, repeats, burst_s): reference once, closed spectra judged on the worst; burst_check (counts ratio vs exposure ratio); SH.attempt / attempts (every attempt kept; a pass records failed_attempts_before since the previous pass); MAX_LEAK_FRAC 0.005. Background narrow lines: counts floor (checks.background_line_min_counts 20) and recurrence across backgrounds (background_lines.json), all listed in narrow_lines. laser_on: seconds = confirm_s + laser_call_s, open_for_s. T3.20 b passed.
- 2026-11-23, 2026.11.23.20: Nicola (serious): spectra after the first in a request shift -8 cm-1 and broaden 2.5 x at the same spot (area kept, LabSpec's axis and acq.* identical, shutter excluded): motion during the exposure. Suspects: the next Acq starting ~1.5 s sooner since the .23.15 flush change; the Template AE calls between spectra (.23.17+). Added: bridge optics reads around Acq (spectro / grating before, after the call, after the data; GetMotorStatus all), since_last_acq_s and settle_s (gLastAcqEnd); runner.optics_record, same_spot_check (unreliable for repeated spectra, warning for series), timing.acquire_settle_s (default 0) passed by Ctx.call; simulator sim_shift_after_first. TESTING T3.21 (a)-(d) to isolate the cause.
- 2026-11-23, 2026.11.23.21: the cause: a 637.75 nm laser mode hop after the first spectrum of a request (laser line and Si move together; a 30 s warm-up first was clean, the 2 s points after it hopped; undone at the next request). Added laser_line_fit / laser_line_reference (perf 'zero' keeps laser_line_cm1 from passing calibrations), acquire.laser_reference report | correct, checks.laser_line with jumps (unreliable unless corrected), warmup_s (acquire or lasers.<k>), lasers.<k>.mode_hops note on calibrations, profile.load fills warmup_s / mode_hops per laser, timing.status_before_each (diagnostic). TESTING T3.21 lists the bridge's start-of-request vs per-spectrum calls; to bisect: status_before_each, then the reads inside status, the dialog, the set-up moves.
- 2026-11-23, 2026.11.23.22: 532 nm stable on the same test (±0.1 cm-1): the 637.75 nm source is at fault (HORIBA). lasers.<k>.unstable_multi (format: refuse or warn multi-spectrum kinds unless laser_reference correct), 'correct' without a laser line -> unreliable, peaks' from_laser_line_cm1. Requests 1 and 3 were mostly in .20/.21 (same_spot_check, laser line jumps, correct mode).
- 2026-11-23, 2026.11.23.23: profile set on the XploRA: the 637.75 nm laser's key is not '637.75' (likely '638'), so the .23.10 longest-key match still failed with "no '637'". set_key: under 'lasers' a numeric run matches a laser's nm (within 0.01); quoted keys; the error lists the keys at that level.
- 2026-11-23, 2026.11.23.24: workaround found by Nicola: acquire_settle_s 5 holds the 637.75 nm mode (run_20261123_211608_3c417c); status_before_each did not help. plan.settle_s (max of timing and lasers.<k>.settle_s, by the laser), Ctx.call passes it with self.optics[0], estimate adds it per spectrum after the first; format accepts unstable_multi with a pause, not with the native map engine. Next: the minimum (2 s, 1 s).
  Then: 3.5 s still hopped (-6 cm-1); the jump shrinks with the pause, none at 4.7 s. HORIBA's mapping scripts (LabSpec_6_8_1/SERVICE StandardMapping/SWIFTMapping XploRA.vbs, read only) hold the shutter open in manual mode for the whole map (2018 change from SHUTTER_AUTO). Bridge VShutterHold / HoldRelease (index restore as HORIBA, ShutterMode back; LaserGuard releases on other verbs; halt / quit / max_s / safe), status shutter_held; plan.hold_shutter, acquire.shutter, lasers.<k>.hold_shutter, run_study holds around the points (settle 0 while held), result shutter_hold; format: hold needs the verified shutter, unstable_multi accepts it. Simulator: v_shutter_hold; held prevents sim_shift_after_first. 165 tests.
- 2026-11-23, 2026.11.23.25: Nicola: filters with 637.75 nm not held (no shutter_hold; 0.54 x), not refused. The hold moves from run_study to run_any (all kinds but move / background / cutoff): Ctx.call holds before an acquire / map_native / guide (not laser_off), notes a release when another verb is sent (HOLD_VERBS shared with the bridge's Const), run_any releases at the end and adds shutter_hold (times_held, log) to the result file. Bridge HOLD_VERBS adds map_native, guide, remove, save, labels, last_error. format: hold accepted for any kind, the native engine too. Also from the first map and series: hold_shutter false for a series whose gap (interval - exposure x accumulations) >= the laser's settle_s; series_waits in the estimate (the total already had them); native .l6s for series; VView retries LabSpec.Map create without fv/tv and reports r and LabSpec's error. The 38.8 s spectrometer move after 2400 -> 1200 is LabSpec's own; open.
- 2026-11-23, 2026.11.23.26: Nicola: burst confirmed on 785.04 nm (counts_ratio 0.77 for 4 x exposure) and on all lasers; 785 failed at 2 s, passed at 8 s. VERIFY_EXPOSURE_S 8 (api, instrument action, CLI). A per-acquisition criterion was the alternative; the longer exposure judges the three lasers the same way with the existing rule. His profile still has max_leak_frac 0.001 (saved before the default changed): set by hand.
- 2026-11-23, 2026.11.23.27: filters held and right; but 1200 -> 2400 stopped at 1800 (settle_stable after 1.5 s, then the hold's commands). VMove until_target (grating, via move_extra) after SettleReadback's stable exit, then WaitMotor all; VShutterHold waits all motors idle; _setup retries a grating mismatch once and raises Stop. Simulator sim_grating_stuck_once. Then reproduced by Nicola: after a scripted visit to 785 nm, LabSpec refuses 2400 (takes 1800) even back on 637.75 nm; a hand selection clears it. runner.reselect_laser (status, move the laser to its own value) before the grating retry, in _setup and api.set_instrument (retried_after_laser_reselect); simulator sim_laser_locks_grating. If the reselect does not clear it on the instrument: Template SET 'Laser' next. Nicola: a bridge restart alone clears it (LabSpec untouched): the state is in the bridge's script session. Messages (runner Stop, api._grating_short) say end-bridge + start again. Open: what in a script's laser change ties LabSpec's grating check to the old laser; the reselect retry stays as a cheap first try.
- 2026-11-23, 2026.11.23.28: Nicola: the reselect must really send a command; 120 s too long for a refusal. reselect_laser always sends the move (the bridge verb has no already_set skip) and via=True goes through the shortest other laser; retry_grating tries same, then via (runner _setup and api set); VMove until_target_stable_s 10 ends the wait on a stable wrong reading. Simulator sim_laser_locks_grating 'never'.
- 2026-11-23 (no release): the reselect retry works on the instrument (.27: same laser again, 2400 reached, no restart; 120 s wait before it, shortened in .28). LabSpec's interface shows the old grating until its next dialog; the spectrum confirms 2400. Spectrometer moves after a laser / grating change take 37-68 s (LabSpec's own).
- 2026-11-23, 2026.11.23.29: T3 passed on .28. filters: plan.filters_steps lists capped filters and those beyond max_exposure_s with a 'skip' reason (format and run_filters warn; result 'skipped'); the proposal is the mean over the last 5 runs with the same reference, offered from performance.filters_min_runs (3). until_target_s only appears when the grating first read a wrong position.
- 2026-11-24, 2026.11.24.1 (prepared as .23.30 before midnight): calibrations batch: 637.75@600 not trusted (FWHM 13 at the edge of 30 cm-1): runner.calibration_window (grating-scaled, calibration.window_cm1), profile calibration.video_before. Live map view: Map returned E_FAIL (0x80004005), LabSpec error empty, retry skipped since fv = tv = 0; message gives the range. Not yet checked against 6.8.1's Map.html or HORIBA's scripts (the LabSpec folder is no longer connected).
  Then the LabSpec folder again (Nicola): HELP\VBS\Map.html (From / To: the frequency range whose average intensity colours the map; the example uses 112.3-114.5) and MapEx.html (known size: AxisX, AxisY, index = Y * SizeX + X), HORIBA's macros\horiba.vbm map loop uses MapEx(CREATE_MAP, 0, ID, index, axes, labels, units) then ADD_TO_MAP. The bridge's view gains kind 'mapex' (NumList for the axes); runner sends it for X/Y maps without Z; Map() gets the spectrum's x range when the study gives none. 637.75@1200 read -1.06 (corrected) after about +0.3 earlier the same day: this laser's zero drifts by about 1 cm-1 within a day.
- 2026-11-24, 2026.11.24.2: the MapEx live view froze / crashed LabSpec twice with the shutter held. display.live_view false (profile), view removed from HOLD_VERBS (bridge Const and runner), heartbeat shutter_held (Beat; forced beats on hold and release), runner.LASER_MAY_BE_ON prefixed by bridge.diagnose (stale or stuck heartbeat with shutter_held=1) and Ctx._held_warning on BridgeGone / BridgeTimeout while holding. The MapEx call is not understood yet; the live view stays off until it is.
  Also .24.2: Ctx._log writes each call to runner.log (append, flush); series t_start per spectrum and spacing (start to start, warning with the reason); plan.hold_shutter's series gap uses acquire_seconds; lasers.<k>.laser_reference (default for laser_reference) and calibration_max_age_h (the xcal age warning). The 1.66 s for 2 s is unexplained by the code: asked for the spacing record.
- 2026-11-24, 2026.11.24.3: MapEx crash cause (Nicola, mapex_test / mapex_live): axes passed as a Variant holding an array (NumList's result, the gViews entry) crash LabSpec; declared arrays pass. VView copies into Dim'd axA / ayA before CREATE and ADD; units 'um'; maps with Z: no live view (Map() always E_FAIL). Checked the bridge for other LabSpec calls with arrays from Variants: none. The test scripts stay outside the repo (outputs). Also: auto ShutterMode opens the shutter for each Acq even with the status-bar laser 'off'.
- 2026-11-24, 2026.11.24.4: live view on .24.3 works but released the hold at every view (9 holds, hops) and AUTO_SHOW hid the map. runner._live_view (extracted); with ctx.holding the views are queued and made after the loop, the hold released first (hold_log 'end of the spectra, before the live view'); a live grid map drops AUTO_SHOW from the Acq mode. Series spacing median / after-first / first, t_start in _point_summary, format warning for an interval shorter than acquire_seconds. Kept: view not in HOLD_VERBS (Nicola's earlier safety request; deferring satisfies both). Explained: spikes (1 accumulation), blocking_s (an allowance), UTF-16 heartbeat (non-ASCII account folders).
- 2026-11-24, 2026.11.24.5: review: view back in HOLD_VERBS (bridge Const and runner; a test checks they match), display.live_view_while_held (default true; false defers as in .24.4); spectra.spikes requires <= 2 pixels above half height over a 15-point median level, spike_cm1 per spectrum; heartbeat blocking_allowance_s (bridge.py reads both).
- 2026-11-24, 2026.11.24.6: review: the width test may not hold at 600 gr/mm. runner.recurring_spikes: positions repeated (2 pixels, >= 2 spectra and 30 %) are lines, removed from spikes / spike_cm1, listed in checks.spikes.recurring_cm1. A single spectrum keeps the width test.
- 2026-11-24, 2026.11.24.7: review of .24.6: recurrence against chance, not a fraction: runner._poisson_tail, LINE_P 1e-3, lam = (n - 1) x 5 / P (P from the spectra's pixel count), at least 3 spectra. Answered: flags never alter the data (counts and warnings only).
  Also .24.7: series wait from the previous start (t_last_start), the view's id / index / axes and mapex_return in runner.log. Open: the live map on .24.5 not shown, and curves instead of a map at the end (axes look right in the code; asked for runner.log, map size, screenshot, live_view and engine).
- 2026-11-24, 2026.11.24.8: Nicola's afternoon (.24.7): live map and held series pass. xcal per window centre (by_centre, get with centre: exact within 1 nm, interpolated between two, nearest with centre_far; store with the calibration's plan.window_centre), warnings in analyse; format's series warning with the hold (timing.held_overhead_s 1.0); spectra.spikes floor 3 sqrt(level) and 5 counts, spike_info; runner._spikes_where_meaningful (edges, cut-off, laser wing); optics_record ignores the busy-before after the bridge's own spectrometer move; display.live_view default true. Not done: the laser line (zero) + Si (scale) per spectrum: laser_reference 'correct' covers the zero.
- 2026-11-24, 2026.11.24.9: review: laser_reference 'correct' + stored correction could count the zero twice. xcal.store laser_line_cm1 (interpolated with the correction); analyse: with corr and 'correct', the laser-line reference is corr's laser line (else the study's first, with a warning), then corr; checks.zero_correction (method, stored, reference). spectra.spikes e_per_count (profile detector.e_per_count).
- 2026-11-24, 2026.11.24.10: Nicola on .24.9: hops between studies (gap < ~20 s). run_request sets ctx.keep_hold when the next study holds with the same laser (_continues_hold); run_any keeps it, the next renews max_s (bridge: an existing hold only renews its time); zero_uncertain (|jump| > 3 cm-1 or laser line > 1.2 x the calibration's / the narrowest width), xcal laser_line_fwhm_cm1; optics moved ignored for ranges (LabSpec places the spectrometer).
  Also .24.10 (safety): VMapNative sets gBlocking 'map_native' / gBlockingS around its Acq; Bridge.call keep_queued (default for safe, shutter_hold off, laser off): not withdrawn on timeout or gone; the bridge's idle lease (gHoldIdleS from idle_s, gLastCmdEnd after each command) releases a forgotten hold; runner passes idle_s (60 + a series' interval). Native maps keep the hold (HORIBA's scripts do the same).
  Also .24.10: VMapNative starts with Acq(SPECTRAL_IMAGE + AUTO_SHOW, -1, 0, 0, 0) as HORIBA's scripts (horiba_start 0 keeps the old call), GetMappingParams read-back per axis; focus_scan counts / counts_sigma (sqrt(h/e_per_count + pixel noise²), the pixel noise measured from the spectrum) / snr, FOCUS_MIN_COUNTS 300 (warning with the exposure), FOCUS_MIN_CONTRAST_SIGMA 4 (no focus, back to the start), z_left_at. Native map not verified on the instrument yet.
- 2026-11-24, 2026.11.24.11: Nicola on .24.10 notes. Interpolation passed on .24.9 (655.30 nm: +1.21 between +2.01 @650.63 and +0.46 @659.66, laser line −5.14, Si 521.13). Range centre: LabSpec's one-window range Acq puts the window's blue edge at From and crops to the range: 676.75 nm is the real centre (633.7 + ~44 nm), the data's apparent 650.6 is the crop. runner.window_centre_used(rng, per_point): single → plan.window_centre; range of one window → median optics_check.spectro_after_data; else None. Used by analyse (xcal.get, checks.window_centre, centre_far text, cover_short text) and _calibration_result (applies_to, xcal.store). Sim: _placed_centre (blue edge at From), spectro_after_data in acquire results. laser_reference: 'correct' = plateaus (pre-pass fits every laser line; a new plateau when |pos − median(current)| > LASER_JUMP_CM1; each spectrum shifted by its plateau median − reference); 'correct_each' = the old per-spectrum follow; zero_correction.follows/plateaus; sim_laser_line_drift. plan.hold_open_s / HOLD_OPEN_DEFAULT_S 5; profile lasers.<k>.hold_open_s None; Ctx._hold_on passes open_wait_s (timeout 30 + w), bridge VShutterHold waits with WaitStoppable after a new open (gBlocking shutter_hold), estimate adds it at k == 0 when held. Bridge: gHoldLabel, gHoldLastWhy; VShutterHold returns renewed 0/1 and previous_release; HoldCheck(res, tag) after the native map loop (also on cancel/timeout) → hold_after_map_held yes/no/not held, _shutter_mode, _shutter_label; runner: after_native_map in shutter_hold, warning and keep_hold False if lost. Renewal with renewed 0 → hold_log reopened + warning. idle_s +120 when keep_hold. Idle lease confirmed: gLastCmdEnd at HandleFile's end, checked only in the main loop, Elapsed midnight-safe. Tests: test_correct_follows_the_laser_lines_steps_not_its_slow_drift, test_hold_open_wait_native_map_hold_read_back_and_reopened_hold, test_range_uses_the_spectrometers_real_centre.
  Also .24.11 (native map works on .24.10; 600 series): runner.screen_points (laser line in most spectra: missing or height < WEAK_POINT_FRAC 0.2 × median; else signal max − median < 0.2 × median for spectrum/series, a warning only for a map's point 0; >= 3 spectra) → rec excluded, checks.excluded_points, warning, unreliable when > 10 %; _quantities gives None for excluded. The laser-line pre-pass now always runs (any mode): raw laser line + raw fits of process.peaks; a step > LASER_JUMP_CM1 from the plateau median starts a new plateau only if the peaks moved with it (|median peak move − step| <= max(0.5 |step|, 1)); else ll_bad (laser_line.suspect, checks.laser_line_bad_fits, kept on its plateau, never used for correct or as reference); hop_info → checks.laser_line.jumps {point, moved_cm1, confirmed_by_peaks}, points_after_a_jump; drift_since_calibration_cm1 (corr's or perf's laser line) with a warning over 1.5. Native: warmup_s before map_native; ctx.native_info (axis readbacks, acq_mode, seconds_map, axes) → result native_map; sim returns readbacks. Installer log: winsetup writes $INSTDIR\install.log (setup step only). Tests: test_weak_points_kept_out_bad_laser_line_fits_and_native_readbacks; hop test now jumps [1] + points_after_a_jump [1, 2].
- 2026-11-24, 2026.11.24.12: Nicola on .24.11: per-window calibration after clearing: centred 650.19 +0.01, 659.66 −0.02, LabSpec-placed range (676.75) −2.09; interpolation passes (+0.008); native map passes (9 good points, readbacks, after_native_map held). plan.acq_range: range within 0.9 x one window (learned width, else the profile's) and range_placement 'centre' (default) → single, From = To = centre, crop_cm1 [lo, hi]; analyse crops by the measured axis before analyze (same mask for every point), coverage checked for crops. 'correct' (steps): plateau_step per plateau = step from ll_ref if |step| > FOLLOW_MIN_CM1 3 (checks.laser_follow_min_cm1) and the raw Raman peaks moved with it (ref: (reference_cm1 − a)/b of the stored correction's lines when ll_ref is the calibration's, else the first plateau's raw peaks); else 0; zero_correction.plateaus {step_cm1, raman_moved_cm1, applied, not_applied_because}; ll_bad points take their plateau's step. laser_line_too_broad (> 2 x the reference lines' median FWHM): not stored at calibration (laser_line_rejected) nor used from a stored correction (study's first plateau instead). Bridge VMapNative: tMap before Acq, seconds_map = Elapsed(tMap). Sim: _ll_drift_n_fixed (constant line offset). run_background's narrow-line check limited to crop_cm1 (the bench's white_light flagged the edge filter at 69 cm-1 once the window was centred). LM Studio Bionic (Nicola): the installer wrote %USERPROFILE%\.lmstudio\mcp.json from .24.9 (LM Studio found from then); Bionic needed a manual entry: where Bionic keeps MCP servers on Windows is to be confirmed before ls6t connect writes it.
- 2026-11-24, 2026.11.24.13: LM Studio Bionic (Nicola: .lmstudio\mcp.json had the installer's ls6t entry, Bionic did not take it; his hand-added entry is in .lmstudio\apps\bionic\.internal\ng-mcp.json as {servers: [{id, name, enabled, connection: {type stdio, command, args, env}}]}, values with doubled backslashes; app-install-location.json → C:\Program Files\Bionic\Bionic.exe). clients: client 'bionic' (aliases lm-studio-bionic, lmstudio-bionic): bionic_dir/bionic_file/bionic_install/bionic_running (tasklist Bionic.exe), bionic_servers (→ the common {command, args, env}), bionic_merge (id/enabled/other connection keys kept, env kept with _undouble, new id uuid4), bionic_remove; _servers(client, path) used by existing_env, status and _entries; files('bionic') empty until Bionic's folder exists (connect reports why). winsetup --bionic (from CLIENTS); NSIS SecBionic, /BIONIC, ticked by the folder or Bionic.exe in Program Files / LocalAppData\Programs. INSTALL step 8 table and notes. Test in test_connect_detect_and_disconnect.
- 2026-11-24, 2026.11.24.14: Nicola: autofocus with the live camera on a feature (rejected without features), and a manual per-objective minimum-distance calibration on a flat sample against a crash. Choices (asked): measured gap; coarse then fine; top camera, white light. plan: AUTOFOCUS + 'camera', camera_params (step = coarse_focus_step_um, range 10 steps, fine = step/4, settle 0.5), camera_seconds; clearance_store / clearance_um, CLEARANCE_FRACTION 0.5 (limits.clearance_fraction), z_limits toward = min(WD fraction, 0.5 x clearance) with toward_from and clearance_um. runner.camera_focus: _cam_frame (video verb, formats '', laser_s 0), noise from two start frames, _sharpness (Tenengrad over mean²), _texture (std of a 1-8 px DoG over the same band of the noise difference), coarse from away to toward within z_limits, CAM_MIN_TEXTURE 3, CAM_MIN_CONTRAST 4, interior peak; fine ±4 quarter steps, 3-point parabola; render.focus_png reused. Wired into run_focus (kind focus with focus.autofocus camera) and both per-point AF branches. format: FOCUS_KEYS camera, _validate_camera (needs z_into_sample), kind focus takes autofocus scan|camera. api.clearance (instrument action, CLI `instrument clearance`, MCP doc): confirm focus, confirm closest by hand, gap = (z_close − z_focus) x into_sign, refused <= 0 or > 1.5 x WD, Z back to the focus, stored. Bridge VVideo skips empty formats. Sim: frames blur 0.4 µm per µm of defocus (_zf_true), sim_featureless, sim_operator_dz. Tests: clearance, camera focus (found within 0.6 µm of a 3 µm defocus; featureless refused; map every point).
- 2026-11-24, 2026.11.24.15: Nicola's evening (.13): centred ranges, broad-line rejection, hop-only correct, hold across studies pass. Fixes: run loop calls ctx._hold_on() before t_a (open wait outside point 0's clock); VAcquire HoldCheck 'hold_after_acq' when gHoldOn or held_expected (runner sends held_expected=1 while holding), _last_release when not held; runner rec shutter_hold_after, warnings (lost / let go → ctx.holding False), runner.log keys; api._instrument refuses non-BUSY_OK actions while a request holds the lock (status, last_error, laser_off, resume, end_bridge, autocal_info). screen_points: reference = median of the strong ones (>= half the max), 'went off' after a good spectrum. miss list excludes excluded points (empty-list unreliable). checks.zero.correction_used. xcal placement (centred | labspec) stored and filtered in get; clear_centre; references clear LASER@GRATING@CENTRE. UNCONFIRMED_MIN_CM1 5. Post-hop multimode: after the loop, spectra on plateaus after the first (and bad fits assigned there) with a process peak > 1.5 x the first plateau's FWHM, < 0.5 x its height, or not fitted → excluded + unreliable. Coverage text for crop ranges. Grating retry: hold released first, readback first move marked retried, checks.setup pass ignores it. VAcquire: Spectro moved once more when > 1 nm off (spectro_first_nm, spectro_retried). Beam loss at 18:51: not explained by the bridge's idle release (between commands only, 180 s while kept) or queued safety commands (none in runner.log); another process's instrument command (camera, snapshot) would have released the hold — now refused; the read-back after every spectrum will tell a closed shutter from a laser that dropped. Sim: sim_multimode_after_hop, hold read-back, held_expected.
- 2026-11-24, 2026.11.24.16: Nicola on .15: read-back on every held point, correction_used, bad fits, 2400 retry (setup pass), multimode exclusion pass; 785 spectrometer arrived first time (retry not exercised); hold not on at the 2400 retry (release not exercised). VAcquire res t_to_acq_s (tAll → just before Acq); runner t_start = sent + t_to_acq_s, t_last_start likewise. Drift warning 'followed' only if a plateau applied (or correct_each). format: warning for a mode_hops/unstable_multi laser, not held, settle_s 0, no warm-up (point 0 unprotected). Test test_first_exposure_unprotected_warned_and_spectrum_clock_from_acq.
  Also .16 (Nicola's second .15 report): 600 gr/mm repeatability (+9.22..+10.34 in 3 min, same-window scatter >= between windows). plan.positions: calibration.repeats N points; runner sends spectro_jog_nm 3 for k > 0 (bridge: Spectro to f+jog, wait, then the normal move back); _repeat_offsets, mean a (zero moved to the repeats' mean), repeats {n, offsets, mean, sd, sem}, pass on |mean|, warning when sd > tol/2; _cm1_per_px from the spectrum's axis; COARSE_CM1_PER_PX 2: store auto → pooled (xcal.store pooled: one record, by_centre dropped, replaced_per_centre; get: no centre_unknown for pooled). format: calibration.repeats 1-20, calibration.store. api.start/run order (permutation check), MCP run_studies order; ls6t never reorders (the app sorted the keys).
  Also .16 (extended range and 5 x 5 maps on .15): spectra.noise_smooth (31-point median residual MAD, lag-1 r), analyze sigma = max(pixel, smooth) for peaks/SNR, pixel for spikes; auto peaks rejected < 2.5 dx or r2 < 0.8; runner.header_ics_denoise (rec ics from LabSpec's header, denoise); checks.noise when r > 0.5; stitch_seams (point-spacing jumps) → checks.stitch; xcal by_range (range_key) stored for extended-range calibrations, get(range_cm1) → range_match; warning otherwise; SPECTRO_NM_PLAUSIBLE 100-2000 nm in optics_record and window_centre_used; plan.acq_next_s (no window move after the first, held: exposures + held_overhead_s/2), view_s 0.2 per point with live view. Native map order: parse_native_map places each value by LabSpec's grid indices (its data cube is stored by position, not by acquisition path), so a snake path would not mirror rows; a patterned sample confirms it (TESTING Q29).
- 2026-11-24, 2026.11.24.17: Nicola: the pixel-size rule would pool real window dependences (532 @ 1200: +4.05 / +2.18 / +1.80; 637.75 @ 1200 at 2.01 cm-1/px). xcal.store(pool=auto|per_centre|pooled, cm1_per_px): per-centre entry always kept; pool_decision: >= 2 centres, spread <= POOL_SPREAD_SD 2 x rms repeat SD → pooled; no repeats: px >= 2 and spread <= 1 cm-1; the top record is then the centres' mean (pooled, pooled_from); get returns it for any centre (not for labspec placement). Order: confirmed upstream reordering, `order` stays. Repeats return to the same nominal spectrometer position (659.6508 nm both times, still 0.6 cm-1 apart): the scatter measured is the fit's real repeatability.
- 2026-11-24, 2026.11.24.18: Nicola on .17: pool test by the means' standard errors (POOL_SE 2 x sqrt(SE1²+SE2²), _se from sem or sd/sqrt(n), every pair), and only entries of the same request or within POOL_WINDOW_H 1 h (fresh_entries); older centres listed, not compared; a centre recalibrated > 1 h later (another request) gives drift_at_centre; runner warns to recalibrate the older centres. Shifting older entries by the drift was not done: the drift need not be the same at every window; recalibrating in one request is the reliable route.
  Also .18 (Nicola): with repeats at only some compared centres the SE test is not used (it already required every centre); the no-repeat default decides. Test added; docstring says so.
- 2026-11-24, 2026.11.24.19: Nicola on .18. stitch_seams: transient (dx off a 41-point median trend by > 15 %) plus lasting change; repeat_drift (least-squares slope > 2 SE; drop leading until flat, >= 3 kept) → rep_info drifting/settled/dropped_leading, warning; xcal._se None for an unsettled drift; plan.hold_open_s(kind): calibration_open_s (default CAL_OPEN_DEFAULT_S 40 for mode_hops / unstable_multi), ctx.study_kind; STALE_DRIFT_CM1 0.5 for the drift-since-calibration warning; age > POOL_WINDOW_H warned for drifting lasers; correction_used keys + age_h; estimate first_acquisition / per_acquisition (next) / acquisitions (sum), series waits from acq_next; xcal.store for an extended range: 'kept for the range', top record untouched. Not done: interleaving centres (A B A B) inside one calibration study — the per-point loop has one window per study; the same is reached with the open wait plus the drift check, or by listing the calibrations interleaved in one request (order).
- 2026-11-24, 2026.11.24.20: Nicola on .19: repeat_drift keeps max(3, ceil(N/2)); hold_open_s(kind): drifting lasers (mode_hops / unstable_multi) OPEN_SETTLE_DEFAULT_S 40 s for any study on a fresh opening (hold_open_s overrides, calibration_open_s for calibrations, a calibration never below 40 s otherwise), other lasers hold_open_s or settle_s; HOLD_OPEN_DEFAULT_S removed. The open wait was already only on a fresh opening (renewals send none; test asserts one wait for two studies).
- 2026-11-25, 2026.11.25.1: Nicola on .20 (spectrometer repositioning at 600: SD 0.9-1.2 moved vs 0.12 still). hop_info after_spectrometer_move (optics_check spectro_moved_by_ls6t) + what, message wording. repeat_stats replaces repeat_drift: median centre, outliers (> 3 robust SD and 0.3), step (STEP_T 5 pooled SE, runs >= 2), trend (TREND_T 5 slope SE, n >= 5); false 'not settled' on pure noise ~3 % (n 5), ~1.5 % (n 8); rep_info median/mean/sd/sem/spectrometer_moved/outliers/settled/step/trend. calibration.repeats_move (no jog when false); store spectro_repeatability.json {moved_sd_cm1, still_sd_cm1} when settled and n >= 3; correction_used expected_uncertainty_cm1 = moved SD. Profile spectrometer.approach_nm → acquire spectro_approach_nm (bridge: f+approach, wait, then f; jog uses it). Calibration repeats after a grating change: one discarded spectrum (_warmup). Wording in the age warning and the pool 'real window dependence'.
- 2026-11-25, 2026.11.25.2: Nicola: no widening of the calibration's pass tolerance; analyse warns when expected_uncertainty_cm1 (moved SD) > checks.si_tol_cm1.
- 2026-11-25, 2026.11.25.3: Nicola's repositioning test (600: still 0.26, moved 0.40/0.59, moved with approach -2 0.30; F 3.8, p ~0.03): backlash. Profile default spectrometer.approach_nm -2, spectrometer.repeatability (person) over the measured store (which records approach_nm; a moved SD with another approach ignored: runner.spectro_repeatability). xcal.fresh_entries: repeats without median_cm1 (pre-2026.11.25.1) or settled False → not compared (older, why). Repeats warning: median. Bridge: t_motors_before_s before the jog; t_spectro_jog_s, t_spectro_approach_s (the approach skipped when the jog already went there); runner PHASES labels.
- 2026-11-25, 2026.11.25.4: Nicola: LabSpec-placed windows (extended ranges, native maps) get no approach: spectro_repeatability(placement) uses '<laser>@<grating>@labspec' (no approach filter); calibration repeats over a LabSpec-placed range record there; analyse picks labspec placement for non-single ranges and native maps; expected_uncertainty_from.
- 2026-11-25, 2026.11.25.5: Nicola: VMapNative approach move before the centre (spectro_approach_nm), spectro_before_map / spectro_map_started (first GetAcqID 0) / spectro_after_map; runner native_info.spectrometer.moved_by_labspec (> 0.01 nm, plausible readings), recs native_spectro_moved_by_labspec; analyse rep_place centred for an unmoved native map, labspec otherwise. The first spectrum's laser line is already compared with the calibration's (drift_since_calibration_cm1). Sim: sim_native_moves_spectro.
- 2026-11-25, 2026.11.25.6: Nicola's repeatability with approach -2: 600 still 0.26 / moved 0.19-0.30, 1200 0.15 / 0.25, 1800 0.16 / 0.25 (profile set). acq carries spectro_approach_nm for the whole study (warm-up included); first acquisition after a grating change (warm-up or point 0) spectro_force=1 (bridge skips the 'already there' check). VMapNative: WaitMotor Spectro (gNoId when MoveMotor returned 0) before spectro_before_map. repeat_stats outliers: leave-one-out, OUTLIER_K 6 x sd_others x sqrt(1+1/m), floor 0.3, max(1, n//5). xcal: drift_at_centre skips pre-2026.11.25.1 / unsettled old entries. Note: [100, 3200] fits one window at 600 (centred, not a LabSpec-placed test).
- 2026-11-25, 2026.11.25.7: Nicola's SKILL.md for Bionic, added to the package (labspec_translator/skills/ls6t/SKILL.md; pyproject package-data). Bionic's user skills: %USERPROFILE%\.lmstudio\skills\<name>\SKILL.md (found after Settings > Skills; built-ins in apps\bionic\.internal\skills with a hash manifest, not touched; hub\skills empty). clients.install_skill / remove_skill on connect / disconnect bionic (backup of a different SKILL.md; only ls6t's SKILL.md removed; folder removed only when empty). Note: adding a SKILL.md in Bionic copies its whole source folder (Nicola's copy brought other files along).
- 2026-11-25, 2026.11.25.8: SKILL.md replaced with Nicola's new version (skills/ls6t/SKILL.md).
- 2026-11-25, 2026.11.25.9: Nicola: small local models (4B) cannot build studies. api: log_rejected / rejected_log (jobs rejected_calls), coerce_studies (JSON text, unnamed single study), snap_laser (nearest within 5 nm), snap_objective (same magnification), _summary, run_and_wait (status loop, 280 s), measure_spectrum, calibrate_si; start logs validation refusals. mcp_server: run_studies / validate_studies take dict | str; measure_spectrum / calibrate_si async with _keepalive; MEASURE_ONLY (LS6T_TOOLS=measure): short instructions, _full_tool hides instrument (full), snapshot, get_inputs, run_benchmarks, alignment_status, spot_status; a status-only instrument. clients.connect tools → env LS6T_TOOLS; cli connect --tools. Arguments rejected by the MCP framework before ls6t (schema) cannot be logged; dict | str for studies avoids the commonest.
- 2026-11-25, 2026.11.25.10: Nicola: every request failed on its first acquisition (AttributeError 'str'.get), traced by him to `profile set spectrometer.repeatability '{...}'` from PowerShell (5.1 strips inner double quotes from native-exe arguments; profile_set fell back to storing text). api.profile_set: _unquoted_json repair, refusal with quoting hint, @file.json, type check against the old value. profile._merge(problems): text where a group belongs → JSON or default + prof["problems"]; repeatability entries must be dicts; spectro_repeatability ignores non-dicts. runner: generic exceptions stopped_by "ls6t", why says not the bridge; Stop handler guarded (trace); finally waits up to RELEASE_WAIT_S 90 s for a held bridge, 'safe' with 120 s, else Bridge.queue('safe'); state shutter_hold_released (+ profile_problems). Ctx._release_after_silence: on BridgeGone/Timeout while held, Bridge.wait_alive, shutter_hold off → "closed", else LASER_MAY_BE_ON + queued safe. VBS: jog / approach / native approach MoveMotor announced with gBlocking. sim: heartbeat shutter_held, sim_hb_pause hook. MCP measure-only instrument allows laser_off; _summary next → laser_off when not released. SKILL.md replaced (SKILL-c7bedf7e). Tests: held-up bridge (released / queued), profile set + load survival, runner fault releases. The AttributeError itself is the profile value (not reproduced as a code path otherwise).
- 2026-11-25, 2026.11.25.11: Nicola (.10 results: measure_spectrum Si 519.93 reliable; the stripped-quote profile loads). api: FLAT_WAIT_S 90 / LS6T_FLAT_WAIT_S, run_and_wait default; CURRENT_FLAT + flat_progress for _keepalive(msg=); _spawn returns how (breakaway or inside the job) -> request.json runner_started; _state interrupted frees the lock (runner.free_lock_of) and names the job case; cancel frees it. runner: Ctx.owns_lock (beat touches the lock only when held: the root of the forever-queue), holder_alive (state final or runner.json > LOCK_STALE_S 90 s), waiting.json around OPERATOR_VERBS; api.waiting_for in status/_summary (next: tell the user). Stale-correction warning uses spectro_repeatability. Tests: dead runner lock + cancel, waiting_for + flat wait. Note: the runner was already started detached by the flat tools; its death at the client's 140 s timeout suggests the breakaway from the app's job was refused (runner_started will tell).
- 2026-11-25, 2026.11.25.11 (cont.): Nicola's run 151438 (.10): grating changes with approach fine; native readbacks fine; profile_problems shown. SERIOUS: hold lost in g600_labspec "(before laser)" = the bridge's LaserGuard on the 'laser' verb, i.e. an instrument laser_off/on during the request (BUSY_OK allows laser_off) -> api laser_off now cancels the lock holder's request. Unsettled repeats (step/trend) -> nothing stored, unreliable, outcome "not stored". Laser line > ZERO_JUMP_CM1 from the calibration's -> unreliable ("another mode"); zero uncertain at > n/10 -> unreliable. FIX: false 'interrupted' at 15:18 -> Ctx.start_heartbeat (thread, 10 s, beat lock), runner.pid_alive (ctypes on Windows), runner_alive (beat or log within 90 s, else pid), used by _state and holder_alive; jobs instrument_lock_stale. Landing jumps (-1.6 at gc_1200 r1, laser line -4.2 at g600_c520 r1) still occur with approach -2: the leave-one-out outlier rule takes one per 5 repeats.
- 2026-11-25, 2026.11.25.12: Nicola: run 152541 stored corrections from two unreliable calibrations; profile_problems persisted after profile set fixed the value. _calibration_result now leaves res["_pending_store"] (args of xcal.store); runner.store_pending(res, warnings, unreliable) runs in run_any after the stop reasons are added: unreliable -> stored_as "nothing stored", outcome "NOT stored"; else xcal.store + stored_as/drift/"now corrects". profile.save drops 'problems'; load pops a saved 'problems'. P.load() runs per request in run_request (and in api.start for the hint); no cache besides the LabSpec ini probe. Tests: unreliable calibration stores nothing, then a reliable one stores; problems cleared after profile set @file.
- 2026-11-25, 2026.11.25.13: Nicola's runs 161318 / 162048 / 162546 / 163122 / 163408: T9.6 and T9.7 PASS (waiting_for, runner outside the job, laser_off cancels the request, no re-open). Common-mode landing scatter at 1200 (Si 0.76 vs Si-LL 0.06). runner._anchor_repeats (narrow LL in every repeat, offset-only fit, calibration.anchor != off) -> repeat_stats on (offset - LL), a = -(centre_rel + LL_median), centre reported absolute-equivalent, pass judged on the absolute median; pool per_centre when anchored; zero_mode anchored/absolute (xcal.store kw). _repeat_offsets and the single-spectrum lines undo laser_line_correction_cm1 (raw). Repeats not anchored: stored LL = median over repeats. analyse: default 'anchor' (corr with narrow LL, no laser_reference asked, kind != calibration): plateau steps <= fmin applied unless the peaks contradict (|dpk-d| > max(0.3, 0.25|d|)), plateau_report landing True, no unconfirmed warning. Calibration: LL jumps between repeats -> warning (repeats judge them); LL > 3 cm-1 from the last calibration -> warning, not unreliable. sd_successive_cm1 (outliers left out) -> repeatability store uses min(SD, successive). Corrections without zero_mode -> warning to recalibrate. sim_landing hook. Not done: matching repeat landings to a measurement's first landing after a grating change (the measurement's landing depends on where the spectrometer comes from; anchoring removes the question where the laser line is narrow).
- 2026-11-25, 2026.11.25.14: Nicola's 532 / 785 @ 1200 repeatability (163715, 164800) and the .13 checks. api.laser_filter (lasers.<k>.default_filter; profile setdefault None); measure_spectrum / calibrate_si filter=None. plan.change_settle_s / CHANGE_SETTLE_DEFAULT_S 40 (calibrations; lasers.<k>.change_settle_s); run_any laser_changed (status laser_nm before/after) -> _warmup of that length (why "after the change of laser"), or Ctx._hold_on open wait max(...) via ctx.laser_changed_now. _anchor_repeats returns why_not (off, scale fit, no line, broad, not enough fits, relative robust SD >= absolute robust SD); rep_info.not_anchored_because; single-spectrum narrow-line calibrations zero_mode anchored; analyse default anchor by zero_mode (width rule only for entries without it). Test: hop + multimode during anchored repeats -> unreliable (zero uncertain), nothing stored. Which lasers anchor is decided per calibration from its own repeats: expected 637.75 @ 1200 and 785.04 @ 1200 yes, 532 @ 1200 no, every laser @ 600 no (broad line).
- 2026-11-25, 2026.11.25.15: Nicola's T9.8/T9.9 (172706, 173408, 173838). 785 default_filter applied once set (173838: 10%). Anchoring per calibration unstable -> landing steps: runner.LANDING_K 3, LANDING_MIN_CM1 0.5, LL_SD_DEFAULT_CM1 0.2; landing_threshold (spectro_repeatability laser_line_sd_cm1, fallback the repeats' own robust SD); _landing_steps (median LL, steps with peaks alike -> offs_eff), learned via _rep_store_add (median of last 5); calibration.anchor auto|always|off (always = _anchor_repeats). Stored LL = landing median (not when broad). analyse: anchor default whenever the correction has a non-broad laser line; land_thr 0 for zero_mode anchored, else the threshold; small steps need |d| > land_thr and peak confirmation. zero_creep(per_point) for series (slope/min, t value, laser-line slope; learned when steady), xcal.age_min, expected uncertainty hypot(moved_sd, min(2, creep x age_min)). spectro_repeatability merges profile over measured. 785 first-repeat landing (LL -0.3, Si -0.9): below the landing threshold, left to the outlier rule. Feature request (live LabSpec map shown for a spectral band, e.g. the Si peak): MapEx has no spectral range; LabSpec's map cursors need their script command (to be found on the instrument PC: HELP\VBS, macros).
- 2026-11-25, 2026.11.25.16: Nicola's feature request (live map for a band) with the LabSpec 6.8.1 folder mounted. Found: PutValue/GetValue "PointerLimits" (red 1 X, red 1 Y, red 2 X, red 2 Y, green..., blue...; the help says nm) and "ActiveCursors" (ACTIVATE_CURSOR 1, ACTIVATE_BASELINE 2); HORIBA's SERVICE "Axial Resolution.vbs" sets DisplayUnit 1/cm then PointerLimits 280/320 on data 0 (before its map; spectrometer at 300 cm-1): values in the display unit, set on 0 before a map. plan.live_band / band_params; format map.live_band; VBS SetBand (unit check, GetValue first, set index 0/2, ActiveCursors index 0, read back) called from VView create (mapex) and VMapNative before Acq; runner _band_said -> result live_band (ctx.live_band reset per study); sim _band. To verify on the instrument: the band lines and the map's contrast (T6.x).
- 2026-11-25, 2026.11.25.17: Nicola: landing threshold ignored the profile and was inflated by the step itself. Decision: a dedicated key laser_line_sd_cm1 (profile entry or learned), still_sd_cm1 as the stand-in when it is missing, then the repeats' MAD (runner._mad_sd), then 0.2. _landing_steps uses _mad_sd for the fallback and for the learned value. Test with Nicola's numbers (LL +0.74 alone: not followed; -0.59 with Si -0.80: followed). T9.11 split into remote / screen / in person (Nicola). Possible later: export the live map's spectrum display with its cursors (LabSpec Paint with SHOW_CURSORS, as HORIBA's Cut-Off.vbs does) so the cursor position can be checked without the screen.
- 2026-11-25, 2026.11.25.18: Nicola's notes on .17 and the 40 x 30 s held series (637.75@1200, 18:19-18:40: creep -0.0018/min not significant; laser line jitter 0.12 point to point, wander +-0.25, drift -0.022/min, corr. with Si 0.14: follow only confirmed steps, never the wander; he sets laser_line_sd_cm1 0.15 in the profile). LL_SD_VERSION / CREEP_VERSION: older learned values dropped in spectro_repeatability. learn_laser_line_sd (series MSSD in analyse -> checks.laser_line_jitter; calibrations only when the threshold was not from their repeats); _tight_sd (tightest ceil(0.6n), min 3) as the last resort with a warning; threshold keys laser_line_sd_used_cm1, source per field. offsets_corrected_cm1. Calibration repeats jog from k = 0 when repeats > 1. learn_zero_creep: last 3 series (>= 3 min), applied when 2 significant with the laser line steady. Report mode: ll_ref from the correction's laser_line_cm1; perf 'zero' records the repeats' median. VBS VPaint (Paint 2, id, 10, 0,0,0,0, path), 'paint' in HOLD_VERBS (VBS + runner); runner _paint_band after the map (points: the MapEx id from the view; native: the map's id); sim v_paint. His test case for .18: the series landing +0.57 laser line / +0.47 Si from the calibration median, over 0.5 with Si alike: followed with the default laser_reference (needs process.peaks Si).
- 2026-11-25, 2026.11.25.19: Nicola's T9.12 PASS (threshold from profile 0.5, step followed at repeat 5, reference = median, small offsets not followed), follow-ups (step after 600->1200 followed: Si 520.76; first landing after a grating change fine; a step Si did not follow not followed), T9.11 (set 1, sent = readback 505.7;535.7, unit 1/cm: values in the display unit; Paint works on 6.8.1; the picture showed red 1 at 505.7, no second red limit, a green cursor at ~643, pink shading to it). Reading: index 1 is the second red limit (HORIBA's script), index 2 is not; .16-.18 wrote 535.7 into index 2 of the cursors (LabSpec's default data too). VBS SetBand: indexes 0 and 1, JoinArr arrays before/after, ActiveCursors before/after; BandReadback on the native map's ID after the map. runner: live_band.red / red_on_the_map; _paint_band moved after the measuring (both engines) with SetScaleEx (band +-200, y from the spectra), WMF at 200 dpi. Followed-drift warning text. _landing_steps: unfollowed jumps out of the quiet set. res zero_mode. objective_laser_note in the resolution warning. 532 @ 1200 laser line 15.3 cm-1 = 2.1 x Si: over LASER_LINE_MAX_WIDTH 2.0, rejected as a reference (TESTING note); learned 0.083 (532) / 0.146 (785) put the thresholds at the 0.5 minimum.
