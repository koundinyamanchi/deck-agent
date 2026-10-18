"""Stand-in for the faster-whisper package, for testing Deck Agent's voice path on a PC.

  DECK_AGENT_STT_PYTHON=python PYTHONPATH=dev python server.py
"""
import os


class _Segment:
    def __init__(self, text):
        self.text = text


class WhisperModel:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, path, **kwargs):
        size = os.path.getsize(path)
        return [_Segment(f"How much space is left?")] if size else [], {"bytes": size}
