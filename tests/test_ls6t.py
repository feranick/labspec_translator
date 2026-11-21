"""Tests for labspec-translator: format, plan, protocol, analysis, runs on the simulator, the LabSpec scripts."""
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

def _flake_map(engine):
    return {"m": {"kind": "map", "sample": {"name": "flake"}, "setup": dict(SETUP, grating=600),
                  "acquire": {"center_cm1": 2000, "exposure_s": 0.5},
                  "map": {"x_um": [-4, 4, 2], "y_um": [0, 2, 1], "engine": engine},
                  "process": {"peaks": [{"name": "G", "near_cm1": 1585}]}}}


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


# --------------------------------------------------------------------------------------------- the type-library reader

def test_typelib_annotate_and_render():
    from labspec_translator import typelib
    lib = {"file": "NFActiveX.ocx", "library": "NFActiveXLib", "doc": "LabSpec 6 ActiveX", "version": "6.6",
           "guid": "{x}", "types": [
               {"name": "_DNFActiveX", "kind": "dispinterface", "doc": "", "guid": "{y}", "properties": [],
                "values": [], "methods": [
                    {"name": "Acq", "invoke": "method", "returns": "long", "dispid": 1, "doc": "",
                     "params": [{"name": "Mode", "type": "long", "dir": "in", "optional": False}]},
                    {"name": "AutoAlignment", "invoke": "method", "returns": "long", "dispid": 2, "doc": "",
                     "params": []},
                    {"name": "GetMapProgress", "invoke": "method", "returns": "double", "dispid": 3, "doc": "",
                     "params": [{"name": "ID", "type": "long", "dir": "in", "optional": True}]},
                    {"name": "Invoke", "invoke": "method", "returns": "void", "dispid": 4, "doc": "", "params": []}]},
               {"name": "AcqMode", "kind": "enum", "doc": "", "guid": "{z}", "methods": [], "properties": [],
                "values": [{"name": "ACQ_SPECTRUM", "type": "int", "value": 0}]}]}
    lib = typelib.annotate(lib)
    s = lib["summary"]
    assert s["new"] == ["AutoAlignment", "GetMapProgress"] and "AutoAlignment" in s["new_of_interest"]
    assert "Invoke" not in s["new"] and "MoveMotor" in s["documented_missing"]
    txt = typelib.render(lib, "text")
    assert "NEW long AutoAlignment()" in txt and "[align]" in txt and "ACQ_SPECTRUM = 0" in txt
    md = typelib.render(lib, "markdown")
    assert md.startswith("# NFActiveXLib") and "| NEW | GetMapProgress" in md
    assert json.loads(typelib.render(lib, "json"))["summary"]["methods"] == 3


def test_typelib_cli_off_windows(capsys):
    from labspec_translator import typelib
    rc = typelib.main(["--list"])
    assert rc == 1 and "no LabSpec ActiveX" in capsys.readouterr().out


@pytest.mark.skipif(not os.environ.get("LS6T_TEST_OCX"), reason="set LS6T_TEST_OCX to LabSpec's NFActiveX.ocx")
def test_typelib_reads_labspec_ocx():
    from labspec_translator import typelib
    lib = typelib.read(os.environ["LS6T_TEST_OCX"])
    names = {m["name"] for t in lib["types"] for m in t["methods"]}
    assert {"Acq", "GetAcqID", "MoveMotor", "SetMappingParams", "MoveXY"} <= names
    assert lib["reader"].startswith("ls6t")


# --------------------------------------------------------------------------------------------- 2026.11.20.1

def test_labspec_config_save_list_load(tmp_path):
    S.ensure(config.active_bridge_dir())
    r = api.labspec_config("save", "baseline")
    assert r["ok"] and r["exists"] and Path(r["file"]).exists()
    assert "baseline" in api.labspec_config("list")["configs"]
    assert "baseline" in api.references()["labspec_configs"]
    api.bridge_call("move", {"motor": "Grating", "mode": "label", "value": "600"})
    assert float(api.instrument("status")["status"]["grating"]) == 600
    r = api.labspec_config("load", "baseline")
    assert r["ok"] and float(api.instrument("status")["status"]["grating"]) == 1800
    assert not api.labspec_config("load", "nothing")["ok"]
    assert not api.labspec_config("save", "../x")["ok"]
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    r = api.labspec_config("load", "baseline")
    assert not r["ok"] and "declined" in r["error"]


def test_restore_config_after_a_request():
    P.save(dict(P.load(), session={"restore_config": True}))
    S.ensure(config.active_bridge_dir())
    api.bridge_call("move", {"motor": "Grating", "mode": "label", "value": "2400"})
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                           "acquire": {"center_cm1": 520, "exposure_s": 0.5}}})
    assert st["state"] == "done" and st["config_restored"] is True
    assert (Path(st["request"]) / "labspec_before.ini").exists()
    assert float(api.instrument("status")["status"]["grating"]) == 2400      # the study used 1800; restored


def test_last_error_is_reported():
    S.ensure(config.active_bridge_dir())
    api.bridge_call("config", {"action": "load", "path": "C:/none.ini"})
    assert "not found" in api.instrument("last_error")["info"]
    assert "not found" in api.instrument("status")["status"]["last_error"]


def test_extended_video_is_guarded(tmp_path):
    S.ensure(config.active_bridge_dir())
    r = api.snapshot(extended=True)
    assert not r["ok"] and "allow_extended" in r["error"]
    assert not api.snapshot(with_laser=True, extended=True)["ok"]
    P.save(dict(P.load(), video=dict(P.load()["video"], allow_extended=True)))
    r = api.snapshot(extended=True)
    assert r["ok"] and Path(r["path"]).exists()
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    assert "declined" in api.snapshot(extended=True)["error"]


def test_native_map_export_and_discovery_probes(tmp_path):
    st = run_inline(_flake_map("native"))
    assert st["studies"]["m"]["reliable"]
    S.ensure(config.active_bridge_dir())
    r = bridge.Bridge().call("discover", timeout_s=10, out=str(tmp_path / "dump.txt"))
    # discovery no longer probes these (2026.11.21.4): dumps from older bridges still carry them
    old = (tmp_path / "dump.txt").read_text() + "\n".join([
        "selftest.get_value_ex=0", "selftest.get_value_simple_part=1", "selftest.get_value_simple_part_size=8",
        "selftest.get_value_simple_part_values=4,5,6,7,8,9,10,11", "selftest.get_config_param=1",
        "config_param.0.name=Delay", "config_param.0.unit=s", "config_param.0.type=double",
        "config_param.0.value=0"]) + "\n"
    prof, notes = P.import_discovery(old)
    assert prof["discovered"]["methods"] == {"GetLastErrorInfo": True, "GetValueEx": False,
                                             "GetValueSimplePart": True, "GetConfigParam": True}
    assert prof["discovered"]["config_params"][0]["name"] == "Delay" and prof["mapping"]["export_chunk"] == 65536
    text = old.replace("selftest.get_value_simple_part=1", "selftest.get_value_simple_part=0")
    prof, notes = P.import_discovery(text)
    assert prof["mapping"]["export_chunk"] == 0 and any("read whole" in n for n in notes)


def test_vbs_new_methods(tmp_path):
    files = api.install_bridge(tmp_path / "vbs")["files"]
    assert set(files) == {"ls6t_bridge.vbs", "ls6t_discover.vbs", "ls6t_script_motor.vbs"}
    br = Path(files["ls6t_bridge.vbs"]).read_text()
    for needle in ("GetValueSimplePart(id, \"Data\"", "LabSpec.GetValueEx(", "LabSpec.SaveConfig(",
                   "LabSpec.LoadConfig(", "LS_START_EXTENDED_VIDEO", "GetArr(id, \"Data\""):
        assert needle in br, needle
    assert "SetScriptPath" not in br.replace("' ", "") or "LabSpec.SetScriptPath" not in br
    sm = Path(files["ls6t_script_motor.vbs"]).read_text()
    assert "LabSpec.GetMotorAction(action, position)" in sm and "LabSpec.SetMotorInfo(" in sm
    disc = Path(files["ls6t_discover.vbs"]).read_text()
    assert "LabSpec.GetConfigParam" not in disc and "LabSpec.SetConfigParam" not in disc   # discovery only reads


# --------------------------------------------------------------------------------------------- Windows installer

