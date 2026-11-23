"""Tests: format, plan and profile, protocol, analysis."""
from helpers import *  # noqa: F401,F403


# --------------------------------------------------------------------------------------------- format

def test_examples_validate():
    res = api.validate(EXAMPLES)
    for name, r in res.items():
        assert r["ok"], (name, r["errors"])
        assert r["estimate"]["minutes"] > 0


@pytest.mark.parametrize("study,needle", [
    ({"kind": "spectrum", "setup": dict(SETUP, laser_nm=405)}, "not on this instrument"),
    ({"kind": "spectrum", "setup": dict(SETUP, filter="100%"), "sample": {"name": "x", "sensitive": True}},
     "sensitive_max_filter_pct"),
    ({"kind": "map", "setup": SETUP, "map": {"x_um": [0, 6000, 100]}}, "max_xy_um"),
    ({"kind": "map", "setup": SETUP, "map": {"z_um": [-500, 500, 10]}}, "max_dz_um"),
    ({"kind": "spectrum", "setup": SETUP, "acquire": {"exposure_s": 9999}}, "max_exposure_s"),
    ({"kind": "spectrum", "setup": SETUP, "acquire": {"exposure_s": 1, "colour": 3}}, "unknown key 'colour'"),
    ({"kind": "calibration", "setup": SETUP, "acquire": {"range_cm1": [1000, 2000]}}, "no reference line"),
    ({"kind": "spectrum", "setup": SETUP, "points": [{"x_um": 1e5}]}, "max_xy_um"),
    ({"kind": "series", "setup": SETUP}, "series"),
    ({"kind": "map", "setup": SETUP, "map": {"x_um": [0, 10, 0]}}, "step must be > 0"),
])
def test_validation_errors(study, needle):
    r = F.validate({"s": study})["s"]
    assert not r["ok"]
    assert any(needle in e for e in r["errors"]), r["errors"]


def test_time_ceiling(monkeypatch):
    monkeypatch.setenv("LS6T_MAX_MINUTES", "1")
    r = F.validate({"m": EXAMPLES["graphene_map"]})["m"]
    assert not r["ok"] and any("LS6T_MAX_MINUTES" in e for e in r["errors"])


def test_study_format_is_compact():
    f = api.study_format()
    assert len(json.dumps(f)) < 14000
    assert f["profile"]["filters"] and f["status"]["settings"]["LS6T_MODE"] == "sim"


# --------------------------------------------------------------------------------------------- plan and profile

def test_axis_and_snake():
    assert plan.axis_values([0, 4, 2]) == [0, 2, 4]
    assert plan.axis_values([4, 0, 2]) == [4, 2, 0]
    pts = plan.positions({"kind": "map", "map": {"x_um": [0, 2, 1], "y_um": [0, 1, 1]}})
    assert [(p["x_um"], p["y_um"]) for p in pts] == [(0, 0), (1, 0), (2, 0), (2, 1), (1, 1), (0, 1)]


def test_range_in_nm():
    prof = P.load()
    st = {"kind": "spectrum", "setup": SETUP, "acquire": {"range_cm1": [100, 3200]}}
    rng = plan.acq_range(st, prof, 532.0)
    assert rng["from_nm"] == pytest.approx(1e7 / (1e7 / 532 - 100), abs=1e-3)
    assert rng["to_nm"] == pytest.approx(1e7 / (1e7 / 532 - 3200), abs=1e-3)
    assert rng["windows"] >= 4
    one = plan.acq_range({"kind": "spectrum", "setup": SETUP, "acquire": {"center_cm1": 520.7}}, prof, 532.0)
    assert one["from_nm"] == one["to_nm"] == pytest.approx(547.157, abs=0.01)


def test_filter_labels():
    assert P.filter_pct("10%") == 10 and P.filter_pct("D1") == 10 and P.filter_pct("OD2") == 1
    assert P.filter_pct("0,1 %") == 0.1 and P.filter_pct("weird") is None


