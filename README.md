# labspec-translator (ls6t)

Raman measurements on a HORIBA instrument, driven by an LLM through LabSpec 6, with every result checked.

An LLM (Claude, through MCP) writes a **study**: a short JSON description of the measurement in the user's terms. It
includes the sample, laser, grating, filter, objective, Raman-shift range, exposure, and the points, map or time
series. ls6t validates the study against the instrument's own limits. It turns the study into a fixed sequence of
commands, and a small script running **inside LabSpec 6** (the *bridge*) carries them out. The operator sees every
move and spectrum in LabSpec, confirms before the first laser exposure, and can stop everything there. ls6t then
reads the exported spectra and does the analysis itself: baseline, peak fits, bands and maps. It checks saturation,
detector temperature, set-up read-back, range coverage and calibration, and says whether each result is
**reliable**.

It is one of the [llm-translators](https://github.com/feranick/llm-translators). Like build123d-translator (b3dt),
it runs **locally**: on the instrument PC, next to the Claude desktop app. It has no SLURM, router or remote
machines. Unlike the other translators, the backend has no Python API: LabSpec is automated with VBScript
(*LabSpec 5/6 Automation*, HORIBA, 2018). That is why the bridge script exists.

First instrument: **XploRA** (LabSpec 6, Windows 11).

**Scope of version 1**

| Kind | What |
|---|---|
| `spectrum` | One spectrum at the current spot, or one per entry of `points` (offsets in µm) |
| `map` | A grid of spectra: XY maps, line scans, Z stacks, or XYZ |
| `series` | Repeated spectra at one spot: a time series |
| `calibration` | The zero of the spectral scale: a reference sample's lines (Si 520.7 cm⁻¹ by default; polystyrene, cyclohexane, naphthalene, diamond, or your own) against their true positions: the offset, and the scale with several lines. ls6t guides the operator to correct LabSpec's zero and measures again, or keeps its own correction for later spectra |
| `depth` | A depth profile at the current spot: spectra at Z steps from above the surface down into the sample; each peak and band against depth, the interfaces (where a layer starts or ends), the depth corrected for the refractive index |
| `background` | A white-light background: a spectrum with the white light on and the laser off, stored by name and subtracted from later spectra taken with the same light |
| `alignment` | Is the system aligned for this laser? The Si reference at the laser's fixed settings, compared with the baseline recorded after the last AutoAlignment. If it fails, the operator is guided through LabSpec's AutoAlignment and the check repeats; then the beam's offset is measured again |
| `focus` | Find the focus by the Raman signal: Z steps across a range around the current focus, a short acquisition at each, a Gaussian through the best points; the focus found serves the rest of the request |
| `move` | No laser: the stage to an offset (and Z to an offset from the focus), or the **beam** onto a point picked in a snapshot (`to_um`), then a video snapshot. With `stay` (default) that spot becomes home for later studies and the next request |
| `spot` | Find where the beam really is (it drifts from LabSpec's marker at the image centre) and remember it per objective and laser: a video frame with the beam on, or the Raman signal while the beam scans over a small feature seen on the video |
| `guide` | A manual adjustment with live feedback: the operator turns something (laser power at the source, focus, a mirror) while ls6t measures a line several times a second; LabSpec shows the signal and **STOP** when the goal is reached or the best position has just been passed |

**Video snapshots**: a frame of LabSpec's camera with the laser position marked, on demand (`snapshot`) or during a
study (`video`, on by default for calibration). Optionally the laser shutter opens for about a second: the beam
shows, ls6t finds the spot, and reports its offset from LabSpec's laser position.

Not yet included: LabSpec's own (native) fast mapping engine, Linkam temperature set-points, polarisation and PMT
detectors. See `planning.md`.

**Contents:**

- [Installation](#installation) (the steps: [INSTALL.md](INSTALL.md))
- [First session at the instrument](#first-session-at-the-instrument)
- [Use](#use)
- [A study](#a-study)
- [Results](#results)
- [Safety and the operator](#safety-and-the-operator)
- [Video snapshots: where the beam is](#video-snapshots-where-the-beam-is)
- [Stage and focus](#stage-and-focus)
- [Mapping engines](#mapping-engines)
- [Depth profiles](#depth-profiles)
- [The spectral zero: calibration](#the-spectral-zero-calibration)
- [White-light background](#white-light-background)
- [The laser spot: finding it and remembering it](#the-laser-spot-finding-it-and-remembering-it)
- [Alignment and guided adjustments](#alignment-and-guided-adjustments)
- [The checker](#the-checker)
- [How it works](#how-it-works)
- [For LLM tools: the MCP server](#for-llm-tools-the-mcp-server)
- [Settings](#settings)
- [Testing](#testing)
- [Files](#files)
- [Versions](#versions)

---

## Installation

On the instrument PC (Windows 11, next to LabSpec 6 and the Claude desktop app), run **`ls6t-setup-<version>.exe`**
(from the repository's Releases, built for every version): per user, no administrator rights, no network; it brings its
own Python, writes the LabSpec scripts, and connects the LLM apps it finds (Claude desktop app, Claude Code, ChatGPT desktop app /
Codex, Gemini CLI, Cursor, GitHub Copilot, Windsurf, LM Studio). Then add the scripts in LabSpec, run the
discovery and set the machine profile. From source instead: Python 3.10+ and Git, a venv,
`pip install -e ".[mcp,render,test,windows]"`, `ls6t bridge install`, and the MCP entry in the Claude app by hand.
The steps, building the installer, the offline install, updating and troubleshooting are in **[INSTALL.md](INSTALL.md)**.

---

## First session at the instrument

In short (details and pass criteria in [TESTING.md](TESTING.md)):

1. `ls6t bridge install` (or the installer); start `ls6t_bridge.vbs` in LabSpec (it reads the instrument); `ls6t profile propose`; with the maintainer, fill in `profile.proposed.json` (power, caps, limits); `ls6t profile import` it.
2. Start `ls6t_bridge.vbs`; `ls6t instrument status` matches LabSpec.
3. With the operator: a few moves with `ls6t bridge call` (no laser), then the silicon calibration study.
4. Then through the Claude app.

---

## Use

**From the Claude app.** Describe the measurement, for example *"Map the G and 2D bands of the graphene flake under
the laser: 20 × 20 µm, 1 µm steps, 532 nm, gentle on the sample."* Claude reads `study_format`, writes and validates
the study, runs it, and reports from the checked results. The operator confirms the first exposure in LabSpec.

**From PowerShell:**

```powershell
ls6t format                                  # the study format and this instrument's choices and limits
ls6t validate examples\studies.json          # static checks and time estimates
ls6t inputs examples\studies.json si_check   # the bridge commands one study would send (nothing runs)
ls6t run my_studies.json                     # start a request and wait for it ("--wait 0" returns at once)
ls6t status <request folder>                 # results, or progress while it runs
ls6t cancel <request folder>                 # stop it: the acquisition or move in progress stops
ls6t jobs                                    # recent requests; which one holds the instrument
ls6t instrument status | discover | resume | last-error
ls6t instrument set --laser-nm 532 --grating 1800 --filter 10% --objective x100   # change the set-up, no laser
ls6t instrument snapshot [--laser]           # a video frame with the laser position marked (PNG)
ls6t instrument snapshot --extended          # LabSpec's extended video (the stage moves; video.allow_extended)
ls6t instrument config list | save NAME | load NAME   # LabSpec's whole configuration (maintainer)
ls6t bridge call <verb> key=value ...        # one bridge command, for the maintainer (TESTING.md)
ls6t profile show | default | import <dump>
ls6t profile set <key> <JSON value>          # one profile value, e.g. video.laser_offset_um "[1.4, -0.6]", lasers.637.75.power_mw 40
ls6t references show                         # spectral corrections, backgrounds, beam offsets, alignment
ls6t references clear correction [LASER@GRATING] | background NAME | spot
ls6t alignment status                        # per laser: baseline, last check, age
ls6t alignment record <request> [--study NAME] [--attempt N]   # after AutoAlignment: the check becomes the baseline
ls6t bench                                   # the benchmarks on the simulator
ls6t-typelib [--format markdown] [--out F]   # every command of LabSpec's ActiveX (read-only), new ones marked
```

Every command prints JSON. `-` instead of a file reads the studies from standard input.

---

## A study

Studies are given as `{name: study}`; names use letters, digits, `_` and `-`. An example (more in
`examples/studies.json`):

```json
{"graphene_map": {
  "kind": "map",
  "sample": {"name": "CVD graphene on SiO2/Si", "sensitive": true},
  "setup": {"laser_nm": 532, "grating": 600, "filter": "10%", "objective": "x100"},
  "acquire": {"range_cm1": [1200, 2900], "exposure_s": 2, "accumulations": 2, "spike": "double"},
  "map": {"x_um": [-10, 10, 1], "y_um": [-10, 10, 1]},
  "focus": {"autofocus": "laser", "every": 50},
  "process": {"peaks": [{"name": "G", "near_cm1": 1585}, {"name": "2D", "near_cm1": 2680, "window_cm1": 60}],
              "bands": [{"name": "D", "from_cm1": 1300, "to_cm1": 1400}]},
  "images": true}}
```

| Key | Meaning |
|---|---|
| `kind` | `spectrum`, `map`, `series` or `calibration` |
| `sample` | `{name, notes, sensitive, site}`; `sensitive: true` caps the filter at `limits.sensitive_max_filter_pct`. In the LabSpec file: the name as Sample, the notes in Remark (after `ls6t <seq>`), `site` as Site (else empty), the study's name as Title and `ls6t <request>` as Project. |
| `setup` | `{laser_nm, grating (gr/mm), filter (a profile label, e.g. "10%"), objective (label), hole_um, slit_um}`. A key left out keeps what LabSpec has; the result records what it was. |
| `acquire` | `range_cm1: [from, to]` (extended range: LabSpec stitches windows) or `center_cm1` (one window); `exposure_s` (per accumulation) or `"auto"` with `auto_exposure: {target_counts, min_s, max_s, test_s}`; `accumulations`; `spike`: `labspec`, `none`, `single`, `double`, `double_auto`; `dark`: `labspec`, `true`, `false`; `subtract_background`: a stored white-light background's name |
| `focus` | `{autofocus: laser, raman or video (LabSpec's routines), or scan (ls6t's focus scan on the Raman line), before the first point and every N points with every; dz_um; scan: {range_um (10), step_um (1), line_cm1 (Si), window_cm1 (30), exposure_s (0.5)}}` |
| `move` | (kind `move`) `{x_um, y_um` or `to_um: [x, y]` (stage coordinates from a snapshot: the beam lands there), `dz_um, stay: true, snapshot: true}`; `setup` may only give the objective |
| `spot` | (kind `spot`) `{method: auto, shutter, acquisition or raman; feature_um: [x, y] (from a snapshot) or "auto"; refine: true; range_um, step_um (by objective: x100 4 and 0.5 µm); line_cm1 (Si); exposure_s (0.3); acq_exposure_s (2); remember: true}`; `setup` needs `laser_nm` and `objective` |
| `points` | (spectrum) `[{x_um, y_um, dz_um}]`: offsets from where the operator left the stage |
| `map` | `{x_um, y_um, z_um: [from, to, step]}` offsets in µm; `snake: true` (default): alternate rows reversed; `engine`: `auto`, `points` or `native` (LabSpec's mapping engine), `swift: true` (native only) |
| `series` | `{count, interval_s}`: seconds from one start to the next |
| `depth` | (kind `depth`) `{from_um (−5: above the focus), to_um (20), step_um (1), refractive_index}`: nominal depth is the stage displacement into the sample from the focus; with `setup`, `acquire`, `process`, `focus`, `images` as for a spectrum |
| `calibration` | (kind `calibration`) `{reference: si, polystyrene, cyclohexane, naphthalene or diamond; lines_cm1: [...] (your own); tol_cm1 (1); adjust: none, labspec or ls6t}` |
| `background` | (kind `background`) `{name, laser_off: shutter or operator}`; `acquire` as for the spectra it will correct (same grating and range) |
| `process` | `baseline: {degree}` or `false` (default: iterative polynomial, degree 3); `peaks: [{name, near_cm1, window_cm1 (40), shape: pseudo_voigt, lorentz or gauss}]`; `auto_peaks: N` (when no `peaks`: the N strongest, default 3); `bands: [{name, from_cm1, to_cm1}]` |
| `expect` | `[{quantity: "dotted.result.path", value, tol}]`, checked after the run |
| `images` | `true`: PNGs (spectra with fits; one map image per peak quantity and band) |
| `save_native` | `true` (default): LabSpec also saves `.l6s` files |
| `alignment` | (kind `alignment`) `{guided: true (default), focus_guide: false}`; only `setup.laser_nm` is needed once the maintainer has recorded the laser's settings |
| `guide` | (kind `guide`) `{instruction, mode: reach, drop or maximize, target (counts/s; reach and drop), line_cm1 (default Si 520.7), window_cm1 (30), exposure_s (0.2), max_s (120), stable_n (2), drop_frac (0.1)}` |
| `video` | `{before, after, with_laser}`: video frames with the laser position marked, before the first and/or after the last acquisition. `before` is on by default for `calibration`. `with_laser` opens the laser shutter for `video.laser_s` (profile) so the beam shows; it needs `setup.filter` and a laser shutter motor in the profile. |

Validation refuses anything outside the profile: a laser, grating, filter or objective the instrument does not have,
a filter above the caps, exposures, accumulations or points above the limits, moves beyond `max_xy_um` or
`max_dz_um`, an estimated time above `max_minutes`. Every error says what to change.

## Results

`study_status` (or `ls6t status REQUEST`) returns per study:

| Key | Content |
|---|---|
| `ok`, `complete` | Something was measured; everything planned was measured |
| `reliable`, `unreliable_because` | The verdict and its reasons (see [The checker](#the-checker)) |
| `warnings` | Weak peaks, spikes left, peaks not detected, no profile yet, … |
| `checks` | `saturation`, `detector_temp_c`, `setup` (each move asked and read back), `range`, `snr`, `spikes`, `calibration` |
| `instrument` | Laser line, filter, objective, grating and detector temperature as read back after the set-up |
| `setup` | Requested, and LabSpec's state before and after |
| `spectra` | (spectrum, series, calibration) per point: offsets, absolute stage position, files, exposure, max counts, noise, spikes, `peaks`, `bands` |
| `peaks`, `peaks_by_name`, `bands` | Of the first spectrum: `position_cm1`, `fwhm_cm1`, `height`, `area`, `eta`, `stderr_position_cm1`, `r2`, `snr` |
| `map` | `nx, ny, nz`, axes, `summary.<peak or band>.<quantity>: {n, mean, std, min, max}`, files: `<name>_map.json` (grids), `<name>_map.npz` (all spectra on one axis), the LabSpec `.l6s` |
| `series` | `t_s` and each quantity over time, with a summary |
| `calibration` | `reference`, `lines` (each: reference, measured, error), `offset_cm1` (mean error), `worst_error_cm1`, `correction` (`true = a + b × measured`), `fit` (offset only, or offset and scale), `residual_cm1`, `pass`; with `adjust: labspec`, `attempts`; with `adjust: ls6t`, `stored_correction`. Also `reference_cm1`, `measured_cm1`, `fwhm_cm1` of the first line |
| `depth` | `nominal_um` (stage displacement into the sample), `depth_um` (× n when given), `profiles` (each peak's height and area, each band's area, against depth), `interfaces` (`quantity`, `rise` or `fall`, `depth_um`, `width_10_90_um`) and `interfaces_by` (quantity → height/area → rise/fall → depth), the plot |
| `background` | the stored background: `name`, `file`, settings, `max_cps`, `mean_cps` |
| per spectrum | `x_correction` (the spectral correction applied) and `background` (what was subtracted) when used |
| `alignment` | `passed` (true, false, or null without a baseline), `attempts` (per check: `counts_per_s`, `ratio` to the baseline, `fwhm_cm1`, `fwhm_ratio`, `position_cm1`, why it failed), `recommendation`, and the snapshot after it with `measured_offset_um` |
| `focus` | (kind `focus`) `found`, `z_um`, `shift_um` from the starting focus, `best_signal`, `contrast`, the Z steps and the signal at each, the plot; why when no clear best (Z then goes back). Other kinds with `autofocus: scan`: `focus_scans` |
| `move` | `stage_um`, `z_um`, `home_um`, `stay`, `beam` (with `to_um`: the point, the offset used and its source), the snapshot (`video.here`) |
| `spot` | `found`, `offset_um` (beam − LabSpec's marker), `method`, `remembered`, `drift_um` and the `previous` record, `attempts` (per method: snapshot, contrast; Raman: feature, grid, signal, where the beam hit); images: the video frame, the Raman scan |
| snapshots | also `features`: small features to aim at (numbered on the PNG: `x_um`, `y_um`, `size_um`, `dark`, `strength`), `offset_used_um` and its `offset_source` |
| `guide` | `reason` (`target`, `past_max`, `saturation`, `timeout`, `operator_done`, `operator_abort`, `stopped`), the STOP `message`, `value`, `max_value` and when (`t_max_s`), `iterations`, `period_s` (seconds per reading), the history file and its plot |
| `expect` | Each expected value: `got`, `pass` |
| `video` | `before` / `after`: the PNG (`path`), the raw frame, LabSpec's own image files, the stage position, `marker_um`, the field of view; with the laser: `spot_um` and `measured_offset_um` |
| `images`, `files`, `seconds`, `execution` | Pictures (including `video_before` / `video_after`), paths, timing, mode and bridge version |
| `timing` | Where the time went: `exposure_s`, `operator_s` (waiting for the operator's answers), `overhead_s`, each bridge command's time, the acquisitions' phases inside the bridge (`acquire_phases_s`) and `note` naming the largest |
| `checks.zero` | The last calibration check for this laser and grating (LabSpec keeps a zero per grating), and those of the other gratings with this laser |

Spectrum files are written by the bridge (`spectra/p00000.txt`: `# key=value` header lines, including LabSpec's
acquisition info as `acq.*`, then wavelength and counts). The Raman-shift version sits next to it (`.shift.txt`).

---

## Safety and the operator

- **The operator watches in LabSpec**: every move, live spectra, the map or series filling in.
- **Confirmation**: before the first laser exposure of a request, LabSpec asks the operator (Yes/No) with the sample,
  laser, filter and objective. `LS6T_CONFIRM=study` asks before every study, `never` disables it. A No ends the request.
- **Stopping**:
  - Closing the bridge's small window stops the acquisition or move in progress and **halts** the bridge. It refuses
    commands until resumed (`instrument resume`), and the operator then answers Yes in LabSpec.
  - Cancelling an acquisition in LabSpec counts as the same operator stop.
  - `ls6t instrument end-bridge` ends the bridge cleanly (this XploRA's LabSpec has no stop button for scripts).
    Do not use LabSpec's live video buttons while the bridge runs: LabSpec ends the script abruptly; end the
    bridge first.
  - `ls6t cancel` (or the `cancel` tool) stops a request from the PC.
- **Positions**: XY moves are offsets from the *home spot*: where the operator left the stage when the request
  started, or where a `move` study chose to stay. Z is relative to the *focus*: the operator's, or the last autofocus
  or focus scan. Absolute positions are never taken from a study. The profile's `stage.x_um`, `y_um` and `z_um`
  (absolute limits) are checked before every move.
- **Back home**: at the end of a request that finished normally, Z returns to the focus first (the safe height),
  then the stage to the home spot. After a stop (operator, cancel) nothing moves, and the next request starts from
  where the stage was left.
- **Power**: the filter is capped per laser, for samples marked sensitive, and globally. The laser shutter opens only
  during acquisitions (LabSpec's automatic shutter mode).
- **One request at a time** holds the instrument (a lock file). Others wait as `queued`.
- **No code travels**: the bridge runs only its fixed verbs, with values checked on both sides.

## Video snapshots: where the beam is

A snapshot copies the current frame of LabSpec's video camera; the live view goes on. ls6t draws the frame with the
stage axes in µm and a **red cross at the laser position**. That position is the stage position, which LabSpec's
video axes use, plus the profile's measured `video.laser_offset_um`. LabSpec also tries to save the frame in its own
image formats (`video.formats`), next to the PNG.

**With the laser** (`snapshot(with_laser=true)`, `ls6t instrument snapshot --laser`, or a study's
`video.with_laser`):

1. The bridge takes a frame with the shutter closed.
2. It opens the laser shutter in manual shutter mode for `video.laser_s` seconds (default 1, at most
   `video.max_laser_s`).
3. It takes a second frame, then closes the shutter and restores the shutter mode on every path out, including a stop.
4. The difference between the two frames is the spot alone. ls6t finds its centre and draws it as a dashed circle.
   It reports `spot_um` and `measured_offset_um`, the value to put in `video.laser_offset_um` so the cross sits on the
   beam.

Guards for the laser: the profile must name the laser shutter motor; the current filter must be within the
profile's caps for the laser in use; the operator answers Yes in LabSpec first (unless `LS6T_CONFIRM=never`); and
it is refused while a request holds the instrument. In a study, the frame is taken after the request's confirmation.

Use it before a calibration or a measurement to check that the beam is on the intended spot. From the Claude app, the
`snapshot` tool returns the image itself, so Claude sees where the beam is.

**Extended video** (`snapshot(extended=true)`, `ls6t instrument snapshot --extended`): LabSpec's own stitched still
image (`Video(3)`) over the area and settings chosen in LabSpec's interface, wider than one field of view. The stage
moves while it is taken, so it is off until the maintainer has tried it (`video.allow_extended`, TESTING.md T5b.6),
and the operator answers Yes each time. No laser. (`SetExtendedVideo`, which would set the area from a script, is in
the manual but not in the installed LabSpec.)

## LabSpec configurations

LabSpec can save and load its whole configuration as an `.ini` file (`SaveConfig` / `LoadConfig`, in the installed
LabSpec's type library but not in the 2018 manual). For the maintainer:

```powershell
ls6t instrument config save before_tests     # LS6T_HOME\configs\before_tests.ini
ls6t instrument config list
ls6t instrument config load before_tests     # the operator confirms in LabSpec; --show opens LabSpec's dialog
```

With `session.restore_config` true in the profile, every request saves LabSpec's configuration when it starts
(`labspec_before.ini` in the request folder) and loads it back when it ends normally, so the operator finds LabSpec
as they left it. It is off until TESTING.md T1.4 shows what the file holds.

## Stage and focus

Both are motorised on the XploRA and driven through LabSpec: `MoveXY` for the stage, the `Z` motor for focus.

- **In measurements**: `points` (offsets), `map` (`x_um`, `y_um`, `z_um` grids, snake order), `focus.dz_um` (an
  offset from the focus).
- **Focus**: LabSpec's own autofocus (`focus.autofocus`: `laser`, `raman`, `video`), or ls6t's **focus scan**
  (`autofocus: "scan"`, or a `focus` study).
  - The scan steps Z across `range_um` (default ±5 µm, 1 µm steps) around the current focus, with a short
    acquisition at each.
  - It measures the line's height (Si 520.7 cm⁻¹ by default) after a 3-point median, so cosmic rays do not count.
  - It fits a Gaussian through the points above 30 % of the best, moves Z there, and draws the curve.
  - With no clear maximum inside the range, Z goes back to where it was and the result says why.
  - On the simulator it finds a focus 2.3 µm off to within 0.01–0.03 µm at the x100.
- **Looking around**: a `move` study (no laser) goes to an offset and takes a video snapshot, for example *"show me
  50 µm to the left"*. With `stay: true` (default), that spot becomes home for the following studies and the next
  request. With `stay: false`, the request ends back at the previous home.
- **For the maintainer**: `ls6t bridge call move_xy x_um=... y_um=...` and `ls6t bridge call move motor=Z ...`, one
  command at a time.

## Mapping engines

Maps (and depth profiles) run on one of two engines (`map.engine`, or `depth.engine`):

| | `points` | `native` |
|---|---|---|
| Who runs it | ls6t: a stage move and an acquisition per point, through the bridge | LabSpec's own mapping engine (`SetMappingParams` per axis, `Acq` spectral image); SWIFT fast mapping with `swift: true` |
| Speed | exposure + about 0.5–1 s per point | exposure + LabSpec's overhead (about 0.05 s, measured on the instrument: TESTING T6.7); SWIFT faster still |
| Spectral range | any (extended range stitched) | one detector window (`acquire.center_cm1`) |
| Between points | checks, autofocus every N points, auto-exposure, live view through `Map()` | nothing: LabSpec runs the map; the autofocus (if any) runs once before |
| Stop | immediate, after any point | cancels LabSpec's map (the partial map is not returned) |
| Checks | every spectrum, as it comes | every spectrum, when the map ends |

`auto` (the default) picks `native` for maps of `mapping.native_min_points` (400) points or more that need nothing
between points (a single window, a fixed exposure, no autofocus every N points), and `points` otherwise.

**How the native engine works:**

1. ls6t converts the study's offsets into stage positions (`mapping.positions`: `absolute`, or `relative` if
   LabSpec turns out to take offsets).
2. It centres the spectrometer, sets the exposure and accumulations in LabSpec's interface, enables the axes, and
   starts the map. The bridge waits, writing its heartbeat and watching for stops.
3. The bridge writes the whole map for ls6t (axes and values) and saves it as `.l6s` and as LabSpec's text export.
4. ls6t splits it into spectra (`mapping.data_order`) and runs the same analysis as for point-by-point maps.

The manual leaves several points open: absolute or relative positions, the order of the values, which exposure
the engine takes, and how fast a large map can be written out from a script. They are settings in the profile's
`mapping` block, to confirm in TESTING T6.7 (Q29–Q33).

## Depth profiles

A `depth` study stays at the current spot and measures down through the sample: Z steps from `from_um` (above the
focus: negative) to `to_um` (deeper), in `step_um`. These *nominal* depths are stage displacements into the sample
from the focus. The profile's `stage.z_into_sample` (`"+"` or `"-"`, TESTING T6.3) says which Z direction goes
deeper; until it is set, `+` is assumed and the result says so.

- Each peak's height and area, and each band's area, against depth: `depth.profiles`, and a plot.
- **Interfaces** are where a profile crosses half-way between its lowest and highest value. Rising means a layer
  starts (a substrate appears, or the surface of a film); falling means it ends. Each comes with its 10–90 % width,
  which reflects the depth resolution.
- **Refractive index.** Inside a transparent sample the focus goes deeper than the stage moves. With
  `depth.refractive_index` n, depth = n × displacement (the paraxial approximation). At high numerical aperture the
  real focus lies deeper still and spreads (Everall, *Appl. Spectrosc.* 54, 2000), so treat corrected depths as
  estimates. A layer of known thickness, or an immersion objective, calibrates them.
- **Safety.** The Z range stays within `limits.max_dz_um` of the focus. Z returns to the focus at the end. Check
  the objective's working distance before going deep with the x100.
- **Aiming first.** To measure at a feature, run a `move` with `to_um` (and `stay`) before the depth study.
- **LabSpec's own depth mapping.** The automation interface can also run a native Z map (`SetMappingParams` on
  `Z`). ls6t measures point by point instead: every spectrum is checked, the profile grows live in LabSpec, and
  stops are immediate. Native maps remain an option for very long profiles (planning.md).

On the simulator, a 12 µm polystyrene film (n = 1.59) on Si gives the Si rise at 12.6 µm and the film's fall at
12.3 µm, against 12.

## The spectral zero: calibration

At the start of a session, measure a sample with strong, well-known lines and check the scale. A `calibration`
study measures the reference lines (`reference`: `si`, 520.7 cm⁻¹, by default; or `polystyrene`, `cyclohexane`,
`naphthalene`, `diamond`, the ASTM E1840 values; or `lines_cm1`).

- **One line** gives the offset (the zero).
- **Lines spread over 300 cm⁻¹ or more** give offset and scale: `true = a + b × measured`. The scale is the
  dispersion coefficient LabSpec calls *Koeff*; the offset is its *Zero*.
- **`pass`**: every line within `tol_cm1` (1 cm⁻¹).

What happens next depends on `adjust`:

| `adjust` | What happens |
|---|---|
| `none` (default) | The result says how far off the scale is; a failed check makes the result unreliable |
| `labspec` | LabSpec shows the operator the error and how much the line must move (cm⁻¹ and nm). The operator corrects the zero (and the coefficient) in *Maintenance > Instrument calibration*, presses OK, and ls6t measures again, up to `calibration.max_retries` (2) times. Once LabSpec is right, any ls6t correction for that laser and grating is dropped |
| `ls6t` | ls6t remembers the correction for this laser and grating (`xcal.json`) and applies it to every later spectrum's Raman shift. Each result says so (`x_correction`, a warning with its age). LabSpec itself is not changed |

The automation interface has no command to change LabSpec's zero or coefficient, which is why `labspec` goes
through the operator. On the simulator, a scale reading 1.5 cm⁻¹ high is measured at +1.52; with polystyrene and
a 0.2 % scale error the fit gives the scale to within 0.0003.

## White-light background

When the white light (the video illumination) stays on during measurements, it adds a broad background to every
spectrum. A `background` study takes it once, and later spectra subtract it.

1. **Taking it.** Give the same grating, range and (ideally) exposure as the spectra it will correct.
   - LabSpec asks the operator to turn the white light on as it will be during the measurements.
   - The laser is kept off. With `laser_off: shutter` (needs the shutter motor in the profile), the bridge closes
     the laser shutter for the acquisition and restores the shutter mode afterwards. With `operator`, LabSpec asks
     the operator to block the laser (*Laser Off*), and to unblock it afterwards.
   - The background is stored by `name` as counts per second per accumulation (`LS6T_HOME\backgrounds`), with its
     settings.
   - If it shows narrow lines, the laser was probably not blocked, and a warning says so.
2. **Using it.** `acquire.subtract_background: "<name>"` in any study.
   - Each spectrum gets the background scaled to its exposure, interpolated onto its axis, and subtracted before
     baseline and fits.
   - Raw files are kept. Results record what was subtracted (`background`), and warn when the grating or laser
     differs.

`references` (tool) / `ls6t references show` lists the backgrounds, the spectral corrections, the beam offsets and
the alignment state. `ls6t references clear ...` removes them.

## The laser spot: finding it and remembering it

LabSpec marks the laser at the stage position, the centre of the video image. The beam itself sits close to it
but drifts. A `spot` study finds where it really is and remembers the offset (beam − marker, µm) for the objective
and laser in use (`LS6T_HOME\spot.json`, with history). From then on:

- snapshots draw the red cross on the beam, not on LabSpec's marker;
- `move.to_um` puts the beam, not the marker, on a point picked in a snapshot;
- `spot_status` (tool) / `ls6t spot status` shows the offsets, their age and the drift since the first record.

Three ways to find it, tried in this order by `method: "auto"`:

1. **The shutter** (`shutter`). With a laser shutter motor in the profile: a frame without and with the beam
   (the snapshot procedure); the difference is the spot alone.
2. **During an acquisition** (`acquisition`). LabSpec opens the shutter itself for a Raman acquisition
   (`acq_exposure_s`, 2 s). The bridge takes a frame before it and one half-way through. This needs no shutter
   motor, provided the camera sees the sample during acquisitions (TESTING Q23).
3. **By the Raman signal** (`raman`), the trial-and-error way, for when the camera doesn't show the beam:
   - Pick a small feature on the video: dust, a particle, a pit. Every snapshot lists candidates under `features`
     and numbers them on the PNG; Claude can pick one, or give `feature_um` read off the axes. ls6t refines its
     centre on the video.
   - The stage scans a small grid around it (x100: ±2 µm in 0.5 µm steps; scaled by the objective).
   - The Si line (or `line_cm1`) is measured at each point. Where it changes most is where the beam sat on the
     feature: a dip for dust on Si, a peak for a particle that has the line.
   - Offset = feature − stage there. The scan's image shows it.
   - On the simulator it recovers a 1.1 / 0.6 µm offset to within 0.03 µm.

A record is kept only if the offset is plausible (≤ `spot.max_offset_um`, 10 µm). A change of more than
`spot.drift_warn_um` (1 µm) from the previous record is reported as drift. Snapshots taken with the laser on (or
during an acquisition) in any study also update the record when the spot is found. `ls6t spot forget` clears the
records, for example after the instrument is serviced.

## Alignment and guided adjustments

**Alignment.** HORIBA's AutoAlignment (LabSpec, *Maintenance* tab) checks the Si reference's Raman intensity and,
below its thresholds, adjusts the internal optics. It is needed for each laser before it is used, and calibration
follows it. The automation interface has no command for it, and it needs a person: the laser at full power at the
source, focus on the HORIBA Si reference with the x100. ls6t therefore does what can be done from outside:

1. **The baseline.** Right after a successful AutoAlignment, run an `alignment` study for that laser (give the
   grating, filter and objective the first time). The maintainer records it:
   `ls6t alignment record <request>`. This stores the laser's fixed check settings and its Si signal
   (counts/s), width and position. Only the command line records baselines; an LLM cannot.
2. **The check.** An `alignment` study with only `setup.laser_nm` repeats the measurement at those settings.
   - It passes when the Si signal is at least `alignment.min_ratio` (0.7) of the baseline, and its width at most
     `max_fwhm_ratio` (1.3) times the baseline's.
   - A different filter is scaled by its transmission.
   - Every check goes into `alignment_log.jsonl`, so drift over weeks shows.
3. **Guided AutoAlignment.** If the check fails (and `guided`, the default), LabSpec shows the operator the failure
   and HORIBA's checklist, and asks them to run AutoAlignment and press OK. The check then repeats once
   (`alignment.max_retries`). If it still fails, the recommendation is HORIBA's: contact service.
4. **The beam's offset.** A video snapshot follows (with the laser if the profile names the shutter). AutoAlignment
   moves LabSpec's laser marker, so a warning says when `video.laser_offset_um` needs updating
   (`ls6t profile set video.laser_offset_um "[x, y]"`).
5. **Readiness.** Once baselines exist, every other study warns when its laser's last check failed, is missing, or
   is older than `alignment.max_age_h` (24 h). `alignment_status` (tool) / `ls6t alignment status` shows the state
   per laser.

**Guided adjustments (`guide`).** Some adjustments only a person can make: the laser power at the source, focus by
hand, a mirror or a polariser. A `guide` study turns them into a closed loop with the operator:

- LabSpec shows a small panel with the instruction (written by the LLM, e.g. *"Raise the laser power at the source
  slowly"*), the line's signal after every reading, and the goal.
- The loop runs **inside the bridge**: a short acquisition (`exposure_s`, default 0.2 s), the line's height above
  its window's edges per second, the decision, the display. No round trip through Python or the LLM, so a reading
  takes the exposure plus LabSpec's overhead (`period_s` in the result, measured).
- The goals:
  - `reach`: the signal ≥ `target` for `stable_n` readings in a row;
  - `drop`: ≤ `target`;
  - `maximize`: the signal rose, then fell `drop_frac` below its best twice in a row. The best position has just
    been passed, and the message says *"STOP – you just passed the best: go back slightly"*.
- Near saturation (90 % of full scale) it stops at once and says to reduce the power.
- The operator can end it with *Done* or *Abort*. Without LabSpec's dialog, the status bar and a final message box
  carry the same information.
- The LLM sets up the goal and reads the result afterwards (`reason`, `value`, the history and its plot). The STOP
  itself never waits for the LLM.

`alignment` with `focus_guide: true` starts with such a loop: focus on the Si until STOP.

## The checker

**Every run checks itself.** A result is *unreliable* when:

- any spectrum is saturated: pixels at 98 % of the detector's full scale;
- the detector is warmer than `detector.max_temp_c`;
- a set-up move was not reached (read-back differs from what was asked);
- the calibration's Si line is off by more than `checks.si_tol_cm1`;
- it was stopped before the end (operator, cancel, bridge gone).

Warnings cover spikes left (outside fitted peaks), weak peaks (SNR below `checks.min_snr`), peaks not detected (SNR
below `checks.detect_snr`: such fits are reported as not detected, and maps show them as empty), ranges not
covered, a missing profile, and expected values out of tolerance.

**The analysis is ours**, so the same file always gives the same numbers:

- Raman shift from wavelength uses the laser line read back from the instrument.
- Iterative polynomial baseline.
- Noise from second differences (robust to peaks).
- Peak fits: pseudo-Voigt, Lorentz or Gauss with a local linear background (scipy), with standard errors and R².
- Band integrals after the baseline.

**Benchmarks** (`ls6t bench`, on the simulator; known answers; 2026-11-19):

| Case | Checks | Result |
|---|---|---|
| `si_calibration` | Si line 520.7 ± 0.3 cm⁻¹, pass, reliable | 520.71 |
| `graphene_map` | G found at the 9 flake points of 15, G 1585 ± 2, 2D 2680 ± 3 | 9; 1584.97; 2680.03 |
| `extended_range` | Si, G, 2D in one stitched 300–2900 cm⁻¹ spectrum; range covered | 520.74; 1585.04; 2680.13 |
| `saturation` | a 30 s exposure at 100 % is flagged unreliable | flagged |
| `series` | 3 spectra, Si 520.7 ± 0.3 | 520.65 |
| `spot_video` | the beam 1.5 / −0.8 µm from the marker, seen in a frame during an acquisition | 1.52 / −0.80 |
| `spot_raman` | the beam 1.1 / 0.6 µm from the marker, found by scanning over a dust particle on Si | 1.07 / 0.58 |
| `focus_scan` | the sample's best focus is 2.3 µm above the operator's; the scan finds it | 2.293 |
| `guided_focus` | the simulated operator passes the best focus at the 8th reading: STOP (`past_max`) comes right after | 12 readings |
| `depth_profile` | a 12 µm polystyrene film (n = 1.59) on Si: Si rises and PS falls at 12 µm (corrected), PS rises at the surface | 12.63; 12.28; 0.31 |
| `calibration_zero` | the scale reads 1.5 cm⁻¹ high; the operator corrects LabSpec's zero when asked; the second check passes | +1.52, then +0.04 |
| `white_light` | a background with the white light on and the laser blocked by the operator: no Raman line in it | clean |
| `native_map` | the graphene map on LabSpec's engine: the same 9 graphene points as point by point | 9; 1585.0 |
| `operator_stop` | the operator stops after 4 of 10 map points: partial, unreliable, `stopped_by: operator` | as expected |

On the instrument, the standing check is the `calibration` study on a silicon wafer (or polystyrene, for the scale
too), at the start of each session.

**Assumptions about LabSpec**, each with what checks it (TESTING.md lists the tests):

| Assumption | Checked by |
|---|---|
| Scripts may use `Scripting.FileSystemObject` and `Scripting.Dictionary` | discovery writes its file |
| `Message(text, 8)` opens a non-blocking window; `Message("", 8)` returns -1 while open, 0 once closed | discovery self-test |
| `MessageEx("", 14)` returns 1 when LabSpec's stop button for scripts was pressed (where LabSpec has one) | discovery self-test; TESTING T2.4 |
| `MoveMotor(..., mode + 10)` returns a move ID at once, and `GetMotorStatus` reaches 0 | set-up read-back on every move |
| `GetAcqID` returns 0 during an acquisition, then the new ID once; -2 after a cancel | every acquisition; TESTING T4 |
| `GetValue(id, "XYData")` gives x in the spectrum axis unit (`AxisUnits` for `AxisType` "Spectr") | Si line at 520.7 cm⁻¹ |
| `Acq(4, …)` returns the detector temperature | `checks.detector_temp_c` |
| Map/Profile build live views from single spectra | TESTING T6 (visual) |
| A dynamic `Dialog` (LabSpec 6) stays open and updates (`DIALOG_UPDATE_VAR`) while acquisitions run | TESTING T5d |
| `Message(text, 1)` (OK/Cancel) returns 0 for OK | TESTING T5c |
| `Video(2)` gives the live frame's id; `Exec(id, 3)` clones it; `GetValue(clone, "Data")` gives the pixels, X varying fastest, colours packed as COLORREF (`video.order`, `video.packing`) | TESTING T5b: orientation and colours |
| Video axes are stage coordinates in µm ("mkm"), so the stage position marks the laser | TESTING T5b: the marker against the beam |
| The live video keeps updating during a Raman acquisition, so a frame half-way through shows the beam | TESTING T5e.1 |
| `PutValue(0, "ShutterMode", 1)` and moving the shutter motor to 1 / 0 open and close the laser shutter | TESTING T5b, with the maintainer |

## How it works

```
Claude app ──MCP──> ls6t-mcp ──> request folder + runner process (ls6t run-request)
                                     │  files: <bridge>\inbox\*.cmd, outbox\*.res, heartbeat.txt, stop.flag
                                     ▼
                          ls6t_bridge.vbs inside LabSpec 6 ──> the instrument
```

| Module | Role |
|---|---|
| `config.py` | Settings (`LS6T_*`) and folders |
| `profile.py` | The machine profile: defaults, loading, discovery import, filter labels, nm ↔ cm⁻¹ |
| `format.py` | `study_format()` and validation |
| `plan.py` | A study as commands: set-up moves, positions (snake order), range in nm and the number of windows, the time estimate |
| `bridge.py` | The file protocol (below) |
| `runner.py` | Runs a request in its own process: instrument lock, confirmation, set-up with read-back, points, live views, analysis, checks, results written atomically |
| `spectra.py` | Spectrum files, Raman shift, baseline, noise, spikes, saturation, peak fits, bands |
| `render.py` | PNG plots (optional extra `render`) |
| `video.py` | Video frames: decoding, the laser marker, features, finding the spot, the PNG |
| `spot.py` | The beam's offsets per objective and laser, with history and drift |
| `xcal.py` | Reference lines (ASTM E1840) and ls6t's spectral corrections per laser and grating |
| `background.py` | White-light backgrounds: storing and subtracting them |
| `typelib.py` | `ls6t-typelib`: reads LabSpec's ActiveX type library straight from the file, on any system (read-only), and lists every method, marking those not in the 2018 manual |
| `sim.py` | A simulated LabSpec + XploRA speaking the same protocol (`LS6T_MODE=sim`, tests, benchmarks) |
| `bench.py` | The benchmarks |
| `api.py`, `cli.py`, `mcp_server.py` | The functions, the command line, the MCP server |
| `testmode.py` | Test mode: TESTING.md read as a plan, single bridge commands with guards and the operator's Yes for the laser, the test log and report (`hardware_test`, `ls6t test`) |
| `clients.py` | `ls6t connect`: finds the LLM apps (Claude desktop app, Claude Code, ChatGPT desktop app / Codex, Gemini CLI, Cursor, GitHub Copilot, Windsurf, LM Studio) and adds or removes the MCP server in their files (JSON or TOML), backed up, other settings kept |
| `winsetup.py` | What the Windows installer does beyond copying files: checks, precompiling, the LabSpec scripts, PATH, the LLM apps (and undoing them) |
| `installer/` | The Windows installer: `ls6t.nsi` (NSIS), `build_installer.py`, the launchers (`bin/`) |
| `vbs/common.vbs`, `vbs/bridge_main.vbs`, `vbs/discover_main.vbs`, `vbs/script_motor_main.vbs` | The LabSpec scripts' parts; `ls6t bridge install` puts them together (the script-motor logger is optional: TESTING.md T1.4) |

**The bridge protocol (1).** ls6t writes a command as `inbox\<seq>.cmd`: `key=value` lines, UTF-16, written to a
`.tmp` file and renamed. `verb` and `seq` come first, and `seq` is a 20-digit number; the bridge runs the lowest
first. The bridge answers in `outbox\<seq>.res`, with `ok` (1/0), `error`, then the verb's keys. It rewrites
`heartbeat.txt` about once a second (`state`: idle, busy, halted, stopped). ls6t considers the bridge gone when the
heartbeat is older than `LS6T_HEARTBEAT_S`, except while the bridge waits on a Yes/No question to the operator.
`stop.flag` names the command it stops. Values use a `.` decimal point whatever the locale, a line end is written
`%0A` and `%` is written `%25`. Spectra never travel in a result: the bridge writes them to the file the command
names.

Verbs: `ping`, `status`, `discover`, `move` (`motor`, `mode`: value, label or index, `value`), `move_xy`,
`autofocus`, `acquire`, `view` (live map or profile in LabSpec: create, add, save), `save`, `remove`, `confirm`,
`message` (status bar), `safe`, `resume`, `quit`, `map_native` (LabSpec's mapping engine; `chunk` values per
`GetValueSimplePart` when written out, 0 for one `GetValue`), `video` (a frame: `out`, `settle_s`, `laser_s`,
`m_laser_shutter`, `formats`, `camera`, `extended`; written as `<out>.video.txt`, with `<out>.nolaser.video.txt` when
the laser was on), `config` (`action` save or load, `path`, `show`), `last_error`, `guide` (the fast loop: `text`, `mode`, `target`, `from_nm`/`to_nm`, the line's window
`lo_nm`/`hi_nm` and `lo_cm1`/`hi_cm1`, `exposure_s`, `max_s`, `stable_n`, `drop_frac`, `sat`, `out`, `ui`),
`notice` (a message the operator acknowledges: OK or Cancel).

## For LLM tools: the MCP server

`ls6t-mcp` (stdio; `--http PORT` for streamable HTTP on 127.0.0.1). It answers the handshake at once (0.3 s
measured). Any app that starts local MCP servers can use it: the Claude desktop app, the ChatGPT desktop app / Codex,
Gemini CLI, Cursor, Claude Code, GitHub Copilot (VS Code, Visual Studio, CLI), Windsurf, LM Studio. `ls6t connect` (and the installer) adds it to those found on the PC ([INSTALL.md, step
8](INSTALL.md#8-connect-an-llm-app)).

| Tool | What it does |
|---|---|
| `study_format` | The format, this instrument's choices and limits, and status (bridge running, which request holds the instrument). Read first. |
| `validate_studies` | Static checks with estimates (minutes, points, windows) |
| `run_studies` | Starts a request; returns within `wait_s` (≤ 50 s) with results or progress |
| `study_status` | Results, or progress with `eta_min` |
| `cancel`, `jobs` | Stop a request; recent requests |
| `instrument` | `status`, `discover` (proposes a profile), `resume` (after an operator halt) |
| `alignment_status` | Per laser: the baseline, the last check, its age (read-only) |
| `references` | Spectral corrections, white-light backgrounds, beam offsets, alignment state (read-only) |
| `snapshot` | A video frame now, with the laser position marked; `with_laser` shows the beam and measures its offset. Returns the image. |
| `get_inputs` | The commands of one study, without running |
| `run_benchmarks` | The benchmarks on the simulator |
| `get_file`, `get_image` | Files as base64; PNGs as images |

Polling costs tokens: when a run needs more than about 5 minutes, Claude tells the user when it should finish and
stops; the result is collected later with `study_status`.

## Settings

Environment variables only (in the Claude config's `env` block for the server; `$env:NAME = "value"` in PowerShell):

| Variable | Meaning | Default |
|---|---|---|
| `LS6T_MODE` | `labspec` (the instrument) or `sim` (the simulator) | `labspec` |
| `LS6T_HOME` | profile, bridge folder, scripts, discovery dumps, instrument lock | `%USERPROFILE%\ls6t` |
| `LS6T_OUT` | request folders | `%USERPROFILE%\ls6t_runs` |
| `LS6T_BRIDGE_DIR` | the folder the bridge watches | `%LS6T_HOME%\bridge` |
| `LS6T_PROFILE` | the machine profile | `%LS6T_HOME%\profile.json` |
| `LS6T_CONFIRM` | operator confirmation: `request`, `study` or `never` | `request` |
| `LS6T_HEARTBEAT_S` | the bridge counts as gone after this many seconds without a heartbeat | `20` |
| `LS6T_MAX_MINUTES` | a lower ceiling than the profile's `max_minutes` | unset |

`ls6t bridge install` writes the bridge folder into the scripts. If you change `LS6T_HOME` or `LS6T_BRIDGE_DIR`,
install the scripts again with the same settings.

## Testing

- **From the chat**: in test mode (`ls6t connect APP --test`) the LLM walks the maintainer through TESTING.md with a
  `hardware_test` tool and records the results ([TESTING.md, test mode](TESTING.md#running-the-tests-from-the-chat-test-mode)).
- **Without the instrument**: `pytest` (format, plan, protocol, analysis, full runs on the simulator, operator stop
  and decline, cancel, saturation, the LabSpec scripts' structure, the MCP server) and `ls6t bench`.
- **With the instrument**: [TESTING.md](TESTING.md), a staged plan to carry out with the instrument's maintainer, from
  discovery (no laser, no motion) to Claude driving full maps.

## Files

```
labspec_translator/
  README.md  INSTALL.md  FUNCTIONALITY.md  TESTING.md  PROMPTS.md  CHANGELOG.md  planning.md  planning_log.md  pyproject.toml
  examples/studies.json          thirteen studies covering the scope
  labspec_translator/            the package (config, profile, format, plan, bridge, runner, spectra, render, spot,
                                 xcal, background, store, video, sim, bench, api, cli, mcp_server, typelib, winsetup;
                                 vbs/ the LabSpec script parts)
  installer/                     the Windows installer: ls6t.nsi, build_installer.py, bin/ (launchers)
  .github/workflows/installer.yml   builds, tests and publishes the installer for each version tag
  tests/                         conftest.py (each test on the simulator in its own folders), helpers.py, and one
                                 file per subsystem: test_format, test_runs, test_video, test_procedures,
                                 test_typelib, test_bringup, test_install, test_testmode, test_instrument
```

A request folder (`LS6T_OUT\run_<date>_<time>_<id>\`) holds `request.json`, `studies.json`, `validation.json`,
`state.json`, `start.json` (the instrument at the start), `runner.json` and `runner.log`. Per study it holds
`progress.json`, `spectra\`, the result `<name>.json`, map files, `images\` and LabSpec's `.l6s` files.
Snapshots taken outside a study go to `LS6T_OUT\snapshots\snap_<date>_<time>_<id>\` (`frame.png`, the raw
frames, LabSpec's image files, `snapshot.json`); a study's frames go to its `video\` folder.

## Versions

What changed in each version: [CHANGELOG.md](CHANGELOG.md). `ls6t --version` prints the one installed.

Author: Nicola Ferralis. License: MIT.
