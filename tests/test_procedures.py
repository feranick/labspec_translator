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
    # by wavelength, the laser stored under another key (the XploRA: '638' with nm 637.75), and quoted
    prof = P.load()
    prof["lasers"]["638"] = dict(prof["lasers"].get("638", {}), nm=637.75, power_mw=35, max_filter_pct=100)
    prof["lasers"].pop("637.75", None)
    P.save(prof)
    assert api.profile_set("lasers.637.75.unstable_multi", "true")["ok"] and P.load()["lasers"]["638"]["unstable_multi"]
    assert api.profile_set('lasers."638".mode_hops', "true")["ok"] and P.load()["lasers"]["638"]["mode_hops"]
    with pytest.raises(KeyError, match="here: "):
        api.profile_set("lasers.640.power_mw", "1")


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
    # (2026-11-23: noise 'lines' of a few counts at nine different places in three backgrounds) only lines above
    # checks.background_line_min_counts that recur in another background are warned about
    (bdir / "sim_shutter_ignored").write_text("1")            # reads Off, beam still passes: the Si line is there
    st = run_inline({"b": dict(bg, background={"name": "shut2", "laser_off": "shutter"})})
    r = st["studies"]["b"]
    nl = r["narrow_lines"]
    assert nl["above_min"] and not nl["recurring"] and not any("recur" in w for w in r["warnings"])
    st = run_inline({"b": dict(bg, background={"name": "shut2b", "laser_off": "shutter"})})
    r = st["studies"]["b"]
    assert r["narrow_lines"]["recurring"] and any("recur in earlier backgrounds" in w and "shutter-verify again" in w
                                                  for w in r["warnings"])
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
    # with switch_off false the bridge only reads AE: LabSpec's AE wins, ls6t stops
    P.save(dict(P.load(), autoexposure={"switch_off": False}))
    st = run_inline({"s": sp})
    assert not st["studies"]["s"]["ok"] and "instead of the 2 s asked" in st["studies"]["s"]["why"]
    P.save(dict(P.load(), autoexposure={"switch_off": True}))      # the default since T3.19 passed
    st = run_inline({"s": sp})
    s = st["studies"]["s"]
    assert s["reliable"] and s["spectra"][0]["exposure_s"] == 2
    ae = s["auto_exposure"]                                          # the pass, visible in the result
    assert ae["on_before"] and ae["switched_off"] and ae["off_during"] and ae["restored"] and ae["all_restored"]
    assert s["spectra"][0]["auto_exposure"]["after"] == 1
    (bdir / "sim_ae_sticky").write_text("1")
    st = run_inline({"s": sp})
    s = st["studies"]["s"]
    assert not s["ok"] and "instead of the 2 s asked" in s["why"] and "AutoExposure" in s["why"]
    # the bridge reaches it through LabSpec's Template (6.8.1 help: SET_VALUE 3, GET_VALUE 4, "AutoExposure")
    text = api.compose_vbs(bdir, config.home())["ls6t_bridge.vbs"]
    assert 'LabSpec.Template(LS_TEMPLATE_GET_VALUE, name, v)' in text and "Const LS_TEMPLATE_SET_VALUE = 3" in text
    assert 'TplSet("AutoExposure", aeOff, res, "ae_set")' in text and "PutValue 0, aeN" not in text
    assert "gTplTest = TplTest()" in text and "Const LS_TEMPLATE_TEST = 8" in text      # the plugin, at start
    assert "autoexposure=1" in api.instrument("status")["ping"]["template_test"]


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
    # at 1 % and 1 s the simulated line is too weak to tell a 5 % leak: not conclusive
    r = api.shutter_verify(1.0)
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
    # judged on the worst of several closed spectra; the failures before this pass are in the record (2026-11-23)
    assert len(r["closed"]) == 3 and r["worst_leak_frac"] == max(r["leak_fracs"])
    assert r["record"]["failed_attempts_before"] == 2 and "earlier attempt" in r["note"]
    assert [a["status"] for a in r["record"]["attempts"]][-3:] == ["not_conclusive", "leak", "blocked"]
    # the burst check: one more closed spectrum of another exposure, counts per acquisition compared
    r2 = api.shutter_verify(2.0, 2, 8.0)
    assert r2["ok"] and len(r2["closed"]) == 3 and r2["burst_check"]["exposure_ratio"] == pytest.approx(4)
    assert r2["record"]["failed_attempts_before"] == 0
    assert r["closed"][0]["laser_shutter"]["during_checked"] is True
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


def test_spectra_at_the_same_spot_must_agree():
    """2026-11-23 (serious): in a request only the first spectrum was right; points 1-4 at the same spot had Si 8 cm-1
    low and 2.5 x broader, and nothing in LabSpec's records showed it. ls6t now compares repeated spectra at the same
    spot with the first (unreliable when they disagree), records what the bridge read around each Acq
    (optics_check) and can pause after each spectrum (timing.acquire_settle_s)."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    st0 = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 1},
           "points": [{}, {}, {}], "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    st = run_inline({"s": st0})
    s = st["studies"]["s"]
    assert s["reliable"] and not s["checks"]["same_spot"]["disagree"]
    (bdir / "sim_shift_after_first").write_text("1")
    st = run_inline({"s": st0})
    s = st["studies"]["s"]
    ss = s["checks"]["same_spot"]
    assert [b["point"] for b in ss["disagree"]] == [1, 2] and ss["disagree"][0]["shift_cm1"] == pytest.approx(-8.7, abs=0.5)
    assert not s["reliable"] and any("disagree with point 0" in u for u in s["unreliable_because"])
    text = api.compose_vbs(bdir, config.home())["ls6t_bridge.vbs"]
    assert 'res("spectro_after_acq_call") = MotorValue(mspec)' in text and 'GetP(cmd, "settle_s", "0")' in text


def test_laser_mode_hop_is_seen_and_corrected_with_the_laser_line():
    """2026-11-23: the 637.75 nm laser hopped after the first spectrum of a request (laser line -4.6 -> -12 cm-1, Si
    520.2 -> 512.4, their difference constant). With the laser line in the window ls6t fits it in every spectrum,
    flags a jump, and with laser_reference 'correct' shifts each spectrum back; warmup_s puts a discarded
    acquisition first."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_laser_line").write_text("1")
    st0 = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600),
           "acquire": {"center_cm1": 250, "exposure_s": 1}, "points": [{}, {}, {}],
           "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    (bdir / "sim_shift_after_first").write_text("1")
    st = run_inline({"s": st0})
    s = st["studies"]["s"]
    ll = s["checks"]["laser_line"]
    assert [j["point"] for j in ll["jumps"]] == [1] and ll["jumps"][0]["moved_cm1"] == pytest.approx(-8.7, abs=0.6)
    assert ll["points_after_a_jump"] == [1, 2] and ll["jumps"][0]["confirmed_by_peaks"] == pytest.approx(-8.7, abs=0.6)
    assert not s["reliable"] and any("laser line moved" in u for u in s["unreliable_because"])
    sim = S.ensure(bdir)
    sim._shift_armed = False                                    # a new 'first spectrum'
    st = run_inline({"s": dict(st0, acquire=dict(st0["acquire"], laser_reference="correct"))})
    s = st["studies"]["s"]
    pos = [p["peaks"][0]["position_cm1"] for p in s["spectra"]]
    assert max(pos) - min(pos) < 1.0, pos                       # corrected: Si where point 0 had it
    assert any("corrected" in w for w in s["warnings"]) and not s["checks"]["same_spot"]["disagree"]
    # a warm-up acquisition first: the hop happens there, the measured spectra agree
    sim._shift_armed = False
    st = run_inline({"s": dict(st0, acquire=dict(st0["acquire"], warmup_s=1))})
    s = st["studies"]["s"]
    assert s["warmup"]["ok"] and not s["checks"]["laser_line"]["jumps"] and not s["checks"]["same_spot"]["disagree"]
    assert not F.validate({"x": dict(st0, acquire=dict(st0["acquire"], laser_reference="maybe"))})["x"]["ok"]
    # each peak also from the spectrum's own laser line
    assert s["spectra"][0]["peaks"][0]["from_laser_line_cm1"] == pytest.approx(520.7, abs=1.0)
    # a laser known to change between acquisitions: multi-spectrum studies refused unless corrected (2026-11-23)
    prof = P.load()
    lk = next(k for k, v in prof["lasers"].items() if abs(v["nm"] - 532) < 1)
    prof["lasers"][lk]["unstable_multi"] = True
    P.save(prof)
    v = F.validate({"x": st0})["x"]
    assert not v["ok"] and any("unstable_multi" in e for e in v["errors"])
    assert F.validate({"x": dict(st0, acquire=dict(st0["acquire"], laser_reference="correct"))})["x"]["ok"]
    assert F.validate({"x": dict(st0, points=[{}])})["x"]["ok"]                 # one spectrum: fine
    # a pause between spectra for that laser fixes it (2026-11-23: 5 s held the 637.75 nm laser), and is counted
    t0 = plan.estimate(st0, prof)["seconds"]["total"]
    prof["lasers"][lk]["settle_s"] = 5
    P.save(prof)
    assert F.validate({"x": st0})["x"]["ok"]
    assert plan.estimate(st0, P.load())["seconds"]["total"] == pytest.approx(t0 + 2 * 5, abs=0.5)
    assert plan.settle_s(P.load(), 532) == 5 and plan.settle_s(P.load(), 785) == 0     # that laser only
    nat = {"kind": "map", "sample": {"name": "x"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 1},
           "map": {"x_um": [0, 2, 1], "y_um": [0, 0, 1], "engine": "native"}}
    v = F.validate({"m": nat})["m"]
    assert not v["ok"] and any("map engine cannot pause" in e for e in v["errors"])
    prof["lasers"][lk]["settle_s"] = 0
    prof["lasers"][lk]["unstable_multi"] = "warn"
    P.save(prof)
    v = F.validate({"x": st0})["x"]
    assert v["ok"] and any("unstable_multi" in w for w in v["warnings"])
    prof["lasers"][lk]["unstable_multi"] = False
    P.save(prof)
    # 'correct' without the laser line in the window: said
    st = run_inline({"s": dict(st0, acquire={"center_cm1": 520, "exposure_s": 1, "laser_reference": "correct"},
                               setup=SETUP)})
    assert any("no laser line was found" in u for u in st["studies"]["s"]["unreliable_because"])
    # the diagnostic: the start-of-request status read before every spectrum
    n0 = s["timing"]["bridge_commands"]["status"]["n"]
    P.save(dict(P.load(), timing=dict(P.load()["timing"], status_before_each=True)))
    st = run_inline({"s": st0})
    assert st["studies"]["s"]["timing"]["bridge_commands"]["status"]["n"] == n0 + 2


