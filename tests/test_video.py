"""Tests: video snapshots."""
from helpers import *  # noqa: F401,F403


# --------------------------------------------------------------------------------------------- video snapshots



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


def test_calibration_video_before_is_off_by_default(tmp_path):
    _profile_with_shutter(tmp_path)
    cal = {"kind": "calibration", "sample": {"name": "Si"}, "setup": SETUP,
           "acquire": {"exposure_s": 1, "accumulations": 2}}
    # default: no video frame before calibration (saves ~17-20 s per calibration)
    assert plan.estimate(cal, P.load())["seconds"]["video_snapshots"] == 0.0
    st = run_inline({"si": dict(cal, video={"after": True, "with_laser": True})})
    r = st["studies"]["si"]
    assert r["reliable"], (r["unreliable_because"], r["warnings"])
    assert set(r["video"]) == {"after"}
    # explicit video.before or profile calibration.video_before=True enables it
    st2 = run_inline({"si": dict(cal, video={"before": True, "after": True, "with_laser": True})})
    r2 = st2["studies"]["si"]
    assert r2["reliable"], (r2["unreliable_because"], r2["warnings"])
    assert set(r2["video"]) == {"before", "after"} and Path(r2["images"]["video_before"]["path"]).exists()
    assert r2["video"]["before"]["measured_offset_um"][0] == pytest.approx(1.5, abs=0.1)
    P.save(dict(P.load(), calibration=dict(P.load()["calibration"], video_before=True)))
    assert plan.estimate(cal, P.load())["seconds"]["video_snapshots"] > 0.0
    st3 = run_inline({"si": cal})
    assert set(st3["studies"]["si"]["video"]) == {"before"}


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
