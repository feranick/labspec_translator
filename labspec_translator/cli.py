"""cli.py — the command line. Every command prints JSON; '-' reads the studies from stdin.
Version: 2026.11.19.9

  ls6t status [REQUEST] [--wait S]   versions, the bridge, the profile; or a request's studies
  ls6t format                        the study format
  ls6t validate studies.json         static checks
  ls6t run studies.json [--wait S]   start a request (a detached runner) and wait up to S seconds
  ls6t inputs studies.json NAME      the bridge commands of one study, without running
  ls6t cancel REQUEST                stop a request (the current acquisition or move stops)
  ls6t jobs [--limit N] [--active]   recent requests
  ls6t bench [NAMES...]              the benchmarks (on the simulator)
  ls6t instrument status|discover|resume
  ls6t instrument snapshot [--laser] a video frame with the laser position marked (--laser: the beam shows)
  ls6t bridge install [--dest DIR]   write the LabSpec scripts configured for this machine
  ls6t bridge call VERB [k=v ...]    one bridge command, for the maintainer's tests (TESTING.md); acquire and
                                     autofocus fire the laser and need --laser
  ls6t profile show|default|import DUMP
  ls6t profile set KEY VALUE         one value (JSON), e.g. video.laser_offset_um "[1.4, -0.6]"
  ls6t alignment status              per laser: baseline, last check, age
  ls6t spot status | forget [--objective O --laser NM]   the beam's remembered offsets
  ls6t references show | clear correction [LASER@GRATING] | clear background NAME
                                     spectral corrections, white-light backgrounds, beam offsets, alignment
  ls6t alignment record REQUEST [--study NAME] [--attempt N]
                                     after a successful AutoAlignment: the check becomes the laser's baseline
  ls6t sim [--seconds S]             run the simulator in the foreground (LS6T_MODE=sim tests by hand)
  ls6t run-request FOLDER            what the detached runner runs
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, api, config
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
    s.add_argument("action", choices=["status", "discover", "resume", "snapshot"])
    s.add_argument("--laser", action="store_true", help="snapshot: open the laser shutter briefly (asks in LabSpec)")
    s = sub.add_parser("bridge")
    s.add_argument("action", choices=["install", "call"])
    s.add_argument("verb", nargs="?")
    s.add_argument("params", nargs="*", help="key=value")
    s.add_argument("--dest")
    s.add_argument("--timeout", type=float, default=120)
    s.add_argument("--laser", action="store_true", help="allow acquire and autofocus (the laser fires)")
    s = sub.add_parser("profile")
    s.add_argument("action", choices=["show", "default", "import", "set"])
    s.add_argument("dump", nargs="?", help="import: the discovery dump; set: the key")
    s.add_argument("value", nargs="?", help="set: the value (JSON)")
    s = sub.add_parser("references")
    s.add_argument("action", choices=["show", "clear"])
    s.add_argument("what", nargs="?", choices=["correction", "background", "spot"])
    s.add_argument("name", nargs="?")
    s = sub.add_parser("spot")
    s.add_argument("action", choices=["status", "forget"])
    s.add_argument("--objective")
    s.add_argument("--laser", type=float)
    s = sub.add_parser("alignment")
    s.add_argument("action", choices=["status", "record"])
    s.add_argument("request", nargs="?")
    s.add_argument("--study")
    s.add_argument("--attempt", type=int)
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
            r = api.snapshot(with_laser=a.laser)
            _print(r)
            return 0 if r.get("ok") else 1
        _print(api.instrument(a.action))
    elif a.cmd == "bridge":
        if a.action == "install":
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
        elif a.action == "set":
            if not a.dump or a.value is None:
                ap.error("profile set needs KEY VALUE")
            _print(api.profile_set(a.dump, a.value))
        else:
            if not a.dump:
                ap.error("profile import needs the discovery dump file")
            prof, notes = P.import_discovery(P.read_text(a.dump))
            _print({"ok": True, "written": str(P.save(prof)), "notes": notes})
    elif a.cmd == "references":
        if a.action == "show":
            _print(api.references())
        else:
            if not a.what:
                ap.error("references clear needs: correction [LASER@GRATING] | background NAME | spot")
            _print(api.references_clear(a.what, a.name))
    elif a.cmd == "spot":
        _print(api.spot_status() if a.action == "status" else api.spot_forget(a.objective, a.laser))
    elif a.cmd == "alignment":
        if a.action == "status":
            _print(api.alignment_status())
        else:
            if not a.request:
                ap.error("alignment record needs the request folder")
            _print(api.alignment_record(a.request, a.study, a.attempt))
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