def test_shutter_held_open_across_spectra(tmp_path):
    """HORIBA's mapping scripts (StandardMapping / SWIFTMapping XploRA.vbs) set ShutterMode manual and open the laser
    shutter for the whole map. acquire.shutter 'hold' does the same for a study's spectra: the 637.75 nm laser
    changed mode when LabSpec closed and opened the shutter around each spectrum (2026-11-23). Needs the verified
    shutter; released after the spectra (and by the bridge at a halt, another command, or max_s)."""
    _profile_with_shutter(tmp_path)
    bdir = config.active_bridge_dir()
    sim = S.ensure(bdir)
    st0 = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 1},
           "points": [{}, {}, {}], "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    (bdir / "sim_shift_after_first").write_text("1")
    sim._shift_armed = False
    st = run_inline({"s": st0})
    assert st["studies"]["s"]["checks"]["same_spot"]["disagree"]            # LabSpec's shutter per spectrum: hop
    sim._shift_armed = False
    st = run_inline({"s": dict(st0, acquire=dict(st0["acquire"], shutter="hold"))})
    s = st["studies"]["s"]
    assert s["reliable"] and not s["checks"]["same_spot"]["disagree"], s["unreliable_because"]
    assert s["shutter_hold"]["held"] and s["shutter_hold"]["released"] and not sim.held
    assert s["shutter_hold"]["times_held"] == 1
    # every kind that acquires: a filters study holds too (2026-11-23: it was not held and read 0.54 x low)
    st = run_inline({"f": {"kind": "filters", "sample": {"name": "Si"}, "setup": {"laser_nm": 532, "grating": 1800,
                                                                                 "objective": "x100"},
                           "acquire": {"shutter": "hold"}, "filters": {"exposure_s": 0.2, "max_exposure_s": 5}}})
    f = st["studies"]["f"]
    assert f["shutter_hold"]["held"] and f["shutter_hold"]["released"] and f["shutter_hold"]["times_held"] == 1
    assert not sim.held
    # the laser's own setting, and refused without a verified shutter
    prof = P.load()
    lk = next(k for k, v in prof["lasers"].items() if abs(v["nm"] - 532) < 1)
    prof["lasers"][lk]["hold_shutter"] = True
    prof["lasers"][lk]["unstable_multi"] = True
    P.save(prof)
    assert plan.hold_shutter(st0, P.load(), 532) and F.validate({"s": st0})["s"]["ok"]
    nat = {"kind": "map", "sample": {"name": "x"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 1},
           "map": {"x_um": [0, 2, 1], "y_um": [0, 0, 1], "engine": "native"}}
    assert F.validate({"m": nat})["m"]["ok"]                   # LabSpec's own map, held as HORIBA's scripts do
    from labspec_translator import shutter as SH
    store_file = SH.store.path(SH.FILE)
    store_file.unlink()
    v = F.validate({"s": st0})["s"]
    assert not v["ok"] and any("acquire.shutter 'hold'" in e for e in v["errors"])
    text = api.compose_vbs(bdir, config.home())["ls6t_bridge.vbs"]
    assert "Sub VShutterHold(cmd, res)" in text and 'HoldRelease "bridge stopped"' in text


def test_series_hold_native_and_estimate():
    """2026-11-23 on the first series with 637.75 nm: spectra 5 s apart need no held shutter (the laser recovers in
    its settle_s), the estimate shows the waits, and series spectra are saved as .l6s too."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    prof = P.load()
    lk = next(k for k, v in prof["lasers"].items() if abs(v["nm"] - 532) < 1)
    prof["lasers"][lk].update(hold_shutter=True, settle_s=5)
    P.save(prof)
    ser = {"kind": "series", "sample": {"name": "Si"}, "setup": SETUP,
           "acquire": {"center_cm1": 520, "exposure_s": 1}, "series": {"count": 3, "interval_s": 12}}
    busy = plan.acquire_seconds(ser, P.load(), plan.acq_range(ser, P.load(), 532))
    assert not plan.hold_shutter(ser, P.load(), 532)            # 12 s apart: the gap >= 5 s: LabSpec's own shutter
    # 6 s apart with 1 s exposures leaves less than 5 s once LabSpec's overheads count (2026-11-24): held
    assert plan.hold_shutter(dict(ser, series={"count": 3, "interval_s": busy + 4}), P.load(), 532)
    fast = dict(ser, series={"count": 3, "interval_s": 2})
    assert plan.hold_shutter(fast, P.load(), 532)               # 1 s apart: held
    assert plan.hold_shutter(dict(ser, acquire=dict(ser["acquire"], shutter="hold")), P.load(), 532)
    e = plan.estimate(ser, P.load())["seconds"]
    assert e["series_waits"] > 0
    prof["lasers"][lk].update(hold_shutter=False, settle_s=0)
    P.save(prof)
    st = run_inline({"s": dict(ser, series={"count": 2, "interval_s": 1})})
    s = st["studies"]["s"]
    assert all(p_.get("native", "").endswith(".l6s") and Path(p_["native"]).exists() for p_ in s["spectra"])


def test_grating_move_must_arrive():
    """2026-11-23: a move 1200 -> 2400 read 1800 for 1.5 s, was taken as settled, and the study ran on the 1800. The
    grating is now read until it is the one asked, moved once more if not, and the study stops if it still is not."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    sp = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=2400),
          "acquire": {"center_cm1": 520, "exposure_s": 1}}
    (bdir / "sim_grating_stuck_once").write_text("2400:1800")
    st = run_inline({"s": sp})
    s = st["studies"]["s"]
    moves = [m for m in s["setup"]["moves"] if m["motor"] == "Grating"]
    assert s["reliable"] and moves[-1].get("retry") and moves[-1]["ok"] and s["instrument"]["grating"] == 2400
    assert s["checks"]["setup"]["pass"]           # the retry arrived (2026-11-24: pass false although it did)
    (bdir / "sim_grating_stuck").write_text("2400:1800")
    api.instrument("set", setup={"grating": 1200})
    st = run_inline({"s": sp})
    assert not st["studies"]["s"]["ok"] and "did not reach 2400" in st["studies"]["s"]["why"]
    assert runner.move_extra(P.load(), "Grating").get("until_target") == 1


