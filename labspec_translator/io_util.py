"""io_util.py — dependency-free file, JSON, process, and numeric parsing helpers shared across ls6t.

Kept as a leaf module (stdlib only at import time; numpy imported lazily in parse_kv_and_csv) so store, bridge,
profile, spectra, video, runner, clients, and sim can share these primitives without circular imports.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path


def replace_retry(src, dst, tries: int = 100, wait_s: float = 0.02) -> None:
    """os.replace, retried while Windows briefly holds a file open (antivirus or a concurrent reader)."""
    for k in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if k == tries - 1:
                raise
            time.sleep(wait_s)


LASER_MAY_BE_ON = ("THE LASER MAY STILL REACH THE SAMPLE: ls6t held the laser shutter open (LabSpec's ShutterMode "
                   "manual) when the bridge stopped answering. Close it now: switch the laser off in LabSpec's status "
                   "bar if LabSpec responds, otherwise switch the laser off at its power supply or key; after "
                   "restarting LabSpec, check that the laser shutter reads Off and ShutterMode is automatic (0).")


def read_text(path) -> str:
    """A text file from the bridge, HORIBA QC, or the simulator: UTF-16 (with or without BOM), UTF-8
    with or without a BOM, or else the Windows ANSI page."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_header_lines(path) -> dict:
    """Reads only the leading '# key=value' lines from a spectrum/data file without loading the body."""
    p = Path(path)
    with open(p, "rb") as fb:
        bom = fb.read(3)
    if bom[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    elif bom == b"\xef\xbb\xbf":
        enc = "utf-8-sig"
    else:
        enc = "utf-8"

    def _scan(encoding: str, errors: str = "strict") -> dict:
        meta: dict = {}
        with open(p, "r", encoding=encoding, errors=errors) as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                if not s.startswith("#"):
                    break
                body = s[1:].strip()
                if "=" in body:
                    k, v = body.split("=", 1)
                    meta[k.strip()] = v.strip()
        return meta

    if enc == "utf-8":
        try:
            return _scan("utf-8", "strict")
        except UnicodeDecodeError:
            return _scan("cp1252", "replace")
    return _scan(enc, "replace")


def parse_kv_and_csv(path):
    """Parses a bridge data dump ('# key=value' header lines followed by comma-separated numeric rows).
    Returns (meta_dict, 1D numpy float64 array) using vectorized np.fromstring."""
    import numpy as np

    meta: dict = {}
    rows: list[tuple[int, str]] = []
    for line_no, line in enumerate(read_text(path).splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            k, _, v = s[1:].strip().partition("=")
            meta[k.strip()] = v.strip()
        else:
            rows.append((line_no, s))
    if not rows:
        return meta, np.empty(0, dtype=np.float64)
    body = ",".join(r for _, r in rows)
    expected = body.count(",") + 1
    data = np.fromstring(body, sep=",", dtype=np.float64)
    if data.size != expected:
        seen = 0
        bad_row, bad_line, bad_col, bad_tok = len(rows), rows[-1][0], 0, ""
        for idx, (ln, r) in enumerate(rows, 1):
            toks = r.split(",")
            if seen + len(toks) > data.size:
                bad_row, bad_line, bad_col = idx, ln, data.size - seen
                bad_tok = toks[bad_col]
                break
            seen += len(toks)
        raise ValueError(
            f"{path} (data row {bad_row}/{len(rows)}, line {bad_line}, col {bad_col + 1}): "
            f"malformed numeric token {bad_tok!r} (parsed {data.size} of {expected} values)"
        )
    return meta, data


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data, *, indent: int = 1, default=str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, default=default), encoding="utf-8")
    replace_retry(tmp, p)


def to_float(v, *, finite: bool = True, comma: bool = False) -> float | None:
    try:
        x = float(str(v).replace(",", ".")) if comma else float(v)
        return x if (not finite or math.isfinite(x)) else None
    except (TypeError, ValueError):
        return None


def pid_alive(pid) -> bool | None:
    """Whether a process with this id runs (None: cannot tell). Windows: OpenProcess + GetExitCodeProcess (never
    os.kill, which ends the process there)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259            # STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True             # exists, another user's
