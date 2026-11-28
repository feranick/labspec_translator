# Translator App Refactoring Guide (from `labspec_translator`)

This guide documents the refactoring of `labspec_translator` (`ls6t`, commits `e5399dd`–`6501910`, versions `2026.11.27.10`–`2026.11.28.11`)—**why** each change was made, **what** changed, and **how** to apply the same playbook to sibling translator apps (`feat`, `cfdt`, `lmpt`, `omct`, `b3dt`, `p3dt`, etc.).

---

## 1. Why Refactor?

Translator apps grow rapidly under live experimental/solver feedback: new study kinds, hardware/solver workarounds, schema checks, and post-processing heuristics accumulate into a few monolithic files. By `2026.11.27.9`, `labspec_translator` had:

1. **A 6,826-line `runner.py` god-module** mixing bridge/worker lifecycle, setup/motor orchestration, per-study execution loops, spectral/statistical analysis, and learned calibration state.
2. **A 600+ line `validate_study()` function** in `format.py` where adding or auditing one study kind meant scrolling through unrelated branches.
3. **Duplicated utility helpers** (`read_json`, `write_json`, `to_float`, `clean`, shutter gating, series timing) copy-pasted across modules, drifting subtly out of sync.
4. **Hot-path performance bottlenecks** on large maps/spectra and repeated calls: Python row-by-row CSV parsing, `O(N^2)` string concatenation in VBS, redundant profile disk reads, repeated laser-line/shift calculations, and `matmul` (`@`) BLAS warnings on macOS Apple Accelerate.
5. **Silent fallbacks and hidden timing overhead**: expired approvals silently falling back to `auto` without warning, settling waits folded into acquisition time, and unnecessary pre-study overhead (e.g. video snapshots on calibrations).

The refactoring addressed these **incrementally across small, testable commits** while keeping 100% backward compatibility with existing callers, MCP tools, CLI, and 244+ pytest tests.

---

## 2. Architecture Before vs. After

### Module Decomposition (`runner.py`: 6,826 lines → 39-line facade)

| Module | Role | Approx. Size |
|---|---|---|
| `io_util.py` *(new leaf)* | Pure stdlib JSON I/O (`read_json`, `write_json` atomic write), `to_float`, `clean`, timestamp parsing. Zero internal imports—prevents circular dependencies. | ~174 lines |
| `runner_core.py` *(new)* | Execution context (`Ctx`), heartbeat/lock/cancel, bridge/worker dispatch, setup/motor moves, read-back normalization, warmup/settle, progress reporting, `run_any()`. | ~1,860 lines |
| `studies.py` *(new)* | Per-kind execution loops (`run_study`, `run_focus`, `run_alignment`, `run_guide`, `run_move`, `run_spot`, `run_depth`, `run_filters`, `run_cutoff`, `run_background`, native maps). | ~2,115 lines |
| `analysis.py` *(new)* | Post-acquisition analysis (`analyse`, zero/background correction, peak fitting, `laser_health`, calibration repeat statistics, map/series summaries, `expect` checks). | ~2,550 lines |
| `calibration_learn.py` *(new)* | Pure statistical/learning helpers (landing steps, creep/drift, repeatability, unfollowed move thresholds, outlier detection). | ~582 lines |
| `runner.py` *(facade)* | Re-exports public & test-used symbols from `runner_core`, `studies`, `analysis`, `calibration_learn` so external callers and `monkeypatch` targets keep working unchanged. | ~39 lines |

### Validator Decomposition (`format.py`)
- Split monolithic `validate_study()` into focused per-kind validators: `_validate_calibration()`, `_validate_filters()`, `_validate_cutoff()`, `_validate_background()`, `_validate_acquire()`, `_validate_spot()`, `_validate_focus()`, etc.
- Each validator owns its key-set (`CAL_KEYS`, `FILTERS_KEYS`, ...) and appends to shared `errors` / `warnings` lists.

