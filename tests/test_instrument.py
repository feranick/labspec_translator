"""Tests: instrument checks and the fixes from the instrument sessions since."""
from helpers import *  # noqa: F401,F403


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
    # one run: no proposal yet (the mean of 3 runs is offered); the filters not measured say why
    assert r["filters"]["profile_command"] is None and "repeat the study" in r["filters"]["profile_command_note"]
    sk = {d["label"]: d["why"] for d in r["filters"]["skipped"]}
    assert "max_exposure_s" in sk["1%"] and "max_exposure_s" in sk["0.1%"]
    assert any("filter 1% not measured" in w for w in r["warnings"])
    for _ in range(2):
        st2 = run_inline({"f": {"kind": "filters", "sample": {"name": "Si"},
                                "setup": {"laser_nm": 532, "grating": 1800, "objective": "x100"},
                                "filters": {"exposure_s": 0.2, "max_exposure_s": 5}}})
    r2 = st2["studies"]["f"]["filters"]
    assert "ls6t profile set filters" in r2["profile_command"] and r2["average"]["50%"]["runs"] == 3
    # the reference again at the end (drift), and each step's exposure as LabSpec used it (2026-11-23)
    assert r["checks"]["reference_repeat"]["ratio_to_first"] == pytest.approx(1.0, abs=0.1)
    assert steps["50%"]["exposure_used_s"] == pytest.approx(0.4) and steps["50%"]["filter_read_back"] == "50%"
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
               "GrabFrame",                # only once the video runs
               "VidKeep"}                  # gated by gVidOff and gVidOwn (a video the bridge started)
    assert set(where) <= allowed, set(where) - allowed
    vk = br[br.index("Sub VidKeep("):]
    assert "If Not gVidOwn Or gVidOff Then Exit Sub" in vk[:vk.index("End Sub")]
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
    assert runner.move_extra(prof, prof["motors"]["grating"]) == {"until_target": 1, "until_target_s": 120, "until_target_stable_s": 10}   # blocking off (.37); read until there
    P.save(dict(prof, blocking_motors=["grating"]))
    prof = P.load()
    assert runner.move_extra(prof, prof["motors"]["grating"]) == {"blocking": 1, **{"until_target": 1, "until_target_s": 120, "until_target_stable_s": 10}}
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


def test_stopped_request_reports_where_the_stage_is(tmp_path):
    """2026-11-22: after a halt the stage stays where it was (right), but the answer did not say where, nor that the
    return home did not happen; moves did not say which objective they assumed; max_xy_um read as per axis."""
    S.ensure(config.active_bridge_dir())
    (config.active_bridge_dir() / "sim_operator_stop_after").write_text("1")
    st = run_inline({"m1": {"kind": "move", "move": {"x_um": 30, "y_um": 0, "stay": False}},
                     "s": {"kind": "spectrum", "sample": {"name": "Si"},
                           "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%", "objective": "x100"},
                           "acquire": {"center_cm1": 520, "exposure_s": 1}},
                     "m2": {"kind": "move", "move": {"x_um": 60, "y_um": 0}}})
    if st["state"] == "cancelled":
        assert st["returned"]["done"] is False and ("stays" in st["returned"]["why"] or
                                                    "at the home spot" in st["returned"]["why"])
        assert "x_um" in st["stage_at_end"]
    f = config.active_bridge_dir() / "sim_operator_stop_after"
    if f.exists():
        f.unlink()
    api.instrument("resume")
    st = run_inline({"m": {"kind": "move", "move": {"x_um": 20, "y_um": 0, "stay": False}}})
    assert st["returned"]["done"] is True and st["stage_at_end"]["from_home_um"] == [0.0, 0.0]
    mv = st["studies"]["m"]["move"]
    assert mv["objective"] and mv["objective_source"].startswith("LabSpec's setting")
    v = api.validate({"mp": {"kind": "map", "sample": {"name": "x"}, "setup": {"laser_nm": 532, "grating": 1800,
                      "filter": "1%", "objective": "x100"}, "acquire": {"center_cm1": 520, "exposure_s": 0.1},
                      "map": {"x_um": [-4000, 4000, 4000], "y_um": [-4000, 4000, 4000]}}})["mp"]
    assert not v["ok"] and any("not per axis" in e for e in v["errors"])
    assert "not per axis" in P.summary(P.load())["limits_meaning"]["max_xy_um"]


def test_z_safety_per_objective_direction_and_stale_focus(tmp_path):
    """Toward the sample, Z is limited to a fraction of the objective's working distance (both ways until the Z
    direction is known); the focus expires after a large XY move with a short working distance; a stated sample
    unevenness keeps x100 moves within that distance."""
    prof = P.load()
    zl = plan.z_limits(prof, "x100_VIS")
    assert zl["working_distance_um"] == 210 and zl["toward_um"] == 21.0 and zl["toward_sign"] is None
    assert plan.z_check(zl, 15) is None and "T3.17" in plan.z_check(zl, -30)        # unknown direction: both ways
    P.save(dict(prof, stage=dict(prof["stage"], z_into_sample="+")))
    prof = P.load()
    zl = plan.z_limits(prof, "x100_VIS")
    assert "toward the sample" in plan.z_check(zl, 30) and plan.z_check(zl, -30) is None
    assert plan.z_limits(prof, "x10_VIS")["toward_um"] == prof["limits"]["max_dz_um"]       # long WD: max_dz
    assert plan.z_limits(prof, "x10_VIS")["focus_valid_xy_um"] is None
    base = {"sample": {"name": "film"}, "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%",
            "objective": "x100"}, "acquire": {"center_cm1": 520, "exposure_s": 0.5}}
    v = api.validate({"d": dict(base, kind="map", map={"x_um": [0, 2, 1], "z_um": [-5, 40, 5]})})["d"]
    assert not v["ok"] and any("toward the sample" in e for e in v["errors"])
    v = api.validate({"d": dict(base, kind="map", map={"x_um": [0, 2, 1], "z_um": [-40, 10, 5]})})["d"]
    assert v["ok"], v["errors"]                                                       # away: up to max_dz
    s = dict(base, kind="map", map={"x_um": [0, 1000, 500]})
    s["sample"] = {"name": "rough", "unevenness_um": 150}
    v = api.validate({"m": s})["m"]
    assert not v["ok"] and any("unevenness" in e for e in v["errors"])
    s["sample"] = {"name": "rough", "unevenness_um": 20}
    assert api.validate({"m": s})["m"]["ok"]
    br = runner.move_z.__code__.co_varnames
    assert "focusing" in br


def test_stale_focus_stops_z_toward_the_sample_at_run_time():
    prof = P.load()
    P.save(dict(prof, stage=dict(prof["stage"], z_into_sample="+")))
    S.ensure(config.active_bridge_dir())
    st = run_inline({"m": {"kind": "map", "sample": {"name": "x"},
                           "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%", "objective": "x100"},
                           "acquire": {"center_cm1": 520, "exposure_s": 0.1},
                           "map": {"x_um": [0, 600, 600], "z_um": [0, 10, 10]}}})
    txt = json.dumps(st["studies"]["m"])
    assert "needs a fresh focus here first" in txt


