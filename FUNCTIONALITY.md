# labspec-translator (ls6t): what it can do

This is the complete list of functions in version 2026.11.20.20. It covers:

- what a study can ask for;
- the tools an LLM sees;
- the commands a person runs;
- what the LabSpec bridge does;
- what ls6t checks and remembers.

How it all fits together: [README.md](README.md). Installation: [INSTALL.md](INSTALL.md). Tests on the instrument:
[TESTING.md](TESTING.md).

Everything below runs on the built-in simulator and is covered by tests and benchmarks. Nothing has run on the
instrument yet: TESTING.md lists what remains to be confirmed there (Q1–Q43).

---

## 1. Measurements and procedures (study kinds)

A request is `{name: study}`; each study has a `kind`.

| Kind | What it does | Main keys | Laser |
|---|---|---|---|
| `spectrum` | One spectrum at the current spot, or one per entry of `points` (offsets in µm) | `setup`, `acquire`, `points`, `focus`, `process` | yes |
| `map` | A grid of spectra: XY maps, line scans, Z stacks, XYZ; on ls6t's point-by-point engine (snake order, checks and autofocus between points, a live map in LabSpec) or LabSpec's own engine (faster for large maps; SWIFT) | `map.x_um / y_um / z_um = [from, to, step]`, `engine`, `swift` | yes |
| `depth` | A depth profile at the current spot: Z steps into the sample; each peak and band against depth; interfaces; depth corrected for the refractive index; a live profile in LabSpec | `depth.from_um, to_um, step_um, refractive_index` | yes |
| `series` | Repeated spectra at one spot (time series); a live profile in LabSpec | `series.count, interval_s` | yes |
| `calibration` | The spectral zero: reference lines (Si, polystyrene, cyclohexane, naphthalene, diamond, or your own) against their true positions; offset, and scale with several lines; `adjust`: `labspec` (the operator corrects LabSpec's zero, ls6t checks again) or `ls6t` (a remembered correction applied to later spectra) | `calibration.reference, lines_cm1, tol_cm1, adjust` | yes |
| `alignment` | Is the system aligned for this laser? The Si reference at fixed settings against the baseline recorded after AutoAlignment; if it fails, the operator is guided through LabSpec's AutoAlignment and the check repeats; then the beam's offset is measured again | `setup.laser_nm`; `alignment.guided, focus_guide` | yes |
| `focus` | Focus by the Raman signal: Z steps around the focus, a Gaussian through the best; the focus found serves the rest of the request | `focus.scan.range_um, step_um, line_cm1, exposure_s` | yes |
| `spot` | Where the beam really is relative to LabSpec's marker, remembered per objective and laser: a video frame with the beam (shutter, or during an acquisition), or the Raman signal while scanning over a small feature | `spot.method, feature_um, range_um, step_um, line_cm1` | yes |
| `move` | The stage to an offset, or the beam onto a point picked in a snapshot (`to_um`); Z to an offset from the focus; a video snapshot; `stay` makes it the new home spot | `move.x_um, y_um / to_um, dz_um, stay, snapshot` | no |
| `guide` | A manual adjustment with live feedback: the operator turns something (power at the source, focus by hand) while the bridge measures several times a second; LabSpec shows the signal and **STOP** when the goal is met (`reach`, `drop`, or `maximize`: just past the best) | `guide.instruction, mode, target, line_cm1, exposure_s, max_s` | yes |
| `background` | A white-light background: white light on, laser off (shutter closed by the bridge, or blocked by the operator); stored by name | `background.name, laser_off`; `acquire` as for the spectra to correct | no (blocked) |
| `filters` | Each filter's real transmission: one line (Si) through every filter within the caps, strongest first, exposure scaled; flags filters off nominal (`performance.filters_tol`); proposes the profile change (a person applies it); remembered per laser | `filters.line_cm1, window_cm1, labels, exposure_s, max_exposure_s` (no `setup.filter`) | yes |
| `cutoff` | Where the edge filter lets light through: 10/50/90 % points (`usable_from_cm1`) from a spectrum across the laser line, with the white light (laser off) or the sample's background; remembered per laser and grating; later ranges below it are warned | `cutoff.from_cm1, to_cm1, light, laser_off, plateau_cm1` | white: no; laser: yes |

### Options any measuring study can use

| Key | What it does |
|---|---|
| `setup` | Laser, grating, filter (laser power), objective, confocal hole, slit; each move read back |
| `acquire` | Range in cm⁻¹ (extended range: LabSpec stitches windows) or one window; exposure (or auto-exposure to a target); accumulations; spike removal; dark correction; `subtract_background` (a stored white-light background) |
| `focus` | LabSpec autofocus (`laser`, `raman`, `video`) or ls6t's `scan`, before the first point and every N points; `dz_um` (an offset from the focus) |
| `process` | Polynomial baseline; peak fits (pseudo-Voigt, Lorentz, Gauss) by name and position, or the N strongest peaks found; band integrals |
| `expect` | Expected values (any result path) with tolerances, checked after the run |
| `video` | Video frames before and/or after the measurement with the laser position marked; optionally with the beam showing |
| `images` | PNG plots: spectra with fits, map images per peak and band, depth profiles |
| `sample` | Name and notes (into the LabSpec file); `sensitive` lowers the power cap |
| `save_native` | LabSpec also saves its own `.l6s` files (default on) |

---

## 2. Tools an LLM sees (MCP server `ls6t-mcp`)

| Tool | What it does |
|---|---|
| `study_format` | The study format, this instrument's lasers, gratings, filters, objectives and limits, and the bridge's state. Read first |
| `validate_studies` | Static checks of a request with time estimates; every error says the fix |
| `run_studies` | Starts a request; returns results, or progress with an ETA, within 50 s |
| `study_status` | A request's results, or its progress |
| `cancel` | Stops a request: the acquisition or move in progress stops |
| `jobs` | Recent requests, and which one holds the instrument |
| `instrument` | `status` (bridge and instrument now, with LabSpec's last error and whether the live camera view is on), `laser_on` / `laser_off` (the laser shutter open for viewing, at most `limits.max_laser_on_s`, after the operator's Yes; closed by the bridge when the time is up, at a halt, or before any other command), `calibration` (LabSpec's spectrometer calibration per grating, read only), `autocal_info` / `autocal_run` (LabSpec's AutoCalibration script: can it run unattended; run it once after the operator's Yes), `set` (laser, grating, filter, objective, hole, slit now, without measuring: filter within the caps, the objective after the operator's Yes, read-back), `camera_on` / `camera_off` (LabSpec's live view; `camera` by name from the profile's `video.cameras`, e.g. "top", or number), `discover` (proposes a machine profile), `resume` (after an operator halt), `last_error` |
| `snapshot` | A video frame now, returned as an image: the beam's position marked, small features numbered; `with_laser` shows the beam and measures its offset; `extended` takes LabSpec's extended video (a stitched still over the area set in LabSpec; the stage moves; only when the profile allows it, after the operator's Yes) |
| `references` | Spectral corrections, white-light backgrounds, beam offsets, alignment state and saved LabSpec configurations (read-only) |
| `hardware_test` | Only in test mode (`ls6t connect APP --test`): TESTING.md with the maintainer. `plan`, `step`, `call` (one bridge command, with guards and the operator's Yes for the laser), `record`, `report`, `status`, `last_error`, `config` |
| `spot_status` | The beam's remembered offsets, their age and drift (read-only) |
| `alignment_status` | Per laser: the alignment baseline, the last check and its age (read-only) |
| `get_inputs` | The bridge commands one study would send, its range and time estimate, without running |
| `run_benchmarks` | The benchmarks on the simulator |
| `get_file` | A file of a request (spectrum, map data, LabSpec file) as base64 |
| `get_image` | A PNG of a study, as an image |

The server answers the MCP handshake at once. Its instructions tell the LLM the workflow, what to check before
trusting numbers, the safety rules and the polling etiquette.

---

## 3. Commands for a person (`ls6t`, PowerShell)

| Command | What it does |
|---|---|
| `ls6t status [REQUEST] [--wait S]` | Versions, settings, the bridge and the profile; or a request's results |
| `ls6t format` | The study format |
| `ls6t validate FILE` | Static checks and estimates |
| `ls6t run FILE [--wait S]` | Start a request and wait |
| `ls6t inputs FILE NAME` | The commands of one study, without running |
| `ls6t cancel REQUEST` | Stop a request |
| `ls6t jobs [--limit N] [--active]` | Recent requests |
| `ls6t bench [NAMES]` | The benchmarks on the simulator |
| `ls6t instrument status / discover / resume / last-error` | The instrument now; the discovery; lift a halt; LabSpec's last error (`GetLastErrorInfo`) |
| `ls6t instrument set [--laser-nm --grating --filter --objective --hole --slit]` | Change the set-up now, without measuring |
| `ls6t instrument snapshot [--laser / --extended]` | A video frame with the beam marked; `--extended`: LabSpec's extended video |
| `ls6t instrument config list / save NAME / load NAME [--show]` | LabSpec's whole configuration as `LS6T_HOME\configs\NAME.ini` (`SaveConfig` / `LoadConfig`); loading asks the operator; `--show` opens LabSpec's dialog. Maintainer only |
| `ls6t bridge install [--dest DIR]` | Write the LabSpec scripts for this PC: the bridge, the discovery, and the optional script-motor logger |
| `ls6t bridge call VERB key=value ... [--laser]` | One bridge command, for the maintainer's tests |
| `ls6t profile ini [FILE]` | What LabSpec's own configuration (`OLConfig.ini`) says: the instrument, cameras (names, numbers, models), objectives (NA, field of view), every motor and its positions. Profiles made from discovery take the camera names and objectives from it |
| `ls6t profile show / default / import DUMP / set KEY VALUE` | The machine profile: view, create, import the discovery, change one value. Until a person does, the profile is built automatically from the inventory the bridge records when it starts (lasers capped at 10 %, no laser shutter) |
| `ls6t alignment status` / `record REQUEST [--study] [--attempt]` | Alignment state; record a baseline after AutoAlignment (person only) |
| `ls6t spot status / forget [--objective --laser]` | The beam's offsets |
| `ls6t references show / clear correction [LASER@GRATING] / clear background NAME / clear spot` | Everything ls6t remembers between sessions |
| `ls6t connect check [APP]` | Start each app's ls6t entry as the app would (stdio handshake): its tools and start-up time, or the error |
| `ls6t connect [APP... --sim --remove]` | Which LLM apps are on the PC and which run ls6t; add or remove the MCP server in the Claude desktop app (`claude`), the ChatGPT desktop app / Codex (`codex`), Gemini CLI (`gemini`), or every app found (`auto`) |
| `ls6t test plan / step ID / record ID RESULT / report` | TESTING.md's tests: progress, one test, recording a result and answers, the filled-in results table (`LS6T_HOME\testing`) |
| `ls6t sim [--seconds S]` | Run the simulator by hand |
| `ls6t-typelib [--path F] [--format text/markdown/json] [--out F] [--list]` | (a separate program, any OS) Read LabSpec's ActiveX type library straight from the file and list every method and property of the installed version; `NEW` marks those not in the 2018 manual, and words of interest are flagged. Read-only: nothing starts or moves |
| `ls6t run-request FOLDER` | What the detached runner runs (internal) |
| `ls6t-setup-<version>.exe` | The Windows installer: per user, offline, with its own Python; the LabSpec scripts, PATH, Start menu and the LLM apps found (Claude, ChatGPT / Codex, Gemini CLI); updates in place; uninstalls keeping the data (INSTALL.md) |

Only the command line can record alignment baselines, change the profile, or clear what is remembered. An LLM can
read them, not change them.

---

## 4. Inside LabSpec (the bridge, protocol 1)

`ls6t_bridge.vbs` runs in LabSpec 6. It carries out only these commands:

| Verb | What it does |
|---|---|
| `ping`, `status` | Version; laser, grating, filter, objective, hole, slit, stage X/Y/Z, detector temperature, exposure, shutter mode, LabSpec's last error |
| `move` | Any motor by value, position label or index, waiting for it with stop checks; read back |
| `move_xy` | The stage in X and Y together |
| `autofocus` | LabSpec's laser, Raman or video autofocus |
| `acquire` | A spectrum (range or one window; exposure or auto-exposure; spike removal; dark); written with LabSpec's acquisition info; optionally saved as `.l6s`; `laser_off` keeps the laser shutter closed (white-light backgrounds) |
| `view` | Live map or profile in LabSpec, growing as points are measured; saved at the end |
| `map_native` | A map on LabSpec's own engine: axes set with `SetMappingParams`, the interface's exposure and accumulations, the spectrometer centred, SWIFT on or off, the spectral image acquired, waited for (or cancelled), written out for ls6t in chunks (`GetValueSimplePart`, `mapping.export_chunk`) or whole, saved as `.l6s` and text |
| `video` | A video frame (cloned from the live video), written for ls6t and saved in LabSpec's image formats; with the laser shutter opened briefly, or half-way through a Raman acquisition, so the beam shows; or LabSpec's extended video (`Video(3)`, no laser) |
| `config` | Save or load LabSpec's configuration file (`SaveConfig` / `LoadConfig`) |
| `last_error` | LabSpec's last error (`GetLastErrorInfo`); also added to every error the bridge reports |
| `camera` | LabSpec's live camera view: `on`, `off`, `status` (optionally the camera's number) |
| `calib_params` | LabSpec's spectrometer calibration per grating, read with `OperVar` (`ACT_INSTRUMENT[AF/BF/Diode/Focal/Angle/Gamma/SpMin/SpMax][i]`) |
| `run_vbs` | One of LabSpec's own scripts (its AutoCalibration), run inside the bridge (ExecuteGlobal); only a .vbs in LabSpec's folder, only when the profile enables it |
| `laser` | `on` (seconds, the shutter motor) / `off`: the laser shutter open in manual mode for viewing; the bridge closes it itself (time up, halt, stop, any other command but quick reads) |
| `labels` | A motor's named positions and the current one (position names are matched to these: `x100` finds `x100_VIS`) |
| `guide` | The fast loop of a guided adjustment: short acquisitions, the line's signal, LabSpec's dialog (or the status bar), STOP |
| `confirm`, `notice` | Yes/No and OK/Cancel questions to the operator |
| `message` | Status-bar messages |
| `save`, `remove` | Save or free a LabSpec data object |
| `safe` | Stop motion and acquisition |
| `resume`, `quit` | Lift a halt (the operator agrees); end the bridge |
| `discover` | (and `ls6t_discover.vbs`) list the motors and positions, read the detector, test file access, the stop window, the stop button and the video; probe the methods outside the 2018 manual (`GetLastErrorInfo`, `GetValueEx`, `GetValueSimplePart` on a scratch spectrum, `GetConfigParam` and its parameters) |

**Reading data.** Arrays are read with `GetValue`, falling back to `GetValueEx` (in the manual, missing from the
installed build) and then `GetValueSimple`; each answer says which worked (`data_via`). A motor position's name past
index 9 is read with `GetValueEx("CurrentPositionName:<motor>")` when available.

**`ls6t_script_motor.vbs`** (optional, TESTING.md T1.4): a virtual script-controlled motor that drives nothing. It
logs every `GetMotorAction` call and answers with `SetMotorInfo`, so the maintainer can record how LabSpec talks to
such a motor before ls6t drives real hardware this way.

**Not used (on purpose):** `SetConfigParam` and `SetScriptPath` change LabSpec's script set-up, whose meaning is not
documented; `SetExtendedVideo` is not in the installed build (the extended video uses the area set in LabSpec).

**The operator stays in charge:**

- closing the bridge's window halts it;
- cancelling an acquisition in LabSpec counts as a stop;
- LabSpec's script stop button ends the bridge;
- the first laser exposure of a request needs a Yes.

---

## 5. What ls6t checks

**What makes a result unreliable:**

- saturation;
- a warm detector;
- a set-up move not reached;
- the spectral zero out of tolerance;
- an alignment check below its baseline;
- a stop before the end;
- a guided adjustment that did not reach its goal;
- a background that could not be applied.

**What it warns about:**

- weak or undetected peaks;
- spikes left with spike removal off;
- ranges not covered;
- a missing machine profile;
- stale alignment checks, spectral corrections or beam offsets;
- beam drift;
- a background taken with other settings, or with narrow lines in it;
- the Z direction not stated in the profile.

**Its own analysis.** ls6t computes these itself, from the exported spectra:

- Raman shift from the read-back laser line;
- the spectral correction, when one is remembered;
- background subtraction;
- iterative baseline;
- noise from second differences;
- peak fits with errors and R²;
- bands;
- maps, with summaries;
- depth profiles and interfaces;
- focus curves;
- spot scans;
- video features and the beam spot.

---

## 6. Safety

**Positions:**

- XY moves are offsets from the home spot (where the operator left the stage, or where a `move` chose to stay).
- Z is relative to the focus.
- Absolute stage limits are checked before every move.
- At the end of a normal request, Z returns to the focus first, then XY to home.

**Limits from the machine profile, set by a person:**

- moves (`max_xy_um`, `max_dz_um`);
- laser power: the filter is capped per laser, globally, and lower for sensitive samples;
- exposure, accumulations, points and time;
- the guided loop's time and exposure.

**One request at a time** holds the instrument.

**The bridge runs only the fixed commands above.** Values are checked on both sides, and no code travels.

**Laser shutter.** Opening it for snapshots, or closing it for backgrounds, needs a shutter motor named in the
profile. The shutter mode is restored on every path.

---

## 7. What ls6t remembers between sessions

| What | Where | Written by | Used for |
|---|---|---|---|
| Machine profile: instrument, limits, timing, video and alignment settings | `LS6T_HOME\profile.json` | a person (`profile import/set`) | everything |
| Alignment baselines and settings per laser | in the profile | a person (`alignment record`) | `alignment` checks |
| Alignment checks | `alignment_log.jsonl` | every `alignment` study | readiness warnings, drift |
| Beam offsets per objective and laser | `spot.json` | `spot` studies, lit snapshots | the marker, `move.to_um` |
| Spectral corrections per laser and grating | `xcal.json` | `calibration` with `adjust: ls6t` | later spectra's Raman shift |
| White-light backgrounds | `backgrounds\` | `background` studies | `acquire.subtract_background` |
| LabSpec configurations | `configs\NAME.ini` | a person (`instrument config save`) | `instrument config load` |
| LabSpec's configuration before a request | `LS6T_OUT\run_...\labspec_before.ini` | requests, with `session.restore_config` | loaded back when the request ends normally |
| Every request: studies, spectra, results, images, LabSpec files | `LS6T_OUT\run_...` | every request | results, `get_file`, `get_image` |

---

## 8. Testing tools

- **Simulator** (`LS6T_MODE=sim`): a simulated LabSpec and XploRA behind the same protocol. It includes:
  - a silicon sample with a graphene flake and a dust particle;
  - a polymer film;
  - a white light;
  - beam offset, misalignment, defocus, spectral scale error, cosmic rays and saturation;
  - an operator who confirms, stops, aborts, runs AutoAlignment or corrects the zero.
- **Benchmarks** (`ls6t bench`, 14 cases with known answers):
  - `si_calibration`, `graphene_map`, `extended_range`, `saturation`, `series`;
  - `guided_focus`, `focus_scan`, `spot_raman`, `spot_video`;
  - `depth_profile`, `calibration_zero`, `white_light`, `native_map`, `operator_stop`.
- **Test suite** (`pytest`, 106 tests), which also checks the LabSpec scripts' structure.
- **Instrument tests** with the maintainer: [TESTING.md](TESTING.md).
