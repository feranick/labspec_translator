"""mcp_server.py — MCP server: lets LLM-based tools run Raman measurements on a HORIBA instrument through LabSpec 6.
Version: 2026.11.19.1

Tools (all JSON):
  study_format      the study format, the profile's choices and limits, the bridge's state. Read it first.
  validate_studies  static checks of {name: study} with time estimates (fix every error before running)
  run_studies       starts a request (one runner process; the instrument does one request at a time)
  study_status      a request's results, or its progress while it runs
  cancel            stops a request (the current acquisition or move stops)
  jobs              recent requests and which one holds the instrument
  instrument        the bridge and the instrument now (status), the discovery dump (discover), resume after a halt
  get_inputs        the bridge commands of one study, without running
  run_benchmarks    the benchmarks on the simulator (known answers)
  get_file          a written file as base64
  get_image         a PNG of a study (spectra with fits, map images), as an image

Run:  ls6t-mcp            (stdio, what the Claude app starts)
      ls6t-mcp --http 8771 [--host 127.0.0.1]
Needs the MCP Python SDK: pip install "mcp>=1.2".
"""
from __future__ import annotations

import argparse
import functools

from . import api

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Image
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
    _V2 = True
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image
    from mcp.server.fastmcp.exceptions import ToolError
    _V2 = False

INSTRUCTIONS = """labspec-translator runs Raman measurements on a HORIBA instrument (an XploRA) through LabSpec 6:
single spectra and multi-point sets, maps (XY grids and depth profiles), time series and a silicon calibration
check. A small script inside LabSpec (the bridge) carries out each command, so the operator sees every move and
spectrum in LabSpec, confirms before the first laser exposure and can stop everything there. Peak fits, bands and
map images are computed here from the exported spectra.
Workflow: call study_format once (it shows the lasers, gratings, filters, objectives and limits of this instrument,
and whether the bridge is running); write {name: study}; validate_studies and fix every error; run_studies, then
study_status until done. Run the 'calibration' study on a silicon reference at the start of a session.
Before trusting numbers check 'reliable' (false: say why from 'unreliable_because' and do not report the numbers
as results), 'warnings', 'checks' (saturation, detector temperature, setup read-back, range, calibration) and each
peak's 'snr'. A saturated spectrum needs a stronger filter or a shorter exposure; a weak one longer exposure or
more accumulations.
Safety: positions are offsets (µm) from where the operator left the stage, Z from the operator's focus; the
filter (laser power) is capped per laser and lower for samples marked sensitive. Ask the user before raising power
or running long maps. If the bridge is not running, ask the user to start ls6t_bridge.vbs in LabSpec.
Polling costs tokens: run_studies returns 'pending' with an estimate for long runs; when a run needs more than
about 5 minutes, tell the user when it should finish and stop; collect it later with study_status.
Pictures: "images": true in a study writes PNGs (results images.<name>.path); get_image shows each.
Units: Raman shift cm-1, positions µm, time s, intensities counts."""

server = _Server("labspec-translator", instructions=INSTRUCTIONS)


def _errors_said(fn):
    """A tool's failure with its text (SDK 2.x otherwise answers 'Error executing tool X' only)."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"{fn.__name__} failed: {type(exc).__name__}: {exc}"[:2000]) from exc
    return wrapper


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
def instrument(action: str = "status") -> dict:
    """status: the bridge's version and the instrument's state now (laser, grating, filter, objective, stage,
    detector temperature). discover: runs the discovery in LabSpec and proposes a machine profile (a person applies
    it). resume: asks the operator in LabSpec to lift a halt after they stopped the bridge."""
    return api.instrument(action)


@server.tool()
def get_inputs(study: dict, name: str = "study") -> dict:
    """The bridge commands one study would send (setup moves, the first points), its range in nm and its time
    estimate, without running anything."""
    return api.inputs(study, name)


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
    """A PNG written by a study (results images.<name>.path), as an image you can see and show."""
    return Image(path=str(api.image_path(path)))


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