def test_winsetup_path_and_json_entries(tmp_path, monkeypatch):
    from labspec_translator import clients as C
    from labspec_translator import winsetup as W
    new, ch = W.path_add(r"C:\a;C:\b", r"C:\ls6t\bin")
    assert ch and new.endswith(r";C:\ls6t\bin")
    assert W.path_add(new, "C:\\ls6t\\bin\\") == (new, False)
    assert W.path_remove(new, r"C:\ls6t\bin") == (r"C:\a;C:\b", True)
    py = str(tmp_path / "python" / "python.exe")
    cfg = {"mcpServers": {"other": {"command": "x"},
                          "ls6t": {"command": "old.exe", "env": {"LS6T_OUT": "D:\\runs", "LS6T_CONFIRM": "study"}}},
           "globalShortcut": "x"}
    cfg2, note = C.merge_server(cfg, "ls6t", py, {"LS6T_CONFIRM": "request"})
    e = cfg2["mcpServers"]["ls6t"]
    assert "replaced" in note and e["command"] == py and e["args"] == ["-m", "labspec_translator.mcp_server"]
    assert e["env"] == {"LS6T_OUT": "D:\\runs", "LS6T_CONFIRM": "study"}          # the person's settings kept
    assert cfg2["mcpServers"]["other"] == {"command": "x"} and cfg2["globalShortcut"] == "x"
    g, _ = C.merge_server({"mcpServers": {"ls6t": {"command": py, "trust": True}}}, "ls6t", py, {}, client="gemini")
    assert g["mcpServers"]["ls6t"]["trust"] is True and g["mcpServers"]["ls6t"]["timeout"] == 3600000
    cfg3, _ = C.merge_server(cfg2, "ls6t-sim", py, {"LS6T_OUT": "s"}, forced={"LS6T_MODE": "sim"})
    assert cfg3["mcpServers"]["ls6t-sim"]["env"] == {"LS6T_OUT": "s", "LS6T_MODE": "sim"}
    cfg4, gone = C.remove_server(cfg3, "ls6t", py)
    assert gone and "ls6t" not in cfg4["mcpServers"]
    assert C.remove_server(cfg, "ls6t", py) == (cfg, False)                       # another install's entry stays
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    backup = C.write_json(p, cfg2)
    assert backup and json.loads(backup.read_text()) == cfg and C.read_json(p) == cfg2


def test_codex_toml_entry():
    from labspec_translator import clients as C
    tomllib = C.tomllib
    py = r"C:\Users\o'neil\AppData\Local\Programs\ls6t\python\python.exe"
    text = ('model = "gpt-5"\n\n[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "c7"]\n\n'
            '[mcp_servers.ls6t]\ncommand = \'C:\\old\\python.exe\'\ntool_timeout_sec = 120\n\n'
            '[mcp_servers.ls6t.env]\nLS6T_OUT = \'D:\\runs\'\n\n[profiles.x]\nmodel = "o3"\n')
    new, note = C.toml_merge(text, "ls6t", py, {"LS6T_CONFIRM": "request", "LS6T_OUT": "x"})
    d = tomllib.loads(new)
    e = d["mcp_servers"]["ls6t"]
    assert "replaced" in note and e["command"] == py and e["args"] == ["-m", "labspec_translator.mcp_server"]
    assert e["tool_timeout_sec"] == 120 and e["env"] == {"LS6T_CONFIRM": "request", "LS6T_OUT": "D:\\runs"}
    assert d["model"] == "gpt-5" and d["mcp_servers"]["context7"]["command"] == "npx" and d["profiles"]["x"]
    new2, _ = C.toml_merge(new, "ls6t-sim", py, {}, forced={"LS6T_MODE": "sim"})
    assert tomllib.loads(new2)["mcp_servers"]["ls6t-sim"]["env"] == {"LS6T_MODE": "sim"}
    gone, ok = C.toml_remove(new2, "ls6t", py)
    d = tomllib.loads(gone)
    assert ok and "ls6t" not in d["mcp_servers"] and "ls6t-sim" in d["mcp_servers"] and d["profiles"]["x"]
    assert C.toml_remove(gone, "ls6t-sim", "other.exe") == (gone, False)
    with pytest.raises(ValueError):
        C.toml_merge("mcp_servers = { ls6t = { command = 'x' } }\n", "ls6t", py, {})
    with pytest.raises(ValueError):
        C.toml_merge("[broken\n", "ls6t", py, {})