### External Script / Bridge Decomposition (`bridge_main.vbs`)
- Extracted repeated motor/shutter/acquisition logic into small helper subs/functions rather than copy-pasting blocks across verbs.
- Replaced `O(N^2)` string concatenation (`s = s & ...`) with array accumulation + `Join(..., vbCrLf)`.

---

## 3. Core Refactoring Patterns (Apply to Other Translators)

### Pattern A: Create a Zero-Dependency `io_util.py` Leaf Module First
- **Problem**: `api.py`, `runner.py`, `plan.py`, `store.py`, and `analysis.py` all need atomic JSON read/write, NaN-safe float parsing, and JSON-safe cleaning (`clean()`). Putting them in `runner.py` creates circular imports.
- **Fix**: Extract `read_json`, `write_json` (temp file + `os.replace`), `to_float(v, finite=True)`, and `clean()` into `io_util.py` with **only Python stdlib imports**. Every other module imports from `io_util`.

### Pattern B: Split the Monolith Along Lifecycle Boundaries + Keep a Monkeypatch-Safe Facade
- **Boundaries that generalize across every translator (`feat`, `cfdt`, `lmpt`, `omct`, `b3dt`, `p3dt`)**:
  1. `runner_core.py`: Request folder, process/SLURM/worker lifecycle, heartbeat/cancellation, tool/solver invocation, progress JSON.
  2. `studies.py`: Study-kind dispatch and execution orchestration (meshing, setup, multi-step sweeps/variants, retries).
  3. `analysis.py`: Result parsing, convergence/balance/residual checks, `expect` assertions, reliability verdicts, summaries.
  4. `format.py`: Schema constants, `study_format()`, and per-kind `_validate_<kind>()` helpers.
  5. `plan.py`: Pure pre-run planning (time/memory/CPU estimates, geometry/parameter resolution, defaults—no side effects).
  6. `api.py` & `mcp_server.py`: Thin public Python API and MCP tool wrappers; no business logic duplicated between them.
- **Monkeypatch-Safe Facade Gotcha (learned in `.28.5`)**:
  - When tests do `monkeypatch.setattr(runner, "_run_kind", ...)` or `monkeypatch.setattr(runner, "analyse", ...)`, internal calls inside `runner_core.py` or `studies.py` that call the local symbol directly will **bypass** the monkeypatch!
  - **Rule**: Keep `runner.py` as a thin re-export facade, and for functions commonly monkeypatched in tests (`analyse`, `_run_kind`, etc.), route calls through `from . import runner as _R; _R.analyse(...)`.

### Pattern C: Single Source of Truth for Policy & Gating Logic
- **Problem**: Gating rules (e.g., shutter approval expiry, `NO_HOLD_KINDS`, drift/unfollowed thresholds) were re-implemented slightly differently in `format.py` (validation), `plan.py` (estimation), and `runner_core.py` (execution).
- **Fix**:
  - Consolidate shared constants and predicates in one place (e.g., `shutter._gate_detail()` shared by `gate()` and `expired_note()`; `NO_HOLD_KINDS` and `_series_recovers_between_spectra()` in `plan.py`; `UNFOLLOWED_MIN_CM1` in `calibration_learn.py`).
  - Document edge-case semantics explicitly (e.g., what `_gate_detail(prof, None)` returns when no laser is specified).

### Pattern D: Never Silently Fall Back Without Warning
- **Problem**: When an automatic feature (like `hold_shutter: None` auto-hold, or a stored calibration) couldn't be used due to expiration or mismatch, it silently fell back to a degraded mode, hiding the reason from both the user and LLM caller.
- **Fix**:
  - Emit explicit warnings at **both** validation time (`format.py`) and runtime (`runner_core.py` / `analysis.py`) explaining *why* the fallback happened, distinct from an explicit user opt-out.
  - Always include key boolean status fields (e.g., `before_labspec_restart: false` rather than omitting the key) so downstream callers and LLMs can reliably check them without guessing whether a missing key means `false` or "not checked".

