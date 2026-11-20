"""Tests for labspec-translator: format, plan, protocol, analysis, runs on the simulator, the LabSpec scripts."""
import json
import re
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


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LS6T_MODE", "sim")
    monkeypatch.setenv("LS6T_OUT", str(tmp_path / "runs"))
    monkeypatch.setenv("LS6T_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LS6T_PROFILE", raising=False)
    monkeypatch.delenv("LS6T_MAX_MINUTES", raising=False)
    monkeypatch.setenv("LS6T_CONFIRM", "request")
    yield
    S.stop_all()


def run_inline(studies, wait=120):
    s = api.start(studies, inline=True)
    assert s["ok"], s
    return api.status(s["request"], wait_s=wait)


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
    ({"kind": "calibration", "setup": SETUP, "acquire": {"range_cm1": [1000, 2000]}}, "Si line"),
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


# --------------------------------------------------------------------------------------------- runs (simulator)

def test_calibration_run():
    st = run_inline({"si": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                            "acquire": {"exposure_s": 1, "accumulations": 2, "spike": "double"},
                            "expect": [{"quantity": "calibration.offset_cm1", "value": 0, "tol": 0.5}],
                            "images": True}})
    assert st["done"] and st["state"] == "done"
    r = st["studies"]["si"]
    assert r["reliable"], r["unreliable_because"]
    assert r["calibration"]["pass"] and r["expect"][0]["pass"]
    assert r["checks"]["setup"]["pass"] and r["checks"]["detector_temp_c"]["pass"]
    assert r["instrument"]["laser_nm"] == 532.0 and r["instrument"]["filter"] == "10%"
    assert Path(r["spectra"][0]["file"]).exists() and Path(r["images"]["spectrum_0"]["path"]).exists()


def test_map_run_and_live_view():
    st = run_inline({"m": {"kind": "map", "sample": {"name": "flake"}, "setup": dict(SETUP, grating=600),
                           "acquire": {"range_cm1": [1200, 2900], "exposure_s": 1, "accumulations": 2,
                                       "spike": "double"},
                           "map": {"x_um": [-2, 2, 2], "y_um": [0, 1, 1]},
                           "process": {"peaks": [{"name": "G", "near_cm1": 1585}]}, "images": True}})
    r = st["studies"]["m"]
    assert r["reliable"] and r["complete"] and r["points_measured"] == 6
    assert r["map"]["nx"] == 3 and r["map"]["ny"] == 2
    assert r["map"]["summary"]["G"]["position_cm1"]["n"] == 4          # x = 0 and 2: the flake
    grids = json.loads(Path(r["map"]["files"]["json"]).read_text())["grids"]
    assert grids["G.position_cm1"][0][0][0] is None                    # x = -2: no graphene
    assert "G_position_cm1" in r["images"]
    npz = np.load(r["map"]["files"]["npz"])
    assert npz["spectra"].shape[0] == 6


def test_stage_moves_are_offsets_from_start():
    st = run_inline({"p": {"kind": "spectrum", "sample": {"name": "x"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520, "exposure_s": 0.5},
                           "points": [{"x_um": 0, "y_um": 0}, {"x_um": 10, "y_um": -5, "dz_um": 3}]}})
    sp = st["studies"]["p"]["spectra"]
    start = json.loads((Path(st["request"]) / "start.json").read_text())["status"]
    x0, y0, z0 = (float(start[k]) for k in ("x_um", "y_um", "z_um"))
    assert sp[1]["stage"]["x_um"] == pytest.approx(x0 + 10) and sp[1]["stage"]["y_um"] == pytest.approx(y0 - 5)
    assert sp[1]["stage"]["z_um"] == pytest.approx(z0 + 3)


def test_operator_declines():
    bdir = config.active_bridge_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "sim_confirm_no").write_text("1")
    st = run_inline({"a": {"kind": "spectrum", "sample": {"name": "x"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520}},
                     "b": {"kind": "spectrum", "sample": {"name": "y"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520}}})
    assert st["state"] == "cancelled" and st["stopped_by"] == "operator"
    assert not st["studies"]["a"]["ok"] and not st["studies"]["b"]["ok"]
    assert not list((Path(st["request"]) / "a" / "spectra").glob("*.txt"))   # no exposure happened


def test_operator_stop_mid_map():
    bdir = config.active_bridge_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "sim_operator_stop_after").write_text("3")
    st = run_inline({"o": {"kind": "map", "sample": {"name": "x"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520, "exposure_s": 1}, "map": {"x_um": [0, 9, 1]}}})
    r = st["studies"]["o"]
    assert st["stopped_by"] == "operator" and r["points_measured"] == 3
    assert not r["reliable"] and not r["complete"] and r["ok"]


