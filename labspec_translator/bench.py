"""bench.py — benchmarks with known answers, run on the simulator (the instrument is never touched).
Version: 2026.11.20.34

They check the chain end to end: the commands, wavelength <-> Raman shift, the extended range, fits, map assembly,
the saturation check and an operator stop. On the instrument itself, the 'calibration' study on a silicon wafer is
the standing check (README, "The checker").
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, runner

SETUP = {"laser_nm": 532, "grating": 1800, "filter": "10%", "objective": "x100"}

CASES = {
    "si_calibration": {
        "studies": {"si": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                           "acquire": {"exposure_s": 1, "accumulations": 2, "spike": "double"}}},
        "checks": [("si", "calibration.measured_cm1", 520.7, 0.3), ("si", "calibration.pass", True, None),
                   ("si", "reliable", True, None)]},
    "graphene_map": {
        "studies": {"m": {"kind": "map", "sample": {"name": "graphene flake"},
                          "setup": dict(SETUP, grating=600),
                          "acquire": {"range_cm1": [1200, 2900], "exposure_s": 1, "accumulations": 2,
                                      "spike": "double"},
                          "map": {"x_um": [-4, 4, 2], "y_um": [0, 2, 1]},
                          "process": {"peaks": [{"name": "G", "near_cm1": 1585},
                                                {"name": "2D", "near_cm1": 2680, "window_cm1": 60}]}}},
        # x = -4, -2 (silicon only) and 0, 2, 4 (flake): G found at 3 of 5 columns x 3 rows = 9 points
        "checks": [("m", "map.summary.G.position_cm1.n", 9, 0), ("m", "map.summary.G.position_cm1.mean", 1585.0, 2.0),
                   ("m", "map.summary.2D.position_cm1.mean", 2680.0, 3.0), ("m", "reliable", True, None)]},
    "extended_range": {
        "studies": {"e": {"kind": "spectrum", "sample": {"name": "flake edge"}, "setup": SETUP,
                          "acquire": {"range_cm1": [300, 2900], "exposure_s": 1, "accumulations": 2,
                                      "spike": "double"},
                          "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30},
                                                {"name": "G", "near_cm1": 1585},
                                                {"name": "2D", "near_cm1": 2680, "window_cm1": 60}]}}},
        "checks": [("e", "peaks_by_name.Si.position_cm1", 520.7, 0.5), ("e", "peaks_by_name.G.position_cm1", 1585.0, 1.5),
                   ("e", "peaks_by_name.2D.position_cm1", 2680.0, 2.0), ("e", "checks.range.pass", True, None)]},
    "saturation": {
        "studies": {"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, filter="100%"),
                          "acquire": {"center_cm1": 520, "exposure_s": 30, "accumulations": 1}}},
        "checks": [("s", "checks.saturation.pass", False, None), ("s", "reliable", False, None)]},
    "series": {
        "studies": {"t": {"kind": "series", "sample": {"name": "Si"}, "setup": SETUP,
                          "acquire": {"center_cm1": 520, "exposure_s": 0.5, "accumulations": 1},
                          "series": {"count": 3, "interval_s": 1},
                          "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}},
        "checks": [("t", "series.summary.Si.position_cm1.n", 3, 0),
                   ("t", "series.summary.Si.position_cm1.mean", 520.7, 0.3)]},
    "guided_focus": {
        "studies": {"f": {"kind": "guide", "sample": {"name": "Si"}, "setup": SETUP,
                          "guide": {"instruction": "Focus slowly through the best position", "mode": "maximize"}}},
        # the simulated operator passes the best focus at the 8th reading: STOP comes right after, not before
        "checks": [("f", "guide.reason", "past_max", None), ("f", "guide.iterations", 11, 2),
                   ("f", "reliable", True, None)]},
    "focus_scan": {
        "studies": {"f": {"kind": "focus", "sample": {"name": "Si"}, "setup": SETUP}},
        "sim_files": {"sim_defocus": "2.3"},      # the sample's best focus is 2.3 µm above the operator's
        "checks": [("f", "focus.found", True, None), ("f", "focus.shift_um", 2.3, 0.15), ("f", "reliable", True, None)]},
    "spot_raman": {
        "studies": {"s": {"kind": "spot", "sample": {"name": "Si with a dust particle"}, "setup": SETUP,
                          "spot": {"method": "raman", "feature_um": [1219.5, -557.8]}}},
        # the beam sits 1.1 / 0.6 µm from LabSpec's marker; the dust particle (dark on the video, no Raman) is at
        # the given point: the scan finds where the Si signal vanishes
        "sim_files": {"sim_spot_offset": "1.1,0.6"},
        "checks": [("s", "spot.found", True, None), ("s", "spot.offset_um.0", 1.1, 0.15),
                   ("s", "spot.offset_um.1", 0.6, 0.15)]},
    "spot_video": {
        "studies": {"v": {"kind": "spot", "sample": {"name": "Si"}, "setup": SETUP,
                          "spot": {"method": "acquisition"}}},
        "checks": [("v", "spot.found", True, None), ("v", "spot.offset_um.0", 1.5, 0.1),
                   ("v", "spot.offset_um.1", -0.8, 0.1)]},
    "depth_profile": {
        "studies": {"d": {"kind": "depth", "sample": {"name": "polystyrene film on Si"},
                          "setup": dict(SETUP, grating=600),
                          "acquire": {"range_cm1": [400, 1700], "exposure_s": 0.5},
                          "depth": {"from_um": -4, "to_um": 18, "step_um": 1, "refractive_index": 1.59},
                          "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30},
                                                {"name": "PS", "near_cm1": 1001.4, "window_cm1": 20}]}}},
        # a 12 µm film (n = 1.59) on Si: the Si rises and the polystyrene falls at 12 µm (corrected depth)
        "sim_files": {"sim_film": "12,1.59"},
        "checks": [("d", "depth.interfaces_by.Si.height.rise", 12.0, 0.8),
                   ("d", "depth.interfaces_by.PS.height.fall", 12.0, 0.8),
                   ("d", "depth.interfaces_by.PS.height.rise", 0.0, 0.8)]},
    "calibration_zero": {
        "studies": {"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                          "calibration": {"adjust": "labspec"}}},
        # the scale reads 1.5 cm-1 high; the operator corrects LabSpec's zero when asked; the second check passes
        "sim_files": {"sim_xshift": "1.5,1.0"},
        "checks": [("c", "calibration.attempts.0.offset_cm1", 1.5, 0.25), ("c", "calibration.pass", True, None),
                   ("c", "calibration.attempts.1.offset_cm1", 0.0, 0.25)]},
    "white_light": {
        "studies": {"b": {"kind": "background", "sample": {"name": "lamp"}, "setup": dict(SETUP, grating=600),
                          "acquire": {"range_cm1": [200, 1800], "exposure_s": 1, "accumulations": 2},
                          "background": {"name": "lamp", "laser_off": "operator"}}},
        # the white light on; the operator blocks the laser: the background holds the lamp only (no Raman line)
        "sim_files": {"sim_lamp": "on"},
        "checks": [("b", "reliable", True, None), ("b", "warnings", [], None)]},
    "native_map": {
        "studies": {"n": {"kind": "map", "sample": {"name": "graphene flake"}, "setup": dict(SETUP, grating=600),
                          "acquire": {"center_cm1": 2000, "exposure_s": 0.5},
                          "map": {"x_um": [-4, 4, 2], "y_um": [0, 2, 1], "engine": "native"},
                          "process": {"peaks": [{"name": "G", "near_cm1": 1585}]}}},
        # LabSpec's own engine: the same 9 graphene points as the point-by-point map
        "checks": [("n", "map.summary.G.position_cm1.n", 9, 0), ("n", "map.summary.G.position_cm1.mean", 1585.0, 2.0),
                   ("n", "points_measured", 15, 0)]},
    "operator_stop": {
        "studies": {"o": {"kind": "map", "sample": {"name": "Si"}, "setup": SETUP,
                          "acquire": {"center_cm1": 520, "exposure_s": 1, "accumulations": 1},
                          "map": {"x_um": [0, 9, 1]}}},
        "operator_stop_after": 4,      # the operator stops the bridge after the 4th spectrum
        "checks": [("o", "reliable", False, None), ("o", "complete", False, None),
                   ("o", "points_measured", 4, 0), ("_state", "stopped_by", "operator", None)]},
}


@contextmanager
def _sim_env(out: Path):
    keep = {k: os.environ.get(k) for k in ("LS6T_MODE", "LS6T_OUT", "LS6T_CONFIRM")}
    os.environ.update(LS6T_MODE="sim", LS6T_OUT=str(out), LS6T_CONFIRM="request")
    try:
        yield
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _check(results: dict, state: dict, who: str, path: str, value, tol):
    got = state.get(path) if who == "_state" else runner.lookup(results.get(who, {}), path)
    if tol is None:
        ok = got == value
        err = None
    else:
        ok = isinstance(got, (int, float)) and abs(got - value) <= tol
        err = round(got - value, 6) if isinstance(got, (int, float)) else None
    return {"what": f"{who}.{path}", "expected": value, "got": got, "error": err, "tol": tol, "passed": bool(ok)}


def run(names: list | None = None, wait_s: float = 600) -> dict:
    from . import api, sim as S
    names = names or list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        raise ValueError(f"unknown benchmarks {unknown}: {', '.join(CASES)}")
    base = config.out_dir() / f"bench_{time.strftime('%Y%m%d_%H%M%S')}"
    cases, passed, failed = {}, [], []
    t0 = time.time()
    for n in names:
        case = CASES[n]
        out = base / n
        with _sim_env(out):
            bdir = config.active_bridge_dir()
            sim = S.ensure(bdir)
            for fname, content in case.get("sim_files", {}).items():
                (bdir / fname).write_text(content)
            if case.get("operator_stop_after"):
                (bdir / "sim_operator_stop_after").write_text(str(case["operator_stop_after"]))
            s = api.start(case["studies"], inline=True)
            if not s.get("ok"):
                cases[n] = {"error": s}
                failed.append(n)
                continue
            st = api.status(s["request"], wait_s=max(5.0, wait_s - (time.time() - t0)))
            if sim is not None:
                sim.stop()
            S.stop_all()
        state = {"state": st["state"], "stopped_by": st.get("stopped_by")}
        checks = [_check(st["studies"], state, *c) for c in case["checks"]]
        ok = all(c["passed"] for c in checks)
        cases[n] = {"request": st["request"], "state": st["state"], "checks": checks}
        (passed if ok else failed).append(n)
    return {"bench": str(base), "passed": passed, "failed": failed, "skipped": [], "pending": [],
            "seconds": round(time.time() - t0, 1), "cases": cases}
