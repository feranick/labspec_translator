"""Tests: guide and alignment, stage and focus, the laser spot, depth profiles, spectral zero, white-light background, native maps."""
from helpers import *  # noqa: F401,F403


# --------------------------------------------------------------------------------------------- guide and alignment

def test_guide_reach_and_maximize(tmp_path):
    st = run_inline({"g": {"kind": "guide", "sample": {"name": "Si"}, "setup": SETUP,
                           "guide": {"instruction": "Raise the laser power at the source slowly", "mode": "reach",
                                     "target": 30000}}})
    g = st["studies"]["g"]["guide"]
    assert st["studies"]["g"]["reliable"] and g["reason"] == "target" and g["value"] >= 30000
    assert g["message"].startswith("STOP") and Path(g["history"]).exists()
    st = run_inline({"f": {"kind": "guide", "sample": {"name": "Si"}, "setup": SETUP,
                           "guide": {"instruction": "Focus slowly", "mode": "maximize"}}})
    g = st["studies"]["f"]["guide"]
    assert g["reason"] == "past_max" and g["value"] < g["max_value"] and "go back" in g["message"]


def test_guide_saturation_and_abort():
    st = run_inline({"s": {"kind": "guide", "sample": {"name": "Si"}, "setup": SETUP,
                           "guide": {"instruction": "Raise power", "mode": "reach", "target": 10000000,
                                     "exposure_s": 1}}})
    r = st["studies"]["s"]
    assert r["guide"]["reason"] == "saturation" and not r["reliable"]
    (config.active_bridge_dir() / "sim_guide_abort").write_text("1")
    st = run_inline({"a": {"kind": "guide", "sample": {"name": "Si"}, "setup": SETUP,
                           "guide": {"instruction": "Focus slowly", "mode": "maximize"}}})
    assert st["studies"]["a"]["guide"]["reason"] == "operator_abort" and not st["studies"]["a"]["reliable"]


def test_guide_validation():
    bad = F.validate({"g": {"kind": "guide", "setup": SETUP, "guide": {"instruction": "x", "mode": "reach"}}})["g"]
    assert not bad["ok"] and any("guide.target" in e for e in bad["errors"])
    bad = F.validate({"g": {"kind": "guide", "setup": SETUP, "guide": {"instruction": "x", "mode": "maximize",
                                                                        "max_s": 10000}}})["g"]
    assert any("guide.max_s" in e for e in bad["errors"])
    bad = F.validate({"g": {"kind": "guide", "setup": dict(SETUP, filter="100%"), "sample": {"sensitive": True},
                            "guide": {"instruction": "x", "mode": "maximize"}}})["g"]
    assert any("sensitive_max_filter_pct" in e for e in bad["errors"])


def test_alignment_baseline_then_guided_autoalignment(tmp_path):
    _profile_with_shutter(tmp_path)
    st = run_inline({"a": {"kind": "alignment", "sample": {"name": "Si ref"}, "setup": SETUP}})
    r = st["studies"]["a"]
    assert r["alignment"]["passed"] is None and "record" in r["alignment"]["recommendation"]
    rec = api.alignment_record(st["request"])
    assert rec["ok"] and rec["baseline"]["counts_per_s"] > 0 and rec["settings"]["filter"] == "10%"
    # now misaligned: the check fails, the operator runs AutoAlignment (OK), the check passes
    (config.active_bridge_dir() / "sim_misaligned").write_text("0.4")
    st = run_inline({"b": {"kind": "alignment", "sample": {"name": "Si ref"}, "setup": {"laser_nm": 532}}})
    r = st["studies"]["b"]
    att = r["alignment"]["attempts"]
    assert [m["passed"] for m in att] == [False, True] and att[0]["ratio"] < 0.7 and r["reliable"]
    assert r["video"]["after"]["measured_offset_um"] == [pytest.approx(0.4, abs=0.1), pytest.approx(0.3, abs=0.1)]
    status = api.alignment_status()["lasers"]["532"]
    assert status["last_check"]["passed"] is True and status["checks_on_record"] >= 3


