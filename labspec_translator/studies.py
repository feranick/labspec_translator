"""studies.py — the 10 Raman study kinds (spectrum/map/series/depth/calibration, focus, move, spot, filters, cutoff, background, guide, alignment).

Split from runner.py so each study procedure lives in a focused module sharing runner_core helpers.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

from . import __version__, BRIDGE_PROTOCOL, config, plan, spectra, video
from . import spot as SP
from . import store
from . import shutter as SH
from . import profile as P
from .io_util import parse_kv_and_csv
from .runner_core import (
    BLOCK_BY_HAND, Ctx, Stop, _acq_timeout, _af_bound, _band_said, _check_abs, _eta, _f,
    _finish, _focus_here, _jsonable, _live_view, _objective_source, _paint_band, _progress,
    _retract_before_xy, _setup, _shutter_gate, _snapshot, _video_focus_after_travel, _warmup,
    _z_direct, ae_record, axial_shape, clean, grating_guard, header_ics_denoise, motor_args,
    move_extra, move_z, note_laser_selected, optics_record, settle_reason, shutter_args,
    shutter_report, study_envelope, timing, travel_xy, warmup_s, write_json,
)
from .analysis import (
    HEALTH_MOVE_MIN_CM1, SPECTRO_NM_PLAUSIBLE, analyse, combo, evaluate_expect, laser_health,
    store_pending,
)
from .calibration_learn import _learn_first_landing



def run_study(name: str, study: dict, ctx: Ctx, sd: Path | None = None) -> dict:
    prof = ctx.prof
    sd = sd or ctx.folder / name
    ctx.study_dir = sd
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    n_calls = len(ctx.calls)
    kind = study.get("kind", "spectrum")
    pts = plan.positions(study)
    _progress(sd, state="running", stage="setup", point=0, points=len(pts), started=t_start,
              estimate_min=plan.estimate(study, prof)["minutes"])
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    if not prof.get("measured"):
        warnings.append("no machine profile: built-in XploRA defaults (run the discovery script)")

    su = _setup(name, study, ctx, warnings, unreliable, checks, len(pts))
    before, after, laser, temp, readback, rng = (su[k] for k in ("before", "after", "laser", "temp", "readback",
                                                                 "rng"))

    # ---- points
    acq = plan.acquire_step(study, prof, rng)
    appr_ = float((prof.get("spectrometer") or {}).get("approach_nm", 0) or 0)
    if appr_ and acq.get("verb", "acquire") == "acquire":
        # every acquisition of the study (the warm-up too) from the same side (2026-11-25: the discarded first
        # spectrum after a grating change made the move without the approach, and the repeats then skipped it)
        acq = dict(acq, spectro_approach_nm=appr_)
    grating_changed = str(before.get("grating")) != str(after.get("grating"))
    lb_, la_ = _f(before.get("laser_nm")), _f(after.get("laser_nm"))
    laser_changed = lb_ is not None and la_ is not None and abs(lb_ - la_) > 0.5
    ctx.laser_changed_now = laser_changed
    ctx.settle_info = None
    ctx.settle_kind = "laser_check" if (kind != "calibration" and (study.get("process") or {}).get("laser_check")) \
        else kind
    if laser_changed:
        note_laser_selected(la_)
    focus = study.get("focus", {})
    af = focus.get("autofocus")
    every = int(focus.get("every", 0) or 0)
    x0, y0 = ctx.home_xy                  # the operator's spot (or where a 'move' study stayed)
    zref = ctx.z_focus                    # the operator's focus (or the last autofocus / focus scan)
    zcur = _f(before.get("z_um"))
    cur_xy = (_f(before.get("x_um")), _f(before.get("y_um")))
    stage = prof.get("stage", {})
    per_point = []
    view = None
    native = study.get("save_native", True)
    stop_exc = None
    t_acq = 0.0
    vd = study.get("video") or {}
    # a calibration takes no video frame first by default (17-20 s each, 2026-11-23), unless the profile's
    # calibration.video_before is true or the study's video.before says otherwise
    want_before = vd.get("before", kind == "calibration" and plan.calibration_video_before(prof))
    focus_scans: list = []
    snaps: dict = {}
    # LabSpec's own map engine: not the same as save_native (`native` above), which this name once overwrote, so no
    # .l6s was saved for ordinary spectra and their Remark could not be checked (2026-11-23)
    native_map = plan.map_engine(study, prof) == "native"
    native_warmup = None
    ctx.native_info = None
    if native_map:
        try:
            if af:
                if af in ("scan", "camera"):
                    fs = (camera_focus(ctx, study, zref if zref is not None else zcur, sd / "focus" / "start",
                                       warnings) if af == "camera" else
                          focus_scan(ctx, study, laser, zref if zref is not None else zcur, sd / "focus" / "start",
                                     warnings))
                    focus_scans.append(fs)
                    if fs.get("found"):
                        zref = zcur = ctx.z_focus = fs["z_um"]
                        _focus_here(ctx)
                else:
                    r = ctx.call("autofocus", timeout_s=240, how=af, **_af_bound(ctx), **motor_args(prof))
                    plan.record_af_motion(r)
                    if r["ok"] and _f(r.get("z_um")) is not None:
                        zref = zcur = ctx.z_focus = _f(r.get("z_um"))
                        _focus_here(ctx)
            if want_before:
                snaps["before"] = _snapshot(ctx, name, sd, "before", bool(vd.get("with_laser")), warnings, laser)
            if warmup_s(study, prof, laser) > 0:
                # a discarded acquisition before LabSpec's map starts (2026-11-24: the first point of a native map
                # was taken before the beam was really on)
                _progress(sd, stage="settling", point=0, points=len(pts))
                native_warmup = _warmup(ctx, acq, sd, warmup_s(study, prof, laser))
            t_a = time.time()
            per_point = _native_map(ctx, name, study, sd, rng, laser, acq, x0, y0, zref, warnings)
            t_acq += time.time() - t_a
        except Stop as exc:
            stop_exc = exc
    warmup_info = native_warmup
    cal_refocus_result = None
    refocused_cal = False
    t_last_start = time.time()
    live_on = bool((prof.get("display") or {}).get("live_view"))
    pending_views: list = []
    for k, p in enumerate(pts if not native_map else []):
        if ctx.cancelled():
            stop_exc = Stop("cancelled", "cancel")
            break
        try:
            if kind == "series" and k > 0:
                # start to start from the previous spectrum's start: a late spectrum does not make the next one
                # start early to catch up (a fixed grid from the first start did: 0.4 s after a slow one)
                wait_until = t_last_start + float(study["series"].get("interval_s", 0))
                while time.time() < wait_until:
                    if ctx.cancelled():
                        raise Stop("cancelled", "cancel")
                    ctx.beat()
                    time.sleep(min(0.5, max(0.0, wait_until - time.time())))
            ax, ay = cur_xy
            if x0 is None or y0 is None:
                if p["x_um"] or p["y_um"]:
                    raise Stop("the stage position could not be read: XY moves are refused", "study")
            elif cur_xy[0] is None or cur_xy[1] is None or \
                    math.hypot(x0 + p["x_um"] - cur_xy[0], y0 + p["y_um"] - cur_xy[1]) > 0.01:
                ax, ay = x0 + p["x_um"], y0 + p["y_um"]
                for val, lim_key in ((ax, "x_um"), (ay, "y_um")):
                    lim = stage.get(lim_key)
                    if lim and not lim[0] <= val <= lim[1]:
                        raise Stop(f"point {k}: stage {lim_key[0].upper()} = {val:g} µm is outside the profile's "
                                   f"stage limits {lim}", "study")
                zcur = _retract_before_xy(ctx, ax, ay, zcur, zref, f"point {k}")
                r = ctx.call("move_xy", timeout_s=180, x_um=round(ax, 3), y_um=round(ay, 3),
                             **motor_args(prof))
                if not r["ok"]:
                    raise Stop(f"point {k}: XY move failed: {r.get('error')}", "study")
                cur_xy = (_f(r.get("x_um")) if _f(r.get("x_um")) is not None else ax,
                          _f(r.get("y_um")) if _f(r.get("y_um")) is not None else ay)
                ax, ay = cur_xy
            if af and (k == 0 or (every and k % every == 0)):
                if af in ("scan", "camera"):
                    fs = (camera_focus(ctx, study, zref if zref is not None else zcur, sd / "focus" / f"p{k:05d}",
                                       warnings) if af == "camera" else
                          focus_scan(ctx, study, laser, zref if zref is not None else zcur, sd / "focus" / f"p{k:05d}",
                                     warnings))
                    focus_scans.append(fs)
                    if fs.get("found"):
                        zref = zcur = fs["z_um"]
                        ctx.z_focus = zref
                    else:
                        zcur = fs.get("z_now", zcur)
                        warnings.append(f"point {k}: focus scan found no clear best ({fs.get('why')}); kept the "
                                        "previous focus")
                else:
                    r = ctx.call("autofocus", timeout_s=240, how=af, **_af_bound(ctx), **motor_args(prof))
                    plan.record_af_motion(r)
                    if r["ok"] and _f(r.get("z_um")) is not None:
                        zref = zcur = _f(r.get("z_um"))
                        ctx.z_focus = zref
                    else:
                        warnings.append(f"point {k}: autofocus failed ({r.get('error')}); kept the previous focus")
            if p["dz_um"] and zref is None:
                raise Stop("the Z position could not be read: Z offsets are refused", "study")
            if zref is not None:
                target = zref + p["dz_um"]       # Z is always relative to the operator's (or autofocus') focus
                if zcur is None or abs(target - zcur) > 1e-6:
                    zcur = move_z(ctx, target, f"point {k}")
            if k == 0 and want_before:
                snaps["before"] = _snapshot(ctx, name, sd, "before", bool(vd.get("with_laser")), warnings, laser)
            if k > 0 and (prof.get("timing") or {}).get("status_before_each"):
                # diagnostic (TESTING T3.21, 2026-11-23): the bridge's status read before every spectrum, as at the
                # start of a request; the 637.75 nm laser's mode was right for the first spectrum of each request only
                ctx.call("status", timeout_s=15, **motor_args(prof))
            cal_reps = kind == "calibration" and int((study.get("calibration") or {}).get("repeats", 1) or 1) > 1
            # after a grating change LabSpec re-places the spectrometer when the turret turns: the first move of the
            # study is always made (with its approach), even when the spectrometer already reads the centre
            first_acq = dict(acq, spectro_force=1) if grating_changed else acq
            warmed = False
            chg_s, chg_why = settle_reason(prof, laser, ctx.settle_kind, laser_changed) if k == 0 else (0.0, "")
            if k == 0 and not ctx.hold_wanted:
                ctx.settle_info = {"settle_waited_s": round(max(chg_s, warmup_s(study, prof, laser))
                                                            if chg_s > 0 else 0.0, 1),
                                   "after_selection_s": round(chg_s, 1), "why": chg_why,
                                   "how": "a discarded acquisition" if chg_s > 0 else None}
            if chg_s > 0 and not ctx.hold_wanted:
                # the laser on the sample for a while after a change to it, discarded (2026-11-25: 532 @ 1200 crept
                # +0.17 cm-1 per repeat right after 637.75 -> 532); with the hold, its open wait does it
                _progress(sd, stage="settling", point=k, points=len(pts))
                warmup_info = _warmup(ctx, first_acq, sd, max(chg_s, warmup_s(study, prof, laser)))
                warmed = True
                warmup_info["why"] = (f"after the change of laser ({lb_:g} -> {la_:g} nm): the laser settling"
                                      if laser_changed else "the laser selected lately (by an earlier request): "
                                                            "the rest of its settling")
            elif k == 0 and warmup_s(study, prof, laser) > 0:
                _progress(sd, stage="settling", point=k, points=len(pts))
                warmup_info = _warmup(ctx, first_acq, sd, warmup_s(study, prof, laser))
                warmed = True
            elif k == 0 and cal_reps and grating_changed:
                # the first spectrum after a grating change was the outlier twice (2026-11-24: 11.31, 11.09 against
                # ~9.5): one discarded first
                _progress(sd, stage="settling", point=k, points=len(pts))
                warmup_info = _warmup(ctx, first_acq, sd, float(acq.get("exposure_s") or 1.0))
                warmed = True
                warmup_info["why"] = "the first spectrum after a grating change (calibration repeats)"
            out = sd / "spectra" / f"p{k:05d}.txt"
            params = dict(acq)
            params.pop("verb", None)
            if live_on and kind == "map" and "mode" in params and (int(params["mode"]) // 10) % 10 == 1:
                # the live map stays in front: LabSpec's AUTO_SHOW brought each new spectrum over it (2026-11-24)
                params["mode"] = int(params["mode"]) - 10
            params["out"] = str(out)
            params["sample"] = study.get("sample", {}).get("name", name)
            # LabSpec's Title / Project / Site: the study, the request, the sample's site (or cleared)
            params.update(title=name, project=f"ls6t {ctx.folder.name}",
                          site=str(study.get("sample", {}).get("site", ""))[:100])
            if study.get("sample", {}).get("notes"):
                params["remark"] = str(study["sample"]["notes"])[:200]     # LabSpec's Remark, after "ls6t <seq>"
            if k == 0 and grating_changed and not warmed:
                params["spectro_force"] = 1
            if kind == "calibration" and (k > 0 or cal_reps) and \
                    (study.get("calibration") or {}).get("repeats_move", True):
                # re-placed: each repeat lands on the pixels afresh; the first repeat too (2026-11-25, 600: after
                # the warm-up repeat 1 still kept the first approach's landing, +0.91 laser line / +1.12 Si): every
                # repeat lands the same way, after a jog
                # away to the other side, then the approach, then the centre: the path a spectrum's landing takes
                # (Nicola, 2026-11-25: with the jog equal to the approach, -2, the repeats skipped the approach
                # move and landed differently from spectra: fresh spectra read +0.45 to +0.93 high at 1200)
                jog_ = (prof.get("spectrometer") or {}).get("repeat_jog_nm")
                params["spectro_jog_nm"] = float(jog_) if jog_ not in (None, "") else (
                    abs(appr_) + 1.0 if appr_ else 3.0)
            if native and kind != "map":             # series too (2026-11-23: no .l6s for series spectra)
                params["native"] = str(sd / "spectra" / f"p{k:05d}.l6s")
            if ctx.hold_wanted and not ctx.holding and str(params.get("laser_off", "0")) != "1":
                # the hold (and its open wait: the laser settling) before this spectrum's clock starts: on .13 the
                # 5 s wait sat inside point 0's call, so point 0's t_start came 5 s early and the first gap read 8.3 s
                ctx._hold_on()
            if k == 0 and kind == "calibration" and (study.get("calibration") or {}).get("refocus") and not af:
                # Order: settle (above), then refocus, then precheck & repeats
                _progress(sd, stage="focusing", point=0, points=len(pts))
                rf_study = _cal_refocus_study(study)
                fs = focus_scan(ctx, rf_study, laser, zref if zref is not None else zcur, sd / "focus" / "refocus", warnings)
                focus_scans.append(fs)
                cal_refocus_result = _cal_refocus_summary(fs, plan.scan_params(rf_study, prof), "calibration.refocus")
                refocused_cal = True
                if fs.get("found"):
                    zref = zcur = ctx.z_focus = fs["z_um"]
                    _focus_here(ctx)
                else:
                    zcur = fs.get("z_now", zcur)
                    warnings.append(f"calibration refocus found no clear best ({fs.get('why')}); kept the previous focus")
            _progress(sd, stage="measuring", point=k, points=len(pts),
                      eta_min=_eta(t_start, k, len(pts), study, prof))
            t_a = time.time()
            t_last_start = t_a
            r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
            # when LabSpec's Acq began: the command's time plus the bridge's time before Acq (the spectrometer move
            # after a grating change, the flush, the settle); series spacing counts from there
            pre = _f(r.get("t_to_acq_s")) or 0.0
            t_last_start = t_a + pre
            t_sent = round(t_a + pre - t_start, 3)
            t_acq += time.time() - t_a
            if not r["ok"]:
                raise Stop(f"point {k}: acquisition failed: {r.get('error')}", "study")
            if k == 0:
                grating_guard(prof, study, after, laser, rng, out, checks, warnings, r)
            rec = {"point": k, "offset": {"x_um": p["x_um"], "y_um": p["y_um"], "dz_um": p["dz_um"]},
                   "stage": {"x_um": ax, "y_um": ay, "z_um": zcur}, "file": str(out),
                   "native": params.get("native"), "id": r.get("id"), "t": round(time.time() - t_start, 3),
                   "exposure_s": _f(r.get("exposure_s")), "accumulations": _f(r.get("accumulations")),
                   "detector_temp_c": _f(r.get("detector_temp_c")), "ics": ctx.last_ics,
                   "auto_exposure": ae_record(r), "optics_check": optics_record(r), "t_start": t_sent,
                   **({"shutter_hold_after": {"held": r.get("hold_after_acq_held"),
                                              "shutter_mode": r.get("hold_after_acq_shutter_mode"),
                                              "shutter_label": r.get("hold_after_acq_shutter_label"),
                                              **({"last_release": r["hold_after_acq_last_release"]}
                                                 if r.get("hold_after_acq_last_release") else {})}}
                      if r.get("hold_after_acq_held") not in (None, "") else {}),
                   **({"settled_s": _f(r.get("settled_s"))} if r.get("settled_s") not in (None, "") else {})}
            hdr_ics, hdr_dn = header_ics_denoise(out)
            if hdr_ics:
                # LabSpec's own record of this spectrum (2026-11-24: 'ICS correction=Off' while ls6t said "LabSpec's
                # setting (a correction file exists)")
                rec["ics"] = hdr_ics
            if hdr_dn:
                rec["denoise"] = hdr_dn
            per_point.append(rec)
            if (kind == "calibration" and k == 0 and int((study.get("calibration") or {}).get("repeats", 1) or 1) > 1
                    and (study.get("calibration") or {}).get("precheck", True) is not False and laser):
                # the laser's health from the first repeat, before the others (Nicola, 2026-11-27: five repeats were
                # measured in a multimode episode): multimode or a weak signal stops the calibration
                hc_ = laser_health(prof, laser, _f(after.get("grating")), out, acq.get("exposure_s"),
                                   "labspec" if not rng.get("single", True) else "centred",
                                   after.get("objective"), after.get("filter"), after.get("hole_um"),
                                   (study.get("acquire") or {}).get("center_cm1"))
                ctx.laser_health = hc_
                if (hc_["verdict"] in ("multimode", "low_signal") and hc_.get("focus_drift_suspect")
                        and not refocused_cal and not af and (zref is not None or zcur is not None)):
                    # Precheck suspected focus drift: refocus once and re-check before stopping
                    _progress(sd, stage="focusing", point=0, points=len(pts))
                    rf_study = _cal_refocus_study(study)
                    fs = focus_scan(ctx, rf_study, laser, zref if zref is not None else zcur,
                                    sd / "focus" / "refocus_precheck", warnings)
                    focus_scans.append(fs)
                    cal_refocus_result = _cal_refocus_summary(fs, plan.scan_params(rf_study, prof),
                                                              "precheck_focus_drift")
                    refocused_cal = True
                    if fs.get("found"):
                        zref = zcur = ctx.z_focus = fs["z_um"]
                        _focus_here(ctx)
                    else:
                        zcur = fs.get("z_now", zcur)
                        warnings.append(f"precheck refocus found no clear best ({fs.get('why')}); kept the previous focus")
                    _progress(sd, stage="measuring", point=0, points=len(pts),
                              eta_min=_eta(t_start, 0, len(pts), study, prof))
                    t_re = time.time()
                    r_re = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
                    t_acq += time.time() - t_re
                    if not r_re["ok"]:
                        raise Stop(f"point 0 (after refocus): acquisition failed: {r_re.get('error')}", "study")
                    rec.update(stage={"x_um": ax, "y_um": ay, "z_um": zcur}, id=r_re.get("id"),
                               t=round(time.time() - t_start, 3))
                    per_point[-1] = rec
                    hc_ = laser_health(prof, laser, _f(after.get("grating")), out, acq.get("exposure_s"),
                                       "labspec" if not rng.get("single", True) else "centred",
                                       after.get("objective"), after.get("filter"), after.get("hole_um"),
                                       (study.get("acquire") or {}).get("center_cm1"))
                    ctx.laser_health = hc_
                    cal_refocus_result["precheck_after_refocus"] = hc_["verdict"]
                    if hc_["verdict"] not in ("multimode", "low_signal"):
                        warnings.append(f"precheck suspected focus drift; refocused (dz={cal_refocus_result['dz_um']:+.3f} um) "
                                        f"and re-check verdict was '{hc_['verdict']}'")
                if hc_["verdict"] in ("multimode", "low_signal"):
                    if hc_.get("focus_drift_suspect"):
                        raise Stop(f"calibration not run: focus may have drifted (counts fell and line broadened together) — "
                                   f"refocus first (or set calibration.refocus true); otherwise the laser may not be healthy "
                                   f"({hc_['verdict']}: " + "; ".join(hc_["reasons"]) + "). If refocusing does not help, "
                                   "wait or power-cycle the laser, then calibrate again (calibration.precheck false calibrates anyway)", "study")
                    raise Stop(f"calibration not run: the laser is not healthy ({hc_['verdict']}: "
                               + "; ".join(hc_["reasons"]) + "). Wait for it to settle, or power-cycle the laser, "
                               "then calibrate again (calibration.precheck false calibrates anyway)", "study")
                mv_ = hc_.get("laser_line_move_cm1")
                if hc_["verdict"] == "mode_change" and hc_.get("same_window_reference") and mv_ is not None \
                        and abs(float(mv_)) > HEALTH_MOVE_MIN_CM1:
                    # a correction stored now would hold in this laser mode only (Nicola, 2026-11-27: 638 at -13.9,
                    # -8.6 from its healthy check at the same window, for hours)
                    msg_ = (f"the laser line sits {float(mv_):+.2f} cm-1 from {hc_.get('laser_line_reference_from')}: "
                            "the laser is in another mode, and a correction measured now holds in this mode only")
                    if (study.get("calibration") or {}).get("accept_mode_change"):
                        warnings.append(msg_ + " (calibration.accept_mode_change: stored for this mode)")
                    else:
                        raise Stop("calibration not run: " + msg_ + ". Wait for the laser to come back (laser_check), "
                                   "or, if this mode is here to stay, calibrate with calibration.accept_mode_change "
                                   "true", "study")
            if r.get("hold_after_acq_held") == "no" and not any("shutter hold was lost" in w for w in warnings):
                warnings.append(f"the shutter hold was lost at point {k}: LabSpec read ShutterMode "
                                f"{r.get('hold_after_acq_shutter_mode')}, the shutter '"
                                f"{r.get('hold_after_acq_shutter_label')}' after it (spectra[].shutter_hold_after)")
            elif r.get("hold_after_acq_held") == "not held" and not any("let the hold go" in w for w in warnings):
                warnings.append(f"at point {k} the bridge had let the hold go ({r.get('hold_after_acq_last_release')}):"
                                " LabSpec opened and closed the shutter itself from there")
                ctx.holding = False
            # live view in LabSpec: the map or the series grows as it is measured (off by default: display.live_view).
            # The view keeps the shutter hold (2026-11-24: releasing it at each view made the 637.75 nm laser hop at
            # every point); display.live_view_while_held false makes the views after the last spectrum instead
            if kind in ("map", "series") and r.get("id") and live_on:
                vval = rec["t"] if kind == "series" else p["dz_um"]
                if ctx.holding and not (prof.get("display") or {}).get("live_view_while_held", True):
                    pending_views.append((r["id"], p, vval, out))
                else:
                    view = _live_view(ctx, view, name, kind, study, rng, r["id"], p, vval, out, warnings)
        except Stop as exc:
            stop_exc = exc
            break
    if pending_views:
        if ctx.holding:
            # the hold ends with the last spectrum, before LabSpec's display is touched
            ctx.holding = False
            try:
                rel = ctx.call("shutter_hold", timeout_s=30, state="off")
                ctx.hold_log.append({"released": bool(rel.get("ok")), "by": "end of the spectra, before the live view"})
            except Stop as exc:
                stop_exc = stop_exc or exc
        for did, p_, vval_, out_ in pending_views:
            try:
                view = _live_view(ctx, view, name, kind, study, rng, did, p_, vval_, out_, warnings)
            except Stop:
                break
    if vd.get("after") and per_point and stop_exc is None:
        try:
            snaps["after"] = _snapshot(ctx, name, sd, "after", bool(vd.get("with_laser")), warnings, laser)
        except Stop as exc:
            stop_exc = exc
    if view and native:
        try:
            vr = ctx.call("view", timeout_s=60, action="save", name=name, path=str(sd / f"{name}.l6s"))
            if not vr["ok"]:
                warnings.append(f"LabSpec could not save the {kind} file: {vr.get('error')}")
        except Stop as exc:
            stop_exc = stop_exc or exc

    if kind == "map" and stop_exc is None:
        _paint_band(ctx, sd, warnings, per_point, study, laser)   # the live map's display with its cursors
    # ---- analysis and checks
    t_an = time.time()
    _progress(sd, stage="analysing")
    if grating_changed:
        checks["grating_changed"] = True     # the first landing after it (analyse: the expected uncertainty)
    from . import runner as _R
    result = _R.analyse(name, study, per_point, prof, laser, rng, sd, warnings, unreliable, checks,
                     grating=_f(after.get("grating")) or _f(study.get("setup", {}).get("grating")),
                     request=str(ctx.folder), objective=after.get("objective"), hole=after.get("hole_um"))
    if kind == "calibration" and warmup_info and "grating change" in str(warmup_info.get("why", "")):
        _learn_first_landing(result, warmup_info, laser, _f(after.get("grating")), rng)
    if stop_exc is not None:
        if stop_exc.by in ("cancel", "operator", "bridge", "timeout"):
            unreliable.append(f"stopped after {len(per_point)} of {len(pts)} points: {stop_exc.why}")
        else:
            unreliable.append(stop_exc.why)
    tm = timing(ctx.calls[n_calls:], per_point, time.time() - t_start)
    result.update(
        ok=bool(per_point), complete=len(per_point) == len(pts) and stop_exc is None,
        reliable=bool(per_point) and not unreliable, unreliable_because=unreliable, warnings=warnings,
        checks=checks, kind=study.get("_kind", kind), sample=study.get("sample", {}),
        setup={"requested": study.get("setup", {}), "before": before, "after": after, "moves": readback},
        instrument={"laser_nm": laser, "detector_temp_c": temp, "system": after.get("system"),
                    "filter": after.get("filter"), "objective": after.get("objective"),
                    "grating": _f(after.get("grating"))},
        range=rng, points_measured=len(per_point), points_planned=len(pts),
        seconds={"total": round(time.time() - t_start, 2), "acquisition": round(t_acq, 2),
                 "operator": tm["operator_s"], "analysis": round(time.time() - t_an, 2)},
        timing=tm,
        **({"warmup": warmup_info} if warmup_info else {}),
        **({"native_map": ctx.native_info} if native_map and getattr(ctx, "native_info", None) else {}),
        **({"laser_health": ctx.laser_health} if getattr(ctx, "laser_health", None) else {}),
        **({"live_band": dict(ctx.live_band, asked=plan.live_band(study))}
           if getattr(ctx, "live_band", None) else {}),
        execution={"mode": config.mode(), "bridge": ctx.bridge_version, "translator": __version__,
                   "protocol": BRIDGE_PROTOCOL})
    if not per_point:
        result["why"] = stop_exc.why if stop_exc else "no spectrum was measured"
    if getattr(ctx, "settle_info", None):
        result["settle"] = ctx.settle_info
    elif laser:
        w_, why_ = settle_reason(prof, laser, getattr(ctx, "settle_kind", kind), bool(getattr(ctx, "laser_changed_now",
                                                                                              False)))
        result["settle"] = {"settle_waited_s": 0.0,
                            "why": why_ if w_ <= 0 else (f"{w_:g} s were due but not waited: "
                                                         + ("the shutter hold continued from the previous study"
                                                            if ctx.holding else "no first acquisition with a wait"))}
    if (study.get("process") or {}).get("laser_check") and per_point and laser:
        hc_ = laser_health(prof, laser, _f(after.get("grating")), per_point[0].get("file"),
                           acq.get("exposure_s"), "centred", after.get("objective"), after.get("filter"),
                           after.get("hole_um"), (study.get("acquire") or {}).get("center_cm1"))
        result["laser_health"] = hc_
        if hc_["verdict"] != "healthy":
            warnings.append(f"laser check: {hc_['verdict']} ({'; '.join(hc_['reasons'])})")
    if cal_refocus_result is not None:
        if isinstance(result.get("calibration"), dict):
            result["calibration"]["refocus_result"] = cal_refocus_result
        else:
            result["refocus_result"] = cal_refocus_result
    if result.get("calibration"):
        store_pending(result["calibration"], warnings, unreliable, prof, objective=after.get("objective"),
                      hole=after.get("hole_um"))
        cr_ = result["calibration"]
        zw_ = cr_.pop("_zero_mode_would", None)
        if isinstance(cr_.get("applies_to"), dict) and laser:
            from . import xcal as _xc2
            k_ = _xc2.key(laser, _f(after.get("grating")), _xc2.setup_of(after.get("objective"), after.get("hole_um")))
            cr_["applies_to"].update(key=k_, setup=_xc2.describe(k_),
                                     what=combo(laser, _f(after.get("grating"))) + f", {_xc2.describe(k_)} only: the "
                                     "objective and the confocal hole move the line too (calibrate with the set-up "
                                     "you measure with)")
        if cr_.get("adjust") == "ls6t" and not cr_.get("stored_as"):
            # said in every case (T9.23 run 2: not judged, no stored_as at all)
            from . import xcal as _xc
            cur_ = (_xc.get(laser, _f(after.get("grating")), cr_.get("stored_correction", {}).get("centre_nm"),
                            setup=_xc.setup_of(after.get("objective"), after.get("hole_um"))) if laser else None)
            cr_["stored_as"] = ("nothing stored" + (f": {cr_['outcome']}" if cr_.get("outcome") else "")
                                + ("; the correction in use stays" + (f" ({cur_.get('time')})" if cur_ else "")
                                   if cur_ else "; no correction is in use for this laser and grating"))
        if str(cr_.get("stored_as") or "").startswith("nothing stored") or not cr_.get("zero_mode"):
            # nothing stored: no zero mode in use from it; the one it would have had, or why none (Nicola, 2026-11-27)
            would_ = cr_.get("zero_mode") or (zw_[0] if zw_ else None)
            cr_["zero_mode"] = None
            if would_:
                cr_["zero_mode_if_stored"] = would_
                cr_["zero_mode_why"] = f"nothing stored; stored, it would have been {would_}"
            else:
                cr_["zero_mode_why"] = (zw_[1] if zw_ and zw_[1] else
                                        "no correction was measured (the reference line not fitted)")
        result["reliable"] = bool(per_point) and not unreliable     # a jump waiting for confirmation
        if (checks.get("calibration") or {}).get("outcome"):
            checks["calibration"]["outcome"] = result["calibration"].get("outcome")
    if kind == "calibration" and (result.get("calibration") or {}).get("fwhm_cm1"):
        _record_resolution(result, after, prof, warnings, str(ctx.folder))
    result["expect"] = evaluate_expect(study.get("expect", []), result)
    if any(e.get("pass") is False for e in result["expect"]):
        warnings.append("an expected value is out of tolerance (see 'expect')")
    if study.get("images") and per_point:
        try:
            from . import render
            result["images"] = render.study_images(name, study, result, sd)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"images not drawn: {type(exc).__name__}: {exc}")
    if focus_scans:
        result["focus_scans"] = focus_scans
    snaps = {k: v for k, v in snaps.items() if v}
    if snaps:
        result["video"] = snaps
        imgs = result.setdefault("images", {})
        for tag, sn in snaps.items():
            if sn.get("path"):
                imgs[f"video_{tag}"] = {"path": sn["path"], "what": sn.get("what")}
    result = clean(result)
    write_json(sd / f"{name}.json", result)
    _progress(sd, state="done" if stop_exc is None else "stopped", stage="done", point=len(per_point))
    ctx.study_dir = None
    if stop_exc is not None and stop_exc.by in ("cancel", "operator", "bridge", "timeout"):
        raise stop_exc
    return result



FOCUS_MIN_COUNTS = 300          # per step at the best, to see a focus curve above counting noise
FOCUS_MIN_CONTRAST_SIGMA = 4.0  # the change with Z must be this many times one step's noise


def _cal_refocus_study(study: dict) -> dict:
    """Study copy with default ±3 um (6 um) range for calibration refocus unless focus.scan.range_um is set."""
    foc = dict(study.get("focus") or {})
    sc = dict(foc.get("scan") or {})
    sc.setdefault("range_um", 6.0)
    foc["scan"] = sc
    return dict(study, focus=foc)


def _cal_refocus_summary(fs: dict, sc: dict, trigger: str) -> dict:
    """Compact refocus report for calibration.refocus_result: range, step, best Z, counts before/after."""
    offs = fs.get("offsets_um") or []
    sig = fs.get("signal") or []
    c_before = None
    for dz, s_ in zip(offs, sig):
        if abs(float(dz)) < 1e-6:
            c_before = round(float(s_), 1)
            break
    c_after = round(float(max(sig)), 1) if (fs.get("found") and sig) else c_before
    z0 = fs.get("z0_um")
    zb = fs.get("z_um") if fs.get("found") else None
    dz_ = round(float(zb) - float(z0), 3) if (zb is not None and z0 is not None) else 0.0
    out = {"trigger": trigger, "range_um": float(sc["range_um"]), "half_range_um": round(float(sc["range_um"]) / 2.0, 3),
           "step_um": float(sc["step_um"]), "found": bool(fs.get("found")),
           "z_before_um": round(float(z0), 3) if z0 is not None else None,
           "z_best_um": round(float(zb), 3) if zb is not None else None,
           "dz_um": dz_, "moved": bool(fs.get("found") and abs(dz_) > 1e-3),
           "counts_before": c_before, "counts_after": c_after,
           "counts_ratio": round(c_after / c_before, 3) if (c_before and c_after is not None) else None,
           "unit": fs.get("unit", "counts/s")}
    if not fs.get("found") and fs.get("why"):
        out["why"] = fs["why"]
    return out


def focus_scan(ctx: Ctx, study: dict, laser: float, z0: float | None, sd: Path, warnings: list) -> dict:
    """Focus by the Raman signal: Z steps across focus.scan.range_um around z0, a short acquisition at each, the
    line's height above its window's edges; the best step (refined by a parabola through it and its neighbours)
    becomes the focus. Without a clear maximum inside the range, Z goes back to z0."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sc = plan.scan_params(study, prof)
    if z0 is None:
        raise Stop("the Z position could not be read: a focus scan needs it", "study")
    sd.mkdir(parents=True, exist_ok=True)
    if sc.get("from_below"):
        return _focus_from_below(ctx, study, laser, sc, sd, warnings)
    offs = plan.scan_offsets(sc)
    line, win = float(sc["line_cm1"]), float(sc["window_cm1"])
    nm = round(shift_to_nm(line, laser), 4)
    zs, vals, counts, sigmas = [], [], [], []
    e_gain = max(float((prof.get("detector") or {}).get("e_per_count", 1.0) or 1.0), 1e-6)
    for i, dz in enumerate(offs):
        z = move_z(ctx, z0 + dz, f"focus scan step {i}", focusing=True)
        out = sd / f"z{i:03d}.txt"
        r = ctx.call("acquire", timeout_s=60 + 3 * float(sc["exposure_s"]), mode=110, exposure_s=float(sc["exposure_s"]),
                     accumulations=1, from_nm=nm, to_nm=nm, out=str(out), sample="focus scan")
        if not r["ok"]:
            raise Stop(f"focus scan step {i}: acquisition failed: {r.get('error')}", "study")
        sp = spectra.read(out)
        x, unit = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        x, y = spectra.sort_xy(x, sp["y"])
        zs.append(z)
        h = spectra.line_signal(x, y, line, win)
        # the noise of that height (2026-11-24: about 25 counts per step, 20 % noise, more than the curve's structure,
        # and a focus was still chosen): the spectrum's own pixel noise (measured: the background's photons and the
        # read noise) and the line's extra photons at its top
        ns = spectra.noise_sigma(y)
        ns = 0.0 if ns is None or not math.isfinite(ns) else ns
        counts.append(round(float(h), 1))
        sigmas.append(round(math.sqrt(max(h, 0.0) / e_gain + ns ** 2), 2))
        vals.append(h / float(sc["exposure_s"]))
    zs_a, v = np.array(zs), np.array(vals)
    i = int(np.argmax(v))
    res = {"z_um": None, "z0_um": z0, "offsets_um": offs, "z_steps_um": zs, "signal": [round(a, 3) for a in vals],
           "counts": counts, "counts_sigma": sigmas,
           "snr": [round(c / s_, 1) if s_ else None for c, s_ in zip(counts, sigmas)],
           "line_cm1": line, "unit": "counts/s", "found": False}
    contrast = (v.max() - v.min()) / max(abs(v.max()), 1e-12)
    interior = 0 < i < len(v) - 1
    # the change with Z against the noise of one step, in counts
    span_counts = (v.max() - v.min()) * float(sc["exposure_s"])
    noise_counts = float(np.median(sigmas)) if sigmas else 0.0
    res["contrast_over_noise"] = round(span_counts / noise_counts, 2) if noise_counts else None
    if max(counts or [0]) < FOCUS_MIN_COUNTS:
        want = float(sc["exposure_s"]) * FOCUS_MIN_COUNTS / max(max(counts or [1.0]), 1.0)
        warnings.append(f"focus scan: at most {max(counts or [0]):.0f} counts per step, too few to see the focus "
                        f"(about {FOCUS_MIN_COUNTS} wanted): focus.scan.exposure_s about {want:.1f} s, or a stronger "
                        "filter")
    noisy = noise_counts and span_counts < FOCUS_MIN_CONTRAST_SIGMA * noise_counts
    if v.max() <= 0 or contrast < 0.2 or not interior or noisy:
        res["why"] = ("the best is at the edge of the range: widen focus.scan.range_um or start closer" if not interior
                      else (f"the signal changes with Z by {span_counts:.0f} counts, less than "
                            f"{FOCUS_MIN_CONTRAST_SIGMA:g} x one step's noise ({noise_counts:.0f} counts): a longer "
                            "exposure or a stronger filter") if noisy
                      else "the signal hardly changes with Z: no line, or the range is far from focus")
        res["z_now"] = move_z(ctx, z0, "focus scan: back to the start", focusing=True)
        res["z_left_at"] = "the starting Z (no focus found)"
    else:
        # a Gaussian through the points above 30 % of the best (a parabola in log, weighted by the signal); a
        # three-point parabola around the best when too few points qualify
        sel = v > 0.3 * v.max()
        zb = None
        fwhm = None
        if sel.sum() >= 4:
            c2, c1, _ = np.polyfit(zs_a[sel], np.log(v[sel]), 2, w=np.sqrt(v[sel]))
            if c2 < 0:
                zb = float(-c1 / (2 * c2))
                fwhm = float(2.0 * math.sqrt(math.log(2.0) / -c2))   # of the Gaussian exp(c2 z^2 + ...)
        if zb is None or not zs_a.min() <= zb <= zs_a.max():
            y1, y2, y3 = v[i - 1], v[i], v[i + 1]
            den = (y1 - 2 * y2 + y3)
            step = zs_a[i + 1] - zs_a[i]
            frac = 0.5 * (y1 - y3) / den if den < 0 else 0.0
            zb = float(zs_a[i] + max(-0.5, min(0.5, frac)) * step)
        res.update(found=True, z_um=round(move_z(ctx, zb, "focus scan: to the best", focusing=True), 3),
                   best_signal=round(float(v.max()), 3), shift_um=round(zb - z0, 3), contrast=round(contrast, 3))
        if fwhm is not None and fwhm < 0.9 * (zs_a.max() - zs_a.min()):
            res["axial_fwhm_um"] = round(fwhm, 3)       # the depth resolution on this sample (stage µm)
        shp_ = axial_shape(zs_a, v, zb)
        if shp_:
            res["axial_profile"] = shp_
            if shp_.get("second_maximum") and res.get("axial_fwhm_um") is not None:
                # one FWHM does not describe two maxima: kept aside, not reported or recorded as the depth resolution
                shp_["fwhm_of_main_peak_um_not_representative"] = res.pop("axial_fwhm_um")
            if shp_.get("lopsided") or shp_.get("second_maximum"):
                warnings.append("the axial profile is not one peak: "
                                + "; ".join(x_ for x_ in (
                                    (f"half widths {shp_['half_width_below_um']:g} and {shp_['half_width_above_um']:g}"
                                     " um on the two sides" if shp_.get("lopsided") else None),
                                    (f"a second maximum at {shp_['second_maximum']['z_um']:+g} um from the focus "
                                     f"({shp_['second_maximum']['fraction']:.0%} of the best)"
                                     if shp_.get("second_maximum") else None)) if x_)
                                + ": a single axial FWHM does not describe it (at a small confocal hole the spot's "
                                  "fill of the hole changes with Z)")
        res["z_now"] = res["z_um"]
        res["z_left_at"] = "the focus found (later spectra and studies use it)"
    # the objective's plausibility: at x50 / x100 the Raman signal falls off within a few µm of focus; flat over
    # +-5 µm suggests a lower magnification than LabSpec reports (2026-11-27: x10 in place while LabSpec said x100)
    mag_ = plan.objective_mag(getattr(ctx, "objective", None))
    offs_a = np.asarray(offs, float)
    near5 = np.abs(offs_a) <= 5.0 + 1e-6
    if mag_ and mag_ >= 50 and near5.sum() >= 3 and offs_a.min() <= -4.0 and offs_a.max() >= 4.0 and v.max() > 0:
        rel_ = float(v[near5].min() / v[near5].max())
        res["objective_check"] = {"objective": getattr(ctx, "objective", None), "signal_min_over_max_5um": round(rel_, 3)}
        if rel_ > 0.8:
            res["objective_check"]["suspect"] = True
            warnings.append(f"the objective may not be the one LabSpec reports ({ctx.objective}): the Raman signal "
                            f"varies by only {100 * (1 - rel_):.0f} % over +-5 µm, as with a lower magnification "
                            "(x100 falls off within a few µm); check the turret")
    try:
        from . import render
        res["image"] = render.focus_png(sd / "focus_scan.png", res)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"focus scan plot not drawn: {type(exc).__name__}: {exc}")
    write_json(sd / "focus_scan.json", clean(res))
    return res


