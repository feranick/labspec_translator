"""Tests: test mode (TESTING.md from the LLM)."""
from helpers import *  # noqa: F401,F403


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
    new = local / "Claude-Data" / "claude_pzs8sxrjxfjjc"
    # 2026-11-22: a Claude-Data folder without the app's marker does not prove the Packages copy is unread (the Store
    # build read it on Nov 20): both are written, nothing removed
    new.mkdir(parents=True)
    assert C.claude_dirs(create=True) == [old, new] and C.claude_stale_files() == []
    C.connect(["claude"], py)
    assert all(C.read_json(d / "claude_desktop_config.json")["mcpServers"]["ls6t"]["command"] == py for d in (old, new))
    (old / ".user-data-left.claude_pzs8sxrjxfjjc").write_text("")
    assert C.claude_dirs(create=True) == [new]                              # moved: Claude-Data
    reps = C.connect(["claude"], py)
    assert C.read_json(new / "claude_desktop_config.json")["mcpServers"]["ls6t"]["command"] == py
    assert C.read_json(old / "claude_desktop_config.json")["mcpServers"] == {}   # the stale entry taken out
    assert any("no longer reads" in " ".join(r.get("notes", [])) for r in reps)
    (old / "claude_desktop_config.json").unlink()                           # 2026-11-22: only the marker left
    reps = C.connect(["claude"], py)
    assert any("no config file here" in " ".join(r.get("notes", [])) for r in reps)
    assert not (old / "claude_desktop_config.json").exists() and C.claude_stale_files() == []
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