def test_connect_detect_and_disconnect(tmp_path, monkeypatch, capsys):
    from labspec_translator import cli
    from labspec_translator import clients as C
    monkeypatch.setenv("HOME", str(tmp_path / "u"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "u"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "u" / ".config"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(C.Path, "home", classmethod(lambda cls: tmp_path / "u"))
    monkeypatch.setattr(C.shutil, "which", lambda exe: None)
    assert not any(C.detect().values())
    (tmp_path / "u" / ".gemini").mkdir(parents=True)
    (tmp_path / "u" / ".gemini" / "settings.json").write_text('{"theme": "dark"}')
    found = C.detect()
    assert found["gemini"] and not found["codex"] and not found["claude"]
    assert cli.main(["connect", "auto", "--sim"]) == 0
    g = json.loads((tmp_path / "u" / ".gemini" / "settings.json").read_text())
    assert g["theme"] == "dark" and set(g["mcpServers"]) == {"ls6t", "ls6t-sim"}
    assert g["mcpServers"]["ls6t"]["env"]["LS6T_HOME"] == g["mcpServers"]["ls6t-sim"]["env"]["LS6T_HOME"]
    assert cli.main(["connect", "codex", "claude"]) == 0
    toml = C.tomllib.loads((tmp_path / "u" / ".codex" / "config.toml").read_text())
    assert toml["mcp_servers"]["ls6t"]["command"] == sys.executable
    st = C.status()
    assert all(st[c]["files"] and any("ls6t" in v for v in st[c]["files"].values()) for c in C.CLIENTS)
    capsys.readouterr()
    assert cli.main(["connect", "gemini", "codex", "claude", "--remove"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(sum((r["removed"] for r in out["files"]), [])) == ["ls6t", "ls6t", "ls6t", "ls6t-sim"]
    assert json.loads((tmp_path / "u" / ".gemini" / "settings.json").read_text())["mcpServers"] == {}


def test_installer_files(tmp_path):
    import zipfile
    sys.path.insert(0, str(ROOT / "installer"))
    try:
        import build_installer as B
    finally:
        sys.path.pop(0)
    z = tmp_path / "py.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("python312._pth", "python312.zip\r\n.\r\n\r\n#import site\r\n")
        zf.writestr("python.exe", "x")
    assert B.stage_python(z, tmp_path / "py") == "312"
    assert "import site" in (tmp_path / "py" / "python312._pth").read_text().splitlines()
    assert B.version() == __import__("labspec_translator").__version__
    nsi = (ROOT / "installer" / "ls6t.nsi").read_text()
    for needle in ("labspec_translator.winsetup install", "labspec_translator.winsetup uninstall",
                   "RequestExecutionLevel user", "ls6t-shell.ps1", "INSTALL.html"):
        assert needle in nsi, needle
    for f in ("ls6t.cmd", "ls6t-mcp.cmd", "ls6t-typelib.cmd", "ls6t-shell.ps1"):
        raw = (ROOT / "installer" / "bin" / f).read_bytes()
        assert all(b < 128 for b in raw), f
    for f, mod in (("ls6t.cmd", "cli"), ("ls6t-mcp.cmd", "mcp_server"), ("ls6t-typelib.cmd", "typelib")):
        raw = (ROOT / "installer" / "bin" / f).read_bytes()
        assert f"-m labspec_translator.{mod} %*".encode() in raw and b"\r\n" in raw


# --------------------------------------------------------------------------------------------- test mode

def test_testing_plan_covers_the_results_table():
    from labspec_translator import testmode as T
    text = (ROOT / "TESTING.md").read_text(encoding="utf-8")
    plan = T.parse(text)
    table = text[text.index("## Results table"):]
    wanted = set()
    for full, base, first, last in re.findall(r"^\| ((T\d+[a-z]?)(?:\.(\d+))?(?:–T\d+[a-z]?\.(\d+))?)", table,
                                              flags=re.M):
        if not first:
            wanted.add(base)
        else:
            wanted |= {f"{base}.{i}" for i in range(int(first), int(last or first) + 1)}
    assert wanted and wanted <= set(plan["steps"]), wanted - set(plan["steps"])
    assert len(plan["questions"]) >= 40 and plan["steps"]["T9.1"]["questions"] == ["Q12"]
    assert plan["steps"]["T5b.3"]["laser"] and not plan["steps"]["T5b.1"]["laser"] and plan["steps"]["T5"]["laser"]


def test_hardware_test_tool(tmp_path, monkeypatch):
    from labspec_translator import testmode as T
    monkeypatch.delenv("LS6T_TEST_MODE", raising=False)
    assert "test mode is off" in T.hardware_test("plan")["error"]
    monkeypatch.setenv("LS6T_TEST_MODE", "1")
    S.ensure(config.active_bridge_dir())
    plan = T.hardware_test("plan")
    assert plan["next"] == "T0.1" and len(plan["steps"]) > 60
    st = T.hardware_test("step", step="T3.4")
    assert "laser-nm" in st["text"] and st["commands_in_chat"]
    with pytest.raises(ValueError):
        T.hardware_test("step", step="T99")
    assert "quit" in T.hardware_test("call", verb="quit")["error"]
    assert not T.hardware_test("call", verb="rm")["ok"]
    r = T.hardware_test("call", step="T3.3", verb="move", params={"motor": "Grating", "mode": "value", "value": 600})
    assert r["ok"] and float(r["value"]) == 600
    r = T.hardware_test("call", step="T3.6", verb="move_xy", params={"x_um": 1234.5 + 6000, "y_um": -567.8})
    assert not r["ok"] and "max_xy_um" in r["error"]
    assert T.hardware_test("call", verb="move", params={"motor": "Filter", "mode": "label", "value": "10%"})["ok"]
    P.save(dict(P.load(), limits=dict(P.load()["limits"], max_filter_pct=5)))
    r = T.hardware_test("call", step="T3.1", verb="move", params={"motor": "Filter", "mode": "label", "value": "25%"})
    assert not r["ok"] and "cap" in r["error"]
    assert "current filter" in T.hardware_test("call", verb="acquire", params={"exposure_s": 1})["error"]  # 10% now
    T.hardware_test("call", verb="move", params={"motor": "Filter", "mode": "label", "value": "1%"})
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    assert "declined" in T.hardware_test("call", step="T4.1", verb="acquire", params={"exposure_s": 1})["error"]
    (config.active_bridge_dir() / "sim_confirm_no").unlink()
    r = T.hardware_test("call", step="T4.1", verb="acquire",
                        params={"exposure_s": 1, "out": "C:/Windows/evil", "from_nm": 540, "to_nm": 560})
    assert r["ok"] and r["summary"]["max_counts"] > 0 and str(T.folder()) in r["summary"]["file"]
    with pytest.raises(ValueError):
        T.hardware_test("record", step="T4.1", result="great")
    with pytest.raises(ValueError):
        T.hardware_test("record", step="T4.1", result="pass", answers={"Q999": "x"})
    r = T.hardware_test("record", step="T4.1", result="pass", notes="Si line seen", values={"max": 1234},
                        answers={"Q6": "x axis in nm"}, who="NF")
    rep = Path(r["report"]).read_text()
    assert "| T4.1 |" in rep and "| pass | Si line seen; max = 1234; Q6: x axis in nm |" in rep and "| Q6 |" in rep
    assert T.hardware_test("plan")["steps"][[s["id"] for s in plan["steps"]].index("T4.1")]["result"] == "pass"
    calls = [e for e in T.entries() if e["kind"] == "call"]
    assert any(e.get("declined") for e in calls) and any(e["step"] == "T4.1" and e.get("ok") for e in calls)
    assert T.hardware_test("report")["recorded"] == 1


def test_mcp_test_mode_and_connect_flag(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import importlib
    from labspec_translator import clients as C
    from labspec_translator import mcp_server
    assert not hasattr(mcp_server, "hardware_test")
    monkeypatch.setenv("LS6T_TEST_MODE", "1")
    m = importlib.reload(mcp_server)
    try:
        assert hasattr(m, "hardware_test") and "TEST MODE" in m.INSTRUCTIONS
        assert len(m.INSTRUCTIONS.splitlines()) <= 30
    finally:
        monkeypatch.delenv("LS6T_TEST_MODE")
        importlib.reload(mcp_server)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(C.Path, "home", classmethod(lambda cls: tmp_path / "u"))
    C.connect(["claude"], "py", sim=True, test=True)
    env = C.read_json(C.claude_files()[0])["mcpServers"]
    assert env["ls6t"]["env"]["LS6T_TEST_MODE"] == "1" and env["ls6t-sim"]["env"]["LS6T_TEST_MODE"] == "1"
    C.connect(["claude"], "py")
    assert C.read_json(C.claude_files()[0])["mcpServers"]["ls6t"]["env"]["LS6T_TEST_MODE"] == "1"   # kept
    C.connect(["claude"], "py", test=False)
    assert C.read_json(C.claude_files()[0])["mcpServers"]["ls6t"]["env"]["LS6T_TEST_MODE"] == "0"


def test_camera_on_off_and_roles(monkeypatch):
    from labspec_translator import testmode as T
    S.ensure(config.active_bridge_dir())
    assert api.instrument("status")["status"]["live_video"] == "0"
    r = api.instrument("camera_on")
    assert r["ok"] and r["live"] == "1" and api.instrument("status")["status"]["live_video"] == "1"
    r = api.instrument("camera_off")                  # HORIBA's stop, on by default since T3.13 passed
    assert r["ok"] and r["live"] == "0"
    P.save(dict(P.load(), video=dict(P.load()["video"], stop_from_script=False)))
    api.instrument("camera_on")
    r = api.instrument("camera_off")                  # a person can turn it off: refused then
    assert not r["ok"] and r["live"] == "1"
    monkeypatch.setenv("LS6T_TEST_MODE", "1")
    assert T.hardware_test("call", verb="camera", params={"action": "on"})["live"] == "1"
    assert not T.hardware_test("call", verb="camera", params={"action": "off"})["ok"]
    r = T.hardware_test("call", verb="move", params={"motor": "grating", "mode": "value", "value": 600})
    assert r["ok"] and r["motor"] == "Grating"


def test_camera_by_name():
    S.ensure(config.active_bridge_dir())
    P.save(dict(P.load(), video=dict(P.load()["video"], cameras={"top": 0, "side": 1})))
    r = api.instrument("camera_on", camera="Top camera")
    assert r["ok"] and r["camera_name"] == "top"
    r = api.instrument("camera_on", camera="bottom")
    assert not r["ok"] and "top (0)" in r["error"]
    assert api.study_format()["profile"]["cameras"] == {"top": 0, "side": 1}
    from labspec_translator import mcp_server
    assert "NFActiveX" in mcp_server.INSTRUCTIONS


def test_claude_desktop_config_folders(tmp_path, monkeypatch):
    from labspec_translator import clients as C
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    monkeypatch.setattr(C, "_windows", lambda: True)
    monkeypatch.setattr(C, "claude_running_dirs", lambda: ())
    monkeypatch.setattr(C, "claude_is_running", lambda: False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.delenv("CLAUDE_USER_DATA_DIR", raising=False)
    assert C.claude_dirs(create=True) == [roaming / "Claude"]               # no Store app: the classic folder
    old = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    old.mkdir(parents=True)
    assert C.claude_dirs(create=True) == [old]                              # a Store build that has not moved
    py = str(tmp_path / "python.exe")
    (old / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"ls6t": {"command": py}}}))
    (old / ".user-data-left.claude_pzs8sxrjxfjjc").write_text("")
    new = local / "Claude-Data" / "claude_pzs8sxrjxfjjc"
    assert C.claude_dirs(create=True) == [new]                              # moved: Claude-Data
    reps = C.connect(["claude"], py)
    assert C.read_json(new / "claude_desktop_config.json")["mcpServers"]["ls6t"]["command"] == py
    assert C.read_json(old / "claude_desktop_config.json")["mcpServers"] == {}   # the stale entry taken out
    assert any("no longer reads" in " ".join(r.get("notes", [])) for r in reps)
    monkeypatch.setattr(C, "claude_running_dirs", lambda: (tmp_path / "custom",))
    assert C.claude_dirs()[0] == tmp_path / "custom"                        # the running app's --user-data-dir


def test_connect_check_starts_the_server(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labspec_translator import clients as C
    r = C.handshake(sys.executable, ["-m", "labspec_translator.mcp_server"],
                    {"PYTHONPATH": os.pathsep.join(sys.path), "LS6T_MODE": "sim"})
    assert r["ok"] and "study_format" in r["tools"], r
    r = C.handshake(sys.executable, ["-c", "import sys; sys.exit('boom')"])
    assert not r["ok"] and "boom" in r["stderr"]


def test_placeholders_from_the_docs_are_replaced():
    from labspec_translator import clients as C
    cfg = {"mcpServers": {"ls6t": {"command": "C:\\Users\\<you>\\py.exe",
                                   "env": {"LS6T_HOME": "C:\\Users\\<you>\\ls6t", "LS6T_CONFIRM": "study"}}}}
    new, _ = C.merge_server(cfg, "ls6t", "C:\\Users\\jo\\py.exe", {"LS6T_HOME": "C:\\Users\\jo\\ls6t"})
    assert new["mcpServers"]["ls6t"]["env"] == {"LS6T_HOME": "C:\\Users\\jo\\ls6t", "LS6T_CONFIRM": "study"}


def test_instrument_set_and_label_matching():
    from labspec_translator.profile import match_label
    assert match_label("x100", ["x10_VIS", "x50_VIS", "x100_VIS"]) == ("x100_VIS", None)
    assert match_label("x10", ["x10_VIS", "x100_VIS"]) == ("x10_VIS", None)
    assert match_label("X50_vis", ["x50_VIS"]) == ("x50_VIS", None)
    assert match_label("x100", ["x100_VIS", "x100_LWD"])[0] is None
    S.ensure(config.active_bridge_dir())
    r = api.instrument("set", setup={"laser_nm": 638, "grating": 600, "filter": "1%"})
    assert r["ok"], r
    assert float(r["after"]["laser_nm"]) == 638 and float(r["after"]["grating"]) == 600 and r["after"]["filter"] == "1%"
    assert [m["set"] for m in r["moves"]] == ["filter", "laser", "grating"]
    assert not runner.read_json(runner.lock_path(), None)                          # the lock is released
    r = api.instrument("set", setup={"objective": "x50"})
    assert r["ok"] and r["after"]["objective"] == "x50"
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    assert "declined" in api.instrument("set", setup={"objective": "x10"})["error"]
    assert not api.instrument("set", setup={"laser_nm": 405})["ok"]
    assert not api.instrument("set", setup={"power": 5})["ok"]
    assert F.validate({"s": {"kind": "spectrum", "sample": {"name": "x"},
                             "setup": dict(SETUP, objective="X100")}})["s"]["ok"]
    P.save(dict(P.load(), limits=dict(P.load()["limits"], max_filter_pct=5)))
    assert "cap" in api.instrument("set", setup={"filter": "25%"})["error"]


def test_profile_from_the_bridge_inventory(tmp_path):
    dump = ROOT / "tests" / "data" / "discovery_xplora_plus.txt"
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "discovery_latest.txt").write_bytes(dump.read_bytes())
    prof = P.load()                                                         # made automatically
    assert prof["auto"] and config.profile_path().exists() and prof["system"] == "XploRA Plus"
    assert prof["objectives"] == ["x100_VIS", "x10_VIS", "x5_VIS"] and "0.1%" in prof["filters"]
    assert all(v["max_filter_pct"] == 10 for v in prof["lasers"].values())
    assert prof["motors"]["laser_shutter"] is None and prof["mapping"]["export_chunk"] == 0
    assert any("bytes" in n for n in prof["auto"]["notes"])
    st = api.tool_status()["profile"]
    assert "automatic" in st and "drift" not in st
    assert F.validate({"s": {"kind": "spectrum", "sample": {"name": "Si"},
                             "setup": {"laser_nm": 638, "grating": 1800, "filter": "1%", "objective": "x100"}}})["s"]["ok"]
    r = api.profile_set("lasers.532.max_filter_pct", "25")                  # a person's edit ends the automatic state
    assert r["note"] and "auto" not in P.load()
    text = (home / "discovery_latest.txt").read_text(encoding="utf-16").replace("x5_VIS", "x50_VIS")
    (home / "discovery_latest.txt").write_text(text.replace("13:50:59", "14:00:00"), encoding="utf-16")
    prof = P.load()
    assert "x5_VIS" in prof["objectives"]                                   # not replaced: a person's profile
    assert any("x50_VIS" in d for d in api.tool_status()["profile"]["drift"])


def test_laser_on_for_viewing(tmp_path):
    S.ensure(config.active_bridge_dir())
    assert "no laser shutter" in api.instrument("laser_on")["error"]             # built-in profile: none
    _profile_with_shutter(tmp_path)
    api.instrument("set", setup={"filter": "1%"})
    assert "max_laser_on_s" in api.instrument("laser_on", seconds=600)["error"]
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    assert "declined" in api.instrument("laser_on", seconds=10)["error"]
    (config.active_bridge_dir() / "sim_confirm_no").unlink()
    r = api.instrument("laser_on", seconds=10)
    assert r["ok"] and r["open"] == "1"
    assert api.instrument("status")["status"]["laser_open"] == "1"
    api.instrument("set", setup={"grating": 600})                               # another command closes it
    assert api.instrument("status")["status"]["laser_open"] == "0"
    api.instrument("laser_on", seconds=10)
    assert api.instrument("laser_off")["was_open"] == "1"
    P.save(dict(P.load(), limits=dict(P.load()["limits"], max_filter_pct=0.5)))
    assert "cap" in api.instrument("laser_on", seconds=10)["error"]


def test_mcp_instrument_takes_a_camera_number():
    pytest.importorskip("mcp")
    import asyncio
    from labspec_translator import mcp_server
    S.ensure(config.active_bridge_dir())
    srv = mcp_server.server
    res = asyncio.run(srv.call_tool("instrument", {"action": "camera_on", "camera": 1}))
    text = str(res)
    assert "is_error=False" in text and "live" in text and "camera_on failed" not in text, text


def test_labspec_ini_completes_the_profile(monkeypatch):
    from labspec_translator import labspec_ini as L
    ini = ROOT / "tests" / "data" / "OLConfig_min.ini"
    s = L.summary(L.read(ini))
    assert s["instrument"] == "XploRA Plus"
    assert s["cameras"]["Internal"]["number"] == 1 and s["motors"]["LaserPol"] == ["Circular", "Horizontal", "Vertical"]
    assert s["objectives"]["x100_VIS"] == {"magnification": 100.0, "na": 0.9, "field_um": [85.56, 58.34]}
    monkeypatch.setenv("LS6T_LABSPEC_INI", str(ini))
    monkeypatch.setenv("LS6T_LABSPEC_SETUP_INI", str(ROOT / "tests" / "data" / "OLSetup_min.ini"))
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "discovery_latest.txt").write_bytes((ROOT / "tests" / "data" / "discovery_xplora_plus.txt").read_bytes())
    prof = P.load()
    assert prof["video"]["cameras"] == {"top": 0, "internal": 1}
    assert prof["objectives_info"]["x10_VIS"]["na"] == 0.25 and "x5_VIS" in prof["objectives_info"]
    assert any("cameras from LabSpec" in n for n in prof["auto"]["notes"])
    r = api.instrument("camera_on", camera="internal")         # 'internal' is camera 1 (no switching: refused)
    assert not r["ok"] and "not 1," in r["error"]
    lc = prof["labspec_config"]
    assert lc["autocalibration_script"].endswith("UniversalAutoCalibrationv3.vbs")
    green = lc["oneclick_presets"]["OneClick Green"]
    assert green == {"enabled": True, "laser": "532nm_Edge", "grating": "600 (750nm)", "center": 1001.38070121135,
                     "range": [100.0, 3500.0]} or green["laser"] == 2      # the made-up ini lists no laser names


def test_autocalibration_flow(tmp_path, monkeypatch):
    from labspec_translator import autocal
    script = tmp_path / "LabSpec" / "AutoCalibration" / "AutoCal.vbs"
    script.parent.mkdir(parents=True)
    script.write_text("Option Explicit\nDim fso, Angle\nSub Main()\n  LabSpec.OperVar 1, \"ACT_INSTRUMENT[BF][0]\", 1, 0\n"
                      "  LabSpec.Dialog DIALOG_SHOW, \"\", \"\", \"\", \"\"\nEnd Sub\nMain\n", encoding="cp1252")
    a = autocal.assess(script, api.compose_vbs(config.bridge_dir(), config.home())["ls6t_bridge.vbs"])
    assert "fso" in a["names_shared_with_the_bridge"] and not a["runs_inside_the_bridge"]
    assert "LabSpec dialogs" in a["asks_the_operator"] and any("OperVar" in c for c in a["changes"])
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_xshift").write_text("2.0,1.0")
    study = {"c": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                   "calibration": {"adjust": "auto"}}}
    st = run_inline(study)                                       # not enabled: guided, like 'labspec'
    cal = st["studies"]["c"]["calibration"]
    assert cal["pass"] and "not enabled" in cal["autocalibration"]
    (bdir / "sim_xshift").write_text("2.0,1.0")
    P.save(dict(P.load(), calibration=dict(P.load()["calibration"], autocal={
        "enabled": True, "script": str(script), "timeout_s": 60, "sample": "the Si wafer"})))
    st = run_inline(study)
    cal = st["studies"]["c"]["calibration"]
    assert cal["pass"] and cal["autocalibration"][0]["ok"] and [x["pass"] for x in cal["attempts"]] == [False, True]
    (bdir / "sim_xshift").write_text("2.0,1.0")
    r = api.instrument("autocal_run")
    assert r["ok"] and r["changed"]["1800"]["BF"]                      # the zero moved back
    assert api.instrument("autocal_info")["enabled"]



# --------------------------------------------------------------------------------------------- instrument checks

def test_filter_transmission_check():
    bdir = config.active_bridge_dir()
    S.ensure(bdir)
    (bdir / "sim_filter_actual").write_text('{"25%": 0.6}')
    st = run_inline({"f": {"kind": "filters", "sample": {"name": "Si"},
                           "setup": {"laser_nm": 532, "grating": 1800, "objective": "x100"},
                           "filters": {"exposure_s": 0.2, "max_exposure_s": 5}}})
    r = st["studies"]["f"]
    assert r["reliable"], r["unreliable_because"]
    steps = {s_["label"]: s_ for s_ in r["filters"]["steps"]}
    assert r["filters"]["reference"] == "100%" and set(steps) == {"100%", "50%", "25%", "10%"}   # 1%: 20 s > 5
    assert steps["50%"]["measured_pct"] == pytest.approx(50, rel=0.08)
    assert steps["25%"]["measured_pct"] == pytest.approx(15, rel=0.1) and any("25%" in f for f in r["filters"]["flagged"])
    assert "ls6t profile set filters" in r["filters"]["profile_command"]
    assert api.instrument("status")["status"]["filter"] == "1%"           # back to the operator's filter
    assert "532" in api.references()["performance"]["filters"]
    bad = F.validate({"f": {"kind": "filters", "setup": dict(SETUP)}})["f"]
    assert not bad["ok"] and any("setup.filter" in e for e in bad["errors"])


def test_spectral_resolution_is_remembered():
    for _ in range(2):
        st = run_inline({"si": {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
                                "acquire": {"exposure_s": 1}}})
    res = st["studies"]["si"]["calibration"]["resolution"]
    assert res["key"].startswith("532@1800") and res["measurements"] == 2 and res["fwhm_cm1"] > 0
    assert res["fwhm_cm1"] == pytest.approx(res["baseline_cm1"], rel=0.2)


def test_edge_filter_cutoff(tmp_path):
    _profile_with_shutter(tmp_path)
    bdir = config.active_bridge_dir()
    (bdir / "sim_lamp").write_text("1")
    st = run_inline({"c": {"kind": "cutoff", "sample": {"name": "white light"},
                           "setup": {"laser_nm": 532, "grating": 1800},
                           "acquire": {"exposure_s": 1, "accumulations": 2}}})
    r = st["studies"]["c"]
    assert r["reliable"], r["unreliable_because"]
    c = r["cutoff"]
    assert c["edge_50_cm1"] == pytest.approx(45, abs=4) and c["edge_10_cm1"] < c["edge_50_cm1"] < c["usable_from_cm1"]
    assert Path(r["images"]["cutoff"]["path"]).exists()
    v = F.validate({"s": {"kind": "spectrum", "sample": {"name": "x"}, "setup": SETUP,
                          "acquire": {"range_cm1": [30, 600]}}})["s"]
    assert any("cut-off" in w for w in v["warnings"])


def test_polarisers_in_the_setup(tmp_path):
    prof = _profile_with_shutter(tmp_path)
    assert prof["motors"]["laser_pol"] == "LaserPol" and "Vertical" in prof["polarizers"]["laser_pol"]
    r = api.instrument("set", setup={"laser_pol": "vertical", "raman_pol": "horizontal"})
    assert r["ok"] and r["after"]["laser_pol"] == "Vertical" and r["after"]["raman_pol"] == "Horizontal"
    v = F.validate({"s": {"kind": "spectrum", "sample": {"name": "x"},
                          "setup": dict(SETUP, laser_pol="Diagonal")}})["s"]
    assert not v["ok"] and any("laser_pol" in e for e in v["errors"])
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, raman_pol="None"),
                           "acquire": {"center_cm1": 520, "exposure_s": 0.5}}})
    moves = st["studies"]["s"]["setup"]["moves"]
    assert any(m["motor"] == "RamanPol" and m["ok"] for m in moves)