def _scan_signal(ctx: Ctx, laser: float, sc: dict, out: Path) -> float:
    """One short acquisition on the focus line: its height above the window's edges, per second."""
    from .profile import shift_to_nm
    line, win = float(sc["line_cm1"]), float(sc["window_cm1"])
    nm = round(shift_to_nm(line, laser), 4)
    r = ctx.call("acquire", timeout_s=60 + 3 * float(sc["exposure_s"]), mode=110, exposure_s=float(sc["exposure_s"]),
                 accumulations=1, from_nm=nm, to_nm=nm, out=str(out), sample="focus scan")
    if not r["ok"]:
        raise Stop(f"focus search: acquisition failed: {r.get('error')}", "study")
    sp = spectra.read(out)
    x, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
    x, y = spectra.sort_xy(x, sp["y"])
    return spectra.line_signal(x, y, line, win) / float(sc["exposure_s"])


CAM_MIN_TEXTURE = 3.0      # the sharpest frame's fine structure over the camera's frame-to-frame noise
CAM_MIN_CONTRAST = 4.0     # the change of sharpness with Z over its own repeat scatter


def _cam_frame(ctx: Ctx, base: Path, settle: float) -> tuple:
    """One top-camera frame, white light, no laser: (decoded frame, luminance)."""
    v = ctx.prof.get("video", {})
    base.parent.mkdir(parents=True, exist_ok=True)
    r = ctx.call("video", timeout_s=90 + settle, out=str(base), settle_s=settle, laser_s=0, formats="",
                 camera=v.get("camera"), switch_camera=int(bool(v.get("switch_camera"))),
                 stop_video=int(bool(v.get("stop_from_script"))), **motor_args(ctx.prof))
    if not r.get("ok") or not r.get("out"):
        raise Stop(f"camera focus: no video frame ({r.get('error') or 'no file'})", "study")
    dec = video.decode(r["out"], v.get("packing", "colorref"), v.get("order", "x_fastest"))
    return dec, video._lum(dec["img"])