def test_inputs_without_review_window_nm_and_setup_breakdown(monkeypatch):
    """get_inputs sends nothing: an unreviewed profile is a note there; a single window shows the nm it covers; the
    setup estimate has its parts."""
    st = {"kind": "calibration", "sample": {"name": "Si"},
          "setup": {"laser_nm": 637.75, "grating": 1800, "filter": "1%", "objective": "x100"}}
    prof = P.load()
    P.save(dict(prof, lasers={"637.75": {"nm": 637.75, "label": "637.75", "power_mw": None, "max_filter_pct": 10},
                              **prof["lasers"]}))
    monkeypatch.setenv("LS6T_MODE", "labspec")
    assert not api.validate({"c": st})["c"]["ok"]                     # the study itself: refused until reviewed
    r = api.inputs(st, "c")
    assert r["ok"] and "reviewed" in r["review_note"]
    rng = r["range"]
    assert rng["from_nm"] == rng["to_nm"] == rng["center_nm"]
    lo, hi = rng["window_nm"]
    assert 646 < lo < 647.5 and 672 < hi < 673.5
    sec = r["estimate"]["seconds"]
    assert set(sec["setup_parts"]) >= {"laser", "grating", "objective", "filter", "commands"}
    assert abs(sum(sec["setup_parts"].values()) - sec["setup"]) < 0.2
    assert "operator" in r["estimate"]["not_included"]


def test_depth_profiles_get_their_own_confirmed_z_limit():
    prof = P.load()
    P.save(dict(prof, stage=dict(prof["stage"], z_into_sample="+")))
    d = {"kind": "depth", "sample": {"name": "film"},
         "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%", "objective": "x100"},
         "acquire": {"center_cm1": 1000, "exposure_s": 0.1},
         "depth": {"from_um": -5, "to_um": 50, "step_um": 5}}
    assert api.validate({"d": d})["d"]["ok"]                    # 50 µm: above 21 (ordinary), within 63 (depth)
    d2 = dict(d, depth={"from_um": -5, "to_um": 80, "step_um": 5})
    v = api.validate({"d": d2})["d"]
    assert not v["ok"] and any("depth_toward_wd_fraction" in e for e in v["errors"])
    m = {"kind": "map", "sample": {"name": "film"}, "setup": d["setup"], "acquire": d["acquire"],
         "map": {"z_um": [0, 50, 5]}}
    assert any("z_toward_wd_fraction" in e for e in api.validate({"m": m})["m"]["errors"])
    S.ensure(config.active_bridge_dir())
    st = run_inline({"d": d})
    assert st["state"] == "done", st
    (config.active_bridge_dir() / "sim_confirm_no").write_text("1")
    st = run_inline({"d": d})
    assert "did not confirm the depth profile" in json.dumps(st)


def test_stop_messages_say_where_and_how_to_resume():
    S.ensure(config.active_bridge_dir())
    (config.active_bridge_dir() / "sim_operator_stop").write_text("1")
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"},
                           "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%", "objective": "x100"},
                           "acquire": {"center_cm1": 520, "exposure_s": 1}}})
    assert st["returned"]["needed"] is False and "at the home spot" in st["returned"]["why"]
    st = run_inline({"m": {"kind": "move", "move": {"x_um": 10, "y_um": 0}}})       # the bridge is halted
    assert "ls6t instrument resume" in st["why"]
    assert st["stage_at_end"]["from_home_um"] == [0.0, 0.0] and "nothing moved" in st["returned"]["why"]


def test_retract_travel_return_and_focus_from_below():
    """Nicola: an optional retract-travel-return for large XY moves on uneven samples, then a focus search from
    below; also a retract before a manual objective change."""
    prof = P.load()
    st = {"kind": "move", "sample": {"name": "rough", "unevenness_um": 30}, "move": {"x_um": 1000, "y_um": 0}}
    v = api.validate({"m": st})["m"]
    assert not v["ok"] and any("T3.17" in e for e in v["errors"])          # needs the Z direction
    P.save(dict(prof, stage=dict(prof["stage"], z_into_sample="+")))
    v = api.validate({"m": st})["m"]
    assert v["ok"], v["errors"]
    assert any("retract-travel-return" in w for w in v["warnings"])
    S.ensure(config.active_bridge_dir())
    z0 = float(api.instrument("status")["status"]["z_um"])
    r = run_inline({"m": dict(st, move=dict(st["move"], snapshot=False)),
                    "f": {"kind": "focus", "sample": {"name": "rough"},
                          "setup": {"laser_nm": 532, "grating": 1800, "filter": "1%", "objective": "x100"},
                          "focus": {"scan": {"from_below": True, "step_um": 0.5}}}})
    tr = r["studies"]["m"]["move"]["travel"]
    assert tr["mode"] == "retract" and abs(tr["retracted_to_um"] - (z0 - 2000)) < 1e-6
    assert abs(tr["descended_to_um"] - (z0 - 30)) < 1e-6
    f = r["studies"]["f"]["focus"]
    assert f["from_below"] is True and f["coarse_step_um"] <= 2.0 and f["found"], f
    assert abs(f["z_um"] - z0) < 1.0                                       # the sim's focus: where it was
    assert plan.coarse_focus_step_um(prof, "x10_VIS") == 25.0
    # retract before a manual objective change (opt-in)
    P.save(dict(P.load(), stage=dict(P.load()["stage"], retract_for_objective_change=True)))
    zb = float(api.instrument("status")["status"]["z_um"])
    r = api.instrument("set", setup={"objective": "x10"})
    assert r["ok"] and abs(float(api.instrument("status")["status"]["z_um"]) - (zb - 2000)) < 1e-6


def test_a_heartbeat_locked_for_a_moment_is_not_a_gone_bridge(tmp_path, monkeypatch):
    """CI (Windows): 'the bridge ended without stopping (last heartbeat idle 0 s ago)': a read of the heartbeat that
    failed while the file was being replaced was taken for a missing bridge."""
    b = bridge.Bridge(tmp_path / "br")
    (b.dir / "heartbeat.txt").write_text("state=idle\nverb=\n", encoding="utf-8")
    real = bridge.read_text
    fails = {"n": 2}

    def flaky(path, *a, **k):
        if str(path).endswith("heartbeat.txt") and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("locked")
        return real(path, *a, **k)
    monkeypatch.setattr(bridge, "read_text", flaky)
    hb = b.heartbeat()
    assert hb and hb["state"] == "idle" and b.alive()