def test_grating_refused_after_785_is_retried_after_reselecting_the_laser():
    """2026-11-23: after a scripted visit to 785 nm, LabSpec took 1800 for every move to 2400, also once back on
    637.75 nm. ls6t selects the laser once more and moves the grating again (in set and in a study's set-up)."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_laser_locks_grating").write_text("1")
    for setup in ({"grating": 2400}, {"laser_nm": 785}, {"grating": 1200}, {"laser_nm": 532}):
        api.instrument("set", setup=setup)
    r = api.instrument("set", setup={"grating": 2400})
    g = [d for d in r["moves"] if d["set"] == "grating"][0]
    tried = g["retried_after_laser_reselect"]
    assert g["ok"] and tried[0]["reselect"]["laser_nm"] == 532 and tried[0]["ok"] and len(tried) == 1
    api.instrument("set", setup={"laser_nm": 785})
    api.instrument("set", setup={"grating": 1200, "laser_nm": 532})
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=2400),
                           "acquire": {"center_cm1": 520, "exposure_s": 1}}})
    assert st["studies"]["s"]["instrument"]["grating"] == 2400

    # LabSpec keeps refusing (only a bridge restart helps): the laser again, then through another laser and back,
    # then the study stops with what to do
    (bdir / "sim_laser_locks_grating").write_text("never")
    api.instrument("set", setup={"laser_nm": 785})
    api.instrument("set", setup={"grating": 1200, "laser_nm": 532})
    r = api.instrument("set", setup={"grating": 2400})
    g = [d for d in r["moves"] if d["set"] == "grating"][0]
    tried = g["retried_after_laser_reselect"]
    assert not g["ok"] and len(tried) == 2 and tried[1]["reselect"]["via"] and "restart" in g["error"]
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=2400),
                           "acquire": {"center_cm1": 520, "exposure_s": 1}}})
    assert not st["studies"]["s"]["ok"] and "end-bridge" in st["studies"]["s"]["why"]


def test_calibration_window_scales_with_the_grating_and_frame_is_optional():
    """2026-11-23: with the 600 gr/mm the Si line (FWHM 13) sat at the edge of the 30 cm-1 window and the calibration
    was not trusted; and each calibration of a batch took a video frame first (17-20 s)."""
    assert runner.calibration_window({}, 1800, 1) == 30 and runner.calibration_window({}, 600, 1) == 60
    assert runner.calibration_window({}, 300, 1) == 90
    assert runner.calibration_window({"calibration": {"window_cm1": 45}}, 600, 1) == 45
    assert not F.validate({"c": {"kind": "calibration", "setup": SETUP, "calibration": {"window_cm1": 1}}})["c"]["ok"]
    S.ensure(config.active_bridge_dir())
    P.save(dict(P.load(), calibration=dict(P.load()["calibration"], video_before=False)))
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600),
                           "acquire": {"exposure_s": 1}}})
    c = st["studies"]["c"]
    assert "video" not in c or not c.get("video")
    assert c["calibration"]["trusted"] and c["calibration"]["lines"][0]["measured_cm1"] is not None


def test_live_map_view_uses_mapex_like_horiba():
    """2026-11-23: LabSpec's Map() returned E_FAIL for the live view (its From / To were 0 - 0). A map of known size
    now goes through MapEx, as HORIBA's map macro does (index = iy * SizeX + ix)."""
    sim = S.ensure(config.active_bridge_dir())
    mp = {"m": {"kind": "map", "sample": {"name": "Si"}, "setup": SETUP,
                "acquire": {"center_cm1": 520, "exposure_s": 0.2},
                "map": {"x_um": [0, 4, 2], "y_um": [0, 2, 2], "engine": "points"}}}
    # off when the profile says so (it was off by default after the 2026-11-24 crash; on again from .24.8)
    P.save(dict(P.load(), display={"live_view": False}))
    sim.views.clear()
    run_inline(mp)
    assert "m" not in sim.views
    P.save(dict(P.load(), display={"live_view": True}))
    st = run_inline(mp)
    m = st["studies"]["m"]
    assert not any("live view" in w for w in m["warnings"]), m["warnings"]
    v = sim.views["m"]
    assert v["kind"] == "mapex" and v["axis_x"] == "0,2,4" and v["axis_y"] == "0,2"
    assert sorted(int(i) for i in v["index"]) == list(range(6))
    text = api.compose_vbs(config.active_bridge_dir(), config.home())["ls6t_bridge.vbs"]
    assert "LabSpec.MapEx(LS_CREATE_MAP, 0, id" in text and "LabSpec.MapEx(LS_ADD_TO_MAP, arr(2), id" in text
    # the axes as declared arrays (a Variant holding an array crashed LabSpec in MapEx, 2026-11-24), units "um"
    assert text.count("axA, ayA, labels, units") == 1 and text.count("axA, ayA, _") == 1
    assert 'units(0) = "um"' in text and '"mkm"' not in text.split("Sub VView")[1].split("End Sub")[0]


def test_laser_may_be_on_when_the_bridge_stops_during_a_hold(tmp_path):
    """2026-11-24: LabSpec froze and crashed with the shutter held; ls6t must say the laser may still reach the
    sample. The heartbeat carries shutter_held; diagnose and a request's stop say it first; a display call is never
    made with the shutter held (the bridge lets it go first)."""
    from labspec_translator import bridge as BR
    msg = BR.diagnose({"state": "busy", "verb": "view", "age_s": 300, "shutter_held": "1"}, tmp_path)
    assert msg.startswith("THE LASER MAY STILL REACH THE SAMPLE")
    assert not BR.diagnose({"state": "busy", "verb": "view", "age_s": 300, "shutter_held": "0"}, tmp_path).startswith("THE LASER")
    # the live view keeps the hold (its crash was MapEx's axes, 2026-11-24); a snapshot (video) still releases it
    assert "view" in runner.HOLD_VERBS and "video" not in runner.HOLD_VERBS
    text = api.compose_vbs(config.active_bridge_dir(), config.home())["ls6t_bridge.vbs"]
    hv = [l for l in text.splitlines() if l.startswith("Const HOLD_VERBS")][0]
    assert ",view," in hv and ",video," not in hv and 'ts.WriteLine "shutter_held=" & Bit(gHoldOn)' in text
    assert set(runner.HOLD_VERBS) == set(hv.split('"')[1].strip(",").split(","))
    ctx = runner.Ctx(tmp_path, BR.Bridge(config.active_bridge_dir()), P.load())
    ctx.holding = True
    assert ctx._held_warning("no answer").startswith("THE LASER MAY STILL REACH THE SAMPLE")


