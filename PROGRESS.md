# GLASS — progress record

Last updated 2026-10-11. Hardware for all measurements: RTX 4070 Laptop GPU (8 GB), i7-13650HX, 32 GB RAM, Windows 11 23H2, Ollama 0.34.2.

## Status by phase

| Phase | Status |
|---|---|
| 1. Real-time assistant (loopback audio, live transcript, question + name detection, real AI answers, start/stop) | **Done, tested end to end** |
| 2. Visual intelligence (window/monitor capture, OCR, maths recognition, audio–screen fusion, Snip & Solve) | **Done, tested** |
| 3. Notes and assignments (incremental notes, tasks, history, subjects, search, export) | **Done, tested** (notes/tasks automated; export paths built, not exercised by tests) |
| 4. Performance and refinement (scheduler, latency, profiles, metrics, UI polish) | **Done** for the core paths; see limitations |
| 5. Packaging (installer, engine setup, onboarding) | **Done**: NSIS installer builds; the packaged app starts its engine from bundled resources; onboarding state verified |

## Technology decisions (and why)

- **Electron instead of Tauri.** There's no Rust toolchain on this PC, and Electron gives Win11 acrylic/mica, non-activating always-on-top windows, click-through and global shortcuts out of the box.
- **Python engine.** faster-whisper/CTranslate2 on CUDA (cuBLAS/cuDNN from pip wheels, no CUDA toolkit needed), Silero VAD v6 (ONNX, ~0.1 ms/frame), PyAudioWPatch (WASAPI loopback), SymPy, Windows.Media.Ocr via winrt, SQLite.
- **Models.** `qwen3:4b-instruct` answers. The preinstalled `qwen3:4b` is a thinking-only build that ignores `think:false` and spent 5–7 s thinking before every answer. `qwen3-vl:4b-instruct` handles vision. In a six-subject head-to-head both were close; the text model was slightly stronger (correct physics formula, better biology detail).
- **Ollama context pinned to 8192.** Without it, Ollama 0.34 allocates the full 262k context (43 GB) and runs mostly on the CPU (160 s per answer).

## Measured performance (tests/report.md)

| Metric | Result |
|---|---|
| Teacher stops speaking → spoken answer ready (harness, 5 scenarios) | **1.36 – 1.87 s** |
| Same, through real speakers → WASAPI loopback → running app | **1.89 s** |
| Name alert vs end of sentence | fires **~3.3 s before** the teacher finishes (partial transcripts) |
| Exact maths result (SymPy pre-solve) | **29 – 65 ms** |
| Re-answer after typing context | **0.84 – 0.98 s** |
| Whisper large-v3-turbo final transcription | 270 – 670 ms per utterance (fp16, GPU) |
| LLM generation | 55 – 70 tok/s, first token 0.05 – 0.35 s when warm |
| Windows OCR (full worksheet incl. Tesseract re-read of maths lines) | ~0.1 s prose; ~0.3–0.9 s with maths lines |
| Snip & Solve, typed worksheet (OCR path) | **2.3 – 3.5 s** for four answers |
| Snip & Solve, diagram (vision model) | **10 – 40 s** with the Balanced profile (model swap, see limitations) |
| Background notes update | 8 – 12 s (pre-emptible, never blocks answers) |
| VRAM in use (Whisper + answer model) | ~6.5 / 8 GB |

## Automated tests

`engine/tests/test_units.py` — **37/37 pass**:
- Name detection: recall ≥ 88% on direct-address phrasings (including split ASR "Sun ad" and alias spellings). **Zero false positives** on sound-alikes ("sound", "sand", "sent", "senate", "second", "standard", "signed", "Sunday", "salad").
- Question typing, comprehension checks, rhetorical detection, references ("question three", "Q4 b", "the last one"), task cues.
- Equation extraction, exact SymPy results, sandbox rejection of injected code, timeout recovery.
- Scheduler pre-emption, bounded queue, job coalescing; lenient JSON recovery.

`engine/tests/harness.py` — **12/12 scenarios pass**, real pipeline with synthesized speech:

| Spec scenario | Covered by | Result |
|---|---|---|
| 1 Teacher addresses Sanad | direct_name | ✅ high-confidence alert, directed answer, "teacher waiting" detected |
| 2 General class question | general | ✅ |
| 3 Rhetorical question | rhetorical | ✅ panel never elevated; self-answered question demoted |
| 4 Question refers to screen | screen_question | ✅ "question three" → on-screen item 3 → x = 6 verified |
| 5 Equation introduced verbally | equation_spoken | ✅ 120 m / 8 s → 15 m/s verified |
| 6 Equation only on screen | written_equation | ✅ "question 5" → x² − 5x + 6 = 0 → 2, 3 |
| 7 Multiple questions assigned | tasks | ✅ page 45, Q1–6, 15 min, timer not auto-started |
| 8 Task changes | tasks | ✅ detected as replacement; homework due Monday; old task kept |
| 9 Long explanation | notes | ✅ definition captured in notes |
| 11 Audio stops | silence | ✅ no hallucinated transcript from silence/noise |
| 15 Student requests summary | notes | ✅ catch-up produced |
| 16/17 Ambiguous/unsupported question | vague | ✅ low confidence flagged; two words of context → correct answer |
| 18 Long lesson vs context window | notes | ⚠️ by design (bounded contexts, chunked incremental notes); not run against a real 60-minute lesson |
| 19 Small/blurry worksheet | blurry | ✅ 700×160 px worksheet at 13 px text read correctly |
| Snip & Solve | snip | ✅ OCR worksheet, vision diagram (Pythagoras → 10 cm), blank capture refused |

