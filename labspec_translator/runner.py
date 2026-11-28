"""runner.py — compatibility facade re-exporting runner_core, studies, analysis, and calibration_learn.

The implementation is split across four focused modules:
  - runner_core.py       (locks, Ctx, run_request, _setup, run_any, live view, stage/Z safety)
  - studies.py           (the 10 study kinds and focus/spot/native-map/alignment helpers)
  - analysis.py          (analyse, laser-line tracking, peak/band/map/depth summaries, laser health, calibration)
  - calibration_learn.py (repeatability, laser-line scatter, zero creep, drift, and window-rate learning)
"""
from __future__ import annotations

import json  # noqa: F401 - re-exported for callers/tests

from . import store  # noqa: F401 - re-exported for callers/tests
from .calibration_learn import *  # noqa: F401, F403
from .calibration_learn import (  # noqa: F401 - explicit underscore re-exports
    _anchor_repeats, _clean_rep_entry, _cm1_per_px, _common_creep, _landing_steps,
    _learn_first_landing, _repeat_offsets, _robust_sd, _tight_sd, _u_total,
    _unfollowed_term,
)
from .analysis import *  # noqa: F401, F403
from .analysis import (  # noqa: F401 - explicit underscore re-exports
    _calibration_result, _check_ref_key, _crossings, _map_result, _point_summary,
    _poisson_tail, _quantities, _series_result, _spikes_where_meaningful, _stats,
    _sw_key,
)
from .runner_core import *  # noqa: F401, F403
from .runner_core import (  # noqa: F401 - explicit underscore re-exports
    _acq_timeout, _acquire_lock, _af_bound, _band_said, _check_abs, _confirm_depth,
    _continues_hold, _eta, _f, _finish, _focus_found_this_session, _focus_here,
    _jsonable, _live_view, _matches, _not_returned, _objective_source, _paint_band,
    _progress, _release_lock, _retract_before_xy, _return_home, _run_kind, _setup,
    _shutter_gate, _snapshot, _stage_now, _study_laser, _video_focus_after_travel,
    _warmup, _z_direct, _z_guard,
)
from .studies import *  # noqa: F401, F403
from .studies import (  # noqa: F401 - explicit underscore re-exports
    _band, _cam_frame, _focus_from_below, _labspec_params, _move_seconds, _native_map,
    _record_resolution, _scan_signal, _sharpness, _texture,
)
