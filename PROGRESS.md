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

## 2026-10-13 — macOS port (target: MacBook Pro 16" 2019, Intel i9, 16 GB, macOS 15)

Built and verified on Windows, **not yet run on a Mac**:
- Platform layer `engine/glass/macos.py`:
  - window and display lists (Quartz),
  - capture via the built-in `screencapture` tool,
  - Apple Vision OCR,
  - microphone and BlackHole input via `sounddevice`,
  - thermal readings via `pmset -g therm`.
- `engine/macos/glass-audio.swift`: a ScreenCaptureKit helper that streams system audio as Float32 PCM. `setup_engine.sh` compiles it.
- **Mac** profile for CPU-only machines:
  - Whisper `small` int8 on 6 threads, no partial re-transcription;
  - `qwen3:1.7b` on Intel (`qwen3:4b-instruct` on Apple Silicon), 4k context, short prompts;
  - notes every 15 minutes and at the end of the lesson, no vision model;
  - the answer prompt is re-warmed in Ollama's cache after background jobs.
- Electron:
  - vibrancy, a non-activating panel over full-screen apps, Retina point coordinates;
  - a Terminal-based setup launcher, a menu-bar template icon, ⌃⌥ shortcuts, a DMG target.
- Every Python dependency was confirmed to have Intel-macOS wheels for Python 3.11 and 3.12. `onnxruntime` is pinned to 1.19.2, the last Intel-Mac build.

Tests:
- `tests/test_macos_port.py` (5/5): Retina scaling, Vision coordinate flip, `pmset` parsing, and a "pretend to be macOS" import of the whole engine with Apple frameworks stubbed. It confirms that every platform switch picks the Mac branch and nothing Windows-only runs on import.
- Windows suite still 43/43.

**Must be checked on the Mac:**
- the Swift helper compiling and capturing (written against the macOS 13+ SDK but never compiled here);
- the permission prompts;
- Vision accuracy on Meet;
- real CPU latency (estimated 4–7 s per answer on the i9);
- the vibrancy look.

## 2026-10-13 — feedback round 2: smarter about what to answer and how

- **Register / attendance** ("Nicki?", "Is Nicki here?", "Here, miss") is never answered. "Sanad?" during the register shows "say here".
- **Context compaction:** the register, tech/logistics chatter, comprehension checks, small talk and verbal fillers are removed from everything the AI sees: answers, catch-up and notes.
- **Ask | Fix tabs** in the overlay. Fix understands commands: *answer it*, *shorter*, *simpler*, *more detail*, *quotes*, *that's wrong*.
- **"That's not an answer" guard:** if the spoken line only describes the question, GLASS regenerates once.
- **Books & notes:** your own texts (PDF/EPUB/DOCX/TXT) are searched for every question (SQLite FTS5). Quotations are verified word-for-word; unverifiable ones are flagged; with no book, quotes are forbidden. Tested on a public-domain *A Christmas Carol* excerpt: real quotes ✓ verified, and the invented quote the model produced *before* this change is now prevented.
- **GLASS asks you:**
  - an `ASK:` line with options when unsure, falling back to the lesson's topics;
  - "Which lesson is this?";
  - "Add your copy of the book?".
- **Feedback 👍/👎** with reasons that become lasting preferences or ignore-rules.
- Heat guard: a single stray NVIDIA "thermal" flag is no longer treated as heat (it needs 3 samples and a warm GPU).

Tests: 97/97 unit tests; harness scenarios incl. register, relevance, quotes (real model).

## 2026-10-14 — feedback round 3: snips, screen questions, topics, clean lesson ends

- **Snip answers are separated per question.** The worksheet text is split into questions and parts (1, 2(a), 2(b)…), handling "2 (a)", "2a)", unpunctuated exam numbering, mark allocations and answer lines. The model answers each under its own label, and the card shows each question as read with its answer indented underneath. Previously the one-line-per-question format ran together, because Markdown joins single line breaks.
- **"What I read" is visible and fixable.** It shows before the answer arrives. **Edit** solves corrected text; **Read again (vision)** re-reads the same selection with the vision model.
- **Maths symbols.** Snips are upscaled harder before OCR, so superscripts survive. All superscript characters are mapped (x¹² → x^12, x⁻¹ → x^(-1)), as are ½ ¼ ¾. "x?" from OCR becomes x^2 on every line, not just Tesseract re-reads. Tokens like "x2" are flagged on the card and in the prompt, never silently rewritten. The model is told not to change any number, sign or power, and the question text shown is GLASS's own reading, not the model's retelling.
- **"answer question 2b" works.** In the Fix tab, "answer" used to be parsed as a command to redo the *previous* answer, and the reference was dropped. References now open that question (with its stem) from the screen or the last snip. Snips are now remembered with their numbering. With nothing to look at, GLASS says how to fix that instead of guessing.
- **Screen reading does something visible.** New questions on the shared screen raise an **On screen** offer (Solve all / per question). Detection is deterministic (no model, no extra heat), and each question is offered once per lesson. Optional auto-solve skips while the laptop is hot.
- **The student can set the topic.** "the topic is concentration", "we're doing moles", "lesson: rates of reaction (chemistry)", or a few words in the Fix tab with nothing to fix. It locks the topic (topic tracking only refines the subtopic), can set the subject, feeds speech-recognition hints, and redoes the current answer with it.
- **Ending a lesson really ends it.** The overlay and Live view clear. In-flight answers, topic tracking and task extraction from the old lesson are cancelled. The final notes pass now runs on its own copy of the ended lesson's memory. Before, it shared memory with the next lesson, so a late topic/subject result could carry the old subject into the new lesson. The new lesson's subject and topic are pushed to the UI immediately, and the screen reader re-reads even an unchanged screen.
- The final notes pass waits for a notes update that is already running. Before, it silently skipped, losing the end of the lesson from the notes.

