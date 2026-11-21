"""mcp_server.py — MCP server: lets LLM-based tools run Raman measurements on a HORIBA instrument through LabSpec 6.
Version: 2026.11.20.39

Tools (all JSON):
  study_format      the study format, the profile's choices and limits, the bridge's state. Read it first.
  validate_studies  static checks of {name: study} with time estimates (fix every error before running)
  run_studies       starts a request (one runner process; the instrument does one request at a time)
  study_status      a request's results, or its progress while it runs
  cancel            stops a request (the current acquisition or move stops)
  jobs              recent requests and which one holds the instrument
  instrument        the bridge and the instrument now (status), the discovery dump (discover), resume after a halt
  snapshot          a video frame now, with the laser position marked (optionally with the beam showing)
  alignment_status  per laser: the alignment baseline, the last check, its age
  spot_status       the beam's remembered offsets per objective and laser
  references        spectral corrections, white-light backgrounds, beam offsets, alignment state (read-only)
  get_inputs        the bridge commands of one study, without running
  run_benchmarks    the benchmarks on the simulator (known answers)
  get_file          a written file as base64
  get_image         a PNG of a study (spectra with fits, map images), as an image
  hardware_test     only in test mode (LS6T_TEST_MODE=1, `ls6t connect APP --test`): TESTING.md with the maintainer

Run:  ls6t-mcp            (stdio, what the Claude app starts)
      ls6t-mcp --http 8771 [--host 127.0.0.1]
Needs the MCP Python SDK: pip install "mcp>=1.2".
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import inspect

from . import api, testmode

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Context, Image
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
    _V2 = True
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Context, Image
    from mcp.server.fastmcp.exceptions import ToolError
    _V2 = False

INSTRUCTIONS = """labspec-translator runs Raman measurements on a HORIBA XploRA through LabSpec 6. A script inside LabSpec (the
bridge) carries out each command: the operator sees every move and spectrum, confirms the first laser exposure and
can stop everything there. Fits, bands, maps and depth profiles are computed here from the exported spectra.
Only these tools reach the instrument. Never use scripts, shell commands, COM/ActiveX (NFActiveX) or the bridge's
files: they bypass the operator's checks and can freeze LabSpec. If no tool does it, say so; do not look for a way.
Workflow: study_format once (this instrument's lasers, gratings, filters, objectives, limits; is the bridge
running); write {name: study}; validate_studies and fix every error; run_studies, then study_status until done.
Kinds: spectrum (points), map (XY/Z grids), depth (spectra down through the sample, interfaces, refractive-index
correction), series, calibration, alignment, spot, focus, move (no laser), guide, background, filters, cutoff.
Start of a session: alignment_status / an 'alignment' study per laser (guides the operator through AutoAlignment);
a 'calibration' on Si or polystyrene (offset and scale of the spectral zero; adjust 'labspec' guides the operator,
'ls6t' remembers a correction applied to later spectra); a 'spot' study if the beam must hit small features;
a 'background' (white light on, laser off) to subtract with acquire.subtract_background. references shows them.
Before trusting numbers check 'reliable' (false: say why from 'unreliable_because'; do not report those numbers),
'warnings', 'checks' (saturation, detector temperature, set-up read-back, range, calibration) and peaks' 'snr'.
Saturated: a stronger filter or shorter exposure; weak: longer exposure or more accumulations.
Stage and focus: offsets (µm) from the home spot (where the operator left the stage, or where a 'move' stayed); Z
from the focus (operator's, autofocus, or a 'focus' scan on the Raman line); at the end Z returns to the focus,
then XY home. The beam drifts from LabSpec's marker: 'spot' finds and remembers it (video with the beam, or the
Raman signal over a small feature: snapshots list 'features'); move.to_um then aims the beam at a picked point.
Manual adjustments (power at the source, focus by hand): a 'guide' study; the bridge measures several times a
second and shows the operator STOP in LabSpec when the goal is met; report the result afterwards.
Safety: the filter (power) is capped per laser and lower for samples marked sensitive; moves are limited; ask the
user before raising power or long maps. If the bridge is not running, ask the user to start ls6t_bridge.vbs.
Polling costs tokens: past about 5 minutes, tell the user when a run should finish and stop; then study_status.
Direct requests: laser, grating, filter, objective, hole, slit: instrument set; camera view: instrument camera_on
/off; the beam on for viewing: instrument laser_on/off; a picture: snapshot; the state: instrument status; a
spectrum, a stage move, focus: a study. PNGs: "images": true, get_image. Units: cm-1, µm, s, counts."""

if testmode.enabled():
    INSTRUCTIONS += """
TEST MODE: a direct request (camera, one motor, a spectrum, the state): hardware_test call at once (its description
maps them). The tests of TESTING.md: hardware_test plan, then per test step, act with the maintainer, record."""

server = _Server("labspec-translator", instructions=INSTRUCTIONS)


def _errors_said(fn):
    """A tool's failure with its text (SDK 2.x otherwise answers 'Error executing tool X' only)."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*a, **k):
            try:
                return await fn(*a, **k)
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"{fn.__name__} failed: {type(exc).__name__}: {exc}"[:2000]) from exc
        return awrapper

    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"{fn.__name__} failed: {type(exc).__name__}: {exc}"[:2000]) from exc
    return wrapper


