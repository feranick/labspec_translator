# Prompts to try with ls6t

Type these into the LLM app that ls6t is connected to: Claude, Claude Code, ChatGPT / Codex, Gemini CLI, Cursor,
GitHub Copilot, Windsurf or LM Studio. Start a new chat after installing, so the app picks up the ls6t tools. Plain
language works: the LLM reads ls6t's study format and turns your request into a study.

Lasers, gratings, objectives and filters below are examples from an XploRA (532 / 638 / 785 nm; x5, x10, x100).
Use what your instrument has: the first prompt below lists it.

**Before anything uses the laser:**

- The operator is at the instrument. Every laser step asks for the operator's Yes in LabSpec, and the operator can
  stop it at any time.
- The maintainer has reviewed the machine profile and its limits (INSTALL.md step 7, TESTING.md T1). Until then,
  ls6t refuses laser measurements and says why.
- Want to practise first? Use the simulator entry `ls6t-sim` (installer option, or `ls6t connect auto --sim`). The
  same prompts work there, with no instrument.

## 1. First steps (no laser)

- *"What can ls6t do on this instrument? List the lasers, gratings, objectives, filters and the limits."*
- *"What is the instrument's state right now?"*
- *"Take a snapshot of the live video and show it to me."*
- *"Set the x10 objective and the 1 % filter."* (LabSpec asks the operator to turn the turret by hand.)
- *"Measure the video's scale for this objective."* (The stage moves a little in X and Y, then comes back.)
- *"Without running anything: what would a 532 nm spectrum from 100 to 1800 cm⁻¹, 10 s, 2 accumulations send to
  LabSpec, and how long would it take?"*
- *"Check this request and tell me what's wrong with it before I run it: a map 200 × 200 µm with 1 µm steps at
  100 % power."* (ls6t's checks explain each refusal.)
- *"Show me the requests run today and which one holds the instrument."*

## 2. First measurements (laser; profile reviewed, operator present)

- *"Take one spectrum of silicon at 532 nm, 1 % filter, 100–1000 cm⁻¹, 5 s. Fit the 520 cm⁻¹ peak and give me its
  position and width."*
- *"Check the spectral calibration with the silicon wafer at 532 nm. If it is off by more than 1 cm⁻¹, guide me
  through correcting it in LabSpec."*
- *"Is the system aligned for 532 nm? Compare with the baseline."*
- *"Focus on the silicon by the Raman signal at 520 cm⁻¹ with the x100, then take a spectrum."*
- *"Find where the laser spot really is relative to LabSpec's marker, with the x100 and 532 nm."* (A `spot` study:
  until it runs, the marker on snapshots is only assumed.)
- *"Measure the real transmission of each filter with silicon at 532 nm."*
- *"Take 20 spectra at this spot, one every 30 s, and plot the 520 cm⁻¹ peak's intensity over time."*

## 3. Advanced

- *"Take a snapshot, number the small particles, and take a 638 nm spectrum on particle 3."* (The beam goes to the
  point picked on the image; run a `spot` study first, so it lands where the beam really is.)
- *"Map a 20 × 20 µm area around here with 1 µm steps at 532 nm, 1 s per point, 1 % power. Make images of the G
  and 2D bands and of their ratio."* (Graphene; ask for the time estimate first.)
- *"Depth profile from 10 µm above to 40 µm below the focus, 2 µm steps, at 785 nm. The sample is a polymer film
  with refractive index 1.49 on glass: find the interface."*
- *"Record a white-light background named 'glass slide' with the laser off, then take the sample's spectrum and
  subtract it."*
- *"Where does the edge filter cut off at 532 nm with the 1800 gr/mm grating? Use the white light."*
- *"Calibrate with polystyrene at 785 nm using several lines, and apply the correction to the next spectra."*
- *"Check the calibration on silicon first. If it is within 2 cm⁻¹, focus and take a 5 × 5 point grid with 10 µm
  spacing on the sample; otherwise tell me and stop."*
- *"Measure this sample at 532 and 785 nm with the same range and exposure, and compare: which peaks change between
  the two lasers?"* (Fluorescence against resonance.)
- *"Take a spectrum and tell me whether the 1350 / 1580 cm⁻¹ intensity ratio is within 0.1 of 0.8; flag it if
  not."* (Expected values are checked after the run.)
- *"Help me align the laser power by hand: show the 520 cm⁻¹ signal live while I turn the knob, and stop just
  past the maximum."* (A `guide` study.)

## Tips

- **Ask for a dry run first** for anything long or new (*"what would this send, and how long?"*). Nothing moves.
- **Name the sample** (*"the sample is 'PET film 3'"*): it goes into LabSpec's file and ls6t's results.
- **Ask for what to trust:** *"Is this fit reliable? Show me the plot."* The results say when a fit or a check
  failed.
- **To stop:** *"Cancel the request"*, or press STOP in LabSpec. The operator can always stop from LabSpec.
- **If the LLM says it can't do something with ls6t's tools**, it must not work around them (scripts, LabSpec's
  ActiveX control, the bridge's files). Tell the ls6t maintainer instead.
- **Results stay on this PC** in the ls6t output folder: spectra, maps, LabSpec `.l6s` files, PNG plots. Ask the
  LLM for a file's path, or for the plot.

More detail: [README.md](README.md) (how it works), [FUNCTIONALITY.md](FUNCTIONALITY.md) (everything a study can ask
for), [INSTALL.md](INSTALL.md), [TESTING.md](TESTING.md).
