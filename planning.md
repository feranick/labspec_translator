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

## Architecture (version 2026.11.20.14)

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