def test_discovery_is_read_only_logged_and_skips_a_crash(tmp_path, monkeypatch):
    """2026-11-20: discovery's probes crashed LabSpec (About NFActiveX, then a crash) on one account. Discovery now
    only reads, logs each step, and skips a step that stopped LabSpec before; jobs lists the attempt."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    body = br[br.index("Function DiscoverTo("):]
    body = body[:body.index("End Function")]
    for bad in ("CreateDataObject", "PutValue", "GetValueSimplePart", "GetValueEx", "GetConfigParam", ".Exec ",
                "LaserSwitch", "E-Cal_Neon"):
        assert bad not in body, bad
    assert 'DStep("motor." & name)' in body and "discovery_skip.txt" in br and "DiscoverTo(GetP(" in br
    S.ensure(config.active_bridge_dir())
    (config.active_bridge_dir() / "sim_discover_crash").write_text("video")
    r = api.instrument("discover")
    assert not r["ok"] and r["stopped_in"] == "video" and "skips it" in r["next"]
    (config.active_bridge_dir() / "sim_discover_crash").unlink()
    r = api.instrument("discover")
    assert r["ok"] and any("video" in n for n in r["notes"]) and "profile.proposed.json" in r["next"]
    assert api.discovery_progress()["finished"] and api.discovery_progress()["skip"] == ["video"]
    acts = api.jobs()["instrument_actions"]
    assert [a["action"] for a in acts[:2]] == ["discover", "discover"] and acts[1]["stopped_in"] == "video"
    assert "dialog" in bridge.labspec_hint()


def test_profile_import_reviewed_json_and_no_defaults_on_the_instrument(tmp_path, monkeypatch):
    prof = P.load()
    prop = tmp_path / "profile.proposed.json"
    P.save(dict(prof, lasers={"637.75": {"nm": 637.75, "label": "637.75", "power_mw": 20, "max_filter_pct": None}}),
           prop)
    with pytest.raises(ValueError, match="max_filter_pct"):
        P.import_reviewed(prop)
    P.save(dict(prof, auto={"x": 1}, lasers={"637.75": {"nm": 637.75, "label": "637.75", "power_mw": None,
                                                        "max_filter_pct": 5}}), prop)
    with pytest.raises(ValueError, match="power_mw"):
        P.import_reviewed(prop)
    P.save(dict(prof, auto={"x": 1}, lasers={"637.75": {"nm": 637.75, "label": "637.75", "power_mw": 12,
                                                        "max_filter_pct": 5}}), prop)
    data, notes = P.import_reviewed(prop)
    assert "auto" not in data and data["lasers"]["637.75"]["max_filter_pct"] == 5 and data["reviewed"]
    assert any("laser_shutter" in n for n in notes)
    from labspec_translator import cli
    assert cli.main(["profile", "import", str(prop)]) == 0
    assert P.load()["lasers"]["637.75"]["max_filter_pct"] == 5 and not P.load().get("auto")
    # on the instrument the laser fires only with a reviewed profile, still complete
    monkeypatch.setenv("LS6T_MODE", "labspec")
    name = "s"
    st = {name: {"kind": "spectrum", "sample": {"name": "Si"},
                 "setup": {"laser_nm": 637.75, "grating": 1800, "filter": "1%", "objective": "x100"},
                 "acquire": {"center_cm1": 520, "exposure_s": 1}}}
    assert api.validate(st)[name]["ok"], api.validate(st)[name]["errors"]
    move = {"kind": "move", "move": {"x_um": 5, "y_um": 0}}
    assert P.review_gate(P.load()) is None
    P.save(dict(P.load(), reviewed=None))
    errs = api.validate({name: st[name]})[name]["errors"]
    assert any("laser fires only with a reviewed profile" in e for e in errs), errs
    assert not any("reviewed" in e for e in api.validate({"m": move})["m"]["errors"])
    assert "reviewed" in api.laser(True, 5)["error"]
    assert "reviewed" in api.snapshot(with_laser=True)["error"]
    P.save(dict(P.load(), auto={"from": "x"}))
    assert "automatic" in P.review_gate(P.load())
    monkeypatch.setenv("LS6T_MODE", "sim")
    # the built-in generic profile is never used on the instrument
    config.profile_path().unlink()
    monkeypatch.setenv("LS6T_MODE", "labspec")
    st = json.loads((Path(__file__).parent.parent / "examples" / "studies.json").read_text())
    name = next(iter(st))
    r = api.validate({name: st[name]})[name]
    assert not r["ok"] and any("generic XploRA" in e for e in r["errors"])


def test_profile_propose_is_the_one_review_route(tmp_path):
    S.ensure(config.active_bridge_dir())
    assert api.instrument("discover")["ok"]
    shutil_src = config.home() / "discovery_latest.txt"
    if not shutil_src.exists():
        r = bridge.Bridge().call("discover", timeout_s=10, out=str(shutil_src))
    before = P.load()
    r = api.profile_propose()
    assert r["ok"] and r["proposed_profile"].endswith("profile.proposed.json") and "profile import" in r["next"]
    assert any("power_mw" in m for m in r["to_fill_in"])
    assert P.load().get("source") == before.get("source")          # the profile in use is unchanged
    assert "profile propose" in P.review_gate(P.load()) and "profile propose" in P.REVIEW_STEPS
    if P.load().get("auto"):
        assert P.load()["auto"]["review"] == P.REVIEW_STEPS


def test_camera_switch_is_checked_and_video_stop_is_not_from_script(tmp_path):
    """2026-11-20 on the XploRA: camera 0 was ignored, camera 1 showed only from the next video start, ls6t's first
    stop hung the bridge, HORIBA's stop works (T3.12-13). Switching (opt-in, video.switch_camera) restarts the video:
    stop HORIBA's way, select, start, and check the camera only once the video runs."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    assert br.count("LabSpec.Video LS_STOP_VIDEO") == 1 and "Video(LS_STOP_VIDEO)" not in br   # StopVideoHoriba only
    cf = br[br.index("Function CameraFor("):]
    cf = cf[:cf.index("End Function")]
    assert cf.index("If Not allowSwitch Then") < cf.index("If live Then") < cf.index("StopVideoHoriba(res)") \
        < cf.index("SelectCamera ")
    sel = br[br.index("Sub SelectCamera("):]
    sel = sel[:sel.index("End Sub")]
    assert sel.index("VidSettle") < sel.index("LS_SET_ACTIVE_CAMERA") and "GET_ACTIVE_CAMERA" not in sel
    cam = br[br.index("Sub VCamera("):]
    cam = cam[:cam.index("End Sub")]
    assert cam.index("LabSpec.Video LS_START_VIDEO") < cam.index("CameraCheck(res)")
    vid = br[br.index("Sub VVideo("):]
    vid = vid[:vid.index("End Sub")]
    assert vid.index("LabSpec.Video LS_START_VIDEO") < vid.index("CameraCheck(res)") and "keepOn" in vid
    prof = P.load()
    assert prof["video"]["switch_camera"] is False and prof["video"]["stop_from_script"] is True
    S.ensure(config.active_bridge_dir())
    P.save(dict(prof, video=dict(prof["video"], cameras={"top": 0, "internal": 1})))
    r = api.instrument("camera_on", camera="top")              # sim starts on camera 0: the active one is fine
    assert r["ok"] and r["camera_name"] == "top" and r["video_started"] == "1"
    r = api.instrument("camera_on", camera="internal")         # switching off: refused, with what to do
    assert not r["ok"] and "video.switch_camera" in r["error"] and "end_bridge" in r["error"]
    P.save(dict(P.load(), video=dict(P.load()["video"], switch_camera=True)))
    r = api.instrument("camera_on", camera="internal")         # running: stopped HORIBA's way, selected, restarted
    assert r["ok"] and r["camera_name"] == "internal" and r["camera_restart"] == "1" and r["live"] == "1"
    (config.active_bridge_dir() / "sim_camera_locked").write_text("1")
    r = api.instrument("camera_on", camera="top")              # LabSpec ignores it (camera 0, Q44): said so
    assert not r["ok"] and "ignored the switch" in r["error"] and r["camera_after"] == "1"
    P.save(dict(P.load(), video=dict(P.load()["video"], stop_from_script=False)))
    r = api.instrument("camera_on", camera="top")
    assert not r["ok"] and "may not stop it" in r["error"]


