"""profile.py — the machine profile: what the instrument has (from the discovery script) and the limits a person sets.

The profile is a JSON file (LS6T_PROFILE, default ~/ls6t/profile.json). Studies never change it: an LLM can only
ask for what the profile allows. Without a file, the built-in XploRA defaults below apply and every result says so
in a warning. The one route to a person's profile: `ls6t profile propose` (profile.proposed.json from the bridge's
inventory, keeping the current limits), edit it, `ls6t profile import profile.proposed.json` (checked, reviewed).
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

from . import config

PROFILE_VERSION = 1

DEFAULT_PROFILE: dict = {
    "profile_version": PROFILE_VERSION,
    # a generic XploRA for the simulator and the tests: refused on the instrument (format.validate), where the
    # profile comes from the bridge's inventory or a person's import
    "source": "built-in generic XploRA defaults (simulator only; not this instrument)",
    "system": "XploRA",
    # LabSpec motor names, from the discovery script. Filter and objective names vary by configuration.
    "motors": {"laser": "Laser", "grating": "Grating", "filter": "Filter", "objective": "Objective",
               "hole": "Hole", "slit": "Slit", "x": "X", "y": "Y", "z": "Z",
               "laser_shutter": None,     # needed only for video snapshots with the laser on (from discovery)
               "laser_pol": None, "raman_pol": None,    # the polarisers, when the instrument has them (discovery)
               "illumination": None,      # bright / dark field (XploRA: IlluminatorV), from discovery
               "illumination_light": None,    # the light source's mode (XploRA: uLight), read only
               "safety_shutter": None},       # the interlock's shutter, if LabSpec has one (OLConfig lists a
    #                                           'SafetyShutter' motor): read only, reported by status
    # the enclosure: interlocked (the laser off whenever a door is open) makes the per-request laser go-ahead in
    # LabSpec optional (Nicola, 2026-11-27); set by a person: ls6t profile set safety.interlocked_enclosure true
    "safety": {"interlocked_enclosure": False, "laser_go_ahead": True},
    # the laser shutter's positions (OLConfig on the XploRA: Off / On); ls6t moves it by label and reads it back
    # (2026-11-23: moved by value 0, unchecked, it did not block the beam). by: label or value
    "shutter": {"off": "Off", "on": "On", "by": "label", "verify_max_days": 30, "max_leak_frac": 0.005},
    # LabSpec's AutoExposure is a template value (Template SET/GET_VALUE "AutoExposure", 0 / 1, in the third
    # argument); SET_VALUE acts on the live settings (TESTING T3.19, passed 2026-11-23): for a fixed exposure the
    # bridge switches it off and puts it back after. false: AE is left alone (a different exposure stops the study)
    "autoexposure": {"switch_off": True},
    # the polarisers' positions by motor role (from discovery), e.g. {"laser_pol": ["Circular", "Horizontal", ...]}
    "polarizers": {},
    "lasers": {"532": {"nm": 532.0, "label": "532", "power_mw": None, "max_filter_pct": 100},
               "638": {"nm": 638.0, "label": "638", "power_mw": None, "max_filter_pct": 100},
               "785": {"nm": 785.0, "label": "785", "power_mw": None, "max_filter_pct": 100}},
    # window_nm: the spectral width of one detector window (for time estimates and the extended-range count)
    # generic estimates (the simulator uses them); the XploRA measured 600 197.8, 1200 85.4-87.4, 1800 46.7 nm at
    # 638 nm (2026-11-23): its profile sets them (ls6t profile set gratings.<g>.window_nm <nm>, as ls6t's warnings say)
    "gratings": {"600": {"window_nm": 78.0}, "1200": {"window_nm": 39.0},
                 "1800": {"window_nm": 26.0}, "2400": {"window_nm": 19.5}},
    "filters": {"100%": 100.0, "50%": 50.0, "25%": 25.0, "10%": 10.0, "1%": 1.0, "0.1%": 0.1},
    "objectives": ["x10", "x50", "x100"],
    "manual_objective": True,      # LabSpec asks the operator to turn the turret
    # after ls6t's own question, the move is sent with MOTOR_NO_MESSAGE so LabSpec does not ask again (false: it does)
    "suppress_labspec_prompt": True,
    # motors moved the blocking way (MoveMotor without NO_WAIT), as HORIBA's scripts move them: by role
    # (the grating's blocking move returned in 0.02-0.04 s like the other way, 2026-11-20: none by default)
    "blocking_motors": [],
    "holes_um": None,              # null: any value LabSpec accepts
    # the confocal hole the simple tools (calibrate_si, measure_spectrum, laser_check) use per objective unless asked
    # otherwise: calibrate at the hole you measure with (Nicola, 2026-11-27: Si 0.53 cm-1 apart between holes 100
    # and 300 at 532, 1.15 at 638; steadier and narrower at 100, ~1000 counts/s on Si at 1 %)
    "default_hole_um": {"x100": 100},
    "slits_um": None,
    # e_per_count: the CCD's electrons per count (the spike test's counting-noise floor); 1 unless known
    "detector": {"full_scale": 65535, "max_temp_c": -50.0, "pixels": 1024, "e_per_count": 1.0},
    "stage": {"x_um": None, "y_um": None, "z_um": None,    # absolute limits [min, max]; null: only the relative ones
              "z_into_sample": None,                      # "+" or "-": which Z direction focuses deeper (TESTING)
              "retract_for_objective_change": False},     # lower the stage by limits.retract_um before a lens change
    "limits": {"max_xy_um": 5000.0,          # farthest a study may move from the home spot (the distance, not per axis)
               "max_dz_um": 100.0,           # farthest from the operator's focus (Z is always relative)
               "max_exposure_s": 600.0, "max_accumulations": 500, "max_points": 10000,
               "max_minutes": 480.0, "max_filter_pct": 100.0, "sensitive_max_filter_pct": 10.0,
               "max_laser_on_s": 60.0},     # longest the laser may be left on for viewing (instrument laser_on)
    # measured on the XploRA (2026-11-21): laser 1.6-2.8 s, grating 0.1-0.3 s (5 s kept: whether the turret turns
    # then is still to see, TESTING T3.16), filter ~1 s, pinhole 2-6 s, slit 2-3 s; the objective: the operator
    "timing": {"acquire_settle_s": 0.0, "status_before_each": False, "held_overhead_s": 1.0, "command_s": 0.3, "move_s": 2.0, "laser_s": 3.0, "grating_s": 5.0, "objective_s": 20.0,
               "xy_um_per_s": 2000.0, "xy_settle_s": 0.3, "autofocus_s": 10.0, "readout_s": 0.5,
               "window_move_s": 2.0},
    # video snapshots: where the beam really is relative to LabSpec's laser position (µm, measured: TESTING T5b),
    # how LabSpec packs colour pixels (colorref 0x00BBGGRR, rgb 0x00RRGGBB, or gray), which axis varies fastest in
    # the pixel data, the image formats LabSpec is asked to save, the settle time and the laser time (s)
    "video": {"laser_offset_um": [0.0, 0.0], "marker": "stage", "packing": "colorref", "order": "x_fastest",
              "formats": ["png", "jpg", "bmp", "tif"], "settle_s": 1.5, "laser_s": 1.0, "max_laser_s": 5.0,
              "camera": None, "allow_extended": False, "extended_max_s": 600.0,
              # switching cameras by a video restart (stop HORIBA's way, select, start, check while it runs): off
              # until tried at the instrument (TESTING T3.14; camera 0 was ignored before, Q44). Off: ls6t refuses a
              # camera other than LabSpec's active one.
              "switch_camera": False,
              # stopping the video as HORIBA's service scripts do (STOP, then GetMotorStatus "Video" until 0, no
              # video query afterwards until the bridge starts it again, 5 s before that start): it works on the
              # XploRA (TESTING T3.12-T3.13, 2026-11-20); ls6t's earlier way hung the bridge twice
              "stop_from_script": True,
              # LabSpec's cameras by name, as a person sees them, e.g. {"top": 0, "side": 1}: requests may name them
              "cameras": {}},
    # alignment checks (kind 'alignment'): per laser the fixed settings and the baseline the maintainer recorded
    # right after a successful AutoAlignment (`ls6t alignment record`); a check passes when the Si signal is at least
    # min_ratio of the baseline and its width at most max_fwhm_ratio of it; studies warn when a laser's last check
    # failed or is older than max_age_h (only once a baseline exists)
    "alignment": {"settings": {}, "baselines": {}, "min_ratio": 0.7, "max_fwhm_ratio": 1.3, "max_age_h": 24.0,
                  "max_retries": 1},
    # guided adjustments (kind 'guide'): limits, and the operator's panel (dialog, or the status bar)
    "guide": {"max_s": 300.0, "max_exposure_s": 5.0, "ui": "dialog", "saturation_frac": 0.9},
    # the laser spot (kind 'spot'): offsets larger than max_offset_um are not remembered; a change larger than
    # drift_warn_um from the previous record is reported; records older than max_age_h are flagged stale
    "spot": {"max_offset_um": 10.0, "drift_warn_um": 1.0, "max_age_h": 24.0},
    # spectral calibration: corrections older than max_age_h are flagged; guided LabSpec adjustments repeat at most
    # max_retries times
    # instrument checks: the filters' measured transmission may differ from nominal by filters_tol (fraction); a
    # spectral resolution (FWHM) worse than resolution_tol x its baseline is reported; resolution_cm1 {grating: max
    # FWHM} (optional, e.g. from the instrument's specification) is checked too
    "performance": {"filters_tol": 0.3, "resolution_tol": 1.25, "resolution_cm1": {}},
    # LabSpec's live display of a map or series as it is measured: off until it has passed on the instrument
    # (2026-11-24: the MapEx view froze, then crashed, LabSpec)
    # (on from 2026.11.24.8: the MapEx view passed on the instrument, held and not)
    "display": {"live_view": True, "live_view_while_held": True},
    # approach_nm: every spectrometer move ends from this side (-2: from 2 nm below; 2026-11-25, 600 gr/mm: the
    # repositioning spread 0.15 cm-1 against ~0.5 without, backlash); 0 turns it off. repeatability: per
    # "<laser>@<grating>", {still_sd_cm1, moved_sd_cm1} set by a person (else the last measured is used)
    "spectrometer": {"approach_nm": -2.0, "repeatability": {}, "repeat_jog_nm": None},
    "calibration": {"max_age_h": 24.0, "max_retries": 2,
                    # a video frame before each calibration (false by default: saves ~17-20 s per calibration; a
                    # study's video.before wins)
                    "video_before": False,
                    # LabSpec's own AutoCalibration, run by the bridge (calibration.adjust 'auto'): off until it was
                    # tried with the maintainer (TESTING T4.9); script: its .vbs; sample: what it needs under the
                    # objective, told to the operator first
                    "autocal": {"enabled": False, "script": None, "timeout_s": 1800.0, "sample": None}},
    # LabSpec's own mapping engine (map.engine 'native'): used by 'auto' from native_min_points points; whether
    # SetMappingParams takes absolute stage positions or offsets; the order of the exported values; whether
    # Acq(ACQ_SPECTRAL_IMAGE) takes the interface's exposure (-1) or ours; the time per point beyond the exposure;
    # export_chunk: values read per GetValueSimplePart call when the map is written out (0: one GetValue; discovery
    # turns it on once its probe shows that StartAt and DataSize count values, TESTING Q35)
    "mapping": {"native_min_points": 400, "positions": "absolute", "data_order": "spectral_fastest",
                "point_order": "x_fastest", "acq_time": "interface", "native_point_s": 0.05, "export_chunk": 0},
    # LabSpec's configuration around each request: with restore_config, the bridge saves it (SaveConfig) when a
    # request starts and loads it back (LoadConfig) when the request ends normally
    "session": {"restore_config": False},
    # LabSpec's ICS (intensity correction) is turned off for these "<grating>@<laser nm>": no correction file there,
    # and with ICS on LabSpec stops each acquisition at a modal Error 2070 (2026-11-23: 2400 with 637.75 nm)
    # auto: off where LabSpec's Parameters\ACQUISITION\Responses holds no Response_*_<laser>_<grating>.l6s
    "ics": {"off_for": [], "auto": True},
    "checks": {"min_snr": 10.0, "detect_snr": 5.0, "si_cm1": 520.7, "si_tol_cm1": 1.0, "saturation_frac": 0.98},
}


# how a person confirms the laser shutter motor: the beam is often not visible on the camera (XploRA top camera, even
# at 100 %), so on the spectrum, in two steps: LabSpec's own toggle blocks the beam (2026-11-23, 637.75 nm on Si: the
# line came and went), and ls6t's use of the motor blocks it too (the same day it did not: the Si line stayed in a
# laser_off 'shutter' background). The first alone is not enough.
HOW = ("confirm it on the spectrum, in two steps (TESTING T1.2): 1. LabSpec's real-time display on Si, then the status "
       "bar's Laser On / Off: the Si line must come and go; 2. with the motor set and the bridge restarted, `ls6t "
       "instrument shutter-verify` on Si for each laser: ls6t uses the shutter only after it passes; if it fails, "
       "set the motor back to null: ls6t cannot block the laser on that instrument")


def _merge(base: dict, over: dict, problems: list | None = None, path: str = "") -> dict:
    """The profile file over the defaults. A value whose default is a group of keys must be one in the file too: a
    group stored as text (2026-11-25, `profile set` from PowerShell) is read as JSON if it is JSON, else the default
    is kept and the problem is listed (problems), so a bad value cannot fail every request."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        where = f"{path}.{k}" if path else str(k)
        if isinstance(out.get(k), dict) and not isinstance(v, dict) and v is not None:
            try:
                fixed = json.loads(v) if isinstance(v, str) else None
            except ValueError:
                fixed = None
            if isinstance(fixed, dict):
                v = fixed
                if problems is not None:
                    problems.append(f"{where} was stored as text; read as JSON (set it again to store it properly)")
            else:
                if problems is not None:
                    problems.append(f"{where} should be a group of keys, the profile has {v!r:.80}: ignored, the "
                                    f"default is used. Fix it: ls6t profile set {where} @file.json")
                continue
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("lasers", "gratings", "filters"):
            out[k] = _merge(out[k], v, problems, where)
        else:
            out[k] = copy.deepcopy(v)
    return out


