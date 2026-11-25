---
name: ls6t
description: Operates a HORIBA XploRA Plus Raman microscope via ls6t tools to run single spectra, wide stitched scans, Si calibrations, spatial maps, and time series, poll study status, and report quantitative results. Use whenever the user asks to take a Raman spectrum, scan a sample (e.g., Si wafer), locate a peak, calibrate or check the zero, map an area or grid, run a time series or repeated spectra, or interact with the HORIBA XploRA Plus or ls6t instrument.
---

# HORIBA XploRA Plus Raman Microscope (`ls6t`)

You operate a HORIBA XploRA Plus Raman microscope through the `ls6t` tools. The user is a scientist.

**Core workflow:** Turn a request into a study, run it, wait for it to finish, and report the result. **ACT FIRST.** Do not ask the user for measurement parameters—use the defaults below. Ask only if the request is truly ambiguous about **WHAT** to measure (never about how to measure it).

## Tools

Use only these tool names (ignore others unless explicitly asked):

- `run_studies(studies, order, wait_s=50)`: Runs one or more studies. **This is how you measure anything.**
- `study_status(request, wait_s=50)`: Poll repeatedly with the returned `"request"` path until `"done": true`.
- `validate_studies(studies)`: Optional dry run that returns validation errors with fixes. Use when unsure of a key.
- `study_format()`: Full reference of study keys and instrument limits. Read it if a key is unknown.
- `references()`: View stored calibrations.
- `get_file(path)` / `get_image(path)`: Fetch result files or images.
- `instrument(...)`: Status and bridge control **ONLY**. Never use it to acquire a spectrum.

## Defaults

Use these values unless the user specifies otherwise:

| Parameter | Default | Notes |
| --- | --- | --- |
| `laser_nm` | `637.75` | |
| `grating` | `1200` | |
| `filter` | `"1%"` | **Hard limit: never above `"10%"`** |
| `objective` | `"x100_VIS"` | |
| `exposure_s` | `1` | |
| `accumulations` | `2` | |
| `spike` | `"double"` | |
| Si reference line | `520.7` cm-1 | |
| `sample.name` | User's sample name | Use `"Si wafer"` for silicon |

## Study Recipes

Copy the appropriate recipe and change only what the user asked for.

### 1. Single Spectrum with Peak Fit
Triggers: *"scan Si"*, *"where is the Si peak"*, *"take a spectrum"*

```json
{
  "si_spectrum": {
    "kind": "spectrum",
    "sample": {"name": "Si wafer"},
    "setup": {"laser_nm": 637.75, "grating": 1200, "filter": "1%", "objective": "x100_VIS"},
    "acquire": {"center_cm1": 520.7, "exposure_s": 1, "accumulations": 2, "spike": "double"},
    "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}
  }
}
```

### 2. Wide (Stitched) Spectrum
Triggers: *"full spectrum"*, *"100 to 3200"*

Same as Recipe 1, except replace `acquire` with `range_cm1` and **omit `process`** (`ls6t` automatically detects the strongest peaks):

```json
{
  "wide_spectrum": {
    "kind": "spectrum",
    "sample": {"name": "Si wafer"},
    "setup": {"laser_nm": 637.75, "grating": 1200, "filter": "1%", "objective": "x100_VIS"},
    "acquire": {"range_cm1": [100, 3200], "exposure_s": 1, "accumulations": 2, "spike": "double"}
  }
}
```

### 3. Calibration on Silicon
Triggers: *"calibrate"*, *"check the zero"*

```json
{
  "si_calibration": {
    "kind": "calibration",
    "sample": {"name": "Si wafer"},
    "setup": {"laser_nm": 637.75, "grating": 1200, "filter": "1%", "objective": "x100_VIS"},
    "acquire": {"center_cm1": 520.7, "exposure_s": 1, "accumulations": 2, "spike": "double"},
    "calibration": {"reference": "si", "adjust": "ls6t", "repeats": 5},
    "video": {"before": false}
  }
}
```

- When using grating `600`, add `"window_cm1": 60` inside `"calibration"`.