def test_bridge_diagnosis_is_one_story_and_end_bridge(tmp_path):
    """2026-11-20: the same stuck bridge was reported once as stuck in 'ping', seconds later as not running; and the
    advice named a script stop button this LabSpec does not have."""
    hb = {"state": "busy", "verb": "ping", "age_s": 40}
    a = bridge.diagnose(hb, tmp_path)
    assert "busy in ping" in a and "not running" not in a and "stop button" not in a and "Cancel" in a
    assert "start-up inventory" in bridge.diagnose({"state": "busy", "verb": "", "age_s": 30}, tmp_path)
    idle = bridge.diagnose({"state": "idle", "verb": "", "age_s": 30}, tmp_path)
    assert "ended without stopping" in idle and "video" in idle
    assert "has not run here" in bridge.diagnose(None, tmp_path)
    # both paths (before sending, while waiting) use it
    b = bridge.Bridge(tmp_path / "br")
    (b.dir / "heartbeat.txt").write_text("state=busy\nverb=ping\n", encoding="utf-8")
    old = time.time() - 100
    os.utime(b.dir / "heartbeat.txt", (old, old))
    with pytest.raises(bridge.BridgeGone, match="busy in ping"):
        b.call("status", timeout_s=2)
    S.ensure(config.active_bridge_dir())
    r = api.instrument("end_bridge")
    assert r["ok"] and "start ls6t_bridge.vbs" in r["next"].lower().replace("start ls6t", "start ls6t")
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    assert "script stop button" not in br and 'gVerb = "inventory"' in br