def test_series_spacing_runner_log_and_laser_defaults():
    """2026-11-24: series spacing reported start to start; every bridge call written to runner.log at once (an empty
    log after a LabSpec crash); the 637.75 nm laser's wavelength drifts, so it can default to laser_reference
    'correct' and give its calibrations a shorter life."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    st = run_inline({"s": {"kind": "series", "sample": {"name": "Si"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520, "exposure_s": 1}, "series": {"count": 3, "interval_s": 1.5}}})
    s = st["studies"]["s"]
    sp = s["series"]["spacing"]
    assert sp["asked_s"] == 1.5 and sp["mean_s"] == pytest.approx(1.5, abs=0.2) and len(sp["start_to_start_s"]) == 2
    log = (Path(st["request"]) / "runner.log").read_text()
    assert "-> acquire" in log and "<- acquire ok=1" in log
    prof = P.load()
    lk = next(k for k, v in prof["lasers"].items() if abs(v["nm"] - 532) < 1)
    prof["lasers"][lk].update(laser_reference="correct", calibration_max_age_h=2)
    P.save(prof)
    (bdir / "sim_laser_line").write_text("1")
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600),
                           "acquire": {"center_cm1": 250, "exposure_s": 1}}})
    assert st["studies"]["s"]["checks"]["laser_line"]["mode"] == "correct"
    prof["lasers"][lk].update(laser_reference="report", calibration_max_age_h=None)
    P.save(prof)


def test_live_view_never_cycles_the_held_shutter(tmp_path):
    """2026-11-24 on .24.3: with the live view on, every view call released the shutter hold and the next point held
    it again, so the 637.75 nm laser hopped at every point. With the hold, the views are now made after the last
    spectrum (the hold released first); a live map acquires without AUTO_SHOW so the map stays in front."""
    _profile_with_shutter(tmp_path)
    P.save(dict(P.load(), display={"live_view": True}))
    sim = S.ensure(config.active_bridge_dir())
    sim.views.clear()
    mp = {"m": {"kind": "map", "sample": {"name": "Si"}, "setup": SETUP,
                "acquire": {"center_cm1": 520, "exposure_s": 0.2, "shutter": "hold"},
                "map": {"x_um": [0, 4, 2], "y_um": [0, 4, 2], "engine": "points"}}}
    st = run_inline(mp)
    m = st["studies"]["m"]
    assert m["shutter_hold"]["times_held"] == 1 and m["shutter_hold"]["released"], m["shutter_hold"]
    assert sim.views["m"]["n"] == 9 and not any("held" in w for w in m["warnings"])
    log = (Path(st["request"]) / "runner.log").read_text().splitlines()
    views = [l for l in log if "-> view action=create" in l or "-> view action=add" in l]
    assert len(views) == 9 and all("[shutter held]" in l for l in views)      # the map grows while held
    # display.live_view_while_held false: the views after the last spectrum, the hold released first
    P.save(dict(P.load(), display={"live_view": True, "live_view_while_held": False}))
    sim.views.clear()
    st = run_inline(mp)
    log = (Path(st["request"]) / "runner.log").read_text().splitlines()
    views = [l for l in log if "-> view action=create" in l or "-> view action=add" in l]
    assert sim.views["m"]["n"] == 9 and all("[shutter held]" not in l for l in views)
    assert st["studies"]["m"]["shutter_hold"]["times_held"] == 1
    P.save(dict(P.load(), display={"live_view": True}))
    # without the hold the map grows as measured
    sim.views.clear()
    mp["m"]["acquire"]["shutter"] = "auto"
    st = run_inline(mp)
    assert sim.views["m"]["n"] == 9 and "shutter_hold" not in st["studies"]["m"]


def test_correction_kept_per_window_centre():
    """2026-11-24: two Si calibrations a minute apart in two windows gave -0.72 and -2.40 cm-1 and each overwrote the
    other. Corrections are kept per window centre; a study gets the one for its centre, interpolated between two,
    or the nearest with a warning."""
    from labspec_translator import xcal
    xcal.clear()
    xcal.store(637.75, 1200, 0.72, 1.0, "si", [], centre_nm=661.0)
    xcal.store(637.75, 1200, 2.40, 1.0, "si", [], centre_nm=652.0)
    assert xcal.get(637.75, 1200, 661.3)["a"] == 0.72 and xcal.get(637.75, 1200, 652.0)["a"] == 2.40
    mid = xcal.get(637.75, 1200, 656.5)
    assert mid["a"] == pytest.approx(1.56, abs=0.01) and mid["interpolated_between_nm"] == [652.0, 661.0]
    far = xcal.get(637.75, 1200, 670.0)
    assert far["centre_far"] and far["a"] == 0.72
    assert xcal.get(637.75, 1200)["a"] == 2.40            # no centre: the last measured
    xcal.clear()
    S.ensure(config.active_bridge_dir())
    (config.active_bridge_dir() / "sim_xshift").write_text("1.5,1.0")
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                           "calibration": {"adjust": "ls6t"}},
                     "s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                           "acquire": {"center_cm1": 900}}})
    assert any("window centred at" in w and "zero differs between windows" in w for w in st["studies"]["s"]["warnings"])
    assert list(xcal.load().values())[0]["by_centre"]


def test_laser_line_and_stored_correction_do_not_count_the_zero_twice():
    """2026-11-24 (review): with laser_reference 'correct' and a stored correction, each spectrum is brought back to
    the laser line of the correction's calibration, then the correction applies (the stored offset relative to the
    laser line); the result says which was done (checks.zero_correction)."""
    from labspec_translator import xcal
    xcal.clear()
    bdir = config.active_bridge_dir()
    sim = S.ensure(bdir)
    (bdir / "sim_laser_line").write_text("1")
    (bdir / "sim_xshift").write_text("1.5,1.0")               # the scale reads 1.5 cm-1 high, laser line included
    setup = dict(SETUP, grating=600)
    acq = {"center_cm1": 250, "exposure_s": 1}
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": setup, "acquire": acq,
                           "calibration": {"adjust": "ls6t", "store": "per_centre"}}})
    stored = st["studies"]["c"]["calibration"]["stored_correction"]
    assert stored["laser_line_cm1"] == pytest.approx(1.5, abs=0.3) and stored["centre_nm"]
    # then the laser moves by 8.7 cm-1 (sim_shift_after_first replaces the scale's error for later spectra)
    (bdir / "sim_xshift").unlink()
    (bdir / "sim_shift_after_first").write_text("1")
    sim._shift_armed = True
    sp = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": setup,
          "acquire": dict(acq, laser_reference="correct"),
          "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    st = run_inline({"s": sp})
    s = st["studies"]["s"]
    zc = s["checks"]["zero_correction"]
    assert "relative to the laser line" in zc["method"] and zc["stored"]["laser_line_cm1"] == stored["laser_line_cm1"]
    assert s["peaks_by_name"]["Si"]["position_cm1"] == pytest.approx(520.7, abs=0.6)
    # a hop over 3 cm-1 was followed, but the zero there is flagged uncertain (2026-11-24: about 1 cm-1 left)
    assert zc["uncertain_points"] == [0] and "a hop" in s["spectra"][0]["zero_uncertain"]
    assert any("zero uncertain" in w for w in s["warnings"])
    # report mode: the stored correction only
    st = run_inline({"s": dict(sp, acquire=acq)})
    assert st["studies"]["s"]["checks"]["zero_correction"]["method"].startswith("the stored correction only")
    (bdir / "sim_shift_after_first").unlink()
    xcal.clear()


def test_shutter_hold_kept_between_studies_with_the_same_laser(tmp_path):
    """2026-11-24: each study took and released its own hold, and the 637.75 nm laser hopped when the shutter closed
    for 5-12 s between studies. The hold is kept into the next study when it holds too, with the same laser; released
    at the end, at a laser change, or at a stop."""
    _profile_with_shutter(tmp_path)
    sim = S.ensure(config.active_bridge_dir())
    sim.hold_count = 0
    one = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
           "acquire": {"center_cm1": 520, "exposure_s": 0.2, "shutter": "hold"}, "points": [{}, {}]}
    other = dict(one, setup=dict(SETUP, laser_nm=785))
    st = run_inline({"a": one, "b": one, "c": other})
    a, b, c = (st["studies"][k]["shutter_hold"] for k in "abc")
    assert a["kept_for_next_study"] and a["released"] == "kept for the next study (same laser)"
    assert b["continued_from_previous_study"] and not b["kept_for_next_study"] and b["released"] is True
    assert c["times_held"] == 1 and not c["continued_from_previous_study"]
    assert sim.hold_count == 2 and not sim.held                 # one hold for a + b, one for c; none left


def test_safety_commands_stay_queued_and_native_map_announces_blocking(tmp_path):
    """2026-11-24: during a native map LabSpec held the bridge; ls6t called it gone after 20 s and its shutter
    release was withdrawn, so the shutter stayed open. A safety command (shutter_hold off, laser off, safe) now stays
    queued for the bridge; the native map announces how long it may block; the bridge releases a hold by itself when
    no command came for idle_s."""
    from labspec_translator.bridge import Bridge, BridgeTimeout
    d = tmp_path / "bridge"
    (d / "inbox").mkdir(parents=True)
    (d / "outbox").mkdir()
    (d / "heartbeat.txt").write_text("state=busy\nverb=map_native\nblocking=map_native\nblocking_allowance_s=600\n")
    b = Bridge(d)
    with pytest.raises(BridgeTimeout, match="left queued"):
        b.call("shutter_hold", timeout_s=0.3, state="off")
    assert len(list((d / "inbox").glob("*.cmd"))) == 1
    with pytest.raises(BridgeTimeout):
        b.call("status", timeout_s=0.3)
    assert len(list((d / "inbox").glob("*.cmd"))) == 1            # an ordinary command is withdrawn
    text = api.compose_vbs(config.active_bridge_dir(), config.home())["ls6t_bridge.vbs"]
    nm = text.split("Sub VMapNative")[1].split("End Sub")[0]
    assert 'gBlocking = "map_native"' in nm and "gBlockingS = tmo" in nm
    assert 'HoldRelease "no command for " & gHoldIdleS & " s"' in text and "gLastCmdEnd = Timer" in text


def test_focus_scan_judges_noise_and_says_where_z_was_left():
    """2026-11-24: about 25 counts per step (20 % noise) and a focus was still chosen. Each step now gives its counts
    and SNR, a change with Z under 4 x one step's noise is no focus (Z back to the start), few counts are warned with
    the exposure to use, and the result says where Z was left."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_defocus").write_text("2.3")
    st = run_inline({"f": {"kind": "focus", "sample": {"name": "Si"}, "setup": SETUP}})
    f = st["studies"]["f"]["focus"]
    assert f["found"] and f["counts"] and f["snr"][0] and "focus found" in f["z_left_at"]
    weak = {"kind": "focus", "sample": {"name": "Si"}, "setup": dict(SETUP, filter="0.1%"),
            "focus": {"scan": {"exposure_s": 0.02}}}
    st = run_inline({"f": weak})
    r = st["studies"]["f"]
    assert not r["focus"]["found"] and "starting Z" in r["focus"]["z_left_at"]
    assert any("counts per step" in w for w in r["warnings"])
    text = api.compose_vbs(bdir, config.home())["ls6t_bridge.vbs"]
    nm = text.split("Sub VMapNative")[1].split("End Sub")[0]
    assert "LabSpec.Acq mode, tval, 0, 0, 0" in nm and "LabSpec.GetMappingParams mname" in nm