def _sharpness(lum) -> float:
    """Gradient energy (Tenengrad) of the frame's central 80 %, over its mean brightness squared: rises as the
    surface's structure comes into focus, independent of the lamp's level."""
    from scipy.ndimage import gaussian_filter, sobel
    a = np.asarray(lum, float)
    h, w = a.shape
    a = gaussian_filter(a[h // 10: h - h // 10, w // 10: w - w // 10], 0.7)
    g = sobel(a, 0) ** 2 + sobel(a, 1) ** 2
    return float(np.mean(g)) / max(float(np.mean(a)), 1.0) ** 2


def _band(a):
    """The frame's structure from about 1 to 8 pixels (a difference of Gaussians): features, edges, particles."""
    from scipy.ndimage import gaussian_filter
    a = np.asarray(a, float)
    return gaussian_filter(a, 1.0) - gaussian_filter(a, 8.0)


def _texture(lum, band_noise: float) -> float:
    """The frame's structure (_band) over what the camera's noise alone gives in the same band."""
    return float(np.std(_band(lum))) / max(band_noise, 0.05)


def camera_focus(ctx: Ctx, study: dict, z0: float | None, sd: Path, warnings: list) -> dict:
    """ls6t's camera autofocus: the top camera under white light, no laser. Coarse Z steps (about half the depth of
    field) from the far side toward the sample, within z_limits (the objective's measured clearance when there is
    one), then a fine pass around the sharpest frame; the best Z from a parabola through the sharpness. Refused, Z
    back where it started, when the view has no structure to focus on (texture under CAM_MIN_TEXTURE x the camera's
    noise), when the sharpness hardly changes with Z, or when the sharpest frame is at the edge of the range."""
    if z0 is None:
        raise Stop("the Z position could not be read: a camera focus needs it", "study")
    prof = ctx.prof
    zl = plan.z_limits(prof, ctx.objective)
    if zl["toward_sign"] is None:
        raise Stop("camera focus: the profile does not say which Z direction approaches the objective "
                   "(stage.z_into_sample, TESTING T3.17)", "study")
    cp = plan.camera_params(study, prof, ctx.objective)
    s, step, settle = zl["toward_sign"], float(cp["step_um"]), float(cp["settle_s"])
    half = float(cp["range_um"]) / 2
    toward = min(half, float(zl["toward_um"]))
    away = min(half, float(zl["away_um"]))
    sd.mkdir(parents=True, exist_ok=True)
    res: dict = {"method": "camera", "z_um": None, "z0_um": z0, "found": False, "step_um": step,
                 "fine_step_um": cp["fine_step_um"], "range_toward_um": round(toward, 2),
                 "range_away_um": round(away, 2), "toward_limit_from": zl.get("toward_from")}
    # the camera's own noise: two frames at the start
    d0, l0 = _cam_frame(ctx, sd / "z_start_a", settle)
    if video.frame_too_dark(l0):
        raise Stop("camera focus: the frame is black or without contrast: switch the white light on (bright field) "
                   "and check the camera", "study")
    _d1, l1 = _cam_frame(ctx, sd / "z_start_b", settle)
    diff = np.asarray(l0, float) - np.asarray(l1, float)
    noise = float(np.std(diff)) / math.sqrt(2)
    band_noise = float(np.std(_band(diff))) / math.sqrt(2)
    # a region to focus on (focus.camera.roi: [x0, y0, x1, y1] as fractions of the frame, or 'auto' for the most
    # structured tile; 2026-11-27: a structured strip at the bottom, featureless Si elsewhere)
    roi = cp.get("roi")
    if roi == "auto":
        a_ = np.asarray(l0, float)
        h_, w_ = a_.shape
        best_ = None
        for ty in range(4):
            for tx in range(4):
                t_ = a_[ty * h_ // 4:(ty + 1) * h_ // 4, tx * w_ // 4:(tx + 1) * w_ // 4]
                tex_ = _texture(t_, band_noise)
                if best_ is None or tex_ > best_[0]:
                    best_ = (tex_, [tx / 4, ty / 4, (tx + 1) / 4, (ty + 1) / 4])
        roi = best_[1]
        res["roi_auto_texture"] = round(best_[0], 2)
    if roi:
        res["roi"] = [round(float(v_), 3) for v_ in roi]

    def crop(lum):
        if not roi:
            return lum
        a_ = np.asarray(lum, float)
        h_, w_ = a_.shape
        x0_, y0_, x1_, y1_ = (float(v_) for v_ in roi)
        return a_[int(y0_ * h_):max(int(y1_ * h_), int(y0_ * h_) + 8), int(x0_ * w_):max(int(x1_ * w_), int(x0_ * w_) + 8)]
    s_rep = abs(_sharpness(crop(l0)) - _sharpness(crop(l1)))
    res["camera_noise"] = round(noise, 3)
    # coarse: from the far side toward the sample (offsets in the toward direction)
    n_away, n_tow = int(math.floor(away / step + 1e-9)), int(math.floor(toward / step + 1e-9))
    offs = [k * step for k in range(-n_away, n_tow + 1)]
    zs, sh, tex = [], [], []
    for i, o in enumerate(offs):
        z = move_z(ctx, z0 + s * o, f"camera focus step {i}", focusing=True)
        _d, lum = _cam_frame(ctx, sd / f"c{i:03d}", settle)
        zs.append(z)
        sh.append(_sharpness(crop(lum)))
        tex.append(_texture(crop(lum), band_noise))
    k = int(np.argmax(sh))
    res.update(offsets_um=[round(o, 3) for o in offs], z_steps_um=zs, sharpness=[round(v, 6) for v in sh],
               texture=[round(v, 2) for v in tex])
    span = max(sh) - min(sh)
    scatter = max(s_rep, 0.02 * max(sh))
    res["contrast_over_noise"] = round(span / scatter, 2) if scatter else None
    res["texture_at_best"] = round(tex[k], 2)
    why = None
    if tex[k] < CAM_MIN_TEXTURE:
        why = (f"no discernible features: the sharpest view's structure is {tex[k]:.1f} x the camera's noise "
               f"(at least {CAM_MIN_TEXTURE:g} wanted): a featureless surface; focus on the Raman line "
               "(focus.autofocus 'scan') or on an edge or a particle")
    elif span < CAM_MIN_CONTRAST * scatter:
        why = (f"the sharpness hardly changes with Z ({span / scatter:.1f} x its repeat scatter, at least "
               f"{CAM_MIN_CONTRAST:g} wanted): out of range, or nothing to focus on")
    elif not 0 < k < len(sh) - 1:
        why = ("the sharpest frame is at the " + ("far" if k == 0 else "near") + " end of the range"
               + (" (the objective's limit toward the sample)" if k == len(sh) - 1 and toward < half else "")
               + ": start closer to the focus")
    if why:
        res["why"] = why
        res["z_now"] = move_z(ctx, z0, "camera focus: back to the start", focusing=True)
        res["z_left_at"] = "the starting Z (no focus found)"
    else:
        # fine: +/- one coarse step around the best, a quarter step apart, from the far side
        c0 = offs[k]
        fo = [round(c0 + j * cp["fine_step_um"], 4) for j in range(-4, 5)
              if -away <= c0 + j * cp["fine_step_um"] <= toward]
        fz, fsh = [], []
        for j, o in enumerate(fo):
            z = move_z(ctx, z0 + s * o, f"camera focus fine step {j}", focusing=True)
            _d, lum = _cam_frame(ctx, sd / f"f{j:03d}", settle)
            fz.append(z)
            fsh.append(_sharpness(crop(lum)))
        kb = int(np.argmax(fsh))
        ob = fo[kb]
        if 0 < kb < len(fsh) - 1:
            y1, y2, y3 = fsh[kb - 1], fsh[kb], fsh[kb + 1]
            den = y1 - 2 * y2 + y3
            if den < 0:
                ob = fo[kb] + max(-0.5, min(0.5, 0.5 * (y1 - y3) / den)) * cp["fine_step_um"]
        ob = max(-away, min(toward, ob))
        zb = move_z(ctx, z0 + s * ob, "camera focus: to the sharpest", focusing=True)
        res.update(found=True, z_um=round(zb, 3), shift_um=round(s * ob, 3), fine_offsets_um=fo,
                   fine_sharpness=[round(v, 6) for v in fsh],
                   z_left_at="the focus found (later spectra and studies use it)")
    try:
        from . import render
        img = render.focus_png(sd / "camera_focus.png",
                               {"z0_um": z0, "z_steps_um": zs, "signal": sh, "found": res["found"],
                                "z_um": res.get("z_um"), "shift_um": res.get("shift_um", 0.0), "line_cm1": 0,
                                "why": res.get("why")})
        img["what"] = "the camera's sharpness against Z; the focus chosen"
        res["image"] = img
    except Exception as exc:  # noqa: BLE001 - a plot is never a reason to fail
        warnings.append(f"camera focus plot: {exc}")
    return res


def _focus_from_below(ctx: Ctx, study: dict, laser: float, sc: dict, sd: Path, warnings: list) -> dict:
    """The focus searched approaching the sample from the away side (after a retract-travel-return): coarse steps
    from where Z is toward the sample, never past the old focus plus the objective's toward limit, stopping once the
    signal has clearly passed its peak; then an ordinary fine scan around the best coarse step."""
    zl = plan.z_limits(ctx.prof, ctx.objective)
    s = zl["toward_sign"]
    if s is None:
        raise Stop("a focus search from below needs stage.z_into_sample (TESTING T3.17)", "study")
    st = ctx.call("status", timeout_s=15, **motor_args(ctx.prof))
    z_start = _f(st.get("z_um"))
    if z_start is None:
        raise Stop("the Z position could not be read: no focus search", "study")
    ref = ctx.z_focus if ctx.z_focus is not None else z_start
    z_end = ref + s * zl["toward_um"]
    span = (z_end - z_start) * s
    if span <= float(sc["step_um"]):
        raise Stop("focus search from below: Z is not on the away side of the old focus (nothing to approach)", "study")
    coarse = max(float(sc["step_um"]), min(span / 30.0, plan.coarse_focus_step_um(ctx.prof, ctx.objective)))
    zs, vals = [], []
    k = 0
    while True:
        z_t = z_start + s * k * coarse
        if (z_t - z_end) * s > 1e-9:
            break
        z = move_z(ctx, z_t, f"focus search from below, step {k}", focusing=True, away_ok=True)
        zs.append(z)
        vals.append(_scan_signal(ctx, laser, sc, sd / f"below{k:03d}.txt"))
        k += 1
        i = int(np.argmax(vals))
        base = float(np.median(vals[:3])) if len(vals) >= 3 else None
        if base is not None and vals[i] > 3 * max(base, 1e-12) and len(vals) - 1 - i >= 2 and vals[-1] < 0.5 * vals[i]:
            break                                    # a clear peak, well passed: no need to go nearer
    i = int(np.argmax(vals)) if vals else 0
    base = float(np.median(vals[:3])) if len(vals) >= 3 else (vals[0] if vals else 0.0)
    res = {"from_below": True, "z_start_um": z_start, "z_limit_um": z_end, "coarse_step_um": round(coarse, 3),
           "coarse_z_um": zs, "coarse_signal": [round(v, 3) for v in vals]}
    if not vals or vals[i] <= 3 * max(base, 1e-12) or not 0 < i < len(vals) - 1:
        res.update(found=False, why="no clear peak between the start and the limit toward the sample",
                   z_now=_z_direct(ctx, z_start, "focus search: back to the start"))
        write_json(sd / "focus_scan.json", clean(res))
        return res
    half = min(2 * coarse, max(float(sc["step_um"]), (z_end - zs[i]) * s))
    fine = dict(study, focus=dict(study.get("focus") or {}, scan=dict(sc, from_below=False, range_um=round(2 * half, 3))))
    ctx_z = ctx.z_focus
    ctx.z_focus = zs[i]                          # the fine scan's Z limits count from the coarse best
    try:
        fs = focus_scan(ctx, fine, laser, zs[i], sd / "fine", warnings)
    finally:
        ctx.z_focus = ctx_z                      # the caller sets the new focus from the result
    fs.update({k_: v for k_, v in res.items() if k_ not in ("found",)})
    write_json(sd / "focus_scan.json", clean(fs))
    return fs


def run_focus(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'focus': a focus scan on the Raman line at the current spot; the focus found becomes the request's
    focus (later studies' Z offsets start from it, and Z returns to it at the end)."""
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    _progress(sd, state="running", stage="setup", started=t_start,
              estimate_min=plan.estimate(study, ctx.prof)["minutes"])
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    _progress(sd, stage="focusing")
    z0 = _f(su["after"].get("z_um"))
    if (study.get("focus") or {}).get("autofocus") == "camera":
        fs = camera_focus(ctx, study, ctx.z_focus if ctx.z_focus is not None else z0, sd / "focus", warnings)
    else:
        fs = focus_scan(ctx, study, su["laser"], ctx.z_focus if ctx.z_focus is not None else z0, sd / "focus",
                        warnings)
    if fs.get("found") and fs.get("method") == "camera":
        ctx.z_focus = fs["z_um"]
        _focus_here(ctx)
    elif fs.get("found"):
        ctx.z_focus = fs["z_um"]
        _focus_here(ctx)
        if fs.get("axial_fwhm_um"):
            from . import perf
            k = perf.key(su["after"].get("objective") or "?", su["laser"], _f(su["after"].get("hole_um")))
            rec = perf.record("axial", k, {"fwhm_um": fs["axial_fwhm_um"], "line_cm1": fs.get("line_cm1"),
                                           "sample": (study.get("sample") or {}).get("name")}, str(ctx.folder))
            fs["axial_resolution"] = {"key": k, "fwhm_um": fs["axial_fwhm_um"],
                                      "baseline_um": rec["baseline"].get("fwhm_um"), "measurements": rec["n"]}
    else:
        unreliable.append(f"no clear focus: {fs.get('why')}")
    result = study_envelope(
        "focus", study, ctx, t_start,
        reliable=fs.get("found", False) and not unreliable,
        unreliable=unreliable, warnings=warnings, checks=checks,
        instrument={"laser_nm": su["laser"], "detector_temp_c": su["temp"],
                    "filter": su["after"].get("filter"), "objective": su["after"].get("objective")},
        setup={"requested": study.get("setup", {}), "before": su["before"], "after": su["after"],
               "moves": su["readback"]},
        extra={"focus": fs},
    )
    if fs.get("image"):
        result["images"] = {"focus_scan": fs["image"]}
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


def run_move(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'move': no laser. The stage to an offset from the home spot (and Z to an offset from the focus), a
    video snapshot there; with stay (default) the new spot becomes home for the rest of the request and the start
    of the next one. Z always returns to the focus at the end of the request."""
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    m = study.get("move", {})
    warnings: list[str] = []
    _progress(sd, state="running", stage="moving", started=t_start)
    before = ctx.call("status", timeout_s=15, **motor_args(prof))
    obj = (study.get("setup") or {}).get("objective")
    if obj is not None:
        confirmed_now = False           # MOTOR_NO_MESSAGE only for the move the operator has just confirmed here
        if prof.get("manual_objective", True) and str(obj) != str(before.get("objective")) \
                and config.confirm_policy() != "never":
            from .api import objective_question
            ans = ctx.call("confirm", timeout_s=3600, text=objective_question(prof, before.get("objective"), obj))
            if ans.get("answer") != "yes":
                raise Stop("the operator declined to change the objective", "operator")
            ctx.objective_by_operator = str(obj)
            confirmed_now = True
        r = ctx.call("move", timeout_s=600, motor=prof["motors"]["objective"], mode="label", value=str(obj),
                     **move_extra(prof, prof["motors"]["objective"], confirmed_now))
        if not r["ok"] or not P.same_position(r.get("label"), obj):
            raise Stop(f"objective {obj}: {r.get('error') or 'got ' + str(r.get('label'))}", "study")
    hx, hy = ctx.home_xy
    if None in (hx, hy):
        raise Stop("the stage position could not be read: moves are refused", "study")
    beam = None
    cur_obj = str(obj) if obj is not None else before.get("objective")
    if m.get("to_um") is not None and prof.get("manual_objective", True) and ctx.objective_by_operator != cur_obj:
        # a point picked on a snapshot: its µm scale and the beam offset come from the objective LabSpec records; with
        # another lens on the (manual) turret the stage would go to the wrong place: the operator confirms the lens
        if config.confirm_policy() != "never":
            ans = ctx.call("confirm", timeout_s=3600,
                           text=f"ls6t will move the stage to a point picked on a video snapshot, scaled for objective "
                                f"{cur_obj} (LabSpec's setting). Is {cur_obj} the lens on the turret now? (Cancel: no "
                                f"move)")
            if ans.get("answer") != "yes":
                raise Stop(f"the operator did not confirm objective {cur_obj}: no move", "operator")
            ctx.objective_by_operator = cur_obj
        else:
            warnings.append(f"objective {cur_obj}: LabSpec's setting, not checked (manual turret, confirmations "
                            "off): if another lens is in place, the stage goes to the wrong place")
    if m.get("to_um") is not None:
        # put the beam (not LabSpec's marker) on a point picked in a snapshot: stage = point - the beam's offset
        objective = obj if obj is not None else before.get("objective")
        from . import video as V
        if V.field_um(prof, objective) and V.axes_directions(prof, objective, before.get("camera"))[2] == "assumed":
            # 2026-11-23: with the assumed directions a particle at y = -155 µm showed at +153 µm
            raise Stop(f"{name}: the snapshot's X / Y directions for objective {objective} are assumed, not measured "
                       "(they can be mirrored: on the XploRA's top camera image up is stage -Y): run `ls6t "
                       "instrument video-scale` with this objective (no laser), take the snapshot again, then pick "
                       "the point", "study")
        off, src = SP.offset_for(prof, objective, _f(before.get("laser_nm")))
        beam = {"to_um": [float(v) for v in m["to_um"]], "offset_um": off, "offset_source": src}
        tx, ty = beam["to_um"][0] - off[0], beam["to_um"][1] - off[1]
        if math.hypot(tx - hx, ty - hy) > prof["limits"]["max_xy_um"]:
            raise Stop(f"{name}: {math.hypot(tx - hx, ty - hy):.0f} µm from the home spot, above "
                       f"limits.max_xy_um = {prof['limits']['max_xy_um']:g} µm", "study")
        if src == "none measured":
            warnings.append("no beam offset measured for this objective and laser: the beam lands where LabSpec's "
                            "marker is (run a 'spot' study to find it)")
    else:
        tx, ty = hx + float(m.get("x_um", 0) or 0), hy + float(m.get("y_um", 0) or 0)
    _check_abs(prof, "x", tx, name)
    _check_abs(prof, "y", ty, name)
    bx, by = _f(before.get("x_um")), _f(before.get("y_um"))
    dist = math.hypot(tx - (bx if bx is not None else tx), ty - (by if by is not None else ty))
    travel = None
    dz = float(m.get("dz_um", 0) or 0)
    if plan.travel_mode(prof, study, dist, cur_obj) == "retract":
        travel = travel_xy(ctx, tx, ty, float((study.get("sample") or {}).get("unevenness_um") or 0), name)
        xy, z = travel["xy_um"], travel["descended_to_um"]
        if m.get("focus") == "video":
            z = _video_focus_after_travel(ctx, travel, name, warnings)
        else:
            warnings.append(f"retract-travel-return: Z is {travel['unevenness_um']:g} µm short of the old focus, away "
                            "from the sample: focus here (move.focus 'video', or a 'focus' study with "
                            "focus.scan.from_below) before measuring")
    else:
        r = ctx.call("move_xy", timeout_s=300, x_um=round(tx, 3), y_um=round(ty, 3), **motor_args(prof))
        if not r["ok"]:
            raise Stop(f"{name}: XY move failed: {r.get('error')}", "study")
        xy = [_f(r.get("x_um")), _f(r.get("y_um"))]
        z = _f(before.get("z_um"))
        if ctx.z_focus is not None and (dz or (z is not None and abs(z - ctx.z_focus) > 0.05)):
            z = move_z(ctx, ctx.z_focus + dz, name)
    if m.get("stay", True):
        ctx.home_xy = (xy[0] if xy[0] is not None else tx, xy[1] if xy[1] is not None else ty)
    snap = None
    if m.get("snapshot", True):
        snap = _snapshot(ctx, name, sd, "here", False, warnings, _f(before.get("laser_nm")))
    result = study_envelope(
        "move", study, ctx, t_start,
        warnings=warnings,
        setup={"before": before},
        extra={"move": {"offset_um": [float(m.get("x_um", 0) or 0), float(m.get("y_um", 0) or 0)],
                        "dz_um": dz, "stage_um": xy, "z_um": z, "stay": m.get("stay", True),
                        "home_um": list(ctx.home_xy), "objective": cur_obj,
                        "objective_source": _objective_source(ctx, cur_obj), "beam": beam,
                        "travel": travel or "direct"}},
    )
    if snap:
        result["video"] = {"here": snap}
        if snap.get("path"):
            result["images"] = {"video_here": {"path": snap["path"], "what": snap.get("what")}}
    if m.get("stay", True):
        warnings.append("the stage stays here: later offsets (in this request and the next) start from this spot")
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


# --------------------------------------------------------------------------------------------- the laser spot

def raman_spot(ctx: Ctx, study: dict, sp: dict, laser: float, objective, sd: Path, warnings: list) -> dict:
    """Find the beam by the Raman signal on a small feature seen on the video: the stage scans a small grid so the
    beam passes over the feature; where the line's signal changes most (dip for dust on Si, peak for a particle with
    the line) the beam sat on the feature. offset = feature - stage there."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sd.mkdir(parents=True, exist_ok=True)
    st = ctx.call("status", timeout_s=15, **motor_args(prof))
    s0 = (_f(st.get("x_um")), _f(st.get("y_um")))
    if None in s0:
        raise Stop("the stage position could not be read", "study")
    snap = _snapshot(ctx, "spot", sd, "feature", False, warnings, laser, remember=False)
    dec = (snap or {}).pop("_decoded", None)
    out: dict = {"method": "raman", "found": False, "snapshot": snap}
    if dec is None or not dec.get("um"):
        out["why"] = "no usable video frame (axes in µm needed) to locate the feature"
        return out
    feat = sp.get("feature_um")
    if feat in (None, "auto"):
        cx, cy = float(np.mean(dec["xs"])), float(np.mean(dec["ys"]))
        cands = [f for f in (snap.get("features") or []) if f["size_um"] <= 6 and f["strength"] >= 10]
        if not cands:
            out["why"] = "no small feature found on the video: give spot.feature_um (a point picked in a snapshot)"
            return out
        f0 = min(cands, key=lambda f: math.hypot(f["x_um"] - cx, f["y_um"] - cy))
        F = (f0["x_um"], f0["y_um"])
        out["feature_auto"] = f0
    else:
        F = (float(feat[0]), float(feat[1]))
    if sp.get("refine", True):
        rf = video.refine_feature(dec, F[0], F[1], max(2.0, float(sp["range_um"]) / 2))
        if rf:
            out["feature_refined"] = rf
            F = (rf["x_um"], rf["y_um"])
        else:
            warnings.append("the feature could not be refined on the video: its given position is used")
    out["feature_um"] = [round(F[0], 3), round(F[1], 3)]
    o0, src = SP.offset_for(prof, objective, laser)
    out["prior_offset_um"], out["prior_source"] = o0, src
    g = plan.axis_values([-float(sp["range_um"]) / 2, float(sp["range_um"]) / 2, float(sp["step_um"])])
    nm = round(shift_to_nm(float(sp["line_cm1"]), laser), 4)
    hx, hy = ctx.home_xy
    V = np.full((len(g), len(g)), np.nan)
    grid_pos = []
    for iy, gy in enumerate(g):
        row = list(enumerate(g)) if iy % 2 == 0 else list(enumerate(g))[::-1]
        for ix, gx in row:
            sx, sy = F[0] - o0[0] + gx, F[1] - o0[1] + gy
            if math.hypot(sx - hx, sy - hy) > prof["limits"]["max_xy_um"]:
                raise Stop(f"the spot scan would go {math.hypot(sx - hx, sy - hy):.0f} µm from the home spot "
                           f"(limits.max_xy_um = {prof['limits']['max_xy_um']:g})", "study")
            _check_abs(prof, "x", sx, "spot scan")
            _check_abs(prof, "y", sy, "spot scan")
            r = ctx.call("move_xy", timeout_s=120, x_um=round(sx, 3), y_um=round(sy, 3), **motor_args(prof))
            if not r["ok"]:
                raise Stop(f"spot scan: XY move failed: {r.get('error')}", "study")
            f = sd / f"g{iy:02d}_{ix:02d}.txt"
            r = ctx.call("acquire", timeout_s=60 + 3 * float(sp["exposure_s"]), mode=110,
                         exposure_s=float(sp["exposure_s"]), accumulations=1, from_nm=nm, to_nm=nm, out=str(f),
                         sample="spot scan")
            if not r["ok"]:
                raise Stop(f"spot scan: acquisition failed: {r.get('error')}", "study")
            spc = spectra.read(f)
            x, _ = spectra.to_shift(spc["x"], spc["meta"].get("x_unit", "nm"), laser)
            x, y = spectra.sort_xy(x, spc["y"])
            V[iy, ix] = spectra.line_signal(x, y, float(sp["line_cm1"]), float(sp["window_cm1"])) / float(sp["exposure_s"])
            grid_pos.append([round(sx, 3), round(sy, 3)])
    ctx.call("move_xy", timeout_s=120, x_um=round(s0[0], 3), y_um=round(s0[1], 3), **motor_args(prof))
    border = np.concatenate([V[0, :], V[-1, :], V[1:-1, 0], V[1:-1, -1]])
    b = float(np.median(border))
    noise = float(1.4826 * np.median(np.abs(border - b))) or max(abs(b) * 0.02, 1e-9)
    dev = V - b
    dark = abs(np.nanmin(dev)) > abs(np.nanmax(dev))
    c = -dev if dark else dev
    contrast = float(np.nanmax(c) / noise)
    GX, GY = np.meshgrid(g, g)
    w = np.clip(c - 0.5 * np.nanmax(c), 0, None)
    out.update(grid_um=g, signal=V.round(3).tolist(), dark=bool(dark), contrast_sigma=round(contrast, 1),
               line_cm1=float(sp["line_cm1"]))
    if contrast < 6 or not np.isfinite(contrast) or w.sum() <= 0:
        out["why"] = (f"the feature hardly changes the signal ({contrast:.1f} sigma): another feature, a longer "
                      "exposure, or the line it has (spot.line_cm1)")
    else:
        gx_hit, gy_hit = float((w * GX).sum() / w.sum()), float((w * GY).sum() / w.sum())
        edge = max(abs(gx_hit), abs(gy_hit)) > g[-1] - float(sp["step_um"])
        sx_hit, sy_hit = F[0] - o0[0] + gx_hit, F[1] - o0[1] + gy_hit
        off = [round(F[0] - sx_hit, 3), round(F[1] - sy_hit, 3)]
        out.update(stage_at_hit_um=[round(sx_hit, 3), round(sy_hit, 3)], offset_um=off)
        if edge:
            out["why"] = "the beam hit the feature at the edge of the scan: a larger spot.range_um"
        else:
            out["found"] = True
    try:
        from . import render
        out["image"] = render.spot_png(sd / "spot_scan.png", out, F, o0)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"spot scan plot not drawn: {type(exc).__name__}: {exc}")
    return out


def run_spot(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'spot': find where the beam really is, and remember it. With the laser shutter (a frame with the beam
    on), during a Raman acquisition (a frame half-way through it), or by the Raman signal on a small feature
    seen on the video; 'auto' tries the video first and falls back to the Raman scan when a feature is given (or
    found)."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    _progress(sd, state="running", stage="setup", started=t_start,
              estimate_min=plan.estimate(study, prof)["minutes"])
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    laser = su["laser"]
    objective = su["after"].get("objective")
    sp = plan.spot_params(study, prof, objective)
    method = sp["method"]
    attempts: list = []
    found = None
    _progress(sd, stage="finding the beam")
    if method in ("auto", "shutter", "acquisition"):
        use = method if method != "auto" else ("shutter" if prof["motors"].get("laser_shutter") else "acquisition")
        snap = _snapshot(ctx, name, sd, use, use == "shutter", warnings, laser,
                         during_acq_s=float(sp["acq_exposure_s"]) if use == "acquisition" else 0.0,
                         acq_nm=round(shift_to_nm(float(sp["line_cm1"]), laser), 4), remember=False)
        if snap:
            snap.pop("_decoded", None)
        att = {"method": use, "found": bool(snap and snap.get("measured_offset_um")),
               "contrast_sigma": (snap or {}).get("spot_contrast_sigma"), "snapshot": snap}
        attempts.append(att)
        if att["found"]:
            found = {"method": use, "offset_um": snap["measured_offset_um"],
                     "quality": {"contrast_sigma": snap.get("spot_contrast_sigma")}}
    if found is None and (method == "raman" or (method == "auto" and sp.get("feature_um") is not None)):
        rs = raman_spot(ctx, study, sp, laser, objective, sd / "raman", warnings)
        attempts.append({k: v for k, v in rs.items() if k != "snapshot"})
        if rs.get("found"):
            found = {"method": "raman", "offset_um": rs["offset_um"],
                     "quality": {"contrast_sigma": rs.get("contrast_sigma"), "dark": rs.get("dark"),
                                 "step_um": sp["step_um"]}}
    rec = None
    if found:
        if sp.get("remember", True):
            rec = SP.record(prof, objective, laser, found["offset_um"], found["method"], found["quality"],
                            str(ctx.folder))
            if rec.get("warning"):
                warnings.append(rec["warning"])
            if not rec.get("stored"):
                warnings.append(rec.get("why", "not remembered"))
    else:
        unreliable.append("the beam was not found: " + "; ".join(
            a.get("why") or ("no spot on the video" if not a.get("found") else "") for a in attempts if a))
    images = {}
    for a in attempts:
        if a.get("snapshot") and a["snapshot"].get("path"):
            images[f"video_{a['method'].replace(' ', '_')}"] = {"path": a["snapshot"]["path"],
                                                                  "what": a["snapshot"].get("what")}
        if a.get("image"):
            images["spot_scan"] = a["image"]
    result = study_envelope(
        "spot", study, ctx, t_start,
        reliable=found is not None and not unreliable,
        unreliable=unreliable, warnings=warnings, checks=checks,
        instrument={"laser_nm": laser, "objective": objective, "filter": su["after"].get("filter")},
        extra={"spot": {"found": found is not None, "objective": objective, "laser_nm": laser,
                        "offset_um": (found or {}).get("offset_um"), "method": (found or {}).get("method"),
                        "remembered": bool(rec and rec.get("stored")), "drift_um": (rec or {}).get("drift_um"),
                        "previous": (rec or {}).get("previous"), "attempts": attempts}},
    )
    if images:
        result["images"] = images
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


# --------------------------------------------------------------------------------------------- spectral zero, background

def run_calibration(name: str, study: dict, ctx: Ctx) -> dict:
    """calibration.adjust 'labspec': measure; if the lines are off, the operator corrects LabSpec's spectral zero
    (Maintenance > Instrument calibration) as LabSpec shows, presses OK, and ls6t measures again (at most
    calibration.max_retries times). Once LabSpec is right, ls6t's own correction for this laser and grating is
    dropped."""
    from . import xcal
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    attempts, res = [], None
    n_max = 1 + int(prof.get("calibration", {}).get("max_retries", 2))
    params_before = _labspec_params(ctx)
    auto = (study.get("calibration") or {}).get("adjust") == "auto"
    ac = prof.get("calibration", {}).get("autocal") or {}
    autocal_runs = []
    for k in range(1, n_max + 1):
        res = run_study(name, study, ctx, sd / f"attempt{k}")
        cal = res.get("calibration") or {}
        attempts.append({"attempt": k, "pass": cal.get("pass"), "offset_cm1": cal.get("offset_cm1"),
                         "worst_error_cm1": cal.get("worst_error_cm1"), "correction": cal.get("correction"),
                         "labspec_parameters": _labspec_params(ctx)})
        if cal.get("pass") or k == n_max or not cal.get("lines_used"):
            break
        if auto and not autocal_runs and ac.get("enabled") and (ac.get("script") or ac.get("method") == "call"):
            # LabSpec's own AutoCalibration, once; then measure again
            from .api import autocal_call, autocal_question
            inst0 = res.get("instrument") or {}
            txt = (f"ls6t: the calibration of {combo(inst0.get('laser_nm'), inst0.get('grating'))} is off "
                   f"({cal.get('offset_cm1')} cm-1); LabSpec calibrates the grating in use only. "
                   + autocal_question(ac))
            ans = ctx.call("confirm", timeout_s=3600, text=txt)
            if ans.get("answer") == "yes":
                t0 = time.time()
                r = autocal_call(ctx.call, ac)
                autocal_runs.append({"ok": bool(r["ok"]), "error": r.get("error"), "seconds": round(time.time() - t0, 1)})
                if r["ok"]:
                    continue                       # measure again
            else:
                autocal_runs.append({"ok": False, "error": "the operator declined"})
        if not cal.get("trusted", True):
            # no correction from a line measured badly: the operator would move the zero by a wrong amount
            res.setdefault("warnings", []).append("no correction asked: " + "; ".join(cal.get("not_trusted_because", [])))
            break
        laser = (res.get("instrument") or {}).get("laser_nm") or 532.0
        ref, meas = cal["reference_cm1"], cal["measured_cm1"]
        err = meas - ref
        nm = shift_to_nm(ref, laser)
        dnm = -err * nm * nm / 1e7
        inst = res.get("instrument") or {}
        step = ((prof.get("calibration") or {}).get("offset_step_cm1") or {}).get(
            f"{_f(inst.get('grating')) or 0:g}@{float(laser):g}")
        units = (f" On this instrument one step of LabSpec's Offset shift moves the line by about {float(step):+.3f} "
                 f"cm-1 here: about {-err / float(step):+.0f} steps (arrows)." if step else "")
        txt = (f"ls6t: the {'Si' if cal.get('reference') == 'si' else cal.get('reference')} line at {ref} cm-1 is measured at {meas:.2f} cm-1 "
               f"({err:+.2f} cm-1) with {combo(laser, inst.get('grating'))}.\n\nCorrect the spectral zero in "
               f"LabSpec (Maintenance > Instrument calibration) for {combo(laser, inst.get('grating'))} only: the "
               f"line must move by {-err:+.2f} cm-1, about {dnm:+.4f} nm.{units} The other gratings keep their own "
               "zero: check each one you use."
               + (f" With several lines, the coefficient (dispersion) too: scale {cal['correction']['b']:.6f}."
                  if cal.get("correction") and abs(cal["correction"]["b"] - 1) > 1e-5 else "")
               + "\n\nPress OK to measure again, or Cancel to stop.")
        ans = ctx.call("notice", timeout_s=7200, text=txt)
        if ans.get("answer") != "ok":
            break
    result = dict(res or {})
    cal = dict(result.get("calibration") or {})
    cal["attempts"] = attempts
    if auto:
        cal["autocalibration"] = autocal_runs or ("not enabled in the profile (calibration.autocal): guided instead"
                                                  if not ac.get("enabled") else "not needed")
    after = attempts[-1].get("labspec_parameters") if attempts else None
    if params_before and after:
        changed = {k: [params_before.get(k), after.get(k)] for k in after if after.get(k) != params_before.get(k)}
        cal["labspec_parameters"] = {"before": params_before, "after": after, "changed": changed}
    if cal.get("pass") and len(attempts) > 1:
        inst = result.get("instrument") or {}
        n = xcal.clear(inst.get("laser_nm"), inst.get("grating"))
        if n:
            result.setdefault("warnings", []).append(f"LabSpec is calibrated again for {combo(inst.get('laser_nm'), inst.get('grating'))}: ls6t's own correction for it was dropped")
    result["calibration"] = cal
    return _finish(sd, name, result)


def _labspec_params(ctx: Ctx) -> dict | None:
    """LabSpec's calibration parameters for the grating in use (read only), or None when the bridge cannot read
    them."""
    try:
        r = ctx.call("calib_params", timeout_s=60, m_grating=ctx.prof["motors"]["grating"])
    except Stop:
        raise
    except Exception:  # noqa: BLE001
        return None
    if not r.get("ok"):
        return None
    i = r.get("grating_index")
    try:
        i = int(float(i))
    except (TypeError, ValueError):
        return None
    out = {}
    for k, v in r.items():
        if k.startswith(f"g{i}.") and not k.endswith(".error"):
            try:
                out[k[len(f"g{i}."):]] = float(v)
            except (TypeError, ValueError):
                out[k[len(f"g{i}."):]] = v
    return out or None


def objective_laser_note(objective, laser) -> str:
    """A note when the objective is made for another range than the laser (Nicola, 2026-11-25: 785 nm through
    x100_VIS: Si weak and broad, SNR 10, FWHM 1.5 x)."""
    o, las = str(objective or "").upper(), _f(laser)
    if not o or las is None:
        return ""
    if "VIS" in o and las >= 700:
        return (f"; the objective {objective} is made for visible light: at {las:g} nm an NIR objective gives more "
                "signal and a sharper focus")
    if ("NIR" in o or "IR" in o.split("_")[-1]) and las < 600:
        return f"; the objective {objective} is made for the near infrared: at {las:g} nm a VIS objective suits better"
    if "UV" in o and las >= 450:
        return f"; the objective {objective} is made for the UV"
    return ""


def _record_resolution(result: dict, after: dict, prof: dict, warnings: list, request: str):
    """The reference line's width after a calibration: the spectral resolution for this laser, grating and slit,
    compared with its first measurement and with the profile's performance.resolution_cm1, if set."""
    from . import perf
    cal = result["calibration"]
    laser = (result.get("instrument") or {}).get("laser_nm")
    grating = _f(after.get("grating"))
    slit = _f(after.get("slit_um"))
    # per objective and hole too (Nicola, 2026-11-27: 785 Si 5.44 cm-1 at x100, 8.55 at x10 with the 300 um hole; the
    # x10 read '1.52 x its first measurement, alignment?' against an x100 baseline)
    obj_, hole_ = after.get("objective"), _f(after.get("hole_um"))
    k = perf.key(laser, grating) + (f"@s{slit:g}" if slit is not None else "") \
        + (f"@{obj_}" if obj_ not in (None, "") else "") \
        + (f"@h{hole_:g}" if hole_ is not None else "")
    fw = float(cal["fwhm_cm1"])
    rec = perf.record("resolution", k, {"fwhm_cm1": round(fw, 3), "line_cm1": cal.get("reference_cm1"),
                                        "reference": cal.get("reference")}, request)
    base = float(rec["baseline"]["fwhm_cm1"])
    pf = prof.get("performance", {})
    out = {"key": k, "fwhm_cm1": round(fw, 3), "baseline_cm1": base, "measurements": rec["n"]}
    if fw > float(pf.get("resolution_tol", 1.25)) * base:
        out["worse_than_baseline"] = True
        ch_ = result.get("checks") or {}
        laser_bad_ = ch_.get("laser_mode_change_cm1") is not None or (ch_.get("linewidth") or {}).get("multimode")
        warnings.append(f"the {cal.get('reference')} line is {fw:.2f} cm-1 wide, {fw / base:.2f} x its first "
                        f"measurement ({base:.2f}) for {k}: "
                        + ("the laser (a mode change or multimode): wait or power-cycle the laser" if laser_bad_ else
                           "alignment, focus or slit?" + objective_laser_note(after.get("objective"), laser)))
    spec = (pf.get("resolution_cm1") or {}).get(f"{grating:g}" if grating else "")
    if spec is not None:
        out["specification_cm1"] = float(spec)
        if fw > float(spec):
            warnings.append(f"the line width {fw:.2f} cm-1 is above the profile's resolution for {grating:g} gr/mm "
                            f"({float(spec):g} cm-1)")
    cal["resolution"] = out


def _move_seconds(r: dict) -> dict:
    """A move's timing as the bridge reported it (seconds, wait, all motors idle, settle)."""
    return {k: _f(r.get(k)) for k in ("seconds", "wait_s", "all_wait_s", "settle_s") if _f(r.get(k)) is not None}


def run_filters(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'filters': the real transmission of each filter, from one Raman line (Si 520.7 by default) measured
    through each, strongest transmission first (the reference) with the exposure scaled so the counts stay
    alike; transmission = reference % x (signal per second / the reference's). Only filters within the caps."""
    from . import perf
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = ctx.folder / name
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    fp = plan.filters_params(study, prof)
    _progress(sd, state="running", stage="setup", started=t_start, estimate_min=plan.estimate(study, prof)["minutes"])
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    laser = su["laser"]
    all_steps = plan.filters_steps(study, prof, laser)
    steps = [s for s in all_steps if s.get("exposure_s")]
    for s_ in all_steps:
        if not s_.get("exposure_s"):
            warnings.append(f"filter {s_['label']} not measured: {s_.get('skip')}")
    before_filter = su["before"].get("filter")
    nm = round(shift_to_nm(fp["line_cm1"], laser), 4)
    rows = []
    repeat = None

    def measure(i, s, tag):
        r = ctx.call("move", timeout_s=120, motor=prof["motors"]["filter"], mode="label", value=s["label"])
        if not r["ok"] or str(r.get("label")) != s["label"]:
            raise Stop(f"filter {s['label']}: LabSpec did not move there ({r.get('error') or r.get('label')})",
                       "study")
        mv = _move_seconds(r)
        moved_to = r.get("label")
        out = sd / "spectra" / f"filter_{tag}.txt"
        r = ctx.call("acquire", timeout_s=120 + 3 * s["exposure_s"], mode=110, exposure_s=s["exposure_s"],
                     accumulations=1, from_nm=nm, to_nm=nm, out=str(out), sample=f"filter {s['label']}")
        if not r["ok"]:
            raise Stop(f"filter {s['label']}: acquisition failed: {r.get('error')}", "study")
        used = _f(r.get("exposure_s")) or s["exposure_s"]         # LabSpec's own record (Ctx checks it)
        sp = spectra.read(out)
        x, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        x, y = spectra.sort_xy(x, sp["y"])
        sat = spectra.saturation(y, prof["detector"]["full_scale"], prof["checks"]["saturation_frac"])
        sig = spectra.line_signal(x, y, fp["line_cm1"], fp["window_cm1"]) / used
        noise = spectra.noise_sigma(y) / used
        return {**s, "exposure_used_s": used, "signal_cps": round(float(sig), 3),
                "snr": round(float(sig / max(noise, 1e-9)), 1), "max_counts": sat["max_counts"],
                "saturated": sat["saturated"], "filter_read_back": moved_to, "move": mv,
                "file": str(out)}

    try:
        for i, s in enumerate(steps):
            _progress(sd, stage=f"filter {s['label']}", point=i, points=len(steps))
            rows.append(measure(i, s, f"{i:02d}"))
        if len(rows) >= 2:
            # the reference again at the end: a change means drift (focus, laser power), not the filters
            # (2026-11-23: the second step read about 3 x low; this tells drift from the filter)
            repeat = measure(len(steps), steps[0], "ref_again")
    finally:
        if before_filter:
            try:
                ctx.call("move", timeout_s=120, motor=prof["motors"]["filter"], mode="label", value=before_filter)
            except Stop:
                pass
    tol = float(prof.get("performance", {}).get("filters_tol", 0.3))
    ref = rows[0] if rows else None
    if ref and ref["saturated"]:
        unreliable.append(f"the reference filter {ref['label']} is saturated: a shorter filters.exposure_s")
    for r_ in rows[1:]:
        if r_["saturated"]:
            unreliable.append(f"filter {r_['label']} is saturated ({r_['max_counts']:.0f} counts): its exposure is "
                              "scaled up from the reference's; a shorter filters.exposure_s")
    drift = None
    if ref and repeat and ref["signal_cps"] > 0:
        drift = round(repeat["signal_cps"] / ref["signal_cps"], 3)
        if abs(drift - 1.0) > 0.1:
            unreliable.append(f"the reference filter {ref['label']} read {drift:.2f} x its first value at the end: "
                              "the signal drifted during the study (focus, laser power, sample), so the "
                              "transmissions are uncertain; refocus and measure again")
    if ref and ref["signal_cps"] <= 0:
        unreliable.append("no line through the reference filter: check the sample and its focus")
    flagged = []
    for r_ in rows:
        if ref and ref["signal_cps"] > 0:
            r_["measured_pct"] = round(ref["nominal_pct"] * r_["signal_cps"] / ref["signal_cps"], 5)
            r_["ratio"] = round(r_["measured_pct"] / r_["nominal_pct"], 3)
            if r_ is not ref and abs(r_["ratio"] - 1.0) > tol:
                flagged.append(f"{r_['label']}: measured {r_['measured_pct']:.4g} % ({r_['ratio']:.2f} x nominal)")
            if r_ is not ref and r_["snr"] < prof["checks"].get("detect_snr", 5.0):
                warnings.append(f"filter {r_['label']}: weak line (SNR {r_['snr']}): its value is uncertain")
    if flagged:
        warnings.append("filters off their nominal transmission by more than performance.filters_tol: "
                        + "; ".join(flagged) + " (relative to " + (ref["label"] if ref else "?") + ")")
    measured = {r_["label"]: r_.get("measured_pct") for r_ in rows if r_.get("measured_pct") is not None}
    rec = None
    if measured and not unreliable:
        rec = perf.record("filters", perf.key(laser), {"reference": ref["label"], "measured_pct": measured,
                                                        "line_cm1": fp["line_cm1"]}, str(ctx.folder))
    # the proposal from several runs (2026-11-23: 1 % read 1.176, 0.971 and 1.260 % in three good runs, about the
    # reference's own drift): the mean of the last runs with the same reference, offered once there are enough
    proposal, avg = None, None
    if measured and not unreliable:
        hist = [e.get("measured_pct") or {} for e in ((perf.get("filters", perf.key(laser)) or {}).get("history") or [])
                if e.get("reference") == ref["label"]][-5:]
        avg = {}
        for lab in measured:
            vals = [float(h[lab]) for h in hist if h.get(lab) is not None]
            if vals:
                m_ = sum(vals) / len(vals)
                sd_ = (sum((v - m_) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else None
                avg[lab] = {"mean_pct": round(m_, 4), "runs": len(vals),
                            **({"std_pct": round(sd_, 4)} if sd_ is not None else {})}
        need = int((prof.get("performance") or {}).get("filters_min_runs", 3))
        runs = min((a["runs"] for a in avg.values()), default=0)
        if runs >= need:
            new = dict(prof.get("filters", {}))
            new.update({k: a["mean_pct"] for k, a in avg.items()})
            proposal = ("ls6t profile set filters '" + json.dumps(new) + "'")
    if repeat:
        checks["reference_repeat"] = {"signal_cps": repeat["signal_cps"], "ratio_to_first": drift,
                                      "file": repeat["file"]}
    result = study_envelope(
        "filters", study, ctx, t_start,
        ok=bool(rows), complete=len(rows) == len(steps), reliable=bool(rows) and not unreliable,
        unreliable=unreliable, warnings=warnings, checks=checks,
        instrument={"laser_nm": laser, "objective": su["after"].get("objective"),
                    "grating": _f(su["after"].get("grating"))},
        extra={"filters": {"reference": ref["label"] if ref else None, "line_cm1": fp["line_cm1"], "steps": rows,
                           "flagged": flagged, "measurements": rec["n"] if rec else 0,
                           "profile_command": proposal, "average": avg,
                           "skipped": [{"label": s_["label"], "why": s_.get("skip")} for s_ in
                                       plan.filters_steps(study, prof, laser) if not s_.get("exposure_s")],
                           **({} if proposal or not avg else
                              {"profile_command_note": f"offered from {int((prof.get('performance') or {}).get('filters_min_runs', 3))} "
                                                      "runs with the same reference (the mean): repeat the study"}),
                           "note": "relative to the reference filter (the strongest allowed); the profile's caps "
                                   "use the filters' values: a person decides whether to set the measured ones "
                                   "(profile_command)"}},
    )
    _progress(sd, state="done", stage="done")
    return _finish(sd, name, result)


def run_cutoff(name: str, study: dict, ctx: Ctx) -> dict:
    """Kind 'cutoff': where the edge filter lets light through, from a spectrum across the laser line: with the
    white light (laser off: the shutter, or the operator blocks it) or the sample's own background with the laser.
    Transmission = (signal - dark) / (plateau - dark), the plateau taken over cutoff.plateau_cm1; reports the shifts
    at 10, 50 and 90 % (usable_from_cm1), remembered per laser and grating (later ranges below it are warned)."""
    from . import perf
    prof = ctx.prof
    sd = ctx.folder / name
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    cp = plan.cutoff_params(study, prof)
    white = cp["light"] == "white"
    _progress(sd, state="running", stage="setup", started=t_start)
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1, confirm=not white)
    laser, rng = su["laser"], su["rng"]
    if white:
        txt = ("ls6t: the edge filter's cut-off, with the white light.\n\nTurn the white light on"
               + (".\n\n" + BLOCK_BY_HAND if cp["laser_off"] == "operator" else " (ls6t keeps the laser shutter closed)")
               + "\n\nPress OK when ready, or Cancel.")
        if ctx.call("notice", timeout_s=3600, text=txt).get("answer") != "ok":
            raise Stop("the operator cancelled", "study")
    acq = plan.acquire_step(study, prof, rng)
    acq.pop("verb", None)
    out = sd / "spectra" / "cutoff.txt"
    params = dict(acq, out=str(out), sample="edge filter cut-off")
    if white and cp["laser_off"] == "shutter":
        _shutter_gate(prof, laser)
        params.update(laser_off=1, m_laser_shutter=prof["motors"]["laser_shutter"], **shutter_args(prof))
    r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
    if white and cp["laser_off"] == "operator":
        ctx.call("notice", timeout_s=3600, text="ls6t: done. Unblock the laser when you want to measure again. OK.")
    if not r["ok"]:
        raise Stop(f"cut-off acquisition failed: {r.get('error')}", "study")
    grating_guard(prof, study, su.get("after"), laser, rng, out, checks, warnings, r)
    sp = spectra.read(out)
    x, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
    x, y = spectra.sort_xy(x, sp["y"])
    k = max(3, int(len(y) // 200) | 1)
    ys = np.convolve(np.pad(y, k // 2, mode="edge"), np.ones(k) / k, mode="valid")[:len(y)]
    lo, hi = cp["plateau_cm1"]
    sel = (x >= lo) & (x <= hi)
    if not white:
        sel &= np.abs(x - 520.7) > 30
    res: dict = {"light": cp["light"], "range_cm1": [float(x.min()), float(x.max())]}
    if sel.sum() < 5:
        unreliable.append(f"the plateau {lo:g}-{hi:g} cm-1 is not inside the range: widen cutoff.to_cm1")
    else:
        plateau = float(np.median(ys[sel]))
        dark = float(np.percentile(ys[x < lo], 5)) if (x < lo).any() else float(ys.min())
        res.update(plateau=round(plateau, 2), dark=round(dark, 2))
        if plateau - dark < 10 * max(spectra.noise_sigma(y), 1.0):
            unreliable.append("no clear step: too little light above the cut-off (more white light, a longer "
                              "exposure, or light 'white')")
        else:
            tr = (ys - dark) / (plateau - dark)
            idx = np.where(x < lo)[0]

            def crossing(level):
                # from the plateau down towards the laser: the first point under the level
                for j in sorted(idx, key=lambda j: -x[j]):
                    if tr[j] < level:
                        j2 = j + 1 if j + 1 < len(x) else j
                        if tr[j2] != tr[j]:
                            return float(x[j] + (level - tr[j]) * (x[j2] - x[j]) / (tr[j2] - tr[j]))
                        return float(x[j])
                return None
            for lvl, key_ in ((0.1, "edge_10_cm1"), (0.5, "edge_50_cm1"), (0.9, "usable_from_cm1")):
                v = crossing(lvl)
                res[key_] = round(v, 1) if v is not None else None
            if res.get("usable_from_cm1") is None:
                unreliable.append("the transmission never drops below 90 % in the range: start lower (cutoff.from_cm1)")
    if res.get("usable_from_cm1") is not None and not unreliable:
        grating = _f(su["after"].get("grating"))
        rec = perf.record("cutoff", perf.key(laser, grating), {k_: res.get(k_) for k_ in
                                                                ("edge_10_cm1", "edge_50_cm1", "usable_from_cm1")}
                          | {"light": cp["light"]}, str(ctx.folder))
        res["measurements"] = rec["n"]
        res["baseline_usable_from_cm1"] = rec["baseline"].get("usable_from_cm1")
    result = study_envelope(
        "cutoff", study, ctx, t_start,
        reliable=not unreliable, unreliable=unreliable, warnings=warnings, checks=checks,
        instrument={"laser_nm": laser, "grating": _f(su["after"].get("grating"))},
        extra={"cutoff": res, "spectra": [{"file": str(out)}]},
    )
    try:
        from . import render
        result["images"] = {"cutoff": render.cutoff_png(sd / "cutoff.png", x, y, res)}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cut-off plot not drawn: {type(exc).__name__}: {exc}")
    return _finish(sd, name, result)


def run_background(name: str, study: dict, ctx: Ctx) -> dict:
    """A white-light background: the white light on (the operator), the laser off (the shutter closed by the bridge,
    or blocked by the operator), one acquisition with the given settings; stored by name as counts per second per
    accumulation for later subtraction."""
    from . import background as BG
    prof = ctx.prof
    sd = ctx.folder / name
    (sd / "spectra").mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    bgk = study.get("background") or {}
    bname = bgk["name"]
    off = bgk.get("laser_off", SH.default_laser_off(prof, (study.get("setup") or {}).get("laser_nm")))
    _progress(sd, state="running", stage="setup", started=t_start)
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1, confirm=False)
    laser, rng = su["laser"], su["rng"]
    txt = (f"ls6t: white-light background '{bname}'.\n\nTurn the white light on, as it will be during the "
           "measurements" + (".\n\n" + BLOCK_BY_HAND if off == "operator" else " (ls6t keeps the laser shutter closed)")
           + "\n\nPress OK when ready, or Cancel.")
    if ctx.call("notice", timeout_s=3600, text=txt).get("answer") != "ok":
        raise Stop("the operator cancelled the background", "study")
    acq = plan.acquire_step(study, prof, rng)
    acq.pop("verb", None)
    out = sd / "spectra" / "background.txt"
    params = dict(acq, out=str(out), sample=f"white-light background {bname}",
                  native=str(sd / "spectra" / "background.l6s") if study.get("save_native", True) else None)
    if off == "shutter":
        _shutter_gate(prof, laser)
        params.update(laser_off=1, m_laser_shutter=prof["motors"]["laser_shutter"], **shutter_args(prof))
    r = ctx.call("acquire", timeout_s=_acq_timeout(study, prof, rng), **params)
    if off == "operator":
        ctx.call("notice", timeout_s=3600, text="ls6t: the background is taken. Unblock the laser when you want "
                                                 "to measure again. OK.")
    if not r["ok"]:
        raise Stop(f"background acquisition failed: {r.get('error')}", "study")
    grating_guard(prof, study, su.get("after"), laser, rng, out, checks, warnings, r)
    sp = spectra.read(out)
    ex = _f(r.get("exposure_s")) or float(acq.get("exposure_s") or 1.0)
    per_s = sp["meta"].get("y_unit", "cnt").lower().startswith("cnt/s")
    cps = sp["y"] if per_s else sp["y"] / ex
    sat = spectra.saturation(sp["y"] if not per_s else sp["y"] * ex, prof["detector"]["full_scale"],
                             prof["checks"]["saturation_frac"])
    if sat["saturated"]:
        unreliable.append("the background is saturated: a shorter exposure, or less white light")
    # narrow lines in a background: the laser not blocked? Cosmic rays first out (a 5-point median: one to three
    # pixels): on 2026-11-23 two good backgrounds (8 counts at most, no Si line) were rejected for spikes at 415 /
    # 516 / 737 and 269 / 358 / 558 cm-1, in other places each time. Then a third had 'lines' of a few counts at 480 /
    # 594 / 661: noise. A line counts only above checks.background_line_min_counts (20), and is warned about only
    # when it recurs (within 5 cm-1) in another background with this laser: real lamp or room-light lines repeat
    narrow_info = None
    try:
        from scipy.signal import medfilt
        shift, _ = spectra.to_shift(sp["x"], sp["meta"].get("x_unit", "nm"), laser)
        xs, ysr = spectra.sort_xy(shift, sp["y"])
        if rng.get("crop_cm1"):
            # a range acquired centred in one window: only the range asked (the window's edge-filter side is not)
            lo_c, hi_c = sorted(float(v) for v in rng["crop_cm1"])
            m_ = (xs >= lo_c) & (xs <= hi_c)
            if m_.sum() >= 10:
                xs, ysr = xs[m_], ysr[m_]
        if per_s:
            ysr = ysr * ex                                      # in counts, as the detector saw them
        sig_raw = spectra.noise_sigma(ysr - spectra.baseline(xs, ysr, 3))     # the noise from the spectrum as taken
        ysr = medfilt(ysr, 5) if len(ysr) >= 5 else ysr
        yc = ysr - spectra.baseline(xs, ysr, 3)
        found = spectra.find_peaks(xs, yc, 5, sig_raw or None, 25.0)
        min_counts = float(prof["checks"].get("background_line_min_counts", 20.0))
        lines = [{"cm1": round(v, 1), "counts": round(float(np.interp(v, xs, yc)), 1)} for v in found]
        strong = [ln for ln in lines if ln["counts"] >= min_counts]
        hist = store.read("background_lines.json")
        key = SH._key(laser)
        prev = (hist.get(key) or [])[-10:]
        recurring = [ln for ln in strong if any(abs(ln["cm1"] - q) <= 5.0 for e in prev for q in e.get("lines", []))]
        hist.setdefault(key, []).append({"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "background": bname,
                                         "lines": [ln["cm1"] for ln in strong]})
        hist[key] = hist[key][-20:]
        store.write("background_lines.json", hist)
        narrow_info = {"min_counts": min_counts, "found": lines, "above_min": strong,
                       "recurring": [ln["cm1"] for ln in recurring], "backgrounds_compared": len(prev)}
        narrow = [ln["cm1"] for ln in recurring]
        verified = SH.records().get(SH._key(laser)) if off == "shutter" else None
        if narrow and off == "shutter":
            # the shutter has been shown to block this laser on Si (shutter.py), so these are the lamp or the room's
            # light more likely than the laser; said, kept
            warnings.append(f"the background has lines at {', '.join(f'{v:.0f}' for v in narrow)} cm-1 that recur in "
                            "earlier backgrounds (after spike removal, above "
                            f"{min_counts:g} counts) with the laser shutter closed: the white lamp or room light? The "
                            f"shutter was shown to block this laser on Si on {(verified or {}).get('date', '?')}; if these "
                            "are the sample's Raman lines, run ls6t instrument shutter-verify again")
        elif strong and off != "shutter":
            warnings.append(f"the background has narrow lines at {', '.join(str(round(ln['cm1'])) for ln in strong)} "
                            "cm-1: was the laser blocked? (a Raman line in the background would be subtracted from "
                            "samples)")
    except ValueError:
        pass
    meta = {"name": bname, "laser_nm": laser, "grating": _f(su["after"].get("grating")),
            "objective": su["after"].get("objective"), "exposure_s": ex,
            "accumulations": int(_f(r.get("accumulations")) or 1), "range": rng, "request": str(ctx.folder),
            "laser_off": off}
    shut = shutter_report(r, warnings, verified=SH.gate(prof, laser) is None) if off == "shutter" else {}
    blocked_doubt = any("NOT blocked" in u for u in unreliable)
    stored = (BG.save(bname, sp["x"], cps, sp["meta"].get("x_unit", "nm"), meta) if not blocked_doubt else
              {"name": bname, "stored": False})
    result = study_envelope(
        "background", study, ctx, t_start,
        reliable=not unreliable, unreliable=unreliable, warnings=warnings, checks=checks,
        instrument={"laser_nm": laser, "grating": meta["grating"], "objective": meta["objective"]},
        extra={"background": {**stored, "max_cps": round(float(np.max(cps)), 3), "mean_cps": round(float(np.mean(cps)), 3)},
               "spectra": [{"file": str(out), "exposure_s": ex, "max_counts": sat["max_counts"]}],
               **({"laser_shutter": shut} if shut else {}),
               **({"narrow_lines": narrow_info} if narrow_info else {})},
    )
    result["seconds"].update(
        # the acquisition alone (the bridge's call): two backgrounds in a row after a grating
        # change show whether the turret moves only then (TESTING T3.16)
        acquire=r.get("seconds"),
        setup_moves=[{"motor": m["motor"], "seconds": m.get("seconds")} for m in su.get("readback", [])],
    )
    return _finish(sd, name, result)



def run_guide(name: str, study: dict, ctx: Ctx, sd: Path | None = None) -> dict:
    """The operator adjusts something by hand; the bridge measures the line every fraction of a second, shows it in
    LabSpec and says STOP when the goal is met. The fast loop runs in the bridge: no round trip through Python or an
    LLM between readings."""
    from .profile import shift_to_nm
    prof = ctx.prof
    sd = sd or ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    _progress(sd, state="running", stage="setup", point=0, points=1, started=t_start,
              estimate_min=plan.estimate(study, prof)["minutes"])
    warnings: list[str] = []
    unreliable: list[str] = []
    checks: dict = {}
    su = _setup(name, study, ctx, warnings, unreliable, checks, 1)
    laser = su["laser"]
    g = plan.guide_params(study, prof)
    line, win = float(g["line_cm1"]), float(g["window_cm1"])
    center_nm = round(shift_to_nm(float(g.get("center_cm1", line)), laser), 4)
    sat = float(prof["guide"].get("saturation_frac", 0.9)) * prof["detector"]["full_scale"]
    _progress(sd, stage="guiding", eta_min=round(float(g["max_s"]) / 60.0, 2))
    r = ctx.call("guide", timeout_s=float(g["max_s"]) + 300, text=g["instruction"], mode=g["mode"],
                 target=g.get("target", 0), from_nm=center_nm, to_nm=center_nm,
                 lo_nm=round(shift_to_nm(line - win / 2, laser), 4), hi_nm=round(shift_to_nm(line + win / 2, laser), 4),
                 lo_cm1=line - win / 2, hi_cm1=line + win / 2, exposure_s=g["exposure_s"], max_s=g["max_s"],
                 stable_n=g["stable_n"], drop_frac=g["drop_frac"], sat=round(sat, 1),
                 out=str(sd / "guide_history.txt"), ui=prof["guide"].get("ui", "dialog"), unit="counts/s")
    hist = []
    try:
        from .profile import read_text
        for line_ in read_text(sd / "guide_history.txt").splitlines():
            if line_.startswith("#") or not line_.strip():
                continue
            t_, v_, y_ = (float(x) for x in line_.split(",")[:3])
            hist.append([t_, v_, y_])
    except (OSError, ValueError):
        pass
    reason = r.get("reason", "")
    good = reason in ("target", "past_max", "operator_done")
    if reason == "saturation":
        unreliable.append("the detector came near saturation: reduce the power, or a stronger filter")
    elif reason == "timeout":
        unreliable.append(f"the goal was not met within {g['max_s']:g} s")
    elif reason in ("operator_abort", "stopped"):
        unreliable.append("the operator or ls6t stopped the adjustment")
    elif not r["ok"] and not reason:
        unreliable.append(f"the guided loop failed: {r.get('error')}")
    if r.get("ui_fallback"):
        warnings.append(f"LabSpec's dialog was not available ({r['ui_fallback']}): the status bar was used")
    guide = {"instruction": g["instruction"], "mode": g["mode"], "target": g.get("target"), "line_cm1": line,
             "reason": reason, "message": r.get("message"), "value": _f(r.get("value")),
             "max_value": _f(r.get("max_value")), "t_max_s": _f(r.get("t_max_s")),
             "iterations": int(_f(r.get("iterations")) or 0), "period_s": _f(r.get("period_s")),
             "unit": "counts/s (line height above its window's edges, per second)",
             "history": str(sd / "guide_history.txt"), "points": len(hist)}
    result = study_envelope(
        "guide", study, ctx, t_start,
        ok=guide["iterations"] > 0, complete=good, reliable=good and not unreliable,
        unreliable=unreliable, warnings=warnings, checks=checks,
        setup={"requested": study.get("setup", {}), "before": su["before"], "after": su["after"],
               "moves": su["readback"]},
        instrument={"laser_nm": laser, "detector_temp_c": su["temp"], "filter": su["after"].get("filter"),
                    "objective": su["after"].get("objective")},
        extra={"guide": guide},
    )
    if hist:
        try:
            from . import render
            result["images"] = {"guide": render.guide_png(sd / "guide.png", hist, guide, name)}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"guide plot not drawn: {type(exc).__name__}: {exc}")
    result = _finish(sd, name, result)
    _progress(sd, state="done", stage="done")
    if r.get("stopped_by") in ("operator", "request") or reason == "stopped":
        raise Stop("stopped by the operator in LabSpec" if r.get("stopped_by") == "operator" else "cancelled",
                   "operator" if r.get("stopped_by") == "operator" else "cancel")
    return result


# --------------------------------------------------------------------------------------------- alignment

CHECKLIST = ("ls6t: the alignment check failed for the {laser:g} nm laser ({ratio}).\n\n"
             "Please run AutoAlignment now: LabSpec > Maintenance tab > AutoAlignment > Start, and follow its "
             "messages. Its checklist: the HORIBA reference Si sample; the laser at full power at the source; the "
             "x100 objective; the laser focused on the Si; no dust or oil on the sample or objective.\n\n"
             "Press OK here when AutoAlignment has finished (ls6t then checks again), or Cancel to stop.")


def alignment_metrics(res: dict, prof: dict, laser: float | None) -> dict:
    """From a Si check's result: counts/s of the Si line, its width and position, compared with the laser's
    baseline. pass is None when there is no baseline to compare with."""
    from .profile import laser_key
    a = prof.get("alignment", {})
    lk = laser_key(prof, laser) if laser else None
    pk = (res.get("peaks_by_name") or {}).get("Si") or {}
    sp = (res.get("spectra") or [{}])[0]
    ex = _f(sp.get("exposure_s")) or _f((res.get("setup", {}).get("requested", {}) or {}).get("exposure_s"))
    m = {"laser_nm": laser, "laser_key": lk, "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "filter": (res.get("instrument") or {}).get("filter"), "position_cm1": pk.get("position_cm1"),
         "fwhm_cm1": pk.get("fwhm_cm1"), "height": pk.get("height"), "exposure_s": ex,
         "counts_per_s": round(pk["height"] / ex, 2) if pk.get("ok") and ex else None,
         "measured": bool(pk.get("ok")), "reliable": bool(res.get("reliable"))}
    base = a.get("baselines", {}).get(lk or "")
    m["baseline"] = base
    if not m["measured"]:
        m.update(passed=False, why="no Si line: check the sample is the Si reference and in focus")
        return m
    if not base:
        m.update(passed=None, why="no baseline for this laser: the maintainer records one after AutoAlignment "
                                  "(`ls6t alignment record`)")
        return m
    cps = m["counts_per_s"]
    from .profile import filter_pct
    f_now, f_base = prof.get("filters", {}).get(m["filter"]), prof.get("filters", {}).get(base.get("filter"))
    f_now = f_now if f_now is not None else filter_pct(m["filter"] or "")
    f_base = f_base if f_base is not None else filter_pct(base.get("filter") or "")
    if f_now and f_base and abs(f_now - f_base) > 1e-9:
        cps = cps * f_base / f_now
        m["scaled_to_baseline_filter"] = base.get("filter")
    ratio = cps / base["counts_per_s"] if base.get("counts_per_s") else None
    wratio = m["fwhm_cm1"] / base["fwhm_cm1"] if base.get("fwhm_cm1") and m["fwhm_cm1"] else None
    ok = ratio is not None and ratio >= float(a.get("min_ratio", 0.7)) and \
        (wratio is None or wratio <= float(a.get("max_fwhm_ratio", 1.3)))
    m.update(ratio=round(ratio, 3) if ratio is not None else None,
             fwhm_ratio=round(wratio, 3) if wratio is not None else None, passed=bool(ok),
             why=None if ok else (f"Si signal at {ratio:.0%} of the baseline (minimum {a.get('min_ratio', 0.7):.0%})"
                                  if ratio is not None and ratio < float(a.get("min_ratio", 0.7)) else
                                  f"Si line {wratio:.2f}x wider than the baseline"))
    return m


def log_alignment(entry: dict):
    p = config.alignment_log()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(clean(entry), default=_jsonable) + "\n")


def alignment_history(laser_key_: str | None = None) -> list:
    out = []
    try:
        for line in config.alignment_log().read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if laser_key_ is None or e.get("laser_key") == laser_key_:
                out.append(e)
    except OSError:
        pass
    return out


def readiness_warnings(prof: dict, laser: float | None, warnings: list):
    """Once the maintainer has recorded alignment baselines: warn when this laser's last check failed, is missing,
    or is older than alignment.max_age_h."""
    from .profile import laser_key
    a = prof.get("alignment", {})
    if not a.get("baselines") or not laser:
        return
    lk = laser_key(prof, laser)
    hist = alignment_history(lk)
    if not hist:
        warnings.append(f"the {laser:g} nm laser has no alignment check on record: run an 'alignment' study first")
        return
    last = hist[-1]
    try:
        age_h = (time.time() - time.mktime(time.strptime(last["time"], "%Y-%m-%dT%H:%M:%S"))) / 3600.0
    except (KeyError, ValueError):
        age_h = None
    if last.get("passed") is False:
        warnings.append(f"the last alignment check of the {laser:g} nm laser failed ({last.get('time')}): "
                        "results may be weak; run AutoAlignment and an 'alignment' study")
    elif age_h is not None and age_h > float(a.get("max_age_h", 24)):
        warnings.append(f"the last alignment check of the {laser:g} nm laser is {age_h:.0f} h old "
                        f"(more than {a.get('max_age_h', 24):g} h): run an 'alignment' study")


def run_alignment(name: str, study: dict, ctx: Ctx) -> dict:
    """The check (Si reference, the laser's fixed settings) against the baseline; if it fails and guided, the
    operator runs AutoAlignment in LabSpec (checklist; OK when done) and the check repeats; then a video snapshot
    (with the laser when the profile names the shutter) measures the beam's offset, which AutoAlignment changes."""
    prof = ctx.prof
    sd = ctx.folder / name
    sd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    a = study.get("alignment", {})
    guided = a.get("guided", True)
    warnings: list[str] = []
    chk = plan.alignment_check_study(study, prof)
    attempts, last, res = [], None, None
    focus = None
    if a.get("focus_guide"):
        gst = {"kind": "guide", "sample": chk["sample"], "setup": chk["setup"],
               "guide": {"instruction": "Focus on the reference Si: turn the focus slowly through the best position",
                         "mode": "maximize", "exposure_s": 0.2, "max_s": 120}}
        try:
            focus = run_guide(f"{name}_focus", gst, ctx, sd / "focus")
        except Stop:
            raise
        if focus.get("guide", {}).get("reason") not in ("past_max", "operator_done", "target"):
            warnings.append(f"the focus guide ended with '{focus.get('guide', {}).get('reason')}'")
    n_max = 1 + (int(prof["alignment"].get("max_retries", 1)) if guided else 0)
    for attempt in range(1, n_max + 1):
        res = run_study(f"check{attempt}", chk, ctx, sd / f"check{attempt}")
        m = alignment_metrics(res, prof, (res.get("instrument") or {}).get("laser_nm"))
        m["attempt"] = attempt
        attempts.append(m)
        last = m
        if m["passed"] is not False or not guided or attempt == n_max:
            break
        txt = CHECKLIST.format(laser=m["laser_nm"] or 0, ratio=m.get("why") or "")
        ans = ctx.call("notice", timeout_s=7200, text=txt)
        if ans.get("answer") != "ok":
            warnings.append("the operator cancelled AutoAlignment: the check was not repeated")
            break
    for m in attempts:
        log_alignment({**{k: v for k, v in m.items() if k != "baseline"}, "request": str(ctx.folder), "study": name})
    # the beam's offset after (Auto)Alignment
    snap = None
    if res and res.get("ok"):
        snap = _snapshot(ctx, name, sd, "after", bool(prof["motors"].get("laser_shutter")), warnings,
                         (res.get("instrument") or {}).get("laser_nm"))
    passed = last["passed"] if last else False
    rec = None
    if passed is False:
        rec = ("run AutoAlignment for this laser (LabSpec > Maintenance > AutoAlignment); if it fails twice, contact "
               "HORIBA service")
    elif passed is None:
        rec = "record a baseline right after a successful AutoAlignment: ls6t alignment record <request>"
    else:
        rec = "aligned; calibrate next (a 'calibration' study) if AutoAlignment ran"
    if snap and snap.get("measured_offset_um"):
        off = prof.get("video", {}).get("laser_offset_um", [0, 0])
        mo = snap["measured_offset_um"]
        if max(abs(mo[0] - off[0]), abs(mo[1] - off[1])) > 0.5:
            warnings.append(f"the beam is {mo} µm from LabSpec's laser position, the profile says {off}: the "
                            "maintainer updates it (ls6t profile set video.laser_offset_um \"[x, y]\")")
    result = dict(res or {})
    result.update(kind="alignment", ok=bool(res and res.get("ok")),
                  reliable=bool(res and res.get("reliable")) and passed is not False,
                  unreliable_because=list((res or {}).get("unreliable_because", [])) +
                  ([last["why"]] if passed is False and last and last.get("why") else []),
                  warnings=list((res or {}).get("warnings", [])) + warnings,
                  alignment={"passed": passed, "attempts": attempts, "recommendation": rec,
                             "guided": guided, "focus": (focus or {}).get("guide")},
                  seconds={"total": round(time.time() - t_start, 2)})
    if snap:
        result["video"] = {"after": snap}
        if snap.get("path"):
            result.setdefault("images", {})["video_after"] = {"path": snap["path"], "what": snap.get("what")}
    if focus and (focus.get("images") or {}).get("guide"):
        result.setdefault("images", {})["focus_guide"] = focus["images"]["guide"]
    return _finish(sd, name, result)



def _native_map(ctx: Ctx, name: str, study: dict, sd: Path, rng: dict, laser: float, acq: dict, x0, y0, zref,
                warnings: list) -> list:
    """LabSpec's own mapping engine: the axes as stage positions (or offsets, profile mapping.positions), one
    detector window at the range's centre, exposure and accumulations in LabSpec's interface, SWIFT if asked;
    the map comes back as one data object, split here into per-point records (with their grid indices)."""
    prof = ctx.prof
    mp = prof.get("mapping", {})
    m = study.get("map", {})
    absolute = mp.get("positions", "absolute") == "absolute"
    params: dict = {}
    refs = {"x": x0, "y": y0, "z": zref}
    for ax in ("x", "y", "z"):
        if not m.get(f"{ax}_um"):
            continue
        a, b, st = (float(v) for v in m[f"{ax}_um"])
        if refs[ax] is None and absolute:
            raise Stop(f"the {ax.upper()} position could not be read: a native map needs it", "study")
        base = refs[ax] if absolute else 0.0
        for v in (a, b):
            _check_abs(prof, ax, (refs[ax] or 0.0) + v, f"native map {ax}")
        params.update({f"{ax}_from": round(base + a, 3), f"{ax}_to": round(base + b, 3), f"{ax}_step": st})
    flags = int(acq.get("mode", 10)) - 10
    n = len(plan.positions(study))
    per_s = float(acq.get("exposure_s") or 1.0) * int(acq.get("accumulations", 1)) + \
        float(mp.get("native_point_s", 0.05))
    _progress(sd, stage="LabSpec is mapping", point=0, points=n, eta_min=round(n * per_s / 60.0, 2))
    appr = float((prof.get("spectrometer") or {}).get("approach_nm", 0) or 0)
    if appr:
        params["spectro_approach_nm"] = appr
    r = ctx.call("map_native", timeout_s=600 + 3 * n * per_s, out=str(sd / name), center_nm=rng.get("from_nm"),
                 from_nm=rng.get("from_nm"), to_nm=rng.get("to_nm"), exposure_s=acq.get("exposure_s"),
                 accumulations=acq.get("accumulations", 1), mode_flags=flags, swift=bool(m.get("swift")),
                 acq_time=mp.get("acq_time", "interface"), chunk=int(mp.get("export_chunk", 65536) or 0),
                 **params, **plan.band_params(study, laser), **motor_args(prof))
    _band_said(ctx, r, "band", warnings)
    # what LabSpec took for each axis (GetMappingParams after setting them), how it was started, how long it took
    ctx.native_info = {k: r[k] for k in r if str(k).endswith("_readback")}
    ctx.native_info.update({k: _f(r.get(k)) if k != "axes" else r.get(k)
                            for k in ("acq_mode", "seconds_map", "axes") if r.get(k) not in (None, "")})
    # did LabSpec's map move the spectrometer from where ls6t put it (with its approach)? Then its windows are
    # LabSpec-placed for the zero's repeatability; otherwise ls6t's approach-based entry applies (Nicola, 2026-11-25)
    sp0 = _f(r.get("spectro_before_map"))
    later = [_f(r.get(k)) for k in ("spectro_map_started", "spectro_after_map")]
    later = [v for v in later if v is not None and SPECTRO_NM_PLAUSIBLE[0] <= v <= SPECTRO_NM_PLAUSIBLE[1]]
    if sp0 is not None and later:
        moved_ls = any(abs(v - sp0) > 0.01 for v in later)
        ctx.native_info["spectrometer"] = {"before_map_nm": sp0, "read_during_after_nm": later,
                                           "moved_by_labspec": moved_ls,
                                           "approached_from_nm": _f(r.get("spectro_approached_from"))}
    # the hold as LabSpec read it at the end of the map: HORIBA's mapping engine may set the shutter mode itself
    ha = r.get("hold_after_map_held")
    if ha not in (None, "") and ctx.holding:
        after = {"held": ha == "yes", "shutter_mode": r.get("hold_after_map_shutter_mode"),
                 "shutter_label": r.get("hold_after_map_shutter_label"), "read": "from LabSpec, after the map"}
        ctx.hold_log.append({"after_native_map": after})
        if ha != "yes":
            ctx.keep_hold = False          # put back as it was at the study's end, held again by the next study
            warnings.append(f"the shutter hold was not in place at the end of LabSpec's map (ShutterMode "
                            f"{after['shutter_mode']}, shutter '{after['shutter_label']}'): LabSpec's mapping engine "
                            "set the shutter itself, so the laser may have been cycled between points (a mode hop on "
                            "the 637.75 nm laser): see the laser line, or map with map.engine 'ls6t'")
    if not r["ok"]:
        if r.get("stopped_by"):
            raise Stop("stopped by the operator in LabSpec" if r.get("stopped_by") == "operator" else "cancelled",
                       "operator" if r.get("stopped_by") == "operator" else "cancel")
        raise Stop(f"native map failed: {r.get('error')}", "study")
    if str(r.get("native_ok")) != "1":
        warnings.append("LabSpec did not save the map's .l6s file")
    recs = parse_native_map(r["out"], study, prof, x0, y0, zref)
    if len(recs) != n:
        warnings.append(f"LabSpec's map has {len(recs)} points, the study {n}: check the axes (TESTING Q29)")
    nsp = (ctx.native_info or {}).get("spectrometer") or {}
    for rec in recs:
        if nsp:
            rec["native_spectro_moved_by_labspec"] = nsp["moved_by_labspec"]
        rec.update(file=r["out"], exposure_s=float(acq.get("exposure_s") or 1.0),
                   accumulations=float(acq.get("accumulations", 1)), id=r.get("id"), data_via=r.get("data_via"))
    return recs


def parse_native_map(path, study: dict, prof: dict, x0, y0, zref) -> list:
    """The bridge's dump of a LabSpec map: axes (types, sizes, units, values) and the values in LabSpec's order
    (profile mapping.data_order: each spectrum contiguous, or each wavelength's image contiguous; points with the
    first spatial axis fastest). Each point becomes a record with its spectrum and grid indices (the study's
    axes)."""
    meta, data = parse_kv_and_csv(path)
    types = [t.strip() for t in meta.get("axis_types", "").split(",")]
    sizes = [int(float(v)) for v in meta.get("axis_sizes", "").split(",") if v.strip()]
    units = [u.strip() for u in meta.get("axis_units", "").split(",")]
    axis = np.array([float(v) for v in meta.get("axis_values", "").split(",") if v.strip()], float)
    vals, pos_ = {}, 0
    for t, sz in zip(types, sizes):
        vals[t] = axis[pos_:pos_ + sz]
        pos_ += sz
    spec_t = next((t for t in types if t.lower().startswith("spectr")), None)
    if spec_t is None:
        raise Stop("the native map has no spectral axis", "study")
    xs_spec = vals[spec_t]
    ns = xs_spec.size
    ui = types.index(spec_t)
    x_unit = units[ui] if ui < len(units) and units[ui] else "nm"
    sp_axes = [t for t in ("X", "Y", "Z") if t in vals and vals[t].size > 0]
    shape = [vals[t].size for t in sp_axes]
    npts = int(np.prod(shape)) if shape else 1
    if data.size != npts * ns:
        raise Stop(f"the native map holds {data.size} values, not {npts} x {ns}", "study")
    order = prof.get("mapping", {}).get("data_order", "spectral_fastest")
    spec = data.reshape(npts, ns) if order == "spectral_fastest" else data.reshape(ns, npts).T
    m = study.get("map", {})
    absolute = prof.get("mapping", {}).get("positions", "absolute") == "absolute"
    refs = {"X": x0, "Y": y0, "Z": zref}
    grid = {t: plan.axis_values(m[f"{t.lower()}_um"]) for t in ("X", "Y", "Z") if m.get(f"{t.lower()}_um")}
    recs = []
    for k in range(npts):
        rem, idx = k, {}
        for t, sz in zip(sp_axes, shape):          # the first spatial axis varies fastest
            idx[t] = rem % sz
            rem //= sz
        ixyz = {"ix": 0, "iy": 0, "iz": 0}
        stage, offs = {}, {}
        for t in sp_axes:
            v = float(vals[t][idx[t]])
            off = v - (refs[t] or 0.0) if absolute else v
            offs[t] = round(off, 4)
            stage[f"{t.lower()}_um"] = v if absolute else (refs[t] or 0.0) + v
            g = grid.get(t)
            if g:
                ixyz["i" + t.lower()] = int(np.argmin([abs(off - gv) for gv in g]))
        recs.append({"point": k, "ixyz": ixyz, "stage": stage,
                     "offset": {"x_um": offs.get("X", 0.0), "y_um": offs.get("Y", 0.0), "dz_um": offs.get("Z", 0.0)},
                     "_sp": {"x": xs_spec, "y": spec[k], "meta": {"x_unit": x_unit, "y_unit": "cnt"}}})
    return recs