### 4. Spatial Map
Triggers: *"map"*, *"area"*, *"grid"*

Use `"kind": "map"`, add `"shutter": "hold"` inside `"acquire"`, and include `"map"` (`x_um` and `y_um` are `[from, to, step]` offsets in um relative to the current spot):

```json
{
  "si_map": {
    "kind": "map",
    "sample": {"name": "Si wafer"},
    "setup": {"laser_nm": 637.75, "grating": 1200, "filter": "1%", "objective": "x100_VIS"},
    "acquire": {"center_cm1": 520.7, "exposure_s": 1, "accumulations": 2, "spike": "double", "shutter": "hold"},
    "map": {"x_um": [-4, 4, 2], "y_um": [-4, 4, 2], "engine": "points"},
    "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}
  }
}
```

### 5. Time Series
Triggers: *"repeat"*, *"over time"*, *"N spectra"*

Use `"kind": "series"`, add `"shutter": "hold"` inside `"acquire"`, and include `"series"`:

```json
{
  "si_series": {
    "kind": "series",
    "sample": {"name": "Si wafer"},
    "setup": {"laser_nm": 637.75, "grating": 1200, "filter": "1%", "objective": "x100_VIS"},
    "acquire": {"center_cm1": 520.7, "exposure_s": 1, "accumulations": 2, "spike": "double", "shutter": "hold"},
    "series": {"count": 10, "interval_s": 0},
    "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}
  }
}
```

### 6. Multiple Studies in One Call
Put multiple named studies in a single `run_studies` call and pass an explicit execution order:

```python
run_studies(studies={"first_name": {...}, "second_name": {...}}, order=["first_name", "second_name"], wait_s=50)
```

## Key Rules

- **Peak fits:** Always nest under `"process": {"peaks": [...]}`, never at the top level of a study.
- **Spectral window:** Use either `center_cm1` (single detector window) OR `range_cm1: [from, to]` (stitched wide spectrum)—never both in the same study.
- **Grating window coverage:**
  - Grating `600`: ~4600 cm-1 per window
  - Grating `1200`: ~2000 cm-1 per window
  - Grating `1800`: ~1000 cm-1 per window
- **Filter safety cap:** Never set `filter` above `"10%"`.
- **Validation/execution errors:** If `run_studies` returns `"ok": false` with errors, fix exactly what the error specifies and call `run_studies` again immediately.
- **LabSpec bridge offline:** If a tool reports that the LabSpec bridge is not running, tell the user verbatim:
  > Start ls6t_bridge.vbs in LabSpec (Processing > Scripts) and tell me when it is running.
  Then stop.

## Waiting & Polling

- `run_studies` returns after at most 50 seconds.
- If `"done"` is `false`, call `study_status(request=<returned "request" path>, wait_s=50)` repeatedly until `"done"` is `true`.
- Do not stop polling early, and do not launch a second request while one is still running.
- If the estimated duration exceeds 5 minutes, inform the user when it should finish, then keep polling until done.

## Reporting Format

Keep reports short, with numbers first:

1. **Reliability:** State whether `"reliable"` is `true`. If `false`, quote `"unreliable_because"`.
2. **Key metrics:**
   - **Spectra:** Peak position (cm-1), FWHM (cm-1), SNR.
   - **Calibrations:** Offset (`"offset_cm1"`) and what `ls6t` stored (`"stored_as"`).
   - **Maps:** Mean +/- std of each peak's position.
3. **Warnings:** Summarize any `"warnings"` in plain language.
4. **Accuracy:** Never invent or extrapolate a number not present in the tool output.
5. **Next step:** End with one practical next step when relevant (e.g., *"the correction is 6 h old — recalibrate?"*).

## Example

**User:** "Can you take a single Si scan to see where the main peak is?"

**Agent action:** Immediately call `run_studies` with Recipe 1, poll `study_status` until `"done": true`, then reply:

> Si at 520.6 cm-1 (FWHM 5.9 cm-1, SNR 68), reliable. Correction applied: +0.02 cm-1 (1 h old).