def test_correct_follows_the_laser_lines_steps_not_its_slow_drift():
    """2026-11-24: 'correct' followed every spectrum's laser line, and a slow creep of the line (more than the Raman
    lines moved) dragged Si from 520.52 to 520.00 in a series. 'correct' now follows plateaus (a hop starts a new
    one); 'correct_each' keeps following every spectrum."""
    bdir = config.active_bridge_dir()
    sim = S.ensure(bdir)
    (bdir / "sim_laser_line").write_text("1")
    (bdir / "sim_laser_line_drift").write_text("0.25")
    st0 = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600),
           "acquire": {"center_cm1": 250, "exposure_s": 1, "laser_reference": "correct"}, "points": [{}] * 6,
           "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    sim._ll_drift_n = -1
    s = run_inline({"s": st0})["studies"]["s"]
    pos = [p["peaks"][0]["position_cm1"] for p in s["spectra"]]
    zc = s["checks"]["zero_correction"]
    assert len(zc["plateaus"]) == 1 and zc["plateaus"][0]["drift_cm1"] == pytest.approx(1.25, abs=0.4), zc
    assert max(pos) - min(pos) < 0.5, pos                              # not dragged by the creep
    sim._ll_drift_n = -1
    s = run_inline({"s": dict(st0, acquire=dict(st0["acquire"], laser_reference="correct_each"))})["studies"]["s"]
    pos_e = [p["peaks"][0]["position_cm1"] for p in s["spectra"]]
    assert pos_e[0] - pos_e[-1] == pytest.approx(1.25, abs=0.4), pos_e     # the old way follows it
    assert "every spectrum" in s["checks"]["zero_correction"]["follows"]
    (bdir / "sim_laser_line_drift").unlink()
    # a hop still starts a new plateau and is corrected
    (bdir / "sim_shift_after_first").write_text("1")
    sim._shift_armed = False
    s = run_inline({"s": dict(st0, points=[{}] * 3)})["studies"]["s"]
    zc = s["checks"]["zero_correction"]
    assert [pl["points"] for pl in zc["plateaus"]] == [[0], [1, 2]], zc
    pos = [p["peaks"][0]["position_cm1"] for p in s["spectra"]]
    assert max(pos) - min(pos) < 1.0, pos
    (bdir / "sim_shift_after_first").unlink()


def test_hold_open_wait_native_map_hold_read_back_and_reopened_hold(tmp_path):
    """2026-11-24: the 637.75 nm laser's first held spectrum had a laser line 9.9 cm-1 wide: the hold waits after
    opening the shutter (lasers.<k>.hold_open_s; by default settle_s, or 5 s for a laser marked mode_hops). HORIBA's
    mapping engine may set the shutter mode itself: the native map's result says whether the hold was still in place,
    as LabSpec read it. A hold the bridge let go between studies is reported as opened again."""
    _profile_with_shutter(tmp_path)
    prof = P.load()
    lk = next(k for k, v in prof["lasers"].items() if abs(v["nm"] - 532) < 1)
    assert plan.hold_open_s(prof, 532) == 0.0
    prof["lasers"][lk].update(mode_hops=True)
    P.save(prof)
    assert plan.hold_open_s(P.load(), 532) == plan.HOLD_OPEN_DEFAULT_S
    prof["lasers"][lk].update(settle_s=3.5)
    P.save(prof)
    assert plan.hold_open_s(P.load(), 532) == 3.5
    sim = S.ensure(config.active_bridge_dir())
    sim.hold_open_waits = 0
    one = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
           "acquire": {"center_cm1": 520, "exposure_s": 0.2, "shutter": "hold"}, "points": [{}, {}]}
    e0 = plan.estimate(dict(one, acquire=dict(one["acquire"], shutter="auto")), P.load())["seconds"]["total"]
    e1 = plan.estimate(one, P.load())["seconds"]["total"]
    assert e1 == pytest.approx(e0 - 3.5 + 3.5, abs=0.5)          # one settle_s less between spectra, one wait more
    st = run_inline({"a": one, "b": one})
    a, b = st["studies"]["a"]["shutter_hold"], st["studies"]["b"]["shutter_hold"]
    assert a["open_wait_s"] == 3.5 and b["continued_from_previous_study"] and sim.hold_open_waits == 1
    # native map: the hold read back from LabSpec after the map
    nat = {"kind": "map", "sample": {"name": "x"}, "setup": SETUP,
           "acquire": {"center_cm1": 520, "exposure_s": 0.2, "shutter": "hold"},
           "map": {"x_um": [0, 2, 1], "y_um": [0, 0, 1], "engine": "native"}}
    st = run_inline({"m": nat})
    m = st["studies"]["m"]
    assert m["shutter_hold"]["after_native_map"]["held"] is True, m.get("shutter_hold")
    bdir = config.active_bridge_dir()
    (bdir / "sim_native_map_drops_hold").write_text("1")
    st = run_inline({"m": nat, "s": one})
    m = st["studies"]["m"]
    assert m["shutter_hold"]["after_native_map"]["held"] is False
    assert any("not in place at the end of LabSpec's map" in w for w in m["warnings"])
    assert not m["shutter_hold"]["kept_for_next_study"]           # released, held again by the next study
    (bdir / "sim_native_map_drops_hold").unlink()
    # the bridge let the hold go between studies (no command for idle_s): the next study says it was opened again
    orig = sim.v_shutter_hold

    def drop_then_hold(p):
        if p.get("state") != "off" and getattr(sim, "held", False) and getattr(sim, "_drop_once", False):
            sim._drop_once = False
            sim.held = False
            sim.hold_last_why = "no command for 60 s"
        return orig(p)
    sim.v_shutter_hold = drop_then_hold
    sim._drop_once = False
    try:
        import labspec_translator.runner as R
        real_run_kind = R._run_kind

        def arm_after_a(name, study, ctx):
            out = real_run_kind(name, study, ctx)
            if name == "a":
                sim._drop_once = True
            return out
        R._run_kind = arm_after_a
        try:
            st = run_inline({"a": one, "b": one})
        finally:
            R._run_kind = real_run_kind
    finally:
        sim.v_shutter_hold = orig
    b = st["studies"]["b"]
    assert b["shutter_hold"].get("reopened_after_bridge_release"), b["shutter_hold"]
    assert any("released by the bridge (no command for 60 s)" in w for w in b["warnings"])
    prof = P.load()
    prof["lasers"][lk].update(mode_hops=False, settle_s=0)
    P.save(prof)