### Pattern E: Performance & Numerical Hygiene
1. **Vectorize bulk I/O**: Replace Python line-by-line loops over large numeric tables/spectra with `numpy.loadtxt` / vectorized parsing (guarded for headers/comments).
2. **Cache disk-backed config/profiles by `(path, mtime_ns, ino)`**: Avoid re-reading and re-parsing JSON/YAML profiles on every study/check while staying instantly fresh when edited on disk.
3. **Cache expensive derived calculations per spectrum/case**: Compute shifts/baselines/reference lines once per spectrum in `analyse()` instead of re-fitting inside each downstream check.
4. **Avoid BLAS `RuntimeWarning`s on macOS Apple Accelerate**: Replace small/degenerate `A @ B` (`matmul`) calls in tight numerical helpers (e.g., polynomial baseline, luminance) with elementwise `np.sum(A * B, axis=...)` or QR-based solves.
5. **Split overhead timing into explicit phases**: Separate setup, settle/warmup, solver/acquisition, and post-processing/save phases (`timing.acquire_phases_s`, `t_settle_s`) so bottlenecks are immediately visible in every run's JSON.
6. **Consistent numeric types in read-backs**: Normalize numeric string read-backs (`"1200"`, `"785.04"`) to `int`/`float` at the boundary (`_num_val()`) so JSON outputs are consistently typed.

### Pattern F: Guard External Script / DSL Syntax in Tests
- If the translator generates or bundles scripts in another language (VBS, OpenFOAM dicts, CalculiX `.inp`, LAMMPS `in.lammps`, Modelica `.mo`), keep a fast static test (like `test_vbs_scripts`) checking ASCII/line-endings, balanced blocks, and verb/keyword parity with the simulator.
- Note from `.28.11`: Keep generated/bundled script constructs simple enough that both the target runtime and the static test parser agree (e.g., avoid VBS `_` line continuations on `If ... Then`).

---

## 4. Recommended Refactoring Workflow for Other Translator Repos

Use this step-by-step sequence in a new chat for `feat`, `cfdt`, `lmpt`, `omct`, `b3dt`, or `p3dt`:

1. **Baseline & Inventory**:
   - Run the full test suite (`pytest`) and benchmark suite (`<cli> bench`) first; record exact pass counts and timings.
   - Measure line counts (`wc -l <pkg>/*.py`) and identify god-modules (>2,000 lines), >200-line functions, duplicated helpers, and `pyflakes` unused imports/dead code.
2. **Phase 1 — Leaf Utilities & Low-Risk Performance (`*.1`)**:
   - Extract `io_util.py` (stdlib only).
   - Add `(mtime_ns, ino)` caching for profile/config loading.
   - Vectorize hot parsing loops and remove dead code/unused imports.
   - Run full `pytest` + `bench` + `pyflakes`.
3. **Phase 2 — Structural Decomposition (`*.2`)**:
   - Split the monolith (`runner.py`) into `runner_core.py`, `studies.py`, `analysis.py`, and domain-specific learning/math helpers.
   - Keep `runner.py` as a compatibility re-export facade; verify monkeypatch targets route through the facade.
   - Split `validate_study()` into per-kind `_validate_<kind>()` functions.
   - Run full `pytest` + `bench`.
4. **Phase 3 — Deduplication & Observability (`*.3`–`*.7`)**:
   - Consolidate duplicated policy/threshold helpers into a single source of truth.
   - Split phase timings (setup vs. settle/mesh vs. solve vs. post-process).
   - Add explicit warnings for silent fallbacks and normalize read-back types.
   - Add regression tests for every bug or edge case found during live smoke tests.
5. **Phase 4 — Release Discipline**:
   - Keep each logical change in its own `YYYY.MM.DD.N` version bump (`__init__.py` + `CHANGELOG.md` + annotated/lightweight git tag).
   - Verify with both automated tests (`pytest`, `pyflakes`, `bench`) and a live smoke test on real hardware/solver before moving to the next phase.