async def _keepalive(ctx, fn, *a, every_s: float = 5.0):
    """Runs a blocking call in a thread and reports progress every few seconds meanwhile, so the app does not give
    up on a call that waits for LabSpec (2026-11-20: a 30.9 s set completed on the instrument, but the app had
    dropped it: 'No client-side handler found')."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *a))
    waited = 0.0
    while True:
        done, _ = await asyncio.wait({task}, timeout=every_s)
        if done:
            return task.result()
        waited += every_s
        try:
            await ctx.report_progress(waited, None, f"waiting for LabSpec ({waited:.0f} s)")
        except Exception:  # noqa: BLE001 - no progress token, an older client: the wait goes on
            pass


_tool = server.tool


def _tool_said(*a, **k):
    deco = _tool(*a, **k)
    return lambda fn: deco(_errors_said(fn))


server.tool = _tool_said


def _wait(w) -> float:
    return min(max(float(w), 0.0), 50.0)


@server.tool()
def study_format() -> dict:
    """The study format (kinds, keys, units, conventions, an example), this instrument's choices and limits from its
    profile, and 'status' (versions, whether the LabSpec bridge is running, which request holds the instrument)."""
    return api.study_format()


@server.tool()
def validate_studies(studies: dict) -> dict:
    """Static checks of {name: study}: per study ok, errors (each says the fix), warnings, the time estimate
    (minutes, points, detector windows) and the acquisition range in nm."""
    return api.validate(studies)


@server.tool()
def run_studies(studies: dict, wait_s: float = 45) -> dict:
    """Starts a request (validated first) and returns after at most wait_s (<= 50) s: {request, state, done,
    studies: {name: result}, pending, progress: {name: {stage, point, points, eta_min}}, next}."""
    return api.run(studies, _wait(wait_s))


@server.tool()
def study_status(request: str, wait_s: float = 30) -> dict:
    """A request's results and the progress of its unfinished studies, after waiting up to wait_s (<= 50) s for it
    to finish."""
    return api.status(request, _wait(wait_s))


@server.tool()
def cancel(request: str) -> dict:
    """Stops a request: the acquisition or move in progress stops, no further study starts. Partial results stay
    (marked unreliable, complete: false)."""
    return api.cancel(request)


@server.tool()
def jobs(limit: int = 20, active: bool = False) -> dict:
    """Recent requests with their state, and the request that holds the instrument now (if any)."""
    return api.jobs(limit, active)


@server.tool()
async def instrument(ctx: Context, action: str = "status", camera: str | int | None = None,
                     setup: dict | None = None, seconds: float | None = None) -> dict:
    """status: the bridge's version and the instrument's state now (laser, grating, filter, objective, stage,
    detector temperature, live_video, the active camera, laser_open). set: change the set-up now without
    measuring, setup {laser_nm, grating, filter, objective, hole_um, slit_um} (any of them, named as people say
    them: objective "x100", filter "10%"); the filter stays within the caps; the objective turret is manual on
    this instrument: LabSpec asks the operator to change the lens by hand and confirm (LabSpec's objective is a
    record, not a reading: say so when it matters); a refused request changes nothing; each move waits for its motor;
    nothing fires the laser; returns what LabSpec read back. laser_on (seconds, up to the
    profile's limit; default 30): the laser shutter open so the beam shows on the camera, after the operator's Yes
    in LabSpec; the bridge closes it when the time is up, at laser_off, at a halt, or before any other command.
    laser_off. camera_on / camera_off (camera: a number, or a name in study_format's profile.cameras such as
    "top"; none: the active one): LabSpec's live view; for a picture use snapshot. Another camera than the
    active one is switched only where the profile allows it (a video restart, checked); otherwise, and whenever an
    answer refuses, it says what the operator does: relay it, do not retry. end_bridge: ends the bridge cleanly
    (before the operator uses LabSpec's video buttons, which end a running script); a person starts it again.
    discover: runs the discovery
    and proposes a machine profile (a person applies it). resume: asks the operator to lift a halt. last_error:
    LabSpec's last error. calibration: LabSpec's spectrometer calibration per grating (read only). autocal_info:
    LabSpec's AutoCalibration script(s) here and whether they can run unattended (nothing runs)."""
    return await _keepalive(ctx, api.instrument, action, camera, setup, seconds)


@server.tool()
def snapshot(with_laser: bool = False, extended: bool = False):
    """A video frame of the sample now, with the laser position marked (red cross), returned as an image plus its
    details (stage position, field of view, files). with_laser: true opens the laser shutter for about a second so
    the beam itself shows (only with the current filter within the profile's caps, after the operator's Yes in
    LabSpec). Use it to check where the beam is before a calibration or a measurement. extended: true takes
    LabSpec's extended video (a wider still image stitched over the area set in LabSpec; the stage moves; only when
    the profile allows it and the operator says Yes; no laser)."""
    import json
    r = api.snapshot(with_laser, extended)
    if not r.get("ok") or not r.get("path"):
        return r
    return [Image(path=r["path"]), json.dumps({k: v for k, v in r.items() if k != "what"}, default=str)]


@server.tool()
def references() -> dict:
    """What ls6t keeps between sessions: the spectral corrections per laser and grating (from 'calibration' with
    adjust 'ls6t'), the white-light backgrounds by name (for acquire.subtract_background), the beam offsets, the
    last alignment check per laser, and the LabSpec configurations a maintainer saved. Read-only."""
    return api.references()


@server.tool()
def spot_status() -> dict:
    """Where the beam is relative to LabSpec's laser marker, remembered per objective and laser: the offset (µm),
    how it was measured, its age and the drift since the first record. Read-only; a 'spot' study measures it."""
    return api.spot_status()


@server.tool()
def alignment_status() -> dict:
    """Per laser: the fixed alignment settings and the baseline recorded after the last AutoAlignment, the last
    alignment check (time, passed, ratio to baseline) and its age. Read-only: baselines are recorded by the
    maintainer on the command line."""
    return api.alignment_status()


@server.tool()
def get_inputs(study: dict, name: str = "study") -> dict:
    """The bridge commands one study would send (setup moves, the first points), its range in nm and its time
    estimate, without running anything."""
    return api.inputs(study, name)


if testmode.enabled():
    @server.tool()
    def hardware_test(action: str, step: str | None = None, verb: str | None = None, params: dict | None = None,
                      result: str | None = None, notes: str = "", values: dict | None = None,
                      answers: dict | None = None, who: str = "", timeout_s: float = 120) -> dict:
        """Direct control and the instrument tests of TESTING.md (test mode only). Direct requests, no lookup:
        camera live view: call camera {action: on|off}; laser, grating, filter, objective, hole, slit: call move
        {motor: "filter" (a role, or LabSpec's name), mode: "label"|"value", value: "1%"}; the stage: call move_xy
        {x_um, y_um} (absolute µm; status says where it is) or move {motor: "z", mode: "value", value}; one
        spectrum: call acquire {exposure_s, accumulations, from_nm, to_nm}; the state: call status; a picture:
        the snapshot tool. Use plan / step only to run the numbered tests. Actions:
        plan: every test with its last result and the next one to do. step (step="T3.4"): its instructions, pass
        criteria, the open questions it answers, and how its commands map to these tools. call (verb, params,
        step): one bridge command (ping, status, discover, move {motor, mode: value|label|index, value}, move_xy
        {x_um, y_um}, autofocus, acquire {exposure_s, accumulations, from_nm, to_nm, mode, laser_off}, view, save,
        remove, confirm {text}, message {text}, safe, video {laser_s, during_acq_s, extended}, map_native, guide,
        notice, resume, last_error): moves within the limits, the filter by label within the caps; anything that
        can fire the laser asks the operator in LabSpec first; acquire returns a summary of the spectrum. record
        (step, result: pass|fail|partial|skipped|blocked, notes, values {name: value}, answers {"Q5": "..."}, who):
        appends to the test log and rewrites the report (TESTING.md's results table filled in). report: the
        report's path and counts. status: versions, settings, the bridge. last_error: LabSpec's last error.
        config (params {action: list|save|load, name}): LabSpec's configuration files.
        Never reach LabSpec another way (scripts, shell, COM/ActiveX, the bridge's files). Go in order; tell the
        maintainer what to do and wait for them; ask what only a person can see (the video
        window, sounds, the stage moving); never record a pass you did not check. Profile changes, alignment
        baselines, clearing references and installing the scripts stay with a person: give the exact command."""
        return testmode.hardware_test(action, step, verb, params, result, notes, values, answers, who, timeout_s)


@server.tool()
def run_benchmarks(names: list | None = None, wait_s: float = 45) -> dict:
    """The benchmarks on the simulator (never the instrument): per case the checks with expected and measured
    values. Takes about a minute for all of them."""
    return api.bench(names, max(_wait(wait_s), 45.0))


@server.tool()
def get_file(path: str, max_mb: float = 20) -> dict:
    """A file written by a request (spectrum, map data, LabSpec file, discovery dump) as base64."""
    return api.read_file(path, max_mb)


@server.tool()
def get_image(path: str) -> Image:
    """An image written by a study or a snapshot (results images.<name>.path, a snapshot's frame.marked.png or LabSpec's
    own snapshot files), as an image you can see and show; images over about 1 MB or in other formats come as a
    reduced copy (also saved next to it as <name>.view.png/.jpg)."""
    data, fmt, _ = api.image_for_view(path)
    return Image(data=data, format=fmt)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", type=int, help="serve MCP over streamable HTTP on this port (default: stdio)")
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    if a.http is None:
        server.run()
    elif _V2:
        server.run(transport="streamable-http", host=a.host, port=a.http)
    else:
        server.settings.host, server.settings.port = a.host, a.http
        server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