def test_range_uses_the_spectrometers_real_centre():
    """2026-11-24: for a range (-100..700 cm-1) LabSpec's Acq placed the window itself (676.75 nm read after it), while
    ls6t keyed the per-window correction by the range's centre (650.63). The spectrometer's reading after the data is
    the centre used to store and look up corrections."""
    from labspec_translator import xcal
    xcal.clear()
    setup = dict(SETUP, grating=1200)
    acq = {"range_cm1": [300, 700], "exposure_s": 1, "range_placement": "labspec"}
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": setup, "acquire": acq,
                           "calibration": {"adjust": "ls6t"}}})
    cal = st["studies"]["c"]["calibration"]
    rng = plan.acq_range({"kind": "calibration", "setup": setup, "acquire": acq}, P.load(), 532)
    win = 46800.0 / 1200
    assert cal["applies_to"]["window_centre_nm"] == pytest.approx(rng["from_nm"] + win / 2, abs=0.01), cal["applies_to"]
    assert "reading" in cal["applies_to"]["window_centre_from"]
    assert cal["stored_correction"]["centre_nm"] == pytest.approx(rng["from_nm"] + win / 2, abs=0.01)
    sp = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": setup, "acquire": acq,
          "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    s = run_inline({"s": sp})["studies"]["s"]
    wc = s["checks"]["window_centre"]
    assert wc["nm"] == pytest.approx(rng["from_nm"] + win / 2, abs=0.01) and wc["range_centre_nm"] != wc["nm"]
    assert not any("window centred at" in w and "away" in w for w in s["warnings"])     # the same window: no far
    # by default a range within one window is acquired centred on it and cut to the range (2026-11-24: the 2 cm-1
    # was only in LabSpec-placed ranges)
    acq_c = {"range_cm1": [300, 700], "exposure_s": 1}
    rc = plan.acq_range({"kind": "spectrum", "setup": setup, "acquire": acq_c}, P.load(), 532)
    assert rc["single"] and rc["from_nm"] == rc["to_nm"] == rc["center_nm"] and rc["crop_cm1"] == [300.0, 700.0]
    s = run_inline({"s": dict(sp, acquire=acq_c)})["studies"]["s"]
    assert "window_centre" not in s["checks"] and s["checks"]["range"]["pass"]
    assert s["spectra"][0]["from_cm1"] >= 299 and s["spectra"][0]["to_cm1"] <= 701
    assert plan.acq_range({"kind": "spectrum", "setup": setup, "acquire": {"range_cm1": [300, 3000]}},
                          P.load(), 532)["single"] is False                                   # wider: LabSpec stitches
    xcal.clear()


def test_weak_points_kept_out_bad_laser_line_fits_and_native_readbacks():
    """2026-11-24, native map: point 0 was taken before the beam was on (6 counts against 150, laser line at +1.5,
    Si 538.8) and entered the summary; the laser line 1.6 cm-1 from its calibration position was called a hop at
    every later point; the axis readbacks were missing. At 600 gr/mm the laser line's fit jumped +3.6 / +5.9 while Si
    stayed: a hop is now confirmed by the Raman peaks, and a laser-line step they do not follow is a bad fit."""
    x = np.linspace(-200, 800, 800)
    nm = 1e7 / (1e7 / 532.0 - x)

    def rec(k, scale):
        y = 300 + scale * (100 * np.exp(-0.5 * (x / 3) ** 2) + 50 * np.exp(-0.5 * ((x - 520.7) / 3) ** 2))
        return {"point": k, "_sp": {"x": nm, "y": y, "meta": {"x_unit": "nm"}}}
    pts = [rec(0, 0.04)] + [rec(k, 1.0) for k in range(1, 5)]
    ex = runner.screen_points(pts, 532.0, "map")
    assert list(ex) == [0] and "laser line" in ex[0]           # the leak through the edge filter: the beam was off
    x = np.linspace(200, 800, 600)                               # no laser line in the window
    nm = 1e7 / (1e7 / 532.0 - x)
    no_ll = [rec(0, 0.04)] + [rec(k, 1.0) for k in range(1, 5)]
    w_: list = []
    assert runner.screen_points(no_ll, 532.0, "map", w_) == {} and "off the sample" in w_[0]   # a map: only said
    assert list(runner.screen_points(no_ll, 532.0, "series")) == [0]                          # one place: out
    pts[0]["excluded"] = ex[0]
    pts[0]["peaks"] = [{"name": "Si", "ok": True, "position_cm1": 538.8}]
    for r in pts[1:]:
        r["peaks"] = [{"name": "Si", "ok": True, "position_cm1": 521.4}]
    q = runner._quantities(pts, {})
    assert q["Si"]["position_cm1"][0] is None and q["Si"]["position_cm1"][1] == 521.4
    # the laser line alone stepping (a bad fit): no hop, the series stays reliable
    bdir = config.active_bridge_dir()
    sim = S.ensure(bdir)
    (bdir / "sim_laser_line").write_text("1")
    (bdir / "sim_laser_line_drift").write_text("2.5")
    sim._ll_drift_n = -1
    st0 = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600),
           "acquire": {"center_cm1": 250, "exposure_s": 1, "laser_reference": "correct"}, "points": [{}] * 3,
           "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    s = run_inline({"s": st0})["studies"]["s"]
    assert not s["checks"]["laser_line"]["jumps"] and set(s["checks"]["laser_line_bad_fits"]) == {"1", "2"}, \
        s["checks"]["laser_line"]
    pos = [p["peaks"][0]["position_cm1"] for p in s["spectra"]]
    assert max(pos) - min(pos) < 0.5 and not any("mode hop" in u for u in s["unreliable_because"])
    # (2026-11-24: "no laser line was found in spectra :" with an empty list when the later fits were bad)
    assert not any("no laser line was found" in u for u in s["unreliable_because"])
    (bdir / "sim_laser_line_drift").unlink()
    # a native map gives LabSpec's axis readbacks
    nat = {"kind": "map", "sample": {"name": "x"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 0.2},
           "map": {"x_um": [0, 2, 1], "y_um": [0, 0, 1], "engine": "native"}}
    m = run_inline({"m": nat})["studies"]["m"]
    assert "x_readback" in m["native_map"] and "y_readback" in m["native_map"] and m["native_map"]["seconds_map"]


def test_correct_applies_only_real_hops_and_rejects_a_broad_calibration_line():
    """2026-11-24: three cases where following a small laser-line offset made Si worse (1200 interpolation +0.73,
    a 1200 series, 600 gr/mm -1.28 -> Si 519.18 instead of 520.46). 'correct' now applies a plateau's step only over
    3 cm-1 and only when the Raman peaks moved with it; each plateau says what was not applied. A calibration whose
    laser line is over twice as wide as its reference line keeps no laser line."""
    from labspec_translator import xcal
    assert runner.laser_line_too_broad(19.4, [{"fwhm_cm1": 6.5, "measured_cm1": 532.4}])
    assert runner.laser_line_too_broad(8.1, [{"fwhm_cm1": 6.0, "measured_cm1": 520.7}]) is None
    xcal.clear()
    bdir = config.active_bridge_dir()
    sim = S.ensure(bdir)
    (bdir / "sim_laser_line").write_text("1")
    (bdir / "sim_xshift").write_text("1.5,1.0")
    setup = dict(SETUP, grating=600)
    acq = {"center_cm1": 250, "exposure_s": 1}
    st = run_inline({"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": setup, "acquire": acq,
                           "calibration": {"adjust": "ls6t"}}})
    assert st["studies"]["c"]["calibration"]["stored_correction"]["laser_line_cm1"] == pytest.approx(1.5, abs=0.3)
    sp = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": setup, "points": [{}] * 2,
          "acquire": dict(acq, laser_reference="correct"),
          "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 30}]}}
    for step in (2.0, 5.0):                       # the laser line alone moves: under 3, then not followed by Si
        (bdir / "sim_laser_line_drift").write_text(str(step))
        sim._ll_drift_n_fixed = True              # every spectrum here: the line `step` from the calibration's
        s = run_inline({"s": sp})["studies"]["s"]
        pl = s["checks"]["zero_correction"]["plateaus"]
        assert pl and not any(p["applied"] for p in pl), pl
        assert "not_applied_because" in pl[0]
        assert s["peaks_by_name"]["Si"]["position_cm1"] == pytest.approx(520.7, abs=0.6), step
    sim._ll_drift_n_fixed = False
    (bdir / "sim_laser_line_drift").unlink()
    (bdir / "sim_xshift").unlink()
    xcal.clear()


def _set_z_direction(sign="+"):
    prof = P.load()
    P.save(dict(prof, stage=dict(prof["stage"], z_into_sample=sign)))