def test_cancel_mid_run():
    bdir = config.active_bridge_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "sim_time_scale").write_text("0.05")
    s = api.start({"c": {"kind": "map", "sample": {"name": "x"}, "setup": SETUP,
                         "acquire": {"center_cm1": 520, "exposure_s": 2}, "map": {"x_um": [0, 30, 1]}}},
                  inline=True)
    for _ in range(200):
        prog = api.status(s["request"])["progress"].get("c", {})
        if prog.get("stage") == "measuring" and prog.get("point", 0) >= 2:
            break
        time.sleep(0.05)
    api.cancel(s["request"])
    st = api.status(s["request"], wait_s=60)
    r = st["studies"]["c"]
    assert st["state"] == "cancelled" and 2 <= r["points_measured"] < 31 and not r["reliable"]


def test_saturation_is_unreliable():
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, filter="100%"),
                           "acquire": {"center_cm1": 520, "exposure_s": 30}}})
    r = st["studies"]["s"]
    assert not r["reliable"] and not r["checks"]["saturation"]["pass"]


def test_invalid_request_is_refused():
    s = api.start({"bad": {"kind": "spectrum", "setup": dict(SETUP, grating=999)}})
    assert not s["ok"] and "bad" in s["errors"]


def test_interrupted_runner_is_reported(tmp_path):
    folder = config.out_dir() / "run_old"
    (folder / "x").mkdir(parents=True)
    runner.write_json(folder / "request.json", {"created": time.time() - 3600})
    runner.write_json(folder / "studies.json", {"x": {}})
    runner.write_json(folder / "state.json", {"state": "running"})
    runner.write_json(folder / "runner.json", {"time": time.time() - 3600})
    st = api.status(str(folder))
    assert st["state"] == "interrupted" and not st["studies"]["x"]["ok"]


def test_inputs_without_running():
    r = api.inputs(EXAMPLES["graphene_map"], "g")
    verbs = [s["verb"] for s in r["steps"]]
    assert r["ok"] and verbs[0] == "status" and "acquire" in verbs and r["points"] == 441
    assert verbs.index("move") < verbs.index("acquire")


def test_read_file_is_confined(tmp_path):
    with pytest.raises(ValueError):
        api.read_file("/etc/hosts")


# --------------------------------------------------------------------------------------------- the LabSpec scripts

def test_vbs_scripts(tmp_path):
    out = api.install_bridge(tmp_path / "vbs")
    sim_verbs = {n[2:] for n in dir(S.SimLabSpec) if n.startswith("v_")}
    for name, path in out["files"].items():
        raw = Path(path).read_bytes()
        assert all(b < 128 for b in raw), name
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
        text = raw.decode()
        assert "@@" not in text and 'Const LS6T_BRIDGE_DIR = "' in text
        # blocks balance
        stack = []
        for n, line in enumerate(text.split("\r\n"), 1):
            s = re.sub(r'"[^"]*"', '""', line).split("'")[0].strip().lower()
            if not s:
                continue
            for opener, closer, pat in (("sub", "end sub", r"^(private |public )?sub "),
                                        ("function", "end function", r"^(private |public )?function "),
                                        ("select", "end select", r"^select case"), ("do", "loop", r"^do\b"),
                                        ("for", "next", r"^for\b")):
                if re.match(pat, s):
                    stack.append(opener)
                elif re.match(re.escape(closer) + r"\b", s):
                    assert stack and stack[-1] == opener, (name, n, line)
                    stack.pop()
            if re.match(r"^if .* then$", s):
                stack.append("if")
            elif s.startswith("end if"):
                assert stack and stack[-1] == "if", (name, n, line)
                stack.pop()
        assert not stack, (name, stack)
        # every LS_ constant used is defined
        used = set(re.findall(r"\bLS_[A-Z_]+\b", text))
        defined = set(re.findall(r"Const (LS_[A-Z_]+)", text))
        assert used <= defined, used - defined
    bridge_text = Path(out["files"]["ls6t_bridge.vbs"]).read_text()
    handled = set(re.findall(r'Case "([a-z_]+)"', bridge_text))
    assert sim_verbs == handled, (sim_verbs ^ handled)


def test_mcp_server_tools():
    pytest.importorskip("mcp")
    from labspec_translator import mcp_server
    assert "Workflow" in mcp_server.INSTRUCTIONS
    assert len(mcp_server.INSTRUCTIONS.splitlines()) <= 30


def test_benchmark_subset():
    r = api.bench(["si_calibration", "saturation"])
    assert r["passed"] == ["si_calibration", "saturation"], r


def test_bridge_call_for_maintainers():
    r = api.bridge_call("acquire", {"out": "x.txt"})
    assert not r["ok"] and "--laser" in r["error"]
    r = api.bridge_call("move", {"motor": "Filter", "mode": "label", "value": "1%"})
    assert r["ok"] and r["label"] == "1%"
    st = api.bridge_call("status", {})
    assert st["ok"] and st["filter"] == "1%"


