# GLASS integration report — 2026-10-13 13:24

GPU: NVIDIA GeForce RTX 4070 Laptop GPU · ASR: large-v3-turbo on cuda · LLM: qwen3:4b-instruct

## ✅ Relevance + newest question wins
- PASS — only the real question got an answer: ['What does RAM stand for?'] | heard: ['Can everyone see my screen?', 'Hmm, where is it?', 'No questions', 'What does RAM stand for?']
- PASS — answer is the expansion: RAM stands for Random Access Memory.
- PASS — newer question answered (read-only memory): ROM stands for Read Only Memory.
- metrics: `{"speech_end_to_answer_s": 1.8, "newer_question_answer_s": 1.92, "newer_question_queued_ms": 1}`

## ✅ 3. Rhetorical question / comprehension check
- PASS — answer panel never elevated: ['passive']
- PASS — question demoted as self-answered/rhetorical: []

## ✅ 1. Teacher addresses Sanad directly
- PASS — name detected (high): [1.0]
- PASS — answer generated: Combustion is exothermic because the bonds in the products (like CO₂ and H₂O) are stronger than the bonds in the reactants (like fuel and ox
- PASS — marked as directed
- PASS — explanation type: explanation
- PASS — mentions bonds/energy
- PASS — teacher-waiting detected after silence
- PASS — overlay mode = direct: direct
- metrics: `{"speech_end_to_answer_s": 2.76, "llm_say_ms": 2072, "attention_after_speech_end_s": 0.64}`

## ✅ 2. General class question
- PASS — answer generated: The powerhouse of the cell is the mitochondria.
- PASS — not marked as directed
- metrics: `{"speech_end_to_answer_s": 1.89}`
