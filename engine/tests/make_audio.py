"""Synthesise classroom test audio with the Windows SAPI voices (offline).

Usage: python -m tests.make_audio  -> writes tests/fixtures/audio/*.wav
Each scenario is spoken text; the harness feeds the WAV through the *real*
VAD -> Whisper -> detector -> LLM pipeline, so nothing downstream is faked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "audio"

SCENARIOS = {
    "direct_name": "Okay everyone, so we said that bonds breaking takes in energy. Sanad, can you explain why combustion is exothermic?",
    "direct_name_late": "So looking at the equation on the board, what's the value of x, Sanad?",
    "general_question": "Can anyone tell me what the powerhouse of the cell is?",
    "rhetorical": "So why does this matter? Well, because without enzymes these reactions would be far too slow. Does that make sense?",
    "screen_question": "Right, can you solve question three for me?",
    "task": "Okay, open your textbooks to page 45 and do questions 1 through 6. You have fifteen minutes.",
    "task_change": "Actually, forget page 45. Instead do the worksheet, questions 2 to 4, for homework, due on Monday.",
    "explain": "Natural selection is the process where organisms with characteristics better suited to their environment are more likely to survive and reproduce, passing on their alleles to the next generation.",
    "equation_spoken": "The equation for speed is distance divided by time, so if a car travels 120 metres in 8 seconds, what's its speed?",
    "vague": "Sanad, why does that happen?",
    "silence": "",
    "classroom_talk": "Can everyone see my screen? Hmm, where is it? No questions? Okay. What does RAM stand for?",
    "register": "Good morning everyone. Nicki? Nicki? Is Nicki here? Omar? Here, miss. Sanad?",
    "two_questions": "What does CPU stand for? Actually, never mind, what does ROM stand for?",
}


def synth(text: str, path: Path, rate: int = 0) -> None:
    import win32com.client

    voice = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = 22  # SAFT22kHz16BitMono
    stream.Format = fmt
    stream.Open(str(path), 3, False)  # SSFMCreateForWrite
    voice.AudioOutputStream = stream
    voice.Rate = rate
    voice.Speak(text or "<silence msec='3000'/>", 8 if not text else 0)  # 8 = SVSFIsXML
    stream.Close()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in SCENARIOS.items():
        synth(text, OUT / f"{name}.wav")
    (OUT / "scenarios.json").write_text(json.dumps(SCENARIOS, indent=2))
    print(f"wrote {len(SCENARIOS)} clips to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