def test_retract_travel_return_with_video_autofocus_no_laser():
    """Nicola: finish the approach with LabSpec's video autofocus (no laser), so the sequence runs before the profile
    is reviewed; ls6t checks where LabSpec's autofocus ended."""
    prof = P.load()
    P.save(dict(prof, stage=dict(prof["stage"], z_into_sample="+")))
    st = {"kind": "move", "sample": {"name": "rough", "unevenness_um": 30},
          "move": {"x_um": 1000, "y_um": 0, "snapshot": False, "focus": "video"}}
    assert api.validate({"m": st})["m"]["ok"]
    bad = dict(st, sample={"name": "flat"})
    assert any("only with a retract-travel-return" in e for e in api.validate({"m": bad})["m"]["errors"])
    S.ensure(config.active_bridge_dir())
    z0 = float(api.instrument("status")["status"]["z_um"])
    # with the assumed (cautious) Z speed and stop time, x100 leaves no room for the overshoot: refused
    assert plan.af_motion(P.load())["z_speed_um_s_from"] == "assumed"
    r = run_inline({"m": st})
    tr = r["studies"]["m"]["move"]["travel"]
    assert tr["video_autofocus"]["skipped"] == "no safe room"
    assert any("overshoot margin" in w for w in r["studies"]["m"]["warnings"])
    # measured (T6.8 with a long working distance): a small margin, the trip point set earlier by it
    (config.home() / "autofocus_motion.json").write_text(json.dumps({"z_speed_um_s": 50, "stop_s": 0.2,
                                                                     "poll_s": 0.12, "runs": 1}))
    mo = plan.af_motion(P.load())
    assert mo["z_speed_um_s_from"] == "measured" and mo["margin_um"] == 20.0
    r = run_inline({"m": dict(st, move=dict(st["move"], x_um=0, y_um=1000))})
    tr = r["studies"]["m"]["move"]["travel"]
    va = tr["video_autofocus"]
    assert va["accepted"] and abs(va["z_um"] - z0) < 1.5
    assert abs((va["safe_limit_um"] - va["z_bound_um"]) - 20.0) < 1e-6         # tripped 20 µm before the limit
    assert tr["video_autofocus"]["z_seen_um"][0] is not None and not tr["video_autofocus"]["stopped_at_bound"]
    (config.active_bridge_dir() / "sim_af_range").write_text("300")     # LabSpec's search reaches too far
    r = run_inline({"m": dict(st, move=dict(st["move"], x_um=-1000))})
    tr = r["studies"]["m"]["move"]["travel"]
    assert tr["video_autofocus"]["stopped_at_bound"] and not tr["video_autofocus"]["ok"]
    assert any("safe bound" in w for w in r["studies"]["m"]["warnings"])
    (config.active_bridge_dir() / "sim_af_range").unlink()
    (config.active_bridge_dir() / "sim_defocus").write_text("200")      # LabSpec's AF ends far away: refused
    r = run_inline({"m": dict(st, move=dict(st["move"], x_um=500))})
    tr = r["studies"]["m"]["move"]["travel"]
    assert tr["video_autofocus"]["accepted"] is False
    assert any("safe bound" in w or "video autofocus ended" in w for w in r["studies"]["m"]["warnings"])


def test_settings_already_in_place_are_not_sent_nor_charged():
    """2026-11-22: a dry run listed (and charged 20 s for) the objective already mounted; at run time a manual
    objective already in place must never be asked for again."""
    S.ensure(config.active_bridge_dir())
    cur = api.instrument("status")["status"]
    st = {"kind": "calibration", "sample": {"name": "Si"},
          "setup": {"laser_nm": float(cur["laser_nm"]), "grating": 600, "filter": cur["filter"],
                    "objective": cur["objective"]}}
    r = api.inputs(st, "c")
    assert r["state_read"]
    marks = {s.get("role"): s.get("already_set", False) for s in r["steps"] if s.get("role")}
    assert marks == {"filter": True, "laser_nm": True, "grating": False, "objective": True}
    parts = r["estimate"]["seconds"]["setup_parts"]
    assert parts["objective"] == 0 and parts["laser"] == 0 and parts["filter"] == 0 and parts["grating"] > 0
    run = run_inline({"c": st})
    moves = run["studies"]["c"]["checks"]["setup"]["moves"]
    assert {m["motor"] for m in moves if m.get("already_set")} >= {"Objective", "Laser", "Filter"}
    assert not any(m.get("already_set") for m in moves if m["motor"] == "Grating")


def test_set_lists_the_objective_already_set_and_grating_note_only_on_change():
    S.ensure(config.active_bridge_dir())
    cur = api.instrument("status")["status"]
    r = api.instrument("set", setup={"objective": cur["objective"], "grating": float(cur["grating"])})
    sets = {m["set"]: m.get("already_set") for m in r["moves"]}
    assert sets == {"objective": True, "grating": True} and r["ok"]
    assert r["objective"]["source"].startswith("LabSpec's setting")          # nothing changed, nobody asked
    assert "LabSpec reports a new grating" not in (r.get("note") or "")
    r = api.instrument("set", setup={"grating": 600 if float(cur["grating"]) != 600 else 1200})
    assert "LabSpec reports a new grating" in r["note"]


def test_orientation_from_labspecs_image_and_video_scale(tmp_path):
    """2026-11-22, the XploRA's first real snapshot: ls6t's frame had its rows reversed and red/blue swapped against
    LabSpec's own JPEG, and its µm scale was LabSpec's 1 'µm' per pixel; a known stage move measures the real one."""
    from labspec_translator import video as V
    from PIL import Image as PILImage
    H, W = 60, 80
    rng = np.random.default_rng(1)
    shown = rng.integers(0, 255, (H, W, 3)).astype(np.uint8)             # as LabSpec shows it
    shown[10:14, 50:54] = [250, 20, 20]                                     # a red particle near the top
    PILImage.fromarray(shown).save(tmp_path / "frame.jpg", quality=95)
    stored = shown[::-1, :, ::-1]                                           # rows reversed, red/blue swapped
    dec = {"img": stored, "xs": np.arange(W, dtype=float), "ys": np.arange(H, dtype=float), "um": True,
           "width": W, "height": H, "channels": 3, "meta": {}}
    ori = V.detect_orientation(dec, tmp_path / "frame.jpg")
    assert ori["flip_y"] and not ori["flip_x"] and ori["swap_rb"] and ori["score"] > 0.8
    fixed = V.apply_orientation(dec, ori)
    assert (fixed["img"] == shown).all() and fixed["ys"][0] == H - 1
    # the stage scale measured with known moves (the simulator's x100: 0.4 µm per pixel)
    S.ensure(config.active_bridge_dir())
    r = api.instrument("video_scale", um=5)
    assert r["ok"] and abs(abs(r["um_per_px_x"]) - 0.4) < 0.02 and abs(abs(r["um_per_px_y"]) - 0.4) < 0.02
    assert r["stored"] in V.scale_store()
    snap = api.snapshot()
    assert snap["ok"] and snap["axes_from"].startswith("measured")


