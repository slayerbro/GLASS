# GLASS integration report — 2026-10-13 14:10

GPU: NVIDIA GeForce RTX 4070 Laptop GPU · ASR: large-v3-turbo on cuda · LLM: qwen3:4b-instruct

## ✅ Register: no answers, 'say here' for Sanad, kept out of context
- PASS — no answer cards: [] | heard: ['Good morning everyone.', 'Nicky', 'Is Nicky here?', 'Omar', 'Here, Miss Sanad']
- PASS — Sanad told to answer the register
- PASS — nothing from the register in the AI context: ''

## ✅ Relevance + newest question wins
- PASS — only the real question got an answer: ['What does RAM stand for?'] | heard: ['Can everyone see my screen?', 'Hmm, where is it?', 'No questions', 'What does RAM stand for?']
- PASS — answer is the expansion: RAM stands for Random Access Memory.
- PASS — newer question answered (read-only memory): ROM stands for Read Only Memory.
- metrics: `{"speech_end_to_answer_s": 1.32, "newer_question_answer_s": 1.41, "newer_question_queued_ms": 0}`

## ✅ 1. Teacher addresses Sanad directly
- PASS — name detected (high): [1.0]
- PASS — answer generated: Combustion is exothermic because the bonds in the products (like CO₂ and H₂O) are stronger than the bonds in the reactants (like fuel and ox
- PASS — marked as directed
- PASS — explanation type: explanation
- PASS — mentions bonds/energy
- PASS — teacher-waiting detected after silence
- PASS — overlay mode = direct: direct
- metrics: `{"speech_end_to_answer_s": 1.83, "llm_say_ms": 1520, "attention_after_speech_end_s": 0.25}`

## ✅ 16/17. Vague question -> uncertainty -> instant context
- PASS — flags uncertainty (low conf or UNSURE): conf=low unsure="that" refers to an event or phenomenon not specified in the current context
- PASS — re-answered with the student's context
- PASS — answer now about survival/reproduction: Because organisms with traits better suited to their environment are more likely to survive and pass on those traits to their offspring.Natural selection works 
- PASS — confidence improved: high
- metrics: `{"context_to_answer_ms": 1084}`

## ✅ 4. Question refers to something on screen (fusion)
- PASS — OCR found numbered items: Solve 2x + 7 = 19
- PASS — resolved to on-screen question 3: ['sympy', 'screen', 'screen item']
- PASS — verified exact result x = 6: x = 6
- PASS — spoken answer says six: Subtract seven from both sides, then divide by two, so x equals six.
- metrics: `{"verified_ms": 82, "speech_end_to_answer_s": 2.72}`

## ✅ 7+8. Teacher assigns work, then changes the task
- PASS — task detected: Textbook page 45, questions 1–6
- PASS — page 45: page 45
- PASS — questions 1–6: 1–6
- PASS — 15 minutes allowed: 15
- PASS — timer not auto-started (start not stated)
- PASS — change detected as replacement: replace
- PASS — homework due Monday: Monday
- PASS — old task kept in history

## ✅ 9/15/18. Explanation -> notes + catch-up
- PASS — notes contain a definition of natural selection: natural selection
- PASS — notes produced in reasonable time: 10.6s
- PASS — catch-up summary produced: - Natural selection is defined as the process where organisms with traits better suited to their environment are more likely to survive and reproduce, passing o
- metrics: `{"notes_s": 10.6}`