def test_alignment_fails_when_autoalignment_does_not_help(tmp_path):
    _profile_with_shutter(tmp_path)
    st = run_inline({"a": {"kind": "alignment", "sample": {"name": "Si ref"}, "setup": SETUP}})
    api.alignment_record(st["request"])
    for f in ("sim_misaligned", "sim_autoalign_fails"):
        (config.active_bridge_dir() / f).write_text("0.4" if f == "sim_misaligned" else "1")
    st = run_inline({"b": {"kind": "alignment", "sample": {"name": "Si ref"}, "setup": {"laser_nm": 532}}})
    r = st["studies"]["b"]
    assert r["alignment"]["passed"] is False and not r["reliable"] and "HORIBA" in r["alignment"]["recommendation"]
    # a later spectrum on that laser warns
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "x"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520}}})
    assert any("alignment check" in w and "failed" in w for w in st["studies"]["s"]["warnings"])


def test_alignment_validation():
    r = F.validate({"a": {"kind": "alignment", "setup": {"laser_nm": 532}}})["a"]
    assert not r["ok"] and any("alignment.settings" in e for e in r["errors"])
    r = F.validate({"a": {"kind": "alignment", "setup": SETUP, "map": {"x_um": [0, 1, 1]}}})["a"]
    assert not r["ok"]


def test_profile_set(tmp_path):
    r = api.profile_set("video.laser_offset_um", "[1.4, -0.6]")
    assert r["ok"] and P.load()["video"]["laser_offset_um"] == [1.4, -0.6]
    with pytest.raises(KeyError):
        api.profile_set("video.nonsense", "1")
    # a laser named with a decimal point (2026-11-23: lasers.637.75.power_mw failed)
    prof = P.load()
    k = next(k for k in prof["lasers"] if "." in k) if any("." in k for k in prof["lasers"]) else None
    if k is None:
        prof["lasers"]["637.75"] = {"nm": 637.75, "power_mw": 35, "max_filter_pct": 100}
        P.save(prof)
        k = "637.75"
    assert api.profile_set(f"lasers.{k}.power_mw", "40")["ok"] and P.load()["lasers"][k]["power_mw"] == 40
    assert api.profile_set(f'lasers["{k}"].power_mw', "41")["ok"] and P.load()["lasers"][k]["power_mw"] == 41


# --------------------------------------------------------------------------------------------- stage and focus

def test_focus_scan_finds_the_focus():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_defocus").write_text("2.3")
    st = run_inline({"f": {"kind": "focus", "sample": {"name": "Si"}, "setup": SETUP}})
    r = st["studies"]["f"]
    start_z = float(json.loads((Path(st["request"]) / "start.json").read_text())["status"]["z_um"])
    assert r["reliable"] and r["focus"]["found"]
    assert r["focus"]["z_um"] - start_z == pytest.approx(2.3, abs=0.15)
    assert Path(r["images"]["focus_scan"]["path"]).exists()
    # the depth resolution: the simulator's axial response at x100 has a FWHM of 2 x 3 x sqrt(ln 2) = 5.0 µm
    assert r["focus"]["axial_fwhm_um"] == pytest.approx(5.0, abs=0.8)
    assert r["focus"]["axial_resolution"]["key"].startswith("x100@532")
    # the request ends at the focus found
    assert st["state"] == "done"
    assert float(api.instrument("status")["status"]["z_um"]) == pytest.approx(r["focus"]["z_um"], abs=0.01)


def test_focus_scan_without_a_clear_best_goes_back():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_defocus").write_text("30")            # far outside the scan: the signal hardly changes
    st = run_inline({"f": {"kind": "focus", "sample": {"name": "Si"}, "setup": SETUP,
                           "focus": {"scan": {"range_um": 6, "step_um": 1}}}})
    r = st["studies"]["f"]
    start_z = float(json.loads((Path(st["request"]) / "start.json").read_text())["status"]["z_um"])
    assert not r["focus"]["found"] and not r["reliable"]
    assert float(api.instrument("status")["status"]["z_um"]) == pytest.approx(start_z, abs=0.01)


def test_move_stays_and_later_offsets_follow():
    st = run_inline({"m": {"kind": "move", "move": {"x_um": -20, "y_um": 5}},
                     "s": {"kind": "spectrum", "sample": {"name": "x"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520, "exposure_s": 0.5}, "points": [{"x_um": 3}]}})
    start = json.loads((Path(st["request"]) / "start.json").read_text())["status"]
    x0, y0 = float(start["x_um"]), float(start["y_um"])
    m = st["studies"]["m"]
    assert m["move"]["stage_um"] == [pytest.approx(x0 - 20), pytest.approx(y0 + 5)]
    assert Path(m["images"]["video_here"]["path"]).exists()
    assert st["studies"]["s"]["spectra"][0]["stage"]["x_um"] == pytest.approx(x0 - 17)
    now = api.instrument("status")["status"]
    assert float(now["x_um"]) == pytest.approx(x0 - 20) and float(now["y_um"]) == pytest.approx(y0 + 5)