AUTO_MAX_FILTER_PCT = 10.0     # per laser, in a profile built automatically, until a person sets the caps
# the one way a person makes the profile theirs (ls6t status, review_gate, the docs say the same)
REVIEW_STEPS = ("ls6t profile propose (writes profile.proposed.json from the bridge's inventory, keeping the current "
                "limits); with the maintainer, fill in every laser's power_mw and max_filter_pct, the limits, and "
                "motors.laser_shutter once confirmed; then ls6t profile import profile.proposed.json (checks it and "
                "marks it reviewed)")


def latest_dump() -> Path | None:
    """The inventory the bridge writes each time it starts (LS6T_HOME/discovery_latest.txt)."""
    p = config.home() / "discovery_latest.txt"
    return p if p.is_file() else None


def auto_profile(dump: Path) -> dict:
    """A profile from the bridge's inventory, made without a person: the instrument as LabSpec reports it (lasers,
    gratings, filters, objectives, holes, slits, motors) with the default limits, every laser capped at
    AUTO_MAX_FILTER_PCT % and no laser shutter (snapshots with the laser stay off) until a person reviews it
    (`ls6t profile import` or `ls6t profile set`, which end the automatic state)."""
    prof, notes = import_discovery(read_text(dump), base=copy.deepcopy(DEFAULT_PROFILE))
    for v in prof.get("lasers", {}).values():
        v["max_filter_pct"] = min(float(v.get("max_filter_pct") or 100), AUTO_MAX_FILTER_PCT)
    if prof["motors"].get("laser_shutter"):
        notes.append(f"laser shutter motor {prof['motors']['laser_shutter']!r} found but not used until a person "
                     f"confirms it: ls6t profile set motors.laser_shutter \"{prof['motors']['laser_shutter']}\"")
        prof["motors"]["laser_shutter"] = None
    prof["auto"] = {"from": str(dump), "dump_time": prof.get("discovered", {}).get("time"),
                    "max_filter_pct": AUTO_MAX_FILTER_PCT, "notes": notes,
                    "review": REVIEW_STEPS}
    prof["source"] = f"automatic, from the bridge's inventory {prof['auto']['dump_time']}"
    return prof


