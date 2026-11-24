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
    assert [j["point"] for j in ll["jumps"]] == [1, 2] and ll["jumps"][0]["moved_cm1"] == pytest.approx(-8.7, abs=0.6)
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
    # off by default (2026-11-24: the MapEx view froze, then crashed, LabSpec on the instrument)
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


def test_laser_may_be_on_when_the_bridge_stops_during_a_hold(tmp_path):
    """2026-11-24: LabSpec froze and crashed with the shutter held; ls6t must say the laser may still reach the
    sample. The heartbeat carries shutter_held; diagnose and a request's stop say it first; a display call is never
    made with the shutter held (the bridge lets it go first)."""
    from labspec_translator import bridge as BR
    msg = BR.diagnose({"state": "busy", "verb": "view", "age_s": 300, "shutter_held": "1"}, tmp_path)
    assert msg.startswith("THE LASER MAY STILL REACH THE SAMPLE")
    assert not BR.diagnose({"state": "busy", "verb": "view", "age_s": 300, "shutter_held": "0"}, tmp_path).startswith("THE LASER")
    assert "view" not in runner.HOLD_VERBS and "video" not in runner.HOLD_VERBS
    text = api.compose_vbs(config.active_bridge_dir(), config.home())["ls6t_bridge.vbs"]
    hv = [l for l in text.splitlines() if l.startswith("Const HOLD_VERBS")][0]
    assert ",view," not in hv and 'ts.WriteLine "shutter_held=" & Bit(gHoldOn)' in text
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