def test_request_returns_home_after_a_map():
    st = run_inline({"mp": {"kind": "map", "sample": {"name": "x"}, "setup": SETUP,
                            "acquire": {"center_cm1": 520, "exposure_s": 0.5},
                            "map": {"x_um": [0, 4, 2], "z_um": [-2, 2, 2]}}})
    start = json.loads((Path(st["request"]) / "start.json").read_text())["status"]
    now = api.instrument("status")["status"]
    for k in ("x_um", "y_um", "z_um"):
        assert float(now[k]) == pytest.approx(float(start[k]), abs=0.01), k
    assert "returned" in json.loads((Path(st["request"]) / "state.json").read_text())


def test_move_and_focus_validation():
    r = F.validate({"m": {"kind": "move", "move": {"x_um": 9000}}})["m"]
    assert not r["ok"] and any("max_xy_um" in e for e in r["errors"])
    r = F.validate({"m": {"kind": "move", "move": {"x_um": 1}, "setup": {"laser_nm": 532}}})["m"]
    assert not r["ok"] and any("only the objective" in e for e in r["errors"])
    r = F.validate({"f": {"kind": "focus", "setup": SETUP, "focus": {"scan": {"range_um": 500}}}})["f"]
    assert not r["ok"] and any("range_um" in e for e in r["errors"])
    r = F.validate({"f": {"kind": "focus", "setup": SETUP, "focus": {"scan": {"range_um": 2, "step_um": 1}}}})["f"]
    assert not r["ok"] and any("at least 5" in e for e in r["errors"])
    r = F.validate({"m": {"kind": "map", "setup": SETUP, "map": {"x_um": [0, 2, 1]},
                          "focus": {"autofocus": "scan", "every": 2}}})["m"]
    assert r["ok"], r["errors"]


def test_absolute_z_limit(tmp_path):
    prof = P.load()
    prof["stage"]["z_um"] = [0, 251]                  # the simulator's focus is at 250
    P.save(prof)
    st = run_inline({"p": {"kind": "spectrum", "sample": {"name": "x"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520}, "points": [{"dz_um": 5}]}})
    r = st["studies"]["p"]
    assert not r["ok"] and "stage limits" in r["why"]


# --------------------------------------------------------------------------------------------- the laser spot

def test_spot_by_video_during_acquisition_is_remembered():
    st = run_inline({"v": {"kind": "spot", "sample": {"name": "Si"}, "setup": SETUP,
                           "spot": {"method": "acquisition"}}})
    sp = st["studies"]["v"]["spot"]
    assert sp["found"] and sp["remembered"]
    assert sp["offset_um"] == [pytest.approx(1.5, abs=0.1), pytest.approx(-0.8, abs=0.1)]
    rec = api.spot_status()["records"]["x100@532"]
    assert rec["method"] == "acquisition"
    # a later snapshot marks the beam with it
    snap = api.snapshot()
    assert snap["marker_um"][0] - snap["stage"]["x_um"] == pytest.approx(1.5, abs=0.1)
    assert snap["offset_source"].startswith("measured")


def test_spot_by_raman_on_a_feature_and_drift():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    snap = api.snapshot()
    dust = [f for f in snap["features"] if f["dark"]]
    assert dust and dust[0]["x_um"] == pytest.approx(1219.5, abs=1.0)
    run_inline({"v": {"kind": "spot", "sample": {"name": "Si"}, "setup": SETUP, "spot": {"method": "acquisition"}}})
    (bdir / "sim_spot_offset").write_text("1.1,0.6")          # the beam drifted
    st = run_inline({"r": {"kind": "spot", "sample": {"name": "Si"}, "setup": SETUP,
                           "spot": {"method": "raman", "feature_um": [dust[0]["x_um"], dust[0]["y_um"]]}}})
    r = st["studies"]["r"]
    assert r["reliable"] and r["spot"]["method"] == "raman"
    assert r["spot"]["offset_um"] == [pytest.approx(1.1, abs=0.15), pytest.approx(0.6, abs=0.15)]
    assert r["spot"]["drift_um"] == pytest.approx(1.47, abs=0.2)
    assert any("the beam moved" in w for w in r["warnings"])
    assert Path(r["images"]["spot_scan"]["path"]).exists()