def drift(prof: dict) -> list[str]:
    """What the bridge's latest inventory has that a person's profile does not (or the reverse): a new objective,
    a laser line, filters... Empty when they agree, when the profile is automatic, or without an inventory."""
    dump = latest_dump()
    if dump is None or prof.get("auto") or not prof.get("measured"):
        return []
    try:
        inv, _ = import_discovery(read_text(dump), base=copy.deepcopy(DEFAULT_PROFILE))
    except (OSError, ValueError):
        return []
    out = []

    def cmp(what, have, now):
        have, now = set(map(str, have)), set(map(str, now))
        if have != now:
            extra = ", ".join(sorted(now - have))
            gone = ", ".join(sorted(have - now))
            out.append(f"{what}: LabSpec reports {', '.join(sorted(now))}" + (f" (new: {extra})" if extra else "") +
                       (f" (not there any more: {gone})" if gone else ""))
    cmp("lasers (nm)", [f"{float(v['nm']):g}" for v in prof.get("lasers", {}).values()],
        [f"{float(v['nm']):g}" for v in inv.get("lasers", {}).values()])
    cmp("gratings", prof.get("gratings", {}), inv.get("gratings", {}))
    cmp("filters", prof.get("filters", {}), inv.get("filters", {}))
    cmp("objectives", prof.get("objectives", []), inv.get("objectives", []))
    return out


def spectro_ranges(d: dict, grating_motor: str = "Grating") -> dict:
    """{'<G>': [SpMin, SpMax]} from a discovery dump: LabSpec's spectrometer range per grating, kept only when it is
    plausible as nm (200 < SpMax <= 1.05 x 2e6 / G; on the XploRA 2400: 690, 1800: 935, 1200: 1112, 600: 1138)."""
    out = {}
    gr = _motor(d, grating_motor)
    for i, v in enumerate((gr or {}).get("values") or []):
        g = _num(v)
        lo, hi = _num(d.get(f"grating.{i}.SpMin")), _num(d.get(f"grating.{i}.SpMax"))
        if g and g > 0 and hi is not None and 200 < hi <= 2e6 / g * 1.05 and (lo is None or 0 <= lo < hi):
            out[f"{g:g}"] = [lo if lo is not None else 0.0, hi]
    return out


