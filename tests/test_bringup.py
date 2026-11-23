"""Tests: first instrument sessions (2026.11.20.1)."""
from helpers import *  # noqa: F401,F403


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
    # discovery no longer probes these (2026.11.22.7): dumps from older bridges still carry them
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