def test_no_labspec_prompt_after_ls6t_asked_and_black_frame(tmp_path):
    """2026-11-22, T3.2: LabSpec asked a second time during the objective move after ls6t's own question (MOTOR_NO_MESSAGE
    now goes with it); a black x5 frame gave five 'features' of strength 1e9 (noise ~0 from the filters)."""
    from labspec_translator import video as V
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    mv = br[br.index("Sub VMove("):]
    mv = mv[:mv.index("End Sub")]
    assert "mc = mc + LS_MOTOR_NO_MESSAGE" in mv and 'res("no_message_refused") = 1' in mv
    prof = P.load()
    m = prof["motors"]
    assert runner.move_extra(prof, m["objective"], True) == {"no_message": 1}
    assert runner.move_extra(prof, m["objective"], False) == {}
    assert runner.move_extra(prof, m["grating"], True) == {"until_target": 1, "until_target_s": 120, "until_target_stable_s": 10}      # no_message: only the objective
    assert runner.move_extra(dict(prof, suppress_labspec_prompt=False), m["objective"], True) == {}
    S.ensure(config.active_bridge_dir())
    r = api.instrument("set", setup={"objective": "x50"})
    mv = [d for d in r["moves"] if d["set"] == "objective"][0]
    assert r["ok"] and mv["labspec_prompt"].startswith("suppressed")
    # black and flat frames: no features
    H, W = 120, 160
    for img in (np.zeros((H, W, 3), np.uint8), np.full((H, W, 3), 3, np.uint8)):
        img = img.copy(); img[60, 80] = 4
        dec = {"img": img, "xs": np.linspace(0, 1700, W), "ys": np.linspace(0, 1170, H), "um": True,
               "width": W, "height": H, "channels": 3, "meta": {}}
        assert V.frame_too_dark(V._lum(img)) and V.features(dec) == []
    # a real speck on a lit, nearly uniform frame keeps a sane strength
    rng = np.random.default_rng(0)
    img = (120 + rng.normal(0, 2.0, (H, W))).clip(0, 255)        # a lit frame with camera noise
    img[58:62, 78:82] = 200
    dec = {"img": img, "xs": np.linspace(0, 160, W), "ys": np.linspace(0, 120, H), "um": True,
           "width": W, "height": H, "channels": 1, "meta": {}}
    f = V.features(dec)
    assert f and f[0]["strength"] < 1e5 and abs(f[0]["x_um"] - 80) < 3


def test_orientation_kept_from_dark_frames_scale_step_marker_assumed(tmp_path):
    """2026-11-22 review: a black frame must not overwrite the orientation remembered for the camera (nor a grey one
    the colour swap); video_scale's default move is a fifth of the field (50 µm left the x100's ~85 µm view), given
    with --um; the marker says it is assumed until a spot study measured the beam; LabSpec's own prompt is
    suppressed only for the objective move just confirmed in ls6t's question."""
    from labspec_translator import video as V
    from PIL import Image as PILImage
    H, W = 60, 80
    rng = np.random.default_rng(2)
    shown = rng.integers(0, 255, (H, W, 3)).astype(np.uint8)
    PILImage.fromarray(shown).save(tmp_path / "lit.jpg", quality=95)
    lit = {"img": shown[::-1, :, ::-1], "xs": np.arange(W, dtype=float), "ys": np.arange(H, dtype=float),
           "um": True, "width": W, "height": H, "channels": 3, "meta": {}}
    ori, why = V.orientation_for(lit, tmp_path / "lit.jpg", 7)
    assert why is None and ori["flip_y"] and ori["swap_rb"]
    assert V._orientation_store()["7"]["flip_y"] is True
    # a black frame with its (black) JPEG: nothing learnt, the stored one applied
    black = np.zeros((H, W, 3), np.uint8)
    PILImage.fromarray(black).save(tmp_path / "black.jpg")
    dk = dict(lit, img=black)
    ori, why = V.orientation_for(dk, tmp_path / "black.jpg", 7)
    assert ori["flip_y"] and ori["swap_rb"] and "dark or flat" in ori["reference"]
    assert V._orientation_store()["7"]["swap_rb"] is True
    # a grey (colourless) structured frame: flips learnt, the swap kept
    grey = np.repeat(rng.integers(0, 255, (H, W, 1)).astype(np.uint8), 3, axis=2)
    PILImage.fromarray(grey).save(tmp_path / "grey.jpg", quality=95)
    ori, why = V.orientation_for(dict(lit, img=grey[::-1]), tmp_path / "grey.jpg", 7)
    assert ori["flip_y"] and ori["swap_rb"] is True and ori["swap_from"].startswith("kept")
    # nothing remembered for another camera and a dark frame: not applied, with a warning
    ori, why = V.orientation_for(dk, tmp_path / "black.jpg", 9)
    assert ori is None and "not decided" in why
    # video_scale: default a fifth of the field; over half refused before anything moves
    S.ensure(config.active_bridge_dir())
    st0 = api.instrument("status")["status"]
    r = api.instrument("video_scale")
    assert r["ok"] and r["step_from"].startswith("0.4 x the field's smaller side"), r
    r2 = api.instrument("video_scale", um=400)
    assert not r2["ok"] and ("over half the field" in r2["error"] or "1 to" in r2["error"])
    st1 = api.instrument("status")["status"]
    assert abs(float(st1["x_um"]) - float(st0["x_um"])) < 0.5 and abs(float(st1["y_um"]) - float(st0["y_um"])) < 0.5
    # the marker is assumed until a spot study measured the offset
    snap = api.snapshot()
    assert snap["ok"] and snap["marker_measured"] is False and snap["marker_source"].startswith("ASSUMED")
    # CLI flag
    from labspec_translator import cli
    assert "--um" in Path(cli.__file__).read_text()


def test_install_says_whether_a_bridge_ran_and_cleans_stale_claude_files(tmp_path, monkeypatch):
    """2026-11-22, install.log: installs with no running bridge logged nothing (they could not be told from a missed
    bridge), and from .24 the Store build's old Packages copy of the Claude configuration kept an ls6t entry."""
    from labspec_translator import clients as C
    from labspec_translator import winsetup as W

    class Log:
        def __init__(self):
            self.lines, self.warnings = [], []

        def __call__(self, m):
            self.lines.append(m)

        def warn(self, m):
            self.warnings.append(m)
    monkeypatch.setenv("LS6T_HOME", str(tmp_path / "home"))
    log = Log()
    rec = W.end_running_bridge(log)
    assert rec["ended"] == "not running" and any(m.startswith("no running bridge found") for m in log.lines)
    S.ensure(config.active_bridge_dir())
    log = Log()
    rec = W.end_running_bridge(log)
    assert rec["ended"] == "cleanly" and rec["bridge_version"] and any("ended (cleanly)" in m for m in log.lines)
    log = Log()
    rec = W.end_running_bridge(log)            # now stopped
    assert rec["bridge_was"] == "stopped" and any("stopped" in m and "no running bridge" in m for m in log.lines)
    assert len((tmp_path / "home" / "deploy_log.jsonl").read_text().splitlines()) == 3
    # the stale copy: any ls6t server there goes (an older Python's too); other servers stay
    stale = tmp_path / "Packages" / "Claude_x" / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"mcpServers": {
        "ls6t": {"command": r"C:\old\python.exe", "args": ["-m", "labspec_translator.mcp_server"]},
        "ls6t-sim": {"command": "other.exe", "args": ["serve"]}, "fs": {"command": "x"}}}))
    monkeypatch.setattr(C, "claude_stale_files", lambda existing_only=True: [stale])
    reps = C.clean_stale(sys.executable)
    cfg = json.loads(stale.read_text())
    assert set(cfg["mcpServers"]) == {"ls6t-sim", "fs"}
    assert reps[0]["ok"] and "removed ls6t" in reps[0]["notes"][0] and reps[0]["backup"]
    assert "no ls6t entry" not in C.clean_stale(sys.executable)[0]["notes"][0]      # ls6t-sim left: not ls6t's


