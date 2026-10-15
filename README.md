# GLASS — real-time classroom copilot for Windows 11

GLASS listens to an online lesson (Google Meet or anything else playing on the PC), reads the window you choose, and keeps a small glass overlay up to date. When the teacher asks a question it shows a short answer you could say out loud, plus an explanation if you want it. It also writes revision notes, tracks the work you're set, and solves questions you select on screen.

Everything runs **locally on your GPU**: no accounts, no API keys, no subscriptions, and nothing is uploaded. The only network use is the one-time download of the open models during setup.

> GLASS is a learning aid. It suggests; you decide what to say. It never speaks, types or acts in the meeting, and it only captures between **Start lesson** and **End lesson**. Check that your school and teacher allow it, and that other participants have agreed where that's required.

## Quick start

**Developer checkout (this folder):**

```bash
npm install
```
```bash
node node_modules/electron/install.js
```
```bash
npm run build
```
```bash
npm start
```

The Python engine lives in `engine/.venv` (already created on this PC). On a new PC, run `engine\setup_engine.ps1` first.

**Installer:** `npm run dist` produces `release\GLASS Setup 0.1.0.exe`. On first launch GLASS shows **Set up engine**. That opens a PowerShell window which installs Python 3.11 (if needed), the engine packages, Ollama, Tesseract (optional), and downloads the models: Whisper large-v3-turbo ≈1.6 GB, qwen3:4b-instruct ≈2.5 GB, qwen3-vl:4b-instruct ≈3.3 GB.

On this PC the installed app already works: `%LOCALAPPDATA%\GLASS\venv` is a link to `engine\.venv`. If you move or delete this folder, re-run setup.

## Updating

**Settings → Updates → Update now.** GLASS checks GitHub every few hours, and the overlay shows an **Update** chip when there's a new version. Updating:
- downloads only what changed;
- reinstalls app or engine packages only if their lists changed;
- rebuilds the screens and restarts.

Your notes, books and settings are kept. It won't update during a lesson. If you edited files on this PC, they're backed up first (`git stash`).

The installed app runs the code from the GLASS folder it was built from, so updating that folder updates the app; there's no need to reinstall. One exception: an update that changes Electron itself. GLASS says so, and you run `npm run dist` once and install it.

**One time only**, to move to this system from an older version, in your GLASS folder:

```bash
git pull
```
```bash
npm install && npm run dist
```

Then install `release\GLASS Setup 0.1.0.exe` (on a Mac: `npm run dist:mac` and the `.dmg` in `release`). From then on, updates come from the button.

**Mac:** macOS stops apps reading **Documents, Desktop and Downloads** until you allow it. If your GLASS folder is in one of those, either:
- allow GLASS in System Settings → Privacy & Security → Files and Folders; or
- keep the folder in your home folder (e.g. `~/GLASS`) and run `npm run dist:mac` there once.

Until then GLASS still starts normally, from its built-in copy, and Settings → Updates explains what's blocked.

## macOS (13 Ventura or newer, Intel or Apple Silicon)

```bash
npm install
```
```bash
node node_modules/electron/install.js
```
```bash
bash engine/setup_engine.sh
```
```bash
npm run build && npm start
```

`setup_engine.sh` needs Python 3.11 (python.org installer) and the Xcode Command Line Tools (it offers to install them). It installs the engine, builds the small system-audio helper, installs Ollama if needed and downloads the models. `npm run dist:mac` builds a `.dmg`; it has to be run on a Mac.

The first time GLASS listens, macOS asks to allow **Screen & System Audio Recording**. Allow it, then restart GLASS.

How the Mac version differs:
- **Audio:** macOS's own system-audio capture (ScreenCaptureKit) instead of Windows loopback. A BlackHole device also works.
- **Screen and text:** the built-in `screencapture` tool for screen reading and Snip, and **Apple Vision** for text recognition.
- **Speed:** Whisper and the AI run on the CPU on Intel Macs. The **Mac** profile uses `qwen3:1.7b`, short prompts, and writes notes at the end of the lesson. Expect answers in roughly 4–7 s on a 2019 Intel i9; exact maths stays instant. Apple Silicon Macs use `qwen3:4b-instruct` and are faster.
- **No diagram reading** on Intel Macs: a vision model is too slow on a CPU. Snip still reads typed questions.
- **Heat protection** uses macOS's own CPU speed-limit readings.
- **Shortcuts** use ⌃⌥ (Control+Option). "Type context" is ⌃⌥K, because ⌃⌥Space switches keyboard input source on macOS.

