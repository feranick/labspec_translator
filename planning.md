# labspec-translator (ls6t): planning and session context

A new chat starts with: "Read planning.md and start." Follow ~/Software/llm-translators/docs/create_new_translators.md
(the template), with ~/Software/build123d-translator as the model for a *local* translator (no SLURM, no router,
no llmt-runtime dependency).

## What this is

Raman studies (JSON) -> HORIBA LabSpec 6 on the instrument PC (Windows; first instrument: XploRA), with checked
results, a CLI (`ls6t`) and an MCP server (`ls6t-mcp`) that the Claude app runs locally on that PC, like b3dt.
LabSpec has no Python API: it runs VBScript (LabSpec 5/6 Automation manual, 2018, uploaded in the first chat). A
**bridge script runs inside LabSpec** so the operator sees every move and spectrum, confirms before the first
exposure, and can stop everything there.

## Architecture (version 2026.11.20.39)

```
Claude app -> ls6t-mcp (Python) -> request folder + detached runner (`ls6t run-request`)
                                     | files in LS6T_BRIDGE_DIR (inbox/*.cmd, outbox/*.res, heartbeat.txt, stop.flag)
                                     v
                     ls6t_bridge.vbs running in LabSpec 6 (fixed verbs only, never runs received code)
```

- `config.py` settings (LS6T_*); `profile.py` machine profile (discovery import; limits set by a person only);
  `format.py` study format + validation; `plan.py` study -> commands, nm <-> cm-1, estimate; `bridge.py` the file
  protocol; `runner.py` one request at a time (instrument lock), checks, results; `spectra.py` baseline, noise,
  spikes, fits, bands; `render.py` PNGs; `sim.py` simulated LabSpec + XploRA on the same protocol; `bench.py`
  known-answer benchmarks on the simulator; `api.py`, `cli.py`, `mcp_server.py`.
- `vbs/common.vbs` + `vbs/bridge_main.vbs` / `vbs/discover_main.vbs` / `vbs/script_motor_main.vbs`: `ls6t bridge
  install` composes `ls6t_bridge.vbs`, `ls6t_discover.vbs` and `ls6t_script_motor.vbs` with this machine's folders
  (ASCII, CRLF).
- Protocol 1: key=value lines, `%0A` line end, `%25` percent; commands UTF-16 (FSO Unicode mode); stop.flag names
  its seq; heartbeat age (file time) = bridge alive, except during modal confirm/resume.
- Safety: XY offsets from where the operator left the stage, Z from the operator's focus (never absolute);
  filter capped per laser / for sensitive samples / globally; max exposure, accumulations, points, minutes;
  operator confirm (LS6T_CONFIRM); popup close = halt; LabSpec cancel = operator stop; LabSpec script stop = quit.

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

## To do next

1. **At the instrument**: carry out TESTING.md (T0-T9, T5b-T5e) with the maintainer; record the answers to Q1-Q40 here and
   change the code where they differ from the assumptions (README "Assumptions about LabSpec").
2. Put the measured times in the profile's `timing` (T3, T7.2).
3. Later: trigger scripts during native maps (per-point actions: Send, PutDataInfo); script-controlled motors
   as map axes (temperature, polariser); Linkam temperature
   series (ManageTemperature has rates only: set points need RS232 through Send); a translators.json entry
   (llmt's bin/ scripts are bash: decide how a Windows install fits llm-translators).

## Open ideas

- Per-objective laser offsets (if T5b.4 shows they differ): video.laser_offset_um as {objective: [dx, dy]}.
- Use the measured offset to correct where maps and points land (today it only places the marker).
- Read AutoAlignment's state (Maintenance tab, status-bar indicator) with Windows UI automation, read-only, if
  T5c.1 shows it is exposed; ask HORIBA whether newer LabSpec automation offers AutoAlignment/AutoCalibration.
- Guided adjustments on other signals: the video spot's brightness or sharpness (focus without a Raman line).
- Adaptive maps: refine around boundaries found in a coarse map.