`engine/tests/live_app.py` drives the **running desktop app**. It played audio through the speakers, captured it with real WASAPI loopback, read a real window by PrintWindow, and saved overlay captures in `engine/tests/out/`. Bugs it found and that are now fixed:
- audio status crash
- SymPy worker deadlock (Windows stdin-pipe inheritance)
- loopback silence gap
- hidden directed questions
- CSS layering

## 2026-10-13 — fixes after first real use

**Laptop freezing, then "hibernating".** The Windows logs show the same sequence twice on 12 Oct: about 2–3 minutes of NVIDIA driver faults (`nvlddmkm` 13, "Graphics FECS Exception"), then Windows tried to sleep, then "failed to resume from hibernate" (0xC000007B), then a hard reset. In the previous 30 days there were no such errors. The student reports the CPU overheats and the fan may not work, so the most likely root cause is **heat under sustained AI load**. Changes:
- New **Low heat** profile, now the default:
  - single-pass transcription;
  - live partial captions every 1.6 s over the last 8 s only (previously the whole growing sentence every 0.7 s);
  - no second-check pass;
  - notes every 5 minutes;
  - fewer topic updates.
- **Thermal guard** (`glass/thermal.py`). It watches GPU temperature and thermal-slowdown flags, CPU throttling, and NVIDIA driver errors in the System log, then steps down:
  - **warm** (78 °C) – less background work;
  - **hot** (85 °C or throttling) – screen reading and notes paused, answers still work;
  - **critical** (91 °C, or any driver error) – capture paused, and the student presses Resume.

  Levels rise immediately and drop only after 45 s of cooling. The overlay explains what happened.
- **Lighter screen reading.** Windows Graphics Capture delivers frames only when the window changes. Live-video areas (webcams) are learnt and ignored, so OCR runs only for new slides or worksheets. Measured on a real Meet call: one OCR in 25 s, 0.2 % CPU.
- The overlay no longer animates endlessly, and window resizes are throttled.

**Snip & Solve made Meet's cameras and screen share grey.** The full-screen selection window made Chrome think Meet was hidden, so it stopped drawing the video. Snip now **freezes the screen first** (Windows Graphics Capture, like the Snipping Tool), shows the frozen image for selection, and solves from it.

Tests: 43/43 unit tests, including the thermal guard with simulated sensors and snipping from a frozen frame. The full GPU scenario harness was **not** re-run this time: the student's laptop has a cooling fault and a live class was open.

## Known limitations (honest)

- **Real classroom audio not tested.** All speech tests used Windows SAPI voices. Real teachers (accents, crosstalk, poor microphones, Meet compression) will reduce accuracy. Add name spellings in Settings → Student if GLASS mishears "Sanad".
- **No speaker diarization** (spec scenario 10). Everything from the PC's audio counts as "Teacher", including other students. Turning on your own microphone labels *your* speech separately.
- **Tab switching** (scenario 12): window capture follows the window, so if Meet and other tabs share one Chrome window, GLASS reads whatever tab is showing. Put Meet in its own window.
- **Screen-share changes** (scenario 13) are handled by change detection plus re-OCR, but there are no automated tests with real screen shares.
- **Teacher corrections** (scenario 14): notes have a "corrections" section and teacher wording overrides inferred definitions, but no dedicated test exists.
- **Resource pressure** (scenario 20): the scheduler, bounded queues and CPU fallback for Whisper are implemented. GPU out-of-memory and pathological load were not stress-tested.
- **Diagram snips are slow on 8 GB GPUs.** Whisper + answer model + vision model don't fit together, so the first diagram snip loads the vision model (~9 s) and GLASS then reloads the answer model in the background (~8 s, before the next question). Toggling **V** in the snip window pre-loads during your drag. The **Quality** profile uses the vision model for everything (no swapping, automatic diagram reading) at slightly lower text-answer quality.
- **4B local models have limits.** They're good at IGCSE-level explanations and the arithmetic is exact (SymPy), but hard multi-step reasoning or literature analysis can be shallow. Balanced/Quality run a second check pass that can correct an answer, and everything shows confidence.
- **Visual check of acrylic on the real screen.** The PC was locked during the UI work, so every visual check used PrintWindow captures, which validate layout and content but not the live DWM blur. If acrylic looks wrong on your screen, Settings → Overlay → Glass style → Solid tint.
- **Global shortcuts** registered without errors, but weren't pressed on a physical keyboard (locked session). The same actions were exercised through the UI and API.
- **Microphone capture** is implemented (separate stream, echo de-duplication) but untested with a live voice.
- **Installer is unsigned**, so Windows SmartScreen will warn on first run.

## Remaining work / next steps

1. Test with consented recordings of real lessons; tune VAD end-silence and name aliases from the results.
2. Speaker diarization (e.g. pyannote or embedding clustering) to separate the teacher from classmates.
3. Per-tab capture via the Chrome tab-capture API (browser extension) to make tab switching safe.
4. A visual "draw the focus area" tool for the screen crop (presets exist today).
5. Startup-at-login option and Start-menu shortcuts that open specific views (`GLASS.exe --view=notes` already works).
6. Code-split the dashboard bundle (KaTeX/markdown) to trim memory.