def test_after_the_bridges_own_video_stop_status_does_not_touch_the_video(tmp_path):
    """T3.12 (2026-11-20): HORIBA's stop returned (3.4 s), then status hung inside LabSpec on its video queries; the
    timeout message did not say what was stopped or by whom."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    st = br[br.index("Sub VStatus("):]
    st = st[:st.index("End Sub")]
    assert "LabSpec.Video" not in st and "VidId()" in st and "VidCam()" in st
    vid_id = br[br.index("Function VidId()"):]
    assert vid_id.index("If gVidOff Then Exit Function") < vid_id.index("LabSpec.Video(LS_GET_VIDEO_ID)")
    sv = br[br.index("Function StopVideoHoriba("):]
    sv = sv[:sv.index("End Function")]
    assert sv.index("If StopVideoHoriba Then") < sv.index("gVidOff = True") < sv.index("gVidStopT = Timer")
    assert br.count("        VidSettle\n        LabSpec.Video LS_START_VIDEO") == 2      # 5 s after a stop, then start
    cam = br[br.index("Sub VCamera("):]
    cam = cam[:cam.index("End Sub")]
    assert "LabSpec.Video(LS_GET_ACTIVE_CAMERA)" not in cam and "gVidOff = False" in cam
    b = bridge.Bridge(tmp_path / "br")
    (b.dir / "heartbeat.txt").write_text("state=busy\nverb=status\n", encoding="utf-8")
    old = time.time() - 12
    os.utime(b.dir / "heartbeat.txt", (old, old))
    msg = b._timeout_text("status", 15)
    assert "stuck inside LabSpec in 'status'" in msg and "drop it" in msg
    (b.dir / "heartbeat.txt").write_text("state=busy\nverb=map_native\n", encoding="utf-8")
    assert "still works" in b._timeout_text("map_native", 15)


def test_every_video_call_respects_the_bridges_own_stop(tmp_path):
    """After the bridge's own stop, no command may ask LabSpec about the video before the bridge starts it again
    (T3.12: status hung on that), and a new bridge starts without that state (it is not saved anywhere)."""
    import re as _re
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    # each LabSpec.Video call, by the procedure it is in
    where, cur = {}, None
    for line in br.splitlines():
        m = _re.match(r"\s*(?:Function|Sub)\s+(\w+)", line)
        if m:
            cur = m.group(1)
        if "LabSpec.Video" in line and not line.strip().startswith("'"):
            where.setdefault(cur, []).append(line.strip())
    allowed = {"VidId", "VidCam",          # gated by gVidOff
               "SelectCamera",             # opt-in switching: VidSettle, then SET only (no read-back while off)
               "StopVideoHoriba",          # only with the video running (not gVidOff)
               "VCamera", "VVideo",        # START after VidSettle; GET_VIDEO_ID after the start (gVidOff cleared)
               "GrabFrame"}                # only once the video runs
    assert set(where) <= allowed, set(where) - allowed
    for proc in ("VCamera", "VVideo"):
        body = br[br.index(f"Sub {proc}("):]
        body = body[:body.index("End Sub")]
        assert body.count("VidSettle") >= 1 and "gVidOff = False" in body
        assert "LabSpec.Video(LS_GET_ACTIVE_CAMERA)" not in body
    ext = br[br.index("r = LabSpec.Video(LS_START_EXTENDED_VIDEO)") - 400:]
    assert "If gVidOff Then" in ext[:400]
    af = br[br.index("Sub VAutofocus("):]
    assert 'how = "video" And gVidOff' in af[:af.index("End Sub")]
    sc = br[br.index("Sub SelectCamera("):]
    sc = sc[:sc.index("End Sub")]
    assert sc.index("VidSettle") < sc.index("LabSpec.Video(LS_SET_ACTIVE_CAMERA")
    # a fresh bridge: the state starts off, and is never written to a file
    assert "gVidOff = False\n" in br[:br.index("Function VidId()")]
    assert not _re.search(r"(WriteKV|WriteLine)[^\n]*gVidOff", br)


def test_colour_frames_and_large_images(tmp_path, monkeypatch):
    """2026-11-20: the top camera's frame had 3 values per pixel (888 x 606 x 3) and was refused as grayscale; LabSpec's
    own PNG was over the ~1 MB a tool result carries, and get_image took only PNGs."""
    from labspec_translator import video as V
    W, H = 40, 30
    yy, xx = np.mgrid[0:H, 0:W]
    r, g, b = (xx * 6), (yy * 8), np.full((H, W), 50)
    for layout, arr in (("pixels", np.stack([r, g, b], -1).reshape(-1)),
                        ("planes", np.concatenate([r.reshape(-1), g.reshape(-1), b.reshape(-1)]))):
        f = tmp_path / f"{layout}.video.txt"
        f.write_text(f"# data_dims={W},{H},3\n" + "\n".join(",".join(map(str, arr[i:i + 40]))
                                                           for i in range(0, arr.size, 40)) + "\n")
        d = V.decode(f)
        assert d["channels"] == 3 and d["img"].shape == (H, W, 3)
        assert (d["img"][..., 0] == r).all() and (d["img"][..., 1] == g).all() and (d["img"][..., 2] == b).all()
        assert (V.decode(f, packing="rgb")["img"][..., 0] == b).all()        # red and blue swapped on request
    with pytest.raises(ValueError, match="1, 3 or 4 per pixel"):
        f = tmp_path / "bad.video.txt"
        f.write_text(f"# data_dims={W},{H}\n" + ",".join(["1"] * (W * H * 2)) + "\n")
        V.decode(f)
    # a large LabSpec PNG: a reduced copy under the limit, saved next to it; a JPEG too
    from PIL import Image as PILImage
    snap = config.out_dir() / "snapshots" / "s1"
    snap.mkdir(parents=True)
    rng = np.random.default_rng(0)
    big = PILImage.fromarray(rng.integers(0, 255, (1200, 1600, 3), dtype=np.uint8))
    big.save(snap / "frame.png")
    big.save(snap / "frame.jpg", quality=95)
    assert (snap / "frame.png").stat().st_size > api.MAX_IMAGE_BYTES
    for name in ("frame.png", "frame.jpg"):
        data, fmt, info = api.image_for_view(str(snap / name))
        assert len(data) <= api.MAX_IMAGE_BYTES and info["reduced"] and Path(info["view"]).is_file()
    small = snap / "small.png"
    PILImage.fromarray(np.zeros((50, 60, 3), np.uint8)).save(small)
    assert api.image_for_view(str(small))[2]["reduced"] is False
    with pytest.raises(ValueError):
        api.image_for_view(str(tmp_path / "elsewhere.png"))


def test_set_waits_for_motors_and_manual_objective(tmp_path):
    """2026-11-20 at the instrument: every set came back with the old value (MoveMotor gave no move id, so the bridge
    did not wait), mid-move readings looked like positions, the turret is manual, and a refused request did not say
    that nothing changed."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    mv = br[br.index("Sub VMove("):]
    mv = mv[:mv.index("End Sub")]
    assert "waited = WaitMotor(motor, gNoId, tmo, res)" in mv and "SettleReadback motor, mode, v, tmo, res" in mv
    assert "If moveId > 0 Then WaitMotor" not in mv
    assert mv.index('waited = WaitMotor("", 0, tmo, res)') < mv.index("SettleReadback")    # all motors, then read
    assert 'res("move_call_s")' in mv and 'res("all_wait_s")' in mv
    assert 'res("bridge_move_n") = gMoveN' in mv
    sr = br[br.index("Sub SettleReadback("):]
    assert "Abs(ParseNum(got) - ParseNum(v)) <= 0.5" in sr[:sr.index("End Sub")]
    S.ensure(config.active_bridge_dir())
    prof = P.load()
    assert prof["manual_objective"] is True
    q = api.objective_question(prof, "x10", "x100")
    assert "BY HAND" in q and "x100" in q
    P.save(dict(prof, limits=dict(prof["limits"], max_filter_pct=10)))
    r = api.instrument("set", setup={"filter": "25%", "grating": 600})
    assert not r["ok"] and "nothing was changed" in r["error"]
    assert api.instrument("status")["status"]["grating"] != "600"
    r = api.instrument("set", setup={"objective": "x50"})
    assert r["ok"] and r["objective"]["source"].startswith("changed by hand")
    r = api.instrument("set", setup={"grating": 1200})
    assert r["objective"]["source"].startswith("LabSpec's setting")
    assert r["moves"][0]["settle_s"] == 0.0 and r["moves"][0]["seconds"] is not None     # reported per move
    assert {"move_call_s", "wait_s", "all_wait_s"} <= set(r["moves"][0])
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    r = api.instrument("set", setup={"objective": "x10", "grating": 600})
    assert not r["ok"] and "nothing was changed" in r["error"]


def test_readback_matching_and_snapshot_move_confirm(tmp_path):
    """Formatting must not turn a reached position into a 30 s wait and a false failure; a move to a snapshot
    point on a manual turret asks the operator to confirm the objective."""
    for got, asked in (("637.8", 637.75), ("637.75", "637.8"), ("10 %", "10%"), ("X100_VIS", "x100_vis"),
                       ("300", 300.0), ("10%", 10), (" 100 µm", "100")):
        assert P.same_position(got, asked), (got, asked)
    for got, asked in (("0.10%", "0.1%"), ("0.01 %", "0.01%")):
        assert P.same_position(got, asked), (got, asked)
    for got, asked in (("1800", 1200), ("1%", "10%"), ("x10_VIS", "x100_VIS"), ("", 5), (None, 1),
                       ("0.1%", "0.01%"), ("0.5%", "0.1%"), ("0.01%", 0), ("1%", "0.5%")):   # filters: no abs. tol
        assert not P.same_position(got, asked), (got, asked)
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    sr = br[br.index("Sub SettleReadback("):]
    sr = sr[:sr.index("End Sub")]
    assert "NormLabel(got) = NormLabel(v)" in sr and 'res("settle_stable") = 1' in sr and "Elapsed(tSame) >= 1.5" in sr
    S.ensure(config.active_bridge_dir())
    st = run_inline({"m": {"kind": "move", "move": {"to_um": [1234.5 + 3, -567.8]}}})   # the operator says Yes
    assert st["state"] not in ("failed", "cancelled"), st["state"]
    assert "did not confirm objective" not in json.dumps(st)
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    st = run_inline({"m2": {"kind": "move", "move": {"to_um": [1234.5 + 3, -567.8]}}})
    assert "did not confirm objective" in json.dumps(st)


def test_grating_blocking_short_stop_timing_and_keepalive(tmp_path):
    """2026-11-20 (.34): the grating's status wait did not track it (wait_s 0.05 s), a grating move 1200 -> 2400
    stopped at 1800 without an error, the first set after a bridge start took 30-45 s, and a 30.9 s call was dropped
    by the app."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    mv = br[br.index("Sub VMove("):]
    mv = mv[:mv.index("End Sub")]
    assert 'GetP(cmd, "blocking", "0") = "1"' in mv and "moveId = LabSpec.MoveMotor(motor, posVal, posName, mc)\n" in mv
    assert 'Execute "BeatNow"' in br                    # the start-up inventory keeps the heartbeat going
    prof = P.load()
    assert runner.move_extra(prof, prof["motors"]["grating"]) == {}       # blocking changed nothing: off (.37)
    P.save(dict(prof, blocking_motors=["grating"]))
    prof = P.load()
    assert runner.move_extra(prof, prof["motors"]["grating"]) == {"blocking": 1}
    assert runner.move_extra(prof, prof["motors"]["filter"]) == {}
    S.ensure(config.active_bridge_dir())
    r = api.instrument("set", setup={"grating": 600})
    assert r["ok"] and r["moves"][0]["blocking"] is True and {"total_s", "status_before_s", "moves_s"} <= set(r["timing"])
    assert "not visible to ls6t" in r["note"]
    (config.active_bridge_dir() / "sim_grating_stuck").write_text("2400:1800")
    r = api.instrument("set", setup={"grating": 2400})
    assert not r["ok"] and "stopped at" in r["moves"][0]["error"] and "SpMax" in r["moves"][0]["error"]
    # long calls keep the app's request alive
    from labspec_translator import mcp_server as M
    import asyncio
    seen = []

    class Ctx:
        async def report_progress(self, p, total=None, message=None):
            seen.append(p)
    out = asyncio.run(M._keepalive(Ctx(), lambda: (time.sleep(0.35), "done")[1], every_s=0.1))
    assert out == "done" and len(seen) >= 2


def test_grating_reach_and_blocking_heartbeat(tmp_path, monkeypatch):
    """No grating diffracts beyond 2 / G in first order (2400 gr/mm: 833 nm); a blocking move holds the bridge
    without a heartbeat and must not be called stuck for that."""
    prof = P.load()
    r = plan.grating_reach(prof, 2400, 785)
    assert r["limit_nm"] == 833.3 and r["max_shift_cm1"] == 739
    assert plan.grating_reach(prof, 2400, 637.75)["max_shift_cm1"] == 3680
    assert plan.grating_reach(prof, 2400, 900)["max_shift_cm1"] is None
    st = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": {"laser_nm": 785, "grating": 2400, "filter": "1%",
          "objective": "x100"}, "acquire": {"center_cm1": 1580, "exposure_s": 1}}
    v = api.validate({"s": st})["s"]
    assert not v["ok"] and any("cannot reach" in e for e in v["errors"])
    st["acquire"]["center_cm1"] = 520
    assert api.validate({"s": st})["s"]["ok"]
    P.save(dict(prof, gratings=dict(prof["gratings"], **{"2400": dict(prof["gratings"]["2400"], max_nm=760)})))
    assert plan.grating_reach(P.load(), 2400, 637.75)["limit_nm"] == 760
    assert any("the laser line itself" in e for e in api.validate({"s": st})["s"]["errors"])
    P.save(prof)
    S.ensure(config.active_bridge_dir())
    r = api.instrument("set", setup={"laser_nm": 785, "grating": 2400})
    assert r["reach"]["max_shift_cm1"] == 739 and "Raman shifts end near 739" in r["note"]
    # a blocking move: busy with 'blocking' and no beat for 60 s is alive; past the limit, said so
    b = bridge.Bridge(tmp_path / "br")
    (b.dir / "heartbeat.txt").write_text("state=busy\nverb=move\nblocking=Grating\n", encoding="utf-8")
    old = time.time() - 60
    os.utime(b.dir / "heartbeat.txt", (old, old))
    assert b.alive()
    monkeypatch.setenv("LS6T_BLOCKING_MOVE_S", "30")
    assert not b.alive()
    assert "blocking move of the Grating" in bridge.diagnose(b.heartbeat(), b.dir)
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    mv = br[br.index("Sub VMove("):]
    assert mv.index("gBlocking = motor") < mv.index("moveId = LabSpec.MoveMotor(motor, posVal, posName, mc)\n")


def test_spectrometer_range_from_labspec(tmp_path):
    """LabSpec's SpMax per grating is nm on the XploRA (600 1138, 1200 1112, 1800 935, 2400 690): discovery records
    it, studies beyond it are refused, set notes it (and never refuses a pair LabSpec accepts)."""
    S.ensure(config.active_bridge_dir())
    r = api.instrument("discover")
    prop = json.loads(Path(r["proposed_profile"]).read_text())
    assert prop["gratings"]["2400"]["spectro_nm"] == [0.0, 690.0] and prop["gratings"]["600"]["spectro_nm"][1] == 1138
    prof = P.load()
    P.save(dict(prof, gratings=prop["gratings"]))
    prof = P.load()
    assert plan.grating_reach(prof, 2400, 785.04)["max_shift_cm1"] is None
    assert plan.grating_reach(prof, 2400, 637.75)["max_shift_cm1"] == 1187
    assert plan.grating_reach(prof, 1800, 785.04)["max_shift_cm1"] == 2043
    assert plan.grating_reach(prof, 1200, 785.04)["basis"] == "LabSpec's SpMax for this grating"
    gr = P.summary(prof)["grating_reach"]                  # what study_format shows
    assert gr["2400"]["limit_nm"] == 690 and gr["2400"]["from"] == "LabSpec SpMax" and "min_nm" not in gr["2400"]
    assert gr["2400"]["max_cm1_at_785"] is None and gr["1800"]["max_cm1_at_785"] == 2044    # sim laser 785.0
    st = {"kind": "spectrum", "sample": {"name": "Si"}, "setup": {"laser_nm": 785, "grating": 2400, "filter": "1%",
          "objective": "x100"}, "acquire": {"center_cm1": 520, "exposure_s": 1}}
    v = api.validate({"s": st})["s"]
    assert not v["ok"] and any("SpMax" in e for e in v["errors"])
    r = api.instrument("set", setup={"laser_nm": 785, "grating": 2400})
    assert r["ok"] and "reaches no Raman shift" in r["note"]


def test_inventory_facts_reach_a_persons_profile_and_dark_timing(tmp_path, monkeypatch):
    """A person's (not reviewed) profile gets LabSpec's spectrometer ranges from the inventory without a review; a
    background with the laser blocked by the operator runs without a reviewed profile and times the acquisition."""
    S.ensure(config.active_bridge_dir())
    assert api.instrument("discover")["ok"]
    dump = sorted(config.home().glob("discovery_2*.txt"))[-1]
    (config.home() / "discovery_latest.txt").write_text(dump.read_text())
    prof = P.load()
    for g in prof["gratings"].values():
        g.pop("spectro_nm", None)
    prof.pop("auto", None)
    P.save(dict(prof, video=dict(prof["video"], stop_from_script=True)))      # a person's, not reviewed
    prof = P.load()
    assert not prof.get("reviewed") and prof["gratings"]["2400"]["spectro_nm"] == [0.0, 690.0]
    assert prof["facts_from_inventory"]["spectro_nm"]
    monkeypatch.setenv("LS6T_MODE", "labspec")
    bg = {"kind": "background", "sample": {"name": "dark"}, "setup": {"laser_nm": 532, "grating": 1800},
          "acquire": {"center_cm1": 520, "exposure_s": 1}, "background": {"name": "dark_t316", "laser_off": "operator"}}
    v = api.validate({"b": bg})["b"]          # blocked only by hand, profile not reviewed: strongest attenuation
    assert not v["ok"] and any("strongest attenuation" in e for e in v["errors"])
    bg["setup"]["filter"] = "0.1%"
    v = api.validate({"b": bg})["b"]
    assert v["ok"], v["errors"]
    assert any("someone at the instrument" in w for w in v["warnings"])
    assert "PHYSICALLY BLOCKED" in runner.BLOCK_BY_HAND and "remote session" in runner.BLOCK_BY_HAND
    monkeypatch.setenv("LS6T_REMOTE_SESSION", "1")             # a Remote Desktop session: refused
    v = api.validate({"b": bg})["b"]
    assert not v["ok"] and any("Remote Desktop" in e for e in v["errors"])
    monkeypatch.setenv("LS6T_HAND_BLOCK_REMOTE_OK", "1")
    assert api.validate({"b": bg})["b"]["ok"]
    monkeypatch.delenv("LS6T_HAND_BLOCK_REMOTE_OK")
    monkeypatch.setenv("LS6T_REMOTE_SESSION", "0")
    spec = dict(bg, kind="spectrum")
    spec.pop("background")
    assert any("reviewed" in e for e in api.validate({"s": spec})["s"]["errors"])
    monkeypatch.setenv("LS6T_MODE", "sim")
    st = run_inline({"b1": bg, "b2": dict(bg, background={"name": "dark_t316b", "laser_off": "operator"})})
    for k in ("b1", "b2"):
        assert st["studies"][k]["seconds"]["acquire"] is not None


def test_spmin_not_applied_and_warmup(tmp_path):
    prof = P.load()
    P.save(dict(prof, gratings={k: dict(v, spectro_nm=[100.0, 935.0]) for k, v in prof["gratings"].items()}))
    assert plan.grating_min_nm(P.load(), 1800) is None                      # SpMin 100: shown, not applied
    assert P.summary(P.load())["grating_reach"]["1800"]["labspec_spmin"] == 100.0
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    main = br[br.index("Sub BridgeMain("):]
    assert main.index("DiscoverTo LS6T_HOME_DIR") < main.index("WarmUp") < main.index('Beat "idle", True')
    wu = br[br.index("Sub WarmUp("):]
    wu = wu[:wu.index("End Sub")]
    assert "GetMotorStatus(nm, gNoId)" in wu and "MoveMotor" not in wu and 'res("warmup") = gWarm' in br


def test_halt_window_ends_the_bridge_and_second_start(tmp_path):
    """2026-11-20: Cancel halted the bridge, its window went away, and a second start seemed to do nothing."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    assert "Halt = the acquisition" in br and "End bridge = ends it" in br
    op = br[br.index("Function OperatorStop()"):]
    op = op[:op.index("End Function")]
    assert op.index("If gHaltPopup Then") < op.index('OperatorStop = "quit"           \' the halted window closed')
    main = br[br.index("Sub BridgeMain()"):]
    assert main.index("other = OtherBridge()") < main.index("EnsureFolder LS6T_BRIDGE_DIR")
    assert "If gHalted And Not gPopupOpen Then OpenHaltedPopup" in main
    rs = br[br.index("Sub VResume("):]
    rs = rs[:rs.index("End Sub")]
    assert rs.index("ClosePopup") < rs.index("OpenMainPopup True")


def test_windows_titles_resume_text_and_startup_wait(tmp_path):
    """T2.3 on .43: the windows were titled 'Non blocking', the reopened window after resume looked like a new
    request, Cancel seemed not to work until LabSpec's call ended, and status timed out during a 51 s warm-up."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    assert '"#ls6t: in control#Halt"' in br and '"#ls6t: HALTED#End bridge"' in br
    assert "ls6t RESUMED" in br and "OpenMainPopup True" in br
    sr = br[br.index("Function StopRequested()"):]
    sr = sr[:sr.index("End Function")]
    assert "OpenHaltedPopup" in sr                     # at once, during a command
    wu = br[br.index("Sub WarmUp("):]
    wu = wu[:wu.index("End Sub")]
    assert "Elapsed(t0) > 120" in wu and "OperatorStop()" in wu
    # a command sent while the bridge warms up waits for it instead of timing out
    b = bridge.Bridge(tmp_path / "br")
    hb = b.dir / "heartbeat.txt"
    hb.write_text("state=busy\nverb=warmup\n", encoding="utf-8")
    import threading

    def answer():
        time.sleep(1.5)
        cmd = sorted(b.inbox.glob("*.cmd"))[0]
        seq = cmd.stem
        (b.outbox / f"{seq}.res").write_text(f"seq={seq}\nok=1\nerror=\n", encoding="utf-8")

    def beat():
        for _ in range(20):
            hb.write_text("state=busy\nverb=warmup\n", encoding="utf-8")
            time.sleep(0.1)
    threading.Thread(target=beat, daemon=True).start()
    threading.Thread(target=answer, daemon=True).start()
    r = b.call("status", timeout_s=0.5)
    assert r["ok"] and r["waited_for_bridge_start_s"] > 0


def test_resume_reports_where_the_time_went(tmp_path, monkeypatch):
    """.42/.43: resume took 58.8 and 99.6 s while the bridge was idle and halted."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    rs = br[br.index("Sub VResume("):]
    rs = rs[:rs.index("End Sub")]
    assert rs.index("ClosePopup") < rs.index('res("prompt_shown_after_s")') < rs.index("LS_MB_YESNO") \
        < rs.index('res("answer_after_s")')
    assert "OpenHaltedPopup" in rs                     # on No, the HALTED window comes back
    S.ensure(config.active_bridge_dir())
    (config.active_bridge_dir() / "sim_operator_stop").write_text("1")
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"},
                           "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%", "objective": "x100"},
                           "acquire": {"center_cm1": 520, "exposure_s": 5}}})
    calls = []
    monkeypatch.setattr(bridge, "labspec_windows", lambda: calls.append(1) or {"running": True, "windows": [],
                                                                            "dialogs": ["ls6t: HALTED", "LabSpec"]})
    r = api.instrument("resume")
    assert r["answer"] == "yes" and {"prompt_shown_after_s", "answer_after_s", "picked_up_after_s"} <= set(r)


