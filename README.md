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

First instrument: **XploRA** (LabSpec 6, Windows 11). Version 2026.11.19.2.

**Scope of version 1**

| Kind | What |
|---|---|
| `spectrum` | One spectrum at the current spot, or one per entry of `points` (offsets in µm) |
| `map` | A grid of spectra: XY maps, line scans, depth (Z) profiles, or XYZ |
| `series` | Repeated spectra at one spot: a time series |
| `calibration` | A spectrum of a silicon reference; fits the Si line and checks it against 520.7 ± 1 cm⁻¹ |

**Video snapshots**: a frame of LabSpec's camera with the laser position marked, on demand (`snapshot`) or during a
study (`video`, on by default for calibration). Optionally the laser shutter opens for about a second: the beam
shows, ls6t finds the spot, and reports its offset from LabSpec's laser position.

Not yet included: LabSpec's own (native) fast mapping engine, Linkam temperature set-points, polarisation and PMT
detectors. See `planning.md`.

**Contents:**

- [Installation on Windows 11](#installation-on-windows-11)
- [First session at the instrument](#first-session-at-the-instrument)
- [Use](#use)
- [A study](#a-study)
- [Results](#results)
- [Safety and the operator](#safety-and-the-operator)
- [Video snapshots: where the beam is](#video-snapshots-where-the-beam-is)
- [The checker](#the-checker)
- [How it works](#how-it-works)
- [For LLM tools: the MCP server](#for-llm-tools-the-mcp-server)
- [Settings](#settings)
- [Testing](#testing)
- [Files](#files)
- [Versions](#versions)

---

## Installation on Windows 11

Everything is installed on the **instrument PC**: the Windows 11 computer that runs LabSpec 6. The Claude desktop app
runs there too. The steps below use **PowerShell** (Start, type *PowerShell*, open *Windows PowerShell* or
*Terminal*). Use your normal Windows account, the **same account that runs LabSpec**, so both programs can read and
write the bridge folder. Administrator rights are needed only for installing Python and Git, if they are missing.

### 1. Python and Git

ls6t needs Python 3.10 or newer (3.12 recommended) and Git.

With **winget** (built into Windows 11):

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Or install them from [python.org](https://www.python.org/downloads/windows/) and [git-scm.com](https://git-scm.com/download/win).
In the Python installer, tick **"Add python.exe to PATH"** and keep the **py launcher**.

Close and reopen PowerShell, then check:

```powershell
py -3.12 --version     # Python 3.12.x
git --version
```

If `py` is not found, the launcher was not installed: run the Python installer again ("Modify") and select it.

### 2. Choose the folders (and keep them out of OneDrive)

Windows 11 often syncs **Documents** and **Desktop** to OneDrive. The bridge exchanges many small files every second,
and a sync client renaming or locking them breaks the exchange. Keep everything in folders OneDrive does not sync:

| What | Default | Notes |
|---|---|---|
| The code | `C:\Users\<you>\Software\labspec_translator` | anywhere outside OneDrive; short paths are easiest |
| ls6t's home: profile, bridge folder, LabSpec scripts, discovery dumps | `C:\Users\<you>\ls6t` (`LS6T_HOME`) | your user folder itself is not synced by OneDrive |
| Results (one folder per request) | `C:\Users\<you>\ls6t_runs` (`LS6T_OUT`) | can grow large with maps |

To check what OneDrive syncs: OneDrive icon in the taskbar, Settings, *Sync and back up*, *Manage back up*.

### 3. Get the code

```powershell
mkdir $HOME\Software -Force
cd $HOME\Software
git clone https://github.com/feranick/labspec_translator.git
cd labspec_translator
```

(Without network access, copy the folder from another computer instead; see [Offline install](#offline-install).)

### 4. A virtual environment and the package

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[mcp,render,test]"
```

- **`Activate.ps1` cannot be loaded because running scripts is disabled**: allow local scripts for your account once,
  then activate again:

  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```

  Or skip activation and call the programs by their full path, e.g. `.\.venv\Scripts\ls6t.exe status`.
- The extras: `mcp` (the server for the Claude app), `render` (PNG plots, matplotlib), `test` (pytest).
- `-e` (editable) means a `git pull` updates the installed code; reinstall only when dependencies change.

The install puts two programs in `.venv\Scripts`: `ls6t.exe` (the command line) and `ls6t-mcp.exe` (the MCP server).

### 5. Check the install (no instrument needed)

```powershell
ls6t status            # versions, settings, "bridge": {"running": false} (normal for now)
ls6t bench             # the benchmarks on the built-in simulator: "failed": []
pytest -q              # optional: the test suite (about 30 s)
```

`ls6t bench` never touches the instrument. It runs on the simulator, which answers the same commands as LabSpec.

### 6. Install the LabSpec scripts

```powershell
ls6t bridge install
```

This writes two scripts, configured with this PC's folders, to `C:\Users\<you>\ls6t\vbs\`:

| Script | When |
|---|---|
| `ls6t_discover.vbs` | Once, and again after the hardware changes: lists the instrument for the profile. It does not use the laser and moves nothing. |
| `ls6t_bridge.vbs` | Whenever ls6t should drive the instrument. It runs until stopped. |

It also creates the bridge folder `C:\Users\<you>\ls6t\bridge\` (with `inbox` and `outbox`).

In **LabSpec 6**: *Processing > Scripts*. Add both scripts (the manual's script manager allows adding, configuring and
starting scripts). Run the scripts again after every `ls6t bridge install`, for example after an update.

### 7. The machine profile

The profile says what the instrument has: lasers, gratings, filters, objectives and stage axes. It also holds the
limits a person sets. Until one exists, ls6t uses built-in XploRA defaults and says so in every result.

1. In LabSpec, run **`ls6t_discover.vbs`**. A message box shows where it wrote the dump, e.g.
   `C:\Users\<you>\ls6t\discovery_20261120_101500.txt`.
2. Import it:

   ```powershell
   ls6t profile import "C:\Users\<you>\ls6t\discovery_20261120_101500.txt"
   ```

   The output lists what was found and what to check by hand.
3. Edit the limits: `notepad $HOME\ls6t\profile.json`. In particular:
   - `lasers.<nm>.max_filter_pct`: the highest filter transmission allowed per laser. `power_mw` is the power at the
     sample at 100 %, if known (recorded with results).
   - `filters`: every label's transmission in %. Labels that could not be parsed are listed by the import.
   - `limits`: `max_xy_um`, how far a study may move from where the operator left the stage; `max_dz_um`, how far
     from the operator's focus; `max_exposure_s`, `max_accumulations`, `max_points`, `max_minutes`;
     `sensitive_max_filter_pct` for samples marked sensitive.
   - `stage.x_um` / `stage.y_um`: absolute stage limits `[min, max]`, if wanted.
   - `detector.max_temp_c`: warmer than this, results are unreliable (the CCD is not cooled down).
   - `motors.laser_shutter`: the laser shutter's motor name, needed only for video snapshots with the laser on.
     The import proposes one if it finds a shutter motor; confirm it with the maintainer.
   - `video.laser_offset_um`: where the beam really is relative to LabSpec's laser position. It is measured with a
     snapshot with the laser on (TESTING T5b).

   Only a person edits this file. Studies cannot change it, and the MCP tools can only *propose* a profile
   (`instrument(action="discover")` writes `profile.proposed.json`).

### 8. Connect the Claude desktop app

Install the Claude desktop app for Windows. In the app: *Settings > Developer > Edit Config* opens
`claude_desktop_config.json`. That is usually `%APPDATA%\Claude\claude_desktop_config.json`; for the Microsoft Store
version it sits under `%LOCALAPPDATA%\Packages\Claude_...\LocalCache\Roaming\Claude\`. Add the server (keep any other
entries; backslashes are doubled in JSON):

```json
{
  "mcpServers": {
    "ls6t": {
      "command": "C:\\Users\\<you>\\Software\\labspec_translator\\.venv\\Scripts\\ls6t-mcp.exe",
      "env": {"LS6T_CONFIRM": "request"}
    }
  }
}
```

Quit the app completely (right-click its icon in the taskbar's notification area, *Quit*) and start it again. The ls6t
tools then appear in a new chat. Settings go in `env`, for example a different `LS6T_OUT`.

To practise without the instrument, add a second entry that uses the simulator, and remove it later:

```json
    "ls6t-sim": {
      "command": "C:\\Users\\<you>\\Software\\labspec_translator\\.venv\\Scripts\\ls6t-mcp.exe",
      "env": {"LS6T_MODE": "sim", "LS6T_OUT": "C:\\Users\\<you>\\ls6t_sim_runs"}
    }
```

### 9. Check with the instrument

Start **`ls6t_bridge.vbs`** in LabSpec. A small window appears: *"ls6t (labspec-translator) is controlling the
instrument. Close this window to STOP"*. LabSpec's status bar shows *"ls6t bridge … running"*. Then:

```powershell
ls6t status                 # "bridge": {"running": true, "heartbeat": {"state": "idle", "age_s": < 2}}
ls6t instrument status      # laser, grating, filter, objective, stage X/Y/Z, detector temperature, as in LabSpec
```

The first measurements follow [TESTING.md](TESTING.md), with the instrument's maintainer.

### Updating

```powershell
cd $HOME\Software\labspec_translator
git pull
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,render,test]"     # only needed when dependencies changed; harmless otherwise
ls6t bridge install                      # the scripts must match the package's bridge protocol
ls6t bench
```

Then, in LabSpec, stop the bridge (LabSpec's script stop button) and start the new `ls6t_bridge.vbs`, and restart
the Claude app. A bridge of another protocol version is refused with a message saying to reinstall it.

### Offline install

Instrument PCs are often kept off the network. On a connected Windows computer with the same Python version:

```powershell
git clone https://github.com/feranick/labspec_translator.git
cd labspec_translator
py -3.12 -m pip download -d wheels ".[mcp,render,test]"
```

Copy the whole folder, `wheels` included, to the instrument PC. Then, in step 4, install with:

```powershell
pip install --no-index --find-links wheels -e ".[mcp,render,test]"
```

(The Claude app itself needs network access to reach Claude.)

### Troubleshooting (Windows)

| Symptom | Cause and fix |
|---|---|
| `ls6t : The term 'ls6t' is not recognized` | The venv is not active: `.\.venv\Scripts\Activate.ps1`, or use the full path `.\.venv\Scripts\ls6t.exe`. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (once). |
| `ls6t status` shows the bridge not running while it runs in LabSpec | The two use different folders: the script's `LS6T_BRIDGE_DIR` line must match `ls6t status` → `settings.LS6T_BRIDGE_DIR`. Run `ls6t bridge install` with the same settings the server uses, then restart the script. LabSpec running under another Windows account also causes it. |
| Errors about access to files in `ls6t\bridge` | Defender's *Controlled folder access* (ransomware protection) or another antivirus. Allow `python.exe` from the venv and LabSpec, or exclude `C:\Users\<you>\ls6t`. Scanning every small file also slows the bridge. |
| SmartScreen or Defender warns about `ls6t.exe` / `ls6t-mcp.exe` | These are pip's standard launchers for the Python package; allow them, or start the server as `python.exe -m labspec_translator.mcp_server` in the Claude config. |
| A request shows `interrupted` after the Claude app was closed | The request's runner is a separate process. Windows may end it with the app if the app does not allow processes to break away from its job. Keep the app open during long runs, or start long runs from PowerShell (`ls6t run studies.json`). Measured spectra stay in the request folder. |
| Long runs stop when the PC sleeps | Settings > System > Power: never sleep while plugged in, at least during measurements. |
| Paths longer than 260 characters | Unlikely with the default folders. If needed: enable *Win32 long paths* (Group Policy, or `LongPathsEnabled` in the registry). |
| Numbers with a comma as decimal separator in LabSpec | Handled: the bridge converts every number to '.' itself, whatever the Windows locale. |
| A folder name containing `"` | Not supported in the LabSpec scripts' folder constants: choose another folder. |

---

## First session at the instrument

In short (details and pass criteria in [TESTING.md](TESTING.md)):

1. `ls6t bridge install`; in LabSpec, run `ls6t_discover.vbs`; `ls6t profile import <dump>`; set the limits.
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
ls6t instrument status | discover | resume
ls6t instrument snapshot [--laser]           # a video frame with the laser position marked (PNG)
ls6t bridge call <verb> key=value ...        # one bridge command, for the maintainer (TESTING.md)
ls6t profile show | default | import <dump>
ls6t bench                                   # the benchmarks on the simulator
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
| `sample` | `{name, notes, sensitive}`; `sensitive: true` caps the filter at `limits.sensitive_max_filter_pct`. The name goes into the LabSpec file. |
| `setup` | `{laser_nm, grating (gr/mm), filter (a profile label, e.g. "10%"), objective (label), hole_um, slit_um}`. A key left out keeps what LabSpec has; the result records what it was. |
| `acquire` | `range_cm1: [from, to]` (extended range: LabSpec stitches windows) or `center_cm1` (one window); `exposure_s` (per accumulation) or `"auto"` with `auto_exposure: {target_counts, min_s, max_s, test_s}`; `accumulations`; `spike`: `labspec`, `none`, `single`, `double`, `double_auto`; `dark`: `labspec`, `true`, `false` |
| `focus` | `{autofocus: laser, raman or video (before the first point, and every N points with every), dz_um}` |
| `points` | (spectrum) `[{x_um, y_um, dz_um}]`: offsets from where the operator left the stage |
| `map` | `{x_um, y_um, z_um: [from, to, step]}` offsets in µm; `snake: true` (default): alternate rows reversed |
| `series` | `{count, interval_s}`: seconds from one start to the next |
| `process` | `baseline: {degree}` or `false` (default: iterative polynomial, degree 3); `peaks: [{name, near_cm1, window_cm1 (40), shape: pseudo_voigt, lorentz or gauss}]`; `auto_peaks: N` (when no `peaks`: the N strongest, default 3); `bands: [{name, from_cm1, to_cm1}]` |
| `expect` | `[{quantity: "dotted.result.path", value, tol}]`, checked after the run |
| `images` | `true`: PNGs (spectra with fits; one map image per peak quantity and band) |
| `save_native` | `true` (default): LabSpec also saves `.l6s` files |
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
| `calibration` | `reference_cm1`, `measured_cm1`, `offset_cm1`, `fwhm_cm1`, `pass` |
| `expect` | Each expected value: `got`, `pass` |
| `video` | `before` / `after`: the PNG (`path`), the raw frame, LabSpec's own image files, the stage position, `marker_um`, the field of view; with the laser: `spot_um` and `measured_offset_um` |
| `images`, `files`, `seconds`, `execution` | Pictures (including `video_before` / `video_after`), paths, timing, mode and bridge version |

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
  - LabSpec's script stop button ends the bridge.
  - `ls6t cancel` (or the `cancel` tool) stops a request from the PC.
- **Positions**: XY moves are offsets from where the operator left the stage when the request started. Z is relative
  to the operator's focus (or the last autofocus). Absolute positions are never taken from a study.
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
| `operator_stop` | the operator stops after 4 of 10 map points: partial, unreliable, `stopped_by: operator` | as expected |

On the instrument, the standing check is the `calibration` study on a silicon wafer, at the start of each session.

**Assumptions about LabSpec**, each with what checks it (TESTING.md lists the tests):

| Assumption | Checked by |
|---|---|
| Scripts may use `Scripting.FileSystemObject` and `Scripting.Dictionary` | discovery writes its file |
| `Message(text, 8)` opens a non-blocking window; `Message("", 8)` returns -1 while open, 0 once closed | discovery self-test |
| `MessageEx("", 14)` returns 1 when LabSpec's script stop button was pressed | discovery self-test; TESTING T2.4 |
| `MoveMotor(..., mode + 10)` returns a move ID at once, and `GetMotorStatus` reaches 0 | set-up read-back on every move |
| `GetAcqID` returns 0 during an acquisition, then the new ID once; -2 after a cancel | every acquisition; TESTING T4 |
| `GetValue(id, "XYData")` gives x in the spectrum axis unit (`AxisUnits` for `AxisType` "Spectr") | Si line at 520.7 cm⁻¹ |
| `Acq(4, …)` returns the detector temperature | `checks.detector_temp_c` |
| Map/Profile build live views from single spectra | TESTING T6 (visual) |
| `Video(2)` gives the live frame's id; `Exec(id, 3)` clones it; `GetValue(clone, "Data")` gives the pixels, X varying fastest, colours packed as COLORREF (`video.order`, `video.packing`) | TESTING T5b: orientation and colours |
| Video axes are stage coordinates in µm ("mkm"), so the stage position marks the laser | TESTING T5b: the marker against the beam |
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
| `video.py` | Video frames: decoding, the laser marker, finding the spot, the PNG |
| `sim.py` | A simulated LabSpec + XploRA speaking the same protocol (`LS6T_MODE=sim`, tests, benchmarks) |
| `bench.py` | The benchmarks |
| `api.py`, `cli.py`, `mcp_server.py` | The functions, the command line, the MCP server |
| `vbs/common.vbs`, `vbs/bridge_main.vbs`, `vbs/discover_main.vbs` | The LabSpec scripts' parts; `ls6t bridge install` puts them together |

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
`message` (status bar), `safe`, `resume`, `quit`, `video` (a frame: `out`, `settle_s`, `laser_s`,
`m_laser_shutter`, `formats`, `camera`; written as `<out>.video.txt`, with `<out>.nolaser.video.txt` when the
laser was on).

## For LLM tools: the MCP server

`ls6t-mcp` (stdio; `--http PORT` for streamable HTTP on 127.0.0.1). It answers the handshake at once (0.3 s
measured).

| Tool | What it does |
|---|---|
| `study_format` | The format, this instrument's choices and limits, and status (bridge running, which request holds the instrument). Read first. |
| `validate_studies` | Static checks with estimates (minutes, points, windows) |
| `run_studies` | Starts a request; returns within `wait_s` (≤ 50 s) with results or progress |
| `study_status` | Results, or progress with `eta_min` |
| `cancel`, `jobs` | Stop a request; recent requests |
| `instrument` | `status`, `discover` (proposes a profile), `resume` (after an operator halt) |
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

- **Without the instrument**: `pytest` (format, plan, protocol, analysis, full runs on the simulator, operator stop
  and decline, cancel, saturation, the LabSpec scripts' structure, the MCP server) and `ls6t bench`.
- **With the instrument**: [TESTING.md](TESTING.md), a staged plan to carry out with the instrument's maintainer, from
  discovery (no laser, no motion) to Claude driving full maps.

## Files

```
labspec_translator/
  README.md  TESTING.md  planning.md  pyproject.toml
  examples/studies.json          six studies covering the scope
  labspec_translator/            the package (config, profile, format, plan, bridge, runner, spectra, render,
                                 video, sim, bench, api, cli, mcp_server; vbs/ the LabSpec script parts)
  tests/test_ls6t.py
```

A request folder (`LS6T_OUT\run_<date>_<time>_<id>\`) holds `request.json`, `studies.json`, `validation.json`,
`state.json`, `start.json` (the instrument at the start), `runner.json` and `runner.log`. Per study it holds
`progress.json`, `spectra\`, the result `<name>.json`, map files, `images\` and LabSpec's `.l6s` files.
Snapshots taken outside a study go to `LS6T_OUT\snapshots\snap_<date>_<time>_<id>\` (`frame.png`, the raw
frames, LabSpec's image files, `snapshot.json`); a study's frames go to its `video\` folder.

## Versions

| Version | Changes |
|---|---|
| 2026.11.19.2 | Video snapshots: `video` bridge verb (frame cloned from the live video; optional timed laser shutter with a frame without and with the beam), `snapshot` tool and `ls6t instrument snapshot`, study key `video` (on by default for calibration), the laser position marked, the spot found and its offset measured; profile `video` settings and `motors.laser_shutter` (from discovery). |
| 2026.11.19.1 | First version: study format and validation, VBS bridge (protocol 1) and discovery script, runner with operator confirmation and stops, analysis and checks, simulator and benchmarks, CLI and MCP server. Not yet tested on the instrument (TESTING.md). |

Author: Nicola Ferralis. License: MIT.
