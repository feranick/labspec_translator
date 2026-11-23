"""Tests: runs on the simulator and the LabSpec scripts."""
from helpers import *  # noqa: F401,F403


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
        assert "@@" not in text and 'Const LS6T_BRIDGE_DIR_INSTALLED = "' in text
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


def test_run_stops_when_labspecs_grating_cannot_reach_the_range():
    """No setup.grating: the grating LabSpec has is checked against the range before anything is measured."""
    prof = P.load()
    prof["gratings"]["1800"]["spectro_nm"] = [100, 935]
    P.save(prof)
    S.ensure(config.active_bridge_dir())
    api.instrument("set", setup={"grating": 1800})
    st = run_inline({"s": {"kind": "spectrum", "sample": {"name": "x"}, "setup": {"laser_nm": 785, "filter": "10%"},
                           "acquire": {"range_cm1": [100, 3400], "exposure_s": 1}}})
    s = st["studies"]["s"]
    assert not s["ok"] and "cannot reach" in json.dumps(s) and not s.get("spectra")