def test_objective_clearance_measured_by_hand_limits_moves_toward_the_sample():
    """Nicola, 2026-11-24: a manual calibration of each objective's closest safe distance to a flat sample, against a
    crash (especially the x100). The operator focuses, moves Z by hand toward the sample to the closest safe gap;
    ls6t stores focus -> closest per objective and never goes past half of it (nor past its working-distance
    limit); Z goes back to the focus."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    plan.clearance_store({})
    r = api.instrument("clearance")
    assert not r["ok"] and "z_into_sample" in r["error"]
    _set_z_direction("+")
    obj = api.instrument("status")["status"]["objective"]
    z0 = float(api.instrument("status")["status"]["z_um"])
    wd_lim = plan.z_limits(P.load(), obj)["toward_um"]
    (bdir / "sim_operator_dz").write_text("-5")              # moved away: refused, nothing stored
    r = api.instrument("clearance")
    assert not r["ok"] and "did not move toward" in r["error"] and plan.clearance_um(obj) is None
    (bdir / "sim_operator_dz").write_text(str(wd_lim))        # half of it is tighter than the WD limit
    r = api.instrument("clearance")
    assert r["ok"] and r["clearance_um"] == pytest.approx(wd_lim, abs=0.01), r
    assert r["z_back_um"] == pytest.approx(z0, abs=0.01)
    zl = plan.z_limits(P.load(), obj)
    assert zl["toward_um"] == pytest.approx(wd_lim / 2, abs=0.1) and zl["toward_from"] == "measured clearance"
    v = F.validate({"m": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, objective=obj),
                          "acquire": {"center_cm1": 520, "exposure_s": 1}, "points": [{"dz_um": wd_lim * 0.75}]}})["m"]
    assert not v["ok"]                                        # 3/4 of the old limit is now past the clearance
    (bdir / "sim_operator_dz").unlink()
    plan.clearance_store({})


def test_camera_autofocus_on_features_and_refused_on_a_featureless_view():
    """Nicola, 2026-11-24: autofocus with the live camera on a feature; rejected when there are no discernible
    features. ls6t's focus.autofocus 'camera': top camera, white light, no laser; coarse then fine steps on the
    frame's sharpness, within the objective's limits."""
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    _set_z_direction("+")
    (bdir / "sim_defocus").write_text("3.0")
    z0 = float(api.instrument("status")["status"]["z_um"])
    st = {"kind": "focus", "sample": {"name": "Si"}, "setup": SETUP, "focus": {"autofocus": "camera"}}
    assert F.validate({"f": st})["f"]["ok"]
    r = run_inline({"f": st})["studies"]["f"]
    f = r["focus"]
    assert f["method"] == "camera" and f["found"], f.get("why")
    assert f["z_um"] == pytest.approx(z0 + 3.0, abs=0.6) and "focus found" in f["z_left_at"]
    assert f["texture_at_best"] >= runner.CAM_MIN_TEXTURE and r["reliable"]
    # a featureless view: refused, Z back where it started
    (bdir / "sim_featureless").write_text("1")
    r = run_inline({"f": st})["studies"]["f"]
    f = r["focus"]
    assert not f["found"] and "no discernible features" in f["why"] and "starting Z" in f["z_left_at"]
    assert not r["reliable"]
    (bdir / "sim_featureless").unlink()
    # in a map, before the first point and every N points
    mp = {"kind": "map", "sample": {"name": "Si"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 0.2},
          "map": {"x_um": [0, 2, 2]}, "focus": {"autofocus": "camera", "every": 1}}
    assert F.validate({"m": mp})["m"]["ok"]
    m = run_inline({"m": mp})["studies"]["m"]
    assert m["complete"] and m.get("focus_scans") and all(fs["method"] == "camera" for fs in m["focus_scans"]), \
        list(m)
    (bdir / "sim_defocus").unlink()
    assert not F.validate({"f": dict(st, focus={"autofocus": "camera", "camera": {"step_um": 0}})})["f"]["ok"]



def test_hold_opens_before_point_zero_reads_back_and_beam_went_off_wording(tmp_path):
    """2026-11-24 evening (.13): a held 30-point series had its first gap 8.3 s: the 5 s open wait ran inside point
    0's call, before LabSpec's Acq but after point 0's clock started. The hold (and its wait) now opens first. After
    every held spectrum the bridge reads ShutterMode and the shutter back (spectra[].shutter_hold_after); spectra that
    lose the light after earlier ones had it are kept out with "the beam went off"."""
    _profile_with_shutter(tmp_path)
    prof = P.load()
    lk = next(k for k, v in prof["lasers"].items() if abs(v["nm"] - 532) < 1)
    prof["lasers"][lk].update(hold_open_s=5)
    P.save(prof)
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_time_scale").write_text("0.2")              # the open wait: 1 s real
    try:
        ser = {"kind": "series", "sample": {"name": "Si"}, "setup": SETUP,
               "acquire": {"center_cm1": 520, "exposure_s": 0.2, "shutter": "hold"},
               "series": {"count": 3, "interval_s": 0.6}}
        s = run_inline({"s": ser})["studies"]["s"]
    finally:
        (bdir / "sim_time_scale").unlink()
    ts = [p["t_start"] for p in s["spectra"]]
    assert ts[1] - ts[0] < 0.85, ts                          # not the open wait + the first spectrum
    assert s["spectra"][0]["shutter_hold_after"]["held"] == "yes" and s["shutter_hold"]["open_wait_s"] == 5
    prof["lasers"][lk].update(hold_open_s=None)
    P.save(prof)
    # the beam lost after earlier spectra had it
    x = np.linspace(-200, 800, 800)
    nm = 1e7 / (1e7 / 532.0 - x)

    def rec(k, scale):
        y = 300 + scale * (100 * np.exp(-0.5 * (x / 3) ** 2) + 50 * np.exp(-0.5 * ((x - 520.7) / 3) ** 2))
        return {"point": k, "_sp": {"x": nm, "y": y, "meta": {"x_unit": "nm"}}}
    ex = runner.screen_points([rec(0, 1.0), rec(1, 1.0), rec(2, 0.03), rec(3, 0.03), rec(4, 0.03)], 532.0, "series")
    assert sorted(ex) == [2, 3, 4] and all("went off" in w for w in ex.values()), ex
    ex = runner.screen_points([rec(0, 0.03), rec(1, 1.0), rec(2, 1.0)], 532.0, "series")
    assert list(ex) == [0] and "not on yet" in ex[0]


def test_corrections_by_placement_one_centre_cleared_and_unconfirmed_hops():
    """2026-11-24: +2.09 stored for 676.75 nm was measured with LabSpec placing a range's window (data on the CCD
    edge): such a correction now serves only studies acquired the same way (range_placement 'labspec'), and one
    window's correction can be cleared (references clear correction 637.75@1200@676.75). A laser-line step with no
    Raman peak to confirm it is applied only over 5 cm-1, flagged unconfirmed."""
    from labspec_translator import xcal
    xcal.clear()
    xcal.store(637.75, 1200, -0.01, 1.0, "si", [], centre_nm=650.19, placement="centred")
    xcal.store(637.75, 1200, 2.09, 1.0, "si", [], centre_nm=676.75, placement="labspec")
    assert xcal.get(637.75, 1200, 676.0, "centred")["a"] == -0.01           # the centred one, far
    assert xcal.get(637.75, 1200, 676.0, "labspec")["a"] == 2.09
    assert xcal.get(637.75, 1200, None, "centred")["a"] == -0.01
    assert api.references_clear("correction", "637.75@1200@676.75")["cleared"] == 1
    assert list(xcal.load()["637.75@1200"]["by_centre"]) == ["650.19"]
    xcal.clear()
    # a hop without Raman peaks to confirm it: applied, flagged
    bdir = config.active_bridge_dir()
    sim = S.ensure(bdir)
    (bdir / "sim_laser_line").write_text("1")
    (bdir / "sim_shift_after_first").write_text("1")
    sim._shift_armed = False
    st = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600), "points": [{}] * 3,
          "acquire": {"center_cm1": 250, "exposure_s": 1, "laser_reference": "correct"}}
    s = run_inline({"s": st})["studies"]["s"]
    pl = s["checks"]["zero_correction"]["plateaus"]
    assert pl[1]["applied"] and pl[1].get("unconfirmed") and any("unconfirmed" in w for w in s["warnings"]), pl
    (bdir / "sim_shift_after_first").unlink()



def test_spectra_after_a_hop_that_are_not_single_mode_are_kept_out():
    """2026-11-24: after real hops (-7.8, -9.4 cm-1, confirmed by Si) the 637.75 nm laser was multimode: Si 6.1 ->
    13-26 cm-1 wide, counts 171 -> 51-63, corrected Si 521.4-525.0. Such spectra are kept out of the summaries and
    the study is unreliable, rather than 'corrected'."""
    bdir = config.active_bridge_dir()
    sim = S.ensure(bdir)
    (bdir / "sim_laser_line").write_text("1")
    (bdir / "sim_shift_after_first").write_text("1")
    (bdir / "sim_multimode_after_hop").write_text("1")
    sim._shift_armed = False
    st = {"kind": "series", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600),
          "acquire": {"center_cm1": 250, "exposure_s": 1, "laser_reference": "correct"},
          "series": {"count": 3, "interval_s": 0}, "process": {"peaks": [{"name": "Si", "near_cm1": 520.7, "window_cm1": 40}]}}
    s = run_inline({"s": st})["studies"]["s"]
    assert not s["reliable"] and any("not single-mode" in u for u in s["unreliable_because"]), s["unreliable_because"]
    assert set(s["checks"]["excluded_points"]) == {"1", "2"}
    for f in ("sim_shift_after_first", "sim_multimode_after_hop"):
        (bdir / f).unlink()


def test_first_exposure_unprotected_warned_and_spectrum_clock_from_acq():
    """2026-11-24 (.15): with the shutter cycled by LabSpec and settle_s 0, point 0 of the 637.75 nm laser was broad
    (Si 8.2 against 6.1 cm-1): warned at validation. Each spectrum's t_start is LabSpec's Acq, after the bridge's
    spectrometer move (a 26 s move counted as point 0's in the first gap)."""
    prof = P.load()
    lk = next(k for k, v in prof["lasers"].items() if abs(v["nm"] - 532) < 1)
    prof["lasers"][lk].update(mode_hops=True)
    P.save(prof)
    st = {"kind": "series", "sample": {"name": "Si"}, "setup": SETUP,
          "acquire": {"center_cm1": 520, "exposure_s": 1}, "series": {"count": 2, "interval_s": 0}}
    v = F.validate({"s": st})["s"]
    assert any("settles after its shutter opens" in w for w in v["warnings"]), v["warnings"]
    assert not any("settles after its shutter opens" in w for w in
                   F.validate({"s": dict(st, acquire=dict(st["acquire"], warmup_s=2))})["s"]["warnings"])
    prof["lasers"][lk].update(mode_hops=False)
    P.save(prof)
    text = api.compose_vbs(config.active_bridge_dir(), config.home())["ls6t_bridge.vbs"]
    acq = text.split('res("t_to_acq_s") = Secs(Elapsed(tAll))')
    assert len(acq) == 2 and acq[1].lstrip().startswith("LabSpec.Acq mode")


def test_calibration_repeats_pooled_at_coarse_gratings_and_run_order():
    """2026-11-24, 637.75 @ 600 gr/mm: seven calibrations in three windows within 3 min scattered as much within a
    window (+9.22 / +9.47 / +9.86) as between windows, Si spanning ~2 pixels at ~4.5 cm-1 per pixel. calibration.repeats
    N re-places the spectrometer before each spectrum and reports mean, SD and SEM; at a coarse grating (>= 2 cm-1 per
    pixel) one pooled correction is stored for every window. Studies run in the order given (order, when an app sorts
    the keys)."""
    from labspec_translator import xcal
    xcal.clear()
    st = {"kind": "calibration", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=600),
          "acquire": {"center_cm1": 520.7, "exposure_s": 1}, "calibration": {"adjust": "ls6t", "repeats": 3}}
    assert F.validate({"c": st})["c"]["ok"]
    c = run_inline({"c": st})["studies"]["c"]["calibration"]
    assert c["repeats"]["n"] == 3 and c["repeats"]["sd_cm1"] >= 0 and c["cm1_per_pixel"] >= 2.0, c.get("repeats")
    assert "one window measured so far" in c["stored_as"]
    xcal.clear()
    # pooled only when the centres differ by less than the repeats' scatter (Nicola, 2026-11-24)
    rep = lambda sd: {"n": 5, "sd_cm1": sd}
    for cen, a_ in ((659.66, -9.52), (650.19, -9.88), (681.0, -9.64)):          # 637.75 @ 600: noise
        r = xcal.store(637.75, 600, a_, 1.0, "si", [], centre_nm=cen, placement="centred", pool="auto",
                       repeats=rep(0.5), cm1_per_px=4.5)
    assert r["pooled"] and r["a"] == pytest.approx(-9.68, abs=0.01) and "within" in r["pool"]["why"]
    g = xcal.get(637.75, 600, 700.0, "centred")
    assert g["pooled"] and not g.get("centre_far") and "by_centre" in xcal.load()["637.75@600"]
    for cen, a_ in ((540.63, -4.05), (547.16, -2.18), (555.65, -1.80)):           # 532 @ 1200: real
        r = xcal.store(532, 1200, a_, 1.0, "si", [], centre_nm=cen, placement="centred", pool="auto",
                       repeats=rep(0.3), cm1_per_px=2.6)
    assert not r.get("pooled") and "real window dependence" in r["pool"]["why"]
    assert xcal.get(532, 1200, 540.63, "centred")["a"] == -4.05
    xcal.clear()
    for cen, a_ in ((540.63, -4.05), (547.16, -2.18)):                            # no repeats, coarse, real
        r = xcal.store(532, 1200, a_, 1.0, "si", [], centre_nm=cen, pool="auto", cm1_per_px=2.6)
    assert not r.get("pooled")
    xcal.clear()
    for cen, a_ in ((659.66, -9.5), (650.19, -9.8)):                              # no repeats, coarse, close
        r = xcal.store(637.75, 600, a_, 1.0, "si", [], centre_nm=cen, pool="auto", cm1_per_px=4.5)
    assert r["pooled"] and "no repeat data" in r["pool"]["why"]
    xcal.clear()
    assert not F.validate({"c": dict(st, calibration={"repeats": 0})})["c"]["ok"]
    # run order
    one = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP, "acquire": {"center_cm1": 520, "exposure_s": 0.2}}
    s = api.start({"a": one, "b": one}, inline=True, order=["b", "a"])
    assert s["ok"] and s["studies"] == ["b", "a"]
    api.status(s["request"], 30)
    assert not api.start({"a": one, "b": one}, order=["b"])["ok"]


