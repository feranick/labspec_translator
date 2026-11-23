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

## Architecture

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
- `vbs/common.vbs` + `vbs/bridge_main.vbs` / `vbs/discover_main.vbs` / `vbs/script_motor_main.vbs`: `ls6t bridge
  install` composes `ls6t_bridge.vbs`, `ls6t_discover.vbs` and `ls6t_script_motor.vbs` with this machine's folders
  (ASCII, CRLF).
- Protocol 1: key=value lines, `%0A` line end, `%25` percent; commands UTF-16 (FSO Unicode mode); stop.flag names
  its seq; heartbeat age (file time) = bridge alive, except during modal confirm/resume.
- Safety: XY offsets from where the operator left the stage, Z from the operator's focus (never absolute);
  filter capped per laser / for sensitive samples / globally; max exposure, accumulations, points, minutes;
  operator confirm (LS6T_CONFIRM); popup close = halt; LabSpec cancel = operator stop; LabSpec script stop = quit.

## State log

The dated log of what was done, found at the instrument and decided: [planning_log.md](planning_log.md) (add new entries at its end).

## Deferred: core refactors (code audit, 2026-11-23)

Behaviour-preserving, but large diffs: to do after the grating / spectrometer-centre questions (TESTING T3.16) are
settled at the instrument, one at a time, each with the full tests and benchmarks before and after and a re-test of
the touched paths at the instrument.

1. **One instrument-setup path.** `api.set_instrument` (~180 lines) and `runner._setup` repeat label matching,
   already-set checks, the manual objective question / retract, illumination and locking; `runner` imports from `api`
   (objective_question, objective_retract...), a circular dependency. Extract a `setup.py` both call; `api` becomes a
   thin facade.
2. **Split `runner.py`** (~2,700 lines, 74 functions): request lifecycle and locking (run_request), study execution
   (run_study), the procedures (run_move, run_spot, run_calibration, run_filters, run_cutoff, run_background,
   run_guide, run_alignment, run_focus) and the analysis (analyse, parse_native_map, depth_result) into separate
   modules; factor the repeated bridge-call / result scaffolding.
3. **Declarative study format.** `format.validate_study` is one ~480-line function branching on ten kinds, and
   `study_format()` documents the same schema by hand: a per-kind schema / validator map that also generates the
   description, so the two cannot drift.
4. **MCP tools.** Fold `spot_status` and `alignment_status` into `references` (fewer tools, shorter prompt); changes
   what the LLM sees, so re-check the instructions and PROMPTS.md.
5. **Bridge and simulator.** Merge `WriteSpectrum` and `WriteData` in bridge_main.vbs; audit the 26 verbs the bridge
   and the simulator both implement (bring-up verbs such as run_vbs, calib_params, labels, notice) and drop the
   ones no longer needed; needs a re-test at the instrument.
6. Later, when bring-up is over: `testmode.py` from a compact checklist instead of reading TESTING.md;
   `vbs/script_motor_main.vbs` (T1.4) to tools/ once Q34/T1.4 are answered. `typelib.py` stays: it is the
   `ls6t-typelib` command the installer puts on the instrument PC.

## To do next

1. **At the instrument**: carry out TESTING.md (T0-T9, T5b-T5e) with the maintainer; record the answers to Q1-Q40 here and
   change the code where they differ from the assumptions (README "Assumptions about LabSpec").
2. Put the measured times in the profile's `timing` (T3, T7.2).
3. Later: trigger scripts during native maps (per-point actions: Send, PutDataInfo); script-controlled motors
   as map axes (temperature, polariser); Linkam temperature
   series (ManageTemperature has rates only: set points need RS232 through Send); a translators.json entry
   (llmt's bin/ scripts are bash: decide how a Windows install fits llm-translators).

## Open ideas

- Per-objective laser offsets (if T5b.4 shows they differ): video.laser_offset_um as {objective: [dx, dy]}.
- Use the measured offset to correct where maps and points land (today it only places the marker).
- Read AutoAlignment's state (Maintenance tab, status-bar indicator) with Windows UI automation, read-only, if
  T5c.1 shows it is exposed; ask HORIBA whether newer LabSpec automation offers AutoAlignment/AutoCalibration.
- Guided adjustments on other signals: the video spot's brightness or sharpness (focus without a Raman line).
- Adaptive maps: refine around boundaries found in a coarse map.
