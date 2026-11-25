---
name: ls6t
description: Take Raman spectra and Si calibrations on the HORIBA XploRA Plus. Use whenever the user asks to scan, measure, take a spectrum, find a peak or calibrate on the Raman microscope.
---

# Measuring on the XploRA Plus

Act at once. Do not ask the user for settings: every argument has a default.

## To measure: call `measure_spectrum`

Pass only what the user said; leave everything else out.

| User says | Argument |
|---|---|
| "638 laser", "532", "785" | `laser_nm=638` (ls6t picks the nearest laser) |
| "0 to 1000 cm-1" | `from_cm1=0, to_cm1=1000` |
| "around 520" / one number | `center_cm1=520` |
| "1 sec" | `exposure_s=1` |
| "1 run", "2 runs" | `accumulations=1`, `accumulations=2` |
| "grating 600" | `grating=600` |
| "silicon", "Si" | `sample="Si wafer"`, `peak_cm1=520.7` |

Example. User: "Take a scan on a Silicon sample to get the peak position using the 638 laser (0-1000 cm-1, 1 sec 1 run)"

```
measure_spectrum(laser_nm=638, from_cm1=0, to_cm1=1000, exposure_s=1, accumulations=1,
                 sample="Si wafer", peak_cm1=520.7)
```

## To calibrate: call `calibrate_si`

User: "calibrate", "check the zero", "calibrate the 600 grating"

```
calibrate_si(laser_nm=638, grating=600)
```

## After the call

- The tool waits for the result. If the answer says it is not finished, call
  `study_status(request=<the request it gave>, wait_s=50)` until it is done.
- Report the numbers from the answer: peak position, FWHM, SNR (calibration: the offset and how it
  was stored). Say if it is not reliable, and why. List the warnings in one line each.
- Never invent a number.

## Do not

- Do not call `instrument` to measure or change settings.
- Do not ask for parameters the user did not give.
- If the answer says the LabSpec bridge is not running, tell the user to start `ls6t_bridge.vbs` in
  LabSpec (Processing > Scripts) and stop.