Tests: 110 unit tests (Linux, CPU only), including splitting, references to parts, powers, topic statements, answer pairing, Fix-box routing, and a start → end → start lifecycle check. Also 5/5 macOS port tests. Not run: the GPU harness and live-app tests, which need the Windows laptop.

## 2026-10-14 — feedback round 4: powers, and maths it can be trusted with

**Powers were still wrong.** Tested with Tesseract on rendered worksheets, OCR returned "x?", "4a°", "2a”", "5?" and "23" for x², 4a³, 2a², 5² and 2³. Which junk appears isn't even consistent, and "4a³" once came back as "4a?". The text can't tell 2 from 3. `glass/superscript.py` now finds exponents in the image:
- connected ink components per OCR line;
- a glyph raised above the baseline, smaller than a full character, taller than wide, right after a base is an exponent;
- each exponent is read enlarged and isolated: Tesseract single-character mode when installed, else the main OCR engine on a sheet of enlarged digits, else comparison with digits drawn in this computer's fonts;
- it is written back as ^n using the OCR word boxes (replacing junk, a flattened digit, or nothing when dropped).

Degrees, quotes, apostrophes, % and * are rejected by shape, and powers only attach to maths-like bases.

Results:
- **Tesseract, all 4 test sheets** (typeset, small, dark slide, Unicode glyphs): 24/24 powers right. The repo's own `worksheet.png` fixture: "x? - 5x" → "x^2 - 5x".
- **Font comparison alone** (no OCR, fonts it hasn't seen): 0 wrong out of hundreds of digits from 9 to 24 px. It says "unsure" for some tiny ones rather than guess.
- **An unreadable exponent** triggers an automatic vision re-read (when available) or a warning naming it.

**Maths quality.** `plan_math` works out each question exactly with SymPy before the model answers: solve (incl. simultaneous equations and inequalities), expand, factorise, simplify, work out (incl. % of, fraction of), f(3) / f(x) = 12 from a stem's f(x), differentiate. It also turns OCR's "4a^3 x 2a^2" back into ×. Results are labelled per question; the model must match them, and the card shows them as **exact**. **Think harder** re-solves with a reasoning model (setting `reasoning_model`, default `qwen3:4b`, the thinking build already on the laptop), optionally for every snip. The answer model is reloaded in the background afterwards.

Tests: 130 unit tests + 5 macOS port tests pass. These include powers read from rendered lines in four OCR failure modes, no false powers on degrees/quotes/%, and 13 exact-maths question types.

Not verified: Windows OCR's own word boxes and digit reading (written to its API, not run). Also not run: the thinking model's latency on the laptop. If Windows OCR skips the enlarged digits, the font comparison still reads them.

## 2026-10-14 — feedback round 5: a real composite-functions slide

The student's slide: "If f(x) = 3x and g(x) = x + 1, determine:" with five answer boxes. GLASS:
- treated it as one question with a bogus "Answer: 6";
- read fg(2) as f(2)×g(2) = 18, when the answer is f(g(2)) = 9;
- read the italic gg(x) as "99 (x)".

Fixed:
- **Fill-in blanks are questions.** Rows like "fg(2) = ?" (including two side by side) become parts (a), (b)… under the shared stem.
- **Functions are understood.** Every definition on the page is parsed, including "f(x) = 3x and g(x) = x + 1". Calls are composed GCSE-style (fg = f∘g, rightmost first), so SymPy gives fg(2) = 9, gf(4) = 13, fg(x) = 3x + 3, gf(x) = 3x + 1 and gg(x) = x + 2. It also handles "Solve fg(x) = 12" and ff(3).
- **Italic g misread as 9** is repaired when g is a function on the page ("99(x)" → gg(x)). A real 9 before a bracket of maths stays a number.
- **The prompt and the maths subject guide state the conventions:** fg(x) = f(g(x)), never a product; f⁻¹ is the inverse.

Tests: 137 unit tests, including the slide's exact OCR text, + 5 macOS port tests.

## 2026-10-14 — feedback round 6: "tell me something smart I can say"

Typed requests all went through the teacher-question pipeline. That pipeline makes the model answer one literal question in a single spoken sentence and looks at only the last 90 s of speech, so "tell me something smart I can say" confused it. Requests are now recognised first:
- **something to say / contribute / participate** → three options grounded in the latest teacher speech (4 min), the slide, the topic and the teacher's last question. They mix building on the teacher's point, a precise key term, and a thoughtful question. Each says why it's strong.
- **a question I can ask** → three good questions for the teacher.
- **what did I miss** → catch-up.
- **what are we talking about** → explanation of what's happening now.

Ordinary typed questions use 3 minutes of speech (was 90 s) plus "what the teacher is doing right now". They are told to interpret the question within the current discussion.

Tests: 152 unit tests (incl. 14 request-recognition cases, and a check that the speech, slide and topic reach the model) + 5 macOS port tests.

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