def test_move_to_puts_the_beam_on_the_point():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_spot_offset").write_text("1.1,0.6")
    run_inline({"v": {"kind": "spot", "sample": {"name": "Si"}, "setup": SETUP, "spot": {"method": "acquisition"}}})
    st = run_inline({"m": {"kind": "move", "move": {"to_um": [1219.5, -557.8], "stay": False}}})
    m = st["studies"]["m"]["move"]
    assert m["stage_um"] == [pytest.approx(1219.5 - 1.1, abs=0.15), pytest.approx(-557.8 - 0.6, abs=0.15)]


def test_spot_validation():
    r = F.validate({"s": {"kind": "spot", "setup": {"laser_nm": 532}}})["s"]
    assert not r["ok"] and any("objective" in e for e in r["errors"])
    r = F.validate({"s": {"kind": "spot", "setup": SETUP, "spot": {"method": "shutter"}}})["s"]
    assert not r["ok"] and any("laser shutter" in e for e in r["errors"])
    r = F.validate({"s": {"kind": "spot", "setup": SETUP, "spot": {"method": "raman", "feature_um": [0, 0],
                                                                   "step_um": 0.05}}})["s"]
    assert not r["ok"] and any("at most 441" in e for e in r["errors"])
    r = F.validate({"m": {"kind": "move", "move": {"to_um": [1, 2], "x_um": 3}}})["m"]
    assert not r["ok"]


# --------------------------------------------------------------------------------------------- depth profiles

def test_depth_profile_finds_the_film_interfaces():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_film").write_text("12,1.59")
    st = run_inline({"d": {"kind": "depth", "sample": {"name": "PS film on Si"}, "setup": dict(SETUP, grating=600),
                           "acquire": {"range_cm1": [400, 1700], "exposure_s": 0.5},
                           "depth": {"from_um": -4, "to_um": 16, "step_um": 1, "refractive_index": 1.59},
                           "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30},
                                                 {"name": "PS", "near_cm1": 1001.4, "window_cm1": 20}]},
                           "images": True}})
    r = st["studies"]["d"]
    assert r["kind"] == "depth" and r["reliable"]
    by = r["depth"]["interfaces_by"]
    assert by["Si"]["height"]["rise"] == pytest.approx(12.0, abs=0.8)
    assert by["PS"]["height"]["fall"] == pytest.approx(12.0, abs=0.8)
    assert by["PS"]["height"]["rise"] == pytest.approx(0.0, abs=0.8)
    assert r["depth"]["depth_um"][0] == pytest.approx(-4 * 1.59)
    assert Path(r["images"]["depth"]["path"]).exists()
    # Z is back at the focus at the end
    start = json.loads((Path(st["request"]) / "start.json").read_text())["status"]
    assert float(api.instrument("status")["status"]["z_um"]) == pytest.approx(float(start["z_um"]), abs=0.01)


def test_depth_direction_and_validation():
    prof = P.load()
    prof["stage"]["z_into_sample"] = "-"
    P.save(prof)
    m = plan.depth_as_map({"kind": "depth", "depth": {"from_um": -2, "to_um": 6, "step_um": 2}}, P.load())
    assert m["map"]["z_um"] == [2.0, -6.0, 2.0]
    assert plan.axis_values(m["map"]["z_um"]) == [2.0, 0.0, -2.0, -4.0, -6.0]
    r = F.validate({"d": {"kind": "depth", "setup": SETUP, "depth": {"from_um": 0, "to_um": 500, "step_um": 1}}})["d"]
    assert not r["ok"] and any("max_dz_um" in e for e in r["errors"])
    r = F.validate({"d": {"kind": "depth", "setup": SETUP, "depth": {"refractive_index": 9}}})["d"]
    assert not r["ok"] and any("refractive_index" in e for e in r["errors"])


# --------------------------------------------------------------------------------------------- spectral zero