def test_cli_bridge_call_takes_params_after_options(capsys):
    from labspec_translator import cli
    rc = cli.main(["bridge", "call", "move", "--timeout", "30", "motor=Grating", "mode=value", "value=600"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and float(out["value"]) == 600


# --------------------------------------------------------------------------------------------- video snapshots

def _profile_with_shutter(tmp_path, **limits):
    sim = S.ensure(config.active_bridge_dir())
    b = bridge.Bridge()
    r = b.call("discover", timeout_s=10, out=str(tmp_path / "dump.txt"))
    assert r["ok"]
    prof, _ = P.import_discovery(P.read_text(tmp_path / "dump.txt"))
    prof["limits"].update(limits)
    P.save(prof)
    return prof


def test_decode_packing_and_order(tmp_path):
    from labspec_translator import video
    W, H = 3, 2
    px = [0x0000FF, 0x00FF00, 0xFF0000, 0x000001, 0x000002, 0x000003]   # COLORREF: red, green, blue, ...
    f = tmp_path / "f.video.txt"
    f.write_text("# axis_types=X,Y,Intens\n# axis_sizes=3,2,0\n# axis_units=mkm,mkm,cnt\n"
                 "# axis_values=10,11,12,20,21\n# data_dims=6\n" + ",".join(map(str, px)) + "\n")
    d = video.decode(f, "colorref", "x_fastest")
    assert d["um"] and d["img"].shape == (2, 3, 3)
    assert tuple(d["img"][0, 0]) == (255, 0, 0) and tuple(d["img"][0, 2]) == (0, 0, 255)
    assert list(d["xs"]) == [10, 11, 12] and list(d["ys"]) == [20, 21]
    d2 = video.decode(f, "rgb", "x_fastest")
    assert tuple(d2["img"][0, 0]) == (0, 0, 255)


def test_snapshot_marks_the_laser_and_finds_the_spot(tmp_path):
    _profile_with_shutter(tmp_path)
    r = api.snapshot()
    assert r["ok"] and Path(r["path"]).exists() and r["um_axes"]
    assert r["marker_um"] == [pytest.approx(1234.5), pytest.approx(-567.8)]
    assert "spot_um" not in r
    r = api.snapshot(with_laser=True)
    assert r["ok"] and r["measured_offset_um"] == [pytest.approx(1.5, abs=0.1), pytest.approx(-0.8, abs=0.1)]
    assert r["raw_nolaser"] and not r["warnings"]


def test_snapshot_with_laser_is_guarded(tmp_path):
    S.ensure(config.active_bridge_dir())
    r = api.snapshot(with_laser=True)                    # built-in profile: no laser shutter motor
    assert not r["ok"] and "laser shutter" in r["error"]
    _profile_with_shutter(tmp_path, max_filter_pct=0.5)
    api.bridge_call("move", {"motor": "Filter", "mode": "label", "value": "10%"})
    r = api.snapshot(with_laser=True)
    assert not r["ok"] and "cap" in r["error"]
    P.save(dict(P.load(), limits=dict(P.load()["limits"], max_filter_pct=100)))
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    r = api.snapshot(with_laser=True)
    assert not r["ok"] and "declined" in r["error"]


def test_calibration_takes_a_video_frame_by_default(tmp_path):
    _profile_with_shutter(tmp_path)
    st = run_inline({"si": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                            "acquire": {"exposure_s": 1, "accumulations": 2},
                            "video": {"after": True, "with_laser": True}}})
    r = st["studies"]["si"]
    assert r["reliable"], (r["unreliable_because"], r["warnings"])
    assert set(r["video"]) == {"before", "after"} and Path(r["images"]["video_before"]["path"]).exists()
    assert r["video"]["before"]["measured_offset_um"][0] == pytest.approx(1.5, abs=0.1)


def test_video_validation():
    r = F.validate({"v": {"kind": "spectrum", "setup": SETUP, "video": {"before": True, "with_laser": True}}})["v"]
    assert not r["ok"] and any("laser shutter" in e for e in r["errors"])
    r = F.validate({"v": {"kind": "spectrum", "setup": SETUP, "video": {"before": "yes"}}})["v"]
    assert not r["ok"] and any("true or false" in e for e in r["errors"])


def test_mcp_snapshot_returns_an_image(tmp_path):
    pytest.importorskip("mcp")
    from labspec_translator import mcp_server
    S.ensure(config.active_bridge_dir())
    out = mcp_server.snapshot()
    assert isinstance(out, list) and out[0].__class__.__name__ == "Image" and "marker_um" in out[1]


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
