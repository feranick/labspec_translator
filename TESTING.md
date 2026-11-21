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

Version under test: 2026.11.20.45 (bridge protocol 1).

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
   (ChatGPT desktop / Codex, Gemini CLI, Claude with a terminal) must not drive LabSpec by scripts, COM/ActiveX or
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
| Q15 | Is `LaserShutter` the laser shutter, and do `PutValue(0, "ShutterMode", 1)` (manual) plus moving it On / Off open and close it? **Known: a motor `LaserShutter` with Off/On**; that it is the laser's is for the maintainer to confirm. | T1.2, T5b.3 |
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
| Q34 | Which commands does the installed LabSpec offer beyond the 2018 manual? **Known:** 15 methods in the `NFActiveX.ocx` type library (planning.md); HORIBA's scripts also use `Dialog`, `OperVar`, `GUI`, `SetSingleScriptParam`, `SetDetectorZone`, `SetAutoExposure`, `MoveXY`, `MapEx`, `Send`. Re-run `ls6t-typelib` after a LabSpec update. | T1.3 |
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

After the session, the tester adds a dated entry to `planning.md` (*State log*). It says what passed, the answers to
Q1–Q45, and the measured times, and lists anything to change.

---

## Running the tests from the chat (test mode)

The tests can also be run by talking to the LLM app (Claude, ChatGPT / Codex, Gemini CLI) instead of typing the
commands. The LLM reads each test from this file, tells the maintainer what to do in LabSpec, checks what it can
itself (the bridge's state, read-backs, spectra, snapshots), asks about what only a person can see, runs the
commands, and records the result and the answers to the open questions in the same log and report.

1. Turn test mode on for the app (it is off by default; normal use never offers these commands):

   ```powershell
   ls6t connect claude --test         # or codex / gemini; add --sim to practise on the simulator first
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
| T0.3 | `ls6t connect check` | each connected app's ls6t entry starts and lists the tools (from source: also `pytest -q`) |
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

Then import the edited file, which checks it and marks it reviewed:

```powershell
ls6t profile import "$HOME\ls6t\profile.proposed.json"
```

Pass: a laser with `nm`, `max_filter_pct` or `power_mw` blank is refused (try once); after the import, `ls6t status`
shows `profile.reviewed: true`; `ls6t format` shows the instrument's choices and the limits agreed.

**T1.3 The installed LabSpec's commands (Q34).** Done on 2026-11-20 from the `.ocx` (planning.md). Repeat only after
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

**T2.2 LabSpec while the bridge runs (Q3).** The maintainer uses LabSpec normally for a minute: live video, menus,
the spectrum window, moving the stage with the joystick or software. Pass: LabSpec responds normally, and
`ls6t status` still shows a fresh heartbeat afterwards. Note any sluggishness.

**T2.3 Halt and resume (Q2).** The maintainer presses **Halt** in the ls6t window (titled "ls6t: in control";
before .44 its button was Cancel and its title LabSpec's "Non blocking").

- The window "ls6t: HALTED" appears at once (also during a move or the start-up warm-up); `ls6t status`:
  `heartbeat.state` becomes `halted` once LabSpec's current call ends. Its button **End bridge** ends the bridge.
- `ls6t instrument set --grating <current grating>` is refused ("halted by the operator").
- `ls6t instrument resume`: LabSpec asks whether ls6t may control the instrument again. The maintainer answers
  **No**: the bridge stays halted. Repeat and answer **Yes**: the HALTED window closes and "ls6t: in control"
  reappears, saying ls6t RESUMED (nothing more to answer); the state is `idle`. Record the answer's
  `picked_up_after_s` (until the bridge took the command up), `prompt_shown_after_s` (until the question was put on
  screen) and `answer_after_s` (on screen until answered): on .42/.43 a resume took 58.8 and 99.6 s with the bridge
  idle and halted. From .45 the HALTED window is closed while the question is asked.
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
| T3.2 | `ls6t instrument set --objective x5` (**Q5**), then back | LabSpec shows ls6t's question: change the objective BY HAND to x5, check the clearance, OK. After OK, LabSpec's objective reads `x5_VIS` and the answer's `objective.source` says it was changed by hand and confirmed. Cancel once: nothing changes. Record whether LabSpec also shows its own prompt for the manual turret after ls6t's (a second question). |
| T3.3 | `ls6t instrument set --grating 600`, then back | the grating moves; read back; note `seconds` |
| T3.4 | `ls6t instrument set --laser-nm 532` (and 638, 785) | the laser line changes in LabSpec (no emission); read back; note the time |
| T3.5 | `ls6t instrument set --hole 300 --slit 100` | read back (or the error, if not motorised) |
| T3.6 | Note X, Y from `ls6t instrument status`; `ls6t bridge call move_xy x_um=<X+10> y_um=<Y>`, then back | the stage moves 10 µm in +X (direction on the video); read back within 1 µm; note whether the call returned before the move ended (**Q4**) |
| T3.7 | `ls6t bridge call move motor=Z mode=value value=<Z+2>`, then back (maintainer checks clearance first) | Z moves 2 µm; read back |
| T3.8 | Stop during a move: `ls6t bridge call move_xy x_um=<X+1000> y_um=<Y> --timeout 60`, and the maintainer closes the ls6t window while it moves | the stage stops; the answer says `stopped_by: operator`; the bridge is halted. Resume (T2.3) and move back. |
| T3.9 | `ls6t bridge call safe` | answers ok; nothing moves |
| T3.10 | Polarisers: `instrument set` with `laser_pol` Vertical and `raman_pol` None (from the chat, or `ls6t bridge call move motor=LaserPol mode=label value=Vertical`), then back | each moves and reads back; `ls6t instrument status` shows `laser_pol` / `raman_pol` |
| T3.11 | Cameras (**Q44**: concluded 2026-11-20, ls6t does not switch cameras on this instrument). With the video stopped and the camera chosen in LabSpec: `ls6t instrument camera-on`. Then `camera-on --camera <the other one>`, and `camera-off` | The first starts the live video on LabSpec's camera (`video_started` 1). The other two are refused, nothing changes, the bridge keeps running, and the answers say what to do (`end-bridge`, change the video and camera in LabSpec, start the bridge). Then do that once: after `end-bridge` and the change by hand, the restarted bridge answers `ls6t instrument status` (if not: record it, restart LabSpec) |
| T3.12 | Stopping the video HORIBA's way (**Q44**; optional; maintainer ready to restart LabSpec; save work first). HORIBA's own service scripts (`SERVICE\StandardMapping XploRA.vbs`, `SWIFTMapping XploRA.vbs`) stop the video with `Video STOP_VIDEO`, then poll `GetMotorStatus("Video", id)` until 0, with no other video call in between; ls6t's earlier stop called `Video(GET_VIDEO_ID)` right after, and hung. With the video started by ls6t (`camera-on`): `ls6t profile set video.stop_from_script true`, then `ls6t instrument camera-off` once | It returns within 15 s with `live` 0 and `video_stop_status` 0, LabSpec's video is stopped, and the bridge answers `ls6t instrument status`. Record `video_stop_s`. If it hangs: restart LabSpec, set `video.stop_from_script` back to false, and record it (then no script way to stop the video is known) |
| T3.13 | After the bridge's own stop (**Q44**; maintainer ready to restart LabSpec; save work first). **Done 2026-11-20 on .26:** the stop returned (`video_stop_s` 3.38) but the next `status` hung inside LabSpec. From .27 the bridge, after its own stop, no longer asks LabSpec anything about the video (it knows it is off and which camera was selected) until it starts the video itself, and it waits at least 5 s after the stop before starting it. With `video.stop_from_script` true: `camera-on`, `camera-off`, then `ls6t instrument status` twice, then `camera-on` again | `status` answers at once with `live_video` 0 and `video_known_off` 1; the second `camera-on` starts the video after the 5 s wait, and `status` then shows `live_video` 1 with `video_known_off` 0. A stop by hand takes about 3–4 s to take effect (Nicola, 2026-11-20), like `video_stop_s` 3.38: the 5 s wait counts from when the stop is reported done |
| T3.14 | Switching cameras by a video restart (**Q44**; maintainer ready to restart LabSpec; save work first). **Passed before (2026-11-20, .28):** T3.12 (HORIBA's stop, 3.38 s) and T3.13 (no video query after the stop; stop then start, 3.09 s). With the live video on the internal camera (1): `ls6t profile set video.switch_camera true`, then `ls6t instrument camera-on --camera top` (camera 0 first: it was ignored before, even with the video stopped and after a LabSpec restart). Then `--camera internal`, then `--camera top` once more | Each switch: the video stops (`camera_restart` 1, `video_stop_s`), about 5 s pass, the video starts again, and the answer is `ok` with `camera_after` the one asked **and the live view shows that camera** (look). If camera 0 is ignored again, the answer says "LabSpec ignored the switch" with `camera_set_returned`: record it, and that camera stays a manual step (`video.switch_camera` back to false). No hang at any point; `status` answers between switches |
| T3.15 | Motors and read-back. **Passed on .32/.33 (2026-11-20):** every move `ok` with the value asked; `settle_s` 0.0 throughout; whole moves: filter 1.0–1.1 s (36.6 s for the first move after the bridge started), laser 1.9–2.8 s, pinhole 2.1–5.7 s (the slowest), slit 1.7–2.9 s, grating 0.2–0.3 s (too short: the grating was seen moving for seconds in .29). From .34 each move also waits for all motors (`GetMotorStatus("", 0)`, the manual's WaitForAllMotors) and reports `move_call_s`, `wait_s`, `all_wait_s`. Repeat `ls6t instrument set --grating 600`, then `--grating 1800`, watching the grating | The answer's `seconds` now covers the grating's real motion (`all_wait_s` of several seconds), or, if it is still 0.2 s while the grating visibly moves, record `move_call_s` / `wait_s` / `all_wait_s`: then LabSpec reports no motor busy during a grating change. Also note the first move's time after each bridge start (36.6 s once): always slow, or once? |
| T3.16 | The grating (**Q45**). **Seen (2026-11-20):** every grating move answers in 0.02–0.05 s, blocking or not (`move_id` 0, no wait), while the laser's status wait works (1.7–1.9 s); LabSpec accepted 785 nm with 2400 gr/mm; its SpMax per grating reads as nm (600: 1138, 1200: 1112, 1800: 935, 2400: 690), which ls6t now uses (from .38 every profile gets it from the bridge's inventory). Start the bridge and wait until `ls6t status` shows it idle. Someone **at the instrument watches and listens to the turret** during: `ls6t instrument set --grating 600`; `--grating 2400`; `--laser-nm 785 --grating 1200` (from 2400: laser and grating together); `--laser-nm 637.75 --grating 1800`; then back to the start. After each grating change, take two dark acquisitions in a row, **with someone in the room**: a request with two `background` studies, `"background": {"name": "dark_a", "laser_off": "operator"}` (then `dark_b`), `"setup": {..., "filter": "0.1%"}` (required while the profile is not reviewed), `"acquire": {"center_cm1": 520, "exposure_s": 1}`. Nothing in ls6t keeps the laser off here (LabSpec opens its shutter as for any acquisition): the person at the instrument physically blocks the beam (or switches the laser off at its key) before pressing OK; LabSpec's prompt says so and must not be confirmed from a remote session. Compare their `seconds.acquire` | **First of all, the slow first move** (24.1 s on the grating with the bridge idle, 36.6 s on the filter another time; both right after a bridge restart, LabSpec perhaps not restarted). Two cases, each answer's `bridge_move_n` saying which move of the bridge it was: (a) **restart only the bridge** (`ls6t instrument end-bridge`, then start `ls6t_bridge.vbs`; LabSpec keeps running; can be done remotely) and make a grating move first: slow again means the delay belongs to the bridge's first command (then ls6t can warm it up at start); fast means it belongs to LabSpec's own start; (b) **restart LabSpec and the bridge**, grating first, someone watching whether the turret turns then, and again on the later changes (they answer in 0.06–0.09 s; `all_wait_s` is 0.0 throughout). A third case was 31.7 s (.40, grating 1200 → 600, all of it in `wait_s`). From .42 the bridge asks each motor's status once at start, nothing moving (`ping`'s `warmup`, and `%USERPROFILE%\ls6t\warmup.txt`, give each motor's time): if the first-command delay is spent there, the first real move is fast; watch the turret during the bridge's start too. For each change: did the turret move when `set` answered, or only at the first acquisition (then the first `seconds.acquire` is longer than the second by the turret's time)? The answers carry `reach` (2400 with 785 nm: no Raman shift; 1800 with 785 nm: up to about 2040 cm-1) and the note that LabSpec reports a grating at once. A study asking beyond SpMax is refused at validation. Also record `timing.status_before_s` of the first `set` after a bridge start |

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

**T5b.2 Orientation and colours (Q14).** Choose a feature that is easy to recognise (an edge, a corner, a coloured
object).

- Take a snapshot, then `ls6t bridge call move_xy x_um=<X+10> y_um=<Y>`, then take another. Pass: the feature moves
  by 10 µm in the PNG, in the same direction as in LabSpec's video window.
- Do the same in Y.
- Pass: colours look as in LabSpec.
  - Red and blue swapped: set `video.packing` to `rgb`.
  - The image scrambled into stripes: set `video.order` to `y_fastest`.
  - If neither fixes it, keep `raw` (the `.video.txt` file) for analysis.

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
| T3.1–T3.16 | | | | Q4: Q5 (turret motorised?): times (grating, laser, XY): Q44 (bridge after end-bridge + video by hand; HORIBA-style stop: passed T3.12–13; T3.14 camera 0 / 1 by restart: shown?): Q45 (grating with each laser; blocking move): first set after start: | |
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
| T5b.2 | | | | Q14 (order, packing, axes): | |
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
| T6.1 | | | | Q10: | |
| T6.2 | | | | | |
| T6.3 | | | | Q26 (direction, thickness found vs known): | |
| T6.4 | | | | Q11: s/point, memory | |
| T6.5 | | | | Q21: Z found vs maintainer, axial FWHM, step at x10/x5: | |
| T6.6 | | | | Q22: | |
| T6.7 | | | | Q29 positions: Q30 exposure: Q31 stop: Q32 order, write-out time: Q33 SWIFT, s/point: | |
| T7.1 | | | | | |
| T7.2 | | | | timing updated: | |
| T8.1–T8.6 | | | | | |
| T9.1–T9.4 | | | | Q12: | |

**Before any unattended use** (not part of version 1): all of T0–T9 pass, the answers to the open questions are
reflected in the code and in this document, and the maintainer agrees on the limits in the profile.
