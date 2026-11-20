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

## Architecture (version 2026.11.19.6)

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
- `vbs/common.vbs` + `vbs/bridge_main.vbs` / `vbs/discover_main.vbs`: `ls6t bridge install` composes
  `ls6t_bridge.vbs` and `ls6t_discover.vbs` with this machine's folders (ASCII, CRLF).
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

## To do next

1. **At the instrument**: carry out TESTING.md (T0-T9, T5b-T5d) with the maintainer; record the answers to Q1-Q28 here and
   change the code where they differ from the assumptions (README "Assumptions about LabSpec").
2. Put the measured times in the profile's `timing` (T3, T7.2).
3. Later: native LabSpec maps (SetMappingParams + ACQ_SPECTRAL_IMAGE, also on Z) for large fast maps; Linkam temperature
   series (ManageTemperature has rates only: set points need RS232 through Send); a translators.json entry
   (llmt's bin/ scripts are bash: decide how a Windows install fits llm-translators).

## Open ideas

- Per-objective laser offsets (if T5b.4 shows they differ): video.laser_offset_um as {objective: [dx, dy]}.
- Use the measured offset to correct where maps and points land (today it only places the marker).
- Read AutoAlignment's state (Maintenance tab, status-bar indicator) with Windows UI automation, read-only, if
  T5c.1 shows it is exposed; ask HORIBA whether newer LabSpec automation offers AutoAlignment/AutoCalibration.
- Guided adjustments on other signals: the video spot's brightness or sharpness (focus without a Raman line).
- Adaptive maps: refine around boundaries found in a coarse map.
