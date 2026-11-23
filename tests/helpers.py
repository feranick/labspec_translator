"""Shared helpers for the tests (the simulator runs inline; the example studies; a profile with the laser shutter set, as the maintainer sets it once confirmed)."""
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from labspec_translator import api, bridge, config, plan, runner, spectra
from labspec_translator import format as F
from labspec_translator import profile as P
from labspec_translator import sim as S

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = json.loads((ROOT / "examples" / "studies.json").read_text())
SETUP = {"laser_nm": 532, "grating": 1800, "filter": "10%", "objective": "x100"}


def run_inline(studies, wait=120):
    s = api.start(studies, inline=True)
    assert s["ok"], s
    return api.status(s["request"], wait_s=wait)


def _profile_with_shutter(tmp_path, **limits):
    sim = S.ensure(config.active_bridge_dir())
    b = bridge.Bridge()
    r = b.call("discover", timeout_s=10, out=str(tmp_path / "dump.txt"))
    assert r["ok"]
    prof, _ = P.import_discovery(P.read_text(tmp_path / "dump.txt"))
    assert prof["motors"]["laser_shutter"] is None                   # found, never set by discovery (2026-11-23)
    prof["motors"]["laser_shutter"] = prof["laser_shutter_candidate"]  # as the maintainer does once confirmed
    P.save(prof)
    # the shutter is used only once shown to block on Si (shutter.py): verified for the simulator's laser, and the
    # same record for its other lasers; the test's own limits after
    api.bridge_call("move", {"motor": "Filter", "mode": "label", "value": "10%"})   # a line strong enough to judge
    v = api.shutter_verify()
    assert v["ok"] and v["blocked"], v
    from labspec_translator import shutter as SH
    for k, las in prof["lasers"].items():
        SH.record(dict(v["record"], laser_nm=las["nm"]))
    prof["limits"].update(limits)
    P.save(prof)
    return prof


def _flake_map(engine):
    return {"m": {"kind": "map", "sample": {"name": "flake"}, "setup": dict(SETUP, grating=600),
                  "acquire": {"center_cm1": 2000, "exposure_s": 0.5},
                  "map": {"x_um": [-4, 4, 2], "y_um": [0, 2, 1], "engine": engine},
                  "process": {"peaks": [{"name": "G", "near_cm1": 1585}]}}}


__all__ = ["ROOT", "EXAMPLES", "SETUP", "run_inline", "_profile_with_shutter", "_flake_map", "api", "bridge", "config", "plan", "runner", "spectra", "F", "P", "S", "np", "pytest", "json", "os", "re", "sys", "time", "Path"]