def _inventory_facts(prof: dict) -> None:
    """Facts LabSpec reports that only restrict what ls6t does (the spectrometer's range per grating) go into a
    person's profile from the bridge's latest inventory, without a review: they never widen a limit. Marked in
    prof['facts_from_inventory']."""
    dump = latest_dump()
    if dump is None or prof.get("auto"):
        return
    try:
        ranges = spectro_ranges(parse_kv(read_text(dump)), prof["motors"].get("grating", "Grating"))
    except (OSError, ValueError, KeyError):
        return
    added = []
    for key, sp in ranges.items():
        g = prof.get("gratings", {}).get(key)
        if isinstance(g, dict) and g.get("spectro_nm") != sp:
            g["spectro_nm"] = sp
            added.append(key)
    if added:
        prof["facts_from_inventory"] = {"spectro_nm": added, "from": str(dump)}


_INI_CACHE: dict = {}


def _ini_facts(prof: dict) -> None:
    """What LabSpec's OLConfig.ini describes and a reviewed profile may lack, added when missing (never replacing a
    person's value): the objectives' field of view, the camera names, the illumination motors. 2026-11-23: a
    profile reviewed from an older proposal had none, so video-scale had no field for the x10, camera 'top' was
    unknown and bright field could not be set. None of them widens a limit. Marked in prof['facts_from_ini']."""
    if prof.get("auto"):
        return
    try:
        from . import labspec_ini
        lp = (prof.get("discovered") or {}).get("labspec_path")
        p = labspec_ini.find(lp)
        if p is None:
            return
        key = (str(p), p.stat().st_mtime)
        if key not in _INI_CACHE:
            probe = {"objectives": list(prof.get("objectives") or []), "motors": {}, "video": {}}
            labspec_ini.enrich(probe, lp, [])
            _INI_CACHE.clear()
            _INI_CACHE[key] = probe
        probe = _INI_CACHE[key]
    except Exception:  # noqa: BLE001 - a description only: the profile works without it
        return
    added = []
    if probe.get("objectives_info") and not prof.get("objectives_info"):
        prof["objectives_info"] = copy.deepcopy(probe["objectives_info"])
        added.append("objectives_info")
    cams = (probe.get("video") or {}).get("cameras")
    if cams and not (prof.get("video") or {}).get("cameras"):
        prof.setdefault("video", {})["cameras"] = dict(cams)
        added.append("video.cameras")
    for role in ("illumination", "illumination_light"):
        m = (probe.get("motors") or {}).get(role)
        if m and not (prof.get("motors") or {}).get(role):
            prof["motors"][role] = m
            added.append(f"motors.{role}")
    if probe.get("illumination_labels") and not prof.get("illumination_labels"):
        prof["illumination_labels"] = list(probe["illumination_labels"])
    if added:
        prof["facts_from_ini"] = {"added": added, "from": str(p)}


_LOAD_CACHE: dict = {}


