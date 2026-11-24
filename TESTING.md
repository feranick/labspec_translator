# Testing labspec-translator on the instrument

This is the plan for the first sessions on the real instrument (XploRA Plus, LabSpec 6, Windows 11). It is carried
out by two people:

- the **maintainer**: the person responsible for the instrument, who knows its hardware, its laser safety rules and
  its normal use;
- the **tester**: the person installing ls6t, who runs the commands and records the results.

Everything ls6t does without the instrument is already tested: `pytest` covers format, protocol, analysis, full runs,
stops and cancel on the simulator, and `ls6t bench` gives known answers. What remains can only be learnt on the real
LabSpec and hardware. The plan goes from no laser and no motion to an LLM app driving a full map, in stages. Each
stage starts only when the previous one passed.

Version under test: the one `ls6t --version` prints (bridge protocol 1); write it in the results table.

**Already known** (2026-11-20, from the instrument's discovery dump, its `NFActiveX.ocx`, `OLConfig.ini`,
`OLSetup.ini` and LabSpec 6.7.1's service scripts): the motors and their positions (lasers 532, 637.75, 785.04 nm;
gratings 600/1200/1800/2400; filters 100–0.1 %; objectives x100_VIS, x10_VIS, x5_VIS, no x50; hole, slit,
LaserShutter Off/On, laser and Raman polarisers), the cameras (Top = 0, Internal = 1), the methods beyond the 2018
manual, and where LabSpec keeps its calibration. The open questions below say what is answered and what remains.

---

## Ground rules

1. **The maintainer is at the instrument for every test** in this plan, with the bridge's window and LabSpec in view,
   ready to stop. Nothing here is run unattended.
2. **The facility's laser safety rules apply as usual.** ls6t does not replace interlocks, eyewear or enclosure rules.
   Stages marked **LASER** fire the laser.
3. **Start gentle**: the lowest useful filter (1 % or 10 %), short exposures, silicon first. Raise power only with the
   maintainer's agreement.
4. **Stage moves**: start with the lowest-magnification objective (x5 or x10, longest working distance) and moves of a
   few µm. Before any Z move, and before turning the turret, the maintainer checks the clearance between objective and
   sample.
