"""bench.py — benchmarks with known answers, run on the simulator (the instrument is never touched).
Version: 2026.11.19.2

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