def test_assumed_axis_directions_learned_from_the_camera_or_to_um_refused():
    """2026-11-23 at the instrument: without a video-scale for the objective the fallback axes put a particle at
    y = +153 µm instead of -155 (image up is stage -Y on the XploRA's top camera). The directions now come from a
    measurement on the same camera; with none, the snapshot says they are assumed and move.to_um is refused."""
    from labspec_translator import video as V
    S.ensure(config.active_bridge_dir())
    prof = P.load()
    cur = api.instrument("status")["status"]
    obj, cam = cur.get("objective"), cur.get("camera")
    P.save(dict(prof, objectives_info={obj: {"field_um": [355.2, 242.4]}}))
    prof = P.load()
    V.scale_store({})
    assert V.axes_directions(prof, obj, cam)[2] == "assumed"
    snap = api.snapshot()
    assert snap["directions_from"] == "assumed" and any("assumed" in w for w in snap["warnings"])
    st = run_inline({"m": {"kind": "move", "move": {"to_um": [1219.5, -557.8], "stay": False}}})
    assert not st["studies"]["m"]["ok"] and "assumed" in json.dumps(st)
    # a measurement with another objective on the same camera gives the directions (image up = stage -Y here)
    V.scale_store({f"other_obj@{cam}": {"sx": 0.977, "ty": -0.982}})
    gx, gy, src = V.axes_directions(prof, obj, cam)
    assert (gx, gy) == (1, -1) and src.startswith("learned from other_obj")
    dec = {"img": np.zeros((11, 21)), "xs": np.arange(21.0), "ys": np.arange(11.0), "um": True, "width": 21,
           "height": 11, "channels": 1, "meta": {}}
    d = V.stage_axes(dec, prof, obj, 100.0, 50.0, cam)
    assert d["directions_from"].startswith("learned") and d["ys"][0] < 50.0 < d["ys"][-1]     # row 0: lower Y
    assert d["xs"][0] < 100.0 < d["xs"][-1]
    st = run_inline({"m2": {"kind": "move", "move": {"to_um": [1219.5, -557.8], "stay": False}}})
    assert st["studies"]["m2"]["ok"], st["studies"]["m2"]


def test_snapshot_warns_when_much_darker_than_the_last_with_the_objective():
    """2026-11-23: after a video restart or objective change LabSpec showed bright field with the light in dark
    field; the frame's brightness against the last one with the same objective and camera catches it."""
    from labspec_translator import video as V
    V._brightness_store({})
    lit = {"img": np.full((20, 30), 120.0)}
    info, why = V.brightness_check(lit, "x10_VIS", 0)
    assert why is None and info["last_mean"] is None
    info, why = V.brightness_check({"img": np.full((20, 30), 110.0)}, "x10_VIS", 0)
    assert why is None and info["last_mean"] == 120.0
    info, why = V.brightness_check({"img": np.full((20, 30), 20.0)}, "x10_VIS", 0)
    assert why and "darker" in why and "dark field" in why
    assert V.brightness_check({"img": np.full((20, 30), 20.0)}, "x100_VIS", 0)[1] is None     # another objective
    assert "brighter" in V.brightness_check({"img": np.full((20, 30), 200.0)}, "x10_VIS", 0)[1]


def test_illumination_set_again_laserswitch_never_and_proposal_keeps_caps(tmp_path):
    """2026-11-23 at the instrument: LabSpec showed bright field with the light in dark field after a video restart or
    an objective change (IlluminatorV and uLight are its two controls); LaserSwitch turns a laser on or off; a profile
    proposal pre-filled the laser shutter and reset the 10 % caps to 100; the AutoCalibration script is from 6.1.174."""
    from labspec_translator import autocal, video as V
    sim = S.ensure(config.active_bridge_dir())
    prof = P.load()
    P.save(dict(prof, motors=dict(prof["motors"], illumination="IlluminatorV", illumination_light="uLight")))
    st = api.instrument("status")["status"]
    assert st["illumination"] == "Bright field" and st["illumination_light"] == "BrightField"
    n0 = sim.illumination_moves
    r = api.instrument("set", setup={"illumination": "bright"})       # already shown: through dark field and back
    assert r["ok"] and r["illumination"]["reasserted"] and r["illumination"]["via"] == ["Dark field", "Bright field"]
    assert sim.illumination_moves == n0 + 2
    r = api.instrument("set", setup={"illumination": "dark"})
    assert r["ok"] and r["illumination"]["got"] == "Dark field" and not r["illumination"]["reasserted"]
    r = api.instrument("set", setup={"objective": "x50"})              # an objective change sets it again
    assert r["ok"] and r["illumination"]["why"].startswith("set again") and r["illumination"]["got"] == "Dark field"
    assert "disagree" in V.illumination_mismatch("Dark field", "BrightField")
    assert V.illumination_mismatch("Bright field", "BrightField") is None and V.illumination_mismatch("Off", "x") is None
    # LaserSwitch: refused by the bridge (and the simulator)
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    mv = br[br.index("Sub VMove("):]
    assert 'If LCase(Replace(motor, " ", "")) = "laserswitch" Then' in mv[:mv.index("End Sub")]
    r = bridge.Bridge(config.active_bridge_dir()).call("move", motor="LaserSwitch", mode="label", value="On")
    assert not r["ok"] and "never moves" in r["error"]
    # the proposal: laser shutter found, not set; caps kept by line (10 %), a new laser capped
    cur = P.load()
    cur["lasers"] = {"532": dict(cur["lasers"]["532"], max_filter_pct=10), "638": dict(cur["lasers"]["638"], nm=637.75,
                                                                                         max_filter_pct=10)}
    cur["motors"]["laser_shutter"] = None
    dump = ("time=t\nmotor.Laser.present=1\nmotor.Laser.size=3\nmotor.Laser.value.0=532\nmotor.Laser.label.0=532\n"
            "motor.Laser.value.1=637.75\nmotor.Laser.label.1=638\nmotor.Laser.value.2=785\nmotor.Laser.label.2=785\n"
            "motor.LaserShutter.present=1\nmotor.LaserShutter.size=2\nmotor.LaserShutter.label.0=Off\n"
            "motor.LaserShutter.label.1=On\n")
    prop, notes = P.import_discovery(dump, base=cur)
    assert prop["motors"]["laser_shutter"] is None and prop["laser_shutter_candidate"] == "LaserShutter"
    assert {k: v["max_filter_pct"] for k, v in prop["lasers"].items()} == {"532": 10, "637.75": 10, "785": 10.0}
    assert any("785 nm is new" in n for n in notes) and any("not set" in n for n in notes)
    # an AutoCalibration script written for another LabSpec
    s = tmp_path / "AutoCalibration.vbs"
    s.write_text("' AutoCalibration for LabSpec 6.1.174\nLabSpec.Acq 0, 1, 1, 0\n")
    assert autocal.assess(s)["written_for_labspec"] == "6.1.174"
    info = autocal.info(dict(cur, discovered={"labspec_version": "6.8.1"},
                             labspec_config={"autocalibration_script": str(s)}))
    assert "6.8.1" in info["scripts"][0]["version_warning"]


