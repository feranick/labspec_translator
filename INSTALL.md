# Installing labspec-translator (ls6t) on Windows 11

How to install ls6t on the instrument PC: the Windows 11 computer that runs HORIBA LabSpec 6, next to the
Claude desktop app. What ls6t is and how it is used: [README.md](README.md). The first measurements with the
instrument's maintainer: [TESTING.md](TESTING.md).

**Contents:** [The installer](#the-installer-recommended) · [1. Python and Git](#1-python-and-git) · [2. Folders](#2-choose-the-folders-and-keep-them-out-of-onedrive) · [3. The code](#3-get-the-code) · [4. The package](#4-a-virtual-environment-and-the-package) · [5. Check](#5-check-the-install-no-instrument-needed) · [6. LabSpec scripts](#6-install-the-labspec-scripts) · [7. Machine profile](#7-the-machine-profile) · [8. Claude app](#8-connect-the-claude-desktop-app) · [9. Check with the instrument](#9-check-with-the-instrument) · [Updating](#updating) · [Offline install](#offline-install) · [Troubleshooting](#troubleshooting-windows)

Everything is installed on the **instrument PC**: the Windows 11 computer that runs LabSpec 6. The Claude desktop app
runs there too. The steps below use **PowerShell** (Start, type *PowerShell*, open *Windows PowerShell* or
*Terminal*). Use your normal Windows account, the **same account that runs LabSpec**, so both programs can read and
write the bridge folder. Administrator rights are needed only for installing Python and Git, if they are missing.

## The installer (recommended)

`ls6t-setup-<version>.exe` installs everything for your Windows account. It needs no administrator rights and no
network: it carries its own Python with every package. Use it unless you develop ls6t itself.

1. **Get it**: from the repository's *Releases* page (it is built for every version, see
   [Building the installer](#building-the-installer)), or build it yourself. Copy it to the instrument PC.
2. **Log in with the Windows account that runs LabSpec**, and quit the Claude app (right-click its icon in the
   taskbar's notification area, *Quit*).
3. **Run it.** The installer is not code-signed, so Windows SmartScreen may say *"Windows protected your PC"*: *More
   info > Run anyway*. Choose the components:

   | Component | Default | What it does |
   |---|---|---|
   | ls6t | always | The commands, the MCP server, a private Python, the guides; writes the LabSpec scripts to `%USERPROFILE%\ls6t\vbs` |
   | Connect to the Claude app | on | Adds the `ls6t` server to `claude_desktop_config.json` (backed up first; other servers and your ls6t settings are kept) |
   | Add ls6t to PATH | on | `ls6t`, `ls6t-mcp`, `ls6t-typelib` in any new PowerShell window |
   | Start menu shortcuts | on | *ls6t PowerShell*, *LabSpec scripts (ls6t)*, *ls6t guides*, *Uninstall ls6t* |
   | Simulator connection | off | Also adds `ls6t-sim`, the same tools on the built-in simulator, to practise |

   It checks that everything loads and precompiles the packages (a minute or two).
4. **Then** continue with: adding the scripts in LabSpec (step [6](#6-install-the-labspec-scripts), from *"In LabSpec
   6"*), the machine profile (step [7](#7-the-machine-profile)), restarting the Claude app, and the check with the
   instrument (step [9](#9-check-with-the-instrument)). Steps 1–5 and 8 are done. Use *Start > ls6t PowerShell* (or
   any new PowerShell window) for the `ls6t` commands.

| Where | What |
|---|---|
| `%LOCALAPPDATA%\Programs\ls6t` | the program: `python\`, `bin\` (launchers), `docs\` (these guides, also as HTML), `examples\`, `install.log` |
| `%USERPROFILE%\ls6t` | your data, kept by updates and by uninstalling: profile, bridge folder, LabSpec scripts, discovery dumps |
| `%USERPROFILE%\ls6t_runs` | the results |

**Updating:** quit the Claude app, let any measurement finish, and run the new setup over the old one. It replaces
the program and rewrites the LabSpec scripts; the profile and the results stay. Then, in LabSpec, stop the bridge and
start the new `ls6t_bridge.vbs`, and start the Claude app again. (If ls6t is still running, the setup says so and
waits.)

**Uninstalling:** *Settings > Apps > Installed apps > labspec-translator (ls6t) > Uninstall*, or *Start > ls6t >
Uninstall ls6t*. It removes the program, the PATH entry and the Claude app's `ls6t` entries; your data stays.

**Silent install** (for IT): `ls6t-setup-<version>.exe /S` installs with the defaults; `/D=C:\path\ls6t` (last on the
line) changes the folder.

### Building the installer

`installer/build_installer.py` stages python.org's embeddable Python 3.12 for 64-bit Windows, installs the package
and its dependencies into it as Windows wheels, adds the launchers and the guides, and runs NSIS on
`installer/ls6t.nsi`. It runs on macOS, Windows or Linux, with network access:

```bash
brew install makensis                    # macOS  (Windows: winget install NSIS.NSIS;  Linux: apt install nsis)
python3 installer/build_installer.py     # -> dist/ls6t-setup-<version>.exe and its .sha256
```

`--no-nsis` stages the files only (`build/installer/stage`); `--python 3.12.10` chooses the embeddable Python;
`--sign "signtool sign /a /fd sha256 {file}"` signs the result if you have a code-signing certificate.

On GitHub, `.github/workflows/installer.yml` does this for every version tag on a clean Windows machine. It also
installs the result silently, runs three benchmarks on the simulator with the installed copy, uninstalls it, and
attaches the setup to the tag's release. *Actions > installer > Run workflow* builds one by hand.

## From source (developers)

The steps below install ls6t from its source code instead, in a virtual environment you manage yourself.

## 1. Python and Git

ls6t needs Python 3.10 or newer (3.12 recommended) and Git.

With **winget** (built into Windows 11):

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Or install them from [python.org](https://www.python.org/downloads/windows/) and [git-scm.com](https://git-scm.com/download/win).
In the Python installer, tick **"Add python.exe to PATH"** and keep the **py launcher**.

Close and reopen PowerShell, then check:

```powershell
py -3.12 --version     # Python 3.12.x
git --version
```

If `py` is not found, the launcher was not installed: run the Python installer again ("Modify") and select it.

## 2. Choose the folders (and keep them out of OneDrive)

Windows 11 often syncs **Documents** and **Desktop** to OneDrive. The bridge exchanges many small files every second,
and a sync client renaming or locking them breaks the exchange. Keep everything in folders OneDrive does not sync:

| What | Default | Notes |
|---|---|---|
| The code | `C:\Users\<you>\Software\labspec_translator` | anywhere outside OneDrive; short paths are easiest |
| ls6t's home: profile, bridge folder, LabSpec scripts, discovery dumps | `C:\Users\<you>\ls6t` (`LS6T_HOME`) | your user folder itself is not synced by OneDrive |
| Results (one folder per request) | `C:\Users\<you>\ls6t_runs` (`LS6T_OUT`) | can grow large with maps |

To check what OneDrive syncs: OneDrive icon in the taskbar, Settings, *Sync and back up*, *Manage back up*.

## 3. Get the code

```powershell
mkdir $HOME\Software -Force
cd $HOME\Software
git clone https://github.com/feranick/labspec_translator.git
cd labspec_translator
```

(Without network access, copy the folder from another computer instead; see [Offline install](#offline-install).)

## 4. A virtual environment and the package

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[mcp,render,test,windows]"
```

- **`Activate.ps1` cannot be loaded because running scripts is disabled**: allow local scripts for your account once,
  then activate again:

  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```

  Or skip activation and call the programs by their full path, e.g. `.\.venv\Scripts\ls6t.exe status`.
- The extras: `mcp` (the server for the Claude app), `render` (PNG plots, matplotlib), `test` (pytest),
  `windows` (comtypes, for `ls6t-typelib`).
- `-e` (editable) means a `git pull` updates the installed code; reinstall only when dependencies change.

The install puts three programs in `.venv\Scripts`: `ls6t.exe` (the command line), `ls6t-mcp.exe` (the MCP
server) and `ls6t-typelib.exe` (lists every command of LabSpec's ActiveX, see step 6).

## 5. Check the install (no instrument needed)

```powershell
ls6t status            # versions, settings, "bridge": {"running": false} (normal for now)
ls6t bench             # the benchmarks on the built-in simulator: "failed": []
pytest -q              # optional: the test suite (about 30 s)
```

`ls6t bench` never touches the instrument. It runs on the simulator, which answers the same commands as LabSpec.

## 6. Install the LabSpec scripts

```powershell
ls6t bridge install
```

This writes three scripts, configured with this PC's folders, to `C:\Users\<you>\ls6t\vbs\`:

| Script | When |
|---|---|
| `ls6t_discover.vbs` | Once, and again after the hardware changes: lists the instrument for the profile. It does not use the laser and moves nothing. |
| `ls6t_bridge.vbs` | Whenever ls6t should drive the instrument. It runs until stopped. |
| `ls6t_script_motor.vbs` | Optional, only for the maintainer's test T1.4 (TESTING.md): a virtual script-controlled motor that drives nothing and logs how LabSpec calls it. Not needed for normal use. |

It also creates the bridge folder `C:\Users\<you>\ls6t\bridge\` (with `inbox` and `outbox`).

In **LabSpec 6**: *Processing > Scripts*. Add the discovery and bridge scripts (the manual's script manager allows adding, configuring and
starting scripts). Run the scripts again after every `ls6t bridge install`, for example after an update.

**What does this LabSpec offer?** `ls6t-typelib` reads LabSpec's ActiveX type library (`NFActiveX.ocx`). It lists
every command and property of the installed version, and marks:

- `NEW`: commands not in the 2018 automation manual;
- commands whose names look useful (alignment, calibration, lamp, progress...).

It only reads the file: nothing starts or moves.

- `ls6t-typelib --format markdown --out labspec_commands.md` writes the list to a file.
- `--path` reads another file (a `.tlb`, or LabSpec's `.exe`).
- It also runs on its own: `python typelib.py`, and on any system: copy `NFActiveX.ocx` to a Mac or Linux PC and
  run `ls6t-typelib --path NFActiveX.ocx` there (it reads the file itself; no Windows needed).

## 7. The machine profile

The profile says what the instrument has: lasers, gratings, filters, objectives and stage axes. It also holds the
limits a person sets. Until one exists, ls6t uses built-in XploRA defaults and says so in every result.

1. In LabSpec, run **`ls6t_discover.vbs`**. A message box shows where it wrote the dump, e.g.
   `C:\Users\<you>\ls6t\discovery_20261120_101500.txt`.
2. Import it:

   ```powershell
   ls6t profile import "C:\Users\<you>\ls6t\discovery_20261120_101500.txt"
   ```

   The output lists what was found and what to check by hand.
3. Edit the limits: `notepad $HOME\ls6t\profile.json`. In particular:
   - `lasers.<nm>.max_filter_pct`: the highest filter transmission allowed per laser. `power_mw` is the power at the
     sample at 100 %, if known (recorded with results).
   - `filters`: every label's transmission in %. Labels that could not be parsed are listed by the import.
   - `limits`: `max_xy_um`, how far a study may move from where the operator left the stage; `max_dz_um`, how far
     from the operator's focus; `max_exposure_s`, `max_accumulations`, `max_points`, `max_minutes`;
     `sensitive_max_filter_pct` for samples marked sensitive.
   - `stage.x_um` / `stage.y_um`: absolute stage limits `[min, max]`, if wanted.
   - `detector.max_temp_c`: warmer than this, results are unreliable (the CCD is not cooled down).
   - `motors.laser_shutter`: the laser shutter's motor name, needed only for video snapshots with the laser on.
     The import proposes one if it finds a shutter motor; confirm it with the maintainer.
   - `video.laser_offset_um`: where the beam really is relative to LabSpec's laser position. It is measured with a
     snapshot with the laser on (TESTING T5b).
   - `alignment`: the alignment baselines and settings per laser are recorded later, with
     `ls6t alignment record` (see [README: Alignment and guided adjustments](README.md#alignment-and-guided-adjustments)).
     `min_ratio`, `max_fwhm_ratio` and `max_age_h` set when a check fails or is stale.
   - `guide`: `max_s` and `max_exposure_s` limit guided adjustments; `ui` is `dialog` (LabSpec's panel) or
     `statusbar`.

   Only a person edits this file. Studies cannot change it, and the MCP tools can only *propose* a profile
   (`instrument(action="discover")` writes `profile.proposed.json`).

## 8. Connect the Claude desktop app

Install the Claude desktop app for Windows. In the app: *Settings > Developer > Edit Config* opens
`claude_desktop_config.json`. That is usually `%APPDATA%\Claude\claude_desktop_config.json`; for the Microsoft Store
version it sits under `%LOCALAPPDATA%\Packages\Claude_...\LocalCache\Roaming\Claude\`. Add the server (keep any other
entries; backslashes are doubled in JSON):

```json
{
  "mcpServers": {
    "ls6t": {
      "command": "C:\\Users\\<you>\\Software\\labspec_translator\\.venv\\Scripts\\ls6t-mcp.exe",
      "env": {"LS6T_CONFIRM": "request"}
    }
  }
}
```

Quit the app completely (right-click its icon in the taskbar's notification area, *Quit*) and start it again. The ls6t
tools then appear in a new chat. Settings go in `env`, for example a different `LS6T_OUT`.

To practise without the instrument, add a second entry that uses the simulator, and remove it later:

```json
    "ls6t-sim": {
      "command": "C:\\Users\\<you>\\Software\\labspec_translator\\.venv\\Scripts\\ls6t-mcp.exe",
      "env": {"LS6T_MODE": "sim", "LS6T_OUT": "C:\\Users\\<you>\\ls6t_sim_runs"}
    }
```

## 9. Check with the instrument

Start **`ls6t_bridge.vbs`** in LabSpec. A small window appears: *"ls6t (labspec-translator) is controlling the
instrument. Close this window to STOP"*. LabSpec's status bar shows *"ls6t bridge … running"*. Then:

```powershell
ls6t status                 # "bridge": {"running": true, "heartbeat": {"state": "idle", "age_s": < 2}}
ls6t instrument status      # laser, grating, filter, objective, stage X/Y/Z, detector temperature, as in LabSpec
```

The first measurements follow [TESTING.md](TESTING.md), with the instrument's maintainer.

## Updating

```powershell
cd $HOME\Software\labspec_translator
git pull
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,render,test,windows]"     # only needed when dependencies changed; harmless otherwise
ls6t bridge install                      # the scripts must match the package's bridge protocol
ls6t bench
```

Then, in LabSpec, stop the bridge (LabSpec's script stop button) and start the new `ls6t_bridge.vbs`, and restart
the Claude app. A bridge of another protocol version is refused with a message saying to reinstall it.

## Offline install

Instrument PCs are often kept off the network. On a connected Windows computer with the same Python version:

```powershell
git clone https://github.com/feranick/labspec_translator.git
cd labspec_translator
py -3.12 -m pip download -d wheels ".[mcp,render,test,windows]"
```

Copy the whole folder, `wheels` included, to the instrument PC. Then, in step 4, install with:

```powershell
pip install --no-index --find-links wheels -e ".[mcp,render,test,windows]"
```

(The Claude app itself needs network access to reach Claude.)

## Troubleshooting (Windows)

| Symptom | Cause and fix |
|---|---|
| `ls6t : The term 'ls6t' is not recognized` | The venv is not active: `.\.venv\Scripts\Activate.ps1`, or use the full path `.\.venv\Scripts\ls6t.exe`. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (once). |
| `ls6t status` shows the bridge not running while it runs in LabSpec | The two use different folders: the script's `LS6T_BRIDGE_DIR` line must match `ls6t status` → `settings.LS6T_BRIDGE_DIR`. Run `ls6t bridge install` with the same settings the server uses, then restart the script. LabSpec running under another Windows account also causes it. |
| Errors about access to files in `ls6t\bridge` | Defender's *Controlled folder access* (ransomware protection) or another antivirus. Allow `python.exe` from the venv and LabSpec, or exclude `C:\Users\<you>\ls6t`. Scanning every small file also slows the bridge. |
| SmartScreen or Defender warns about `ls6t.exe` / `ls6t-mcp.exe` | These are pip's standard launchers for the Python package; allow them, or start the server as `python.exe -m labspec_translator.mcp_server` in the Claude config. |
| A request shows `interrupted` after the Claude app was closed | The request's runner is a separate process. Windows may end it with the app if the app does not allow processes to break away from its job. Keep the app open during long runs, or start long runs from PowerShell (`ls6t run studies.json`). Measured spectra stay in the request folder. |
| Long runs stop when the PC sleeps | Settings > System > Power: never sleep while plugged in, at least during measurements. |
| Paths longer than 260 characters | Unlikely with the default folders. If needed: enable *Win32 long paths* (Group Policy, or `LongPathsEnabled` in the registry). |
| Numbers with a comma as decimal separator in LabSpec | Handled: the bridge converts every number to '.' itself, whatever the Windows locale. |
| A folder name containing `"` | Not supported in the LabSpec scripts' folder constants: choose another folder. |
