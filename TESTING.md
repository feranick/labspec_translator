# Testing labspec-translator on the instrument

This is the plan for the first sessions on the real instrument (XploRA, LabSpec 6, Windows 11). It is carried out by
two people:

- the **maintainer**: the person responsible for the instrument, who knows its hardware, its laser safety rules and
  its normal use;
- the **tester**: the person installing ls6t, who runs the commands and records the results.

Everything ls6t does without the instrument is already tested: `pytest` covers format, protocol, analysis, full runs,
stops and cancel on the simulator, and `ls6t bench` gives known answers. What remains can only be learnt on the real
LabSpec and hardware. The plan goes from no laser and no motion to Claude driving a full map, in stages. Each stage
starts only when the previous one passed.

Version under test: 2026.11.19.3 (bridge protocol 1).

---

## Ground rules

1. **The maintainer is at the instrument for every test** in this plan, with the bridge's window and LabSpec in view,
   ready to stop. Nothing here is run unattended.
2. **The facility's laser safety rules apply as usual.** ls6t does not replace interlocks, eyewear or enclosure rules.
   Stages marked **LASER** fire the laser.
3. **Start gentle**: the lowest useful filter (1 % or 10 %), short exposures, silicon first. Raise power only with the
   maintainer's agreement.
4. **Stage moves**: start with the lowest-magnification objective (x10, longest working distance) and moves of a few
   µm. Before any Z move, the maintainer checks the clearance between objective and sample.
5. **How to stop**, in order of preference:
   - close the ls6t window in LabSpec (stops the current command, halts the bridge);
   - cancel the acquisition in LabSpec;
   - LabSpec's script stop button (ends the bridge);
   - the instrument's own stop or emergency controls.

   The maintainer should try each of them during the plan (T2, T3, T4).