def test_video_scale_step_from_the_smaller_side_height_limit_and_camera_agreement():
    """2026-11-23, x100 (85 x 58 µm): 17 µm gave a 4 % X / Y mismatch, 25 µm agreed; 35 µm was accepted and the Y
    move left the 58 µm-tall field. The default is now 0.4 x the smaller side, the limit checks the height for Y, and
    a result that disagrees with the same camera's other objectives is flagged."""
    from labspec_translator import video as V
    S.ensure(config.active_bridge_dir())
    V.scale_store({})
    r = api.instrument("video_scale")
    assert r["ok"] and r["step_from"].startswith("0.4 x the field's smaller side"), r
    assert abs(r["aspect"] - 1) < 0.02 and "warnings" not in r
    side = float(r["step_from"].split("(")[1].split()[0])
    assert r["step_um"] == pytest.approx(0.4 * side, rel=0.06)
    r2 = api.instrument("video_scale", um=round(0.55 * side, 1))
    assert not r2["ok"] and "in Y" in r2["error"] and "tall" in r2["error"]
    # another objective on the same camera with another pixel aspect: stored, but flagged
    store = V.scale_store()
    cam = r["camera"]
    store[f"other@{cam}"] = {"sx": 0.0949, "ty": 0.0990, "rotation_deg": 0.0}
    V.scale_store(store)
    r3 = api.instrument("video_scale")
    assert r3["ok"] and r3["warnings"] and "disagrees" in r3["warnings"][0] and "pixel aspect" in r3["warnings"][0]
    assert V.scale_store()[r3["stored"]]["disagrees"]


