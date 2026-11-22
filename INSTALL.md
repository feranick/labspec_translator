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
2. **Log in with the Windows account that runs LabSpec**, and quit the LLM apps (Claude: right-click its icon in the
   taskbar's notification area, *Quit*; closing its window is not enough; ChatGPT; Codex; Gemini CLI): the
   installation cannot go on while they run. LabSpec can stay open.
3. **Run it.** The installer is not code-signed, so Windows SmartScreen may say *"Windows protected your PC"*: *More
   info > Run anyway*. Choose the components:

   | Component | Default | What it does |
   |---|---|---|
   | ls6t | always | The commands, the MCP server, a private Python, the guides; writes the LabSpec scripts to `%USERPROFILE%\ls6t\vbs` |
   | Connect ls6t to the LLM apps found | the apps found are ticked | Adds the `ls6t` MCP server to the Claude desktop app, the ChatGPT desktop app / Codex, and Gemini CLI: each one ticked (see [step 8](#8-connect-an-llm-app); files backed up first) |
   | Add ls6t to PATH | on | `ls6t`, `ls6t-mcp`, `ls6t-typelib` in any new PowerShell window |
   | Start menu shortcuts | on | *ls6t PowerShell*, *LabSpec scripts (ls6t)*, *ls6t guides*, *Uninstall ls6t* |
   | Also the simulator | off | Also adds `ls6t-sim`, the same tools on the built-in simulator, to those apps |

   It checks that everything loads and precompiles the packages (a minute or two). An app installed after ls6t:
   `ls6t connect auto` ([step 8](#8-connect-an-llm-app)).
4. **Then** start `ls6t_bridge.vbs` in LabSpec (step [6](#6-install-the-labspec-scripts), from *"In LabSpec 6"*).
   An update ends a running bridge cleanly first (its window closes; `deploy_log.jsonl` records it): start it again.
   LabSpec need not be restarted for an update. The grating's first status after a start has sometimes taken
   30–50 s (cause under observation, `warmup_log.jsonl`); the bridge's start-up warm-up waits for it. When
   it starts, it reads the instrument's inventory and ls6t builds the machine profile from it by itself (cautious
   until reviewed: step [7](#7-the-machine-profile)); running `ls6t_discover.vbs` separately is optional. Restart
   the connected apps, and check with the instrument (step [9](#9-check-with-the-instrument)). Steps 1–5 and 8 are
   done. Use *Start > ls6t PowerShell* (or any new PowerShell window) for the `ls6t` commands.

   The installer itself cannot discover the instrument: only a script running inside LabSpec can read it. On a
   reinstall, an inventory already on the PC is used at once.

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
Uninstall ls6t*. It removes the program, the PATH entry and the `ls6t` entries it finds in the LLM apps; your data stays.

**Silent install** (for IT): `ls6t-setup-<version>.exe /S` installs with the defaults (the apps found are connected);
`/CLAUDE /CODEX /GEMINI` choose the apps instead, `/NOAPPS` none, `/SIM` adds the simulator entries; `/D=C:\path\ls6t`
(last on the line) changes the folder.

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
limits a person sets.

**Automatic at first.** Each time `ls6t_bridge.vbs` starts in LabSpec, it records the instrument's inventory
(motors and their positions, the detector; nothing moves, no laser) in `%USERPROFILE%\ls6t\discovery_latest.txt`.
While no person has reviewed a profile, ls6t builds `profile.json` from it automatically and keeps it in step with
later inventories. An automatic profile is enough to set up the instrument (laser line, grating, filter, objective,
cameras) and move the stage, but **the laser does not fire** with it: no studies that acquire, no `laser_on`, no
snapshots with the laser. Every laser is also capped at a 10 % filter and the laser shutter is not used.

`ls6t status` and `study_format` say when the profile is automatic. Once a person runs `ls6t profile import` or
`ls6t profile set`, the profile is theirs: it is never replaced again, and `ls6t status` reports any difference
with a newer inventory (a new objective, say). The laser fires once the profile is **reviewed** (steps below), with
every laser's `nm`, `max_filter_pct` and `power_mw` filled in. `ls6t status` shows `profile.reviewed`. Facts LabSpec reports that only restrict ls6t (the spectrometer's range per grating, SpMin / SpMax) reach a person's profile from the bridge's inventory at each start, reviewed or not (`facts_from_inventory` in `ls6t profile show`).

**Reviewing it** (TESTING.md T1.2), with the maintainer:

1. The instrument's list is already there: the bridge's inventory, `%USERPROFILE%\ls6t\discovery_latest.txt`,
   written each time `ls6t_bridge.vbs` starts. (`ls6t_discover.vbs` adds a few self-tests of the bridge's design;
   it is not needed for the profile. If LabSpec crashes during it, do not repeat it: see *Troubleshooting*.)
2. Make a proposal from it:

   ```powershell
   ls6t profile propose
   ```

   It writes `%USERPROFILE%\ls6t\profile.proposed.json` (keeping the current limits) and lists what was found and
   what to fill in. The profile in use does not change.
3. Edit the proposal: `notepad $HOME\ls6t\profile.proposed.json`. In particular:
   - `lasers.<nm>.max_filter_pct`: the highest filter transmission allowed per laser, and `power_mw`, the power at
     the sample at 100 % (measured with a power meter; recorded with results). Both are required.
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
4. Import it, which checks it and marks the profile reviewed: `ls6t profile import
   "$HOME\ls6t\profile.proposed.json"`. A laser with `nm`, `max_filter_pct` or `power_mw` blank is refused. Later
   changes: `ls6t profile set KEY VALUE`, or the same propose / edit / import.

   Without any profile (no inventory, no import), ls6t refuses studies on the instrument: its built-in defaults are
   a generic XploRA for the simulator (other laser lines, an x50, no caps), not this instrument.

## 8. Connect an LLM app

ls6t's MCP server works with every app that starts local (stdio) MCP servers. On Windows:

| `ls6t connect` name | App | Its MCP file |
|---|---|---|
| `claude` | Claude desktop app | `claude_desktop_config.json` in the folder the app reads: Microsoft Store builds (current) `%LOCALAPPDATA%\Claude-Data\claude_<id>\`; older Store builds `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\`; the installer from claude.ai `%APPDATA%\Claude\` |
| `codex` | ChatGPT desktop app, Codex CLI and Codex IDE extension: they share one file | `%USERPROFILE%\.codex\config.toml` (or `%CODEX_HOME%`) |
| `gemini` | Gemini CLI | `%USERPROFILE%\.gemini\settings.json` |

The Gemini app and ChatGPT on the web take only remote servers (an internet address). ls6t does not put the
instrument on the internet, so they cannot use it; use Gemini CLI or the ChatGPT desktop app on the instrument PC.

```powershell
ls6t connect                 # which apps are here, and which already run ls6t
ls6t connect check           # start each app's ls6t entry as the app would: its tools, or why it fails
ls6t connect auto            # add "ls6t" to every app found (or name them: ls6t connect codex gemini)
ls6t connect auto --sim      # also "ls6t-sim": the same tools on the simulator, to practise
ls6t connect codex --remove  # take this install's entries out again
ls6t connect claude --test   # test mode: the instrument tests of TESTING.md from the chat (--no-test: off)
```

Each file is backed up first (`<file>.ls6t-backup-<date>`); other servers and settings in an existing ls6t entry are
kept. Every entry gets the same `LS6T_HOME` and `LS6T_OUT`, so all apps share one instrument lock, one bridge and
one profile: two apps cannot drive the instrument at the same time. The entry runs the Python that ran `ls6t connect`
(`python.exe -m labspec_translator.mcp_server`).

**Quit the Claude app before** `ls6t connect claude` (or the installer): it rewrites its configuration file when it
quits, which would undo the change (right-click its icon in the taskbar's notification area, *Quit*). The running
app's `--user-data-dir` says which folder it reads; `ls6t connect` finds it (and the Store builds' newer
`Claude-Data` folder) and removes ls6t's entries from folders the app no longer reads. Then start the app again; the
ls6t tools appear in a new chat or session (Claude: the tools / connectors menu). Notes per app:

- **ChatGPT desktop app / Codex**: tool calls may wait up to an hour for the operator's answer in LabSpec, so the entry
  sets `tool_timeout_sec = 3600` (and `startup_timeout_sec = 60`). A report from 2026 says local servers' tools may
  not reach ordinary chats in the Windows desktop app; Codex tasks do get them.
- **Gemini CLI**: the entry keeps `trust: false`, so Gemini CLI asks before each tool call; `timeout` is one hour.

**Apps that can also run commands on the PC.** The ChatGPT desktop app / Codex and Gemini CLI (and Claude with a
terminal extension) can run shell commands. An LLM that cannot find what it wants in ls6t's tools may then try to
drive LabSpec directly, with scripts, its ActiveX control or the bridge's files. That bypasses the operator's
confirmations and ls6t's limits, and can freeze LabSpec: a modal dialog stops LabSpec's scripts, the bridge with
them. ls6t's instructions forbid it, but on the instrument PC also make the app ask before every command:

- Codex / ChatGPT desktop: in `%USERPROFILE%\.codex\config.toml`, `approval_policy = "on-request"` and
  `sandbox_mode = "read-only"` (or choose the read-only, ask-first mode in the app's settings);
- Gemini CLI: in `%USERPROFILE%\.gemini\settings.json`, `"tools": {"exclude": ["run_shell_command"]}` (no shell), or
  at least `"tools": {"confirmationRequired": ["run_shell_command"]}`; never start it with `--yolo`;
- Claude desktop app: do not add terminal or computer-control extensions on the instrument PC, or approve each
  command.

Answer *No* to any command that touches LabSpec, `NFActiveX` or the `ls6t\bridge` folder. If the LLM says it cannot
do something with ls6t's tools, tell the ls6t maintainer: that is a missing tool, not a reason to go around it.

By hand (Claude, JSON; backslashes doubled). Prefer `ls6t connect`, which writes this PC's real paths: a
`<you>` left from the example below makes the app fail with `spawn ... ENOENT`, and `tools\ls6t_diagnose.ps1`
reports such placeholders and commands that do not exist.

```json
{
  "mcpServers": {
    "ls6t": {
      "command": "C:\\Users\\<you>\\Software\\labspec_translator\\.venv\\Scripts\\python.exe",
      "args": ["-m", "labspec_translator.mcp_server"],
      "env": {"LS6T_CONFIRM": "request"}
    }
  }
}
```

Gemini CLI takes the same `mcpServers` block in its `settings.json`. Codex (TOML):

```toml
[mcp_servers.ls6t]
command = 'C:\Users\<you>\Software\labspec_translator\.venv\Scripts\python.exe'
args = ["-m", "labspec_translator.mcp_server"]
startup_timeout_sec = 60
tool_timeout_sec = 3600

[mcp_servers.ls6t.env]
LS6T_CONFIRM = "request"
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

Then end the bridge (`ls6t instrument end-bridge`), start the new `ls6t_bridge.vbs` in LabSpec, and restart
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
| `camera_on` answers but the live view stays on the other camera; `camera_off` never returns; the bridge stops when the video is started or stopped in LabSpec | Seen on 2026-11-20 with earlier versions. LabSpec switches cameras only when the video starts, a script must stop the video HORIBA's way (STOP, then wait on its status, then no video query until it starts again: ls6t does this from .27, on by default from .29), and LabSpec's video buttons end a running script. `camera_off` works. Switching cameras is off unless `video.switch_camera` is true (TESTING T3.14): then ls6t stops the video, selects the camera, waits, starts it and checks it. Otherwise, to change camera: `ls6t instrument end-bridge`, then in LabSpec stop the video, choose the camera, start the video, then start `ls6t_bridge.vbs`. |
| ls6t says the bridge is stuck inside LabSpec (busy in a command, no dialog) | Nothing in LabSpec frees it: not the ls6t window's Cancel (LabSpec froze after it on 2026-11-20). Wait a minute; then save what you can, close LabSpec (Task Manager if it does not respond), start it again, then `ls6t_bridge.vbs`. Tell the ls6t maintainer the command named. |
| LabSpec shows *About NFActiveX Control* during discovery, then crashes | Seen with 2026.11.20.21's discovery (its write probe of a scratch spectrum is the suspect; removed in .22). Why the About box opened is not known: ls6t never calls `AboutBox`. If it opens again on .22 or later, even without a crash, note what ran (`discovery_progress.txt`, `ls6t jobs`) and tell the ls6t maintainer. Do not repeat discovery on that account: the bridge's inventory (`discovery_latest.txt`) already gives the profile. From .22 each step is logged in `%USERPROFILE%\ls6t\discovery_progress.txt` (send it to the ls6t maintainer), and a step that stopped LabSpec is skipped by later runs (`discovery_skip.txt`; delete it to try again). |
| The LLM app does not show ls6t's tools | Copy `tools\ls6t_diagnose.ps1` to the PC and run `powershell -ExecutionPolicy Bypass -File .\ls6t_diagnose.ps1` as the user who runs the app. It changes nothing and writes `ls6t_diagnose.txt` on the Desktop: which configuration file Claude reads, whether the ls6t entry is there and valid, whether the server starts and answers, and Claude's MCP logs. With ls6t 2026.11.20.8 or later, `ls6t connect check` does the main checks too. If the report shows the Claude file Claude reads (`READS:`) without an `ls6t` entry: `tools\ls6t_fix_claude.ps1` (same way) waits for Claude to be closed and adds it there. |
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