def test_calibration_offset_and_ls6t_correction():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_xshift").write_text("1.5,1.0")         # the scale reads 1.5 cm-1 high
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                           "calibration": {"adjust": "ls6t"}},
                     "s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520},
                           "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}})
    cal = st["studies"]["c"]["calibration"]
    assert cal["offset_cm1"] == pytest.approx(1.5, abs=0.25) and cal["stored_correction"]
    assert cal["corrected"] and cal["outcome"].startswith("out of tolerance before correction (+1.5")
    assert st["studies"]["s"]["peaks_by_name"]["Si"]["position_cm1"] == pytest.approx(520.7, abs=0.25)
    assert any("spectral correction applied" in w for w in st["studies"]["s"]["warnings"])


def test_calibration_guided_labspec_adjustment():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_xshift").write_text("2.0,1.0")
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                           "calibration": {"adjust": "labspec"}}})
    cal = st["studies"]["c"]["calibration"]
    assert [a["pass"] for a in cal["attempts"]] == [False, True] and cal["pass"]
    assert st["studies"]["c"]["reliable"]
    assert list(cal["labspec_parameters"]["changed"]) == ["BF"]          # which parameter the operator changed
    r = api.instrument("calibration")
    assert r["ok"] and r["grating_in_use"] == "1800" and r["gratings"]["600"]["Focal"] == 198.8


def test_calibration_polystyrene_offset_and_scale():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_xshift").write_text("2.0,1.002")
    (bdir / "sim_film").write_text("50,1.59")
    st = run_inline({"p": {"kind": "calibration", "sample": {"name": "PS"}, "setup": dict(SETUP, grating=600),
                           "calibration": {"reference": "polystyrene"},
                           "acquire": {"exposure_s": 2, "accumulations": 2}}})
    cal = st["studies"]["p"]["calibration"]
    assert cal["lines_used"] >= 3 and "scale" in cal["fit"]
    assert cal["correction"]["b"] == pytest.approx(1 / 1.002, abs=0.0015)
    assert not cal["pass"]


# --------------------------------------------------------------------------------------------- white-light background

def test_white_light_background_is_subtracted(tmp_path):
    _profile_with_shutter(tmp_path)
    bdir = config.active_bridge_dir()
    (bdir / "sim_lamp").write_text("on")
    acq = {"range_cm1": [200, 1800], "exposure_s": 1, "accumulations": 2}
    setup = dict(SETUP, grating=600)
    st = run_inline({"bg": {"kind": "background", "sample": {"name": "lamp"}, "setup": setup, "acquire": acq,
                            "background": {"name": "lamp_x100"}}})
    r = st["studies"]["bg"]
    assert r["reliable"] and r["background"]["max_cps"] > 500 and not r["warnings"]
    st = run_inline({"raw": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": setup, "acquire": acq},
                     "sub": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": setup,
                             "acquire": dict(acq, subtract_background="lamp_x100")}})
    levels = {}
    for k in ("raw", "sub"):
        d = spectra.read(st["studies"][k]["spectra"][0]["file"].replace(".txt", ".shift.txt"))
        sel = (d["x"] > 1100) & (d["x"] < 1400)
        levels[k] = float(d["y"][sel].mean())
    assert levels["raw"] > 1000 and levels["sub"] < 0.2 * levels["raw"]
    assert st["studies"]["sub"]["spectra"][0]["peaks"]


def test_background_validation():
    r = F.validate({"x": {"kind": "spectrum", "setup": SETUP, "acquire": {"subtract_background": "nope"}}})["x"]
    assert not r["ok"] and any("no such background" in e for e in r["errors"])
    r = F.validate({"b": {"kind": "background", "setup": SETUP, "background": {"name": "a b"}}})["b"]
    assert not r["ok"] and any("background.name" in e for e in r["errors"])


# --------------------------------------------------------------------------------------------- native maps



def test_native_map_matches_points():
    res = {}
    for eng in ("points", "native"):
        st = run_inline(_flake_map(eng))
        res[eng] = st["studies"]["m"]
        assert res[eng]["reliable"] and res[eng]["points_measured"] == 15
    g = {e: json.loads(Path(r["map"]["files"]["json"]).read_text())["grids"]["G.position_cm1"][0]
         for e, r in res.items()}
    found = {e: [[v is not None for v in row] for row in g[e]] for e in g}
    assert found["points"] == found["native"]                  # the same points see graphene
    assert res["native"]["map"]["summary"]["G"]["position_cm1"]["mean"] == pytest.approx(1585, abs=1)