def test_spectrometer_centre_moved_and_the_window_checked(tmp_path):
    """2026-11-23 at the XploRA (637.75 nm): a study centred at 1500 cm-1 kept the previous window (LabSpec's Acq does
    not move the spectrometer); after grating changes the window read '-1016 to -974 nm' or sat 27 nm off centre; no
    .l6s was saved (save_native overwritten); the warm-up's last grating change ignored study setups."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    vi = br[br.index("Sub VAcquireInner("):]
    vi = vi[:vi.index("End Sub")]
    assert vi.index("LabSpec.MoveMotor(spm, f, \"\", LS_MOTOR_VALUE)") < vi.index("LabSpec.Acq mode, ex, acc, f, t")
    assert 'res("spectro_nm") = MotorValue(spm)' in vi
    prof = P.load()
    rng = {"single": True, "center_nm": 659.66, "from_nm": 659.66}
    plan.window_store({})
    nm = np.linspace(-1016, -974, 100)
    r = plan.grating_window_check(nm, "nm", 637.75, 1800, prof, rng)
    assert not r["ok"] and "impossible" in r["stop"][0]
    nm = np.linspace(646, 728, 1000)                   # 82 nm around 687: off centre
    r = plan.grating_window_check(nm, "nm", 637.75, 1200, prof, rng)
    assert not r["ok"] and "centred at 687" in r["stop"][0]
    nm = np.linspace(616.5, 702.5, 1000)               # 86 nm around 659.5: right; the profile's 39 nm is off
    r = plan.grating_window_check(nm, "nm", 637.75, 1200, prof, rng)
    assert r["ok"] and r.get("learned_now") and "window_nm" in r["warn"][0]
    nm = np.linspace(640, 680, 1000)                   # later: 40 nm with "1200": not the grating measured before
    r = plan.grating_window_check(nm, "nm", 637.75, 1200, prof, rng)
    assert not r["ok"] and "86.0 nm was measured" in r["stop"][0]
    plan.window_store({})
    # end to end on the simulator: centred spectra, the spectrometer read back, .l6s saved, last grating noted
    S.ensure(config.active_bridge_dir())
    st = run_inline({"a": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=1200),
                           "acquire": {"center_cm1": 520, "exposure_s": 1}},
                     "b": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": dict(SETUP, grating=1800),
                           "acquire": {"center_cm1": 1500, "exposure_s": 1}}})
    for k in ("a", "b"):
        s = st["studies"][k]
        assert s["ok"], s
        assert s["checks"]["grating_window"]["pass"] and s["checks"]["grating_window"]["spectro_nm"]
    assert list(config.out_dir().rglob("p00000.l6s"))           # LabSpec's own file saved (save_native)
    assert json.loads((config.home() / "last_grating_set.json").read_text())["by"].startswith("study")


def test_maintainer_session_2026_11_23(tmp_path, monkeypatch):
    """From the maintainer's Codex session at the XploRA (2026-11-23): an acquisition holding the bridge inside
    LabSpec was called stuck after 20 s; the live video ls6t started froze (GET_VIDEO_ID must be asked all along);
    LabSpec switched to dark field when the video started; video-scale returned NaN when the Y move left the view;
    a reviewed profile lacked OLConfig's fields, camera names and illumination motor; a halted bridge was described
    as ended by LabSpec."""
    from labspec_translator import video as V, profile as PR, labspec_ini
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    vi = br[br.index("Sub VAcquireInner("):]
    vi = vi[:vi.index("End Sub")]
    assert vi.index('gBlocking = "acquire"') < vi.index("LabSpec.Acq mode, ex, acc, f, t") < vi.rindex('gBlocking = ""')
    assert "Sub VidKeep()" in br and br.count("VidKeep") >= 4 and br.count("IllumRestore cmd, illum0, res") == 2
    hb = {"state": "busy", "verb": "acquire", "blocking": "acquire", "blocking_s": "120", "age_s": 60}
    assert bridge.blocking_allowance(hb) == 150 and "acquisition has held" in bridge.diagnose(dict(hb, age_s=200))
    assert "halted by the operator" in bridge.diagnose({"state": "halted", "age_s": 242614})
    # video-scale: a move that leaves the view gives NaN, not a wrong number; frac never makes NaN of a fit
    a = np.random.default_rng(3).normal(size=(60, 80))
    r, c = V.image_shift(a, np.random.default_rng(4).normal(size=(60, 80)), axis="y")
    assert all(isinstance(v, float) for v in (r, c))
    # OLConfig's descriptions fill a reviewed profile's gaps, never a person's values
    def fake_enrich(probe, lp, notes):
        probe["objectives_info"] = {"x10_VIS": {"field_um": [862.9, 590.0]}}
        probe.setdefault("video", {})["cameras"] = {"top": 0, "internal": 1}
        probe["motors"].update(illumination="IlluminatorV", illumination_light="uLight")
        return probe
    ini = tmp_path / "OLConfig.ini"
    ini.write_text("[x]\n")
    monkeypatch.setattr(labspec_ini, "find", lambda lp=None, name="OLConfig.ini": ini)
    monkeypatch.setattr(labspec_ini, "enrich", fake_enrich)
    PR._INI_CACHE.clear()
    prof = P.load()
    P.save({k: v for k, v in prof.items() if k not in ("objectives_info", "facts_from_ini", "path", "measured")})
    PR._INI_CACHE.clear()
    prof = P.load()
    assert prof["objectives_info"]["x10_VIS"]["field_um"] == [862.9, 590.0]
    assert prof["video"]["cameras"] == {"top": 0, "internal": 1} and prof["motors"]["illumination"] == "IlluminatorV"
    assert "objectives_info" in prof["facts_from_ini"]["added"]
    P.save(dict(prof, video=dict(prof["video"], cameras={"top": 2})))
    PR._INI_CACHE.clear()
    assert P.load()["video"]["cameras"] == {"top": 2}                  # a person's value stays


def test_maintainer_session_2026_11_23_afternoon(tmp_path):
    """The maintainer's second session: a calibration check at 1 %, 1 s fitted a weak Si line at the edge of its
    window (525.12 cm-1) and asked for a -4.42 cm-1 correction that AutoCalibration then showed was wrong (520.64); a
    spectrum 100.3-997.6 cm-1 for 100-1000 was called short; the start-up inventory hung inside LabSpec right after
    AutoCalibration was run by hand; after x10 -> x100 the focus scan started from the old focus."""
    prof = P.load()
    study = {"kind": "calibration", "calibration": {"reference": "si", "tol_cm1": 1, "adjust": "ls6t"}}
    checks, unreliable, warnings = {}, [], []
    rec = {"peaks": [{"ok": True, "position_cm1": 525.12, "fwhm_cm1": 9.0, "snr": 4.0,
                      "warning": "the fitted peak sits at the edge of its window: widen window_cm1 or move near_cm1"}]}
    cal = runner._calibration_result(study, prof, rec, [520.7], 532.0, 1200, {}, "r", checks, unreliable, warnings)
    assert cal["trusted"] is False and len(cal["not_trusted_because"]) == 1 and "stored_correction" not in cal
    assert any("not measured well enough" in u for u in unreliable)
    good = {"peaks": [{"ok": True, "position_cm1": 520.33, "fwhm_cm1": 5.0, "snr": 80.0}]}
    cal = runner._calibration_result(study, prof, good, [520.7], 532.0, 1200, {}, "r", {}, [], [])
    runner.store_pending(cal, [], [])           # stored once the study ends reliable (2026.11.25.12)
    assert cal["trusted"] and cal["pass"] and cal["stored_correction"]
    # the bridge's start-up inventory only when needed
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    assert "Function InventoryNeeded()" in br and "If InventoryNeeded() Then" in br
    # the objective question says to focus by eye after a manual change
    assert "into focus by eye" in api.objective_question(prof, "x10_VIS", "x100_VIS")
    # a spectrum a pixel or two short of the range asked is not reported as short
    S.ensure(config.active_bridge_dir())
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                           "acquire": {"range_cm1": [100, 1000], "exposure_s": 1}}})
    assert st["studies"]["s"]["checks"]["range"]["pass"], st["studies"]["s"]["checks"]["range"]


def test_ics_off_where_labspec_has_no_correction_file():
    """2026-11-23: with the 2400 grating and 637.75 nm LabSpec stopped every acquisition at 'The required ICS
    correction file does not exist ... Error Code=2070' until OK was pressed. Profile ics.off_for turns ICS off there."""
    prof = P.load()
    assert not plan.ics_off(prof, 637.75, "2400 (400nm)")
    prof = dict(prof, ics={"off_for": ["2400@637.75"]})
    assert plan.ics_off(prof, 637.75, "2400 (400nm)") and plan.ics_off(prof, "637.75", 2400)
    assert not plan.ics_off(prof, 532, 2400) and not plan.ics_off(prof, 637.75, 1200)
    assert (plan.acq_mode({"acquire": {"ics": False}}) // 100000) % 10 == 1
    assert (plan.acq_mode({"acquire": {"ics": True}}) // 100000) % 10 == 2
    assert not F.validate({"s": {"kind": "spectrum", "setup": SETUP, "acquire": {"ics": "maybe"}}})["s"]["ok"]
    # on the simulator: the acquisition is sent with ACQ_NO_ICS and the result says so
    S.ensure(config.active_bridge_dir())
    P.save(dict(P.load(), ics={"off_for": ["1800@532"]}))
    sent = []
    orig = bridge.Bridge.call

    def spy(self, verb, *a, **kw):
        if verb == "acquire":
            sent.append(int(kw.get("mode", 0)))
        return orig(self, verb, *a, **kw)
    import unittest.mock as um
    with um.patch.object(bridge.Bridge, "call", spy):
        st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                               "acquire": {"center_cm1": 520, "exposure_s": 1}}})
    assert sent and all((m // 100000) % 10 == 1 for m in sent), sent
    assert any("ICS" in w for w in st["studies"]["s"]["warnings"])


def test_horiba_qc_reports_and_ics_responses(tmp_path, monkeypatch):
    """HORIBA's QC folder (2026-11-23): per-laser reports (cut-off, laser power, axial resolution...) kept as
    references, the cut-off used for range warnings; ICS on only where LabSpec has a Response_*_<laser>_<grating>.l6s
    (on this XploRA: 532 all four gratings, 638 600/1200/1800, none at 785 in the 6.8.1 install)."""
    from labspec_translator import qc, perf, labspec_ini
    prof = P.load()
    prof["lasers"] = {"532": {"nm": 532.0, "label": "532nm_Edge", "power_mw": 100, "max_filter_pct": 100},
                      "637.75": {"nm": 637.75, "label": "638nm_Edge", "power_mw": 35, "max_filter_pct": 100}}
    P.save(prof)
    prof = P.load()
    # synthetic reports in the QC scripts' layout (UTF-16, as they are written)
    d = tmp_path / "QC Auto" / "Laser 638nm_Edge" / "Cut-Off" / "06_09_2023_14_21_27"
    d.mkdir(parents=True)
    (d / "r.txt").write_bytes("info=test=Cut-Off\r\nmoteur=Laser Filtering=638nm_Edge\r\nvalue=Ratio=0.88\r\n"
                              "value=Low_Cut-Off=35.3\r\nvalue=High_Cut-Off=58.7\r\n".encode("utf-16-le"))
    d2 = tmp_path / "QC Auto" / "Laser 638nm_Edge" / "Laser Power" / "06_09_2023_13_48_12"
    d2.mkdir(parents=True)
    (d2 / "p.txt").write_text("info=test=Laser Power\nmoteur=Laser Filtering=638nm_Edge\nvalue=Output_Power=55\n"
                              "value=Sample_Power=40\n")
    r = qc.import_dir(prof, tmp_path / "QC Auto")
    assert r["ok"] and r["cutoff_cm1"] == {"638nm_Edge": 58.7}
    assert perf.cutoff_for(637.75, 1200) == 58.7                  # the laser's own cut-off for any grating
    s = api.references()["horiba_qc"]["lasers"]["638nm_Edge"]
    assert s["at_sample_mw"] == 40 and s["profile_power_mw"] == 35 and s["cutoff_cm1"] == [35.3, 58.7]
    v = api.validate({"s": {"kind": "spectrum", "sample": {"name": "x"},
                            "setup": {"laser_nm": 637.75, "grating": 1200, "filter": "10%"},
                            "acquire": {"range_cm1": [40, 1000], "exposure_s": 1}}})["s"]
    assert any("below the measured cut-off (58.7" in w for w in v["warnings"])
    # ICS responses in LabSpec's folder
    acq = tmp_path / "LabSpec" / "Parameters" / "ACQUISITION"
    (acq / "Responses").mkdir(parents=True)
    (acq / "OLConfig.ini").write_text("[x]\n")
    for name in ("532nm_Edge_1200 (750nm)", "532nm_Edge_2400 (400nm)", "638nm_Edge_1200 (750nm)"):
        (acq / "Responses" / f"Response_Syncerity OE_{name}.l6s").write_text("x")
    monkeypatch.setattr(labspec_ini, "find", lambda lp=None, name="OLConfig.ini": acq / "OLConfig.ini")
    plan._ICS_CACHE.clear()
    assert not plan.ics_off(prof, 532, "2400 (400nm)") and not plan.ics_off(prof, 637.75, 1200)
    assert plan.ics_off(prof, 637.75, "2400 (400nm)") and plan.ics_off(prof, 637.75, 600)
    assert not plan.ics_off(dict(prof, ics={"off_for": [], "auto": False}), 637.75, 2400)


def test_labspec_autocalibration_called_directly(tmp_path):
    """HORIBA's service folder (2026-11-23): LabSpec 6.7+ runs AutoCalibration from a compiled module (no .vbs for
    autocal_info to find); LabSpec's help documents AutoCalibration(Mode, Status) for scripts. ls6t calls it when the
    profile says method 'call', after the operator's Yes."""
    br = Path(api.install_bridge(tmp_path / "vbs")["files"]["ls6t_bridge.vbs"]).read_text()
    assert 'Case "autocal": VAutoCal cmd, res' in br and "LabSpec.AutoCalibration(1, st)" in br
    assert "ls6t instrument autocal-run" in api.autocal_info()["labspec_call"]
    S.ensure(config.active_bridge_dir())
    P.save(dict(P.load(), calibration=dict(P.load()["calibration"],
                                           autocal={"enabled": True, "method": "call", "mode": "full"})))
    r = api.autocal_run()
    assert r["ok"], r
    assert "the laser fires" in api.autocal_question({"method": "call"})
    assert "no sample" in api.autocal_question({"method": "call", "mode": "offset"})


def test_each_spectrum_records_its_ics_state():
    """Nicola, 2026-11-23: intensities with and without ICS are not comparable: every spectrum says which."""
    S.ensure(config.active_bridge_dir())
    P.save(dict(P.load(), ics={"off_for": ["1800@532"], "auto": True}))
    st = run_inline({"off": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                             "acquire": {"center_cm1": 520, "exposure_s": 1}},
                     "on": {"kind": "spectrum", "sample": {"name": "Si"}, "setup": SETUP,
                            "acquire": {"center_cm1": 520, "exposure_s": 1, "ics": True}}})
    off, on = st["studies"]["off"], st["studies"]["on"]
    assert off["ics"].startswith("off (no correction file") and off["spectra"][0]["ics"] == off["ics"]
    assert on["ics"] == "on (asked)"
    shift = Path(off["spectra"][0]["file"]).with_suffix(".shift.txt").read_text()
    assert "# ics=off (no correction file" in shift


def test_qc_folder_found_on_any_fixed_drive(tmp_path, monkeypatch):
    """2026-11-23: on the instrument PC the QC folder is D:\\Horiba Jobin Yvon\\1-QC\\QC Auto, with a copy under
    D:\\LabSpec Software & Drivers\\QC; ls6t looked only on C:. Every fixed drive is looked at, and the import says
    which folder it used."""
    import os
    from labspec_translator import qc
    c_drive, d_drive = tmp_path / "C", tmp_path / "D"
    main = d_drive / "Horiba Jobin Yvon" / "1-QC" / "QC Auto"
    copy = d_drive / "LabSpec Software & Drivers" / "QC" / "1-QC" / "QC Auto"
    for q, age in ((main, 0), (copy, 86400)):
        r = q / "Laser 638nm_Edge" / "Cut-Off" / "06_09_2023_14_21_27"
        r.mkdir(parents=True)
        f = r / "r.txt"
        f.write_text("info=test=Cut-Off\nmoteur=Laser Filtering=638nm_Edge\nvalue=High_Cut-Off=58.7\n")
        os.utime(f, (f.stat().st_mtime - age,) * 2)
    c_drive.mkdir()
    monkeypatch.delenv("LS6T_QC_DIR", raising=False)
    monkeypatch.setattr(qc, "_drive_roots", lambda: [c_drive, d_drive])
    found: list = []
    assert qc.find_dir(None, found) == main and len(found) == 2
    r = qc.import_dir(P.load())
    assert r["ok"] and r["from"] == str(main) and r["chosen"] == "the folder with the newest report"
    # elsewhere on a drive: found by the short search
    other = tmp_path / "E" / "Service" / "1-QC" / "QC Auto"
    other.mkdir(parents=True)
    monkeypatch.setattr(qc, "_drive_roots", lambda: [c_drive, tmp_path / "E"])
    assert qc.find_dir() == other
    monkeypatch.setattr(qc, "_drive_roots", lambda: [c_drive])
    assert "give its path" in qc.import_dir(P.load())["error"]