def test_correlated_noise_auto_peaks_stitch_seams_range_corrections_and_headers(tmp_path):
    """2026-11-24 evening: LabSpec's DeNoise (Lite) correlates neighbouring points (lag-1 r ~0.87), and the
    point-to-point noise read ~8 x low, inflating every SNR; auto 'peaks' one point wide or poorly fitted were
    accepted; an extended range got one window's correction; the header's ICS and DeNoise were not reported; a
    bogus spectrometer read-back (-2027 nm) after a grating change; the points engine's estimate was ~2 x high."""
    from scipy.ndimage import gaussian_filter1d
    from labspec_translator import xcal
    rng_ = np.random.default_rng(3)
    n = gaussian_filter1d(rng_.normal(0, 4, 3000), 1.2)
    sm, rho = spectra.noise_smooth(300 + n)
    assert spectra.noise_sigma(300 + n) < 0.5 * np.std(n) and sm > 0.75 * np.std(n) and rho > 0.5
    x = np.linspace(100, 3200, 2000)
    y = 300 + gaussian_filter1d(rng_.normal(0, 4, x.size), 1.2)
    y[800] += 60                                                   # one point: not a Raman line
    a = spectra.analyze(x, y, {}, 10.0, 5.0)
    assert not any(pk.get("ok") and pk["fwhm_cm1"] < 2.5 * (x[1] - x[0]) for pk in a["peaks"]), a["peaks"]
    assert a["noise_sigma"] > 2 * a["noise_sigma_pixel"]
    # seams where the point spacing jumps
    xs = np.concatenate([np.arange(100, 1945, 1.56), np.arange(1945, 3200, 1.12)])
    assert runner.stitch_seams(xs) and abs(runner.stitch_seams(xs)[0] - 1945) < 5
    # an extended range keeps its own correction
    xcal.clear()
    xcal.store(637.75, 1200, 0.02, 1.0, "si", [], centre_nm=659.66, placement="centred")
    xcal.store(637.75, 1200, 1.5, 1.0, "si", [], placement="labspec", range_cm1=[100, 3200])
    assert xcal.get(637.75, 1200, None, "labspec", range_cm1=[100, 3200])["range_match"]
    assert xcal.get(637.75, 1200, 659.66, "centred")["a"] == 0.02
    xcal.clear()
    # LabSpec's header
    f = tmp_path / "s.txt"
    f.write_text("# acq.ICS correction=Off\n# acq.DeNoise=Lite\n500\t1\n")
    ics, dn = runner.header_ics_denoise(f)
    assert ics.startswith("off") and dn == "Lite"
    oc = runner.optics_record({"spectro_before_acq": "-2027.24", "spectro_after_data": "659.66"})
    assert oc["spectro_reading_invalid"] == ["spectro_before_acq"] and "spectro_moved_nm" not in oc
    # the estimate: one spectrometer move, not one per point
    st = {"kind": "map", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=1200),
          "acquire": {"center_cm1": 520, "exposure_s": 1, "accumulations": 2, "shutter": "hold"},
          "map": {"x_um": [-4, 4, 2], "y_um": [-4, 4, 2]}}
    e = plan.estimate(st, P.load())["seconds"]
    assert e["total"] - e["setup"] < 25 * 4.5, e
