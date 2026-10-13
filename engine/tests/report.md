# GLASS integration report — 2026-10-11 19:40

GPU: NVIDIA GeForce RTX 4070 Laptop GPU · ASR: large-v3-turbo on cuda · LLM: qwen3:4b-instruct

## ✅ 1. Teacher addresses Sanad directly
- PASS — name detected (high): [0.75, 1.0]
- PASS — answer generated: Combustion is exothermic because bonds in the fuel and oxygen are broken, and new bonds in the products like carbon dioxide and water are fo
- PASS — marked as directed
- PASS — explanation type: explanation
- PASS — mentions bonds/energy
- PASS — teacher-waiting detected after silence
- PASS — overlay mode = direct: direct
- metrics: `{"speech_end_to_answer_s": 1.65, "llm_say_ms": 1420, "attention_after_speech_end_s": -3.3}`

## ✅ 2. General class question
- PASS — answer generated: The powerhouse of the cell is the mitochondria.
- PASS — not marked as directed
- metrics: `{"speech_end_to_answer_s": 1.7}`

## ✅ 3. Rhetorical question / comprehension check
- PASS — answer panel never elevated: ['passive']
- PASS — question demoted as self-answered/rhetorical: []

## ✅ 4. Question refers to something on screen (fusion)
- PASS — OCR found numbered items: Solve 2x + 7 = 19
- PASS — resolved to on-screen question 3: ['sympy', 'screen', 'screen item', 'transcript']
- PASS — verified exact result x = 6: x = 6
- PASS — spoken answer says six: x equals six
- metrics: `{"verified_ms": 65, "speech_end_to_answer_s": 1.69}`

## ✅ 5. Teacher introduces an equation verbally (calculation)
- PASS — calculation verified by SymPy: calc=120 / 8 result=15
- PASS — answer is 15 m/s: The speed is 15 meters per second.
- metrics: `{"speech_end_to_answer_s": 1.85}`

## ✅ 6. Equation written on screen, not read aloud
- PASS — solves x^2 - 5x + 6 = 0 exactly: x = 2, x = 3

## ✅ 7+8. Teacher assigns work, then changes the task
- PASS — task detected: Textbook page 45, questions 1–6
- PASS — page 45: page 45
- PASS — questions 1–6: 1–6
- PASS — 15 minutes allowed: 15
- PASS — timer not auto-started (start not stated)
- PASS — change detected as replacement: replace
- PASS — homework due Monday: Monday
- PASS — old task kept in history

## ✅ 16/17. Vague question -> uncertainty -> instant context
- PASS — flags uncertainty (low conf or UNSURE): conf=low unsure=what "that" refers to (no context provided)
- PASS — re-answered with the student's context
- PASS — answer now about survival/reproduction: Because organisms with helpful traits are more likely to survive and pass on those traits.Natural selection happens because individuals with beneficial traits a
- PASS — confidence improved: high
- metrics: `{"context_to_answer_ms": 838}`

## ✅ 9/15/18. Explanation -> notes + catch-up
- PASS — notes contain a definition of natural selection: natural selection
- PASS — notes produced in reasonable time: 8.2s
- PASS — catch-up summary produced: - Natural selection is defined as the process where organisms with traits better suited to their environment are more likely to survive and reproduce, passing o
- metrics: `{"notes_s": 8.2}`

## ✅ 11. Audio stops (silence) — no hallucinated speech
- PASS — no transcript produced from silence/noise: []

## ✅ 19. Small worksheet text OCR
- PASS — numbered item recovered: 'a) Calculate the speed of a car that travels 120 m in 8 s.\nb) State the unit of speed'
- PASS — 120 m and 8 s readable: Q3 a) Calculate the speed of a car that travels 120 m in 8 s. | b) State the unit of speed

## ✅ Snip & solve: worksheet (OCR), diagram (vision), blank capture
- PASS — OCR path: all three answers present (x=5, x=5, x=6): 1. Add 5 to both sides, then divide by 3, so x equals 5.   2. Divide both sides by 4, then subtract 1, so x equals 5.   3. Subtract 7 from both sides, then divide by 2, so x equals 6.   4. Subtract 2x
- PASS — OCR path: verified by SymPy: 3x -5 = 10 → x = 5; 4(x + 1) = 24 → x = 5; 2x + 7 = 19 → x = 6; 5x-3 = 2x + 9 → x = 4
- PASS — vision path: reads the diagram and finds x = 10: **7.** x = 10 cm — Use Pythagoras: x² = 8² + 6², so x = √(64 + 36) = √100 = 10
- PASS — blank image refused (no invented content): The selection couldn't be read. Try selecting a larger, sharper area.
- metrics: `{"ocr_snip_s": 2.34, "vision_snip_s": 39.6, "next_answer_after_vision_s": 2.77}`