def test_native_map_operator_stop():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_operator_stop_after").write_text("0")
    st = run_inline(_flake_map("native"))
    assert st["stopped_by"] == "operator" and not st["studies"]["m"]["ok"]


def test_native_map_validation_and_auto():
    r = F.validate({"m": {"kind": "map", "setup": SETUP, "acquire": {"range_cm1": [100, 3000]},
                          "map": {"x_um": [0, 1, 1], "engine": "native"}}})["m"]
    assert not r["ok"] and any("one detector window" in e for e in r["errors"])
    r = F.validate({"m": {"kind": "map", "setup": SETUP, "acquire": {"center_cm1": 520},
                          "map": {"x_um": [0, 1, 1], "swift": True}}})["m"]
    assert not r["ok"] and any("swift" in e for e in r["errors"])
    big = {"kind": "map", "acquire": {"center_cm1": 520}, "map": {"x_um": [0, 19, 1], "y_um": [0, 19, 1]}}
    assert plan.map_engine(big, P.load()) == "native"
    assert plan.map_engine(dict(big, focus={"autofocus": "laser", "every": 10}), P.load()) == "points"


def test_calibration_belongs_to_one_laser_and_grating():
    """2026-11-23, 637.75 nm: after AutoCalibration Si was +0.3 cm-1 off with the 1200 grating and +3.1 with the
    2400. A calibration check is kept per laser and grating; spectra say what the last check of their pair found;
    the result and LabSpec's prompts name the pair. And each study says where its time went."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_xshift").write_text("3.0,1.0")
    si = {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP},
                     "s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520}, "process": si},
                     "o": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": {**SETUP, "grating": 1200},
                           "acquire": {"center_cm1": 520}, "process": si}})
    c = st["studies"]["c"]
    assert c["calibration"]["applies_to"]["key"] == "532@1800" and "1800 gr/mm grating with 532 nm" in \
        c["calibration"]["applies_to"]["what"]
    assert any("for the 1800 gr/mm grating with 532 nm" in u for u in c["unreliable_because"])
    assert c["calibration"]["outcome"].startswith("out of tolerance (+3.0") and not c["calibration"].get("corrected")
    s, o = st["studies"]["s"], st["studies"]["o"]
    assert s["checks"]["zero"]["offset_cm1"] == pytest.approx(3.0, abs=0.3)
    assert any("last calibration check for the 1800 gr/mm grating with 532 nm" in w for w in s["warnings"])
    assert o["checks"]["zero"]["offset_cm1"] is None and "532@1800" in o["checks"]["zero"]["other_gratings"]
    assert any("no calibration check yet for the 1200 gr/mm grating" in w for w in o["warnings"])
    t = s["timing"]
    assert t["bridge_commands"]["acquire"]["n"] == 1 and "t_until_data_s" in t["acquire_phases_s"]
    assert t["overhead_s"] == pytest.approx(t["total_s"] - t["exposure_s"], abs=0.02)
    assert t["operator_s"] == 0 and "operator" in s["seconds"]          # the simulator answers at once
    from labspec_translator import runner
    tm = runner.timing([{"verb": "confirm", "s": 6.0}, {"verb": "acquire", "s": 23.0, "t_acq_call_s": 0.1,
                                                         "t_motors_after_s": 18.0, "t_acquire_total_s": 22.5}],
                       [{"exposure_s": 1, "accumulations": 1}], 30.0)
    assert tm["operator_s"] == 6.0 and tm["overhead_s"] == 23.0 and tm["acquire_handoff_s"] == 0.5
    assert "until every motor reported idle (18.0 s)" in tm["note"]


def test_laser_off_shutter_is_read_back_and_checked(tmp_path):
    """2026-11-23 (safety): a laser_off 'shutter' background kept 86 % of the Si signal. The bridge now moves the
    shutter by its label (Off), reads it back before and during the acquisition, cancels if LabSpec opens it, and a
    background with Raman lines while the shutter was trusted is not stored."""
    _profile_with_shutter(tmp_path)
    bdir = config.active_bridge_dir()
    setup = dict(SETUP, grating=1800)
    bg = {"kind": "background", "sample": {"name": "Si"}, "setup": setup,
          "acquire": {"center_cm1": 520, "exposure_s": 1}, "background": {"name": "shut", "laser_off": "shutter"}}
    st = run_inline({"b": bg})
    r = st["studies"]["b"]
    assert r["reliable"] and r["laser_shutter"]["shutter_closed_label"] == "Off" and r["background"].get("stored", True)
    assert r["laser_shutter"]["during_checked"] is True and r["laser_shutter"]["reads"] > 1
    # LabSpec's Acq holds the bridge until the spectrum is taken: no read during it, and the result says so
    (bdir / "sim_acq_holds").write_text("1")
    st = run_inline({"b": dict(bg, background={"name": "shut1", "laser_off": "shutter"})})
    r = st["studies"]["b"]
    assert r["laser_shutter"]["during_checked"] is False and "verification on Si" in r["laser_shutter"]["during_note"]
    assert not any("could not read the laser shutter" in w for w in r["warnings"])     # verified: a note, no warning
    warns: list = []
    rep = runner.shutter_report({"t_acq_call_s": 17.46, "t_until_data_s": 0.05, "shutter_reads": 1}, warns)
    assert rep["during_checked"] is False and any("could not read the laser shutter" in w for w in warns)
    (bdir / "sim_acq_holds").unlink()
    # lines that survive spike removal with a verified shutter: said (lamp, room light?), not taken for the laser
    (bdir / "sim_shutter_ignored").write_text("1")            # reads Off, beam still passes: the Si line is there
    st = run_inline({"b": dict(bg, background={"name": "shut2", "laser_off": "shutter"})})
    r = st["studies"]["b"]
    assert any("after spike removal" in w and "shutter-verify again" in w for w in r["warnings"])
    (bdir / "sim_shutter_ignored").unlink()
    (bdir / "sim_shutter_reopens").write_text("1")            # LabSpec opens it during the acquisition
    st = run_inline({"b": dict(bg, background={"name": "shut3", "laser_off": "shutter"})})
    assert "LabSpec opened it" in st["studies"]["b"]["why"]
    (bdir / "sim_shutter_reopens").unlink()
    # the diagnostic: five moves, each read back and asked; LabSpec's mode and the shutter put back
    t = api.shutter_test()
    assert t["ok"] and len(t["steps"]) == 5 and t["steps"][2]["read_label"] == "Off" and "verdict" in t
    text = api.compose_vbs(bdir, config.home())["ls6t_bridge.vbs"]
    assert "LabSpec.MoveMotor shutter, 0, lab, LS_MOTOR_STRING" in text and "ShutterOpened()" in text
    assert 'LabSpec.MoveMotor shutter, 0, "", LS_MOTOR_VALUE' not in text.split("Function ShutterTo")[0]


def test_labspec_auto_exposure_does_not_replace_the_exposure_asked():
    """2026-11-23: with LabSpec's AutoExposure on, 2 s asked ran 15 s while ls6t reported 2 s. The bridge switches it
    off for a fixed exposure; the exposure reported is LabSpec's own; if LabSpec still used another, the study stops."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_autoexposure").write_text("1")
    sp = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 2}}
    # by default the bridge only reads AE (the template's write is not confirmed yet): LabSpec's AE wins, ls6t stops
    st = run_inline({"s": sp})
    assert not st["studies"]["s"]["ok"] and "instead of the 2 s asked" in st["studies"]["s"]["why"]
    P.save(dict(P.load(), autoexposure={"switch_off": True}))
    st = run_inline({"s": sp})
    s = st["studies"]["s"]
    assert s["reliable"] and s["spectra"][0]["exposure_s"] == 2
    (bdir / "sim_ae_sticky").write_text("1")
    st = run_inline({"s": sp})
    s = st["studies"]["s"]
    assert not s["ok"] and "instead of the 2 s asked" in s["why"] and "AutoExposure" in s["why"]
    # the bridge reaches it through LabSpec's Template (6.8.1 help: SET_VALUE 3, GET_VALUE 4, "AutoExposure")
    text = api.compose_vbs(bdir, config.home())["ls6t_bridge.vbs"]
    assert 'LabSpec.Template(LS_TEMPLATE_GET_VALUE, name, v)' in text and "Const LS_TEMPLATE_SET_VALUE = 3" in text
    assert 'TplSet("AutoExposure", aeOff, res, "ae_set")' in text and "PutValue 0, aeN" not in text