5. **How to stop**, in order of preference:
   - close the ls6t window in LabSpec (stops the current command, halts the bridge, closes the laser shutter);
   - cancel the acquisition in LabSpec;
   - `ls6t instrument end-bridge` (ends the bridge cleanly; this XploRA's LabSpec has no stop button for scripts),
     or, once halted, Cancel in the ls6t HALTED window;
   - the instrument's own stop or emergency controls.

   The maintainer should try each of them during the plan (T2, T3, T4). **Do not use LabSpec's live video buttons
   while the bridge runs**: LabSpec ends a running script when its video is started or stopped, and on 2026-11-20 a
   bridge restarted after that got stuck in its first command; after Cancel in the ls6t window LabSpec froze (STOP
   ALL did not respond). End the bridge first (`end-bridge`), use the video, then start the bridge again. If the
   bridge is stuck inside LabSpec (ls6t says so), nothing in LabSpec frees it: restart LabSpec.
6. **Stop the plan** at any unexpected motion, a move beyond what was asked, a dialog nobody understands, or LabSpec
   not responding. Record what happened (see [Recording](#recording)) before continuing.
7. **A laser blocked by hand needs someone at the instrument.** Backgrounds and white-light cut-offs with
   `laser_off: operator` rely on a physical beam block (LabSpec opens its shutter as usual). From a Remote Desktop
   session ls6t refuses them (`LS6T_HAND_BLOCK_REMOTE_OK=1` only with someone in the room blocking the beam).
8. **The LLM reaches the instrument only through ls6t's tools.** An app that can also run commands on the PC
   (ChatGPT desktop / Codex, Gemini CLI, Cursor, Claude Code, GitHub Copilot, Windsurf, Claude with a terminal) must not drive LabSpec by scripts, COM/ActiveX or
   the bridge's files: that bypasses every check above and can freeze LabSpec (a 2026-11-20 session opened LabSpec's
   *About NFActiveX Control* dialog this way and the bridge stopped). Set the app to ask before each command
   (INSTALL.md, step 8) and refuse commands that touch LabSpec.

## What to prepare

- The instrument PC with ls6t installed with `ls6t-setup-<version>.exe` ([INSTALL.md](INSTALL.md), *The installer*)
  and an LLM app connected (`ls6t connect`; `ls6t connect check` lists its tools).
- **Samples**:
  - a silicon wafer piece (calibration: 520.7 cm⁻¹);
  - polystyrene (a bead or film; reference lines 620.9, 1001.4, 1031.8, 1155.3, 1583.1, 1602.3, 2852.4, 2904.5,
    3054.3 cm⁻¹, ASTM E1840);
  - a sample with lateral contrast for maps, ideally one the maintainer already knows: graphene on SiO₂/Si, a
    patterned Si/SiO₂ chip, or a polymer bead on Si;
  - a transparent film on silicon for a depth profile (optional);
  - the white light (for backgrounds and the edge filter's cut-off), and a matt white sample if the maintainer has one.
- The maintainer's usual settings for these samples (laser, grating, filter, objective, exposure). They are the
  reference for what ls6t should reproduce.
- About **5.5 hours** for stages T0–T6 (T5c needs the maintainer to run AutoAlignment), and 1 hour for T7–T9 (they can
  be another day).

## Open questions this plan answers

The vendor manual (2018) leaves these open. Each is answered by the test listed; **Known** gives what the 2026-11-20
files already showed.

| ID | Question | Test |
|---|---|---|
| Q1 | Can LabSpec scripts use `Scripting.FileSystemObject` and `Scripting.Dictionary` (file access)? Without it the bridge cannot work as designed. **Known: yes** (the bridge wrote the discovery dump). | T1.1 |
| Q2 | Does the non-blocking window (`Message(..., 8)`) open, report -1 while open and 0 once closed? Does `MessageEx("", 14)` see a stop button for scripts, where LabSpec has one (this XploRA's has none)? (The bridge's discovery gave `selftest.stop_button` empty, not 0: check what it returns when pressed.) | T1.1, T2.3, T2.4 |
| Q3 | Does LabSpec stay usable (live video, menus) while the bridge loops? | T2.2 |
| Q4 | Do `MoveMotor(..., +10)` and `GetMotorStatus` behave as assumed? Does `MoveXY` return at once or block until the move ends? | T3 |
| Q5 | What are the motor names and position labels of the filter wheel and the objective turret? Is the turret manual? **Answered (2026-11-20): names and labels** (Filter 100 %–0.1 %; Objective x100_VIS, x10_VIS, x5_VIS), and **the turret is manual**: LabSpec's objective is a record of what someone selected, not a reading; discovery and OLConfig.ini list the configured objectives, not the mounted ones. | T3.2 |
| Q6 | In which unit does `GetValue(id, "XYData")` give x (nm or cm⁻¹)? How is `AcqInfo` laid out (a header row)? Where is the exposure that auto-exposure chose? | T4.1, T4.3 |
| Q7 | Does `GetAcqID` report -2 after a cancel, and does that state persist into the next acquisition? | T4.5 |
| Q8 | Does autofocus leave a spectrum behind (a data ID), and does it keep the new focus or restore the old one? | T5 |
| Q9 | Does `Save(id, "...l6s", "")` write a file LabSpec reopens? | T4.2, T6.1 |
| Q10 | Do the live views (`Map`, `Profile`) appear and fill in? Are the axis units right ("mkm")? What do Map's From/To limits do? | T6.1, T7.1 |
| Q11 | How does LabSpec's memory behave with large maps built point by point? | T6.4 |
| Q12 | Does a request survive closing the LLM app (the runner process breaking away from the app's job)? | T9.1 |
| Q13 | Video: does `Video(2)` return the live frame's id from a script, and does `Exec(id, 3)` clone it? Which image formats does `Save` write for a frame? **Known: the active camera is read** (`video.active_camera`); with the video off, `id_now` was 0. | T5b.1 |
| Q14 | Video pixels: which axis varies fastest, how are colours packed (COLORREF 0x00BBGGRR or RGB), and are the axes stage coordinates in µm? | T5b.2 |
| Q15 | Is `LaserShutter` the laser shutter, and do `PutValue(0, "ShutterMode", 1)` (manual) plus moving it On / Off open and close it? **Known: a motor `LaserShutter` with Off/On. On the XploRA (2026-11-23, 637.75 nm, Si): LabSpec's own Laser On / Off blocks the beam (the Si line comes and goes in the real-time display; the beam is not visible on the top camera, even at 100 %), but ls6t's use of the motor did not (the Si line stayed in a `laser_off: "shutter"` background). Open until the bridge's shutter fix passes T1.2 step 2; keep `motors.laser_shutter` null. From .23.13: `ls6t instrument shutter-test` tells which move blocks.** **Answered on .23.14 (2026-11-23, 10 % filter, display zoomed on the line): `LaserShutter` blocks by value and by label, and Off holds with ShutterMode back to automatic (verdict all true). The morning's failure (Si at 86 % with the shutter "closed", .23.9) was most likely LabSpec's acquisition reopening the shutter in automatic mode, which the manual mode used since .23.13 prevents. Two laser_off 'shutter' backgrounds then showed no Si line (8.5 counts at most against 257 open). Remaining: `ls6t instrument shutter-verify` per laser (.23.15) to let ls6t use it.** | T1.2, T5b.3 |
| Q16 | How far is the beam from LabSpec's laser position, per objective, and how repeatable is the measurement? | T5b.4, T5e |
| Q17 | Does this XploRA have HORIBA's AutoAlignment module (Maintenance tab)? Can its *Current status* be read from outside (Windows UI automation, read-only)? | T5c.1 |
| Q18 | Does `Message(text, 1)` (OK/Cancel) return 0 for OK and wait while the operator runs AutoAlignment in the same LabSpec? | T5c.2 |
| Q19 | Does LabSpec's dynamic `Dialog` stay open, update and answer Done/Abort while short acquisitions run? How long is one reading (`period_s`) at 0.2 s exposure? (HORIBA's own scripts use `Dialog` throughout.) | T5d.1 |
| Q20 | Is the guided loop fast enough for a person turning a knob (focus, power): does STOP come before the operator has gone too far? | T5d.2–T5d.3 |
| Q21 | Does the focus scan find the same focus as the maintainer (and as LabSpec's autofocus) on Si and on a real sample? Is 1 µm a good step at x100, and what at x10 and x5? What depth resolution (`axial_fwhm_um`) does it report? | T6.5 |
| Q22 | Does the stage return home and Z to the focus at the end of a request, and does a `move` with `stay` change the next request's start? | T6.6 |
| Q23 | Does the live video keep updating during a Raman acquisition (so a frame half-way through shows the beam), on this XploRA? | T5e.1 |
| Q24 | Does the Raman spot scan over a small feature agree with the beam seen on the video (T5b.3 / T5e.1)? Which features work (dust on Si, a gold dot, a pit)? | T5e.2 |
| Q25 | How much does the beam drift from LabSpec's marker over a day, between objectives, after AutoAlignment? | T5e.3 |
| Q26 | Which Z direction focuses deeper into the sample (`stage.z_into_sample`)? Does a depth profile through a film of known thickness give it back with the refractive index? | T6.3 |
| Q27 | Which of LabSpec's calibration values do *Maintenance > Instrument calibration*'s zero and coefficient change? **Known: the values** (`OperVar ACT_INSTRUMENT[AF / BF / Diode / Focal / Angle / Gamma / SpMin / SpMax][grating]`, read by `instrument calibration`); a `calibration` with `adjust: labspec` records which change (`calibration.labspec_parameters.changed`). | T4.7 |
| Q28 | Can the white light stay on during acquisitions, and does a background with the laser off (shutter, or *Laser Off*) remove it cleanly? | T4.8 |
| Q29 | Native maps: does `SetMappingParams` take absolute stage positions or offsets (`mapping.positions`)? Does the map have the asked points? | T6.7 |
| Q30 | Native maps: does `Acq(ACQ_SPECTRAL_IMAGE, -1, ...)` use the interface's exposure and accumulations set by `PutValue`, or does it need them in `Acq` (`mapping.acq_time`)? Which spectral window does it use? | T6.7 |
| Q31 | Native maps: does `GetAcqID` return the map when it ends, and does `ACQ_CANCEL` stop it? | T6.7 |
| Q32 | Native maps: in which order are the values in `GetValue(map, "Data")` (`mapping.data_order`), and how long does writing out a 50 × 50 map take? Does `Save(map, "...txt", "txt")` write LabSpec's text export? | T6.7 |
| Q33 | Is SWIFT available on this XploRA (`GetValue SWIFT`), and how much faster is it? Time per point native vs point by point (`mapping.native_point_s`) | T6.7 |
| Q34 | Which commands does the installed LabSpec offer beyond the 2018 manual? **Known:** 15 methods in the `NFActiveX.ocx` type library (planning_log.md); HORIBA's scripts also use `Dialog`, `OperVar`, `GUI`, `SetSingleScriptParam`, `SetDetectorZone`, `SetAutoExposure`, `MoveXY`, `MapEx`, `Send`. Re-run `ls6t-typelib` after a LabSpec update. | T1.3 |
| Q35 | `GetValueSimplePart(id, "Data", 0, VT_R4, start, size)`: in which units are start and size? **Known: not values** (size 8 gave 2 values: bytes?), so native maps are read whole (`mapping.export_chunk` 0). Discovery no longer probes it (its write probe is the suspect in the 2026-11-20 crash): left open, chunked export stays off. | — |
| Q36 | `SaveConfig(ini, 0)` / `LoadConfig(ini, 0)`: what does the file hold (laser, grating, filter, hole, slit, acquisition settings, stage?), does `ShowDialog` 0 run without a dialog, and does loading move motors? Is it safe to restore it after each request (`session.restore_config`)? | T1.4 |
| Q37 | `GetConfigParam`: what parameters does it list? **Known: it works, and lists none** for the bridge (it is the script's own configuration; no longer probed). `SetConfigParam` is not used. | — |
| Q38 | Script-controlled motors: when does LabSpec run the motor script, and what do `GetMotorAction`'s Action and Position hold for a move to a value, to a named position, and a stop? What does `SetMotorInfo`'s State mean? | T1.4 |
| Q39 | Extended video (`Video(3)`): where is its area set, does the call return only when the image is complete, does it return the image's id, and where is the stage left? | T5b.6 |
| Q40 | `GetValueEx` is in the manual but not in this build. **Known: confirmed missing** (`selftest.get_value_ex=0`, no longer probed); nothing depends on it. | — |
| Q41 | `OLConfig.ini` (`Parameters\ACQUISITION`): ls6t reads the camera names, the objectives' NA and their field of view (inferred) from it. Is the field of view right (a snapshot's `field_um`)? **Known:** cameras Top = 0 and Internal = 1, confirmed by switching in LabSpec. | T5b.1 |
| Q42 | LabSpec's AutoCalibration (`OLSetup.ini` names `...\AutoCalibration\UniversalAutoCalibrationv3.vbs`, an older install's path): is the script on this PC, and can the bridge run it (`instrument autocal_info`)? | T4.9 |
| Q43 | LabSpec 6.7's constants read a motor's positions with 100+i (value) and 200+i (name); the 2018 manual's 10+i / 20+i worked here for motors of up to ten positions. ls6t uses 100+/200+ beyond ten: check such a motor's labels, if any. | T1.1 |
| Q44 | Cameras. **Answered (2026-11-20): ls6t cannot switch cameras reliably on this XploRA.** `Video(10 + 0)` was ignored; `Video(10 + 1)` changed LabSpec's selection (what `GET_ACTIVE_CAMERA` reports) but the view only from the next video start; a script's `Video(STOP_VIDEO)` hung the bridge (twice, no dialog, LabSpec running); stopping or starting the video in LabSpec's interface ends the bridge script, and a bridge started right after got stuck in `ping`; after Cancel in the ls6t window LabSpec froze. So ls6t never stops the video and does not switch cameras (`video.switch_camera` false); the operator changes cameras in LabSpec with the bridge ended. Open: does a bridge started after a clean `end-bridge` and a video change by hand answer normally? HORIBA's own stop sequence (STOP, then `GetMotorStatus("Video")` until 0) **returned** (3.38 s, T3.12), but the next video query (`status`) hung: does avoiding every video query after the bridge's own stop, and waiting 5 s before a new start, keep the bridge answering?  Separate from the stop hang: switching to camera 0 was ignored even with the video stopped and right after a LabSpec restart; when switching is tried again, first check `Video(10 + 0)` after a clean stop and the 5 s wait.  **T3.12–T3.13 passed on .28:** HORIBA's stop returns (3.09–3.38 s) and, with no video query after it and 5 s before the next start, the bridge keeps answering; `video.stop_from_script` is now on by default. | T3.11–T3.14 |
| Q45 | The grating. **Answered in part (2026-11-20):** LabSpec accepts any grating with any laser; its SpMax per grating is nm (600: 1138, 1200: 1112, 1800: 935, 2400: 690; consistent with one maximum grating angle for 1800 and 2400, and the CCD's limit for 600 and 1200), so 2400 gr/mm reaches no Raman shift at 785.04 nm, about 1190 cm-1 at 637.75 nm and 4300 cm-1 at 532 nm; 1800: about 2040 cm-1 at 785 nm; 1200: about 3750 cm-1 at 785 nm. ls6t refuses studies beyond it. SpMin reads 100 for every grating (not a plausible limit: a placeholder or another unit): shown, not applied. Confirmed on .39: every `set` reports its reach from SpMax. Open: does the turret turn when LabSpec answers a grating move, at the next acquisition, or only on the first motor command (24.1 s then; 0.06–0.09 s later), and is that first command slow after a bridge restart alone or only after LabSpec's? From .38 the SpMin / SpMax reach every profile from the bridge's inventory (they only restrict), reviewed or not. | T3.16 |

---

## Recording

For each test, record one line (in a copy of the table at the end, or in a notebook):

`ID | date and time | tester, maintainer | pass / fail / partial | what was seen | files`

- **Files to keep**: the discovery dumps, the request folders (`%USERPROFILE%\ls6t_runs\run_...`), and
  `%USERPROFILE%\ls6t\bridge\heartbeat.txt` when something looks wrong.
- **Screenshots** of LabSpec dialogs nobody expected.

**Or record from the command line:** `ls6t test record T4.1 pass --notes "Si line seen" --answers '{"Q6": "x in nm"}'`.
Every record goes to `%USERPROFILE%\ls6t\testing\log.jsonl`, and `%USERPROFILE%\ls6t\testing\report.md` is
this plan's results table filled in, with the answers to the open questions (`ls6t test report`; `ls6t test plan`
shows what is done and what is next).

After the session, the tester adds a dated entry at the end of `planning_log.md`. It says what passed, the answers to
Q1–Q45, and the measured times, and lists anything to change.

---

## Running the tests from the chat (test mode)

The tests can also be run by talking to the LLM app (Claude, Claude Code, ChatGPT / Codex, Gemini CLI, Cursor, GitHub Copilot, Windsurf, LM Studio) instead of typing the
commands. The LLM reads each test from this file, tells the maintainer what to do in LabSpec, checks what it can
itself (the bridge's state, read-backs, spectra, snapshots), asks about what only a person can see, runs the
commands, and records the result and the answers to the open questions in the same log and report.

1. Turn test mode on for the app (it is off by default; normal use never offers these commands):

   ```powershell
   ls6t connect claude --test         # or codex / gemini / cursor / claudecode / copilot / windsurf / lmstudio; --sim to practise on the simulator first
   ```

   Restart the app. A new tool, `hardware_test`, appears. `ls6t connect claude --no-test` turns it off again.
2. Say, for example: *"We are testing ls6t on the instrument. Start with T0 and go in order."* or *"Let's do T3.4."*
   Direct requests work too and are carried out at once: *"turn the internal camera on"*, *"filter to 1 %"*,
   *"grating 600"*, *"move the stage 10 µm in X"*, *"take a 1 s spectrum around 520 cm⁻¹"*, *"what is the state?"*.
3. For each test the LLM:
   - reads it (`hardware_test step`), and tells the maintainer what to prepare or do;
   - runs the commands: `ls6t instrument ...` in this plan is the `instrument` tool, `ls6t bridge call ...` is
     `hardware_test call`, `ls6t instrument snapshot` is the `snapshot` tool; studies go through `validate_studies` /
     `run_studies`;
   - checks the pass criteria, asking the maintainer for what it cannot see (the video window, the stage moving);
   - records the result, the values and the answers (`hardware_test record`).

Guards, beyond those of `ls6t bridge call`:

- every command that can fire the laser asks the operator in LabSpec, every time;
- the filter only moves by label and within the profile's caps;
- X/Y moves stay within `limits.max_xy_um` and Z within `limits.max_dz_um` of where the stage is;
- nothing runs while a request holds the instrument, and `quit` is refused;
- files go to `%USERPROFILE%\ls6t\testing\files`.

What changes ls6t's own settings stays with a person: `ls6t profile set/import`, `ls6t alignment record`, `ls6t
references clear`, `ls6t bridge install`, `ls6t-typelib`, `pytest`. The LLM gives the exact command to run.
Turn test mode off when the tests are done.

---

## T0. Software on the instrument PC (no LabSpec needed)

Use *Start > ls6t PowerShell* (installer), or a PowerShell with the venv active (from source).

| Step | Command (PowerShell) | Pass |
|---|---|---|
| T0.1 | `ls6t status` | versions listed; `settings.LS6T_MODE` is `labspec`; `bridge.running` false |
| T0.2 | `ls6t bench` | `"failed": []` (all 14 benchmarks pass on this PC) |
| T0.3 | `ls6t connect check` | each connected app's ls6t entry starts and lists the tools (from source: also `pytest -q`). **After installing .22.14 or later:** quit the Claude app completely (tray icon > Quit), start it again, and check that the ls6t tools are there in a new chat; record `running_app_reads` from `ls6t connect check claude`, and whether `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude` holds a `.user-data-left.<id>` file (the app's own sign that it moved to `Claude-Data`). If the tools are missing, Claude still read the `Packages` copy: `ls6t connect claude` and report it. **Passed on .22.14 (2026-11-22):** the app reads `Claude-Data\claude_pzs8sxrjxfjjc` (`running_app_reads`), server up in 2.48 s with 15 tools, tools present after the restart; the marker is there (122 bytes, written 21:17, the minute of the install and the restart: the app rewrites it; ls6t only reads it) |
| T0.4 | `dir $HOME\ls6t\vbs` | three scripts (bridge, discovery, the optional script-motor logger) and the bridge folder exist (the installer wrote them; from source: `ls6t bridge install`) |

## T1. Discovery (no laser, no motion)

**T1.1 The discovery script (optional).** The bridge records the instrument's inventory each time it starts, and
the profile comes from that; this script adds the self-tests of the bridge's design. It only reads (no LabSpec data
is created) and logs each step in `%USERPROFILE%\ls6t\discovery_progress.txt`. On 2026-11-20 the .21 discovery
crashed LabSpec on one account (an *About NFActiveX Control* box, then a crash); if LabSpec shows any unexpected
dialog or stops, **do not run it again**: send `discovery_progress.txt` (its last `begin` line is the step) to the
ls6t maintainer; a later run skips that step (`discovery_skip.txt`). Why the About box opened is not known (ls6t
never calls `AboutBox`): if it appears again, even without a crash, record it. In LabSpec: *Processing > Scripts*, add
`ls6t_discover.vbs`, run it. A small window appears and closes by itself; a message box gives the dump's path.

Pass:
- the dump file exists (**Q1**), and `discovery_progress.txt` ends with `finished`;
- in the dump: `selftest.pause=1`, `selftest.popup_open=-1`, `selftest.popup_closed=0`, `selftest.status_bar=1`, and
  record `selftest.stop_button` (**Q2**);
- `system=XploRA Plus`, `detector_temp_c` close to LabSpec's display;
- `video.id_now` > 0 if the live video was running (start it first to check), and no `video.error` (**Q13**);
- `selftest.last_error_info=1`; any motor with more than ten positions has right labels (**Q43**).

**T1.2 Review the profile (before any LASER stage).** ls6t already uses a profile made automatically from the
bridge's inventory and LabSpec's `OLConfig.ini`: the real lasers, gratings, filters, objectives (with their NA),
holes, slits, cameras and polarisers, every laser capped at a 10 % filter, and no laser shutter. `ls6t status` says
so (`profile.automatic`). With it the instrument can be set up and the stage moved (T2, T3), but **the laser does
not fire** on the instrument until a person reviews the profile. Make it the maintainer's:

```powershell
ls6t profile propose                                    # profile.proposed.json from the bridge's inventory
notepad $HOME\ls6t\profile.proposed.json
```

With the maintainer, go through:
- `motors`, `lasers`, `gratings`, `filters`, `objectives`, `holes_um`, `slits_um`, `polarizers`,
  `video.cameras`: as on the instrument (they came from LabSpec; check, don't retype);
- **per laser, required**: `max_filter_pct` (the automatic profile's 10 % until now) and `power_mw`, the power at the
  sample at 100 %, measured with a power meter;
- **limits, decided by the maintainer**: `sensitive_max_filter_pct`, `max_xy_um` (start with 500), `max_dz_um`
  (start with 20), `max_laser_on_s`, `max_exposure_s`, `max_accumulations`, `max_points`, `max_minutes`,
  `stage.x_um` / `stage.y_um` if wanted, `detector.max_temp_c`;
- `manual_objective`: true on this XploRA (manual turret, the default): ls6t then asks the operator to change the lens by hand and confirm, and records the objective as operator-confirmed or as LabSpec's unchecked setting; `objectives`: keep only the lenses really mounted (discovery lists what LabSpec is configured for);
- `motors.laser_shutter` (**Q15**): only once the maintainer has confirmed that `LaserShutter` is the **laser**
  shutter, set it to `"LaserShutter"`. Until then snapshots with the laser and `laser_on` are refused.
  **How to confirm it (any instrument): on the spectrum, not on the camera.** The beam is often invisible on the
  video (the XploRA's top camera shows nothing even at 100 %). Put silicon under the objective, in focus, with a low
  filter (1 %); start LabSpec's real-time display (RTD) around 520 cm⁻¹ with a short exposure; then switch the
  status bar's Laser On / Off (or move the candidate motor On / Off in LabSpec) a few times. Pass: the Si line
  appears with On and disappears with Off, each time; nothing else changes (no grating or filter move). If the
  line does not come and go, that motor is not the laser's shutter: leave `motors.laser_shutter` empty and tell the
  ls6t maintainer.
  **This first step only shows that LabSpec's toggle blocks the beam, not that ls6t can.** Second step, required
  before ls6t uses the shutter (from .23.15 ls6t enforces it): set the motor (`ls6t profile set motors.laser_shutter
  "<name>"`), restart the bridge, put silicon under the objective, in focus, with a filter within the caps, and run
  **`ls6t instrument shutter-verify [--seconds 2]`** for each laser you will use the shutter with. It takes a
  reference spectrum at 520 cm⁻¹ with the laser, then the same with the shutter closed (each 2 accumulations with
  LabSpec's spike removal, so a cosmic ray is not taken for the line), and passes when what passes the closed
  shutter is at most `shutter.max_leak_frac` of the open signal (default 0.001 = 0.1 %; `leak_frac` is recorded),
  measured with a reference strong enough to see a leak of that size (`sensitivity`; else "not conclusive": a
  stronger filter such as 10 %, or longer). From .23.16: on .23.15 a 99.96 % block (0.04 % left, seen at SNR 14.8
  against 1185 cps) failed the earlier rule "undetectable", which any real shutter fails against a strong reference.
  **On .23.18 (2026-11-23, 10 %, 2 × 2 s), six runs:** 0.107 %, 0.164 %, 0.172 % (fail at 0.1 %) before a LabSpec
  restart, 0.058 %, 0.089 % (pass) after it; 0.042 % on .15 with 5 s. The shutter blocks ≥ 99.8 % every time (2–7 µW
  at the sample); the closed signal is 1.3–3.5 counts per acquisition in every run, more like a short burst at the
  start or end of each acquisition than a steady leak. **Both runs after the LabSpec restart were lower than the three
  before it:** if that repeats, something in LabSpec's state raises the leak (note the time since LabSpec started with
  each run). **From .23.19:** `--repeats N` closed spectra (default 3), judged on the worst (`worst_leak_frac`,
  `leak_fracs`, counts per accumulation in `closed[].si_counts`); `--burst-seconds S` adds one closed spectrum of
  another exposure (`burst_check`: a burst gives the same counts at any exposure, a steady leak counts in proportion;
  try `--seconds 2 --burst-seconds 8`); every attempt, passed or not, is kept (`attempts` in `shutter_verified.json`;
  a pass records `failed_attempts_before` and the recent attempts); the default limit is 0.5 % (about 3 × the worst
  seen; a profile saved earlier may still say 0.001: `ls6t profile set shutter.max_leak_frac 0.005`).
  **The burst confirmed on every laser (2026-11-23):** the closed shutter passes a few counts per acquisition
  whatever the exposure (532: 0.5–1.7; 637.75: 1.3–3.5; 785.04: 1.3–3.3; on 785 nm 1.6–6.6 counts at 2 s against 2.55 at
  8 s, `burst_check` "a burst per acquisition"), so the fraction depends on each laser's Si signal (785 nm: about 680
  cps against 4500 at 532). 785 nm failed at 2 s on the burst alone and passed at 8 s (0.042–0.048 %); 532 nm passed at
  2 s (0.019 %). **From .23.26 the default verification exposure is 8 s** (2 accumulations, 1 reference + 3 closed:
  about a minute of laser on Si). Only a
  pass writes the record (`shutter_verified.json`: date, laser, version, motor, how it is moved, the ratio,
  `during_checked`) without which ls6t refuses every use of the shutter: `laser_off: "shutter"` (background, cutoff;
  without it they default to `operator`), `laser_on`, snapshots with the laser and `spot` 'shutter'. A record is
  per laser, and lapses after `shutter.verify_max_days` (30), when the motor or `shutter.by` changes, or when a
  release changes the bridge's shutter handling. If it fails ("NOT BLOCKED"), set `motors.laser_shutter` back to
  `null` and run `shutter-test`; tell the ls6t maintainer.
  **XploRA, 2026-11-23:** step 1 passed (637.75 nm: the Si line came and went with LabSpec's Laser On / Off; the beam
  is not visible on the top camera even at 100 %); step 2 failed (ls6t's own use of `LaserShutter` did not block
  the beam). Keep `motors.laser_shutter` null on this instrument until the bridge's shutter fix is in and step 2
  passes. Details: request `run_20261123_154736_fbb6e8`, study `shutter_test` (background, `laser_off: "shutter"`,
  637.75 nm, 2400, 1 %, x100, 520 cm⁻¹; LabSpec's Laser On set by hand before; AutoExposure on, 15 s): 2870 counts
  against 3319 in the reference 17 min earlier (86 %). The .23.12 bridge moved the shutter by value 0 and never read
  it back.
  **From .23.13, between step 1 and step 2: `ls6t instrument shutter-test` (`--motor LaserShutter` while the profile
  has none).** Use a filter that makes the Si line unmistakable (10 % on the XploRA) and **zoom LabSpec's display onto
  the line** before answering, waiting two or three refreshes: on 2026-11-23 a first run at 1 % on a zoomed-out
  display had the line "visible" after every move (and led to a wrong note, now discarded). With the real-time
  display running on Si and the status bar's laser On, ls6t moves the shutter five
  times (value 0, value 1, label Off, label On in manual ShutterMode, then Off with ShutterMode back to automatic),
  reads each back and asks whether the Si line is there; then it puts LabSpec's ShutterMode and the shutter back.
  Note `steps` (read-backs and answers) and `verdict`: which move blocks the beam, and whether Off holds in automatic
  mode. Then step 2 with `shutter.by` as found. From .23.13 the bridge moves the shutter by label (profile `shutter`:
  `{"off": "Off", "on": "On", "by": "label"}`), refuses to acquire unless it reads back Off, watches it during the
  acquisition (cancels if LabSpec opens it) and records it (`laser_shutter` in the background's result); a background
  with Raman lines while the shutter was trusted is not stored and is unreliable.
  **Narrow lines in backgrounds (.23.19):** with a verified shutter, three backgrounds (7.8 counts at most in 2 s
  against 257 for Si open) still showed "lines" of a few counts at nine different places (415, 516, 737; 269, 358,
  558; 480, 594, 661): noise. A line now counts only above `checks.background_line_min_counts` (20 counts) and is
  warned about only when it recurs within 5 cm⁻¹ in another background with the same laser
  (`background_lines.json`); the result lists them all (`narrow_lines`: `found`, `above_min`, `recurring`).
  **Can the bridge read the shutter during an acquisition? (.23.14)** LabSpec's Acq has held the script until the
  spectrum was taken (the extended-range acquisitions that looked stuck). Then the "during" reads start only
  afterwards, and LabSpec could open the shutter for the acquisition and close it again unseen. In step 2, note in
  `laser_shutter`: `acq_call_held_s` (how long Acq kept the bridge), `after_acq_until_data_s`, `reads`,
  `first_read_s`, `watched_fraction` and `during_checked`. Pass for the read-backs to count as a check during the
  acquisition: `during_checked` true (at least one read while LabSpec was still acquiring, `after_acq_until_data_s`
  ≥ 0.5 s). If false, ls6t says so in `warnings`, and only the spectral test protects.
  **What protects, in order:** the spectral test (step 2, on Si), `shutter-test`, and the read-backs. The
  background's narrow-line check is only a backstop: it caught the XploRA failure because Si has a strong, narrow
  line; on a sample with only broad features or fluorescence an unblocked laser would go unnoticed.

Then import the edited file, which checks it and marks it reviewed:

```powershell
ls6t profile import "$HOME\ls6t\profile.proposed.json"
```

Pass: a laser with `nm`, `max_filter_pct` or `power_mw` blank is refused (try once); after the import, `ls6t status`
shows `profile.reviewed: true`; `ls6t format` shows the instrument's choices and the limits agreed.

**T1.3 The installed LabSpec's commands (Q34).** Done on 2026-11-20 from the `.ocx` (planning_log.md). Repeat only after
a LabSpec update: `ls6t-typelib --format markdown --out labspec_commands.md`, and compare.

**T1.4 LabSpec configurations and script motors (Q36, Q38; maintainer; no laser).** Start the bridge first (T2.1).

1. `ls6t instrument config save before_tests`. Pass: `ok`, `exists` true; the file is in `LS6T_HOME\configs`. Open it
   in Notepad and note what it holds (**Q36**). `ls6t instrument last-error` shows LabSpec's last error, if any.
2. In LabSpec, change the grating and the filter. `ls6t instrument config load before_tests`; LabSpec asks (answer
   Yes). Pass: grating and filter are back; note whether anything else moved (stage, turret, laser). Try once with
   `--show` to see LabSpec's own dialog, and once answer No (nothing changes).
3. Only if steps 1–2 pass and the maintainer agrees: `ls6t profile set session.restore_config true`, run a short
   spectrum study with another grating, and check LabSpec's settings are back afterwards (`config_restored` true in
   the request's status).
4. Script motors (optional, **Q38**): add `ls6t_script_motor.vbs` as a script-controlled motor in *Processing >
   Scripts*, move it from LabSpec's interface to a value, to a named position if offered, and stop it. Send
   `LS6T_HOME\script_motor_log.txt` to the ls6t maintainer. Remove the motor afterwards.

## T2. The bridge, idle (no laser, no motion)

**T2.1 Start.** In LabSpec, add and start `ls6t_bridge.vbs`.

| Pass | |
|---|---|
| The ls6t window appears; the status bar says the bridge is running | |
| `ls6t status`: `bridge.running` true, `heartbeat.state` idle, `age_s` below 2 | |
| `ls6t instrument status`: laser, grating, filter, objective, polarisers, X/Y/Z, detector temperature, camera match LabSpec's display | |
| `discovery_latest.txt` in `%USERPROFILE%\ls6t` has the start's time (the inventory) | |

**T2.1b Two Windows accounts (2026-11-23, .23.9).** With ls6t installed under two accounts: under account B, start the `ls6t_bridge.vbs` that account A added to LabSpec's list. Pass: the heartbeat appears in B's `%USERPROFILE%\ls6t\bridge` with `account=` B's profile folder; if A's copy has another version, LabSpec's status bar says to start B's own copy and `ls6t instrument status` shows `bridge_mismatch`. Then, with the bridge running under A and none under B, `ls6t status` under B names A's bridge.

**T2.2 LabSpec while the bridge runs (Q3).** The maintainer uses LabSpec normally for a minute: live video, menus,
the spectrum window, moving the stage with the joystick or software. Pass: LabSpec responds normally, and
`ls6t status` still shows a fresh heartbeat afterwards. Note any sluggishness.

**T2.3 Halt and resume (Q2).** **Passed on .46 (2026-11-21, over RDP):** titles and buttons as below (HORIBA's
`#title#button` form works), Halt opens the HALTED window, resume answered in 10.3 s (`picked_up_after_s` 0.22,
`prompt_shown_after_s` 0.02, `answer_after_s` 10.1: reading time; the question appeared in under a second; the
58.8 / 99.6 s of .42/.43 were the question hidden behind the HALTED window, now closed first), the window came back
saying RESUMED, End bridge ended the bridge cleanly (reported stopped) and it restarted. `dialog_appeared_after_s`
was missing: from 2026.11.21.1 any new window of LabSpec's or its helper processes counts (with its class and
process), and `null` means it was watched and none was seen. The maintainer presses **Halt** in the ls6t window (titled "ls6t: in control";
before .44 its button was Cancel and its title LabSpec's "Non blocking").

- The window "ls6t: HALTED" appears at once (also during a move or the start-up warm-up); `ls6t status`:
  `heartbeat.state` becomes `halted` once LabSpec's current call ends. Its button **End bridge** ends the bridge.
- `ls6t instrument set --grating <current grating>` is refused ("halted by the operator").
- `ls6t instrument resume`: LabSpec asks whether ls6t may control the instrument again. The maintainer answers
  **No**: the bridge stays halted. Repeat and answer **Yes**: the HALTED window closes and "ls6t: in control"
  reappears, saying ls6t RESUMED (nothing more to answer); the state is `idle`. Record the answer's
  `picked_up_after_s` (until the bridge took the command up), `prompt_shown_after_s` (until the question was put on
  screen) and `answer_after_s` (on screen until answered): on .42/.43 a resume took 58.8 and 99.6 s with the bridge
  idle and halted. From .45 the HALTED window is closed while the question is asked. `prompt_shown_after_s` is when
  the bridge *asked*; if LabSpec shows the question late, or behind another window over RDP, that time counts as
  `answer_after_s`. So whoever answers also notes roughly how many seconds after the request the question became
  **visible**; from .46 the answer also gives `dialog_appeared_after_s` (when Windows first listed LabSpec's
  question window, Windows only; it cannot see whether another window covers it).
- Halt again, then close the HALTED window: the bridge ends (`stopped`). Start `ls6t_bridge.vbs` again.
- **Known (2026-11-20):** with a bridge running or halted, LabSpec does not start `ls6t_bridge.vbs` a second time (it
  runs one script at a time): end the bridge first (End bridge, or `ls6t instrument end-bridge`).
- Right after a bridge start, `ls6t status` waits for the inventory and warm-up (the grating's first status took
  36–51 s) instead of timing out, and says so (`waited_for_bridge_start_s`). Record the warm-up times (`ping`).

**T2.4 Ending the bridge (Q2).** `ls6t instrument end-bridge`. Pass: the ls6t window closes, `heartbeat.state` is
`stopped`, and `ls6t status` says the bridge was stopped (not "ended without stopping"). Start the bridge again. If
this LabSpec has a stop button for scripts (this XploRA's does not), also try it once: record what happens.

**T2.5 Stale stop.** While idle: `ls6t bridge call ping`, then create an old stop request by running
`ls6t bridge call move motor=Grating mode=value value=<current> --timeout 0.1` (it times out on purpose), then
`ls6t bridge call ping`. Pass: the last call answers normally.

## T3. Motion and set-up (no laser)

Use the x5 or x10 objective, the stage at a safe height, and small moves. Compare each answer with LabSpec.
`instrument set` is what studies and the LLM use (names as people say them, caps, read-back); `bridge call` checks
the single command underneath.

| Step | Command | Pass |
|---|---|---|
| T3.1 | `ls6t instrument set --filter 1%`, then each other filter up to the caps; `--filter 50%` with the 10 % cap | the wheel moves; `after.filter` equals the label asked; above the cap: refused, nothing moves |
| T3.2 | `ls6t instrument set --objective x5` (**Q5**), then back | LabSpec shows ls6t's question: change the objective BY HAND to x5, check the clearance, OK. After OK, LabSpec's objective reads `x5_VIS` and the answer's `objective.source` says it was changed by hand and confirmed. Cancel once: nothing changes. 2026-11-22 (.22.7): LabSpec asked a second time during the move (wait_s 3.4 s and 1.3 s). Since .22.8 the move goes with MOTOR_NO_MESSAGE only right after the operator's Yes to ls6t's question (never for any other objective move, and not with confirmations off: then LabSpec's own prompt is the one confirmation): pass when LabSpec does not ask again and the move's `labspec_prompt` says "suppressed" ("LabSpec refused MOTOR_NO_MESSAGE": record it; profile `suppress_labspec_prompt` false restores the old way). |
| T3.3 | `ls6t instrument set --grating 600`, then back | the grating moves; read back; note `seconds` |
| T3.4 | `ls6t instrument set --laser-nm 532` (and 638, 785) | the laser line changes in LabSpec (no emission); read back; note the time |
| T3.5 | `ls6t instrument set --hole 300 --slit 100` | read back (or the error, if not motorised) |
| T3.6 | **XY passed (2026-11-22, .21.4):** offsets (500, 300), (3500, 3500), (3500, 0) µm reached with encoder read-backs (300.1 for 300), 0.73 s for ~580 µm, 1.14 s for ~4950 µm; `stay` true/false right; Halt mid-request left the stage where it was (from 2026.11.22.1 the answer gives `stage_at_end` and says it was not returned); Y crept 0.1 → 0.3 µm over three round trips (watch in long maps). Note X, Y from `ls6t instrument status`; `ls6t bridge call move_xy x_um=<X+10> y_um=<Y>`, then back | the stage moves 10 µm in +X (direction on the video); read back within 1 µm; note whether the call returned before the move ended (**Q4**) |
| T3.7 | `ls6t bridge call move motor=Z mode=value value=<Z+2>`, then back (maintainer checks clearance first) | Z moves 2 µm; read back |
| T3.8 | Stop during a move: `ls6t bridge call move_xy x_um=<X+1000> y_um=<Y> --timeout 60`, and the maintainer closes the ls6t window while it moves | the stage stops; the answer says `stopped_by: operator`; the bridge is halted. Resume (T2.3) and move back. |
| T3.9 | `ls6t bridge call safe` | answers ok; nothing moves |
| T3.10 | Polarisers: `instrument set` with `laser_pol` Vertical and `raman_pol` None (from the chat, or `ls6t bridge call move motor=LaserPol mode=label value=Vertical`), then back | each moves and reads back; `ls6t instrument status` shows `laser_pol` / `raman_pol` |
| T3.11 | Cameras (**Q44**: concluded 2026-11-20, ls6t does not switch cameras on this instrument). With the video stopped and the camera chosen in LabSpec: `ls6t instrument camera-on`. Then `camera-on --camera <the other one>`, and `camera-off` | The first starts the live video on LabSpec's camera (`video_started` 1). The other two are refused, nothing changes, the bridge keeps running, and the answers say what to do (`end-bridge`, change the video and camera in LabSpec, start the bridge). Then do that once: after `end-bridge` and the change by hand, the restarted bridge answers `ls6t instrument status` (if not: record it, restart LabSpec) |
| T3.12 | Stopping the video HORIBA's way (**Q44**; optional; maintainer ready to restart LabSpec; save work first). HORIBA's own service scripts (`SERVICE\StandardMapping XploRA.vbs`, `SWIFTMapping XploRA.vbs`) stop the video with `Video STOP_VIDEO`, then poll `GetMotorStatus("Video", id)` until 0, with no other video call in between; ls6t's earlier stop called `Video(GET_VIDEO_ID)` right after, and hung. With the video started by ls6t (`camera-on`): `ls6t profile set video.stop_from_script true`, then `ls6t instrument camera-off` once | It returns within 15 s with `live` 0 and `video_stop_status` 0, LabSpec's video is stopped, and the bridge answers `ls6t instrument status`. Record `video_stop_s`. If it hangs: restart LabSpec, set `video.stop_from_script` back to false, and record it (then no script way to stop the video is known) |
| T3.13 | After the bridge's own stop (**Q44**; maintainer ready to restart LabSpec; save work first). **Done 2026-11-20 on .26:** the stop returned (`video_stop_s` 3.38) but the next `status` hung inside LabSpec. From .27 the bridge, after its own stop, no longer asks LabSpec anything about the video (it knows it is off and which camera was selected) until it starts the video itself, and it waits at least 5 s after the stop before starting it. With `video.stop_from_script` true: `camera-on`, `camera-off`, then `ls6t instrument status` twice, then `camera-on` again | `status` answers at once with `live_video` 0 and `video_known_off` 1; the second `camera-on` starts the video after the 5 s wait, and `status` then shows `live_video` 1 with `video_known_off` 0. A stop by hand takes about 3–4 s to take effect (Nicola, 2026-11-20), like `video_stop_s` 3.38: the 5 s wait counts from when the stop is reported done |
| T3.14 | Switching cameras by a video restart (**Q44**; maintainer ready to restart LabSpec; save work first). **Passed before (2026-11-20, .28):** T3.12 (HORIBA's stop, 3.38 s) and T3.13 (no video query after the stop; stop then start, 3.09 s). With the live video on the internal camera (1): `ls6t profile set video.switch_camera true`, then `ls6t instrument camera-on --camera top` (camera 0 first: it was ignored before, even with the video stopped and after a LabSpec restart). Then `--camera internal`, then `--camera top` once more | Each switch: the video stops (`camera_restart` 1, `video_stop_s`), about 5 s pass, the video starts again, and the answer is `ok` with `camera_after` the one asked **and the live view shows that camera** (look). If camera 0 is ignored again, the answer says "LabSpec ignored the switch" with `camera_set_returned`: record it, and that camera stays a manual step (`video.switch_camera` back to false). No hang at any point; `status` answers between switches **2026-11-22 (.22.7):** clean HORIBA stop (3.37 s), the settle wait, `Video(11)` returned 0, restart: the view stayed on camera 0 (snapshot), 16.3 s. `Video(10 + id)` is what LabSpec 6.8.1's own help documents (HELP\\VBS\\Video.html: `long Video(long Mode)`, one argument, "10+CameraID : SET_ACTIVE_CAMERA"), so the code sent is the documented one. **T3.14b (to tell ls6t from LabSpec):** with the video stopped in LabSpec and no bridge running, run in LabSpec's own script window `r = LabSpec.Video(11)` then `MsgBox r & " / " & LabSpec.Video(4)`, then start the video by hand. Record what the message shows and which camera the view shows. Camera 1 shown: the difference is in how ls6t starts the video; still camera 0: LabSpec's script switch does not work on this build, and cameras stay a manual step (`video.switch_camera` false). |
| T3.15 | Motors and read-back. **Passed on .32/.33 (2026-11-20):** every move `ok` with the value asked; `settle_s` 0.0 throughout; whole moves: filter 1.0–1.1 s (36.6 s for the first move after the bridge started), laser 1.9–2.8 s, pinhole 2.1–5.7 s (the slowest), slit 1.7–2.9 s, grating 0.2–0.3 s (too short: the grating was seen moving for seconds in .29). From .34 each move also waits for all motors (`GetMotorStatus("", 0)`, the manual's WaitForAllMotors) and reports `move_call_s`, `wait_s`, `all_wait_s`. Repeat `ls6t instrument set --grating 600`, then `--grating 1800`, watching the grating | The answer's `seconds` now covers the grating's real motion (`all_wait_s` of several seconds), or, if it is still 0.2 s while the grating visibly moves, record `move_call_s` / `wait_s` / `all_wait_s`: then LabSpec reports no motor busy during a grating change. Also note the first move's time after each bridge start (36.6 s once): always slow, or once? |
| T3.16 | The grating (**Q45**). **Seen (2026-11-20):** every grating move answers in 0.02–0.05 s, blocking or not (`move_id` 0, no wait), while the laser's status wait works (1.7–1.9 s); LabSpec accepted 785 nm with 2400 gr/mm; its SpMax per grating reads as nm (600: 1138, 1200: 1112, 1800: 935, 2400: 690), which ls6t now uses (from .38 every profile gets it from the bridge's inventory). Start the bridge and wait until `ls6t status` shows it idle. Someone **at the instrument watches and listens to the turret** during: `ls6t instrument set --grating 600`; `--grating 2400`; `--laser-nm 785 --grating 1200` (from 2400: laser and grating together); `--laser-nm 637.75 --grating 1800`; then back to the start. After each grating change, take two dark acquisitions in a row, **with someone in the room**: a request with two `background` studies, `"background": {"name": "dark_a", "laser_off": "operator"}` (then `dark_b`), `"setup": {..., "filter": "0.1%"}` (required while the profile is not reviewed), `"acquire": {"center_cm1": 520, "exposure_s": 1}`. Nothing in ls6t keeps the laser off here (LabSpec opens its shutter as for any acquisition): the person at the instrument physically blocks the beam (or switches the laser off at its key) before pressing OK; LabSpec's prompt says so and must not be confirmed from a remote session. Compare their `seconds.acquire` | **First of all, the slow first move** (24.1 s on the grating with the bridge idle, 36.6 s on the filter another time; both right after a bridge restart, LabSpec perhaps not restarted). Two cases, each answer's `bridge_move_n` saying which move of the bridge it was: (a) **restart only the bridge** (`ls6t instrument end-bridge`, then start `ls6t_bridge.vbs`; LabSpec keeps running; can be done remotely) and make a grating move first: slow again means the delay belongs to the bridge's first command (then ls6t can warm it up at start); fast means it belongs to LabSpec's own start; (b) **restart LabSpec and the bridge**, grating first, someone watching whether the turret turns then, and again on the later changes (they answer in 0.06–0.09 s; `all_wait_s` is 0.0 throughout). A third case was 31.7 s (.40, grating 1200 → 600, all of it in `wait_s`). From .42 the bridge asks each motor's status once at start, nothing moving (`ping`'s `warmup`, and `%USERPROFILE%\ls6t\warmup.txt`, give each motor's time): if the first-command delay is spent there, the first real move is fast; watch the turret during the bridge's start too. **Seen so far:** Grating 36.0 s (.42) and 50.9 s (.43), both bridge restarts in a running Windows session; 0 s for every motor after a fresh Windows login with LabSpec and the bridge started fresh (2026-11-21). So it is not simply LabSpec's or the bridge's start: from 2026.11.21.1 `ls6t instrument status` logs each bridge session's warm-up once to `%USERPROFILE%\ls6t\warmup_log.jsonl`, with LabSpec's start time and how long it has run, Windows' uptime, where the grating was, and ls6t's last grating `set`; note anything else done in LabSpec since its start (gratings changed by hand, acquisitions). **Later (2026-11-21):** 0 s on a fresh login and after two clean End-bridge restarts (one after two grating moves); all five slow cases came right after deploying a new version, when the old bridge had been left running or ended abruptly. From 2026.11.21.1 the installer (and `ls6t bridge install`) ends a running bridge cleanly before replacing the scripts, logs it in `deploy_log.jsonl`, and the warm-up log names the last deploy: at the next deploy, record the warm-up and whether the old bridge was ended cleanly. **2026.11.21.1:** the deploy found the bridge already stopped, yet the grating's warm-up took 29.5 s with LabSpec running for only 1.2 min: more likely the grating still initialising for about a minute after LabSpec starts; `labspec_running_min` in the warm-up log will tell over the next sessions. **Likely, under observation:** the grating initialising for about a minute after LabSpec starts (LabSpec running 1.2 min: Grating 29.5 s, bridge 43 s after LabSpec; LabSpec running 8.3 min: 0 s) — but on 2026-11-22 three bridges started 18 s, 24 s and 26 s after LabSpec (20:29:56 → 20:30:20 the last) all had 0 s grating warm-ups, so the LabSpec-start explanation is weak; the two slow cases of Nov 20–21 remain unexplained: keep recording `warmup_log.jsonl` (LabSpec's start, uptime, last grating move, last deploy) for each session. Whatever the cause, the warm-up absorbs it. The installer ends a running bridge cleanly (`bridge_was: idle`, `ended: cleanly`); from .13 its log also says "no running bridge found" (with the last heartbeat's state and age) when there was none. The 20:23 install of .12 found the bridge already stopped (`bridge_was: stopped`, `ended: not running`): `deploy_log.jsonl` shows the same for `.22.4` (16:27:14) and `.22.6` (16:49:41): `bridge_was: stopped`, `ended: not running`. A running bridge rewrites its heartbeat every second, and only the bridge's own exit (End bridge, the HALTED window closed, `quit`) writes `stopped`, so these were bridges already ended, not misread; .13's `heartbeat_age_s` and `bridge_version` will confirm at the next install with a bridge running. For each change: did the turret move when `set` answered, or only at the first acquisition (then the first `seconds.acquire` is longer than the second by the turret's time)? The answers carry `reach` (2400 with 785 nm: no Raman shift; 1800 with 785 nm: up to about 2040 cm-1) and the note that LabSpec reports a grating at once. A study asking beyond SpMax is refused at validation. Also record `timing.status_before_s` of the first `set` after a bridge start. **Seen 2026-11-23 (.22.15, Si, 637.75 nm, x100, 1 %):** ls6t set 1800 (LabSpec said 1800): data "-1016 to -974 nm" (42 nm), header `Spectro (nm)` = -996.66; set 1200 (LabSpec and its UI said 1200): 646-728 nm (82 nm), centred near 687 instead of 659.66, Si at 523.4 cm-1; two studies with 1200 centred at 520 and 1500 cm-1 gave the same window (-574 to 1448 cm-1): **the centre was never sent** (LabSpec's Acq with From = To does not move the spectrometer). The 1200's window measured 85-87 nm here (the profile said 39: its window_nm values were about 2.2x too small), so the 82 nm was the 1200's own width and the main fault was the centre; the 1800's 42 nm with impossible wavelengths remains to be explained. From 2026.11.23.1 every single-window acquisition first waits for all motors, moves `Spectro` to the centre (as HORIBA's scripts do) and reads it back (`spectro_nm`); the first spectrum of each study is checked (`checks.grating_window`): impossible wavelengths, a centre more than a quarter window off, the spectrometer not where it was sent, or a width 20 % off the one measured before with that grating and laser (`grating_windows.json`) stop the study; a profile `window_nm` off by more than 20 % is a warning with the value to set. Check: the 520 / 1500 cm-1 pair now gives two different windows; 1200 → 1800 → 1200 each centred, `spectro_nm` = the centre asked; set `gratings.<g>.window_nm` from the warnings. Fill in the manual row: 1200 set and acquired in LabSpec by hand (bridge ended): its X range. **Item 4 (2026-11-23):** grating changed by hand with the bridge ended (1200 → other → 1200), bridge restarted: LabSpec's status bar showed "ls6t discovery running" (the start-up inventory) and the next `status` stayed inside LabSpec for over 76 s (longest so far; before: 51 s); LabSpec had to be restarted. While the bridge runs, LabSpec's own grating control is blocked. Record: which controls are blocked (grating only, or laser, filter, slit, hole too); whether it is blocked from the bridge's start or only after its first grating command; whether End bridge releases it at once; and whether changing the grating in LabSpec with the bridge ended, then starting the bridge, gives a fast or slow first grating status (the bridge's presence may change how LabSpec carries out script-requested grating changes) |
| T3.17 | **Which Z direction approaches the objective** (no laser; **x10 or x5**, **empty holder**, the maintainer at the instrument). A few µm cannot be seen against a ~10 mm gap, so the move is large and made by the maintainer in LabSpec, not by ls6t (no exception to ls6t's limits). Note Z from `ls6t instrument status`. In LabSpec, the maintainer raises the stage **toward the objective by about 1 mm** (its Z control or the joystick), watching the gap shrink, then `ls6t instrument status` again, then brings the stage back. If Z increased while the stage approached: `ls6t profile set stage.z_into_sample '"+"'`; if it decreased: `'"-"'` (upright microscope: deeper into the sample = toward the objective). HORIBA's documentation of the stage's Z sign, if found, is an equal answer: record which was used | Z changed by about 1000 µm (record the sign and the value) while the gap visibly shrank. If the direction is not unmistakable, **leave it unset**: unset keeps the cautious limit both ways (x100: about 21 µm), a confident wrong answer would turn a retract into an approach |

Also record the times of T3.3, T3.4 and T3.6. They replace the defaults in the profile's `timing` (`grating_s`,
`laser_s`, `xy_um_per_s`, `xy_settle_s`), which feed the time estimates.

## T4. Silicon and the spectrometer (LASER)

The maintainer places the Si wafer, chooses the objective (x100), focuses by hand, and sets a low filter.

**T4.1 One spectrum by hand.**

```powershell
ls6t bridge call acquire --laser exposure_s=1 accumulations=1 mode=10 from_nm=547.16 to_nm=547.16 out=$HOME\ls6t\t41.txt
```

(547.16 nm is 520.7 cm⁻¹ at 532 nm; for another laser use `ls6t inputs` on a calibration study to get the value.)

Pass:
- a spectrum appears in LabSpec;
- the answer has an `id`, `points`, `max_counts`, `x_unit`, `y_unit`;
- the file has `# x_unit=` (**Q6**: record nm or 1/cm), the `# acq.*` lines (**Q6**: is the first a header?), and
  rows of numbers.

**T4.0 Dry run (no laser; possible before the profile review).** `get_inputs` (or `ls6t inputs`) on the Si
calibration study, with the instrument already on some of its settings. **Seen on 2026.11.22.3:** steps, `review_note`,
`center_nm` / `window_nm` and the per-motor estimate right; settings already in place were still listed and charged
(the objective 20 s). Pass from 2026.11.22.4: `state_read` true; the steps already in place carry `already_set` and
cost 0 in `setup_parts`; at run time they are not sent (`checks.setup.moves[].already_set`), and the operator is never
asked to change to the lens already mounted.

**T4.2 The calibration study, through the runner.**

```powershell
ls6t validate si.json                        # si.json: only "si_check" from examples\studies.json, with the
ls6t run si.json --wait 0                    #   maintainer's laser, grating, filter and objective
ls6t status <request folder> --wait 50
```

Pass:
- LabSpec asks for confirmation before the exposure, naming the sample, laser, filter and objective, and the
  maintainer answers Yes;
- the result has `reliable: true`, `calibration.measured_cm1` within 1 cm⁻¹ of 520.7 (record it),
  `checks.setup.pass` and `checks.detector_temp_c.pass` true;
- `calibration.resolution`: the line's width for this laser, grating and slit (the first record is the baseline;
  compare with the instrument's specification if the maintainer has it: `ls6t profile set
  performance.resolution_cm1 '{"1800": <cm-1>}'`);
- the `.l6s` file in `spectra\` opens in LabSpec (**Q9**).
- with `sample.notes` given, LabSpec's Remark of that file reads `ls6t <seq>: <notes>` (the dry run cannot show it).

**T4.3 Auto-exposure (Q6).** A spectrum study with `"exposure_s": "auto"` and
`"auto_exposure": {"target_counts": 30000, "min_s": 0.1, "max_s": 5}`. Pass: `max_counts` near the target; the
exposure LabSpec chose appears in the file's `acq.*` lines (record the line's name).

**T4.4 Saturation is caught.** With the maintainer's agreement, a filter and exposure that saturate silicon (or
`max_counts` near 65 535). Pass: `reliable: false`, *saturated* in `unreliable_because`.

**T4.5 Cancels (Q7).**

- A spectrum study with a 30 s exposure; after 5 s, `ls6t cancel <request>`. Pass: the acquisition stops in LabSpec
  within about 1 s; the request ends `cancelled`.
- The same, but the maintainer cancels the acquisition in LabSpec. Pass: the request ends `cancelled` with
  `stopped_by: operator`; the bridge is halted. Resume.
- Right after, a normal 1 s spectrum. Pass: it is measured normally (no stale "cancelled" state).

**T4.6 Extended range on polystyrene.** The maintainer places polystyrene and focuses. Run a spectrum study:
`range_cm1` [200, 3200], `spike` double, `accumulations` 2, with `expect` on 1001.4, 1031.8, 1602.3 and 3054.3 cm⁻¹
(tol 2) via `peaks_by_name`. Pass: LabSpec stitches several windows; `checks.range.pass`; the expected values pass.
Compare with the maintainer's own spectrum of the same sample.

**T4.7 The spectral zero (Q27).** On silicon:

1. `ls6t instrument calibration`: LabSpec's calibration values per grating. Keep the output.
2. `{"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": {...}}}`. Record `offset_cm1`.
3. Run it with `"calibration": {"adjust": "labspec"}` (set `tol_cm1` to 0.2 if the offset is already small, to
   exercise the loop). Follow LabSpec's message in *Maintenance > Instrument calibration*. Pass: the next check
   passes; `calibration.labspec_parameters.changed` names the value the dialog changed (**Q27**: record which is the
   zero and which the coefficient).
4. With polystyrene: `"calibration": {"reference": "polystyrene"}` on the 600 gr/mm grating. Pass: at least 4 lines
   found; `correction.b` within 0.001 of 1 after LabSpec is calibrated.
5. `"adjust": "ls6t"` once, then a spectrum study. Pass: its warnings say the correction is applied, and the Si line
   is at 520.7 ± 0.3. Clear it with `ls6t references clear correction` afterwards, unless the maintainer wants to
   keep it.

**T4.8 White-light background (Q28).** With the maintainer: the white light on as for routine measurements.

1. Run `{"bg": {"kind": "background", "sample": {"name": "lamp"}, "setup": {... grating 600 ...}, "acquire":
   {"range_cm1": [200, 1800], "exposure_s": 1, "accumulations": 2}, "background": {"name": "lamp_x100"}}}`
   (`laser_off` defaults to `shutter` with a shutter motor, else `operator`: block the laser when asked). Pass:
   no warning about narrow lines; the laser shutter mode is back to normal afterwards.
2. On silicon, with the white light still on: the same spectrum with and without
   `"subtract_background": "lamp_x100"`. Pass: the broad lamp background is gone in the second, and the Si line is
   unchanged.
3. Note the white light's own stability: repeat the background 10 minutes later and compare (`max_cps`).

**T4.9 LabSpec's AutoCalibration from ls6t (Q42; maintainer).** ls6t checks the calibration; LabSpec's own
AutoCalibration should correct it.

1. `ls6t instrument autocal-info`: the AutoCalibration script(s) on this PC and, from their text, whether the bridge
   can run them (`runs_inside_the_bridge`) and whether they ask the operator (`asks_the_operator`). Nothing runs.
   Send the output to the ls6t maintainer. If no script is found, stop here: correction stays guided (T4.7).
2. Run LabSpec's AutoCalibration once by hand from LabSpec's menu, to see what it needs (sample, lamp) and does.
3. If step 1 says it can run inside the bridge: `ls6t profile set calibration.autocal '{"enabled": true, "script":
   "<path>", "timeout_s": 1800, "sample": "<what it needs>"}'`, then `ls6t instrument autocal-run`. Pass: LabSpec asks
   first; the script runs (its dialogs, if any, appear); `changed` lists LabSpec's calibration values it changed; the
   bridge answers again afterwards. If LabSpec hangs or shows an error, close it, restart the bridge, set
   `calibration.autocal.enabled` back to false and note what happened.
4. A `calibration` study with `"adjust": "auto"` on the Si wafer, after shifting the zero slightly by hand in
   LabSpec. Pass: the check fails, AutoCalibration runs (after Yes), the second check passes.

**T4.10 The filters' real transmission.** On the Si wafer, focused, x100: a `filters` study
(`{"kind": "filters", "sample": {"name": "Si"}, "setup": {"laser_nm": 532, "grating": 1800, "objective": "x100"}}`),
with the per-laser caps agreed in T1.2. Pass: every filter within the caps measured, the reference not saturated;
note filters flagged off nominal. With the maintainer, decide whether to apply `profile_command` (the caps then act
on the real values). Repeat per laser (transmission can differ with wavelength).

**T4.11 The edge filter's cut-off.** White light on, a matt white sample or the Si wafer: a `cutoff` study per laser
(`{"kind": "cutoff", "sample": {"name": "white light"}, "setup": {"laser_nm": 532, "grating": 1800}}`). Pass: the
plot shows the step; `usable_from_cm1` is plausible for the edge filter (about 50–100 cm⁻¹). Then a spectrum study
with `range_cm1` starting below it is warned at validation.

## T5. Autofocus (LASER, Q8)

Once `stage.z_into_sample` is set, LabSpec's autofocus inside studies is also guarded (stopped at the focus plus the objective's toward limit, minus the overshoot margin of T6.8): with the x100 it is refused until that margin is measured and leaves room. Run this with the x10 first.

On silicon, defocus by about 10 µm by hand. Run a calibration study with `"focus": {"autofocus": "laser"}`. Repeat
with `video` and `raman` if the instrument has them.

Pass:
- the answer's `z_um` changes, and LabSpec's Z shows the new focus after the study (not restored);
- the Si signal is close to the hand-focused one;
- the calibration spectrum is the real one: `max_counts` is plausible, and the file's id differs from any autofocus
  spectrum LabSpec may have shown.

Record whether autofocus left a spectrum in LabSpec.


## T5b. Video and the beam (T5b.1, T5b.2, T5b.6 no laser; T5b.3–T5b.5, T5b.7 LASER)

**T5b.1 A frame (Q13, Q41).** Start the live video in LabSpec (or `ls6t instrument camera-on --camera top`), with the
Si wafer or the contrast sample in focus, then run `ls6t instrument snapshot`. Pass: `ok`; `frame.marked.png` (ls6t's plot; LabSpec's own image stays `frame.png` / `.jpg`) shows the same
view as LabSpec's video window; `um_axes` is true and `field_um` matches the objective's field of view
(`objectives_info` in `ls6t format`, from `OLConfig.ini`: **Q41**). Record which formats appear under `native`, `channels` and `pixels` (2026-11-20: the top camera gave 888 x 606 x 3, below its sensor's resolution: record the internal camera's too, and whether LabSpec's camera settings (binning, resolution) or its video window size change it). `get_image` shows any of these files, reduced when over about 1 MB.
Repeat with the live video stopped: the bridge starts it and takes the frame, and leaves the video on (`video_left_on`: ls6t never stops it). With `video.camera` set to another camera than LabSpec's, the snapshot is refused (no frame from the wrong camera).

**T5b.2 Orientation, colours and scale (Q14).** 2026-11-22 (.22.7, Si, x10, top camera): ls6t's frame had its rows
reversed against LabSpec's image (two bright particles at y ~ 105 px in LabSpec appeared at y ~ 490-505 in ls6t's
plot), red and blue swapped, and a scale of LabSpec's 1 "µm" per pixel; +100 µm in X moved the features -101 px,
+100 µm in Y +102 px (in the flipped plot). Since .22.8 ls6t compares its frame with LabSpec's own saved JPEG and
corrects the flip and the colours by itself (`orientation` in the answer: `flip_y`, `flip_x`, `swap_rb`, `score`),
remembering them per camera; the scale comes from the objective's field of view (`axes_from`) until measured.

- Take a snapshot with the same view as on 2026-11-22. Pass: `frame.marked.png` looks like LabSpec's image (same
  way up, same colours); `orientation` shows `flip_y` true, `swap_rb` true and a `score` above 0.8; `axes_from` names
  the objective's field; the laser marker is inside the image (at its centre, the stage position). With the x5 the field is about 1713 x 1170 µm, not 887 x 605 (2026-11-22). A black frame (too little light) gives `frame_dark`, a warning and no features (2026-11-22: five 'features' of strength 1e9).
- `ls6t instrument video-scale` (x10, no laser, a lit view with visible structure; the stage moves 0.4 x the field's
  smaller side, ~240 µm, in +X and +Y and comes back; `--um 100` for 100 µm; over half the field's width or height is
  refused). Pass: `ok`; `um_per_px_x` and `um_per_px_y` about 0.97-0.99 in size
  (field / pixels: 862.89 / 888 = 0.972; measured by hand 2026-11-22: x10 0.98-0.99, x100 0.096); `cross_talk_px` small; `note` names the axis directions; the stage back
  where it started. Record the values and the note.
- Repeat the 2026-11-22 move test (snapshot, `move_xy` +100 µm in X, snapshot; then Y). Pass: `axes_from` says
  "measured"; the features move by about -100 µm in X and -100 µm in Y in ls6t's plot, in µm (the stage moved
  under the view: the features go the other way), and the same way as in LabSpec's video window.
- Repeat `video-scale` for the x100 (default move ~23 µm; `step_from` says so) and the x5; each is stored separately.
  **Seen on .22.15 (2026-11-23):** x100 17 µm: 0.0949 / 0.0990 µm/px, cross-talk 2.34 px (a 4 % X / Y mismatch); 35 µm:
  X 0.0966, Y failed (features left the 58 µm-tall field); 25 µm: 0.0963 / 0.0968, 0.91 px. From .22.16: default 0.4 x
  the smaller side (23 µm), the Y move checked against the height, and `aspect` / `rotation_deg` compared with the
  same camera's other objectives (1 %, 0.5 degrees): a disagreeing result is stored with a warning. Check: x100
  default gives `aspect` within 1 % of the x10's and no warning; `--um 35` is refused before the stage moves.
- Orientation from a black frame: with the x5 and the light off (black frame), take a snapshot. Pass: `orientation`
  says "remembered for this camera (the frame is dark or flat)" and keeps `flip_y` / `swap_rb` from the lit view;
  a grey Si view keeps the stored colour swap (`swap_from` "kept").
- The marker: until a `spot` study measured the beam for this objective and laser, `marker_measured` is false,
  `marker_source` starts with "ASSUMED" and the plot labels the marker "assumed".
- **Seen on .22.15 (2026-11-23, Si, x10, top camera):** orientation learnt from the first snapshot (`flip_y` true,
  score 0.996, no red/blue swap); `video-scale` x10 0.977 / 0.982 µm/px (X / Y), cross-talk 0.05 px, "image up is
  stage -Y", stored as `x10_VIS@0`; repeated in bright field 0.981 / 0.979, cross-talk 0.84 px: repeatable to about
  0.4 % (features found as dark spots in bright field). Move test with the measured axes: features keep their stage
  coordinates within about 0.6 µm over +100 µm moves in X and in Y. Without a measurement the fallback (OLConfig
  field) had Y reversed (a particle at +153 µm instead of -155): from .22.16 the directions come from a measurement
  on the same camera (`directions_from` "learned from x10_VIS@0"); with none, the snapshot warns that they are
  assumed and `move.to_um` is refused. Check: with the x100 (no video-scale for it yet), a snapshot says
  `directions_from` "learned from x10_VIS@0 (same camera)" and the particle's Y has the right sign.
- **Illumination (2026-11-23):** twice, after a video restart (`camera_off` / `camera_on`) or an objective change,
  LabSpec's indicator showed bright field while the light was dark field; toggling dark → bright in LabSpec fixed it.
  From .22.16 a snapshot without the laser compares its mean grey level with the last one for the same objective and
  camera (`brightness`) and warns when it is 2.5x darker or brighter. Check: after such a restart, a snapshot in the
  wrong mode carries the warning. Also record which motor LabSpec uses for the illumination (the profile notes list
  `IlluminatorV` among "other motors in LabSpec's configuration" when present) and its positions: if it is the
  bright / dark field selector, ls6t could set it again after restarting the video. **From .22.16:** discovery and
  OLConfig.ini set `motors.illumination` to `IlluminatorV` (Dark field, Bright field) and `motors.illumination_light`
  to `uLight` (Off, BrightField, DarkField1-4, Full; read only); an existing profile needs `ls6t profile set
  motors.illumination IlluminatorV` and `ls6t profile set motors.illumination_light uLight`. Snapshots carry both
  (`illumination`) and warn when they disagree. `ls6t instrument set --illumination bright` (or dark) moves
  IlluminatorV, through the other position when LabSpec already shows the one asked; after an objective change
  (`set`) or a video restart (`camera_on`), ls6t sets again what LabSpec showed before. Check: after the next
  restart in dark field, the answer's `illumination` (`why`: "set again ...") and the view agree, and a snapshot
  shows no disagreement warning. `LaserSwitch` is refused by the bridge (`ls6t bridge call move motor=LaserSwitch
  mode=label value=Off` answers "ls6t never moves LaserSwitch").
- If the colours or the way up still differ (no JPEG saved, or a view without structure: `score` low), set
  `video.packing` to `rgb`, or keep `raw` (the `.video.txt` file) for analysis.

**T5b.2b From the maintainer's session (2026-11-23, .22.16, Codex).** Internal camera (1), x10: a 240 µm move gave X -213 px (1.126 µm/px) and lost Y (out of view); with the live video started by ls6t the image froze and no shift was found; LabSpec fell back to dark field after video starts; top camera = 0 confirmed, x10@0 0.977 / 0.982 µm/px with 50 µm. From 2026.11.23.3 the bridge keeps a video it started alive (GET_VIDEO_ID asked all along) and sets the illumination back after a video start. Check: `camera-on`, then watch LabSpec's live view for a minute (it must keep moving); `video-scale` with the live video on finds the shift; after `camera-on` and a snapshot, `illumination` still says bright field (`illumination_restored` when the bridge had to set it back). A spectrum over an extended range (532 nm, 100-1000 cm-1, 5 s) must not be called stuck while LabSpec collects (the heartbeat shows `blocking: acquire`).

**T4.x Calibration check quality (2026-11-23).** A check at 1 %, 1 s fitted a weak Si line at the edge of its window (525.12 cm-1) and asked for a -4.42 cm-1 correction; AutoCalibration then gave 520.64 and the check at 10 % passed (520.33). From 2026.11.23.5 a weak (`snr` below `checks.min_snr`) or edge-fitted line gives no correction request (`calibration.trusted` false, `not_trusted_because`). Check: a calibration at 0.1 % / 1 s ends without a notice and says why. To make the notice give Offset-shift steps: with the Si line well measured, move LabSpec's Offset shift by a known number of steps, measure again, and set `ls6t profile set calibration.offset_step_cm1 '{"1200@532": <cm-1 per step>}'` (one entry per grating@laser). Also find LabSpec's AutoCalibration script for `autocal_info` (it found none; LabSpec's menu runs it).

**T3.16 results (2026-11-23, .23.3).** Centre fix confirmed: 1200 @ 637.75 nm, 520 / 1500 cm-1 -> Spectro 659.6251 / 659.6308 and 705.2124 / 705.2123 nm, windows 87.37 / 85.36 nm; 1800 46.66, 1200 87.37, 600 197.79 nm, all centred. Set the profile: `ls6t profile set gratings.600.window_nm 197.8`, `... 1200 ... 87.4`, `... 1800 ... 46.7`. 2400 @ 637.75: LabSpec's modal *ICS correction file does not exist ... Error Code=2070* on every acquisition: `ls6t profile set ics.off_for '["2400@637.75"]'` (add other grating@laser pairs that show it), then repeat the 2400 spectrum (no dialog; the result warns that ICS is off) and note its width. Which it is (the file missing for 2400 with 637.75 nm, or the window going beyond the range the ICS file covers): with the bridge ended, in LabSpec by hand, ICS on, take 2400 @ 637.75 nm spectra centred at about 150 cm-1 and at about 900 cm-1 (both well inside the grating's reach, up to 1187 cm-1 = SpMax 690 nm), and one 2400 @ 532 nm; note which show Error 2070. Also look for the ICS files in LabSpec's folder (names with the grating and laser) and note which pairs exist. Still open: the manual row (1200 by hand, bridge ended: its X range), the long acquisition, video / illumination, Remark, control lock.

**ICS and HORIBA's QC (2026-11-23, .23.7).** `ls6t profile ics`: `responses_found` lists LabSpec's ICS corrections and `ics_on` each grating@laser; check that it matches LabSpec (785: none). Then a 2400 @ 637.75 nm spectrum runs with no Error 2070 dialog. `ls6t profile qc` (or with the QC folder's path; from .23.9 it looks on every fixed drive and gives `from`, `found` and `chosen`: on this PC `D:\Horiba Jobin Yvon\1-QC\QC Auto`): `cutoff_cm1` per laser, and `ls6t references` shows `horiba_qc` with the laser powers next to the profile's power_mw (QC at the sample: 85 / 40 / 50 mW; installation, no objective: 96.6 / 33.8 / 86.6 mW): the maintainer decides whether power_mw changes.

**T3.17 Where an acquisition's time goes (2026-11-23, .23.10).** Same spot, Si, 637.75 nm, 1 % filter: (a) 1 × 1 s with nothing to move; (b) 2 × 2 s after a grating change; (c) (a) again right after (b). For each, note `timing.total_s`, `operator_s`, `overhead_s`, `acquire_phases_s`, `acquire_handoff_s`, `bridge_commands` and `note`, and whether (c) says `spectro_move_skipped`. Pass: the phases add up to the acquire command's time within a second; then decide what to change (the large ones so far: 23 s for (a), 42–64 s for 2 × 2 s; 10–15 s on .22.15). If `t_motors_before_s` is large after a grating change, the turret was still turning when the 0.31 s grating move returned.

**T4.4c Calibration per grating (2026-11-23, .23.10).** Si calibration check with 1200 and then 2400 at 637.75 nm (adjust none). Pass: each result's `calibration.applies_to` names its grating; a 2400 spectrum afterwards has `checks.zero.offset_cm1` near +3.1 and a warning; a 1800 spectrum warns that only 1200 and 2400 were checked; the guided prompt (adjust labspec) names the grating and laser.

**T3.18 LabSpec's data info (.23.10).** Open a spectrum's `.l6s` in LabSpec: Title is the study's name, Project `ls6t <request folder>`, Site `sample.site` or empty, Sample and Remark as before.

**T3.20 Filters study and laser_on's question (.23.16).** (a) On .23.15 a `filters` study read its second step
about 3 × low. From .23.16 each step records `exposure_used_s` (LabSpec's), `max_counts`, `saturated`,
`filter_read_back` and its move's timing, and the reference filter is measured again at the end
(`checks.reference_repeat.ratio_to_first`; off by more than 10 %: drift, unreliable). Repeat it on Si (637.75 nm,
x100) and send the steps. The hardware is already excluded: plain spectra at the same spot, same session, gave
1376 cps at 10 % and 137–159 cps at 1 % (the 1 % filter close to nominal), with the exposures confirmed in LabSpec's
headers. So a low second step with `ratio_to_first` near 1 and the right `filter_read_back` points at the study's
own second step (its window, its measure, or the timing around the filter move: compare its `move` timing and
`exposure_used_s` with the plain spectrum's), not at the filter; a drifting reference points at focus or power. (b) `laser_on` asks every time (the
operator's Yes in LabSpec); a repeated laser_on that took 0.2 s in all cannot have been answered by a person. From
.23.16 the answer is timed (`confirm_s`) and a Yes faster than 0.4 s is not taken: repeat `ls6t instrument
laser-on --seconds 5` twice and note `confirm_s` and whether LabSpec showed the question both times.
**(b) passed on .23.18:** two laser_on in a row both waited for the operator (`confirm_s` 2.54 and 3.16 s); the earlier
"0.2 s" was the `seconds` field, which left the confirmation out. From .23.19 `seconds` is the whole (confirmation
included), with `confirm_s`, `laser_call_s` and `open_for_s` named.

**T3.21 Only the first spectrum of a request is right (2026-11-23, serious).** On .23.18, five spectra at the same
spot in one request (Si, 637.75 nm, 2400, x100): point 0 Si 520.29 cm⁻¹, FWHM 3.94; points 1–4 511.94–512.43, FWHM
9.16–10.59 (at 10 %: 511.9–514.8, FWHM 15–22), area kept (1588–1753 vs 1582); the same at 1 % and 10 % (not
heating); LabSpec's axis (252.758–751.508) and every `acq.*` header line identical, only the id differs; the same
with `motors.laser_shutter` null and the bridge restarted (`run_20261123_202103_42a7c8`). A shift with broadening
and the area kept means the line moved during the exposure: something in the optical path or the spectrometer moving
while LabSpec acquires. The `filters` study's falling signal is probably the same effect. Two changes made the next
acquisition start sooner or touch LabSpec between spectra: the flush shortened in .23.15 (from about 1.6 s to 0.05 s
between one spectrum and the next) and, since .23.17, AutoExposure read and switched through the Template before and
after each spectrum. **From .23.20** each spectrum has `optics_check` (time since the previous spectrum, any pause,
the spectrometer and grating read before Acq, right after it returns and after the data, and whether a motor is
busy), repeated spectra at the same spot are compared with the first (`checks.same_spot`; more than 2 cm⁻¹ or 1.5 ×
as wide: unreliable, a warning for a series), and `timing.acquire_settle_s` pauses after each spectrum. Steps, five
points at the same spot each: (a) as is, and note `optics_check` per point; (b) `ls6t profile set
timing.acquire_settle_s 2`, then 5; (c) `acquire_settle_s` 0 with `autoexposure.switch_off false` and AE off by hand
(no Template call between spectra); (d) five acquisitions by hand in LabSpec, back to back. Whichever brings points
1–4 back to 520 cm⁻¹ / FWHM ≈ 4 names the cause.
**Found (2026-11-23): the 637.75 nm laser changes mode after the first spectrum of a request.** With the laser line
and Si in one 1200 window (`run_20261123_203003_bfdb68`): laser line −4.6 → −11.9, −12.7, −12.1, −12.4 cm⁻¹ and Si
520.2 → 512.6, 512.2, 512.2, 512.3, their difference constant (524.3–524.8): the laser moved by about 0.35 nm, not
the spectrometer. LabSpec's `.l6s` of point 1 shows both positions (a hop during the exposure). A 30 s warm-up
acquisition first (`run_20261123_203245_1701fa`) was itself clean (laser −4.0, Si 520.7) and the five 2 s points
after it all shifted, with both lines about twice as broad: not time or heat, but something between the first and
later acquisitions of a request, undone at the start of the next request.
**What the bridge does, at the start of a request and between spectra:**
- Start of a request (once): `ping`; `status` (below); a status-bar message; then per study the set-up: `status`,
  `labels` of the motors to set, a `move` only for what is not already set (laser, grating, filter, objective, hole,
  slit, polarisers, illumination), `status` again, and the operator's laser Yes (a LabSpec Yes/No dialog, first
  laser study only).
- `status` reads: SystemName; the detector temperature (`Acq` with mode 4, GET_TEMPERATURE); the Laser, Grating,
  Hole, Slit, X, Y, Z values and the Filter, Objective labels (GetMotorPosition); LabSpecExposureTime,
  LabSpecAccumulation, ShutterMode (GetValue); AutoExposure (Template GET); the laser shutter's label (when set);
  LabSpec's last error (GetLastErrorInfo); the polarisers and illumination labels (when set); the live video's id
  and camera (Video GET_VIDEO_ID, GET_ACTIVE_CAMERA).
- Each spectrum (point 0 included): AutoExposure read (Template GET) and, when on, switched off (SET 0); the flush
  (GetAcqID until -1 or three equal answers); the spectrometer read (moved only if more than 0.01 nm off); `Acq`;
  GetAcqID until the data; the spectrum read (GetValue XYData, AxisType, AxisUnits, AcqInfo); PutDataInfo Sample,
  Remark, Title, Project, Site; Save of the .l6s; the detector temperature (`Acq` mode 4); AutoExposure put back
  (Template SET) when it was switched. Nothing touches the laser motor, the shutter or ShutterMode for an ordinary
  spectrum.
- Between spectra of the same spot: nothing else (no move, no status, no dialog).
So the differences are the `status` reads (laser motor value, video queries, last error, exposure/accumulation
values), the set-up moves (only when something changes), and the operator's dialog. **Test (from .23.21):**
`ls6t profile set timing.status_before_each true` runs the same `status` before every spectrum after the first. If
points 1–4 then stay at 520 / −4.6, the reset is in one of those reads, and the maintainer bisects them; if not, the
dialog or a set-up move is the reset (try a request whose set-up has nothing to move).
**Safeguards from .23.21:** with the laser line in the window it is fitted in every spectrum (`spectra[].laser_line`,
`checks.laser_line` with any jump over 1.5 cm⁻¹: unreliable); `acquire.laser_reference: "correct"` shifts each
spectrum by the laser line's move (relative to the line recorded at the last calibration within tolerance, else the
first spectrum); `acquire.warmup_s` / `lasers.<k>.warmup_s` puts a discarded acquisition first (it did not help
here); `lasers.<k>.mode_hops: true` makes calibrations say they hold for the mode they were measured in. The
spectrometer move to a new 1200 centre took 37 s (`t_spectro_call_s`).
**532 nm is stable (2026-11-23, `run_20261123_203455_558e8e`, same five-point test, 1200, 1 %, 2 s):** laser line
−8.16 to −8.58 cm⁻¹, Si 518.69–518.85, FWHM 8.5–8.8, heights 879–905. The acquisition sequence is fine; the 637.75 nm
source changes between acquisitions (hardware: raise with HORIBA). **From .23.22:** `ls6t profile set
lasers.637.75.unstable_multi true` refuses studies of several spectra with that laser (map, series, depth, filters,
a spectrum with several points) unless `acquire.laser_reference: "correct"` with the laser line in the window
(`"warn"`: a warning instead), saying why; with `correct` and no laser line found, the study says so (unreliable);
each peak also gives `from_laser_line_cm1`, its shift from that spectrum's own laser line.
`profile set` and laser names (.23.23): `lasers.637.75.…` finds the laser by its wavelength whatever its key in the
profile ('638' with nm 637.75 on the XploRA, which is why `lasers.637.75.power_mw` answered "no '637'"); a key can
also be quoted (`lasers."638".unstable_multi`), and an unknown key lists the ones there. Hand-editing `profile.json`
(`LS6T_HOME\profile.json`, the laser's entry under "lasers") remains an alternative; the profile stays reviewed.
The laser-line test already confirmed the hop (the laser line moved with Si; Si − laser constant at 524.3–524.8).
**`timing.status_before_each` is now also a possible workaround:** if a `status` read resets the 637.75 nm laser,
running it before every spectrum would make multi-spectrum studies usable with it. Report the five-point result with
it on; if all five agree, ls6t will accept it in place of `laser_reference: "correct"` for `unstable_multi` lasers.
**Result (2026-11-23):** `status_before_each` did not help. **A pause did:** with `timing.acquire_settle_s` 5
(`run_20261123_211608_3c417c`, 1200, 1 %, 2 s, `settled_s` ≈ 4.7) the five points stayed together: laser line −5.27
to −4.51 cm⁻¹, Si 519.78–520.14, FWHM 6.3–6.8, heights 993–1080, same-spot within 0.76 cm⁻¹, reliable; with about
0.5 s between acquisitions the laser jumped by about −8 cm⁻¹ after the first every time. 532 nm needs no pause.
**From .23.24:** `lasers.<k>.settle_s` is a pause after each spectrum for that laser only (the larger of it and
`timing.acquire_settle_s`), counted in the time estimates; an `unstable_multi` laser with a pause is accepted (not
with LabSpec's own map engine, which cannot pause between points). Set it with `ls6t profile set
lasers.637.75.settle_s 5` and `timing.acquire_settle_s 0`. **Next: the minimum.** Five points with settle_s 2, then
1 (and 3 if 2 fails); note the laser line per point and `optics_check.since_last_acq_s`; keep the smallest that holds,
with a margin.
**Minimum (2026-11-23):** the jump shrinks with the pause: about −8 cm⁻¹ at 0.5 s, −7 at 1.7 s, −6 at 3.2 s
(`acquire_settle_s` 3.5, `run_20261123_212018_eef690`: laser −4.48 → −9.85…−9.54, Si 520.11 → 514.0–514.2), none at
4.7 s: the laser needs about 4–5 s to recover between acquisitions. Use `lasers.637.75.settle_s` 5 (6 for margin until
more runs confirm). `timing.acquire_settle_s` and `lasers.<k>.settle_s` apply without a bridge restart.
**LabSpec's own map engine and the laser between points (open):** if HORIBA's mapping keeps the laser on between
points, multi-point studies with 637.75 nm could go through it without the pause. To check: (1) how HORIBA's mapping
scripts (`StandardMapping XploRA.vbs`, `SWIFTMapping XploRA.vbs` and variants) handle `ShutterMode`, `LaserShutter`
or the laser between points; (2) a small LabSpec map (5 × 1 points) with 637.75 nm and the laser line in the window:
any jump between points? Until then ls6t refuses the native map engine for an `unstable_multi` laser.
**HORIBA's mapping scripts answer it (LabSpec 6.8.1 `SERVICE\StandardMapping XploRA.vbs`, `SWIFTMapping XploRA.vbs`,
read on the instrument PC, nothing copied):** their first line records a change on 11/06/2018 "from SHUTTER_AUTO to
SHUTTER_MANUAL + opening the shutter". Before the map they save `ShutterMode` and the `LaserShutter` index, set
ShutterMode manual and move `LaserShutter` to index 1 (open) for the whole map, then put both back. So HORIBA's maps
keep the laser on between points; in automatic mode LabSpec opens and closes the shutter around each acquisition,
which is what ls6t's spectra did, and the 637.75 nm laser changes mode after such a cycle unless it gets about 5 s.
**From .23.24: `acquire.shutter: "hold"`** (or `lasers.<k>.hold_shutter: true`) does the same for a study's spectra:
ShutterMode manual and the shutter open from the first spectrum to the last (the beam stays on the sample between
points, as in HORIBA's maps), then both put back (`shutter_hold` in the result). The bridge releases it before any
other command than the spectra, stage moves, the live view and quick reads, at a halt, when it stops, and after
`max_s` (1.5 × the study's estimate + 2 min). It needs the verified shutter (`shutter-verify`). An `unstable_multi`
laser accepts it in place of the pause. **Test:** five points, 637.75 nm, laser line in the window, `acquire.shutter:
"hold"` and `settle_s` 0: no jump and `shutter_hold.released` true; LabSpec's status bar and `instrument status`
(`shutter_held` 0, ShutterMode as before) after the study. If it holds, `ls6t profile set lasers.637.75.hold_shutter
true` and `settle_s 0`.
**Passed on .23.24 (2026-11-23, `run_20261123_215834_37fed8`, hold, no pause):** laser line −5.29, −5.15, −5.06,
−4.98, −4.90 cm⁻¹; Si 519.68–519.93; FWHM 6.0–6.2; no jumps, reliable; `shutter_hold` held and released (before Off,
mode 0; On while held); `status` afterwards `shutter_held` 0, shutter Off, mode 0. About 2.5 s per point against about
9 s with `settle_s` 5. **To watch:** a slow monotonic drift of the laser line, +0.4 cm⁻¹ over five points (about
10 s); in longer maps keep the laser line in the window (`checks.laser_line` reports every spectrum's position, and
`laser_reference: "correct"` follows a drift too). Set: `ls6t profile set lasers.637.75.hold_shutter true` and
`lasers.637.75.settle_s 0` (a laser is found by its wavelength since .23.23).
**Not every kind held on .23.24:** a `filters` study with 637.75 nm and `hold_shutter` true (`run_20261123_221051_ab4daf`)
had no `shutter_hold` and the same fall (10 % 1405.7 cps → 1 % 75.8, 0.54 ×; reference again 484.9, 0.345 ×): the hold
lived in the spectrum/map/series/depth runner only, and `unstable_multi` accepted the profile's hold anyway. **From
.23.25** the hold applies to every study that acquires (spectrum, map, series, depth, filters, focus, spot,
calibration, alignment, guide; not move, background, cutoff): it is taken before the study's first acquisition, the
bridge keeps it through the spectra, LabSpec's own map, the guided loop, motor and stage moves, the live view and quick
reads, and ls6t takes it again if another command (a snapshot, an autofocus, the operator's question) let it go
(`shutter_hold.times_held` above 1 then, with a warning). LabSpec's own map engine is accepted for an `unstable_multi`
laser when held. **Test:** the same `filters` study: `shutter_hold.held` and `released` true, `times_held` 1, and the
transmissions near nominal (10 % → 1 % about 0.1 ×, the reference again near 1).
**First map and series held (.23.24, 637.75 nm, 1200, 1 %, 1 s):** map 3 × 3, 2 µm (`run_20261123_221434_2536da`):
laser −3.52 to −3.76 (σ 0.07), Si 520.47–520.72 (σ 0.09), no jumps, about 2.5 s per point; series of 5, 5 s apart
(`run_20261123_221637_872628`): intervals kept, laser −3.45 to −3.72, Si 520.75–520.90. Notes and changes (.23.25):
(a) the map's live view in LabSpec failed ("LabSpec could not create the map"): the bridge now tries again without
the spectral range and gives Map's return value and LabSpec's last error (`map_create_return`, `_2`); send them if
it still fails. (b) The spectrometer move after a grating change (2400 → 1200) took 38.8 s (37 s before): LabSpec's
own MoveMotor of the spectrometer, which the bridge waits on; to look at with HORIBA's timing (a slow spectrometer
drive after a grating change?) — it is `t_spectro_call_s` in `timing`. (c) The estimate includes the series' waits
(`seconds.series_waits`; `acquisitions` alone leaves them out). (d) Series spectra are now saved as `.l6s` too. (e) A
series whose spectra are further apart than the laser's `settle_s` is not held (the beam is off the sample between
spectra): with `lasers.637.75.settle_s 5` and `hold_shutter true`, a series 5 s or more apart lets LabSpec's shutter
cycle, faster ones are held; `acquire.shutter` in a study decides otherwise.
**Filters held (.23.26, `run_20261123_224625_a8b563`):** `times_held` 1, released; the reference again 1.027; 1 % = 1.176 %
of 10 %, as plain spectra: the filters study is right when held. **But the set-up move 1200 → 2400 ended at 1800**
(`got: 1800 (450-850nm)`, `settle_stable` after 1.5 s, `wait_s` 0.06), and the study ran on the 1800 (10 % 1956 cps
against 1406 at 2400); the same move worked at 22:10 without the hold. The turret read 1800 for 1.5 s on its way, the
bridge took it as settled, and the next commands (the hold among them) went to LabSpec mid-move. **From .23.27:** a
grating move is read until it is the grating asked (`until_target`, up to 120 s, however long a passing position
reads still), then every motor idle; the hold waits for every motor idle before touching the shutter; a grating that
still is not the one asked is moved once more, and the study stops if it still is not (nothing measured on the wrong
grating). **Test:** the same filters study from 1200: the grating move's `until_target_s` and the result's grating
2400.
**Correction, the 1800 was LabSpec's refusal (2026-11-23, reproduced on .23.26, nothing fired):** grating 2400 →
laser 785.04 (grating stays 2400) → grating 1200 → laser 637.75 → grating 2400: stops at 1800, stable, no error.
Before the visit to 785 nm, 1200 → 2400 worked; selecting 2400 by hand in LabSpec (then a bridge restart) cleared it.
LabSpec seems to keep judging grating choices against the 785 nm laser after a scripted change back (2400's range
ends at 690 nm, so it takes the nearest usable grating). The same was seen on 2026-11-20 (1200 → 2400 stopped at 1800
with 637.75 nm; 1800 → 2400 with a switch to 785 worked). **From .23.27** a grating that does not arrive is tried
once more after selecting the laser LabSpec has again (a move to its own value), in `instrument set`
(`retried_after_laser_reselect`) and in a study's set-up; if it still does not arrive the study stops and says to
select it by hand in LabSpec. **Test:** the reproduction above through `ls6t instrument set`; does the reselect clear
it? If not, the next try is LabSpec's Template (`SET_VALUE "Laser"`, which acts on the live settings for
AutoExposure), and otherwise only the hand selection; also note whether the hand selection alone, without the bridge
restart, clears it.
**A bridge restart alone clears it (2026-11-23):** after the stuck state (785.04 → 637.75 via `set`, 2400 stopped at
1800), restarting only the bridge, LabSpec untouched, let 1800 → 2400 work at once: the state lives in the bridge's
script session (how a script changes the laser, or what LabSpec ties to that script). Until the cause is found the
messages say: `ls6t instrument end-bridge`, start `ls6t_bridge.vbs` again, then set the grating again. The first
`set` after that restart waited 40.7 s in `status_before`: the bridge's start-up (each motor's first status, T3.16)
was still running.
**From .23.28 the retry really sends something, and sooner:** the laser move is always sent to LabSpec (the bridge's
move does not skip a position already in place; only set-up steps do), and if the grating still does not arrive, a
second try goes through another laser (the shortest wavelength, which every grating can use; no laser fires) and back
before the grating again; then the study stops. `retried_after_laser_reselect` (set) and the set-up move's `retry`
list each try (`reselect.laser_nm`, `via`, `got`, `ok`). A wrong grating reading that stays the same for 10 s
(`until_target_stable`) ends the wait at once (it is a refusal); 120 s stays the limit for a turret still moving.
**Test:** the reproduction through `instrument set`: does the first try (the same laser again) or the second (via
another laser) bring 2400, or neither (then only the bridge restart)?
**Passed on .23.28 (2026-11-23):** (1) 785 → 637.75 → 2400 via `set`: stuck at first, cleared by the first try (the
same laser again, `via: null`), 12.95 s in all. (2) `filters` from 1200 (`run_20261123_234644_7db84e`): to 2400
directly, held once, reliable, reference again 1.099, 1 % = 1.260 %. `until_target_s` appears only when the grating
first read a wrong position (the extra wait); a move that arrives directly has `settle_s` alone. Across three good
runs 1 % read 1.176, 0.971 and 1.260 % (σ ≈ 0.15 points, like the reference's ±10 %): **from .23.29** the
`profile_command` is the mean of the last runs with the same reference, offered once there are 3
(`performance.filters_min_runs`; `average` gives mean, runs and spread per filter), and every filter not measured is
listed with why (`skipped`, and a warning: above the cap, or the exposure it would need beyond
`filters.max_exposure_s`, as the 0.1 % at 2 s × 100).
**Calibrations (.23.28, `run_20261123_235031_79dcca`, Si, 1 %, 2 × 2 s):** 637.75@1800 +3.50 (corrected); 532@1200
−0.85 and 785.04@1200 −0.76 (within tolerance, stored); 637.75@600 not trusted: Si at 529.62, FWHM 13.0, at the edge of
the 30 cm⁻¹ window. **From 2026.11.24.1** the fit window scales with the grating (30 cm⁻¹ at 1200 and finer, 60 at 600, at
most 90; `calibration.window_cm1` sets it), and `calibration.video_before: false` in the profile drops the frame each
calibration took first (17–20 s each; a study's `video.before` still decides for itself). The warning "no stored
calibration for 637.75@1200" is right: that batch calibrated 637.75 with 1800 and 600 only (each grating has its own
zero); a 637.75@1200 calibration clears it.
**Live map view in LabSpec (open):** on .23.28 `Map` returned −2147467259 (0x80004005, E_FAIL) with LabSpec's last error
empty; the retry without the spectral range did not run, because the range was already 0 (one window). The bridge's
call follows the 2018 manual (`Map(CREATE_MAP, DataID, MapID, SpectrumID, Values, Labels, Units, Display, From, To)`);
it has not been checked against 6.8.1's help. **Checked (2026-11-24):** in 6.8.1's `HELP\VBS\Map.html`, From / To
are the range whose average intensity colours the map (never 0 – 0, which is what a one-window study sent), and for a
map of known size the help and HORIBA's own map macro (`macros\horiba.vbm`) use `MapEx(CREATE_MAP, 0, SpectrumID,
index, AxisX, AxisY, Labels, Units)`, then `ADD_TO_MAP` with index = iy × SizeX + ix. **From 2026.11.24.1** the live
view of an X/Y map uses MapEx that way; a map with Z still uses `Map()`, now with the spectrum's own range. **Test:** a
3 × 3 map: LabSpec shows the map growing point by point, no "live view" warning.
**Failed on 2026.11.24.1, serious (`run_20261124_090756_087048`, `run_20261124_090921_4db97c`; 3 × 3, 637.75 nm,
1200, 1 %, `hold_shutter`):** point 0 measured, then the bridge hung in `view` (the MapEx create), no map window; the
first time LabSpec froze (restarted by hand), the second it crashed. The shutter hold was on both times, so the beam
may have stayed on the sample until LabSpec was restarted. **From 2026.11.24.2:** the live view is off
(`display.live_view: false`; on only for a test); a display call (`view`, video) is never made with the shutter held
(the bridge lets the hold go first: `view` is no longer among the commands that keep it); the heartbeat says
`shutter_held`, and when the bridge stops answering while it was held, every message (status, a stopped request)
starts with "THE LASER MAY STILL REACH THE SAMPLE" and how to switch it off. After such a crash: switch the laser off
in LabSpec's status bar (or at its supply / key if LabSpec does not respond), restart LabSpec, check that the shutter
reads Off and ShutterMode is 0. The MapEx call itself (arguments as HORIBA's macro, axes as doubles, labels "X"/"Y",
units "mkm") stays to be understood before the live view is tried again, without the hold.
**Cause found (2026-11-24, Nicola, `mapex_test.vbs` / `mapex_live.vbs`):** not the hold (the live variant with the
shutter automatic crashed the same way at the view create of point 0). Offline, with loaded spectra and no laser, the
only difference between a pass and a crash in MapEx CREATE is how the axes are passed: a declared array (`Dim ax()`,
`ReDim`, filled) passes; a Variant holding an array (what the bridge's `NumList` returned, and what its views
dictionary kept for ADD) crashes LabSpec. `Map()` takes no axis arrays, which is why it never crashed (it returns
E_FAIL here whatever its range, so it is no longer used). **From 2026.11.24.3** the bridge copies the axes into
arrays declared in VView before every MapEx call (CREATE and ADD), the units are "um", and a map with Z gets no live
view (only `Map()` would do it). No other LabSpec call in the bridge passes an array held in a Variant. The live view
stays off by default until it passes: **test** `ls6t profile set display.live_view true`, a 3 × 3 map at 637.75 nm
with the hold: the map grows in LabSpec point by point; then the same without the hold.
**Not dark:** with ShutterMode automatic, "laser off" in LabSpec's status bar does not keep the beam off an
acquisition: LabSpec opens the shutter for each Acq (`mapex_live` variant 6, point 0: Si at 654 counts). A dark
acquisition needs ShutterMode manual with the shutter closed (what `laser_off: "shutter"` does).
**Live view on .24.3 (2026-11-24):** the MapEx fix works (3 × 3 Si maps at 637.75 nm, one create and eight adds, all
ok, held and automatic). But each `view` released the shutter hold (it is not among the commands that keep it, since
.24.2) and the next point held it again: a held map was released 9 times "by: view", laser line −2.4 to −12.8 cm⁻¹, Si
513.8 ± 3.1 (unreliable), and the re-holds cost 13.8 s of 42 s; a 2 s series the same. The automatic-shutter map
had no hops (about 6 s per point, as the ≥ 5 s pause). And the map window came to the front after point 0 only:
LabSpec's AUTO_SHOW brought each new spectrum over it. **From 2026.11.24.4:** with the shutter held, the live view is
made after the last spectrum, once the hold is released (no display call with the shutter open, as asked after the
crash, and no cycling); without the hold it grows point by point; a live X/Y map acquires without AUTO_SHOW so the map
stays in front. **Test:** the held 3 × 3 map with the live view: `shutter_hold.times_held` 1, laser line steady, the
map appears at the end; the automatic one: the map in front and growing, and check the "um" axes.
**Series spacing (.24.3, 2 s asked):** start to start 9.63, 3.53, 3.55, 3.52 s: the first gap carries one-off costs
(the hold, the first display). From .24.4 `series.spacing` gives `median_s`, `mean_after_first_s` and `first_s`, the
warning judges the median, every spectrum's `t_start` is in `spectra[]` (it was recorded but not shown), and
validation warns when `interval_s` is shorter than one spectrum takes (1 s exposures take about 3.5 s here).
**Notes:** spikes flagged in most 0.5–1 s spectra are likely real cosmic rays: with one accumulation LabSpec cannot
remove them (its removal compares accumulations); use `accumulations: 2` with `spike: "double"`. The heartbeat's
`blocking_s` (about 124 s during an acquisition) is the allowance, the longest the acquisition may hold the bridge
(the request's acquisition timeout), not the time elapsed. The heartbeat is UTF-16 on purpose (Windows account
folders can have non-ASCII names); read it as UTF-16, as ls6t does.
**From 2026.11.24.5 (review):** the live view keeps the shutter hold again (`view` among the commands that keep it):
the rule against display calls with the shutter held came from the .24.1 crash, whose cause was MapEx's axes (it
crashed with the shutter automatic too), and releasing the hold at each view brought the hop back. The remaining risk,
LabSpec freezing in a view with the shutter open, is reported by the heartbeat's `shutter_held` ("THE LASER MAY STILL
REACH THE SAMPLE"). `display.live_view_while_held: false` makes the view after the last spectrum instead (the .24.4
behaviour). **Spikes:** a flagged spike must now also be one or two pixels wide at half its height above the local
level (a sharp Si or laser line, about 3 pixels at 1200 gr/mm, could pass the old test), and every spectrum lists where
(`spectra[].spike_cm1`): if they sit at 520 or near 0 cm⁻¹ they were the lines. The heartbeat's `blocking_s` is now
`blocking_allowance_s`. **Tests on .24.5:** (1) a held 3 × 3 map at 637.75 nm with the live view: `times_held` 1, no
laser-line hops, Si near 520.7, the map growing; (2) the same with the shutter automatic: the map in front, growing,
"um" axes; (3) a 2 s series: the validation warning, `median_s` about 3.5 s, `t_start` in each spectrum; and the spike
count with `spike_cm1`.
**Spikes at 600 gr/mm (.24.6, review):** there the Si and laser lines can be as narrow as a cosmic ray, so the width
test alone may flag them. A spike that comes back within two pixels in at least two spectra and 30 % of a study's
spectra is now taken as a line and removed from every spectrum's count (`checks.spikes.recurring_cm1` says where); a
cosmic ray almost never repeats. A single spectrum keeps the width test (and a fitted peak's exclusion). **Test:** a
5-point series at 600 gr/mm on Si, 1 accumulation: `recurring_cm1` near 520 (and 0 if the laser line is in the
window), few or no spikes left.
**From .24.7 (review):** the 30 % rule would have missed a minority phase's narrow line (in 10 % of a map's points it
stayed a "spike" at every one). A position is now a line when at least 3 spectra have a spike there and that many is
unlikely by chance: with n spikes over P pixels in all, another spectrum has one within ±2 pixels with mean
(n − 1) × 5 / P, and the count must have a Poisson probability below 0.001. **Spikes never change the data:** ls6t only
counts and warns (and, at `spike: "none"`, says so); LabSpec's own spike removal (`spike: single / double`) is the
only thing that alters a spectrum, during the acquisition.
**Also in .24.7:** a series waits `interval_s` from the previous spectrum's start (a fixed grid from the first start
let a spectrum after a slow one start early to catch up), and `runner.log` gives each view's `id`, `index`, `axis_x`,
`axis_y` and the answer's `mapex_return`, to check the grid LabSpec was given.
**Live map not shown as a map on .24.5 (2026-11-24, open):** no map during the acquisition, and at the end the Maps tab
shows curves, nothing to click. The axes ls6t sends are the grid's (`axis_x` from `map.x_um`, `axis_y` from
`map.y_um`, index iy × nx + ix), as in the offline test that gave a real map. To check first: `display.live_view` on
for that run; the map engine (with `map.engine` 'auto' a large map goes to LabSpec's own engine, which has no ls6t live
view); then the run's `runner.log` view lines (`axis_x`, `axis_y`, `mapex_return`), the map's size and steps, and a
screenshot of the Maps tab.
**Results on .24.7 (2026-11-24 afternoon, 637.75 nm / 1200 / 1 % / x100, Si):** the live map works (3 × 3, held, live
view on: it grew in LabSpec, R/G/B cursor images clickable, axes "um"; 1 create + 8 adds + save, axes 0,2,4, snake
indices; `times_held` 1, laser line −5.27 … −4.70, Si 519.81 ± 0.13, reliable; the earlier "curves" were the live view
off). A 2 s held series met its interval (2.008, 2.006, 2.003 s after a first 3.22; `median_s` 2.006). Si and the laser
line are no longer flagged as spikes. **Changes in 2026.11.24.8:** (1) **corrections per window centre**: two Si
calibrations a minute apart, one window centred on Si and one for −100 … 700 cm⁻¹, gave −0.72 and −2.40 cm⁻¹ (and Si
from the laser line 523.2–524.4: the dispersion differs too), each overwriting the other; ls6t now keeps a correction
per window centre, applies the one for a study's centre (or interpolates between two), and warns when only a distant
one exists or the study has several windows; the calibration says its `window_centre_nm`. Calibrate with the window
your studies use, or keep the laser line in the window with `laser_reference: "correct"`. (2) **Series validation
with the hold**: about the exposure + 1 s per spectrum (`timing.held_overhead_s`), not 3.8 s. (3) **Spikes**: not
on the window's first or last three pixels, below the measured cut-off, or within 20 cm⁻¹ (or 3 FWHM) of the laser
line; a spike must also stand 3 √(level) and 5 counts above its local level (a very smooth noise estimate let 2-count
noise through); each spike gives its height (`spike_info`: cm⁻¹, counts, sigma). (4) The spectrometer move ls6t
makes for the study's window no longer counts as "moved / busy around point 0" in `optics_check`. (5) The live view is
on by default. **Test:** calibrate 637.75@1200 in both windows, then a spectrum in each: each gets its own correction,
and a third window between them an interpolated one.
**The two zero corrections (.24.9, review):** a stored correction already holds the laser's position at its
calibration, so following the laser line as well could count the zero twice. Now a correction keeps the laser line it
was measured with (when the window had it), and with `laser_reference: "correct"` each spectrum is first brought back
to THAT laser line, then the correction applies: the stored offset relative to the laser line. A correction measured
without the laser line in the window: the spectra are brought to the study's first laser line (the drift within the
study), then the correction applies as measured, and a warning says the laser's move since the calibration is not
covered. Without `correct`: the stored correction only. `checks.zero_correction` says which (`method`, the stored
`a`, `b`, `centre_nm`, `laser_line_cm1`, and the laser-line reference used). **Test:** calibrate 637.75@1200 with the
laser line in the window (`range_cm1` from about −100), then a spectrum with `laser_reference: "correct"` some hours
later: Si near 520.7 and `method` "the stored correction relative to the laser line". The spike floor's counting
noise assumes one electron per count unless `detector.e_per_count` says otherwise (the 5-count minimum covers low
signal).
**Per-window zero on .24.9 (2026-11-24, 637.75 / 1200 / 1 % / x100):** stored centre 650.63 nm (−100 … 700): −2.01,
laser line −5.74; centre 659.66 (Si): −0.46, −4.58; `method` "the stored correction relative to the laser line"; Si
520.98 with no hop. **Problems and changes in 2026.11.24.10:** (1) hops between studies: each study took and released
its hold, so the shutter closed between them; hop against the gap since the last close: 4.9, 9.9, 11.7 s yes, 21.9,
48.8 s no. The hold is now kept into the next study when it holds too, with the same laser (`shutter_hold`:
`kept_for_next_study`, `continued_from_previous_study`); released at the end of the request, at a laser change, at a
stop, or when another command (the operator's question, a snapshot) needs it. (2) After a hop, following the laser line
left about 1 cm⁻¹ (Si 519.57 and 520.05 after −8.0 and −8.9 cm⁻¹; lines 1.5–2 × wide): a spectrum whose laser line
jumped more than 3 cm⁻¹, or is over 1.2 × as wide as at the calibration (or the study's narrowest), is flagged
`zero_uncertain` (with `checks.zero_correction.uncertain_points` and a warning); corrections now keep the laser line's
width too. (3) With a range (From ≠ To) LabSpec's Acq places the spectrometer itself (676.75 nm read after Acq for data
centred near 650.6): ls6t no longer warns about that move. **Corrected in 2026.11.24.11:** the real centre is the
spectrometer's, 676.75 nm. For a range that fits one window LabSpec puts the window's blue edge at From (−100 cm⁻¹ =
633.7 nm; 633.7 + half the 1200 window ≈ 677.6, as read) and returns only From…To, so the *data* look centred at
650.6 while the grating sat at 676.75. "−89.7 instead of −100" is the window's first pixels landing just inside From.
ls6t now keys per-window corrections by the spectrometer's reading after the data (`checks.window_centre`:
`nm`, `from`, `range_centre_nm`; the calibration's `applies_to.window_centre_from`). **The correction stored as
"650.63" on .24.9 was measured at 676.75:** recalibrate that range (or clear it with `ls6t references clear correction 637.75@1200`
and calibrate again) before relying on it. (4) **Interpolation passed (Nicola, .24.9):** a window centred at 655.30 nm got
+1.21, halfway between +2.01 at 650.63 and +0.46 at 659.66 (laser line −5.14 interpolated), Si 521.13. Since the
650.63 end was really 676.75 (above), repeat it after recalibrating: Si should come closer to 520.7.
**Follow-up on .24.10 (2026-11-24):** (a) the bridge's idle lease counts from the end of its last command (when the
result is written), only between commands: a long exposure or a native map never triggers it (midnight-safe). A hold
kept into the next study gets 120 s more for the analysis in between; if the bridge still let it go, the next study
says so (`shutter_hold.reopened_after_bridge_release` and a warning). (b) `laser_reference: "correct"` now follows the
laser line's **steps** (a hop starts a new plateau; each spectrum uses its plateau's median), not its slow creep, which
dragged Si 520.52 → 520.00 in a series; `"correct_each"` keeps the old per-spectrum following.
`checks.zero_correction.plateaus` lists them with their drift. (c) After the hold opens the shutter ls6t waits
`lasers.<k>.hold_open_s` before the first spectrum (default: the laser's `settle_s`, else 5 s for a laser marked
`mode_hops` / `unstable_multi`), for the first-spectrum width (9.9 cm⁻¹ against about 6); `shutter_hold.open_wait_s`.
(d) A native map's `shutter_hold.after_native_map` says whether the hold was still in place at the end, as LabSpec
read it (ShutterMode and the shutter's label); if HORIBA's engine changed it, a warning and the hold is put back at the
study's end. **Tests:** a 10-spectrum held series with 637.75 and `correct`: Si within about 0.2 cm⁻¹ and one plateau;
the first spectrum's laser-line FWHM like the others; a native map with `acquire.shutter: "hold"`: `after_native_map`.
**Native map works (.24.10, 3 × 3, 2 µm, 637.75 / 1200 / 1 %, centre 300, 1 s × 2, held):** LabSpec's map window
filled point by point, 27.9 s (about 60 s with the points engine); hold released afterwards. Point 0 was taken before
the beam was really on (about 6 counts against 150, laser line +1.5, Si 538.8) and entered the summary. **From
2026.11.24.11:** such a spectrum (no laser line where most others have one, or its laser line under 20 % of the median height; without
the line in the window, a signal under 20 % of the median for a spectrum or series, only a warning for a map) is kept out of the summaries (`spectra[].excluded`, `checks.excluded_points`); a held native map now waits
`hold_open_s` after opening the shutter (5 s for the 637.75 with its `settle_s`), and `warmup_s` also runs before
LabSpec's map. "Moved by +1.6 at points 4–8" was the line's distance from the calibration, not a hop: jumps are now
judged spectrum to spectrum (and confirmed by the Raman peaks moving with the laser line), and the distance from the
calibration is `checks.laser_line.drift_since_calibration_cm1`. The axis readbacks are in `native_map`. Si 0.7 high
with `correct`: the correction it used ("650.63") was measured with the spectrometer at 676.75 nm (the range
calibration, above), whose laser line sits elsewhere; recalibrate (with the range or the map's window) on .24.11.
**600 gr/mm series (.24.10):** spikes pass (two real cosmic rays, 46 and 48 σ). A "mode hop" of the laser line (+3.6 /
+5.9, 1.5–1.8 × wider) with Si steady was a bad fit of the 2-pixel line on the edge filter: **from .24.11** a laser-line
step the Raman peaks do not follow is listed in `checks.laser_line_bad_fits`, is not a hop and is not used for
`correct`. The 600 zero (Si 522.85 with the 09:12 correction, no window centre): recalibrate. **Test:** the same series:
no hop, reliable, `laser_line_bad_fits` at those points.
**Per-window zero on .24.11 (after clearing 637.75@1200):** centred at 300 cm⁻¹ (650.19 nm) +0.01, centred on Si
(659.66) −0.02, range −100…700 placed by LabSpec (676.75) −2.09: the 2 cm⁻¹ is only where LabSpec puts From at the
window's edge. Interpolation passed (+0.008). Native map passed (9 good points, Si 520.80 ± 0.17, readbacks,
`after_native_map` held). **From 2026.11.24.12** a range within one window is acquired centred on it and cut by ls6t
(`range_placement: "labspec"` for the old way); `correct` applies a laser-line step only over 3 cm⁻¹ with the Raman
peaks moving too (Si 521.01 with `correct` against 520.28 without at 655.30 nm; 519.18 against 520.46 at 600 gr/mm),
and `zero_correction.plateaus` shows each step and whether it was applied; a calibration laser line over 2 × Si's width
(600: 19.4 cm⁻¹) is not kept as a reference. **Tests:** −100…700 range: Si within 0.1 of a centred window, no
correction needed; the 600 series with `correct`: `applied` false, Si ≈ 520.5; `native_map.seconds_map` ≈ the map's
time.
**Native map on .24.9, safety (3 × 3, 2 µm, 637.75 / 1200 / 1 %, 1 s × 2):** the map held the bridge about a minute
(LabSpec's own map acquisition); ls6t called the bridge gone after 20 s ("busy in map_native"), and the shutter release
it then sent was withdrawn from the bridge's queue, so the shutter stayed held open (laser_shutter On, ShutterMode 1,
`shutter_held` 1) until a hand laser_off a minute later. **From 2026.11.24.10:** the native map announces in the
heartbeat that it holds the bridge and for how long (as acquisitions do), so ls6t waits; a safety command
(`shutter_hold` off, `laser` off, `safe`) is never withdrawn: it stays queued and the bridge carries it out as soon as
it is free; and the bridge releases a hold by itself when no command came for `idle_s` (60 s, plus a series'
interval). The hold is kept for the native map: HORIBA's mapping scripts do the same (ShutterMode manual, LaserShutter
open), so the stall was the 20 s, not the hold. **Test:** the same native map: it runs to the end with no "stopped
answering", `shutter_hold` released, and `instrument status` after it shows `shutter_held` 0, ShutterMode 0, shutter
Off; then stop ls6t in the middle of a held map (cancel) and check that the bridge releases the hold within about
60 s by itself.
**Native map did not map (.24.9, two tries, held and not):** LabSpec made an empty map object ("Pixel" axes 0–30 ×
0–1, no titles, no data), acquired nothing, and the stage never moved; the bridge was held about a minute. The bridge
started LabSpec's map with its own flags, the accumulations and From = To; **from 2026.11.24.10** it starts it as
HORIBA's mapping scripts do (`Acq ACQ_AUTO_SHOW + ACQ_SPECTRAL_IMAGE, -1, 0, 0, 0`: time, accumulations and range from
LabSpec's interface, set just before; the spectrometer at the centre first), and reads back what LabSpec took for
each axis (`x_readback`, `y_readback`: from / to / step / mode / use). **Test:** the same 3 × 3 native map: LabSpec's own
map window fills point by point (no ls6t live view with the native engine), the stage moves; send the readbacks if it
still does not. Running HORIBA's own map from LabSpec's interface with the same settings shows whether the engine maps
here at all.
**Focus scan (.24.9, 637.75 / 1200 / 1 % / x100, hole 300, 0.5 s per step, ±5 µm × 1 µm):** 36 … 57 cps, about 25
counts per step (20 % counting noise), more than the curve's structure, and +0.51 µm was still chosen. **From .24.10**
each step gives its `counts`, `counts_sigma` and `snr`; a change with Z under 4 × one step's noise is no focus (Z goes
back to the start; `contrast_over_noise` says by how much); fewer than 300 counts at the best step is warned with the
exposure to use; `z_left_at` says where Z was left. A standalone focus study keeps the focus it found (that is its
purpose: later spectra and studies in the request use it); with no clear focus Z returns to where it started.
**runner.log was empty after the crash:** from 2026.11.24.2 every bridge command is written to the request's
`runner.log` when it is sent and when it returns (`-> view kind=mapex`, `<- acquire ok=1 2.4 s`, marked `[shutter
held]`), each line flushed at once, so the last call before a hang or crash is always there.
**The 637.75 nm "zero drift" is the laser's wavelength (2026-11-24, `run_20261124_091431_0d748d`):** nine hours after
the 637.75@1200 calibration Si read 521.67–522.21 corrected (+1.3), but the laser line moved with it (−4.69 at the
calibration, −3.13 … −2.63 now) and Si from the laser line stayed 524.8–525.0: about 0.07 nm overnight, +0.5 cm⁻¹ within
a 30 s series. **From 2026.11.24.2** a laser can default to following its line (`lasers.<k>.laser_reference:
"correct"`, used whenever the line is in the window) and give its calibrations a shorter life
(`lasers.<k>.calibration_max_age_h`, e.g. 2): `ls6t profile set lasers.637.75.laser_reference correct` and
`lasers.637.75.calibration_max_age_h 2`.
**Series spacing (2026-11-24):** 2 s asked gave 1.66 s (`run_20261124_091707_5c0dc5`, held), 6 s gave 7.8–7.9 s
(`run_20261124_091431_0d748d`, not held: the laser's 5 s pause after each acquisition came on top, since a 1 s
exposure takes about 2.5–3 s with LabSpec's overheads). `interval_s` is start to start; ls6t waits until each
spectrum's start time. **From 2026.11.24.2** each spectrum has `t_start` (when its acquisition was sent), the series
has `spacing` (asked, mean, min, max, every start-to-start gap) with a warning and the reason when it differs from the
interval by more than 10 %; whether a series is held now counts each acquisition's real duration (6 s with 1 s
exposures is held, so the pause no longer stretches it). The 1.66 s for 2 s is not explained by the code (it waits
until start₀ + k × interval): repeat it and send `series.spacing` and `t_start`.
**Zero drift:** 637.75@1200 read −1.06 cm⁻¹ (Si 519.64, corrected) late on 2026-11-23, about +0.3 earlier that day:
this laser's zero moves by about 1 cm⁻¹ within a day; `calibration.max_age_h` (24) may be long for it.
**Passed on .23.27 (2026-11-23):** 785 → 637.75 → 2400 through `instrument set` reached 2400 with the laser selected
again (`retried_after_laser_reselect` ok), no bridge restart; 123 s in all, the full 120 s wait before the retry (from
.23.28 a stable wrong reading ends it after 10 s). LabSpec's interface kept showing 1800 until its next dialog, but the
spectrum straight after (`run_20261123_233004_9bc562`) was on the 2400: window 252.8–751.5 cm⁻¹ (21.66 nm, as
learned), Si FWHM 3.95, 520.52 cm⁻¹ with the 637.75@2400 correction. So the same laser again is enough; the
interface's display lags while the bridge runs (trust `instrument status` and the spectrum). The spectrometer move for
that spectrum took 67.9 s (`t_spectro_call_s`; 37–50 s before): LabSpec's own move, longer the farther the
spectrometer was left (here by the 785 nm visit).

**T3.19 LabSpec's AutoExposure (2026-11-23, .23.13).** With AutoExposure On in LabSpec (status bar "AE"), a
spectrum asking 2 s ran 15 s while ls6t reported 2 s (this explains most of the 23–64 s acquisitions of T3.17).
From .23.13 the bridge switches AutoExposure off for a fixed exposure (and back after), and ls6t takes the exposure
from LabSpec's acquisition info: with AE On in LabSpec, run a 1 × 2 s spectrum. Pass: it takes about 2 s of exposure,
`spectra[].exposure_s` is 2 with `exposure_source` LabSpec's, and `auto_exposure_before` / `auto_exposure_set` in
the bridge's answer show the switch; LabSpec shows AE again afterwards. If LabSpec still used another exposure, the
study stops and says so (then AutoExposure is not a value the bridge can set: switch it off by hand and tell the
maintainer). Today's results from studies with AE on carry the wrong exposure: their counts per second are off by
the ratio (15 / 2).
**Results on .23.14 (2026-11-23):** `status` gave `auto_exposure: ""` and the switch `-> ?`: `AutoExposure` is not
LabSpec's GetValue name (the spectra's header says `acq.AutoExposure=On`, the acquisition info's row). The safety
held: with AE on, 15 s for 2 s asked, the study stopped with nothing stored. With AE off by hand: `exposure_s` 2.0
from LabSpec's record, total 10.5 s (operator 2.75, Acq call 3.85, flush 1.62, analysis 1.3). From .23.15 the bridge
tries `LabSpecAutoExposure`, `AutoExposure`, `LabSpecAutoExposition`, `AutoExposition`, `LabSpecAE`,
`AcqAutoExposure` and reports which one reads (`auto_exposure_name` in `status` and in the acquire's answer); repeat
with AE on and note it. Until one is found, switch AE off by hand: ls6t stops rather than mislabel the exposure.
**Acq holds the bridge for the whole exposure, one window too** (`t_acq_call_s` 17.46 s for 15 s, 2.18 s for 2 s):
`laser_shutter.during_checked` is always false on this instrument; the shutter's verification on Si is the proof.
**The flush (`t_flush_s` 1.62 s) is shortened in .23.15**: it stops when GetAcqID answers the same three times
(`flush_tries`, `flush_last_id` in the answer) and never takes that answer for the new spectrum. Check that
`t_flush_s` drops below 0.2 s and spectra are still this acquisition's (the Remark's `ls6t <seq>`).
**On .23.15:** `auto_exposure_name` stayed empty with AE on and off: none of the six names reads. **Found in
LabSpec 6.8.1's help (`HELP\VBS\Template.html`, `constants.vbm`):** AutoExposure is a template value,
`Template(TEMPLATE_GET_VALUE = 4 / TEMPLATE_SET_VALUE = 3, "AutoExposure", value)`; `Acq` already receives the asked
exposure (the bridge passes it), and LabSpec's AE still replaced it. **From .23.17** the bridge reads it through
`Template` (`status`: `auto_exposure`, with `ae_status_param` / `ae_status_return`, since the help's lines 31–53 on
what GET returns are still to read) and writes it only when the profile has `autoexposure.switch_off: true`, then
puts the value read back after the acquisition (`auto_exposure_before`, `_set`, `_after` in the answer). It never
applies a template. Steps: (1) `ls6t instrument status` with AE on and with AE off: note `auto_exposure`,
`ae_status_param`, `ae_status_return`. (2) Read `Template.html` lines 31–53 (what SET / GET take and return, the
values of "AutoExposure", whether SET_VALUE changes the live settings or a stored template that must be applied).
(3) Only if SET_VALUE acts on the live settings: `ls6t profile set autoexposure.switch_off true`, AE on in LabSpec, a
1 × 2 s spectrum. Pass: `exposure_s` 2 from LabSpec's header and LabSpec shows AE again afterwards. Until then AE is
switched off by hand; ls6t stops a study whose exposure differs from the one asked.
**Passed on .23.17 (2026-11-23, Si, 637.75 nm, 2400, 1 %, x100):** GET returns the value in the third argument
(`ae_status_param` 0 with AE off, 1 with AE on; `ae_status_return` 0 both times), following the status bar. With
`autoexposure.switch_off: true` and AE on, a 1 × 2 s spectrum ran at 2 s (LabSpec's header; Si 244.9, as earlier
2 s spectra) and AE read 1 again afterwards: SET_VALUE acts on the live settings. From .23.18 `switch_off` is true
by default, and each spectrum (`spectra[].auto_exposure`) and the study (`auto_exposure`: `before`, `switched_off`,
`while_acquiring`, `off_during`, `after`, `restored`, `all_restored`) show it in the result. Flush: `t_flush_s` 0.05
s (was 1.62). Largest overhead now `t_spectro_call_s` 6.0 s, just after a LabSpec restart (to watch: does it stay
large in later studies?).
**TEST_TEMPLATE at start (.23.18):** LabSpec's help documents no return values for SET / GET and no value list for
"AutoExposure" (0 / 1 observed). The bridge calls `Template(TEST_TEMPLATE = 8, "", v)` once when it starts, with
an AutoExposure read, and reports both in `ls6t instrument status` (`ping.template_test`, e.g. `return=…, value=…,
autoexposure=1`). Note what it says on this XploRA, with AE on and off: if a return value tells a loaded plugin
apart, ls6t can then refuse to rely on SET / GET when it is missing.
**Passed on .23.18 (2026-11-23), with the default `switch_off: true`:** AE on in LabSpec, a 1 × 2 s spectrum ran at
2 s; the result showed `before` 1, `switched_off`, `while_acquiring` 0, `off_during`, `after` 1, `restored`,
`all_restored`. Total 8.85 s (operator 2.55, `t_acq_call_s` 3.80, `t_flush_s` 0.08, spectrometer move skipped), against
23–64 s this afternoon. `template_test`: `return=0, value=, autoexposure=1`: with no template name the 0 does not tell
a loaded plugin from a missing template, so it stays a record, not a gate. The AutoExposure read itself is the
working evidence (a value 0 / 1 that follows the status bar); if it ever comes back empty or as an error, the bridge
leaves AE alone and the header check stops a study whose exposure differs. Deploying .23.18 ended the running bridge
cleanly (`bridge_was: idle`, `ended: cleanly`).

**T4.9b LabSpec's AutoCalibration from ls6t (maintainer; the laser fires on Si).** `ls6t profile set calibration.autocal '{"enabled": true, "method": "call", "mode": "full"}'`, Si wafer in focus, 532 nm, 1200, 10 %; `ls6t instrument autocal-run`: LabSpec asks, then runs its AutoCalibration. Pass: `ok`, `changed` lists the 1200's parameters (as by hand: offset 34 -> 44 on 2026-11-23), and a calibration check after it passes. If the answer says LabSpec does not offer the call to scripts, set `"enabled": false` and keep running it from LabSpec's menu.

**Start-up inventory.** From 2026.11.23.5 the bridge takes the inventory only when none exists, it is older than 7 days, or `inventory.flag` is in the bridge folder (create it to force one). Check: a bridge restart after AutoCalibration or a grating changed by hand starts without 'inventory' in the heartbeat.

**T5b.3 The beam (LASER, Q15).** Only after T1.2 confirmed the laser shutter motor. Use silicon, the lowest filter
that shows a spot on the camera (typically 1 %), and the maintainer at the instrument. Run
`ls6t instrument snapshot --laser`.

Pass:
- LabSpec asks for confirmation (answer Yes);
- the shutter opens for about 1 s, then closes;
- LabSpec's shutter mode is back to what it was;
- the answer has `shutter_closed: 1`;
- the PNG shows the spot inside the dashed circle;
- `spot_um` and `measured_offset_um` are given, with no warning.

Then answer No to the question once and check nothing opens. Also check the 785 nm laser: the camera may not see it,
which gives a "spot not found" warning.

**T5b.4 Repeatability of the beam's offset (LASER, Q16).** Repeat T5b.3 three times at x100. Pass: the three
`measured_offset_um` agree within about 0.5 µm (the spot's size sets the precision). Nothing to copy by hand: each
measurement is remembered per objective and laser (`ls6t spot status`) and the marker uses it; the profile's
`video.laser_offset_um` is only the fallback. Other objectives and drift: T5e.3.

**T5b.5 In a calibration study.** Run the calibration study with `"video": {"after": true}`. Pass: `video.before`
and `video.after` are in the result and the images, and the Si result is as in T4.2.

**T5b.6 Extended video (no laser; THE STAGE MOVES; Q39).** The maintainer sets a small extended-video area in
LabSpec's interface (a few fields of view), at x10 or x5, and checks the objective's clearance over it. Then
`ls6t profile set video.allow_extended true` and `ls6t instrument snapshot --extended`. Pass: LabSpec asks (answer
Yes); the stage scans the area; `frame.marked.png` shows the stitched image with µm axes over the whole area; record
`extended_returned`, `extended_s`, and where the stage ended. If the image is not found or LabSpec misbehaves, set
`video.allow_extended` back to false and note what happened.

**T5b.7 The laser on for viewing (LASER).** After T5b.3, at the lowest filter, the internal camera on:
`ls6t instrument laser-on --seconds 20` (or ask the LLM *"turn the laser on so I can see it"*). Pass: LabSpec asks
first; the beam shows on the camera; the shutter closes by itself after 20 s and the status bar says so. Then open it
again and (a) `ls6t instrument laser-off`, (b) close the ls6t window, (c) send any move: each closes it at once.
Answer No once: nothing opens.

## T5c. Alignment (LASER)

**T5c.1 The module (Q17).** With the maintainer, find *Maintenance > AutoAlignment* in LabSpec. Record whether it
exists, its *Last run on* and *Current status*, and the status-bar indicator's colour. Optional: check with
Windows' *Accessibility Insights* or *Inspect* whether the *Current status* text is visible to UI automation
(read-only).

**T5c.2 Baseline after AutoAlignment (Q18).** For each laser in use:

1. The maintainer runs AutoAlignment as HORIBA's quick start guide says (reference Si, x100, full power at the
   source, focus), until it passes.
2. Run an `alignment` study for that laser with the maintainer's grating, filter and objective, e.g.
   `{"a532": {"kind": "alignment", "sample": {"name": "HORIBA Si reference"}, "setup": {"laser_nm": 532,
   "grating": 1800, "filter": "10%", "objective": "x100"}}}`.
   Pass: `alignment.passed` is null (no baseline yet) and the check is reliable.
3. `ls6t alignment record <request>`. Pass: `ls6t alignment status` shows the laser's settings and baseline.
4. Run the study again with only `setup.laser_nm`. Pass: `passed: true`, `ratio` between about 0.9 and 1.1
   (record it: it shows how repeatable the check is).

**T5c.3 A failing check.** Defocus by about 5 µm, or use a filter one step stronger in a hand-written setup, and run
the alignment study. Pass:
- `passed: false` with the ratio;
- LabSpec shows the checklist message (**Q18**). Answer **Cancel**: the study ends with the recommendation, and
  nothing else runs.

Run it again, refocus, answer **OK** without running AutoAlignment. Pass: the second check passes; `attempts` lists
both.

**T5c.4 Readiness.** With a baseline recorded, run a spectrum study on a laser without a check today. Pass: the
warning about its alignment check.

## T5d. Guided adjustments (LASER, Q19–Q20)

**T5d.1 The panel (Q19).** Silicon, in focus, a low filter. Run
`{"g": {"kind": "guide", "sample": {"name": "Si"}, "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%",
"objective": "x100"}, "guide": {"instruction": "Do nothing: this tests the panel", "mode": "maximize",
"max_s": 20}}}`.
Pass:
- LabSpec shows the ls6t guide panel with the instruction;
- the signal updates after every reading while the spectra appear;
- after 20 s, *Time is up* appears;
- *Done* closes it.

Record `guide.period_s`. If the panel did not appear, the result has a `ui_fallback` warning: set
`ls6t profile set guide.ui statusbar` and repeat. Pass: the status bar shows the readings, and a message box
shows the final message.

**T5d.2 Focus by hand (Q20).** The maintainer defocuses by about 10 µm. Run a `maximize` guide with the instruction
*"Turn the focus slowly towards the sample"*. The maintainer turns the focus slowly. Pass: STOP (*you just passed the
best*) appears within about one turn past the best focus; going back slightly gives the best signal. Record
`max_value`, the signal after going back, and how far past the best the maintainer had gone.

**T5d.3 Power to a level.** If the laser has an adjustable power at the source: a `reach` guide with a target
about twice the current signal (`ls6t validate` shows the estimate). The maintainer raises the power slowly. Pass:
STOP when the target is reached; never above the saturation guard. Repeat with `drop`.

**T5d.4 Abort.** During a guide, the maintainer presses *Abort*, then in another run closes the bridge's window.
Pass: `operator_abort`, then `stopped` with the bridge halted (resume).

## T5e. The laser spot (LASER, Q23–Q25)

**T5e.1 In a frame during an acquisition (Q23).** Silicon, in focus, a low filter, the live video running. Run
`{"v": {"kind": "spot", "sample": {"name": "Si"}, "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%",
"objective": "x100"}, "spot": {"method": "acquisition"}}}`.

Pass:
- the result's video image shows the spot;
- `spot.found`, and `offset_um` agrees with T5b.3 (the shutter) within about 0.5 µm;
- `ls6t spot status` lists it.

If the frame shows no spot (the video freezes or is blanked during acquisitions), record that: then only `shutter`
and `raman` work on this instrument.

**T5e.2 By the Raman signal (Q24).** Find a small isolated feature (dust or a particle, 1–3 µm) on the Si wafer, in
focus. Take a snapshot (`ls6t instrument snapshot`) and check that the feature is among `features` (numbered on the
PNG). Run a spot study with `"method": "raman", "feature_um": [x, y]` from it.

Pass:
- the scan image shows a clear dip (or peak);
- `spot.offset_um` agrees with T5e.1 / T5b.3 within about 0.5 µm at x100.

Then aim: `{"m": {"kind": "move", "move": {"to_um": [x, y]}}}` and a 1 s spectrum. LabSpec first asks whether
the objective it records is the lens on the turret (the snapshot's µm scale comes from it): answer Yes; once, answer
Cancel and check nothing moves. Pass: the Si signal is lowest there (the beam on the dust). Repeat at x10.

**T5e.3 Per objective and drift (Q16, Q25).** Repeat T5e.1 (or T5e.2):
- at x10 and x5 (each objective keeps its own offset);
- at the start and end of the session;
- after changing objective and back;
- after AutoAlignment (T5c).

Record `drift_um` each time. If it often exceeds 1 µm within a day, plan a spot study at the start of each
session (or before aiming at small features).

## T5f. Objective clearance and camera focus (no laser)

**T5f.1 Objective clearance (no laser).** Maintainer at the instrument, watching the gap from the side; x10 first,
then x50 and x100. Needs `stage.z_into_sample` (T3.17). Flat Si wafer, white light, live video. Run
`ls6t instrument clearance`: LabSpec asks to focus on the surface (Yes), then to move Z by hand toward the sample,
slowly, to the closest gap judged safe, never touching (Yes). ls6t reads both Z positions, stores focus -> closest for
that objective (`objective_clearance.json`) and moves Z back to the focus. Pass: `clearance_um` is plausible against
the nominal working distance (x100 about 210 µm, x10 about 10 mm: a few mm is enough there); Z is back at the focus;
`toward_limit_um` is the smaller of half the clearance and the working-distance limit (`toward_from` says which); a
study asking for more toward the sample is refused at validation. Refused and nothing stored: moving away instead, or
a gap over 1.5 x the nominal working distance.

**T5f.2 Camera autofocus (no laser).** Needs T3.17; better after T5f.1. Flat Si with a visible particle, scratch or edge
in the x100 view, white light. Defocus by about 3 µm by hand. Run `{"kind": "focus", "setup": {...}, "focus":
{"autofocus": "camera"}}`: ls6t takes two frames (the camera's noise), then Z steps of about half the depth of field
(x100 2 µm) from the far side toward the sample, within the limit of T5f.1, a fine pass at a quarter step, and the
parabola's best. Pass: `found`, `z_um` within about 0.5 µm of the maintainer's own focus, `texture_at_best` and
`contrast_over_noise` well above 3 and 4, the plot (`focus/camera_focus.png`) a clear peak. Then a clean,
featureless area (no particle in view): refused, "no discernible features", Z back at the start. Record the time per
frame (`timing` of the video command) to size `focus.camera.settle_s`.

## T6. Maps, depth and focus (LASER)

**T6.1 A small map (Q9, Q10).** On the contrast sample: a map study, 5 × 5 points, 1 µm steps, a short exposure, the
peaks that distinguish the regions, `images: true`.

Pass:
- LabSpec shows a map window that fills in as points are measured (**Q10**: record whether the axes show µm and
  what the colour scale represents);
- the stage visits a snake pattern and stays within 2 µm of the start + offsets (compare `spectra` positions or the
  video);
- the result is reliable;
- the map images match what the maintainer sees in LabSpec;
- `<name>.l6s` opens in LabSpec.

**T6.2 Operator stop mid-map.** A 10 × 10 map; the maintainer closes the ls6t window after about 20 points. Pass: the
current acquisition stops; the result has `complete: false`, `reliable: false` and the measured points; the request
is `cancelled`. Resume afterwards.

**T6.3 Depth profile (Q26).** First the Z direction: from focus on Si, move Z by +2 µm
(`ls6t bridge call move motor=Z mode=value value=<Z+2>`) and watch on the video whether the stage moves towards the
objective (the focus goes into the sample) or away. Set `ls6t profile set stage.z_into_sample "+"` (or `"-"`).
Then, with a transparent film of known thickness on Si (a polymer film, or a coverslip on Si), with the maintainer
checking the clearance:
`{"d": {"kind": "depth", "sample": {"name": "film on Si"}, "setup": {...}, "acquire": {"range_cm1": [400, 1700],
"exposure_s": 1}, "depth": {"from_um": -5, "to_um": 30, "step_um": 1, "refractive_index": <n>},
"process": {"peaks": [{"name": "Si", "near_cm1": 520.7}, {"name": "film", "near_cm1": <a strong film line>}]},
"images": true}}` (raise `limits.max_dz_um` if needed).
Pass:
- the film's line rises at the surface and falls where the Si rises;
- `interfaces_by.Si.height.rise` is close to the known thickness (record it, and the nominal value);
- Z is back at the focus at the end;
- LabSpec showed the profile growing.

**T6.4 A larger map (Q11).** 50 × 50 points at the shortest useful exposure (about 0.1–0.5 s), after the maintainer
agrees on the time estimate (`ls6t validate`). Watch LabSpec's memory in Task Manager. Pass: it completes, and
LabSpec stays responsive. Record the time per point (`seconds.total / points_measured`) and peak memory.

**T6.5 Focus scan (Q21).** On silicon, the maintainer focuses by hand and notes Z. Then:

- Defocus by +3 µm (`ls6t bridge call move motor=Z mode=value value=<Z+3>`) and run a `focus` study:
  `{"f": {"kind": "focus", "sample": {"name": "Si"}, "setup": {"laser_nm": 532, "grating": 1800, "filter": "10%",
  "objective": "x100"}}}`. Pass: `focus.found`, the final Z within 0.5 µm of the maintainer's, a smooth curve in the
  plot (`images.focus_scan`), and `focus.axial_fwhm_um` (the depth resolution, remembered per objective, laser and
  hole: record it).
- Repeat from -3 µm.
- Compare with LabSpec's autofocus (`"focus": {"autofocus": "laser"}` in a calibration study).
- On the contrast sample, try a different line (`focus.scan.line_cm1`). Record the step that gives a clean curve at
  x10 and x5 (`step_um` 2–5).

**T6.6 Moves and home (Q22).**
- Run `{"m": {"kind": "move", "move": {"x_um": -20, "y_um": 0}}}`. Pass: the stage moves 20 µm in −X (check on the
  video); the snapshot shows the new spot; `ls6t instrument status` shows the new X afterwards.
- Run a 3-point spectrum study. Pass: its points start from the new spot.
- Run a small map with `z_um` [-2, 2, 2]. Pass: at the end, Z is back at the focus and the stage at the spot
  (`state.json` → `returned`).
- Run a `move` with `"stay": false`. Pass: the request ends back at the previous spot.

**T6.7 Native maps (Q29–Q33).** On the contrast sample (or the Si wafer with dust), x100, a short exposure.

1. A small native map: `{"n": {"kind": "map", "sample": {"name": "..."}, "setup": {...}, "acquire": {"center_cm1":
   520, "exposure_s": 0.2}, "map": {"x_um": [-4, 4, 1], "y_um": [-2, 2, 1], "engine": "native"}, "images": true}}`.
   Pass: LabSpec maps the same area as the point-by-point map of the same study with `"engine": "points"`. Compare
   the two maps' images: same features in the same places, so positions and data order are right (Q29, Q32).
   - If the map lands elsewhere: `ls6t profile set mapping.positions relative`.
   - If its image is scrambled: `mapping.data_order wavelength_fastest`.
   - Repeat until they match.
2. The exposure (Q30): compare `max_counts` with the points map. If they differ by the exposure ratio, set
   `mapping.acq_time exposure`.
3. Stop (Q31): start a 20 × 20 native map and press *Stop* in the bridge's window after a few seconds. Pass: LabSpec
   stops; the request ends `cancelled`.
4. Speed (Q32, Q33): a 50 × 50 native map at 0.1 s. Record `seconds.total`, the time LabSpec took (`seconds_map`)
   and the time to write it out; with SWIFT (`"swift": true`) if available. Put the time per point beyond the
   exposure in `mapping.native_point_s`.

**T6.8 Retract-travel-return on an uneven sample (after T3.17; maintainer watching the gap).** **Only with the x10
or x5 at first** (their working distances leave room for an overshoot); the x100 only once the overshoot margin
below has been measured with them and leaves room (ls6t refuses it otherwise). The bridge measures, at every guarded
autofocus, Z's speed during the search (`measured.z_speed_um_s`), its own poll interval (`poll_s`) and, when it trips,
how long LabSpec's stop takes to halt Z (`stop_s`) and how far Z went past the trip point (`overshoot_um`); the
largest values seen are kept (`%USERPROFILE%\ls6t\autofocus_motion.json`) and the trip point is set earlier by
speed x (poll + stop) x 1.25 (`margin`), so the stage stops at the limit rather than starting to stop there. Until
measured, cautious values are assumed (200 µm/s, 0.5 s, 0.15 s: a 162 µm margin, which rules out the x100). To
measure the stop, make one run trip on purpose with the x10: a small `sample.unevenness_um` (so the bound is close)
and the sample a little below the old focus; record `stop_s` and `overshoot_um`. **First without the laser** (possible before the profile review): white light on,
live video running, `"move": {..., "focus": "video"}`: after the descent LabSpec's video autofocus finishes the
approach. LabSpec's own autofocus search range applies (its script interface neither sets nor reports it): the
bridge reads Z at every poll and stops the autofocus (and Z) the moment it passes the safe bound toward the sample
(`video_autofocus.z_bound_um`: the unevenness plus the toward limit, and never closer than a tenth of the working
distance to a surface that may be `unevenness` higher). Pass: `video_autofocus.accepted` true and the spot in focus
on the video; record `z_seen_um` (how far LabSpec's search actually went: its range, measured) and LabSpec's own
autofocus range setting. If it reports `stopped_at_bound`, LabSpec's range is too wide for this objective: reduce
it in LabSpec. **Then with the laser (LASER)**, as below. Focus by hand on the sample, then a request with
`{"m": {"kind": "move", "sample": {"name": "...", "unevenness_um": <a generous estimate>}, "move": {"x_um": 1000,
"y_um": 0, "snapshot": false}}, "f": {"kind": "focus", "sample": {"name": "..."}, "setup": {...}, "focus": {"scan":
{"from_below": true}}}}`. Pass:
- Z goes `limits.retract_um` (2000 µm) away first (the gap visibly opens), the stage travels, Z comes back to the old
  focus minus the unevenness (`move.travel` in the answer), never closer;
- the focus search starts there and approaches the sample in coarse steps (no larger than about half the depth of
  field: `coarse_step_um`), stops once past the peak, then refines: `focus.found`, and the spot is in focus on the
  video;
- with `stage.retract_for_objective_change` true, an objective change by `instrument set` first lowers the stage by
  `retract_um` (record whether LabSpec then shows the new lens's view unfocused, as expected).

## T7. Series and timing (LASER)

**T7.1 Time series.** `series` with `count` 5 and `interval_s` 20 on silicon. Pass: a profile window fills in
LabSpec (**Q10**); `series.t_s` is about 0, 20, 40, 60, 80; the Si position is stable within 0.3 cm⁻¹.

**T7.2 Estimates.** For each request so far, compare `estimate_min` (from `ls6t validate`) with the real
`seconds.total`. Update the profile's `timing` with the measured values, so estimates and ETAs follow the instrument.

## T8. An LLM app end to end (LASER)

With the LLM app connected (test mode off), in a new chat:

| Step | Ask the LLM | Pass |
|---|---|---|
| T8.1 | "Is the Raman instrument ready? What can it do?" | it calls `study_format`, reports the bridge running and the instrument's lasers, gratings, filters, limits |
| T8.2 | "Check the calibration on the silicon wafer." | it validates and runs a calibration study; the maintainer confirms in LabSpec; it reports the Si position and that it is reliable |
| T8.3 | "Map the sample under the laser, 10 × 10 µm, 1 µm steps, at full power." (on a sensitive sample) | it asks before raising power, or validation refuses the filter above the cap and it explains the limit; nothing exceeds the profile |
| T8.4 | With the bridge stopped in LabSpec: "Take a spectrum." | it reports the bridge is not running and asks to start `ls6t_bridge.vbs` (and runs no command of its own) |
| T8.5 | A run longer than 5 minutes | it reports the expected finish time and stops polling; `study_status` later returns the results |
| T8.6 | "Switch to the 638 nm laser, filter 1 %, turn the internal camera on." | done at once with `instrument set` and `camera_on`, no searching, no commands of its own |

## T9. Robustness

| Step | What | Pass |
|---|---|---|
| T9.1 (**Q12**) | Start a 5-minute map from the LLM app, then quit the app | Either the request finishes (`ls6t status` afterwards: done), or it ends `interrupted` and the instrument is idle. Record which. |
| T9.2 | During a map, the operator cancels the acquisition in LabSpec; then, during another, `ls6t instrument end-bridge` | The acquisition in progress stops; the request ends `cancelled` with `stopped_by: operator`; measured points are kept. `end-bridge` is refused while a request holds the instrument (cancel the request first) |
| T9.3 | Two requests at once (two `ls6t run`) | The second waits as `queued` ("the instrument is busy with ...") and runs after the first |
| T9.4 | Close LabSpec during an idle period, reopen it, restart the bridge | `ls6t status` follows (not running, then running); the next study runs normally |

---

## Results table

| ID | Date | Tester / maintainer | Result | Notes (values, answers to the open questions, times) | Files |
|---|---|---|---|---|---|
| T0.1 | | | | | |
| T0.2 | | | | | |
| T0.3 | | | | | |
| T0.4 | | | | | |
| T1.1 | | | | Q2 (stop button): Q43: | discovery dump, discovery_progress.txt |
| T1.2 | | | | Q15 (shutter confirmed): caps and power_mw per laser: reviewed: | profile.json |
| T1.3 | | | | Q34 (after a LabSpec update only): | labspec_commands.md |
| T1.4 | | | | Q36 (config contents, motors moved?): Q38 (actions logged): | configs\before_tests.ini, script_motor_log.txt |
| T2.1 | | | | | |
| T2.2 | | | | Q3: | |
| T2.3 | | | | | |
| T2.4 | | | | Q2: | |
| T2.5 | | | | | |
| T3.1–T3.17 | | | | Q4: Q5 (turret motorised?): times (grating, laser, XY): Q44 (bridge after end-bridge + video by hand; HORIBA-style stop: passed T3.12–13; T3.14 camera 0 / 1 by restart: shown?): Q45 (grating with each laser; blocking move): first set after start: | |
| T4.0 | | | | already_set marked, setup estimate: | |
| T4.1 | | | | Q6 (x unit, AcqInfo): | |
| T4.2 | | | | Si = ; FWHM = ; Q9: | |
| T4.3 | | | | Q6 (exposure line): | |
| T4.4 | | | | | |
| T4.5 | | | | Q7: | |
| T4.6 | | | | | |
| T4.7 | | | | Q27 (value changed by the zero / the coefficient): | |
| T4.8 | | | | Q28: | |
| T4.9 | | | | Q42 (script found, runs inside the bridge?, asks?, values changed): | |
| T4.10 | | | | filters measured / flagged, per laser: | |
| T4.11 | | | | usable_from_cm1 per laser: | |
| T5 | | | | Q8: | |
| T5b.1 | | | | Q13 (formats saved): Q41 (field of view): | |
| T5b.2 | | | | Q14 (orientation, video-scale µm/px x10 / x100, directions): | |
| T5b.3 | | | | Q15: | |
| T5b.4 | | | | Q16 (repeatability at x100): | |
| T5b.5 | | | | | |
| T5b.6 | | | | Q39 (returned, seconds, stage left at): | |
| T5b.7 | | | | laser on: closes by time / laser_off / window / a move: | |
| T5c.1 | | | | Q17: | |
| T5c.2 | | | | Q18: ratio repeat: | |
| T5c.3 | | | | | |
| T5c.4 | | | | | |
| T5d.1 | | | | Q19 period_s: | |
| T5d.2 | | | | Q20: | |
| T5d.3 | | | | | |
| T5d.4 | | | | | |
| T5e.1 | | | | Q23: | |
| T5e.2 | | | | Q24 (offset raman vs video): | |
| T5e.3 | | | | Q16 offsets x100 / x10 / x5: Q25 drift: | |
| T5f.1 | | | | clearance x10 / x50 / x100 (µm), toward limit after: | |
| T5f.2 | | | | camera focus vs hand focus (µm), texture, featureless refused, s per frame: | |
| T6.1 | | | | Q10: | |
| T6.2 | | | | | |
| T6.3 | | | | Q26 (direction, thickness found vs known): | |
| T6.4 | | | | Q11: s/point, memory | |
| T6.5 | | | | Q21: Z found vs maintainer, axial FWHM, step at x10/x5: | |
| T6.6 | | | | Q22: | |
| T6.7 | | | | Q29 positions: Q30 exposure: Q31 stop: Q32 order, write-out time: Q33 SWIFT, s/point: | |
| T6.8 | | | | retract and descent heights, coarse step, focus found: | |
| T7.1 | | | | | |
| T7.2 | | | | timing updated: | |
| T8.1–T8.6 | | | | | |
| T9.1–T9.4 | | | | Q12: | |

**Before any unattended use** (not part of version 1): all of T0–T9 pass, the answers to the open questions are
reflected in the code and in this document, and the maintainer agrees on the limits in the profile.
