"""Tests: the type-library reader."""
from helpers import *  # noqa: F401,F403


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