def test_warmup_logged_once_per_bridge_session(tmp_path):
    """The slow first grating status (36, 51 s after bridge restarts in a running session; 0 s after a fresh login):
    each bridge session's warm-up is logged once, with its context."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    wu = br[br.index("Sub WarmUp("):]
    assert 'gWarm = "started="' in wu and '"grating_at=" & MotorValue("Grating")' in wu
    S.ensure(config.active_bridge_dir())
    api.instrument("set", setup={"grating": 600})
    a = api.instrument("status")["warmup"]
    b = api.instrument("status")["warmup"]
    assert a == b and a["motors_s"]["Grating"] == 0 and a["grating_at"] and a["last_grating_set"]
    lines = (config.home() / "warmup_log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert isinstance(bridge.session_context(), dict)


def test_installer_ends_a_running_bridge_cleanly(tmp_path):
    """Every slow first grating status followed a deploy: the installer now ends a running bridge cleanly first and
    logs it; the warm-up log names the last deploy."""
    from labspec_translator import winsetup as W

    class L:
        def __init__(self):
            self.lines = []

        def __call__(self, m):
            self.lines.append(m)

        def warn(self, m):
            self.lines.append("WARN " + m)
    S.ensure(config.active_bridge_dir())
    rec = W.end_running_bridge(L())
    assert rec["bridge_was"] in ("idle", "busy") and rec["ended"] in ("cleanly", "asked: True")
    rec2 = W.end_running_bridge(L())
    assert rec2["ended"] == "not running"
    assert len((config.home() / "deploy_log.jsonl").read_text().splitlines()) == 2
    S.ensure(config.active_bridge_dir())
    w = api.instrument("status")["warmup"]
    assert w["last_deploy"]["ended"] == "not running"


def test_durations_plain_decimals(tmp_path):
    """prompt_shown_after_s came back as '1.953125E-02' (VBScript's CStr)."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    assert "Fmt(Elapsed(" not in br and "Secs = Fmt(Round(CDbl(x), 2))" in br
