"""cli.py — the command line. Every command prints JSON; '-' reads the studies from stdin.

  ls6t status [REQUEST] [--wait S]   versions, the bridge, the profile; or a request's studies
  ls6t format                        the study format
  ls6t validate studies.json         static checks
  ls6t run studies.json [--wait S]   start a request (a detached runner) and wait up to S seconds
  ls6t inputs studies.json NAME      the bridge commands of one study, without running
  ls6t cancel REQUEST                stop a request (the current acquisition or move stops)
  ls6t jobs [--limit N] [--active]   recent requests
  ls6t bench [NAMES...]              the benchmarks (on the simulator)
  ls6t instrument status|discover|resume|last-error|end-bridge|camera-on|camera-off [--camera N] | laser-on [--seconds S] | laser-off
  ls6t instrument video-scale [--um 15]   measure the video's µm per pixel (the stage moves +X, +Y and back)
  ls6t instrument clearance          with the operator: the objective's closest safe gap on a flat sample (by hand),
                                     stored; ls6t then never goes past half of it toward the sample
  ls6t instrument set [--laser-nm 532] [--grating 1800] [--filter 10%] [--objective x100] [--hole 300] [--slit 100]
                     [--illumination bright|dark]
                                     change the set-up now (no laser; the objective asks the operator)
  ls6t instrument snapshot [--laser | --extended]
                                     a video frame with the laser position marked (--laser: the beam shows;
                                     --extended: LabSpec's extended video, the stage moves)
  ls6t instrument config list | save NAME | load NAME [--show]
                                     LabSpec's whole configuration as LS6T_HOME/configs/NAME.ini (maintainer)
  ls6t bridge install [--dest DIR]   write the LabSpec scripts configured for this machine
  ls6t bridge call VERB [k=v ...]    one bridge command, for the maintainer's tests (TESTING.md); acquire and
                                     autofocus fire the laser and need --laser
  ls6t profile propose [DUMP]        profile.proposed.json from the bridge's inventory, to edit with the maintainer
  ls6t profile import FILE           the edited profile.proposed.json: checked, marked reviewed (the laser may fire)
  ls6t profile show|default
  ls6t profile qc [FOLDER]           HORIBA's QC reports (found on the fixed drives: '1-QC\QC Auto') as references
  ls6t profile ics                   where LabSpec has an ICS correction (grating@laser); ls6t turns ICS off elsewhere
  ls6t profile set KEY VALUE         one value (JSON), e.g. video.laser_offset_um "[1.4, -0.6]"
  ls6t profile ini [OLConfig.ini]    what LabSpec's own configuration says (cameras, objectives, motors)
  ls6t alignment status              per laser: baseline, last check, age
  ls6t spot status | forget [--objective O --laser NM]   the beam's remembered offsets
  ls6t references show | clear correction [LASER@GRATING[@CENTRE_NM]] | clear background NAME
                                     spectral corrections, white-light backgrounds, beam offsets, alignment
  ls6t alignment record REQUEST [--study NAME] [--attempt N]
                                     after a successful AutoAlignment: the check becomes the laser's baseline
  ls6t connect                       which LLM apps are here and which run ls6t's MCP server
  ls6t connect check [APP...]        start each app's ls6t entry as the app would and list its tools
  ls6t connect APP... [--sim] [--remove]
                                     add (or remove) the MCP server in: claude (Claude desktop app), codex (ChatGPT
                                     desktop app, Codex CLI and IDE extension), gemini (Gemini CLI), cursor (Cursor),
                                     claudecode (Claude Code), copilot (GitHub Copilot: VS Code, Visual Studio,
                                     Copilot CLI), windsurf (Windsurf), lmstudio (LM Studio), bionic (LM Studio Bionic), or auto (every
                                     app found); --sim also adds 'ls6t-sim' (the simulator); --test turns test
                                     mode on for those entries (the hardware_test tool), --no-test off
  ls6t test plan | step ID | report  TESTING.md's tests: the plan with results so far, one test, the report
  ls6t test record ID RESULT [--notes T] [--values JSON] [--answers JSON] [--who NAME]
                                     record a test (pass, fail, partial, skipped, blocked) in LS6T_HOME/testing
  ls6t sim [--seconds S]             run the simulator in the foreground (LS6T_MODE=sim tests by hand)
  ls6t run-request FOLDER            what the detached runner runs
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, api, config, plan
from . import profile as P


def _studies(path: str) -> dict:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def _print(o):
    sys.stdout.write(json.dumps(o, indent=1, default=str) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ls6t", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("request", nargs="?")
    s.add_argument("--wait", type=float, default=0)
    sub.add_parser("format")
    s = sub.add_parser("validate")
    s.add_argument("studies")
    s = sub.add_parser("run")
    s.add_argument("studies")
    s.add_argument("--wait", type=float, default=1e9, help="seconds to wait (default: until done)")
    s = sub.add_parser("inputs")
    s.add_argument("studies")
    s.add_argument("name")
    s = sub.add_parser("cancel")
    s.add_argument("request")
    s = sub.add_parser("jobs")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--active", action="store_true")
    s = sub.add_parser("bench")
    s.add_argument("names", nargs="*")
    s = sub.add_parser("instrument")
    s.add_argument("action", choices=["status", "set", "discover", "resume", "last-error", "snapshot", "config",
                                      "camera-on", "camera-off", "laser-on", "laser-off", "calibration",
                                      "autocal-info", "autocal-run", "end-bridge", "video-scale", "shutter-test",
                                      "shutter-verify", "clearance"])
    s.add_argument("--motor", help="shutter-test: the laser shutter's motor (default: the profile's, or discovery's "
                                   "candidate)")
    s.add_argument("--repeats", type=int, default=3, help="shutter-verify: closed-shutter spectra, judged on the worst")
    s.add_argument("--burst-seconds", type=float, help="shutter-verify: one more closed spectrum of this exposure: a "
                                                       "burst per acquisition gives the same counts at any exposure")
    s.add_argument("--seconds", type=float, help="laser-on: how long (at most limits.max_laser_on_s)")
    s.add_argument("--um", type=float, help="video-scale: the stage move in µm (default: 0.4 x the field's smaller side, "
                                            "~23 µm x100, ~240 µm x10)")
    s.add_argument("--camera", help="camera-on/off: a number or a name from the profile's video.cameras")
    s.add_argument("what", nargs="?", help="config: list, save or load")
    s.add_argument("name", nargs="?", help="config save/load: the configuration's name")
    s.add_argument("--laser", action="store_true", help="snapshot: open the laser shutter briefly (asks in LabSpec)")
    s.add_argument("--extended", action="store_true", help="snapshot: LabSpec's extended video (the stage moves)")
    s.add_argument("--show", action="store_true", help="config: open LabSpec's own dialog")
    for opt, key in (("--laser", None), ("--grating", "grating"), ("--filter", "filter"), ("--objective", "objective"),
                     ("--hole", "hole_um"), ("--slit", "slit_um"), ("--illumination", "illumination")):
        if opt != "--laser":
            s.add_argument(opt, dest=f"set_{key}", help=f"set: {key}")
    s.add_argument("--laser-nm", dest="set_laser_nm", help="set: the laser line (nm)")
    s = sub.add_parser("bridge")
    s.add_argument("action", choices=["install", "call"])
    s.add_argument("verb", nargs="?")
    s.add_argument("params", nargs="*", help="key=value")
    s.add_argument("--dest")
    s.add_argument("--timeout", type=float, default=120)
    s.add_argument("--laser", action="store_true", help="allow acquire and autofocus (the laser fires)")
    s = sub.add_parser("profile")
    s.add_argument("action", choices=["show", "default", "propose", "import", "set", "ini", "qc", "ics"])
    s.add_argument("dump", nargs="?", help="propose: a discovery dump (default: the bridge's inventory); import: the "
                   "edited profile.proposed.json (or a dump); set: the key")
    s.add_argument("value", nargs="?", help="set: the value (JSON)")
    s = sub.add_parser("references")
    s.add_argument("action", choices=["show", "clear"])
    s.add_argument("what", nargs="?", choices=["correction", "background", "spot", "learned", "laser_log", "shutter"])
    s.add_argument("name", nargs="?")
    s.add_argument("field", nargs="?", help="learned: zero_drift | first_landing | laser_line_sd | zero_creep | "
                                            "window_zero | moved_sd | still_sd (all when left out)")
    s = sub.add_parser("spot")
    s.add_argument("action", choices=["status", "forget"])
    s.add_argument("--objective")
    s.add_argument("--laser", type=float)
    s = sub.add_parser("alignment")
    s.add_argument("action", choices=["status", "record"])
    s.add_argument("request", nargs="?")
    s.add_argument("--study")
    s.add_argument("--attempt", type=int)
    s = sub.add_parser("connect")
    s.add_argument("apps", nargs="*", help="claude, codex, gemini, cursor, claudecode, copilot, windsurf, lmstudio, bionic, or auto (every app found)")
    s.add_argument("--sim", action="store_true", help="also add 'ls6t-sim' (the simulator)")
    s.add_argument("--remove", action="store_true", help="remove this install's entries instead")
    s.add_argument("--test", dest="test", action="store_true", default=None, help="test mode on (hardware_test)")
    s.add_argument("--no-test", dest="test", action="store_false", help="test mode off")
    s.add_argument("--tools", choices=["full", "measure"], help="the tool set those apps see: full, or measure (small "
                                                               "local models: measure_spectrum, calibrate_si, study "
                                                               "tools; no laser_on, set or snapshot)")
    s = sub.add_parser("test")
    s.add_argument("action", choices=["plan", "step", "record", "report"])
    s.add_argument("id", nargs="?", help="step / record: the test's ID, e.g. T3.4")
    s.add_argument("result", nargs="?", help="record: pass, fail, partial, skipped or blocked")
    s.add_argument("--notes", default="")
    s.add_argument("--values", default="{}", help='record: JSON, e.g. \'{"si_cm1": 520.6}\'')
    s.add_argument("--answers", default="{}", help='record: JSON, e.g. \'{"Q5": "motorised"}\'')
    s.add_argument("--who", default="")
    s = sub.add_parser("sim")
    s.add_argument("--seconds", type=float, default=0, help="0: until Ctrl-C")
    s = sub.add_parser("run-request")
    s.add_argument("folder")
    a, extra = ap.parse_known_args(argv)
    if extra:
        if a.cmd == "bridge" and a.action == "call":     # key=value after an option (e.g. --laser)
            a.params = list(a.params) + extra
        else:
            ap.error("unrecognized arguments: " + " ".join(extra))

    if a.cmd == "status":
        _print(api.status(a.request, a.wait) if a.request else api.tool_status())
    elif a.cmd == "format":
        _print(api.study_format())
    elif a.cmd == "validate":
        _print(api.validate(_studies(a.studies)))
    elif a.cmd == "run":
        r = api.start(_studies(a.studies))
        if r.get("ok"):
            r = api.status(r["request"], a.wait)
        _print(r)
        return 0 if r.get("done", r.get("ok")) else 1
    elif a.cmd == "inputs":
        _print(api.inputs(_studies(a.studies)[a.name], a.name))
    elif a.cmd == "cancel":
        _print(api.cancel(a.request))
    elif a.cmd == "jobs":
        _print(api.jobs(a.limit, a.active))
    elif a.cmd == "bench":
        r = api.bench(a.names or None)
        _print(r)
        return 0 if not r["failed"] else 1
    elif a.cmd == "instrument":
        if a.action == "snapshot":
            r = api.snapshot(with_laser=a.laser, extended=a.extended)
            _print(r)
            return 0 if r.get("ok") else 1
        if a.action == "set":
            setup = {k[4:]: v for k, v in vars(a).items() if k.startswith("set_") and v is not None}
            for k in ("laser_nm", "grating", "hole_um", "slit_um"):
                if k in setup:
                    setup[k] = float(setup[k])
            r = api.set_instrument(setup)
            _print(r)
            return 0 if r.get("ok") else 1
        if a.action == "config":
            if a.what not in ("list", "save", "load"):
                ap.error("instrument config list | save NAME | load NAME")
            r = api.labspec_config(a.what, a.name, show=a.show)
            _print(r)
            return 0 if r.get("ok") else 1
        if a.action == "shutter-test":
            _print(api.shutter_test(a.motor))
        elif a.action == "shutter-verify":
            # --laser-nm / --filter / --objective... set first (the laser to verify)
            sv_ = {k[4:]: v for k, v in vars(a).items() if k.startswith("set_") and v is not None}
            for k in ("laser_nm", "grating", "hole_um", "slit_um"):
                if k in sv_:
                    sv_[k] = float(sv_[k])
            _print(api.shutter_verify(a.seconds or api.VERIFY_EXPOSURE_S, a.repeats, a.burst_seconds, setup=sv_))
        else:
            _print(api.instrument(a.action.replace("-", "_"), camera=a.camera, seconds=a.seconds, um=a.um))
    elif a.cmd == "bridge":
        if a.action == "install":
            if not a.dest:                      # replacing the scripts in use: end a running bridge cleanly first
                from . import winsetup as W

                class _Log:
                    def __call__(self, m):
                        print(m, file=sys.stderr)

                    def warn(self, m):
                        print("warning: " + m, file=sys.stderr)
                W.end_running_bridge(_Log())
            _print(api.install_bridge(Path(a.dest) if a.dest else None))
        else:
            if not a.verb:
                ap.error("bridge call needs a verb (ping, status, move, move_xy, autofocus, acquire, ...)")
            kv = {}
            for item in a.params:
                if "=" not in item:
                    ap.error(f"{item!r}: give key=value")
                k, v = item.split("=", 1)
                kv[k] = v
            r = api.bridge_call(a.verb, kv, a.timeout, allow_laser=a.laser)
            _print(r)
            return 0 if r.get("ok") else 1
    elif a.cmd == "profile":
        if a.action == "show":
            _print(P.load())
        elif a.action == "default":
            p = config.profile_path()
            if p.exists():
                _print({"ok": False, "error": f"{p} exists: edit it, or move it away first"})
                return 1
            _print({"ok": True, "written": str(P.save(P.load(), p)),
                    "next": "edit the limits and the lasers' max_filter_pct, or import a discovery dump"})
        elif a.action == "ini":
            from . import labspec_ini as L
            p = a.dump or L.find((P.load().get("discovered") or {}).get("labspec_path"))
            if not p:
                _print({"ok": False, "error": "OLConfig.ini not found: give its path (it is in LabSpec's folder)"})
                return 1
            _print({"ok": True, "file": str(p), **L.summary(L.read(p))})
        elif a.action == "qc":
            from . import qc
            r = qc.import_dir(P.load(), a.dump)
            _print(r)
            return 0 if r["ok"] else 1
        elif a.action == "ics":
            prof = P.load()
            pairs = plan.ics_files(prof)
            table = {f"{float(g):g}@{float(v['nm']):g}": not plan.ics_off(prof, v["nm"], g)
                     for v in prof.get("lasers", {}).values() for g in prof.get("gratings", {})}
            _print({"ok": True, "responses_found": None if pairs is None else sorted(pairs),
                    "ics_on": table, "off_for": (prof.get("ics") or {}).get("off_for", []),
                    "note": "ICS on = LabSpec has a correction for that grating@laser; off: ls6t sends ACQ_NO_ICS"})
        elif a.action == "propose":
            r = api.profile_propose(a.dump)
            _print(r)
            return 0 if r["ok"] else 1
        elif a.action == "set":
            if not a.dump or a.value is None:
                ap.error("profile set needs KEY VALUE")
            _print(api.profile_set(a.dump, a.value))
        else:
            if not a.dump:
                ap.error("profile import needs a reviewed profile (.json) or a discovery dump (.txt)")
            src = Path(a.dump)
            if src.suffix.lower() == ".json":
                try:
                    prof, notes = P.import_reviewed(src)
                except ValueError as exc:
                    _print({"ok": False, "error": str(exc)})
                    return 1
            else:
                prof, notes = P.import_discovery(P.read_text(src))
                prop = config.profile_path().with_name("profile.proposed.json")
                if prop.is_file() and src.is_file() and prop.stat().st_mtime > src.stat().st_mtime:
                    notes.append(f"{prop} is newer than this dump: if it holds your edits (power_mw, caps), import "
                                 f"it instead: ls6t profile import \"{prop}\"")
                notes.append("not reviewed: the laser does not fire on the instrument until a person imports the "
                             "edited JSON (" + P.REVIEW_STEPS + ")")
            _print({"ok": True, "written": str(P.save(prof)), "notes": notes})
    elif a.cmd == "references":
        if a.action == "show":
            _print(api.references())
        else:
            if not a.what:
                ap.error("references clear needs: correction [LASER@GRATING[@CENTRE_NM]] | background NAME | spot | "
                         "learned LASER@GRATING [FIELD]")
            _print(api.references_clear(a.what, a.name, a.field))
    elif a.cmd == "spot":
        _print(api.spot_status() if a.action == "status" else api.spot_forget(a.objective, a.laser))
    elif a.cmd == "alignment":
        if a.action == "status":
            _print(api.alignment_status())
        else:
            if not a.request:
                ap.error("alignment record needs the request folder")
            _print(api.alignment_record(a.request, a.study, a.attempt))
    elif a.cmd == "connect":
        from . import clients as C
        if not a.apps:
            _print(C.status())
            return 0
        if a.apps[0] == "check":
            r = C.check([y for y in (C.ALIASES.get(x.lower(), x.lower()) for x in a.apps[1:]) if y in C.CLIENTS]
                        or None)
            _print(r)
            return 0 if all(v.get("ok") for v in r.values() if "no 'ls6t' entry" not in str(v.get("error"))) else 1
        apps = [C.ALIASES.get(x.lower(), x.lower()) for x in a.apps]
        if "auto" in apps:
            found = C.detect()
            apps = [c for c in C.CLIENTS if found[c]]
            if not apps:
                _print({"ok": False, "error": "no LLM app found (Claude desktop app, ChatGPT desktop app / Codex, "
                                              "Gemini CLI, Cursor, Claude Code, GitHub Copilot, Windsurf, LM Studio): name one to prepare its "
                                              "file anyway"})
                return 1
        bad = [x for x in apps if x not in C.CLIENTS]
        if bad:
            ap.error(f"connect: unknown app {', '.join(bad)} ({', '.join(C.CLIENTS)}, auto)")
        reps = (C.disconnect(apps, sys.executable) if a.remove
                else C.connect(apps, sys.executable, sim=a.sim, test=a.test, tools=a.tools))
        ok = all(r.get("ok") for r in reps)
        _print({"ok": ok, "python": sys.executable, "files": reps,
                "next": "quit and restart each app changed: the ls6t tools appear in a new chat or session"})
        return 0 if ok else 1
    elif a.cmd == "test":
        from . import testmode as T
        if a.action == "plan":
            _print(T.plan_summary())
        elif a.action == "report":
            _print(T.report())
        elif a.action == "step":
            r = T.step_info(a.id or "")
            r.pop("commands_in_chat", None)
            _print(r)
        else:
            _print(T.record(a.id or "", a.result or "", a.notes, json.loads(a.values), json.loads(a.answers), a.who))
    elif a.cmd == "sim":
        from . import sim as S
        bdir = config.sim_bridge_dir()
        sim = S.ensure(bdir)
        sys.stderr.write(f"ls6t simulator on {bdir} (Ctrl-C to stop)\n")
        try:
            t0 = time.time()
            while not a.seconds or time.time() - t0 < a.seconds:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        if sim:
            sim.stop()
    elif a.cmd == "run-request":
        from . import runner
        st = runner.run_request(Path(a.folder))
        return 0 if st.get("state") == "done" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