def _file_sig(p: Path | None):
    if p is None:
        return None
    try:
        st = p.stat()
        return (str(p), st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load(path: Path | None = None) -> dict:
    """The profile in use: the file merged over the defaults ('measured': false when there is no file). With no
    file but an inventory from the bridge, a profile is made from it automatically (auto_profile); an automatic
    profile follows newer inventories until a person has reviewed it."""
    p = Path(path) if path else config.profile_path()
    dump = latest_dump() if path is None else None
    cache_key = (str(p), _file_sig(p), _file_sig(dump), path is None)
    cached = _LOAD_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    if path is None and dump is not None:
        try:
            cur = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
            stale = cur is not None and isinstance(cur.get("auto"), dict) and \
                cur["auto"].get("dump_time") != parse_kv(read_text(dump)).get("time")
            if cur is None or stale:
                save(auto_profile(dump), p)
        except (OSError, ValueError, KeyError):
            pass
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.pop("problems", None)      # written by .25.10's profile set: found at load time, never stored
        problems: list = []
        prof = _merge(DEFAULT_PROFILE, data, problems)
        rp = (prof.get("spectrometer") or {}).get("repeatability") or {}
        for kk in [kk for kk, vv in rp.items() if not isinstance(vv, dict)]:
            problems.append(f"spectrometer.repeatability.{kk} should be {{moved_sd_cm1, still_sd_cm1}}, the profile "
                            f"has {rp[kk]!r:.60}: ignored")
            rp.pop(kk)
        if problems:
            prof["problems"] = problems
        prof["measured"] = True
        prof["path"] = str(p)
        if path is None:
            _inventory_facts(prof)
            _ini_facts(prof)
    else:
        prof = copy.deepcopy(DEFAULT_PROFILE)
        prof["measured"] = False
        prof["path"] = None
    for v in (prof.get("lasers") or {}).values():
        # per laser, settable with `profile set lasers.<k>.…` (2026-11-23, the 637.75 nm mode hop): a discarded
        # acquisition before the first spectrum, and whether the laser is known to hop between modes
        if isinstance(v, dict):
            v.setdefault("warmup_s", 0)
            v.setdefault("mode_hops", False)
            v.setdefault("unstable_multi", False)      # true: refuse multi-spectrum studies; "warn": warn
            v.setdefault("settle_s", 0)                # a pause between spectra with this laser (its mode holds)
            v.setdefault("hold_shutter", None)         # None: hold automatically when the shutter is verified for
            # this laser; True / False overrides
            v.setdefault("calibration_open_s", None)   # before a calibration (None: 40 s for mode_hops lasers)
            v.setdefault("hold_open_s", None)          # the wait after a fresh opening (None: 40 s for mode_hops /
            # unstable_multi lasers, else settle_s
            # for a laser marked mode_hops / unstable_multi)
            v.setdefault("laser_reference", "report")  # 'correct': follow the laser line's steps (hops) when it is in
            # the window; 'correct_each': every spectrum's laser line (its slow drift too)
            v.setdefault("calibration_max_age_h", None)  # a shorter life for this laser's calibrations
            v.setdefault("default_filter", None)       # the flat tools' filter with this laser (None: 1%)
            v.setdefault("change_settle_s", None)      # after a change to this laser, before a calibration
            # (None: CHANGE_SETTLE_DEFAULT_S for calibrations, none otherwise)
    if len(_LOAD_CACHE) > 8:
        _LOAD_CACHE.clear()
    final_key = (str(p), _file_sig(p), _file_sig(dump), path is None)
    _LOAD_CACHE[final_key] = copy.deepcopy(prof)
    return prof


def save(prof: dict, path: Path | None = None) -> Path:
    from .io_util import replace_retry
    p = Path(path) if path else config.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 'problems' is what load() found, never part of the file (2026-11-25: profile set saved it, and the next load
    # still reported a problem the set had just fixed)
    data = {k: v for k, v in prof.items() if k not in ("measured", "path", "problems")}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    replace_retry(tmp, p)
    _LOAD_CACHE.clear()
    return p


def filter_pct(label: str) -> float | None:
    """A filter label as a transmission in %: '10%' -> 10, 'D1' or 'OD1' -> 10, '100' -> 100."""
    s = str(label).strip().replace(",", ".")
    m = re.fullmatch(r"([0-9.]+)\s*%?", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = re.fullmatch(r"(?:O?D)\s*([0-9.]+)", s, flags=re.I)
    if m:
        try:
            return round(100.0 * 10 ** (-float(m.group(1))), 6)
        except ValueError:
            return None
    return None


def laser_key(prof: dict, laser) -> str | None:
    """The profile key of a laser given as a number (nm) or its key/label."""
    lasers = prof.get("lasers", {})
    if str(laser) in lasers:
        return str(laser)
    try:
        nm = float(laser)
    except (TypeError, ValueError):
        for k, v in lasers.items():
            if str(v.get("label")) == str(laser):
                return k
        return None
    best = min(lasers.items(), key=lambda kv: abs(float(kv[1]["nm"]) - nm), default=None)
    if best and abs(float(best[1]["nm"]) - nm) <= 1.0:
        return best[0]
    return None


# --------------------------------------------------------------------------------------------- discovery import

from .io_util import read_text, to_float  # noqa: E402, F401 - re-exported for callers


def unescape(v: str) -> str:
    return v.replace("%0A", "\n").replace("%25", "%")


def escape(v: str) -> str:
    return v.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def parse_kv(text: str) -> dict:
    """key=value lines (the bridge's format); later keys win; '#' lines are comments; %0A is a line end, %25 a %."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip("\r\n\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = unescape(v)
    return out


def _motor(d: dict, name: str) -> dict | None:
    pre = f"motor.{name}."
    if d.get(pre + "present") != "1":
        return None
    try:
        size = int(float(d.get(pre + "size", "0")))
    except ValueError:
        size = 0
    values = [d.get(f"{pre}value.{i}") for i in range(size)]
    labels = [d.get(f"{pre}label.{i}") for i in range(size)]
    return {"name": name, "size": size, "values": values, "labels": labels,
            "current": d.get(pre + "current"), "current_label": d.get(pre + "current_label")}


def _num(s) -> float | None:
    return to_float(s, finite=False, comma=True)


def laser_problems(prof: dict) -> list[str]:
    """What a person still has to fill in per laser: its line (nm), its filter cap (max_filter_pct, 0-100) and its
    power at the sample at 100 % (power_mw > 0)."""
    out = []
    for k, v in (prof.get("lasers") or {}).items():
        v = v if isinstance(v, dict) else {}
        nm, cap, mw = _num(v.get("nm")), _num(v.get("max_filter_pct")), _num(v.get("power_mw"))
        if nm is None or nm <= 0:
            out.append(f"lasers.{k}.nm")
        if cap is None or not 0 < cap <= 100:
            out.append(f"lasers.{k}.max_filter_pct (0-100)")
        if mw is None or mw <= 0:
            out.append(f"lasers.{k}.power_mw (at the sample at 100 %, measured)")
    if not prof.get("lasers"):
        out.append("lasers")
    return out


def go_ahead_waived(prof: dict) -> bool:
    """The per-request laser go-ahead is skipped only when a person has said both: the enclosure is interlocked (the
    laser off with a door open) and no go-ahead is wanted (Nicola, 2026-11-27)."""
    sf_ = prof.get("safety") or {}
    return bool(sf_.get("interlocked_enclosure")) and sf_.get("laser_go_ahead") is False


def review_gate(prof: dict) -> str | None:
    """Why the laser may not fire on the instrument with this profile, or None. It may only with a profile a person
    reviewed (`ls6t profile import <profile.json>`) that still has every laser's line, cap and power."""
    if not prof.get("reviewed"):
        what = "automatic (from the bridge's inventory)" if prof.get("auto") else "not reviewed"
        return f"the profile is {what}: the laser fires only with a reviewed profile: " + REVIEW_STEPS
    missing = laser_problems(prof)
    if missing:
        return ("the reviewed profile lacks " + ", ".join(missing) + ": fill them in (ls6t profile set, or edit "
                "profile.json and ls6t profile import it)")
    return None


def import_reviewed(path: Path) -> tuple[dict, list[str]]:
    """A profile a person reviewed and edited (profile.proposed.json from `instrument discover`, profile.json
    itself, or a copy): checked and returned ready to save, marked reviewed. Refused (ValueError) when it is not a
    profile or when a laser lacks its line (nm), its cap (max_filter_pct) or its power (power_mw): what a person
    decides or measures."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: not a readable JSON profile ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("lasers"), dict) or not data["lasers"]:
        raise ValueError(f"{path}: no 'lasers': not an ls6t profile")
    missing = laser_problems(data)
    if missing:
        raise ValueError(f"{path}: fill in before importing: " + ", ".join(missing))
    notes = []
    if not (data.get("motors") or {}).get("laser_shutter"):
        notes.append("motors.laser_shutter not set: laser_on and snapshots with the laser stay off (set it once the "
                     f"maintainer confirmed which motor is the laser shutter: {HOW})")
    for k in ("auto", "measured", "path"):
        data.pop(k, None)
    data["source"] = f"reviewed {Path(path).name} {time.strftime('%Y-%m-%d %H:%M')}"
    data["reviewed"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "from": str(path)}
    notes.insert(0, "lasers: " + ", ".join(f"{float(v['nm']):g} nm, at most {float(v['max_filter_pct']):g} %, "
                                           f"{float(v['power_mw']):g} mW at 100 %" for v in data["lasers"].values()))
    return data, notes


def import_discovery(text: str, base: dict | None = None) -> tuple[dict, list[str]]:
    """A profile from the discovery script's dump, keeping the limits and timing of `base` (the current profile).
    Returns (profile, notes): the notes say what was found and what a person should check."""
    d = parse_kv(text)
    prof = copy.deepcopy(base or load())
    prof.pop("auto", None)
    prof.pop("measured", None)
    prof.pop("path", None)
    prof.pop("reviewed", None)          # rebuilt from a dump: a person reviews it again
    notes: list[str] = []
    prof["source"] = f"discovery {d.get('time', '')} (LabSpec {d.get('labspec_version', '?')})".strip()
    if d.get("system"):
        prof["system"] = d["system"]
    names = {k[len("motor."):-len(".present")] for k, v in d.items()
             if k.startswith("motor.") and k.endswith(".present") and v == "1"}
    notes.append("motors found: " + ", ".join(sorted(names)))
    if d.get("discovery.skipped"):
        notes.append("discovery skipped steps that stopped LabSpec before: " + d["discovery.skipped"])

    las = _motor(d, prof["motors"]["laser"])
    if las and las["size"] > 0:
        lasers = {}
        for v, lab in zip(las["values"], las["labels"]):
            nm = _num(v)
            if nm is None or nm <= 0:
                continue
            key = f"{nm:g}"
            # the current profile's laser by its line (not its key: 638 vs 637.75), so its cap and power are kept
            # (2026-11-23: a proposal reset the 10 % caps to 100); a laser it does not know gets the cautious cap
            olds = [v for v in (prof.get("lasers") or {}).values() if _num(v.get("nm")) is not None
                    and abs(_num(v.get("nm")) - nm) <= 0.5]
            old = olds[0] if olds else {}
            lasers[key] = {"nm": nm, "label": lab or key, "power_mw": old.get("power_mw"),
                           "max_filter_pct": old.get("max_filter_pct", AUTO_MAX_FILTER_PCT)}
            if not olds:
                notes.append(f"laser {nm:g} nm is new to this profile: capped at {AUTO_MAX_FILTER_PCT:g} % until a "
                             "person sets lasers." + key + ".max_filter_pct")
        if lasers:
            prof["lasers"] = lasers
            notes.append("lasers: " + ", ".join(f"{v['nm']:g} nm" for v in lasers.values())
                         + " (set power_mw and max_filter_pct per laser by hand)")

    gr = _motor(d, prof["motors"]["grating"])
    if gr and gr["size"] > 0:
        gratings = {}
        spans = []
        for i, v in enumerate(gr["values"]):
            g = _num(v)
            if g and g > 0:
                key = f"{g:g}"
                old = prof.get("gratings", {}).get(key, {})
                gratings[key] = {k: v_ for k, v_ in old.items() if k in ("window_nm", "max_nm")}
                gratings[key].setdefault("window_nm", round(46800.0 / g, 2))
                sp = spectro_ranges(d, prof["motors"]["grating"]).get(key)
                if sp:
                    gratings[key]["spectro_nm"] = sp
                    spans.append(f"{key} up to {sp[1]:g} nm")
        if gratings:
            prof["gratings"] = gratings
            notes.append("gratings: " + ", ".join(gratings) + " gr/mm (window_nm estimated: measure it)")
            if spans:
                notes.append("spectrometer range per grating (LabSpec's SpMin / SpMax): " + ", ".join(spans))

    for role, cand in (("filter", ["Filter", "Density", "ND", "Attenuator"]),
                       ("objective", ["Objective", "Objectives", "Microscope", "Nosepiece"])):
        m = None
        for name in [prof["motors"].get(role)] + cand:
            if name and name in names:
                m = _motor(d, name)
                prof["motors"][role] = name
                break
        if not m:
            notes.append(f"no {role} motor found among {', '.join(cand)}: check the motor list in the dump")
            continue
        labels = [lab for lab in m["labels"] if lab]
        if role == "filter":
            filters = {lab: filter_pct(lab) for lab in labels}
            unknown = [lab for lab, p in filters.items() if p is None]
            prof["filters"] = filters
            notes.append(f"filters ({m['name']}): " + ", ".join(labels)
                         + (f"; set the % of {unknown} by hand" if unknown else ""))
        else:
            prof["objectives"] = labels
            notes.append(f"objectives ({m['name']}): " + ", ".join(labels))

    for name in ("Shutter", "LaserShutter", "Laser Shutter", "Laser shutter"):
        if name in names:
            # found, not set: a person confirms it is the laser's shutter first (2026-11-23: a proposal pre-filled it)
            prof["laser_shutter_candidate"] = name
            if prof["motors"].get("laser_shutter") == name:
                notes.append(f"laser shutter motor: {name!r} (already set in the current profile)")
            else:
                notes.append(f"laser shutter motor found: {name!r}, not set: once the maintainer confirms it is the "
                             f"laser shutter ({HOW}), set motors.laser_shutter to \"{name}\" (ls6t profile set "
                             f"motors.laser_shutter \"{name}\")")
            break
    for role, cands in (("laser_pol", ("LaserPol", "Laser Polarizer", "Laser pol")),
                        ("raman_pol", ("RamanPol", "Raman Polarizer", "Raman pol"))):
        for name in cands:
            m = _motor(d, name) if name in names else None
            if m and m["size"] > 0:
                prof["motors"][role] = name
                prof.setdefault("polarizers", {})[role] = [lab for lab in m["labels"] if lab]
                notes.append(f"{role.replace('_', ' ')}arizer ({name}): " + ", ".join(prof["polarizers"][role]))
                break
    m = _motor(d, "uLight") if "uLight" in names else None
    if m and m["size"] > 0:
        prof["motors"]["illumination_light"] = "uLight"         # read and compared, never set
        notes.append("illumination light (uLight, read only): " + ", ".join(x for x in m["labels"] if x))
    for name in ("IlluminatorV",):
        m = _motor(d, name) if name in names else None
        if m and m["size"] > 0 and any("field" in str(lab).lower() for lab in m["labels"]):
            prof["motors"]["illumination"] = name
            prof["illumination_labels"] = [lab for lab in m["labels"] if lab]
            notes.append(f"illumination ({name}): " + ", ".join(prof["illumination_labels"]))
            break
    for role, key in (("hole", "holes_um"), ("slit", "slits_um")):
        m = _motor(d, prof["motors"][role])
        if m and m["size"] > 0:
            prof[key] = [x for x in (_num(v) for v in m["values"]) if x is not None]
    for ax in ("x", "y", "z"):
        if prof["motors"][ax] not in names:
            notes.append(f"stage axis {ax.upper()} ({prof['motors'][ax]}) not found: maps along it are refused")
    det = _num(d.get("detector_temp_c"))
    if det is not None:
        notes.append(f"detector at {det:g} C (max_temp_c {prof['detector']['max_temp_c']:g})")
    # the self-tests of methods outside the 2018 manual (TESTING.md T1.3, T1.4)
    probes = {k[len("selftest."):]: d[k] for k in d if k.startswith("selftest.")}
    methods = {m: probes.get(k) == "1" for m, k in (("GetLastErrorInfo", "last_error_info"),
                                                     ("GetValueEx", "get_value_ex"),
                                                     ("GetValueSimplePart", "get_value_simple_part"),
                                                     ("GetConfigParam", "get_config_param")) if k in probes}
    if methods:
        notes.append("extra LabSpec methods: " + ", ".join(f"{m} {'works' if ok else 'missing'}"
                                                           for m, ok in methods.items()))
        part = [x for x in str(probes.get("get_value_simple_part_values", "")).split(",") if x.strip()]
        if methods.get("GetValueSimplePart") and part:
            try:
                vals = [float(x) for x in part]
            except ValueError:
                vals = []
            if vals == [float(i) for i in range(4, 12)]:
                prof.setdefault("mapping", {})["export_chunk"] = 65536
                notes.append("GetValueSimplePart counts values: native maps are written out in chunks")
            else:
                prof.setdefault("mapping", {})["export_chunk"] = 0
                notes.append(f"GetValueSimplePart(start 4, size 8) gave {len(vals)} values ({', '.join(part)}): its "
                             "units are not values (bytes?): native maps are read whole (TESTING Q35)")
        elif methods.get("GetValueSimplePart") and probes.get("get_value_simple_part_size") not in (None, "8"):
            prof.setdefault("mapping", {})["export_chunk"] = 0
            notes.append(f"GetValueSimplePart(start 4, size 8) gave {probes.get('get_value_simple_part_size')} "
                         "values: its units are not values (bytes?): native maps are read whole (TESTING Q35)")
        elif methods.get("GetValueSimplePart") is False:
            prof.setdefault("mapping", {})["export_chunk"] = 0
            notes.append("GetValueSimplePart missing: native maps are read whole (mapping.export_chunk 0)")
    cps = sorted({k.split(".")[1] for k in d if k.startswith("config_param.")}, key=lambda v: int(v) if v.isdigit() else 0)
    config_params = [{f: d.get(f"config_param.{i}.{f}") for f in ("name", "unit", "type", "value")} for i in cps]
    if config_params:
        notes.append(f"{len(config_params)} script configuration parameter(s) (GetConfigParam): "
                     + ", ".join(str(c["name"]) for c in config_params[:10]))
    from . import labspec_ini
    labspec_ini.enrich(prof, d.get("labspec_path"), notes)
    prof["discovered"] = {"time": d.get("time"), "labspec_path": d.get("labspec_path"),
                          "labspec_version": d.get("labspec_version"),
                          "motors": sorted(names), "methods": methods, "config_params": config_params}
    return prof, notes


def set_key(prof: dict, dotted: str, value) -> dict:
    """profile[a][b]... = value (for `ls6t profile set`, a person's edit); the key must exist already. Keys with a dot
    of their own are matched against the profile: at each level the longest run of parts that is a key there wins;
    a key can be quoted ('lasers."637.75".power_mw', 'lasers["637.75"].power_mw'); and under 'lasers' a laser is
    also found by its wavelength ('lasers.637.75…' finds the laser stored as '638' with nm 637.75: 2026-11-23,
    `profile set lasers.637.75.power_mw 40` answered "no '637' in the profile")."""
    import re
    # a bracketed or quoted key is one part, dots and all
    parts = [m.group(1) or m.group(2) or m.group(3) for m in
             re.finditer(r"\[[\"']?([^\"'\]]+)[\"']?\]|[\"']([^\"']+)[\"']|([^.\[\]\"']+)", dotted)]
    cur, i = prof, 0
    while True:
        if not isinstance(cur, dict):
            raise KeyError(f"{dotted}: {'.'.join(parts[:i])} is not a group of keys")
        hit = next((j for j in range(len(parts), i, -1) if ".".join(parts[i:j]) in cur), None)
        k = ".".join(parts[i:hit]) if hit else None
        if hit is None and i > 0 and parts[i - 1] == "lasers":
            # a laser by its wavelength, the longest numeric run first
            for j in range(len(parts), i, -1):
                try:
                    nm = float(".".join(parts[i:j]))
                except ValueError:
                    continue
                k = next((kk for kk, v in cur.items() if isinstance(v, dict) and v.get("nm") is not None
                          and abs(float(v["nm"]) - nm) < 0.01), None)
                if k:
                    hit = j
                    break
        if hit is None:
            have = ", ".join(sorted(map(str, cur))) if isinstance(cur, dict) else ""
            raise KeyError(f"{dotted}: no {parts[i]!r} in the profile" + (f" (here: {have})" if have else "")
                           if i < len(parts) - 1 else f"{dotted}: no such key in the profile"
                           + (f" (here: {have})" if have else ""))
        if hit == len(parts):
            cur[k] = value
            return prof
        cur, i = cur[k], hit


def summary(prof: dict) -> dict:
    """The compact view study_format shows: what may be asked for."""
    return {"system": prof.get("system"), "measured": prof.get("measured", False), "source": prof.get("source"),
            "automatic": bool(prof.get("auto")),
            "lasers_nm": [v["nm"] for v in prof.get("lasers", {}).values()],
            "gratings": [float(g) for g in prof.get("gratings", {})],
            "filters": list(prof.get("filters", {})),
            "objectives": list(prof.get("objectives", [])),
            "holes_um": prof.get("holes_um"), "slits_um": prof.get("slits_um"),
            "limits": prof.get("limits"),
            "limits_meaning": {"max_xy_um": "the farthest distance from the home spot (where the operator left the "
                                            "stage, or a move with stay), not per axis",
                               "max_dz_um": "the farthest Z from the operator's focus, either way"},
            "alignment_lasers": sorted(prof.get("alignment", {}).get("baselines", {})),
            "laser_shutter": bool(prof.get("motors", {}).get("laser_shutter")),
            # what a client needs to know about the go-ahead and the motors (Nicola, 2026-11-27)
            "safety": dict(prof.get("safety") or {}),
            "laser_go_ahead": ("not asked: interlocked enclosure (safety)" if go_ahead_waived(prof) else
                               "asked in LabSpec (LS6T_CONFIRM)"),
            "motors": {k: v for k, v in (prof.get("motors") or {}).items() if v},
            "default_hole_um": prof.get("default_hole_um"),
            "cameras": prof.get("video", {}).get("cameras") or {},
            "polarizers": prof.get("polarizers") or {},
            "objectives_info": prof.get("objectives_info") or {},
            "laser_limits": {k: {"max_filter_pct": v.get("max_filter_pct", 100), "power_mw": v.get("power_mw")}
                             for k, v in prof.get("lasers", {}).items()},
            "grating_reach": grating_reach_table(prof)}


def grating_reach_table(prof: dict) -> dict:
    """Per grating: the spectrometer's range (nm) and the largest Raman shift (cm-1) each laser reaches with it,
    None when the laser line itself lies beyond (LabSpec's SpMax, a measured max_nm, or 2 / G)."""
    from . import plan
    out = {}
    for g, gp in (prof.get("gratings") or {}).items():
        sp = (gp or {}).get("spectro_nm")
        row = {"limit_nm": round(plan.grating_limit_nm(prof, g) or 0, 1),
               "from": "LabSpec SpMax" if sp else ("max_nm" if (gp or {}).get("max_nm") else "2 / G")}
        if sp and sp[0]:
            row["labspec_spmin"] = sp[0]          # shown, not applied (100 for every grating: TESTING Q45)
        if (gp or {}).get("min_nm"):
            row["min_nm"] = gp["min_nm"]
        for v in (prof.get("lasers") or {}).values():
            r = plan.grating_reach(prof, g, v["nm"])
            row[f"max_cm1_at_{float(v['nm']):g}"] = r["max_shift_cm1"] if r else None
        out[g] = row
    return out


def same_position(got, asked, tol: float = 0.5) -> bool:
    """Whether a read-back position is the one asked, whatever the formatting: labels without spaces and case
    ('10 %' and '10%', 'X100_VIS' and 'x100_vis'); numbers within tol for positions (637.8 and 637.75; '300' and
    300.0). Filters (anything in %) are a discrete set tied to the laser power: they match only as the same number
    (relative 0.1 %: '0.10%' = '0.1%', but never 0.1 % = 0.01 %), never within an absolute tolerance."""
    if got is None or asked is None:
        return False
    a, b = str(got).strip(), str(asked).strip()
    if a.replace(" ", "").casefold() == b.replace(" ", "").casefold():
        return True

    def num(s):
        m = re.fullmatch(r"\s*([-+]?\d+(?:[.,]\d+)?)\s*(%|nm|µm|um|gr/mm)?\s*", s)
        return (float(m.group(1).replace(",", ".")), m.group(2)) if m else (None, None)
    (x, ux), (y, uy) = num(a), num(b)
    if x is None or y is None:
        return False
    if "%" in (ux, uy):
        return abs(x - y) <= 1e-3 * max(abs(x), abs(y)) + 1e-12
    return abs(x - y) <= tol


def match_label(value, labels) -> tuple:
    """A position name as people say it ('x100', '10%') matched to LabSpec's labels ('x100_VIS', '10%'): the same
    name (any case), else the one label that starts with it and goes on with something other than a letter or
    digit ('x10' is not 'x100_VIS'). Returns (label or None, error or None)."""
    labs = [str(x) for x in labels or []]
    v = str(value).strip()
    for lab in labs:
        if lab.lower() == v.lower():
            return lab, None
    cands = [lab for lab in labs if lab.lower().startswith(v.lower()) and
             (len(lab) == len(v) or not lab[len(v)].isalnum())]
    if len(cands) == 1:
        return cands[0], None
    if len(cands) > 1:
        return None, f"{value!r} could be {', '.join(cands)}: say which"
    return None, f"{value!r}: not one of {', '.join(labs) or 'the known positions'}"


def camera_number(prof: dict, camera) -> tuple:
    """A camera given by name (the profile's video.cameras), number, or nothing (LabSpec's active one).
    Returns (number or None, error or None)."""
    if camera in (None, ""):
        return None, None
    cams = {str(k).lower(): v for k, v in (prof.get("video", {}).get("cameras") or {}).items()}
    key = str(camera).strip().lower()
    for cand in (key, key.replace(" camera", "").strip()):
        if cand in cams:
            return int(cams[cand]), None
    try:
        return int(float(camera)), None
    except (TypeError, ValueError):
        known = ", ".join(f"{k} ({v})" for k, v in cams.items()) or "none named in the profile (video.cameras)"
        return None, f"camera {camera!r}: known cameras: {known}; or give its number"


def nm_to_shift(nm, laser_nm: float):
    """Raman shift (cm-1) of absolute wavelengths (nm) for a laser line (nm). Works on numbers and numpy arrays."""
    return 1e7 / laser_nm - 1e7 / nm


def shift_to_nm(cm1, laser_nm: float):
    return 1e7 / (1e7 / laser_nm - cm1)