## Using it

1. Open GLASS → Settings → **Privacy** and confirm you have permission.
2. Optional: Settings → **Screen**. Turn on screen reading and pick the Google Meet window. Use "Skip browser bar and Meet controls" for cleaner text.
3. Click **Start lesson** (pick the subject, or let GLASS detect it).
4. Leave it running. The overlay changes with the lesson:
   - **Passive**: the current topic and a one-line summary.
   - **Question**: appears once the teacher is actually waiting for an answer, so rhetorical questions never pop up.
   - **"You were asked"** (amber): your name was heard. It shows immediately, before the teacher finishes the sentence.
   - **Task**: page, questions, time allowed, and where to start.
   - **Pill**: shrinks when nothing is happening. It can be click-through.
5. Not sure what the teacher means? Press **Ctrl+Alt+Space** and type two or three words (`natural selection`, `question 4`, `the graph`). The answer is redone with that context.

**The text box has two tabs** (press Tab to switch):
- **Ask:** any new question about the lesson (it looks at the last few minutes and the slide, so "what does that mean?" works). *answer question 2b* solves that question from the screen or your last snip.
  - *tell me something smart I can say* / *what can I add* → three things you could say right now, built on what the teacher just said, each with why it's a strong point.
  - *a good question I can ask* → thoughtful questions for the teacher about the current point.
  - *what did I miss* → catch-up; *what are we talking about* → explanation of what's happening now.
- **Fix:** the answer on screen. Add context ("natural selection", "question 4") or tell it what to do: *answer it*, *shorter*, *simpler*, *more detail*, *quotes*, *that's wrong*.
- **Tell it the topic** in either tab: *the topic is concentration*, *we're doing moles*, *this lesson is about natural selection (biology)*. The topic sticks for the whole lesson, and the answer on screen is redone with it.

**Snip & Solve** shows every question it read, each with its own answer underneath.
- **Powers are read from the image**, not guessed from OCR text. Text recognisers turn x² into "x?", "x°", "x2" or nothing. GLASS finds the small raised digits itself, reads each one enlarged, and shows "Read powers: x², (x + 3)²". If one can't be read, it re-reads the selection with the vision model or tells you which one to check.
- **Exact maths:** solve, expand, factorise, simplify, work out, f(3) from a given f(x), simultaneous equations, inequalities, percentages and differentiation are computed by SymPy for each question (green **exact**). The AI explains the answer; it doesn't do the algebra.
- **Think harder** re-solves with a reasoning model (`qwen3:4b` by default; Settings → AI models). It's slower, much better on hard multi-step questions, and can be switched on for every snip.
- **Edit** fixes anything misread and solves again; **Read again (vision)** re-reads it with the vision model.

**Questions on screen:** with screen reading on, GLASS notices a new worksheet or question on the shared screen and offers **Solve all** or one question at a time. Settings → Screen → *Solve questions as they appear* answers them without asking.

**End lesson** clears the overlay and the Live view; the next lesson starts with the subject you pick.

**Books and notes (Subjects → Books & notes):** add your own copy of a set text, revision guide or notes (PDF, EPUB, Word, text). GLASS answers from it and checks every quotation word-for-word against it. A ✓ means the quote was found in your book. Without the book, GLASS won't give quotes at all; it names the moment to quote instead.

**GLASS asks you** when it's unsure (which lesson this is, what "that" means, whether to add the book), with one-tap answers. **👍 / 👎** teaches it: "Not a real question" ignores similar questions from then on, and "Too long" makes future answers shorter.

**Off-topic speech is kept out:** the register, "can you hear me", tech trouble and small talk are greyed out in the transcript and never reach the AI.

