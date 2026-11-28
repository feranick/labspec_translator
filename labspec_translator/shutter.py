"""shutter.py — when ls6t may rely on the laser shutter.

2026-11-23 on the XploRA: a laser_off 'shutter' background kept 86 % of the Si signal; the shutter had been moved by
value and never checked. The bridge now moves it by label and reads it back, but a read-back says where LabSpec thinks
the shutter is, not that the beam is blocked, and LabSpec may hold the bridge during an acquisition. The only proof is
spectral: with the shutter closed, the Si line must be gone. `ls6t instrument shutter-verify` measures that (a
reference with the laser, then the same with the shutter closed) and, when it passes, writes a record here
(shutter_verified.json, per laser). Every use of the shutter (laser_off 'shutter', laser_on, snapshots with the laser,
spot 'shutter') is refused without a valid record: missing, older than shutter.verify_max_days, for another motor or
way of moving it, or from a bridge whose shutter handling has changed since (REV).
"""
from __future__ import annotations

import time

from . import store

REV = 1                  # the bridge's shutter handling: a change here asks for a new verification
FILE = "shutter_verified.json"
MAX_DAYS = 30.0


def _key(laser_nm) -> str:
    try:
        return f"{float(laser_nm):g}"
    except (TypeError, ValueError):
        return "?"


def records() -> dict:
    return store.read(FILE).get("lasers") or {}


def record(rec: dict) -> dict:
    d = store.read(FILE)
    d.setdefault("lasers", {})[_key(rec["laser_nm"])] = rec
    d.setdefault("history", []).append(rec)
    d["history"] = d["history"][-50:]
    store.write(FILE, d)
    return rec


def attempt(a: dict) -> dict:
    """Every verification attempt, passed or not (2026-11-23: passes and fails alternated around the limit; a pass
    after failures must show)."""
    d = store.read(FILE)
    a = dict(a, time=time.time(), date=time.strftime("%Y-%m-%dT%H:%M:%S"))
    d.setdefault("attempts", []).append(a)
    d["attempts"] = d["attempts"][-100:]
    store.write(FILE, d)
    return a


def attempts(laser_nm=None) -> list:
    """Every attempt kept for this laser (or all), oldest first."""
    al = store.read(FILE).get("attempts") or []
    return [a for a in al if laser_nm is None or _key(a.get("laser_nm")) == _key(laser_nm)]


def _profile_nm(prof: dict, laser_nm):
    """The profile's laser nearest to laser_nm (within 5 nm), or laser_nm itself."""
    try:
        want = float(laser_nm)
    except (TypeError, ValueError):
        return laser_nm
    have = [float(v["nm"]) for v in (prof.get("lasers") or {}).values() if isinstance(v, dict) and v.get("nm")]
    near = min(have, key=lambda v: abs(v - want), default=None)
    return near if near is not None and abs(near - want) <= 5.0 else want


def _gate_detail(prof: dict, laser_nm=None) -> tuple[str | None, str | None]:
    """(reason_code, message) for shutter gating: (None, None) when ls6t may rely on the shutter for this laser,
    else one of ('no_motor', 'missing', 'mismatch', 'rev', 'expired') with the human-readable explanation."""
    motor = (prof.get("motors") or {}).get("laser_shutter")
    if not motor:
        return "no_motor", "no laser shutter motor in the profile (motors.laser_shutter)"
    by = str((prof.get("shutter") or {}).get("by", "label"))
    recs = records()
    rec = None
    if laser_nm is not None:
        # the laser as the profile knows it (2026-11-26: a study asking 638 found no record for 637.75 and the hold
        # was refused, "not been shown to block the 638 nm laser")
        rec = recs.get(_key(laser_nm)) or recs.get(_key(_profile_nm(prof, laser_nm)))
    elif recs:
        rec = max(recs.values(), key=lambda r: r.get("time", 0))
    how = ("run `ls6t instrument shutter-verify` with silicon under the objective, in focus (TESTING T1.2 step 2): "
           "the Si line must disappear with the shutter closed")
    what = f"the {float(laser_nm):g} nm laser" if laser_nm is not None else "any laser"
    if not rec:
        return "missing", f"the laser shutter has not been shown to block {what} on this instrument: {how}"
    if rec.get("motor") != motor or rec.get("by") != by:
        return "mismatch", (f"the shutter was verified as {rec.get('motor')!r} moved by {rec.get('by')}, the profile "
                            f"now has {motor!r} by {by}: {how}")
    if int(rec.get("rev", 0)) != REV:
        return "rev", (f"the bridge's shutter handling has changed since the last verification "
                       f"({rec.get('version')}): {how}")
    days = (time.time() - float(rec.get("time", 0))) / 86400.0
    mx = max_days(prof, rec)
    if days > mx:
        return "expired", (f"the shutter was last shown to block {what} {days:.0f} days ago (more than {mx:g}"
                           + (": shorter, as it leaked in the 30 days before that pass"
                              if rec.get("recent_leaks") else "")
                           + f"): {how}")
    return None, None


def gate(prof: dict, laser_nm=None) -> str | None:
    """None when ls6t may rely on the shutter for this laser; else why not (said to the LLM and the person)."""
    return _gate_detail(prof, laser_nm)[1]


MAX_DAYS_AFTER_LEAK = 7.0


def expired_note(prof: dict, laser_nm=None) -> str | None:
    """When a previously verified shutter record for this laser has expired (older than max_days), returns the
    gate reason; None if valid, never verified, or invalidated for another reason."""
    if laser_nm is None:
        return None
    code, msg = _gate_detail(prof, laser_nm)
    return msg if code == "expired" else None


def max_days(prof: dict, rec: dict | None) -> float:
    """How long a pass holds: shutter.verify_max_days (30), or shutter.verify_max_days_after_leak (7) when the laser
    leaked in the 30 days before it (Nicola, 2026-11-27: 637.75 leaked 0.38 % on 11-23, passed at 0.057 % on 11-27)."""
    sh_ = prof.get("shutter") or {}
    mx = float(sh_.get("verify_max_days", MAX_DAYS))
    if (rec or {}).get("recent_leaks"):
        mx = min(mx, float(sh_.get("verify_max_days_after_leak", MAX_DAYS_AFTER_LEAK)))
    return mx


def default_laser_off(prof: dict, laser_nm=None) -> str:
    """'shutter' only where it has been shown to block; else the operator blocks the laser by hand."""
    return "shutter" if gate(prof, laser_nm) is None else "operator"
