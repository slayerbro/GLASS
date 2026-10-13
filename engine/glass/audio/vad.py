"""Streaming Silero VAD (v6 ONNX bundled with faster-whisper), ~0.1 ms/frame on CPU."""
from __future__ import annotations

import os

import numpy as np
import onnxruntime as ort

CONTEXT = 64


class SileroVAD:
    def __init__(self) -> None:
        import faster_whisper

        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.context = np.zeros(CONTEXT, np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        """frame: 512 float32 samples at 16 kHz. Returns speech probability."""
        x = np.concatenate([self.context, frame]).reshape(1, -1).astype(np.float32, copy=False)
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = frame[-CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])
