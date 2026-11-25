---
name: ls6t
description: Run Raman measurements (spectra, calibrations, maps, series) on the HORIBA XploRA Plus through the ls6t tools. Use whenever the user asks to scan, measure, calibrate, map or check a peak on the Raman microscope.
---

# Measuring on the XploRA Plus with ls6t

You operate a HORIBA XploRA Plus Raman microscope through the ls6t tools.

## Rules

1. **To measure anything, call `run_studies`.** Never call `instrument()` to set the laser, grating or
   anything else: `run_studies` sets everything itself from `setup`.
2. **Do not ask the user for parameters.** Use the defaults below and act at once. Ask only if you
   cannot tell *what* to measure.
3. **`studies` is a JSON object, not a string:** `{"name": {study}}`.

## Lasers: use these exact values for `laser_nm`

| User says | `laser_nm` |
|---|---|
| 638, 637, red | `637.75` |
| 532, green | `532` |
| 785, NIR | `785.04` |

## Defaults

`laser_nm` 637.75 · `grating` 1200 · `filter` "1%" · `objective` "x100_VIS" · `exposure_s` 1 ·
`accumulations` 2 · `spike` "double". Never use a filter above "10%". Si line: 520.7 cm⁻¹.

Translating the user's words:

- "1 run" / "1 acquisition" → `"accumulations": 1` and `"spike": "single"`
- "N sec" → `"exposure_s": N`
- "from A to B cm-1" → `"range_cm1": [A, B]`
- a single wavenumber → `"center_cm1": value`
- no range given → `"center_cm1": 520.7`

## Tools

- `run_studies(studies, order, wait_s)`: runs studies. **The only way to measure.**
- `study_status(request, wait_s=50)`: poll until `"done": true`.
- `validate_studies(studies)`: dry run; each error says exactly how to fix it.
- `study_format()`: full reference of keys and the instrument's limits.
- `references()`, `get_file(path)`, `get_image(path)`: stored calibrations and result files.
- `instrument()`: status only. Not for measurements or settings.

## Example: copy this pattern

User: "Take a scan on a Silicon sample to get the peak position using the 638 laser (0-1000 cm-1, 1 sec 1 run)"

Call immediately:

```
run_studies(studies={"si_scan": {
  "kind": "spectrum",
  "sample": {"name": "Si wafer"},
  "setup": {"laser_nm": 637.75, "grating": 1200, "filter": "1%", "objective": "x100_VIS"},
  "acquire": {"range_cm1": [0, 1000], "exposure_s": 1, "accumulations": 1, "spike": "single"},
  "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}
}}, wait_s=50)
```

Then poll `study_status` until done and answer like:

> Si at 520.6 cm⁻¹ (FWHM 5.9 cm⁻¹, SNR 68), reliable. Warning: the range below ~59 cm⁻¹ is blocked by the edge filter.

## Other recipes

Use the same `sample`, `setup` and `acquire` as the example; change only what the user asks.

- **Wide spectrum** (e.g. 100–3200 cm⁻¹): `"range_cm1": [100, 3200]`; leave out `process` (the
  strongest peaks are found automatically).
- **Calibration**: `"kind": "calibration"`, add
  `"calibration": {"reference": "si", "adjust": "ls6t", "repeats": 5}` and `"video": {"before": false}`;
  no `process`. At grating 600 add `"window_cm1": 60` inside `calibration`.
- **Map**: `"kind": "map"`, add `"map": {"x_um": [-4, 4, 2], "y_um": [-4, 4, 2], "engine": "points"}`
  and `"shutter": "hold"` inside `acquire`. x/y are `[from, to, step]` in µm from the current spot.
- **Series**: `"kind": "series"`, add `"series": {"count": 10, "interval_s": 0}` and
  `"shutter": "hold"` inside `acquire`.
- **Several studies**: put them in one call and add `order=["first", "second", ...]`.
- Peak fits go under `"process": {"peaks": [...]}`, never at the top level of a study.

Grating coverage per detector window: 600 ≈ 4600 cm⁻¹, 1200 ≈ 2000 cm⁻¹, 1800 ≈ 1000 cm⁻¹.

## If it fails

- `"ok": false` with errors: read each error, change exactly what it says, call `run_studies` again.
  Do not give up after one error and do not switch to `instrument()`.
- "the LabSpec bridge … not running": tell the user to start `ls6t_bridge.vbs` in LabSpec
  (Processing > Scripts), then stop.

## Waiting

If `"done"` is false, call `study_status(request=<the "request" path>, wait_s=50)` again until
`"done"` is true. Never start another request while one is running. If the estimate is over
5 minutes, tell the user when it should finish, then keep checking.

## Reporting

- Numbers first: peak position, FWHM and SNR. For a calibration, `offset_cm1` and `stored_as`; for a
  map, the mean ± std of each peak's position.
- Say whether `reliable` is true; if not, quote `unreliable_because`.
- List the warnings in plain words.
- Never invent a number that is not in the result.
- End with one useful next step if there is one (e.g. "the correction is 6 h old: recalibrate?").