def test_profile_import_from_discovery(tmp_path):
    sim = S.SimLabSpec(tmp_path / "b").start()
    try:
        b = bridge.Bridge(tmp_path / "b")
        r = b.call("discover", timeout_s=10, out=str(tmp_path / "dump.txt"))
        assert r["ok"]
    finally:
        sim.stop()
    prof, notes = P.import_discovery(P.read_text(tmp_path / "dump.txt"))
    assert [v["nm"] for v in prof["lasers"].values()] == [532.0, 638.0, 785.0]
    assert "1800" in prof["gratings"] and prof["objectives"] == ["x10", "x50", "x100"]
    assert prof["filters"]["10%"] == 10.0
    assert prof["limits"] == P.DEFAULT_PROFILE["limits"]     # limits are a person's, never imported


# --------------------------------------------------------------------------------------------- protocol

def test_kv_round_trip():
    d = {"path": r"C:\data\new\native.l6s", "text": "line 1\nline 2 100% ok", "x": 1.5, "flag": True}
    back = bridge.decode(bridge.encode(d))
    assert back == {"path": d["path"], "text": d["text"], "x": "1.5", "flag": "1"}


def test_command_files_are_utf16(tmp_path):
    b = bridge.Bridge(tmp_path / "b")
    (b.dir / "heartbeat.txt").write_text("state=idle\n")
    with pytest.raises(bridge.BridgeTimeout):
        b.call("ping", timeout_s=0.3, sample="µm °C")
    # the command was withdrawn after the timeout; the stop flag names it
    flag = (b.dir / "stop.flag").read_bytes()
    assert flag[:2] == b"\xff\xfe" and "seq=" in P.read_text(b.dir / "stop.flag")


def test_stale_stop_flag_is_ignored(tmp_path):
    sim = S.SimLabSpec(tmp_path / "b").start()
    try:
        b = bridge.Bridge(tmp_path / "b")
        b.request_stop("00000000000000000001")          # for some earlier command
        r = b.call("move", timeout_s=10, motor="Grating", mode="value", value=600)
        assert r["ok"] and float(r["value"]) == 600
        assert not (b.dir / "stop.flag").exists()
    finally:
        sim.stop()


def test_bridge_gone(tmp_path):
    b = bridge.Bridge(tmp_path / "nobridge")
    with pytest.raises(bridge.BridgeGone):
        b.call("ping", timeout_s=1)


def test_modal_question_keeps_bridge_alive(tmp_path, monkeypatch):
    b = bridge.Bridge(tmp_path / "b")
    hb = b.dir / "heartbeat.txt"
    hb.write_text("state=busy\nverb=confirm\n")
    old = time.time() - 600
    import os
    os.utime(hb, (old, old))
    assert b.alive()
    hb.write_text("state=busy\nverb=acquire\n")
    os.utime(hb, (old, old))
    assert not b.alive()


# --------------------------------------------------------------------------------------------- analysis

def test_fit_recovers_peak():
    x = np.linspace(400, 650, 800)
    rng = np.random.default_rng(3)
    y = 5000 / (1 + 4 * ((x - 520.7) / 4.0) ** 2) + 200 + 0.5 * (x - 400) + rng.normal(0, 10, x.size)
    a = spectra.analyze(x, y, {"peaks": [{"near_cm1": 521, "name": "Si"}]})
    pk = a["peaks"][0]
    assert pk["ok"] and pk["position_cm1"] == pytest.approx(520.7, abs=0.05)
    assert pk["fwhm_cm1"] == pytest.approx(4.0, rel=0.1) and pk["snr"] > 100


def test_not_detected_in_noise():
    x = np.linspace(1400, 1800, 600)
    y = 300 + np.random.default_rng(1).normal(0, 5, x.size)
    pk = spectra.fit_peak(x, y - 300, 1585, 40)
    assert not pk["ok"] and pk.get("detected") is False


def test_saturation_and_spikes():
    y = np.full(200, 1000.0)
    y[50] = 9000.0
    assert spectra.spikes(y + np.random.default_rng(0).normal(0, 3, 200)) == [50]
    y2 = y.copy()
    y2[100:104] = 65535
    s = spectra.saturation(y2, 65535)
    assert s["saturated"] and s["pixels"] == 4


def test_shift_conversion():
    sh, unit = spectra.to_shift([1e7 / (1e7 / 532.0 - 520.7)], "nm", 532.0)
    assert sh[0] == pytest.approx(520.7, abs=1e-6) and unit == "nm"
    with pytest.raises(ValueError):
        spectra.to_shift([500], "furlongs", 532.0)
