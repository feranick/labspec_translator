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

## Architecture (version 2026.11.19.1)

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
  pid. 42 tests pass.

## To do next

1. **At the instrument**: carry out TESTING.md (T0-T9) with the maintainer; record the answers to Q1-Q12 here and
   change the code where they differ from the assumptions (README "Assumptions about LabSpec").
2. Put the measured times in the profile's `timing` (T3, T7.2).
3. Later: native LabSpec maps (SetMappingParams + ACQ_SPECTRAL_IMAGE) for large fast maps; Linkam temperature
   series (ManageTemperature has rates only: set points need RS232 through Send); a translators.json entry
   (llmt's bin/ scripts are bash: decide how a Windows install fits llm-translators).

## Open ideas

- Video snapshot of the sample (Video + export) so the LLM can see where it measures.
- Adaptive maps: refine around boundaries found in a coarse map.