| Shortcut (changeable in Settings) | Action |
|---|---|
| Ctrl+Alt+G | Show / hide the overlay (expands the pill) |
| Ctrl+Alt+Space | Type context or a question |
| Ctrl+Alt+A | Quick → Explain → Detailed |
| Ctrl+Alt+S | Snip & solve (drag around any question; press **V** for diagrams/handwriting) |
| Ctrl+Alt+E | Explain what's being discussed |
| Ctrl+Alt+M | What did I miss? (last 3 minutes) |
| Ctrl+Alt+T | Current task |
| Ctrl+Alt+N | Notes |
| Ctrl+Alt+P | Pause / resume capture |

## How it works

```
 Electron (overlay · dashboard · snip)  ⇄  WebSocket, 127.0.0.1, per-launch token  ⇄  Python engine
                                                                                        │
  WASAPI loopback ─► Silero VAD ─► Whisper large-v3-turbo (CUDA) ─► name + question detectors
  (+ optional mic)    32 ms frames    partials ~0.7 s, finals          │
                                                                      ▼
  Window capture ─► change detection ─► Windows OCR (+Tesseract     Answer engine ──► SymPy (exact maths,
  (PrintWindow)      tile diff           re-read of maths lines)      │                 sandboxed process)
                                                                      ▼
                              Priority scheduler (one GPU slot): direct question > answer > task > vision > notes
                                                                      │
                                     Ollama: qwen3:4b-instruct (answers)  ·  qwen3-vl:4b-instruct (vision)
                                                                      │
                                        3-layer memory: last 90 s · lesson state/notes · past lessons (SQLite FTS5)
```

- **Answers stream in a strict line format** (question, type, confidence, assumption, CALC, SAY, WHY). The overlay renders progressively. If the model writes a `CALC:` line, generation pauses, SymPy computes the exact value, and the result is injected before the answer sentence. The model never does arithmetic for you. Equations that appear in the question or on screen are solved *before* the model starts (≈40 ms).
- **Uncertainty is explicit.** Every answer carries a confidence level and its assumption. Vague questions ("why does that happen?") are flagged, and the suggested line becomes a clarifying question rather than a guess. Blank captures are refused before any model sees them.
- **Background work never delays answers.** Notes, topic tracking and task extraction are pre-emptible, coalesced and bounded, and a question cancels them instantly.
- **Subject-aware.** Each subject has its own answer rules: maths method + answer, physics equation + units, biology mark-scheme keywords, English with no invented quotations, business application + evaluation, and Arabic answers in Arabic.

Code map: `engine/glass/` — `audio/` (capture, VAD, ASR), `detect.py`, `answer.py`, `intel.py` (notes/tasks/state), `screen.py`, `mathsolve.py`, `memory.py`, `llm.py` (Ollama + scheduler), `engine.py` (orchestration), `server.py` (API). UI: `src/overlay`, `src/dashboard`, `src/snip`. Shell: `electron/`.

## Privacy

- Capture runs only during a lesson you started. The lumen line and status dot show it, and the eye icon shows screen reading.
- Raw audio and screenshots are never written to disk. Transcript text is saved for your notes; you can turn that off.
- Only the window you choose is captured. With PrintWindow, overlapping windows are never included. You can black out areas (e.g. a chat panel) before anything is read.
- Retention: keep forever, or auto-delete after 7–365 days. Lessons can be deleted individually.
- The engine only accepts connections from this PC that carry the per-launch token, so a web page can't read your lesson.

## Tests

```bash
cd engine && .venv/Scripts/python.exe -m pytest -q tests/test_units.py
```
```bash
cd engine && .venv/Scripts/python.exe -m tests.harness
```

The harness drives the **real** pipeline (VAD → Whisper on GPU → detectors → LLM → SymPy → notes/tasks) with synthesized classroom audio (`tests/make_audio.py`) and rendered worksheets (`tests/make_screens.py`), and writes `tests/report.md`. `tests/live_app.py` drives the running desktop app through real speakers → WASAPI loopback. See [PROGRESS.md](PROGRESS.md) for results, measured latency, and known limitations.
