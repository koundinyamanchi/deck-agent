#!/usr/bin/env python3
"""Whisper speech-to-text worker for Deck Agent. Runs inside stt-venv (faster-whisper).

Reads one JSON request per line on stdin ({"path": "<audio file>"}) and answers with one
JSON line ({"text": ...} or {"error": ...}). The first line it prints is {"ready": true}.
"""
import json
import os
import sys

try:
    import av
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel(os.environ.get("DECK_AGENT_WHISPER_MODEL", "base.en"),
                         device="cpu", compute_type="int8", cpu_threads=4)
except Exception as e:  # report instead of crashing, so the service can say why
    print(json.dumps({"ready": False, "error": str(e)}), flush=True)
    sys.exit(1)


def load_audio(path):
    """Decode any audio file (webm/opus from the browser, wav, ...) to 16 kHz mono float32.

    faster-whisper has its own loader, but it passes an option that PyAV 15+ removed,
    so the decoding happens here and Whisper gets the samples directly.
    """
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks.extend(f.to_ndarray() for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray() for f in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32) / 32768.0


print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    try:
        audio = load_audio(json.loads(line)["path"])
        segments, _info = model.transcribe(audio, language="en", beam_size=1, vad_filter=True)
        print(json.dumps({"text": " ".join(s.text.strip() for s in segments).strip()}), flush=True)
    except Exception as e:
        print(json.dumps({"error": str(e)}), flush=True)