6. **Stop the plan** at any unexpected motion, a move beyond what was asked, a dialog nobody understands, or LabSpec
   not responding. Record what happened (see [Recording](#recording)) before continuing.

## What to prepare

- The instrument PC with ls6t installed as in README *Installation on Windows 11* (steps 1–6), and the Claude desktop
  app connected (step 8).
- **Samples**:
  - a silicon wafer piece (calibration: 520.7 cm⁻¹);
  - polystyrene (a bead or film; reference lines 620.9, 1001.4, 1031.8, 1155.3, 1583.1, 1602.3, 2852.4, 2904.5,
    3054.3 cm⁻¹, ASTM E1840);
  - a sample with lateral contrast for maps, ideally one the maintainer already knows: graphene on SiO₂/Si, a
    patterned Si/SiO₂ chip, or a polymer bead on Si;
  - a transparent film on silicon for a depth profile (optional).
- The maintainer's usual settings for these samples (laser, grating, filter, objective, exposure). They are the
  reference for what ls6t should reproduce.
- About **4.5 hours** for stages T0–T6 (including T5b–T5d; T5c needs the maintainer to run AutoAlignment), and 1 hour for T7–T9 (they can be another day).

## Open questions this plan answers

The vendor manual (2018) leaves these open. Each is answered by the test listed.

| ID | Question | Test |
|---|---|---|
| Q1 | Can LabSpec scripts use `Scripting.FileSystemObject` and `Scripting.Dictionary` (file access)? Without it the bridge cannot work as designed. | T1.1 |
| Q2 | Does the non-blocking window (`Message(..., 8)`) open, report -1 while open and 0 once closed? Does `MessageEx("", 14)` see LabSpec's script stop button? | T1.1, T2.3, T2.4 |
| Q3 | Does LabSpec stay usable (live video, menus) while the bridge loops? | T2.2 |
| Q4 | Do `MoveMotor(..., +10)` and `GetMotorStatus` behave as assumed? Does `MoveXY` return at once or block until the move ends? | T3 |
| Q5 | What are the motor names and position labels of the filter wheel and the objective turret? Is the turret manual (LabSpec prompts the operator)? | T1.2, T3.2 |
| Q6 | In which unit does `GetValue(id, "XYData")` give x (nm or cm⁻¹)? How is `AcqInfo` laid out (a header row)? Where is the exposure that auto-exposure chose? | T4.1, T4.3 |
| Q7 | Does `GetAcqID` report -2 after a cancel, and does that state persist into the next acquisition? | T4.5 |
| Q8 | Does autofocus leave a spectrum behind (a data ID), and does it keep the new focus or restore the old one? | T5 |
| Q9 | Does `Save(id, "...l6s", "")` write a file LabSpec reopens? | T4.1, T6.1 |
| Q10 | Do the live views (`Map`, `Profile`) appear and fill in? Are the axis units right ("mkm")? What do Map's From/To limits do? | T6.1, T7.1 |
| Q11 | How does LabSpec's memory behave with large maps built point by point? | T6.4 |
| Q12 | Does a request survive closing the Claude app (the runner process breaking away from the app's job)? | T9.1 |
| Q13 | Video: does `Video(2)` return the live frame's id from a script, and does `Exec(id, 3)` clone it? Which image formats does `Save` write for a frame? | T1.1, T5b.1 |
| Q14 | Video pixels: which axis varies fastest, how are colours packed (COLORREF 0x00BBGGRR or RGB), and are the axes stage coordinates in µm? | T5b.2 |
| Q15 | Which motor is the laser shutter, and do `PutValue(0, "ShutterMode", 1)` plus moving it to 1 / 0 open and close it? | T1.2, T5b.3 |
| Q16 | How far is the beam from LabSpec's laser position, per objective (`video.laser_offset_um`)? | T5b.4 |
| Q17 | Does this XploRA have HORIBA's AutoAlignment module (Maintenance tab)? Can its *Current status* be read from outside (Windows UI automation, read-only)? | T5c.1 |
| Q18 | Does `Message(text, 1)` (OK/Cancel) return 0 for OK and wait while the operator runs AutoAlignment in the same LabSpec? | T5c.2 |
| Q19 | Does LabSpec's dynamic `Dialog` stay open, update and answer Done/Abort while short acquisitions run? How long is one reading (`period_s`) at 0.2 s exposure? | T5d.1 |
| Q20 | Is the guided loop fast enough for a person turning a knob (focus, power): does STOP come before the operator has gone too far? | T5d.2–T5d.3 |

---

## Recording

For each test, record one line (in a copy of the table at the end, or in a notebook):

`ID | date and time | tester, maintainer | pass / fail / partial | what was seen | files`

- **Files to keep**: the discovery dump, the request folders (`%USERPROFILE%\ls6t_runs\run_...`), and
  `%USERPROFILE%\ls6t\bridge\heartbeat.txt` when something looks wrong.
- **Screenshots** of LabSpec dialogs nobody expected.

After the session, the tester adds a dated entry to `planning.md` (*State log*). It says what passed, the answers
to Q1–Q12, and the measured times, and lists anything to change.

---

## T0. Software on the instrument PC (no LabSpec needed)

| Step | Command (PowerShell, venv active) | Pass |
|---|---|---|
| T0.1 | `ls6t status` | versions listed; `settings.LS6T_MODE` is `labspec`; `bridge.running` false |
| T0.2 | `ls6t bench` | `"failed": []` (all six benchmarks pass on this PC) |
| T0.3 | `pytest -q` | all tests pass |
| T0.4 | `ls6t bridge install` | two scripts written to `%USERPROFILE%\ls6t\vbs`; the bridge folder exists |

## T1. Discovery (no laser, no motion)

**T1.1 Run the discovery script.** In LabSpec: *Processing > Scripts*, add `ls6t_discover.vbs`, run it. A small window
appears and closes by itself. Then a message box gives the dump's path.

Pass:
- the dump file exists (**Q1**: file access works);
- in the dump: `selftest.pause=1`, `selftest.popup_open=-1`, `selftest.popup_closed=0`, `selftest.stop_button=0`,
  `selftest.status_bar=1` (**Q2**);
- `system=XploRA` (or the actual model), and `detector_temp_c` close to LabSpec's display;
- `video.id_now` > 0 if the live video was running (start it first to check), and no `video.error` (**Q13**).

If the dump is missing, **stop**: file access is not available to scripts, and the transport must change before
anything else (see planning.md). Note any error message LabSpec showed.

**T1.2 Import and review the profile.**

```powershell
ls6t profile import "<the dump's path>"
notepad $HOME\ls6t\profile.json
```

With the maintainer, go through:
- `motors`: the names found for laser, grating, **filter** and **objective** (**Q5**). The import tries common names.
  If the filter wheel or turret was not found, look for its name in the dump (`motor.<name>.present=1`) and set
  `motors.filter` / `motors.objective` by hand.
- `lasers`, `gratings`, `filters` (each label with its transmission in %), `objectives`, `holes_um`, `slits_um`: as on
  the instrument. Set any filter percentage the import could not read.
- **Limits, decided by the maintainer**: `max_filter_pct` per laser, `sensitive_max_filter_pct`, `max_xy_um` (start
  with 500), `max_dz_um` (start with 20), `max_exposure_s`, `max_accumulations`, `max_points`, `max_minutes`,
  `stage.x_um` / `stage.y_um` if wanted, `detector.max_temp_c`.
- `manual_objective`: true if LabSpec asks the operator to turn the turret.
- `motors.laser_shutter` (**Q15**): the import proposes a motor named like a shutter. The maintainer confirms it
  is the **laser** shutter, not the detector's. Leave it `null` until then: snapshots with the laser are refused
  without it.

Pass: `ls6t format` shows the instrument's lasers, gratings, filters and objectives, and the limits agreed.

## T2. The bridge, idle (no laser, no motion)

**T2.1 Start.** In LabSpec, add and start `ls6t_bridge.vbs`.

| Pass | |
|---|---|
| The ls6t window appears; the status bar says the bridge is running | |
| `ls6t status`: `bridge.running` true, `heartbeat.state` idle, `age_s` below 2 | |
| `ls6t instrument status`: laser, grating, filter, objective, X/Y/Z, detector temperature match LabSpec's display | |

**T2.2 LabSpec while the bridge runs (Q3).** The maintainer uses LabSpec normally for a minute: live video, menus,
the spectrum window, moving the stage with the joystick or software. Pass: LabSpec responds normally, and
`ls6t status` still shows a fresh heartbeat afterwards. Note any sluggishness.

**T2.3 Halt and resume (Q2).** The maintainer closes the ls6t window.

- `ls6t status`: `heartbeat.state` becomes `halted` within 2 s.
- `ls6t bridge call move motor=Grating mode=value value=<current grating>` is refused ("halted by the operator").
- `ls6t instrument resume`: LabSpec asks whether ls6t may control the instrument again. The maintainer answers
  **No**: the bridge stays halted. Repeat and answer **Yes**: the window reappears and the state is `idle`.

**T2.4 The script stop button (Q2).** The maintainer presses LabSpec's stop button for scripts. Pass: the window
closes, `heartbeat.state` is `stopped`, and `ls6t status` shows the bridge not running. Start the bridge again.

**T2.5 Stale stop.** While idle: `ls6t bridge call ping`, then create an old stop request by running
`ls6t bridge call move motor=Grating mode=value value=<current> --timeout 0.1` (it times out on purpose), then
`ls6t bridge call ping`. Pass: the last call answers normally.

## T3. Motion and set-up (no laser)

Use the x10 objective, the stage at a safe height, and small moves. Run each command, watch the hardware, then
compare the answer's `value` / `label` with LabSpec. Replace names and labels with those in the profile.

| Step | Command | Pass |
|---|---|---|
| T3.1 | `ls6t bridge call move motor=Filter mode=label value=1%`, then each other label | the wheel moves; `label` read back equals the label asked |
| T3.2 | `ls6t bridge call move motor=Objective mode=label value=x50` (**Q5**) | motorised: it turns and reads back. Manual: LabSpec asks the operator to change it; after confirming, the read-back is right. Record which. |
| T3.3 | `ls6t bridge call move motor=Grating mode=value value=600`, then back | the grating moves; `value` read back; note the time (`seconds`) |
| T3.4 | `ls6t bridge call move motor=Laser mode=value value=532` (and the others) | the laser line changes in LabSpec (no emission); read-back right; note the time |
| T3.5 | `ls6t bridge call move motor=Hole mode=value value=300` and `motor=Slit` | read-back right (or record the error if these are not motorised) |
| T3.6 | Note X, Y from `ls6t instrument status`; `ls6t bridge call move_xy x_um=<X+10> y_um=<Y>`, then back | the stage moves 10 µm in +X (check direction on the video); read-back within 1 µm; note whether the call returned before the move ended (**Q4**) |
| T3.7 | `ls6t bridge call move motor=Z mode=value value=<Z+2>`, then back (maintainer checks clearance first) | Z moves 2 µm; read-back right |
| T3.8 | Stop during a move: `ls6t bridge call move_xy x_um=<X+1000> y_um=<Y> --timeout 60`, and the maintainer closes the ls6t window while it moves | the stage stops; the answer says `stopped_by: operator`; the bridge is halted. Resume (T2.3) and move back. |
| T3.9 | `ls6t bridge call safe` | answers ok; nothing moves |

Also record the times of T3.3, T3.4 and T3.6. They replace the defaults in the profile's `timing` (`grating_s`,
`laser_s`, `xy_um_per_s`, `xy_settle_s`), which feed the time estimates.

## T4. First light: silicon (LASER)

The maintainer places the Si wafer, chooses the objective (x50 or x100), focuses by hand, and sets a low filter.

**T4.1 One spectrum by hand.**

```powershell
ls6t bridge call acquire --laser exposure_s=1 accumulations=1 mode=10 from_nm=547.16 to_nm=547.16 out=C:\Users\<you>\ls6t\t41.txt
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
- the result has `reliable: true`, `calibration.measured_cm1` within 1 cm⁻¹ of 520.7 (record the value),
  `checks.setup.pass` and `checks.detector_temp_c.pass` true;
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

## T5. Autofocus (LASER, Q8)

On silicon, defocus by about 10 µm by hand. Run a calibration study with `"focus": {"autofocus": "laser"}`. Repeat
with `video` and `raman` if the instrument has them.

Pass:
- the answer's `z_um` changes, and LabSpec's Z shows the new focus after the study (not restored);
- the Si signal is close to the hand-focused one;
- the calibration spectrum is the real one: `max_counts` is plausible, and the file's id differs from any autofocus
  spectrum LabSpec may have shown.

Record whether autofocus left a spectrum in LabSpec.

## T5b. Video snapshots (T5b.1–T5b.2 no laser; T5b.3–T5b.4 LASER)

**T5b.1 A frame (Q13).** Start the live video in LabSpec, with the Si wafer or the contrast sample in focus, then run
`ls6t instrument snapshot`. Pass: `ok`; `frame.png` shows the same view as LabSpec's video window; `um_axes` is true
and `field_um` matches the objective's field of view. Record which formats appear under `native`, i.e. those LabSpec
saved. Repeat with the live video stopped: the bridge starts it, takes the frame and stops it again.

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

Then answer No to the question once and check nothing opens. Also check the 785 nm laser, if present: the camera
may not see it, which gives a "spot not found" warning.

**T5b.4 The beam's offset (LASER, Q16).** Repeat T5b.3 three times per objective (x10, x50, x100). Record
`measured_offset_um` each time. Pass: the three agree within about 0.5 µm at x100 (the spot's size sets the
precision). Put the mean in the profile's `video.laser_offset_um`. If the objectives disagree by more than 1 µm, note
the values per objective (the profile has one offset: planning.md). Take a last snapshot with the laser: the red
cross now sits on the spot.

**T5b.5 In a calibration study.** Run the calibration study with `"video": {"after": true}`. Pass: `video.before`
and `video.after` are in the result and the images, and the Si result is as in T4.2.

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

## T6. Maps (LASER)

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

**T6.3 Depth profile.** Optional, with a film on Si. A map with `z_um` [-10, 10, 1] (maintainer checks the
clearance at -10 first). Pass: the Si signal peaks near dz = 0; Z returns within 1 µm of the start at the end of the
next study.

**T6.4 A larger map (Q11).** 50 × 50 points at the shortest useful exposure (about 0.1–0.5 s), after the maintainer
agrees on the time estimate (`ls6t validate`). Watch LabSpec's memory in Task Manager. Pass: it completes, and
LabSpec stays responsive. Record the time per point (`seconds.total / points_measured`) and peak memory.

## T7. Series and timing (LASER)

**T7.1 Time series.** `series` with `count` 5 and `interval_s` 20 on silicon. Pass: a profile window fills in
LabSpec (**Q10**); `series.t_s` is about 0, 20, 40, 60, 80; the Si position is stable within 0.3 cm⁻¹.

**T7.2 Estimates.** For each request so far, compare `estimate_min` (from `ls6t validate`) with the real
`seconds.total`. Update the profile's `timing` with the measured values, so estimates and ETAs follow the instrument.

## T8. Claude end to end (LASER)

With the Claude desktop app connected (README step 8):

| Step | Ask Claude | Pass |
|---|---|---|
| T8.1 | "Is the Raman instrument ready? What can it do?" | Claude calls `study_format`, reports the bridge running and the instrument's lasers, gratings, filters, limits |
| T8.2 | "Check the calibration on the silicon wafer." | Claude validates and runs a calibration study; the maintainer confirms in LabSpec; Claude reports the Si position and that it is reliable |
| T8.3 | "Map the sample under the laser, 10 × 10 µm, 1 µm steps, at full power." (on a sensitive sample) | Claude asks before raising power, or validation refuses the filter above the cap and Claude explains the limit; nothing exceeds the profile |
| T8.4 | With the bridge stopped in LabSpec: "Take a spectrum." | Claude reports the bridge is not running and asks to start `ls6t_bridge.vbs` |
| T8.5 | A run longer than 5 minutes | Claude reports the expected finish time and stops polling; `study_status` later returns the results |

## T9. Robustness

| Step | What | Pass |
|---|---|---|
| T9.1 (**Q12**) | Start a 5-minute map from the Claude app, then quit the app | Either the request finishes (`ls6t status` afterwards: done), or it ends `interrupted` and the instrument is idle. Record which. |
| T9.2 | During a map, stop the bridge with LabSpec's script stop button | The acquisition in progress stops; the request ends `cancelled` with `stopped_by: operator` (or `failed`, bridge gone, within `LS6T_HEARTBEAT_S` if LabSpec ended the script outright: record which); measured points are kept |
| T9.3 | Two requests at once (two `ls6t run`) | The second waits as `queued` ("the instrument is busy with ...") and runs after the first |
| T9.4 | Close LabSpec during an idle period, reopen it, restart the bridge | `ls6t status` follows (not running, then running); the next study runs normally |

---

## Results table

| ID | Date | Tester / maintainer | Result | Notes (values, answers to Q1–Q12, times) | Files |
|---|---|---|---|---|---|
| T0.1 | | | | | |
| T0.2 | | | | | |
| T0.3 | | | | | |
| T0.4 | | | | | |
| T1.1 | | | | Q1: Q2: | |
| T1.2 | | | | Q5: | |
| T2.1 | | | | | |
| T2.2 | | | | Q3: | |
| T2.3 | | | | | |
| T2.4 | | | | | |
| T2.5 | | | | | |
| T3.1–T3.9 | | | | Q4: Q5: times: | |
| T4.1 | | | | Q6 (x unit, AcqInfo): | |
| T4.2 | | | | Si = ; Q9: | |
| T4.3 | | | | Q6 (exposure line): | |
| T4.4 | | | | | |
| T4.5 | | | | Q7: | |
| T4.6 | | | | | |
| T5 | | | | Q8: | |
| T5b.1 | | | | Q13 (formats saved): | |
| T5b.2 | | | | Q14 (order, packing, axes): | |
| T5b.3 | | | | Q15: | |
| T5b.4 | | | | Q16 offsets x10 / x50 / x100: | |
| T5b.5 | | | | | |
| T5c.1 | | | | Q17: | |
| T5c.2 | | | | Q18: ratio repeat: | |
| T5c.3 | | | | | |
| T5c.4 | | | | | |
| T5d.1 | | | | Q19 period_s: | |
| T5d.2 | | | | Q20: | |
| T5d.3 | | | | | |
| T5d.4 | | | | | |
| T6.1 | | | | Q10: | |
| T6.2 | | | | | |
| T6.3 | | | | | |
| T6.4 | | | | Q11: s/point, memory | |
| T7.1 | | | | | |
| T7.2 | | | | timing updated: | |
| T8.1–T8.5 | | | | | |
| T9.1–T9.4 | | | | Q12: | |

**Before any unattended use** (not part of version 1): all of T0–T9 pass, the answers to Q1–Q12 are reflected in
the code and in this document, and the maintainer agrees on the limits in the profile.