def test_shutter_used_only_once_shown_to_block_on_si(tmp_path):
    """Nicola's suggestion (2026-11-23): the narrow-line check cannot protect samples without sharp lines, so
    laser_off 'shutter' (and laser_on, snapshots with the laser) needs a passed spectral test on Si: a record per
    laser, refused when missing, too old, for another motor or way of moving it, or another shutter revision."""
    import time as _t
    from labspec_translator import shutter as SH
    sim = S.ensure(config.active_bridge_dir())
    b = bridge.Bridge()
    b.call("discover", timeout_s=10, out=str(tmp_path / "dump.txt"))
    prof, _ = P.import_discovery(P.read_text(tmp_path / "dump.txt"))
    prof["motors"]["laser_shutter"] = prof["laser_shutter_candidate"]
    prof["shutter"]["max_leak_frac"] = 0.05        # the simulator's Si line is too weak to see a 0.1 % leak
    P.save(prof)
    # the rule, with the XploRA's numbers (2026-11-23): 0.04 % left, seen (SNR 14.8) but under the 0.1 % limit
    v = api.leak_verdict({"si_signal_cps": 1185.4}, {"si_signal_cps": 0.494, "noise_cps": 0.033, "snr": 14.8}, 5.0, 0.001)
    assert v["status"] == "blocked" and v["leak_detected"] and v["leak_frac"] == pytest.approx(0.000417, abs=1e-5)
    assert api._pct(v["leak_frac"]) == "0.042 %"
    assert api.leak_verdict({"si_signal_cps": 100}, {"si_signal_cps": 86, "noise_cps": 1, "snr": 86}, 5, 0.001)[
        "status"] == "leak"
    assert api.leak_verdict({"si_signal_cps": 50}, {"si_signal_cps": 0.1, "noise_cps": 1, "snr": 0.1}, 5, 0.001)[
        "status"] == "not_conclusive"
    bg = {"kind": "background", "sample": {"name": "lamp"}, "setup": SETUP,
          "acquire": {"center_cm1": 520, "exposure_s": 1}, "background": {"name": "g", "laser_off": "shutter"}}
    v = api.validate({"b": bg})["b"]
    assert not v["ok"] and any("has not been shown to block the 532 nm laser" in e and "shutter-verify" in e
                               for e in v["errors"])
    assert "has not been shown" in api.laser(True, 5)["error"]
    # unverified: a background without laser_off defaults to the operator blocking it by hand
    assert plan.cutoff_params({"kind": "cutoff", "setup": SETUP}, P.load())["laser_off"] == "operator"
    # at 1 % the simulated line is too weak to tell a 5 % leak: not conclusive
    r = api.shutter_verify()
    assert not r["ok"] and "not conclusive" in r["error"] and not SH.records()
    api.bridge_call("move", {"motor": "Filter", "mode": "label", "value": "10%"})
    # the beam passes although the shutter reads Off: the verification fails and writes nothing
    (config.active_bridge_dir() / "sim_shutter_ignored").write_text("1")
    r = api.shutter_verify()
    assert not r["ok"] and r["blocked"] is False and "NOT BLOCKED" in r["error"] and not SH.records()
    (config.active_bridge_dir() / "sim_shutter_ignored").unlink()
    r = api.shutter_verify()
    assert r["ok"] and r["sensitivity"] <= 0.05 and r["record"]["laser_nm"] == 532, r.get("error")
    assert r["record"]["leak_frac"] <= 0.05 and "reference_cps" in r["record"]
    assert r["shutter_closed"]["laser_shutter"]["during_checked"] is True
    assert api.validate({"b": bg})["b"]["ok"]
    assert SH.gate(P.load(), 637.75) and "637.75 nm" in SH.gate(P.load(), 637.75)     # per laser
    # another way of moving it, or too old: verify again
    prof = P.load()
    prof["shutter"]["by"] = "value"
    assert "moved by label" in SH.gate(prof, 532)
    rec = SH.records()["532"]
    SH.record(dict(rec, time=_t.time() - 40 * 86400))
    assert "days ago" in SH.gate(P.load(), 532)
    SH.record(dict(rec, rev=SH.REV - 1))
    assert "shutter handling has changed" in SH.gate(P.load(), 532)
    r = api.start({"b": bg}, inline=True)
    assert not r["ok"] and "shutter handling has changed" in r["errors"]["b"][0]
    # and at run time, should validation be bypassed
    with pytest.raises(runner.Stop):
        runner._shutter_gate(P.load(), 532)
    del sim
