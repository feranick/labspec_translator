"""build_installer.py — builds the Windows installer of labspec-translator: dist/ls6t-setup-<version>.exe.
Version: 2026.11.20.32

  python installer/build_installer.py                 # everything (needs network and NSIS)
  python installer/build_installer.py --no-nsis       # stage the files only (build/installer/stage)

Runs on Windows, macOS or Linux with Python 3.10+ and pip. It:
  1. takes python.org's embeddable Python for 64-bit Windows (downloaded once to build/installer/cache, or
     --python-zip FILE) and turns on its site-packages ('import site' in its ._pth file);
  2. builds the labspec-translator wheel and installs it, with its extras mcp, render and windows and every
     dependency, as Windows wheels into that Python's Lib/site-packages (pip --platform win_amd64: no
     compiler, nothing runs at install time);
  3. adds the launchers (installer/bin), the guides (Markdown and HTML) and the examples;
  4. runs makensis (NSIS 3) on installer/ls6t.nsi.

NSIS: Windows `winget install NSIS.NSIS`; macOS `brew install makensis`; Debian/Ubuntu `apt install nsis`.
The installer is not code-signed: Windows SmartScreen shows "Windows protected your PC" until it is (More info >
Run anyway). --sign "CMD {file}" runs a signing command on the result (e.g. signtool on Windows).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "installer"
BUILD = ROOT / "build" / "installer"
DEFAULT_PYTHON = "3.12.10"       # the last 3.12 release with Windows binaries (embeddable zip) on python.org
EXTRAS = "mcp,render,windows"
# dependencies that are Windows-only by their markers: pip evaluates markers for the computer it runs on, so a build
# on macOS or Linux would leave them out
WINDOWS_ONLY = ["comtypes>=1.2", "pywin32>=306", "colorama"]
DOCS = ["README.md", "INSTALL.md", "FUNCTIONALITY.md", "TESTING.md"]

CSS = """body{font-family:Segoe UI,system-ui,sans-serif;max-width:1000px;margin:2em auto;padding:0 1em;line-height:1.5;
color:#1d1d1f}code,pre{font-family:Consolas,monospace;background:#f3f3f3}pre{padding:.8em;overflow-x:auto}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3em .6em;vertical-align:top}h1,h2{border-bottom:1px
solid #ddd}"""


def log(msg: str):
    print(f"[build] {msg}", flush=True)


def version() -> str:
    text = (ROOT / "labspec_translator" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def run(cmd: list, **kw):
    log(" ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def get_python_zip(ver: str, given: str | None) -> Path:
    if given:
        return Path(given).resolve()
    cache = BUILD / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    z = cache / f"python-{ver}-embed-amd64.zip"
    if not z.exists():
        url = f"https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip"
        log(f"downloading {url}")
        tmp = z.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        tmp.replace(z)
    return z


def stage_python(zip_path: Path, pydir: Path) -> str:
    """Unpacks the embeddable Python and enables site-packages. Returns its tag, e.g. '312'."""
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(pydir)
    pth = sorted(pydir.glob("python3*._pth"))
    if not pth:
        raise SystemExit(f"{zip_path}: no python3XX._pth: not python.org's embeddable Python")
    tag = re.match(r"python(3\d+)\._pth", pth[0].name).group(1)
    lines = pth[0].read_text(encoding="utf-8").splitlines()
    lines = ["import site" if ln.strip() in ("#import site", "# import site") else ln for ln in lines]
    if "import site" not in [ln.strip() for ln in lines]:
        lines.append("import site")
    pth[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
    (pydir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    return tag


def build_wheel() -> Path:
    out = BUILD / "wheel"
    shutil.rmtree(out, ignore_errors=True)
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", out, ROOT])
    whl = sorted(out.glob("labspec_translator-*.whl"))
    if not whl:
        raise SystemExit("the wheel was not built")
    return whl[-1]


def install_packages(whl: Path, site: Path, tag: str, wheels: str | None):
    pyver = f"{tag[0]}.{tag[1:]}"
    cmd = [sys.executable, "-m", "pip", "install", "--target", site, "--platform", "win_amd64",
           "--python-version", pyver, "--implementation", "cp", "--abi", f"cp{tag}", "--only-binary=:all:",
           "--upgrade", "--no-warn-script-location", "--disable-pip-version-check"]
    if wheels:
        cmd += ["--no-index", "--find-links", wheels]
    run(cmd + [f"{whl}[{EXTRAS}]"] + WINDOWS_ONLY)
    shutil.rmtree(site / "bin", ignore_errors=True)            # scripts made for the build computer
    for pkg in ("numpy", "scipy", "matplotlib"):                # their test suites: about 30 MB, never imported
        for t in sorted((site / pkg).rglob("tests"), key=lambda p: -len(p.parts)):
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
    for junk in site.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)


def render_docs(dest: Path):
    """The guides as Markdown and HTML (the HTML opens with a double click on any Windows PC)."""
    tools = BUILD / "tools"
    try:
        sys.path.insert(0, str(tools))
        import markdown  # noqa: F401
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "--target", tools, "--disable-pip-version-check", "-q",
             "markdown>=3.4"])
        import importlib
        importlib.invalidate_caches()
    import markdown
    dest.mkdir(parents=True, exist_ok=True)
    for name in DOCS:
        src = ROOT / name
        shutil.copy2(src, dest / name)
        body = markdown.markdown(src.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "toc"])
        body = re.sub(r'href="(' + "|".join(re.escape(d[:-3]) for d in DOCS) + r')\.md(#[^"]*)?"',
                      lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"', body)
        title = name[:-3]
        (dest / f"{title}.html").write_text(
            f"<!doctype html><html><head><meta charset='utf-8'><title>ls6t {title}</title><style>{CSS}</style>"
            f"</head><body>{body}</body></html>\n", encoding="utf-8")


def find_makensis(given: str | None) -> str | None:
    if given:
        return given
    found = shutil.which("makensis")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (Path(base) / "NSIS" / "makensis.exe").exists():
            return str(Path(base) / "NSIS" / "makensis.exe")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default=DEFAULT_PYTHON, help=f"embeddable Python version (default {DEFAULT_PYTHON})")
    ap.add_argument("--python-zip", help="a python-X.Y.Z-embed-amd64.zip already downloaded")
    ap.add_argument("--wheels", help="a folder of Windows wheels to install from (offline build)")
    ap.add_argument("--makensis", help="the makensis program (default: found on PATH or in Program Files)")
    ap.add_argument("--no-nsis", action="store_true", help="stage the files only")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="where the setup goes (default dist/)")
    ap.add_argument("--sign", help='a command to sign the result, with {file} for its path')
    a = ap.parse_args(argv)
    ver = version()
    stage = BUILD / "stage"
    log(f"labspec-translator {ver}: staging in {stage}")
    shutil.rmtree(stage, ignore_errors=True)
    tag = stage_python(get_python_zip(a.python, a.python_zip), stage / "python")
    log(f"embeddable Python 3.{tag[1:]}")
    install_packages(build_wheel(), stage / "python" / "Lib" / "site-packages", tag, a.wheels)
    shutil.copytree(HERE / "bin", stage / "bin")
    render_docs(stage / "docs")
    shutil.copytree(ROOT / "examples", stage / "examples")
    (stage / "VERSION.txt").write_text(f"labspec-translator {ver}\n", encoding="utf-8")
    size = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
    log(f"staged {size / 1e6:.0f} MB")
    if a.no_nsis:
        return 0
    mk = find_makensis(a.makensis)
    if not mk:
        raise SystemExit("makensis not found: install NSIS 3 (Windows: winget install NSIS.NSIS; macOS: brew install "
                         "makensis; Linux: apt install nsis), or give --makensis, or use --no-nsis")
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    exe = out / f"ls6t-setup-{ver}.exe"
    run([mk, "-V2", f"-DVERSION={ver}", f"-DSTAGE={stage}", f"-DOUTFILE={exe}", HERE / "ls6t.nsi"])
    if a.sign:
        run(shlex.split(a.sign.replace("{file}", str(exe)), posix=os.name != "nt"))
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (exe.with_suffix(".exe.sha256")).write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
    log(f"{exe} ({exe.stat().st_size / 1e6:.1f} MB), sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
